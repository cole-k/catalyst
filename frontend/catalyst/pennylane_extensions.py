# Copyright 2022-2023 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import uuid
import functools
import numbers

import jax
import jax.numpy as jnp
from jax import ShapedArray
from jax.tree_util import tree_flatten, tree_unflatten, treedef_is_leaf
from jax.linear_util import wrap_init
from jax._src.lax.control_flow import _initial_style_jaxpr, _initial_style_jaxprs_with_common_consts
from jax._src.lax.lax import _abstractify

import pennylane as qml
from pennylane.operation import Operation, Wires, AnyWires
from pennylane.measurements import MidMeasureMP

import catalyst.jax_primitives as jprim
from catalyst.jax_tape import JaxTape
from catalyst.jax_tracer import trace_quantum_tape, insert_to_qreg, get_traceable_fn
from catalyst.utils.patching import Patcher
from catalyst.utils.tracing import TracingContext


class QFunc:
    """A device specific quantum function.

    Args:
        qfunc (Callable): the quantum function
        shots (int): How many times the circuit should be evaluated (or sampled) to estimate
            the expectation values
        device (a derived class from QubitDevice): a device specification which determines
            the valid gate set for the quantum function
    """

    def __init__(self, fn, device):
        self.func = fn
        self.device = device
        functools.update_wrapper(self, fn)

    def __call__(self, *args, **kwargs):
        if isinstance(self, qml.QNode):
            if self.device.short_name != "lightning.qubit":
                raise TypeError(
                    "Only the lightning.qubit device is supported for compilation at the moment."
                )
            device = QJITDevice(self.device.shots, self.device.wires)
        else:
            # Allow QFunc to still be used by itself for internal testing.
            device = self.device

        traceable_fn = get_traceable_fn(self.func, device)
        jaxpr = jax.make_jaxpr(traceable_fn)(*args)
        x = lambda *args: jax.core.eval_jaxpr(jaxpr.jaxpr, jaxpr.consts, *args)
        wrapped = wrap_init(x)
        retval = jprim.func_p.bind(wrapped, *args, fn=self)
        return retval


def qfunc(num_wires, *, shots=1000, device=None):
    """A Device specific quantum function.

    Args:
        num_wires (int): the number of wires
        fn (Callable): the quantum function
        shots (int): How many times the circuit should be evaluated (or sampled) to estimate
            the expectation values. Defaults to 1000
        device (a derived class from QubitDevice): a device specification which determines
            the valid gate set for the quantum function. Defaults to ``QJITDevice`` if not
            specified

    Returns:
        Grad: A QFunc object that denotes the the declaration of a quantum function.

    """
    if not device:
        device = QJITDevice(shots=shots, wires=num_wires)

    def dec_no_params(fn):
        return QFunc(fn, device)

    return dec_no_params


class Function:
    def __init__(self, grad):
        """An object that represents a compiled function.

        At the moment, it is only used to compute sensible names for higher order derivative
        functions in MLIR.
        """
        self.fn = grad
        self.__name__ = "grad." + grad.__name__

    def __call__(self, *args, **kwargs):
        jaxpr = jax.make_jaxpr(self.fn)(*args)
        x = lambda *args: jax.core.eval_jaxpr(jaxpr.jaxpr, jaxpr.consts, *args)
        return jprim.func_p.bind(wrap_init(x), *args, fn=self)


class Grad:
    def __init__(self, fn, *, method, h, argnum):
        """An object that specifies that a function will be differentiated.

        Args:
            fn: the function to differentiate
            method: the method used for differentiation
            h: the step-size value for the finite difference method
            argnum: the argument indices which define over which arguments to differentiate

        Raises:
            AssertionError: Higher-order derivatives can only be computed with the finite difference
                            method.
        """
        self.fn = fn
        self.__name__ = fn.__name__
        self.method = method
        self.h = h
        self.argnum = argnum
        if self.method != "fd":
            assert isinstance(
                self.fn, qml.QNode
            ), "Only finite difference can compute higher order derivatives."

    def __call__(self, *args, **kwargs):
        """Specifies that an actual call to the differentiated function.
        Args:
            args: the arguments to the differentiated function
        """
        jaxpr = jax.make_jaxpr(self.fn)(*args)
        assert len(jaxpr.eqns) == 1, "Grad is not well defined"
        assert (
            jaxpr.eqns[0].primitive == jprim.func_p
        ), "Attempting to differentiate something other than a function"
        return jprim.grad_p.bind(
            *args, jaxpr=jaxpr, fn=self, method=self.method, h=self.h, argnum=self.argnum
        )


