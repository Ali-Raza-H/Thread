"""Command-line interface and full-screen terminal explorer entry point."""

from __future__ import annotations

import argparse
import csv
import html
import json
import shutil
import sqlite3
import sys
from io import StringIO
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import configuration_path, load as load_config, merge as merge_config, write_default as write_default_config
from .engine import Engine, ThreadError
from .exports import header_lines, report_bundle, write_text
from .query import QueryService
from .store import Store


def _json_default(value: Any) -> str:
    return str(value)


def _ai_options(configuration: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Merge configured AI settings with explicit command-line overrides."""
    options = dict(configuration["ai"])
    overrides = {
        "provider": args.ai_provider, "model": args.ai_model, "endpoint": args.ai_endpoint,
        "temperature": args.ai_temperature, "generation_limit": args.ai_tokens,
        "template_path": args.ai_template,
    }
    options.update({key: value for key, value in overrides.items() if value is not None})
    options["parameters"] = list(configuration["ai"].get("parameters") or []) + list(args.ai_parameter or [])
    return options


def _ai_approval(args: argparse.Namespace) -> Callable[[dict[str, Any]], bool]:
    """Approve exactly one remote payload by digest, or interactively after review."""
    def approve(request: dict[str, Any]) -> bool:
        if args.ai_approve and args.ai_approve == request["digest"]:
            print(json.dumps(request["payload"], indent=2, default=_json_default), file=sys.stderr)
            return True
        if not sys.stdin.isatty():
            return False
        print(json.dumps(request["payload"], indent=2, default=_json_default), file=sys.stderr)
        print(f"Send exactly this payload to {request['manifest']['endpoint']}? Type 'approve': ",
              end="", file=sys.stderr, flush=True)
        return sys.stdin.readline().strip().lower() == "approve"
    return approve


def render(value: Any, output_format: str) -> None:
    if output_format == "markdown":
        print(_markdown(value))
        return
    if output_format == "json":
        print(json.dumps(value, indent=2, default=_json_default))
        return
    if output_format == "csv":
        print(_csv(value), end="")
        return
    if output_format == "html":
        print(_html(value))
        return
    print(_text_report(value))


def _profile_environment(assignments: list[str], removals: list[str]) -> dict[str, str | None]:
    environment: dict[str, str | None] = {name: None for name in removals}
    for assignment in assignments:
        name, separator, value = assignment.partition("=")
        if not name or not separator:
            raise ThreadError("--env needs NAME=VALUE.")
        if name in environment:
            raise ThreadError(f"Environment variable '{name}' cannot be both set and removed.")
        environment[name] = value
    return environment


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


def _markdown(value: Any, heading: str = "ThreadTrace report") -> str:
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


def _csv_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, default=_json_default, sort_keys=True)
    return "" if value is None else str(value)


def _csv(value: Any) -> str:
    """Export a list of records, or one report record, without dependencies."""
    rows = value if isinstance(value, list) else [value]
    records = [row if isinstance(row, dict) else {"value": row} for row in rows]
    fields = list(dict.fromkeys(field for row in records for field in row))
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({field: _csv_value(row.get(field)) for field in fields} for row in records)
    return output.getvalue()


def _html(value: Any, title: str = "ThreadTrace report") -> str:
    """Create a self-contained, read-only report suitable for local export."""
    data = html.escape(json.dumps(value, indent=2, default=_json_default))
    return "\n".join((
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{html.escape(title)}</title>",
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.5}pre{white-space:pre-wrap;word-break:break-word}</style>",
        "</head><body>",
        f"<h1>{html.escape(title)}</h1>",
        "<p>Exported from ThreadTrace. Verify evidence and capability status before relying on this report.</p>",
        f"<pre>{data}</pre>",
        "</body></html>",
    ))


def _mermaid_text(value: Any) -> str:
    """Escape a Mermaid label without breaking its quoted syntax."""
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    for character, entity in (("#", "#35;"), ("&", "#38;"), ('"', "#quot;"), ("<", "#lt;"),
                              (">", "#gt;"), ("|", "#124;"), ("`", "#96;")):
        text = text.replace(character, entity)
    return "".join(character for character in text if character >= " ")


def _dot_text(value: Any) -> str:
    """Escape a DOT quoted label, including renderer control characters."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r", "").replace("\n", "\\n").replace("\t", "\\t")
    return "".join(character for character in text if character >= " ")


def _graph_export(value: dict[str, Any], output_format: str) -> str:
    components = value.get("components", [])
    relationships = value.get("relationships", [])
    names = {item["name"] for item in components}
    if output_format == "mermaid":
        lines = ["flowchart LR"]
        ids = {component["name"]: f"n{index}" for index, component in enumerate(components, 1)}
        for component in components:
            identifier = ids[component["name"]]
            lines.append(f'  {identifier}["{_mermaid_text(component["name"])}\\n{_mermaid_text(component["role"])}"]')
        for edge in relationships:
            source, target = edge.get("source_module"), edge.get("target_module")
            if source in ids and target in ids:
                lines.append(f"  {ids[source]} -->|{_mermaid_text(edge['kind'])}| {ids[target]}")
        return "\n".join(lines)
    lines = ["digraph thread {", "  rankdir=LR;"]
    for component in components:
        label = f"{_dot_text(component['name'])}\\n{_dot_text(component['role'])}"
        lines.append(f'  "{_dot_text(component["name"])}" [label="{label}"];')
    for edge in relationships:
        source, target = edge.get("source_module"), edge.get("target_module")
        if source in names and target in names:
            lines.append(f'  "{_dot_text(source)}" -> "{_dot_text(target)}" [label="{_dot_text(edge["kind"])}"];')
    lines.append("}")
    return "\n".join(lines)


def _render_export(bundle: dict[str, Any], output_format: str) -> str:
    """Render one bundle so every supported format carries the same metadata."""
    if output_format == "json":
        return json.dumps(bundle, indent=2, sort_keys=True, default=_json_default)
    if output_format == "markdown":
        return _markdown(bundle, "ThreadTrace export")
    if output_format == "html":
        return _html(bundle, "ThreadTrace export")
    if output_format in {"mermaid", "dot"}:
        prefix = "%%" if output_format == "mermaid" else "//"
        return "\n".join([*header_lines(bundle, prefix), "", _graph_export(bundle["payload"]["architecture"], output_format)])
    records = []
    for section, value in bundle["payload"].items():
        for row in value if isinstance(value, list) else [value]:
            records.append({"section": section, **(row if isinstance(row, dict) else {"value": row})})
    return "\n".join([*header_lines(bundle, "#"), _csv(records)])


