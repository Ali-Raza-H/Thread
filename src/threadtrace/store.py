"""SQLite persistence for registered projects and immutable scan results."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 3


def default_database_path() -> Path:
    """Return the platform-appropriate local database path without creating it."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "thread" / "thread.sqlite3"


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("Database was created by a newer version of Thread.")
        if version == 0:
            self.connection.executescript(
                """
                CREATE TABLE projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    root_path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE scans (
                    scan_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
                    tool_version TEXT NOT NULL,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT
                );
                CREATE INDEX scans_project_completed ON scans(project_id, status, completed_at DESC);
                CREATE TABLE files (
                    file_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    module_name TEXT NOT NULL,
                    content_hash TEXT,
                    parse_status TEXT NOT NULL,
                    error_message TEXT,
                    error_line INTEGER,
                    is_entry_point INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(scan_id, relative_path)
                );
                CREATE INDEX files_scan_module ON files(scan_id, module_name);
                CREATE TABLE symbols (
                    symbol_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
                    qualified_name TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    parent_qualified_name TEXT,
                    signature TEXT,
                    docstring TEXT,
                    purpose TEXT,
                    decorators_json TEXT NOT NULL DEFAULT '[]',
                    line_start INTEGER NOT NULL,
                    line_end INTEGER,
                    column_start INTEGER,
                    complexity INTEGER,
                    UNIQUE(scan_id, qualified_name, line_start)
                );
                CREATE INDEX symbols_scan_name ON symbols(scan_id, name);
                CREATE INDEX symbols_scan_qualified ON symbols(scan_id, qualified_name);
                CREATE TABLE attributes (
                    attribute_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    class_symbol_id TEXT NOT NULL REFERENCES symbols(symbol_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    line_number INTEGER NOT NULL,
                    value_expression TEXT
                );
                CREATE TABLE imports (
                    import_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    source_file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
                    target_module TEXT NOT NULL,
                    imported_name TEXT,
                    alias TEXT,
                    level INTEGER NOT NULL DEFAULT 0,
                    category TEXT NOT NULL DEFAULT 'unknown',
                    resolved_module TEXT,
                    line_number INTEGER NOT NULL,
                    column_number INTEGER
                );
                CREATE INDEX imports_scan_source ON imports(scan_id, source_file_id);
                CREATE INDEX imports_scan_target ON imports(scan_id, resolved_module);
                CREATE TABLE calls (
                    call_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    caller_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
                    call_file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
                    target_expression TEXT NOT NULL,
                    callee_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
                    resolution TEXT NOT NULL CHECK(resolution IN ('resolved','ambiguous','unresolved')),
                    candidate_json TEXT NOT NULL DEFAULT '[]',
                    line_number INTEGER NOT NULL,
                    column_number INTEGER
                );
                CREATE INDEX calls_scan_caller ON calls(scan_id, caller_symbol_id);
                CREATE INDEX calls_scan_callee ON calls(scan_id, callee_symbol_id);
                CREATE TABLE flows (
                    flow_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    symbol_id TEXT NOT NULL REFERENCES symbols(symbol_id) ON DELETE CASCADE,
                    variable_name TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    expression TEXT,
                    line_number INTEGER NOT NULL
                );
                CREATE INDEX flows_symbol ON flows(symbol_id, line_number);
                CREATE TABLE libraries (
                    library_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    declared_in TEXT,
                    UNIQUE(scan_id, name, category, declared_in)
                );
                CREATE TABLE findings (
                    finding_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    rule_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    relative_path TEXT,
                    line_number INTEGER,
                    evidence_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX findings_scan_severity ON findings(scan_id, severity);
                PRAGMA user_version = 1;
                """
            )
            self.connection.commit()
            version = 1
        if version < 2:
            # Keep v1 snapshots readable and add the reconstruction layer in a
            # forward-only migration.  Analysis records are scan-scoped, so no
            # lossy backfill is necessary: a fresh scan populates the new facts.
            self.connection.executescript(
                """
                ALTER TABLE symbols ADD COLUMN parameters_json TEXT NOT NULL DEFAULT '[]';
                ALTER TABLE calls ADD COLUMN arguments_json TEXT NOT NULL DEFAULT '[]';
                ALTER TABLE calls ADD COLUMN keywords_json TEXT NOT NULL DEFAULT '{}';
                ALTER TABLE calls ADD COLUMN assignment_target TEXT;
                ALTER TABLE calls ADD COLUMN in_main_guard INTEGER NOT NULL DEFAULT 0;

                CREATE TABLE entry_points (
                    entry_point_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
                    module_name TEXT,
                    input_json TEXT NOT NULL DEFAULT '{}',
                    confidence TEXT NOT NULL CHECK(confidence IN ('confirmed','high','possible','unknown')),
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(scan_id, kind, name, module_name)
                );
                CREATE INDEX entry_points_scan_kind ON entry_points(scan_id, kind);

                CREATE TABLE relationships (
                    relationship_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    source_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
                    target_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
                    source_module TEXT,
                    target_module TEXT,
                    kind TEXT NOT NULL,
                    confidence TEXT NOT NULL CHECK(confidence IN ('confirmed','high','possible','unknown')),
                    evidence_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX relationships_scan_source ON relationships(scan_id, source_symbol_id, source_module);
                CREATE INDEX relationships_scan_target ON relationships(scan_id, target_symbol_id, target_module);

                CREATE TABLE components (
                    component_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    confidence TEXT NOT NULL CHECK(confidence IN ('confirmed','high','possible','unknown')),
                    summary TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(scan_id, name)
                );
                CREATE TABLE component_members (
                    component_id TEXT NOT NULL REFERENCES components(component_id) ON DELETE CASCADE,
                    member_type TEXT NOT NULL CHECK(member_type IN ('module','symbol')),
                    member_id TEXT NOT NULL,
                    role TEXT,
                    PRIMARY KEY(component_id, member_type, member_id)
                );

                CREATE TABLE resources (
                    resource_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
                    module_name TEXT,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    confidence TEXT NOT NULL CHECK(confidence IN ('confirmed','high','possible','unknown')),
                    evidence_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX resources_scan_kind ON resources(scan_id, kind);

                CREATE TABLE data_edges (
                    data_edge_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    source_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
                    target_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
                    source_name TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    confidence TEXT NOT NULL CHECK(confidence IN ('confirmed','high','possible','unknown')),
                    evidence_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX data_edges_scan_symbols ON data_edges(scan_id, source_symbol_id, target_symbol_id);

                CREATE TABLE reports (
                    report_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    report_kind TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    UNIQUE(scan_id, report_kind)
                );
                PRAGMA user_version = 2;
                """
            )
            self.connection.commit()
            version = 2
        if version < 3:
            self.connection.executescript(
                """
                CREATE TABLE runtime_traces (
                    trace_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    scan_id TEXT REFERENCES scans(scan_id) ON DELETE SET NULL,
                    command_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    exit_code INTEGER,
                    timed_out INTEGER NOT NULL DEFAULT 0,
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '',
                    event_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX runtime_traces_project ON runtime_traces(project_id, finished_at DESC);
                CREATE TABLE runtime_events (
                    event_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL REFERENCES runtime_traces(trace_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    event_kind TEXT NOT NULL,
                    qualified_name TEXT,
                    relative_path TEXT,
                    line_number INTEGER,
                    evidence_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX runtime_events_trace ON runtime_events(trace_id, ordinal);
                PRAGMA user_version = 3;
                """
            )
            self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with self.connection:
                yield self.connection
        except Exception:
            raise

    def execute(self, query: str, values: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.connection.execute(query, values)

    def rows(self, query: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(query, values)]

    def one(self, query: str, values: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = self.connection.execute(query, values).fetchone()
        return dict(row) if row else None

    @staticmethod
    def json(value: Any) -> str:
        return json.dumps(value, sort_keys=True)

