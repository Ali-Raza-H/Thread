"""Small, dependency-free, versioned configuration support for ThreadTrace."""

from __future__ import annotations

import copy
import os
import re
import sys
from pathlib import Path
from typing import Any

CONFIG_VERSION = 1
_SECRET_NAMES = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential", "private_key", "cookie")

DEFAULTS: dict[str, Any] = {
    "schema_version": CONFIG_VERSION,
    "storage": {
        "database_path": None,
        "retention_days": 30,
        "trace_saving": "explicit",
        "quota_mb": 1024,
        "usage_report": True,
    },
    "discovery": {"include": [], "exclude": []},
    "profiles": {"default_interpreter": None},
    "tui": {"compact": False, "ascii": False, "colour": True, "keybindings": []},
    "browser": {"host": "127.0.0.1", "port": 0},
    "runtime": {
        "timeout_seconds": 30,
        "output_cap": 100_000,
        "capture_level": "metadata",
        "capture_limit": 4096,
    },
    "exports": {"default_format": "text"},
    "ai": {
        "provider": "ollama", "model": "llama3.2", "endpoint": None,
        "temperature": 0.2, "generation_limit": 900, "timeout_seconds": 45,
        "key_env": "THREADTRACE_AI_API_KEY", "template_path": None, "parameters": [],
    },
}

_SCHEMA: dict[str, dict[str, tuple[str, Any]]] = {
    "storage": {
        "database_path": ("path", None),
        "retention_days": ("int", 0),
        "trace_saving": ("choice", {"explicit", "none"}),
        "quota_mb": ("int", 0),
        "usage_report": ("bool", None),
    },
    "discovery": {"include": ("list", None), "exclude": ("list", None)},
    "profiles": {"default_interpreter": ("path", None)},
    "tui": {"compact": ("bool", None), "ascii": ("bool", None), "colour": ("bool", None), "keybindings": ("bindings", None)},
    "browser": {"host": ("choice", {"127.0.0.1", "::1"}), "port": ("int", 0)},
    "runtime": {
        "timeout_seconds": ("int", 1),
        "output_cap": ("int", 1),
        "capture_level": ("choice", {"off", "metadata", "content"}),
        "capture_limit": ("int", 1),
    },
    "exports": {"default_format": ("choice", {"text", "json", "markdown", "csv", "html"})},
    "ai": {
        "provider": ("choice", {"ollama", "openai"}), "model": ("text", None),
        "endpoint": ("path", None), "temperature": ("float", (0.0, 2.0)),
        "generation_limit": ("int", 1), "timeout_seconds": ("int", 1),
        "key_env": ("text", None), "template_path": ("path", None),
        "parameters": ("parameters", None),
    },
}


