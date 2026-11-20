"""Bounded Python call tracing for a command the user explicitly supplied."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .failure import failure_report, redact_text
from .schema import SOURCE_EVENT_KINDS, instrumentation_plan, normalise_event
from .session import _write_runtime_support


def run_python_trace(
    root: Path, command: list[str], timeout: int, output_cap: int, event_cap: int = 10_000,
    cwd: Path | None = None, capture_level: str = "metadata", capture_limit: int = 4096,
    redact: bool = True, environment: dict[str, str | None] | None = None,
    instrumentation: str = "auto", include_third_party: bool = False,
) -> dict[str, Any]:
    """Run *command* and return source-root call events.

    This is intentionally not a sandbox: process execution is only possible
    through the explicit CLI confirmation.  The injected profile hook is
    Python-specific and safely degrades to an empty event list for commands
    that do not import ``sitecustomize``.
    """
    if not command:
        raise ValueError("A command is required after '--'.")
    if capture_level not in {"off", "metadata", "content"}:
        raise ValueError("Capture level must be off, metadata, or content.")
    if capture_limit < 1:
        raise ValueError("Capture limit must be positive.")
    root = root.resolve()
    run_cwd = (cwd or root).resolve()
    if not run_cwd.is_dir():
        raise ValueError(f"Runtime working directory does not exist: {run_cwd}")
    overrides = environment or {}
    inherited = os.environ.copy()
    selected = inherited.copy()
    for name, value in overrides.items():
        if value is None:
            selected.pop(name, None)
        else:
            selected[name] = value
    resolved = _resolve_executable(command[0], selected.get("PATH"), run_cwd)
    resolved_command = [resolved, *command[1:]]
    interpreter_version = _interpreter_version(resolved)
    plan = instrumentation_plan(instrumentation, interpreter_version)
    run_id = uuid.uuid4().hex[:16]
    instrumented = {
        "PYTHONPATH", "THREADTRACE_RUNTIME_LOG", "THREADTRACE_RUNTIME_ROOT",
        "THREADTRACE_RUNTIME_MAX_EVENTS", "THREADTRACE_RUNTIME_CAPTURE_LEVEL",
        "THREADTRACE_RUNTIME_CAPTURE_LIMIT", "THREADTRACE_RUNTIME_REDACT",
        "THREADTRACE_RUNTIME_INSTRUMENTATION", "THREADTRACE_RUNTIME_INCLUDE_THIRD_PARTY",
        "THREADTRACE_RUNTIME_RUN_ID",
    }
    launch = {
        "run_id": run_id,
        "requested_command": list(command),
        "resolved_command": resolved_command,
        "resolved_executable": resolved,
        "arguments": list(command[1:]),
        "cwd": str(run_cwd),
        "interpreter_version": interpreter_version,
        "instrumentation": plan,
        "environment_inherited": sorted(inherited),
        "environment_overridden": sorted(name for name, value in overrides.items() if value is not None),
        "environment_removed": sorted(name for name, value in overrides.items() if value is None),
        "instrumentation_collisions": sorted(instrumented & set(overrides)),
    }
    started_at = datetime.now(UTC).isoformat()
    with tempfile.TemporaryDirectory(prefix="threadtrace-runtime-") as directory:
        trace_dir = Path(directory)
        log_path = trace_dir / "events.jsonl"
        _write_runtime_support(trace_dir, _profile_source())
        # Preserve the selected environment; only add ThreadTrace's own names.
        selected["PYTHONPATH"] = os.pathsep.join(filter(None, [str(trace_dir), selected.get("PYTHONPATH", "")]))
        selected["THREADTRACE_RUNTIME_LOG"] = str(log_path)
        selected["THREADTRACE_RUNTIME_ROOT"] = str(root)
        selected["THREADTRACE_RUNTIME_MAX_EVENTS"] = str(event_cap)
        selected["THREADTRACE_RUNTIME_CAPTURE_LEVEL"] = capture_level
        selected["THREADTRACE_RUNTIME_CAPTURE_LIMIT"] = str(capture_limit)
        selected["THREADTRACE_RUNTIME_REDACT"] = "1" if redact else "0"
        selected["THREADTRACE_RUNTIME_INSTRUMENTATION"] = instrumentation
        selected["THREADTRACE_RUNTIME_INCLUDE_THIRD_PARTY"] = "1" if include_third_party else "0"
        selected["THREADTRACE_RUNTIME_RUN_ID"] = run_id
        timed_out = False
        try:
            completed = subprocess.run(
                resolved_command, cwd=run_cwd, env=selected, text=True, errors="replace", capture_output=True,
                timeout=timeout, check=False,
            )
            exit_code = completed.returncode
            stdout, stderr = completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = None
            stdout = _as_text(exc.stdout)
            stderr = _as_text(exc.stderr)
        events, truncation = _read_events(log_path, root, event_cap, include_third_party)
        launch["process_metrics"] = _process_metrics()
        result = {
            "run_id": run_id,
            "command": command,
            "cwd": str(run_cwd),
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": redact_text(stdout, redact)[:output_cap],
            "stderr": redact_text(stderr, redact)[:output_cap],
            "output_truncated": len(stdout) > output_cap or len(stderr) > output_cap,
            "events": events,
            "capture_level": capture_level,
            "capture_limit": capture_limit,
            "redaction_enabled": redact,
            "instrumentation": plan,
            "recording_scope": "project+reached-third-party" if include_third_party else "project",
            "presentation_scope": "recorded-events",
            "event_truncation": truncation,
            "launch": launch,
        }
        failure_input = {**result, "stderr": redact_text(stderr, redact)}
        result["failure_report"] = failure_report(root, failure_input)
        return result


def _resolve_executable(executable: str, path: str | None, run_cwd: Path) -> str:
    """Resolve the executable the selected launch environment would run."""
    if os.path.dirname(executable):
        candidate = Path(executable).expanduser()
        if not candidate.is_absolute():
            candidate = run_cwd / candidate
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise ValueError(f"Executable '{executable}' does not exist or is not executable.")
    found = shutil.which(executable, path=path)
    if not found:
        raise ValueError(f"Executable '{executable}' was not found on PATH.")
    return found


def _interpreter_version(executable: str) -> str | None:
    if not re.fullmatch(r"(python(\d+(\.\d+)*)?|py)(\.exe)?", Path(executable).name, re.IGNORECASE):
        return None
    try:
        completed = subprocess.run(
            [executable, "-c", "import platform;print(platform.python_version())"],
            text=True, errors="replace", capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _process_metrics() -> dict[str, Any]:
    """Expose supported child-process memory without claiming allocation data."""
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        max_rss = int(usage.ru_maxrss)
        if sys.platform != "darwin":
            max_rss *= 1024
        return {"status": "observed", "source": "getrusage(RUSAGE_CHILDREN)", "max_rss_bytes": max_rss,
                "allocation_profiling": False,
                "limitation": "This is cumulative child-process peak RSS, not an allocation profile."}
    except (ImportError, OSError, AttributeError):
        return {"status": "unsupported", "source": "getrusage(RUSAGE_CHILDREN)", "allocation_profiling": False}


def _read_events(path: Path, root: Path, maximum: int, include_third_party: bool = False
                 ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stream the event journal; never read an unbounded file into memory."""
    if not path.exists():
        return [], {"bounded": True, "truncated": False, "malformed": 0, "capped": False}
    events: list[dict[str, Any]] = []
    malformed = 0
    capped = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if len(events) >= maximum:
                    capped = True
                    break
                try:
                    item = normalise_event(json.loads(line), len(events) + 1)
                except (ValueError, json.JSONDecodeError):
                    malformed += 1
                    continue
                source_text = item.get("path")
                if isinstance(source_text, str) and source_text:
                    try:
                        source = Path(source_text).resolve()
                        if source.is_relative_to(root):
                            item["relative_path"] = source.relative_to(root).as_posix()
                        elif not include_third_party:
                            continue
                    except (OSError, ValueError):
                        continue
                elif not include_third_party and item.get("kind") in SOURCE_EVENT_KINDS:
                    continue
                events.append(item)
    except OSError:
        pass
    return events, {"bounded": True, "truncated": capped, "malformed": malformed, "capped": capped}


