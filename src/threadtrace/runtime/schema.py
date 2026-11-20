"""Versioned runtime event schema and instrumentation capability selection."""

from __future__ import annotations

import sys
from typing import Any

from ..formats import TRACE_PROTOCOL_VERSION

EVENT_SCHEMA_VERSION = 2
EVENT_KINDS = {
    "call", "line", "return", "yield", "resume", "exception",
    "import", "c_call", "c_return", "c_exception", "audit",
    "process", "thread", "task", "resource", "profiling", "tracepoint", "watchpoint", "wait",
}
SOURCE_EVENT_KINDS = {"call", "line", "return", "yield", "resume", "exception", "c_call", "c_return", "c_exception"}
EVENT_CATEGORIES = {
    "call": "execution", "line": "execution", "return": "execution", "yield": "lifecycle",
    "resume": "lifecycle", "exception": "exception", "import": "import",
    "c_call": "native-call", "c_return": "native-call", "c_exception": "native-call",
    "audit": "audit", "process": "process", "thread": "process", "task": "process",
    "resource": "resource", "profiling": "profiling", "tracepoint": "debugger",
    "watchpoint": "debugger", "wait": "concurrency",
}
MECHANISM_CAPABILITIES = {
    "settrace": {
        "mechanisms": ["sys.settrace"],
        "events": ["call", "line", "return", "yield", "resume", "exception", "thread"],
        "status": "Full",
        "limitations": ["Python frame hooks cannot observe C bodies or already-running attached processes."],
    },
    "monitoring": {
        "mechanisms": ["sys.monitoring"],
        "events": ["call", "line", "return", "exception"],
        "status": "Partial",
        "limitations": ["Requires CPython 3.12+ and does not expose frame locals in every callback; ThreadTrace falls back to sys.settrace when unsupported."],
    },
    "profile": {
        "mechanisms": ["sys.setprofile"],
        "events": ["call", "return", "c_call", "c_return", "c_exception"],
        "status": "Partial",
        "limitations": ["No line events or local-variable snapshots."],
    },
    "audit": {
        "mechanisms": ["sys.addaudithook"],
        "events": ["audit", "import", "process", "resource"],
        "status": "Partial",
        "limitations": ["Audit hooks provide policy events, not Python line execution or return values."],
    },
    "adapters": {
        "mechanisms": ["runtime adapters"],
        "events": ["resource"],
        "status": "Partial",
        "limitations": ["Only supported libraries and explicitly wrapped operations are observed."],
    },
}


def _version_tuple(version: str | None) -> tuple[int, int]:
    parts = (version or "").split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return sys.version_info.major, sys.version_info.minor


def instrumentation_plan(requested: str = "auto", version: str | None = None) -> dict[str, Any]:
    """Describe mechanisms, event coverage, and any honest fallback."""
    if requested not in {"auto", "settrace", "monitoring", "profile", "audit", "all"}:
        raise ValueError("Instrumentation must be auto, settrace, monitoring, profile, audit, or all.")
    major, minor = _version_tuple(version)
    monitoring_available = (major, minor) >= (3, 12)
    if requested in {"auto", "settrace"}:
        selected = ["settrace"]
    elif requested == "monitoring":
        selected = ["settrace"]
    elif requested == "profile":
        selected = ["profile"]
    elif requested == "audit":
        selected = ["audit", "settrace"]
    else:
        selected = ["settrace", "profile", "audit"]
    reports = {name: dict(value) for name, value in MECHANISM_CAPABILITIES.items()}
    if not monitoring_available:
        reports["monitoring"]["status"] = "Unsupported"
    reports["coverage.py"] = {
        "mechanisms": ["coverage.py import (optional)"], "events": ["line", "branch"],
        "status": "Partial", "limitations": ["Only stored coverage.py line/branch data can be imported; it is not executed by ThreadTrace."],
    }
    return {
        "requested": requested, "selected": selected, "interpreter_version": version,
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "fallback": requested == "monitoring",
        "fallback_reason": ("This build reports sys.monitoring availability but uses the frame-local sys.settrace path for variables and debugger controls."
                            if requested == "monitoring" else None),
        "mechanisms": reports,
    }


def normalise_event(event: dict[str, Any], ordinal: int | None = None) -> dict[str, Any]:
    """Fill schema defaults and preserve unknown event kinds explicitly."""
    item = dict(event)
    item.setdefault("protocol_version", TRACE_PROTOCOL_VERSION)
    item["event_schema_version"] = EVENT_SCHEMA_VERSION
    kind = str(item.get("kind") or "unknown")
    item["kind"] = kind
    item["category"] = EVENT_CATEGORIES.get(kind, "unknown")
    if ordinal is not None:
        item["ordinal"] = ordinal
    item.setdefault("ordinal", 0)
    if kind in SOURCE_EVENT_KINDS and not item.get("relative_path"):
        item["relative_path"] = item.get("relative_path") or item.get("path")
    if kind not in {"call", "line", "return", "yield", "resume", "exception", "c_call", "c_return", "c_exception"}:
        item.pop("variables", None)
    return item


def event_errors(event: dict[str, Any]) -> list[str]:
    """Return schema violations without raising on partially observed data."""
    errors: list[str] = []
    kind = event.get("kind")
    if kind not in EVENT_KINDS:
        errors.append("unknown event kind")
    if event.get("event_schema_version") != EVENT_SCHEMA_VERSION:
        errors.append("event schema version")
    if kind in SOURCE_EVENT_KINDS:
        if not isinstance(event.get("path"), str) and not isinstance(event.get("relative_path"), str):
            errors.append("source event path")
        if not isinstance(event.get("ordinal"), int):
            errors.append("source event ordinal")
    if kind in {"call", "line", "return", "yield", "resume", "exception"}:
        if not isinstance(event.get("thread_id"), int):
            errors.append("execution event thread")
        if not isinstance(event.get("elapsed_seconds"), (int, float)):
            errors.append("execution event elapsed time")
    return errors
