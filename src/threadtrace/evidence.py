"""Versioned evidence records shared by static, runtime, and hybrid output."""

from __future__ import annotations

from typing import Any

EVIDENCE_VERSION = 1
EVIDENCE_CLASSES = ("statically resolved", "heuristically predicted", "observed at runtime", "unknown")


def _runtime_occurred(raw: Any) -> bool:
    """A runtime class is observed only when the event actually happened."""
    if not isinstance(raw, dict) or raw.get("kind") != "runtime":
        return False
    details = raw.get("details")
    if not isinstance(details, dict):
        return False
    status = details.get("status")
    if isinstance(status, str):
        return status in {"completed", "failed", "raised", "returned", "read", "written", "succeeded"}
    return details.get("kind") in {
        "line", "call", "return", "exception", "process", "thread", "task", "wait",
        "resource", "import", "audit", "tracepoint", "watchpoint", "yield", "resume",
    }


def classify(legacy_confidence: str | None = None, raw: Any = None) -> str:
    """Map the alpha confidence vocabulary to the v1 evidence classes."""
    if _runtime_occurred(raw):
        return "observed at runtime"
    if legacy_confidence == "confirmed":
        return "statically resolved"
    if legacy_confidence in {"high", "possible"}:
        return "heuristically predicted"
    return "unknown"


def record(evidence_class: str, mechanism: str, *, source: Any = None, run: Any = None,
           limitations: tuple[str, ...] | list[str] = (), legacy_confidence: str | None = None) -> dict[str, Any]:
    """Create one stable record without hiding the original confidence label."""
    if evidence_class not in EVIDENCE_CLASSES:
        raise ValueError(f"Unknown evidence class: {evidence_class}")
    return {
        "version": EVIDENCE_VERSION,
        "class": evidence_class,
        "mechanism": mechanism,
        "source": source,
        "run": run,
        "limitations": list(limitations),
        "legacy_confidence": legacy_confidence,
    }


def attach(row: dict[str, Any], mechanism: str, *, source: Any = None, run: Any = None,
           limitations: tuple[str, ...] | list[str] = (), evidence_class: str | None = None) -> dict[str, Any]:
    """Attach a record to one public result row, preserving legacy confidence."""
    classification = evidence_class or classify(row.get("confidence"), row.get("evidence"))
    row["evidence_class"] = classification
    row["evidence_record"] = record(
        classification, mechanism, source=source, run=run, limitations=limitations,
        legacy_confidence=row.get("confidence"),
    )
    return row