def grad(f, *, method=None, h=None, argnum=None):
    """A qjit compatible grad transformation for PennyLane.

    Args:
        f (Callable): the function to differentiate
        method (str): the method used for differentiation (any of ``["fd", "ps", "adj"]``)
        h (float): the step-size value for the finite-difference (fd) method
        argnum (int, List(int)): the argument indices to differentiate

    Returns:
        Grad: A Grad object that denotes the derivative of a function.

    Raises:
        AssertionError: Invalid method or step size parameters.
    """
    if method is None:
        method = "fd"
    assert method in {"fd", "ps", "adj"}, "invalid differentiation method"
    if method == "fd" and h is None:
        h = 1e-7
    assert h is None or isinstance(h, numbers.Number), "invalid h value"
    if argnum is None:
        argnum = [0]
    elif isinstance(argnum, int):
        argnum = [argnum]

    if isinstance(f, qml.QNode):
        return Grad(f, method=method, h=h, argnum=argnum)
    else:
        return Grad(Function(f), method=method, h=h, argnum=argnum)


class Cond(Operation):
    num_wires = AnyWires

    def __init__(
        self, pred, consts, true_jaxpr, false_jaxpr, args_tree, out_trees, *args, **kwargs
    ):
        self.pred = pred
        self.consts = consts
        self.true_jaxpr = true_jaxpr
        self.false_jaxpr = false_jaxpr
        self.args_tree = args_tree
        self.out_trees = out_trees
        kwargs["wires"] = Wires(Cond.num_wires)
        super().__init__(*args, **kwargs)


class CondCallable:
    """
    Some code in this class has been adapted from the cond implementation in the JAX project at
    https://github.com/google/jax/blob/jax-v0.4.1/jax/_src/lax/control_flow/conditionals.py
    released under the Apache License, Version 2.0, with the following copyright notice:

    Copyright 2021 The JAX Authors.
    """

    def __init__(self, pred, true_fn):
        self.pred = pred
        self.true_fn = true_fn
        self.false_fn = lambda: None

    def otherwise(self, false_fn):
        assert (
            false_fn.__code__.co_argcount == 0
        ), "conditional 'False' function is not allowed to have any arguments"
        self.false_fn = false_fn
        return self

    @staticmethod
    def check_branches_return_types(true_jaxpr, false_jaxpr):
        if true_jaxpr.out_avals[:-1] != false_jaxpr.out_avals[:-1]:
            raise TypeError(
                "Conditional branches require the same return type, got:\n"
                f" - True branch: {true_jaxpr.out_avals[:-1]}\n"
                f" - False branch: {false_jaxpr.out_avals[:-1]}\n"
                "Please specify an else branch if none was specified."
            )

    def call_with_quantum_ctx(self, ctx):
        def new_true_fn(qreg):
            with JaxTape(do_queue=False) as tape:
                with tape.quantum_tape:
                    out = self.true_fn()
                tape.set_return_val(out)
                new_quantum_tape = JaxTape.device.expand_fn(tape.quantum_tape)
                tape.quantum_tape = new_quantum_tape
                tape.quantum_tape.jax_tape = tape

            return_values, qreg, qubit_states = trace_quantum_tape(
                tape, qreg, has_tracer_return_values=out is not None
            )
            qreg = insert_to_qreg(qubit_states, qreg)

            return return_values, qreg

        def new_false_fn(qreg):
            with JaxTape(do_queue=False) as tape:
                with tape.quantum_tape:
                    out = self.false_fn()
                tape.set_return_val(out)
                new_quantum_tape = JaxTape.device.expand_fn(tape.quantum_tape)
                tape.quantum_tape = new_quantum_tape
                tape.quantum_tape.jax_tape = tape

            return_values, qreg, qubit_states = trace_quantum_tape(
                tape, qreg, has_tracer_return_values=out is not None
            )
            qreg = insert_to_qreg(qubit_states, qreg)

            return return_values, qreg

        args, args_tree = tree_flatten((jprim.Qreg(),))
        args_avals = tuple(map(_abstractify, args))

        (true_jaxpr, false_jaxpr), consts, out_trees = _initial_style_jaxprs_with_common_consts(
            (new_true_fn, new_false_fn), args_tree, args_avals, "cond"
        )

        CondCallable.check_branches_return_types(true_jaxpr, false_jaxpr)
        Cond(self.pred, consts, true_jaxpr, false_jaxpr, args_tree, out_trees)

        # Create tracers for any non-qreg return values (if there are any).
        ret_vals, _ = tree_unflatten(out_trees[0], true_jaxpr.out_avals)
        a, t = tree_flatten(ret_vals)
        return ctx.jax_tape.create_tracer(t, a)

    def call_with_classical_ctx(self):
        args, args_tree = tree_flatten([])
        args_avals = tuple(map(_abstractify, args))

        (true_jaxpr, false_jaxpr), consts, out_trees = _initial_style_jaxprs_with_common_consts(
            (self.true_fn, self.false_fn), args_tree, args_avals, "cond"
        )

        CondCallable.check_branches_return_types(true_jaxpr, false_jaxpr)

        inputs = [self.pred] + consts
        ret_tree_flat = jprim.qcond(true_jaxpr, false_jaxpr, *inputs)
        return tree_unflatten(out_trees[0], ret_tree_flat)

    def __call__(self):
        TracingContext.check_is_tracing("Must use 'cond' inside tracing context.")

        ctx = qml.QueuingManager.active_context()
        if ctx is None:
            return self.call_with_classical_ctx()
        else:
            return self.call_with_quantum_ctx(ctx)


