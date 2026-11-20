"""PTY-preserving runtime host and private local listener transport."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..formats import TRACE_PROTOCOL_VERSION
from .schema import instrumentation_plan, normalise_event
from .session import _trace_source, _write_runtime_support

_PROTOCOL_VERSION = TRACE_PROTOCOL_VERSION
_MAX_MESSAGE_BYTES = 1_000_000
_MAX_EVENT_BYTES = 128 * 1024
_MAX_PAGE_EVENTS = 200
_MAX_PAGE_BYTES = 256 * 1024
_ACTIVE_SESSION_SECONDS = 60.0
_SESSION_RETENTION_SECONDS = 24 * 60 * 60
_LISTENER_TAKEOVER_SECONDS = 2.0
_SESSION_DIRECTORY = re.compile(r"[0-9a-f]{12}")
_HEARTBEAT_SECONDS = 10.0


class LiveTransportError(RuntimeError):
    """The local host cannot accept this listener request."""


def _user_token() -> str:
    try:
        return str(os.getuid())
    except AttributeError:
        import getpass
        return re.sub(r"[^A-Za-z0-9_.-]", "_", getpass.getuser()) or "user"


def _sessions_root() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if os.name == "nt":
        candidates = [Path(tempfile.gettempdir())]
    else:
        candidates = ([Path(runtime_dir)] if runtime_dir else []) + [Path("/tmp")]
    for parent in candidates:
        base = parent / f"threadtrace-live-{_user_token()}"
        try:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            if base.is_symlink() or not base.is_dir():
                continue
            if hasattr(os, "getuid") and base.stat().st_uid != os.getuid():
                continue
            base.chmod(0o700)
            return base
        except OSError:
            continue
    raise OSError("ThreadTrace could not create a private live-runtime session directory.")


def _session_time(metadata: dict[str, Any], directory: Path) -> float:
    try:
        return datetime.fromisoformat(str(metadata["updated_at"])).timestamp()
    except (KeyError, ValueError):
        try:
            return directory.stat().st_mtime
        except OSError:
            return 0.0


def _session_active(metadata: dict[str, Any], directory: Path, now: float | None = None) -> bool:
    return (metadata.get("status") in {"starting", "running"}
            and (now or time.time()) - _session_time(metadata, directory) <= _ACTIVE_SESSION_SECONDS)


def cleanup_live_sessions(now: float | None = None) -> list[str]:
    """Remove expired sessions after validating that each path belongs to this user."""
    root = _sessions_root()
    cutoff = (now or time.time()) - _SESSION_RETENTION_SECONDS
    removed: list[str] = []
    for directory in root.iterdir():
        try:
            if directory.is_symlink() or not directory.is_dir() or not _SESSION_DIRECTORY.fullmatch(directory.name):
                continue
            if directory.resolve().parent != root.resolve():
                continue
            if hasattr(os, "getuid") and directory.stat().st_uid != os.getuid():
                continue
            metadata: dict[str, Any] = {}
            try:
                loaded = json.loads((directory / "session.json").read_text(encoding="utf-8"))
                metadata = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError, TypeError):
                pass
            if _session_time(metadata, directory) >= cutoff:
                continue
            shutil.rmtree(directory)
            removed.append(directory.name)
        except OSError:
            continue
    return removed


def live_sessions(project_root: Path, session_id: str | None = None,
                  include_finished: bool = False) -> list[dict[str, Any]]:
    """Read current-user session metadata; defaults to live sessions only."""
    root = project_root.resolve()
    cleanup_live_sessions()
    found: list[dict[str, Any]] = []
    now = time.time()
    for directory in _sessions_root().iterdir():
        metadata_path = directory / "session.json"
        try:
            if directory.is_symlink() or not directory.is_dir():
                continue
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if Path(metadata["project_root"]).resolve() != root:
                continue
            if session_id and metadata.get("session_id") != session_id:
                continue
            active = _session_active(metadata, directory, now)
            if not session_id and not include_finished and not active:
                continue
            metadata["directory"] = str(directory)
            metadata["active"] = active
            found.append(metadata)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(found, key=lambda item: (bool(item.get("active")), item.get("updated_at", "")), reverse=True)


def receive_live_events(metadata: dict[str, Any], after: int, listener_id: str,
                        limit: int | None = None, max_bytes: int | None = None
                        ) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    """Fetch one bounded page of events from an active Host."""
    socket_path = metadata.get("socket_path")
    session_id = metadata.get("session_id")
    if not isinstance(socket_path, str) or not isinstance(session_id, str):
        raise LiveTransportError("This Host session does not provide local IPC.")
    if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
        raise LiveTransportError("Local Host/Listener IPC is unavailable on this platform.")
    request: dict[str, Any] = {"kind": "listen", "protocol_version": _PROTOCOL_VERSION,
                               "session_id": session_id, "listener_id": listener_id, "after": after}
    if isinstance(limit, int) and limit > 0:
        request["limit"] = limit
    if isinstance(max_bytes, int) and max_bytes > 0:
        request["max_bytes"] = max_bytes
    reply = _request_socket(Path(socket_path), request)
    if not reply.get("ok"):
        raise LiveTransportError(str(reply.get("error", "The Host rejected the listener.")))
    events = reply.get("events")
    latest = reply.get("latest")
    received = reply.get("received", latest)
    next_position = reply.get("next", after + len(events) if isinstance(events, list) else after)
    if (not isinstance(events, list) or not isinstance(latest, int) or not isinstance(received, int)
            or not isinstance(next_position, int)):
        raise LiveTransportError("The Host returned an invalid event response.")
    status = {
        "next": next_position,
        "lag": max(0, latest - next_position),
        "dropped": int(reply.get("dropped", 0)),
        "oversized": int(reply.get("oversized", 0)),
        "journal_truncated": bool(reply.get("journal_truncated", False)),
        "journal_error": reply.get("journal_error"),
        "received": received,
    }
    return [event for event in events if isinstance(event, dict)], latest, status


def release_live_listener(metadata: dict[str, Any], listener_id: str) -> None:
    """Release this listener identity when its terminal closes."""
    socket_path = metadata.get("socket_path")
    session_id = metadata.get("session_id")
    if not isinstance(socket_path, str) or not isinstance(session_id, str) or not hasattr(socket, "AF_UNIX"):
        return
    try:
        _request_socket(Path(socket_path), {"kind": "release", "protocol_version": _PROTOCOL_VERSION,
                                            "session_id": session_id, "listener_id": listener_id})
    except LiveTransportError:
        pass


def _request_socket(path: Path, message: dict[str, Any]) -> dict[str, Any]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.3)
            connection.connect(str(path))
            connection.sendall(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            payload = _receive_message(connection)
    except OSError as error:
        raise LiveTransportError("The Host is no longer available.") from error
    try:
        reply = json.loads(payload)
    except ValueError as error:
        raise LiveTransportError("The Host returned an invalid response.") from error
    if not isinstance(reply, dict):
        raise LiveTransportError("The Host returned an invalid response.")
    return reply


def _receive_message(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < _MAX_MESSAGE_BYTES:
        chunk = connection.recv(min(65536, _MAX_MESSAGE_BYTES - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b"\n" in chunk:
            break
    payload = b"".join(chunks).split(b"\n", 1)[0]
    if not payload or total >= _MAX_MESSAGE_BYTES:
        raise OSError("Invalid local IPC message.")
    return payload


class _LiveSocketServer:
    """Current-user Unix socket used by one Host session."""

    def __init__(self, session_id: str, socket_path: Path, journal_path: Path, event_cap: int) -> None:
        self.session_id = session_id
        self.socket_path = socket_path
        self.journal_path = journal_path
        self.event_cap = event_cap
        self.events: list[dict[str, Any]] = []
        self.received = 0
        self.dropped = 0
        self.oversized = 0
        self.journal_error: str | None = None
        self._listener_id: str | None = None
        self._listener_seen_at = 0.0
        self._lock = threading.Lock()
        self._running = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.name == "nt":
            raise OSError("Host/Listener IPC is not yet supported on Windows; the Host keeps its local journal.")
        if not hasattr(socket, "AF_UNIX"):
            raise OSError("Unix-domain sockets are unavailable on this platform.")
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
        except OSError:
            server.close()
            raise
        self.socket_path.chmod(0o600)
        server.listen(16)
        server.settimeout(0.1)
        self._socket = server
        self._running.set()
        self._thread = threading.Thread(target=self._serve, name="threadtrace-live-ipc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._socket:
            self._socket.close()
        if self._thread:
            self._thread.join(timeout=1)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def _serve(self) -> None:
        assert self._socket is not None
        while self._running.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(1.0)
            if not self._current_user(connection):
                self._reply(connection, {"ok": False, "error": "Current-user access is required."})
                return
            try:
                message = json.loads(_receive_message(connection))
            except (OSError, ValueError, TimeoutError):
                self._reply(connection, {"ok": False, "error": "Invalid IPC request."})
                return
            if not isinstance(message, dict) or message.get("session_id") != self.session_id:
                self._reply(connection, {"ok": False, "error": "Session identity does not match."})
                return
            if message.get("protocol_version") != _PROTOCOL_VERSION:
                self._reply(connection, {"ok": False, "error": "Unsupported IPC protocol."})
                return
            kind = message.get("kind")
            if kind == "event":
                item = message.get("event")
                if not isinstance(item, dict):
                    self._reply(connection, {"ok": False, "error": "Invalid trace event."})
                    return
                self._record(item)
                self._reply(connection, {"ok": True})
                return
            listener_id = message.get("listener_id")
            if not isinstance(listener_id, str) or not listener_id:
                self._reply(connection, {"ok": False, "error": "A listener identity is required."})
                return
            if kind == "release":
                with self._lock:
                    if self._listener_id == listener_id:
                        self._listener_id = None
                self._reply(connection, {"ok": True})
                return
            if kind != "listen":
                self._reply(connection, {"ok": False, "error": "Unknown IPC request."})
                return
            after = message.get("after")
            if not isinstance(after, int) or after < 0:
                self._reply(connection, {"ok": False, "error": "Invalid event position."})
                return
            requested_limit = message.get("limit", _MAX_PAGE_EVENTS)
            requested_bytes = message.get("max_bytes", _MAX_PAGE_BYTES)
            if not isinstance(requested_limit, int) or not 1 <= requested_limit <= _MAX_PAGE_EVENTS:
                self._reply(connection, {"ok": False, "error": "Invalid page size."})
                return
            if not isinstance(requested_bytes, int) or not 1024 <= requested_bytes <= _MAX_PAGE_BYTES:
                self._reply(connection, {"ok": False, "error": "Invalid byte limit."})
                return
            with self._lock:
                now = time.monotonic()
                if self._listener_id not in (None, listener_id) and now - self._listener_seen_at < _LISTENER_TAKEOVER_SECONDS:
                    self._reply(connection, {"ok": False, "error": "Another Listener is already active."})
                    return
                self._listener_id = listener_id
                self._listener_seen_at = now
                latest = len(self.events)
                page: list[dict[str, Any]] = []
                size = 0
                for item in self.events[after:after + requested_limit]:
                    encoded = json.dumps(item, separators=(",", ":"), sort_keys=True).encode()
                    if page and size + len(encoded) > requested_bytes:
                        break
                    page.append(item)
                    size += len(encoded)
                next_position = after + len(page)
                status = {
                    "dropped": self.dropped,
                    "oversized": self.oversized,
                    "journal_truncated": self.dropped > 0,
                    "journal_error": self.journal_error,
                    "received": self.received,
                    "lag": max(0, latest - next_position),
                }
            self._reply(connection, {"ok": True, "events": page, "latest": latest,
                                     "next": next_position, **status})

    def _record(self, item: dict[str, Any]) -> None:
        with self._lock:
            self.received += 1
            item = normalise_event(dict(item), self.received)
            try:
                encoded = json.dumps(item, separators=(",", ":"), sort_keys=True).encode()
            except (TypeError, ValueError):
                self.dropped += 1
                return
            if len(encoded) > _MAX_EVENT_BYTES:
                self.oversized += 1
                self.dropped += 1
                return
            if len(self.events) >= self.event_cap:
                self.dropped += 1
                return
            self.events.append(item)
            try:
                with self.journal_path.open("ab") as journal:
                    journal.write(encoded + b"\n")
            except OSError as error:
                self.journal_error = str(error)

    @staticmethod
    def _reply(connection: socket.socket, payload: dict[str, Any]) -> None:
        try:
            connection.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        except OSError:
            pass

    @staticmethod
    def _current_user(connection: socket.socket) -> bool:
        if os.name != "posix" or not hasattr(socket, "SO_PEERCRED"):
            return True
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _, uid, _ = struct.unpack("3i", credentials)
            return uid == os.getuid()
        except OSError:
            return False


class RuntimeHost:
    """Run an explicit command while preserving its terminal/PTY."""

    def __init__(self, project_root: Path, project_name: str, command: list[str], event_cap: int = 100_000,
                 cwd: Path | None = None, capture_level: str = "metadata", capture_limit: int = 4096,
                 redact: bool = True, instrumentation: str = "auto", include_third_party: bool = False) -> None:
        if not command:
            raise ValueError("A command is required after '--'.")
        self.project_root = project_root.resolve()
        self.cwd = (cwd or project_root).resolve()
        if not self.cwd.is_dir():
            raise ValueError(f"Runtime working directory does not exist: {self.cwd}")
        self.project_name = project_name
        self.command = command
        self.event_cap = event_cap
        if capture_level not in {"off", "metadata", "content"}:
            raise ValueError("Capture level must be off, metadata, or content.")
        if capture_limit < 1:
            raise ValueError("Capture limit must be positive.")
        self.capture_level = capture_level
        self.capture_limit = capture_limit
        self.redact = redact
        self.instrumentation = instrumentation
        self.include_third_party = include_third_party
        self.instrumentation_plan = instrumentation_plan(instrumentation)
        cleanup_live_sessions()
        self.session_id = uuid.uuid4().hex[:12]
        self.directory = _sessions_root() / self.session_id
        self.directory.mkdir(mode=0o700)
        self._metadata_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()
        self._finished = threading.Event()
        self._terminal_fd: int | None = None
        self._terminal_group: int | None = None
        self.log_path = self.directory / "events.jsonl"
        self.socket_path = self.directory / "listener.sock"
        self.control_path = self.directory / "control.json"
        self.config_path = self.directory / "config.json"
        self.metadata_path = self.directory / "session.json"
        self._transport: _LiveSocketServer | None = _LiveSocketServer(
            self.session_id, self.socket_path, self.log_path, self.event_cap
        )

    def run(self) -> int:
        streams = (sys.stdin, sys.stdout, sys.stderr)
        if not all(getattr(stream, "isatty", lambda: False)() for stream in streams):
            raise OSError("Runtime Host needs a real interactive terminal. Run it directly in the terminal where the target TUI should appear; do not pipe or redirect runtime-host output.")
        try:
            self._transport.start()
        except OSError:
            # Some confined environments prohibit local sockets. The target
            # still runs and retains its local journal, but Listener mode is
            # intentionally unavailable instead of silently using a weaker
            # transport.
            self._transport = None
        process: subprocess.Popen[Any] | None = None
        heartbeat: threading.Thread | None = None
        previous_sigterm: Any = None
        try:
            _write_runtime_support(self.directory, _trace_source())
            self.control_path.write_text(json.dumps({"sequence": 1, "action": "continue"}), encoding="utf-8")
            self.config_path.write_text(json.dumps({"protocol_version": 1, "breakpoints": []}), encoding="utf-8")
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(self.directory), environment.get("PYTHONPATH", "")]))
            environment["THREADTRACE_RUNTIME_LOG"] = str(self.log_path)
            if self._transport:
                environment["THREADTRACE_RUNTIME_SOCKET"] = str(self.socket_path)
                environment["THREADTRACE_RUNTIME_SESSION"] = self.session_id
            environment["THREADTRACE_RUNTIME_CONTROL"] = str(self.control_path)
            environment["THREADTRACE_RUNTIME_CONFIG"] = str(self.config_path)
            environment["THREADTRACE_RUNTIME_ROOT"] = str(self.project_root)
            environment["THREADTRACE_RUNTIME_MAX_EVENTS"] = str(self.event_cap)
            environment["THREADTRACE_RUNTIME_CAPTURE_LEVEL"] = self.capture_level
            environment["THREADTRACE_RUNTIME_CAPTURE_LIMIT"] = str(self.capture_limit)
            environment["THREADTRACE_RUNTIME_REDACT"] = "1" if self.redact else "0"
            environment["THREADTRACE_RUNTIME_INSTRUMENTATION"] = self.instrumentation
            environment["THREADTRACE_RUNTIME_INCLUDE_THIRD_PARTY"] = "1" if self.include_third_party else "0"
            environment["THREADTRACE_RUNTIME_RUN_ID"] = self.session_id
            self._write_metadata("starting")
            process_group = {"process_group": 0} if os.name == "posix" else {}
            process = subprocess.Popen(self.command, cwd=self.cwd, env=environment, stdin=sys.stdin, stdout=sys.stdout,
                                       stderr=sys.stderr, start_new_session=False, close_fds=False, **process_group)
            self._move_terminal_to(process.pid)
            self._write_metadata("running", pid=process.pid)
            heartbeat = threading.Thread(target=self._heartbeat, args=(process.pid,), name="threadtrace-live-heartbeat",
                                         daemon=True)
            heartbeat.start()
            if os.name == "posix":
                def interrupt(_signum: int, _frame: Any) -> None:
                    raise KeyboardInterrupt
                previous_sigterm = signal.signal(signal.SIGTERM, interrupt)
            try:
                code = process.wait()
            except KeyboardInterrupt:
                stopped = self._stop_process(process)
                code = stopped if stopped is not None else 130
            self._heartbeat_stop.set()
            self._finished.set()
            heartbeat.join(timeout=1)
            heartbeat = None
            self._write_metadata("finished", pid=process.pid, exit_code=code)
            return code
        finally:
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
            self._heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=1)
            if process is not None and process.poll() is None:
                self._stop_process(process)
            self._restore_terminal()
            if self._transport:
                self._transport.stop()

    def _heartbeat(self, pid: int) -> None:
        while not self._heartbeat_stop.wait(_HEARTBEAT_SECONDS):
            try:
                self._write_metadata("running", pid=pid)
            except OSError:
                return

    def _stop_process(self, process: subprocess.Popen[Any]) -> int | None:
        """Stop the controlled process group, escalating only if it does not exit."""
        if process.poll() is not None:
            return process.returncode
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            return process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        try:
            return process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return None

    def _move_terminal_to(self, pid: int) -> None:
        if os.name != "posix":
            return
        try:
            descriptor = sys.stdin.fileno()
            group = os.tcgetpgrp(descriptor)
            self._set_terminal_group(descriptor, pid)
            self._terminal_fd = descriptor
            self._terminal_group = group
        except (OSError, ValueError, TypeError):
            self._terminal_fd = None
            self._terminal_group = None

    def _restore_terminal(self) -> None:
        if self._terminal_fd is None or self._terminal_group is None:
            return
        try:
            self._set_terminal_group(self._terminal_fd, self._terminal_group)
        except (OSError, ValueError, TypeError):
            pass
        self._terminal_fd = None
        self._terminal_group = None

    @staticmethod
    def _set_terminal_group(descriptor: int, group: int) -> None:
        previous: Any = None
        if hasattr(signal, "SIGTTOU"):
            previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        try:
            os.tcsetpgrp(descriptor, group)
        finally:
            if previous is not None:
                signal.signal(signal.SIGTTOU, previous)

    def _write_metadata(self, status: str, pid: int | None = None, exit_code: int | None = None) -> None:
        data = {"session_id": self.session_id, "run_id": self.session_id, "project_name": self.project_name, "project_root": str(self.project_root),
                "cwd": str(self.cwd), "command": self.command, "status": status, "pid": pid, "exit_code": exit_code,
                "log_path": str(self.log_path), "config_path": str(self.config_path),
                "capture_level": self.capture_level, "capture_limit": self.capture_limit,
                "redaction_enabled": self.redact, "instrumentation": self.instrumentation_plan,
                "recording_scope": "project+reached-third-party" if self.include_third_party else "project",
                "updated_at": datetime.now(UTC).isoformat()}
        if self._transport:
            data["socket_path"] = str(self.socket_path)
            data["socket_protocol_version"] = _PROTOCOL_VERSION
        temporary = self.directory / "session.next.json"
        with self._metadata_lock:
            if status == "running" and self._finished.is_set():
                return
            temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self.metadata_path)
