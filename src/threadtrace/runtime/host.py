"""PTY-preserving live runtime host and session discovery for interactive programs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .session import _trace_source


def _sessions_root() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    candidates = ([Path(runtime_dir)] if runtime_dir else []) + [Path("/tmp")]
    for parent in candidates:
        base = parent / f"threadtrace-live-{os.getuid()}"
        try:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            base.chmod(0o700)
            return base
        except OSError:
            continue
    raise OSError("Thread could not create a private live-runtime session directory.")


def live_sessions(project_root: Path, session_id: str | None = None) -> list[dict[str, Any]]:
    """Read local live-session metadata owned by this user, newest first."""
    root = project_root.resolve()
    found: list[dict[str, Any]] = []
    for directory in _sessions_root().iterdir():
        metadata_path = directory / "session.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if Path(metadata["project_root"]).resolve() != root:
                continue
            if session_id and metadata.get("session_id") != session_id:
                continue
            metadata["directory"] = str(directory)
            found.append(metadata)
        except (OSError, ValueError, KeyError):
            continue
    return sorted(found, key=lambda item: item.get("updated_at", ""), reverse=True)


class RuntimeHost:
    """Run an explicit command while preserving its real terminal/PTY.

    The target program owns stdin/stdout/stderr exactly as if it had been run
    directly.  Thread's injected trace hook writes safe events to a local
    current-user session directory for a separate listener dashboard.
    """

    def __init__(self, project_root: Path, project_name: str, command: list[str], event_cap: int = 100_000) -> None:
        if not command:
            raise ValueError("A command is required after '--'.")
        self.project_root = project_root.resolve()
        self.project_name = project_name
        self.command = command
        self.event_cap = event_cap
        self.session_id = uuid.uuid4().hex[:12]
        self.directory = _sessions_root() / self.session_id
        self.directory.mkdir(mode=0o700)
        self.log_path = self.directory / "events.jsonl"
        self.control_path = self.directory / "control.json"
        self.config_path = self.directory / "config.json"
        self.metadata_path = self.directory / "session.json"

    def run(self) -> int:
        # Interactive programs such as Urwid need the original controlling
        # terminal, not pipes owned by Thread. Be explicit here rather than
        # relying on Popen's implicit defaults: stdin remains where the user
        # types, while stdout/stderr keep their terminal capabilities (colour,
        # alternate screen, resize notifications, and isatty()).
        streams = (sys.stdin, sys.stdout, sys.stderr)
        if not all(getattr(stream, "isatty", lambda: False)() for stream in streams):
            raise OSError(
                "Runtime Host needs a real interactive terminal. Run it directly in the terminal "
                "where the target TUI should appear; do not pipe or redirect runtime-host output."
            )
        (self.directory / "sitecustomize.py").write_text(_trace_source(), encoding="utf-8")
        self.control_path.write_text(json.dumps({"sequence": 1, "action": "continue"}), encoding="utf-8")
        self.config_path.write_text(json.dumps({"protocol_version": 1, "breakpoints": []}), encoding="utf-8")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(self.directory), environment.get("PYTHONPATH", "")]))
        environment["THREADTRACE_RUNTIME_LOG"] = str(self.log_path)
        environment["THREADTRACE_RUNTIME_CONTROL"] = str(self.control_path)
        environment["THREADTRACE_RUNTIME_CONFIG"] = str(self.config_path)
        environment["THREADTRACE_RUNTIME_ROOT"] = str(self.project_root)
        environment["THREADTRACE_RUNTIME_MAX_EVENTS"] = str(self.event_cap)
        self._write_metadata("starting")
        process = subprocess.Popen(
            self.command,
            cwd=self.project_root,
            env=environment,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            # Keep the child in the terminal's foreground process group. This
            # is what lets Urwid receive keystrokes, terminal resizes and Ctrl-C
            # as if it had been run without Thread.
            start_new_session=False,
            close_fds=False,
        )
        self._write_metadata("running", pid=process.pid)
        try:
            code = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            code = process.wait()
        self._write_metadata("finished", pid=process.pid, exit_code=code)
        return code

    def _write_metadata(self, status: str, pid: int | None = None, exit_code: int | None = None) -> None:
        data = {
            "session_id": self.session_id,
            "project_name": self.project_name,
            "project_root": str(self.project_root),
            "command": self.command,
            "status": status,
            "pid": pid,
            "exit_code": exit_code,
            "log_path": str(self.log_path),
            "config_path": str(self.config_path),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        temporary = self.directory / "session.next.json"
        temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.metadata_path)