def cond(pred):
    """A qjit compatible decorator for if-else conditionals in PennyLane/Catalyst.

    This form of control flow is a functional version of the traditional if-else conditional. This
    means that each execution path, a 'True' branch and a 'False' branch, is provided as a separate
    function. Both functions will be traced during compilation, but only one of them the will be
    executed at runtime, depending of the value of a Boolean predicate. The JAX equivalent is the
    ``jax.lax.cond`` function, but this version is optimized to work with quantum programs in
    PennyLane.

    Values produced inside the scope of a conditional can be returned to the outside context, but
    the return type signature of each branch must be identical. If no values are returned, the
    'False' branch is optional. Refer to the example below to learn more about the syntax of this
    decorator.

    Args:
        pred (bool): the predicate with which to control the branch to execute

    Returns:
        A callable decorator that wraps the 'True' branch of the conditional.

    Raises:
        ValueError: Called outside the tape context.
        AssertionError: True- or False-branch functions cannot have arguments.

    **Example**

    .. code-block:: python

        @cond(predicate: bool)
        def conditional_fn():
            # do something when the predicate is true
            return "optionally return some value"

        @conditional_fn.otherwise
        def conditional_fn():
            # optionally define an alternative execution path
            return "if provided, return types need to be identical in both branches"

        ret_val = conditional_fn()  # must invoke the defined function
    """

    def decorator(true_fn):
        assert (
            true_fn.__code__.co_argcount == 0
        ), "conditional 'True' function is not allowed to have any arguments"
        return CondCallable(pred, true_fn)

    return decorator


class WhileLoop(Operation):
    num_wires = AnyWires

    def __init__(
        self,
        iter_args,
        body_jaxpr,
        cond_jaxpr,
        cond_consts,
        body_consts,
        body_tree,
        *args,
        **kwargs,
    ):
        self.iter_args = iter_args
        self.body_jaxpr = body_jaxpr
        self.cond_jaxpr = cond_jaxpr
        self.cond_consts = cond_consts
        self.body_consts = body_consts
        self.body_tree = body_tree
        kwargs["wires"] = Wires(WhileLoop.num_wires)
        super().__init__(*args, **kwargs)


