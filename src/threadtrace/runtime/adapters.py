"""Runtime resource adapters loaded only inside an explicitly traced child."""

from __future__ import annotations

import builtins
import contextvars
import ftplib
import functools
import io
import itertools
import json
import os
import pickle
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request

try:
    from .adapter_versions import ADAPTERS, distribution_version as _distribution_version, version_tuple as _version_tuple
except ImportError:  # Loaded as a copied top-level module inside a traced child.
    from _threadtrace_runtime_adapter_versions import (ADAPTERS, distribution_version as _distribution_version,
                                                       version_tuple as _version_tuple)

_root = os.path.realpath(os.environ.get("THREADTRACE_RUNTIME_ROOT", ""))
_log = os.environ.get("THREADTRACE_RUNTIME_LOG")
_socket_path = os.environ.get("THREADTRACE_RUNTIME_SOCKET")
_session_id = os.environ.get("THREADTRACE_RUNTIME_SESSION")
_level = os.environ.get("THREADTRACE_RUNTIME_CAPTURE_LEVEL", "metadata")
_limit = max(1, int(os.environ.get("THREADTRACE_RUNTIME_CAPTURE_LIMIT", "4096")))
_redact = os.environ.get("THREADTRACE_RUNTIME_REDACT", "1") != "0"
_started_monotonic = time.monotonic()
_started_cpu = time.process_time()
_original_open = builtins.open
_original_io_open = io.open
_original_stat = os.stat
_original_access = os.access
_original_os_open = os.open
_original_os_read = os.read
_original_os_write = os.write
_original_os_close = os.close
_original_remove = os.remove
_original_unlink = os.unlink
_original_rmdir = os.rmdir
_original_rename = os.rename
_original_replace = os.replace
_original_mkdir = os.mkdir
_original_chmod = os.chmod
_original_symlink = os.symlink
_original_link = os.link
_original_truncate = os.truncate
_json_dumps = json.dumps
_busy = threading.local()
_http_depth = contextvars.ContextVar("threadtrace_http_depth", default=0)
_database_depth = contextvars.ContextVar("threadtrace_database_depth", default=0)
_import_depth = contextvars.ContextVar("threadtrace_import_depth", default=0)
_operation_correlation = contextvars.ContextVar("threadtrace_operation_correlation", default=None)
_correlation_ids = itertools.count(1)
_file_descriptors = {}
_file_descriptors_lock = threading.Lock()
_sensitive = (
    "authorization", "proxy-authorization", "password", "passwd", "secret",
    "token", "api_key", "apikey", "credential", "cookie", "set-cookie",
    "private_key", "private-key",
    "access_key", "access-key", "client_secret", "client-secret", "session_key", "session-key",
)
_environment_secrets = {
    value for key, value in os.environ.items()
    if len(value) >= 4 and any(word.replace("-", "_") in key.lower().replace("-", "_") for word in _sensitive)
}

_adapter_cache = None


def statuses():
    """Report installed versions and whether each adapter is safe to patch."""
    global _adapter_cache
    if _adapter_cache is not None:
        return [dict(item) for item in _adapter_cache]
    python_version = "%d.%d.%d" % sys.version_info[:3]
    rows = []
    for name, spec in ADAPTERS.items():
        if name == "urllib":
            version = python_version
        elif name == "sqlite3":
            version = getattr(sqlite3, "sqlite_version", python_version)
        elif name in {"socket", "ftplib"}:
            version = python_version
        else:
            version = _distribution_version(spec["package"])
        if version is None:
            status = "not installed"
        elif _version_tuple(version) < _version_tuple(spec["minimum"]):
            status = "unsupported"
        else:
            status = "supported"
        rows.append({
            "adapter": name, "version": version, "minimum": spec["minimum"],
            "status": status,
            "boundary": "metadata only; payload/TLS content is not inspected" if name in {"socket", "ftplib"} else None,
        })
    _adapter_cache = rows
    return [dict(item) for item in rows]


def _supported(name):
    return any(item["adapter"] == name and item["status"] == "supported" for item in statuses())


def _in_root(path):
    try:
        if not os.path.isabs(path):
            return False
        return os.path.commonpath([os.path.realpath(path), _root]) == _root
    except (TypeError, ValueError):
        return False


def _origin():
    stack = []
    try:
        frame = sys._getframe(1)
    except ValueError:
        return None
    while frame:
        path = frame.f_code.co_filename
        if path == __file__:
            frame = frame.f_back
            continue
        if os.path.basename(path) == "sitecustomize.py":
            return None
        if _in_root(path):
            item = {
                "path": path,
                "relative_path": os.path.relpath(os.path.realpath(path), _root).replace(os.sep, "/"),
                "line": frame.f_lineno,
                "name": frame.f_code.co_name,
            }
            stack.append(item)
        frame = frame.f_back
    if not stack:
        return None
    return stack[0], stack


