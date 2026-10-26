"""Evidence-grounded reconstruction built from a completed static scan.

The parser deliberately remains a small, safe AST collector.  This module is
the second pass that turns those facts into user-facing relationships without
ever importing target code.  Every inferred record retains its source location
and a deliberately small confidence vocabulary.
"""

from __future__ import annotations

import json
import re
import tomllib
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .store import Store

CONFIRMED = "confirmed"
HIGH = "high"
POSSIBLE = "possible"
UNKNOWN = "unknown"


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


class Reconstruction:
    """Create derived reverse-engineering facts for one immutable scan."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def build(self, scan_id: str, root: Path) -> None:
        self._relationships(scan_id)
        self._entry_points(scan_id, root)
        self._resources(scan_id)
        self._dynamic_boundaries(scan_id)
        self._data_edges(scan_id)
        self._components(scan_id)
        self._overview_report(scan_id)

    def _relationships(self, scan_id: str) -> None:
        imports = self.store.rows(
            """SELECT i.*, f.module_name, f.relative_path FROM imports i
               JOIN files f ON f.file_id=i.source_file_id
               WHERE i.scan_id=? AND i.resolved_module IS NOT NULL""",
            (scan_id,),
        )
        for item in imports:
            self._relationship(
                scan_id, None, None, item["module_name"], item["resolved_module"], "import", CONFIRMED,
                self._evidence("import", item["relative_path"], item["line_number"], target=item["target_module"]),
            )
        calls = self.store.rows(
            """SELECT c.*, f.module_name, f.relative_path FROM calls c
               JOIN files f ON f.file_id=c.call_file_id WHERE c.scan_id=?""",
            (scan_id,),
        )
        for call in calls:
            if call["resolution"] == "resolved" and call["callee_symbol_id"]:
                target = self.store.one("SELECT file_id FROM symbols WHERE symbol_id=?", (call["callee_symbol_id"],))
                target_module = None
                if target:
                    target_file = self.store.one("SELECT module_name FROM files WHERE file_id=?", (target["file_id"],))
                    target_module = target_file["module_name"] if target_file else None
                self._relationship(
                    scan_id, call["caller_symbol_id"], call["callee_symbol_id"], call["module_name"], target_module,
                    "direct_call", CONFIRMED,
                    self._evidence("call", call["relative_path"], call["line_number"], expression=call["target_expression"]),
                )
            elif call["resolution"] == "ambiguous":
                for candidate in _json(call["candidate_json"], []):
                    self._relationship(
                        scan_id, call["caller_symbol_id"], candidate, call["module_name"], None,
                        "candidate_call", POSSIBLE,
                        self._evidence("call", call["relative_path"], call["line_number"], expression=call["target_expression"]),
                    )

    def _entry_points(self, scan_id: str, root: Path) -> None:
        files = self.store.rows("SELECT * FROM files WHERE scan_id=? AND parse_status='parsed'", (scan_id,))
        symbols = self.store.rows("SELECT * FROM symbols WHERE scan_id=?", (scan_id,))
        by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for symbol in symbols:
            file = self.store.one("SELECT module_name,relative_path FROM files WHERE file_id=?", (symbol["file_id"],))
            if file:
                symbol["module_name"] = file["module_name"]
                symbol["relative_path"] = file["relative_path"]
                symbol["decorators"] = _json(symbol.get("decorators_json"), [])
                by_module[file["module_name"]].append(symbol)

        # Main guards and python -m modules.  Prefer a conventional main
        # function, then a resolved call inside the guard, then the file itself.
        guard_calls = self.store.rows(
            """SELECT c.*, f.module_name, f.relative_path FROM calls c JOIN files f ON f.file_id=c.call_file_id
               WHERE c.scan_id=? AND c.in_main_guard=1 ORDER BY c.line_number""",
            (scan_id,),
        )
        for file in files:
            module = file["module_name"]
            is_test_file = file["relative_path"].startswith("tests/") or "/tests/" in f"/{file['relative_path']}"
            if file["is_entry_point"] and not is_test_file:
                candidate = next((s for s in by_module[module] if s["name"] == "main"), None)
                if not candidate:
                    call = next((c for c in guard_calls if c["module_name"] == module and c["callee_symbol_id"]), None)
                    candidate = self.store.one("SELECT * FROM symbols WHERE symbol_id=?", (call["callee_symbol_id"],)) if call else None
                self._entry(
                    scan_id, "script", f"python {file['relative_path']}", candidate["symbol_id"] if candidate else None,
                    module, {}, CONFIRMED,
                    self._evidence("main_guard", file["relative_path"], 1),
                )
            if module.endswith(".__main__") or file["relative_path"].endswith("/__main__.py"):
                candidate = next((s for s in by_module[module] if s["name"] == "main"), None)
                self._entry(scan_id, "module", f"python -m {module.removesuffix('.__main__')}", candidate["symbol_id"] if candidate else None,
                            module, {}, CONFIRMED, self._evidence("module", file["relative_path"], 1))

        for name, target, source in self._console_scripts(root):
            module, _, attribute = target.partition(":")
            candidate = next((s for s in by_module.get(module, []) if s["name"] == attribute), None)
            self._entry(scan_id, "console", name, candidate["symbol_id"] if candidate else None, module,
                        {"command": name}, HIGH if candidate else POSSIBLE,
                        self._evidence("configuration", source, 1, target=target))

        for symbol in symbols:
            decorators = symbol.get("decorators", [])
            joined = " ".join(decorators).lower()
            path = symbol["relative_path"]
            if re.search(r"\.(get|post|put|patch|delete|route)\(", joined):
                decorator = next(value for value in decorators if re.search(r"\.(get|post|put|patch|delete|route)\(", value.lower()))
                method = (re.search(r"\.(get|post|put|patch|delete|route)\(", decorator.lower()) or [None, "route"])[1].upper()
                route = re.search(r"\(\s*['\"]([^'\"]+)", decorator)
                self._entry(scan_id, "route", f"{method} {route.group(1) if route else symbol['name']}", symbol["symbol_id"],
                            symbol["module_name"], {"method": method, "route": route.group(1) if route else None}, HIGH,
                            self._evidence("decorator", path, symbol["line_start"], decorator=decorator))
            if any(re.search(r"(^|\.)((command)|(group))\b", value.lower()) for value in decorators):
                self._entry(scan_id, "cli", symbol["name"], symbol["symbol_id"], symbol["module_name"],
                            {"command": symbol["name"]}, HIGH,
                            self._evidence("decorator", path, symbol["line_start"], decorators=decorators))
            if any("task" in value.lower() for value in decorators):
                self._entry(scan_id, "worker", symbol["name"], symbol["symbol_id"], symbol["module_name"], {}, HIGH,
                            self._evidence("decorator", path, symbol["line_start"], decorators=decorators))
            is_test_function = symbol["kind"] in {"function", "async_function", "method", "async_method"}
            if is_test_function and (symbol["name"].startswith("test_") or "/tests/" in f"/{path}" or path.startswith("tests/")):
                self._entry(scan_id, "test", symbol["qualified_name"], symbol["symbol_id"], symbol["module_name"], {}, CONFIRMED,
                            self._evidence("definition", path, symbol["line_start"], name=symbol["name"]))

        argparse_calls = self.store.rows(
            """SELECT c.*,f.module_name,f.relative_path FROM calls c JOIN files f ON f.file_id=c.call_file_id
               WHERE c.scan_id=? AND c.target_expression LIKE '%parse_args%'""",
            (scan_id,),
        )
        for call in argparse_calls:
            self._entry(scan_id, "argparse", f"argparse: {call['module_name']}", call["caller_symbol_id"], call["module_name"], {}, HIGH,
                        self._evidence("call", call["relative_path"], call["line_number"], expression=call["target_expression"]))

        django_calls = self.store.rows(
            """SELECT c.*,f.module_name,f.relative_path FROM calls c JOIN files f ON f.file_id=c.call_file_id
               WHERE c.scan_id=? AND c.target_expression IN ('path','re_path','django.urls.path','django.urls.re_path')""",
            (scan_id,),
        )
        for call in django_calls:
            arguments = _json(call.get("arguments_json"), [])
            route = arguments[0].strip("'\"") if arguments else None
            handler_name = arguments[1] if len(arguments) > 1 else None
            handler = next((s for s in by_module.get(call["module_name"], []) if s["name"] == handler_name), None)
            self._entry(scan_id, "route", f"DJANGO {route or call['module_name']}", handler["symbol_id"] if handler else None,
                        call["module_name"], {"route": route, "framework": "django"}, HIGH if handler else POSSIBLE,
                        self._evidence("call", call["relative_path"], call["line_number"], expression=call["target_expression"]))

    @staticmethod
    def _console_scripts(root: Path) -> list[tuple[str, str, str]]:
        pyproject = root / "pyproject.toml"
        if not pyproject.exists():
            return []
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return []
        scripts = dict(data.get("project", {}).get("scripts", {}))
        scripts.update(data.get("tool", {}).get("poetry", {}).get("scripts", {}))
        return [(str(name), str(target), "pyproject.toml") for name, target in scripts.items() if isinstance(target, str)]

    def _resources(self, scan_id: str) -> None:
        calls = self.store.rows(
            """SELECT c.*, f.module_name, f.relative_path FROM calls c
               JOIN files f ON f.file_id=c.call_file_id WHERE c.scan_id=?""",
            (scan_id,),
        )
        for call in calls:
            classified = self._resource_kind(call["target_expression"])
            if not classified:
                continue
            kind, operation = classified
            self.store.execute(
                "INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?)",
                (_id(), scan_id, call["caller_symbol_id"], call["module_name"], kind, call["target_expression"], operation,
                 CONFIRMED, self.store.json(self._evidence("call", call["relative_path"], call["line_number"], expression=call["target_expression"]))),
            )

    def _dynamic_boundaries(self, scan_id: str) -> None:
        calls = self.store.rows(
            """SELECT c.target_expression,c.line_number,f.relative_path FROM calls c
               JOIN files f ON f.file_id=c.call_file_id WHERE c.scan_id=?""",
            (scan_id,),
        )
        dynamic = {"getattr", "setattr", "delattr", "eval", "exec", "__import__"}
        for call in calls:
            expression = call["target_expression"]
            if expression in dynamic or expression.startswith("importlib."):
                evidence = self._evidence("call", call["relative_path"], call["line_number"], expression=expression)
                self.store.execute("INSERT INTO findings VALUES(?,?,?,?,?,?,?,?)", (
                    _id(), scan_id, "TB-DYNAMIC-001", "info",
                    f"Dynamic boundary '{expression}' prevents complete static resolution.",
                    call["relative_path"], call["line_number"], self.store.json(evidence),
                ))

    @staticmethod
    def _resource_kind(expression: str) -> tuple[str, str] | None:
        value = expression.lower()
        if value in {"open", "path.open", "path.read_text", "path.write_text", "path.read_bytes", "path.write_bytes"} or ".read_text" in value or ".write_text" in value:
            return "file", "write" if "write" in value else "read"
        if value.startswith(("requests.", "httpx.", "urllib.")) or ".request" in value:
            return "http", value.rsplit(".", 1)[-1]
        if value.startswith(("subprocess.", "os.system")):
            return "subprocess", value.rsplit(".", 1)[-1]
        if value.startswith(("os.getenv", "os.environ")):
            return "env", "read"
        if any(token in value for token in ("sqlite", "sqlalchemy", "psycopg", ".execute", ".commit", ".connect")):
            return "database", value.rsplit(".", 1)[-1]
        if any(token in value for token in ("celery", ".delay", ".apply_async", ".publish", ".enqueue")):
            return "queue", value.rsplit(".", 1)[-1]
        return None

    def _data_edges(self, scan_id: str) -> None:
        calls = self.store.rows("SELECT * FROM calls WHERE scan_id=? AND resolution='resolved'", (scan_id,))
        for call in calls:
            if not call["caller_symbol_id"] or not call["callee_symbol_id"]:
                continue
            callee = self.store.one("SELECT parameters_json FROM symbols WHERE symbol_id=?", (call["callee_symbol_id"],))
            if not callee:
                continue
            parameters = _json(callee["parameters_json"], [])
            arguments = _json(call.get("arguments_json"), [])
            keywords = _json(call.get("keywords_json"), {})
            for index, argument in enumerate(arguments):
                if index < len(parameters):
                    self._data_edge(scan_id, call["caller_symbol_id"], call["callee_symbol_id"], argument, parameters[index],
                                    "argument", CONFIRMED, {"line": call["line_number"], "kind": "call"})
            for name, expression in keywords.items():
                if name in parameters:
                    self._data_edge(scan_id, call["caller_symbol_id"], call["callee_symbol_id"], expression, name,
                                    "keyword_argument", CONFIRMED, {"line": call["line_number"], "kind": "call"})
            if call["assignment_target"]:
                self._data_edge(scan_id, call["callee_symbol_id"], call["caller_symbol_id"], "return", call["assignment_target"],
                                "return_assignment", HIGH, {"line": call["line_number"], "kind": "assignment"})

    def _components(self, scan_id: str) -> None:
        files = self.store.rows("SELECT * FROM files WHERE scan_id=? AND parse_status='parsed'", (scan_id,))
        entry_modules = {row["module_name"] for row in self.store.rows("SELECT module_name FROM entry_points WHERE scan_id=?", (scan_id,))}
        resources = self.store.rows("SELECT module_name,kind FROM resources WHERE scan_id=?", (scan_id,))
        resource_kinds: dict[str, set[str]] = defaultdict(set)
        for row in resources:
            resource_kinds[row["module_name"]].add(row["kind"])
        by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for file in files:
            by_module[file["module_name"]].append(file)
        confidence_rank = {CONFIRMED: 0, HIGH: 1, POSSIBLE: 2, UNKNOWN: 3}
        for module, module_files in sorted(by_module.items()):
            classifications = [
                self._role(module, file["relative_path"], module in entry_modules, resource_kinds[module])
                for file in module_files
            ]
            role, confidence = min(classifications, key=lambda item: confidence_rank[item[1]])
            paths = sorted(file["relative_path"] for file in module_files)
            summary = f"{role} component containing module {module}."
            if len(paths) > 1:
                summary = f"{role} component containing logical module {module} across {len(paths)} source files."
            component_id = _id()
            evidence = self._evidence("module", paths[0], 1, module=module, role=role, paths=paths)
            self.store.execute("INSERT INTO components VALUES(?,?,?,?,?,?,?)", (
                component_id, scan_id, module, role, confidence, summary, self.store.json(evidence),
            ))
            self.store.execute("INSERT INTO component_members VALUES(?,?,?,?)", (component_id, "module", module, role))

    @staticmethod
    def _role(module: str, path: str, entry: bool, resources: set[str]) -> tuple[str, str]:
        lower = f"{module} {path}".lower()
        if path.startswith("tests/") or ".test" in module or "test_" in lower:
            return "test", CONFIRMED
        if entry or any(token in lower for token in ("cli", "command", "manage")):
            return "CLI", HIGH
        if any(token in lower for token in ("route", "view", "api", "web", "wsgi", "asgi")):
            return "HTTP API", HIGH
        if any(token in lower for token in ("store", "repository", "database", "persistence", "model")) or "database" in resources:
            return "persistence", HIGH
        if "tui" in lower or "ui" in lower:
            return "presentation", HIGH
        if any(token in lower for token in ("parser", "semantic", "engine", "analysis")):
            return "analysis", HIGH
        if resources:
            return "integration", HIGH
        return "application", POSSIBLE

    def _overview_report(self, scan_id: str) -> None:
        components = self.store.rows("SELECT name,role,confidence,summary FROM components WHERE scan_id=? ORDER BY name", (scan_id,))
        entry_points = self.store.rows("SELECT kind,name,module_name,confidence FROM entry_points WHERE scan_id=? ORDER BY kind,name", (scan_id,))
        resources = self.store.rows("SELECT kind,COUNT(*) AS count FROM resources WHERE scan_id=? GROUP BY kind ORDER BY kind", (scan_id,))
        findings = self.store.rows("SELECT severity,COUNT(*) AS count FROM findings WHERE scan_id=? GROUP BY severity", (scan_id,))
        roles = Counter(item["role"] for item in components)
        purpose = "Static reconstruction is inconclusive; inspect the evidence-backed components."
        if roles:
            purpose = "Python project with " + ", ".join(f"{count} {role}" for role, count in sorted(roles.items())) + " component(s)."
        content = {"purpose": purpose, "components": components, "entry_points": entry_points,
                   "resources": resources, "findings": findings, "confidence_policy": [CONFIRMED, HIGH, POSSIBLE, UNKNOWN]}
        self.store.execute("INSERT OR REPLACE INTO reports VALUES(?,?,?,?,?)", (_id(), scan_id, "overview", self.store.json(content), _now()))

    def _entry(self, scan_id: str, kind: str, name: str, symbol_id: str | None, module: str | None,
               inputs: dict[str, Any], confidence: str, evidence: dict[str, Any]) -> None:
        self.store.execute("INSERT OR IGNORE INTO entry_points VALUES(?,?,?,?,?,?,?,?,?)", (
            _id(), scan_id, kind, name, symbol_id, module, self.store.json(inputs), confidence, self.store.json(evidence),
        ))

    def _relationship(self, scan_id: str, source: str | None, target: str | None, source_module: str | None,
                      target_module: str | None, kind: str, confidence: str, evidence: dict[str, Any]) -> None:
        self.store.execute("INSERT INTO relationships VALUES(?,?,?,?,?,?,?,?,?)", (
            _id(), scan_id, source, target, source_module, target_module, kind, confidence, self.store.json(evidence),
        ))

    def _data_edge(self, scan_id: str, source: str, target: str, source_name: str, target_name: str,
                   kind: str, confidence: str, evidence: dict[str, Any]) -> None:
        self.store.execute("INSERT INTO data_edges VALUES(?,?,?,?,?,?,?,?,?)", (
            _id(), scan_id, source, target, source_name, target_name, kind, confidence, self.store.json(evidence),
        ))

    @staticmethod
    def _evidence(kind: str, path: str | None, line: int | None, **extra: Any) -> dict[str, Any]:
        return {"kind": kind, "path": path, "line_start": line, "line_end": line, **extra}
