"""One versioned export envelope shared by reports, investigations, and experiments."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .capabilities import capability_report
from .formats import EXPORT_FORMAT, EXPORT_SCHEMA_VERSION, TRACE_PROTOCOL_VERSION

_TRUNCATION_KEYS = frozenset({"truncated", "truncation", "truncated_records", "omitted", "dropped"})
_MAX_LISTED_TRUNCATION = 32
_HEADER_KEYS = ("format", "export_version", "application_version", "database_schema_version",
                "trace_protocol_version", "payload_type", "generated_at", "project", "scan_id", "source")


def digest(value: Any) -> str:
    """Stable content hash used for source revisions."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def source_state(store: Any, scan_id: str | None) -> dict[str, Any]:
    """Describe the exact stored source revision without reading target files."""
    if not scan_id:
        return {"scan_id": None, "files": {}, "inputs": {}, "digest": None}
    files = {row["relative_path"]: row["content_hash"] for row in store.rows(
        "SELECT relative_path,content_hash FROM files WHERE scan_id=? ORDER BY relative_path", (scan_id,))}
    inputs = {row["input_key"]: row["input_hash"] for row in store.rows(
        "SELECT input_key,input_hash FROM scan_inputs WHERE scan_id=?", (scan_id,))}
    return {"scan_id": scan_id, "files": files, "inputs": inputs, "digest": digest({"files": files, "inputs": inputs})}


def truncations(payload: Any, path: str = "payload") -> list[str]:
    """List stored truncation markers so an export never claims completeness."""
    found: list[str] = []

    def walk(value: Any, location: str) -> None:
        if len(found) >= _MAX_LISTED_TRUNCATION:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in _TRUNCATION_KEYS and item not in (None, False, 0, "", [], {}):
                    found.append(f"{location}.{key}={item}")
                else:
                    walk(item, f"{location}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{location}[{index}]")

    walk(payload, path)
    return found


def envelope(payload_type: str, *, project: dict[str, Any], schema_version: int,
             scan: dict[str, Any] | None = None, source: dict[str, Any] | None = None,
             run_ids: Iterable[str] = (), truncation: Iterable[str] = (),
             limitations: Iterable[str] = ()) -> dict[str, Any]:
    """Build the versioned header that every portable artifact shares."""
    return {
        "format": EXPORT_FORMAT,
        "export_version": EXPORT_SCHEMA_VERSION,
        "application_version": __version__,
        "database_schema_version": schema_version,
        "trace_protocol_version": TRACE_PROTOCOL_VERSION,
        "payload_type": payload_type,
        "generated_at": datetime.now(UTC).isoformat(),
        "project": {"id": project["project_id"], "name": project["name"], "root": project["root_path"]},
        "scan_id": scan["scan_id"] if scan else None,
        "scan_completed_at": scan.get("completed_at") if scan else None,
        "run_ids": list(run_ids),
        "source": source or {"scan_id": None, "files": {}, "inputs": {}, "digest": None},
        "capabilities": [{"feature": item["feature"], "status": item["status"]} for item in capability_report()],
        "limitations": list(limitations),
        "truncation": list(truncation),
    }


def report_bundle(engine: Any, queries: Any, project_ref: str) -> dict[str, Any]:
    """Build a self-describing snapshot of the latest completed scan."""
    project = engine.get_project(project_ref)
    scan = engine.latest_scan(project_ref)
    payload = {
        "overview": queries.overview(project_ref),
        "architecture": queries.architecture(project_ref),
        "findings": queries.findings(project_ref),
        "staleness": queries.staleness(project_ref),
        "capabilities": capability_report(project=project["name"], scan_id=scan["scan_id"]),
    }
    bundle = envelope(
        "report", project=project, scan=scan, schema_version=engine.store.schema_version,
        source=source_state(engine.store, scan["scan_id"]), truncation=truncations(payload),
        limitations=("An export describes stored static evidence for one scan; it does not prove runtime behaviour.",),
    )
    bundle["payload"] = payload
    return bundle


def write_text(output: str, text: str) -> Path:
    """Write a portable artifact with restrictive permissions."""
    path = Path(output).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def header_lines(bundle: dict[str, Any], prefix: str) -> list[str]:
    """Keep the envelope visible in formats that have no metadata slot."""
    return [f"{prefix} {key}: {json.dumps(bundle.get(key), default=str, sort_keys=True)}" for key in _HEADER_KEYS]
