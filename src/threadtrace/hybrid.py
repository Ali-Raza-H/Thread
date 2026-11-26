"""Explicit, reversible experiments that never alter registered source."""

from __future__ import annotations

import ast
import asyncio
import builtins
import contextlib
import ctypes
import datetime as datetime_module
import difflib
import hashlib
import importlib
import inspect
import io
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType, ModuleType
from typing import Any, Callable, Iterator

from .hybrid_adapters import AdapterController
from .parser import DEFAULT_EXCLUDES


def create_workspace(source: Path, destination_root: Path) -> Path:
    source = source.resolve()
    destination_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = destination_root / uuid.uuid4().hex
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*DEFAULT_EXCLUDES), symlinks=True)
    return destination


def _file_metadata(path: Path) -> dict[str, Any]:
    """Hash one file in fixed-size chunks so large files are never held in memory."""
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while chunk := handle.read(65_536):
            digest.update(chunk)
            size += len(chunk)
    return {"kind": "file", "size": size, "sha256": digest.hexdigest()}


def source_revision(root: Path) -> dict[str, Any]:
    """Hash the source tree copied into an experiment without following links."""
    root = root.resolve(strict=True)
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in DEFAULT_EXCLUDES for part in relative.parts):
            continue
        name = relative.as_posix()
        if path.is_symlink():
            files[name] = {"kind": "symlink", "target": os.readlink(path)}
        elif path.is_file():
            files[name] = {**_file_metadata(path), "mode": path.stat().st_mode & 0o777}
        elif path.is_dir():
            files[name] = {"kind": "directory", "mode": path.stat().st_mode & 0o777}
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {"tree_hash": hashlib.sha256(encoded).hexdigest(), "files": files}


PREVIEW_LIMIT = 20_000


def _tree_files(root: Path) -> dict[str, dict[str, Any]]:
    """Describe every file-like workspace entry without following links."""
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in DEFAULT_EXCLUDES for part in relative.parts):
            continue
        name = relative.as_posix()
        if path.is_symlink():
            try:
                entries[name] = {"kind": "symlink", "target": os.readlink(path)}
            except OSError as exc:
                entries[name] = {"kind": "unreadable", "reason": str(exc)}
        elif path.is_file():
            try:
                entries[name] = _file_metadata(path)
            except OSError as exc:
                entries[name] = {"kind": "unreadable", "reason": str(exc)}
    return entries


def _text_preview(path: Path, limit: int = PREVIEW_LIMIT) -> tuple[str | None, bool, str | None]:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        return None, False, str(exc)
    truncated = len(data) > limit
    try:
        text = data[:limit].decode("utf-8")
    except UnicodeDecodeError:
        return None, truncated, "binary content"
    return text, truncated, None if "\x00" not in text else "binary content"


def patch_file(workspace: Path, relative_path: str, start_line: int, end_line: int, replacement: str) -> dict[str, Any]:
    """Replace a line range atomically after proving the result is valid Python."""
    if start_line < 1 or end_line < start_line:
        raise ValueError("Patch lines must be a positive inclusive range.")
    path = (workspace / relative_path).resolve()
    if not path.is_relative_to(workspace.resolve()) or not path.is_file() or path.is_symlink():
        raise ValueError("Experiment patches must target an existing file inside the workspace.")
    before = path.read_bytes()
    try:
        lines = before.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise ValueError("Experiment patches require UTF-8 text files.") from exc
    if end_line > len(lines):
        raise ValueError("Patch range is outside the selected file.")
    lines[start_line - 1:end_line] = [replacement + ("\n" if replacement and not replacement.endswith("\n") else "")]
    after = "".join(lines).encode("utf-8")
    if path.suffix == ".py":
        try:
            compile(after, str(path), "exec")
        except SyntaxError as exc:
            raise ValueError(f"Patch would leave invalid Python at line {exc.lineno}: {exc.msg}") from exc
    if after != before:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".threadtrace-patch-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(after)
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    return {"path": relative_path, "start_line": start_line, "end_line": end_line,
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(), "changed": after != before}


def workspace_changes(source: Path, workspace: Path) -> dict[str, Any]:
    """Report every workspace change with hashes, bounded previews, and reasons."""
    before, after = _tree_files(source), _tree_files(workspace)
    removed, added = set(before) - set(after), set(after) - set(before)
    changes: list[dict[str, Any]] = []
    renames: dict[str, str] = {}
    available = sorted(path for path in added if after[path].get("kind") == "file")
    for old_path in sorted(removed):
        original = before[old_path]
        if original.get("kind") != "file":
            continue
        matches = [path for path in available if after[path]["sha256"] == original["sha256"] and after[path]["size"] == original["size"]]
        if len(matches) == 1:
            renames[old_path] = matches[0]
            available.remove(matches[0])
            changes.append({"kind": "renamed", "path": matches[0], "old_path": old_path,
                            "before": original, "after": after[matches[0]], "preview": None, "preview_truncated": False})
    for path in sorted(set(before) & set(after)):
        old, new = before[path], after[path]
        if old == new or path in renames.values():
            continue
        if old.get("kind") == "unreadable" or new.get("kind") == "unreadable":
            changes.append({"kind": "unreadable", "path": path, "before": old, "after": new, "preview": None,
                            "preview_truncated": False, "reason": new.get("reason") or old.get("reason")})
            continue
        if old.get("kind") == "file" and new.get("kind") == "file":
            before_text, before_truncated, reason = _text_preview(source / path)
            after_text, after_truncated, _ = _text_preview(workspace / path)
            if reason or before_text is None or after_text is None:
                changes.append({"kind": "binary", "path": path, "before": old, "after": new, "preview": None,
                                "preview_truncated": before_truncated or after_truncated, "reason": reason or "binary content"})
                continue
            preview = "".join(difflib.unified_diff(
                before_text.splitlines(keepends=True), after_text.splitlines(keepends=True),
                f"source/{path}", f"experiment/{path}",
            ))[:PREVIEW_LIMIT]
            changes.append({"kind": "modified", "path": path, "before": old, "after": new, "preview": preview,
                            "preview_truncated": before_truncated or after_truncated or len(preview) >= PREVIEW_LIMIT})
        else:
            changes.append({"kind": "modified", "path": path, "before": old, "after": new, "preview": None,
                            "preview_truncated": False, "reason": f"entry changed from {old.get('kind')} to {new.get('kind')}"})
    for path in sorted(added - set(renames.values())):
        entry, preview, truncated, reason = after[path], None, False, None
        if entry.get("kind") == "file":
            preview, truncated, reason = _text_preview(workspace / path)
        kind = "binary" if reason else "created"
        changes.append({"kind": kind, "path": path, "before": None, "after": entry, "preview": preview,
                        "preview_truncated": truncated, "reason": reason})
    for path in sorted(removed - set(renames)):
        changes.append({"kind": "deleted", "path": path, "before": before[path], "after": None, "preview": None,
                        "preview_truncated": False})
    changes.sort(key=lambda item: (item["path"], item["kind"]))
    summary = Counter(item["kind"] for item in changes)
    return {"kind": "experiment_changes", "changes": changes, "summary": dict(summary),
            "preview_limit": PREVIEW_LIMIT}


def workspace_diff(source: Path, workspace: Path) -> str:
    rendered: list[str] = []
    for change in workspace_changes(source, workspace)["changes"]:
        heading = f"{change['old_path']} -> {change['path']}" if change["kind"] == "renamed" else change["path"]
        rendered.append(f"=== {change['kind']}: {heading}\n")
        before, after = change.get("before") or {}, change.get("after") or {}
        if before:
            rendered.append(f"before: {before.get('sha256') or before.get('target') or before.get('reason')}\n")
        if after:
            rendered.append(f"after: {after.get('sha256') or after.get('target') or after.get('reason')}\n")
        if change.get("preview"):
            rendered.append(change["preview"])
            if not change["preview"].endswith("\n"):
                rendered.append("\n")
    return "".join(rendered)


