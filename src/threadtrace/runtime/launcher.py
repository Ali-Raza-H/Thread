"""Bounded Python call tracing for a command the user explicitly supplied."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def run_python_trace(root: Path, command: list[str], timeout: int, output_cap: int, event_cap: int = 10_000) -> dict[str, Any]:
    """Run *command* and return source-root call events.

    This is intentionally not a sandbox: process execution is only possible
    through the explicit CLI confirmation.  The injected profile hook is
    Python-specific and safely degrades to an empty event list for commands
    that do not import ``sitecustomize``.
    """
    if not command:
        raise ValueError("A command is required after '--'.")
    root = root.resolve()
    with tempfile.TemporaryDirectory(prefix="threadtrace-runtime-") as directory:
        trace_dir = Path(directory)
        log_path = trace_dir / "events.jsonl"
        (trace_dir / "sitecustomize.py").write_text(_profile_source(), encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP"}}
        inherited_path = os.environ.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(trace_dir), inherited_path]))
        environment["THREADTRACE_RUNTIME_LOG"] = str(log_path)
        environment["THREADTRACE_RUNTIME_ROOT"] = str(root)
        environment["THREADTRACE_RUNTIME_MAX_EVENTS"] = str(event_cap)
        timed_out = False
        try:
            completed = subprocess.run(
                command, cwd=root, env=environment, text=True, errors="replace", capture_output=True,
                timeout=timeout, check=False,
            )
            exit_code = completed.returncode
            stdout, stderr = completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            stdout = _as_text(exc.stdout)
            stderr = _as_text(exc.stderr)
        events = _read_events(log_path, root, event_cap)
        return {
            "command": command,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": stdout[:output_cap],
            "stderr": stderr[:output_cap],
            "output_truncated": len(stdout) > output_cap or len(stderr) > output_cap,
            "events": events,
        }


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _read_events(path: Path, root: Path, maximum: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:maximum]:
            item = json.loads(line)
            source = Path(item["path"]).resolve()
            if source.is_relative_to(root):
                item["relative_path"] = source.relative_to(root).as_posix()
                events.append(item)
    except (OSError, ValueError, json.JSONDecodeError):
        return events
    return events


def _profile_source() -> str:
    # Keep this self-contained because it is loaded in another interpreter.
    return r'''
import json
import os
import sys

_root = os.environ.get("THREADTRACE_RUNTIME_ROOT", "")
_log = os.environ.get("THREADTRACE_RUNTIME_LOG")
_maximum = int(os.environ.get("THREADTRACE_RUNTIME_MAX_EVENTS", "10000"))
_count = 0

def _profile(frame, event, arg):
    global _count
    if event not in ("call", "return") or _count >= _maximum:
        return _profile
    filename = frame.f_code.co_filename
    try:
        if not os.path.commonpath([os.path.realpath(filename), os.path.realpath(_root)]) == os.path.realpath(_root):
            return _profile
    except ValueError:
        return _profile
    _count += 1
    try:
        with open(_log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": event, "path": filename, "line": frame.f_lineno, "name": frame.f_code.co_name}) + "\n")
    except OSError:
        pass
    return _profile

sys.setprofile(_profile)
'''