def _through_importlib():
    try:
        frame = sys._getframe(2)
    except ValueError:
        return False
    while frame:
        if str(frame.f_code.co_filename).startswith("<frozen importlib"):
            return True
        if _in_root(frame.f_code.co_filename):
            return False
        frame = frame.f_back
    return False


def _is_sensitive(name, value=None):
    lowered = str(name).lower().replace("-", "_")
    if any(word.replace("-", "_") in lowered for word in _sensitive):
        return True
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        upper = value.upper()
        if "-----BEGIN " in upper and "PRIVATE KEY-----" in upper:
            return True
        return any(secret in value for secret in _environment_secrets)
    return False


_URL = re.compile(r"https?://[^\s\"'<>]+")


def _scrub_url(text):
    """Redact userinfo, every query value, and the fragment of one URL."""
    try:
        parsed = urllib.parse.urlsplit(text)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        netloc = parsed.netloc
        if "@" in netloc:
            netloc = "<redacted>@" + netloc.rsplit("@", 1)[1]
        return urllib.parse.urlunsplit((
            parsed.scheme, netloc, parsed.path,
            urllib.parse.urlencode([(key, "<redacted>") for key, _item in query]),
            "<redacted>" if parsed.fragment else "",
        ))
    except (TypeError, ValueError):
        text = re.sub(r"([?&][^=&#?]+)=([^&#]*)", r"\1=<redacted>", str(text))
        return re.sub(r"(://)[^/@\s]+@", r"\1<redacted>@", text)


def _scrub_text(rendered):
    keys = "authorization|proxy-authorization|password|passwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|session[_-]?key|credential|cookie|private[_-]?key"
    rendered = re.sub(
        rf"(?i)([\"']?(?:{keys})[\"']?\s*[:=]\s*[\"']?)([^\"',&}}\s]+)",
        lambda match: match.group(1) + "<redacted>", rendered,
    )
    rendered = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+", r"\1<redacted>", rendered)
    for secret in sorted(_environment_secrets, key=len, reverse=True):
        rendered = rendered.replace(secret, "<redacted>")
    return _URL.sub(lambda match: _scrub_url(match.group(0)), rendered)


def _text(value):
    if isinstance(value, bytes):
        rendered, size = value[:_limit].decode("utf-8", errors="replace"), len(value)
    else:
        rendered, size = (value if isinstance(value, str) else str(value)), None
    if _redact:
        rendered = _scrub_text(rendered)
    return rendered[:_limit], len(rendered) if size is None else size


def _clean(value, name="", depth=0):
    if _redact and _is_sensitive(name, value):
        return "<redacted>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (str, bytes)):
        preview, size = _text(value)
        return {"preview": preview, "size": size, "truncated": size > _limit}
    if depth >= 3:
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, dict):
        items = list(value.items())
        cleaned = {
            str(key)[:80]: _clean(item, str(key), depth + 1)
            for key, item in items[:50]
        }
        if len(items) > 50:
            cleaned["_threadtrace_truncation"] = {"size": len(items), "captured": 50, "truncated": True}
        return cleaned
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        cleaned = [_clean(item, name, depth + 1) for item in items[:50]]
        if len(items) > 50:
            cleaned.append({"type": "truncation", "size": len(items), "captured": 50, "truncated": True})
        return cleaned
    return {"type": type(value).__name__}


def _path(value):
    preview, size = _text(os.fspath(value) if isinstance(value, (str, bytes, os.PathLike)) else str(value))
    return preview if size <= _limit else {"preview": preview, "size": size, "truncated": True}


def _redacted_url(value):
    text = str(value)
    rendered = _scrub_url(text) if _redact else text
    preview, size = _text(rendered)
    return preview if size <= _limit else {"preview": preview, "size": size, "truncated": True}


def _headers(value):
    if value is None:
        return {}
    try:
        items = value.items()
    except AttributeError:
        try:
            items = dict(value).items()
        except (TypeError, ValueError):
            return {}
    items = list(items)
    result = {str(key): _clean(item, str(key)) for key, item in items[:100]}
    if len(items) > 100:
        result["_threadtrace_truncation"] = {"size": len(items), "captured": 100, "truncated": True}
    return result