def _terminate_group(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    else:
        with contextlib.suppress(OSError):
            process.kill()


def _bounded_run(command: list[str], timeout: int, cwd: Path | None = None, input_text: str | None = None,
                 environment: dict[str, str] | None = None) -> tuple[int | None, str, str, bool]:
    """Run a child in its own process group and kill the whole group on timeout."""
    process = subprocess.Popen(
        command, cwd=cwd, env=environment or os.environ.copy(), text=True, errors="replace",
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout)
        return process.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired:
        _terminate_group(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout = stderr = ""
        return None, stdout, stderr, True


def _harness_command(interpreter: str | None) -> list[str]:
    return [interpreter or sys.executable, "-c", "from threadtrace.hybrid import _worker; _worker()"]


def _harness_environment() -> dict[str, str]:
    """Keep the harness importable by a selected interpreter outside this installation."""
    environment = os.environ.copy()
    package_root = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (package_root, environment.get("PYTHONPATH", ""))))
    return environment


def run_workspace(workspace: Path, command: list[str], timeout: int, output_cap: int) -> dict[str, Any]:
    if not command:
        raise ValueError("A command is required after '--'.")
    if timeout < 1 or output_cap < 1:
        raise ValueError("Timeout and output cap must be positive.")
    before, started = _filesystem_snapshot(workspace), time.monotonic()
    exit_code, stdout, stderr, timed_out = _bounded_run(command, timeout, workspace)
    return {"command": command, "workspace": str(workspace), "exit_code": exit_code, "timed_out": timed_out,
            "stdout": stdout[:output_cap], "stderr": stderr[:output_cap],
            "output_truncated": len(stdout) > output_cap or len(stderr) > output_cap,
            "elapsed_seconds": time.monotonic() - started,
            "side_effects": {"filesystem": _filesystem_changes(before, _filesystem_snapshot(workspace))}}


def run_harness(workspace: Path, request: dict[str, Any], timeout: int = 30, output_cap: int = 100_000,
                interpreter: str | None = None) -> dict[str, Any]:
    if timeout < 1 or output_cap < 1:
        raise ValueError("Timeout and output cap must be positive.")
    payload = {"workspace": str(workspace.resolve()), "request": {**request, "output_cap": output_cap}}
    exit_code, stdout, stderr, timed_out = _bounded_run(_harness_command(interpreter), timeout,
                                                       input_text=json.dumps(payload), environment=_harness_environment())
    if timed_out:
        return {"timed_out": True, "worker_error": "Callable harness timed out."}
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        result = {"worker_error": "Callable harness did not return valid JSON.", "worker_stdout": stdout[:output_cap]}
    if stderr:
        result["worker_stderr"] = stderr[:output_cap]
    result.update({"worker_exit_code": exit_code, "timed_out": False})
    return result


def generate_input_cases(workspace: Path, target: str, config: dict[str, Any], timeout: int,
                         output_cap: int, interpreter: str | None = None) -> list[dict[str, Any]]:
    result = run_harness(workspace, {"action": "generate", "target": target, "config": config}, timeout, output_cap,
                         interpreter)
    if result.get("exception"):
        raise ValueError(result["exception"]["message"])
    return result.get("cases", [])


def _exploration_budgets(config: dict[str, Any], timeout: int, output_cap: int) -> dict[str, Any]:
    budgets = config.get("budgets") if isinstance(config.get("budgets"), dict) else {}
    return {
        "max_cases": int(budgets.get("max_cases", 0)) or None,
        "global_timeout": float(budgets["global_timeout"]) if "global_timeout" in budgets else None,
        "case_timeout": int(min(timeout, budgets.get("case_timeout", timeout))),
        "max_events": int(budgets.get("max_events", 0)) or None,
        "max_output": int(budgets.get("max_output", output_cap)),
        "max_memory_bytes": int(budgets.get("max_memory_bytes", 0)) or None,
        "max_storage_bytes": int(budgets.get("max_storage_bytes", 0)) or None,
    }


def _has_side_effects(result: dict[str, Any]) -> bool:
    effects = result.get("side_effects") or {}
    filesystem = effects.get("filesystem") or {}
    return any(filesystem.get(key) for key in ("created", "modified", "deleted")) or bool(effects.get("external_operations"))


def _retention_reasons(result: dict[str, Any], signature: str, first: str | None) -> list[str]:
    reasons = []
    if result.get("timed_out"):
        reasons.append("timeout")
    if result.get("exception"):
        reasons.append("exception")
    if result.get("worker_error") or result.get("worker_exit_code") not in (0, None):
        reasons.append("crash")
    if _has_side_effects(result):
        reasons.append("side effects")
    if first is not None and signature != first:
        reasons.append("distinct behaviour")
    return reasons


def explore_inputs(workspace: Path, target: str, cases: list[dict[str, Any]] | None, config: dict[str, Any],
                   timeout: int, output_cap: int, interpreter: str | None = None) -> dict[str, Any]:
    limits = _exploration_budgets(config, timeout, output_cap)
    selected = cases if cases is not None else generate_input_cases(workspace, target, config, timeout, output_cap,
                                                                   interpreter)
    requested = len(selected)
    selected = selected[:limits["max_cases"]] if limits["max_cases"] else selected
    harness_config = {**config, "event_cap": min(int(config.get("event_cap", 10_000)), limits["max_events"])} \
        if limits["max_events"] else config
    deadline = time.monotonic() + limits["global_timeout"] if limits["global_timeout"] else None
    results, distinct, seen, retained = [], [], set(), []
    memory_peak = stored_bytes = 0
    stopped_reason = "case_limit" if requested > len(selected) else None
    first_signature = None
    for index, case in enumerate(selected):
        if deadline is not None and time.monotonic() >= deadline:
            stopped_reason = "global_timeout"
            break
        if not isinstance(case, dict):
            raise ValueError("Each input case must be a JSON object.")
        case_timeout = limits["case_timeout"]
        if deadline is not None:
            case_timeout = max(1, min(case_timeout, int(deadline - time.monotonic())))
        request = {"target": target, "args": case.get("args", []), "kwargs": case.get("kwargs", {}),
                   "config": harness_config}
        result = run_harness(workspace, request, case_timeout, limits["max_output"], interpreter)
        memory_peak = max(memory_peak, int(result.get("memory_bytes") or 0))
        record = {"index": index, "case": case, "result": result}
        results.append(record)
        stored_bytes += len(json.dumps(record, default=str))
        signature = json.dumps(_behavior_signature(result), sort_keys=True, default=str)
        if signature not in seen:
            reasons = _retention_reasons(result, signature, first_signature)
            first_signature = first_signature or signature
            seen.add(signature)
            distinct.append(record)
            if reasons:
                retained.append({"index": index, "case": case, "reasons": reasons, "signature": signature})
        if limits["max_memory_bytes"] and memory_peak > limits["max_memory_bytes"]:
            stopped_reason = "memory"
            break
        if limits["max_storage_bytes"] and stored_bytes > limits["max_storage_bytes"]:
            stopped_reason = "storage"
            break
    return {"target": target, "generated": cases is None, "results": results, "distinct_cases": distinct,
            "retained": retained,
            "crashes": [item for item in results if item["result"].get("exception") or item["result"].get("timed_out")
                        or item["result"].get("worker_error")],
            "budgets": {"requested_cases": requested, "executed_cases": len(results),
                        "cases_truncated": requested > len(selected), "case_timeout": limits["case_timeout"],
                        "output_cap": limits["max_output"], "event_cap": harness_config.get("event_cap"),
                        "max_memory_bytes": limits["max_memory_bytes"], "max_storage_bytes": limits["max_storage_bytes"],
                        "memory_peak_bytes": memory_peak, "stored_bytes": stored_bytes,
                        "stopped_reason": stopped_reason},
            "experimental": True}


def edge_cases() -> list[dict[str, Any]]:
    """Generic presets retained for callers without a resolvable signature."""
    return [{"label": "none", "args": [None]}, {"label": "empty string", "args": [""]},
            {"label": "negative", "args": [-1]}, {"label": "zero", "args": [0]},
            {"label": "large integer", "args": [10 ** 12]}, {"label": "empty list", "args": [[]]},
            {"label": "empty object", "args": [{}]}, {"label": "unexpected string", "args": ["unexpected"]}]


def _canonical_target(target: str) -> str:
    return target.replace(":", ".")


