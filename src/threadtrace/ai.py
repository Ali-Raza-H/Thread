"""Optional, evidence-only AI narration for ThreadTrace stored analysis.

Providers receive a bounded, redacted subset of already persisted facts. Remote
endpoints require approval of the exact payload, and this module never reads
target source, executes target code, or logs credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .config import application_directory


class AIError(RuntimeError):
    """An optional AI provider could not produce a valid result."""


DEFAULT_ENDPOINTS = {
    "ollama": "http://127.0.0.1:11434/api/chat",
    "openai": "http://127.0.0.1:8080/v1/chat/completions",
}
SUPPORTED_PARAMETERS = {
    "ollama": frozenset({"top_p", "top_k", "seed", "num_ctx", "stop", "repeat_penalty"}),
    "openai": frozenset({"top_p", "seed", "stop", "presence_penalty", "frequency_penalty"}),
}
_SECRET_KEYS = frozenset({
    "password", "passwd", "secret", "token", "api_key", "apikey", "credential",
    "private_key", "cookie", "authorization",
})
_SECRET_TEXT = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|credential|authorization)\b(\s*[:=]\s*)([^\s,;\"']+)"
)
_MAX_TEMPLATE_BYTES = 20_000


@dataclass(frozen=True)
class AISettings:
    """Nonsecret provider settings; credentials live in the environment."""

    provider: str = "ollama"
    model: str = "llama3.2"
    endpoint: str | None = None
    temperature: float = 0.2
    token_limit: int = 900
    timeout_seconds: int = 45
    key_env: str = "THREADTRACE_AI_API_KEY"
    template_path: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return self.endpoint or DEFAULT_ENDPOINTS[self.provider]

    @property
    def remote(self) -> bool:
        return not _loopback(self.url)

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "model": self.model, "endpoint": safe_url(self.url),
            "temperature": self.temperature, "token_limit": self.token_limit,
            "timeout_seconds": self.timeout_seconds, "key_env": self.key_env,
            "template": self.template_path, "parameters": dict(self.parameters),
            "remote": self.remote,
        }


def settings_from(options: dict[str, Any] | None = None) -> AISettings:
    """Validate configuration into provider settings, rejecting unsupported options."""
    values = dict(options or {})
    provider = str(values.get("provider") or "ollama")
    if provider not in DEFAULT_ENDPOINTS:
        raise AIError(f"Unsupported AI provider '{provider}'. Choose ollama or openai.")
    endpoint = values.get("endpoint") or None
    if endpoint is not None:
        endpoint = str(endpoint)
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise AIError("AI endpoint must be an http:// or https:// URL.")
        if parsed.username or parsed.password:
            raise AIError("AI credentials belong in the environment, not in the endpoint URL.")
    parameters: dict[str, Any] = {}
    raw = values.get("parameters") or []
    if isinstance(raw, dict):
        items = list(raw.items())
    else:
        items = []
        for item in raw:
            name, separator, value = str(item).partition("=")
            if not separator:
                raise AIError(f"AI parameter '{item}' needs key=value.")
            items.append((name, value))
    supported = SUPPORTED_PARAMETERS[provider]
    for name, value in items:
        name = str(name).strip()
        if not name:
            raise AIError("AI parameters need a non-empty name.")
        if name not in supported:
            raise AIError(f"Unsupported {provider} parameter '{name}'. Supported: {', '.join(sorted(supported))}.")
        parameters[name] = _scalar(value)
    temperature = float(values.get("temperature", 0.2))
    token_limit = int(values.get("generation_limit", values.get("token_limit", 900)))
    timeout_seconds = int(values.get("timeout_seconds", 45))
    if not 0 <= temperature <= 2:
        raise AIError("AI temperature must be between 0 and 2.")
    if token_limit < 1 or timeout_seconds < 1:
        raise AIError("AI token limit and timeout must be positive.")
    key_env = str(values.get("key_env") or "THREADTRACE_AI_API_KEY")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise AIError("ai.key_env must be an environment-variable name.")
    return AISettings(
        provider=provider, model=str(values.get("model") or "llama3.2"), endpoint=endpoint,
        temperature=temperature, token_limit=token_limit, timeout_seconds=timeout_seconds,
        key_env=key_env, template_path=values.get("template_path") or None, parameters=parameters,
    )


def build_request(facts: dict[str, Any], settings: AISettings) -> dict[str, Any]:
    """Build the exact bounded payload and its approval digest without sending it."""
    redacted, redactions = _redact(_bounded(facts))
    prompt = _render_prompt(redacted, settings)
    payload = _payload(prompt, settings)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "digest": digest,
        "remote": settings.remote,
        "payload": payload,
        "prompt": prompt,
        "manifest": {
            "provider": settings.provider, "model": settings.model, "endpoint": safe_url(settings.url),
            "prompt_bytes": len(prompt.encode("utf-8")),
            "sections": [
                {"name": name, "bytes": len(json.dumps(value, default=str).encode("utf-8"))}
                for name, value in redacted.items()
            ],
            "redactions": redactions,
            "limits": {"max_depth": 5, "max_items": 40, "max_string": 1000},
            "notice": "Only this bounded, redacted stored-analysis context is submitted.",
        },
    }


def explain(
    facts: dict[str, Any], settings: AISettings | None = None,
    approve: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Request, validate, and describe one AI interpretation of stored facts."""
    options = settings or settings_from()
    request = build_request(facts, options)
    if request["remote"] and (approve is None or not approve(request)):
        raise AIError(
            f"Remote AI endpoint {safe_url(options.url)} requires fresh approval of the exact payload "
            f"(SHA-256 {request['digest']}). Review it with --ai-show, then re-run with --ai-approve {request['digest']}."
        )
    structured = _validate(_call(request["payload"], options), facts)
    return {
        "provider": options.provider,
        "model": options.model,
        "settings": options.public(),
        "request": {
            "digest": request["digest"], "remote": request["remote"],
            "prompt": request["prompt"], "manifest": request["manifest"],
        },
        "response": structured,
        "narrative": _narrative(structured),
        "evidence": sorted({claim["evidence"] for claim in structured["claims"] if claim["evidence"] != "unknown"}),
        "notice": "AI interpretation, not verified evidence. Check every citation against the stored analysis.",
    }