def _publish(resource_kind, operation, started, details, error=None, origin=None, status=None, correlation_id=None):
    if _level == "off" or getattr(_busy, "active", False):
        return
    origin = origin or _origin()
    if origin is None:
        return
    location, call_stack = origin
    item = {
        "protocol_version": 1,
        "kind": "resource",
        "resource_kind": resource_kind,
        "operation": operation,
        "status": status or ("failed" if error else "completed"),
        "correlation_id": correlation_id,
        "path": location["path"],
        "relative_path": location["relative_path"],
        "line": location["line"],
        "name": location["name"],
        "call_stack": call_stack,
        "pid": os.getpid(),
        "thread_id": threading.get_ident(),
        "wall_time": time.time(),
        "elapsed_seconds": time.monotonic() - _started_monotonic,
        "cpu_seconds": time.process_time() - _started_cpu,
        "duration_seconds": time.monotonic() - started,
        "adapter_version": _adapter_version(details.get("adapter")) if isinstance(details, dict) else None,
        "capture": {"level": _level, "limit": _limit, "redacted": _redact},
        "details": details,
        "evidence_class": "observed at runtime",
    }
    if error is not None:
        item["error"] = {"type": type(error).__name__, "message": _clean(str(error), "exception_message")}
    _busy.active = True
    try:
        payload = _json_dumps(item, sort_keys=True, separators=(",", ":"))
        if _socket_path and _session_id and hasattr(__import__("socket"), "AF_UNIX"):
            import socket
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(0.05)
                    connection.connect(_socket_path)
                    message = {"kind": "event", "protocol_version": 1,
                               "session_id": _session_id, "event": item}
                    connection.sendall(_json_dumps(message, separators=(",", ":")).encode() + b"\n")
                return
            except OSError:
                pass
        if _log:
            with _original_open(_log, "a", encoding="utf-8") as handle:
                handle.write(payload + "\n")
    except (OSError, TypeError, ValueError):
        pass
    finally:
        _busy.active = False


def _response_body(response):
    if _level != "content":
        return None
    content = getattr(response, "_content", None)
    if isinstance(content, (str, bytes)):
        return _clean(content, "response_body")
    return None


def _http_details(adapter, method, url, kwargs):
    details = {
        "adapter": adapter,
        "method": str(method or "GET").upper(),
        "url": _redacted_url(url),
        "request_headers": _headers(kwargs.get("headers")),
    }
    if _level == "content":
        for key in ("json", "data", "content", "body", "fields"):
            if key in kwargs:
                details["request_body"] = _clean(kwargs[key], key)
                break
    return details


def _finish_http(details, response):
    status = getattr(response, "status_code", getattr(response, "status", None))
    if status is None and hasattr(response, "getcode"):
        try:
            status = response.getcode()
        except Exception:
            status = None
    details["status_code"] = status
    details["response_headers"] = _headers(getattr(response, "headers", None))
    body = _response_body(response)
    if body is not None:
        details["response_body"] = body


def _sync_http(adapter, original, method_index, url_index):
    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        depth = _http_depth.get()
        token = _http_depth.set(depth + 1)
        started = time.monotonic()
        origin = _origin() if depth == 0 else None
        method = args[method_index] if len(args) > method_index else kwargs.get("method", "GET")
        url = args[url_index] if len(args) > url_index else kwargs.get("url", kwargs.get("fullurl", ""))
        captured = dict(kwargs)
        if adapter == "urllib":
            data = args[2] if len(args) > 2 else kwargs.get("data")
            method = "POST" if data is not None else "GET"
            if data is not None:
                captured["data"] = data
            if hasattr(url, "full_url"):
                method = url.get_method()
                captured["headers"] = dict(url.header_items())
                url = url.full_url
        details = _http_details(adapter, method, url, captured)
        try:
            response = original(*args, **kwargs)
            _finish_http(details, response)
            if depth == 0:
                _publish("http", "request", started, details, origin=origin)
            return response
        except BaseException as error:
            if depth == 0:
                _publish("http", "request", started, details, error, origin)
            raise
        finally:
            _http_depth.reset(token)
    return wrapped


def _async_http(adapter, original, method_index, url_index):
    @functools.wraps(original)
    async def wrapped(*args, **kwargs):
        depth = _http_depth.get()
        token = _http_depth.set(depth + 1)
        started = time.monotonic()
        origin = _origin() if depth == 0 else None
        method = args[method_index] if len(args) > method_index else kwargs.get("method", "GET")
        url = args[url_index] if len(args) > url_index else kwargs.get("url", "")
        details = _http_details(adapter, method, url, kwargs)
        try:
            response = await original(*args, **kwargs)
            _finish_http(details, response)
            if depth == 0:
                _publish("http", "request", started, details, origin=origin)
            return response
        except BaseException as error:
            if depth == 0:
                _publish("http", "request", started, details, error, origin)
            raise
        finally:
            _http_depth.reset(token)
    return wrapped