def validate_experiment_config(config: dict[str, Any]) -> None:
    """Validate a saved definition without importing or executing its project."""
    for name in ("patches", "substitutions", "disabled_modules", "line_overrides", "input_generators"):
        if name in config and not isinstance(config[name], list):
            raise ValueError(f"{name} must be a list.")
    for name in ("mocks", "overrides", "argument_overrides", "adapters", "fixtures", "environment"):
        if name in config and not isinstance(config[name], dict):
            raise ValueError(f"{name} must be an object.")
    for name in ("patches", "substitutions"):
        targets = [rule.get("target") for rule in config.get(name, []) if isinstance(rule, dict)]
        if len(targets) != len(config.get(name, [])) or any(not target for target in targets):
            raise ValueError(f"Every {name[:-1]} rule needs a target.")
        if len(targets) != len({_canonical_target(target) for target in targets}):
            raise ValueError(f"Conflicting {name} target the same attribute.")
    collisions = ({_canonical_target(rule["target"]) for rule in config.get("patches", [])}
                  & {_canonical_target(rule["target"]) for rule in config.get("substitutions", [])})
    if collisions:
        raise ValueError(f"Patches and substitutions target the same attribute: {', '.join(sorted(collisions))}.")
    line_overrides = config.get("line_overrides", [])
    for rule in line_overrides:
        if not isinstance(rule, dict) or not isinstance(rule.get("line"), int) or rule["line"] < 1:
            raise ValueError("Every line override needs a positive line number.")
        for field in ("locals", "globals"):
            if field in rule and not isinstance(rule[field], dict):
                raise ValueError(f"Line override {field} must be an object.")
    selectors = [(rule.get("path"), rule["line"], rule.get("function")) for rule in line_overrides]
    if len(selectors) != len(set(selectors)):
        raise ValueError("Conflicting line overrides target the same path, line, and function.")
    for rule in config.get("patches", []):
        code = rule.get("code")
        if not isinstance(code, str):
            raise ValueError("Every patch needs replacement code.")
        try:
            compile(code, f"<ThreadTrace patch {rule['target']}>", "exec")
        except SyntaxError as exc:
            raise ValueError(f"Patch syntax error at replacement line {exc.lineno}: {exc.msg}") from exc
    overlap = set(config.get("disabled_modules", [])) & set(config.get("mocks", {}))
    if overlap:
        raise ValueError(f"Modules cannot be mocked and disabled together: {', '.join(sorted(overlap))}.")
    unknown_adapters = set(config.get("adapters", {})) - {"http", "database", "filesystem", "network", "process"}
    if unknown_adapters:
        raise ValueError(f"Unsupported adapter(s): {', '.join(sorted(unknown_adapters))}.")
    selector_fields = {"http": ("method", "url_contains"), "database": ("query_contains",),
                       "filesystem": ("operation", "path_contains"), "network": ("address_contains",),
                       "process": ("command_contains",)}
    for adapter, raw_rules in config.get("adapters", {}).items():
        rules = raw_rules if isinstance(raw_rules, list) else [raw_rules]
        if not all(isinstance(rule, dict) for rule in rules):
            raise ValueError(f"{adapter} adapter rules must be objects.")
        adapter_selectors = [tuple(rule.get(field) for field in selector_fields[adapter]) for rule in rules]
        if len(adapter_selectors) != len(set(adapter_selectors)):
            raise ValueError(f"Conflicting {adapter} adapter rules use the same selector.")
        for rule in rules:
            if "latency" in rule and (not isinstance(rule["latency"], (int, float)) or isinstance(rule["latency"], bool)
                                       or rule["latency"] < 0):
                raise ValueError(f"{adapter} adapter latency must be a non-negative number.")
            if "pass_through" in rule and not isinstance(rule["pass_through"], bool):
                raise ValueError(f"{adapter} adapter pass_through must be true or false.")
            if rule.get("pass_through") and (rule.get("error") or rule.get("block")):
                raise ValueError(f"{adapter} adapter rules cannot pass through and fail or block together.")
    for generator in config.get("input_generators", []):
        if not isinstance(generator, dict) or not isinstance(generator.get("expression"), str) or not generator["expression"].strip():
            raise ValueError("Every input generator needs an expression.")
        try:
            compile(generator["expression"], "<ThreadTrace input generator>", "eval")
        except SyntaxError as exc:
            raise ValueError(f"Input generator syntax error: {exc.msg}") from exc
    if "budgets" in config:
        budgets = config["budgets"]
        if not isinstance(budgets, dict):
            raise ValueError("budgets must be an object.")
        unknown_budgets = set(budgets) - {"max_cases", "global_timeout", "case_timeout", "max_events",
                                          "max_output", "max_memory_bytes", "max_storage_bytes"}
        if unknown_budgets:
            raise ValueError(f"Unsupported budget field(s): {', '.join(sorted(unknown_budgets))}.")
        for name, value in budgets.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"budgets.{name} must be a positive number.")
    if "seed" in config and not isinstance(config["seed"], (int, float, str, bytes)):
        raise ValueError("seed must be an int, float, string, or bytes.")
    if "interpreter" in config and (not isinstance(config["interpreter"], str) or not config["interpreter"].strip()):
        raise ValueError("interpreter must be a non-empty executable path.")
    if "freeze_time" in config:
        float(config["freeze_time"])
    if "uuid" in config:
        uuid.UUID(str(config["uuid"]))
    for name in ("yield_cap", "event_cap"):
        if name in config and (not isinstance(config[name], int) or config[name] < 1):
            raise ValueError(f"{name} must be a positive integer.")


def replay_report(config: dict[str, Any]) -> dict[str, Any]:
    """Record controlled inputs and every nondeterministic input left uncontrolled."""
    controlled: list[dict[str, Any]] = []
    if "seed" in config:
        controlled.append({"input": "random", "control": f"random.seed({config['seed']})",
                           "scope": "module-level random functions"})
    if "freeze_time" in config:
        controlled.append({"input": "time", "control": f"time.time() and datetime now/today return {float(config['freeze_time'])}",
                           "scope": "time and datetime module attributes plus loaded project aliases"})
    if "uuid" in config:
        controlled.append({"input": "uuid", "control": f"uuid.uuid4() returns {config['uuid']}",
                           "scope": "uuid.uuid4 module attribute plus loaded project aliases"})
    if config.get("environment"):
        controlled.append({"input": "environment", "control": f"set or removed for the run: {', '.join(sorted(config['environment']))}",
                           "scope": "process environment"})
    uncontrolled = [
        {"input": "thread, process, and async scheduling", "reason": "Interleaving is observed, not replayed."},
        {"input": "random.Random and third-party generator instances", "reason": "Seeding module functions does not reset existing instances."},
        {"input": "os.urandom, secrets, and hash randomisation", "reason": "OS entropy and interpreter hash seeds are not intercepted."},
        {"input": "time.monotonic, perf_counter, and process time", "reason": "Only time.time and datetime class methods are frozen."},
        {"input": "network, database, subprocess, and filesystem state", "reason": "Observed for comparison but replayed only when an adapter rule replaces it."},
        {"input": "object identity, addresses, and garbage collection", "reason": "They may differ between runs and are compared by value where possible."},
        {"input": "native extension state", "reason": "Python-level controls cannot observe C-level nondeterminism."},
    ]
    if "seed" not in config:
        uncontrolled.insert(0, {"input": "random", "reason": "No seed is configured."})
    return {"controlled": controlled, "uncontrolled": uncontrolled, "fully_controlled": not uncontrolled}


