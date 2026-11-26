"""SQLite persistence for registered projects and immutable scan results."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .config import application_directory
from .evidence import EVIDENCE_VERSION

SCHEMA_VERSION = 17

INITIAL_SCHEMA = """
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



def default_database_path() -> Path:
    """Return the platform-appropriate local database path without creating it."""
    return application_directory() / "thread.sqlite3"


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_database_path()
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        backup = self._backup() if 0 < version < SCHEMA_VERSION else None
        try:
            self._migrate()
        except BaseException:
            self.connection.close()
            for suffix in ("-wal", "-shm"):
                try:
                    Path(str(self.path) + suffix).unlink()
                except OSError:
                    pass
            if backup:
                try:
                    shutil.copy2(backup, self.path)
                except OSError:
                    pass
            raise
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _backup(self) -> Path | None:
        target = self.path.with_name(self.path.name + ".bak")
        try:
            backup = sqlite3.connect(target)
            try:
                self.connection.backup(backup)
            finally:
                backup.close()
            target.chmod(0o600)
            return target
        except (OSError, sqlite3.Error):
            return None

    @property
    def schema_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("Database was created by a newer version of ThreadTrace.")
        if version == 0:
            self.connection.executescript(INITIAL_SCHEMA)
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
            version = 3
        if version < 4:
            self.connection.executescript(
                """
                CREATE TABLE launch_profiles (
                    profile_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    interpreter TEXT,
                    command_json TEXT NOT NULL DEFAULT '[]',
                    cwd_path TEXT,
                    environment_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, name)
                );
                CREATE TABLE experiments (
                    experiment_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    workspace_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(project_id, name)
                );
                CREATE TABLE investigations (
                    investigation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    content_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(project_id, name)
                );
                CREATE TABLE trace_bookmarks (
                    bookmark_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL REFERENCES runtime_traces(trace_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX trace_bookmarks_trace ON trace_bookmarks(trace_id, ordinal);
                PRAGMA user_version = 4;
                """
            )
            self.connection.commit()
            version = 4
        if version < 5:
            self.connection.executescript(
                """
                ALTER TABLE runtime_traces ADD COLUMN cwd_path TEXT;
                PRAGMA user_version = 5;
                """
            )
            self.connection.commit()
            version = 5
        if version < 6:
            self.connection.executescript(
                """
                CREATE TABLE runtime_failures (
                    failure_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL UNIQUE REFERENCES runtime_traces(trace_id) ON DELETE CASCADE,
                    report_json TEXT NOT NULL,
                    captured_at TEXT NOT NULL
                );
                PRAGMA user_version = 6;
                """
            )
            self.connection.commit()
            version = 6
        if version < 7:
            self.connection.executescript(
                """
                CREATE TABLE experiment_runs (
                    run_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX experiment_runs_experiment ON experiment_runs(experiment_id, created_at DESC);
                PRAGMA user_version = 7;
                """
            )
            self.connection.commit()
            version = 7
        if version < 8:
            self.connection.executescript(
                """
                ALTER TABLE runtime_traces ADD COLUMN capture_level TEXT NOT NULL DEFAULT 'metadata';
                ALTER TABLE runtime_traces ADD COLUMN capture_limit INTEGER NOT NULL DEFAULT 4096;
                ALTER TABLE runtime_traces ADD COLUMN redaction_enabled INTEGER NOT NULL DEFAULT 1;
                PRAGMA user_version = 8;
                """
            )
            self.connection.commit()
            version = 8
        if version < 9:
            self.connection.executescript(
                """
                ALTER TABLE experiments ADD COLUMN baseline_revision_json TEXT;
                PRAGMA user_version = 9;
                """
            )
            self.connection.commit()
        if version < 10:
            # Each call now records the binding that resolved it (or the reason
            # it could not be resolved). Existing scans keep NULL until rescanned.
            self.connection.executescript(
                """
                ALTER TABLE calls ADD COLUMN basis TEXT;
                PRAGMA user_version = 10;
                """
            )
            self.connection.commit()
        if version < 11:
            # Launch details are evidence about how a traced command was started.
            self.connection.executescript(
                """
                ALTER TABLE runtime_traces ADD COLUMN launch_json TEXT NOT NULL DEFAULT '{}';
                PRAGMA user_version = 11;
                """
            )
            self.connection.commit()
        if version < 12:
            # Keep a durable before/after record for every successful source patch.
            self.connection.executescript(
                """
                CREATE TABLE experiment_patches (
                    patch_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    before_sha256 TEXT NOT NULL,
                    after_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX experiment_patches_experiment ON experiment_patches(experiment_id, created_at);
                PRAGMA user_version = 12;
                """
            )
            self.connection.commit()
        if version < 13:
            # Evidence records are versioned output; legacy confidence remains
            # queryable and its historic counts are preserved per scan.
            self.connection.executescript(
                """
                ALTER TABLE scans ADD COLUMN evidence_version INTEGER NOT NULL DEFAULT 1;
                ALTER TABLE scans ADD COLUMN legacy_confidence_json TEXT NOT NULL DEFAULT '{}';
                PRAGMA user_version = 13;
                """
            )
            self.record_legacy_confidence()
            self.connection.commit()
        if version < 14:
            self.connection.executescript(
                """
                CREATE TABLE project_preferences (
                    project_id TEXT PRIMARY KEY REFERENCES projects(project_id) ON DELETE CASCADE,
                    preferences_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                PRAGMA user_version = 14;
                """
            )
            self.connection.commit()
        if version < 15:
            self.connection.executescript(
                """
                ALTER TABLE files ADD COLUMN origin_kind TEXT NOT NULL DEFAULT 'file';
                ALTER TABLE files ADD COLUMN original_path TEXT NOT NULL DEFAULT '';
                ALTER TABLE files ADD COLUMN resolved_path TEXT NOT NULL DEFAULT '';
                ALTER TABLE files ADD COLUMN parser TEXT NOT NULL DEFAULT 'ast';
                ALTER TABLE files ADD COLUMN parser_version TEXT NOT NULL DEFAULT '';
                ALTER TABLE files ADD COLUMN capability TEXT NOT NULL DEFAULT 'Full';
                ALTER TABLE files ADD COLUMN category TEXT NOT NULL DEFAULT 'source';
                ALTER TABLE files ADD COLUMN line_offset INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE files ADD COLUMN origin_id TEXT NOT NULL DEFAULT '';
                PRAGMA user_version = 15;
                """
            )
            self.connection.commit()
        if version < 16:
            self.connection.executescript(
                """
                ALTER TABLE imports ADD COLUMN visible_name TEXT;
                ALTER TABLE imports ADD COLUMN binding_path_json TEXT NOT NULL DEFAULT '[]';
                ALTER TABLE imports ADD COLUMN reexport INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE imports ADD COLUMN is_type_checking INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE imports ADD COLUMN source_path TEXT;
                ALTER TABLE calls ADD COLUMN is_type_checking INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE calls ADD COLUMN binding_path_json TEXT NOT NULL DEFAULT '[]';
                CREATE TABLE scopes (
                    scope_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
                    parent_scope_id TEXT REFERENCES scopes(scope_id) ON DELETE SET NULL,
                    kind TEXT NOT NULL,
                    qualified_name TEXT NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER
                );
                CREATE INDEX scopes_scan_file ON scopes(scan_id, file_id);
                CREATE TABLE definitions (
                    definition_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
                    scope_id TEXT REFERENCES scopes(scope_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    qualified_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    line_number INTEGER NOT NULL,
                    expression TEXT,
                    exported INTEGER NOT NULL DEFAULT 0,
                    is_type_checking INTEGER NOT NULL DEFAULT 0,
                    conditional INTEGER NOT NULL DEFAULT 0,
                    resolved_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
                    resolution TEXT NOT NULL DEFAULT 'unresolved',
                    binding_path_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX definitions_scan_scope ON definitions(scan_id, scope_id, name);
                CREATE TABLE symbol_references (
                    reference_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
                    scope_id TEXT REFERENCES scopes(scope_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    expression TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    line_number INTEGER NOT NULL,
                    is_type_checking INTEGER NOT NULL DEFAULT 0,
                    resolved_definition_id TEXT REFERENCES definitions(definition_id) ON DELETE SET NULL,
                    resolved_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
                    resolution TEXT NOT NULL DEFAULT 'unresolved',
                    binding_path_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX references_scan_symbol ON symbol_references(scan_id, resolved_symbol_id);
                CREATE TABLE boundaries (
                    boundary_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
                    scope_id TEXT REFERENCES scopes(scope_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    expression TEXT NOT NULL,
                    line_number INTEGER NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE exports (
                    export_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                PRAGMA user_version = 16;
                """
            )
            self.connection.commit()
        if version < 17:
            self.connection.executescript(
                """
                CREATE TABLE metadata_files (
                    metadata_file_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content_hash TEXT,
                    error_message TEXT,
                    limitations_json TEXT NOT NULL DEFAULT '[]',
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX metadata_files_scan ON metadata_files(scan_id, relative_path);
                CREATE TABLE dependencies (
                    dependency_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    raw TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    marker TEXT,
                    extras_json TEXT NOT NULL DEFAULT '[]',
                    source_file TEXT NOT NULL,
                    direct_url TEXT,
                    path TEXT,
                    editable INTEGER NOT NULL DEFAULT 0,
                    metadata_kind TEXT NOT NULL,
                    limitations_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX dependencies_scan_name ON dependencies(scan_id, name);
                CREATE TABLE scan_inputs (
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    input_key TEXT NOT NULL,
                    category TEXT NOT NULL,
                    value TEXT NOT NULL DEFAULT '',
                    input_hash TEXT NOT NULL,
                    PRIMARY KEY(scan_id, input_key)
                );
                PRAGMA user_version = 17;
                """
            )
            self.connection.commit()

    def record_legacy_confidence(self, scan_id: str | None = None) -> None:
        """Keep the pre-v1 confidence distribution as migration metadata."""
        scan_rows = (self.connection.execute("SELECT scan_id FROM scans WHERE scan_id=?", (scan_id,)).fetchall()
                     if scan_id else self.connection.execute("SELECT scan_id FROM scans").fetchall())
        for scan in scan_rows:
            counts: dict[str, int] = {}
            for table in ("entry_points", "relationships", "components", "resources", "data_edges"):
                rows = self.connection.execute(
                    f"SELECT confidence,COUNT(*) AS count FROM {table} WHERE scan_id=? GROUP BY confidence",
                    (scan["scan_id"],),
                ).fetchall()
                for row in rows:
                    counts[row["confidence"]] = counts.get(row["confidence"], 0) + row["count"]
            self.connection.execute(
                "UPDATE scans SET evidence_version=?,legacy_confidence_json=? WHERE scan_id=?",
                (EVIDENCE_VERSION, json.dumps(counts, sort_keys=True), scan["scan_id"]),
            )

    def usage(self) -> dict[str, Any]:
        """Report local storage size and retained record counts."""
        size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                size += Path(str(self.path) + suffix).stat().st_size
            except OSError:
                pass
        tables = ("projects", "scans", "runtime_traces", "runtime_events", "experiments", "experiment_runs",
                  "investigations", "trace_bookmarks")
        counts = {table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        return {"database_path": str(self.path), "database_bytes": size, "schema_version": SCHEMA_VERSION, "counts": counts}

    def prune(self, retention_days: int) -> dict[str, Any]:
        """Delete records older than the retention window while keeping the latest scan per project."""
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1.")
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        scans = self.rows(
            """SELECT scan_id FROM scans WHERE status IN ('completed','failed') AND completed_at IS NOT NULL
               AND completed_at < ? AND scan_id != COALESCE((
                   SELECT newest.scan_id FROM scans newest WHERE newest.project_id=scans.project_id
                   AND newest.status='completed' ORDER BY newest.completed_at DESC LIMIT 1), '')""",
            (cutoff,),
        )
        traces = self.rows("SELECT trace_id FROM runtime_traces WHERE finished_at < ?", (cutoff,))
        with self.transaction():
            for row in scans:
                self.execute("DELETE FROM scans WHERE scan_id=?", (row["scan_id"],))
            for row in traces:
                self.execute("DELETE FROM runtime_traces WHERE trace_id=?", (row["trace_id"],))
        return {"cutoff": cutoff, "removed_scans": len(scans), "removed_traces": len(traces)}

    def remove_trace(self, trace_id: str, project_id: str | None = None) -> int:
        clauses = ["trace_id=?"]
        values: list[Any] = [trace_id]
        if project_id:
            clauses.append("project_id=?")
            values.append(project_id)
        with self.transaction():
            cursor = self.connection.execute(f"DELETE FROM runtime_traces WHERE {' AND '.join(clauses)}", tuple(values))
        return cursor.rowcount

    def remove_report(self, scan_id: str, report_kind: str) -> int:
        with self.transaction():
            cursor = self.connection.execute("DELETE FROM reports WHERE scan_id=? AND report_kind=?", (scan_id, report_kind))
        return cursor.rowcount

    def clear_projects(self) -> int:
        with self.transaction():
            cursor = self.connection.execute("DELETE FROM projects")
        return cursor.rowcount

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