def application_directory() -> Path:
    """Return the per-user application directory without creating it."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Thread"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Thread"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Thread"


def configuration_path() -> Path:
    return application_directory() / "config.yaml"


def _scalar(value: str) -> Any:
    value = value.strip()
    if value in {"", "null", "~"}:
        return None
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [item.strip().strip("\"'") for item in inner.split(",")]
    if value.isdecimal():
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value.strip("\"'")


def _parse(text: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    section: str | None = None
    seen: set[tuple[str | None, str]] = set()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent not in {0, 2} or ":" not in line:
            raise ValueError(f"Unsupported configuration syntax on line {number}.")
        key, value = line.strip().split(":", 1)
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", key):
            raise ValueError(f"Invalid configuration key on line {number}.")
        marker = (section, key)
        if marker in seen:
            raise ValueError(f"Duplicate configuration key '{key}' on line {number}.")
        seen.add(marker)
        if indent == 0:
            if value.strip():
                section = None
                parsed[key] = _scalar(value)
            else:
                section = key
                parsed.setdefault(key, {})
        else:
            if section is None or not isinstance(parsed.get(section), dict):
                raise ValueError(f"Indented configuration key '{key}' has no section on line {number}.")
            parsed[section][key] = _scalar(value)
    return parsed


def _validate_value(name: str, value: Any, kind: str, constraint: Any) -> Any:
    if kind == "path":
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{name} must be a path string or null.")
        return value
    if kind == "text":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-empty text.")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false.")
        return value
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool) or value < constraint:
            raise ValueError(f"{name} must be an integer from {constraint} upward.")
        return value
    if kind == "float":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{name} must be a number.")
        low, high = constraint
        if not low <= float(value) <= high:
            raise ValueError(f"{name} must be between {low} and {high}.")
        return float(value)
    if kind == "parameters":
        if not isinstance(value, list):
            raise ValueError(f"{name} must be a list of name=value parameters.")
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str) or item.count("=") != 1:
                raise ValueError(f"{name} entries must look like name=value.")
            parameter, raw = (part.strip() for part in item.split("="))
            if not re.fullmatch(r"[a-z][a-z0-9_]*", parameter) or not re.fullmatch(r"[!-~]{1,32}", raw):
                raise ValueError(f"{name} entry '{item}' needs a lowercase parameter name and a printable value.")
            if parameter in seen:
                raise ValueError(f"{name} repeats parameter '{parameter}'.")
            seen.add(parameter)
        return value
    if kind == "choice":
        if value not in constraint:
            raise ValueError(f"{name} must be one of: {', '.join(sorted(constraint))}.")
        return value
    if kind == "list":
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ValueError(f"{name} must be a list of non-empty text values.")
        return value
    if kind == "bindings":
        if not isinstance(value, list):
            raise ValueError(f"{name} must be a list of action=key strings.")
        actions: set[str] = set()
        keys: set[str] = set()
        for item in value:
            if not isinstance(item, str) or item.count("=") != 1:
                raise ValueError(f"{name} entries must look like action=key.")
            action, key = (part.strip() for part in item.split("="))
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", action) or not re.fullmatch(r"[!-~]{1,8}", key):
                raise ValueError(f"{name} entry '{item}' needs a lowercase action and one printable key.")
            if action in actions:
                raise ValueError(f"{name} repeats action '{action}'.")
            if key in keys:
                raise ValueError(f"{name} assigns key '{key}' more than once.")
            actions.add(action)
            keys.add(key)
        return value
    raise ValueError(f"Unsupported schema type for {name}.")


def _validate(data: Any, partial: bool) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a mapping.")
    result: dict[str, Any] = {} if partial else copy.deepcopy(DEFAULTS)
    if "schema_version" in data:
        if data["schema_version"] != CONFIG_VERSION:
            raise ValueError(f"Unsupported configuration schema_version; expected {CONFIG_VERSION}.")
    elif not partial:
        result["schema_version"] = CONFIG_VERSION
    legacy = data.get("database_path")
    if legacy is not None:
        if not isinstance(legacy, str):
            raise ValueError("database_path must be a path string or null.")
        result.setdefault("storage", {})["database_path"] = legacy
    for section, values in data.items():
        if section in {"schema_version", "database_path"}:
            continue
        if section not in _SCHEMA:
            raise ValueError(f"Unknown configuration section '{section}'.")
        if any(word in section.lower() for word in _SECRET_NAMES):
            raise ValueError(f"Secret-looking configuration section '{section}' is not allowed.")
        if not isinstance(values, dict):
            raise ValueError(f"Configuration section '{section}' must be a mapping.")
        target = result.setdefault(section, {})
        for key, value in values.items():
            if key not in _SCHEMA[section]:
                raise ValueError(f"Unknown configuration key '{section}.{key}'.")
            if any(word in key.lower() for word in _SECRET_NAMES):
                raise ValueError(f"Secret-looking key '{section}.{key}' is not allowed.")
            kind, constraint = _SCHEMA[section][key]
            target[key] = _validate_value(f"{section}.{key}", value, kind, constraint)
    if partial:
        return result
    for section, fields in _SCHEMA.items():
        result.setdefault(section, {})
        for key, (_, _constraint) in fields.items():
            result[section].setdefault(key, copy.deepcopy(DEFAULTS[section][key]))
    return result


def validate(data: Any) -> dict[str, Any]:
    """Validate and fill one complete configuration mapping."""
    return _validate(data, partial=False)


def validate_preferences(data: Any) -> dict[str, Any]:
    """Validate a partial per-project preference override."""
    preferences = _validate(data, partial=True)
    if "storage" in preferences and "database_path" in preferences["storage"]:
        raise ValueError("Per-project preferences cannot change the global database path.")
    return preferences


def merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge one validated preference mapping into a validated configuration."""
    result = copy.deepcopy(base)
    for section, values in overrides.items():
        if section == "schema_version":
            continue
        if isinstance(values, dict):
            result.setdefault(section, {}).update(copy.deepcopy(values))
        else:
            result[section] = copy.deepcopy(values)
    return result


def load(path: Path | None = None) -> dict[str, Any]:
    """Read the documented YAML subset and reject ambiguous or secret configuration."""
    selected = path or configuration_path()
    try:
        text = selected.read_text(encoding="utf-8")
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    except OSError as exc:
        raise ValueError(f"Could not read configuration: {exc}") from exc
    return validate(_parse(text))


def write_default(path: Path | None = None, overwrite: bool = False) -> Path:
    """Create the versioned nonsecret configuration file on explicit request."""
    selected = path or configuration_path()
    if selected.exists() and not overwrite:
        raise FileExistsError(f"Configuration already exists: {selected}. Use --force to replace it.")
    selected.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    selected.write_text(
        "# ThreadTrace configuration. Do not store secrets in this file.\n"
        f"schema_version: {CONFIG_VERSION}\n"
        "storage:\n"
        "  database_path: null\n"
        "  retention_days: 30\n"
        "  trace_saving: explicit\n"
        "  quota_mb: 1024\n"
        "  usage_report: true\n"
        "discovery:\n"
        "  include: []\n"
        "  exclude: []\n"
        "profiles:\n"
        "  default_interpreter: null\n"
        "tui:\n"
        "  compact: false\n"
        "  ascii: false\n"
        "  colour: true\n"
        "  keybindings: []\n"
        "browser:\n"
        "  host: 127.0.0.1\n"
        "  port: 0\n"
        "runtime:\n"
        "  timeout_seconds: 30\n"
        "  output_cap: 100000\n"
        "  capture_level: metadata\n"
        "  capture_limit: 4096\n"
        "exports:\n"
        "  default_format: text\n"
        "ai:\n"
        "  provider: ollama\n"
        "  model: llama3.2\n"
        "  endpoint: null\n"
        "  temperature: 0.2\n"
        "  generation_limit: 900\n"
        "  timeout_seconds: 45\n"
        "  key_env: THREADTRACE_AI_API_KEY\n"
        "  template_path: null\n"
        "  parameters: []\n",
        encoding="utf-8",
    )
    try:
        selected.chmod(0o600)
    except OSError:
        pass
    return selected