def _prebound_aliases(workspace: Path, targets: list[str]) -> list[dict[str, Any]]:
    """Find project imports that bind a rule target before the rule is applied."""
    modules: dict[str, list[str]] = {}
    for target in targets:
        module, attribute = target.split(":", 1) if ":" in target else target.rsplit(".", 1)
        modules.setdefault(module, []).append(attribute)
    found: list[dict[str, Any]] = []
    for path in sorted(workspace.rglob("*.py")):
        if path.is_symlink() or any(part in DEFAULT_EXCLUDES for part in path.relative_to(workspace).parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level or node.module not in modules:
                continue
            for alias in node.names:
                if alias.name in modules[node.module]:
                    found.append({"target": f"{node.module}:{alias.name}", "module": node.module,
                                  "path": path.relative_to(workspace).as_posix(),
                                  "line": node.lineno, "bound_as": alias.asname or alias.name})
    return found


def experiment_preflight(workspace: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Validate an experiment and describe its rules without importing or executing the project."""
    validate_experiment_config(config)
    patches = {rule["target"] for rule in config.get("patches", [])}
    substitutions = {rule["target"] for rule in config.get("substitutions", [])}
    overrides = set(config.get("overrides", {}))
    normalized = {name: {_canonical_target(target) for target in targets}
                  for name, targets in (("override", overrides), ("patch", patches), ("substitution", substitutions))}
    shadowed = [{"target": target, "winner": "override",
                 "shadowed_rule": "patch" if _canonical_target(target) in normalized["patch"] else "substitution"}
                for target in sorted(overrides)
                if _canonical_target(target) in normalized["patch"] | normalized["substitution"]]
    interpreter = config.get("interpreter")
    return {
        "status": "ready",
        "rules": {"patches": sorted(patches), "substitutions": sorted(substitutions),
                  "overrides": sorted(overrides), "adapters": sorted(config.get("adapters", {})),
                  "line_overrides": len(config.get("line_overrides", []))},
        "precedence": [
            {"level": 1, "rule": "explicit per-call/per-experiment override", "wins_over": "named patch/substitution"},
            {"level": 2, "rule": "named patch/substitution", "wins_over": "adapter mock/replay"},
            {"level": 3, "rule": "adapter mock/replay", "wins_over": "original behaviour"},
            {"level": 4, "rule": "original behaviour", "wins_over": None},
        ],
        "shadowed": shadowed,
        "prebound_aliases": _prebound_aliases(workspace, sorted(patches | substitutions | overrides)),
        "interpreter": {"executable": interpreter or sys.executable,
                        "available": Path(interpreter).is_file() if interpreter else True},
        "limits": [
            "Rules apply to fresh imports, the selected callable, and identity-matched loaded project modules; already-active frames cannot change.",
            "Line overrides reach traced project lines but may miss closure cells and optimised locals.",
            "Capture caps mark truncation, and truncated comparisons report unknown instead of equality.",
        ],
        "replay": replay_report(config),
    }


def compare_runs(baseline: dict[str, Any], modified: dict[str, Any], float_tolerance: float = 1e-9) -> dict[str, Any]:
    fields = ("return_value", "exception", "stdout", "stderr", "state", "state_changes", "argument_changes",
              "global_changes", "side_effects", "interactions", "trace")
    differences, unknown = [], []
    for name in fields:
        comparison = _structural_compare(baseline.get(name), modified.get(name), float_tolerance)
        if comparison == "different":
            differences.append({"field": name, "baseline": baseline.get(name), "modified": modified.get(name)})
        elif comparison == "unknown":
            unknown.append({"field": name, "reason": "Values cannot be compared reliably."})
    baseline_time, modified_time = baseline.get("elapsed_seconds"), modified.get("elapsed_seconds")
    timing = {"baseline_seconds": baseline_time, "modified_seconds": modified_time, "comparable": False}
    if isinstance(baseline_time, (int, float)) and isinstance(modified_time, (int, float)):
        timing.update({"comparable": True, "difference_seconds": modified_time - baseline_time})
    if baseline.get("output_truncated") or modified.get("output_truncated"):
        unknown.append({"field": "stdout/stderr", "reason": "Captured output was truncated."})
    if baseline.get("trace_truncated") or modified.get("trace_truncated"):
        unknown.append({"field": "trace", "reason": "Trace events were truncated."})
    if baseline.get("collection_truncated") or modified.get("collection_truncated"):
        unknown.append({"field": "return_value", "reason": "Returned collections hit the harness cap."})
    return {"same_behaviour": not differences and not unknown, "differences": differences,
            "unknown_comparisons": unknown, "timing": timing,
            "baseline": _comparison_view(baseline), "modified": _comparison_view(modified)}


@dataclass
class TraceController:
    workspace: Path
    rules: list[dict[str, Any]]
    interactions: list[dict[str, Any]]
    pause_handler: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
    event_cap: int = 10_000
    events: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    _previous: Any = None

    def __enter__(self) -> TraceController:
        self._previous = sys.gettrace()
        sys.settrace(self._trace)
        return self

    def __exit__(self, *_args: Any) -> None:
        sys.settrace(self._previous)

    def _trace(self, frame: FrameType, event: str, arg: Any) -> Any:
        if frame.f_code.co_filename.startswith("<"):
            return self._trace
        try:
            resolved = Path(frame.f_code.co_filename).resolve()
            if not resolved.is_file():
                return self._trace
            relative = resolved.relative_to(self.workspace).as_posix()
        except (OSError, ValueError):
            return self._trace
        if event in {"call", "line", "return", "exception"}:
            item = {"event": event, "path": relative, "line": frame.f_lineno, "function": frame.f_code.co_name}
            if event == "line":
                item["locals"] = _safe_value(dict(frame.f_locals))
                self._apply_rules(frame, relative)
            elif event == "return":
                item["value"] = _safe_value(arg)
            elif event == "exception":
                item["exception"] = getattr(arg[0], "__name__", str(arg[0]))
            if len(self.events) < self.event_cap:
                self.events.append(item)
            else:
                self.truncated = True
        return self._trace

    def _apply_rules(self, frame: FrameType, relative: str) -> None:
        applicable = [rule for rule in self.rules if rule.get("line") == frame.f_lineno and
                      (not rule.get("path") or rule["path"] == relative) and
                      (not rule.get("function") or rule["function"] == frame.f_code.co_name)]
        if self.pause_handler:
            supplied = self.pause_handler({"path": relative, "line": frame.f_lineno, "function": frame.f_code.co_name,
                                           "locals": _safe_value(dict(frame.f_locals))})
            if supplied:
                applicable.append({"locals": supplied})
        for rule in applicable:
            local_values, global_values = rule.get("locals", {}), rule.get("globals", {})
            if not isinstance(local_values, dict) or not isinstance(global_values, dict):
                raise ValueError("Line override locals/globals must be objects.")
            frame.f_locals.update(local_values)
            frame.f_globals.update(global_values)
            _sync_frame_locals(frame)
            self.interactions.append({"kind": "line_override", "path": relative, "line": frame.f_lineno,
                                      "locals": _safe_value(local_values), "globals": _safe_value(global_values)})


@dataclass
class HybridHarness:
    workspace: Path
    config: dict[str, Any] = field(default_factory=dict)
    output_cap: int = 100_000
    pause_handler: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
    instances: dict[str, Any] = field(default_factory=dict)
    fixtures: dict[str, Any] = field(default_factory=dict)
    interactions: list[dict[str, Any]] = field(default_factory=list)
    recorded_returns: dict[str, Any] = field(default_factory=dict)
    _operation_restores: list[tuple[Any, str, Any]] = field(default_factory=list)
    _mock_parent_restores: list[tuple[Any, str, Any]] = field(default_factory=list)
    _setup_complete: bool = False
    _adapters: AdapterController | None = None
    preflight: dict[str, Any] = field(default_factory=dict)
    _replay_aliases: list[tuple[Any, Any, dict[str, Any]]] = field(default_factory=list)

    def call(self, target: str, args: list[Any] | None = None, kwargs: dict[str, Any] | None = None,
             per_call: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            with self._controlled_environment(per_call):
                return self._call_loaded(target, args, kwargs)
        except BaseException as exc:
            return {**_failed_result(exc, self.interactions), "preflight": self.preflight,
                    "replay": replay_report(self.config)}

    def execute(self, operations: list[dict[str, Any]], per_call: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        try:
            with self._controlled_environment(per_call):
                for operation in operations:
                    results.append(self._operation(operation, results))
        except BaseException as exc:
            results.append({**_failed_result(exc, self.interactions), "preflight": self.preflight,
                            "replay": replay_report(self.config)})
        for result in results:
            result.pop("_raw_return", None)
        return results

    def generate_cases(self, target: str) -> list[dict[str, Any]]:
        with self._controlled_environment():
            signature = inspect.signature(self._resolve(target))
            parameters = [parameter for parameter in signature.parameters.values()
                          if parameter.kind not in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}]
            base = {parameter.name: parameter.default if parameter.default is not inspect.Parameter.empty else _default_input(parameter)
                    for parameter in parameters}
            cases = [{"label": "nominal", "kwargs": _safe_value(base)}]
            for parameter in parameters:
                for label, value in _boundary_values(parameter):
                    values = dict(base)
                    values[parameter.name] = value
                    cases.append({"label": f"{parameter.name}: {label}", "kwargs": _safe_value(values)})
            for generator in self.config.get("input_generators", []):
                if not isinstance(generator, dict) or not isinstance(generator.get("expression"), str):
                    raise ValueError("Each input generator needs an expression.")
                generated = eval(generator["expression"], {"__builtins__": _safe_builtins()}, dict(self.fixtures))
                for index, value in enumerate(generated):
                    case = value if isinstance(value, dict) else {"args": [value]}
                    cases.append({"label": f"generator {index + 1}", **case,
                                  "generator": "advanced expression", "execution": "explicit experiment execution"})
            return cases

    def capability(self, target: str) -> dict[str, Any]:
        with self._controlled_environment():
            return _callable_capability(self._resolve(target), target)

    def _operation(self, operation: dict[str, Any], prior: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(operation, dict):
            raise ValueError("Each harness operation must be an object.")
        kind = operation.get("kind", "call")
        if kind == "call":
            return self._call_loaded(operation.get("target", ""), operation.get("args"), operation.get("kwargs"), prior)
        if kind == "construct":
            name = operation.get("name", "")
            if not name or not str(name).replace("_", "a").isalnum():
                raise ValueError("Instance names may contain only letters, digits, and underscores.")
            result = self._call_loaded(operation.get("target", ""), operation.get("args"), operation.get("kwargs"), prior)
            if result.get("exception") is None:
                self.instances[name] = result.pop("_raw_return")
                result.update({"instance": name, "state": _safe_value(self.instances[name])})
            return result
        if kind == "call_instance":
            name, method = operation.get("instance", ""), operation.get("method", "")
            if name not in self.instances:
                raise ValueError(f"No persistent instance named '{name}'.")
            if not method or str(method).startswith("_"):
                raise ValueError("A public method name is required.")
            return self._invoke(getattr(self.instances[name], method), operation.get("args"), operation.get("kwargs"),
                                self.instances[name], prior)
        if kind == "set_instance":
            name, attribute = operation.get("instance", ""), operation.get("attribute", "")
            if name not in self.instances or not attribute or str(attribute).startswith("_"):
                raise ValueError("set_instance needs a named instance and public attribute.")
            setattr(self.instances[name], attribute, self._decode(operation.get("value"), prior))
            self.interactions.append({"kind": "instance_override", "instance": name, "attribute": attribute})
            return {"instance": name, "state": _safe_value(self.instances[name]), "exception": None}
        if kind == "set_global":
            owner, name = _owner_and_name(operation.get("target", ""), self.workspace)
            if not any(saved_owner is owner and saved_name == name for saved_owner, saved_name, _ in self._operation_restores):
                self._operation_restores.append((owner, name, inspect.getattr_static(owner, name)))
            setattr(owner, name, self._decode(operation.get("value"), prior))
            return {"updated": operation["target"], "value": _safe_value(getattr(owner, name)), "exception": None}
        if kind == "inspect_instance":
            name = operation.get("instance", "")
            if name not in self.instances:
                raise ValueError(f"No persistent instance named '{name}'.")
            return {"instance": name, "state": _safe_value(self.instances[name]), "exception": None}
        if kind == "get_instance":
            name, attribute = operation.get("instance", ""), operation.get("attribute", "")
            if name not in self.instances or not attribute or str(attribute).startswith("_"):
                raise ValueError("get_instance needs a named instance and public attribute.")
            return {"instance": name, "attribute": attribute,
                    "value": _safe_value(getattr(self.instances[name], attribute)), "exception": None}
        raise ValueError(f"Unknown harness operation '{kind}'.")

    def _call_loaded(self, target: str, args: list[Any] | None, kwargs: dict[str, Any] | None,
                     prior: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        callable_object = self._resolve(target)
        if self._adapters:
            self._adapters.refresh_aliases()
        decoded_args = [self._decode(item, prior or []) for item in (args or [])]
        decoded_kwargs = {name: self._decode(value, prior or []) for name, value in (kwargs or {}).items()}
        decoded_args, decoded_kwargs = self._overridden_arguments(target, decoded_args, decoded_kwargs)
        result = self._invoke(callable_object, decoded_args, decoded_kwargs, None, prior)
        result["target"] = target
        return result

    def _invoke(self, callable_object: Any, args: Any, kwargs: Any, state_object: Any = None,
                prior: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not callable(callable_object):
            raise TypeError("The selected target is not callable.")
        decoded_args = [self._decode(item, prior or []) for item in (args or [])]
        decoded_kwargs = {name: self._decode(value, prior or []) for name, value in (kwargs or {}).items()}
        capability, stdout, stderr = _callable_capability(callable_object), io.StringIO(), io.StringIO()
        module, files_before = inspect.getmodule(callable_object), _filesystem_snapshot(self.workspace)
        globals_before, started = _module_state(module), time.monotonic()
        arguments_before, state_before = _safe_value({"args": decoded_args, "kwargs": decoded_kwargs}), _safe_value(state_object)
        tracer = TraceController(self.workspace, self.config.get("line_overrides", []), self.interactions,
                                 self.pause_handler, int(self.config.get("event_cap", 10_000)))
        exception, value = None, None
        try:
            with tracer, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                value, collection_truncated = _complete_value(callable_object(*decoded_args, **decoded_kwargs),
                                                             int(self.config.get("yield_cap", 1_000)))
        except BaseException as exc:
            exception = exc
        result = {"return_value": _safe_value(value) if exception is None else None,
                  "collection_truncated": exception is None and collection_truncated,
                  "exception": _exception_value(exception) if exception else None, **self._output(stdout.getvalue(), stderr.getvalue()),
                  "state": _safe_value(state_object) if state_object is not None else None,
                  "state_changes": {"before": state_before, "after": _safe_value(state_object)} if state_object is not None else None,
                  "argument_changes": {"before": arguments_before,
                                       "after": _safe_value({"args": decoded_args, "kwargs": decoded_kwargs})},
                  "global_changes": _dictionary_changes(globals_before, _module_state(module)),
                  "side_effects": {"filesystem": _filesystem_changes(files_before, _filesystem_snapshot(self.workspace)),
                                   "external_operations": [item for item in self.interactions if item.get("kind") == "adapter"]},
                  "interactions": list(self.interactions), "trace": tracer.events,
                  "trace_truncated": tracer.truncated,
                  "elapsed_seconds": time.monotonic() - started, "memory_bytes": _memory_peak(), "capability": capability,
                  "preflight": self.preflight, "replay": replay_report(self.config),
                  "context": {"signature": capability["signature"], "args": _safe_value(decoded_args),
                              "kwargs": _safe_value(decoded_kwargs), "fixtures": sorted(self.fixtures),
                              "mocked_modules": sorted(self.config.get("mocks", {})),
                              "disabled_modules": sorted(self.config.get("disabled_modules", [])),
                              "dependencies": _dependency_context(callable_object)}}
        if exception is None:
            result["_raw_return"] = value
            self.recorded_returns[str(len(self.recorded_returns))] = value
        return result

    def _output(self, stdout: str, stderr: str) -> dict[str, Any]:
        return {"stdout": stdout[:self.output_cap], "stderr": stderr[:self.output_cap],
                "output_truncated": len(stdout) > self.output_cap or len(stderr) > self.output_cap}

    def _resolve(self, target: str) -> Any:
        module_name, attribute = _target_parts(target, self.workspace)
        value: Any = importlib.import_module(module_name)
        for part in attribute.split("."):
            if not part or part.startswith("_"):
                raise ValueError("Targets must name public module attributes.")
            value = getattr(value, part)
        return value

    def _decode(self, value: Any, prior: list[dict[str, Any]]) -> Any:
        if isinstance(value, list):
            return [self._decode(item, prior) for item in value]
        if not isinstance(value, dict):
            return value
        if "$instance" in value:
            return self.instances[value["$instance"]]
        if "$fixture" in value:
            return self.fixtures[value["$fixture"]]
        if "$recorded" in value:
            return self.recorded_returns[str(value["$recorded"])]
        if "$operation" in value:
            return prior[int(value["$operation"])].get("_raw_return")
        if "$expr" in value:
            if not self.config.get("allow_expressions"):
                raise ValueError("Advanced expressions require allow_expressions in the experiment configuration.")
            return eval(str(value["$expr"]), {"__builtins__": _safe_builtins()}, {**self.fixtures, **self.instances, "Path": Path})
        if "$type" in value:
            content = self._decode(value.get("value"), prior)
            constructors = {"tuple": tuple, "set": set, "frozenset": frozenset,
                            "bytes": lambda item: str(item).encode(), "path": Path, "complex": complex}
            if value["$type"] not in constructors:
                raise ValueError(f"Unsupported typed input '{value['$type']}'.")
            return constructors[value["$type"]](content)
        return {name: self._decode(item, prior) for name, item in value.items()}

    def _overridden_arguments(self, target: str, args: list[Any], kwargs: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        rules = self.config.get("argument_overrides", {})
        override = rules.get(target, rules.get("*", {})) if isinstance(rules, dict) else {}
        if not isinstance(override, dict):
            raise ValueError("Argument overrides must be objects.")
        if "args" in override:
            args = [self._decode(item, []) for item in override["args"]]
        if "kwargs" in override:
            kwargs.update({name: self._decode(value, []) for name, value in override["kwargs"].items()})
        return args, kwargs

    @contextlib.contextmanager
    def _controlled_environment(self, per_call: dict[str, Any] | None = None) -> Iterator[None]:
        workspace, original_config = self.workspace.resolve(), self.config
        previous_cwd, old_path, original_modules, originals, old_environment = Path.cwd(), list(sys.path), {}, [], {}
        modules_before, adapters = set(sys.modules), None
        if per_call:
            self.config = _merge_config(self.config, per_call)
        try:
            os.chdir(workspace)
            sys.path.insert(0, str(workspace))
            self._validate_config()
            self.preflight = experiment_preflight(workspace, self.config)
            self._install_modules(original_modules)
            self._apply_environment(old_environment)
            random_state = random.getstate()
            if "seed" in self.config:
                random.seed(self.config["seed"])
            self._apply_replay_controls(originals)
            self._run_setup()
            self._refresh_replay_aliases(originals)
            adapters = AdapterController(workspace, self.config.get("adapters", {}), self.interactions)
            self._adapters = adapters.__enter__()
            self._apply_rules(originals, "patches")
            self._apply_rules(originals, "substitutions")
            self._apply_overrides(originals)
            self._report_aliases()
            yield
        finally:
            self._adapters = None
            for owner, name, original in reversed(self._operation_restores):
                setattr(owner, name, original)
            self._operation_restores.clear()
            for owner, name, original in reversed(self._mock_parent_restores):
                if original is _MISSING:
                    delattr(owner, name)
                else:
                    setattr(owner, name, original)
            self._mock_parent_restores.clear()
            for owner, name, original in reversed(originals):
                setattr(owner, name, original)
            if adapters is not None:
                adapters.__exit__(None, None, None)
            for name, previous in original_modules.items():
                sys.modules.pop(name, None) if previous is _MISSING else sys.modules.__setitem__(name, previous)
            for name, previous in old_environment.items():
                os.environ.pop(name, None) if previous is None else os.environ.__setitem__(name, previous)
            if "random_state" in locals():
                random.setstate(random_state)
            self.config, sys.path[:] = original_config, old_path
            for name in set(sys.modules) - modules_before:
                module = sys.modules.get(name)
                if isinstance(module, ModuleType) and _project_module(module, workspace):
                    sys.modules.pop(name, None)
            os.chdir(previous_cwd)

    def _validate_config(self) -> None:
        validate_experiment_config(self.config)

    def _install_modules(self, originals: dict[str, Any]) -> None:
        for name in self.config.get("disabled_modules", []):
            originals[name] = sys.modules.get(name, _MISSING)
            sys.modules[name] = None
            self.interactions.append({"kind": "disabled_module", "target": name})
        for name, attributes in self.config.get("mocks", {}).items():
            if not isinstance(attributes, dict):
                raise ValueError("Mock modules must map attribute names to values or rules.")
            originals[name] = sys.modules.get(name, _MISSING)
            module = ModuleType(name)
            for attribute, value in attributes.items():
                setattr(module, attribute, _mock_value(value, self.interactions, f"{name}.{attribute}", self.recorded_returns))
            sys.modules[name] = module
            if "." in name:
                parent_name, child_name = name.rsplit(".", 1)
                if parent_name not in sys.modules:
                    originals[parent_name] = _MISSING
                    parent = ModuleType(parent_name)
                    parent.__path__ = []
                    sys.modules[parent_name] = parent
                parent = sys.modules[parent_name]
                self._mock_parent_restores.append((parent, child_name, getattr(parent, child_name, _MISSING)))
                setattr(parent, child_name, module)

    def _apply_environment(self, originals: dict[str, str | None]) -> None:
        for name, value in self.config.get("environment", {}).items():
            originals[name] = os.environ.get(name)
            os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, str(value))

    def _apply_replay_controls(self, originals: list[tuple[Any, str, Any]]) -> None:
        self._replay_aliases = []
        if "freeze_time" in self.config:
            timestamp = float(self.config["freeze_time"])
            original_time = time.time
            _replace(originals, time, "time", lambda: timestamp)
            frozen = datetime_module.datetime.fromtimestamp(timestamp, datetime_module.UTC)
            original_datetime, original_date = datetime_module.datetime, datetime_module.date

            class FrozenDateTime(original_datetime):
                @classmethod
                def now(cls, tz: Any = None) -> Any:
                    return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

                @classmethod
                def utcnow(cls) -> Any:
                    return frozen.replace(tzinfo=None)

            class FrozenDate(original_date):
                @classmethod
                def today(cls) -> Any:
                    return frozen.date()

            _replace(originals, datetime_module, "datetime", FrozenDateTime)
            _replace(originals, datetime_module, "date", FrozenDate)
            interaction = {"kind": "replay_control", "target": "time", "value": timestamp, "aliases": []}
            self.interactions.append(interaction)
            self._replay_aliases.extend([(original_time, time.time, interaction),
                                         (original_datetime, FrozenDateTime, interaction),
                                         (original_date, FrozenDate, interaction)])
            self._refresh_replay_aliases(originals)
        if "uuid" in self.config:
            value = uuid.UUID(str(self.config["uuid"]))
            original_uuid4 = uuid.uuid4
            _replace(originals, uuid, "uuid4", lambda: value)
            interaction = {"kind": "replay_control", "target": "uuid.uuid4", "value": str(value), "aliases": []}
            self.interactions.append(interaction)
            self._replay_aliases.append((original_uuid4, uuid.uuid4, interaction))
            self._refresh_replay_aliases(originals)

    def _refresh_replay_aliases(self, originals: list[tuple[Any, str, Any]]) -> None:
        for original, replacement, interaction in self._replay_aliases:
            interaction["aliases"].extend(_replace_aliases(original, replacement, self.workspace, originals))

    def _report_aliases(self) -> None:
        resolved = {alias for interaction in self.interactions for alias in interaction.get("aliases", [])}
        self.preflight["resolved_aliases"] = sorted(resolved)
        self.preflight["unresolved_prebound_aliases"] = [
            item for item in self.preflight.get("prebound_aliases", [])
            if f"{item['module']}:{item['bound_as']}" not in resolved
        ]

    def _run_setup(self) -> None:
        if self._setup_complete:
            return
        code = self.config.get("setup_code")
        if code:
            namespace = {"Path": Path, "workspace": self.workspace}
            exec(compile(str(code), "<ThreadTrace harness setup>", "exec"), namespace)
            self.fixtures.update({name: value for name, value in namespace.items() if not name.startswith("_")})
        self.fixtures.update(self.config.get("fixtures", {}))
        self._setup_complete = True

    def _apply_rules(self, originals: list[tuple[Any, str, Any]], field_name: str) -> None:
        for rule in self.config.get(field_name, []):
            owner, name = _owner_and_name(rule["target"], self.workspace)
            original, current = inspect.getattr_static(owner, name), getattr(owner, name)
            if field_name == "patches":
                replacement = _compile_patch(rule, current)
                if isinstance(original, staticmethod):
                    replacement = staticmethod(replacement)
                elif isinstance(original, classmethod):
                    replacement = classmethod(replacement)
                elif isinstance(original, property):
                    replacement = property(replacement, original.fset, original.fdel, original.__doc__)
                kind = "patch"
            else:
                replacement, kind = _substitution(rule, current, self.interactions, self.recorded_returns), "substitution"
            originals.append((owner, name, original))
            setattr(owner, name, replacement)
            aliases = _replace_aliases(current, getattr(owner, name), self.workspace, originals)
            self.interactions.append({"kind": kind, "target": rule["target"], "aliases": aliases,
                                      "alias_scope": "fresh imports and identity-matched loaded project modules",
                                      "source": rule.get("source") or _source_location(current)})

    def _apply_overrides(self, originals: list[tuple[Any, str, Any]]) -> None:
        for target, value in self.config.get("overrides", {}).items():
            owner, name = _owner_and_name(target, self.workspace)
            current, replacement = getattr(owner, name), self._decode(value, [])
            originals.append((owner, name, inspect.getattr_static(owner, name)))
            setattr(owner, name, replacement)
            aliases = _replace_aliases(current, replacement, self.workspace, originals)
            self.interactions.append({"kind": "override", "target": target, "value": _safe_value(value),
                                      "aliases": aliases})


_MISSING = object()


def _target_parts(target: str, workspace: Path) -> tuple[str, str]:
    if ":" in target:
        module_name, attribute = target.split(":", 1)
        if module_name and attribute:
            return module_name, attribute
    pieces = target.split(".")
    for index in range(len(pieces) - 1, 0, -1):
        candidate = workspace.joinpath(*pieces[:index])
        if candidate.with_suffix(".py").is_file() or (candidate / "__init__.py").is_file():
            return ".".join(pieces[:index]), ".".join(pieces[index:])
    raise ValueError("Target must use module:attribute, for example app.core:Service.process.")


def _owner_and_name(target: str, workspace: Path) -> tuple[Any, str]:
    module_name, attribute = _target_parts(target, workspace)
    parts, owner = attribute.split("."), importlib.import_module(module_name)
    for part in parts[:-1]:
        owner = getattr(owner, part)
    if not parts[-1] or parts[-1].startswith("_"):
        raise ValueError("Only public attributes can be changed.")
    return owner, parts[-1]


def _compile_patch(rule: dict[str, Any], original: Any) -> Any:
    if not isinstance(rule.get("code"), str):
        raise ValueError("Temporary patch code must be a string.")
    namespace = getattr(original, "__globals__", {}).copy()
    try:
        exec(compile(rule["code"], f"<ThreadTrace patch {rule['target']}>", "exec"), namespace)
    except SyntaxError as exc:
        raise ValueError(f"Patch syntax error at replacement line {exc.lineno}: {exc.msg}") from exc
    name = rule["target"].rsplit(".", 1)[-1].rsplit(":", 1)[-1]
    replacement = namespace.get("replacement", namespace.get(name))
    if replacement is None:
        raise ValueError("Patch code must define 'replacement' or the target attribute name.")
    if callable(original) and not callable(replacement):
        raise ValueError("A callable target requires a callable replacement.")
    return replacement


def _substitution(rule: dict[str, Any], original: Any, interactions: list[dict[str, Any]], recorded: dict[str, Any]) -> Any:
    mode = rule.get("mode", "return")
    if mode == "value":
        return rule.get("value")
    if mode not in {"return", "none", "raise", "passthrough", "recorded"}:
        raise ValueError("Substitution mode must be return, none, raise, passthrough, recorded, or value.")
    if not callable(original):
        raise ValueError("Callable substitutions must target a callable; use an override for values.")

    def replacement(*args: Any, **kwargs: Any) -> Any:
        interactions.append({"kind": "intercepted_call", "target": rule["target"], "mode": mode,
                             "args": _safe_value(args), "kwargs": _safe_value(kwargs)})
        if mode == "raise":
            exception_type = getattr(builtins, str(rule.get("exception", "RuntimeError")), RuntimeError)
            if not isinstance(exception_type, type) or not issubclass(exception_type, BaseException):
                raise ValueError("Substitution exception must be a built-in exception type.")
            raise exception_type(str(rule.get("message", "Substituted failure.")))
        if mode == "passthrough":
            result = original(*args, **kwargs)
            interactions[-1]["return_value"] = _safe_value(result)
            return result
        if mode == "recorded":
            key = str(rule.get("recording", "0"))
            if key not in recorded and "value" not in rule:
                raise ValueError(f"No recorded return named '{key}'.")
            return recorded.get(key, rule.get("value"))
        return None if mode == "none" else rule.get("value")

    return replacement


def _mock_value(value: Any, interactions: list[dict[str, Any]], target: str, recorded: dict[str, Any]) -> Any:
    return _substitution({"target": target, **value}, lambda *_args, **_kwargs: None, interactions, recorded) if isinstance(value, dict) and "mode" in value else value


def _replace_aliases(original: Any, replacement: Any, workspace: Path, restores: list[tuple[Any, str, Any]]) -> list[str]:
    aliases = []
    for module in list(sys.modules.values()):
        if not isinstance(module, ModuleType) or not _project_module(module, workspace):
            continue
        for name, value in list(vars(module).items()):
            if value is original:
                restores.append((module, name, value))
                setattr(module, name, replacement)
                aliases.append(f"{module.__name__}:{name}")
    return aliases


def _project_module(module: ModuleType, workspace: Path) -> bool:
    try:
        return bool(module.__file__) and Path(module.__file__).resolve().is_relative_to(workspace)
    except (AttributeError, OSError):
        return False


def _replace(restores: list[tuple[Any, str, Any]], owner: Any, name: str, value: Any) -> None:
    restores.append((owner, name, getattr(owner, name)))
    setattr(owner, name, value)


def _complete_value(value: Any, cap: int) -> tuple[Any, bool]:
    if inspect.isawaitable(value):
        return asyncio.run(value), False
    if inspect.isasyncgen(value):
        async def collect() -> tuple[list[Any], bool]:
            results = []
            async for item in value:
                results.append(item)
                if len(results) >= cap:
                    break
            return results, len(results) >= cap
        return asyncio.run(collect())
    if inspect.isgenerator(value):
        results = []
        for item in value:
            results.append(item)
            if len(results) >= cap:
                break
        return results, len(results) >= cap
    return value, False


def _callable_capability(value: Any, target: str | None = None) -> dict[str, Any]:
    details, status, kind = [], "Full", "callable"
    if inspect.isclass(value): kind = "class"
    elif inspect.ismethod(value): kind = "bound_method"
    elif inspect.iscoroutinefunction(value): kind = "coroutine_function"
    elif inspect.isasyncgenfunction(value): kind = "async_generator_function"
    elif inspect.isgeneratorfunction(value): kind = "generator_function"
    elif inspect.isfunction(value): kind = "function"
    elif inspect.isbuiltin(value):
        kind, status = "native_function", "Partial"
        details.append("Invocation is supported; implementation replacement and source mapping may be unavailable.")
    elif inspect.ismethoddescriptor(value) or not callable(value):
        kind, status = "descriptor" if inspect.ismethoddescriptor(value) else "non_callable", "Partial"
        details.append("Direct harness invocation is unavailable; patch the owning class attribute and exercise it through an instance.")
    if inspect.isfunction(value) and value.__closure__:
        status = "Partial"
        details.append("Invocation is supported; direct closure-cell overrides are interpreter-dependent.")
    if hasattr(value, "__wrapped__"):
        details.append("The selected attribute is decorated; patches apply to the displayed attribute.")
    try:
        signature = str(inspect.signature(value))
    except (TypeError, ValueError):
        signature, status = "unknown", "Partial"
    return {"status": status, "kind": kind, "target": target, "signature": signature, "details": details}


def _source_location(value: Any) -> dict[str, Any] | None:
    try:
        path = inspect.getsourcefile(value)
        lines, line = inspect.getsourcelines(value)
    except (OSError, TypeError):
        return None
    return {"path": path, "line_start": line, "line_end": line + len(lines) - 1}


def _dependency_context(value: Any) -> list[dict[str, str]]:
    function = value.__func__ if inspect.ismethod(value) else value
    code, namespace = getattr(function, "__code__", None), getattr(function, "__globals__", {})
    if code is None:
        return []
    dependencies = []
    for name in code.co_names:
        if name not in namespace:
            continue
        dependency = namespace[name]
        kind = "module" if isinstance(dependency, ModuleType) else "callable" if callable(dependency) else "value"
        dependencies.append({"name": name, "kind": kind, "type": f"{type(dependency).__module__}.{type(dependency).__qualname__}"})
    return dependencies


def _default_input(parameter: inspect.Parameter) -> Any:
    annotation = parameter.annotation
    if annotation in {int, float}: return annotation(1)
    if annotation is bool: return True
    if annotation is bytes: return b"value"
    if annotation in {list, tuple, set, dict}: return annotation()
    return "value"


def _boundary_values(parameter: inspect.Parameter) -> list[tuple[str, Any]]:
    sample = parameter.default if parameter.default is not inspect.Parameter.empty else _default_input(parameter)
    values: list[tuple[str, Any]] = [("none", None), ("unexpected type", {"unexpected": True})]
    if parameter.annotation in {int, float} or isinstance(sample, (int, float)) and not isinstance(sample, bool):
        values.extend((("zero", 0), ("negative", -1), ("large", 10 ** 12)))
    if parameter.annotation is str or isinstance(sample, str):
        values.extend((("empty", ""), ("whitespace", "   "), ("long", "x" * 4_096)))
    if parameter.annotation in {list, tuple, set} or isinstance(sample, (list, tuple, set)):
        values.extend((("empty", []), ("single", [1])))
    if parameter.annotation is dict or isinstance(sample, dict):
        values.append(("empty", {}))
    return values


def _module_state(module: ModuleType | None) -> dict[str, Any]:
    return {} if module is None else {name: _safe_value(value) for name, value in vars(module).items()
                                      if not name.startswith("_") and not callable(value) and not isinstance(value, ModuleType)}


def _dictionary_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {"created": {name: after[name] for name in after.keys() - before.keys()},
            "changed": {name: {"before": before[name], "after": after[name]} for name in before.keys() & after.keys() if before[name] != after[name]},
            "removed": {name: before[name] for name in before.keys() - after.keys()}}


def _memory_peak() -> int:
    """Report the child's peak resident memory in bytes when the platform exposes it."""
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, OSError):
        return 0
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _filesystem_snapshot(workspace: Path) -> dict[str, dict[str, Any]]:
    """Hash workspace files in bounded chunks without holding their contents."""
    snapshot = {}
    for path in workspace.rglob("*"):
        relative = path.relative_to(workspace)
        if path.is_symlink() or not path.is_file() or any(part in DEFAULT_EXCLUDES for part in relative.parts):
            continue
        try:
            snapshot[relative.as_posix()] = _file_metadata(path)
        except OSError:
            continue
    return snapshot


def _filesystem_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {"created": {name: after[name] for name in after.keys() - before.keys()},
            "modified": {name: {"before": before[name], "after": after[name]} for name in before.keys() & after.keys() if before[name] != after[name]},
            "deleted": {name: before[name] for name in before.keys() - after.keys()}}


def _exception_value(exc: BaseException) -> dict[str, Any]:
    return {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc(limit=20)}


def _failed_result(exc: BaseException, interactions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"return_value": None, "exception": _exception_value(exc), "stdout": "", "stderr": "", "state": None,
            "interactions": list(interactions), "side_effects": {}, "trace": []}


_TRUNCATION = "_threadtrace_truncation"


def _truncation_marker(size: int, captured: int) -> dict[str, Any]:
    return {"type": "truncation", "size": size, "captured": captured, "truncated": True}


def _safe_value(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return {"type": type(value).__name__, "summary": "depth limit", "comparable": "unknown", "truncated": True}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 4_000 else {"type": "str", "length": len(value), "preview": value[:4_000],
                                                         "truncated": True}
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value), "preview": value[:128].decode(errors="replace"),
                "truncated": len(value) > 128}
    if isinstance(value, (list, tuple)):
        items = [_safe_value(item, depth + 1) for item in value[:100]]
        return items + ([_truncation_marker(len(value), 100)] if len(value) > 100 else [])
    if isinstance(value, (set, frozenset)):
        items = sorted((_safe_value(item, depth + 1) for item in value), key=str)[:100]
        result = {"type": type(value).__name__, "items": items}
        if len(value) > 100:
            result[_TRUNCATION] = _truncation_marker(len(value), 100)
        return result
    if isinstance(value, dict):
        result = {str(key): _safe_value(item, depth + 1) for key, item in list(value.items())[:100]}
        if len(value) > 100:
            result[_TRUNCATION] = _truncation_marker(len(value), 100)
        return result
    state = getattr(value, "__dict__", None)
    if isinstance(state, dict):
        return {"type": f"{type(value).__module__}.{type(value).__qualname__}", "attributes": _safe_value(state, depth + 1)}
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}", "comparable": "unknown"}


