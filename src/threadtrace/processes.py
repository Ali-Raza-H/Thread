"""Safe discovery and explicit restart support for the current user's Python processes."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

from .engine import ThreadError


def running_python_processes() -> list[dict[str, Any]]:
    """Return inspectable metadata for current-user Python interpreters on Linux.

    Process discovery is intentionally read-only and excludes other users.  It
    does not attach, inject, or signal any process.
    """
    if os.name != "posix" or not Path("/proc").is_dir():
        raise ThreadError("Running-process discovery currently requires Linux /proc.")
    uid = os.getuid()
    processes: list[dict[str, Any]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
            owner = next((int(line.split()[1]) for line in status.splitlines() if line.startswith("Uid:")), -1)
            if owner != uid:
                continue
            executable = os.readlink(entry / "exe")
            if "python" not in Path(executable).name.lower():
                continue
            raw = (entry / "cmdline").read_bytes().split(b"\0")
            raw_command = [part.decode(errors="replace") for part in raw if part]
            if not raw_command:
                continue
            cwd = Path(os.readlink(entry / "cwd"))
            environment = _runtime_environment(entry / "environ")
            root = _infer_root(cwd, raw_command)
            start_time = _start_time(entry / "stat")
            command = _redact_command(raw_command)
            processes.append({
                "pid": pid,
                "ppid": _parent_pid(status),
                "start_time": start_time,
                "executable": executable,
                "command": command,
                "command_text": " ".join(command),
                "_restart_command": raw_command,
                "cwd": str(cwd),
                "project_root": str(root) if root else None,
                "venv": environment.get("VIRTUAL_ENV") or _venv_from_executable(executable),
                "runtime_environment": environment,
            })
        except (OSError, ValueError, StopIteration):
            continue
    return sorted(processes, key=lambda item: item["pid"])


def public_process(process: dict[str, Any]) -> dict[str, Any]:
    """Remove in-memory restart data before process metadata is rendered/exported."""
    return {key: value for key, value in process.items() if not key.startswith("_")}


def process_tree(processes: list[dict[str, Any]], pid: int) -> list[dict[str, Any]]:
    """Return the selected process and all currently visible descendants."""
    by_parent: dict[int, list[dict[str, Any]]] = {}
    by_pid = {item["pid"]: item for item in processes}
    for item in processes:
        by_parent.setdefault(item["ppid"], []).append(item)
    result: list[dict[str, Any]] = []

    def visit(current: int) -> None:
        item = by_pid.get(current)
        if item:
            result.append(item)
        for child in sorted(by_parent.get(current, []), key=lambda row: row["pid"]):
            visit(child["pid"])

    visit(pid)
    return result


def stop_process_tree(selected: dict[str, Any]) -> list[int]:
    """Terminate exactly the selected current-user process and its descendants.

    A fresh discovery validates PID ownership and Linux start time first, which
    prevents signalling a recycled PID.  Descendants are terminated before the
    selected parent so workers do not become orphaned.
    """
    current = {item["pid"]: item for item in running_python_processes()}
    latest = current.get(selected["pid"])
    if not latest or latest.get("start_time") != selected.get("start_time"):
        raise ThreadError("Selected process is no longer running; refresh the process list before restarting.")
    tree = _owned_process_tree(selected["pid"])
    stopped: list[int] = []
    for item in reversed(tree):
        try:
            os.kill(item["pid"], signal.SIGTERM)
            stopped.append(item["pid"])
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise ThreadError(f"Thread is not allowed to stop PID {item['pid']}.") from exc
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        remaining = [item for item in tree if Path(f"/proc/{item['pid']}").exists()]
        if not remaining:
            break
        time.sleep(0.05)
    for item in tree:
        if Path(f"/proc/{item['pid']}").exists():
            try:
                os.kill(item["pid"], signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return stopped


def _owned_process_tree(pid: int) -> list[dict[str, int]]:
    """Collect every current-user descendant, not only Python child processes."""
    uid = os.getuid()
    by_parent: dict[int, list[dict[str, int]]] = {}
    selected: dict[int, dict[str, int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
            owner = next((int(line.split()[1]) for line in status.splitlines() if line.startswith("Uid:")), -1)
            if owner != uid:
                continue
            item = {"pid": int(entry.name), "ppid": _parent_pid(status)}
            selected[item["pid"]] = item
            by_parent.setdefault(item["ppid"], []).append(item)
        except (OSError, ValueError, StopIteration):
            continue
    result: list[dict[str, int]] = []

    def visit(current: int) -> None:
        item = selected.get(current)
        if item:
            result.append(item)
        for child in by_parent.get(current, []):
            visit(child["pid"])

    visit(pid)
    return result


def _parent_pid(status: str) -> int:
    parent = next((line for line in status.splitlines() if line.startswith("PPid:")), "PPid:\t0")
    return int(parent.split()[1])


def _start_time(path: Path) -> str:
    # Field 22 in proc(5) remains stable across Linux versions; retain it as a
    # string because it is only an identity token for PID-reuse protection.
    fields = path.read_text(encoding="utf-8", errors="replace").split()
    return fields[21] if len(fields) > 21 else ""


def _runtime_environment(path: Path) -> dict[str, str]:
    try:
        entries = path.read_bytes().split(b"\0")
    except OSError:
        return {}
    allowed = {"PATH", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX"}
    result: dict[str, str] = {}
    for entry in entries:
        key, separator, value = entry.partition(b"=")
        if separator and key.decode(errors="replace") in allowed:
            result[key.decode(errors="replace")] = value.decode(errors="replace")
    return result


def _venv_from_executable(executable: str) -> str | None:
    path = Path(executable)
    return str(path.parent.parent) if path.parent.name in {"bin", "Scripts"} and path.parent.parent.name in {".venv", "venv"} else None


def _redact_command(command: list[str]) -> list[str]:
    sensitive = ("password", "passwd", "token", "secret", "api_key", "apikey", "credential", "cookie")
    result: list[str] = []
    redact_next = False
    for item in command:
        if redact_next:
            result.append("<redacted>")
            redact_next = False
            continue
        option, separator, value = item.partition("=")
        normalised = option.lstrip("-").lower().replace("-", "_")
        if any(marker in normalised for marker in sensitive):
            result.append(f"{option}=<redacted>" if separator else item)
            redact_next = not bool(separator)
        else:
            result.append(item)
    return result


def _infer_root(cwd: Path, command: list[str]) -> Path | None:
    candidates = [cwd]
    for argument in command[1:]:
        if argument.endswith(".py"):
            source = Path(argument)
            candidates.append((cwd / source).resolve() if not source.is_absolute() else source.resolve())
    for candidate in candidates:
        directory = candidate if candidate.is_dir() else candidate.parent
        for parent in (directory, *directory.parents):
            if any((parent / marker).exists() for marker in ("pyproject.toml", "setup.py", "requirements.txt", ".git")):
                return parent
    return cwd if any(cwd.glob("*.py")) else None
