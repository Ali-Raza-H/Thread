"""Optional evidence-only AI narration for Thread scan results.

The model receives a compact, structured subset of already persisted scan
facts. It never receives a repository path and this module never reads target
source files.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AIError(RuntimeError):
    """An optional AI provider could not produce a narrative."""


def explain(facts: dict[str, Any], model: str | None = None, endpoint: str | None = None) -> str:
    """Produce a cited Markdown explanation through a local Ollama server."""
    prompt = _prompt(facts)
    body = {
        "model": model or os.environ.get("THREADTRACE_OLLAMA_MODEL", "llama3.2"),
        "stream": False,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": prompt},
        ],
        "options": {"num_predict": 900, "temperature": 0.2},
    }
    response = _post(endpoint or os.environ.get("THREADTRACE_OLLAMA_ENDPOINT", "http://127.0.0.1:11434/api/chat"), body)
    content = response.get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise AIError("The provider returned no explanation.")
    return content.strip()


def _system_prompt() -> str:
    return (
        "You explain Python projects from Thread static-analysis evidence. Use only supplied facts. "
        "Every architectural or behavioural claim must cite a source location in [path:line] form when one exists. "
        "Never invent runtime behaviour, library capabilities, or missing links. Say 'unknown' when evidence is insufficient. "
        "Be concise, practical, and return Markdown."
    )


def _prompt(facts: dict[str, Any]) -> str:
    return "Explain this Thread evidence:\n\n```json\n" + json.dumps(_bounded(facts), indent=2, default=str) + "\n```"


def _bounded(value: Any, depth: int = 0) -> Any:
    """Cap provider input without changing the source facts held by Thread."""
    if depth > 5:
        return "<truncated>"
    if isinstance(value, str):
        return value[:1_000]
    if isinstance(value, dict):
        return {str(key): _bounded(item, depth + 1) for key, item in list(value.items())[:40]}
    if isinstance(value, list):
        return [_bounded(item, depth + 1) for item in value[:40]]
    return value


def _post(url: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AIError(f"AI provider returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AIError(f"AI provider request failed: {exc}") from exc
