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

For v1.0, browser views are read-only. Browser endpoints bind only to loopback (`127.0.0.1`/`::1` as supported), choose an available local port by default, and may expose a configured port. The local server is deliberately single-threaded and handles one request at a time so its SQLite connection stays on its creating thread. API failures return bounded JSON without internal exception details. Mermaid is a required export/output format.

## Implemented v1 graph explorer

The companion serves the latest completed scan through a shared graph model. Required families are dependency/import, call, class/module, AST, data-flow, control-flow, and evidence-supported execution flow. Each family is available as a paginated JSON page and as Mermaid generated from the same filtered nodes and edges; edges with unknown endpoints are dropped and reported rather than rendered.

The page provides pan, wheel zoom, label search/filtering, relationship-type and evidence-class filters, node focus, depth-bounded hop expansion, bounded source windows inside the registered project, and Mermaid export. Large graphs load progressively with `next_offset` rather than a hard size cutoff. Selected nodes and edges show evidence class, source location, capability status, graph truncation, and staleness.

Only GET requests from the bound loopback `Host` and a same-origin `Origin` are accepted; write methods are rejected. Responses set no-store/CSP/nosniff/frame-deny headers, and project strings are rendered with DOM text nodes rather than injected HTML. Local-only operation and further access controls remain governed by [[Safety and Performance]].

## Initial views

The companion renders the outputs of [[Shared Systems#Visualisation|the visualisation system]] for the graph families above. Architecture views, timelines, coverage, and experiment comparisons remain read-only companion work; only static graph families and stored entry-point execution flow are implemented in this step. Local-only operation and access controls are governed by [[Safety and Performance]].
