"""Best-effort static summary of likely runtime side effects.

This reads stored static resources only. It never imports or executes target
code and is deliberately not presented as a security guarantee.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


_CATEGORIES = {
    "database": "database",
    "service": "network",
    "queue": "network",
    "subprocess": "process",
    "environment": "environment",
    "file": "filesystem",
    "configuration": "filesystem",
    "structured-data": "filesystem",
    "text": "filesystem",
    "template": "filesystem",
    "media": "filesystem",
    "compression": "filesystem",
    "package": "filesystem",
    "diagnostics": "diagnostics",
}


def summarize(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Group stored resource syntax into a bounded pre-run estimate."""
    counts: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = {}
    for resource in resources:
        kind = str(resource.get("kind") or "unknown")
        category = _CATEGORIES.get(kind, "other")
        counts[category] += 1
        examples.setdefault(category, [])
        if len(examples[category]) < 10:
            examples[category].append({
                "kind": kind,
                "name": resource.get("name"),
                "operation": resource.get("operation"),
                "module": resource.get("module_name"),
                "confidence": resource.get("confidence"),
            })
    return {
        "status": "static-estimate",
        "guarantee": False,
        "counts": dict(sorted(counts.items())),
        "examples": examples,
        "limitations": [
            "This is a source-level estimate, not a sandbox or security guarantee.",
            "Dynamic paths and indirect calls can add effects that no static resource row records.",
            "The estimate does not decide whether a path is actually executed.",
        ],
    }


def summary_line(summary: dict[str, Any]) -> str:
    """Return a short terminal warning for a preflight summary."""
    counts = summary.get("counts") or {}
    if not counts:
        return "Pre-run static side-effect estimate: no obvious resource operations found (not a security guarantee)."
    parts = ", ".join(f"{name}: {count}" for name, count in counts.items())
    return f"Pre-run static side-effect estimate: {parts} (not a security guarantee)."