def _is_truncated(value: Any) -> bool:
    return isinstance(value, dict) and (value.get("truncated") is True or _TRUNCATION in value
                                        or value.get("comparable") == "unknown")


def _comparison_view(result: dict[str, Any]) -> dict[str, Any]:
    return {key: result.get(key) for key in ("return_value", "exception", "stdout", "stderr", "state", "state_changes",
                                              "argument_changes", "global_changes", "side_effects", "trace", "timed_out")}


def _behavior_signature(result: dict[str, Any]) -> dict[str, Any]:
    view = _comparison_view(result)
    view["trace"] = [(item.get("path"), item.get("line"), item.get("event")) for item in result.get("trace", [])]
    return view


def _structural_compare(before: Any, after: Any, tolerance: float) -> str:
    if _is_truncated(before) or _is_truncated(after): return "unknown"
    if isinstance(before, (int, float)) and isinstance(after, (int, float)) and not isinstance(before, bool) and not isinstance(after, bool): return "equal" if abs(float(before) - float(after)) <= tolerance else "different"
    if type(before) is not type(after): return "different"
    if isinstance(before, dict):
        if before.keys() != after.keys(): return "different"
        outcomes = {_structural_compare(before[key], after[key], tolerance) for key in before}
        return "different" if "different" in outcomes else "unknown" if "unknown" in outcomes else "equal"
    if isinstance(before, list):
        if len(before) != len(after): return "different"
        outcomes = {_structural_compare(left, right, tolerance) for left, right in zip(before, after)}
        return "different" if "different" in outcomes else "unknown" if "unknown" in outcomes else "equal"
    return "equal" if before == after else "different"


