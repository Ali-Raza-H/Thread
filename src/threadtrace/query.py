"""Read-only access to the most recently completed analysis."""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC, datetime
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .engine import Engine, ThreadError
from .parser import discover_python_files


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
        "confirmed": "Thread can see this relationship directly in source.",
        "high": "Several source facts strongly support this relationship.",
        "possible": "This may happen, but Python's dynamic behaviour prevents a complete proof.",
        "unknown": "Thread cannot safely determine what happens next.",
    }.get(confidence, "Thread could not determine confidence for this relationship.")


def _walk_explanation(relation: dict[str, Any], target: dict[str, Any] | None) -> str:
    name = relation.get("target_name") or "the next step"
    if relation["kind"] == "direct_call":
        if target and target.get("purpose"):
            return f"Calls {name}. {target['purpose']}"
        return f"Calls {name} directly after the previous step."
    if relation["kind"] == "candidate_call":
        return f"May call {name}, but Thread cannot resolve the target uniquely."
    if relation["kind"] == "import":
        return f"Loads code from {name} so this step can use it."
    return f"Moves to {name} through a {relation['kind'].replace('_', ' ')} relationship."


def _friendly_finding(row: dict[str, Any]) -> tuple[str, str]:
    mapping = {
        "TB-ANALYSIS-001": ("Thread could not read this file", "Some project behaviour may be missing because this file could not be analysed."),
        "TB-CYCLE-001": ("Circular import", "These modules import one another, which can make startup order harder to reason about."),
        "TB-DYNAMIC-001": ("Dynamic Python boundary", "Thread cannot follow this dynamic operation, so paths past this line may be incomplete."),
        "TB-IMPORT-001": ("Unknown import", "Thread cannot identify this dependency as project code, a declared package, or the standard library."),
        "TB-DEPS-001": ("Highly connected module", "This module imports many internal modules, so changes may have broad effects."),
        "TB-DEPENDANTS-001": ("Widely used module", "Many internal modules depend on this code, so changes can have broad effects."),
        "TB-UNUSED-001": ("Possible external entry point", "No internal module imports this file; it may be started by a command, plugin, framework, or another external mechanism."),
        "TB-CALL-001": ("Unresolved call", "Thread cannot identify the call target from static source alone."),
    }
    return mapping.get(row["rule_id"], ("Analysis finding", row["message"]))


