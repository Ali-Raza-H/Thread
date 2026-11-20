# Changelog

This file is the canonical release history for ThreadTrace. Package metadata
and `threadtrace --version` use the same version.

## 0.7.0a1

- Initial alpha release of static project reconstruction and controlled local
  runtime tracing.
- Guided static exploration, path reconstruction, file/code graphs, lessons,
  scan comparison, and explicit runtime sessions.
- Read-only loopback browser companion, nonsecret local configuration, and
  capability-status reporting.
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
