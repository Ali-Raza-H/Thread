"""Cycle-safe value summaries and restricted, read-only debugger expressions."""

from __future__ import annotations

import ast
import json
from typing import Any

MAX_DEPTH = 3
MAX_ITEMS = 20
MAX_STRING = 160
MAX_NODES = 80


def configure_budgets(depth: int | None = None, items: int | None = None, byte_limit: int | None = None) -> None:
    """Adjust per-process value budgets without allowing unbounded values."""
    global MAX_DEPTH, MAX_ITEMS, MAX_STRING
    MAX_DEPTH = max(1, min(depth or MAX_DEPTH, 10))
    MAX_ITEMS = max(1, min(items or MAX_ITEMS, 200))
    MAX_STRING = max(16, min(byte_limit or MAX_STRING, 4096))
_SENSITIVE = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential", "cookie", "auth")
_OBJECT_IDS: dict[int, str] = {}
_NEXT_OBJECT_ID = 1


def reset_object_ids() -> None:
    global _NEXT_OBJECT_ID
    _OBJECT_IDS.clear()
    _NEXT_OBJECT_ID = 1


def object_id(value: Any) -> str | None:
    """Stable identity for this run only; immutable scalars intentionally have none."""
    global _NEXT_OBJECT_ID
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return None
    identity = id(value)
    if identity not in _OBJECT_IDS:
        _OBJECT_IDS[identity] = f"obj-{_NEXT_OBJECT_ID}"
        _NEXT_OBJECT_ID += 1
    return _OBJECT_IDS[identity]


def _bounded_text(value: str) -> dict[str, Any]:
    encoded = value.encode("utf-8", errors="replace")
    preview = encoded[:MAX_STRING].decode("utf-8", errors="replace")
    return {"preview": preview, "length": len(value), "truncated": len(encoded) > MAX_STRING}


def safe_value(value: Any, name: str = "", depth: int = 0, seen: set[int] | None = None,
               max_depth: int | None = None, max_items: int | None = None) -> dict[str, Any]:
    """Serialize a value without following cycles or unbounded containers."""
    max_depth = MAX_DEPTH if max_depth is None else max_depth
    max_items = MAX_ITEMS if max_items is None else max_items
    seen = seen if seen is not None else set()
    result: dict[str, Any] = {"type": type(value).__name__}
    identity = object_id(value)
    if identity:
        result["object_id"] = identity
    if any(word in name.lower() for word in _SENSITIVE):
        result["value"] = "<redacted>"
        return result
    if value is None or isinstance(value, bool):
        result["value"] = value
        return result
    if isinstance(value, (int, float)):
        result["value"] = value
        return result
    if isinstance(value, str):
        result.update(_bounded_text(value))
        return result
    if isinstance(value, bytes):
        result.update({"length": len(value), "preview": value[:MAX_STRING].hex()})
        return result
    if depth >= max_depth:
        result["summary"] = "<depth limit>"
        result["truncated"] = True
        return result
    identity_value = id(value)
    if identity_value in seen:
        result["cycle"] = True
        result["reference"] = identity
        return result
    seen = {*seen, identity_value}
    if isinstance(value, dict):
        items = list(value.items())[:max_items]
        result.update({
            "length": len(value), "truncated": len(value) > max_items,
            "items": [{"key": safe_value(key, "", depth + 1, seen, max_depth, max_items),
                       "value": safe_value(item, str(key), depth + 1, seen, max_depth, max_items)}
                      for key, item in items],
        })
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)[:max_items]
        result.update({
            "length": len(value), "truncated": len(value) > max_items,
            "items": [safe_value(item, "", depth + 1, seen, max_depth, max_items) for item in items],
        })
        return result
    try:
        namespace = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError):
        namespace = None
    if isinstance(namespace, dict):
        selected = list(namespace.items())[:max_items]
        result["attributes"] = {
            str(key): safe_value(item, str(key), depth + 1, seen, max_depth, max_items)
            for key, item in selected
        }
        result["attribute_count"] = len(namespace)
        result["attribute_truncated"] = len(namespace) > max_items
    else:
        result["value"] = "<native/opaque object>"
    return result


