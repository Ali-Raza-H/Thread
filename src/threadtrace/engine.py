"""Project lifecycle, scan orchestration, static resolution, and diagnostics."""

from __future__ import annotations

import json
import hashlib
import builtins
import re
import shutil
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .config import application_directory, merge as merge_preferences, validate_preferences
from .formats import EXPORT_SCHEMA_VERSION, TRACE_PROTOCOL_VERSION
from .metadata import MetadataSnapshot, parse_metadata
from .hybrid import compare_runs, create_workspace, explore_inputs, patch_file, run_harness, run_workspace, source_revision, validate_experiment_config, workspace_changes, workspace_diff
from .models import (AttributeRecord, BoundaryRecord, CallRecord, DefinitionRecord, FlowRecord, ImportRecord,
                     ParsedFile, ReferenceRecord, ScopeRecord, SymbolRecord)
from .parser import classify_project_files, discover_python_files, discover_source_units
from .reconstruction import Reconstruction
from .resolution import Resolution
from .store import Store


def now() -> str:
    return datetime.now(UTC).isoformat()


_PYTHON_EXECUTABLE = re.compile(r"(python(\d+(\.\d+)*)?|py)(\.exe)?", re.IGNORECASE)
_SECRET_ENVIRONMENT_NAMES = (
    "authorization", "password", "passwd", "secret", "token", "api_key", "apikey",
    "access_key", "client_secret", "session_key", "credential", "cookie", "private_key",
)


def _is_python_executable(token: str) -> bool:
    return bool(_PYTHON_EXECUTABLE.fullmatch(Path(token).name))


