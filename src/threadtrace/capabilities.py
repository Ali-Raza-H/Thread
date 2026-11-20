"""Honest, versioned support information for ThreadTrace features."""

from __future__ import annotations

from typing import Any

from .evidence import record


CAPABILITIES: tuple[dict[str, str], ...] = (
    {
        "area": "Static analysis",
        "feature": "Python source, symbols, imports, calls, entry points, and resources",
        "status": "Partial",
        "details": "Uses Python's ast parser. Framework analysers require an explicit import and a bound application, blueprint, router, parser, or task object before creating a route or command entry. They cover Flask, Quart, FastAPI, Starlette, aiohttp, Django URLconfs, argparse, Click, Typer, optparse, command registries, Celery/RQ/Dramatiq/Huey tasks, APScheduler jobs, pluggy hooks, lifecycle hooks, tests, scripts, and modules. Direct Flask/FastAPI/Django controls and DB-API, SQLAlchemy, and Django ORM data-access calls remain syntax-only evidence. Same-name decorators without framework registration stay under generic analysis. Dynamic code, third-party environments, and indirect registration remain unsupported.",
    },
    {
        "area": "Static analysis",
        "feature": "Source discovery and provenance",
        "status": "Partial",
        "details": "Ordinary and PEP 420 namespace packages are discovered without broad traversal; directory symlinks are never followed and external file symlinks require an explicit path. Explicit external files, validated zip members, notebook code cells, and generated text are accepted with logical module, origin kind, original/resolved path, content hash, parser version, capability, category, and stable origin identity. Bounded archive/notebook/generated sizes and path validation prevent traversal. Per-source syntax/read failures preserve unaffected findings, skipped sources retain reasons in the scan summary, and the summary classifies source, test, generated, notebook, archive, metadata, resource, native, and unclassified files. REPL/source-free and native-extension bodies remain Partial or Unsupported.",
    },
    {
        "area": "Static analysis",
        "feature": "Project metadata and staleness",
        "status": "Partial",
        "details": "Recognised metadata covers pyproject.toml, requirements includes/constraints, setup.py/setup.cfg, Pipfile, Pipfile.lock, poetry.lock, uv.lock, and a bounded Conda environment subset without executing project code. Dependency groups, markers, extras, direct URLs/paths, editable flags, source files, statuses, and limitations persist separately from imports. Unrecognised root metadata is preserved by hash as unsupported. Scan inputs hash effective discovery settings, interpreter identity, metadata files, and source units. Staleness reports source, metadata/lockfile, discovery, and interpreter causes with affected capabilities. Generated text cannot be revalidated from a hash alone.",
    },
    {
        "area": "Static analysis",
        "feature": "Python-aware bindings, references, and usage",
        "status": "Partial",
        "details": "Lexical scopes and definitions cover modules, functions, classes, comprehensions, parameters, locals, constants, imports, and global/nonlocal declarations. Aliases, forwarding imports, re-exports, __all__, wildcard imports, overloads, inherited attributes/descriptors, conditional imports, and TYPE_CHECKING are preserved. References, calls, and definitions retain a binding path or explicit unresolved/conditional/type-checking outcome. Standard-library and installed source locations come from interpreter search paths without importing target modules. Third-party bodies are not traversed transitively by default. Dynamic imports, eval/exec, reflection, monkey patching, and unresolved attributes are separate boundary facts.",
    },
    {
        "area": "Static analysis",
        "feature": "Static paths, impact, data flow, slices, architecture, and control flow",
        "status": "Partial",
        "details": "Results are conservative source relationships, not proof that code executes. Impact traverses upstream and downstream calls, resolved references, import dependants, inheritance users, configured entry points, and affected resources with an explicit direction and bounded truncation. Data flow links resolved positional/keyword arguments and return assignments and correlates literal resource sinks. Slices combine source-scoped def-use records with resolved interprocedural edges; attribute, subscript, mutation, dynamic alias, unknown index, reflection, and indirect-call boundaries remain explicit. Control flow models branches, loop break/continue, return/raise, try/except/else/finally, with/async-with, match, yield, and conservative call-exception edges under node and edge budgets. Runtime branch selection and dynamic dispatch are not proven.",
    },
    {
        "area": "Static analysis",
        "feature": "Structural complexity, algorithms, and advanced Python facts",
        "status": "Partial",
        "details": "Cyclomatic count remains separate from a deterministic time and space estimate. Straight-line work, loop nesting, sorting/iteration/reduction calls, comprehensions, resolved project calls, recursion, and unknown external calls are labelled with assumptions; unsupported or recursive bounds return unknown. Resource classes distinguish filesystem paths, configuration, structured data, text, templates, media, serialization, compression, packages, databases, services, queues, environment, interpreter use, diagnostics, and subprocesses, including literal, f-string/template, and unresolved dynamic path expressions. Advanced syntax records decorators and targets, generators, async constructs, context managers, closures, descriptors, properties, metaclasses, dynamic attributes, concurrency creation, native boundaries, and heuristic algorithm patterns. No sensitive-data taint analysis is performed; runtime execution is never claimed.",
    },
    {
        "area": "Runtime",
        "feature": "Controlled local CPython tracing and debugging",
        "status": "Partial",
        "details": "Explicit child-process sessions support project calls, lines, returns, exceptions, breakpoints, tracepoints, watchpoints, threads, and available asyncio task identity. Values are cycle-safe, carry per-run object identity, and distinguish binding creation, rebinding, deletion, in-place mutation, and native/opaque state under configurable depth/item/byte budgets. Debugger expressions use a restricted read-only grammar that rejects calls and dunder traversal. Breakpoint conditions bind to source hash, file, line, and optional symbol, while per-thread pause/step controls prevent one thread consuming another's command. Failure reports correlate the final traceback with trace evidence, distinguish the terminating exception from bounded observed exceptions, and preserve unknown when evidence conflicts.",
    },
    {
        "area": "Runtime",
        "feature": "Process, thread, task, and wait correlation",
        "status": "Partial",
        "details": "Each run carries stable run/process/thread identities across instrumented Python children. Root and child process events include PID, PPID, executable, argv, lifecycle, instrumentation status, and parent process identity. Thread start/finish and observable asyncio task creation/completion plus coroutine suspend/resume are recorded. Wait adapters cover thread joins, queues, futures, threading conditions/semaphores, asyncio locks/events/semaphores/conditions/queues, and asyncio wait operations. Duration-based long waits and overlapping blocked waits are reported as observations or low-confidence heuristics, never scheduler proof. Native locks, uninstrumented/non-Python children, ownership, wakeup causality, and race conditions remain explicitly unobserved.",
    },
    {
        "area": "Runtime",
        "feature": "Instrumentation selection, event schema, and profiling",
        "status": "Partial",
        "details": "A versioned event schema covers call, line, return, yield/resume, exception lifecycle, import, C call, audit, process/thread/task, resource, and profiling categories. The selector reports sys.settrace, sys.monitoring availability, sys.setprofile, audit hooks, adapters, and coverage.py capability per run; monitoring reports its frame-local settrace fallback instead of claiming unsupported locals. Project code is the default recording scope, with an explicit reached-third-party expansion and separate presentation filters. Event journals are streamed under event budgets with visible truncation. Per-function call counts, observed durations, cumulative time, hot paths, and supported child-process peak memory are reported without treating tracer overhead as target-only time. Pause/inspect, fresh-timeout continuation, graceful stop, and emergency process-group stop are available.",
    },
    {
        "area": "Runtime",
        "feature": "Native trace-derived coverage",
        "status": "Partial",
        "details": "Stored line events produce source-checked line/function coverage and directly observed branch bodies. A compatible coverage.py SQLite database is imported separately with line granularity and explicit provenance; generated code and unobservable branch outcomes are not supported, and external coverage is never merged into native coverage.",
    },
    {
        "area": "Runtime",
        "feature": "Runtime resource adapters",
        "status": "Partial",
        "details": "Explicit traces support bounded, redacted HTTP, SQLite/SQLAlchemy/Django, filesystem, and JSON/pickle events with initiating frames and timing. Safe wrappers publish final completed/failed status after return, while audit-only evidence is attempted and correlated. URL credentials/query values, headers, cookies, database parameters, sensitive environment values, exceptions, bytes, and nested structures are redacted by default with explicit truncation. Native/unverified drivers, packet inspection, and operations without a project Python origin are unavailable.",
    },
    {
        "area": "Runtime",
        "feature": "Local Host/Listener sessions",
        "status": "Partial",
        "details": "A Runtime Host preserves the target terminal, requires explicit execution confirmation, heartbeats its session every ten seconds, and stops the target process group with a bounded SIGTERM-then-SIGKILL fallback. The read-only Listener follows running Unix-socket sessions in event pages capped at 200 events and 256 KiB, reports dropped/oversized events, journal truncation, pending lag, and transport errors, and permits one listener with a two-second takeover expiry. Session directories are current-user checked and expire after 24 hours. Windows Host/Listener IPC is not implemented; Windows Host retains a bounded local journal.",
    },
    {
        "area": "Runtime",
        "feature": "Existing-process attachment and child-process inheritance",
        "status": "Unsupported",
        "details": "Process discovery is read-only. Attachment and multiprocessing correlation/instrumentation inheritance are not implemented.",
    },
    {
        "area": "Hybrid",
        "feature": "Copy-based patches, callable harnesses, controlled substitutions, replay, and comparisons",
        "status": "Full",
        "verified": True,
        "details": "The documented Hybrid workflow is implemented: private workspaces, callable/class sessions, typed and recorded inputs, live line overrides, descriptor-aware syntax-checked atomic patches with hash history, substitutions, mocks, HTTP/SQLite/filesystem/network/process adapters, generated input cases, deterministic replay, side-effect evidence, source-revision-checked baseline comparison, complete workspace change reports, verified experiment cleanup on project removal, persistence, and export. Comparisons run fresh baseline and modified copies in separate child processes and record both source revisions, configurations, and interpreters. Each selected callable reports Full/Partial support; closure cells and native implementations expose their Python limits. A Hybrid process is controlled but is not an OS security sandbox.",
    },
    {
        "area": "Interfaces",
        "feature": "Configuration and persistence lifecycle",
        "status": "Partial",
        "details": "A versioned nonsecret YAML subset validates storage, discovery globs, profiles, TUI/browser settings, runtime budgets, exports, and local AI settings; duplicate, unknown, secret-looking, and invalid fields are rejected. Per-project preferences live in the database and override global defaults. Storage usage, quota thresholds, explicit retention pruning, trace-saving policy, and confirmed deletion of traces, reports, projects, or all local records are available. Older schemas are backed up and restored if migration fails. SQLite uses WAL, foreign keys, a five-second busy timeout, and restrictive local permissions where supported.",
    },
    {
        "area": "Interfaces",
        "feature": "Terminal explorer and local browser companion",
        "status": "Partial",
        "details": "The browser companion is read-only, binds only to loopback, and presents stored static results through a single-threaded local server. Requests may briefly queue, and the interface does not control a target process. CLI report commands support text, JSON, Markdown, CSV, and HTML; architecture adds Mermaid and DOT. Unsupported command/format pairs and expected configuration/database/migration failures exit with code 2 without a traceback.",
    },
)


