"""Temporary external-service adapters used only by explicit Hybrid runs."""

from __future__ import annotations

import builtins
import io
import json
import os
import re
import socket
import sqlite3
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path
from types import ModuleType
from typing import Any


_LIMIT = 4096
_SENSITIVE = (
    "authorization", "proxy_authorization", "password", "passwd", "secret", "token",
    "api_key", "apikey", "access_key", "client_secret", "session_key", "credential",
    "cookie", "set_cookie", "private_key", "parameters",
)
_ENVIRONMENT_SECRETS = {
    value for key, value in os.environ.items()
    if len(value) >= 4 and any(word in key.lower().replace("-", "_") for word in _SENSITIVE)
}


class MockHttpResponse:
    """Small response object compatible with common requests/urllib usage."""

    def __init__(self, rule: dict[str, Any]) -> None:
        self.status_code = int(rule.get("status", 200))
        self.status = self.status_code
        self.headers = dict(rule.get("headers", {}))
        body = rule.get("body", "")
        if "json" in rule:
            body = json.dumps(rule["json"])
            self.headers.setdefault("content-type", "application/json")
        self.content = body if isinstance(body, bytes) else str(body).encode()
        self.text = self.content.decode(errors="replace")
        self.reason = str(rule.get("reason", "Mocked response"))

    def json(self) -> Any:
        return json.loads(self.text)

    def read(self, amount: int = -1) -> bytes:
        return self.content if amount < 0 else self.content[:amount]

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.reason}")

    def __enter__(self) -> MockHttpResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class MockCursor:
    def __init__(self, real: Any, controller: AdapterController) -> None:
        self._real = real
        self._controller = controller
        self._rows: list[Any] | None = None

    def execute(self, query: str, parameters: Any = ()) -> MockCursor:
        rule = self._controller.database_rule(query)
        self._controller.record("database", "execute", query=query, parameters=parameters, mocked=bool(rule))
        if rule:
            _raise_rule_error(rule, "database")
            self._rows = list(rule.get("rows", []))
            return self
        self._real.execute(query, parameters)
        return self

    def executemany(self, query: str, parameters: Any) -> MockCursor:
        materialised = list(parameters)
        rule = self._controller.database_rule(query)
        self._controller.record("database", "executemany", query=query, parameters=materialised, mocked=bool(rule))
        if rule:
            _raise_rule_error(rule, "database")
            self._rows = list(rule.get("rows", []))
            return self
        self._real.executemany(query, materialised)
        return self

    def fetchone(self) -> Any:
        if self._rows is None:
            return self._real.fetchone()
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[Any]:
        if self._rows is None:
            return self._real.fetchall()
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self) -> Any:
        return iter(self.fetchall() if self._rows is not None else self._real)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class MockConnection:
    def __init__(self, real: Any, controller: AdapterController) -> None:
        self._real = real
        self._controller = controller

    def cursor(self, *args: Any, **kwargs: Any) -> MockCursor:
        return MockCursor(self._real.cursor(*args, **kwargs), self._controller)

    def execute(self, query: str, parameters: Any = ()) -> MockCursor:
        return self.cursor().execute(query, parameters)

    def executemany(self, query: str, parameters: Any) -> MockCursor:
        return self.cursor().executemany(query, parameters)

    def commit(self) -> None:
        self._controller.record("database", "commit")
        self._real.commit()

    def rollback(self) -> None:
        self._controller.record("database", "rollback")
        self._real.rollback()

    def close(self) -> None:
        self._controller.record("database", "close")
        self._real.close()

    def __enter__(self) -> MockConnection:
        self._real.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._real.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class AdapterController:
    """Install verified standard-library and requests-compatible adapters."""

    def __init__(self, workspace: Path, config: dict[str, Any], interactions: list[dict[str, Any]]) -> None:
        self.workspace = workspace.resolve()
        self.config = config if isinstance(config, dict) else {}
        self.interactions = interactions
        self._restores: list[tuple[Any, str, Any]] = []
        self._installed: set[tuple[int, str]] = set()

    def __enter__(self) -> AdapterController:
        self._install_filesystem()
        self._install_http()
        self._install_database()
        self._install_network()
        self._install_processes()
        return self

    def __exit__(self, *_args: Any) -> None:
        for owner, name, original in reversed(self._restores):
            setattr(owner, name, original)

    def refresh_aliases(self) -> None:
        """Replace project-module aliases that were bound before adapters."""
        for owner, name, original in list(self._restores):
            replacement = getattr(owner, name)
            self._replace_project_aliases(original, replacement)

    def record(self, adapter: str, operation: str, **details: Any) -> None:
        self.interactions.append({"kind": "adapter", "adapter": adapter, "operation": operation, **_safe(details)})

    def database_rule(self, query: str) -> dict[str, Any] | None:
        for rule in self._rules("database"):
            pattern = str(rule.get("query_contains", ""))
            if not pattern or pattern in query:
                return rule
        return None

    def _rules(self, name: str) -> list[dict[str, Any]]:
        value = self.config.get(name, [])
        if isinstance(value, dict):
            value = [value]
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def _patch(self, owner: Any, name: str, replacement: Any) -> None:
        key = (id(owner), name)
        if key in self._installed or not hasattr(owner, name):
            return
        self._installed.add(key)
        self._restores.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)

    def _install_filesystem(self) -> None:
        original_open = builtins.open

        def open_file(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            path = self._path(file)
            operation = "write" if any(flag in mode for flag in "wax+") else "read"
            rule = self._file_rule(path, operation)
            if self._record_file(path, rule):
                self.record("filesystem", operation, path=str(path), mode=mode, mocked=bool(rule))
            if rule:
                _raise_rule_error(rule, "filesystem")
                if "content" in rule and operation == "read":
                    content = rule["content"]
                    return io.BytesIO(str(content).encode()) if "b" in mode else io.StringIO(str(content))
            return original_open(file, mode, *args, **kwargs)

        self._patch(builtins, "open", open_file)
        self._patch(io, "open", open_file)

    def _install_http(self) -> None:
        original_urlopen = urllib.request.urlopen

        def urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
            url = getattr(request, "full_url", request)
            method = getattr(request, "method", None) or "GET"
            rule = self._http_rule(str(url), method)
            self.record("http", method.upper(), url=str(url), mocked=bool(rule))
            if rule:
                _raise_rule_error(rule, "http")
                return MockHttpResponse(rule)
            return original_urlopen(request, *args, **kwargs)

        self._patch(urllib.request, "urlopen", urlopen)
        try:
            import requests.sessions
        except ImportError:
            return
        original_request = requests.sessions.Session.request

        def request(session: Any, method: str, url: str, **kwargs: Any) -> Any:
            rule = self._http_rule(url, method)
            self.record("http", method.upper(), url=url, mocked=bool(rule), request=kwargs)
            if rule:
                _raise_rule_error(rule, "http")
                response = MockHttpResponse(rule)
                response.url = url
                return response
            return original_request(session, method, url, **kwargs)

        self._patch(requests.sessions.Session, "request", request)

    def _install_database(self) -> None:
        original_connect = sqlite3.connect

        def connect(database: Any, *args: Any, **kwargs: Any) -> MockConnection:
            self.record("database", "connect", database=str(database))
            return MockConnection(original_connect(database, *args, **kwargs), self)

        self._patch(sqlite3, "connect", connect)

    def _install_network(self) -> None:
        original_create = socket.create_connection

        def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
            rule = self._network_rule(address)
            self.record("network", "connect", address=address, mocked=bool(rule))
            if rule:
                _raise_rule_error(rule, "network")
            return original_create(address, *args, **kwargs)

        self._patch(socket, "create_connection", create_connection)

    def _install_processes(self) -> None:
        original_popen = subprocess.Popen

        def popen(command: Any, *args: Any, **kwargs: Any) -> Any:
            rule = self._process_rule(command)
            self.record("process", "start", command=command, mocked=bool(rule))
            if rule:
                _raise_rule_error(rule, "process")
                if rule.get("block", False):
                    raise PermissionError("Process start blocked by experiment adapter.")
            return original_popen(command, *args, **kwargs)

        self._patch(subprocess, "Popen", popen)

    def _http_rule(self, url: str, method: str) -> dict[str, Any] | None:
        for rule in self._rules("http"):
            if rule.get("method", method).upper() == method.upper() and str(rule.get("url_contains", "")) in url:
                return rule
        return None

    def _file_rule(self, path: Path, operation: str) -> dict[str, Any] | None:
        for rule in self._rules("filesystem"):
            if rule.get("operation", operation) == operation and str(rule.get("path_contains", "")) in str(path):
                return rule
        return None

    def _network_rule(self, address: Any) -> dict[str, Any] | None:
        text = str(address)
        return next((rule for rule in self._rules("network") if str(rule.get("address_contains", "")) in text), None)

    def _process_rule(self, command: Any) -> dict[str, Any] | None:
        text = str(command)
        return next((rule for rule in self._rules("process") if str(rule.get("command_contains", "")) in text), None)

    def _replace_project_aliases(self, original: Any, replacement: Any) -> None:
        import sys
        for module in list(sys.modules.values()):
            if not isinstance(module, ModuleType) or not self._project_module(module):
                continue
            for name, value in list(vars(module).items()):
                if value is original:
                    self._patch(module, name, replacement)

    def _project_module(self, module: ModuleType) -> bool:
        path = getattr(module, "__file__", None)
        if not path:
            return False
        try:
            return Path(path).resolve().is_relative_to(self.workspace)
        except OSError:
            return False

    def _path(self, value: Any) -> Path:
        try:
            path = Path(value)
        except TypeError:
            return self.workspace / "<file descriptor>"
        return (Path.cwd() / path).resolve() if not path.is_absolute() else path.resolve()

    def _record_file(self, path: Path, rule: dict[str, Any] | None) -> bool:
        if rule:
            return True
        try:
            return path.is_relative_to(self.workspace) and path.suffix not in {".py", ".pyc"}
        except OSError:
            return False


def _raise_rule_error(rule: dict[str, Any], adapter: str) -> None:
    error = rule.get("error")
    if not error:
        return
    message = str(rule.get("message", f"Mocked {adapter} failure."))
    if str(error).lower() == "timeout":
        raise TimeoutError(message)
    if str(error).lower() in {"network", "connection"}:
        raise ConnectionError(message)
    if str(error).lower() in {"permission", "denied"}:
        raise PermissionError(message)
    raise RuntimeError(message)


def _sensitive(name: str, value: Any = None) -> bool:
    lowered = name.lower().replace("-", "_")
    if any(word in lowered for word in _SENSITIVE):
        return True
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return isinstance(value, str) and any(secret in value for secret in _ENVIRONMENT_SECRETS)


def _safe_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
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
        value = re.sub(r"([?&][^=&#?]+)=([^&#]*)", r"\1=<redacted>", value)
        return re.sub(r"(://)[^/@\s]+@", r"\1<redacted>@", value)


_URL = re.compile(r"https?://[^\s\"'<>]+")
_CREDENTIAL = re.compile(
    rf"(?i)([\"']?(?:{'|'.join(_SENSITIVE)})[\"']?\s*[:=]\s*[\"']?)([^\"',&}}\s]+)"
)


def _scrub(value: str) -> str:
    value = _CREDENTIAL.sub(lambda match: match.group(1) + "<redacted>", value)
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+", r"\1<redacted>", value)
    value = _URL.sub(lambda match: _safe_url(match.group(0)), value)
    for secret in sorted(_ENVIRONMENT_SECRETS, key=len, reverse=True):
        value = value.replace(secret, "<redacted>")
    return value


def _safe(value: Any, depth: int = 0, name: str = "") -> Any:
    if _sensitive(name, value):
        return "<redacted>"
    if depth > 3:
        return {"type": type(value).__name__, "truncated": True, "reason": "depth"}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        rendered = _scrub(value)
        return rendered if len(rendered) <= _LIMIT else {
            "preview": rendered[:_LIMIT], "size": len(rendered), "truncated": True,
        }
    if isinstance(value, bytes):
        preview = value[:_LIMIT].decode(errors="replace")
        return {"type": "bytes", "preview": preview, "size": len(value), "truncated": len(value) > _LIMIT}
    if isinstance(value, (list, tuple)):
        items = [_safe(item, depth + 1, name) for item in value[:50]]
        if len(value) > 50:
            items.append({"type": "truncation", "size": len(value), "captured": 50, "truncated": True})
        return items
    if isinstance(value, dict):
        values = list(value.items())
        result = {str(key)[:80]: _safe(item, depth + 1, str(key)) for key, item in values[:50]}
        if len(values) > 50:
            result["_threadtrace_truncation"] = {"size": len(values), "captured": 50, "truncated": True}
        return result
    return {"type": type(value).__name__}