def _patch_method(owner, name, wrapper):
    original = getattr(owner, name, None)
    if original is None or getattr(original, "_threadtrace_adapter", False):
        return
    replacement = wrapper(original)
    replacement._threadtrace_adapter = True
    setattr(owner, name, replacement)


def _adapter_version(name):
    return next((item["version"] for item in statuses() if item["adapter"] == name), None)


def _address_details(address):
    host, port = None, None
    if isinstance(address, tuple) and address:
        host = address[0]
        port = address[1] if len(address) > 1 else None
    else:
        host = address
    return {"host": str(host)[:300], "port": port, "protocol": "tcp",
            "tls": False, "tls_intercepted": False}


def _socket_connect(original, operation):
    @functools.wraps(original)
    def wrapped(self, address):
        origin = _origin()
        started = time.monotonic()
        details = {"adapter": "socket", **_address_details(address)}
        try:
            result = original(self, address)
            error = OSError(result, os.strerror(result)) if operation == "connect_ex" and result else None
            _publish("network", operation, started, details, error=error, origin=origin)
            return result
        except BaseException as error:
            _publish("network", operation, started, details, error, origin)
            raise
    return wrapped


def _patch_network():
    if not _supported("socket"):
        return
    _patch_method(socket.socket, "connect", lambda fn: _socket_connect(fn, "connect"))
    _patch_method(socket.socket, "connect_ex", lambda fn: _socket_connect(fn, "connect_ex"))


def _ftp_operation(original, operation, fields):
    @functools.wraps(original)
    def wrapped(self, *args, **kwargs):
        origin = _origin()
        started = time.monotonic()
        tls = isinstance(self, ftplib.FTP_TLS)
        details = {"adapter": "ftplib", "protocol": "ftps" if tls else "ftp",
                   "operation": operation, "tls": tls, "tls_intercepted": False}
        for index, name in fields:
            value = args[index] if len(args) > index else kwargs.get(name)
            if value is not None and name not in {"password", "passwd", "acct"}:
                details[name] = str(value)[:500]
        try:
            result = original(self, *args, **kwargs)
            _publish("network", operation, started, details, origin=origin)
            return result
        except BaseException as error:
            _publish("network", operation, started, details, error, origin)
            raise
    return wrapped


def _patch_ftp():
    if not _supported("ftplib"):
        return
    operations = {
        "connect": ("host", "port"),
        "login": ("user",),
        "retrbinary": ("cmd",),
        "retrlines": ("cmd",),
        "storbinary": ("cmd",),
        "storlines": ("cmd",),
        "voidcmd": ("cmd",),
        "delete": ("filename",),
        "rename": ("fromname", "toname"),
        "mkd": ("pathname",),
        "rmd": ("pathname",),
        "size": ("filename",),
        "quit": (),
    }
    for name, fields in operations.items():
        indexed = tuple((index, field) for index, field in enumerate(fields))
        _patch_method(ftplib.FTP, name, lambda fn, operation=name, values=indexed:
                      _ftp_operation(fn, operation, values))


def _patch_http():
    _patch_method(urllib.request.OpenerDirector, "open", lambda fn: _sync_http("urllib", fn, 999, 1))
    requests = sys.modules.get("requests.sessions")
    if requests and _supported("requests"):
        _patch_method(getattr(requests, "Session", None), "request",
                      lambda fn: _sync_http("requests", fn, 1, 2))
    httpx = sys.modules.get("httpx")
    if httpx and _supported("httpx"):
        _patch_method(getattr(httpx, "Client", None), "request",
                      lambda fn: _sync_http("httpx", fn, 1, 2))
        _patch_method(getattr(httpx, "AsyncClient", None), "request",
                      lambda fn: _async_http("httpx", fn, 1, 2))
    aiohttp = sys.modules.get("aiohttp.client")
    if aiohttp and _supported("aiohttp"):
        _patch_method(getattr(aiohttp, "ClientSession", None), "_request",
                      lambda fn: _async_http("aiohttp", fn, 1, 2))
    urllib3 = sys.modules.get("urllib3.poolmanager")
    if urllib3 and _supported("urllib3"):
        _patch_method(getattr(urllib3, "PoolManager", None), "request",
                      lambda fn: _sync_http("urllib3", fn, 1, 2))


