"""Command-line interface and full-screen terminal explorer entry point."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .engine import Engine, ThreadError
from .query import QueryService
from .store import Store


def _json_default(value: Any) -> str:
    return str(value)


def render(value: Any, output_format: str) -> None:
    if output_format == "markdown":
        print(_markdown(value))
        return
    if output_format == "json":
        print(json.dumps(value, indent=2, default=_json_default))
        return
    print(_text_report(value))


def _label(key: str) -> str:
    return key.replace("_", " ").strip().title()


def _display(value: Any, limit: int = 56) -> str:
    """Render a field for people, reserving JSON for lossless automation."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple, set)):
        if not value:
            return "—"
        if all(not isinstance(item, (dict, list, tuple, set)) for item in value):
            return _truncate(", ".join(map(str, value)), limit)
        return f"{len(value)} item(s)"
    if isinstance(value, dict):
        return f"{len(value)} field(s)"
    return _truncate(str(value).replace("\n", " "), limit)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _table(rows: list[Any]) -> list[str]:
    if not rows:
        return ["  No results."]
    if not all(isinstance(row, dict) for row in rows):
        return [f"  {index}. {_display(row, 120)}" for index, row in enumerate(rows, 1)]
    records = [dict(row) for row in rows]
    priorities = (
        "name", "qualified_name", "kind", "role", "status", "relative_path", "module_name",
        "line_start", "line_number", "confidence", "severity", "operation", "description",
        "purpose", "message", "created_at", "completed_at", "started_at", "finished_at",
        "scan_id", "trace_id", "project_id",
    )
    available = [
        key for key in priorities
        if key != "project_id" and any(row.get(key) not in (None, "", [], {}) for row in records)
    ]
    # Preserve useful, command-specific scalar fields that are not in the
    # shared priority list, but never flood a terminal with database blobs.
    for key in records[0]:
        if key.endswith("_json") or key in {"project_id", "scan_id", "trace_id"}:
            continue
        if key not in available and any(not isinstance(row.get(key), (dict, list)) and row.get(key) not in (None, "") for row in records):
            available.append(key)
    columns = available[:6] or list(records[0])[:4]
    terminal = shutil.get_terminal_size(fallback=(110, 24)).columns
    maximum = max(12, min(36, (terminal - (3 * max(0, len(columns) - 1))) // max(1, len(columns))))
    values = [[_display(row.get(column), maximum) for column in columns] for row in records]
    widths = [min(maximum, max(len(_label(column)), *(len(row[index]) for row in values))) for index, column in enumerate(columns)]
    header = "  ".join(_label(column).ljust(widths[index]) for index, column in enumerate(columns))
    rule = "  ".join("-" * width for width in widths)
    body = ["  ".join(row[index].ljust(widths[index]) for index in range(len(columns))) for row in values]
    return [header, rule, *body]


def _text_report(value: Any, heading: str | None = None) -> str:
    """Readable default output for interactive use; JSON remains the API."""
    if isinstance(value, list):
        return "\n".join(([heading, ""] if heading else []) + _table(value))
    if not isinstance(value, dict):
        return str(value)
    # Project show is common enough to deserve a calm, task-oriented summary.
    if {"name", "root_path", "project_id"}.issubset(value) and "scans" in value:
        lines = [f"Project: {value['name']}", "=" * (9 + len(str(value['name']))), ""]
        fields = (("Location", value["root_path"]), ("Project ID", value["project_id"]), ("Registered", value.get("created_at")))
        lines.extend(f"{label:<12} {item}" for label, item in fields if item not in (None, ""))
        lines.extend(["", f"Scans ({len(value['scans'])})", "-" * 18, *_table(value["scans"])])
        return "\n".join(lines)
    scalars = [(key, item) for key, item in value.items() if not isinstance(item, (dict, list)) and item not in (None, "")]
    nested = [(key, item) for key, item in value.items() if isinstance(item, (dict, list))]
    lines: list[str] = []
    if heading:
        lines.extend([heading, "=" * len(heading), ""])
    if scalars:
        width = max(len(_label(key)) for key, _ in scalars)
        lines.extend(f"{_label(key):<{width}}  {_display(item, 120)}" for key, item in scalars)
    for key, item in nested:
        if lines:
            lines.append("")
        lines.extend([_label(key), "-" * len(_label(key))])
        if isinstance(item, list):
            lines.extend(_table(item))
        else:
            lines.append(_text_report(item))
    return "\n".join(lines) or "No results."


def _markdown(value: Any, heading: str = "Thread report") -> str:
    """Small dependency-free Markdown renderer for automation and hand-off."""
    lines = [f"# {heading}", ""]
    if isinstance(value, dict):
        for key, item in value.items():
            title = key.replace("_", " ").title()
            if isinstance(item, list):
                lines.extend([f"## {title}", ""])
                if not item:
                    lines.append("No results.")
                for row in item:
                    if isinstance(row, dict):
                        lines.append("- " + "; ".join(f"{name}: {_short(data)}" for name, data in row.items() if data not in (None, "", [], {})))
                    else:
                        lines.append(f"- {row}")
                lines.append("")
            elif isinstance(item, dict):
                lines.extend([f"## {title}", "", "```json", json.dumps(item, indent=2, default=_json_default), "```"])
            elif item not in (None, ""):
                lines.append(f"**{title}:** {item}")
    elif isinstance(value, list):
        for row in value:
            lines.append(f"- {_short(row)}")
    else:
        lines.append(str(value))
    return "\n".join(lines)


def _graph_export(value: dict[str, Any], output_format: str) -> str:
    components = value.get("components", [])
    relationships = value.get("relationships", [])
    names = {item["name"] for item in components}
    if output_format == "mermaid":
        lines = ["flowchart LR"]
        ids = {component["name"]: f"n{index}" for index, component in enumerate(components, 1)}
        for component in components:
            identifier = ids[component["name"]]
            lines.append(f'  {identifier}["{component["name"]}\\n{component["role"]}"]')
        for edge in relationships:
            source, target = edge.get("source_module"), edge.get("target_module")
            if source in ids and target in ids:
                lines.append(f"  {ids[source]} -->|{edge['kind']}| {ids[target]}")
        return "\n".join(lines)
    lines = ["digraph thread {", "  rankdir=LR;"]
    for component in components:
        lines.append(f'  "{component["name"]}" [label="{component["name"]}\\n{component["role"]}"];')
    for edge in relationships:
        source, target = edge.get("source_module"), edge.get("target_module")
        if source in names and target in names:
            lines.append(f'  "{source}" -> "{target}" [label="{edge["kind"]}"];')
    lines.append("}")
    return "\n".join(lines)


def _short(value: Any, limit: int = 180) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value)
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", metavar="PATH", help="Override the local SQLite database path.")
    parser.add_argument("--format", choices=("text", "json", "markdown", "mermaid", "dot"), default="text", help="Output format (default: text).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threadtrace", description="Explore Python project structure without executing it.")
    parser.add_argument("--version", action="version", version=f"threadtrace {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    project = commands.add_parser("project", help="Register and manage projects.")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    add = project_commands.add_parser("add", help="Register a Python project.")
    add.add_argument("path")
    add.add_argument("--name")
    _common(add)
    listing = project_commands.add_parser("list", help="List registered projects.")
    _common(listing)
    show = project_commands.add_parser("show", help="Show a registered project.")
    show.add_argument("project")
    _common(show)
    remove = project_commands.add_parser("remove", help="Remove project registration and stored scans.")
    remove.add_argument("project")
    remove.add_argument("--yes", action="store_true", help="Confirm removal. Source files are never deleted.")
    _common(remove)

    scan = commands.add_parser("scan", help="Analyse a project explicitly.")
    scan.add_argument("project")
    _common(scan)
    history = commands.add_parser("scan-history", help="List scans for a project.")
    history.add_argument("project")
    _common(history)

    overview = commands.add_parser("overview", help="Summarise a project as evidence-backed components and entry points.")
    overview.add_argument("project")
    _common(overview)
    guide = commands.add_parser("guide", help="Start a beginner-friendly, evidence-backed project tour.")
    guide.add_argument("project")
    _common(guide)
    entrypoints = commands.add_parser("entrypoints", help="List detected scripts, commands, routes, workers, and tests.")
    entrypoints.add_argument("project")
    entrypoints.add_argument("--kind", choices=("script", "module", "console", "argparse", "cli", "route", "worker", "test"))
    _common(entrypoints)
    architecture = commands.add_parser("architecture", help="Show evidence-backed components and their relationships.")
    architecture.add_argument("project")
    _common(architecture)
    path = commands.add_parser("path", help="Follow a detected entry point through static calls.")
    path.add_argument("project")
    path.add_argument("entrypoint")
    path.add_argument("--depth", type=int, default=8)
    path.add_argument("--confidence", choices=("confirmed", "high", "possible", "unknown"), default="possible")
    _common(path)
    walk = commands.add_parser("walk", help="Explain one supported entry-point path in beginner-friendly language.")
    walk.add_argument("project")
    walk.add_argument("entrypoint")
    walk.add_argument("--depth", type=int, default=8)
    walk.add_argument("--confidence", choices=("confirmed", "high", "possible", "unknown"), default="possible")
    _common(walk)
    file_map = commands.add_parser("map", help="Show files linked to an entry point, module, or symbol.")
    file_map.add_argument("project")
    file_map.add_argument("target", nargs="?", help="Optional entry point, qualified symbol, or module to focus on.")
    file_map.add_argument("--depth", type=int, default=4)
    _common(file_map)
    impact = commands.add_parser("impact", help="Show statically reachable downstream symbols.")
    impact.add_argument("project")
    impact.add_argument("symbol")
    impact.add_argument("--depth", type=int, default=3)
    _common(impact)
    dataflow = commands.add_parser("dataflow", help="Show bounded direct-call argument and return flow.")
    dataflow.add_argument("project")
    dataflow.add_argument("symbol")
    dataflow.add_argument("--to", dest="resource_kind", choices=("database", "http", "file", "queue", "env", "subprocess"))
    _common(dataflow)
    resources = commands.add_parser("resources", help="List detected external resource boundaries.")
    resources.add_argument("project")
    resources.add_argument("--kind", choices=("database", "http", "file", "queue", "env", "subprocess"))
    _common(resources)
    explain = commands.add_parser("explain", help="Explain an entry point, symbol, or project overview from stored evidence.")
    explain.add_argument("project")
    explain.add_argument("target")
    explain.add_argument("--ai", action="store_true", help="Optionally generate an evidence-only narrative using local Ollama.")
    explain.add_argument("--ai-model", help="Ollama model name (default: THREADTRACE_OLLAMA_MODEL or llama3.2).")
    explain.add_argument("--ai-endpoint", help="Override the local Ollama endpoint.")
    _common(explain)
    lesson = commands.add_parser("lesson", help="Create a deterministic teaching card for one function, method, or class.")
    lesson.add_argument("project")
    lesson.add_argument("symbol")
    _common(lesson)
    compare = commands.add_parser("compare", help="Compare reconstruction facts from two completed scans.")
    compare.add_argument("project")
    compare.add_argument("scan_a")
    compare.add_argument("scan_b")
    _common(compare)
    trace = commands.add_parser("trace", help="Explicitly run a user-supplied command with bounded Python call tracing.")
    trace.add_argument("project")
    trace.add_argument("--yes-run", action="store_true", help="Confirm that this command may execute target-project code.")
    trace.add_argument("--timeout", type=int, default=30, help="Kill the command after this many seconds (default: 30).")
    trace.add_argument("--output-cap", type=int, default=100_000, help="Maximum stdout and stderr characters to retain.")
    trace.add_argument("run_command", nargs="+", help="Command to run, supplied after '--'.")
    _common(trace)
    runtime = commands.add_parser("runtime", help="Open an interactive, step-by-step dashboard for one user-supplied command.")
    runtime.add_argument("project")
    runtime.add_argument("--timeout", type=int, default=30, help="Stop the runtime subprocess after this many seconds (default: 30).")
    runtime.add_argument("--output-cap", type=int, default=100_000, help="Maximum stdout and stderr characters to retain.")
    _common(runtime)
    runtime.add_argument("run_command", nargs="+", help="Command to run, supplied after '--'. The dashboard waits for Start before executing it.")
    runtime_host = commands.add_parser("runtime-host", help="Run an interactive Python program in this terminal while publishing safe live events.")
    runtime_host.add_argument("project")
    runtime_host.add_argument("--event-cap", type=int, default=100_000, help="Maximum project runtime events to publish (default: 100000).")
    _common(runtime_host)
    runtime_host.add_argument("run_command", nargs="+", help="Interactive command to run, supplied after '--'. It keeps this terminal.")
    runtime_listen = commands.add_parser("runtime-listen", help="Open a read-only dashboard for a Runtime Host session in another terminal.")
    runtime_listen.add_argument("project")
    runtime_listen.add_argument("--session", help="Specific live session ID; defaults to the newest session for the project.")
    _common(runtime_listen)
    processes = commands.add_parser("processes", help="List current-user Python processes that Thread can inspect safely.")
    _common(processes)
    process = commands.add_parser("process", help="Select a running Python process, then scan it or explicitly restart it under Runtime.")
    process.add_argument("pid", type=int, nargs="?", help="Optional PID to preselect in the confirmation screen.")
    process.add_argument("--timeout", type=int, default=30, help="Runtime timeout after a restarted process is continued (default: 30).")
    process.add_argument("--output-cap", type=int, default=100_000, help="Maximum stdout and stderr characters to retain after restart.")
    _common(process)
    traces = commands.add_parser("traces", help="List explicitly captured runtime traces.")
    traces.add_argument("project")
    _common(traces)

    modules = commands.add_parser("modules", help="List project modules.")
    modules.add_argument("project")
    _common(modules)
    deps = commands.add_parser("deps", help="Trace module dependencies.")
    deps.add_argument("project")
    deps.add_argument("module")
    deps.add_argument("--reverse", action="store_true", help="Show modules that depend on the selected module.")
    deps.add_argument("--depth", type=int, default=1)
    _common(deps)
    cycles = commands.add_parser("cycles", help="Show circular internal imports.")
    cycles.add_argument("project")
    _common(cycles)

    search = commands.add_parser("search", help="Search definitions.")
    search.add_argument("project")
    search.add_argument("term")
    search.add_argument("--kind")
    search.add_argument("--module")
    _common(search)
    symbol = commands.add_parser("symbol", help="Show one or more symbol definitions.")
    symbol.add_argument("project")
    symbol.add_argument("symbol")
    _common(symbol)
    callers = commands.add_parser("callers", help="Show statically resolved callers.")
    callers.add_argument("project")
    callers.add_argument("symbol")
    _common(callers)
    callees = commands.add_parser("callees", help="Show calls made by a function or method.")
    callees.add_argument("project")
    callees.add_argument("symbol")
    callees.add_argument("--depth", type=int, default=1)
    _common(callees)

    functions = commands.add_parser("functions", help="List functions and methods with source-derived summaries.")
    functions.add_argument("project")
    functions.add_argument("--module")
    _common(functions)
    classes = commands.add_parser("classes", help="List classes, methods, and attributes.")
    classes.add_argument("project")
    classes.add_argument("--module")
    _common(classes)
    inherit = commands.add_parser("inheritance", help="Show a class and its declared bases.")
    inherit.add_argument("project")
    inherit.add_argument("class_name")
    _common(inherit)
    flow = commands.add_parser("flow", help="Show intra-function parameters, assignments, returns, and yields.")
    flow.add_argument("project")
    flow.add_argument("symbol")
    _common(flow)
    libraries = commands.add_parser("libraries", help="Show imported and declared libraries.")
    libraries.add_argument("project")
    _common(libraries)
    diagnostics = commands.add_parser("diagnostics", help="Show analysis findings.")
    diagnostics.add_argument("project")
    diagnostics.add_argument("--severity", choices=("high", "medium", "low", "info"))
    diagnostics.add_argument("--rule")
    diagnostics.add_argument("--include-unresolved-calls", action="store_true", help="Include informational unresolved call sites.")
    _common(diagnostics)
    findings = commands.add_parser("findings", help="Show important analysis gaps without routine call-resolution noise.")
    findings.add_argument("project")
    findings.add_argument("--details", action="store_true", help="Include all unresolved-call details.")
    _common(findings)
    tui = commands.add_parser("tui", help="Open the full-screen terminal explorer.")
    tui.add_argument("project")
    _common(tui)
    return parser


def _warn_if_stale(queries: QueryService, project: str) -> None:
    if queries.stale(project):
        print("Warning: stored results may not match files on disk. Run 'threadtrace scan' to refresh them.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(getattr(args, "database", None))
    engine = Engine(store)
    queries = QueryService(engine)
    try:
        if args.command == "project":
            if args.project_command == "add":
                value = engine.add_project(args.path, args.name)
            elif args.project_command == "list":
                value = engine.list_projects()
            elif args.project_command == "show":
                value = engine.get_project(args.project)
                value["scans"] = engine.scan_history(args.project)
            else:
                if not args.yes:
                    raise ThreadError("Refusing to remove registration without --yes. Source files are not affected.")
                value = engine.remove_project(args.project)
            render(value, args.format)
            return 0
        if args.command == "scan":
            value = engine.scan(args.project)
            value["summary"] = json.loads(value["summary_json"])
            render(value, args.format)
            return 0
        if args.command == "scan-history":
            render(engine.scan_history(args.project), args.format)
            return 0
        if args.command == "trace":
            if not args.yes_run:
                raise ThreadError("Runtime tracing executes code. Re-run with --yes-run and a command after '--'.")
            if not args.run_command:
                raise ThreadError("Supply a command after '--', for example: threadtrace trace PROJECT --yes-run -- python -m app")
            print("Warning: runtime tracing executes the supplied command. It is not a security sandbox.", file=sys.stderr)
            value = engine.trace(args.project, args.run_command, args.timeout, args.output_cap)
            render(value, args.format)
            return value["exit_code"] if isinstance(value["exit_code"], int) and value["exit_code"] >= 0 else 124
        if args.command == "runtime":
            if not args.run_command:
                raise ThreadError("Supply a start command after '--', for example: threadtrace runtime PROJECT -- python -m app")
            if args.timeout < 1 or args.output_cap < 1:
                raise ThreadError("Runtime timeout and output cap must be positive.")
            try:
                from .runtime_tui import RuntimeApp
            except ModuleNotFoundError as exc:
                raise ThreadError("The runtime dashboard requires prompt_toolkit. Reinstall Thread with: uv pip install -e .") from exc
            print("Warning: Runtime mode can execute the supplied command. The dashboard opens paused; press s or n to start it. It is not a security sandbox.", file=sys.stderr)
            RuntimeApp(engine, args.project, args.run_command, args.timeout, args.output_cap).run()
            return 0
        if args.command == "runtime-host":
            if args.event_cap < 1:
                raise ThreadError("Runtime Host event cap must be positive.")
            project = engine.get_project(args.project)
            from .runtime.host import RuntimeHost
            host = RuntimeHost(Path(project["root_path"]), project["name"], args.run_command, args.event_cap)
            print(f"Runtime Host session {host.session_id} is starting. In another terminal run: threadtrace runtime-listen {project['name']} --session {host.session_id}", file=sys.stderr)
            return host.run()
        if args.command == "runtime-listen":
            try:
                from .live_tui import LiveRuntimeApp
            except ModuleNotFoundError as exc:
                raise ThreadError("The live runtime listener requires prompt_toolkit. Reinstall Thread with: uv pip install -e .") from exc
            LiveRuntimeApp(engine, args.project, args.session).run()
            return 0
        if args.command == "processes":
            from .processes import public_process, running_python_processes
            render([public_process(item) for item in running_python_processes()], args.format)
            return 0
        if args.command == "process":
            if args.timeout < 1 or args.output_cap < 1:
                raise ThreadError("Process runtime timeout and output cap must be positive.")
            try:
                from .process_tui import ProcessApp
            except ModuleNotFoundError as exc:
                raise ThreadError("The process selector requires prompt_toolkit. Reinstall Thread with: uv pip install -e .") from exc
            print("Process discovery is read-only until you confirm Scan or Restart. Restart stops the selected current-user Python process tree before relaunching it.", file=sys.stderr)
            ProcessApp(engine, args.pid, args.timeout, args.output_cap).run()
            return 0
        if args.command == "tui":
            _warn_if_stale(queries, args.project)
            try:
                from .tui import ThreadApp
            except ModuleNotFoundError as exc:
                raise ThreadError("The terminal explorer requires prompt_toolkit. Reinstall Thread with: uv pip install -e .") from exc

            ThreadApp(engine, queries, args.project).run()
            return 0
        _warn_if_stale(queries, args.project)
        handlers: dict[str, Callable[[], Any]] = {
            "overview": lambda: queries.overview(args.project),
            "guide": lambda: queries.guide(args.project),
            "entrypoints": lambda: queries.entrypoints(args.project, args.kind),
            "architecture": lambda: queries.architecture(args.project),
            "path": lambda: queries.path(args.project, args.entrypoint, args.depth, args.confidence),
            "walk": lambda: queries.walk(args.project, args.entrypoint, args.depth, args.confidence),
            "map": lambda: queries.file_map(args.project, args.target, args.depth),
            "impact": lambda: queries.impact(args.project, args.symbol, args.depth),
            "dataflow": lambda: queries.dataflow(args.project, args.symbol, args.resource_kind),
            "resources": lambda: queries.resources(args.project, args.kind),
            "explain": lambda: queries.ai_explain(args.project, args.target, args.ai_model, args.ai_endpoint)
            if args.ai else queries.explain(args.project, args.target),
            "lesson": lambda: queries.lesson(args.project, args.symbol),
            "compare": lambda: queries.compare(args.project, args.scan_a, args.scan_b),
            "traces": lambda: queries.traces(args.project),
            "modules": lambda: queries.modules(args.project),
            "deps": lambda: queries.dependencies(args.project, args.module, args.reverse, args.depth),
            "cycles": lambda: queries.cycles(args.project),
            "search": lambda: queries.search(args.project, args.term, args.kind, args.module),
            "symbol": lambda: queries.symbol(args.project, args.symbol),
            "callers": lambda: queries.callers(args.project, args.symbol),
            "callees": lambda: queries.callees(args.project, args.symbol, args.depth),
            "functions": lambda: queries.functions(args.project, args.module),
            "classes": lambda: queries.classes(args.project, args.module),
            "inheritance": lambda: queries.inheritance(args.project, args.class_name),
            "flow": lambda: queries.flow(args.project, args.symbol),
            "libraries": lambda: queries.libraries(args.project),
            "diagnostics": lambda: queries.diagnostics(args.project, args.severity, args.rule, args.include_unresolved_calls),
            "findings": lambda: queries.findings(args.project, args.details),
        }
        value = handlers[args.command]()
        if args.command == "architecture" and args.format in {"mermaid", "dot"}:
            print(_graph_export(value, args.format))
        else:
            render(value, args.format)
        return 0
    except ThreadError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