class QueryService:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.store = engine.store

    def _scan(self, project: str) -> dict[str, Any]:
        return self.engine.latest_scan(project)

    def stale(self, project: str) -> bool:
        """Conservative check: additions/removals and changed tracked file contents."""
        scan = self._scan(project)
        project_row = self.engine.get_project(project)
        stored = {row["relative_path"]: row["content_hash"] for row in self.store.rows("SELECT relative_path,content_hash FROM files WHERE scan_id=?", (scan["scan_id"],))}
        root = Path(project_row["root_path"])
        current: dict[str, str | None] = {}
        paths, warnings = discover_python_files(root)
        if warnings:
            return True
        for path in paths:
            try:
                rel = path.relative_to(root).as_posix()
                import hashlib
                current[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                return True
        return current != stored

    @staticmethod
    def _decoded(rows: list[dict[str, Any]], *fields: str) -> list[dict[str, Any]]:
        for row in rows:
            for field in fields:
                if field in row:
                    try:
                        row[field.removesuffix("_json")] = json.loads(row.pop(field))
                    except json.JSONDecodeError:
                        row[field.removesuffix("_json")] = {}
        return rows

    def overview(self, project: str) -> dict[str, Any]:
        scan = self._scan(project)
        row = self.store.one("SELECT content_json,generated_at FROM reports WHERE scan_id=? AND report_kind='overview'", (scan["scan_id"],))
        if not row:
            raise ThreadError("This scan predates reconstruction support. Run 'threadtrace scan' again.")
        result = json.loads(row["content_json"])
        result.update({"scan_id": scan["scan_id"], "generated_at": row["generated_at"]})
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
        return self._decoded(self.store.rows(sql + " ORDER BY e.kind,e.name", tuple(values)), "input_json", "evidence_json")

    def architecture(self, project: str) -> dict[str, Any]:
        scan = self._scan(project)
        components = self._decoded(self.store.rows("SELECT * FROM components WHERE scan_id=? ORDER BY name", (scan["scan_id"],)), "evidence_json")
        members = self.store.rows("SELECT * FROM component_members WHERE component_id IN (SELECT component_id FROM components WHERE scan_id=?)", (scan["scan_id"],))
        by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for member in members:
            by_component[member["component_id"]].append(member)
        for component in components:
            component["members"] = by_component[component["component_id"]]
        edges = self._decoded(self.store.rows("""SELECT * FROM relationships WHERE scan_id=?
            AND source_module IS NOT NULL AND target_module IS NOT NULL
            ORDER BY source_module,target_module,kind""", (scan["scan_id"],)), "evidence_json")
        return {"components": components, "relationships": edges}

    def resources(self, project: str, kind: str | None = None) -> list[dict[str, Any]]:
        scan = self._scan(project)
        sql = "SELECT r.*,s.qualified_name FROM resources r LEFT JOIN symbols s ON s.symbol_id=r.symbol_id WHERE r.scan_id=?"
        values: list[Any] = [scan["scan_id"]]
        if kind:
            sql += " AND r.kind=?"
            values.append(kind)
        return self._decoded(self.store.rows(sql + " ORDER BY r.kind,r.module_name,r.name", tuple(values)), "evidence_json")

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
                steps.append(relation)
                target = relation["target_symbol_id"]
                if target not in seen:
                    seen.add(target)
                    queue.append((target, level + 1))
        resources = self.store.rows("SELECT kind,name,operation,confidence,module_name FROM resources WHERE scan_id=? AND symbol_id IN (%s)" % ",".join("?" * len(seen)), (scan["scan_id"], *seen))
        return {"entry_point": point["name"], "handler": point["symbol_id"], "depth": depth, "steps": steps, "resources": resources}

    @staticmethod
    def _confidence_values(minimum: str) -> set[str]:
        levels = ["confirmed", "high", "possible", "unknown"]
        if minimum not in levels:
            raise ThreadError(f"Unknown confidence '{minimum}'.")
        return set(levels[: levels.index(minimum) + 1])

    def impact(self, project: str, target: str, depth: int = 3) -> dict[str, Any]:
        scan = self._scan(project)
        matches = self.symbol(project, target)
        if len(matches) != 1:
            if not matches:
                raise ThreadError(f"No symbol matches '{target}'.")
            raise ThreadError("Impact analysis requires one exact qualified symbol name.")
        start = matches[0]["symbol_id"]
        rows = self.store.rows("""SELECT r.*,source.qualified_name AS source_name,target.qualified_name AS target_name
            FROM relationships r LEFT JOIN symbols source ON source.symbol_id=r.source_symbol_id
            LEFT JOIN symbols target ON target.symbol_id=r.target_symbol_id WHERE r.scan_id=? AND r.target_symbol_id IS NOT NULL""", (scan["scan_id"],))
        graph: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["source_symbol_id"]:
                graph[row["source_symbol_id"]].append(row)
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        seen = {start}
        edges: list[dict[str, Any]] = []
        while queue:
            current, level = queue.popleft()
            if level >= depth:
                continue
            for relation in graph[current]:
                relation["depth"] = level + 1
                relation["evidence"] = json.loads(relation.pop("evidence_json"))
                edges.append(relation)
                if relation["target_symbol_id"] not in seen:
                    seen.add(relation["target_symbol_id"])
                    queue.append((relation["target_symbol_id"], level + 1))
        return {"symbol": matches[0]["qualified_name"], "depth": depth, "downstream": edges}

    def dataflow(self, project: str, reference: str, resource_kind: str | None = None) -> dict[str, Any]:
        scan = self._scan(project)
        symbols = self.symbol(project, reference)
        if len(symbols) != 1:
            raise ThreadError("Data-flow analysis requires one exact qualified symbol name.")
        symbol_id = symbols[0]["symbol_id"]
        edges = self._decoded(self.store.rows("""SELECT d.*,source.qualified_name AS source_symbol,target.qualified_name AS target_symbol
            FROM data_edges d LEFT JOIN symbols source ON source.symbol_id=d.source_symbol_id
            LEFT JOIN symbols target ON target.symbol_id=d.target_symbol_id
            WHERE d.scan_id=? AND (d.source_symbol_id=? OR d.target_symbol_id=?) ORDER BY d.data_edge_id""", (scan["scan_id"], symbol_id, symbol_id)), "evidence_json")
        resources = self.resources(project, resource_kind)
        return {"symbol": symbols[0]["qualified_name"], "edges": edges,
                "direct_resources": [item for item in resources if item.get("symbol_id") == symbol_id]}

    def explain(self, project: str, target: str) -> dict[str, Any]:
        overview = self.overview(project)
        symbols = self.symbol(project, target)
        entries = [item for item in self.entrypoints(project) if item["name"] == target or item.get("qualified_name") == target]
        libraries = [item for item in self.libraries(project) if item["name"] == target]
        if entries:
            return {"kind": "entry_point", "target": target, "entry_points": entries, "overview_purpose": overview["purpose"]}
        if libraries:
            return {"kind": "library", "target": target, "libraries": libraries, "overview_purpose": overview["purpose"]}
        if symbols:
            symbol = symbols[0]
            return {"kind": "symbol", "target": symbol["qualified_name"], "purpose": symbol["purpose"],
                    "signature": symbol["signature"], "incoming": self.callers(project, symbol["qualified_name"]),
                    "outgoing": self.callees(project, symbol["qualified_name"])["calls"]}
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
                   "notice": "AI-assisted interpretation. Verify every claim against the included source evidence."}
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
        return {
            "purpose": overview["purpose"],
            "starting_points": starting_points,
            "main_parts": components,
            "needs_attention": self.findings(project),
            "next_step": starting_points[0]["name"] if starting_points else None,
        }

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
                links.append({
                    "from": relation["source_name"] or relation["source_module"],
                    "to": relation["target_name"] or relation["target_module"],
                    "kind": relation["kind"], "confidence": relation["confidence"],
                    "source_paths": source_paths, "target_paths": target_paths,
                    "evidence": json.loads(relation["evidence_json"]), "depth": level + 1,
                })
                next_key = (relation["target_symbol_id"], relation["target_module"])
                if next_key not in seen and (next_key[0] or next_key[1]):
                    seen.add(next_key)
                    queue.append((next_key[0], next_key[1], level + 1))
        files = [{"relative_path": path, "reasons": sorted(set(reasons))} for path, reasons in sorted(file_reasons.items())]
        return {"target": target, "depth": depth, "files": files, "links": links}

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
        return {"kind": "file", "nodes": nodes, "edges": list(grouped.values())}

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
        nodes = [dict(row, id=row["symbol_id"]) for row in symbols]
        return {"kind": "code", "nodes": nodes, "edges": edges}

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
        return {"before": before, "after": after, "changes": {
            key: {"added": sorted(values[after][key] - values[before][key]), "removed": sorted(values[before][key] - values[after][key])}
            for key in values[before]
        }}

    def traces(self, project: str) -> list[dict[str, Any]]:
        project_row = self.engine.get_project(project)
        rows = self.store.rows("SELECT * FROM runtime_traces WHERE project_id=? ORDER BY finished_at DESC", (project_row["project_id"],))
        for row in rows:
            row["command"] = json.loads(row.pop("command_json"))
        return rows

    def modules(self, project: str) -> list[dict[str, Any]]:
        scan = self._scan(project)
        rows = self.store.rows("SELECT relative_path,module_name,parse_status,error_message,is_entry_point FROM files WHERE scan_id=? ORDER BY module_name", (scan["scan_id"],))
        outgoing = self.store.rows("""SELECT f.module_name,COUNT(DISTINCT i.resolved_module) AS count FROM imports i JOIN files f ON f.file_id=i.source_file_id WHERE i.scan_id=? AND i.resolved_module IS NOT NULL GROUP BY f.module_name""", (scan["scan_id"],))
        incoming = self.store.rows("SELECT resolved_module AS module_name,COUNT(DISTINCT source_file_id) AS count FROM imports WHERE scan_id=? AND resolved_module IS NOT NULL GROUP BY resolved_module", (scan["scan_id"],))
        out_map = {row["module_name"]: row["count"] for row in outgoing}
        in_map = {row["module_name"]: row["count"] for row in incoming}
        for row in rows:
            row["dependencies"] = out_map.get(row["module_name"], 0)
            row["dependants"] = in_map.get(row["module_name"], 0)
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
                discovered.append({"from": source, "to": target, "depth": level + 1})
                if target not in seen:
                    seen.add(target)
                    queue.append((target, level + 1))
        return {"module": module, "direction": "dependants" if reverse else "dependencies", "depth": depth, "edges": discovered}

    def cycles(self, project: str) -> list[dict[str, Any]]:
        scan = self._scan(project)
        rows = self.store.rows("SELECT * FROM findings WHERE scan_id=? AND rule_id='TB-CYCLE-001'", (scan["scan_id"],))
        for row in rows:
            row["evidence"] = json.loads(row.pop("evidence_json"))
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
        return self.store.rows(f"SELECT s.qualified_name,s.name,s.kind,s.signature,s.purpose,s.line_start,s.line_end,f.relative_path,f.module_name FROM symbols s JOIN files f ON f.file_id=s.file_id WHERE {' AND '.join(clauses)} ORDER BY s.qualified_name", tuple(values))

    def symbol(self, project: str, reference: str) -> list[dict[str, Any]]:
        scan = self._scan(project)
        return self.store.rows("""SELECT s.*,f.relative_path,f.module_name FROM symbols s JOIN files f ON f.file_id=s.file_id WHERE s.scan_id=? AND (s.qualified_name=? OR s.name=?) ORDER BY s.qualified_name""", (scan["scan_id"], reference, reference))

    def functions(self, project: str, module: str | None = None) -> list[dict[str, Any]]:
        scan = self._scan(project)
        sql = "SELECT s.qualified_name,s.kind,s.signature,s.purpose,s.complexity,s.line_start,s.line_end,f.relative_path,f.module_name FROM symbols s JOIN files f ON f.file_id=s.file_id WHERE s.scan_id=? AND s.kind IN ('function','async_function','method','async_method')"
        values: list[Any] = [scan["scan_id"]]
        if module:
            sql += " AND f.module_name=?"
            values.append(module)
        return self.store.rows(sql + " ORDER BY s.qualified_name", tuple(values))

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
        return self.store.rows(f"""SELECT c.target_expression,c.resolution,c.candidate_json,c.line_number,f.relative_path,s.qualified_name AS caller FROM calls c JOIN files f ON f.file_id=c.call_file_id LEFT JOIN symbols s ON s.symbol_id=c.caller_symbol_id WHERE c.scan_id=? AND c.callee_symbol_id IN ({placeholders}) ORDER BY f.relative_path,c.line_number""", (scan["scan_id"], *ids))

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
            rows = self.store.rows("""SELECT c.target_expression,c.resolution,c.candidate_json,c.line_number,f.relative_path,callee.qualified_name AS callee FROM calls c JOIN files f ON f.file_id=c.call_file_id LEFT JOIN symbols callee ON callee.symbol_id=c.callee_symbol_id WHERE c.scan_id=? AND c.caller_symbol_id=? ORDER BY c.line_number""", (scan["scan_id"], current))
            for row in rows:
                row["depth"] = level + 1
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
        return self.store.rows("SELECT variable_name,event_kind,expression,line_number FROM flows WHERE scan_id=? AND symbol_id=? ORDER BY line_number,flow_id", (scan["scan_id"], symbols[0]["symbol_id"]))

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
            raise ThreadError(f"Thread could not read source for this lesson: {exc}") from exc
        node = next((candidate for candidate in ast.walk(tree)
                     if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and candidate.lineno == symbol["line_start"]), None)
        if node is None:
            raise ThreadError("Thread could not match this stored symbol to its current source. Run a new scan.")

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
        return {
            "kind": symbol["kind"],
            "qualified_name": symbol["qualified_name"],
            "purpose": symbol.get("purpose") or "Thread could not infer a concise purpose; inspect the source evidence below.",
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
        return rows
