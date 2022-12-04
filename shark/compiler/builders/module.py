from __future__ import annotations

from ast import literal_eval
from contextlib import contextmanager
from typing import Optional, Union

import libcst as cst
from libcst import Integer as cst_Integer, Name as cst_Name
from libcst import matchers as m
from libcst.metadata import (
    ParentNodeProvider,
    PositionProvider,
    ExpressionContextProvider,
    ExpressionContext,
)

from shark.ir import (
    Context,
    Module as MLIRModule,
    Location,
    Block,
    InsertionPoint,
    Value as MLIRValue,
    OpView,
    Operation,
    TypeAttr,
    FunctionType,
)
from shark.compiler.builders.body import BodyBuilder
from shark.compiler.providers import ReturnFinder
from shark.compiler.providers.scope import MyScopeProvider, LiveInLiveOutProvider
from shark.compiler.providers.type import MLIRTypeProvider, map_type_str_to_mlir_type
from shark.dialects import func, scf, affine_


class FindName(cst.CSTVisitor):
    _name: Optional[str] = None

    def leave_Name(self, node: cst.Name):
        if self._name is not None:
            raise Exception(f"multiple names {node}")
        else:
            self._name = node.value

    def __call__(self, node: cst.CSTNode):
        if isinstance(node, cst.Name):
            return node.value

        node.visit(self)
        assert self._name and isinstance(self._name, str)
        return self._name