def _profile_source() -> str:
    # Keep this self-contained because it is loaded in another interpreter.
    return r'''
import atexit
import json
import os
import sys
import time
import threading

import _threadtrace_runtime_adapters
import _threadtrace_runtime_waits as _waits

_root = os.path.realpath(os.environ.get("THREADTRACE_RUNTIME_ROOT", ""))
_run_id = os.environ.get("THREADTRACE_RUNTIME_RUN_ID") or "run-%s-%s" % (os.getpid(), time.time_ns())
_parent_process_id = os.environ.get("THREADTRACE_RUNTIME_PROCESS_ID") or None
_process_id = "%s:%s" % (_run_id, os.getpid())
os.environ["THREADTRACE_RUNTIME_PROCESS_ID"] = _process_id
_argv = list(sys.argv)
_log = os.environ.get("THREADTRACE_RUNTIME_LOG")
_maximum = int(os.environ.get("THREADTRACE_RUNTIME_MAX_EVENTS", "10000"))
_instrumentation = os.environ.get("THREADTRACE_RUNTIME_INSTRUMENTATION", "auto")
_include_third_party = os.environ.get("THREADTRACE_RUNTIME_INCLUDE_THIRD_PARTY") == "1"
_started_monotonic = time.monotonic()
_started_cpu_time = time.process_time()
_count = 0
_frame_ids = {}
_frame_sequence = 0
_generator_frames = set()
_threads = set()
_finished_threads = set()
_depths = {}


def _allowed(filename):
    try:
        if not os.path.isabs(filename):
            return False
        source = os.path.realpath(filename)
        if _include_third_party:
            return True
        return os.path.commonpath([source, _root]) == _root
    except ValueError:
        return False


def _write(item):
    global _count
    if _count >= _maximum:
        return
    _count += 1
    item["event_schema_version"] = 2
    item["protocol_version"] = 1
    try:
        with open(_log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, sort_keys=True) + "\n")
    except OSError:
        pass


def _aux(item):
    item.setdefault("path", "")
    item.setdefault("relative_path", "")
    item.setdefault("line", 0)
    item.setdefault("name", "")
    item.setdefault("evidence_class", "observed at runtime")
    item.update({"run_id": _run_id, "process_id": _process_id, "parent_process_id": _parent_process_id,
                 "pid": os.getpid(), "wall_time": time.time(),
                 "elapsed_seconds": time.monotonic() - _started_monotonic,
                 "cpu_seconds": time.process_time() - _started_cpu_time})
    _write(item)

def _base(kind):
    thread_id = threading.get_ident()
    if thread_id not in _threads:
        _threads.add(thread_id)
        _aux({"kind": "thread", "lifecycle": "started", "thread_id": thread_id,
              "run_thread_id": "%s:%s" % (_process_id, thread_id), "name": threading.current_thread().name})
    return {"kind": kind, "thread_id": thread_id, "run_thread_id": "%s:%s" % (_process_id, thread_id),
            "pid": os.getpid(), "run_id": _run_id, "process_id": _process_id,
            "parent_process_id": _parent_process_id,
            "wall_time": time.time(), "elapsed_seconds": time.monotonic() - _started_monotonic,
            "cpu_seconds": time.process_time() - _started_cpu_time}


def _profile(frame, event, arg):
    global _frame_sequence
    if event not in ("call", "line", "return", "exception", "c_call", "c_return", "c_exception"):
        return _profile
    filename = frame.f_code.co_filename
    if not _allowed(filename):
        return _profile
    thread_id = threading.get_ident()
    depth = _depths.get(thread_id, 0)
    if event == "call":
        depth += 1
        _depths[thread_id] = depth
        if id(frame) in _frame_ids:
            event = "resume"
        else:
            _frame_sequence += 1
            _frame_ids[id(frame)] = "%s:%s" % (os.getpid(), _frame_sequence)
        if frame.f_code.co_flags & 0x20 or frame.f_code.co_flags & 0x200:
            _generator_frames.add(id(frame))
    item = _base(event)
    item.update({"path": filename, "line": frame.f_lineno, "name": frame.f_code.co_name,
                 "call_depth": depth, "frame_id": _frame_ids.get(id(frame)),
                 "is_coroutine": bool(frame.f_code.co_flags & 0x80)})
    if event in ("call", "line"):
        item["variables"] = {}
    if event == "return":
        if id(frame) in _generator_frames:
            item["kind"] = "yield"
        _frame_ids.pop(id(frame), None)
        _depths[thread_id] = max(0, depth - 1)
        if item["kind"] != "yield" and depth <= 1 and thread_id not in _finished_threads:
            _finished_threads.add(thread_id)
            _aux({"kind": "thread", "lifecycle": "finished", "thread_id": thread_id,
                  "run_thread_id": "%s:%s" % (_process_id, thread_id),
                  "name": threading.current_thread().name})
    elif event == "exception" and isinstance(arg, tuple) and len(arg) > 1:
        item["exception"] = type(arg[1]).__name__
        item["phase"] = "raised"
    elif event == "c_call" and isinstance(arg, tuple) and len(arg) > 1:
        item["c_function"] = getattr(arg[1], "__name__", str(arg[1]))
    _write(item)
    return _profile


def _audit(name, args):
    if name in {"import", "subprocess.Popen", "os.system", "socket.connect", "open"}:
        kind = "import" if name == "import" else "process" if name in {"subprocess.Popen", "os.system"} else "audit"
        item = _base(kind)
        item.update({"audit_event": name, "argument_count": len(args)})
        _write(item)


if _instrumentation in {"profile", "all"}:
    sys.setprofile(_profile)
if _instrumentation not in {"profile"}:
    sys.settrace(_profile)
    threading.settrace(_profile)
if _instrumentation in {"audit", "all"}:
    sys.addaudithook(_audit)
_aux({"kind": "process", "lifecycle": "started", "ppid": os.getppid(), "executable": sys.executable,
      "argv": _argv, "interpreter_version": "%d.%d.%d" % sys.version_info[:3],
      "instrumentation": _instrumentation})
atexit.register(lambda: _aux({"kind": "process", "lifecycle": "ended"}))
_waits.install(_aux, _root)
'''