def _short(value: Any, limit: int = 180) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value)
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


ALL_FORMATS = ("text", "json", "markdown", "csv", "html", "mermaid", "dot")
STANDARD_FORMATS = {"text", "json", "markdown", "csv", "html"}
_COMMAND_FORMATS: dict[tuple[str, str | None] | str, set[str]] = {
    "architecture": STANDARD_FORMATS | {"mermaid", "dot"},
    "export": {"json", "markdown", "csv", "html", "mermaid", "dot"},
    ("experiment", "diff"): {"text", "json"},
    "runtime": {"text"},
    "runtime-host": {"text"},
    "runtime-listen": {"text"},
    "process": {"text"},
    "tui": {"text"},
    "browser": {"text"},
}


def _format_key(args: argparse.Namespace) -> tuple[str, str | None]:
    for attribute in ("project_command", "profile_command", "investigation_command", "experiment_command", "config_command"):
        subcommand = getattr(args, attribute, None)
        if subcommand:
            return args.command, subcommand
    return args.command, None


def _formats(command: str, subcommand: str | None = None) -> tuple[str, ...]:
    allowed = _COMMAND_FORMATS.get((command, subcommand)) or _COMMAND_FORMATS.get(command) or STANDARD_FORMATS
    return tuple(output_format for output_format in ALL_FORMATS if output_format in allowed)


def _validate_format(args: argparse.Namespace) -> None:
    key = _format_key(args)
    allowed = _formats(*key)
    output_format = getattr(args, "format", "text")
    if output_format not in allowed:
        label = f"{args.command} {key[1]}" if key[1] else args.command
        raise ThreadError(
            f"'{label}' does not support --format {output_format}; choose from {', '.join(allowed)}."
        )


def _set_format_choices(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()) -> None:
    """Keep each command's help in step with its actual output capabilities."""
    command = path[0] if path else None
    subcommand = path[1] if len(path) > 1 else None
    for action in parser._actions:
        if "--format" in action.option_strings and command:
            action.choices = _formats(command, subcommand)
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                _set_format_choices(child, path + (name,))


def _common(parser: argparse.ArgumentParser, format_help: str = "Output format (default: text or exports.default_format).") -> None:
    parser.add_argument("--database", metavar="PATH", help="Override the local SQLite database path.")
    parser.add_argument("--format", choices=("text", "json", "markdown", "csv", "html", "mermaid", "dot"),
                        help=format_help)


def _print_preflight(queries, project: str) -> None:
    """Show the static estimate without turning it into a safety guarantee."""
    from .runtime.preflight import summary_line

    try:
        print(summary_line(queries.preflight(project)), file=sys.stderr)
    except ThreadError as exc:
        print(f"Pre-run static side-effect estimate unavailable: {exc}", file=sys.stderr)


