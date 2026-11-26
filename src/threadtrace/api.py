"""Experimental read-only Python interface for projects, scans, queries, traces, and exports.

Execution stays in the CLI or in :class:`threadtrace.hybrid.HybridHarness`; this
facade neither scans, traces, nor runs experiments.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load as load_config
from .engine import Engine, ThreadError
from .exports import report_bundle
from .query import QueryService
from .store import Store

READ_ONLY_QUERIES = (
    "overview", "entrypoints", "architecture", "resources", "path", "impact", "dataflow", "slice",
    "control_flow", "explain", "file_map", "file_graph", "code_graph", "findings", "compare",
    "traces", "trace_events", "trace_concurrency", "preflight", "trace_resources", "trace_failure",
    "trace_profile", "coverage", "api", "modules", "references", "usage", "boundaries", "dependencies",
    "cycles", "search", "symbol", "functions", "classes", "inheritance", "callers", "callees", "flow",
    "lesson", "complexity", "advanced", "metadata", "libraries", "diagnostics", "staleness",
    "capabilities", "ai_history",
)


class ThreadTrace:
    """Read-only view over an existing local database."""

    def __init__(self, database: str | Path | None = None) -> None:
        self._store = Store(database or load_config()["storage"]["database_path"])
        self._engine = Engine(self._store)
        self._queries = QueryService(self._engine)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> ThreadTrace:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def projects(self) -> list[dict[str, Any]]:
        return self._engine.list_projects()

    def project(self, reference: str) -> dict[str, Any]:
        return self._engine.get_project(reference)

    def scans(self, reference: str) -> list[dict[str, Any]]:
        return self._engine.scan_history(reference)

    def latest_scan(self, reference: str) -> dict[str, Any]:
        return self._engine.latest_scan(reference)

    def query(self, name: str, reference: str, **options: Any) -> Any:
        """Call a documented read-only query by name."""
        if name not in READ_ONLY_QUERIES:
            raise ThreadError(f"'{name}' is not a read-only query; use the CLI or HybridHarness for execution.")
        return getattr(self._queries, name)(reference, **options)

    def traces(self, reference: str) -> list[dict[str, Any]]:
        return self._queries.traces(reference)

    def investigations(self, reference: str) -> list[dict[str, Any]]:
        return self._engine.investigations(reference)

    def investigation(self, reference: str, name: str) -> dict[str, Any]:
        return self._engine.investigation(reference, name)

    def export(self, reference: str) -> dict[str, Any]:
        """Return the same versioned report bundle the CLI export writes."""
        return report_bundle(self._engine, self._queries, reference)
