"""Project lifecycle, scan orchestration, static resolution, and diagnostics."""

from __future__ import annotations

import json
import hashlib
import re
import sys
import tomllib
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .models import AttributeRecord, CallRecord, FlowRecord, ImportRecord, ParsedFile, SymbolRecord
from .parser import discover_python_files, parse_file
from .reconstruction import Reconstruction
from .store import Store


def now() -> str:
    return datetime.now(UTC).isoformat()


def identifier() -> str:
    return str(uuid.uuid4())


def normalise_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


class ThreadError(RuntimeError):
    pass


class Engine:
    def __init__(self, store: Store) -> None:
        self.store = store

    def add_project(self, path_text: str, name: str | None = None) -> dict[str, Any]:
        path = Path(path_text).expanduser()
        try:
            root = path.resolve(strict=True)
        except OSError as exc:
            raise ThreadError(f"Cannot access project path: {exc}") from exc
        if not root.is_dir():
            raise ThreadError("Project path must be a directory.")
        files, _ = discover_python_files(root)
        if not files:
            raise ThreadError("Project contains no eligible Python source files.")
        project_name = name or root.name
        existing = self.store.one("SELECT * FROM projects WHERE root_path = ?", (str(root),))
        if existing:
            return existing
        try:
            with self.store.transaction():
                project_id = identifier()
                self.store.execute(
                    "INSERT INTO projects(project_id,name,root_path,created_at) VALUES(?,?,?,?)",
                    (project_id, project_name, str(root), now()),
                )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise ThreadError(f"A project named '{project_name}' is already registered.") from exc
            raise
        return self.get_project(project_id)

    def get_project(self, reference: str) -> dict[str, Any]:
        row = self.store.one("SELECT * FROM projects WHERE project_id = ? OR name = ? COLLATE NOCASE", (reference, reference))
        if not row:
            raise ThreadError(f"No registered project matches '{reference}'.")
        return row

    def list_projects(self) -> list[dict[str, Any]]:
        return self.store.rows(
            """SELECT p.*, s.scan_id, s.completed_at, s.status, s.summary_json
               FROM projects p LEFT JOIN scans s ON s.scan_id = (
                  SELECT scan_id FROM scans WHERE project_id=p.project_id AND status='completed'
                  ORDER BY completed_at DESC LIMIT 1) ORDER BY p.name"""
        )

    def remove_project(self, reference: str) -> dict[str, Any]:
        project = self.get_project(reference)
        with self.store.transaction():
            self.store.execute("DELETE FROM projects WHERE project_id = ?", (project["project_id"],))
        return project

    def latest_scan(self, project_ref: str) -> dict[str, Any]:
        project = self.get_project(project_ref)
        scan = self.store.one(
            "SELECT * FROM scans WHERE project_id=? AND status='completed' ORDER BY completed_at DESC LIMIT 1",
            (project["project_id"],),
        )
        if not scan:
            raise ThreadError(f"Project '{project['name']}' has no completed scan. Run: threadtrace scan {project['name']}")
        scan["summary"] = json.loads(scan["summary_json"])
        return scan

    def scan_history(self, project_ref: str) -> list[dict[str, Any]]:
        project = self.get_project(project_ref)
        rows = self.store.rows("SELECT * FROM scans WHERE project_id=? ORDER BY started_at DESC", (project["project_id"],))
        for row in rows:
            row["summary"] = json.loads(row["summary_json"])
        return rows

    def trace(self, project_ref: str, command: list[str], timeout: int = 30, output_cap: int = 100_000) -> dict[str, Any]:
        """Explicitly execute a user-provided Python command with a bounded profiler.

        Unlike :meth:`scan`, this can execute untrusted code and is only exposed
        by a CLI command that requires a separate confirmation flag.
        """
        if timeout < 1 or output_cap < 1:
            raise ThreadError("Trace timeout and output cap must be positive.")
        project = self.get_project(project_ref)
        from .runtime.launcher import run_python_trace

        try:
            result = run_python_trace(Path(project["root_path"]), command, timeout, output_cap)
        except (OSError, ValueError) as exc:
            raise ThreadError(f"Trace could not start: {exc}") from exc
        return self.record_runtime(project_ref, result)

    def record_runtime(self, project_ref: str, result: dict[str, Any]) -> dict[str, Any]:
        """Persist a completed explicit trace or interactive runtime session."""
        project = self.get_project(project_ref)
        scan = self.store.one(
            "SELECT scan_id FROM scans WHERE project_id=? AND status='completed' ORDER BY completed_at DESC LIMIT 1",
            (project["project_id"],),
        )
        trace_id = identifier()
        with self.store.transaction():
            self.store.execute("INSERT INTO runtime_traces VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                trace_id, project["project_id"], scan["scan_id"] if scan else None, self.store.json(result["command"]),
                now(), now(), result["exit_code"], int(result["timed_out"]), result["stdout"], result["stderr"], len(result["events"]),
            ))
            for ordinal, event in enumerate(result["events"], 1):
                self.store.execute("INSERT INTO runtime_events VALUES(?,?,?,?,?,?,?,?)", (
                    identifier(), trace_id, ordinal, event["kind"], event.get("name"), event.get("relative_path"), event.get("line"),
                    self.store.json({"kind": "runtime", "path": event.get("relative_path"), "line_start": event.get("line"), "details": event}),
                ))
        return {"trace_id": trace_id, "scan_id": scan["scan_id"] if scan else None, **result}

    def scan(self, project_ref: str) -> dict[str, Any]:
        project = self.get_project(project_ref)
        root = Path(project["root_path"])
        scan_id = identifier()
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO scans(scan_id,project_id,started_at,status,tool_version) VALUES(?,?,?,?,?)",
                (scan_id, project["project_id"], now(), "running", __version__),
            )
        try:
            paths, skipped = discover_python_files(root)
            previous = self.store.one(
                """SELECT scan_id FROM scans WHERE project_id=? AND status='completed' AND tool_version=?
                   ORDER BY completed_at DESC LIMIT 1""",
                (project["project_id"], __version__),
            )
            prior_files: dict[str, dict[str, Any]] = {}
            if previous:
                prior_files = {row["relative_path"]: row for row in self.store.rows(
                    "SELECT * FROM files WHERE scan_id=? AND parse_status='parsed'", (previous["scan_id"],)
                )}
            parsed: list[ParsedFile] = []
            reused_files = 0
            for path in paths:
                relative = path.relative_to(root).as_posix()
                previous_file = prior_files.get(relative)
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    digest = None
                if previous_file and digest and digest == previous_file["content_hash"]:
                    parsed.append(self._reuse_parsed_file(previous["scan_id"], previous_file))
                    reused_files += 1
                else:
                    parsed.append(parse_file(root, path))
            summary = self._persist_scan(scan_id, root, parsed, skipped)
            summary["reused_files"] = reused_files
            with self.store.transaction():
                self.store.execute(
                    "UPDATE scans SET completed_at=?,status='completed',summary_json=? WHERE scan_id=?",
                    (now(), self.store.json(summary), scan_id),
                )
            return self.store.one("SELECT * FROM scans WHERE scan_id=?", (scan_id,)) or {}
        except Exception as exc:
            with self.store.transaction():
                self.store.execute(
                    "UPDATE scans SET completed_at=?,status='failed',error_message=? WHERE scan_id=?",
                    (now(), str(exc), scan_id),
                )
            raise ThreadError(f"Scan failed: {exc}") from exc

    def _reuse_parsed_file(self, prior_scan_id: str, file: dict[str, Any]) -> ParsedFile:
        """Hydrate unchanged parse facts from an immutable earlier snapshot."""
        file_id = file["file_id"]
        symbols = [SymbolRecord(
            row["qualified_name"], row["name"], row["kind"], row["parent_qualified_name"], row["signature"],
            row["docstring"], row["purpose"], json.loads(row["decorators_json"]), row["line_start"], row["line_end"],
            row["column_start"], row["complexity"], json.loads(row["parameters_json"]),
        ) for row in self.store.rows("SELECT * FROM symbols WHERE scan_id=? AND file_id=?", (prior_scan_id, file_id))]
        attributes = [AttributeRecord(row["class_name"], row["name"], row["kind"], row["line_number"], row["value_expression"])
                      for row in self.store.rows("""SELECT a.*,s.qualified_name AS class_name FROM attributes a
                         JOIN symbols s ON s.symbol_id=a.class_symbol_id WHERE a.scan_id=? AND s.file_id=?""", (prior_scan_id, file_id))]
        imports = [ImportRecord(row["target_module"], row["imported_name"], row["alias"], row["level"], row["line_number"], row["column_number"])
                   for row in self.store.rows("SELECT * FROM imports WHERE scan_id=? AND source_file_id=?", (prior_scan_id, file_id))]
        calls = [CallRecord(row["caller_name"], row["target_expression"], row["line_number"], row["column_number"],
                            json.loads(row["arguments_json"]), json.loads(row["keywords_json"]), row["assignment_target"], bool(row["in_main_guard"]))
                 for row in self.store.rows("""SELECT c.*,s.qualified_name AS caller_name FROM calls c
                    LEFT JOIN symbols s ON s.symbol_id=c.caller_symbol_id WHERE c.scan_id=? AND c.call_file_id=?""", (prior_scan_id, file_id))]
        flows = [FlowRecord(row["symbol_name"], row["variable_name"], row["event_kind"], row["expression"], row["line_number"])
                 for row in self.store.rows("""SELECT fl.*,s.qualified_name AS symbol_name FROM flows fl
                    JOIN symbols s ON s.symbol_id=fl.symbol_id WHERE fl.scan_id=? AND s.file_id=?""", (prior_scan_id, file_id))]
        return ParsedFile(file["relative_path"], file["module_name"], file["content_hash"], "parsed", is_entry_point=bool(file["is_entry_point"]),
                          symbols=symbols, attributes=attributes, imports=imports, calls=calls, flows=flows)

    def _persist_scan(self, scan_id: str, root: Path, parsed: list[ParsedFile], skipped: list[str]) -> dict[str, Any]:
        file_ids: dict[str, str] = {}
        symbol_ids: dict[str, list[str]] = defaultdict(list)
        symbol_rows: dict[str, dict[str, Any]] = {}
        import_rows: list[dict[str, Any]] = []
        with self.store.transaction():
            for item in parsed:
                file_id = identifier()
                file_ids[item.relative_path] = file_id
                self.store.execute(
                    """INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)""",
                    (file_id, scan_id, item.relative_path, item.module_name, item.content_hash, item.parse_status,
                     item.error_message, item.error_line, int(item.is_entry_point)),
                )
                if item.parse_status != "parsed":
                    continue
                for symbol in item.symbols:
                    symbol_id = identifier()
                    symbol_ids[symbol.qualified_name].append(symbol_id)
                    row = {"symbol_id": symbol_id, "file_id": file_id, "relative_path": item.relative_path,
                           "module_name": item.module_name, "qualified_name": symbol.qualified_name,
                           "name": symbol.name, "kind": symbol.kind,
                           "parent_qualified_name": symbol.parent_qualified_name, "line_start": symbol.line_start}
                    symbol_rows[symbol_id] = row
                    self.store.execute(
                        """INSERT INTO symbols(
                            symbol_id,scan_id,file_id,qualified_name,name,kind,parent_qualified_name,
                            signature,docstring,purpose,decorators_json,line_start,line_end,column_start,
                            complexity,parameters_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (symbol_id, scan_id, file_id, symbol.qualified_name, symbol.name, symbol.kind,
                         symbol.parent_qualified_name, symbol.signature, symbol.docstring, symbol.purpose,
                         self.store.json(symbol.decorators), symbol.line_start, symbol.line_end, symbol.column_start,
                         symbol.complexity, self.store.json(symbol.parameters)),
                    )
                import_rows.extend([{"source": item, "file_id": file_id, "record": record} for record in item.imports])
            modules = {item.module_name for item in parsed if item.parse_status == "parsed"}
            declarations = self._dependency_declarations(root)
            for declaration, source in declarations:
                self.store.execute("INSERT OR IGNORE INTO libraries VALUES(?,?,?,?,?)", (identifier(), scan_id, declaration, "declared", source))
            aliases: dict[str, dict[str, str]] = defaultdict(dict)
            for wrapped in import_rows:
                item, file_id, record = wrapped["source"], wrapped["file_id"], wrapped["record"]
                target = self._absolute_import(item.module_name, item.relative_path.endswith("__init__.py"), record.target_module, record.level)
                resolved = self._resolve_import(target, record.imported_name, modules)
                root_name = (target or record.imported_name or "").split(".")[0]
                category = "internal" if resolved else self._import_category(root_name, declarations)
                self.store.execute(
                    "INSERT INTO imports VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (identifier(), scan_id, file_id, target, record.imported_name, record.alias, record.level,
                     category, resolved, record.line_number, record.column_number),
                )
                if category != "internal" and root_name:
                    self.store.execute("INSERT OR IGNORE INTO libraries VALUES(?,?,?,?,?)", (identifier(), scan_id, root_name, category, None))
                visible = record.alias or record.imported_name or target.split(".")[0]
                if visible:
                    base = resolved or target
                    if record.imported_name and resolved and not resolved.endswith("." + record.imported_name):
                        base += "." + record.imported_name
                    if not record.imported_name and not record.alias:
                        base = target.split(".")[0]
                    aliases[file_id][visible] = base
            class_by_method: dict[str, str] = {}
            for row in symbol_rows.values():
                parent = row["parent_qualified_name"]
                if parent and any(candidate["qualified_name"] == parent and candidate["kind"] == "class" for candidate in symbol_rows.values()):
                    class_by_method[row["symbol_id"]] = parent
            for item in parsed:
                if item.parse_status != "parsed":
                    continue
                file_id = file_ids[item.relative_path]
                for attribute in item.attributes:
                    class_candidates = symbol_ids.get(attribute.class_qualified_name, [])
                    if class_candidates:
                        self.store.execute("INSERT INTO attributes VALUES(?,?,?,?,?,?,?)", (
                            identifier(), scan_id, class_candidates[0], attribute.name, attribute.kind,
                            attribute.line_number, attribute.value_expression,
                        ))
                for flow in item.flows:
                    for sid in symbol_ids.get(flow.symbol_qualified_name, []):
                        self.store.execute("INSERT INTO flows VALUES(?,?,?,?,?,?,?)", (
                            identifier(), scan_id, sid, flow.variable_name, flow.event_kind, flow.expression, flow.line_number,
                        ))
                for call in item.calls:
                    caller_ids = symbol_ids.get(call.caller_qualified_name or "", [])
                    caller_id = caller_ids[0] if len(caller_ids) == 1 else None
                    candidates = self._call_candidates(
                        call.target_expression, item.module_name, file_id, caller_id, aliases,
                        symbol_rows, symbol_ids, class_by_method,
                    )
                    resolution = "resolved" if len(candidates) == 1 else "ambiguous" if candidates else "unresolved"
                    callee = candidates[0] if resolution == "resolved" else None
                    self.store.execute(
                        """INSERT INTO calls(
                            call_id,scan_id,caller_symbol_id,call_file_id,target_expression,callee_symbol_id,
                            resolution,candidate_json,line_number,column_number,arguments_json,keywords_json,
                            assignment_target,in_main_guard
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (identifier(), scan_id, caller_id, file_id, call.target_expression, callee, resolution,
                         self.store.json(candidates), call.line_number, call.column_number,
                         self.store.json(call.arguments), self.store.json(call.keywords),
                         call.assignment_target, int(call.in_main_guard)),
                    )
            self._diagnose(scan_id, modules)
            Reconstruction(self.store).build(scan_id, root)
        counts = Counter(item.parse_status for item in parsed)
        return {
            "scanned_files": len(parsed), "parsed_files": counts["parsed"], "failed_files": counts["failed"],
            "skipped_files": len(skipped) + counts["skipped"], "discovery_warnings": skipped,
            "symbols": sum(len(item.symbols) for item in parsed), "imports": len(import_rows),
        }

    @staticmethod
    def _absolute_import(source_module: str, is_package: bool, target: str, level: int) -> str:
        if not level:
            return target
        package = source_module.split(".") if is_package else source_module.split(".")[:-1]
        if level > 1:
            package = package[: -(level - 1)]
        return ".".join([*package, *filter(None, target.split("."))])

    @staticmethod
    def _resolve_import(target: str, imported: str | None, modules: set[str]) -> str | None:
        candidate = f"{target}.{imported}" if target and imported else ""
        if candidate in modules:
            return candidate
        if target in modules:
            return target
        return None

    @staticmethod
    def _dependency_declarations(root: Path) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        pyproject = root / "pyproject.toml"
        if pyproject.exists():
            try:
                project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
                dependencies = list(project.get("dependencies", []))
                for group in project.get("optional-dependencies", {}).values():
                    dependencies.extend(group)
                for spec in dependencies:
                    name = re.split(r"[<>=!~;[ ]", spec, maxsplit=1)[0]
                    if name:
                        found.append((normalise_distribution(name), "pyproject.toml"))
            except (OSError, tomllib.TOMLDecodeError):
                pass
        for requirements in root.glob("requirements*.txt"):
            try:
                for line in requirements.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith(("#", "-")):
                        continue
                    name = re.split(r"[<>=!~;[ ]", line, maxsplit=1)[0]
                    if name:
                        found.append((normalise_distribution(name), requirements.name))
            except (OSError, UnicodeDecodeError):
                pass
        return found

    @staticmethod
    def _import_category(root_name: str, declarations: Iterable[tuple[str, str]]) -> str:
        if root_name in sys.stdlib_module_names:
            return "stdlib"
        declared = {normalise_distribution(name).replace("-", "_") for name, _ in declarations}
        if root_name.lower() in declared or normalise_distribution(root_name) in {name for name, _ in declarations}:
            return "third_party"
        return "unknown_external"

    @staticmethod
    def _call_candidates(
        expression: str, module: str, file_id: str, caller_id: str | None,
        aliases: dict[str, dict[str, str]], symbol_rows: dict[str, dict[str, Any]],
        symbol_ids: dict[str, list[str]], class_by_method: dict[str, str],
    ) -> list[str]:
        def exact(name: str) -> list[str]:
            return symbol_ids.get(name, [])
        if expression.startswith(("self.", "cls.")) and caller_id:
            klass = class_by_method.get(caller_id)
            return exact(f"{klass}.{expression.split('.', 1)[1]}") if klass else []
        first, dot, rest = expression.partition(".")
        mapped = aliases[file_id].get(first)
        if mapped:
            return exact(mapped + (f".{rest}" if dot else ""))
        if dot:
            return exact(expression)
        local = exact(f"{module}.{expression}")
        if local:
            return local
        return [sid for sid, row in symbol_rows.items() if row["name"] == expression]

    def _diagnose(self, scan_id: str, modules: set[str]) -> None:
        files = self.store.rows("SELECT * FROM files WHERE scan_id=? AND parse_status='parsed'", (scan_id,))
        imports = self.store.rows("SELECT i.*, f.module_name, f.relative_path FROM imports i JOIN files f ON f.file_id=i.source_file_id WHERE i.scan_id=?", (scan_id,))
        edges: dict[str, set[str]] = defaultdict(set)
        locations: dict[tuple[str, str], tuple[str, int]] = {}
        for item in imports:
            if item["resolved_module"]:
                edges[item["module_name"]].add(item["resolved_module"])
                locations[(item["module_name"], item["resolved_module"])] = (item["relative_path"], item["line_number"])
            elif item["category"] == "unknown_external":
                self._finding(scan_id, "TB-IMPORT-001", "low", f"Import '{item['target_module']}' is not a project module, standard-library module, or declared dependency.", item["relative_path"], item["line_number"], {"target": item["target_module"]})
        for cycle in self._cycles(edges):
            first, second = cycle[0], cycle[1]
            path, line = locations.get((first, second), (None, None))
            self._finding(scan_id, "TB-CYCLE-001", "high", f"Circular import: {' -> '.join(cycle)}.", path, line, {"cycle": cycle})
        incoming = Counter(target for targets in edges.values() for target in targets)
        for module in modules:
            outgoing = len(edges[module])
            if outgoing > 10:
                file = next((f for f in files if f["module_name"] == module), None)
                self._finding(scan_id, "TB-DEPS-001", "medium", f"Module '{module}' imports {outgoing} internal modules.", file["relative_path"] if file else None, None, {"count": outgoing})
            if incoming[module] > 10:
                file = next((f for f in files if f["module_name"] == module), None)
                self._finding(scan_id, "TB-DEPENDANTS-001", "medium", f"Module '{module}' has {incoming[module]} internal dependants.", file["relative_path"] if file else None, None, {"count": incoming[module]})
        for file in files:
            module = file["module_name"]
            conventional_entry = module.split(".")[-1] in {"main", "__main__", "manage", "cli"}
            if module != "__root__" and not incoming[module] and not file["is_entry_point"] and not conventional_entry:
                self._finding(scan_id, "TB-UNUSED-001", "low", f"Module '{module}' has no detected internal importers; it may be an entry point, plugin, or externally loaded module.", file["relative_path"], None, {"module": module})
        calls = self.store.rows("SELECT c.*, f.relative_path FROM calls c JOIN files f ON f.file_id=c.call_file_id WHERE c.scan_id=? AND c.resolution='unresolved'", (scan_id,))
        for call in calls:
            self._finding(scan_id, "TB-CALL-001", "info", f"Could not statically resolve call '{call['target_expression']}'.", call["relative_path"], call["line_number"], {"expression": call["target_expression"]})
        failed = self.store.rows("SELECT * FROM files WHERE scan_id=? AND parse_status!='parsed'", (scan_id,))
        for file in failed:
            self._finding(scan_id, "TB-ANALYSIS-001", "medium", f"File could not be fully analysed: {file['error_message']}", file["relative_path"], file["error_line"], {"status": file["parse_status"]})

    @staticmethod
    def _cycles(edges: dict[str, set[str]]) -> list[list[str]]:
        cycles: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        def walk(start: str, current: str, path: list[str]) -> None:
            for next_node in edges[current]:
                if next_node == start:
                    cycle = path + [start]
                    core = cycle[:-1]
                    rotations = [tuple(core[index:] + core[:index]) for index in range(len(core))]
                    key = min(rotations)
                    if key not in seen:
                        seen.add(key)
                        cycles.append(cycle)
                elif next_node not in path:
                    walk(start, next_node, path + [next_node])
        for node in list(edges):
            walk(node, node, [node])
        return cycles

    def _finding(self, scan_id: str, rule: str, severity: str, message: str, path: str | None, line: int | None, evidence: dict[str, Any]) -> None:
        self.store.execute("INSERT INTO findings VALUES(?,?,?,?,?,?,?,?)", (identifier(), scan_id, rule, severity, message, path, line, self.store.json(evidence)))
