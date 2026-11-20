"""Read-only loopback browser companion for stored ThreadTrace results."""

from __future__ import annotations

import json
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .capabilities import capability_report
from .query import QueryService


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ThreadTrace</title><style>
:root{color-scheme:dark light;font-family:system-ui,sans-serif}body{margin:auto;max-width:72rem;padding:2rem;line-height:1.45}h1{margin-bottom:.1rem}.muted{opacity:.72}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(18rem,1fr));gap:1rem}.card{border:1px solid #8886;border-radius:.5rem;padding:1rem}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:.45rem;border-bottom:1px solid #8886;vertical-align:top}code{word-break:break-word}
</style></head><body><h1>ThreadTrace</h1><p class="muted">Read-only local view of stored analysis results.</p><main id="app">Loading…</main>
<script>
const text=value=>document.createTextNode(value ?? "—");
const card=(title,body)=>{const item=document.createElement("section");item.className="card";const heading=document.createElement("h2");heading.textContent=title;item.append(heading,body);return item};
const table=(rows,fields)=>{const value=document.createElement("table"),head=value.insertRow();fields.forEach(([key,label])=>{const cell=document.createElement("th");cell.textContent=label;head.append(cell)});rows.forEach(row=>{const line=value.insertRow();fields.forEach(([key])=>{const cell=line.insertCell();cell.append(text(typeof row[key]==="object"?JSON.stringify(row[key]):String(row[key]??"—")))})});return value};
Promise.all(["overview","architecture","capabilities"].map(name=>fetch("/api/"+name).then(response=>response.json()))).then(([overview,architecture,capabilities])=>{const app=document.querySelector("#app"),grid=document.createElement("div");grid.className="grid";grid.append(card("Project",document.createTextNode(overview.project.name+" — "+overview.purpose)),card("Next starting point",document.createTextNode(overview.next_step||"No supported entry point found.")));app.replaceChildren(grid,card("Starting points",table(overview.starting_points,[["kind","Kind"],["name","Name"],["plain_summary","Summary"]])),card("Main parts",table(overview.main_parts,[["name","Name"],["role","Role"],["confidence","Evidence"]])),card("Capability status",table(capabilities,[["area","Area"],["feature","Feature"],["status","Status"],["details","Details"]])),card("Architecture relationships",table(architecture.relationships,[["kind","Kind"],["source_name","From"],["target_name","To"],["confidence","Evidence"]])))}).catch(error=>document.querySelector("#app").textContent="Could not load this local analysis: "+error);
</script></body></html>"""


def is_loopback_host(host: str) -> bool:
    """Return whether a command-line address is an allowed browser binding."""
    return host in {"127.0.0.1", "::1"}


def _payload(queries: QueryService, project: str, name: str) -> Any:
    if name == "overview":
        overview = queries.overview(project)
        value = {
            "purpose": overview["purpose"],
            "starting_points": [item for item in queries.entrypoints(project) if item["kind"] != "test"],
            "main_parts": overview["components"],
            "next_step": None,
        }
        if value["starting_points"]:
            value["next_step"] = value["starting_points"][0]["name"]
        value["project"] = queries.engine.get_project(project)
        return value
    if name == "architecture":
        return queries.architecture(project)
    if name == "capabilities":
        return capability_report(project=project)
    raise KeyError(name)


class BrowserServer(HTTPServer):
    """A single-threaded server restricted to a chosen loopback address."""

    allow_reuse_address = True

    def __init__(self, host: str, port: int, queries: QueryService, project: str) -> None:
        if not is_loopback_host(host):
            raise ValueError("Browser views may bind only to 127.0.0.1 or ::1.")
        if host == "::1":
            self.address_family = socket.AF_INET6
        self.queries = queries
        self.project = project
        super().__init__((host, port), BrowserRequestHandler)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/"


class BrowserRequestHandler(BaseHTTPRequestHandler):
    server: BrowserServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        if self.path == "/":
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", _PAGE.encode())
            return
        if self.path.startswith("/api/"):
            name = self.path.removeprefix("/api/")
            if "?" in name or name not in {"overview", "architecture", "capabilities"}:
                self._send_json_error(HTTPStatus.NOT_FOUND, "Unknown local analysis endpoint.")
                return
            try:
                body = json.dumps(_payload(self.server.queries, self.server.project, name), default=str).encode()
            except Exception:
                self._send_json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not build local analysis response.")
                return
            self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _send_json_error(self, status: HTTPStatus, message: str) -> None:
        body = json.dumps({"error": message, "status": int(status)}).encode()
        self._send(status, "application/json; charset=utf-8", body)