def _runtime_capture(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--capture-level", choices=("off", "metadata", "content"), default="metadata",
                        help="Runtime resource capture: off, metadata, or bounded content (default: metadata).")
    parser.add_argument("--capture-limit", type=int, default=4096,
                        help="Maximum characters or bytes retained for one captured value (default: 4096).")
    parser.add_argument("--no-redact", action="store_true",
                        help="Retain known secrets in local runtime resource events (requires --yes-sensitive).")
    parser.add_argument("--yes-sensitive", action="store_true",
                        help="Confirm that unredacted runtime data may be persisted locally.")


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
    preferences = project_commands.add_parser("preferences", help="Show or merge this project's validated preferences.")
    preferences.add_argument("project")
    preferences.add_argument("--content", help="Partial JSON preference object to save.")
    _common(preferences)
    remove = project_commands.add_parser("remove", help="Remove project registration and stored scans.")
    remove.add_argument("project")
    remove.add_argument("--yes", action="store_true", help="Confirm removal. Source files are never deleted.")
    remove.add_argument("--keep-experiments", action="store_true",
                        help="Preserve experiment workspaces and report their paths instead of deleting them.")
    _common(remove)

    storage = commands.add_parser("storage", help="Report storage usage and enforce the retention window.")
    storage.add_argument("--prune", action="store_true", help="Delete old scans and traces outside the retention window.")
    storage.add_argument("--retention-days", type=int, help="Override storage.retention_days for this report.")
    _common(storage)

    delete = commands.add_parser("delete", help="Permanently delete stored traces, reports, or all local data.")
    delete_commands = delete.add_subparsers(dest="delete_command", required=True)
    delete_trace = delete_commands.add_parser("trace", help="Delete one saved runtime trace.")
    delete_trace.add_argument("project")
    delete_trace.add_argument("trace_id")
    delete_trace.add_argument("--yes", action="store_true", help="Confirm permanent deletion.")
    _common(delete_trace)
    delete_report = delete_commands.add_parser("report", help="Delete one stored report from the latest scan.")
    delete_report.add_argument("project")
    delete_report.add_argument("report_kind")
    delete_report.add_argument("--yes", action="store_true", help="Confirm permanent deletion.")
    _common(delete_report)
    delete_all = delete_commands.add_parser("all", help="Delete every registered project, trace, report, and experiment workspace.")
    delete_all.add_argument("--yes", action="store_true", help="Confirm permanent deletion of all ThreadTrace data.")
    _common(delete_all)

    config = commands.add_parser("config", help="Create or inspect the nonsecret local configuration file.")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("path", help="Show the configuration-file path.")
    config_commands.add_parser("show", help="Show the effective nonsecret configuration.")
    initialise = config_commands.add_parser("init", help="Create the documented configuration file.")
    initialise.add_argument("--force", action="store_true", help="Replace an existing configuration file.")

    profiles = commands.add_parser("profile", help="Manage named local launch profiles.")
    profile_commands = profiles.add_subparsers(dest="profile_command", required=True)
    profile_list = profile_commands.add_parser("list", help="List a project's launch profiles.")
    profile_list.add_argument("project")
    _common(profile_list)
    profile_save = profile_commands.add_parser("save", help="Save an explicit command as a launch profile.")
    profile_save.add_argument("project")
    profile_save.add_argument("name")
    profile_save.add_argument("--interpreter", help="Python executable that replaces the command's leading python token.")
    profile_save.add_argument("--cwd", help="Working directory; relative paths resolve against the project root.")
    profile_save.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                              help="Nonsecret environment override; repeat for multiple values.")
    profile_save.add_argument("--unset-env", action="append", default=[], metavar="NAME",
                              help="Remove an inherited environment variable; repeat for multiple values.")
    profile_save.add_argument("profile_command_line", nargs="+", help="Command to save, supplied after '--'.")
    _common(profile_save)
    profile_run = profile_commands.add_parser("run", help="Explicitly run a saved profile under bounded tracing.")
    profile_run.add_argument("project")
    profile_run.add_argument("name")
    profile_run.add_argument("--yes-run", action="store_true", help="Confirm that this profile may execute code.")
    profile_run.add_argument("--timeout", type=int, default=30)
    profile_run.add_argument("--output-cap", type=int, default=100_000)
    _common(profile_run)

    export = commands.add_parser("export", help="Write a versioned, self-describing export of the latest completed scan.")
    export.add_argument("project")
    export.add_argument("--output", help="Optional destination; stdout otherwise.")
    _common(export, "Export format (default: json).")

    investigations = commands.add_parser("investigation", help="Save, resume, export, and import named investigation context.")
    investigation_commands = investigations.add_subparsers(dest="investigation_command", required=True)
    investigation_list = investigation_commands.add_parser("list", help="List a project's investigations.")
    investigation_list.add_argument("project")
    _common(investigation_list)
    investigation_show = investigation_commands.add_parser("show", help="Show a saved investigation and its captured source references.")
    investigation_show.add_argument("project")
    investigation_show.add_argument("name")
    _common(investigation_show)
    investigation_resume = investigation_commands.add_parser("resume", help="Reopen an investigation and report changed, moved, or missing references.")
    investigation_resume.add_argument("project")
    investigation_resume.add_argument("name")
    _common(investigation_resume)
    investigation_save = investigation_commands.add_parser("save", help="Save JSON notes and selected references for an investigation.")
    investigation_save.add_argument("project")
    investigation_save.add_argument("name")
    investigation_save.add_argument("--content", default="{}", help="JSON object with files, symbols, events, notes, hypotheses, graphs, relationships, traces, experiments, and bookmarks.")
    _common(investigation_save)
    investigation_export = investigation_commands.add_parser("export", help="Export an investigation with its source revision and limitations.")
    investigation_export.add_argument("project")
    investigation_export.add_argument("name")
    investigation_export.add_argument("--output", help="Optional JSON destination.")
    _common(investigation_export)
    investigation_import = investigation_commands.add_parser("import", help="Import a portable investigation export into a project.")
    investigation_import.add_argument("project")
    investigation_import.add_argument("path")
    investigation_import.add_argument("--name", help="Optional local investigation name.")
    _common(investigation_import)

    experiments = commands.add_parser("experiment", help="Manage isolated, copy-based source experiments.")
    experiment_commands = experiments.add_subparsers(dest="experiment_command", required=True)
    experiment_list = experiment_commands.add_parser("list", help="List a project's experiments.")
    experiment_list.add_argument("project")
    _common(experiment_list)
    experiment_create = experiment_commands.add_parser("create", help="Create a private copy of a project for an experiment.")
    experiment_create.add_argument("project")
    experiment_create.add_argument("name")
    _common(experiment_create)
    experiment_remove = experiment_commands.add_parser("remove", help="Permanently remove a named experiment workspace and history.")
    experiment_remove.add_argument("project")
    experiment_remove.add_argument("name")
    experiment_remove.add_argument("--yes", action="store_true", help="Confirm permanent experiment removal.")
    _common(experiment_remove)
    experiment_configure = experiment_commands.add_parser("configure", help="Save the complete JSON experiment definition.")
    experiment_configure.add_argument("project")
    experiment_configure.add_argument("name")
    experiment_configure.add_argument("--config", required=True, help="JSON object containing patches, substitutions, overrides, mocks, adapters, inputs, and replay controls.")
    _common(experiment_configure)
    experiment_patch = experiment_commands.add_parser("patch", help="Replace a source-line range in an experiment copy.")
    experiment_patch.add_argument("project")
    experiment_patch.add_argument("name")
    experiment_patch.add_argument("path")
    experiment_patch.add_argument("start_line", type=int)
    experiment_patch.add_argument("end_line", type=int)
    experiment_patch.add_argument("replacement", help="Replacement source text for the inclusive range.")
    _common(experiment_patch)
    experiment_diff = experiment_commands.add_parser("diff", help="Show source changes in an experiment copy.")
    experiment_diff.add_argument("project")
    experiment_diff.add_argument("name")
    _common(experiment_diff)
    experiment_runs = experiment_commands.add_parser("runs", help="List saved callable, exploration, replay, and comparison runs.")
    experiment_runs.add_argument("project")
    experiment_runs.add_argument("name")
    _common(experiment_runs)
    experiment_preflight = experiment_commands.add_parser("preflight", help="Validate experiment rules and report precedence, aliases, limits, and replay controls without executing code.")
    experiment_preflight.add_argument("project")
    experiment_preflight.add_argument("name")
    _common(experiment_preflight)
    experiment_capability = experiment_commands.add_parser("capability", help="Inspect support for one callable form in the experiment workspace.")
    experiment_capability.add_argument("project")
    experiment_capability.add_argument("name")
    experiment_capability.add_argument("target")
    experiment_capability.add_argument("--yes-run", action="store_true", help="Confirm importing the callable's module may execute code.")
    experiment_capability.add_argument("--timeout", type=int, default=30)
    experiment_capability.add_argument("--output-cap", type=int, default=100_000)
    _common(experiment_capability)
    experiment_export = experiment_commands.add_parser("export", help="Export the saved definition, diff, and run history as JSON.")
    experiment_export.add_argument("project")
    experiment_export.add_argument("name")
    experiment_export.add_argument("--output", help="Optional JSON destination.")
    _common(experiment_export)
    experiment_call = experiment_commands.add_parser("call", help="Run a function, class constructor, or method sequence in the copied workspace.")
    experiment_call.add_argument("project")
    experiment_call.add_argument("name")
    experiment_call.add_argument("target", nargs="?", help="Callable target as module:attribute, e.g. app.core:Service.process.")
    experiment_call.add_argument("--args", default="[]", help="JSON positional argument list.")
    experiment_call.add_argument("--kwargs", default="{}", help="JSON keyword argument object.")
    experiment_call.add_argument("--operations", help="JSON operation list supporting call, construct, call_instance, get/set/inspect_instance, and set_global.")
    experiment_call.add_argument("--per-call", default="{}", help="JSON rules that override saved configuration for this call.")
    experiment_call.add_argument("--yes-run", action="store_true", help="Confirm that this callable experiment may execute code.")
    experiment_call.add_argument("--timeout", type=int, default=30)
    experiment_call.add_argument("--output-cap", type=int, default=100_000)
    _common(experiment_call)
    experiment_explore = experiment_commands.add_parser("explore", help="Generate or run boundary/invalid cases against a callable.")
    experiment_explore.add_argument("project")
    experiment_explore.add_argument("name")
    experiment_explore.add_argument("target", help="Callable target as module:attribute.")
    experiment_explore.add_argument("--cases", help="JSON list of {label, args, kwargs} cases; defaults to signature-derived cases.")
    experiment_explore.add_argument("--yes-run", action="store_true", help="Confirm that input exploration may execute code.")
    experiment_explore.add_argument("--timeout", type=int, default=30)
    experiment_explore.add_argument("--output-cap", type=int, default=100_000)
    _common(experiment_explore)
    experiment_replay = experiment_commands.add_parser("replay", help="Re-run an exact saved request and check observed nondeterminism.")
    experiment_replay.add_argument("project")
    experiment_replay.add_argument("name")
    experiment_replay.add_argument("run_id")
    experiment_replay.add_argument("--yes-run", action="store_true", help="Confirm that replay may execute code.")
    experiment_replay.add_argument("--timeout", type=int, default=30)
    experiment_replay.add_argument("--output-cap", type=int, default=100_000)
    _common(experiment_replay)
    experiment_compare = experiment_commands.add_parser("compare", help="Compare baseline and configured callable behaviour.")
    experiment_compare.add_argument("project")
    experiment_compare.add_argument("name")
    experiment_compare.add_argument("target", help="Callable target as module:attribute.")
    experiment_compare.add_argument("--args", default="[]", help="JSON positional argument list.")
    experiment_compare.add_argument("--kwargs", default="{}", help="JSON keyword argument object.")
    experiment_compare.add_argument("--yes-run", action="store_true", help="Confirm that comparison may execute code.")
    experiment_compare.add_argument("--timeout", type=int, default=30)
    experiment_compare.add_argument("--output-cap", type=int, default=100_000)
    _common(experiment_compare)
    experiment_run = experiment_commands.add_parser("run", help="Explicitly execute an experiment copy.")
    experiment_run.add_argument("project")
    experiment_run.add_argument("name")
    experiment_run.add_argument("--yes-run", action="store_true", help="Confirm that this experiment command may execute code.")
    experiment_run.add_argument("--timeout", type=int, default=30)
    experiment_run.add_argument("--output-cap", type=int, default=100_000)
    experiment_run.add_argument("experiment_command_line", nargs="+", help="Command to run, supplied after '--'.")
    _common(experiment_run)

    scan = commands.add_parser("scan", help="Analyse a project explicitly.")
    scan.add_argument("project")
    scan.add_argument("--source", action="append", default=[], metavar="PATH",
                        help="Explicit Python file, notebook, or zip source outside the automatic project walk.")
    scan.add_argument("--generated", action="append", default=[], metavar="NAME=TEXT",
                        help="Add generated Python text under a stable virtual origin.")
    _common(scan)
    history = commands.add_parser("scan-history", help="List scans for a project.")
    history.add_argument("project")
    _common(history)
    staleness = commands.add_parser("staleness", help="Explain whether stored results are stale and why.")
    staleness.add_argument("project")
    _common(staleness)

    overview = commands.add_parser("overview", help="Summarise a project as evidence-backed components and entry points.")
    overview.add_argument("project")
    _common(overview)
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
    file_map = commands.add_parser("map", help="Show files linked to an entry point, module, or symbol.")
    file_map.add_argument("project")
    file_map.add_argument("target", nargs="?", help="Optional entry point, qualified symbol, or module to focus on.")
    file_map.add_argument("--depth", type=int, default=4)
    _common(file_map)
    impact = commands.add_parser("impact", help="Show directional static impact through calls, imports, inheritance, and references.")
    impact.add_argument("project")
    impact.add_argument("symbol")
    impact.add_argument("--depth", type=int, default=3)
    impact.add_argument("--direction", choices=("both", "upstream", "downstream"), default="both")
    _common(impact)
    dataflow = commands.add_parser("dataflow", help="Show bounded direct-call argument and return flow.")
    dataflow.add_argument("project")
    dataflow.add_argument("symbol")
    dataflow.add_argument("--to", dest="resource_kind", choices=("database", "http", "file", "configuration", "structured-data", "text",
                                                                 "template", "media", "serialization", "compression", "package", "service", "environment",
                                                                 "interpreter", "diagnostics", "queue", "env", "subprocess"))
    _common(dataflow)
    slice_command = commands.add_parser("slice", help="Show a bounded forward, backward, or combined slice for one value.")
    slice_command.add_argument("project")
    slice_command.add_argument("symbol")
    slice_command.add_argument("variable")
    slice_command.add_argument("--direction", choices=("both", "backward", "forward"), default="both")
    _common(slice_command)
    control_flow = commands.add_parser("control-flow", help="Show a syntax-level control-flow graph for one function or method.")
    control_flow.add_argument("project")
    control_flow.add_argument("symbol")
    _common(control_flow)
    complexity = commands.add_parser("complexity", help="Explain a structural time/space estimate for one function or method.")
    complexity.add_argument("project")
    complexity.add_argument("symbol")
    _common(complexity)
    advanced = commands.add_parser("advanced", help="Show decorators, async/generator, closure, descriptor, concurrency, and native facts.")
    advanced.add_argument("project")
    advanced.add_argument("symbol")
    _common(advanced)
    resources = commands.add_parser("resources", help="List detected external resource boundaries.")
    resources.add_argument("project")
    resources.add_argument("--kind", choices=("database", "http", "file", "configuration", "structured-data", "text",
                                              "template", "media", "serialization", "compression", "package", "service", "environment",
                                              "interpreter", "diagnostics", "queue", "env", "subprocess"))
    _common(resources)
    explain = commands.add_parser("explain", help="Explain an entry point, symbol, or project overview from stored evidence.")
    explain.add_argument("project")
    explain.add_argument("target")
    explain.add_argument("--ai", action="store_true", help="Optionally generate a validated interpretation using a configured provider.")
    explain.add_argument("--ai-provider", choices=("ollama", "openai"), help="Local Ollama or an OpenAI-compatible provider.")
    explain.add_argument("--ai-model", help="Provider model name (default: ai.model).")
    explain.add_argument("--ai-endpoint", help="Provider endpoint URL (default: ai.endpoint or the provider's local URL).")
    explain.add_argument("--ai-temperature", type=float, help="Sampling temperature from 0 to 2.")
    explain.add_argument("--ai-tokens", type=int, help="Maximum generated tokens.")
    explain.add_argument("--ai-template", help="Custom prompt template containing the {facts} placeholder.")
    explain.add_argument("--ai-parameter", action="append", default=[], metavar="NAME=VALUE",
                         help="Supported provider parameter; repeat to add more.")
    explain.add_argument("--ai-show", action="store_true", help="Print the exact bounded payload and digest; send nothing.")
    explain.add_argument("--ai-approve", metavar="SHA256", help="Approve this exact remote payload digest for one request.")
    _common(explain)
    ai_history = commands.add_parser("ai-history", help="List stored AI interpretations and their deletion handles.")
    ai_history.add_argument("project")
    _common(ai_history)
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
    trace.add_argument("--cwd", help="Working directory for the explicit command; defaults to the project root.")
    trace.add_argument("--instrumentation", choices=("auto", "settrace", "monitoring", "profile", "audit", "all"), default="auto",
                       help="Choose the runtime instrumentation plan; unsupported mechanisms report a fallback.")
    trace.add_argument("--include-third-party", action="store_true", help="Record reached third-party Python frames in addition to project code.")
    trace.add_argument("run_command", nargs="+", help="Command to run, supplied after '--'.")
    _runtime_capture(trace)
    _common(trace)
    runtime = commands.add_parser("runtime", help="Open an interactive, step-by-step dashboard for one user-supplied command.")
    runtime.add_argument("project")
    runtime.add_argument("--timeout", type=int, default=30, help="Stop the runtime subprocess after this many seconds (default: 30).")
    runtime.add_argument("--output-cap", type=int, default=100_000, help="Maximum stdout and stderr characters to retain.")
    runtime.add_argument("--cwd", help="Working directory for the explicit command; defaults to the project root.")
    _common(runtime)
    runtime.add_argument("run_command", nargs="+", help="Command to run, supplied after '--'. The dashboard waits for Start before executing it.")
    _runtime_capture(runtime)
    runtime_host = commands.add_parser("runtime-host", help="Run an interactive Python program in this terminal while publishing safe live events.")
    runtime_host.add_argument("project")
    runtime_host.add_argument("--yes-run", action="store_true", help="Confirm that the Runtime Host may execute target-project code.")
    runtime_host.add_argument("--event-cap", type=int, default=100_000, help="Maximum project runtime events to publish (default: 100000).")
    runtime_host.add_argument("--cwd", help="Working directory for the explicit command; defaults to the project root.")
    runtime_host.add_argument("--instrumentation", choices=("auto", "settrace", "monitoring", "profile", "audit", "all"), default="auto")
    runtime_host.add_argument("--include-third-party", action="store_true", help="Record reached third-party frames.")
    _common(runtime_host)
    runtime_host.add_argument("run_command", nargs="+", help="Interactive command to run, supplied after '--'. It keeps this terminal.")
    _runtime_capture(runtime_host)
    runtime_listen = commands.add_parser("runtime-listen", help="Open a read-only dashboard for a Runtime Host session in another terminal.")
    runtime_listen.add_argument("project")
    runtime_listen.add_argument("--session", help="Specific live session ID; defaults to the newest session for the project.")
    _common(runtime_listen)
    processes = commands.add_parser("processes", help="List current-user Python processes that ThreadTrace can inspect safely.")
    _common(processes)
    process = commands.add_parser("process", help="Select a running Python process, then scan it or explicitly restart it under Runtime.")
    process.add_argument("pid", type=int, nargs="?", help="Optional PID to preselect in the confirmation screen.")
    process.add_argument("--timeout", type=int, default=30, help="Runtime timeout after a restarted process is continued (default: 30).")
    process.add_argument("--output-cap", type=int, default=100_000, help="Maximum stdout and stderr characters to retain after restart.")
    _common(process)
    traces = commands.add_parser("traces", help="List explicitly captured runtime traces.")
    traces.add_argument("project")
    _common(traces)
    trace_events = commands.add_parser("trace-events", help="Show recorded events from one explicit runtime trace.")
    trace_events.add_argument("project")
    trace_events.add_argument("trace_id")
    trace_events.add_argument("--kind", help="Presentation filter for one event kind.")
    trace_events.add_argument("--path", help="Presentation filter for a relative path prefix.")
    trace_events.add_argument("--search", help="Search recorded event history without changing it.")
    _common(trace_events)
    trace_concurrency = commands.add_parser("trace-concurrency", help="Show process, thread, task, and wait correlation for one trace.")
    trace_concurrency.add_argument("project")
    trace_concurrency.add_argument("trace_id")
    _common(trace_concurrency)
    trace_resources = commands.add_parser("trace-resources", help="Show the observed resource and serialisation timeline for one trace.")
    trace_resources.add_argument("project")
    trace_resources.add_argument("trace_id")
    _common(trace_resources)
    trace_failure = commands.add_parser("trace-failure", help="Show the recorded failure report for one explicit runtime trace.")
    trace_failure.add_argument("project")
    trace_failure.add_argument("trace_id")
    _common(trace_failure)
    coverage = commands.add_parser("coverage", help="Show native line/function coverage reconstructed from one runtime trace.")
    coverage.add_argument("project")
    coverage.add_argument("trace_id")
    _common(coverage)
    trace_profile = commands.add_parser("trace-profile", help="Show per-function timing, call counts, hot paths, and memory support from one trace.")
    trace_profile.add_argument("project")
    trace_profile.add_argument("trace_id")
    _common(trace_profile)
    api = commands.add_parser("api", help="Show statically recognised web API routes and controls.")
    api.add_argument("project")
    _common(api)
    bookmark = commands.add_parser("bookmark", help="Save a labelled event in an explicit runtime trace.")
    bookmark.add_argument("project")
    bookmark.add_argument("trace_id")
    bookmark.add_argument("ordinal", type=int)
    bookmark.add_argument("label")
    bookmark.add_argument("--note", default="")
    _common(bookmark)

    modules = commands.add_parser("modules", help="List project modules.")
    modules.add_argument("project")
    _common(modules)
    references = commands.add_parser("references", help="Show references bound to a symbol or definition.")
    references.add_argument("project")
    references.add_argument("target")
    _common(references)
    usage = commands.add_parser("usage", help="Summarise references by visible name and binding resolution.")
    usage.add_argument("project")
    usage.add_argument("target")
    _common(usage)
    boundaries = commands.add_parser("boundaries", help="Show dynamic import, code, reflection, and monkey-patch boundaries.")
    boundaries.add_argument("project")
    _common(boundaries)
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
    metadata = commands.add_parser("metadata", help="Show parsed project metadata files and dependencies.")
    metadata.add_argument("project")
    _common(metadata)
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
    capabilities = commands.add_parser("capabilities", help="Show Full, Partial, and Unsupported ThreadTrace capabilities.")
    capabilities.add_argument("project")
    capabilities.add_argument("--scan", help="Scope the report to one stored scan ID.")
    capabilities.add_argument("--run", help="Scope the report to one stored runtime trace ID.")
    capabilities.add_argument("--interpreter", help="Record the interpreter context used for runtime status.")
    capabilities.add_argument("--feature", help="Filter capabilities by feature or area text.")
    capabilities.add_argument("--entity", help="Record one entity the capability question applies to.")
    _common(capabilities)
    browser = commands.add_parser("browser", help="Serve a read-only project view on a loopback-only local address.")
    browser.add_argument("project")
    browser.add_argument("--host", choices=("127.0.0.1", "::1"), help="Override browser.host from config.yaml.")
    browser.add_argument("--port", type=int, help="Override browser.port from config.yaml; 0 chooses an available port.")
    _common(browser)
    tui = commands.add_parser("tui", help="Open the full-screen terminal explorer.")
    tui.add_argument("project")
    _common(tui)
    _set_format_choices(parser)
    return parser


