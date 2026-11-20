"""Correlate persisted process, thread, task, and wait observations."""

from __future__ import annotations

from typing import Any

MAX_TIMELINE = 20_000


def _elapsed(event: dict[str, Any]) -> float:
    value = event.get("elapsed_seconds")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _process_rows(events: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") != "process":
            continue
        pid = event.get("pid")
        process_id = event.get("process_id") or f"{run_id}:{pid}"
        item = rows.setdefault(process_id, {
            "process_id": process_id, "run_id": run_id, "pid": pid,
            "ppid": event.get("ppid"), "parent_process_id": event.get("parent_process_id"),
            "executable": event.get("executable"), "argv": event.get("argv") or [],
            "instrumentation": event.get("instrumentation"), "status": "observed",
            "lifecycle": [], "first_seen_seconds": _elapsed(event), "last_seen_seconds": _elapsed(event),
            "evidence_class": "observed at runtime",
        })
        lifecycle = event.get("lifecycle")
        if lifecycle:
            item["lifecycle"].append({"lifecycle": lifecycle, "elapsed_seconds": _elapsed(event)})
        item["last_seen_seconds"] = max(item["last_seen_seconds"], _elapsed(event))
    return sorted(rows.values(), key=lambda item: (item["first_seen_seconds"], str(item["process_id"])))


def _lane_rows(events: list[dict[str, Any]], kind: str, identity: str, name: str) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") != kind:
            continue
        key = str(event.get(identity) or f"{event.get('process_id')}:{event.get('thread_id')}")
        item = rows.setdefault(key, {
            identity: key, "process_id": event.get("process_id"), "name": event.get(name) or key,
            "lifecycle": [], "first_seen_seconds": _elapsed(event), "last_seen_seconds": _elapsed(event),
            "evidence_class": "observed at runtime",
        })
        if event.get("lifecycle"):
            item["lifecycle"].append({"lifecycle": event["lifecycle"], "elapsed_seconds": _elapsed(event)})
        item["last_seen_seconds"] = max(item["last_seen_seconds"], _elapsed(event))
    return sorted(rows.values(), key=lambda item: (item["first_seen_seconds"], str(item[identity])))


def _wait_rows(events: list[dict[str, Any]], last_elapsed: float) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") != "wait":
            continue
        wait_id = str(event.get("wait_id") or event.get("ordinal"))
        item = rows.setdefault(wait_id, {
            "wait_id": wait_id, "resource_kind": event.get("resource_kind"),
            "operation": event.get("operation"), "resource_id": event.get("resource_id"),
            "process_id": event.get("process_id"), "thread_id": event.get("thread_id"),
            "run_thread_id": event.get("run_thread_id"), "status": "started",
            "started_seconds": _elapsed(event), "duration_seconds": 0.0,
            "evidence_class": "observed at runtime",
        })
        if event.get("status") == "started":
            item["started_seconds"] = _elapsed(event)
        else:
            item["status"] = event.get("status")
            item["duration_seconds"] = event.get("wait_seconds")
            if not isinstance(item["duration_seconds"], (int, float)):
                item["duration_seconds"] = max(0.0, _elapsed(event) - item["started_seconds"])
    for item in rows.values():
        if item["status"] == "started":
            item["duration_seconds"] = max(0.0, last_elapsed - item["started_seconds"])
            item["status"] = "unresolved"
    return sorted(rows.values(), key=lambda item: (item["started_seconds"], item["wait_id"]))


def _findings(waits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for wait in waits:
        if wait["status"] != "unresolved" and wait["duration_seconds"] < 1.0:
            continue
        findings.append({
            "kind": "long_wait", "confidence": "observed duration",
            "resource_kind": wait["resource_kind"], "resource_id": wait["resource_id"],
            "wait_id": wait["wait_id"], "thread": wait["run_thread_id"],
            "duration_seconds": wait["duration_seconds"], "status": wait["status"],
            "message": f"{wait['resource_kind']} {wait['operation']} waited at least {wait['duration_seconds']:.3f}s.",
        })
    active = [wait for wait in waits if wait["status"] == "unresolved"]
    by_process: dict[str, list[dict[str, Any]]] = {}
    for wait in active:
        by_process.setdefault(str(wait["process_id"]), []).append(wait)
    for process_id, items in by_process.items():
        threads = {item["run_thread_id"] for item in items}
        resources = {item["resource_id"] for item in items}
        if len(threads) >= 2 and len(resources) >= 2:
            findings.append({
                "kind": "possible_deadlock", "confidence": "heuristic overlap, not proof",
                "process_id": process_id, "threads": sorted(threads), "resources": sorted(resources),
                "message": "Multiple threads remained blocked on different resources at the end of the observed trace; this may be a deadlock, but ownership and wakeups are not proven.",
            })
    return findings


def build_timeline(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Build bounded lanes and heuristic findings from observed events."""
    events = [
        {**event, **event.get("evidence", {}).get("details", {}),
         "kind": event.get("evidence", {}).get("details", {}).get("kind") or event.get("event_kind")}
        for event in events
    ]
    processes_events = [event for event in events if event.get("kind") == "process"]
    run_id = next((str(event.get("run_id")) for event in processes_events if event.get("run_id")), "")
    if not run_id:
        run_id = next((str(event.get("run_id")) for event in events if event.get("run_id")), "")
    processes = _process_rows(processes_events, run_id)
    threads = _lane_rows(events, "thread", "run_thread_id", "name")
    tasks = _lane_rows(events, "task", "task_id", "task_name")
    last_elapsed = max((_elapsed(event) for event in events), default=0.0)
    waits = _wait_rows(events, last_elapsed)
    ordered = sorted(events, key=lambda event: (_elapsed(event), event.get("ordinal", 0)))
    timeline = [{
        "ordinal": event.get("ordinal"), "elapsed_seconds": _elapsed(event), "kind": event.get("kind"),
        "lane": event.get("task_name") or event.get("run_thread_id") or event.get("process_id"),
        "process_id": event.get("process_id"), "thread_id": event.get("thread_id"),
        "task_id": event.get("task_id"), "path": event.get("relative_path") or event.get("path"),
        "line": event.get("line"), "resource_kind": event.get("resource_kind"),
        "operation": event.get("operation"), "status": event.get("status"),
    } for event in ordered[:MAX_TIMELINE]]
    return {
        "run_id": run_id, "processes": processes, "threads": threads, "tasks": tasks,
        "waits": waits, "timeline": timeline, "timeline_truncated": len(ordered) > MAX_TIMELINE,
        "findings": _findings(waits),
        "limitations": [
            "Only instrumented Python processes and observable Python wait points are shown.",
            "Native locks, uninstrumented Python children, non-Python children, and OS scheduler state are not observed.",
            "Task creation/completion and coroutine suspend/resume require a currently observed asyncio task.",
            "Deadlock findings describe overlapping blocked waits; they are not ownership or scheduler proof.",
            "Timeline navigation is history only and never restores real process state.",
        ],
    }