LIMITATIONS = {
    "Python source, symbols, imports, calls, entry points, and resources":
        ("Dynamic registration, route ordering, CLI runtime values, and framework configuration are not executed.",
         "Framework versions come from declared metadata, not the installed environment.",
         "Dynamic code, third-party environments, and indirect registration remain outside static proof."),
    "Source discovery and provenance":
        ("Native or source-free bodies stay opaque; notebook line numbers are cell-local and archive members require an explicit archive.",),
    "Project metadata and staleness":
        ("Conda YAML support is a documented line-based subset; generated text and complex dynamic metadata are not revalidated from hashes.",
         "Lockfile transitives are parsed where their schema exposes them, but environment resolution is not performed."),
    "Structural complexity, algorithms, and advanced Python facts":
        ("Time/space bounds are structural estimates, not measured performance; recursion, dynamic dispatch, and unknown external calls can leave them unknown.",
         "Advanced Python and resource facts are source syntax, not proof of runtime behaviour; sensitive-data taint analysis is outside v1."),
    "Python-aware bindings, references, and usage":
        ("Third-party symbol bodies are not traversed by default; dynamic rebinding can invalidate any static binding.",
         "Conditional and TYPE_CHECKING branches remain explicitly uncertain rather than runtime-proven."),
    "Static paths, impact, data flow, slices, architecture, and control flow":
        ("Paths, impact, data flow, and slices are bounded to stored or source-resolved relationships.",
         "CFG call-exception edges are conservative; runtime branch selection and dynamic dispatch are not proven."),
    "Controlled local CPython tracing and debugging":
        ("Tracing observes only the explicitly started child process and its instrumentable Python threads.",
         "Object IDs and history are per run and per observed summary; history navigation never restores process state.",
         "Watchpoints compare bounded summaries and may miss changes outside observable fields or between sampled events."),
    "Process, thread, task, and wait correlation":
        ("Native locks, uninstrumented Python children, non-Python children, and OS scheduler state are not observed.",
         "Deadlock labels are heuristic overlap findings, not ownership or wakeup proof; race findings are not inferred from wait timing."),
    "Instrumentation selection, event schema, and profiling":
        ("Mechanism availability depends on the target interpreter; sys.monitoring currently reports a settrace fallback for frame locals.",
         "Durations include tracer overhead and memory is child-process peak RSS when supported, not an allocation profile."),
    "Native trace-derived coverage":
        ("Only statement-start lines and directly observable branch bodies are reconstructed.",
         "Imported coverage.py data keeps separate provenance and is never merged into native coverage."),
    "Runtime resource adapters":
        ("Native drivers, packet/TLS inspection, and operations without a project Python origin are unavailable.",),
    "Local Host/Listener sessions":
        ("Windows IPC is unavailable, the listener is read-only, and a target can escape process-group cleanup by starting a new session.",),
    "Existing-process attachment and child-process inheritance":
        ("Discovery is read-only and does not attach to or instrument existing processes.",),
    "Copy-based patches, callable harnesses, controlled substitutions, replay, and comparisons":
        ("Closure-cell mutation and native implementation replacement remain Partial; execution is not an OS sandbox.",),
    "Configuration and persistence lifecycle":
        ("Quota thresholds are reported rather than automatically enforced; pruning and deletion are explicit local actions.",
         "Windows ACL and retention scheduler behaviour may differ from POSIX permissions."),
    "Terminal explorer and local browser companion":
        ("The browser is read-only and several planned TUI views and fallbacks remain work.",),
}