class WhileCallable:
    """
    Some code in this class has been adapted from the while loop implementation in the JAX project at
    https://github.com/google/jax/blob/jax-v0.4.1/jax/_src/lax/control_flow/loops.py
    released under the Apache License, Version 2.0, with the following copyright notice:

    Copyright 2021 The JAX Authors.
    """

    def __init__(self, cond_fn, body_fn):
        self.cond_fn = cond_fn
        self.body_fn = body_fn

    @staticmethod
    def _create_jaxpr(init_val, new_cond, new_body):
        init_vals, in_tree = tree_flatten(init_val)
        init_avals = tuple(_abstractify(val) for val in init_vals)
        cond_jaxpr, cond_consts, cond_tree = _initial_style_jaxpr(
            new_cond, in_tree, init_avals, "while_cond"
        )
        body_jaxpr, body_consts, body_tree = _initial_style_jaxpr(
            new_body, in_tree, init_avals, "while_loop"
        )
        if not treedef_is_leaf(cond_tree) or len(cond_jaxpr.out_avals) != 1:
            msg = "cond_fun must return a boolean scalar, but got pytree {}."
            raise TypeError(msg.format(cond_tree))
        pred_aval = cond_jaxpr.out_avals[0]
        if not isinstance(
            pred_aval, ShapedArray
        ) or pred_aval.strip_weak_type().strip_named_shape() != ShapedArray((), jnp.bool_):
            msg = "cond_fun must return a boolean scalar, but got output type(s) {}."
            raise TypeError(msg.format(cond_jaxpr.out_avals))

        return body_jaxpr, cond_jaxpr, cond_consts, body_consts, body_tree

    def call_with_quantum_ctx(self, ctx, args):
        def new_cond(*args_and_qreg):
            args = args_and_qreg[:-1]
            return self.cond_fn(*args)

        def new_body(*args_and_qreg):
            args, qreg = args_and_qreg[:-1], args_and_qreg[-1]

            with JaxTape(do_queue=False) as tape:
                with tape.quantum_tape:
                    out = self.body_fn(*args)
                tape.set_return_val(out)
                new_quantum_tape = JaxTape.device.expand_fn(tape.quantum_tape)
                tape.quantum_tape = new_quantum_tape
                tape.quantum_tape.jax_tape = tape

            return_values, qreg, qubit_states = trace_quantum_tape(
                tape, qreg, has_tracer_return_values=True
            )
            qreg = insert_to_qreg(qubit_states, qreg)

            return return_values, qreg

        body_jaxpr, cond_jaxpr, cond_consts, body_consts, body_tree = WhileCallable._create_jaxpr(
            (*args, jprim.Qreg()), new_cond, new_body
        )
        flat_init_vals_no_qubits = tree_flatten(args)[0]

        WhileLoop(
            flat_init_vals_no_qubits,
            body_jaxpr,
            cond_jaxpr,
            cond_consts,
            body_consts,
            body_tree,
        )

        ret_vals, _ = tree_unflatten(body_tree, body_jaxpr.out_avals)
        a, t = tree_flatten(ret_vals)
        return ctx.jax_tape.create_tracer(t, a)

    def call_with_classical_ctx(self, args):
        body_jaxpr, cond_jaxpr, cond_consts, body_consts, body_tree = WhileCallable._create_jaxpr(
            args, self.cond_fn, self.body_fn
        )
        flat_init_vals_no_qubits = tree_flatten(args)[0]

        inputs = cond_consts + body_consts + flat_init_vals_no_qubits
        ret_tree_flat = jprim.qwhile(
            cond_jaxpr, body_jaxpr, len(cond_consts), len(body_consts), *inputs
        )
        return tree_unflatten(body_tree, ret_tree_flat)

    def __call__(self, *args):
        TracingContext.check_is_tracing("Must use 'while_loop' inside tracing context.")

        ctx = qml.QueuingManager.active_context()
        if ctx is not None:
            return self.call_with_quantum_ctx(ctx, args)
        else:
            return self.call_with_classical_ctx(args)


def while_loop(cond_fn):
    """A qjit compatible while-loop decorator for PennyLane.

    Args:
        cond_fn (Callable): the condition function in the while loop

    Returns:
        Callable: A wrapper around the while-loop function.

    Raises:
        ValueError: Called outside the tape context.
        TypeError: Invalid return type of the condition expression.

    The semantics of ``while_loop`` are given by the following Python implementation:

    .. code-block:: python

        while cond_fun(args):
            args = body_fn(args)
        return args
    """

    def _while_loop(body_fn):
        return WhileCallable(cond_fn, body_fn)

    return _while_loop


class ForLoop(Operation):
    num_wires = AnyWires

    def __init__(self, loop_bounds, iter_args, body_jaxpr, body_consts, body_tree, *args, **kwargs):
        self.loop_bounds = loop_bounds
        self.iter_args = iter_args
        self.body_jaxpr = body_jaxpr
        self.body_consts = body_consts
        self.body_tree = body_tree
        kwargs["wires"] = Wires(ForLoop.num_wires)
        super().__init__(*args, **kwargs)