def _scalar(value: Any) -> Any:
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value).strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def _loopback(url: str) -> bool:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return host in {"127.0.0.1", "::1", "localhost"}


def safe_url(url: str) -> str:
    """Describe an endpoint without user information or query values."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}" + ("?<redacted>" if parts.query else "")


def _system_prompt() -> str:
    return (
        "You explain Python projects from ThreadTrace static-analysis evidence. Use only the supplied facts. "
        "Return one JSON object with exactly: summary (string), claims (array of objects with text and evidence), "
        "and mermaid (string or null). Each claim evidence must be \"path:line\" copied from the supplied facts, "
        "or \"unknown\". A mermaid diagram must start with graph TD (or flowchart TD/LR), declare every node as "
        "ID[\"symbol.name\"], then add ID --> ID edges; only use symbols from the facts. "
        "Never invent runtime behaviour, library capabilities, missing links, or source locations. "
        "Say unknown when evidence is insufficient."
    )


def _payload(prompt: str, settings: AISettings) -> dict[str, Any]:
    messages = [{"role": "system", "content": _system_prompt()}, {"role": "user", "content": prompt}]
    if settings.provider == "ollama":
        return {
            "model": settings.model, "stream": False, "messages": messages, "format": "json",
            "options": {"temperature": settings.temperature, "num_predict": settings.token_limit, **settings.parameters},
        }
    return {
        "model": settings.model, "messages": messages, "temperature": settings.temperature,
        "max_tokens": settings.token_limit, "response_format": {"type": "json_object"}, **settings.parameters,
    }


def _render_prompt(facts: dict[str, Any], settings: AISettings) -> str:
    facts_json = json.dumps(facts, indent=2, default=str)
    if not settings.template_path:
        return _prompt(facts)
    path = Path(settings.template_path).expanduser()
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AIError(f"Cannot read the AI template: {exc}") from exc
    if len(template.encode("utf-8")) > _MAX_TEMPLATE_BYTES:
        raise AIError("AI template must be at most 20 KB.")
    if template.count("{facts}") != 1:
        raise AIError("AI template must contain the {facts} placeholder exactly once.")
    return template.replace("{facts}", facts_json)


def _prompt(facts: dict[str, Any]) -> str:
    return "Explain this ThreadTrace evidence:\n\n```json\n" + json.dumps(_bounded(facts), indent=2, default=str) + "\n```"


def _bounded(value: Any, depth: int = 0) -> Any:
    """Cap provider input without changing the source facts held by ThreadTrace."""
    if depth > 5:
        return "<truncated>"
    if isinstance(value, str):
        return value[:1_000]
    if isinstance(value, dict):
        return {str(key): _bounded(item, depth + 1) for key, item in list(value.items())[:40]}
    if isinstance(value, list):
        return [_bounded(item, depth + 1) for item in value[:40]]
    return value


def _redact(value: Any) -> tuple[Any, int]:
    """Remove secret-looking fields and assignments from the outbound context."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            if any(word in str(key).lower() for word in _SECRET_KEYS):
                result[str(key)] = "<redacted>"
                count += 1
                continue
            cleaned, nested = _redact(item)
            result[str(key)] = cleaned
            count += nested
        return result, count
    if isinstance(value, list):
        cleaned = [_redact(item) for item in value]
        return [item for item, _ in cleaned], sum(count for _, count in cleaned)
    if isinstance(value, str):
        redacted, count = _SECRET_TEXT.subn(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", value)
        return redacted, count
    return value, 0


def _credential(settings: AISettings) -> str | None:
    value = os.environ.get(settings.key_env, "").strip()
    if value:
        return value
    path = application_directory() / ".env"
    try:
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise AIError(f"The local AI credential file {path} must not be readable by group or others; use chmod 600.")
        for line in path.read_text(encoding="utf-8").splitlines():
            name, separator, raw = line.strip().partition("=")
            if separator and name.strip() == settings.key_env:
                return raw.strip().strip("\"'")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AIError(f"Cannot read the local AI credential file: {exc}") from exc
    return None


def _call(payload: dict[str, Any], settings: AISettings) -> str:
    headers: dict[str, str] = {}
    credential = _credential(settings)
    if settings.provider == "openai" and not credential:
        raise AIError(f"The OpenAI-compatible provider needs a credential in {settings.key_env}.")
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    response = _post(settings.url, payload, headers, settings.timeout_seconds)
    if settings.provider == "ollama":
        content = response.get("message", {}).get("content")
    else:
        choices = response.get("choices") or [{}]
        content = choices[0].get("message", {}).get("content") if isinstance(choices[0], dict) else None
    if not isinstance(content, str) or not content.strip():
        raise AIError("The provider returned no explanation.")
    return content


def _post(url: str, body: dict[str, Any], headers: dict[str, str] | None, timeout: int) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AIError(f"AI provider returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AIError(f"AI provider request failed: {exc}") from exc


def _validate(content: str, facts: dict[str, Any]) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIError(f"The provider returned invalid JSON: {exc.msg}.") from exc
    if not isinstance(data, dict):
        raise AIError("AI response must be a JSON object.")
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise AIError("AI response needs a non-empty summary.")
    claims = data.get("claims", [])
    if not isinstance(claims, list) or len(claims) > 50:
        raise AIError("AI response claims must be a list of at most 50 items.")
    known = _known_locations(facts)
    validated: list[dict[str, str]] = []
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not claim["text"].strip():
            raise AIError("Each AI claim needs non-empty text.")
        evidence = str(claim.get("evidence") or "").strip()
        if evidence.lower() != "unknown" and not _known_evidence(evidence, known):
            raise AIError(f"AI claim cites evidence not present in the stored analysis: {evidence}")
        validated.append({"text": claim["text"].strip(), "evidence": evidence or "unknown"})
    mermaid = data.get("mermaid")
    if mermaid not in (None, ""):
        if not isinstance(mermaid, str):
            raise AIError("AI mermaid output must be text or null.")
        _validate_mermaid(mermaid, facts)
    else:
        mermaid = None
    return {"summary": summary.strip(), "claims": validated, "mermaid": mermaid}


def _known_locations(facts: Any) -> set[str]:
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            path = value.get("relative_path") or value.get("path")
            line = value.get("line_start") or value.get("line_number") or value.get("line")
            if isinstance(path, str) and isinstance(line, int):
                found.add(f"{path}:{line}")
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(facts)
    return found


def _known_evidence(evidence: str, known: set[str]) -> bool:
    path, _, span = evidence.rpartition(":")
    start = span.split("-", 1)[0]
    if path and start.isdigit() and f"{path}:{start}" in known:
        return True
    return any(location.startswith(evidence + ":") for location in known)


def _known_names(facts: Any) -> set[str]:
    names: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("qualified_name", "name", "module_name", "relative_path", "target", "entry_point"):
                item = value.get(key)
                if isinstance(item, str) and item:
                    names.add(item)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(facts)
    return names


def _validate_mermaid(mermaid: str, facts: dict[str, Any]) -> None:
    lines = [line.strip() for line in mermaid.splitlines() if line.strip()]
    if not lines or not re.fullmatch(r"(graph|flowchart)\s+(TD|TB|BT|LR|RL)", lines[0]):
        raise AIError("AI mermaid must start with graph TD (or flowchart TD/LR).")
    known = _known_names(facts)
    declared: set[str] = set()
    for line in lines[1:]:
        line = line.rstrip(";").strip()
        node = re.fullmatch(r"([A-Za-z][\w-]*)\s*\[\s*\"([^\"]+)\"\s*\]", line)
        if node:
            label = node.group(2)
            if "." in label and label not in known:
                raise AIError(f"AI mermaid references a symbol not present in the stored analysis: {label}")
            declared.add(node.group(1))
            continue
        edge = re.fullmatch(r"([A-Za-z][\w-]*)\s*-->\s*([A-Za-z][\w-]*)", line)
        if edge and edge.group(1) in declared and edge.group(2) in declared:
            continue
        raise AIError(f"AI mermaid contains an unsupported or invalid line: {line[:80]}")


def _narrative(structured: dict[str, Any]) -> str:
    lines = [structured["summary"], ""]
    if structured["claims"]:
        lines.append("Claims")
        lines.extend(f"- {claim['text']} [{claim['evidence']}]" for claim in structured["claims"])
    if structured["mermaid"]:
        lines.extend(["", "```mermaid", structured["mermaid"], "```"])
    lines.extend(["", "AI interpretation, not verified evidence. Check every citation against the stored analysis."])
    return "\n".join(lines)
