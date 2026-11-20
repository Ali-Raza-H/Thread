"""Controllable, opt-in Python runtime sessions used by the runtime dashboard."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .failure import failure_report, redact_text
from .schema import event_errors, instrumentation_plan, normalise_event
from .values import validate_expression


class RuntimeSession:
    """Launch one explicit command and pause it at project-level trace events.

    This is a debugger-style aid, not a sandbox.  The target is never launched
    by construction; callers must explicitly invoke :meth:`start`.
    """

    def __init__(self, root: Path, command: list[str], timeout: int = 30, output_cap: int = 100_000,
                 event_cap: int = 10_000, runtime_environment: dict[str, str] | None = None,
                 cwd: Path | None = None, capture_level: str = "metadata", capture_limit: int = 4096,
                 redact: bool = True, instrumentation: str = "auto", include_third_party: bool = False,
                 value_depth: int = 3, value_items: int = 20, value_bytes: int = 160) -> None:
        if not command:
            raise ValueError("A command is required after '--'.")
        self.root = root.resolve()
        self.cwd = (cwd or root).resolve()
        if not self.cwd.is_dir():
            raise ValueError(f"Runtime working directory does not exist: {self.cwd}")
        self.command = command
        self.timeout = timeout
        self.output_cap = output_cap
        self.event_cap = event_cap
        self.runtime_environment = runtime_environment or {}
        if capture_level not in {"off", "metadata", "content"}:
            raise ValueError("Capture level must be off, metadata, or content.")
        if capture_limit < 1:
            raise ValueError("Capture limit must be positive.")
        self.capture_level = capture_level
        self.capture_limit = capture_limit
        self.redact = redact
        self.instrumentation = instrumentation
        self.include_third_party = include_third_party
        self.run_id = uuid.uuid4().hex[:16]
        self.value_depth, self.value_items, self.value_bytes = value_depth, value_items, value_bytes
        if min(value_depth, value_items, value_bytes) < 1:
            raise ValueError("Runtime value budgets must be positive.")
        self.instrumentation_plan = instrumentation_plan(instrumentation)
        self._directory = tempfile.TemporaryDirectory(prefix="threadtrace-runtime-session-")
        self.directory = Path(self._directory.name)
        self.log_path = self.directory / "events.jsonl"
        self.control_path = self.directory / "control.json"
        self.config_path = self.directory / "config.json"
        self.process: subprocess.Popen[bytes] | None = None
        self.started_at: float | None = None
        self.started_timestamp: str | None = None
        self.finished_timestamp: str | None = None
        self.timed_out = False
        self.mode = "not_started"
        self._sequence = 0
        self._offset = 0
        self.events: list[dict[str, Any]] = []
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._stderr_tail = bytearray()
        self._output_lock = threading.Lock()
        self._breakpoints: dict[tuple[str, int], dict[str, Any]] = {}
        self._focused_task_id: str | None = None

    @property
    def started(self) -> bool:
        return self.process is not None

    @property
    def finished(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def start(self) -> None:
        if self.process is not None:
            return
        _write_runtime_support(self.directory, _trace_source())
        self._write_config()
        self._send("paused")
        # A runtime session should start as closely as possible to a normal
        # invocation.  In particular, projects commonly need HOME, locale,
        # cloud credentials, or variables loaded by a wrapper such as `dev`.
        # ThreadTrace only adds its own namespaced tracing variables below.
        environment = os.environ.copy()
        environment.update(self.runtime_environment)
        inherited_path = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(self.directory), inherited_path]))
        environment["THREADTRACE_RUNTIME_LOG"] = str(self.log_path)
        environment["THREADTRACE_RUNTIME_CONTROL"] = str(self.control_path)
        environment["THREADTRACE_RUNTIME_CONFIG"] = str(self.config_path)
        environment["THREADTRACE_RUNTIME_ROOT"] = str(self.root)
        environment["THREADTRACE_RUNTIME_MAX_EVENTS"] = str(self.event_cap)
        environment["THREADTRACE_RUNTIME_CAPTURE_LEVEL"] = self.capture_level
        environment["THREADTRACE_RUNTIME_CAPTURE_LIMIT"] = str(self.capture_limit)
        environment["THREADTRACE_RUNTIME_REDACT"] = "1" if self.redact else "0"
        environment["THREADTRACE_RUNTIME_INSTRUMENTATION"] = self.instrumentation
        environment["THREADTRACE_RUNTIME_INCLUDE_THIRD_PARTY"] = "1" if self.include_third_party else "0"
        environment["THREADTRACE_RUNTIME_VALUE_DEPTH"] = str(self.value_depth)
        environment["THREADTRACE_RUNTIME_VALUE_ITEMS"] = str(self.value_items)
        environment["THREADTRACE_RUNTIME_VALUE_BYTES"] = str(self.value_bytes)
        environment["THREADTRACE_RUNTIME_RUN_ID"] = self.run_id
        self.process = subprocess.Popen(
            self.command, cwd=self.cwd, env=environment,
            # The dashboard owns the real terminal.  A private pipe lets the
            # input panel send complete lines to input() without two programs
            # competing to read the same terminal in raw mode.
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # `dev run` and similar launchers can create descendants. Put the
            # controlled execution in its own group so q/Ctrl-C can reliably
            # stop all of them instead of leaving the terminal wedged.
            start_new_session=os.name == "posix",
        )
        self.started_at = time.monotonic()
        self.started_timestamp = datetime.now(UTC).isoformat()
        self.mode = "paused"
        assert self.process.stdin is not None and self.process.stdout is not None and self.process.stderr is not None
        threading.Thread(target=self._drain, args=(self.process.stdout, self._stdout), daemon=True).start()
        threading.Thread(
            target=self._drain, args=(self.process.stderr, self._stderr, self._stderr_tail), daemon=True,
        ).start()

    def send_input(self, text: str) -> bool:
        """Send one line of user input to the controlled child process.

        Direct Runtime intentionally does not share its terminal with the
        child.  This keeps the dashboard responsive while still supporting
        line-oriented prompts such as ``input()``.
        """
        if not self.process or self.finished or not self.process.stdin:
            return False
        try:
            self.process.stdin.write((text + "\n").encode())
            self.process.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def step(self, thread_id: int | None = None) -> None:
        self.start()
        self._send("step", **({"thread_id": thread_id} if thread_id is not None else {}))
        self.mode = "paused"

    def step_over(self, thread_id: int | None = None) -> None:
        """Resume nested calls and pause at the next event in this frame."""
        self.start()
        current = next((event for event in reversed(self.events)
                        if thread_id is None or event.get("thread_id") == thread_id), {})
        if not current:
            self.step(thread_id)
            return
        self._send("step_over", thread_id=current.get("thread_id"), depth=current.get("call_depth"))
        self.mode = "step_over"

    def step_out(self, thread_id: int | None = None) -> None:
        """Resume until the selected frame returns to its caller."""
        self.start()
        current = next((event for event in reversed(self.events)
                        if thread_id is None or event.get("thread_id") == thread_id), {})
        if not current:
            self.step(thread_id)
            return
        self._send("step_out", thread_id=current.get("thread_id"), depth=current.get("call_depth"))
        self.mode = "step_out"

    def continue_run(self) -> None:
        self.start()
        self._send("continue")
        self.mode = "continue"

    def pause(self, thread_id: int | None = None) -> None:
        if self.process and not self.finished:
            self._send("pause", **({"thread_id": thread_id} if thread_id is not None else {}))
            self.mode = "paused"

    def extend_timeout(self, seconds: int) -> None:
        """Continue a paused or running inspection with a fresh timeout budget."""
        if seconds < 1:
            raise ValueError("Runtime timeout must be positive.")
        self.timeout = seconds
        self.started_at = time.monotonic()
        self.timed_out = False
        if self.process and not self.finished and self.mode == "continue":
            self.mode = "continue"

    def graceful_stop(self) -> None:
        """Request normal process-group termination without an immediate kill."""
        if self.process and not self.finished:
            self._signal_group(signal.SIGTERM)
            self.mode = "stopping"

    def force_stop(self) -> None:
        """Emergency process-group termination for an unresponsive target."""
        if self.process and not self.finished:
            self._signal_group(signal.SIGKILL)
            self.mode = "stopped"

    @property
    def breakpoints(self) -> set[tuple[str, int]]:
        """Absolute project-source locations where free running should pause."""
        return set(self._breakpoints)

    def add_breakpoint(self, path: Path | str, line: int, *, condition: str | None = None,
                       kind: str = "breakpoint", symbol: str | None = None) -> None:
        """Add a breakpoint, tracepoint, or watchpoint with a safe optional expression."""
        if kind not in {"breakpoint", "tracepoint", "watchpoint"}:
            raise ValueError("Debugger locations must be breakpoints, tracepoints, or watchpoints.")
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Debugger locations must point to a file inside the project root.")
        if line < 1:
            raise ValueError("Debugger lines start at 1.")
        if condition:
            validate_expression(condition)
        source_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
        self._breakpoints[(str(resolved), line)] = {
            "kind": kind, "condition": condition, "symbol": symbol, "source_hash": source_hash,
        }
        self._write_config()

    def remove_breakpoint(self, path: Path | str, line: int) -> None:
        self._breakpoints.pop((str(Path(path).resolve()), line), None)
        self._write_config()

    def toggle_breakpoint(self, path: Path | str, line: int) -> bool:
        """Toggle one project-local source breakpoint and return whether it is enabled."""
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Breakpoints must point to a file inside the project root.")
        if line < 1:
            raise ValueError("Breakpoint lines start at 1.")
        key = (str(resolved), line)
        if key in self._breakpoints:
            self.remove_breakpoint(resolved, line)
            return False
        self.add_breakpoint(resolved, line)
        return True

    def revalidate_breakpoints(self) -> list[tuple[str, int]]:
        """Drop locations whose source disappeared or changed after they were set."""
        stale = []
        for (path, line), record in list(self._breakpoints.items()):
            try:
                current = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError:
                current = None
            if current != record["source_hash"]:
                stale.append((path, line))
                del self._breakpoints[(path, line)]
        if stale:
            self._write_config()
        return stale

    @property
    def focused_task_id(self) -> str | None:
        """The asyncio task being isolated, or ``None`` while showing all lanes."""
        return self._focused_task_id

    def focus_task(self, task_id: str | None) -> None:
        """Show and pause only a selected asyncio task; ``None`` restores all lanes."""
        self._focused_task_id = task_id
        self._write_config()

    def stop(self) -> None:
        if self.process and not self.finished:
            self._signal_group(signal.SIGTERM)
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._signal_group(signal.SIGKILL)
                self.process.wait(timeout=1)
            self.mode = "stopped"

    def poll(self) -> list[dict[str, Any]]:
        if self.process and self.mode == "continue" and not self.finished and self.started_at and time.monotonic() - self.started_at > self.timeout:
            self.timed_out = True
            self._signal_group(signal.SIGTERM)
        if not self.log_path.exists():
            return []
        try:
            with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._offset)
                lines = handle.readlines()
                self._offset = handle.tell()
        except OSError:
            return []
        fresh: list[dict[str, Any]] = []
        for line in lines:
            if len(self.events) >= self.event_cap:
                break
            try:
                event = normalise_event(json.loads(line), len(self.events) + 1)
            except (json.JSONDecodeError, ValueError):
                continue
            if event_errors(event):
                continue
            path = event.get("path")
            if isinstance(path, str) and path:
                try:
                    resolved = Path(path).resolve()
                    if not resolved.is_relative_to(self.root):
                        if not self.include_third_party:
                            continue
                        event["relative_path"] = resolved.as_posix()
                    else:
                        event["relative_path"] = resolved.relative_to(self.root).as_posix()
                except (OSError, ValueError):
                    continue
            elif event.get("kind") in {"call", "line", "return", "yield", "resume", "exception", "c_call", "c_return", "c_exception"}:
                continue
            fresh.append(event)
            self.events.append(event)
        return fresh

    def result(self) -> dict[str, Any]:
        self.poll()
        exit_code = self.process.poll() if self.process else None
        if self.finished and self.finished_timestamp is None:
            self.finished_timestamp = datetime.now(UTC).isoformat()
        with self._output_lock:
            stdout = redact_text(bytes(self._stdout).decode(errors="replace"), self.redact, self.runtime_environment)
            stderr = redact_text(bytes(self._stderr).decode(errors="replace"), self.redact, self.runtime_environment)
            failure_stderr = redact_text(
                bytes(self._stderr_tail).decode(errors="replace"), self.redact, self.runtime_environment,
            )
        result = {
            "command": self.command,
            "cwd": str(self.cwd),
            "exit_code": exit_code,
            "timed_out": self.timed_out,
            "stdout": stdout,
            "stderr": stderr,
            "output_truncated": len(self._stdout) >= self.output_cap or len(self._stderr) >= self.output_cap,
            "run_id": self.run_id,
            "events": self.events,
            "started_at": self.started_timestamp,
            "finished_at": self.finished_timestamp,
            "capture_level": self.capture_level,
            "capture_limit": self.capture_limit,
            "redaction_enabled": self.redact,
            "instrumentation": self.instrumentation_plan,
            "recording_scope": "project+reached-third-party" if self.include_third_party else "project",
            "event_truncation": {"bounded": True, "truncated": len(self.events) >= self.event_cap,
                                  "event_cap": self.event_cap},
        }
        failure_input = {**result, "stderr": failure_stderr}
        result["failure_report"] = failure_report(self.root, failure_input)
        return result

    def close(self) -> None:
        self.stop()
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            if self.process.stdout:
                self.process.stdout.close()
            if self.process.stderr:
                self.process.stderr.close()
        self._directory.cleanup()

    def _signal_group(self, signal_number: int) -> None:
        """Signal the controlled command and launcher descendants together."""
        assert self.process is not None
        if os.name == "posix":
            try:
                os.killpg(self.process.pid, signal_number)
                return
            except ProcessLookupError:
                return
        if signal_number == signal.SIGKILL:
            self.process.kill()
        else:
            self.process.terminate()

    def _send(self, action: str, **details: Any) -> None:
        self._sequence += 1
        temporary = self.directory / f"control-{self._sequence}.json"
        temporary.write_text(json.dumps({"sequence": self._sequence, "action": action, **details}), encoding="utf-8")
        os.replace(temporary, self.control_path)

    def _write_config(self) -> None:
        """Atomically publish persistent debugger settings to the traced process."""
        payload = {
            "protocol_version": 1,
            "focus_task_id": self._focused_task_id,
            "breakpoints": [
                {"path": path, "line": line, **record}
                for (path, line), record in sorted(self._breakpoints.items())
            ],
        }
        temporary = self.directory / "config.next.json"
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.config_path)

    def _drain(self, stream: Any, destination: bytearray, tail: bytearray | None = None) -> None:
        # BufferedReader.read(4096) is allowed to wait until the buffer is
        # full.  read1 returns a flushed prompt promptly, which is important
        # when the program is waiting for a response in Program input.
        reader = getattr(stream, "read1", stream.read)
        while chunk := reader(4096):
            with self._output_lock:
                remaining = self.output_cap - len(destination)
                if remaining > 0:
                    destination.extend(chunk[:remaining])
                if tail is not None:
                    tail.extend(chunk)
                    del tail[:-16_384]


def _trace_source() -> str:
    """Source injected through sitecustomize in the explicit child process."""
    return r'''
import atexit
import hashlib
import json
import os
import socket
import sys
import time
import threading
from functools import lru_cache
try:
    import asyncio
except Exception:
    asyncio = None

import _threadtrace_runtime_adapters
import _threadtrace_runtime_waits as _waits
import _threadtrace_runtime_values as _values

def _preserve_dotenv_lookup():
    """Keep python-dotenv's normal caller-file lookup under this tracer.

    python-dotenv treats any sys.settrace() hook as a debugger and then only
    looks for .env in the current working directory. A runtime tracer is not a
    user debugger, and that change makes a normal layout such as
    ``backend/.env`` invisible when a command starts from the project root.
    Limit the compatibility adjustment to find_dotenv itself; explicit
    ``usecwd=True`` calls retain their requested behaviour.
    """
    try:
        import dotenv.main as _threadtrace_dotenv
    except ImportError:
        return
    original = _threadtrace_dotenv.find_dotenv
    if getattr(original, "_threadtrace_preserves_lookup", False):
        return
    def find_dotenv(filename=".env", raise_error_if_not_found=False, usecwd=False):
        # This mirrors python-dotenv's normal (non-debugger) lookup. Calling
        # the original function from a wrapper is not sufficient: its frame
        # walk would stop at this injected sitecustomize.py file.
        def interactive():
            if hasattr(sys, "ps1") or hasattr(sys, "ps2"):
                return True
            try:
                main = __import__("__main__", None, None, fromlist=["__file__"])
            except ModuleNotFoundError:
                return False
            return not hasattr(main, "__file__")
        if usecwd or interactive() or getattr(sys, "frozen", False):
            path = os.getcwd()
        else:
            frame = sys._getframe()
            dotenv_file = os.path.abspath(_threadtrace_dotenv.__file__)
            tracer_file = os.path.abspath(__file__)
            while frame and (
                os.path.abspath(frame.f_code.co_filename) in (dotenv_file, tracer_file)
                or not os.path.exists(frame.f_code.co_filename)
            ):
                frame = frame.f_back
            path = os.path.dirname(os.path.abspath(frame.f_code.co_filename)) if frame else os.getcwd()
        while True:
            candidate = os.path.join(path, filename)
            if os.path.isfile(candidate):
                return candidate
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
        if raise_error_if_not_found:
            raise IOError("File not found")
        return ""
    find_dotenv._threadtrace_preserves_lookup = True
    _threadtrace_dotenv.find_dotenv = find_dotenv

_preserve_dotenv_lookup()

_root = os.path.realpath(os.environ.get("THREADTRACE_RUNTIME_ROOT", ""))
_run_id = os.environ.get("THREADTRACE_RUNTIME_RUN_ID") or "run-%s-%s" % (os.getpid(), time.time_ns())
_parent_process_id = os.environ.get("THREADTRACE_RUNTIME_PROCESS_ID") or None
_process_id = "%s:%s" % (_run_id, os.getpid())
os.environ["THREADTRACE_RUNTIME_PROCESS_ID"] = _process_id
_argv = list(sys.argv)
_log = os.environ.get("THREADTRACE_RUNTIME_LOG")
_event_socket = os.environ.get("THREADTRACE_RUNTIME_SOCKET")
_session_id = os.environ.get("THREADTRACE_RUNTIME_SESSION")
_control = os.environ.get("THREADTRACE_RUNTIME_CONTROL")
_config = os.environ.get("THREADTRACE_RUNTIME_CONFIG")
_maximum = int(os.environ.get("THREADTRACE_RUNTIME_MAX_EVENTS", "10000"))
_instrumentation = os.environ.get("THREADTRACE_RUNTIME_INSTRUMENTATION", "auto")
_include_third_party = os.environ.get("THREADTRACE_RUNTIME_INCLUDE_THIRD_PARTY") == "1"
try:
    _values.configure_budgets(
        int(os.environ.get("THREADTRACE_RUNTIME_VALUE_DEPTH", "3")),
        int(os.environ.get("THREADTRACE_RUNTIME_VALUE_ITEMS", "20")),
        int(os.environ.get("THREADTRACE_RUNTIME_VALUE_BYTES", "160")),
    )
except ValueError:
    pass

_started_monotonic = time.monotonic()
_started_cpu_time = time.process_time()
_values.reset_object_ids()
_count = 0
_seen_sequence = -1
_default_mode = "paused"
_modes = {}
_target_thread = None
_target_depth = None
_snapshots = {}
_frame_ids = {}
_depths = {}
_ordinal = 0
_frame_sequence = 0
_config_mtime = None
_breakpoint_records = {}
_focus_task_id = None
_watch_values = {}
_threads_seen = set()
_finished_threads = set()
_tasks_seen = set()
_generator_frames = set()
_suspended_frames = set()

@lru_cache(maxsize=4096)
def _in_root(filename):
    try:
        if not os.path.isabs(filename):
            return False
        source = os.path.realpath(filename)
        if _include_third_party:
            return True
        if os.path.commonpath([source, _root]) != _root:
            return False
        # A project commonly contains .venv, but its installed dependencies
        # are not application source. Detect every nested Python environment
        # by its standard marker so this also works for names such as env or
        # venv and for environments created by either uv or stdlib venv.
        directory = os.path.dirname(source)
        while directory != _root:
            if os.path.isfile(os.path.join(directory, "pyvenv.cfg")):
                return False
            parent = os.path.dirname(directory)
            if parent == directory:
                return False
            directory = parent
        return True
    except ValueError:
        return False

def _safe(value, name=""):
    return _values.safe_value(value, name)

def _locals(frame):
    return {str(name): _safe(value, str(name)) for name, value in frame.f_locals.items() if not str(name).startswith("__")}

def _selected_globals(frame):
    names = [name for name in frame.f_globals if not name.startswith("__")][:20]
    return {name: _safe(frame.f_globals[name], name) for name in names}

def _arguments(frame):
    count = frame.f_code.co_argcount + frame.f_code.co_kwonlyargcount
    return {name: _safe(frame.f_locals[name], name) for name in frame.f_code.co_varnames[:count] if name in frame.f_locals}

def _changes(frame, current):
    key = id(frame)
    before = _snapshots.get(key, {})
    _snapshots[key] = current
    return _values.binding_changes(current, before)

def _task_info():
    if asyncio is None:
        return {}
    try:
        task = asyncio.current_task()
        if task is None:
            return {}
        task_id = str(id(task))
        if task_id not in _tasks_seen:
            _tasks_seen.add(task_id)
            _task_lifecycle(task, "created")
            def finished(done):
                try:
                    cancelled = done.cancelled()
                except Exception:
                    cancelled = False
                _task_lifecycle(done, "cancelled" if cancelled else "completed")
            task.add_done_callback(finished)
        return {"task_id": task_id, "task_name": task.get_name() if hasattr(task, "get_name") else "task",
                "run_thread_id": "%s:%s" % (_process_id, threading.get_ident())}
    except (RuntimeError, AttributeError):
        return {}

def _command(thread_id=None):
    global _seen_sequence
    try:
        with open(_control, encoding="utf-8") as handle:
            message = json.load(handle)
        target = message.get("thread_id")
        if target is not None and thread_id is not None and target != thread_id:
            return None
        if message.get("sequence", -1) != _seen_sequence:
            _seen_sequence = message["sequence"]
            return message
    except (OSError, ValueError, KeyError):
        pass
    return None

_hash_cache = {}

def _source_hash(filename):
    try:
        stat = os.stat(filename)
        cached = _hash_cache.get(filename)
        if cached and cached[0] == stat.st_mtime_ns:
            return cached[1]
        with open(filename, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        _hash_cache[filename] = (stat.st_mtime_ns, digest)
        return digest
    except OSError:
        return None

def _load_config():
    global _config_mtime, _breakpoint_records, _focus_task_id
    if not _config:
        return
    try:
        mtime = os.stat(_config).st_mtime_ns
        if mtime == _config_mtime:
            return
        with open(_config, encoding="utf-8") as handle:
            data = json.load(handle)
        records = {}
        for item in data.get("breakpoints", []):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            kind = item.get("kind", "breakpoint")
            if kind not in ("breakpoint", "tracepoint", "watchpoint"):
                continue
            line = item.get("line")
            if kind != "watchpoint" and (not isinstance(line, int) or line < 1):
                continue
            key = (os.path.realpath(item["path"]), line if isinstance(line, int) else 0, kind, item.get("symbol") or "")
            records[key] = item
        _breakpoint_records = records
        focus = data.get("focus_task_id")
        _focus_task_id = focus if isinstance(focus, str) and focus else None
        _config_mtime = mtime
    except (OSError, ValueError, KeyError, TypeError):
        return

def _location_active(record, filename, frame):
    expected = record.get("source_hash")
    if expected and _source_hash(filename) != expected:
        return False
    symbol = record.get("symbol")
    return not symbol or frame.f_code.co_name == symbol

def _active_record(record, filename, line, frame):
    if not _location_active(record, filename, frame):
        return False
    if record.get("line") not in (None, line):
        return False
    variables = {**frame.f_globals, **frame.f_locals}
    return _values.condition_result(record.get("expression") or record.get("condition"), variables)

def _at_breakpoint(filename, line, frame):
    _load_config()
    path = os.path.realpath(filename)
    for key, record in _breakpoint_records.items():
        if key[0] == path and key[2] == "breakpoint" and _active_record(record, filename, line, frame):
            return True
    return False

def _checkpoints(filename, line, frame, event):
    """Apply tracepoints and watchpoints; return whether this thread should pause."""
    _load_config()
    path = os.path.realpath(filename)
    pause = False
    for key, record in list(_breakpoint_records.items()):
        if key[0] != path:
            continue
        if key[2] == "tracepoint":
            if key[1] == line and _active_record(record, filename, line, frame):
                _publish({"protocol_version": 1, "event_schema_version": 2, "ordinal": 0,
                          "kind": "tracepoint", "path": filename, "line": line,
                          "name": frame.f_code.co_name, "thread_id": threading.get_ident(),
                          "tracepoint": record.get("symbol") or str(line), "wall_time": time.time(),
                          "elapsed_seconds": time.monotonic() - _started_monotonic})
        if key[2] == "watchpoint" and event == "line":
            expression = record.get("expression") or record.get("condition")
            if not expression or not _location_active(record, filename, frame):
                continue
            try:
                value = _values.evaluate_expression(expression, {**frame.f_globals, **frame.f_locals})
                current = _values.serialise(_values.safe_value(value, "watch"))
            except Exception:
                continue
            watch_key = (threading.get_ident(), key)
            if watch_key in _watch_values and _watch_values[watch_key] != current:
                _publish({"protocol_version": 1, "event_schema_version": 2, "ordinal": 0,
                          "kind": "watchpoint", "path": filename, "line": line,
                          "name": frame.f_code.co_name, "thread_id": threading.get_ident(),
                          "watchpoint": expression, "wall_time": time.time(),
                          "elapsed_seconds": time.monotonic() - _started_monotonic})
                pause = True
            _watch_values[watch_key] = current
    return pause

def _publish(item):
    if _event_socket and _session_id:
        try:
            payload = {"kind": "event", "protocol_version": 1, "session_id": _session_id, "event": item}
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.05)
                connection.connect(_event_socket)
                connection.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
            return
        except OSError:
            pass
    if _log:
        try:
            with open(_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, sort_keys=True) + "\n")
        except OSError:
            pass

def _emit_aux(item):
    global _count
    if _count >= _maximum:
        return
    _count += 1
    item.setdefault("thread_id", threading.get_ident())
    item.setdefault("call_depth", _depths.get(threading.get_ident(), 0))
    item.setdefault("path", "")
    item.setdefault("relative_path", "")
    item.setdefault("line", 0)
    item.setdefault("name", "")
    item.setdefault("evidence_class", "observed at runtime")
    item.update({"run_id": _run_id, "process_id": _process_id,
                 "parent_process_id": _parent_process_id, "pid": os.getpid(),
                 "wall_time": time.time(), "elapsed_seconds": time.monotonic() - _started_monotonic,
                 "cpu_seconds": time.process_time() - _started_cpu_time})
    _publish(item)

def _process_lifecycle(lifecycle):
    _emit_aux({"kind": "process", "lifecycle": lifecycle, "ppid": os.getppid(),
               "executable": sys.executable, "argv": _argv, "instrumentation": _instrumentation})

def _thread_lifecycle(thread_id, lifecycle):
    _emit_aux({"kind": "thread", "thread_id": thread_id, "lifecycle": lifecycle,
               "run_thread_id": "%s:%s" % (_process_id, thread_id),
               "name": threading.current_thread().name})

def _task_lifecycle(task, lifecycle):
    try:
        task_id = str(id(task))
        name = task.get_name() if hasattr(task, "get_name") else "task"
    except Exception:
        return
    _emit_aux({"kind": "task", "task_id": task_id, "task_name": name, "lifecycle": lifecycle,
               "thread_id": threading.get_ident(),
               "run_thread_id": "%s:%s" % (_process_id, threading.get_ident())})

def _current_task_lifecycle(lifecycle):
    try:
        task = asyncio.current_task() if asyncio is not None else None
    except Exception:
        return
    if task is not None:
        _task_lifecycle(task, lifecycle)

def _wait(thread_id, depth):
    global _default_mode, _target_thread, _target_depth
    while True:
        # A global continue must release every thread already waiting here,
        # not only the thread that happened to read the command file first.
        if _default_mode == "continue" and _modes.get(thread_id) != "paused":
            return
        message = _command(thread_id)
        action = message.get("action") if message else None
        target = message.get("thread_id", thread_id) if message else thread_id
        if action == "continue":
            _default_mode = "continue"
            _modes.clear()
            return
        if action == "pause":
            if message.get("thread_id") is None:
                _default_mode = "paused"
                _modes.clear()
            else:
                _modes[target] = "paused"
            return
        if action == "step":
            _modes[target] = "paused"
            return
        if action in ("step_over", "step_out"):
            _modes[target] = action
            _target_thread = target
            _target_depth = message.get("depth", depth)
            return
        time.sleep(0.02)

def _trace(frame, event, arg):
    global _count, _ordinal, _frame_sequence
    filename = frame.f_code.co_filename
    if not _in_root(filename):
        # The global hook will still see calls into project code, even when
        # their callers are in dependencies. Do not trace every line of those
        # dependencies: it can prevent interactive programs from starting.
        return None
    if event not in ("call", "line", "return", "exception"):
        return _trace
    if _count >= _maximum:
        return None
    thread_id = threading.get_ident()
    if thread_id not in _threads_seen:
        _threads_seen.add(thread_id)
        _thread_lifecycle(thread_id, "started")
    depth = _depths.get(thread_id, 0)
    if event == "call":
        depth += 1
        _depths[thread_id] = depth
        if id(frame) in _suspended_frames:
            event = "resume"
            _suspended_frames.discard(id(frame))
        else:
            _frame_sequence += 1
            _frame_ids[id(frame)] = "%s:%s" % (os.getpid(), _frame_sequence)
            if frame.f_code.co_flags & (0x20 | 0x80 | 0x200):
                _generator_frames.add(id(frame))
    frame_id = _frame_ids.get(id(frame))
    values = _locals(frame)
    _ordinal += 1
    item = {
        "protocol_version": 1, "event_schema_version": 2, "ordinal": _ordinal, "kind": event,
        "run_id": _run_id, "process_id": _process_id, "parent_process_id": _parent_process_id,
        "pid": os.getpid(), "thread_id": thread_id,
        "run_thread_id": "%s:%s" % (_process_id, thread_id),
        "path": filename, "line": frame.f_lineno,
        "name": frame.f_code.co_name, "call_depth": depth,
        "frame_id": frame_id, "is_coroutine": bool(frame.f_code.co_flags & 0x80),
        "wall_time": time.time(), "elapsed_seconds": time.monotonic() - _started_monotonic,
        "cpu_seconds": time.process_time() - _started_cpu_time,
        "variables": _changes(frame, values) if event in ("call", "line") else {},
        "globals": _selected_globals(frame) if event == "call" else {},
    }
    task = _task_info()
    if event == "resume" and task.get("task_id"):
        _current_task_lifecycle("resumed")
    _load_config()
    # A focused task is a deliberate, non-invasive way to make busy asyncio
    # applications readable. Other tasks keep running; ThreadTrace simply neither
    # records nor pauses them until focus is cleared.
    if _focus_task_id and task.get("task_id") != _focus_task_id:
        if event == "return":
            _depths[thread_id] = max(0, depth - 1)
            if id(frame) in _generator_frames:
                _suspended_frames.add(id(frame))
            else:
                _frame_ids.pop(id(frame), None)
                _snapshots.pop(id(frame), None)
        # Never let changing the observation filter resume a process that is
        # already stopped at a debugger gate. The user must still issue a
        # normal step/continue command.
        if _modes.get(thread_id, _default_mode) == "paused":
            _wait(thread_id, depth)
        return _trace
    item.update(task)
    if event == "call":
        item["inputs"] = values
        item["arguments"] = _arguments(frame)
    elif event == "return":
        if id(frame) in _generator_frames:
            item["kind"] = "yield"
            _suspended_frames.add(id(frame))
            if task.get("task_id"):
                _current_task_lifecycle("suspended")
        else:
            item["output"] = _safe(arg, "return")
            _snapshots.pop(id(frame), None)
            _frame_ids.pop(id(frame), None)
    elif event == "exception":
        item["exception"] = type(arg[1]).__name__ if isinstance(arg, tuple) and len(arg) > 1 else "Exception"
        item["exception_message"] = _safe(arg[1], "exception") if isinstance(arg, tuple) and len(arg) > 1 else {}
    _count += 1
    _publish(item)
    checkpoint = _checkpoints(filename, frame.f_lineno, frame, event)
    if event == "return":
        _depths[thread_id] = max(0, depth - 1)
        if item["kind"] != "yield" and depth <= 1 and thread_id not in _finished_threads:
            _finished_threads.add(thread_id)
            _thread_lifecycle(thread_id, "finished")
    mode = _modes.get(thread_id, _default_mode)
    if mode == "continue":
        message = _command(thread_id)
        action = message.get("action") if message else None
        if action == "pause":
            _modes[thread_id] = "paused"
        elif action and action != "paused":
            _modes[thread_id] = "paused" if action == "step" else action
            _target_thread = message.get("thread_id", thread_id)
            _target_depth = message.get("depth", depth)
        if action is None and not _at_breakpoint(filename, frame.f_lineno, frame) and not checkpoint:
            return _trace
        _modes[thread_id] = "paused"
    if checkpoint:
        _modes[thread_id] = "paused"
    mode = _modes.get(thread_id, _default_mode)
    if mode == "step_over":
        if thread_id != _target_thread or depth > _target_depth:
            return _trace
        _modes[thread_id] = "paused"
    elif mode == "step_out":
        if thread_id != _target_thread or depth >= _target_depth:
            return _trace
        _modes[thread_id] = "paused"
    _wait(thread_id, depth)
    return _trace

def _audit(name, args):
    if name in {"import", "subprocess.Popen", "os.system", "socket.connect", "open"}:
        kind = "import" if name == "import" else "process" if name in {"subprocess.Popen", "os.system"} else "audit"
        _emit_aux({"protocol_version": 1, "event_schema_version": 2, "ordinal": 0, "kind": kind,
                   "audit_event": name, "argument_count": len(args)})

_process_lifecycle("started")
atexit.register(lambda: _process_lifecycle("ended"))
_waits.install(_emit_aux, _root)
if _instrumentation in {"profile", "all"}:
    sys.setprofile(_trace)
if _instrumentation not in {"profile"}:
    sys.settrace(_trace)
    try:
        import threading
        threading.settrace(_trace)
    except Exception:
        pass
if _instrumentation in {"audit", "all"}:
    sys.addaudithook(_audit)
'''


def _write_runtime_support(directory: Path, trace_source: str) -> None:
    """Write self-contained tracing support for the target interpreter."""
    (directory / "sitecustomize.py").write_text(trace_source, encoding="utf-8")
    adapter_source = Path(__file__).with_name("adapters.py").read_text(encoding="utf-8")
    (directory / "_threadtrace_runtime_adapters.py").write_text(adapter_source, encoding="utf-8")
    value_source = Path(__file__).with_name("values.py").read_text(encoding="utf-8")
    (directory / "_threadtrace_runtime_values.py").write_text(value_source, encoding="utf-8")
    wait_source = Path(__file__).with_name("waits.py").read_text(encoding="utf-8")
    (directory / "_threadtrace_runtime_waits.py").write_text(wait_source, encoding="utf-8")
