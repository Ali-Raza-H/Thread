"""Safe source discovery and AST extraction. Target modules are never imported."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .models import (
    AttributeRecord,
    BoundaryRecord,
    CallRecord,
    DefinitionRecord,
    FlowRecord,
    ImportRecord,
    ParsedFile,
    ReferenceRecord,
    ScopeRecord,
    SymbolRecord,
)

DEFAULT_EXCLUDES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".tox", ".nox", "build", "dist",
    "node_modules", ".eggs",
}
PARSER_NAME = "ast"
PARSER_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 4 * 1024 * 1024
MAX_NOTEBOOK_BYTES = 8 * 1024 * 1024
MAX_GENERATED_BYTES = 4 * 1024 * 1024
MAX_INVENTORY_FILES = 100_000


@dataclass
class SourceUnit:
    """One accepted source form before AST extraction."""

    relative_path: str
    module_name: str
    origin_kind: str
    original_path: str
    resolved_path: str
    category: str = "source"
    capability: str = "Full"
    content_hash: str | None = None
    source_text: str | None = None
    line_offset: int = 0
    origin_id: str = ""
    parser: str = PARSER_NAME
    parser_version: str = PARSER_VERSION

    def parse(self) -> ParsedFile:
        if self.source_text is None:
            try:
                self.source_text = Path(self.resolved_path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return self._parsed("skipped", error_message=str(exc))
        digest = self.content_hash or hashlib.sha256(self.source_text.encode("utf-8")).hexdigest()
        try:
            tree = ast.parse(self.source_text, filename=self.relative_path, type_comments=True)
        except SyntaxError as exc:
            return self._parsed("failed", digest, exc.msg, exc.lineno)
        visitor = ExtractionVisitor(self.module_name)
        visitor.visit(tree)
        return self._parsed("parsed", digest, is_entry_point=visitor.is_entry_point,
                            symbols=visitor.symbols, attributes=visitor.attributes, imports=visitor.imports,
                            calls=visitor.calls, flows=visitor.flows, scopes=visitor.scopes,
                            definitions=visitor.definitions, references=visitor.references,
                            boundaries=visitor.boundaries, exports=sorted(visitor.exports))

    def _parsed(self, status: str, digest: str | None = None, error_message: str | None = None,
                error_line: int | None = None, **facts) -> ParsedFile:
        return ParsedFile(
            self.relative_path, self.module_name, digest, status, error_message, error_line,
            origin_kind=self.origin_kind, original_path=self.original_path,
            resolved_path=self.resolved_path, parser=self.parser, parser_version=self.parser_version,
            capability=self.capability, category=self.category, line_offset=self.line_offset,
            origin_id=self.origin_id or self.resolved_path, **facts,
        )


def file_category(path: str | Path, text: str | None = None) -> str:
    """Classify a path for discovery inventory and skip reporting."""
    name = PurePosixPath(str(path).replace(os.sep, "/")).name.lower()
    suffix = PurePosixPath(name).suffix
    parts = {part.lower() for part in PurePosixPath(str(path).replace(os.sep, "/")).parts}
    if suffix == ".py":
        if "tests" in parts or name.startswith("test_") or name.endswith("_test.py"):
            return "test"
        if "generated" in parts or "generated" in name or (text and re.search(r"(?im)^\s*#\s*generated\b", text[:2000])):
            return "generated"
        return "source"
    if suffix == ".ipynb":
        return "notebook"
    if suffix == ".zip":
        return "archive"
    if suffix in {".so", ".pyd", ".dll", ".dylib"}:
        return "native"
    if name in {"pyproject.toml", "setup.py", "setup.cfg", "tox.ini"} or name.startswith("requirements"):
        return "metadata"
    if suffix in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt", ".md", ".rst", ".html", ".css", ".js", ".sql"}:
        return "resource"
    return "unclassified"


def _excluded(relative: str, excludes: set[str]) -> bool:
    parts = PurePosixPath(relative).parts
    if any(part in excludes for part in parts):
        return True
    return any(fnmatch.fnmatch(relative, pattern) for pattern in excludes if "/" in pattern or "*" in pattern)


def _included(relative: str, includes: list[str] | None) -> bool:
    return not includes or any(fnmatch.fnmatch(relative, pattern) for pattern in includes)


def _module_from_relative(relative: str, has_src_package: bool = False) -> str:
    pieces = list(PurePosixPath(relative).with_suffix("").parts)
    if pieces and pieces[0] == "src" and not has_src_package:
        pieces.pop(0)
    if pieces and pieces[-1] == "__init__":
        pieces.pop()
    return ".".join(pieces) or "__root__"


def module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    return _module_from_relative(relative.as_posix(), (root / "src" / "__init__.py").exists())


def _file_unit(root: Path, path: Path, origin_kind: str = "file", relative: str | None = None,
               module: str | None = None) -> SourceUnit:
    relative = relative or path.relative_to(root).as_posix()
    return SourceUnit(
        relative, module or _module_from_relative(relative, (root / "src" / "__init__.py").exists()),
        origin_kind, str(path), str(path.resolve()),
        category=file_category(relative),
        capability="Full" if origin_kind != "external_file" else "Partial",
        origin_id=str(path.resolve()),
    )


def discover_python_files(root: Path, excludes: set[str] | None = None,
                          includes: list[str] | None = None) -> tuple[list[Path], list[str]]:
    """Find in-root Python files without following directory symlinks."""
    excludes = DEFAULT_EXCLUDES | (excludes or set())
    root = root.resolve()
    found: list[Path] = []
    skipped: list[str] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        kept = []
        for name in sorted(dirnames):
            child = directory_path / name
            relative = child.relative_to(root).as_posix()
            if _excluded(relative, excludes):
                continue
            if child.is_symlink():
                skipped.append(f"{relative}: directory symlink is not traversed")
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if _excluded(relative, excludes) or not _included(relative, includes):
                continue
            try:
                if path.is_symlink() and not path.resolve().is_relative_to(root):
                    skipped.append(f"{relative}: symlink points outside project")
                    continue
                if not path.is_file():
                    continue
                found.append(path)
            except OSError as exc:
                skipped.append(f"{relative}: {exc}")
    return sorted(found), skipped


def classify_project_files(root: Path, excludes: set[str] | None = None) -> dict[str, int]:
    """Count project files by category without reading their contents."""
    excludes = DEFAULT_EXCLUDES | (excludes or set())
    root = root.resolve()
    counts: Counter[str] = Counter()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        dirnames[:] = [
            name for name in sorted(dirnames)
            if not _excluded((directory_path / name).relative_to(root).as_posix(), excludes)
            and not (directory_path / name).is_symlink()
        ]
        for name in sorted(filenames):
            if sum(counts.values()) >= MAX_INVENTORY_FILES:
                return dict(sorted(counts.items()))
            relative = (directory_path / name).relative_to(root).as_posix()
            if not _excluded(relative, excludes):
                counts[file_category(relative)] += 1
    return dict(sorted(counts.items()))


def _zip_units(path: Path, display: str) -> tuple[list[SourceUnit], list[str]]:
    units: list[SourceUnit] = []
    skipped: list[str] = []
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        return [], [f"{display}: invalid zip archive ({exc})"]
    total = 0
    with archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.endswith(".py"):
                continue
            member = PurePosixPath(name)
            if member.is_absolute() or ".." in member.parts or ":" in member.parts[0]:
                skipped.append(f"{display}!/{name}: unsafe archive member path")
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                skipped.append(f"{display}!/{name}: archive symlink is not followed")
                continue
            if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                skipped.append(f"{display}!/{name}: archive member exceeds size limit")
                continue
            total += info.file_size
            if total > MAX_ARCHIVE_BYTES:
                skipped.append(f"{display}!/{name}: archive expansion exceeds total size limit")
                break
            try:
                with archive.open(info) as handle:
                    raw = handle.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError, RuntimeError) as exc:
                skipped.append(f"{display}!/{name}: {exc}")
                continue
            relative = f"{display}!/{name}"
            resolved = f"{path.resolve()}!/{name}"
            units.append(SourceUnit(
                relative, _module_from_relative(name), "zip_member", str(path), resolved,
                category=file_category(name), capability="Partial", source_text=text, origin_id=resolved,
            ))
    return units, skipped


def _notebook_units(path: Path, display: str) -> tuple[list[SourceUnit], list[str]]:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_NOTEBOOK_BYTES:
            return [], [f"{display}: notebook exceeds size limit"]
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [], [f"{display}: invalid notebook ({exc})"]
    cells = document.get("cells") if isinstance(document, dict) else None
    if not isinstance(cells, list):
        return [], [f"{display}: notebook has no cell list"]
    units: list[SourceUnit] = []
    skipped: list[str] = []
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        source = cell.get("source", "")
        text = "".join(source) if isinstance(source, list) and all(isinstance(item, str) for item in source) else source
        if not isinstance(text, str):
            skipped.append(f"{display}#cell-{index + 1}: invalid cell source")
            continue
        if len(text.encode("utf-8")) > MAX_ARCHIVE_MEMBER_BYTES:
            skipped.append(f"{display}#cell-{index + 1}: cell exceeds size limit")
            continue
        cell_id = str(cell.get("id") or f"cell-{index + 1}")
        relative = f"{display}#{cell_id}"
        resolved = f"{path.resolve()}#{cell_id}"
        units.append(SourceUnit(
            relative, f"{_module_from_relative(PurePosixPath(display).name)}_cell_{index + 1}", "notebook_cell",
            str(path), resolved, category="notebook", capability="Partial", source_text=text,
            origin_id=resolved,
        ))
    return units, skipped


def discover_source_units(root: Path, excludes: set[str] | None = None, includes: list[str] | None = None,
                          explicit_sources: list[str] | None = None,
                          generated: list[dict[str, str]] | None = None) -> tuple[list[SourceUnit], list[str]]:
    """Discover accepted source forms with explicit, bounded traversal."""
    excludes = DEFAULT_EXCLUDES | (excludes or set())
    root = root.resolve()
    paths, skipped = discover_python_files(root, excludes, includes)
    units = [_file_unit(root, path) for path in paths]
    seen = {unit.origin_id for unit in units}
    for supplied in explicit_sources or []:
        path = Path(supplied).expanduser()
        if not path.is_absolute():
            path = root / path
        original = path.absolute()
        try:
            path = path.resolve()
            if not path.exists():
                skipped.append(f"{supplied}: explicit source does not exist")
                continue
            if path.is_dir():
                skipped.append(f"{supplied}: explicit directories are not traversed")
                continue
            if path.suffix == ".zip":
                archive_units, archive_skipped = _zip_units(path, supplied)
                units.extend(archive_units)
                skipped.extend(archive_skipped)
                continue
            if path.suffix == ".ipynb":
                notebook_units, notebook_skipped = _notebook_units(path, supplied)
                units.extend(notebook_units)
                skipped.extend(notebook_skipped)
                continue
            if path.suffix != ".py":
                skipped.append(f"{supplied}: {file_category(path)} source is not supported explicitly")
                continue
            if path.is_relative_to(root):
                relative = path.relative_to(root).as_posix()
                if _excluded(relative, excludes):
                    continue
                unit = _file_unit(root, path)
            else:
                unit = _file_unit(root, path, "external_file", f"external/{path.name}", _module_from_relative(path.name))
                unit.original_path = str(original)
            if unit.origin_id not in seen:
                units.append(unit)
                seen.add(unit.origin_id)
        except OSError as exc:
            skipped.append(f"{supplied}: {exc}")
    for item in generated or []:
        name = str(item.get("name", "generated")).strip() or "generated"
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip(".") or "generated"
        text = str(item.get("text", ""))
        if len(text.encode("utf-8")) > MAX_GENERATED_BYTES:
            skipped.append(f"generated:{safe_name}: generated text exceeds size limit")
            continue
        relative = f"generated://{safe_name}"
        units.append(SourceUnit(relative, f"generated_{safe_name.replace('.', '_')}", "generated", name,
                                relative, category="generated", capability="Partial", source_text=text,
                                origin_id=relative))
    unique = {unit.relative_path: unit for unit in units}
    return sorted(unique.values(), key=lambda item: item.relative_path), skipped


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


def _is_type_checking(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
    )


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
        self.scopes = [ScopeRecord(f"{module}:module", "module", module, None, 1)]
        self.definitions: list[DefinitionRecord] = []
        self.references: list[ReferenceRecord] = []
        self.boundaries: list[BoundaryRecord] = []
        self.exports: set[str] = set()
        self.scope_keys = [self.scopes[0].key]
        self.type_checking_depth = 0
        self.conditional_depth = 0

    def _qualified(self, name: str) -> str:
        return ".".join([self.module, *self.scope, name])

    @property
    def current_scope_key(self) -> str:
        return self.scope_keys[-1]

    def _add_definition(self, name: str, kind: str, source: str, line: int,
                        expression: str | None = None, exported: bool = False,
                        qualified: str | None = None) -> None:
        self.definitions.append(DefinitionRecord(
            self.current_scope_key, name, qualified or self._qualified(name), kind, source, line, expression,
            exported, self.type_checking_depth > 0, self.conditional_depth > 0,
        ))

    def _push_scope(self, kind: str, name: str, node: ast.AST) -> str:
        key = f"{self.current_scope_key}.{name}:{getattr(node, 'lineno', 0)}"
        self.scopes.append(ScopeRecord(
            key, kind, ".".join([self.module, *self.scope]), self.current_scope_key,
            getattr(node, "lineno", 1), getattr(node, "end_lineno", None),
        ))
        self.scope_keys.append(key)
        return key

    def _pop_scope(self) -> None:
        self.scope_keys.pop()

    @property
    def current_function(self) -> str | None:
        return self.function_scope[-1] if self.function_scope else None

    @property
    def current_class(self) -> str | None:
        return self.class_scope[-1] if self.class_scope else None

    def _add_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> None:
        qualified = self._qualified(node.name)
        parent = ".".join([self.module, *self.scope]) if self.scope else self.module
        overload = any("overload" in decorator for decorator in _decorators(node))
        self._add_definition(node.name, "overload" if overload else kind, "definition", node.lineno,
                             qualified=qualified, exported=not self.scope and not node.name.startswith("_"))
        if not overload:
            self.symbols.append(SymbolRecord(
                qualified, node.name, kind, parent, _signature(node), ast.get_docstring(node, clean=True),
                _purpose(node, kind), _decorators(node), node.lineno, getattr(node, "end_lineno", None),
                getattr(node, "col_offset", None), _complexity(node),
                _parameters(node),
            ))
        self.scope.append(node.name)
        self.function_scope.append(qualified)
        self._push_scope("function", node.name, node)
        for argument in list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs):
            self._add_definition(argument.arg, "parameter", "parameter", argument.lineno)
            self.flows.append(FlowRecord(qualified, argument.arg, "parameter", _unparse(argument.annotation), argument.lineno))
        if node.args.vararg:
            self._add_definition(node.args.vararg.arg, "parameter", "parameter", node.args.vararg.lineno)
            self.flows.append(FlowRecord(qualified, node.args.vararg.arg, "parameter", _unparse(node.args.vararg.annotation), node.args.vararg.lineno))
        if node.args.kwarg:
            self._add_definition(node.args.kwarg.arg, "parameter", "parameter", node.args.kwarg.lineno)
            self.flows.append(FlowRecord(qualified, node.args.kwarg.arg, "parameter", _unparse(node.args.kwarg.annotation), node.args.kwarg.lineno))
        self.generic_visit(node)
        self._pop_scope()
        self.function_scope.pop()
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add_function(node, "method" if self.current_class else "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._add_function(node, "async_method" if self.current_class else "async_function")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = self._qualified(node.name)
        parent = ".".join([self.module, *self.scope]) if self.scope else self.module
        self._add_definition(node.name, "class", "definition", node.lineno, qualified=qualified,
                             exported=not self.scope and not node.name.startswith("_"))
        self.symbols.append(SymbolRecord(
            qualified, node.name, "class", parent, "(" + ", ".join(filter(None, (_unparse(base) for base in node.bases))) + ")", ast.get_docstring(node, clean=True),
            _purpose(node, "class"), _decorators(node), node.lineno, getattr(node, "end_lineno", None),
            getattr(node, "col_offset", None), None,
            [],
        ))
        self.scope.append(node.name)
        self.class_scope.append(qualified)
        self._push_scope("class", node.name, node)
        for base in node.bases:
            self.references.append(ReferenceRecord(
                self.current_scope_key, _unparse(base) or "<expression>", _unparse(base) or "<expression>",
                "base", node.lineno, self.type_checking_depth > 0,
            ))
        self.generic_visit(node)
        self._pop_scope()
        self.class_scope.pop()
        self.scope.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            visible = alias.asname or alias.name.split(".")[0]
            self.imports.append(ImportRecord(alias.name, None, alias.asname, 0, node.lineno, node.col_offset,
                                             self.type_checking_depth > 0, self.conditional_depth > 0))
            self._add_definition(visible, "import", f"import {alias.name}", node.lineno,
                                 qualified=f"{self.module}.{visible}",
                                 exported=not self.scope and not visible.startswith("_"))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            visible = alias.asname or alias.name
            self.imports.append(ImportRecord(node.module or "", alias.name, alias.asname, node.level,
                                             node.lineno, node.col_offset, self.type_checking_depth > 0,
                                             self.conditional_depth > 0))
            if alias.name != "*":
                self._add_definition(visible, "import", f"from {node.module or '.'} import {alias.name}", node.lineno,
                                     qualified=f"{self.module}.{visible}",
                                     exported=not self.scope and not visible.startswith("_"))

    def visit_Call(self, node: ast.Call) -> None:
        keywords = {
            keyword.arg: _unparse(keyword.value) or "<expression>"
            for keyword in node.keywords if keyword.arg is not None
        }
        expression = _unparse(node.func) or "<dynamic>"
        self.calls.append(CallRecord(
            self.current_function,
            expression,
            node.lineno,
            node.col_offset,
            [_unparse(argument) or "<expression>" for argument in node.args],
            keywords,
            self.assignment_targets[-1] if self.assignment_targets else None,
            self.main_guard_depth > 0,
            self.type_checking_depth > 0,
        ))
        short = expression.rsplit(".", 1)[-1]
        kind = ({"import_module": "dynamic_import", "__import__": "dynamic_import",
                 "eval": "dynamic_code", "exec": "dynamic_code", "compile": "dynamic_code",
                 "getattr": "reflection", "setattr": "monkey_patch", "delattr": "monkey_patch",
                 "globals": "reflection", "locals": "reflection", "vars": "reflection"}).get(short)
        if kind:
            self.boundaries.append(BoundaryRecord(
                self.current_scope_key, kind, expression, node.lineno,
                {"arguments": ", ".join(_unparse(argument) or "<expression>" for argument in node.args)},
            ))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        expression = _unparse(node.value)
        for target in node.targets:
            for name in _stored_names(target):
                kind = "constant" if name.isupper() else ("local" if self.current_function else "variable")
                self._add_definition(name, kind, "assignment", node.lineno, expression,
                                     exported=not self.scope and not name.startswith("_"))
        if not self.scope and len(node.targets) == 1 and _stored_names(node.targets[0]) == ["__all__"]:
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                value = None
            if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
                self.exports = set(value)
            else:
                self.boundaries.append(BoundaryRecord(
                    self.current_scope_key, "dynamic_exports", "__all__", node.lineno,
                    {"reason": "__all__ is not a literal sequence of names"},
                ))
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
        for name in _stored_names(node.target):
            kind = "constant" if name.isupper() else ("local" if self.current_function else "variable")
            self._add_definition(name, kind, "annotation", node.lineno, expression,
                                 exported=not self.scope and not name.startswith("_"))
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
        if isinstance(node.ctx, ast.Load):
            self.references.append(ReferenceRecord(
                self.current_scope_key, node.id, node.id, "read", node.lineno, self.type_checking_depth > 0,
            ))
            if self.current_function:
                self.flows.append(FlowRecord(self.current_function, node.id, "read", None, node.lineno))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            expression = _unparse(node) or node.attr
            self.references.append(ReferenceRecord(
                self.current_scope_key, node.attr, expression, "attribute", node.lineno,
                self.type_checking_depth > 0,
            ))
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            self.definitions.append(DefinitionRecord(
                self.current_scope_key, name, f"{self.module}.{name}", "global", "global", node.lineno,
                exported=False, is_type_checking=self.type_checking_depth > 0,
                conditional=self.conditional_depth > 0,
            ))

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        for name in node.names:
            self.definitions.append(DefinitionRecord(
                self.current_scope_key, name, name, "nonlocal", "nonlocal", node.lineno,
                exported=False, is_type_checking=self.type_checking_depth > 0,
                conditional=self.conditional_depth > 0,
            ))

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        for name in _stored_names(node.target):
            self._add_definition(name, "local" if self.current_function else "variable", "for",
                                 node.lineno, _unparse(node.iter))
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            if item.optional_vars:
                for name in _stored_names(item.optional_vars):
                    self._add_definition(name, "local" if self.current_function else "variable", "with",
                                         node.lineno, _unparse(item.context_expr))
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._add_definition(node.name, "local", "exception", node.lineno, _unparse(node.type))
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        for name in _stored_names(node.target):
            self._add_definition(name, "local" if self.current_function else "variable", "assignment",
                                 node.lineno, _unparse(node.value))
        self.generic_visit(node)

    def _visit_comprehension(self, node: ast.AST, label: str) -> None:
        self._push_scope("comprehension", f"<{label}>", node)
        generators = getattr(node, "generators", [])
        for generator in generators:
            for name in _stored_names(generator.target):
                self._add_definition(name, "comprehension", "comprehension", getattr(generator, "lineno", node.lineno),
                                     _unparse(generator.iter))
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for attribute in ("elt", "key", "value"):
            child = getattr(node, attribute, None)
            if child is not None:
                self.visit(child)
        self._pop_scope()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node, "listcomp")

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node, "setcomp")

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node, "dictcomp")

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node, "genexpr")

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
        type_checking = _is_type_checking(node.test)
        if main_guard:
            self.is_entry_point = True
            self.main_guard_depth += 1
        self.visit(node.test)
        for statement in node.body:
            self.conditional_depth += 1
            if type_checking:
                self.type_checking_depth += 1
            self.visit(statement)
            if type_checking:
                self.type_checking_depth -= 1
            self.conditional_depth -= 1
        if main_guard:
            self.main_guard_depth -= 1
        for statement in node.orelse:
            self.conditional_depth += 1
            if type_checking:
                self.type_checking_depth += 1
            self.visit(statement)
            if type_checking:
                self.type_checking_depth -= 1
            self.conditional_depth -= 1

    def visit_Try(self, node: ast.Try) -> None:
        self.conditional_depth += 1
        for statement in node.body:
            self.visit(statement)
        self.conditional_depth -= 1
        for handler in node.handlers:
            self.conditional_depth += 1
            self.visit(handler)
            self.conditional_depth -= 1
        for statement in node.orelse + node.finalbody:
            self.visit(statement)


def parse_file(root: Path, path: Path) -> ParsedFile:
    return _file_unit(root.resolve(), path).parse()