def _database_call(adapter, operation, database, statement, parameters, function, args, kwargs):
    depth = _database_depth.get()
    token = _database_depth.set(depth + 1)
    started = time.monotonic()
    origin = _origin() if depth == 0 else None
    details = {"adapter": adapter, "database": _clean(database, "database")}
    if statement is not None:
        details["statement"] = _clean(statement, "statement")
    if parameters is not None and _level == "content":
        details["parameters"] = "<redacted>" if _redact else _clean(parameters, "parameters")
    owner = getattr(function, "__self__", None)
    connection = getattr(owner, "connection", owner)
    in_transaction_before = getattr(connection, "in_transaction", None)
    try:
        result = function(*args, **kwargs)
        if depth == 0:
            in_transaction_after = getattr(connection, "in_transaction", None)
            details["transaction"] = {"before": in_transaction_before, "after": in_transaction_after}
            if in_transaction_before is False and in_transaction_after is True:
                _publish("database", "begin", started,
                         {"adapter": adapter, "database": _clean(database, "database")}, origin=origin)
            _publish("database", operation, started, details, origin=origin)
        return result
    except BaseException as error:
        if depth == 0:
            _publish("database", operation, started, details, error, origin)
        raise
    finally:
        _database_depth.reset(token)


def _database_fetch(cursor, operation, function, args=(), kwargs=None):
    kwargs = kwargs or {}
    started = time.monotonic()
    origin = _origin()
    details = {"adapter": "sqlite3", "database": _clean(cursor.connection._threadtrace_database, "database")}
    try:
        result = function(*args, **kwargs)
        if isinstance(result, list):
            details["row_count"] = len(result)
        elif result is None:
            details["row_count"] = 0
        else:
            details["row_count"] = 1
        if _level == "content":
            statement = getattr(cursor, "_threadtrace_statement", "")
            row_name = "secret" if any(word in str(statement).lower() for word in _sensitive) else "rows"
            details["rows"] = _clean(result, row_name)
        _publish("database", operation, started, details, origin=origin)
        return result
    except BaseException as error:
        _publish("database", operation, started, details, error, origin)
        raise


def _sql_operation(sql):
    match = re.match(r"\s*(savepoint|release|rollback\s+to|begin|commit|rollback)\b", str(sql), re.I)
    if not match:
        return "execute"
    return re.sub(r"\s+", "_", match.group(1).lower())


class _SQLiteCursor(sqlite3.Cursor):
    _threadtrace_statement = ""

    def execute(self, sql, parameters=()):
        self._threadtrace_statement = str(sql)
        return _database_call("sqlite3", _sql_operation(sql), self.connection._threadtrace_database,
                              sql, parameters, super().execute, (sql, parameters), {})

    def executemany(self, sql, parameters):
        self._threadtrace_statement = str(sql)
        return _database_call("sqlite3", "executemany", self.connection._threadtrace_database,
                              sql, parameters, super().executemany, (sql, parameters), {})

    def executescript(self, sql_script):
        self._threadtrace_statement = str(sql_script)
        return _database_call("sqlite3", "executescript", self.connection._threadtrace_database,
                              sql_script, None, super().executescript, (sql_script,), {})

    def fetchone(self):
        return _database_fetch(self, "fetchone", super().fetchone)

    def fetchmany(self, size=None):
        args = () if size is None else (size,)
        return _database_fetch(self, "fetchmany", super().fetchmany, args)

    def fetchall(self):
        return _database_fetch(self, "fetchall", super().fetchall)

    def __next__(self):
        return _database_fetch(self, "fetchone", super().__next__)


class _SQLiteConnection(sqlite3.Connection):
    _threadtrace_database = ""

    def cursor(self, factory=_SQLiteCursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script):
        return self.cursor().executescript(sql_script)

    def commit(self):
        return _database_call("sqlite3", "commit", self._threadtrace_database, None, None,
                              super().commit, (), {})

    def rollback(self):
        return _database_call("sqlite3", "rollback", self._threadtrace_database, None, None,
                              super().rollback, (), {})

    def __exit__(self, exception_type, exception, traceback):
        operation = "rollback" if exception_type else "commit"
        return _database_call("sqlite3", operation, self._threadtrace_database, None, None,
                              super().__exit__, (exception_type, exception, traceback), {})


_sqlite_connect = sqlite3.connect


@functools.wraps(_sqlite_connect)
def _connect(*args, **kwargs):
    if kwargs.get("factory") is not None or len(args) >= 6:
        return _sqlite_connect(*args, **kwargs)
    kwargs["factory"] = _SQLiteConnection
    connection = _sqlite_connect(*args, **kwargs)
    database = args[0] if args else kwargs.get("database", "")
    connection._threadtrace_database = str(database)
    return connection