def capability_report(*, project: str | None = None, scan_id: str | None = None,
                      interpreter: str | None = None, run_id: str | None = None,
                      feature: str | None = None, entity: str | None = None) -> list[dict[str, Any]]:
    """Return capability status scoped to the requested analysis context."""
    scope = {name: value for name, value in (
        ("project", project), ("scan_id", scan_id), ("interpreter", interpreter),
        ("run_id", run_id), ("entity", entity),
    ) if value is not None}
    selected = feature.lower() if feature else None
    report: list[dict[str, Any]] = []
    for item in CAPABILITIES:
        if selected and selected not in f"{item['area']} {item['feature']}".lower():
            continue
        status = item["status"]
        if status == "Full" and not item.get("verified"):
            status = "Partial"
        result = {
            "area": item["area"],
            "feature": item["feature"],
            "status": status,
            "details": item["details"],
            "limitations": list(LIMITATIONS.get(item["feature"], ())),
            "scope": scope,
            "evidence_record": record(
                "unknown", "tested capability and documented limitation",
                source={"project": project, "scan_id": scan_id} if project or scan_id else None,
                run={"run_id": run_id, "interpreter": interpreter} if run_id or interpreter else None,
                limitations=LIMITATIONS.get(item["feature"], ()),
            ),
        }
        report.append(result)
    return report
