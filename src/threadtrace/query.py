"""Read-only access to the most recently completed analysis."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from .advanced import MAX_REPORTED_LOOP_DEPTH, advanced_facts, local_metrics, node_at
from .capabilities import capability_report
from .engine import Engine, ThreadError
from .runtime.concurrency import build_timeline
from .config import load as load_config, merge as merge_config
from .evidence import attach, classify, record
from .flowgraph import ControlFlow, def_use, definition_at, slice_records
from .metadata import parse_metadata
from .parser import discover_python_files, discover_source_units


LIBRARY_DESCRIPTIONS = {
    "argparse": "Builds command-line interfaces and parses command-line arguments.",
    "ast": "Parses Python source into an abstract syntax tree for inspection or transformation.",
    "asyncio": "Provides asynchronous I/O, tasks, and event loops.",
    "click": "Builds composable command-line interfaces using decorators.",
    "celery": "Runs distributed background tasks through message brokers.",
    "django": "A full-stack web framework with ORM, routing, templates, and admin tooling.",
    "fastapi": "Builds typed HTTP APIs, commonly with ASGI servers.",
    "flask": "Builds lightweight WSGI web applications and routes.",
    "httpx": "Makes synchronous and asynchronous HTTP requests.",
    "json": "Encodes and decodes JSON data.",
    "logging": "Emits structured application logs.",
    "numpy": "Provides numerical arrays and vectorised computation.",
    "pandas": "Provides tabular data structures and analysis tools.",
    "pathlib": "Offers object-oriented filesystem paths and file operations.",
    "pydantic": "Validates and serialises typed data models.",
    "pytest": "Runs Python tests and supplies fixtures and assertions.",
    "requests": "Makes HTTP requests as a synchronous client.",
    "sqlalchemy": "Provides SQL database access, query construction, and ORM facilities.",
    "sqlite3": "Accesses SQLite databases from Python.",
    "subprocess": "Starts and communicates with external processes.",
    "typer": "Builds typed command-line interfaces on top of Click.",
    "urllib": "Provides URL handling and HTTP client utilities.",
}


def _library_key(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _library_description(name: str) -> str:
    """Prefer a safe curated explanation; fall back to installed metadata."""
    key = _library_key(name)
    if key in LIBRARY_DESCRIPTIONS:
        return LIBRARY_DESCRIPTIONS[key]
    try:
        from importlib.metadata import PackageNotFoundError, metadata

        summary = metadata(name).get("Summary")
        if summary:
            return str(summary)
    except (PackageNotFoundError, ValueError):
        pass
    return "No local description is available; inspect its import sites and declared dependency metadata."


def _entrypoint_summary(entry: dict[str, Any]) -> str:
    kind = entry["kind"]
    if kind == "route":
        return "Handles a user-facing HTTP request."
    if kind in {"console", "cli", "argparse"}:
        return "Starts from a command a person can run in a terminal."
    if kind in {"script", "module"}:
        return "Starts when this Python script or module is run."
    if kind == "worker":
        return "Runs as background work, usually after another part of the program schedules it."
    return "A detected place where this project can start work."


def _confidence_explanation(confidence: str) -> str:
    return {
        "confirmed": "ThreadTrace can see this relationship directly in source.",
        "high": "Several source facts strongly support this relationship.",
        "possible": "This may happen, but Python's dynamic behaviour prevents a complete proof.",
        "unknown": "ThreadTrace cannot safely determine what happens next.",
    }.get(confidence, "ThreadTrace could not determine confidence for this relationship.")


def _evidence_class(confidence: str | None, evidence: Any) -> str:
    """Map alpha confidence to the v1 evidence vocabulary without losing it."""
    return classify(confidence, evidence)


def _source(row: dict[str, Any]) -> dict[str, Any] | None:
    values = {name: row.get(name) for name in ("relative_path", "module_name", "path", "line", "line_start", "line_number")
              if row.get(name) is not None}
    return values or None


def _coverage_py_import(root: Path) -> dict[str, Any]:
    """Read a coverage.py SQLite database without importing target code."""
    database = root / ".coverage"
    if not database.is_file():
        return {"status": "unavailable", "granularity": "line", "provenance": "coverage.py SQLite file", "files": {}}
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            files = {row[0]: row[1] for row in connection.execute("SELECT id,path FROM file")}
            contexts = {row[0]: row[1] for row in connection.execute("SELECT id,context FROM context")} if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='context'"
            ).fetchone() else {}
            found: dict[str, set[int]] = defaultdict(set)
            for file_id, context_id, bits in connection.execute("SELECT file_id,context_id,numbits FROM line_bits"):
                path = files.get(file_id)
                if not path:
                    continue
                try:
                    resolved = Path(path).resolve().relative_to(root.resolve()).as_posix()
                except (OSError, ValueError):
                    resolved = path
                for index, value in enumerate(bytes(bits)):
                    for bit in range(8):
                        if value & (1 << bit):
                            found[resolved].add(index * 8 + bit + 1)
            return {
                "status": "imported", "granularity": "line", "provenance": "coverage.py SQLite .coverage",
                "contexts": sorted(contexts.values()), "files": {path: sorted(lines) for path, lines in sorted(found.items())},
            }
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError) as error:
        return {"status": "unsupported", "granularity": "line", "provenance": "coverage.py SQLite .coverage",
                "files": {}, "error": type(error).__name__}


def _coverage_structure(path: Path) -> tuple[set[int], list[dict[str, Any]]]:
    """Return AST statement lines and directly observable branch outcomes."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set(), []
    lines = {node.lineno for node in ast.walk(tree) if isinstance(node, ast.stmt) and hasattr(node, "lineno")}
    branches: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            continue
        outcomes: list[dict[str, Any]] = []
        if node.body:
            outcomes.append({"name": "body", "start_line": node.body[0].lineno,
                             "end_line": getattr(node.body[-1], "end_lineno", node.body[-1].lineno)})
        if isinstance(node, ast.If) and node.orelse:
            outcomes.append({"name": "else", "start_line": node.orelse[0].lineno,
                             "end_line": getattr(node.orelse[-1], "end_lineno", node.orelse[-1].lineno)})
        branches.append({"line": node.lineno, "kind": type(node).__name__.lower(), "outcomes": outcomes})
    return lines, branches


def _walk_explanation(relation: dict[str, Any], target: dict[str, Any] | None) -> str:
    name = relation.get("target_name") or "the next step"
    if relation["kind"] == "direct_call":
        if target and target.get("purpose"):
            return f"Calls {name}. {target['purpose']}"
        return f"Calls {name} directly after the previous step."
    if relation["kind"] == "candidate_call":
        return f"May call {name}, but ThreadTrace cannot resolve the target uniquely."
    if relation["kind"] == "import":
        return f"Loads code from {name} so this step can use it."
    return f"Moves to {name} through a {relation['kind'].replace('_', ' ')} relationship."


def _friendly_finding(row: dict[str, Any]) -> tuple[str, str]:
    mapping = {
        "TB-ANALYSIS-001": ("ThreadTrace could not read this file", "Some project behaviour may be missing because this file could not be analysed."),
        "TB-CYCLE-001": ("Circular import", "These modules import one another, which can make startup order harder to reason about."),
        "TB-DYNAMIC-001": ("Dynamic Python boundary", "ThreadTrace cannot follow this dynamic operation, so paths past this line may be incomplete."),
        "TB-IMPORT-001": ("Unknown import", "ThreadTrace cannot identify this dependency as project code, a declared package, or the standard library."),
        "TB-DEPS-001": ("Highly connected module", "This module imports many internal modules, so changes may have broad effects."),
        "TB-DEPENDANTS-001": ("Widely used module", "Many internal modules depend on this code, so changes can have broad effects."),
        "TB-UNUSED-001": ("Possible external entry point", "No internal module imports this file; it may be started by a command, plugin, framework, or another external mechanism."),
        "TB-CALL-001": ("Unresolved call", "ThreadTrace cannot identify the call target from static source alone."),
    }
    return mapping.get(row["rule_id"], ("Analysis finding", row["message"]))