def _sync_frame_locals(frame: FrameType) -> None:
    try:
        sync = ctypes.pythonapi.PyFrame_LocalsToFast
        sync.argtypes = [ctypes.py_object, ctypes.c_int]
        sync(frame, 0)
    except (AttributeError, OSError):
        pass


def _safe_builtins() -> dict[str, Any]:
    return {name: getattr(builtins, name) for name in ("bool", "bytes", "dict", "float", "frozenset", "int", "list", "range", "set", "str", "tuple", "zip")}


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Apply per-call rules, letting each named replacement shadow the saved rule at the same target."""
    merged = dict(base)
    for name, value in override.items():
        if name in {"overrides", "argument_overrides", "environment", "fixtures", "adapters"} and isinstance(value, dict):
            merged[name] = {**merged.get(name, {}), **value}
            continue
        if name in {"patches", "substitutions"} and isinstance(value, list):
            shadowed = {_canonical_target(rule["target"]) for rule in value
                        if isinstance(rule, dict) and rule.get("target")}
            other = "substitutions" if name == "patches" else "patches"
            merged[other] = [rule for rule in merged.get(other, [])
                             if _canonical_target(rule.get("target", "")) not in shadowed]
        merged[name] = value
    return merged


def _worker() -> None:
    try:
        payload, result = json.load(sys.stdin), {}
        request = payload["request"]
        harness = HybridHarness(Path(payload["workspace"]), request.get("config", {}), int(request.get("output_cap", 100_000)))
        if request.get("action") == "generate": result = {"cases": harness.generate_cases(request["target"]), "exception": None}
        elif request.get("action") == "capability": result = {"capability": harness.capability(request["target"]), "exception": None}
        elif request.get("action") == "preflight":
            result = {"preflight": experiment_preflight(Path(payload["workspace"]), request.get("config", {})), "exception": None}
        elif "operations" in request: result = {"operations": harness.execute(request["operations"], request.get("per_call")), "instances": sorted(harness.instances), "interactions": harness.interactions}
        else:
            result = harness.call(request["target"], request.get("args"), request.get("kwargs"), request.get("per_call"))
            result.pop("_raw_return", None)
        print(json.dumps(result, default=str))
    except BaseException as exc:
        print(json.dumps(_failed_result(exc, []), default=str))


if __name__ == "__main__":
    _worker()
