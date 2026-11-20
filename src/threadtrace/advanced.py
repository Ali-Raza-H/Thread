"""Deterministic structural, complexity, and advanced-Python facts."""

from __future__ import annotations

import ast
import builtins
from typing import Any

MAX_REPORTED_LOOP_DEPTH = 3
CONCURRENCY_CALLS = {
    "Thread", "Timer", "Process", "Pool", "create_task", "ensure_future", "gather",
    "to_thread", "run_in_executor", "submit", "map", "ThreadPoolExecutor",
    "ProcessPoolExecutor", "fork", "spawn",
}
NATIVE_CALLS = {"CDLL", "PyDLL", "WinDLL", "ffi", "dlopen", "load_library"}
COLLECTION_MUTATORS = {"append", "extend", "insert", "add", "update", "setdefault", "appendleft", "put"}
ITERATING_CALLS = {"sum", "min", "max", "any", "all", "list", "tuple", "set", "dict", "sorted", "reversed", "enumerate", "zip"}
SORTING_CALLS = {"sorted", "sort"}
SEQUENCE_OPERATIONS = {"count", "index", "remove", "insert", "copy", "extend", "pop"}
PATH_SUFFIXES = {
    "configuration": {".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".env", ".properties"},
    "structured-data": {".json", ".jsonl", ".csv", ".tsv", ".xml", ".parquet", ".avro"},
    "text": {".txt", ".md", ".rst", ".log"},
    "template": {".html", ".htm", ".jinja", ".jinja2", ".j2", ".tmpl", ".mustache"},
    "media": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".mp3", ".wav", ".ogg", ".mp4", ".mov"},
    "compression": {".zip", ".gz", ".bz2", ".xz", ".tar", ".tgz", ".7z"},
    "database": {".db", ".sqlite", ".sqlite3", ".sql"},
}


def node_at(tree: ast.Module, line: int) -> ast.AST | None:
    return next(
        (node for node in ast.walk(tree)
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.lineno == line),
        None,
    )


def path_kind(path: str) -> str:
    suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    for kind, suffixes in PATH_SUFFIXES.items():
        if suffix in suffixes:
            return kind
    return "file"


def resource_class(expression: str, argument: str | None) -> tuple[str, str | None]:
    """Classify a literal or unresolved resource expression without executing it."""
    value = expression.lower()
    literal_argument = argument or ""
    pathk = path_kind(literal_argument) if literal_argument else None
    if value.startswith(("json.", "pickle.", "marshal.", "yaml.")):
        return "serialization", pathk
    if value.startswith(("gzip.", "bz2.", "lzma.", "zipfile.", "tarfile.", "shutil.")):
        return "compression", pathk
    if value.startswith(("importlib.resources", "pkg_resources.")):
        return "package", pathk
    if value.startswith(("configparser.", "tomllib.", "toml.")):
        return "configuration", pathk
    if value.startswith(("PIL.", "imageio.", "wave.", "moviepy.")):
        return "media", pathk
    if value.endswith(("render_template", "render_template_string", "get_template", "TemplateResponse")):
        return "template", pathk
    if value.startswith(("csv.", "xml.", "pandas.")):
        return "structured-data", pathk
    if value.startswith(("sys.", "platform.", "importlib.metadata")):
        return "interpreter", pathk
    if value.startswith(("logging.", "warnings.")):
        return "diagnostics", pathk
    if pathk:
        return pathk, pathk
    return "file", pathk


def call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return "<dynamic>"


def full_call_name(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<dynamic>"


def _loop_depth(node: ast.AST) -> int:
    maximum = 0

    def walk(current: ast.AST, depth: int) -> None:
        nonlocal maximum
        if isinstance(current, (ast.For, ast.AsyncFor, ast.While, ast.comprehension)):
            depth += 1
            maximum = max(maximum, depth)
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) and child is not node:
                continue
            walk(child, depth)

    walk(node, 0)
    return maximum


def local_metrics(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    at_loop: list[tuple[ast.AST, int]] = []

    def walk(current: ast.AST, depth: int) -> None:
        if isinstance(current, (ast.For, ast.AsyncFor, ast.While, ast.comprehension)):
            depth += 1
            operations.append({"kind": "loop", "line": getattr(current, "lineno", None), "depth": depth})
        if isinstance(current, ast.Call):
            name = call_name(current.func)
            calls.append({"expression": full_call_name(current.func), "name": name, "line": current.lineno, "loop_depth": depth})
            if name in SORTING_CALLS:
                operations.append({"kind": "sort", "line": current.lineno, "depth": depth, "bound": "O(n log n)"})
            elif name in ITERATING_CALLS:
                operations.append({"kind": "iterate", "line": current.lineno, "depth": depth, "bound": "O(n)"})
            if name in {"setattr", "getattr", "delattr"}:
                operations.append({"kind": "dynamic-attribute", "line": current.lineno, "depth": depth, "bound": "O(1)"})
        if isinstance(current, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in current.ops):
            operations.append({"kind": "membership", "line": getattr(current, "lineno", None), "depth": depth,
                               "bound": "unknown", "reason": "Container type is not resolved"})
        if isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute) and current.func.attr in COLLECTION_MUTATORS:
            at_loop.append((current, depth))
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) and child is not node:
                continue
            walk(child, depth)

    walk(node, 0)
    recursion = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
        call["name"] == node.name for call in calls
    )
    comprehension = any(isinstance(item, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)) for item in ast.walk(node))
    return {
        "loop_depth": _loop_depth(node), "comprehension": comprehension, "recursion": recursion,
        "calls": calls, "operations": operations,
        "collection_mutation_depth": max((depth for _, depth in at_loop), default=0),
    }


