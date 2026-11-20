"""Build bounded, evidence-based reports for failed runtime sessions."""

from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any


_SENSITIVE_KEYS = (
    "authorization", "proxy-authorization", "password", "passwd", "secret", "token",
    "api_key", "apikey", "access_key", "client_secret", "session_key", "credential",
    "cookie", "set-cookie", "private_key",
)
_URL = re.compile(r"https?://[^\s\"'<>]+")


def _scrub_url(text: str) -> str:
    """Redact userinfo and every query value of one URL."""
    try:
        parsed = urllib.parse.urlsplit(text)
        netloc = parsed.netloc
        if "@" in netloc:
            netloc = "<redacted>@" + netloc.rsplit("@", 1)[1]
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        return urllib.parse.urlunsplit((
            parsed.scheme, netloc, parsed.path,
            urllib.parse.urlencode([(key, "<redacted>") for key, _item in query]),
            "<redacted>" if parsed.fragment else "",
        ))
    except ValueError:
        text = re.sub(r"([?&][^=&#?]+)=([^&#]*)", r"\1=<redacted>", text)
        return re.sub(r"(://)[^/@\s]+@", r"\1<redacted>@", text)


def redact_text(value: str, enabled: bool = True, environment: dict[str, str] | None = None) -> str:
    """Redact known credentials from persisted process output and errors."""
    if not enabled:
        return value
    keys = "|".join(key.replace("_", "[_-]?") for key in _SENSITIVE_KEYS)
    value = re.sub(
        rf"(?i)([\"']?(?:{keys})[\"']?\s*[:=]\s*[\"']?)([^\"',&}}\s]+)",
        lambda match: match.group(1) + "<redacted>", value,
    )
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+", r"\1<redacted>", value)
    value = _URL.sub(lambda match: _scrub_url(match.group(0)), value)
    variables = {**os.environ, **(environment or {})}
    for key, secret in sorted(variables.items(), key=lambda item: len(item[1]), reverse=True):
        lowered = key.lower().replace("-", "_")
        if len(secret) >= 4 and any(name.replace("-", "_") in lowered for name in _SENSITIVE_KEYS):
            value = value.replace(secret, "<redacted>")
    return value


def failure_report(root: Path, result: dict[str, Any]) -> dict[str, Any] | None:
    """Summarise a failed or timed-out run without evaluating target values."""
    if not result.get("timed_out") and result.get("exit_code") in (0, None):
        return None
    events = [event for event in result.get("events", []) if isinstance(event, dict)]
    stderr = redact_text(str(result.get("stderr", "")), bool(result.get("redaction_enabled", True)))
    traceback_excerpt = _traceback_excerpt(stderr)
    traceback_chain = _traceback_chain(root, traceback_excerpt)
    stderr_exception, stderr_message = _stderr_exception(traceback_excerpt)
    exception = _correlated_exception(root, events, stderr_exception, traceback_chain)
    has_observed_exceptions = any(event.get("kind") == "exception" for event in events)
    correlation = (
        "traceback_and_trace" if exception else
        "unknown" if stderr_exception and has_observed_exceptions else
        "traceback_only" if stderr_exception else
        "unknown"
    )
    if stderr_exception:
        exception_name, exception_message = stderr_exception, stderr_message
        location = traceback_chain[-1] if traceback_chain else None
    elif not result.get("timed_out"):
        exception = _terminal_control_flow_exception(events)
        exception_name = exception.get("exception") if exception else "unknown"
        exception_message = _event_message(exception)
        if exception and exception_name == "SystemExit" and not exception_message:
            exception_message = stderr.strip()[:1_000] or None
        location = _location(root, exception)
        correlation = "trace_only" if exception else "unknown"
    else:
        exception_name, exception_message, location = "unknown", None, None
    call_chain = _call_chain(events, exception) if exception else []
    if not call_chain:
        call_chain = traceback_chain
    captured_state = _captured_state(events, exception)
    observed, observed_truncated = _observed_exceptions(root, events, exception)
    side_effects = [event for event in events if event.get("kind") == "resource"]
    terminating_exception = {
        "type": exception_name,
        "message": exception_message,
        "location": location,
        "correlation": correlation,
    }
    return {
        "reason": "timeout" if result.get("timed_out") else "nonzero_exit",
        "exit_code": result.get("exit_code"),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "exception": exception_name,
        "exception_message": exception_message,
        "terminating_exception": terminating_exception,
        "observed_exceptions": observed,
        "location": location,
        "source_hash": _source_hash(root, location),
        "call_chain": call_chain,
        "captured_state": captured_state,
        "traceback_excerpt": traceback_excerpt,
        "side_effects": side_effects[-100:],
        "limitations": [
            "Captured state contains safe summaries only.",
            *(["Only the first and last 50 observed exception events are included."] if observed_truncated else []),
            *(["Only the latest 100 observed resource events are included."] if len(side_effects) > 100 else []),
        ],
    }


