"""Bounded syntax-level control-flow, def-use, and slicing support."""

from __future__ import annotations

import ast
from collections import defaultdict
from typing import Any, Iterable

DEFAULT_NODE_BUDGET = 400
DEFAULT_EDGE_BUDGET = 1200
DEFAULT_RECORD_BUDGET = 500


def definition_at(tree: ast.Module, line: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find the stored function or method at a source line."""
    return next(
        (node for node in ast.walk(tree)
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno == line),
        None,
    )


def _contains_call(statement: ast.stmt) -> bool:
    return any(isinstance(node, ast.Call) for node in ast.walk(statement))


class ControlFlow:
    def __init__(self, qualified: str, budget: int = DEFAULT_NODE_BUDGET) -> None:
        self.qualified = qualified
        self.budget = budget
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self._ids: set[str] = set()
        self.truncated = False
        self.loops: list[dict[str, Any]] = []

    def node(self, line: int, kind: str, label: str, identifier: str | None = None) -> str:
        base = identifier or f"{kind}-{line}"
        node_id = base
        suffix = 1
        while node_id in self._ids:
            suffix += 1
            node_id = f"{base}-{suffix}"
        self._ids.add(node_id)
        if len(self.nodes) < self.budget or identifier in {"start", "end"}:
            self.nodes.append({"id": node_id, "line": line, "kind": kind, "label": label})
        else:
            self.truncated = True
        return node_id

    def edge(self, source: str, target: str, kind: str) -> None:
        if any(item["source"] == source and item["target"] == target and item["kind"] == kind for item in self.edges):
            return
        self.edges.append({"source": source, "target": target, "kind": kind})

    def sequence(self, statements: list[ast.stmt], incoming: list[str],
                 exceptions: Iterable[str] = ()) -> list[str]:
        current = incoming
        handlers = list(exceptions)
        for statement in statements:
            node_id = self.node(
                statement.lineno, type(statement).__name__.removeprefix("Async").lower(),
                type(statement).__name__.removeprefix("Async"),
                f"line-{statement.lineno}-{statement.col_offset}",
            )
            for source in current:
                self.edge(source, node_id, "next")
            if handlers and _contains_call(statement):
                for handler in handlers:
                    self.edge(node_id, handler, "exception")

            if isinstance(statement, ast.If):
                body = self.sequence(statement.body, [node_id], handlers) or [node_id]
                alternate = self.sequence(statement.orelse, [node_id], handlers) if statement.orelse else [node_id]
                current = body + alternate
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                join = self.node(getattr(statement, "end_lineno", statement.lineno) or statement.lineno,
                                 "join", "loop exit")
                loop_context = {"head": node_id, "join": join}
                self.loops.append(loop_context)
                loop = self.sequence(statement.body, [node_id], handlers)
                self.loops.pop()
                for source in loop:
                    self.edge(source, node_id, "loop")
                for source in loop_context.get("breaks", []):
                    self.edge(source, join, "break")
                for source in loop_context.get("continues", []):
                    self.edge(source, node_id, "continue")
                alternate = self.sequence(statement.orelse, [node_id], handlers) if statement.orelse else [node_id]
                for source in alternate:
                    self.edge(source, join, "loop-else")
                current = [join]
            elif isinstance(statement, ast.Try):
                handler_nodes = [
                    self.node(handler.lineno, "except", "ExceptHandler",
                              f"except-{handler.lineno}-{handler.col_offset}")
                    for handler in statement.handlers
                ]
                for handler_node in handler_nodes:
                    self.edge(node_id, handler_node, "exception")
                body = self.sequence(statement.body, [node_id], [*handler_nodes, *handlers])
                handler_exits = [
                    exit_node for handler, handler_node in zip(statement.handlers, handler_nodes)
                    for exit_node in self.sequence(handler.body, [handler_node], handlers)
                ]
                alternate = self.sequence(statement.orelse, body, handlers) if statement.orelse else body
                merged = alternate + handler_exits
                final = self.sequence(statement.finalbody, merged, handlers) if statement.finalbody else merged
                if statement.finalbody:
                    for source in [node_id, *handler_nodes]:
                        self.edge(source, final[0] if final else node_id, "finally")
                current = final or ([node_id] if statement.finalbody else [])
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                current = self.sequence(statement.body, [node_id], handlers) or [node_id]
            elif isinstance(statement, ast.Match):
                exits = [node_id]
                for case in statement.cases:
                    case_node = self.node(case.pattern.lineno, "case", "match case",
                                          f"case-{case.pattern.lineno}-{case.pattern.col_offset}")
                    self.edge(node_id, case_node, "case")
                    exits.extend(self.sequence(case.body, [case_node], handlers) or [case_node])
                current = exits
            elif isinstance(statement, ast.Break):
                if self.loops:
                    self.loops[-1].setdefault("breaks", []).append(node_id)
                current = []
            elif isinstance(statement, ast.Continue):
                if self.loops:
                    self.loops[-1].setdefault("continues", []).append(node_id)
                current = []
            elif isinstance(statement, (ast.Return, ast.Raise)):
                self.edge(node_id, "end", "return" if isinstance(statement, ast.Return) else "raise")
                current = []
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, (ast.Yield, ast.YieldFrom)):
                yielded = next((item for item in self.nodes if item["id"] == node_id), None)
                if yielded:
                    yielded["kind"] = "yield"
                    yielded["label"] = "Yield"
                current = [node_id]
            else:
                current = [node_id]
        return current

    def build(self, definition: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        start = self.node(definition.lineno, "start", self.qualified, "start")
        self.node(definition.end_lineno or definition.lineno, "end", "end", "end")
        exits = self.sequence(definition.body, [start])
        for source in exits:
            self.edge(source, "end", "exit")
        return self.nodes, self.edges

    def truncate_edges(self, budget: int = DEFAULT_EDGE_BUDGET) -> bool:
        if len(self.edges) <= budget:
            return self.truncated
        self.edges = self.edges[:budget]
        self.truncated = True
        return True


def _scope_name(parent: str, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    return f"{parent}.{getattr(node, 'name', '<scope>')}"


def _target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for item in target.elts for name in _target_names(item)]
    if isinstance(target, ast.Attribute):
        return [ast.unparse(target)]
    if isinstance(target, ast.Subscript):
        return [ast.unparse(target)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _expression_names(expression: str | None) -> list[str]:
    if not expression:
        return []
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return []
    return sorted({ast.unparse(node) for node in ast.walk(tree)
                   if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)) and isinstance(node.ctx, ast.Load)})


class DefUse:
    def __init__(self, qualified: str, budget: int = DEFAULT_RECORD_BUDGET) -> None:
        self.root = qualified
        self.budget = budget
        self.scope = qualified
        self.definitions: list[dict[str, Any]] = []
        self.uses: list[dict[str, Any]] = []
        self.boundaries: list[dict[str, Any]] = []
        self.truncated = False

    def add(self, collection: list[dict[str, Any]], record: dict[str, Any]) -> None:
        if len(collection) < self.budget:
            collection.append(record)
        else:
            self.truncated = True

    def define(self, name: str, kind: str, line: int, expression: str | None = None) -> None:
        self.add(self.definitions, {
            "scope": self.scope, "name": name, "kind": kind, "line": line,
            "expression": expression, "dependencies": _expression_names(expression),
        })

    def use(self, name: str, line: int) -> None:
        self.add(self.uses, {"scope": self.scope, "name": name, "line": line})

    def boundary(self, kind: str, expression: str, line: int) -> None:
        self.add(self.boundaries, {"scope": self.scope, "kind": kind, "expression": expression, "line": line})

    def visit(self, node: ast.AST, root: bool = False) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not root:
            self.define(node.name, "function", node.lineno)
            previous = self.scope
            self.scope = _scope_name(previous, node)
            self._arguments(node)
            for statement in node.body:
                self.visit(statement)
            self.scope = previous
            return
        if isinstance(node, ast.ClassDef) and not root:
            self.define(node.name, "class", node.lineno)
            previous = self.scope
            self.scope = _scope_name(previous, node)
            for statement in node.body:
                self.visit(statement)
            self.scope = previous
            return
        if isinstance(node, ast.Assign):
            expression = ast.unparse(node.value)
            for target in node.targets:
                for name in _target_names(target):
                    self.define(name, "assignment", node.lineno, expression)
                self.visit(target)
            self.visit(node.value)
            return
        if isinstance(node, ast.AnnAssign):
            expression = ast.unparse(node.value) if node.value else None
            for name in _target_names(node.target):
                self.define(name, "assignment", node.lineno, expression)
            self.visit(node.target)
            if node.value:
                self.visit(node.value)
            return
        if isinstance(node, ast.AugAssign):
            names = _target_names(node.target)
            for name in names:
                self.define(name, "mutation", node.lineno, ast.unparse(node))
                self.use(name, node.lineno)
            self.visit(node.value)
            return
        if isinstance(node, (ast.For, ast.AsyncFor)):
            for name in _target_names(node.target):
                self.define(name, "loop", node.lineno, ast.unparse(node.iter))
            self.visit(node.iter)
            for statement in node.body + node.orelse:
                self.visit(statement)
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars:
                    for name in _target_names(item.optional_vars):
                        self.define(name, "context", node.lineno, ast.unparse(item.context_expr))
                self.visit(item.context_expr)
            for statement in node.body:
                self.visit(statement)
            return
        if isinstance(node, ast.NamedExpr):
            for name in _target_names(node.target):
                self.define(name, "assignment", node.lineno, ast.unparse(node.value))
            self.visit(node.value)
            return
        if isinstance(node, ast.comprehension):
            for name in _target_names(node.target):
                self.define(name, "comprehension", node.target.lineno, ast.unparse(node.iter))
            self.visit(node.iter)
            for condition in node.ifs:
                self.visit(condition)
            return
        if isinstance(node, ast.ExceptHandler):
            if node.name:
                self.define(node.name, "exception", node.lineno, ast.unparse(node.type) if node.type else None)
            for statement in node.body:
                self.visit(statement)
            return
        if isinstance(node, ast.Call):
            self._mutation(node)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            self.use(node.id, node.lineno)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            self.use(ast.unparse(node), node.lineno)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            self.use(ast.unparse(node), node.lineno)
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def _arguments(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg:
            arguments.append(node.args.vararg)
        if node.args.kwarg:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            self.define(argument.arg, "parameter", argument.lineno, ast.unparse(argument.annotation) if argument.annotation else None)

    def _mutation(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in {
            "append", "extend", "insert", "remove", "pop", "clear", "add", "discard",
            "update", "setdefault", "sort", "reverse",
        }:
            return
        receiver = ast.unparse(node.func.value)
        self.define(receiver, "mutation", node.lineno, ast.unparse(node))
        self.boundary("mutation", receiver, node.lineno)

    def records(self, definition: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
        self._arguments(definition)
        for statement in definition.body:
            self.visit(statement)
        return {
            "definitions": self.definitions, "uses": self.uses, "boundaries": self.boundaries,
            "truncated": self.truncated,
        }


def def_use(definition: ast.FunctionDef | ast.AsyncFunctionDef, qualified: str,
            budget: int = DEFAULT_RECORD_BUDGET) -> dict[str, Any]:
    return DefUse(qualified, budget).records(definition)


def _edges_for_scope(definitions: list[dict[str, Any]], uses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Connect a use to its nearest preceding definition in the same scope and name."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    events = sorted(
        [*((item["line"], 0, item) for item in definitions), *((item["line"], 1, item) for item in uses)],
        key=lambda item: (item[0], item[1]),
    )
    edges: list[dict[str, Any]] = []
    for _, kind, item in events:
        key = (item["scope"], item["name"])
        if kind == 0:
            latest[key] = item
        elif key in latest:
            edges.append({
                "source": latest[key]["name"], "target": item["name"], "scope": item["scope"],
                "source_line": latest[key]["line"], "target_line": item["line"], "kind": "def-use",
            })
    return edges


def _slice_records(variable: str, records: dict[str, Any], data_edges: list[dict[str, Any]],
                   direction: str, budget: int) -> dict[str, Any]:
    definitions = [item for item in records["definitions"] if item["name"] == variable]
    uses = [item for item in records["uses"] if item["name"] == variable]
    boundary = [
        item for item in records["boundaries"]
        if item["expression"] == variable or variable in item["expression"]
    ]
    selected_definitions = {id(item) for item in definitions}
    selected_uses = {id(item) for item in uses}
    selected_edges: list[dict[str, Any]] = []
    selected_data: list[dict[str, Any]] = []
    truncated = records["truncated"]

    def add_data(item: dict[str, Any]) -> bool:
        nonlocal truncated
        if item in selected_data:
            return False
        if len(selected_data) >= budget:
            truncated = True
            return False
        selected_data.append(item)
        return True

    def backward(names: set[str]) -> None:
        pending = list(names)
        seen: set[str] = set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            for definition in records["definitions"]:
                if definition["name"] != name:
                    continue
                if id(definition) not in selected_definitions:
                    if len(selected_definitions) >= budget:
                        truncated = True
                        continue
                    selected_definitions.add(id(definition))
                    pending.extend(definition["dependencies"])
                for edge in data_edges:
                    if edge.get("target_name") == name:
                        add_data(edge)
            for edge in data_edges:
                if edge.get("target_name") == name and edge.get("source_name"):
                    pending.append(edge["source_name"])

    def forward(names: set[str]) -> None:
        pending = list(names)
        seen: set[str] = set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            for use in records["uses"]:
                if use["name"] == name and id(use) not in selected_uses:
                    if len(selected_uses) >= budget:
                        truncated = True
                        continue
                    selected_uses.add(id(use))
            for definition in records["definitions"]:
                if name not in definition["dependencies"]:
                    continue
                if id(definition) not in selected_definitions:
                    if len(selected_definitions) >= budget:
                        truncated = True
                        continue
                    selected_definitions.add(id(definition))
                    pending.append(definition["name"])
                for edge in data_edges:
                    if edge.get("target_name") == definition["name"]:
                        add_data(edge)
            for edge in data_edges:
                if edge.get("source_name") == name and edge.get("target_name"):
                    add_data(edge)
                    pending.append(edge["target_name"])

    if direction in {"both", "backward"}:
        backward({variable})
    if direction in {"both", "forward"}:
        forward({variable})
    events = sorted(
        [*((item["line"], "definition", item) for item in records["definitions"] if id(item) in selected_definitions),
         *((item["line"], "use", item) for item in records["uses"] if id(item) in selected_uses)],
        key=lambda item: (item[0], item[1]),
    )
    all_edges = _edges_for_scope(records["definitions"], records["uses"])
    selected_edges = [
        edge for edge in all_edges
        if (edge["source_line"], edge["source"]) in {(item["line"], item["name"]) for item in records["definitions"] if id(item) in selected_definitions}
        and (edge["target_line"], edge["target"]) in {(item["line"], item["name"]) for item in records["uses"] if id(item) in selected_uses}
    ]
    return {
        "direction": direction,
        "definitions": [item for item in records["definitions"] if id(item) in selected_definitions],
        "uses": [item for item in records["uses"] if id(item) in selected_uses],
        "events": [item for _, _, item in events],
        "def_use_edges": selected_edges,
        "data_edges": selected_data,
        "boundaries": boundary,
        "truncated": truncated,
        "budget": budget,
    }


def slice_records(variable: str, records: dict[str, Any], data_edges: list[dict[str, Any]],
                  direction: str = "both", budget: int = DEFAULT_RECORD_BUDGET) -> dict[str, Any]:
    return _slice_records(variable, records, data_edges, direction, budget)