def algorithm_patterns(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[dict[str, Any]]:
    nodes = list(ast.walk(node))
    calls = [call_name(item.func) for item in nodes if isinstance(item, ast.Call)]
    result: list[dict[str, Any]] = []
    if any(isinstance(item, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)) for item in nodes):
        result.append({"name": "Collection transformation", "reason": "Comprehension syntax transforms an iterable."})
    if any(isinstance(item, (ast.For, ast.AsyncFor, ast.While)) for item in nodes) and any(
        name in {"append", "extend", "add", "update"} for name in calls
    ):
        result.append({"name": "Accumulation loop", "reason": "A loop writes into a collection."})
    if any(name in SORTING_CALLS for name in calls):
        result.append({"name": "Sorting", "reason": "Calls sorted() or .sort()."})
    if any(name in {"sum", "min", "max", "any", "all"} for name in calls):
        result.append({"name": "Reduction", "reason": "Uses a standard reduction call."})
    if any(isinstance(item, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in item.ops) for item in nodes):
        result.append({"name": "Membership test", "reason": "Uses in/not-in comparison syntax."})
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(name == node.name for name in calls):
        result.append({"name": "Direct recursion", "reason": "The definition calls its own name."})
    if any(isinstance(item, (ast.Try,)) for item in nodes):
        result.append({"name": "Error handling", "reason": "Contains try/except/finally syntax."})
    return result


def advanced_facts(node: ast.AST) -> dict[str, Any]:
    nodes = list(ast.walk(node))
    methods = {
        item.name for item in nodes
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    decorators = [
        ast.unparse(item) for item in getattr(node, "decorator_list", [])
        if isinstance(item, (ast.Name, ast.Attribute, ast.Call))
    ]
    yields = any(isinstance(item, (ast.Yield, ast.YieldFrom)) for item in nodes)
    is_async = isinstance(node, ast.AsyncFunctionDef) or any(
        isinstance(item, (ast.AsyncFor, ast.AsyncWith, ast.Await)) for item in nodes
    )
    nested = [item for item in nodes if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item is not node]
    outer_names = {
        child.id for child in nodes
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    }
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        outer_names.update(argument.arg for argument in [
            *node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
            *([node.args.vararg] if node.args.vararg else []),
            *([node.args.kwarg] if node.args.kwarg else []),
        ])
    closures = []
    for function in nested:
        parameters = {argument.arg for argument in [
            *function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs,
            *([function.args.vararg] if function.args.vararg else []),
            *([function.args.kwarg] if function.args.kwarg else []),
        ]}
        assigned = {
            child.id for child in ast.walk(function)
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
        }
        free = {
            child.id for child in ast.walk(function)
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
            and child.id not in parameters and child.id not in assigned
            and child.id in outer_names and not hasattr(builtins, child.id)
        }
        if free:
            closures.append({"name": function.name, "line": function.lineno, "free_names": sorted(free)})
    dynamic = sorted({
        call_name(item.func) for item in nodes
        if isinstance(item, ast.Call) and call_name(item.func) in {"getattr", "setattr", "delattr", "eval", "exec", "__import__"}
    })
    concurrency = [
        {"expression": full_call_name(item.func), "line": item.lineno}
        for item in nodes if isinstance(item, ast.Call) and call_name(item.func) in CONCURRENCY_CALLS
    ]
    native = [
        {"expression": full_call_name(item.func), "line": item.lineno}
        for item in nodes if isinstance(item, ast.Call) and call_name(item.func) in NATIVE_CALLS
    ]
    native.extend(
        {"expression": ast.unparse(item), "line": item.lineno}
        for item in nodes if isinstance(item, ast.Constant) and isinstance(item.value, str)
        and item.value.endswith((".so", ".dll", ".dylib", ".pyd"))
    )
    descriptors = sorted(methods & {"__get__", "__set__", "__delete__", "__set_name__"})
    dynamic_members = sorted(methods & {"__getattr__", "__setattr__", "__getattribute__", "__dir__"})
    if any(isinstance(item, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "__slots__" for target in item.targets
    ) for item in nodes):
        dynamic_members.append("__slots__")
    properties = sorted({
        item.name for item in nodes if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            (isinstance(decorator, ast.Name) and decorator.id in {"property", "cached_property"})
            or (isinstance(decorator, ast.Attribute) and decorator.attr in {"setter", "getter", "deleter", "cached_property"})
            or (isinstance(decorator, ast.Call) and call_name(decorator.func) == "property")
            for decorator in item.decorator_list
        )
    })
    return {
        "decorators": decorators,
        "generator": yields,
        "async": is_async,
        "async_generator": yields and isinstance(node, ast.AsyncFunctionDef),
        "context_managers": [
            {"kind": "async-with" if isinstance(item, ast.AsyncWith) else "with", "line": item.lineno}
            for item in nodes if isinstance(item, (ast.With, ast.AsyncWith))
        ],
        "closures": closures,
        "descriptors": descriptors,
        "properties": properties,
        "metaclass": next(
            (ast.unparse(item.value) for item in getattr(node, "keywords", []) if item.arg == "metaclass"), None
        ),
        "dynamic_attributes": dynamic,
        "dynamic_members": dynamic_members,
        "concurrency": concurrency,
        "native_boundaries": native,
        "algorithms": algorithm_patterns(node),
    }