class QueryService:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.store = engine.store

    def _scan(self, project: str) -> dict[str, Any]:
        scan = self.engine.latest_scan(project)
        if "legacy_confidence_json" in scan:
            try:
                scan["legacy_confidence"] = json.loads(scan.pop("legacy_confidence_json") or "{}")
            except json.JSONDecodeError:
                scan["legacy_confidence"] = {}
        return scan

    def stale(self, project: str) -> bool:
        """Conservative Boolean compatibility wrapper for :meth:`staleness`."""
        return bool(self.staleness(project)["stale"])

    def staleness(self, project: str) -> dict[str, Any]:
        """Report changed inputs by cause and affected capability."""
        scan = self._scan(project)
        project_row = self.engine.get_project(project)
        root = Path(project_row["root_path"])
        inputs = {row["input_key"]: row for row in self.store.rows(
            "SELECT * FROM scan_inputs WHERE scan_id=?", (scan["scan_id"],))}
        if not inputs:
            stored = {row["relative_path"]: row["content_hash"] for row in self.store.rows(
                "SELECT relative_path,content_hash FROM files WHERE scan_id=?", (scan["scan_id"],))}
            current, warnings = self._current_sources(root, set(), [], [])
            causes = []
            if warnings or current != stored:
                causes.append({"cause": "source_changed",
                               "detail": "Tracked source files changed or could not be discovered.",
                               "capabilities": ["static-analysis", "references", "architecture"],
                               "files": sorted(set(stored) | set(current))})
            return {"scan_id": scan["scan_id"], "stale": bool(causes), "causes": causes,
                    "limitations": ["This pre-input-record scan can only compare source files."]}
        stored_discovery = json.loads(inputs["discovery"]["value"]) if "discovery" in inputs else {}
        includes = stored_discovery.get("includes", [])
        excludes = stored_discovery.get("excludes", [])
        explicit = stored_discovery.get("explicit_sources", [])
        generated = stored_discovery.get("generated", [])
        causes: list[dict[str, Any]] = []
        try:
            configuration = merge_config(load_config(), self.engine.project_preferences(project))
            current_discovery = {
                "includes": sorted(configuration["discovery"]["include"]),
                "excludes": sorted(configuration["discovery"]["exclude"]),
                "explicit_sources": sorted(explicit),
                "generated": generated,
            }
            if current_discovery != stored_discovery:
                causes.append({"cause": "discovery_changed",
                               "detail": "Effective discovery configuration changed.",
                               "capabilities": ["scan-coverage", "all-static-results"],
                               "files": []})
                includes = current_discovery["includes"]
                excludes = current_discovery["excludes"]
        except (OSError, ValueError) as exc:
            causes.append({"cause": "configuration_invalid",
                           "detail": f"Discovery configuration could not be validated: {exc}",
                           "capabilities": ["scan-coverage", "all-static-results"], "files": []})
        current, warnings = self._current_sources(root, set(excludes), includes, explicit)
        stored_sources = {key[7:]: value["input_hash"] for key, value in inputs.items()
                          if key.startswith("source:") and not key.startswith("source:generated://")}
        changed = sorted(path for path in set(current) | set(stored_sources)
                         if current.get(path) != stored_sources.get(path))
        if changed or warnings:
            causes.append({"cause": "source_changed",
                           "detail": "Tracked source units changed, were added/removed, or could not be read.",
                           "capabilities": ["static-analysis", "references", "architecture"],
                           "files": changed})
        current_metadata = {file.relative_path: file.content_hash or "" for file in parse_metadata(root).files}
        stored_metadata = {key[9:]: value["input_hash"] for key, value in inputs.items() if key.startswith("metadata:")}
        metadata_changed = sorted(path for path in set(current_metadata) | set(stored_metadata)
                                  if current_metadata.get(path) != stored_metadata.get(path))
        if metadata_changed:
            causes.append({"cause": "metadata_changed",
                           "detail": "Dependency or project metadata changed.",
                           "capabilities": ["dependencies", "imports", "entry-points"],
                           "files": metadata_changed})
        interpreter = {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "implementation": sys.implementation.name,
        }
        if "interpreter" in inputs and json.loads(inputs["interpreter"]["value"]) != interpreter:
            causes.append({"cause": "interpreter_changed",
                           "detail": "The analysis interpreter identity changed.",
                           "capabilities": ["import-resolution", "runtime-compatibility"],
                           "files": []})
        limitations = ["Generated-text inputs cannot be recreated from stored hashes alone."]
        return {"scan_id": scan["scan_id"], "stale": bool(causes), "causes": causes,
                "limitations": limitations}

    def _current_sources(self, root: Path, excludes: set[str], includes: list[str],
                         explicit: list[str]) -> tuple[dict[str, str], list[str]]:
        units, warnings = discover_source_units(root, excludes=excludes, includes=includes,
                                                explicit_sources=explicit)
        current: dict[str, str] = {}
        for unit in units:
            digest = unit.content_hash
            if digest is None and unit.source_text is not None:
                digest = hashlib.sha256(unit.source_text.encode("utf-8")).hexdigest()
            if digest is None:
                try:
                    digest = hashlib.sha256(Path(unit.resolved_path).read_bytes()).hexdigest()
                except OSError:
                    digest = ""
            current[unit.relative_path] = digest
        return current, warnings

    def capabilities(self, project: str, *, scan_id: str | None = None, interpreter: str | None = None,
                     run_id: str | None = None, feature: str | None = None,
                     entity: str | None = None) -> list[dict[str, Any]]:
        """Report scoped feature support without overstating project coverage."""
        project_row = self.engine.get_project(project)
        if scan_id:
            if not self.store.one("SELECT 1 FROM scans WHERE scan_id=? AND project_id=?", (scan_id, project_row["project_id"])):
                raise ThreadError("Scan does not belong to this project.")
        if run_id:
            if not self.store.one("SELECT 1 FROM runtime_traces WHERE trace_id=? AND project_id=?", (run_id, project_row["project_id"])):
                raise ThreadError("Runtime trace does not belong to this project.")
        return capability_report(project=project, scan_id=scan_id, interpreter=interpreter,
                                 run_id=run_id, feature=feature, entity=entity)

    @staticmethod
    def _decoded(rows: list[dict[str, Any]], *fields: str, mechanism: str = "stored static analysis evidence",
                 limitations: tuple[str, ...] = (), run: Any = None) -> list[dict[str, Any]]:
        for row in rows:
            for field in fields:
                if field in row:
                    try:
                        row[field.removesuffix("_json")] = json.loads(row.pop(field))
                    except json.JSONDecodeError:
                        row[field.removesuffix("_json")] = {}
            if "confidence" in row or isinstance(row.get("evidence"), dict):
                attach(row, mechanism, source=_source(row), run=run or row.get("trace_id"), limitations=limitations)
        return rows

    def overview(self, project: str) -> dict[str, Any]:
        scan = self._scan(project)
        row = self.store.one("SELECT content_json,generated_at FROM reports WHERE scan_id=? AND report_kind='overview'", (scan["scan_id"],))
        if not row:
            raise ThreadError("This scan predates reconstruction support. Run 'threadtrace scan' again.")
        result = json.loads(row["content_json"])
        result.update({"scan_id": scan["scan_id"], "generated_at": row["generated_at"]})
        for component in result.get("components", []):
            attach(component, "component inference from static module relationships")
        for entry in result.get("entry_points", []):
            attach(entry, "entry-point recognition from AST and stored evidence")
        result["evidence_record"] = record(
            "heuristically predicted", "stored static overview report", source={"scan_id": scan["scan_id"]},
            limitations=("The overview is a static synthesis, not proof of runtime behaviour.",),
        )
        return result

    def entrypoints(self, project: str, kind: str | None = None) -> list[dict[str, Any]]:
        scan = self._scan(project)
        sql = """SELECT e.*,s.qualified_name,f.relative_path FROM entry_points e
                 LEFT JOIN symbols s ON s.symbol_id=e.symbol_id LEFT JOIN files f ON f.file_id=s.file_id
                 WHERE e.scan_id=?"""
        values: list[Any] = [scan["scan_id"]]
        if kind:
            sql += " AND e.kind=?"
            values.append(kind)
        return self._decoded(self.store.rows(sql + " ORDER BY e.kind,e.name", tuple(values)), "input_json", "evidence_json",
                             mechanism="entry-point recognition from AST and stored evidence",
                             limitations=("Dynamic registration and framework conventions outside the recognised syntax remain unknown.",))

    def architecture(self, project: str) -> dict[str, Any]:
        scan = self._scan(project)
        components = self._decoded(self.store.rows("SELECT * FROM components WHERE scan_id=? ORDER BY name", (scan["scan_id"],)),
                                   "evidence_json", mechanism="component inference from static module relationships")
        members = self.store.rows("SELECT * FROM component_members WHERE component_id IN (SELECT component_id FROM components WHERE scan_id=?)", (scan["scan_id"],))
        by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for member in members:
            by_component[member["component_id"]].append(member)
        for component in components:
            component["members"] = by_component[component["component_id"]]
        edges = self._decoded(self.store.rows("""SELECT * FROM relationships WHERE scan_id=?
            AND source_module IS NOT NULL AND target_module IS NOT NULL
            ORDER BY source_module,target_module,kind""", (scan["scan_id"],)), "evidence_json",
            mechanism="stored static relationship")
        result = {"components": components, "relationships": edges}
        result["evidence_record"] = record("heuristically predicted", "static architecture reconstruction",
                                           source={"scan_id": scan["scan_id"]},
                                           limitations=("Component roles and relationships are conservative static inferences.",))
        return result

    def resources(self, project: str, kind: str | None = None) -> list[dict[str, Any]]:
        scan = self._scan(project)
        sql = "SELECT r.*,s.qualified_name FROM resources r LEFT JOIN symbols s ON s.symbol_id=r.symbol_id WHERE r.scan_id=?"
        values: list[Any] = [scan["scan_id"]]
        aliases = {
            "file": {"file", "configuration", "structured-data", "text", "template", "media", "compression", "package"},
            "env": {"environment"}, "environment": {"environment"}, "http": {"service"}, "service": {"service"},
        }
        if kind:
            kinds = aliases.get(kind, {kind})
            sql += " AND r.kind IN (%s)" % ",".join("?" for _ in kinds)
            values.extend(sorted(kinds))
        return self._decoded(self.store.rows(sql + " ORDER BY r.kind,r.module_name,r.name", tuple(values)), "evidence_json",
                             mechanism="resource recognition from static source")

    def path(self, project: str, entry: str, depth: int = 8, confidence: str = "possible") -> dict[str, Any]:
        scan = self._scan(project)
        selected = self.store.rows(
            "SELECT * FROM entry_points WHERE scan_id=? AND (entry_point_id=? OR name=?) ORDER BY confidence LIMIT 2",
            (scan["scan_id"], entry, entry),
        )
        if not selected:
            selected = self.store.rows(
                "SELECT * FROM entry_points WHERE scan_id=? AND name LIKE ? ORDER BY confidence LIMIT 2",
                (scan["scan_id"], f"%{entry}%"),
            )
        if len(selected) != 1:
            if not selected:
                raise ThreadError(f"No entry point matches '{entry}'.")
            raise ThreadError(f"Entry point '{entry}' is ambiguous; use its exact name or ID.")
        point = selected[0]
        allowed = self._confidence_values(confidence)
        if not point["symbol_id"]:
            return {"entry_point": point["name"], "steps": [], "stopped_at": "No static handler symbol was identified."}
        rows = self.store.rows("""SELECT r.*,source.qualified_name AS source_name,target.qualified_name AS target_name
            FROM relationships r LEFT JOIN symbols source ON source.symbol_id=r.source_symbol_id
            LEFT JOIN symbols target ON target.symbol_id=r.target_symbol_id WHERE r.scan_id=?""", (scan["scan_id"],))
        graph: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["source_symbol_id"] and row["target_symbol_id"] and row["confidence"] in allowed:
                graph[row["source_symbol_id"]].append(row)
        queue: deque[tuple[str, int]] = deque([(point["symbol_id"], 0)])
        seen = {point["symbol_id"]}
        steps: list[dict[str, Any]] = []
        while queue:
            source, level = queue.popleft()
            if level >= depth:
                continue
            for relation in graph[source]:
                relation["depth"] = level + 1
                relation["evidence"] = json.loads(relation.pop("evidence_json"))
                attach(relation, "stored static relationship", source=_source(relation),
                       limitations=("Dynamic dispatch can make this stored path incomplete.",))
                steps.append(relation)
                target = relation["target_symbol_id"]
                if target not in seen:
                    seen.add(target)
                    queue.append((target, level + 1))
        resources = self.store.rows("SELECT kind,name,operation,confidence,module_name FROM resources WHERE scan_id=? AND symbol_id IN (%s)" % ",".join("?" * len(seen)), (scan["scan_id"], *seen))
        for resource in resources:
            attach(resource, "resource recognition from static source", source=_source(resource))
        result = {"entry_point": point["name"], "handler": point["symbol_id"], "depth": depth, "steps": steps, "resources": resources}
        result["evidence_record"] = record(
            "heuristically predicted", "bounded entry-point path",
            source=_source(point), limitations=("The path follows stored direct relationships and is bounded by depth.",),
        )
        return result

    @staticmethod
    def _confidence_values(minimum: str) -> set[str]:
        levels = ["confirmed", "high", "possible", "unknown"]
        if minimum not in levels:
            raise ThreadError(f"Unknown confidence '{minimum}'.")
        return set(levels[: levels.index(minimum) + 1])

    def impact(self, project: str, target: str, depth: int = 3, direction: str = "both") -> dict[str, Any]:
        """Traverse callers, importers, inheritance, references, entries, and resources."""
        if direction not in {"both", "upstream", "downstream"}:
            raise ThreadError("Impact direction must be both, upstream, or downstream.")
        scan = self._scan(project)
        matches = self.symbol(project, target)
        if len(matches) != 1:
            if not matches:
                raise ThreadError(f"No symbol matches '{target}'.")
            raise ThreadError("Impact analysis requires one exact qualified symbol name.")
        start = matches[0]
        graph: dict[str, list[dict[str, Any]]] = defaultdict(list)
        reverse: dict[str, list[dict[str, Any]]] = defaultdict(list)
        module_graph: dict[str, set[str]] = defaultdict(set)
        rows = self.store.rows("""SELECT r.*,source.qualified_name AS source_name,target.qualified_name AS target_name
            FROM relationships r LEFT JOIN symbols source ON source.symbol_id=r.source_symbol_id
            LEFT JOIN symbols target ON target.symbol_id=r.target_symbol_id WHERE r.scan_id=?""", (scan["scan_id"],))
        for row in rows:
            if row["source_symbol_id"] and row["target_symbol_id"]:
                graph[row["source_symbol_id"]].append(row)
                reverse[row["target_symbol_id"]].append(row)
            if row["kind"] == "import" and row["source_module"] and row["target_module"]:
                module_graph[row["target_module"]].add(row["source_module"])

        def traverse(edges: dict[str, list[dict[str, Any]]], direction_name: str) -> tuple[list[dict[str, Any]], set[str], bool]:
            queue: deque[tuple[str, int]] = deque([(start["symbol_id"], 0)])
            seen = {start["symbol_id"]}
            discovered: list[dict[str, Any]] = []
            truncated = False
            while queue:
                current, level = queue.popleft()
                if level >= depth:
                    continue
                for relation in edges[current]:
                    if len(discovered) >= 500:
                        truncated = True
                        break
                    other = relation["target_symbol_id"] if direction_name == "downstream" else relation["source_symbol_id"]
                    found = dict(relation)
                    found["depth"] = level + 1
                    found["direction"] = direction_name
                    found["evidence"] = json.loads(found.pop("evidence_json"))
                    attach(found, f"stored {direction_name} static relationship", source=_source(found))
                    discovered.append(found)
                    if other not in seen:
                        seen.add(other)
                        queue.append((other, level + 1))
                if truncated:
                    break
            return discovered, seen, truncated

        downstream, downstream_ids, down_truncated = ([], {start["symbol_id"]}, False)
        upstream, upstream_ids, up_truncated = ([], {start["symbol_id"]}, False)
        if direction in {"both", "downstream"}:
            downstream, downstream_ids, down_truncated = traverse(graph, "downstream")
        if direction in {"both", "upstream"}:
            upstream, upstream_ids, up_truncated = traverse(reverse, "upstream")
        affected = downstream_ids | upstream_ids
        affected_modules = {start["module_name"]}
        for row in [*downstream, *upstream]:
            affected_modules.update(item for item in (row.get("source_module"), row.get("target_module")) if item)
        frontier = list(affected_modules)
        while frontier:
            module = frontier.pop()
            for importer in module_graph.get(module, set()):
                if importer in affected_modules:
                    continue
                affected_modules.add(importer)
                frontier.append(importer)
                if len(affected_modules) >= 500:
                    break
        references = self.store.rows(
            """SELECT r.reference_id,r.name,r.expression,r.kind,r.line_number,f.relative_path,
                      s.qualified_name AS scope_name
               FROM symbol_references r JOIN files f ON f.file_id=r.file_id
               LEFT JOIN scopes s ON s.scope_id=r.scope_id
               WHERE r.scan_id=? AND r.resolved_symbol_id=? ORDER BY f.relative_path,r.line_number""",
            (scan["scan_id"], start["symbol_id"]),
        )
        inheritance_users: list[dict[str, Any]] = []
        if start["kind"] == "class":
            for candidate in self.store.rows(
                "SELECT s.symbol_id,s.qualified_name,s.signature,s.line_start,f.relative_path FROM symbols s "
                "JOIN files f ON f.file_id=s.file_id WHERE s.scan_id=? AND s.kind='class' AND s.symbol_id<>?",
                (scan["scan_id"], start["symbol_id"]),
            ):
                bases = {item.strip().split(".")[-1] for item in (candidate["signature"] or "()").strip("()").split(",") if item.strip()}
                if start["name"] in bases:
                    inheritance_users.append(candidate)
        placeholders = ",".join("?" for _ in affected) or "NULL"
        entry_points = self.store.rows(
            f"SELECT entry_point_id,kind,name,module_name,confidence FROM entry_points WHERE scan_id=? AND symbol_id IN ({placeholders})",
            (scan["scan_id"], *affected),
        )
        module_slots = ",".join("?" for _ in affected_modules) or "NULL"
        resources = self.store.rows(
            f"""SELECT resource_id,kind,name,operation,confidence,module_name,symbol_id
                FROM resources WHERE scan_id=? AND (symbol_id IN ({placeholders}) OR module_name IN ({module_slots}))""",
            (scan["scan_id"], *affected, *affected_modules),
        )
        for item in entry_points:
            attach(item, "affected configured entry point", source=_source(item),
                   evidence_class=_evidence_class(item.get("confidence"), None))
        for item in resources:
            attach(item, "affected resource boundary", source=_source(item),
                   evidence_class=_evidence_class(item.get("confidence"), None))
        result = {
            "symbol": start["qualified_name"], "depth": depth, "direction": direction,
            "downstream": downstream, "upstream": upstream, "references": references,
            "inheritance_users": inheritance_users, "import_dependants": sorted(affected_modules - {start["module_name"]}),
            "entry_points": entry_points, "resources": resources,
            "budget": 500, "truncated": down_truncated or up_truncated or len(affected_modules) >= 500,
        }
        result["evidence_record"] = record(
            "heuristically predicted", "bounded directional static impact",
            source=_source(start),
            limitations=("Only stored relationships and resolved references are followed.",
                         "Dynamic dispatch, reflection, and runtime rebinding can add or remove impact."),
        )
        return result

    def _definition(self, project: str, symbol: dict[str, Any]) -> ast.FunctionDef | ast.AsyncFunctionDef:
        root = Path(self.engine.get_project(project)["root_path"])
        path = root / symbol["relative_path"]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise ThreadError(f"ThreadTrace could not read source for this definition: {exc}") from exc
        definition = definition_at(tree, symbol["line_start"])
        if definition is None:
            raise ThreadError("ThreadTrace could not match this stored symbol to current source. Run a new scan.")
        return definition

    def dataflow(self, project: str, reference: str, resource_kind: str | None = None) -> dict[str, Any]:
        scan = self._scan(project)
        symbols = self.symbol(project, reference)
        if len(symbols) != 1:
            raise ThreadError("Data-flow analysis requires one exact qualified symbol name.")
        symbol = symbols[0]
        symbol_id = symbol["symbol_id"]
        all_edges = self._decoded(self.store.rows("""SELECT d.*,source.qualified_name AS source_symbol,target.qualified_name AS target_symbol
            FROM data_edges d LEFT JOIN symbols source ON source.symbol_id=d.source_symbol_id
            LEFT JOIN symbols target ON target.symbol_id=d.target_symbol_id
            WHERE d.scan_id=? AND (d.source_symbol_id=? OR d.target_symbol_id=?) ORDER BY d.data_edge_id""", (scan["scan_id"], symbol_id, symbol_id)), "evidence_json",
            mechanism="bounded static data-flow edge")
        edges_truncated = len(all_edges) > 500
        edges = all_edges[:500]
        resources = self.resources(project, resource_kind)
        direct = [item for item in resources if item.get("symbol_id") == symbol_id]
        calls = self.store.rows(
            "SELECT target_expression,arguments_json,keywords_json,line_number FROM calls WHERE scan_id=? AND caller_symbol_id=?",
            (scan["scan_id"], symbol_id),
        )
        resource_flows: list[dict[str, Any]] = []
        for resource in direct:
            evidence = resource.get("evidence") or {}
            expression = evidence.get("expression") or resource["name"]
            names = {resource["name"]}
            for call in calls:
                if call["target_expression"] not in {expression, resource["name"]} and resource["name"] not in call["target_expression"]:
                    continue
                names.update(json.loads(call["arguments_json"] or "[]"))
                names.update(json.loads(call["keywords_json"] or "{}").values())
            sources = [edge for edge in edges if edge.get("target_symbol_id") == symbol_id and edge.get("target_name") in names]
            resource_flows.append({"resource": resource, "source_names": sorted(names), "data_edges": sources})
        boundaries = self.store.rows(
            """SELECT b.kind,b.expression,b.line_number,b.details_json FROM boundaries b
               JOIN scopes s ON s.scope_id=b.scope_id JOIN files f ON f.file_id=s.file_id
               WHERE b.scan_id=? AND f.relative_path=? AND s.qualified_name=?""",
            (scan["scan_id"], symbol["relative_path"], symbol["qualified_name"]),
        )
        for item in boundaries:
            item["details"] = json.loads(item.pop("details_json"))
        result = {"symbol": symbol["qualified_name"], "edges": edges,
                  "interprocedural": [item for item in edges if item.get("kind") in {"argument", "keyword_argument", "return_assignment"}],
                  "direct_resources": direct, "resource_flows": resource_flows, "boundaries": boundaries,
                  "budget": 500, "truncated": edges_truncated}
        result["evidence_record"] = record("heuristically predicted", "resolved direct-call and local data-flow edges",
                                           source=_source(symbol),
                                           limitations=("Only resolved direct calls and literal resource arguments are linked.",
                                                        "Dynamic aliases, mutation through unknown references, and indirect calls are boundaries."))
        return result

    def slice(self, project: str, reference: str, variable: str, direction: str = "both") -> dict[str, Any]:
        """Return a bounded forward, backward, or combined source slice."""
        if direction not in {"both", "backward", "forward"}:
            raise ThreadError("Slice direction must be both, backward, or forward.")
        symbols = self.symbol(project, reference)
        if len(symbols) != 1:
            raise ThreadError("Static slicing requires one exact qualified function or method name.")
        symbol = symbols[0]
        scan = self._scan(project)
        data_edges = self._decoded(self.store.rows(
            """SELECT * FROM data_edges WHERE scan_id=? AND (source_symbol_id=? OR target_symbol_id=?)
               AND (source_name=? OR target_name=?) ORDER BY data_edge_id""",
            (scan["scan_id"], symbol["symbol_id"], symbol["symbol_id"], variable, variable),
        ), "evidence_json", mechanism="bounded static slice")
        records = def_use(self._definition(project, symbol), symbol["qualified_name"])
        sliced = slice_records(variable, records, data_edges, direction)
        for item in sliced["events"]:
            item.setdefault("variable_name", item["name"])
            item.setdefault("event_kind", "assignment" if "expression" in item else "read")
            item.setdefault("line_number", item["line"])
        result = {
            "symbol": symbol["qualified_name"], "variable": variable, **sliced,
            "edges": data_edges,
            "limitation": "This is a conservative source slice. Dynamic aliases, unknown indexes, reflection, and indirect mutation are boundaries.",
        }
        result["evidence_record"] = record("heuristically predicted", "bounded static def-use slice", source=_source(symbol),
                                           limitations=(result["limitation"],))
        return result

    def control_flow(self, project: str, reference: str) -> dict[str, Any]:
        """Build a bounded syntax-level control-flow graph for one definition."""
        symbols = self.symbol(project, reference)
        if len(symbols) != 1:
            raise ThreadError("Control-flow analysis requires one exact qualified function or method name.")
        symbol = symbols[0]
        builder = ControlFlow(symbol["qualified_name"])
        nodes, edges = builder.build(self._definition(project, symbol))
        truncated = builder.truncate_edges()
        known = {node["id"] for node in nodes}
        edges = [edge for edge in edges if edge["source"] in known and edge["target"] in known]
        for node in nodes:
            attach(node, "syntax-level control-flow node", source=_source(symbol), evidence_class="statically resolved")
        for edge in edges:
            attach(edge, "syntax-level control-flow edge", source=_source(symbol), evidence_class="statically resolved")
        result = {
            "symbol": symbol["qualified_name"], "nodes": nodes, "edges": edges,
            "budget": {"nodes": 400, "edges": 1200}, "truncated": truncated,
            "limitation": "Syntax-level control flow. Call-exception edges are conservative; runtime branch selection, dynamic dispatch, and exact generator resumption are not proven.",
        }
        result["evidence_record"] = record("statically resolved", "bounded AST control-flow construction", source=_source(symbol),
                                           limitations=(result["limitation"],))
        return result

    def explain(self, project: str, target: str) -> dict[str, Any]:
        overview = self.overview(project)
        symbols = self.symbol(project, target)
        entries = [item for item in self.entrypoints(project) if item["name"] == target or item.get("qualified_name") == target]
        libraries = [item for item in self.libraries(project) if item["name"] == target]
        if entries:
            result = {"kind": "entry_point", "target": target, "entry_points": entries, "overview_purpose": overview["purpose"]}
        elif libraries:
            result = {"kind": "library", "target": target, "libraries": libraries, "overview_purpose": overview["purpose"]}
        elif symbols:
            symbol = symbols[0]
            result = {"kind": "symbol", "target": symbol["qualified_name"], "purpose": symbol["purpose"],
                      "signature": symbol["signature"], "incoming": self.callers(project, symbol["qualified_name"]),
                      "outgoing": self.callees(project, symbol["qualified_name"])["calls"]}
        else:
            result = None
        if result is not None:
            result["evidence_record"] = record("statically resolved", "stored static explanation",
                                               source={"target": target}, limitations=("Inspect the referenced stored evidence.",))
            return result
        if target in {"overview", "project"}:
            return overview
        raise ThreadError(f"No entry point or symbol matches '{target}'.")

    def ai_explain(self, project: str, target: str, model: str | None = None,
                   endpoint: str | None = None) -> dict[str, Any]:
        """Ask the explicitly selected local Ollama service to narrate stored evidence."""
        from .ai import AIError, explain as explain_with_ai

        scan = self._scan(project)
        static = self.explain(project, target)
        evidence = {
            "target": static,
            "project_overview": self.overview(project),
            "libraries": self.libraries(project),
        }
        try:
            narrative = explain_with_ai(evidence, model, endpoint)
        except AIError as exc:
            raise ThreadError(str(exc)) from exc
        report_kind = "ai_explain:" + hashlib.sha256(f"{target}|ollama|{model or ''}".encode()).hexdigest()[:16]
        content = {"target": target, "provider": "ollama", "model": model, "narrative": narrative,
                   "notice": "AI-assisted interpretation. Verify every claim against the included source evidence.",
                   "evidence_record": record(
                       "heuristically predicted", "local AI narration of stored evidence", source={"target": target},
                       run={"model": model, "provider": "ollama"},
                       limitations=("The narrative is generated text; verify every claim against the stored evidence.",),
                   )}
        self.store.execute(
            "INSERT OR REPLACE INTO reports VALUES(?,?,?,?,?)",
            (hashlib.sha256(f"{scan['scan_id']}|{report_kind}".encode()).hexdigest(), scan["scan_id"], report_kind,
             self.store.json(content), datetime.now(UTC).isoformat()),
        )
        self.store.connection.commit()
        return content

    def guide(self, project: str) -> dict[str, Any]:
        """Return a beginner-facing starting point without hiding evidence."""
        overview = self.overview(project)
        order = {"route": 0, "console": 1, "cli": 2, "argparse": 3, "script": 4, "module": 5, "worker": 6, "test": 9}
        starting_points = [item for item in self.entrypoints(project) if item["kind"] != "test"]
        starting_points.sort(key=lambda item: (order.get(item["kind"], 8), item["name"]))
        for item in starting_points:
            item["plain_summary"] = _entrypoint_summary(item)
        components = [item for item in overview["components"] if item["role"] != "test"]
        result = {
            "purpose": overview["purpose"],
            "starting_points": starting_points,
            "main_parts": components,
            "needs_attention": self.findings(project),
            "next_step": starting_points[0]["name"] if starting_points else None,
        }
        result["evidence_record"] = record("heuristically predicted", "static starting-point guidance",
                                           source=overview["evidence_record"]["source"],
                                           limitations=("Guidance is derived from stored static evidence.",))
        return result

    def walk(self, project: str, entry: str, depth: int = 8, confidence: str = "possible") -> dict[str, Any]:
        """Explain one supported entry-point path in plain, evidence-backed terms."""
        path = self.path(project, entry, depth, confidence)
        steps: list[dict[str, Any]] = []
        for ordinal, relation in enumerate(path["steps"], 1):
            target = self.store.one(
                """SELECT s.qualified_name,s.kind,s.purpose,s.line_start,s.line_end,f.relative_path,f.module_name
                   FROM symbols s JOIN files f ON f.file_id=s.file_id WHERE s.symbol_id=?""",
                (relation["target_symbol_id"],),
            )
            step = {
                "ordinal": ordinal,
                "kind": relation["kind"],
                "confidence": relation["confidence"],
                "source": relation.get("source_name") or "starting point",
                "target": relation.get("target_name") or "unknown target",
                "explanation": _walk_explanation(relation, target),
                "confidence_explanation": _confidence_explanation(relation["confidence"]),
                "evidence": relation["evidence"],
            }
            if target:
                step.update(target)
            attach(step, "entry-point path step", source=_source(step))
            steps.append(step)
        return {
            "entry_point": path["entry_point"],
            "depth": path["depth"],
            "steps": steps,
            "resources": path["resources"],
            "stopped_at": path.get("stopped_at"),
        }

    def file_map(self, project: str, target: str | None = None, depth: int = 4) -> dict[str, Any]:
        """Show the files linked to a selected symbol, entry point, or module."""
        if depth < 1:
            raise ThreadError("Map depth must be at least 1.")
        scan = self._scan(project)
        if target is None:
            files = self.store.rows(
                "SELECT relative_path,module_name FROM files WHERE scan_id=? ORDER BY relative_path",
                (scan["scan_id"],),
            )
            links = self.store.rows(
                """SELECT DISTINCT source_file.relative_path AS source_file,target_file.relative_path AS target_file,
                          r.kind,r.confidence
                   FROM relationships r
                   JOIN symbols source ON source.symbol_id=r.source_symbol_id
                   JOIN files source_file ON source_file.file_id=source.file_id
                   JOIN symbols target_symbol ON target_symbol.symbol_id=r.target_symbol_id
                   JOIN files target_file ON target_file.file_id=target_symbol.file_id
                   WHERE r.scan_id=? AND source_file.relative_path != target_file.relative_path
                   ORDER BY source_file.relative_path,target_file.relative_path,r.kind""",
                (scan["scan_id"],),
            )
            return {
                "target": "entire project",
                "depth": None,
                "files": [dict(row, reasons=["Included in the completed project scan."]) for row in files],
                "links": links,
                "evidence_record": record("statically resolved", "completed project scan file inventory",
                                           source={"scan_id": scan["scan_id"]}),
            }
        entry_matches = self.store.rows(
            "SELECT * FROM entry_points WHERE scan_id=? AND (entry_point_id=? OR name=?)", (scan["scan_id"], target, target)
        )
        symbols = self.symbol(project, target)
        module = self.store.one("SELECT module_name FROM files WHERE scan_id=? AND module_name=?", (scan["scan_id"], target))
        if len(entry_matches) == 1:
            start_symbol, start_module = entry_matches[0]["symbol_id"], entry_matches[0]["module_name"]
        elif len(symbols) == 1:
            start_symbol, start_module = symbols[0]["symbol_id"], symbols[0]["module_name"]
        elif module:
            start_symbol, start_module = None, module["module_name"]
        elif len(entry_matches) > 1 or len(symbols) > 1:
            raise ThreadError(f"Target '{target}' is ambiguous; use an entry-point ID, module name, or qualified symbol.")
        else:
            raise ThreadError(f"No entry point, module, or symbol matches '{target}'.")

        files_by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for file in self.store.rows("SELECT module_name,relative_path FROM files WHERE scan_id=?", (scan["scan_id"],)):
            files_by_module[file["module_name"]].append(file)
        relations = self.store.rows(
            """SELECT r.*,ss.qualified_name AS source_name,ts.qualified_name AS target_name,
                      sf.relative_path AS source_path,tf.relative_path AS target_path
               FROM relationships r
               LEFT JOIN symbols ss ON ss.symbol_id=r.source_symbol_id
               LEFT JOIN symbols ts ON ts.symbol_id=r.target_symbol_id
               LEFT JOIN files sf ON sf.file_id=ss.file_id
               LEFT JOIN files tf ON tf.file_id=ts.file_id
               WHERE r.scan_id=?""",
            (scan["scan_id"],),
        )
        queue: deque[tuple[str | None, str | None, int]] = deque([(start_symbol, start_module, 0)])
        seen = {(start_symbol, start_module)}
        links: list[dict[str, Any]] = []
        file_reasons: dict[str, list[str]] = defaultdict(list)
        while queue:
            symbol_id, module_name, level = queue.popleft()
            if level >= depth:
                continue
            for relation in relations:
                if symbol_id and relation["source_symbol_id"] != symbol_id:
                    continue
                if not symbol_id and module_name and relation["source_module"] != module_name:
                    continue
                source_paths = [relation["source_path"]] if relation["source_path"] else [item["relative_path"] for item in files_by_module.get(relation["source_module"], [])]
                target_paths = [relation["target_path"]] if relation["target_path"] else [item["relative_path"] for item in files_by_module.get(relation["target_module"], [])]
                for path in source_paths:
                    file_reasons[path].append(f"source of {relation['kind']}")
                for path in target_paths:
                    file_reasons[path].append(f"target of {relation['kind']}")
                link = {
                    "from": relation["source_name"] or relation["source_module"],
                    "to": relation["target_name"] or relation["target_module"],
                    "kind": relation["kind"], "confidence": relation["confidence"],
                    "source_paths": source_paths, "target_paths": target_paths,
                    "evidence": json.loads(relation["evidence_json"]), "depth": level + 1,
                }
                attach(link, "bounded file-link traversal", source={"paths": source_paths})
                links.append(link)
                next_key = (relation["target_symbol_id"], relation["target_module"])
                if next_key not in seen and (next_key[0] or next_key[1]):
                    seen.add(next_key)
                    queue.append((next_key[0], next_key[1], level + 1))
        files = [{"relative_path": path, "reasons": sorted(set(reasons))} for path, reasons in sorted(file_reasons.items())]
        for item in files:
            attach(item, "reachable static file link", source={"path": item["relative_path"]},
                   evidence_class="heuristically predicted", limitations=("Links are bounded static relationships.",))
        result = {"target": target, "depth": depth, "files": files, "links": links}
        result["evidence_record"] = record("heuristically predicted", "bounded file-link traversal",
                                           limitations=("Links follow stored static relationships and are depth-bounded.",))
        return result

    def file_graph(self, project: str) -> dict[str, Any]:
        """Return the complete file-level graph without hiding why edges exist."""
        scan = self._scan(project)
        files = self.store.rows(
            """SELECT file_id,relative_path,module_name,parse_status FROM files
               WHERE scan_id=? AND parse_status='parsed' ORDER BY relative_path""",
            (scan["scan_id"],),
        )
        module_to_file = {row["module_name"]: row for row in files}

        def known_module(reference: str | None) -> str | None:
            candidate = reference or ""
            while candidate:
                if candidate in module_to_file:
                    return candidate
                candidate = candidate.rpartition(".")[0]
            return None

        relationships = self.store.rows(
            """SELECT r.*, source.qualified_name AS source_name, target.qualified_name AS target_name
               FROM relationships r
               LEFT JOIN symbols source ON source.symbol_id=r.source_symbol_id
               LEFT JOIN symbols target ON target.symbol_id=r.target_symbol_id
               WHERE r.scan_id=? AND r.source_module IS NOT NULL AND r.target_module IS NOT NULL
               ORDER BY r.source_module,r.target_module,r.kind""",
            (scan["scan_id"],),
        )
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in relationships:
            source, target = known_module(row["source_module"]), known_module(row["target_module"])
            if not source or not target or source == target:
                continue
            key = (source, target, row["kind"], row["confidence"])
            edge = grouped.setdefault(key, {
                "source": source, "target": target, "kind": row["kind"], "confidence": row["confidence"],
                "labels": [], "evidence": [],
            })
            label = (
                f"{row['source_name']} → {row['target_name']}"
                if row["source_name"] and row["target_name"] else row["kind"].replace("_", " ")
            )
            if label not in edge["labels"]:
                edge["labels"].append(label)
            evidence = json.loads(row["evidence_json"])
            if evidence not in edge["evidence"]:
                edge["evidence"].append(evidence)
        nodes = [
            {"id": row["module_name"], "name": row["relative_path"], "kind": "file", "relative_path": row["relative_path"], "module_name": row["module_name"]}
            for row in files
        ]
        for node in nodes:
            attach(node, "file discovery and AST parse", source=_source(node), evidence_class="statically resolved")
        for edge in grouped.values():
            attach(edge, "stored static relationship", evidence_class=_evidence_class(edge.get("confidence"), None))
        result = {"kind": "file", "nodes": nodes, "edges": list(grouped.values())}
        result["evidence_record"] = record("statically resolved", "complete stored file-level graph",
                                           source={"scan_id": scan["scan_id"]})
        return result

    def code_graph(self, project: str) -> dict[str, Any]:
        """Return functions/classes and their static calls, membership, and known bases."""
        scan = self._scan(project)
        symbols = self.store.rows(
            """SELECT s.symbol_id,s.qualified_name,s.name,s.kind,s.parent_qualified_name,s.signature,s.purpose,
                      s.line_start,s.line_end,f.relative_path,f.module_name
               FROM symbols s JOIN files f ON f.file_id=s.file_id
               WHERE s.scan_id=? AND s.kind IN ('function','async_function','method','async_method','class')
               ORDER BY s.qualified_name""",
            (scan["scan_id"],),
        )
        by_id = {row["symbol_id"]: row for row in symbols}
        by_qualified = {row["qualified_name"]: row for row in symbols}
        edges: list[dict[str, Any]] = []
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}

        def add_edge(source: str, target: str, kind: str, confidence: str, label: str, evidence: dict[str, Any] | None = None) -> None:
            key = (source, target, kind, confidence)
            edge = grouped.setdefault(key, {"source": source, "target": target, "kind": kind, "confidence": confidence, "labels": [], "evidence": []})
            if label not in edge["labels"]:
                edge["labels"].append(label)
            if evidence and evidence not in edge["evidence"]:
                edge["evidence"].append(evidence)

        relationships = self.store.rows(
            """SELECT r.*, source.qualified_name AS source_name,target.qualified_name AS target_name
               FROM relationships r LEFT JOIN symbols source ON source.symbol_id=r.source_symbol_id
               LEFT JOIN symbols target ON target.symbol_id=r.target_symbol_id
               WHERE r.scan_id=? AND r.source_symbol_id IS NOT NULL AND r.target_symbol_id IS NOT NULL
               ORDER BY r.kind""",
            (scan["scan_id"],),
        )
        for row in relationships:
            if row["source_symbol_id"] not in by_id or row["target_symbol_id"] not in by_id:
                continue
            add_edge(
                row["source_symbol_id"], row["target_symbol_id"], row["kind"], row["confidence"],
                f"{row.get('source_name') or 'source'} → {row.get('target_name') or 'target'}",
                json.loads(row["evidence_json"]),
            )
        for symbol in symbols:
            parent = by_qualified.get(symbol["parent_qualified_name"])
            if parent and parent["kind"] == "class":
                add_edge(parent["symbol_id"], symbol["symbol_id"], "contains", "confirmed", f"{parent['qualified_name']} contains {symbol['name']}")
        classes_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for symbol in symbols:
            if symbol["kind"] == "class":
                classes_by_name[symbol["name"]].append(symbol)
        for symbol in symbols:
            if symbol["kind"] != "class" or not symbol["signature"]:
                continue
            bases = symbol["signature"].strip().strip("()")
            for base in (item.strip().split(".")[-1] for item in bases.split(",") if item.strip()):
                matches = classes_by_name.get(base, [])
                if len(matches) == 1:
                    add_edge(symbol["symbol_id"], matches[0]["symbol_id"], "inherits", "possible", f"{symbol['qualified_name']} inherits {base}")
        edges.extend(grouped.values())
        for edge in edges:
            attach(edge, "stored symbol relationship", evidence_class=_evidence_class(edge.get("confidence"), None))
        nodes = [dict(row, id=row["symbol_id"]) for row in symbols]
        for node in nodes:
            attach(node, "AST symbol definition", source=_source(node), evidence_class="statically resolved")
        result = {"kind": "code", "nodes": nodes, "edges": edges}
        result["evidence_record"] = record("statically resolved", "complete stored symbol graph",
                                           source={"scan_id": scan["scan_id"]})
        return result

    def findings(self, project: str, details: bool = False) -> list[dict[str, Any]]:
        """Keep important analysis gaps separate from low-level call resolution noise."""
        rows = self.diagnostics(project, include_unresolved_calls=details)
        if not details:
            rows = [row for row in rows if row["rule_id"] != "TB-CALL-001"]
        for row in rows:
            row["plain_title"], row["plain_explanation"] = _friendly_finding(row)
        return rows

    def compare(self, project: str, first: str, second: str) -> dict[str, Any]:
        project_row = self.engine.get_project(project)
        scans = self.store.rows("SELECT scan_id,completed_at,status FROM scans WHERE project_id=? AND status='completed' AND (scan_id=? OR scan_id=?)", (project_row["project_id"], first, second))
        if len(scans) != 2:
            raise ThreadError("Both scans must be completed scans from this project.")
        values: dict[str, dict[str, set[str]]] = {}
        for scan in scans:
            scan_id = scan["scan_id"]
            values[scan_id] = {
                "entry_points": {f"{row['kind']}:{row['name']}" for row in self.store.rows("SELECT kind,name FROM entry_points WHERE scan_id=?", (scan_id,))},
                "components": {row["name"] for row in self.store.rows("SELECT name FROM components WHERE scan_id=?", (scan_id,))},
                "resources": {f"{row['kind']}:{row['name']}" for row in self.store.rows("SELECT kind,name FROM resources WHERE scan_id=?", (scan_id,))},
                "findings": {f"{row['rule_id']}:{row['relative_path']}:{row['line_number']}" for row in self.store.rows("SELECT rule_id,relative_path,line_number FROM findings WHERE scan_id=?", (scan_id,))},
            }
        before, after = first, second
        result = {"before": before, "after": after, "changes": {
            key: {"added": sorted(values[after][key] - values[before][key]), "removed": sorted(values[before][key] - values[after][key])}
            for key in values[before]
        }}
        result["evidence_record"] = record(
            "statically resolved", "comparison of two completed static scans",
            source={"before": before, "after": after},
            limitations=("Only stored entry points, components, resources, and findings are compared.",),
        )
        return result

    def traces(self, project: str) -> list[dict[str, Any]]:
        project_row = self.engine.get_project(project)
        rows = self.store.rows("SELECT * FROM runtime_traces WHERE project_id=? ORDER BY finished_at DESC", (project_row["project_id"],))
        for row in rows:
            row["command"] = json.loads(row.pop("command_json"))
            row["launch"] = json.loads(row.pop("launch_json") or "{}")
            row["redaction_enabled"] = bool(row["redaction_enabled"])
            attach(row, "persisted explicit runtime trace", source={"command": row["command"], "cwd": row.get("cwd_path")},
                   run=row["trace_id"], evidence_class="observed at runtime" if row.get("finished_at") else "unknown",
                   limitations=("Only the explicitly started command and instrumentable Python children are observed.",))
        return rows

    def trace_events(self, project: str, trace_id: str, kind: str | None = None,
                     path_prefix: str | None = None, search: str | None = None) -> list[dict[str, Any]]:
        project_row = self.engine.get_project(project)
        trace = self.store.one("SELECT 1 FROM runtime_traces WHERE trace_id=? AND project_id=?", (trace_id, project_row["project_id"]))
        if not trace:
            raise ThreadError("Trace does not belong to this project.")
        events = self._decoded(self.store.rows("SELECT * FROM runtime_events WHERE trace_id=? ORDER BY ordinal", (trace_id,)),
                               "evidence_json", mechanism="persisted runtime event", run=trace_id)
        if kind:
            events = [event for event in events if event["event_kind"] == kind]
        if path_prefix:
            events = [event for event in events if str(event.get("relative_path") or "").startswith(path_prefix)]
        if search:
            needle = search.casefold()
            events = [event for event in events if needle in json.dumps(event, sort_keys=True, default=str).casefold()]
        return events

    def trace_concurrency(self, project: str, trace_id: str) -> dict[str, Any]:
        """Return process/thread/task lanes, observed waits, and heuristic findings."""
        events = self.trace_events(project, trace_id)
        result = build_timeline(events)
        result.update({"trace_id": trace_id, "evidence_class": "observed at runtime"})
        return result

    def trace_resources(self, project: str, trace_id: str) -> dict[str, Any]:
        """Return the observed resource and serialisation timeline for a trace."""
        events = [
            event for event in self.trace_events(project, trace_id)
            if event["event_kind"] == "resource"
        ]
        counts: Counter[str] = Counter()
        statuses: Counter[str] = Counter()
        failures = 0
        for event in events:
            details = event["evidence"]["details"]
            counts[details.get("resource_kind", "unknown")] += 1
            status = details.get("status", "unknown")
            statuses[status] += 1
            failures += int(status == "failed")
        result = {
            "trace_id": trace_id,
            "evidence_class": "observed at runtime",
            "event_count": len(events),
            "failure_count": failures,
            "by_status": dict(sorted(statuses.items())),
            "by_resource": dict(sorted(counts.items())),
            "timeline": events,
        }
        result["evidence_record"] = record(
            "observed at runtime", "persisted resource events from one trace", run=trace_id,
            limitations=("Native drivers, packet inspection, and operations without a project Python origin are unavailable.",),
        )
        return result

    def trace_failure(self, project: str, trace_id: str) -> dict[str, Any]:
        """Return the captured report for one failed explicit runtime trace."""
        project_row = self.engine.get_project(project)
        trace = self.store.one("SELECT 1 FROM runtime_traces WHERE trace_id=? AND project_id=?", (trace_id, project_row["project_id"]))
        if not trace:
            raise ThreadError("Trace does not belong to this project.")
        failure = self.store.one("SELECT * FROM runtime_failures WHERE trace_id=?", (trace_id,))
        if not failure:
            raise ThreadError("This trace has no recorded failure report.")
        failure["report"] = json.loads(failure.pop("report_json"))
        attach(failure, "persisted failure report from one trace", run=trace_id,
               evidence_class="observed at runtime" if failure["report"].get("terminating_exception") else "unknown",
               limitations=("The failure report correlates observed trace evidence and may not explain unobserved state.",))
        return failure

    def trace_profile(self, project: str, trace_id: str) -> dict[str, Any]:
        """Summarise observed function timing without presenting tracer overhead as target time."""
        project_row = self.engine.get_project(project)
        trace = self.store.one("SELECT * FROM runtime_traces WHERE trace_id=? AND project_id=?", (trace_id, project_row["project_id"]))
        if not trace:
            raise ThreadError("Trace does not belong to this project.")
        events = self.trace_events(project, trace_id)
        starts: dict[str, dict[str, Any]] = {}
        functions: dict[str, dict[str, Any]] = {}
        for event in events:
            details = event["evidence"]["details"]
            frame = details.get("frame_id")
            if not frame:
                continue
            name = str(details.get("name") or "<function>")
            if details.get("kind") == "call":
                starts[frame] = details
                item = functions.setdefault(name, {"name": name, "path": details.get("relative_path"),
                                                  "calls": 0, "cumulative_seconds": 0.0, "max_seconds": 0.0})
                item["calls"] += 1
            elif details.get("kind") in {"return", "yield"} and frame in starts:
                start = starts.pop(frame)
                duration = max(0.0, float(details.get("elapsed_seconds", 0)) - float(start.get("elapsed_seconds", 0)))
                item = functions.setdefault(name, {"name": name, "path": details.get("relative_path"),
                                                  "calls": 1, "cumulative_seconds": 0.0, "max_seconds": 0.0})
                item["cumulative_seconds"] = round(item["cumulative_seconds"] + duration, 6)
                item["max_seconds"] = round(max(item["max_seconds"], duration), 6)
        for item in functions.values():
            item["average_seconds"] = round(item["cumulative_seconds"] / item["calls"], 6) if item["calls"] else 0.0
        hot_path = sorted(functions.values(), key=lambda item: item["cumulative_seconds"], reverse=True)[:20]
        launch = json.loads(trace.get("launch_json") or "{}")
        result = {
            "trace_id": trace_id, "evidence_class": "observed at runtime",
            "functions": sorted(functions.values(), key=lambda item: item["name"]),
            "hot_path": hot_path,
            "memory": launch.get("process_metrics", {"status": "unknown", "source": "child process rusage"}),
            "timing_source": "recorded call/return elapsed timestamps",
            "instrumentation": (launch.get("instrumentation") or {}),
            "limitations": [
                "Durations are observed wall time and include tracer overhead; they are not target-only profiles.",
                "Recursive frames are attributed to the function name and may be combined.",
                "Memory is a child-process peak when the platform exposes it, not an allocation profile.",
            ],
        }
        result["evidence_record"] = record("observed at runtime", "joins persisted call/return frames", run=trace_id,
                                           limitations=tuple(result["limitations"]))
        return result

    def coverage(self, project: str, trace_id: str) -> dict[str, Any]:
        """Build native line/function coverage from the stored trace events."""
        project_row = self.engine.get_project(project)
        trace = self.store.one(
            "SELECT * FROM runtime_traces WHERE trace_id=? AND project_id=?", (trace_id, project_row["project_id"])
        )
        if not trace:
            raise ThreadError("Trace does not belong to this project.")
        if not trace.get("scan_id"):
            raise ThreadError("This trace is not associated with a static scan, so coverage cannot be compared to source.")
        root = Path(project_row["root_path"])
        source_files = self.store.rows(
            "SELECT relative_path,content_hash FROM files WHERE scan_id=? AND parse_status='parsed' ORDER BY relative_path",
            (trace["scan_id"],),
        )
        line_events = self.store.rows(
            "SELECT relative_path,line_number FROM runtime_events WHERE trace_id=? AND event_kind='line'"
            " AND relative_path IS NOT NULL AND line_number IS NOT NULL",
            (trace_id,),
        )
        observed: dict[str, set[int]] = defaultdict(set)
        for event in line_events:
            observed[event["relative_path"]].add(event["line_number"])
        function_rows = self.store.rows(
            """SELECT f.relative_path,s.qualified_name,s.line_start,s.line_end FROM symbols s
               JOIN files f ON f.file_id=s.file_id WHERE s.scan_id=? AND s.kind IN ('function','method')
               ORDER BY f.relative_path,s.line_start""",
            (trace["scan_id"],),
        )
        files: list[dict[str, Any]] = []
        candidate_total = executed_total = 0
        branch_total = branch_observed = 0
        structures: dict[str, set[int]] = {}
        for item in source_files:
            relative = item["relative_path"]
            path = root / relative
            candidates, branches = _coverage_structure(path)
            try:
                matches_scan = hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest() == item["content_hash"]
            except (OSError, UnicodeDecodeError):
                matches_scan = False
            executed = observed.get(relative, set()) & candidates
            structures[relative] = candidates
            rendered_branches: list[dict[str, Any]] = []
            for branch in branches:
                outcomes = []
                for outcome in branch["outcomes"]:
                    hit = any(outcome["start_line"] <= line <= outcome["end_line"] for line in observed.get(relative, set()))
                    outcomes.append({**outcome, "observed": hit})
                    branch_total += 1
                    branch_observed += int(hit)
                rendered_branches.append({"line": branch["line"], "kind": branch["kind"], "outcomes": outcomes})
            candidate_total += len(candidates)
            executed_total += len(executed)
            files.append({
                "path": relative,
                "source_matches_scan": matches_scan,
                "candidate_lines": sorted(candidates),
                "executed_lines": sorted(executed),
                "unexecuted_lines": sorted(candidates - executed),
                "line_coverage_percent": round(100 * len(executed) / len(candidates), 2) if candidates else None,
                "branches": rendered_branches,
            })
        functions: list[dict[str, Any]] = []
        for symbol in function_rows:
            candidates = {line for line in structures.get(symbol["relative_path"], set())
                          if symbol["line_start"] <= line <= (symbol["line_end"] or symbol["line_start"])}
            executed = observed.get(symbol["relative_path"], set()) & candidates
            functions.append({
                "symbol": symbol["qualified_name"], "path": symbol["relative_path"],
                "executed": bool(executed), "executed_lines": sorted(executed),
                "candidate_line_count": len(candidates),
            })
        imported = _coverage_py_import(root)
        result = {
            "trace_id": trace_id,
            "scan_id": trace["scan_id"],
            "evidence_class": "observed at runtime",
            "coverage_py_import": imported,
            "granularity": "line",
            "provenance": "native trace events joined to AST source; coverage.py data is reported separately",
            "line_coverage": {"executed": executed_total, "candidates": candidate_total,
                              "percent": round(100 * executed_total / candidate_total, 2) if candidate_total else None},
            "branch_observation": {"observed_outcomes": branch_observed, "observable_outcomes": branch_total,
                                   "percent": round(100 * branch_observed / branch_total, 2) if branch_total else None},
            "files": files,
            "functions": functions,
            "limitations": [
                "Coverage is reconstructed from persisted line trace events; imported coverage.py data is kept separate and is not merged.",
                "Only AST statement-start lines are candidates; decorators, expression-only lines, generated code, and native code are partial or unavailable.",
                "A missing else or loop-exit outcome is not inferred as uncovered because this trace cannot observe every decision outcome safely.",
                "Source changed after its associated scan is marked and should be rescanned before comparison.",
            ],
        }
        result["evidence_record"] = record(
            "observed at runtime", "native line events joined to AST statement lines", run=trace_id,
            source={"scan_id": trace["scan_id"]}, limitations=tuple(result["limitations"]),
        )
        for item in files:
            attach(item, "native line events joined to AST statement lines", source={"path": item["path"]},
                   run=trace_id, evidence_class="observed at runtime", limitations=tuple(result["limitations"]))
        for item in functions:
            attach(item, "native line events filtered to AST function range", source={"path": item["path"]},
                   run=trace_id, evidence_class="observed at runtime", limitations=tuple(result["limitations"]))
        return result

    def api(self, project: str) -> dict[str, Any]:
        """Report supported static web API evidence without importing the project."""
        scan = self._scan(project)
        routes = self.store.rows(
            """SELECT e.*,s.qualified_name,s.signature,s.parameters_json,f.relative_path FROM entry_points e
               LEFT JOIN symbols s ON s.symbol_id=e.symbol_id LEFT JOIN files f ON f.file_id=s.file_id
               WHERE e.scan_id=? AND e.kind='route' ORDER BY e.name""",
            (scan["scan_id"],),
        )
        calls = self.store.rows(
            "SELECT caller_symbol_id,target_expression,line_number FROM calls WHERE scan_id=? ORDER BY line_number",
            (scan["scan_id"],),
        )
        calls_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for call in calls:
            if call["caller_symbol_id"]:
                calls_by_symbol[call["caller_symbol_id"]].append(call)
        imports = self.store.rows(
            """SELECT f.module_name,i.target_module FROM imports i JOIN files f ON f.file_id=i.source_file_id
               WHERE i.scan_id=?""",
            (scan["scan_id"],),
        )
        frameworks: dict[str, set[str]] = defaultdict(set)
        for item in imports:
            target = item["target_module"].lower()
            if target.startswith("fastapi"):
                frameworks[item["module_name"]].add("fastapi")
            elif target.startswith("flask"):
                frameworks[item["module_name"]].add("flask")
            elif target.startswith("django"):
                frameworks[item["module_name"]].add("django")
            elif target.startswith("aiohttp"):
                frameworks[item["module_name"]].add("aiohttp")
            elif target.startswith("starlette"):
                frameworks[item["module_name"]].add("starlette")
            elif target.startswith("quart"):
                frameworks[item["module_name"]].add("quart")
        endpoints: list[dict[str, Any]] = []
        for route in routes:
            details = json.loads(route["input_json"])
            evidence = json.loads(route["evidence_json"])
            signals = [str(details.get("decorator", "")), str(details.get("dependencies", "")),
                       str(route.get("signature") or "")]
            signals.extend(str(item) for item in details.get("handler_decorators", []))
            signals.extend(call["target_expression"] for call in calls_by_symbol.get(route.get("symbol_id"), []))
            checks = self._api_checks(signals)
            endpoint = {
                "method": details.get("method"), "route": details.get("route"), "name": route["name"],
                "handler": route.get("qualified_name"), "path": route.get("relative_path"),
                "line": evidence.get("line_start"), "frameworks": sorted(frameworks.get(route.get("module_name"), set())),
                "framework": details.get("framework"), "version": details.get("version"),
                "variant": details.get("variant"), "capability": details.get("capability"),
                "limits": details.get("limitations", []),
                "parameters": json.loads(route.get("parameters_json") or "[]"),
                "checks": checks, "evidence_class": _evidence_class(route["confidence"], evidence),
                "evidence": evidence,
            }
            attach(endpoint, "route decorator and handler syntax", source=_source(route), evidence_class=endpoint["evidence_class"],
                   limitations=("Dynamic route construction and indirect registration are not resolved.",))
            endpoints.append(endpoint)
        middleware = self._middleware(scan["scan_id"])
        result = {
            "scan_id": scan["scan_id"], "endpoints": endpoints, "middleware": middleware,
            "limitations": [
                "Framework recognition is syntax-based and does not import or execute the project.",
                "Authentication, authorisation, validation, rate limiting, and serialisation checks are recognised by direct decorator/call evidence and remain heuristic unless runtime-observed.",
                "WebSockets, dynamic route construction, indirect registration, and complex protocols are not resolved.",
            ],
        }
        result["evidence_record"] = record(
            "heuristically predicted", "framework route recognition from syntax", source={"scan_id": scan["scan_id"]},
            limitations=tuple(result["limitations"]),
        )
        return result

    @staticmethod
    def _api_checks(signals: list[str]) -> list[dict[str, Any]]:
        combined = "\n".join(signals).lower()
        patterns = {
            "authentication": ("login_required", "requires_auth", "authenticate", "oauth", "jwt", "current_user", "require_user"),
            "authorisation": ("permission_required", "permission", "authorize", "authorise", "require_user", "role_required"),
            "validation": ("validate", "pydantic", "schema", "body(", "query(", "form(", "serializer"),
            "rate_limiting": ("ratelimit", "rate_limit", ".limit", "throttle"),
            "serialisation": ("response_model", "jsonresponse", "jsonify", "serialize", "serializer"),
        }
        checks: list[dict[str, Any]] = []
        for kind, tokens in patterns.items():
            if any(token in combined for token in tokens):
                check = {"kind": kind, "evidence_class": "heuristically predicted",
                         "signals": [token for token in tokens if token in combined]}
                attach(check, "API control-call signal match", evidence_class="heuristically predicted",
                       limitations=("Signal names do not prove the control is enforced on every path.",))
                checks.append(check)
        return checks

    def _middleware(self, scan_id: str) -> list[dict[str, Any]]:
        symbols = self.store.rows(
            """SELECT s.qualified_name,s.decorators_json,s.line_start,f.relative_path FROM symbols s
               JOIN files f ON f.file_id=s.file_id WHERE s.scan_id=?""", (scan_id,)
        )
        calls = self.store.rows(
            """SELECT c.target_expression,c.line_number,f.relative_path FROM calls c
               JOIN files f ON f.file_id=c.call_file_id WHERE c.scan_id=?""", (scan_id,)
        )
        found: list[dict[str, Any]] = []
        for symbol in symbols:
            decorators = json.loads(symbol["decorators_json"])
            matching = [item for item in decorators if "middleware" in item.lower()]
            if matching:
                item = {"name": symbol["qualified_name"], "path": symbol["relative_path"], "line": symbol["line_start"],
                        "evidence": matching, "evidence_class": "statically resolved"}
                attach(item, "middleware decorator syntax", source=_source(item), evidence_class="statically resolved")
                found.append(item)
        for call in calls:
            if "middleware" in call["target_expression"].lower():
                item = {"name": call["target_expression"], "path": call["relative_path"], "line": call["line_number"],
                        "evidence": [call["target_expression"]], "evidence_class": "statically resolved"}
                attach(item, "middleware call syntax", source=_source(item), evidence_class="statically resolved")
                found.append(item)
        return found

    def modules(self, project: str) -> list[dict[str, Any]]:
        scan = self._scan(project)
        rows = self.store.rows(
            """SELECT relative_path,module_name,parse_status,error_message,is_entry_point,origin_kind,
                      original_path,resolved_path,parser,parser_version,capability,category,line_offset,origin_id
               FROM files WHERE scan_id=? ORDER BY module_name""", (scan["scan_id"],))
        outgoing = self.store.rows("""SELECT f.module_name,COUNT(DISTINCT i.resolved_module) AS count FROM imports i JOIN files f ON f.file_id=i.source_file_id WHERE i.scan_id=? AND i.resolved_module IS NOT NULL GROUP BY f.module_name""", (scan["scan_id"],))
        incoming = self.store.rows("SELECT resolved_module AS module_name,COUNT(DISTINCT source_file_id) AS count FROM imports WHERE scan_id=? AND resolved_module IS NOT NULL GROUP BY resolved_module", (scan["scan_id"],))
        out_map = {row["module_name"]: row["count"] for row in outgoing}
        in_map = {row["module_name"]: row["count"] for row in incoming}
        for row in rows:
            row["dependencies"] = out_map.get(row["module_name"], 0)
            row["dependants"] = in_map.get(row["module_name"], 0)
            classification = "unknown" if row.get("parse_status") != "parsed" else "statically resolved"
            attach(row, "source discovery and AST parse", source=_source(row), evidence_class=classification,
                   limitations=("Only accepted Python files, explicit external files, safe zip members, notebook cells, and generated text are represented.",))
        return rows

    def references(self, project: str, target: str) -> list[dict[str, Any]]:
        scan = self._scan(project)
        symbols = self.store.rows(
            "SELECT symbol_id FROM symbols WHERE scan_id=? AND (qualified_name=? OR name=? OR qualified_name LIKE ?)",
            (scan["scan_id"], target, target, f"%.{target}"),
        )
        definitions = self.store.rows(
            "SELECT definition_id FROM definitions WHERE scan_id=? AND (qualified_name=? OR name=? OR qualified_name LIKE ?)",
            (scan["scan_id"], target, target, f"%.{target}"),
        )
        symbol_ids = [row["symbol_id"] for row in symbols]
        definition_ids = [row["definition_id"] for row in definitions]
        if not symbol_ids and not definition_ids:
            raise ThreadError(f"No stored definition matches '{target}'.")
        symbol_slots = ",".join("?" for _ in symbol_ids) or "NULL"
        definition_slots = ",".join("?" for _ in definition_ids) or "NULL"
        rows = self.store.rows(
            f"""SELECT r.*,f.relative_path,rdef.qualified_name AS definition_name,d.qualified_name AS symbol_name
                FROM symbol_references r JOIN files f ON f.file_id=r.file_id
                LEFT JOIN definitions rdef ON rdef.definition_id=r.resolved_definition_id
                LEFT JOIN symbols d ON d.symbol_id=r.resolved_symbol_id
                WHERE r.scan_id=? AND (r.resolved_symbol_id IN ({symbol_slots})
                   OR r.resolved_definition_id IN ({definition_slots}))
                ORDER BY f.relative_path,r.line_number""",
            (scan["scan_id"], *symbol_ids, *definition_ids),
        )
        for row in rows:
            row["binding_path"] = json.loads(row.pop("binding_path_json"))
            attach(row, "lexical scope and import binding", source=_source(row),
                   evidence_class=("statically resolved" if row["resolution"] in {"definition", "import", "class", "inherited"}
                                   else "heuristically predicted"),
                   limitations=("Binding is static; reflection or runtime rebinding can change it.",))
        return rows

    def usage(self, project: str, target: str) -> dict[str, Any]:
        references = self.references(project, target)
        groups: Counter[tuple[str, str]] = Counter((row["name"], row["resolution"]) for row in references)
        return {
            "target": target,
            "references": len(references),
            "by_name_resolution": [
                {"name": name, "resolution": resolution, "count": count}
                for (name, resolution), count in sorted(groups.items())
            ],
            "references_detail": references,
        }

    def boundaries(self, project: str) -> list[dict[str, Any]]:
        scan = self._scan(project)
        rows = self.store.rows(
            """SELECT b.*,f.relative_path FROM boundaries b JOIN files f ON f.file_id=b.file_id
               WHERE b.scan_id=? ORDER BY f.relative_path,b.line_number""", (scan["scan_id"],),
        )
        for row in rows:
            row["details"] = json.loads(row.pop("details_json"))
            attach(row, "static dynamic-behaviour boundary", source=_source(row), evidence_class="unknown",
                   limitations=("The surrounding runtime behaviour is not executed or proven statically.",))
        return rows

    def dependencies(self, project: str, module: str, reverse: bool = False, depth: int = 1) -> dict[str, Any]:
        scan = self._scan(project)
        if not self.store.one("SELECT 1 FROM files WHERE scan_id=? AND module_name=?", (scan["scan_id"], module)):
            raise ThreadError(f"Module '{module}' is not in the latest scan.")
        imports = self.store.rows("SELECT f.module_name AS source,resolved_module AS target FROM imports i JOIN files f ON f.file_id=i.source_file_id WHERE i.scan_id=? AND resolved_module IS NOT NULL", (scan["scan_id"],))
        graph: dict[str, set[str]] = defaultdict(set)
        for row in imports:
            graph[row["target"] if reverse else row["source"]].add(row["source"] if reverse else row["target"])
        discovered: list[dict[str, Any]] = []
        queue: deque[tuple[str, int]] = deque([(module, 0)])
        seen = {module}
        while queue:
            source, level = queue.popleft()
            if level >= depth:
                continue
            for target in sorted(graph[source]):
                edge = {"from": source, "to": target, "depth": level + 1}
                attach(edge, "resolved AST import", evidence_class="statically resolved",
                       limitations=("Dynamic and conditional imports may not be resolved.",))
                discovered.append(edge)
                if target not in seen:
                    seen.add(target)
                    queue.append((target, level + 1))
        result = {"module": module, "direction": "dependants" if reverse else "dependencies", "depth": depth, "edges": discovered}
        result["evidence_record"] = record("statically resolved", "resolved AST import graph",
                                           limitations=("Conditional and dynamic imports may be missing.",))
        return result

    def cycles(self, project: str) -> list[dict[str, Any]]:
        scan = self._scan(project)
        rows = self.store.rows("SELECT * FROM findings WHERE scan_id=? AND rule_id='TB-CYCLE-001'", (scan["scan_id"],))
        for row in rows:
            row["evidence"] = json.loads(row.pop("evidence_json"))
            attach(row, "import-cycle analysis rule", source=_source(row),
                   evidence_class="heuristically predicted", limitations=("A cycle is a structural warning, not an execution proof.",))
        return rows

    def search(self, project: str, term: str, kind: str | None = None, module: str | None = None) -> list[dict[str, Any]]:
        scan = self._scan(project)
        clauses = ["s.scan_id=?", "(s.name LIKE ? OR s.qualified_name LIKE ? OR f.relative_path LIKE ?)"]
        values: list[Any] = [scan["scan_id"], f"%{term}%", f"%{term}%", f"%{term}%"]
        if kind:
            clauses.append("s.kind=?")
            values.append(kind)
        if module:
            clauses.append("f.module_name=?")
            values.append(module)
        rows = self.store.rows(f"SELECT s.qualified_name,s.name,s.kind,s.signature,s.purpose,s.line_start,s.line_end,f.relative_path,f.module_name FROM symbols s JOIN files f ON f.file_id=s.file_id WHERE {' AND '.join(clauses)} ORDER BY s.qualified_name", tuple(values))
        for row in rows:
            attach(row, "AST symbol definition", source=_source(row), evidence_class="statically resolved")
        return rows

    def symbol(self, project: str, reference: str) -> list[dict[str, Any]]:
        scan = self._scan(project)
        rows = self.store.rows("""SELECT s.*,f.relative_path,f.module_name FROM symbols s JOIN files f ON f.file_id=s.file_id WHERE s.scan_id=? AND (s.qualified_name=? OR s.name=?) ORDER BY s.qualified_name""", (scan["scan_id"], reference, reference))
        for row in rows:
            attach(row, "AST symbol definition", source=_source(row), evidence_class="statically resolved")
        return rows

    def functions(self, project: str, module: str | None = None) -> list[dict[str, Any]]:
        scan = self._scan(project)
        sql = "SELECT s.qualified_name,s.kind,s.signature,s.purpose,s.complexity,s.line_start,s.line_end,f.relative_path,f.module_name FROM symbols s JOIN files f ON f.file_id=s.file_id WHERE s.scan_id=? AND s.kind IN ('function','async_function','method','async_method')"
        values: list[Any] = [scan["scan_id"]]
        if module:
            sql += " AND f.module_name=?"
            values.append(module)
        rows = self.store.rows(sql + " ORDER BY s.qualified_name", tuple(values))
        for row in rows:
            attach(row, "AST function definition", source=_source(row), evidence_class="statically resolved")
        return rows

    def classes(self, project: str, module: str | None = None) -> list[dict[str, Any]]:
        scan = self._scan(project)
        sql = "SELECT s.symbol_id,s.qualified_name,s.signature,s.docstring,s.purpose,s.line_start,s.line_end,f.relative_path,f.module_name FROM symbols s JOIN files f ON f.file_id=s.file_id WHERE s.scan_id=? AND s.kind='class'"
        values: list[Any] = [scan["scan_id"]]
        if module:
            sql += " AND f.module_name=?"
            values.append(module)
        rows = self.store.rows(sql + " ORDER BY s.qualified_name", tuple(values))
        for row in rows:
            row["methods"] = self.store.rows("SELECT name,kind,line_start FROM symbols WHERE scan_id=? AND parent_qualified_name=? AND kind IN ('method','async_method') ORDER BY line_start", (scan["scan_id"], row["qualified_name"]))
            row["attributes"] = self.store.rows("SELECT name,kind,line_number,value_expression FROM attributes WHERE scan_id=? AND class_symbol_id=? ORDER BY line_number", (scan["scan_id"], row["symbol_id"]))
            row.pop("symbol_id")
            attach(row, "AST class definition", source=_source(row), evidence_class="statically resolved")
        return rows

    def inheritance(self, project: str, reference: str) -> list[dict[str, Any]]:
        rows = [row for row in self.classes(project) if row["qualified_name"] == reference or row["qualified_name"].endswith("." + reference)]
        if not rows:
            raise ThreadError(f"No class matches '{reference}'.")
        return rows

    def callers(self, project: str, reference: str) -> list[dict[str, Any]]:
        scan = self._scan(project)
        symbols = self.symbol(project, reference)
        ids = [row["symbol_id"] for row in symbols]
        if not ids:
            raise ThreadError(f"No symbol matches '{reference}'.")
        placeholders = ",".join("?" for _ in ids)
        rows = self.store.rows(f"""SELECT c.target_expression,c.resolution,c.basis,c.candidate_json,c.line_number,f.relative_path,s.qualified_name AS caller FROM calls c JOIN files f ON f.file_id=c.call_file_id LEFT JOIN symbols s ON s.symbol_id=c.caller_symbol_id WHERE c.scan_id=? AND c.callee_symbol_id IN ({placeholders}) ORDER BY f.relative_path,c.line_number""", (scan["scan_id"], *ids))
        for row in rows:
            classification = "statically resolved" if row.get("resolution") == "resolved" else (
                "heuristically predicted" if json.loads(row.get("candidate_json") or "[]") else "unknown")
            attach(row, f"call binding: {row.get('basis') or 'stored resolution'}", source=_source(row),
                   evidence_class=classification, limitations=("Dynamic dispatch is not resolved.",))
        return rows

    def callees(self, project: str, reference: str, depth: int = 1) -> dict[str, Any]:
        scan = self._scan(project)
        symbols = self.symbol(project, reference)
        if len(symbols) != 1:
            if not symbols:
                raise ThreadError(f"No symbol matches '{reference}'.")
            raise ThreadError(f"Symbol '{reference}' is ambiguous; use a qualified name.")
        start = symbols[0]["symbol_id"]
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        seen = {start}
        edges: list[dict[str, Any]] = []
        while queue:
            current, level = queue.popleft()
            if level >= depth:
                continue
            rows = self.store.rows("""SELECT c.target_expression,c.resolution,c.basis,c.candidate_json,c.line_number,f.relative_path,callee.qualified_name AS callee FROM calls c JOIN files f ON f.file_id=c.call_file_id LEFT JOIN symbols callee ON callee.symbol_id=c.callee_symbol_id WHERE c.scan_id=? AND c.caller_symbol_id=? ORDER BY c.line_number""", (scan["scan_id"], current))
            for row in rows:
                row["depth"] = level + 1
                attach(row, f"call binding: {row.get('basis') or 'stored resolution'}", source=_source(row),
                       evidence_class="statically resolved" if row.get("resolution") == "resolved" else (
                           "heuristically predicted" if json.loads(row.get("candidate_json") or "[]") else "unknown"),
                       limitations=("Dynamic dispatch is not resolved.",))
                edges.append(row)
                candidates = json.loads(row["candidate_json"])
                if row["resolution"] == "resolved" and candidates and candidates[0] not in seen:
                    seen.add(candidates[0])
                    queue.append((candidates[0], level + 1))
        return {"symbol": symbols[0]["qualified_name"], "depth": depth, "calls": edges}

    def flow(self, project: str, reference: str) -> list[dict[str, Any]]:
        scan = self._scan(project)
        symbols = self.symbol(project, reference)
        if len(symbols) != 1:
            raise ThreadError("Flow analysis requires one exact qualified function or method name.")
        rows = self.store.rows("SELECT variable_name,event_kind,expression,line_number FROM flows WHERE scan_id=? AND symbol_id=? ORDER BY line_number,flow_id", (scan["scan_id"], symbols[0]["symbol_id"]))
        for row in rows:
            attach(row, "AST local value flow", source=_source(row), evidence_class="statically resolved")
        return rows

    def lesson(self, project: str, reference: str) -> dict[str, Any]:
        """Build a deterministic, source-backed teaching card for one symbol.

        This deliberately uses small syntax rules rather than an LLM: a pattern
        is shown only when its supporting syntax occurs inside the selected
        definition.  It is a learning hint, not a claim about all execution
        paths or asymptotic complexity.
        """
        symbols = self.symbol(project, reference)
        if len(symbols) != 1:
            if not symbols:
                raise ThreadError(f"No symbol matches '{reference}'.")
            raise ThreadError("Lesson analysis requires one exact qualified function, method, or class name.")
        symbol = symbols[0]
        if symbol["kind"] not in {"function", "async_function", "method", "async_method", "class"}:
            raise ThreadError("Lessons are available for functions, methods, and classes.")
        root = Path(self.engine.get_project(project)["root_path"])
        path = root / symbol["relative_path"]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise ThreadError(f"ThreadTrace could not read source for this lesson: {exc}") from exc
        node = next((candidate for candidate in ast.walk(tree)
                     if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and candidate.lineno == symbol["line_start"]), None)
        if node is None:
            raise ThreadError("ThreadTrace could not match this stored symbol to its current source. Run a new scan.")

        def call_name(call: ast.Call) -> str:
            if isinstance(call.func, ast.Name):
                return call.func.id
            if isinstance(call.func, ast.Attribute):
                return call.func.attr
            return "<dynamic call>"

        patterns: list[dict[str, Any]] = []
        nodes = list(ast.walk(node))
        calls = [candidate for candidate in nodes if isinstance(candidate, ast.Call)]
        has_loop = any(isinstance(candidate, (ast.For, ast.AsyncFor, ast.While)) for candidate in nodes)
        call_names = {call_name(call) for call in calls}
        if any(isinstance(candidate, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)) for candidate in nodes):
            patterns.append({"name": "Collection transformation", "confidence": "confirmed", "reason": "Uses a list, set, dictionary, or generator comprehension."})
        if has_loop and any(isinstance(candidate, ast.Call) and isinstance(candidate.func, ast.Attribute) and candidate.func.attr in {"append", "extend", "add", "update"} for candidate in nodes):
            patterns.append({"name": "Collection-building loop", "confidence": "confirmed", "reason": "A loop adds values to a collection."})
        if call_names & {"sorted", "sort"}:
            patterns.append({"name": "Sorting", "confidence": "confirmed", "reason": "Calls sorted() or .sort() within this definition."})
        if call_names & {"sum", "min", "max", "any", "all"}:
            patterns.append({"name": "Aggregation or reduction", "confidence": "confirmed", "reason": "Combines a collection through a standard reduction function."})
        if any(isinstance(candidate, ast.Compare) and any(isinstance(operator, (ast.In, ast.NotIn)) for operator in candidate.ops) for candidate in nodes):
            patterns.append({"name": "Lookup or membership check", "confidence": "confirmed", "reason": "Checks whether a value is present in another collection."})
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(name == node.name for name in call_names):
            patterns.append({"name": "Recursion", "confidence": "confirmed", "reason": "The function directly calls itself."})
        if any(isinstance(candidate, ast.Try) for candidate in nodes):
            patterns.append({"name": "Error handling", "confidence": "confirmed", "reason": "Uses try/except or try/finally control flow."})
        if isinstance(node, ast.ClassDef):
            methods = [candidate.name for candidate in node.body if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if methods:
                patterns.append({"name": "Object-oriented interface", "confidence": "confirmed", "reason": f"Defines {len(methods)} method(s) on a class."})
        flow = self.flow(project, symbol["qualified_name"]) if symbol["kind"] != "class" else []
        outputs = [item for item in flow if item["event_kind"] in {"return", "yield"}]
        callees = self.callees(project, symbol["qualified_name"])["calls"] if symbol["kind"] != "class" else []
        callers = self.callers(project, symbol["qualified_name"])
        for pattern in patterns:
            attach(pattern, "local AST pattern match", source=_source(symbol), limitations=("This is a local source hint, not a runtime proof.",))
        result = {
            "kind": symbol["kind"],
            "qualified_name": symbol["qualified_name"],
            "purpose": symbol.get("purpose") or "ThreadTrace could not infer a concise purpose; inspect the source evidence below.",
            "signature": symbol.get("signature"),
            "complexity": symbol.get("complexity"),
            "source": {"path": symbol["relative_path"], "line_start": symbol["line_start"], "line_end": symbol["line_end"]},
            "inputs": [item for item in flow if item["event_kind"] == "parameter"],
            "outputs": outputs,
            "calls": [{"name": item.get("callee") or item["target_expression"], "line": item["line_number"], "resolution": item["resolution"]} for item in callees],
            "used_by": [{"name": item.get("caller") or "<module>", "path": item["relative_path"], "line": item["line_number"]} for item in callers],
            "patterns": patterns,
            "note": "Patterns are local source observations, not a proof of every runtime path or performance bound.",
        }
        result["evidence_record"] = record("statically resolved", "stored symbol facts and bounded AST analysis",
                                           source=_source(symbol), limitations=(result["note"],))
        return result

    def _symbol_node(self, project: str, symbol: dict[str, Any]) -> ast.AST:
        root = Path(self.engine.get_project(project)["root_path"])
        path = root / symbol["relative_path"]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise ThreadError(f"ThreadTrace could not read source for this definition: {exc}") from exc
        node = node_at(tree, symbol["line_start"])
        if node is None:
            raise ThreadError("ThreadTrace could not match this stored symbol to current source. Run a new scan.")
        return node

    @staticmethod
    def _cost_rank(rank: float) -> str:
        return {0: "O(1)", 1: "O(n)", 1.5: "O(n log n)", 2: "O(n^2)", 3: "O(n^3)"}.get(rank, "unknown")

    def complexity(self, project: str, reference: str) -> dict[str, Any]:
        """Explain a deterministic time/space estimate separately from cyclomatic complexity."""
        symbols = self.symbol(project, reference)
        if len(symbols) != 1:
            raise ThreadError("Complexity analysis requires one exact qualified function or method name.")
        symbol = symbols[0]
        if symbol["kind"] not in {"function", "async_function", "method", "async_method"}:
            raise ThreadError("Complexity analysis is available for functions and methods.")
        cache: dict[str, dict[str, Any]] = {}

        def analyse(current: dict[str, Any], stack: set[str]) -> dict[str, Any]:
            if current["symbol_id"] in cache:
                return cache[current["symbol_id"]]
            if current["symbol_id"] in stack or len(stack) >= 4:
                return {"rank": float("inf"), "space": float("inf"), "assumptions": ["Recursive or over-deep called-function cost was not expanded."],
                        "called_functions": []}
            stack = {*stack, current["symbol_id"]}
            node = self._symbol_node(project, current)
            metrics = local_metrics(node)
            assumptions = [
                "n represents the larger of the input and iterated collection sizes.",
                "Only local syntax and resolved project calls are composed; external calls are treated as O(1).",
            ]
            unknown = metrics["recursion"] or any(item.get("bound") == "unknown" for item in metrics["operations"])
            rank = float(metrics["loop_depth"] if metrics["loop_depth"] <= MAX_REPORTED_LOOP_DEPTH else float("inf"))
            for operation in metrics["operations"]:
                if operation["kind"] == "sort":
                    rank = max(rank, operation.get("depth", 0) + 1.5)
                elif operation["kind"] in {"iterate", "membership"}:
                    rank = max(rank, operation.get("depth", 0) + 1)
            space = max(
                1 if metrics["comprehension"] else 0,
                metrics["collection_mutation_depth"],
                1 if any(item["kind"] == "sort" for item in metrics["operations"]) else 0,
            )
            calls = self.store.rows(
                """SELECT target_expression,callee_symbol_id FROM calls
                   WHERE scan_id=? AND caller_symbol_id=? AND resolution='resolved' AND callee_symbol_id IS NOT NULL""",
                (self._scan(project)["scan_id"], current["symbol_id"]),
            )
            by_expression = {row["target_expression"]: row["callee_symbol_id"] for row in calls}
            called: list[dict[str, Any]] = []
            for call in metrics["calls"]:
                callee_id = by_expression.get(call["expression"])
                if not callee_id:
                    continue
                callee = self.store.one(
                    "SELECT s.*,f.relative_path,f.module_name FROM symbols s JOIN files f ON f.file_id=s.file_id WHERE s.symbol_id=?",
                    (callee_id,),
                )
                if not callee:
                    continue
                nested = analyse(callee, stack)
                called.append({"name": callee["qualified_name"], "line": call["line"], "bound": nested.get("bound", self._cost_rank(nested["rank"]))})
                if nested["rank"] == float("inf"):
                    unknown = True
                else:
                    rank = max(rank, nested["rank"] + call["loop_depth"])
                    space = max(space, nested["space"])
            if unknown:
                rank = float("inf")
                space = float("inf")
                assumptions.append("Recursion, unresolved membership, or an unsupported construct left the bound unknown.")
            result = {
                "rank": rank, "space": space, "bound": self._cost_rank(rank),
                "space_bound": self._cost_rank(space), "assumptions": assumptions,
                "called_functions": called, "operations": metrics["operations"],
                "loop_depth": metrics["loop_depth"],
            }
            cache[current["symbol_id"]] = result
            return result

        result = analyse(symbol, set())
        payload = {
            "symbol": symbol["qualified_name"], "cyclomatic": symbol.get("complexity"),
            "time": {"bound": result["bound"], "assumptions": result["assumptions"], "confidence": "heuristic"},
            "space": {"bound": result["space_bound"], "assumptions": result["assumptions"], "confidence": "heuristic"},
            "loop_depth": result["loop_depth"], "operations": result["operations"],
            "called_functions": result["called_functions"], "recursion": any(
                item.get("name", "").endswith("." + symbol["name"]) for item in result["called_functions"]
            ),
            "note": "Cyclomatic complexity is a control-flow count, not a time bound. Estimates are structural and may be unknown.",
        }
        payload["evidence_record"] = record("heuristically predicted", "bounded AST cost composition",
                                            source=_source(symbol), limitations=(payload["note"], *result["assumptions"]))
        return payload

    def advanced(self, project: str, reference: str) -> dict[str, Any]:
        """Report advanced Python constructs with source-backed boundaries."""
        symbols = self.symbol(project, reference)
        if len(symbols) != 1:
            raise ThreadError("Advanced structure analysis requires one exact qualified symbol name.")
        symbol = symbols[0]
        node = self._symbol_node(project, symbol)
        facts = advanced_facts(node)
        facts["decorators"] = [
            {"expression": expression, "target": symbol["qualified_name"], "line": node.lineno}
            for expression in facts["decorators"]
        ]
        imports = self.store.rows(
            "SELECT target_module,line_number FROM imports WHERE scan_id=? AND source_file_id=(SELECT file_id FROM symbols WHERE symbol_id=?)",
            (self._scan(project)["scan_id"], symbol["symbol_id"]),
        )
        native_imports = [
            {"expression": item["target_module"], "line": item["line_number"], "kind": "import"}
            for item in imports if item["target_module"].split(".")[0] in {"ctypes", "cffi"}
        ]
        facts["native_boundaries"].extend(native_imports)
        for item in facts["algorithms"]:
            item["confidence"] = "heuristic"
        for group in ("decorators", "closures", "descriptors", "context_managers", "concurrency", "native_boundaries", "algorithms"):
            for item in facts[group]:
                if isinstance(item, dict):
                    attach(item, "local AST advanced-structure detection", source=_source(symbol),
                           evidence_class="heuristically predicted" if group == "algorithms" else "statically resolved",
                           limitations=("Structural syntax evidence only; runtime behaviour is not proven.",))
        result = {"symbol": symbol["qualified_name"], "kind": symbol["kind"], **facts,
                  "note": "Advanced facts are syntax observations; dynamic dispatch, C bodies, and runtime concurrency remain boundaries."}
        if symbol["kind"] in {"function", "async_function", "method", "async_method"}:
            result["complexity"] = self.complexity(project, symbol["qualified_name"])
        result["evidence_record"] = record("statically resolved", "AST advanced-structure facts",
                                           source=_source(symbol), limitations=(result["note"],))
        return result

    def metadata(self, project: str) -> dict[str, Any]:
        scan = self._scan(project)
        files = self.store.rows("SELECT * FROM metadata_files WHERE scan_id=? ORDER BY relative_path", (scan["scan_id"],))
        dependencies = self.store.rows("SELECT * FROM dependencies WHERE scan_id=? ORDER BY name,source_file", (scan["scan_id"],))
        for row in files:
            row["limitations"] = json.loads(row.pop("limitations_json"))
            row.pop("details_json", None)
        for row in dependencies:
            row["extras"] = json.loads(row.pop("extras_json"))
            row["limitations"] = json.loads(row.pop("limitations_json"))
            row["editable"] = bool(row["editable"])
        return {"scan_id": scan["scan_id"], "files": files, "dependencies": dependencies}

    def libraries(self, project: str) -> list[dict[str, Any]]:
        scan = self._scan(project)
        rows = self.store.rows("SELECT name,category,declared_in FROM libraries WHERE scan_id=? ORDER BY category,name", (scan["scan_id"],))
        imports = self.store.rows("""SELECT i.target_module,i.imported_name,i.alias,i.category,i.line_number,f.relative_path,f.module_name
            FROM imports i JOIN files f ON f.file_id=i.source_file_id
            WHERE i.scan_id=? AND i.category!='internal' ORDER BY i.target_module,f.relative_path,i.line_number""", (scan["scan_id"],))
        usage: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in imports:
            root = row["target_module"].split(".")[0]
            usage[_library_key(root)].append({
                "module": row["module_name"], "path": row["relative_path"], "line": row["line_number"],
                "imported": row["imported_name"] or row["target_module"], "alias": row["alias"],
            })
        for row in rows:
            sites = usage.get(_library_key(row["name"]), [])
            row["usages"] = len(sites)
            row["usage_sites"] = sites
            row["usage_files"] = sorted({site["path"] for site in sites})
            row["description"] = _library_description(row["name"])
            attach(row, "AST import and declared-dependency evidence", source={"usage_sites": sites},
                   evidence_class="statically resolved", limitations=("Import sites do not prove runtime loading.",))
        return rows

    def diagnostics(self, project: str, severity: str | None = None, rule: str | None = None, include_unresolved_calls: bool = False) -> list[dict[str, Any]]:
        scan = self._scan(project)
        clauses = ["scan_id=?"]
        values: list[Any] = [scan["scan_id"]]
        if severity:
            clauses.append("severity=?")
            values.append(severity)
        if rule:
            clauses.append("rule_id=?")
            values.append(rule)
        elif not include_unresolved_calls:
            clauses.append("rule_id != 'TB-CALL-001'")
        rows = self.store.rows(f"SELECT * FROM findings WHERE {' AND '.join(clauses)} ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, rule_id", tuple(values))
        for row in rows:
            row["evidence"] = json.loads(row.pop("evidence_json"))
            classification = "unknown" if row["rule_id"] == "TB-ANALYSIS-001" else "heuristically predicted"
            attach(row, f"static analysis rule {row['rule_id']}", source=_source(row),
                   evidence_class=classification, limitations=("A finding is a review prompt, not proof of runtime behaviour.",))
        return rows
