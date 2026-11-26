# Changelog

This file is the canonical release history for ThreadTrace. Package metadata
and `threadtrace --version` use the same version.

## 0.7.0a1

- Release verification: a clean checkout installed with the documented `uv`
  flow on CPython 3.11.16 passed the offline suite (129 tests, 865 subtests),
  schema migration from version 1 through 17, export-version 1 import, the
  safety subset, the accuracy corpus, measured performance bounds, and every
  executable quick-start command. The sdist includes the licence and user
  documentation, and the wheel reports the same 20-row capability manifest.
- Initial alpha release of static project reconstruction and controlled local
  runtime tracing.
- Static exploration, path reconstruction, file/code graphs, lessons, scan
  comparison, and explicit runtime sessions.
- Terminal explorer with a compact single-pane fallback, configurable view
  keys and command palette, Unicode/ASCII and colour/no-colour modes, syntax
  highlighting, definition/reference navigation, and diff-confirmed,
  syntax-checked, atomic source editing that preserves view location on rescan.
- Read-only loopback browser companion with paginated dependency, call,
  class/module, AST, data-flow, control-flow, and execution-flow graphs,
  search/filter/focus/source navigation, Mermaid export, nonsecret local
  configuration, and capability-status reporting.
- Verification gates: deterministic offline fixtures for scripts, packages,
  namespaces, malformed projects, CLI and web frameworks, databases,
  concurrency, imports, exceptions, and adapters; a labelled call-resolution
  accuracy corpus; safety failures for corrupt databases and exports; measured
  scan/query/export/storage bounds; a capability-to-test manifest exposed by
  capability reporting; a `dev` pytest extra; and classifiers limited to the
  verified CPython 3.11 release.
- Packaging and documentation reconciliation: the distribution name changed
  from `thread-trace` to `threadtrace` to match the import package and CLI; the
  repository ships the complete GPL-3.0-only licence text; and an automated
  gate parses every command documented in the README and user manual and checks
  capability-audit statuses against the tested manifest.
- Portable exports and investigation workspaces: one versioned envelope with
  source hashes, capability status, limitations, and truncation for report,
  experiment, and investigation artifacts; investigations store typed
  sections with captured source hashes and snippets, resume with changed,
  moved, or missing reference warnings, and export/import without losing
  references; a read-only `ThreadTrace` Python API for projects, scans,
  queries, traces, investigations, and exports.
- Optional Ollama and OpenAI-compatible AI explanations with nonsecret
  provider settings, environment-only credentials, bounded best-effort
  redaction, per-payload approval for remote endpoints, validated JSON claims
  and Mermaid references, and persistent exchange history with explicit
  deletion.
- Full Hybrid experimentation with isolated callable/class sessions, live
  overrides, temporary patches and substitutions, dependency/adaptor mocks,
  generated inputs, deterministic replay, side-effect comparison, persistence,
  and export.
- Runtime resource adapters for supported HTTP clients, SQLite/database
  frameworks, filesystem operations, and JSON/pickle serialisation, with
  capture levels, pre-persistence redaction, failure evidence, and saved
  side-effect timelines.

## Release policy

Versions follow semantic versioning. Application, database schema, trace
protocol, and export formats are versioned independently. A stable v1.0
release requires the documented capability audit and migration guarantees.
