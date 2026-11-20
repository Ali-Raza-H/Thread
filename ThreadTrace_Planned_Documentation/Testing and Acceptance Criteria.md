# Testing and Acceptance Criteria

> [!note]
> This is a documentation placeholder for future verification criteria, not a delivery roadmap.

## Required coverage areas

- Static-analysis correctness for Python syntax, imports, symbols, dependencies, entry points, resources, and explicitly uncertain findings.
- Dynamic tracing accuracy for calls, lines, returns, exceptions, coverage, and source-event correlation.
- Hybrid experiment isolation, reversibility, callable/class persistence, typed/recorded inputs, live overrides, alias-aware patches, substitutions, dependency and HTTP/database/filesystem/network/process adapters, input generation, deterministic replay, source protection, side-effect evidence, export, and comparison reports.
- Compatibility tests across supported Python versions/interpreters and the Full/Partial/Unsupported model.
- Safety tests for redaction, target termination, confirmation flows, and local-interface access controls.
- Performance benchmarks at each progressive-instrumentation level.
- UI interaction tests for TUI resizing, keybindings, accessibility of command-palette actions, and browser report rendering.