def _patch_database_frameworks():
    sqlalchemy = sys.modules.get("sqlalchemy.engine.base")
    if sqlalchemy and _supported("sqlalchemy"):
        for name in ("execute", "exec_driver_sql"):
            def factory(original, operation=name):
                @functools.wraps(original)
                def wrapped(connection, statement, *args, **kwargs):
                    parameters = args[0] if args else kwargs.get("parameters")
                    database = getattr(getattr(connection, "engine", None), "url", "")
                    return _database_call("sqlalchemy", operation, str(database), statement, parameters,
                                          original, (connection, statement, *args), kwargs)
                return wrapped
            _patch_method(getattr(sqlalchemy, "Connection", None), name, factory)
        for name in ("begin", "begin_nested", "commit", "rollback"):
            def factory(original, operation=name):
                @functools.wraps(original)
                def wrapped(connection, *args, **kwargs):
                    database = getattr(getattr(connection, "engine", None), "url", "")
                    return _database_call("sqlalchemy", operation, str(database), None, None,
                                          original, (connection, *args), kwargs)
                return wrapped
            _patch_method(getattr(sqlalchemy, "Connection", None), name, factory)
    django = sys.modules.get("django.db.backends.utils")
    if django and _supported("django"):
        for name in ("execute", "executemany"):
            def factory(original, operation=name):
                @functools.wraps(original)
                def wrapped(cursor, sql, params=None):
                    alias = getattr(getattr(cursor, "db", None), "alias", "default")
                    return _database_call("django", operation, alias, sql, params,
                                          original, (cursor, sql, params), {})
                return wrapped
            _patch_method(getattr(django, "CursorWrapper", None), name, factory)
    django_base = sys.modules.get("django.db.backends.base.base")
    if django_base and _supported("django"):
        for name in ("commit", "rollback"):
            def factory(original, operation=name):
                @functools.wraps(original)
                def wrapped(database, *args, **kwargs):
                    alias = getattr(database, "alias", "default")
                    return _database_call("django", operation, alias, None, None,
                                          original, (database, *args), kwargs)
                return wrapped
            _patch_method(getattr(django_base, "BaseDatabaseWrapper", None), name, factory)


class _ObservedFile:
    def __init__(self, handle, path, mode, origin):
        self._handle = handle
        self._path = _path(path)
        self._mode = mode
        self._origin = origin

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *args):
        return self._handle.__exit__(*args)

    def __iter__(self):
        return self

    def __next__(self):
        started = time.monotonic()
        try:
            value = next(self._handle)
            details = {"path": self._path, "mode": self._mode}
            if _level == "content":
                details["content"] = _clean(value, "content")
            _publish("filesystem", "read", started, details, origin=self._origin)
            return value
        except StopIteration:
            raise

    def read(self, *args, **kwargs):
        return self._read("read", self._handle.read, args, kwargs)

    def readline(self, *args, **kwargs):
        return self._read("readline", self._handle.readline, args, kwargs)

    def readlines(self, *args, **kwargs):
        return self._read("readlines", self._handle.readlines, args, kwargs)

    def _read(self, operation, function, args, kwargs):
        started = time.monotonic()
        try:
            value = function(*args, **kwargs)
            details = {"path": self._path, "mode": self._mode}
            if _level == "content":
                details["content"] = _clean(value, "content")
            _publish("filesystem", operation, started, details, origin=self._origin)
            return value
        except BaseException as error:
            _publish("filesystem", operation, started, {"path": self._path, "mode": self._mode}, error, self._origin)
            raise

    def write(self, value):
        return self._write("write", self._handle.write, value)

    def writelines(self, values):
        material = list(values)
        return self._write("writelines", self._handle.writelines, material)

    def _write(self, operation, function, value):
        started = time.monotonic()
        details = {"path": self._path, "mode": self._mode}
        if _level == "content":
            details["content"] = _clean(value, "content")
        try:
            result = function(value)
            details["written"] = result
            _publish("filesystem", operation, started, details, origin=self._origin)
            return result
        except BaseException as error:
            _publish("filesystem", operation, started, details, error, self._origin)
            raise


def _open(function, file, mode="r", *args, **kwargs):
    origin = _origin()
    started = time.monotonic()
    creates = any(flag in mode for flag in "wax")
    try:
        existed = bool(_original_stat(file))
    except (OSError, TypeError, ValueError):
        existed = False
    try:
        handle = function(file, mode, *args, **kwargs)
        if origin is None:
            return handle
        details = {"path": _path(file), "mode": mode, "encoding": kwargs.get("encoding")}
        if creates and not existed:
            _publish("filesystem", "create", started, details, origin=origin)
        _publish("filesystem", "open", started, details, origin=origin)
        return _ObservedFile(handle, file, mode, origin)
    except BaseException as error:
        if origin is not None:
            _publish("filesystem", "open", started, {"path": _path(file), "mode": mode}, error, origin)
        raise


def _builtin_open(file, mode="r", *args, **kwargs):
    return _open(_original_open, file, mode, *args, **kwargs)


