"""Safe source discovery and AST extraction. Target modules are never imported."""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

from .models import (
    AttributeRecord,
    CallRecord,
    FlowRecord,
    ImportRecord,
    ParsedFile,
    SymbolRecord,
)

DEFAULT_EXCLUDES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".tox", ".nox", "build", "dist",
    "node_modules", ".eggs",
}


def discover_python_files(root: Path, excludes: set[str] | None = None) -> tuple[list[Path], list[str]]:
    """Find in-root Python files, returning skipped paths with a short reason."""
    excludes = DEFAULT_EXCLUDES | (excludes or set())
    root = root.resolve()
    found: list[Path] = []
    skipped: list[str] = []
    try:
        paths = root.rglob("*.py")
        for path in paths:
            relative = path.relative_to(root)
            if any(part in excludes for part in relative.parts):
                continue
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    skipped.append(f"{relative}: symlink points outside project")
                    continue
                if not path.is_file():
                    continue
                found.append(path)
            except OSError as exc:
                skipped.append(f"{relative}: {exc}")
    except OSError as exc:
        skipped.append(f"<project>: {exc}")
    return sorted(found), skipped


def module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    pieces = list(relative.with_suffix("").parts)
    # The widely used ``src/package`` layout places importable packages below a
    # source container that is not itself part of the import name.
    if pieces and pieces[0] == "src" and not (root / "src" / "__init__.py").exists():
        pieces.pop(0)
    if pieces[-1] == "__init__":
        pieces.pop()
    return ".".join(pieces) or "__root__"