def _correlated_exception(
    root: Path, events: list[dict[str, Any]], exception_name: str | None,
    traceback_chain: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Match the final traceback's innermost project frame to a trace event."""
    if not exception_name or not traceback_chain:
        return None
    location = traceback_chain[-1]
    matches = [
        event for event in events
        if event.get("kind") == "exception"
        and event.get("exception") == exception_name
        and _same_location(_location(root, event), location)
    ]
    return matches[-1] if matches else None


def _same_location(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    return bool(left and right and left.get("path") == right.get("path") and left.get("line") == right.get("line"))


def _terminal_control_flow_exception(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Recognise traceback-suppressing control-flow exceptions at termination."""
    last_line = max((index for index, event in enumerate(events) if event.get("kind") == "line"), default=-1)
    for event in events[last_line + 1:]:
        if event.get("kind") == "exception" and event.get("exception") in {"SystemExit", "KeyboardInterrupt"}:
            return event
    return None


def _event_message(event: dict[str, Any] | None) -> str | None:
    if not event:
        return None
    message = event.get("exception_message")
    if isinstance(message, str):
        return message[:1_000]
    if isinstance(message, dict):
        value = message.get("preview", message.get("value"))
        if value not in (None, "<object>"):
            return str(value)[:1_000]
    return None


def _captured_state(events: list[dict[str, Any]], target: dict[str, Any] | None) -> dict[str, Any]:
    if target is None:
        return {}
    state = target.get("variables")
    if state:
        return state
    frame_id = target.get("frame_id")
    if frame_id is None:
        return {}
    target_index = next(index for index, event in enumerate(events) if event is target)
    for event in reversed(events[:target_index]):
        if event.get("frame_id") == frame_id:
            return event.get("variables") or event.get("inputs") or {}
    return {}


def _observed_exceptions(
    root: Path, events: list[dict[str, Any]], terminating: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], bool]:
    observed = []
    for ordinal, event in enumerate(events, 1):
        if event.get("kind") != "exception":
            continue
        observed.append({
            "ordinal": event.get("ordinal", ordinal),
            "type": event.get("exception", "unknown"),
            "message": _event_message(event),
            "location": _location(root, event),
            "thread_id": event.get("thread_id"),
            "task_id": event.get("task_id"),
            "terminating": event is terminating,
        })
    if len(observed) <= 100:
        return observed, False
    return observed[:50] + observed[-50:], True


def _location(root: Path, event: dict[str, Any] | None) -> dict[str, Any] | None:
    if not event:
        return None
    relative = event.get("relative_path")
    if not isinstance(relative, str):
        path = event.get("path")
        if isinstance(path, str):
            try:
                resolved = Path(path).resolve()
                if resolved.is_relative_to(root):
                    relative = resolved.relative_to(root).as_posix()
            except OSError:
                pass
    if not isinstance(relative, str):
        return None
    return {"path": relative, "line": event.get("line"), "function": event.get("name")}


def _source_hash(root: Path, location: dict[str, Any] | None) -> str | None:
    if not location or not isinstance(location.get("path"), str):
        return None
    try:
        path = (root / location["path"]).resolve()
        if not path.is_relative_to(root):
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _call_chain(events: list[dict[str, Any]], target: dict[str, Any] | None) -> list[dict[str, Any]]:
    if target is None:
        return []
    thread_id = target.get("thread_id")
    stacks: dict[Any, list[dict[str, Any]]] = {}
    for event in events:
        current_thread = event.get("thread_id")
        stack = stacks.setdefault(current_thread, [])
        if event.get("kind") == "call":
            stack.append(_frame(event))
        if event is target:
            return list(stack)
        if event.get("kind") == "return" and stack:
            stack.pop()
    return list(stacks.get(thread_id, []))


def _frame(event: dict[str, Any]) -> dict[str, Any]:
    return {"function": event.get("name"), "path": event.get("relative_path"), "line": event.get("line")}


def _traceback_excerpt(stderr: str) -> str:
    marker = stderr.rfind("Traceback (most recent call last):")
    excerpt = stderr[marker:] if marker >= 0 else stderr
    return excerpt[-4_000:]


def _traceback_chain(root: Path, stderr: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for path, line, function in re.findall(r'File "([^"]+)", line (\d+), in ([^\n]+)', stderr):
        try:
            resolved = Path(path).resolve()
            if resolved.is_relative_to(root):
                frames.append({"path": resolved.relative_to(root).as_posix(), "line": int(line), "function": function})
        except OSError:
            continue
    return frames


def _stderr_exception(stderr: str) -> tuple[str | None, str | None]:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return None, None
    name, separator, message = lines[-1].partition(":")
    if not name or not name.replace("_", "").isalnum() or not name[:1].isupper():
        return None, None
    return name, message.strip()[:1_000] if separator else None