def _io_open(file, mode="r", *args, **kwargs):
    return _open(_original_io_open, file, mode, *args, **kwargs)


def _audit(event, args):
    operations = {
        "os.remove": "delete", "os.rmdir": "delete", "os.rename": "rename",
        "os.mkdir": "create", "os.chmod": "permission", "os.symlink": "create",
        "os.link": "create", "os.truncate": "truncate",
    }
    if event == "import" and len(args) > 1 and isinstance(args[1], (str, bytes, os.PathLike)):
        origin = _origin()
        if origin is not None:
            correlation_id = _operation_correlation.get() or _new_correlation_id()
            _publish("filesystem", "import", time.monotonic(),
                     {"module": str(args[0]), "path": _path(args[1]), "audit_event": event},
                     origin=origin, status="attempted", correlation_id=correlation_id)
        return
    operation = operations.get(event)
    if not operation:
        return
    origin = _origin()
    if origin is None:
        return
    paths = [_path(value) for value in args[:2] if isinstance(value, (str, bytes, os.PathLike))]
    correlation_id = _operation_correlation.get() or _new_correlation_id()
    _publish("filesystem", operation, time.monotonic(), {"audit_event": event, "paths": paths},
             origin=origin, status="attempted", correlation_id=correlation_id)


def _new_correlation_id():
    return f"{os.getpid()}:{threading.get_ident()}:{next(_correlation_ids)}"


def _filesystem_operation(original, operation, action, path_names):
    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        origin = _origin()
        started = time.monotonic()
        correlation_id = _new_correlation_id()
        token = _operation_correlation.set(correlation_id)
        values = [args[index] if len(args) > index else kwargs.get(name)
                  for index, name in enumerate(path_names)]
        details = {
            "action": action,
            "paths": [_path(value) for value in values if isinstance(value, (str, bytes, os.PathLike))],
        }
        try:
            result = original(*args, **kwargs)
            if origin is not None:
                _publish("filesystem", operation, started, details, origin=origin,
                         status="completed", correlation_id=correlation_id)
            return result
        except BaseException as error:
            if origin is not None:
                _publish("filesystem", operation, started, details, error, origin,
                         correlation_id=correlation_id)
            raise
        finally:
            _operation_correlation.reset(token)
    return wrapped


def _stat(path, *args, **kwargs):
    if _through_importlib():
        return _original_stat(path, *args, **kwargs)
    origin = _origin()
    started = time.monotonic()
    try:
        result = _original_stat(path, *args, **kwargs)
        if origin is not None:
            _publish("filesystem", "stat", started, {
                "path": _path(path), "size": result.st_size, "mode": result.st_mode,
                "modified_ns": result.st_mtime_ns,
            }, origin=origin)
        return result
    except BaseException as error:
        if origin is not None:
            _publish("filesystem", "stat", started, {"path": _path(path)}, error, origin)
        raise


def _access(path, mode, *args, **kwargs):
    if _through_importlib():
        return _original_access(path, mode, *args, **kwargs)
    origin = _origin()
    started = time.monotonic()
    result = _original_access(path, mode, *args, **kwargs)
    if origin is not None:
        _publish("filesystem", "permission", started,
                 {"path": _path(path), "mode": mode, "allowed": result}, origin=origin)
    return result


def _os_open(path, flags, mode=0o777, *, dir_fd=None):
    origin = _origin()
    started = time.monotonic()
    details = {"path": _path(path), "flags": flags, "mode": mode}
    try:
        existed = bool(_original_stat(path))
    except (OSError, TypeError, ValueError):
        existed = False
    try:
        if dir_fd is None:
            descriptor = _original_os_open(path, flags, mode)
        else:
            descriptor = _original_os_open(path, flags, mode, dir_fd=dir_fd)
        with _file_descriptors_lock:
            _file_descriptors[descriptor] = details["path"]
        if origin is not None:
            if flags & os.O_CREAT and not existed:
                _publish("filesystem", "create", started, details, origin=origin)
            _publish("filesystem", "open", started, details, origin=origin)
        return descriptor
    except BaseException as error:
        if origin is not None:
            _publish("filesystem", "open", started, details, error, origin)
        raise


def _os_read(descriptor, length):
    origin = _origin()
    started = time.monotonic()
    with _file_descriptors_lock:
        path = _file_descriptors.get(descriptor, f"<fd:{descriptor}>")
    try:
        result = _original_os_read(descriptor, length)
        details = {"path": path, "requested": length, "read": len(result)}
        if _level == "content":
            details["content"] = _clean(result, "content")
        _publish("filesystem", "read", started, details, origin=origin)
        return result
    except BaseException as error:
        _publish("filesystem", "read", started, {"path": path, "requested": length}, error, origin)
        raise