class ForLoopCallable:
    """
    Some code in this class has been adapted from the for loop implementation in the JAX project at
    https://github.com/google/jax/blob/jax-v0.4.1/jax/_src/lax/control_flow/for_loop.py
    released under the Apache License, Version 2.0, with the following copyright notice:

    Copyright 2021 The JAX Authors.
    """

    def __init__(self, lower_bound, upper_bound, step, body_fn):
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound
        self.step = step
        self.body_fn = body_fn

    @staticmethod
    def _create_jaxpr(init_val, new_body):
        init_vals, in_tree = tree_flatten(init_val)
        init_avals = tuple(_abstractify(val) for val in init_vals)
        body_jaxpr, body_consts, body_tree = _initial_style_jaxpr(
            new_body, in_tree, init_avals, "for_loop"
        )

        return body_jaxpr, body_consts, body_tree

    def call_with_quantum_ctx(self, ctx, *args):
        # Insert iteration counter into loop body arguments with the type of the lower bound.
        args = (self.lower_bound, *args)

        def new_body(*args_and_qreg):
            args, qreg = args_and_qreg[:-1], args_and_qreg[-1]

            with JaxTape(do_queue=False) as tape:
                with tape.quantum_tape:
                    out = self.body_fn(*args)
                tape.set_return_val(out)
                new_quantum_tape = JaxTape.device.expand_fn(tape.quantum_tape)
                tape.quantum_tape = new_quantum_tape
                tape.quantum_tape.jax_tape = tape

            return_values, qreg, qubit_states = trace_quantum_tape(
                tape, qreg, has_tracer_return_values=out is not None
            )
            qreg = insert_to_qreg(qubit_states, qreg)

            return return_values, qreg

        body_jaxpr, body_consts, body_tree = ForLoopCallable._create_jaxpr(
            (*args, jprim.Qreg()), new_body
        )

        flat_init_vals_no_qubits = tree_flatten(args)[0]

        ForLoop(
            [self.lower_bound, self.upper_bound, self.step],
            flat_init_vals_no_qubits,
            body_jaxpr,
            body_consts,
            body_tree,
        )

        # Create tracers for any non-qreg return values (if there are any).
        ret_vals, _ = tree_unflatten(body_tree, body_jaxpr.out_avals)
        a, t = tree_flatten(ret_vals)
        return ctx.jax_tape.create_tracer(t, a)

    def call_with_classical_ctx(self, *args):
        # Insert iteration counter into loop body arguments with the type of the lower bound.
        args = (self.lower_bound, *args)

        body_jaxpr, body_consts, body_tree = ForLoopCallable._create_jaxpr(args, self.body_fn)

        flat_init_vals_no_qubits = tree_flatten(args)[0]

        inputs = (
            [self.lower_bound, self.upper_bound, self.step] + body_consts + flat_init_vals_no_qubits
        )
        ret_tree_flat = jprim.qfor(body_jaxpr, len(body_consts), *inputs)
        return tree_unflatten(body_tree, ret_tree_flat)

    def __call__(self, *args):
        TracingContext.check_is_tracing("Must use 'for_loop' inside tracing context.")

        ctx = qml.QueuingManager.active_context()
        if ctx is None:
            return self.call_with_classical_ctx(*args)
        return self.call_with_quantum_ctx(ctx, *args)


def for_loop(lower_bound, upper_bound, step):
    """A qjit compatible for-loop decorator for PennyLane.

    This for-loop representation is a functional version a the traditional for-loop. That is, any
    variables that are modified across iterations need to be provided as inputs/outputs to the loop
    body function. Input arguments contain the value of a variable at the start of an iteration,
    while output arguments contain the value at the end of the iteration. The outputs are then fed
    back as inputs to the next iteration. The final iteration values are also returned from the
    transformed function.

    Args:
        lower_bound (int): starting value of the iteration index
        upper_bound (int): (exclusive) upper bound of the iteration index
        step (int): increment applied to the iteration index at the end of each iteration

    Returns:
        Callable[[int, ...], ...]: A wrapper around the loop body function.
        Note that the loop body function must always have the iteration index as its first argument,
        which can be used arbitrarily inside the loop body. As the value of the index across
        iterations is handled automatically by the provided loop bounds, it must not be returned
        from the function.

    Raises:
        ValueError: Called outside the tape context.

    The semantics of ``for_loop`` are given by the following Python implementation:

    .. code-block:: python

        for i in range(lower_bound, upper_bound, step):
            args = body_fn(i, *args)
        return args
    """

    def _for_loop(body_fn):
        return ForLoopCallable(lower_bound, upper_bound, step, body_fn)

    return _for_loop


class MidCircuitMeasure(Operation):
    num_wires = 1

    def __init__(self, measurement_id, *args, **kwargs):
        self.measurement_id = measurement_id
        super().__init__(*args, **kwargs)


