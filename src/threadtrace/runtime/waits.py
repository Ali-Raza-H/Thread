"""Non-blocking wait observations for standard concurrency primitives."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import queue
import sys
import threading
import time
from typing import Any, Callable

_write: Callable[[dict[str, Any]], None] | None = None
_root = ""
_run_id = ""
_process_id = ""
_installed = False
_wait_sequence = 0
_started = 0.0


def _new_wait() -> str:
    global _wait_sequence
    _wait_sequence += 1
    return f"{_process_id}:{threading.get_ident()}:{_wait_sequence}"


def _in_project() -> bool:
    """Accept only calls whose first external frame is project code."""
    try:
        frame = sys._getframe(1)
    except ValueError:
        return False
    own = os.path.realpath(__file__)
    while frame is not None:
        filename = os.path.realpath(frame.f_code.co_filename) if frame.f_code.co_filename else ""
        if filename and filename != own:
            return filename.startswith(_root + os.sep)
        frame = frame.f_back
    return False


def _emit(resource_kind: str, operation: str, resource: Any, status: str, started: float | None = None,
          details: dict[str, Any] | None = None, wait_id: str | None = None) -> None:
    if _write is None or not _in_project():
        return
    now = time.monotonic()
    item = {
        "wait_id": wait_id or _new_wait(),
        "protocol_version": 1, "event_schema_version": 2, "kind": "wait",
        "run_id": _run_id, "process_id": _process_id, "thread_id": threading.get_ident(),
        "run_thread_id": f"{_process_id}:{threading.get_ident()}",
        "resource_kind": resource_kind, "operation": operation, "status": status,
        "resource_id": f"{_process_id}:{id(resource)}", "wall_time": time.time(),
        "elapsed_seconds": now - _started, "blocked": status == "started",
        "wait_seconds": max(0.0, now - started) if started is not None else None,
    }
    if details:
        item["details"] = details
    _write(item)


def _wrap_sync(owner: Any, name: str, resource_kind: str, resource_getter: Callable[[Any], Any],
               operation: str, timeout_arg: int | None = None) -> None:
    original = getattr(owner, name, None)
    if original is None or getattr(original, "_threadtrace_wrapped", False):
        return

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        resource = resource_getter(args[0] if args else owner)
        started = time.monotonic()
        wait_id = _new_wait()
        _emit(resource_kind, operation, resource, "started", started, wait_id=wait_id)
        try:
            result = original(*args, **kwargs)
        except BaseException as exc:
            status = "timeout" if type(exc).__name__ in {"Empty", "TimeoutError", "TimeoutExpired"} else "failed"
            _emit(resource_kind, operation, resource, status, started, wait_id=wait_id)
            raise
        timeout = kwargs.get("timeout", args[timeout_arg] if timeout_arg is not None and len(args) > timeout_arg else None)
        blocked = timeout is not None and (result is False or (
            isinstance(resource, threading.Thread) and resource.is_alive()))
        status = "timeout" if blocked else "completed"
        _emit(resource_kind, operation, resource, status, started, wait_id=wait_id)
        return result

    wrapped._threadtrace_wrapped = True
    setattr(owner, name, wrapped)


def _wrap_async(owner: Any, name: str, resource_kind: str, resource_getter: Callable[[Any], Any],
                operation: str) -> None:
    original = getattr(owner, name, None)
    if original is None or getattr(original, "_threadtrace_wrapped", False):
        return

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        resource = resource_getter(args[0] if args else owner)
        started = time.monotonic()
        wait_id = _new_wait()
        _emit(resource_kind, operation, resource, "started", started, wait_id=wait_id)
        try:
            result = await original(*args, **kwargs)
        except asyncio.CancelledError:
            _emit(resource_kind, operation, resource, "cancelled", started, wait_id=wait_id)
            raise
        except BaseException:
            _emit(resource_kind, operation, resource, "failed", started, wait_id=wait_id)
            raise
        _emit(resource_kind, operation, resource, "completed", started, wait_id=wait_id)
        return result

    wrapped._threadtrace_wrapped = True
    setattr(owner, name, wrapped)


def _self(resource: Any) -> Any:
    return resource


def _event_loop_waits() -> None:
    _wrap_async(asyncio.Lock, "acquire", "asyncio-lock", _self, "acquire")
    _wrap_sync(asyncio.Lock, "release", "asyncio-lock", _self, "release")
    _wrap_async(asyncio.Semaphore, "acquire", "asyncio-semaphore", _self, "acquire")
    _wrap_sync(asyncio.Semaphore, "release", "asyncio-semaphore", _self, "release")
    _wrap_async(asyncio.Condition, "wait", "asyncio-condition", _self, "wait")
    if hasattr(asyncio.Condition, "acquire"):
        _wrap_async(asyncio.Condition, "acquire", "asyncio-condition", _self, "acquire")
        _wrap_sync(asyncio.Condition, "release", "asyncio-condition", _self, "release")
    _wrap_async(asyncio.Event, "wait", "asyncio-event", _self, "wait")
    _wrap_sync(asyncio.Event, "set", "asyncio-event", _self, "set")
    _wrap_async(asyncio.Queue, "get", "asyncio-queue", _self, "get")
    _wrap_async(asyncio.Queue, "put", "asyncio-queue", _self, "put")
    _wrap_async(asyncio, "sleep", "asyncio-sleep", lambda _: None, "sleep")
    _wrap_async(asyncio, "wait_for", "asyncio-wait", lambda _: None, "wait_for")
    _wrap_async(asyncio, "wait", "asyncio-wait", lambda _: None, "wait")


def install(write: Callable[[dict[str, Any]], None], root: str) -> None:
    """Install adapters once; they emit only while project code is on the stack."""
    global _write, _root, _run_id, _process_id, _installed, _started
    if _installed:
        return
    _installed = True
    _write, _root = write, os.path.realpath(root or "")
    _run_id = os.environ.get("THREADTRACE_RUNTIME_RUN_ID", "")
    _process_id = os.environ.get("THREADTRACE_RUNTIME_PROCESS_ID", f"{_run_id}:{os.getpid()}")
    _started = time.monotonic()
    _wrap_sync(threading.Thread, "join", "thread", _self, "join", timeout_arg=1)
    _wrap_sync(threading.Event, "wait", "event", _self, "wait", timeout_arg=1)
    _wrap_sync(threading.Event, "set", "event", _self, "set")
    _wrap_sync(threading.Condition, "wait", "condition", _self, "wait", timeout_arg=1)
    _wrap_sync(threading.Semaphore, "acquire", "semaphore", _self, "acquire", timeout_arg=2)
    _wrap_sync(threading.Semaphore, "release", "semaphore", _self, "release")
    _wrap_sync(queue.Queue, "get", "queue", _self, "get", timeout_arg=2)
    _wrap_sync(queue.Queue, "put", "queue", _self, "put", timeout_arg=3)
    _wrap_sync(concurrent.futures.Future, "result", "future", _self, "result", timeout_arg=1)
    _event_loop_waits()