def _os_write(descriptor, data):
    origin = _origin()
    started = time.monotonic()
    with _file_descriptors_lock:
        path = _file_descriptors.get(descriptor, f"<fd:{descriptor}>")
    details = {"path": path, "requested": len(data)}
    if _level == "content":
        details["content"] = _clean(data, "content")
    try:
        result = _original_os_write(descriptor, data)
        details["written"] = result
        _publish("filesystem", "write", started, details, origin=origin)
        return result
    except BaseException as error:
        _publish("filesystem", "write", started, details, error, origin)
        raise


def _os_close(descriptor):
    with _file_descriptors_lock:
        _file_descriptors.pop(descriptor, None)
    return _original_os_close(descriptor)


def _serialization_wrapper(format_name, operation, original):
    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        origin = _origin()
        started = time.monotonic()
        details = {"format": format_name}
        if _level == "content" and args:
            if operation == "serialize":
                details["input"] = _clean(args[0], "input")
            elif isinstance(args[0], (str, bytes)):
                details["input_size"] = len(args[0])
        try:
            result = original(*args, **kwargs)
            if isinstance(result, (str, bytes)):
                details["output_size"] = len(result)
            if _level == "content" and operation == "deserialize":
                details["output"] = _clean(result, "output")
            _publish("serialization", operation, started, details, origin=origin)
            return result
        except BaseException as error:
            _publish("serialization", operation, started, details, error, origin)
            raise
    wrapped._threadtrace_adapter = True
    return wrapped


def _patch_serialization():
    for module, format_name in ((json, "json"), (pickle, "pickle")):
        for name, operation in (("dump", "serialize"), ("dumps", "serialize"),
                                ("load", "deserialize"), ("loads", "deserialize")):
            original = getattr(module, name)
            if not getattr(original, "_threadtrace_adapter", False):
                setattr(module, name, _serialization_wrapper(format_name, operation, original))


_original_import = builtins.__import__


@functools.wraps(_original_import)
def _import(name, globals=None, locals=None, fromlist=(), level=0):
    depth = _import_depth.get()
    depth_token = _import_depth.set(depth + 1)
    origin = _origin() if depth == 0 else None
    correlation_id = _new_correlation_id()
    correlation_token = _operation_correlation.set(correlation_id)
    started = time.monotonic()
    details = {"module": str(name), "fromlist": _clean(fromlist, "fromlist"), "level": level}
    try:
        module = _original_import(name, globals, locals, fromlist, level)
        if name.split(".", 1)[0] in {"requests", "httpx", "aiohttp", "urllib3", "sqlalchemy", "django", "ftplib"}:
            try:
                _patch_http()
                _patch_database_frameworks()
                _patch_ftp()
            except (AttributeError, TypeError):
                pass
        if depth == 0 and origin is not None:
            _publish("filesystem", "import", started, details, origin=origin,
                     status="completed", correlation_id=correlation_id)
        return module
    except BaseException as error:
        if depth == 0 and origin is not None:
            _publish("filesystem", "import", started, details, error, origin,
                     correlation_id=correlation_id)
        raise
    finally:
        _operation_correlation.reset(correlation_token)
        _import_depth.reset(depth_token)


def install():
    """Install observational wrappers without importing optional libraries."""
    if _level == "off":
        return
    sqlite3.connect = _connect
    builtins.open = _builtin_open
    io.open = _io_open
    os.stat = _stat
    os.access = _access
    os.open = _os_open
    os.read = _os_read
    os.write = _os_write
    os.close = _os_close
    os.remove = _filesystem_operation(_original_remove, "delete", "remove", ("path",))
    os.unlink = _filesystem_operation(_original_unlink, "delete", "unlink", ("path",))
    os.rmdir = _filesystem_operation(_original_rmdir, "delete", "rmdir", ("path",))
    os.rename = _filesystem_operation(_original_rename, "rename", "rename", ("src", "dst"))
    os.replace = _filesystem_operation(_original_replace, "rename", "replace", ("src", "dst"))
    os.mkdir = _filesystem_operation(_original_mkdir, "create", "mkdir", ("path",))
    os.chmod = _filesystem_operation(_original_chmod, "permission", "chmod", ("path",))
    os.symlink = _filesystem_operation(_original_symlink, "create", "symlink", ("src", "dst"))
    os.link = _filesystem_operation(_original_link, "create", "link", ("src", "dst"))
    os.truncate = _filesystem_operation(_original_truncate, "truncate", "truncate", ("path",))
    builtins.__import__ = _import
    _patch_http()
    _patch_network()
    _patch_ftp()
    _patch_serialization()
    sys.addaudithook(_audit)


install()
