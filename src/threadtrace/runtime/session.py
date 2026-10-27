"""Controllable, opt-in Python runtime sessions used by the runtime dashboard."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class RuntimeSession:
    """Launch one explicit command and pause it at project-level trace events.

    This is a debugger-style aid, not a sandbox.  The target is never launched
    by construction; callers must explicitly invoke :meth:`start`.
    """

    def __init__(self, root: Path, command: list[str], timeout: int = 30, output_cap: int = 100_000,
                 event_cap: int = 10_000, runtime_environment: dict[str, str] | None = None) -> None:
        if not command:
            raise ValueError("A command is required after '--'.")
        self.root = root.resolve()
        self.command = command
        self.timeout = timeout
        self.output_cap = output_cap
        self.event_cap = event_cap
        self.runtime_environment = runtime_environment or {}
        self._directory = tempfile.TemporaryDirectory(prefix="threadtrace-runtime-session-")
        self.directory = Path(self._directory.name)
        self.log_path = self.directory / "events.jsonl"
        self.control_path = self.directory / "control.json"
        self.config_path = self.directory / "config.json"
        self.process: subprocess.Popen[bytes] | None = None
        self.started_at: float | None = None
        self.timed_out = False
        self.mode = "not_started"
        self._sequence = 0
        self._offset = 0
        self.events: list[dict[str, Any]] = []
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._output_lock = threading.Lock()
        self._breakpoints: set[tuple[str, int]] = set()
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
        (self.directory / "sitecustomize.py").write_text(_trace_source(), encoding="utf-8")
        self._write_config()
        self._send("paused")
        # A runtime session should start as closely as possible to a normal
        # invocation.  In particular, projects commonly need HOME, locale,
        # cloud credentials, or variables loaded by a wrapper such as `dev`.
        # Thread only adds its own namespaced tracing variables below.
        environment = os.environ.copy()
        environment.update(self.runtime_environment)
        inherited_path = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(self.directory), inherited_path]))
        environment["THREADTRACE_RUNTIME_LOG"] = str(self.log_path)
        environment["THREADTRACE_RUNTIME_CONTROL"] = str(self.control_path)
        environment["THREADTRACE_RUNTIME_CONFIG"] = str(self.config_path)
        environment["THREADTRACE_RUNTIME_ROOT"] = str(self.root)
        environment["THREADTRACE_RUNTIME_MAX_EVENTS"] = str(self.event_cap)
        self.process = subprocess.Popen(
            self.command, cwd=self.root, env=environment,
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
        self.mode = "paused"
        assert self.process.stdin is not None and self.process.stdout is not None and self.process.stderr is not None
        threading.Thread(target=self._drain, args=(self.process.stdout, self._stdout), daemon=True).start()
        threading.Thread(target=self._drain, args=(self.process.stderr, self._stderr), daemon=True).start()

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

    def step(self) -> None:
        self.start()
        self._send("step")
        self.mode = "paused"

    def step_over(self) -> None:
        """Resume nested calls and pause at the next event in this frame."""
        self.start()
        if not self.events:
            self.step()
            return
        current = self.events[-1] if self.events else {}
        self._send("step_over", thread_id=current.get("thread_id"), depth=current.get("call_depth"))
        self.mode = "step_over"

    def step_out(self) -> None:
        """Resume until the selected frame returns to its caller."""
        self.start()
        if not self.events:
            self.step()
            return
        current = self.events[-1] if self.events else {}
        self._send("step_out", thread_id=current.get("thread_id"), depth=current.get("call_depth"))
        self.mode = "step_out"

    def continue_run(self) -> None:
        self.start()
        self._send("continue")
        self.mode = "continue"

    def pause(self) -> None:
        if self.process and not self.finished:
            self._send("paused")
            self.mode = "paused"

    @property
    def breakpoints(self) -> set[tuple[str, int]]:
        """Absolute project-source locations where free running should pause."""
        return set(self._breakpoints)

    def toggle_breakpoint(self, path: Path | str, line: int) -> bool:
        """Toggle one project-local source breakpoint and return whether it is enabled."""
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Breakpoints must point to a file inside the project root.")
        if line < 1:
            raise ValueError("Breakpoint lines start at 1.")
        key = (str(resolved), line)
        if key in self._breakpoints:
            self._breakpoints.remove(key)
            enabled = False
        else:
            self._breakpoints.add(key)
            enabled = True
        self._write_config()
        return enabled

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
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = event.get("path")
            if not isinstance(path, str):
                continue
            try:
                resolved = Path(path).resolve()
                if not resolved.is_relative_to(self.root):
                    continue
                event["relative_path"] = resolved.relative_to(self.root).as_posix()
            except (OSError, ValueError):
                continue
            fresh.append(event)
            self.events.append(event)
        return fresh

    def result(self) -> dict[str, Any]:
        self.poll()
        exit_code = self.process.poll() if self.process else None
        with self._output_lock:
            stdout = bytes(self._stdout).decode(errors="replace")
            stderr = bytes(self._stderr).decode(errors="replace")
        return {
            "command": self.command,
            "exit_code": exit_code,
            "timed_out": self.timed_out,
            "stdout": stdout,
            "stderr": stderr,
            "output_truncated": len(self._stdout) >= self.output_cap or len(self._stderr) >= self.output_cap,
            "events": self.events,
        }

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
                {"path": path, "line": line}
                for path, line in sorted(self._breakpoints)
            ],
        }
        temporary = self.directory / "config.next.json"
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.config_path)

    def _drain(self, stream: Any, destination: bytearray) -> None:
        # BufferedReader.read(4096) is allowed to wait until the buffer is
        # full.  read1 returns a flushed prompt promptly, which is important
        # when the program is waiting for a response in Program input.
        reader = getattr(stream, "read1", stream.read)
        while chunk := reader(4096):
            with self._output_lock:
                remaining = self.output_cap - len(destination)
                if remaining > 0:
                    destination.extend(chunk[:remaining])


def _trace_source() -> str:
    """Source injected through sitecustomize in the explicit child process."""
    return r'''
import json
import os
import sys
import time
import threading
from functools import lru_cache
try:
    import asyncio
except Exception:
    asyncio = None

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
_log = os.environ.get("THREADTRACE_RUNTIME_LOG")
_control = os.environ.get("THREADTRACE_RUNTIME_CONTROL")
_config = os.environ.get("THREADTRACE_RUNTIME_CONFIG")
_maximum = int(os.environ.get("THREADTRACE_RUNTIME_MAX_EVENTS", "10000"))
_count = 0
_seen_sequence = -1
_mode = "paused"
_target_thread = None
_target_depth = None
_snapshots = {}
_frame_ids = {}
_depths = {}
_ordinal = 0
_frame_sequence = 0
_config_mtime = None
_breakpoints = set()
_focus_task_id = None
_sensitive = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential", "cookie", "auth")

@lru_cache(maxsize=4096)
def _in_root(filename):
    try:
        if not os.path.isabs(filename):
            return False
        source = os.path.realpath(filename)
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
    if any(word in name.lower() for word in _sensitive):
        return {"type": type(value).__name__, "value": "<redacted>"}
    if value is None or isinstance(value, bool):
        return {"type": type(value).__name__, "value": value}
    if isinstance(value, (int, float)):
        return {"type": type(value).__name__, "value": value}
    if isinstance(value, str):
        return {"type": "str", "length": len(value), "preview": value[:80]}
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value)}
    if isinstance(value, dict):
        return {"type": "dict", "length": len(value), "keys": [str(key)[:40] for key in list(value.keys())[:12]]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {"type": type(value).__name__, "length": len(value)}
    return {"type": type(value).__name__, "value": "<object>"}

def _locals(frame):
    return {str(name): _safe(value, str(name)) for name, value in frame.f_locals.items() if not str(name).startswith("__")}

def _changes(frame, current):
    key = id(frame)
    before = _snapshots.get(key, {})
    _snapshots[key] = current
    return {
        "created": {name: value for name, value in current.items() if name not in before},
        "changed": {name: value for name, value in current.items() if name in before and value != before[name]},
        "removed": sorted(name for name in before if name not in current),
    }

def _task_info():
    if asyncio is None:
        return {}
    try:
        task = asyncio.current_task()
        if task is None:
            return {}
        return {"task_id": str(id(task)), "task_name": task.get_name() if hasattr(task, "get_name") else "task"}
    except (RuntimeError, AttributeError):
        return {}

def _command():
    global _seen_sequence
    try:
        with open(_control, encoding="utf-8") as handle:
            message = json.load(handle)
        if message.get("sequence", -1) != _seen_sequence:
            _seen_sequence = message["sequence"]
            return message
    except (OSError, ValueError, KeyError):
        pass
    return None

def _load_config():
    global _config_mtime, _breakpoints, _focus_task_id
    if not _config:
        return
    try:
        mtime = os.stat(_config).st_mtime_ns
        if mtime == _config_mtime:
            return
        with open(_config, encoding="utf-8") as handle:
            data = json.load(handle)
        candidates = data.get("breakpoints", [])
        _breakpoints = {
            (os.path.realpath(item["path"]), int(item["line"]))
            for item in candidates
            if isinstance(item, dict) and isinstance(item.get("path"), str)
            and isinstance(item.get("line"), int) and item["line"] >= 1
        }
        focus = data.get("focus_task_id")
        _focus_task_id = focus if isinstance(focus, str) and focus else None
        _config_mtime = mtime
    except (OSError, ValueError, KeyError, TypeError):
        return

def _at_breakpoint(filename, line):
    _load_config()
    return (os.path.realpath(filename), line) in _breakpoints

def _wait(thread_id, depth):
    global _mode, _target_thread, _target_depth
    while True:
        message = _command()
        action = message.get("action") if message else None
        if action == "continue":
            _mode = "continue"
            return
        if action == "step":
            _mode = "paused"
            return
        if action in ("step_over", "step_out"):
            _mode = action
            _target_thread = message.get("thread_id", thread_id)
            _target_depth = message.get("depth", depth)
            return
        time.sleep(0.02)

def _trace(frame, event, arg):
    global _count, _mode, _ordinal, _frame_sequence
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
    depth = _depths.get(thread_id, 0)
    if event == "call":
        depth += 1
        _depths[thread_id] = depth
        _frame_sequence += 1
        _frame_ids[id(frame)] = "%s:%s" % (os.getpid(), _frame_sequence)
    frame_id = _frame_ids.get(id(frame))
    values = _locals(frame)
    _ordinal += 1
    item = {
        "protocol_version": 1, "ordinal": _ordinal, "kind": event, "path": filename, "line": frame.f_lineno,
        "name": frame.f_code.co_name, "thread_id": thread_id, "call_depth": depth,
        "frame_id": frame_id, "is_coroutine": bool(frame.f_code.co_flags & 0x80),
        "variables": _changes(frame, values) if event in ("call", "line") else {},
    }
    task = _task_info()
    _load_config()
    # A focused task is a deliberate, non-invasive way to make busy asyncio
    # applications readable. Other tasks keep running; Thread simply neither
    # records nor pauses them until focus is cleared.
    if _focus_task_id and task.get("task_id") != _focus_task_id:
        if event == "return":
            _depths[thread_id] = max(0, depth - 1)
            _frame_ids.pop(id(frame), None)
            _snapshots.pop(id(frame), None)
        # Never let changing the observation filter resume a process that is
        # already stopped at a debugger gate. The user must still issue a
        # normal step/continue command.
        if _mode == "paused":
            _wait(thread_id, depth)
        return _trace
    item.update(task)
    if event == "call":
        item["inputs"] = values
    elif event == "return":
        item["output"] = _safe(arg, "return")
        _snapshots.pop(id(frame), None)
        _frame_ids.pop(id(frame), None)
    elif event == "exception":
        item["exception"] = type(arg[1]).__name__ if isinstance(arg, tuple) and len(arg) > 1 else "Exception"
    _count += 1
    try:
        with open(_log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, sort_keys=True) + "\n")
    except OSError:
        pass
    if event == "return":
        _depths[thread_id] = max(0, depth - 1)
    if _mode == "continue":
        message = _command()
        action = message.get("action") if message else None
        if action != "paused" and not _at_breakpoint(filename, frame.f_lineno):
            return _trace
        _mode = "paused"
    if _mode == "step_over":
        if thread_id != _target_thread or depth > _target_depth:
            return _trace
        _mode = "paused"
    elif _mode == "step_out":
        if thread_id != _target_thread or depth >= _target_depth:
            return _trace
        _mode = "paused"
    _wait(thread_id, depth)
    return _trace

sys.settrace(_trace)
try:
    import threading
    threading.settrace(_trace)
except Exception:
    pass
'''