def _validate_profile_environment(environment: dict[str, str | None] | None) -> None:
    for name, value in (environment or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ThreadError(f"'{name}' is not a valid environment variable name.")
        if value is not None and not isinstance(value, str):
            raise ThreadError(f"Environment variable '{name}' must be text or removed.")
        lowered = name.lower().replace("-", "_")
        if any(word in lowered for word in _SECRET_ENVIRONMENT_NAMES):
            raise ThreadError(
                f"Secret environment variable '{name}' cannot be stored in a launch profile; inherit it from the environment instead."
            )


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

    def remove_project(self, reference: str, keep_experiments: bool = False) -> dict[str, Any]:
        """Delete a registration after removing or explicitly preserving experiment data."""
        project = self.get_project(reference)
        experiments = self.store.rows(
            "SELECT name,workspace_path FROM experiments WHERE project_id=? ORDER BY name",
            (project["project_id"],),
        )
        removed: list[dict[str, Any]] = []
        preserved: list[dict[str, Any]] = []
        retained: list[dict[str, Any]] = []
        if keep_experiments:
            preserved = [dict(experiment) for experiment in experiments]
        else:
            for experiment in experiments:
                resolved, reason = self._experiment_workspace(experiment, Path(project["root_path"]))
                if resolved is None:
                    if reason == "missing":
                        continue
                    retained.append({**experiment, "reason": reason})
                    continue
                try:
                    shutil.rmtree(resolved)
                except OSError as exc:
                    retained.append({**experiment, "reason": str(exc)})
                else:
                    removed.append({**experiment, "workspace_path": str(resolved)})
        if retained:
            details = "; ".join(f"{item['name']}: {item['reason']}" for item in retained)
            raise ThreadError(
                f"Refusing to remove the project registration while experiment data remains: {details}. Fix or preserve it, then retry."
            )
        with self.store.transaction():
            self.store.execute("DELETE FROM projects WHERE project_id = ?", (project["project_id"],))
        return {**project, "experiments_removed": removed, "experiments_preserved": preserved}

    def project_preferences(self, project_ref: str) -> dict[str, Any]:
        project = self.get_project(project_ref)
        row = self.store.one("SELECT preferences_json FROM project_preferences WHERE project_id=?", (project["project_id"],))
        return json.loads(row["preferences_json"]) if row else {}

    def save_project_preferences(self, project_ref: str, content: dict[str, Any]) -> dict[str, Any]:
        project = self.get_project(project_ref)
        try:
            preferences = validate_preferences(content)
        except ValueError as exc:
            raise ThreadError(str(exc)) from exc
        preferences = merge_preferences(self.project_preferences(project_ref), preferences)
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO project_preferences(project_id,preferences_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(project_id) DO UPDATE SET preferences_json=excluded.preferences_json,
                   updated_at=excluded.updated_at""",
                (project["project_id"], self.store.json(preferences), now()),
            )
        return preferences

    def storage_usage(self) -> dict[str, Any]:
        usage = self.store.usage()
        external_bytes = 0
        workspaces = self.store.rows("SELECT workspace_path FROM experiments")
        for workspace in workspaces:
            path = Path(workspace["workspace_path"])
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                external_bytes += sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            except OSError:
                continue
        usage["experiment_workspace_bytes"] = external_bytes
        usage["total_bytes"] = usage["database_bytes"] + external_bytes
        return usage

    def prune_storage(self, retention_days: int) -> dict[str, Any]:
        try:
            return self.store.prune(retention_days)
        except ValueError as exc:
            raise ThreadError(str(exc)) from exc

    def remove_trace(self, project_ref: str, trace_id: str) -> dict[str, Any]:
        project = self.get_project(project_ref)
        removed = self.store.remove_trace(trace_id, project["project_id"])
        if not removed:
            raise ThreadError("Trace does not belong to this project.")
        return {"trace_id": trace_id, "project": project["name"], "removed": removed, "external_paths": []}

    def remove_report(self, project_ref: str, report_kind: str) -> dict[str, Any]:
        project = self.get_project(project_ref)
        scan = self.store.one(
            "SELECT scan_id FROM scans WHERE project_id=? AND status='completed' ORDER BY completed_at DESC LIMIT 1",
            (project["project_id"],),
        )
        if not scan:
            raise ThreadError("This project has no completed scan reports.")
        removed = self.store.remove_report(scan["scan_id"], report_kind)
        if not removed:
            raise ThreadError(f"No report named '{report_kind}' exists in the latest scan.")
        return {"project": project["name"], "scan_id": scan["scan_id"], "report_kind": report_kind,
                "removed": removed, "external_paths": []}

    def remove_all_data(self) -> dict[str, Any]:
        projects = self.list_projects()
        removed_workspaces: list[dict[str, Any]] = []
        preserved_workspaces: list[dict[str, Any]] = []
        for project in projects:
            result = self.remove_project(project["name"])
            removed_workspaces.extend(result["experiments_removed"])
            preserved_workspaces.extend(result["experiments_preserved"])
        self.store.clear_projects()
        return {"projects_removed": len(projects), "workspaces_removed": removed_workspaces,
                "workspaces_preserved": preserved_workspaces, "external_paths": []}

    def launch_profiles(self, project_ref: str) -> list[dict[str, Any]]:
        project = self.get_project(project_ref)
        rows = self.store.rows("SELECT * FROM launch_profiles WHERE project_id=? ORDER BY name", (project["project_id"],))
        for row in rows:
            row["command"] = json.loads(row.pop("command_json"))
            row["environment"] = json.loads(row.pop("environment_json"))
        return rows

    def save_launch_profile(
        self, project_ref: str, name: str, command: list[str], interpreter: str | None = None,
        cwd_path: str | None = None, environment: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        if not name.strip() or not command:
            raise ThreadError("A profile needs a name and an explicit command.")
        if interpreter is not None:
            if not interpreter.strip():
                raise ThreadError("An interpreter cannot be empty.")
            if not _is_python_executable(command[0]):
                raise ThreadError(
                    "A profile with --interpreter must start with a Python command; the interpreter replaces that first token."
                )
        _validate_profile_environment(environment)
        project = self.get_project(project_ref)
        timestamp = now()
        with self.store.transaction():
            existing = self.store.one("SELECT profile_id,created_at FROM launch_profiles WHERE project_id=? AND name=?", (project["project_id"], name))
            if existing:
                self.store.execute(
                    "UPDATE launch_profiles SET interpreter=?,command_json=?,cwd_path=?,environment_json=?,updated_at=? WHERE profile_id=?",
                    (interpreter, self.store.json(command), cwd_path, self.store.json(environment or {}), timestamp, existing["profile_id"]),
                )
            else:
                self.store.execute(
                    "INSERT INTO launch_profiles VALUES(?,?,?,?,?,?,?,?,?)",
                    (identifier(), project["project_id"], name, interpreter, self.store.json(command), cwd_path,
                     self.store.json(environment or {}), timestamp, timestamp),
                )
        return next(row for row in self.launch_profiles(project_ref) if row["name"] == name)

    def run_launch_profile(self, project_ref: str, name: str, timeout: int, output_cap: int) -> dict[str, Any]:
        profile = next((item for item in self.launch_profiles(project_ref) if item["name"] == name), None)
        if not profile:
            raise ThreadError(f"No launch profile named '{name}' exists for this project.")
        command = list(profile["command"])
        if profile["interpreter"]:
            command[0] = profile["interpreter"]
        cwd = profile["cwd_path"]
        if cwd:
            path = Path(cwd).expanduser()
            cwd = str(path if path.is_absolute() else Path(self.get_project(project_ref)["root_path"]) / path)
        return self.trace(project_ref, command, timeout, output_cap, cwd, environment=profile["environment"])

    def investigations(self, project_ref: str) -> list[dict[str, Any]]:
        project = self.get_project(project_ref)
        rows = self.store.rows("SELECT * FROM investigations WHERE project_id=? ORDER BY updated_at DESC", (project["project_id"],))
        for row in rows:
            row["content"] = json.loads(row.pop("content_json"))
        return rows

    def save_investigation(self, project_ref: str, name: str, content: dict[str, Any]) -> dict[str, Any]:
        if not name.strip():
            raise ThreadError("An investigation needs a name.")
        project = self.get_project(project_ref)
        timestamp = now()
        with self.store.transaction():
            existing = self.store.one("SELECT investigation_id,created_at FROM investigations WHERE project_id=? AND name=?", (project["project_id"], name))
            if existing:
                self.store.execute("UPDATE investigations SET content_json=?,updated_at=? WHERE investigation_id=?", (self.store.json(content), timestamp, existing["investigation_id"]))
            else:
                self.store.execute("INSERT INTO investigations VALUES(?,?,?,?,?,?)", (identifier(), project["project_id"], name, timestamp, timestamp, self.store.json(content)))
        return next(row for row in self.investigations(project_ref) if row["name"] == name)

    def bookmark_trace(self, project_ref: str, trace_id: str, ordinal: int, label: str, note: str = "") -> dict[str, Any]:
        if ordinal < 1 or not label.strip():
            raise ThreadError("A bookmark needs a positive event number and a label.")
        project = self.get_project(project_ref)
        trace = self.store.one("SELECT trace_id FROM runtime_traces WHERE trace_id=? AND project_id=?", (trace_id, project["project_id"]))
        event = self.store.one("SELECT 1 FROM runtime_events WHERE trace_id=? AND ordinal=?", (trace_id, ordinal))
        if not trace or not event:
            raise ThreadError("Bookmark target is not an event in this project's trace.")
        record = {"bookmark_id": identifier(), "trace_id": trace_id, "ordinal": ordinal, "label": label, "note": note, "created_at": now()}
        with self.store.transaction():
            self.store.execute("INSERT INTO trace_bookmarks VALUES(?,?,?,?,?,?)", tuple(record.values()))
        return record

    def trace_bookmarks(self, project_ref: str, trace_id: str) -> list[dict[str, Any]]:
        project = self.get_project(project_ref)
        trace = self.store.one("SELECT 1 FROM runtime_traces WHERE trace_id=? AND project_id=?", (trace_id, project["project_id"]))
        if not trace:
            raise ThreadError("Trace does not belong to this project.")
        return self.store.rows("SELECT * FROM trace_bookmarks WHERE trace_id=? ORDER BY ordinal,created_at", (trace_id,))

    def experiments(self, project_ref: str) -> list[dict[str, Any]]:
        project = self.get_project(project_ref)
        rows = self.store.rows("SELECT * FROM experiments WHERE project_id=? ORDER BY updated_at DESC", (project["project_id"],))
        for row in rows:
            row["config"] = json.loads(row.pop("config_json"))
            revision_json = row.pop("baseline_revision_json")
            revision = json.loads(revision_json) if revision_json else None
            row["baseline_source_hash"] = revision["tree_hash"] if revision else None
        return rows

    def create_experiment(self, project_ref: str, name: str) -> dict[str, Any]:
        if not name.strip():
            raise ThreadError("An experiment needs a name.")
        project = self.get_project(project_ref)
        if self.store.one("SELECT 1 FROM experiments WHERE project_id=? AND name=?", (project["project_id"], name)):
            raise ThreadError(f"An experiment named '{name}' already exists for this project.")
        source = Path(project["root_path"])
        try:
            baseline_revision = source_revision(source)
            workspace = create_workspace(source, application_directory() / "experiments")
            if source_revision(workspace) != baseline_revision:
                shutil.rmtree(workspace)
                raise ThreadError("Project source changed while the experiment copy was being created. Try again.")
        except OSError as exc:
            raise ThreadError(f"Could not create experiment workspace: {exc}") from exc
        timestamp = now()
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO experiments(
                    experiment_id,project_id,name,workspace_path,created_at,updated_at,status,
                    config_json,baseline_revision_json
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (identifier(), project["project_id"], name, str(workspace), timestamp, timestamp, "ready",
                 self.store.json({}), self.store.json(baseline_revision)),
            )
        return next(row for row in self.experiments(project_ref) if row["name"] == name)

    def _experiment_workspace(self, experiment: dict[str, Any], project_root: Path) -> tuple[Path | None, str | None]:
        """Validate one experiment workspace without trusting its stored path."""
        workspace = Path(experiment["workspace_path"])
        if not workspace.exists() and not workspace.is_symlink():
            return None, "missing"
        try:
            resolved = workspace.resolve(strict=True)
        except OSError as exc:
            return None, f"inaccessible: {exc}"
        valid_identifier = len(resolved.name) == 32 and all(character in "0123456789abcdef" for character in resolved.name)
        project_root = project_root.resolve()
        if (workspace.is_symlink() or not resolved.is_dir() or resolved.parent.name != "experiments" or
                not valid_identifier or project_root == resolved or project_root.is_relative_to(resolved)):
            return None, "workspace failed safety validation"
        return resolved, None

    def remove_experiment(self, project_ref: str, name: str) -> dict[str, Any]:
        experiment = self._experiment(project_ref, name)
        project = self.get_project(project_ref)
        resolved, reason = self._experiment_workspace(experiment, Path(project["root_path"]))
        if resolved is None:
            if reason == "missing":
                with self.store.transaction():
                    self.store.execute("DELETE FROM experiments WHERE experiment_id=?", (experiment["experiment_id"],))
                return {"experiment_id": experiment["experiment_id"], "name": name,
                        "workspace_path": experiment["workspace_path"], "removed": False, "reason": reason}
            raise ThreadError(f"Refusing to remove an invalid experiment workspace: {reason}.")
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            raise ThreadError(f"Could not remove experiment workspace: {exc}") from exc
        with self.store.transaction():
            self.store.execute("DELETE FROM experiments WHERE experiment_id=?", (experiment["experiment_id"],))
        return {"experiment_id": experiment["experiment_id"], "name": name, "workspace_path": str(resolved), "removed": True}

    def configure_experiment(self, project_ref: str, name: str, config: dict[str, Any]) -> dict[str, Any]:
        """Save repeatable harness rules without changing the registered source."""
        if not isinstance(config, dict):
            raise ThreadError("Experiment configuration must be a JSON object.")
        experiment = self._experiment(project_ref, name)
        allowed = {
            "patches", "substitutions", "disabled_modules", "mocks", "overrides", "argument_overrides",
            "line_overrides", "adapters", "fixtures", "setup_code", "allow_expressions", "input_generators",
            "yield_cap", "event_cap", "environment", "seed", "freeze_time", "uuid", "float_tolerance",
        }
        unknown = sorted(set(config) - allowed)
        if unknown:
            raise ThreadError(f"Unsupported experiment configuration field(s): {', '.join(unknown)}.")
        try:
            validate_experiment_config(config)
        except (TypeError, ValueError) as exc:
            raise ThreadError(f"Invalid experiment configuration: {exc}") from exc
        with self.store.transaction():
            self.store.execute("UPDATE experiments SET config_json=?,status=?,updated_at=? WHERE experiment_id=?",
                               (self.store.json(config), "configured", now(), experiment["experiment_id"]))
        return next(row for row in self.experiments(project_ref) if row["name"] == name)

    def patch_experiment(self, project_ref: str, name: str, path: str, start_line: int, end_line: int, replacement: str) -> dict[str, Any]:
        experiment = self._experiment(project_ref, name)
        try:
            patch = patch_file(Path(experiment["workspace_path"]), path, start_line, end_line, replacement)
        except (OSError, ValueError) as exc:
            raise ThreadError(f"Could not patch experiment: {exc}") from exc
        with self.store.transaction():
            self.store.execute(
                """INSERT INTO experiment_patches(
                    patch_id,experiment_id,relative_path,start_line,end_line,before_sha256,after_sha256,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (identifier(), experiment["experiment_id"], path, start_line, end_line,
                 patch["before_sha256"], patch["after_sha256"], now()),
            )
        self._touch_experiment(experiment["experiment_id"], "patched")
        return {**experiment, "patch": patch, "diff": self.experiment_diff(project_ref, name)}

    def experiment_patches(self, project_ref: str, name: str) -> list[dict[str, Any]]:
        experiment = self._experiment(project_ref, name)
        return self.store.rows(
            "SELECT relative_path,start_line,end_line,before_sha256,after_sha256,created_at FROM experiment_patches WHERE experiment_id=? ORDER BY created_at",
            (experiment["experiment_id"],),
        )

    def experiment_changes(self, project_ref: str, name: str) -> dict[str, Any]:
        experiment = self._experiment(project_ref, name)
        project = self.get_project(project_ref)
        return workspace_changes(Path(project["root_path"]), Path(experiment["workspace_path"]))

    def experiment_diff(self, project_ref: str, name: str) -> str:
        experiment = self._experiment(project_ref, name)
        project = self.get_project(project_ref)
        return workspace_diff(Path(project["root_path"]), Path(experiment["workspace_path"]))

    def run_experiment(self, project_ref: str, name: str, command: list[str], timeout: int, output_cap: int) -> dict[str, Any]:
        experiment = self._experiment(project_ref, name)
        try:
            result = run_workspace(Path(experiment["workspace_path"]), command, timeout, output_cap)
        except (OSError, ValueError) as exc:
            raise ThreadError(f"Could not run experiment: {exc}") from exc
        self._touch_experiment(experiment["experiment_id"], "ran")
        return result

    def callable_experiment(self, project_ref: str, name: str, target: str, args: list[Any] | None = None,
                            kwargs: dict[str, Any] | None = None, timeout: int = 30, output_cap: int = 100_000,
                            operations: list[dict[str, Any]] | None = None,
                            per_call: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke a copied-workspace callable or a persistent class-operation sequence."""
        experiment = self._experiment(project_ref, name)
        config = json.loads(experiment["config_json"])
        request: dict[str, Any] = {"config": config}
        if per_call:
            request["per_call"] = per_call
        if operations is not None:
            if not isinstance(operations, list):
                raise ThreadError("Harness operations must be a JSON list.")
            request["operations"] = operations
        else:
            if not target:
                raise ThreadError("A callable target is required.")
            if not isinstance(args or [], list) or not isinstance(kwargs or {}, dict):
                raise ThreadError("Callable args must be a JSON list and kwargs must be a JSON object.")
            request.update({"target": target, "args": args or [], "kwargs": kwargs or {}})
        try:
            result = run_harness(Path(experiment["workspace_path"]), request, timeout, output_cap)
        except (OSError, ValueError) as exc:
            raise ThreadError(f"Could not run callable experiment: {exc}") from exc
        result["run_id"] = self._record_experiment_run(experiment["experiment_id"], "harness", request, result)
        self._touch_experiment(experiment["experiment_id"], "ran")
        return result

    def explore_experiment(self, project_ref: str, name: str, target: str, cases: list[dict[str, Any]] | None = None,
                           timeout: int = 30, output_cap: int = 100_000) -> dict[str, Any]:
        experiment = self._experiment(project_ref, name)
        if cases is not None and not isinstance(cases, list):
            raise ThreadError("Input cases must be a JSON list.")
        try:
            result = explore_inputs(Path(experiment["workspace_path"]), target, cases,
                                    json.loads(experiment["config_json"]), timeout, output_cap)
        except (OSError, ValueError) as exc:
            raise ThreadError(f"Could not explore callable inputs: {exc}") from exc
        self._record_experiment_run(experiment["experiment_id"], "exploration", {"target": target, "cases": cases}, result)
        self._touch_experiment(experiment["experiment_id"], "explored")
        return result

    def experiment_runs(self, project_ref: str, name: str) -> list[dict[str, Any]]:
        experiment = self._experiment(project_ref, name)
        rows = self.store.rows("SELECT * FROM experiment_runs WHERE experiment_id=? ORDER BY created_at DESC", (experiment["experiment_id"],))
        for row in rows:
            row["request"] = json.loads(row.pop("request_json"))
            row["result"] = json.loads(row.pop("result_json"))
        return rows

    def replay_experiment(self, project_ref: str, name: str, run_id: str, timeout: int = 30,
                          output_cap: int = 100_000) -> dict[str, Any]:
        experiment = self._experiment(project_ref, name)
        stored = self.store.one("SELECT * FROM experiment_runs WHERE run_id=? AND experiment_id=?", (run_id, experiment["experiment_id"]))
        if not stored:
            raise ThreadError("No saved harness run with that ID exists for this experiment.")
        request = json.loads(stored["request_json"])
        if "target" not in request and "operations" not in request:
            raise ThreadError("Only callable harness runs can be replayed.")
        try:
            result = run_harness(Path(experiment["workspace_path"]), request, timeout, output_cap)
            confirmation = run_harness(Path(experiment["workspace_path"]), request, timeout, output_cap)
        except (OSError, ValueError) as exc:
            raise ThreadError(f"Could not replay experiment: {exc}") from exc
        original = json.loads(stored["result_json"])
        comparison = compare_runs(original, result, float(json.loads(experiment["config_json"]).get("float_tolerance", 1e-9)))
        repeat = compare_runs(result, confirmation, float(json.loads(experiment["config_json"]).get("float_tolerance", 1e-9)))
        replay = {"replayed_run_id": run_id, "result": result, "comparison": comparison,
                  "deterministic": repeat["same_behaviour"], "nondeterminism": repeat["differences"] + repeat["unknown_comparisons"]}
        replay["replay_run_id"] = self._record_experiment_run(experiment["experiment_id"], "replay", request, replay)
        return replay

    def compare_experiment(self, project_ref: str, name: str, target: str, args: list[Any] | None = None,
                           kwargs: dict[str, Any] | None = None, timeout: int = 30, output_cap: int = 100_000) -> dict[str, Any]:
        experiment = self._experiment(project_ref, name)
        request = {"target": target, "args": args or [], "kwargs": kwargs or {}}
        if not experiment.get("baseline_revision_json"):
            raise ThreadError("This experiment predates source-revision tracking. Create a new experiment before comparing runs.")
        project = self.get_project(project_ref)
        source = Path(project["root_path"])
        baseline_revision = json.loads(experiment["baseline_revision_json"])
        config = json.loads(experiment["config_json"])
        try:
            current_revision = source_revision(source)
            if current_revision != baseline_revision:
                changed = self._revision_changes(baseline_revision, current_revision)
                detail = "; ".join(f"{kind}: {', '.join(paths[:5])}" for kind, paths in changed.items() if paths)
                raise ThreadError(
                    "Registered source changed after this experiment was created"
                    + (f" ({detail})" if detail else "")
                    + ". Create a new experiment or restore the original source before comparing."
                )
            modified_revision = source_revision(Path(experiment["workspace_path"]))
            with tempfile.TemporaryDirectory(prefix="threadtrace-comparison-") as directory:
                temporary_root = Path(directory)
                baseline_workspace = create_workspace(source, temporary_root / "baseline")
                modified_workspace = create_workspace(Path(experiment["workspace_path"]), temporary_root / "modified")
                if source_revision(baseline_workspace) != baseline_revision:
                    raise ThreadError("Registered source changed while the baseline comparison copy was being created. Try again.")
                if source_revision(modified_workspace) != modified_revision:
                    raise ThreadError("Experiment source changed while the modified comparison copy was being created. Try again.")
                baseline = run_harness(baseline_workspace, {**request, "config": {}}, timeout, output_cap)
                modified = run_harness(modified_workspace, {**request, "config": config}, timeout, output_cap)
        except (OSError, ValueError) as exc:
            raise ThreadError(f"Could not compare experiment behaviour: {exc}") from exc
        tolerance = float(config.get("float_tolerance", 1e-9))
        result = compare_runs(baseline, modified, tolerance)
        result["baseline"].update(self._comparison_provenance("baseline", baseline_revision, {}))
        result["modified"].update(self._comparison_provenance("modified", modified_revision, config))
        run_id = self._record_experiment_run(experiment["experiment_id"], "comparison", request, result)
        return {"run_id": run_id, **result}

    def experiment_capability(self, project_ref: str, name: str, target: str, timeout: int = 30,
                              output_cap: int = 100_000) -> dict[str, Any]:
        experiment = self._experiment(project_ref, name)
        request = {"action": "capability", "target": target, "config": json.loads(experiment["config_json"])}
        try:
            return run_harness(Path(experiment["workspace_path"]), request, timeout, output_cap)
        except (OSError, ValueError) as exc:
            raise ThreadError(f"Could not inspect callable capability: {exc}") from exc

    def export_experiment(self, project_ref: str, name: str, output: str | None = None) -> dict[str, Any]:
        experiment = self._experiment(project_ref, name)
        project = self.get_project(project_ref)
        bundle = {
            "format_version": EXPORT_SCHEMA_VERSION, "application_version": __version__,
            "database_schema_version": self.store.connection.execute("PRAGMA user_version").fetchone()[0],
            "trace_protocol_version": TRACE_PROTOCOL_VERSION,
            "project": project["name"], "experiment": name,
            "config": json.loads(experiment["config_json"]),
            "baseline_revision": json.loads(experiment["baseline_revision_json"]) if experiment.get("baseline_revision_json") else None,
            "workspace_diff": workspace_diff(Path(project["root_path"]), Path(experiment["workspace_path"])),
            "workspace_changes": workspace_changes(Path(project["root_path"]), Path(experiment["workspace_path"])),
            "patches": self.experiment_patches(project_ref, name),
            "runs": self.experiment_runs(project_ref, name), "exported_at": now(),
        }
        if output:
            path = Path(output).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            bundle["output_path"] = str(path.resolve())
        return bundle

    def _experiment(self, project_ref: str, name: str) -> dict[str, Any]:
        project = self.get_project(project_ref)
        experiment = self.store.one("SELECT * FROM experiments WHERE project_id=? AND name=?", (project["project_id"], name))
        if not experiment:
            raise ThreadError(f"No experiment named '{name}' exists for this project.")
        return experiment

    def _touch_experiment(self, experiment_id: str, status: str) -> None:
        with self.store.transaction():
            self.store.execute("UPDATE experiments SET status=?,updated_at=? WHERE experiment_id=?", (status, now(), experiment_id))

    @staticmethod
    def _revision_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
        before_files, after_files = before.get("files", {}), after.get("files", {})
        return {
            "created": sorted(after_files.keys() - before_files.keys()),
            "modified": sorted(name for name in before_files.keys() & after_files.keys()
                               if before_files[name] != after_files[name]),
            "deleted": sorted(before_files.keys() - after_files.keys()),
        }

    @staticmethod
    def _comparison_provenance(role: str, revision: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        executable = Path(sys.executable)
        return {
            "workspace_role": role,
            "source_revision": revision,
            "configuration": config,
            "interpreter": {
                "executable": str(executable),
                "resolved_executable": str(executable.resolve()),
                "implementation": sys.implementation.name,
                "version": sys.version.split()[0],
            },
        }

    def _record_experiment_run(self, experiment_id: str, kind: str, request: dict[str, Any], result: dict[str, Any]) -> str:
        run_id = identifier()
        with self.store.transaction():
            self.store.execute("INSERT INTO experiment_runs VALUES(?,?,?,?,?,?)",
                               (run_id, experiment_id, kind, self.store.json(request), self.store.json(result), now()))
        return run_id

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

    def trace(
        self, project_ref: str, command: list[str], timeout: int = 30, output_cap: int = 100_000,
        cwd: str | None = None, capture_level: str = "metadata", capture_limit: int = 4096,
        redact: bool = True, environment: dict[str, str | None] | None = None, save: bool = True,
        instrumentation: str = "auto", include_third_party: bool = False,
    ) -> dict[str, Any]:
        """Explicitly execute a user-provided Python command with a bounded profiler.

        Unlike :meth:`scan`, this can execute untrusted code and is only exposed
        by a CLI command that requires a separate confirmation flag.
        """
        if timeout < 1 or output_cap < 1 or capture_limit < 1:
            raise ThreadError("Trace timeout, output cap, and capture limit must be positive.")
        if capture_level not in {"off", "metadata", "content"}:
            raise ThreadError("Capture level must be off, metadata, or content.")
        if instrumentation not in {"auto", "settrace", "monitoring", "profile", "audit", "all"}:
            raise ThreadError("Instrumentation must be auto, settrace, monitoring, profile, audit, or all.")
        project = self.get_project(project_ref)
        from .runtime.launcher import run_python_trace

        try:
            result = run_python_trace(
                Path(project["root_path"]), command, timeout, output_cap,
                cwd=Path(cwd).expanduser() if cwd else None, capture_level=capture_level,
                capture_limit=capture_limit, redact=redact, environment=environment,
                instrumentation=instrumentation, include_third_party=include_third_party,
            )
        except (OSError, ValueError) as exc:
            raise ThreadError(f"Trace could not start: {exc}") from exc
        if not save:
            return {**result, "trace_id": None, "scan_id": None, "persisted": False}
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
            self.store.execute(
                """INSERT INTO runtime_traces(trace_id,project_id,scan_id,command_json,started_at,finished_at,
                   exit_code,timed_out,stdout,stderr,event_count,cwd_path,capture_level,capture_limit,
                   redaction_enabled,launch_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (trace_id, project["project_id"], scan["scan_id"] if scan else None, self.store.json(result["command"]),
                 result.get("started_at") or now(), result.get("finished_at") or now(),
                 result["exit_code"], int(result["timed_out"]), result["stdout"], result["stderr"],
                 len(result["events"]), result.get("cwd"), result.get("capture_level", "metadata"),
                 result.get("capture_limit", 4096), int(result.get("redaction_enabled", True)),
                 self.store.json(result.get("launch") or {})),
            )
            from .runtime.schema import normalise_event
            for ordinal, event in enumerate(result["events"], 1):
                event = normalise_event(event, ordinal)
                self.store.execute("INSERT INTO runtime_events VALUES(?,?,?,?,?,?,?,?)", (
                    identifier(), trace_id, ordinal, event["kind"], event.get("name"), event.get("relative_path"), event.get("line"),
                    self.store.json({"kind": "runtime", "path": event.get("relative_path"), "line_start": event.get("line"), "details": event}),
                ))
            if result.get("failure_report"):
                self.store.execute(
                    "INSERT INTO runtime_failures VALUES(?,?,?,?)",
                    (identifier(), trace_id, self.store.json(result["failure_report"]), now()),
                )
        return {"trace_id": trace_id, "scan_id": scan["scan_id"] if scan else None, **result}

    def scan(self, project_ref: str, includes: list[str] | None = None,
             excludes: set[str] | None = None, explicit_sources: list[str] | None = None,
             generated: list[dict[str, str]] | None = None) -> dict[str, Any]:
        project = self.get_project(project_ref)
        root = Path(project["root_path"])
        scan_id = identifier()
        with self.store.transaction():
            self.store.execute(
                "INSERT INTO scans(scan_id,project_id,started_at,status,tool_version) VALUES(?,?,?,?,?)",
                (scan_id, project["project_id"], now(), "running", __version__),
            )
        try:
            units, skipped = discover_source_units(root, excludes=excludes, includes=includes,
                                                    explicit_sources=explicit_sources, generated=generated)
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
            for unit in units:
                previous_file = prior_files.get(unit.relative_path)
                digest = unit.content_hash
                if digest is None and unit.source_text is not None:
                    digest = hashlib.sha256(unit.source_text.encode("utf-8")).hexdigest()
                if digest is None:
                    try:
                        digest = hashlib.sha256(Path(unit.resolved_path).read_bytes()).hexdigest()
                    except OSError:
                        digest = None
                if previous_file and digest and digest == previous_file["content_hash"]:
                    parsed_file = self._reuse_parsed_file(previous["scan_id"], previous_file)
                    reused_files += 1
                else:
                    parsed_file = unit.parse()
                self._apply_source_metadata(parsed_file, unit)
                parsed.append(parsed_file)
            metadata = parse_metadata(root)
            summary = self._persist_scan(
                scan_id, root, parsed, skipped, metadata=metadata, includes=includes, excludes=excludes,
                explicit_sources=explicit_sources, generated=generated,
            )
            summary["reused_files"] = reused_files
            summary["file_categories"] = classify_project_files(root, excludes)
            summary["metadata_files"] = len(metadata.files)
            summary["dependencies"] = len(metadata.dependencies)
            with self.store.transaction():
                self.store.execute(
                    "UPDATE scans SET completed_at=?,status='completed',summary_json=? WHERE scan_id=?",
                    (now(), self.store.json(summary), scan_id),
                )
                self.store.record_legacy_confidence(scan_id)
            return self.store.one("SELECT * FROM scans WHERE scan_id=?", (scan_id,)) or {}
        except Exception as exc:
            with self.store.transaction():
                self.store.execute(
                    "UPDATE scans SET completed_at=?,status='failed',error_message=? WHERE scan_id=?",
                    (now(), str(exc), scan_id),
                )
            raise ThreadError(f"Scan failed: {exc}") from exc

    @staticmethod
    def _apply_source_metadata(parsed: ParsedFile, unit) -> ParsedFile:
        parsed.origin_kind = unit.origin_kind
        parsed.original_path = unit.original_path
        parsed.resolved_path = unit.resolved_path
        parsed.parser = unit.parser
        parsed.parser_version = unit.parser_version
        parsed.capability = unit.capability
        parsed.category = unit.category
        parsed.line_offset = unit.line_offset
        parsed.origin_id = unit.origin_id
        return parsed

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
        # Stored target modules are already absolute; reusing their relative level
        # would prefix the current package a second time.
        imports = [ImportRecord(row["target_module"], row["imported_name"], row["alias"], 0, row["line_number"], row["column_number"],
                                bool(row["is_type_checking"]), False)
                   for row in self.store.rows("SELECT * FROM imports WHERE scan_id=? AND source_file_id=?", (prior_scan_id, file_id))]
        calls = [CallRecord(row["caller_name"], row["target_expression"], row["line_number"], row["column_number"],
                            json.loads(row["arguments_json"]), json.loads(row["keywords_json"]), row["assignment_target"],
                            bool(row["in_main_guard"]), bool(row["is_type_checking"]))
                 for row in self.store.rows("""SELECT c.*,s.qualified_name AS caller_name FROM calls c
                    LEFT JOIN symbols s ON s.symbol_id=c.caller_symbol_id WHERE c.scan_id=? AND c.call_file_id=?""", (prior_scan_id, file_id))]
        scopes = [ScopeRecord(row["scope_id"], row["kind"], row["qualified_name"], row["parent_scope_id"],
                              row["line_start"], row["line_end"])
                  for row in self.store.rows("SELECT * FROM scopes WHERE scan_id=? AND file_id=?", (prior_scan_id, file_id))]
        definitions = [DefinitionRecord(row["scope_id"], row["name"], row["qualified_name"], row["kind"], row["source"],
                                        row["line_number"], row["expression"], bool(row["exported"]),
                                        bool(row["is_type_checking"]), bool(row["conditional"]))
                       for row in self.store.rows("SELECT * FROM definitions WHERE scan_id=? AND file_id=?", (prior_scan_id, file_id))]
        references = [ReferenceRecord(row["scope_id"], row["name"], row["expression"], row["kind"], row["line_number"],
                                      bool(row["is_type_checking"]))
                      for row in self.store.rows("SELECT * FROM symbol_references WHERE scan_id=? AND file_id=?", (prior_scan_id, file_id))]
        boundaries = [BoundaryRecord(row["scope_id"], row["kind"], row["expression"], row["line_number"], json.loads(row["details_json"]))
                      for row in self.store.rows("SELECT * FROM boundaries WHERE scan_id=? AND file_id=?", (prior_scan_id, file_id))]
        exports = [row["name"] for row in self.store.rows("SELECT name FROM exports WHERE scan_id=? AND file_id=?", (prior_scan_id, file_id))]
        flows = [FlowRecord(row["symbol_name"], row["variable_name"], row["event_kind"], row["expression"], row["line_number"])
                 for row in self.store.rows("""SELECT fl.*,s.qualified_name AS symbol_name FROM flows fl
                    JOIN symbols s ON s.symbol_id=fl.symbol_id WHERE fl.scan_id=? AND s.file_id=?""", (prior_scan_id, file_id))]
        return ParsedFile(file["relative_path"], file["module_name"], file["content_hash"], "parsed", is_entry_point=bool(file["is_entry_point"]),
                          symbols=symbols, attributes=attributes, imports=imports, calls=calls, flows=flows,
                          origin_kind=file["origin_kind"], original_path=file["original_path"],
                          resolved_path=file["resolved_path"], parser=file["parser"],
                          parser_version=file["parser_version"], capability=file["capability"],
                          category=file["category"], line_offset=file["line_offset"], origin_id=file["origin_id"],
                          scopes=scopes, definitions=definitions, references=references, boundaries=boundaries,
                          exports=exports)

    def _persist_scan(self, scan_id: str, root: Path, parsed: list[ParsedFile], skipped: list[str], *,
                      metadata: MetadataSnapshot, includes: list[str] | None = None, excludes: set[str] | None = None,
                      explicit_sources: list[str] | None = None,
                      generated: list[dict[str, str]] | None = None) -> dict[str, Any]:
        file_ids: dict[str, str] = {}
        symbol_ids: dict[str, list[str]] = defaultdict(list)
        symbol_rows: dict[str, dict[str, Any]] = {}
        import_rows: list[dict[str, Any]] = []
        with self.store.transaction():
            for item in parsed:
                file_id = identifier()
                file_ids[item.relative_path] = file_id
                self.store.execute(
                    """INSERT INTO files(file_id,scan_id,relative_path,module_name,content_hash,parse_status,
                       error_message,error_line,is_entry_point,origin_kind,original_path,resolved_path,
                       parser,parser_version,capability,category,line_offset,origin_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (file_id, scan_id, item.relative_path, item.module_name, item.content_hash, item.parse_status,
                     item.error_message, item.error_line, int(item.is_entry_point), item.origin_kind,
                     item.original_path, item.resolved_path, item.parser, item.parser_version,
                     item.capability, item.category, item.line_offset, item.origin_id),
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
                scope_ids: dict[str, str] = {}
                for scope in item.scopes:
                    scope_id = identifier()
                    scope_ids[scope.key] = scope_id
                    self.store.execute(
                        """INSERT INTO scopes(scope_id,scan_id,file_id,parent_scope_id,kind,qualified_name,line_start,line_end)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (scope_id, scan_id, file_id, scope_ids.get(scope.parent_key or ""), scope.kind,
                         scope.qualified_name, scope.line_start, scope.line_end),
                    )
                for definition in item.definitions:
                    self.store.execute(
                        """INSERT INTO definitions(definition_id,scan_id,file_id,scope_id,name,qualified_name,
                           kind,source,line_number,expression,exported,is_type_checking,conditional)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (identifier(), scan_id, file_id, scope_ids.get(definition.scope_key), definition.name,
                         definition.qualified_name, definition.kind, definition.source, definition.line_number,
                         definition.expression, int(definition.exported), int(definition.is_type_checking),
                         int(definition.conditional)),
                    )
                for reference in item.references:
                    self.store.execute(
                        """INSERT INTO symbol_references(reference_id,scan_id,file_id,scope_id,name,expression,
                           kind,line_number,is_type_checking) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (identifier(), scan_id, file_id, scope_ids.get(reference.scope_key), reference.name,
                         reference.expression, reference.kind, reference.line_number,
                         int(reference.is_type_checking)),
                    )
                for boundary in item.boundaries:
                    self.store.execute(
                        """INSERT INTO boundaries(boundary_id,scan_id,file_id,scope_id,kind,expression,
                           line_number,details_json) VALUES(?,?,?,?,?,?,?,?)""",
                        (identifier(), scan_id, file_id, scope_ids.get(boundary.scope_key), boundary.kind,
                         boundary.expression, boundary.line_number, self.store.json(boundary.details)),
                    )
                for name in item.exports:
                    self.store.execute("INSERT INTO exports VALUES(?,?,?,?,?)",
                                       (identifier(), scan_id, file_id, name, "__all__"))
                import_rows.extend([{"source": item, "file_id": file_id, "record": record} for record in item.imports])
            modules = {item.module_name for item in parsed if item.parse_status == "parsed"}
            for file in metadata.files:
                self.store.execute(
                    """INSERT INTO metadata_files(metadata_file_id,scan_id,relative_path,kind,status,
                       content_hash,error_message,limitations_json,details_json) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (identifier(), scan_id, file.relative_path, file.kind, file.status, file.content_hash,
                     file.error, self.store.json(file.limitations), "{}"),
                )
            for dependency in metadata.dependencies:
                self.store.execute(
                    """INSERT INTO dependencies(dependency_id,scan_id,name,raw,group_name,marker,extras_json,
                       source_file,direct_url,path,editable,metadata_kind,limitations_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (identifier(), scan_id, dependency.name, dependency.raw, dependency.group, dependency.marker,
                     self.store.json(dependency.extras), dependency.source_file, dependency.direct_url,
                     dependency.path, int(dependency.editable), dependency.metadata_kind,
                     self.store.json(dependency.limitations)),
                )
                self.store.execute("INSERT OR IGNORE INTO libraries VALUES(?,?,?,?,?)",
                                   (identifier(), scan_id, dependency.name, "declared", dependency.source_file))
            declarations = metadata.declarations
            aliases: dict[str, dict[str, str]] = defaultdict(dict)
            module_aliases: dict[str, dict[str, str]] = defaultdict(dict)
            for wrapped in import_rows:
                item, file_id, record = wrapped["source"], wrapped["file_id"], wrapped["record"]
                target = self._absolute_import(item.module_name, item.relative_path.endswith("__init__.py"), record.target_module, record.level)
                resolved = self._resolve_import(target, record.imported_name, modules)
                root_name = (target or record.imported_name or "").split(".")[0]
                category = "internal" if resolved else self._import_category(root_name, declarations)
                visible = record.alias or record.imported_name or target.split(".")[0]
                local = any(start <= record.line_number <= end for start, end in
                            ((symbol.line_start, symbol.line_end or symbol.line_start)
                             for symbol in item.symbols if symbol.kind != "class"))
                binding = self._import_binding(target, record.imported_name, record.alias, resolved)
                self.store.execute(
                    """INSERT INTO imports(import_id,scan_id,source_file_id,target_module,imported_name,alias,
                       level,category,resolved_module,line_number,column_number,visible_name,binding_path_json,
                       reexport,is_type_checking,source_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (identifier(), scan_id, file_id, target, record.imported_name, record.alias, record.level,
                     category, resolved, record.line_number, record.column_number, visible,
                     self.store.json([binding] if binding else []), int(not local and not visible.startswith("_")),
                     int(record.is_type_checking), self._module_source(target or record.imported_name or "")),
                )
                if category != "internal" and root_name:
                    self.store.execute("INSERT OR IGNORE INTO libraries VALUES(?,?,?,?,?)", (identifier(), scan_id, root_name, category, None))
                if visible and not local:
                    aliases[file_id][visible] = binding
                    module_aliases[item.module_name][visible] = binding
            local_bindings: dict[str, set[str]] = defaultdict(set)
            local_aliases: dict[str, dict[str, str]] = defaultdict(dict)
            for item in parsed:
                if item.parse_status != "parsed":
                    continue
                for flow in item.flows:
                    if flow.event_kind in {"parameter", "assignment"}:
                        local_bindings[flow.symbol_qualified_name].add(flow.variable_name)
                functions = [(symbol.qualified_name, symbol.line_start, symbol.line_end or symbol.line_start)
                             for symbol in item.symbols if symbol.kind != "class"]
                for record in item.imports:
                    visible = record.alias or record.imported_name or record.target_module.split(".")[0]
                    for qualified, start, end in functions:
                        if start <= record.line_number <= end:
                            local_bindings[qualified].add(visible)
                            target = self._absolute_import(item.module_name, item.relative_path.endswith("__init__.py"),
                                                           record.target_module, record.level)
                            resolved = self._resolve_import(target, record.imported_name, modules)
                            local_aliases[qualified][visible] = self._import_binding(
                                target, record.imported_name, record.alias, resolved)
                            break
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
                    candidates, basis = self._call_candidates(
                        call.target_expression, item.module_name, file_id, caller_id, aliases,
                        symbol_rows, symbol_ids, class_by_method, local_bindings, local_aliases, module_aliases,
                    )
                    confirmed = basis in {"lexical", "module", "import", "re-export", "class"}
                    resolution = (
                        "resolved" if confirmed and len(candidates) == 1 else
                        "ambiguous" if confirmed and candidates else "unresolved"
                    )
                    callee = candidates[0] if resolution == "resolved" else None
                    self.store.execute(
                        """INSERT INTO calls(
                            call_id,scan_id,caller_symbol_id,call_file_id,target_expression,callee_symbol_id,
                            resolution,candidate_json,basis,line_number,column_number,arguments_json,keywords_json,
                            assignment_target,in_main_guard,is_type_checking,binding_path_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (identifier(), scan_id, caller_id, file_id, call.target_expression, callee, resolution,
                         self.store.json(candidates), basis, call.line_number, call.column_number,
                         self.store.json(call.arguments), self.store.json(call.keywords),
                         call.assignment_target, int(call.in_main_guard), int(call.is_type_checking),
                         self.store.json([symbol_rows[cid]["qualified_name"] for cid in candidates if cid in symbol_rows])),
                    )
            Resolution(self.store).resolve(scan_id)
            self._diagnose(scan_id, modules)
            Reconstruction(self.store).build(scan_id, root)
            self._record_scan_inputs(scan_id, parsed, metadata, includes, excludes, explicit_sources, generated)
        counts = Counter(item.parse_status for item in parsed)
        return {
            "scanned_files": len(parsed), "parsed_files": counts["parsed"], "failed_files": counts["failed"],
            "skipped_files": len(skipped) + counts["skipped"], "discovery_warnings": skipped,
            "symbols": sum(len(item.symbols) for item in parsed), "imports": len(import_rows),
        }

    def _record_scan_inputs(self, scan_id: str, parsed: list[ParsedFile], metadata: MetadataSnapshot,
                            includes: list[str] | None, excludes: set[str] | None,
                            explicit_sources: list[str] | None,
                            generated: list[dict[str, str]] | None) -> None:
        entries: list[tuple[str, str, str, str]] = []

        def add(key: str, category: str, value: str, digest: str) -> None:
            entries.append((key, category, value, digest))

        interpreter = {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "implementation": sys.implementation.name,
        }
        discovery = {
            "includes": sorted(includes or []),
            "excludes": sorted(excludes or []),
            "explicit_sources": sorted(explicit_sources or []),
            "generated": [
                {"name": item.get("name", "generated"),
                 "hash": hashlib.sha256(str(item.get("text", "")).encode("utf-8")).hexdigest()}
                for item in generated or []
            ],
        }
        interpreter_text = json.dumps(interpreter, sort_keys=True)
        discovery_text = json.dumps(discovery, sort_keys=True)
        add("interpreter", "interpreter", interpreter_text, hashlib.sha256(interpreter_text.encode()).hexdigest())
        add("discovery", "discovery", discovery_text, hashlib.sha256(discovery_text.encode()).hexdigest())
        for item in parsed:
            digest = item.content_hash or hashlib.sha256(b"").hexdigest()
            add(f"source:{item.relative_path}", "source", item.relative_path, digest)
        for file in metadata.files:
            digest = file.content_hash or hashlib.sha256(b"").hexdigest()
            add(f"metadata:{file.relative_path}", "metadata", file.relative_path, digest)
        with self.store.transaction():
            for key, category, value, digest in entries:
                self.store.execute(
                    "INSERT OR REPLACE INTO scan_inputs(scan_id,input_key,category,value,input_hash) VALUES(?,?,?,?,?)",
                    (scan_id, key, category, value, digest),
                )

    @staticmethod
    def _module_source(module: str) -> str | None:
        """Locate importable source without importing the target module."""
        if not module:
            return None
        relative = Path(*module.split("."))
        for entry in sys.path:
            base = Path(entry or ".")
            try:
                package = base / relative
                if (package / "__init__.py").is_file():
                    return str(package / "__init__.py")
                module_file = package.with_suffix(".py")
                if module_file.is_file():
                    return str(module_file)
                if package.is_dir():
                    return str(package)
            except OSError:
                continue
        return None

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
    def _import_binding(target: str, imported: str | None, alias: str | None, resolved: str | None) -> str:
        """Return the absolute source name one visible import name refers to."""
        if not imported and not alias:
            return target.split(".")[0]
        base = resolved or target
        if imported and resolved and not resolved.endswith("." + imported):
            return base + "." + imported
        return base

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
        local_bindings: dict[str, set[str]], local_aliases: dict[str, dict[str, str]],
        module_aliases: dict[str, dict[str, str]],
    ) -> tuple[list[str], str]:
        """Resolve a call through Python name bindings, never a project-wide name match."""
        def exact(name: str) -> list[str]:
            return symbol_ids.get(name, [])

        def exported(name: str, depth: int = 0) -> list[str]:
            found = exact(name)
            if found or depth >= 3:
                return found
            owner, _, attribute = name.rpartition(".")
            mapping = module_aliases.get(owner, {})
            return exported(mapping[attribute], depth + 1) if attribute in mapping else []

        def scopes() -> list[str]:
            chain: list[str] = []
            row = symbol_rows.get(caller_id) if caller_id else None
            while row:
                if row["kind"] != "class":
                    chain.append(row["qualified_name"])
                parent = row["parent_qualified_name"]
                row = next((item for item in symbol_rows.values() if item["qualified_name"] == parent), None)
            if module not in chain:
                chain.append(module)
            return chain

        if not expression or expression == "<dynamic>" or "(" in expression or "[" in expression:
            return [], "dynamic"
        if expression.startswith(("self.", "cls.")) and caller_id:
            klass = class_by_method.get(caller_id)
            return (exact(f"{klass}.{expression.split('.', 1)[1]}") if klass else []), "class"
        first, dot, rest = expression.partition(".")
        for scope in scopes():
            if first in local_bindings.get(scope, set()) and not exact(f"{scope}.{first}"):
                local = local_aliases.get(scope, {}).get(first)
                if not local:
                    return [], "shadowed"
                found = exported(local + (f".{rest}" if dot else ""))
                return found, "import" if found == exact(local + (f".{rest}" if dot else "")) else "re-export"
            if dot:
                found = exact(f"{scope}.{expression}")
                if found:
                    return found, "module" if scope == module else "lexical"
                continue
            found = exact(f"{scope}.{first}")
            if found:
                return found, "module" if scope == module else "lexical"
        mapped = aliases[file_id].get(first)
        if mapped:
            found = exported(mapped + (f".{rest}" if dot else ""))
            return found, "import" if found == exact(mapped + (f".{rest}" if dot else "")) else "re-export"
        if dot:
            return [], "unresolved"
        if hasattr(builtins, first):
            return [], "builtin"
        return [sid for sid, row in symbol_rows.items() if row["name"] == first], "heuristic"

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
            self._finding(scan_id, "TB-CALL-001", "info", f"Could not statically resolve call '{call['target_expression']}'.", call["relative_path"], call["line_number"],
                          {"expression": call["target_expression"], "basis": call.get("basis"), "resolution": call["resolution"]})
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
