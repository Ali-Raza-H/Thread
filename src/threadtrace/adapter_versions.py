"""Adapter version bounds shared by runtime and Hybrid tracing."""

from __future__ import annotations

import re
import sys
from typing import Any

# Each advertised adapter has a tested lower bound. An installed package below
# it is reported as unsupported instead of silently patched.
ADAPTERS: dict[str, dict[str, Any]] = {
    "urllib": {"minimum": "3.11", "package": None},
    "socket": {"minimum": "3.11", "package": None},
    "ftplib": {"minimum": "3.11", "package": None},
    "sqlite3": {"minimum": "3.11", "package": None},
    "urllib3": {"minimum": "1.26", "package": "urllib3"},
    "requests": {"minimum": "2.0", "package": "requests"},
    "httpx": {"minimum": "0.20", "package": "httpx"},
    "aiohttp": {"minimum": "3.8", "package": "aiohttp"},
    "sqlalchemy": {"minimum": "1.4", "package": "sqlalchemy"},
    "django": {"minimum": "3.2", "package": "django"},
}


def version_tuple(version: str) -> tuple[int, int]:
    try:
        parts = re.split(r"[^0-9]+", str(version).split("+", 1)[0])
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return (0, 0)


def distribution_version(package: str | None) -> str | None:
    if not package:
        return None
    try:
        from importlib.metadata import version

        return version(package)
    except Exception:
        return None


def python_version() -> str:
    return "%d.%d.%d" % sys.version_info[:3]