class CompilerVisitor(m.MatcherDecoratableVisitor):
    METADATA_DEPENDENCIES = (
        MyScopeProvider,
        ParentNodeProvider,
        MLIRTypeProvider,
        PositionProvider,
        ExpressionContextProvider,
        LiveInLiveOutProvider,
    )

    def __init__(
        self,
        mlir_context: Context,
        mlir_module: MLIRModule,
        mlir_location: Location,
        use_affine_fors=False,
        py_constants=None,
        py_global_defs=None,
        global_scope=None,
    ):
        super().__init__()
        if global_scope is None:
            global_scope = {}
        if py_global_defs is None:
            py_global_defs = {}
        self.mlir_context = mlir_context
        self.mlir_module = mlir_module
        self._mlir_block_stack = []
        self.mlir_location = mlir_location
        self.body_builder = BodyBuilder(
            {}, {}, {}, self.mlir_context, self.mlir_location
        )
        self.py_global_defs = py_global_defs
        self.local_defs = {}
        self.global_uses = {}
        self.scf_fors = not use_affine_fors  # affine for loops

    def enter_mlir_block_scope(
        self,
        block: Block,
        *,
        block_args: tuple[str] = None,
        insertion_point: Optional[InsertionPoint] = None,
        mlir_location: Optional[Location] = None,
        # for debugging
        scope_name: Optional[str] = None,
    ):
        if insertion_point is None:
            # one past the last op but still inside the block
            insertion_point = InsertionPoint(block)
        if mlir_location is None:
            mlir_location = self.mlir_location
        if block_args is None:
            block_args = ()
        if scope_name is None:
            scope_name = "UNNAMED_SCOPE"

        self.mlir_context.__enter__()
        insertion_point.__enter__()
        mlir_location.__enter__()
        self._mlir_block_stack.append(
            (block, block_args, insertion_point, mlir_location, scope_name)
        )
        return block, mlir_location

    def exit_mlir_block_scope(self, exit_scope_name=None):
        if exit_scope_name is None:
            exit_scope_name = "UNNAMED_SCOPE"
        (
            block,
            block_args,
            insertion_point,
            mlir_location,
            enter_scope_name,
        ) = self._mlir_block_stack.pop()
        assert (
            enter_scope_name == exit_scope_name
        ), f"enter and exit in two different scopes {enter_scope_name} {exit_scope_name}"
        mlir_location.__exit__(None, None, None)
        insertion_point.__exit__(None, None, None)
        self.mlir_context.__exit__(None, None, None)
        return block

    @contextmanager
    def mlir_block_scope(
        self,
        block: Block,
        *,
        block_args: tuple[str] = None,
        insertion_point: Optional[InsertionPoint] = None,
        mlir_location: Optional[Location] = None,
        scope_name: Optional[str] = None,
    ):
        if scope_name is None:
            scope_name = "UNNAMED_SCOPE"
        yield self.enter_mlir_block_scope(
            block,
            block_args=block_args,
            insertion_point=insertion_point,
            mlir_location=mlir_location,
            scope_name=scope_name,
        )
        self.exit_mlir_block_scope(exit_scope_name=scope_name)

    def peek_block_scope(self):
        assert len(self._mlir_block_stack), "no block scope yet"
        return self._mlir_block_stack[-1][:-1]

    def _get_or_make_mlir_constant(
        self,
        py_cst: Union[int, float, bool],
        name: Optional[str] = None,
        index_type: bool = False,
    ):
        if name is None:
            name = str(py_cst)
        if (name, index_type) not in self.local_defs:
            self.local_defs[name, index_type] = self.body_builder.constant(
                py_cst, index_type
            )
        return self.local_defs[name, index_type]

    def _get_mlir_value(self, name) -> MLIRValue:
        # Float and Integer nodes that don't have names...
        if name in self.local_defs:
            ret = self.local_defs[name]
        else:
            raise ValueError(f"{name} is not defined")
        return ret

    def set_mlir_value(
        self, name: Union[str, cst.CSTNode, tuple[str, bool]], value: MLIRValue
    ) -> None:
        self.local_defs[name] = value

    def get_mlir_value(self, node: cst.CSTNode) -> MLIRValue:
        # Float and Integer nodes that don't have names...
        if isinstance(node, cst.BaseNumber):
            return self._get_or_make_mlir_constant(literal_eval(node.value))
        elif isinstance(node, (cst.BinaryOperation, cst.Call, cst.Subscript)):
            # values associated with expression nodes (e.g. binary expression) are stored as nodes in
            # local_defs
            return self._get_mlir_value(node)
        else:
            name = FindName()(node)
            return self._get_mlir_value(name)

    # visit_<node> methods ###########################################

    @m.visit(m.Assign())
    @m.visit(m.Call())
    @m.visit(m.BinaryOperation())
    @m.visit(m.Subscript())
    @m.visit(m.Expr())
    @m.visit(m.Return())
    def visit_(self, _node):
        return True

    def leave_Call(self, node: cst.Call) -> None:
        if m.matches(
            node,
            m.Call(
                func=m.Attribute(value=m.Name(value="np"), attr=m.Name(value="empty"))
            ),
        ):
            dim_sizes = []
            assert len(node.args) == 1, f"dtype note supporty for np.empty {node.func}"
            assert isinstance(
                node.args[0].value, (cst.Tuple, cst.List)
            ), f"unsupported dims format {node.args[0].value}"
            for elt in node.args[0].value.elements:
                dim_sizes.append(int(elt.value.value))

            f64 = map_type_str_to_mlir_type("float", self.mlir_context)
            arr = self.body_builder.memref_alloc(dim_sizes, f64)
            self.set_mlir_value(node, arr)

    def leave_Assign(self, node: cst.Assign):
        if m.matches(
            node,
            m.Assign(targets=[m.AssignTarget(target=m.Subscript())]),
        ):
            lhs = node.targets[0].target
            arr_name = lhs.value
            memref = self.get_mlir_value(arr_name)
            slice_idxs = lhs.slice
            slice_idxs = [
                # these are Name nodes...
                self._get_mlir_value(slice_idx.slice.value.value)
                for slice_idx in slice_idxs
            ]
            store_value = self.get_mlir_value(node.value)
            # this just returns back the same memref
            value = self.body_builder.memref_store(store_value, memref, slice_idxs)
            # TODO(max): do i need this?
            # value = self.body_builder.affine_store(store_value, memref, slice_idxs)
        else:
            lhs = [n.target for n in node.targets]
            assert len(lhs) == 1, f"multiple assign targets unsupported {lhs}"
            lhs = lhs[0]
            value = self.get_mlir_value(node.value)

        # if a python name is assigned to another python name eg a = b
        while not isinstance(value, MLIRValue):
            assert isinstance(value, cst.CSTNode) and hasattr(
                value, "name"
            ), f"unknown value {value}"
            value = self.get_mlir_value(value.name)
        self.set_mlir_value(lhs.value, value)

    def visit_BinaryOperation_operator(self, node: cst.BinaryOperation) -> None:
        node.left.visit(self)
        node.right.visit(self)
        if isinstance(node.left, cst.Name):
            left_mlir_value = self._get_mlir_value(node.left.value)
        else:
            left_mlir_value = self.get_mlir_value(node.left)
        if isinstance(node.right, cst.Name):
            right_mlir_value = self._get_mlir_value(node.right.value)
        else:
            right_mlir_value = self.get_mlir_value(node.right)
        assert (
            left_mlir_value.type == right_mlir_value.type
        ), f"op requires the same type for all operands and results {left_mlir_value.type=} {right_mlir_value.type=}"
        mul_val = self.body_builder[node.operator.__class__.__name__](
            left_mlir_value, right_mlir_value
        )
        self.set_mlir_value(node, mul_val)

    def leave_Subscript(self, node: cst.Subscript):
        ctx = self.get_metadata(ExpressionContextProvider, node)
        if ctx == ExpressionContext.LOAD:
            memref = self.get_mlir_value(node.value)
            slice_idxs = [
                # these are Name nodes...
                self._get_mlir_value(slice_idx.slice.value.value)
                for slice_idx in node.slice
            ]
            value = self.body_builder.memref_load(memref, slice_idxs)
            # TODO(max): do i need this?
            # value = self.body_builder.affine_load(memref, slice_idxs)
            self.set_mlir_value(node, value)

    def leave_Expr(self, node: cst.Expr) -> Optional[bool]:
        return True

    def visit_FunctionDef(self, node: cst.FunctionDef):
        args = node.params
        arg_types = []
        defaults = [p.default for p in args.params]
        # left to right but starting from the right (since you can't have non-defaults after defaults)
        for i, arg in enumerate(args.params):
            if arg.annotation is not None:
                arg_types.append(self.get_metadata(MLIRTypeProvider, arg.annotation))
            elif defaults[i] is not None:
                arg_types.append(self.get_metadata(MLIRTypeProvider, defaults[i]))
            else:
                raise Exception(f"can't infer type for {repr(arg)}")

        (
            _block,
            _block_args,
            _insertion_point,
            mlir_location,
        ) = self.peek_block_scope()
        function_type = self.get_metadata(MLIRTypeProvider, node)
        func_op = func.FuncOp(
            name=node.name.value,
            type=function_type,
            visibility="public",
            loc=mlir_location,
        )
        func_op_entry_block = func_op.add_entry_block()
        for i, func_op_arg in enumerate(func_op.arguments):
            self.set_mlir_value(args.params[i].name.value, func_op_arg)

        with self.mlir_block_scope(func_op_entry_block, scope_name=node.name.value):
            node.body.visit(self)

            function_returns = ReturnFinder()(node)
            # Coerce return values, add ReturnOp and rewrite func type.
            if function_returns is not None:
                assert isinstance(
                    function_returns.value, cst.Name
                ), f"complex returns types not supported yet {function_returns}"
                return_values = self.local_defs[function_returns.value.value]
                if isinstance(return_values, tuple):
                    return_values = list(return_values)
                elif isinstance(return_values, MLIRValue):
                    return_values = [return_values]
                elif isinstance(return_values, OpView):
                    return_values = return_values.operation.results
                elif isinstance(return_values, Operation):
                    return_values = return_values.results
                else:
                    return_values = list(return_values)
            else:
                return_values = []

            canonical_func_type = FunctionType.get(
                inputs=function_type[0], results=[return_values[0].type]
            )
            func_op.attributes["function_type"] = TypeAttr.get(canonical_func_type)
            func.ReturnOp(return_values)
        return False

    def leave_FunctionDef(self, node: cst.FunctionDef):
        self.local_defs = {}

    # _live_ins, live_outs, scope_name = self.get_metadata(
    #     LiveInLiveOutProvider, node
    # )
    # live_outs = tuple(live_outs)
    # yielded_types = []
    # for l in live_outs:
    #     items = scope._getitem_from_self_or_parent(l)
    #     assert len(items) == 1, f"multiple parent items {l}"
    #     item = list(items)[0].node
    #     yielded_types.append(self.get_metadata(MLIRTypeProvider, item))
    # undef_ops = [llvm.UndefOp(yt, loc=mlir_location) for yt in yielded_types]

    # for i, yielded_var_name in enumerate(live_outs):
    #     self.local_defs[yielded_var_name] = block.owner.operation.results[i]
    #     yielded_type = yielded_types[i]
    #
    #     with InsertionPoint.at_block_begin(func_body_block), mlir_location:
    #         cst = arith.ConstantOp(yielded_type, 0.0)
    #         undef_ops[i].operation.replace_all_uses_with(cst.operation)
    #
    # for undef_op in undef_ops:
    #     undef_op.operation.erase()

    def visit_For(self, node: cst.For):
        (
            func_body_block,
            _block_args,
            _insertion_point,
            mlir_location,
        ) = self.peek_block_scope()

        if node.iter.func.value != "range":
            raise RuntimeError("Only `range` iterator currently supported")
        for arg in node.iter.args:
            arg.visit(self)
            assert isinstance(
                arg.value, cst_Integer
            ), f"symbolic range not supported yet {arg.value}"

        lb = int(node.iter.args[0].value.value) if len(node.iter.args) > 1 else 0
        ub = (
            int(node.iter.args[1].value.value)
            if len(node.iter.args) > 1
            else int(node.iter.args[0].value.value)
        )
        step = int(node.iter.args[2].value.value) if len(node.iter.args) > 2 else 1
        induction_var = node.target
        assert isinstance(
            induction_var, cst_Name
        ), f"complex for range targets unsupported {induction_var}"

        if self.scf_fors:
            lb, ub, step = [
                self._get_or_make_mlir_constant(c, index_type=self.scf_fors)
                for c in [lb, ub, step]
            ]
            assert all(
                [isinstance(x, MLIRValue) for x in [lb, ub, step]]
            ), f"something went wrong converting range for {lb=} {ub=} {step=}"

            loop = scf.ForOp(lb, ub, step, [], loc=mlir_location)
        else:
            loop = affine_.AffineForOp(lb, ub, step, loc=mlir_location)

        # yielded_vals = [self._get_mlir_value(y) for y in live_outs]
        # yielded_types = []
        # undef_ops = []
        # live_outs = []
        yielded_vals = []

        self.set_mlir_value(induction_var.value, loop.induction_variable)
        with self.mlir_block_scope(
            loop.body,
        ) as (block, mlir_location):
            node.body.visit(self)
            if self.scf_fors:
                scf.YieldOp(yielded_vals, loc=mlir_location)
            else:
                affine_.AffineYieldOp(yielded_vals, loc=mlir_location)

        return False

    @m.visit(m.Float())
    @m.visit(m.Integer())
    def visit_python_number(self, node: cst.Float | cst_Integer):
        self._get_or_make_mlir_constant(literal_eval(node.value))

    def visit_Module(self, node: cst.Module):
        with self.mlir_block_scope(
            self.mlir_module.body, scope_name=node.__class__.__name__
        ):
            for child in node.body:
                child.visit(self)

        return False