def measure(wires):
    """A qjit compatible mid-circuit measurement for PennyLane.

    Args:
        wires (Wires): The wire of the qubit the measurement process applies to

    Returns:
        A JAX tracer for the mid-circuit measurement.

    Raises:
        ValueError: Called outside the tape context.
    """
    measurement_id = str(uuid.uuid4())[:8]
    MidCircuitMeasure(measurement_id, wires=wires)

    ctx = qml.QueuingManager.active_context()
    if hasattr(ctx, "jax_tape"):
        jax_tape = ctx.jax_tape
        a, t = tree_flatten(jax.core.get_aval(True))
        return jax_tape.create_tracer(t, a)
    raise ValueError("measure can only be used when it jitted mode")


class QJITDevice(qml.QubitDevice):
    """QJIT device.

    A device that interfaces the compilation pipeline of Pennylane programs.

    Args:
        wires (int): the number of wires to initialize the device with
        shots (int): How many times the circuit should be evaluated (or sampled) to estimate
            the expectation values. Defaults to ``None`` if not specified. Setting
            to ``None`` results in computing statistics like expectation values and
            variances analytically.
    """

    name = "QJIT device"
    short_name = "qjit.device"
    pennylane_requires = "0.1.0"
    version = "0.0.1"
    author = ""
    operations = [
        "MidCircuitMeasure",
        "Cond",
        "WhileLoop",
        "ForLoop",
        "PauliX",
        "PauliY",
        "PauliZ",
        "Hadamard",
        "Identity",
        "S",
        "T",
        "PhaseShift",
        "RX",
        "RY",
        "RZ",
        "CNOT",
        "CY",
        "CZ",
        "SWAP",
        "IsingXX",
        "IsingYY",
        "IsingXY",
        "IsingZZ",
        "ControlledPhaseShift",
        "CRX",
        "CRY",
        "CRZ",
        "CRot",
        "CSWAP",
        "MultiRZ",
        "QubitUnitary",
    ]
    observables = [
        "Identity",
        "PauliX",
        "PauliY",
        "PauliZ",
        "Hadamard",
        "Hermitian",
        "Hamiltonian",
    ]

    def __init__(self, shots=None, wires=None):
        super().__init__(wires=wires, shots=shots)

    def apply(self, operations, **kwargs):
        pass

    def default_expand_fn(self, circuit, max_expansion):
        """
        Most decomposition logic will be equivalent to PennyLane's decomposition.
        However, decomposition logic will differ in the following cases:

        1. All :class:`qml.QubitUnitary <pennylane.ops.op_math.Controlled>` operations
            will decompose to :class:`qml.QubitUnitary <pennylane.QubitUnitary>` operations.
        2. :class:`qml.ControlledQubitUnitary <pennylane.ControlledQubitUnitary>` operations
            will decompose to :class:`qml.QubitUnitary <pennylane.QubitUnitary>` operations.
        3. Unsupported gates in Catalyst will decompose into gates supported by Catalyst.

        Args:
            circuit: circuit to expand
            max_expansion: the maximum number of expansion steps if no fixed-point is reached.
        """
        # Ensure catalyst.measure is used instead of qml.measure.
        if any(isinstance(op, MidMeasureMP) for op in circuit.operations):
            raise TypeError("Must use 'measure' from Catalyst instead of PennyLane.")

        # Fallback for controlled gates that won't decompose successfully.
        # Doing so before rather than after decomposition is generally a trade-off. For low
        # numbers of qubits, a unitary gate might be faster, while for large qubit numbers prior
        # decomposition is generally faster.
        # At the moment, bypassing decomposition for controlled gates will generally have a higher
        # success rate, as complex decomposition paths can fail to trace (c.f. PL #3521, #3522).

        def _decomp_controlled_unitary(self, *args, **kwargs):
            return qml.QubitUnitary(qml.matrix(self), wires=self.wires)

        def _decomp_controlled(self, *args, **kwargs):
            return qml.QubitUnitary(qml.matrix(self), wires=self.wires)

        with Patcher(
            (qml.ops.ControlledQubitUnitary, "compute_decomposition", _decomp_controlled_unitary),
            (qml.ops.Controlled, "has_decomposition", lambda self: True),
            (qml.ops.Controlled, "decomposition", _decomp_controlled),
        ):
            expanded_tape = super().default_expand_fn(circuit, max_expansion)

        self.check_validity(expanded_tape.operations, [])
        return expanded_tape