def binding_changes(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Distinguish created, rebound, mutated, removed, and unknown bindings."""
    created, rebound, mutated, removed, unknown = {}, {}, {}, [], []
    for name, item in current.items():
        if name not in previous:
            created[name] = item
            continue
        before = previous[name]
        if before.get("object_id") and item.get("object_id"):
            if before["object_id"] != item["object_id"]:
                rebound[name] = item
            elif before != item:
                mutated[name] = item
        elif before != item:
            rebound[name] = item
        if item.get("value") == "<native/opaque object>" or item.get("summary") == "<depth limit>":
            unknown.append(name)
    for name in previous:
        if name not in current:
            removed.append(name)
    return {
        "created": created,
        "rebound": rebound,
        "mutated": mutated,
        "removed": sorted(removed),
        "unknown": sorted(set(unknown)),
        # Compatibility for the existing TUI patch application.
        "changed": {**rebound, **mutated},
    }


class UnsafeExpression(ValueError):
    pass


def validate_expression(expression: str) -> ast.Expression:
    """Parse the restricted condition/watch grammar without executing it."""
    if not expression or len(expression) > 500:
        raise UnsafeExpression("Expression must be between 1 and 500 characters.")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpression("Expression is not valid Python.") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise UnsafeExpression("Expression is too complex.")
    allowed = (
        ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Name, ast.Constant,
        ast.Attribute, ast.Subscript, ast.Tuple, ast.List, ast.Load, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Eq, ast.NotEq, ast.Lt,
        ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
    )
    for node in nodes:
        if not isinstance(node, allowed):
            raise UnsafeExpression(f"{type(node).__name__} is not allowed in a debugger expression.")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise UnsafeExpression("Dunder names are not allowed.")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise UnsafeExpression("Dunder attributes are not allowed.")
        if isinstance(node, ast.UnaryOp) and not isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
            raise UnsafeExpression("Unary operator is not allowed.")
    return tree


def _attribute(base: Any, name: str) -> Any:
    if name.startswith("__"):
        raise UnsafeExpression("Dunder attributes are not allowed.")
    if isinstance(base, dict) and name in base:
        return base[name]
    try:
        namespace = object.__getattribute__(base, "__dict__")
    except (AttributeError, TypeError):
        namespace = None
    if isinstance(namespace, dict) and name in namespace:
        return namespace[name]
    raise UnsafeExpression(f"Attribute '{name}' is not a stored read-only value.")


def _plain(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str, bytes))


def _subscript(base: Any, key: Any) -> Any:
    if not isinstance(base, (dict, list, tuple, str, bytes)):
        raise UnsafeExpression("Subscript is only allowed on built-in containers.")
    try:
        return base[key]
    except (KeyError, IndexError, TypeError) as exc:
        raise UnsafeExpression("Subscript is out of range or invalid.") from exc


def evaluate_expression(expression: str, variables: dict[str, Any]) -> Any:
    """Evaluate only literals, names, safe attributes, subscripts, and comparisons."""
    tree = validate_expression(expression)

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise UnsafeExpression(f"Name '{node.id}' is not in this frame.")
            return variables[node.id]
        if isinstance(node, ast.Attribute):
            return _attribute(visit(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            return _subscript(visit(node.value), visit(node.slice))
        if isinstance(node, (ast.Tuple, ast.List)):
            return tuple(visit(item) for item in node.elts)
        if isinstance(node, ast.UnaryOp):
            value = visit(node.operand)
            if isinstance(node.op, ast.Not):
                return not value
            if isinstance(node.op, ast.USub):
                return -value
            return +value
        if isinstance(node, ast.BoolOp):
            values = [visit(item) for item in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if not _plain(left) or not _plain(right):
                raise UnsafeExpression("Arithmetic is limited to primitive values.")
            operations = {ast.Add: lambda: left + right, ast.Sub: lambda: left - right,
                          ast.Mult: lambda: left * right, ast.Div: lambda: left / right,
                          ast.FloorDiv: lambda: left // right, ast.Mod: lambda: left % right}
            operation = operations.get(type(node.op))
            if operation is None:
                raise UnsafeExpression("Binary operator is not allowed.")
            try:
                return operation()
            except (TypeError, ValueError, ZeroDivisionError) as exc:
                raise UnsafeExpression("Binary operation failed safely.") from exc
        if isinstance(node, ast.Compare):
            left = visit(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = visit(comparator)
                if isinstance(op, (ast.In, ast.NotIn)):
                    if not _plain(left) or not isinstance(right, (list, tuple, set, frozenset, dict, str, bytes)):
                        raise UnsafeExpression("Membership is limited to primitive values and built-in containers.")
                elif not _plain(left) or not _plain(right):
                    raise UnsafeExpression("Comparisons are limited to primitive values.")
                checks = {
                    ast.Eq: lambda: left == right, ast.NotEq: lambda: left != right,
                    ast.Lt: lambda: left < right, ast.LtE: lambda: left <= right,
                    ast.Gt: lambda: left > right, ast.GtE: lambda: left >= right,
                    ast.In: lambda: left in right, ast.NotIn: lambda: left not in right,
                    ast.Is: lambda: left is right, ast.IsNot: lambda: left is not right,
                }
                check = checks.get(type(op))
                if check is None:
                    raise UnsafeExpression("Comparison is not allowed.")
                try:
                    if not check():
                        return False
                except (TypeError, ValueError) as exc:
                    raise UnsafeExpression("Comparison failed safely.") from exc
                left = right
            return True
        raise UnsafeExpression("Expression node is not allowed.")

    return visit(tree)


def condition_result(expression: str | None, variables: dict[str, Any]) -> bool:
    if not expression:
        return True
    try:
        return bool(evaluate_expression(expression, variables))
    except Exception:
        return False


def json_condition(expression: str | None) -> str | None:
    """Return the validated source expression or raise for unsafe input."""
    if not expression:
        return None
    validate_expression(expression)
    return expression


def serialise(value: Any) -> str:
    return json.dumps(value, sort_keys=True)
