# Browser Interface

> [!summary]
> A local visualisation companion, not a hosted website.

## Delivery progression

1. Introduce a loopback-only local server for read-only live visualisations in v1.0.
2. Generate self-contained HTML visualisation reports.
3. Optionally wrap the same interface in a native webview.

> [!decision]
> v1.0 includes a local browser server. It binds only to a loopback address and
> prints its URL for the user to open in their browser of choice. A native
> webview remains later work.

For v1.0, browser views are read-only. Browser endpoints bind only to loopback (`127.0.0.1`/`::1` as supported), choose an available local port by default, and may expose a configured port. The local server is deliberately single-threaded and handles one request at a time so its SQLite connection stays on its creating thread. API failures return bounded JSON without internal exception details. Mermaid is a required export/output format. Whether reports/server delivery are v1.0 or later remains in [[Outstanding Decisions]].

## Initial views

The companion should render outputs from [[Shared Systems#Visualisation|the visualisation system]], including dependency/call/inheritance/control/data-flow graphs, architecture views, timelines, coverage, and experiment comparisons. Local-only operation and access controls are governed by [[Safety and Performance]].