def _unparse(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _decorators(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[str]:
    return [_unparse(item) or "<expression>" for item in node.decorator_list]


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = _unparse(node.args) or ""
    annotation = f" -> {_unparse(node.returns)}" if node.returns else ""
    return f"({args}){annotation}"


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return source-level parameter names in call order.

    This deliberately records names rather than attempting to model Python's
    binding rules in the parser.  The semantic pass only connects positional
    arguments where that relationship is unambiguous.
    """
    result = [argument.arg for argument in node.args.posonlyargs]
    result.extend(argument.arg for argument in node.args.args)
    if node.args.vararg:
        result.append(node.args.vararg.arg)
    result.extend(argument.arg for argument in node.args.kwonlyargs)
    if node.args.kwarg:
        result.append(node.args.kwarg.arg)
    return result


def _complexity(node: ast.AST) -> int:
    branches = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.comprehension)
    return 1 + sum(1 for item in ast.walk(node) if isinstance(item, branches)) + sum(
        max(0, len(item.values) - 1) for item in ast.walk(node) if isinstance(item, ast.BoolOp)
    )


def _purpose(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, kind: str) -> str:
    doc = ast.get_docstring(node, clean=True)
    if doc:
        return re.split(r"(?<=[.!?])\s+|\n", doc.strip(), maxsplit=1)[0]
    if kind == "class":
        bases = ", ".join(filter(None, (_unparse(base) for base in node.bases)))
        return f"Class {node.name}" + (f" inherits {bases}." if bases else ".")
    params = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
    calls = sum(1 for item in ast.walk(node) if isinstance(item, ast.Call))
    returns = sum(1 for item in ast.walk(node) if isinstance(item, (ast.Return, ast.Yield, ast.YieldFrom)))
    parts = [f"{kind.capitalize()} {node.name}", f"accepts {len(params)} parameter(s)"]
    if calls:
        parts.append(f"contains {calls} call(s)")
    if returns:
        parts.append(f"contains {returns} return or yield statement(s)")
    return "; ".join(parts) + "."


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    return (
        isinstance(left, ast.Name) and left.id == "__name__" and isinstance(right, ast.Constant) and right.value == "__main__"
    ) or (
        isinstance(right, ast.Name) and right.id == "__name__" and isinstance(left, ast.Constant) and left.value == "__main__"
    )


def _stored_names(node: ast.AST) -> list[str]:
    return [item.id for item in ast.walk(node) if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)]


class ExtractionVisitor(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.symbols: list[SymbolRecord] = []
        self.attributes: list[AttributeRecord] = []
        self.imports: list[ImportRecord] = []
        self.calls: list[CallRecord] = []
        self.flows: list[FlowRecord] = []
        self.scope: list[str] = []
        self.function_scope: list[str] = []
        self.class_scope: list[str] = []
        self.is_entry_point = False
        self.main_guard_depth = 0
        self.assignment_targets: list[str | None] = []

    def _qualified(self, name: str) -> str:
        return ".".join([self.module, *self.scope, name])

    @property
    def current_function(self) -> str | None:
        return self.function_scope[-1] if self.function_scope else None

    @property
    def current_class(self) -> str | None:
        return self.class_scope[-1] if self.class_scope else None

    def _add_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> None:
        qualified = self._qualified(node.name)
        parent = ".".join([self.module, *self.scope]) if self.scope else self.module
        self.symbols.append(SymbolRecord(
            qualified, node.name, kind, parent, _signature(node), ast.get_docstring(node, clean=True),
            _purpose(node, kind), _decorators(node), node.lineno, getattr(node, "end_lineno", None),
            getattr(node, "col_offset", None), _complexity(node),
            _parameters(node),
        ))
        self.scope.append(node.name)
        self.function_scope.append(qualified)
        for argument in list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs):
            self.flows.append(FlowRecord(qualified, argument.arg, "parameter", _unparse(argument.annotation), argument.lineno))
        if node.args.vararg:
            self.flows.append(FlowRecord(qualified, node.args.vararg.arg, "parameter", _unparse(node.args.vararg.annotation), node.args.vararg.lineno))
        if node.args.kwarg:
            self.flows.append(FlowRecord(qualified, node.args.kwarg.arg, "parameter", _unparse(node.args.kwarg.annotation), node.args.kwarg.lineno))
        self.generic_visit(node)
        self.function_scope.pop()
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add_function(node, "method" if self.current_class else "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._add_function(node, "async_method" if self.current_class else "async_function")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = self._qualified(node.name)
        parent = ".".join([self.module, *self.scope]) if self.scope else self.module
        self.symbols.append(SymbolRecord(
            qualified, node.name, "class", parent, "(" + ", ".join(filter(None, (_unparse(base) for base in node.bases))) + ")", ast.get_docstring(node, clean=True),
            _purpose(node, "class"), _decorators(node), node.lineno, getattr(node, "end_lineno", None),
            getattr(node, "col_offset", None), None,
            [],
        ))
        self.scope.append(node.name)
        self.class_scope.append(qualified)
        self.generic_visit(node)
        self.class_scope.pop()
        self.scope.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(ImportRecord(alias.name, None, alias.asname, 0, node.lineno, node.col_offset))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.imports.append(ImportRecord(node.module or "", alias.name, alias.asname, node.level, node.lineno, node.col_offset))

    def visit_Call(self, node: ast.Call) -> None:
        keywords = {
            keyword.arg: _unparse(keyword.value) or "<expression>"
            for keyword in node.keywords if keyword.arg is not None
        }
        self.calls.append(CallRecord(
            self.current_function,
            _unparse(node.func) or "<dynamic>",
            node.lineno,
            node.col_offset,
            [_unparse(argument) or "<expression>" for argument in node.args],
            keywords,
            self.assignment_targets[-1] if self.assignment_targets else None,
            self.main_guard_depth > 0,
        ))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        expression = _unparse(node.value)
        if self.current_function:
            for target in node.targets:
                for name in _stored_names(target):
                    self.flows.append(FlowRecord(self.current_function, name, "assignment", expression, node.lineno))
                self._record_instance_attribute(target, expression, node.lineno)
        elif self.current_class:
            for target in node.targets:
                for name in _stored_names(target):
                    self.attributes.append(AttributeRecord(self.current_class, name, "class", node.lineno, expression))
        targets = _stored_names(node.targets[0]) if node.targets else []
        self.assignment_targets.append(targets[0] if len(targets) == 1 else None)
        self.visit(node.value)
        self.assignment_targets.pop()
        for target in node.targets:
            self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        expression = _unparse(node.value)
        if self.current_function:
            for name in _stored_names(node.target):
                self.flows.append(FlowRecord(self.current_function, name, "assignment", expression, node.lineno))
            self._record_instance_attribute(node.target, expression, node.lineno)
        elif self.current_class:
            for name in _stored_names(node.target):
                self.attributes.append(AttributeRecord(self.current_class, name, "class", node.lineno, expression))
        targets = _stored_names(node.target)
        self.assignment_targets.append(targets[0] if len(targets) == 1 else None)
        if node.value:
            self.visit(node.value)
        self.assignment_targets.pop()
        self.visit(node.target)
        if node.annotation:
            self.visit(node.annotation)

    def _record_instance_attribute(self, target: ast.AST, value: str | None, line: int) -> None:
        if not self.current_class or not isinstance(target, ast.Attribute):
            return
        if isinstance(target.value, ast.Name) and target.value.id in {"self", "cls"}:
            self.attributes.append(AttributeRecord(self.current_class, target.attr, "instance", line, value))

    def visit_Return(self, node: ast.Return) -> None:
        if self.current_function:
            self.flows.append(FlowRecord(self.current_function, "return", "return", _unparse(node.value), node.lineno))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if self.current_function and isinstance(node.ctx, ast.Load):
            self.flows.append(FlowRecord(self.current_function, node.id, "read", None, node.lineno))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # Environment lookups are a meaningful external input even though they
        # are represented as subscription syntax rather than an AST Call.
        expression = _unparse(node.value) or ""
        if expression.endswith(".environ"):
            self.calls.append(CallRecord(
                self.current_function, f"{expression}.__getitem__", node.lineno, node.col_offset,
                [_unparse(node.slice) or "<expression>"], {}, None, self.main_guard_depth > 0,
            ))
        self.generic_visit(node)

    def visit_Yield(self, node: ast.Yield) -> None:
        if self.current_function:
            self.flows.append(FlowRecord(self.current_function, "yield", "yield", _unparse(node.value), node.lineno))
        self.generic_visit(node)

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        if self.current_function:
            self.flows.append(FlowRecord(self.current_function, "yield", "yield", _unparse(node.value), node.lineno))
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        main_guard = _is_main_guard(node)
        if main_guard:
            self.is_entry_point = True
            self.main_guard_depth += 1
        self.visit(node.test)
        for statement in node.body:
            self.visit(statement)
        if main_guard:
            self.main_guard_depth -= 1
        for statement in node.orelse:
            self.visit(statement)


def parse_file(root: Path, path: Path) -> ParsedFile:
    relative = path.relative_to(root).as_posix()
    module = module_name(root, path)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ParsedFile(relative, module, None, "skipped", str(exc))
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    try:
        tree = ast.parse(source, filename=str(path), type_comments=True)
    except SyntaxError as exc:
        return ParsedFile(relative, module, digest, "failed", exc.msg, exc.lineno)
    visitor = ExtractionVisitor(module)
    visitor.visit(tree)
    return ParsedFile(
        relative, module, digest, "parsed", is_entry_point=visitor.is_entry_point,
        symbols=visitor.symbols, attributes=visitor.attributes, imports=visitor.imports,
        calls=visitor.calls, flows=visitor.flows,
    )
