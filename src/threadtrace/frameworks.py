"""Framework-specific entry-point analysis from stored AST facts.

Every result requires syntax that names both a recognised framework and a
registration object.  Names that merely resemble a route or command are left
to generic Python analysis.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FrameworkEntry:
    kind: str
    name: str
    handler: str | None
    module: str | None
    framework: str
    version: str | None
    variant: str | None
    inputs: dict[str, Any]
    confidence: str
    evidence_kind: str
    line: int
    path: str | None
    expression: str
    limitations: tuple[str, ...] = field(default_factory=tuple)
    capability: str = "full"


_FACTORIES = {
    "flask.Flask": ("flask", "application"),
    "flask.Blueprint": ("flask", "blueprint"),
    "quart.Quart": ("quart", "application"),
    "quart.Blueprint": ("quart", "blueprint"),
    "fastapi.FastAPI": ("fastapi", "application"),
    "fastapi.APIRouter": ("fastapi", "router"),
    "starlette.applications.Starlette": ("starlette", "application"),
    "starlette.routing.Router": ("starlette", "router"),
    "aiohttp.web.Application": ("aiohttp", "application"),
    "click.Command": ("click", "command"),
    "click.Group": ("click", "group"),
    "typer.Typer": ("typer", "application"),
    "argparse.ArgumentParser": ("argparse", "parser"),
    "optparse.OptionParser": ("optparse", "parser"),
    "celery.Celery": ("celery", "application"),
    "apscheduler.schedulers.background.BackgroundScheduler": ("apscheduler", "scheduler"),
    "apscheduler.schedulers.blocking.BlockingScheduler": ("apscheduler", "scheduler"),
    "apscheduler.schedulers.asyncio.AsyncIOScheduler": ("apscheduler", "scheduler"),
}
_ROUTE_ACTIONS = {"get", "post", "put", "patch", "delete", "head", "options", "route", "api_route", "websocket"}
_FLASK_HOOKS = {"before_request", "after_request", "teardown_request", "teardown_appcontext", "before_first_request"}
_WORKER_DECORATORS = {
    "celery.shared_task": ("celery", "task"),
    "celery.task": ("celery", "task"),
    "rq.job": ("rq", "task"),
    "dramatiq.actor": ("dramatiq", "task"),
    "huey.task": ("huey", "task"),
    "huey.periodic_task": ("huey", "scheduled"),
}
_DB_CALLS = {
    "execute": "execute", "executemany": "execute", "executescript": "execute",
    "query": "read", "filter": "read", "filter_by": "read", "get": "read", "all": "read",
    "first": "read", "one": "read", "one_or_none": "read", "scalar": "read",
    "select": "read", "insert": "create", "update": "update", "delete": "delete",
    "add": "create", "add_all": "create", "merge": "update", "save": "create",
    "commit": "commit", "rollback": "rollback", "flush": "flush",
}


@dataclass
class _Object:
    framework: str
    variant: str
    module: str
    line: int


def _value(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _literal(expression: str) -> Any:
    try:
        return ast.literal_eval(expression)
    except (ValueError, SyntaxError, TypeError):
        return None


def _root(expression: str) -> str:
    return expression.split(".", 1)[0]


def _bindings(imports: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for item in imports:
        if item.get("type_checking"):
            continue
        module = item.get("module_name") or ""
        target = item.get("target_module") or ""
        imported = item.get("imported_name")
        visible = item.get("alias") or imported or target.split(".", 1)[0]
        if not visible:
            continue
        full = f"{target}.{imported}" if imported and target else target
        result.setdefault(module, {})[visible] = full
    return result


def _resolve(expression: str, aliases: dict[str, str]) -> str:
    root = _root(expression)
    return aliases.get(root, root) + expression[len(root):]


def _factory(full: str) -> tuple[str, str] | None:
    for signature, result in _FACTORIES.items():
        if full == signature or full.endswith("." + signature):
            return result
    return None


def _objects(calls: list[dict[str, Any]], aliases: dict[str, dict[str, str]]) -> dict[tuple[str, str], _Object]:
    result: dict[tuple[str, str], _Object] = {}
    for call in calls:
        target = call.get("assignment_target")
        if not target:
            continue
        full = _resolve(call["target_expression"], aliases.get(call["module_name"], {}))
        factory = _factory(full)
        if factory:
            framework, variant = factory
            result[(call["module_name"], target)] = _Object(framework, variant, call["module_name"], call["line_number"])
    return result


def _decorator_call(text: str) -> tuple[str, list[str], dict[str, str]] | None:
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError:
        return None
    try:
        if isinstance(node, (ast.Name, ast.Attribute)):
            return ast.unparse(node), [], {}
        if not isinstance(node, ast.Call):
            return None
        name = ast.unparse(node.func)
        arguments = [ast.unparse(item) for item in node.args]
        keywords = {item.arg: ast.unparse(item.value) for item in node.keywords if item.arg}
    except (ValueError, RecursionError):
        return None
    return name, arguments, keywords


def _literal_arguments(arguments: list[str]) -> list[Any]:
    return [value for value in (_literal(item) for item in arguments) if isinstance(value, (str, int, float))]


def _route_inputs(symbol: dict[str, Any], route: str | None, methods: list[str], **extra: Any) -> dict[str, Any]:
    parameters = _value(symbol.get("parameters_json"), [])
    path_names = set(re.findall(r"{([^}:]+)(?::[^}]+)?}", route or ""))
    path_names.update(re.findall(r"<(?:\w+:)?(\w+)>", route or ""))
    return {
        "route": route, "method": methods[0] if methods else None, "methods": methods, "handler_parameters": parameters,
        "handler_decorators": symbol.get("decorators", []), "path_parameters": sorted(path_names),
        "signature": symbol.get("signature"),
        **extra,
    }


def _entry(kind: str, name: str, symbol: dict[str, Any] | None, framework: str, version: str | None,
           variant: str | None, inputs: dict[str, Any], confidence: str, evidence_kind: str,
           line: int, path: str | None, expression: str, limitations: tuple[str, ...] = (),
           capability: str = "full", handler: str | None = None) -> FrameworkEntry:
    inputs = {"framework": framework, "version": version, "variant": variant,
              "limitations": list(limitations), "capability": capability, **inputs}
    return FrameworkEntry(kind, name, handler or (symbol["qualified_name"] if symbol else None),
                          symbol["module_name"] if symbol else None, framework, version, variant,
                          inputs, confidence, evidence_kind, line, path, expression, limitations, capability)


def _versions(values: dict[str, str], name: str) -> str | None:
    return values.get(name) or values.get(name.replace("-", "_"))


def _route_entries(module: str, symbols: list[dict[str, Any]], aliases: dict[str, str],
                   objects: dict[tuple[str, str], _Object], versions: dict[str, str]) -> list[FrameworkEntry]:
    entries: list[FrameworkEntry] = []
    for symbol in symbols:
        for decorator in symbol.get("decorators", []):
            parsed = _decorator_call(decorator)
            if not parsed:
                continue
            call, arguments, keywords = parsed
            if "." not in call:
                continue
            receiver, action = call.rsplit(".", 1)
            if action not in _ROUTE_ACTIONS:
                continue
            obj = objects.get((module, receiver))
            if not obj or obj.framework not in {"flask", "quart", "fastapi", "starlette"}:
                continue
            if obj.variant not in {"application", "router", "blueprint"}:
                continue
            literals = _literal_arguments(arguments)
            route = str(literals[0]) if literals and isinstance(literals[0], str) else None
            methods = [action.upper()] if action not in {"route", "api_route"} else ["GET"]
            method_value = _literal(keywords.get("methods", ""))
            if action == "websocket":
                methods = ["WEBSOCKET"]
            elif isinstance(method_value, (list, tuple)):
                methods = [str(item).upper() for item in method_value]
            version = _versions(versions, obj.framework)
            limitations = (
                "Dynamic route construction and indirect registration are not resolved.",
                "The declared dependency constraint is not proof of the installed framework version.",
            )
            inputs = _route_inputs(
                symbol, route, methods, decorator=decorator, registration=decorator,
                dependencies=keywords.get("dependencies"), response_model=keywords.get("response_model"),
                status_code=keywords.get("status_code"),
            )
            entries.append(_entry(
                "route", f"{methods[0] if methods else 'ROUTE'} {route or symbol['name']}", symbol, obj.framework, version,
                obj.variant, inputs, "high" if route else "possible", "decorator", symbol["line_start"],
                symbol.get("relative_path"), decorator, limitations,
                "full" if route else "partial",
            ))
        for decorator in symbol.get("decorators", []):
            parsed = _decorator_call(decorator)
            if not parsed:
                continue
            call, _, keywords = parsed
            receiver, _, action = call.rpartition(".")
            obj = objects.get((module, receiver)) if receiver else None
            if obj and obj.framework in {"flask", "quart"} and action in _FLASK_HOOKS:
                entries.append(_entry(
                    "lifecycle", f"{obj.framework} {action}: {symbol['name']}", symbol, obj.framework,
                    _versions(versions, obj.framework), obj.variant, {"hook": action, "registration": decorator},
                    "high", "decorator", symbol["line_start"], symbol.get("relative_path"), decorator,
                    ("Hook ordering is not evaluated.",),
                ))
            if obj and obj.framework == "fastapi" and action == "middleware":
                entries.append(_entry(
                    "middleware", f"fastapi middleware: {symbol['name']}", symbol, "fastapi",
                    _versions(versions, "fastapi"), obj.variant, {"registration": decorator}, "high",
                    "decorator", symbol["line_start"], symbol.get("relative_path"), decorator,
                    ("Middleware ordering and short-circuit behaviour are not evaluated.",),
                ))
            if obj and obj.framework == "fastapi" and action == "on_event":
                arguments = _literal_arguments(parsed[1])
                event = str(arguments[0]) if arguments else None
                entries.append(_entry(
                    "lifecycle", f"fastapi {event or 'event'}: {symbol['name']}", symbol, "fastapi",
                    _versions(versions, "fastapi"), obj.variant, {"event": event, "registration": decorator},
                    "high" if event else "possible", "decorator", symbol["line_start"], symbol.get("relative_path"),
                    decorator, ("Lifespan ordering is not evaluated.",),
                ))
    return entries


def _django_entries(module: str, calls: list[dict[str, Any]], aliases: dict[str, str],
                    symbols_by_name: dict[str, dict[str, Any]], versions: dict[str, str]) -> list[FrameworkEntry]:
    entries: list[FrameworkEntry] = []
    for call in calls:
        full = _resolve(call["target_expression"], aliases)
        if full not in {"django.urls.path", "django.urls.re_path"} and not full.endswith(".django.urls.path"):
            continue
        arguments = _value(call.get("arguments_json"), [])
        keywords = _value(call.get("keywords_json"), {})
        literals = _literal_arguments(arguments)
        route = str(literals[0]) if literals and isinstance(literals[0], str) else None
        handler = arguments[1] if len(arguments) > 1 else None
        symbol = symbols_by_name.get((handler or "").replace(".as_view()", "").split(".")[-1])
        limitations = ("Django includes and URLResolver prefixes are not expanded.", "Class-based view method dispatch is not resolved.")
        entries.append(_entry(
            "route", f"DJANGO {route or module}", symbol, "django", _versions(versions, "django"), "urlconf",
            {"route": route, "handler_expression": handler, "view_name": keywords.get("name"),
             "registration": call["target_expression"]},
            "high" if route and handler else "possible", "call", call["line_number"], call["relative_path"],
            call["target_expression"], limitations, "full" if route and handler else "partial", handler=handler,
        ))
    for call in calls:
        full = _resolve(call["target_expression"], aliases)
        if full.rsplit(".", 1)[-1] not in {"add_get", "add_post", "add_put", "add_patch", "add_delete", "add_route"}:
            continue
        if not full.startswith(("aiohttp.", "web.")) and ".add_" not in full:
            continue
        if "aiohttp" not in " ".join(aliases.values()) and full.startswith("web."):
            continue
        if not full.startswith("aiohttp.") and "aiohttp" not in " ".join(aliases.values()):
            continue
        arguments = _value(call.get("arguments_json"), [])
        keywords = _value(call.get("keywords_json"), {})
        literals = _literal_arguments(arguments)
        route = str(literals[0]) if literals and isinstance(literals[0], str) else None
        handler = arguments[1] if len(arguments) > 1 else None
        method = full.rsplit(".", 1)[-1].removeprefix("add_")
        methods = [method.upper()] if method != "route" else [str(keywords.get("method", "GET")).upper()]
        symbol = symbols_by_name.get((handler or "").split(".")[-1])
        entries.append(_entry(
            "route", f"{' '.join(methods)} {route or module}", symbol, "aiohttp", _versions(versions, "aiohttp"),
            "router", {"route": route, "methods": methods, "handler_expression": handler,
                       "registration": call["target_expression"]},
            "high" if route and handler else "possible", "call", call["line_number"], call["relative_path"],
            call["target_expression"], ("Route prefixes and sub-applications are not expanded.",),
            "full" if route and handler else "partial", handler=handler,
        ))
    for call in calls:
        full = _resolve(call["target_expression"], aliases)
        if full not in {"starlette.routing.Route", "starlette.routing.WebSocketRoute", "starlette.routing.Mount"}:
            continue
        arguments = _value(call.get("arguments_json"), [])
        keywords = _value(call.get("keywords_json"), {})
        literals = _literal_arguments(arguments)
        route = str(literals[0]) if literals and isinstance(literals[0], str) else None
        handler = arguments[1] if len(arguments) > 1 else None
        method_value = _literal(keywords.get("methods", ""))
        methods = [str(item).upper() for item in method_value] if isinstance(method_value, (list, tuple)) else ["GET"]
        symbol = symbols_by_name.get((handler or "").split(".")[-1])
        entries.append(_entry(
            "route", f"{' '.join(methods)} {route or module}", symbol, "starlette",
            _versions(versions, "starlette"), "router",
            {"route": route, "methods": methods, "handler_expression": handler,
             "registration": call["target_expression"]},
            "high" if route and handler else "possible", "call", call["line_number"], call["relative_path"],
            call["target_expression"], ("Mount nesting and route ordering are not expanded.",),
            "full" if route and handler else "partial", handler=handler,
        ))
    return entries


def _argument_inputs(arguments: list[str], keywords: dict[str, str]) -> dict[str, Any]:
    literals = _literal_arguments(arguments)
    flags = [item for item in literals if isinstance(item, str) and item.startswith("-")]
    positional = next((item for item in literals if isinstance(item, str) and not item.startswith("-")), None)
    default = _literal(keywords["default"]) if "default" in keywords else None
    return {
        "flags": flags, "name": keywords.get("dest") or positional or (flags[-1].lstrip("-") if flags else None),
        "default": default, "required": keywords.get("required"), "action": keywords.get("action"),
        "choices": keywords.get("choices"), "type": keywords.get("type"), "help": keywords.get("help"),
    }


def _cli_entries(module: str, symbols: list[dict[str, Any]], calls: list[dict[str, Any]],
                 aliases: dict[str, str], objects: dict[tuple[str, str], _Object],
                 symbols_by_name: dict[str, dict[str, Any]], versions: dict[str, str]) -> list[FrameworkEntry]:
    entries: list[FrameworkEntry] = []
    parser_data: dict[str, dict[str, Any]] = {}
    for call in calls:
        receiver, _, action = call["target_expression"].rpartition(".")
        obj = objects.get((module, receiver))
        if not obj and action == "add_parser":
            fallback = next(((name, item) for (scope, name), item in objects.items()
                             if scope == module and item.framework in {"argparse", "optparse"}), None)
            if fallback:
                receiver, obj = fallback
        if not obj or obj.framework not in {"argparse", "optparse"}:
            continue
        data = parser_data.setdefault(receiver, {"arguments": [], "subcommands": [], "handler": None, "call": call})
        if action in {"add_argument", "add_option"}:
            data["arguments"].append(_argument_inputs(_value(call.get("arguments_json"), []),
                                                      _value(call.get("keywords_json"), {})))
        elif action == "add_parser":
            literals = _literal_arguments(_value(call.get("arguments_json"), []))
            if literals and isinstance(literals[0], str):
                data["subcommands"].append(literals[0])
        elif action == "set_defaults":
            handler = _value(call.get("keywords_json"), {}).get("handler")
            if handler:
                data["handler"] = handler
        elif action in {"parse_args", "parse_known_args"}:
            data["parse"] = call
    for receiver, data in parser_data.items():
        obj = objects[(module, receiver)]
        parse_call = data.get("parse") or data["call"]
        handler = data.get("handler")
        symbol = symbols_by_name.get((handler or "").split(".")[-1]) if handler else None
        if not symbol and parse_call.get("caller_symbol_id"):
            symbol = next((item for item in symbols if item.get("symbol_id") == parse_call["caller_symbol_id"]), None)
        entries.append(_entry(
            "cli", f"{obj.framework}: {module}", symbol, obj.framework, _versions(versions, obj.framework),
            obj.variant, {"parser": receiver, "arguments": data["arguments"], "subcommands": data["subcommands"],
                          "handler_expression": handler},
            "high" if data["arguments"] or data["subcommands"] else "possible", "call",
            parse_call["line_number"], parse_call.get("relative_path"), parse_call["target_expression"],
            ("Argument parsing is recorded from parser calls; runtime defaults and validators are not executed.",),
            "full" if data["arguments"] else "partial",
        ))

    groups = {key[1] for key, value in objects.items() if key[0] == module and value.framework in {"click", "typer"}
              and value.variant in {"group", "application"}}
    for symbol in symbols:
        if any((_decorator_call(item) or ("",))[0].endswith((".group", "click.group"))
               for item in symbol.get("decorators", [])):
            groups.add(symbol["name"])
    for symbol in symbols:
        decorators = symbol.get("decorators", [])
        command = None
        command_group = False
        options: list[dict[str, Any]] = []
        arguments: list[dict[str, Any]] = []
        framework = None
        for decorator in decorators:
            parsed = _decorator_call(decorator)
            if not parsed:
                continue
            call, args, keywords = parsed
            receiver, _, action = call.rpartition(".")
            bound = _resolve(call, aliases)
            receiver_obj = objects.get((module, receiver)) if receiver else None
            known = bound.startswith(("click.", "typer.")) or (
                receiver_obj is not None and receiver_obj.framework in {"click", "typer"}
            ) or (receiver in groups and receiver)
            if not known:
                continue
            if action in {"command", "group"} and framework is None:
                command = decorator
                command_group = action == "group"
                framework = "typer" if bound.startswith("typer.") or (receiver_obj and receiver_obj.framework == "typer") else "click"
            elif action in {"option", "argument", "opt"}:
                item = _argument_inputs(args, keywords)
                (options if action in {"option", "opt"} else arguments).append(item)
        if command and framework:
            entries.append(_entry(
                "cli", f"{framework}: {symbol['qualified_name']}", symbol, framework,
                _versions(versions, framework), "command",
                {"registration": command, "options": options, "arguments": arguments,
                 "signature": symbol.get("signature")},
                "high", "decorator", symbol["line_start"], symbol.get("relative_path"), command,
                ("Click/Typer context and runtime parameter conversion are not evaluated.",),
            ))
        if command and command_group:
            groups.add(symbol["name"])

    for call in calls:
        action = call["target_expression"].rsplit(".", 1)[-1]
        if action not in {"add_command", "register_command", "register"}:
            continue
        if not call["target_expression"].startswith(("cli.", "app.", "command.", "registry.")):
            continue
        arguments = _value(call.get("arguments_json"), [])
        if not arguments:
            continue
        handler = arguments[0]
        symbol = symbols_by_name.get(handler.split(".")[-1])
        entries.append(_entry(
            "cli", f"registry: {handler}", symbol, "command-registry", None, "registry",
            {"registration": call["target_expression"], "handler_expression": handler},
            "high" if symbol else "possible", "call", call["line_number"], call.get("relative_path"),
            call["target_expression"], ("Registry dispatch and dynamic command loading are not resolved.",),
        ))
    return entries


def _worker_entries(module: str, symbols: list[dict[str, Any]], calls: list[dict[str, Any]],
                    aliases: dict[str, str], objects: dict[tuple[str, str], _Object],
                    versions: dict[str, str]) -> list[FrameworkEntry]:
    entries: list[FrameworkEntry] = []
    for call in calls:
        receiver, _, action = call["target_expression"].rpartition(".")
        obj = objects.get((module, receiver))
        if not obj or obj.framework != "apscheduler" or action != "add_job":
            continue
        arguments = _value(call.get("arguments_json"), [])
        handler = arguments[0] if arguments else None
        symbol = next((item for item in symbols if item["name"] == (handler or "").split(".")[-1]), None)
        entries.append(_entry(
            "scheduled", f"apscheduler job: {handler or call['target_expression']}", symbol, "apscheduler",
            _versions(versions, "apscheduler"), "scheduler",
            {"handler_expression": handler, "trigger": arguments[1] if len(arguments) > 1 else None,
             "registration": call["target_expression"]}, "high" if handler else "possible", "call",
            call["line_number"], call.get("relative_path"), call["target_expression"],
            ("Runtime scheduler configuration is not evaluated.",), handler=handler,
        ))
    for symbol in symbols:
        for decorator in symbol.get("decorators", []):
            parsed = _decorator_call(decorator)
            if not parsed:
                continue
            call = parsed[0]
            full = _resolve(call, aliases)
            matched = next((value for signature, value in _WORKER_DECORATORS.items()
                            if full == signature or full.endswith("." + signature)), None)
            receiver, _, action = call.rpartition(".")
            obj = objects.get((module, receiver)) if receiver else None
            if obj and obj.framework == "celery" and action in {"task", "periodic_task"}:
                matched = ("celery", "task" if action == "task" else "scheduled")
            if not matched:
                continue
            framework, variant = matched
            entries.append(_entry(
                "worker" if variant == "task" else "scheduled", f"{framework} {variant}: {symbol['name']}",
                symbol, framework, _versions(versions, framework), variant,
                {"registration": decorator}, "high", "decorator", symbol["line_start"],
                symbol.get("relative_path"), decorator,
                ("Broker configuration, retries, and runtime scheduling are not evaluated.",),
            ))
        if symbol.get("kind") == "class" and any(
            _resolve(base, aliases).startswith("django.middleware") for base in
            re.findall(r"[A-Za-z_][\w.]*", symbol.get("signature") or "")
        ):
            entries.append(_entry(
                "middleware", f"django middleware: {symbol['qualified_name']}", symbol, "django",
                _versions(versions, "django"), "middleware", {"registration": symbol.get("signature")},
                "high", "definition", symbol["line_start"], symbol.get("relative_path"), symbol.get("signature") or "",
                ("Middleware ordering and short-circuit behaviour are not evaluated.",),
            ))
        for decorator in symbol.get("decorators", []):
            parsed = _decorator_call(decorator)
            if not parsed:
                continue
            full = _resolve(parsed[0], aliases)
            receiver, _, action = parsed[0].rpartition(".")
            obj = objects.get((module, receiver)) if receiver else None
            if obj and obj.framework == "apscheduler" and action == "scheduled_job":
                entries.append(_entry(
                    "scheduled", f"apscheduler job: {symbol['name']}", symbol, "apscheduler",
                    _versions(versions, "apscheduler"), "scheduler", {"registration": decorator}, "high",
                    "decorator", symbol["line_start"], symbol.get("relative_path"), decorator,
                    ("Runtime scheduler configuration is not evaluated.",),
                ))
            if full in {"pluggy.hookimpl", "pluggy.hookspec"}:
                entries.append(_entry(
                    "plugin", f"pluggy {full.rsplit('.', 1)[-1]}: {symbol['qualified_name']}", symbol,
                    "pluggy", _versions(versions, "pluggy"), full.rsplit(".", 1)[-1],
                    {"registration": decorator}, "high", "decorator", symbol["line_start"],
                    symbol.get("relative_path"), decorator,
                    ("Plugin discovery and hook dispatch are not resolved.",),
                ))
    return entries


def analyse(symbols: list[dict[str, Any]], imports: list[dict[str, Any]], calls: list[dict[str, Any]],
            versions: dict[str, str] | None = None) -> list[FrameworkEntry]:
    """Return only entries anchored in a recognised framework registration."""
    versions = versions or {}
    alias_map = _bindings(imports)
    objects = _objects(calls, alias_map)
    symbols_by_name = {item["name"]: item for item in symbols}
    by_module: dict[str, list[dict[str, Any]]] = {}
    calls_by_module: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        by_module.setdefault(symbol["module_name"], []).append(symbol)
    for call in calls:
        calls_by_module.setdefault(call["module_name"], []).append(call)
    entries: list[FrameworkEntry] = []
    modules = set(by_module) | set(calls_by_module) | {item["module_name"] for item in imports}
    for module in modules:
        module_symbols = by_module.get(module, [])
        aliases = alias_map.get(module, {})
        module_calls = calls_by_module.get(module, [])
        entries.extend(_route_entries(module, module_symbols, aliases, objects, versions))
        entries.extend(_django_entries(module, module_calls, aliases, symbols_by_name, versions))
        entries.extend(_cli_entries(module, module_symbols, module_calls, aliases, objects, symbols_by_name, versions))
        entries.extend(_worker_entries(module, module_symbols, module_calls, aliases, objects, versions))
    return entries


def data_access(expression: str, imports: list[dict[str, Any]], aliases: dict[str, str],
                versions: dict[str, str]) -> tuple[str, str, str, str] | None:
    """Classify a DB-API, SQLAlchemy, or Django ORM call without importing it."""
    full = _resolve(expression, aliases)
    action = full.rsplit(".", 1)[-1]
    operation = _DB_CALLS.get(action)
    if not operation:
        return None
    imported = " ".join(aliases.values())
    if ".objects." in full or full.startswith(("django.db.", "django.db.models.")):
        return "database", operation, "django-orm", full
    if full.startswith(("sqlalchemy.", "sqlalchemy.orm.")):
        return "database", operation, "sqlalchemy", full
    if action in {"query", "filter", "filter_by", "add", "add_all", "merge", "flush", "scalar", "all", "first"} \
            and "sqlalchemy" in imported:
        return "database", operation, "sqlalchemy", full
    if full.startswith(("sqlite3.", "psycopg.", "psycopg2.", "pymysql.", "asyncpg.")) or ".cursor" in full:
        return "database", operation, "db-api", full
    if action in _DB_CALLS and any(driver in imported for driver in ("sqlite3", "psycopg", "pymysql", "asyncpg")):
        return "database", operation, "db-api", full
    return None


def import_aliases(imports: list[dict[str, Any]], module: str) -> dict[str, str]:
    return _bindings(imports).get(module, {})