def _warn_if_stale(queries: QueryService, project: str) -> None:
    report = queries.staleness(project)
    if report["stale"]:
        causes = ", ".join(item["cause"] for item in report["causes"])
        print(f"Warning: stored results are stale ({causes}). Run 'threadtrace scan' to refresh.", file=sys.stderr)


def _json_value(text: str, label: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ThreadError(f"{label} must be valid JSON: {exc.msg}") from exc


def _json_object(text: str, label: str) -> dict[str, Any]:
    value = _json_value(text, label)
    if not isinstance(value, dict):
        raise ThreadError(f"{label} must be a JSON object.")
    return value


def _json_list(text: str, label: str) -> list[Any]:
    value = _json_value(text, label)
    if not isinstance(value, list):
        raise ThreadError(f"{label} must be a JSON list.")
    return value


def _confirm_experiment_execution(confirmed: bool) -> None:
    if not confirmed:
        raise ThreadError("Experiment execution can run code. Re-run with --yes-run.")
    print("Warning: experiment execution runs copied project code and is not a security sandbox.", file=sys.stderr)


def _warn_experiment_preflight(engine, project: str, name: str) -> None:
    """Report precedence, alias, and replay limits before any project code runs."""
    report = engine.experiment_preflight(project, name)
    notes = []
    if report["shadowed"]:
        notes.append(f"{len(report['shadowed'])} override rule(s) shadow lower-precedence rules")
    if report["prebound_aliases"]:
        notes.append(f"{len(report['prebound_aliases'])} pre-bound alias site(s) may keep the original value")
    uncontrolled = report["replay"]["uncontrolled"]
    if uncontrolled:
        notes.append(f"{len(uncontrolled)} nondeterministic input(s) remain uncontrolled")
    if notes:
        print("Experiment preflight: " + "; ".join(notes) + ". Run 'threadtrace experiment preflight' for details.",
              file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        configuration = load_config()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "format", None) is None:
        configured = "json" if args.command == "export" else configuration["exports"]["default_format"]
        allowed = _formats(*_format_key(args)) if hasattr(args, "command") else ("text",)
        args.format = configured if configured in allowed else ("json" if args.command == "export" else "text")
    try:
        _validate_format(args)
    except ThreadError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.command == "config":
        try:
            if args.config_command == "path":
                print(configuration_path())
            elif args.config_command == "show":
                render(configuration, "text")
            else:
                print(write_default_config(overwrite=args.force))
            return 0
        except (FileExistsError, OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    database_path = getattr(args, "database", None) or configuration["storage"]["database_path"]
    store: Store | None = None
    try:
        try:
            store = Store(database_path)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            print(f"Error: could not open database: {exc}", file=sys.stderr)
            return 2
        engine = Engine(store)
        queries = QueryService(engine)
        if args.command == "storage":
            days = args.retention_days or configuration["storage"]["retention_days"]
            usage = engine.storage_usage()
            quota_bytes = configuration["storage"]["quota_mb"] * 1024 * 1024
            value = {
                "usage": usage, "retention_days": days, "quota_mb": configuration["storage"]["quota_mb"],
                "quota_exceeded": bool(quota_bytes and usage["total_bytes"] > quota_bytes),
            }
            if args.prune:
                value["pruned"] = engine.prune_storage(days)
                value["usage_after"] = engine.storage_usage()
            render(value, args.format)
            return 0
        if args.command == "delete":
            if not args.yes:
                raise ThreadError("Deletion is permanent. Re-run with --yes to confirm.")
            if args.delete_command == "trace":
                value = engine.remove_trace(args.project, args.trace_id)
            elif args.delete_command == "report":
                value = engine.remove_report(args.project, args.report_kind)
            else:
                value = engine.remove_all_data()
            render(value, args.format)
            return 0
        if args.command == "project":
            if args.project_command == "add":
                value = engine.add_project(args.path, args.name)
            elif args.project_command == "list":
                value = engine.list_projects()
            elif args.project_command == "show":
                value = engine.get_project(args.project)
                value["scans"] = engine.scan_history(args.project)
            elif args.project_command == "preferences":
                if args.content:
                    saved = engine.save_project_preferences(args.project, _json_object(args.content, "Project preferences"))
                else:
                    saved = engine.project_preferences(args.project)
                value = {"project": args.project, "preferences": saved,
                         "effective": merge_config(configuration, saved)}
            else:
                if not args.yes:
                    raise ThreadError("Refusing to remove registration without --yes. Source files are not affected.")
                value = engine.remove_project(args.project, keep_experiments=args.keep_experiments)
            render(value, args.format)
            return 0
        if args.command == "profile":
            if args.profile_command == "list":
                value = engine.launch_profiles(args.project)
            elif args.profile_command == "run":
                if not args.yes_run:
                    raise ThreadError("Profile execution can run code. Re-run with --yes-run.")
                print("Warning: profile execution runs the saved command and is not a security sandbox.", file=sys.stderr)
                value = engine.run_launch_profile(args.project, args.name, args.timeout, args.output_cap)
            else:
                environment = _profile_environment(args.env, args.unset_env)
                value = engine.save_launch_profile(
                    args.project, args.name, args.profile_command_line, args.interpreter, args.cwd, environment,
                )
            render(value, args.format)
            return 0
        if args.command == "export":
            text = _render_export(report_bundle(engine, queries, args.project), args.format)
            if args.output:
                print(f"Wrote {args.format} export to {write_text(args.output, text)}", file=sys.stderr)
            else:
                print(text)
            return 0
        if args.command == "investigation":
            if args.investigation_command == "list":
                value = engine.investigations(args.project)
            elif args.investigation_command == "show":
                value = engine.investigation(args.project, args.name)
            elif args.investigation_command == "resume":
                value = engine.resume_investigation(args.project, args.name)
                for warning in value["warnings"]:
                    print(f"Warning: {warning}", file=sys.stderr)
            elif args.investigation_command == "export":
                if args.output:
                    print("Warning: investigation notes and source snippets may be sensitive; protect the output file.", file=sys.stderr)
                value = engine.export_investigation(args.project, args.name, args.output)
            elif args.investigation_command == "import":
                value = engine.import_investigation(args.project, args.path, args.name)
                for warning in value["warnings"]:
                    print(f"Warning: {warning}", file=sys.stderr)
            else:
                try:
                    content = json.loads(args.content)
                except json.JSONDecodeError as exc:
                    raise ThreadError(f"Investigation content must be a JSON object: {exc.msg}") from exc
                if not isinstance(content, dict):
                    raise ThreadError("Investigation content must be a JSON object.")
                value = engine.save_investigation(args.project, args.name, content)
            render(value, args.format)
            return 0
        if args.command == "experiment":
            if args.experiment_command == "list":
                value = engine.experiments(args.project)
            elif args.experiment_command == "create":
                value = engine.create_experiment(args.project, args.name)
            elif args.experiment_command == "remove":
                if not args.yes:
                    raise ThreadError("Refusing to remove the experiment without --yes. The registered source is not affected.")
                value = engine.remove_experiment(args.project, args.name)
            elif args.experiment_command == "patch":
                value = engine.patch_experiment(args.project, args.name, args.path, args.start_line, args.end_line, args.replacement)
            elif args.experiment_command == "configure":
                value = engine.configure_experiment(args.project, args.name, _json_object(args.config, "Experiment configuration"))
            elif args.experiment_command == "diff":
                if args.format == "json":
                    render(engine.experiment_changes(args.project, args.name), "json")
                else:
                    print(engine.experiment_diff(args.project, args.name), end="")
                return 0
            elif args.experiment_command == "runs":
                value = engine.experiment_runs(args.project, args.name)
            elif args.experiment_command == "export":
                if args.output:
                    print("Warning: experiment exports may contain captured inputs, outputs, state, and source diffs. Protect the output file accordingly.", file=sys.stderr)
                value = engine.export_experiment(args.project, args.name, args.output)
            elif args.experiment_command == "preflight":
                value = engine.experiment_preflight(args.project, args.name)
            elif args.experiment_command == "capability":
                _warn_experiment_preflight(engine, args.project, args.name)
                _confirm_experiment_execution(args.yes_run)
                value = engine.experiment_capability(args.project, args.name, args.target, args.timeout, args.output_cap)
            elif args.experiment_command == "call":
                _warn_experiment_preflight(engine, args.project, args.name)
                _confirm_experiment_execution(args.yes_run)
                operations = _json_value(args.operations, "Harness operations") if args.operations else None
                if operations is None and not args.target:
                    raise ThreadError("A callable target is required unless --operations is supplied.")
                value = engine.callable_experiment(args.project, args.name, args.target or "", _json_list(args.args, "Callable args"),
                                                   _json_object(args.kwargs, "Callable kwargs"), args.timeout, args.output_cap, operations,
                                                   _json_object(args.per_call, "Per-call configuration"))
            elif args.experiment_command == "explore":
                _warn_experiment_preflight(engine, args.project, args.name)
                _confirm_experiment_execution(args.yes_run)
                cases = _json_value(args.cases, "Input cases") if args.cases else None
                value = engine.explore_experiment(args.project, args.name, args.target, cases, args.timeout, args.output_cap)
            elif args.experiment_command == "replay":
                _warn_experiment_preflight(engine, args.project, args.name)
                _confirm_experiment_execution(args.yes_run)
                value = engine.replay_experiment(args.project, args.name, args.run_id, args.timeout, args.output_cap)
            elif args.experiment_command == "compare":
                _warn_experiment_preflight(engine, args.project, args.name)
                _confirm_experiment_execution(args.yes_run)
                value = engine.compare_experiment(args.project, args.name, args.target, _json_list(args.args, "Callable args"),
                                                  _json_object(args.kwargs, "Callable kwargs"), args.timeout, args.output_cap)
            else:
                _confirm_experiment_execution(args.yes_run)
                value = engine.run_experiment(args.project, args.name, args.experiment_command_line, args.timeout, args.output_cap)
            render(value, args.format)
            return 0
        if args.command == "bookmark":
            render(engine.bookmark_trace(args.project, args.trace_id, args.ordinal, args.label, args.note), args.format)
            return 0
        if args.command == "scan":
            effective = merge_config(configuration, engine.project_preferences(args.project))
            generated = []
            for item in args.generated:
                name, separator, text = item.partition("=")
                if not separator or not name.strip():
                    print("Error: --generated expects NAME=TEXT.", file=sys.stderr)
                    return 2
                generated.append({"name": name.strip(), "text": text})
            value = engine.scan(
                args.project, includes=effective["discovery"]["include"],
                excludes=set(effective["discovery"]["exclude"]),
                explicit_sources=args.source, generated=generated,
            )
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
            if args.capture_limit < 1:
                raise ThreadError("Runtime capture limit must be positive.")
            if args.no_redact and not args.yes_sensitive:
                raise ThreadError("Unredacted capture can persist credentials and private data. Re-run with --no-redact --yes-sensitive to confirm.")
            effective = merge_config(configuration, engine.project_preferences(args.project))
            _print_preflight(queries, args.project)
            value = engine.trace(
                args.project, args.run_command, args.timeout, args.output_cap, args.cwd,
                args.capture_level, args.capture_limit, not args.no_redact,
                save=effective["storage"]["trace_saving"] == "explicit",
                instrumentation=args.instrumentation, include_third_party=args.include_third_party,
            )
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
                raise ThreadError("The runtime dashboard requires prompt_toolkit. Reinstall ThreadTrace with: uv pip install -e .") from exc
            print("Warning: Runtime mode can execute the supplied command. The dashboard opens paused; press s or n to start it. It is not a security sandbox.", file=sys.stderr)
            if args.capture_limit < 1:
                raise ThreadError("Runtime capture limit must be positive.")
            if args.no_redact and not args.yes_sensitive:
                raise ThreadError("Unredacted capture can persist credentials and private data. Re-run with --no-redact --yes-sensitive to confirm.")
            _print_preflight(queries, args.project)
            RuntimeApp(
                engine, args.project, args.run_command, args.timeout, args.output_cap,
                cwd=Path(args.cwd).expanduser() if args.cwd else None,
                capture_level=args.capture_level, capture_limit=args.capture_limit,
                redact=not args.no_redact,
            ).run()
            return 0
        if args.command == "runtime-host":
            if not args.yes_run:
                raise ThreadError("Runtime Host executes target-project code. Re-run with --yes-run.")
            print("Warning: Runtime Host executes the supplied command and is not a security sandbox.", file=sys.stderr)
            if args.event_cap < 1:
                raise ThreadError("Runtime Host event cap must be positive.")
            project = engine.get_project(args.project)
            from .runtime.host import RuntimeHost
            if args.capture_limit < 1:
                raise ThreadError("Runtime capture limit must be positive.")
            if args.no_redact and not args.yes_sensitive:
                raise ThreadError("Unredacted capture can persist credentials and private data. Re-run with --no-redact --yes-sensitive to confirm.")
            _print_preflight(queries, args.project)
            host = RuntimeHost(
                Path(project["root_path"]), project["name"], args.run_command, args.event_cap,
                Path(args.cwd).expanduser() if args.cwd else None, args.capture_level,
                args.capture_limit, not args.no_redact, args.instrumentation, args.include_third_party,
            )
            print(f"Runtime Host session {host.session_id} is starting. In another terminal run: threadtrace runtime-listen {project['name']} --session {host.session_id}", file=sys.stderr)
            return host.run()
        if args.command == "runtime-listen":
            try:
                from .live_tui import LiveRuntimeApp
            except ModuleNotFoundError as exc:
                raise ThreadError("The live runtime listener requires prompt_toolkit. Reinstall ThreadTrace with: uv pip install -e .") from exc
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
                raise ThreadError("The process selector requires prompt_toolkit. Reinstall ThreadTrace with: uv pip install -e .") from exc
            print("Process discovery is read-only until you confirm Scan or Restart. Restart stops the selected current-user Python process tree before relaunching it.", file=sys.stderr)
            ProcessApp(engine, args.pid, args.timeout, args.output_cap).run()
            return 0
        if args.command == "tui":
            _warn_if_stale(queries, args.project)
            try:
                from .tui import ThreadApp
            except ModuleNotFoundError as exc:
                raise ThreadError("The terminal explorer requires prompt_toolkit. Reinstall ThreadTrace with: uv pip install -e .") from exc

            effective = merge_config(configuration, engine.project_preferences(args.project))
            ThreadApp(engine, queries, args.project, effective["tui"], effective["ai"]).run()
            return 0
        if args.command == "browser":
            host = args.host or configuration["browser"]["host"]
            port = args.port if args.port is not None else configuration["browser"]["port"]
            if not 0 <= port <= 65535:
                raise ThreadError("Browser port must be between 0 and 65535.")
            from .browser import BrowserServer

            _warn_if_stale(queries, args.project)
            server = BrowserServer(host, port, queries, args.project)
            print(f"ThreadTrace browser view: {server.url}", file=sys.stderr)
            print("Press Ctrl-C to stop the local server.", file=sys.stderr)
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return 0
        if args.command != "staleness":
            _warn_if_stale(queries, args.project)

        def _explain() -> Any:
            if not args.ai:
                return queries.explain(args.project, args.target)
            options = _ai_options(configuration, args)
            if args.ai_show:
                request = queries.ai_preview(args.project, args.target, options)
                print(json.dumps(request, indent=2, default=_json_default), file=sys.stderr)
                return {"approval_required": request["remote"], "digest": request["digest"],
                        "notice": f"Review the payload on stderr, then run with --ai-approve {request['digest']}."}
            return queries.ai_explain(args.project, args.target, options, _ai_approval(args))

        handlers: dict[str, Callable[[], Any]] = {
            "overview": lambda: queries.overview(args.project),
            "entrypoints": lambda: queries.entrypoints(args.project, args.kind),
            "architecture": lambda: queries.architecture(args.project),
            "path": lambda: queries.path(args.project, args.entrypoint, args.depth, args.confidence),
            "map": lambda: queries.file_map(args.project, args.target, args.depth),
            "impact": lambda: queries.impact(args.project, args.symbol, args.depth, args.direction),
            "dataflow": lambda: queries.dataflow(args.project, args.symbol, args.resource_kind),
            "slice": lambda: queries.slice(args.project, args.symbol, args.variable, args.direction),
            "control-flow": lambda: queries.control_flow(args.project, args.symbol),
            "complexity": lambda: queries.complexity(args.project, args.symbol),
            "advanced": lambda: queries.advanced(args.project, args.symbol),
            "resources": lambda: queries.resources(args.project, args.kind),
            "explain": _explain,
            "ai-history": lambda: queries.ai_history(args.project),
            "lesson": lambda: queries.lesson(args.project, args.symbol),
            "compare": lambda: queries.compare(args.project, args.scan_a, args.scan_b),
            "traces": lambda: queries.traces(args.project),
            "trace-events": lambda: queries.trace_events(args.project, args.trace_id, args.kind, args.path, args.search),
            "trace-concurrency": lambda: queries.trace_concurrency(args.project, args.trace_id),
            "trace-resources": lambda: queries.trace_resources(args.project, args.trace_id),
            "trace-failure": lambda: queries.trace_failure(args.project, args.trace_id),
            "coverage": lambda: queries.coverage(args.project, args.trace_id),
            "trace-profile": lambda: queries.trace_profile(args.project, args.trace_id),
            "api": lambda: queries.api(args.project),
            "staleness": lambda: queries.staleness(args.project),
            "modules": lambda: queries.modules(args.project),
            "references": lambda: queries.references(args.project, args.target),
            "usage": lambda: queries.usage(args.project, args.target),
            "boundaries": lambda: queries.boundaries(args.project),
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
            "metadata": lambda: queries.metadata(args.project),
            "diagnostics": lambda: queries.diagnostics(args.project, args.severity, args.rule, args.include_unresolved_calls),
            "findings": lambda: queries.findings(args.project, args.details),
            "capabilities": lambda: queries.capabilities(
                args.project, scan_id=args.scan, interpreter=args.interpreter, run_id=args.run,
                feature=args.feature, entity=args.entity,
            ),
        }
        value = handlers[args.command]()
        if args.command == "explain" and args.ai and not args.ai_show and args.format in {"text", "markdown"}:
            print(value["narrative"])
        elif args.command == "architecture" and args.format in {"mermaid", "dot"}:
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
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
