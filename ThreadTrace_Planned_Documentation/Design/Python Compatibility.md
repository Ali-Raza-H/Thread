# Python Compatibility

> [!warning]
> CPython is the v1.0 dynamic/hybrid reference implementation. Exact supported versions are those verified by the tracing test suite; static parsing may have a wider, parser-dependent range.

## Requirements

- Document supported CPython versions and implementations explicitly.
- Identify features dependent on version-specific instrumentation such as `sys.monitoring`.
- Record the support level for language/runtime features, native extensions, dynamically generated code, concurrency, process boundaries, and alternative interpreters.
- Link each limitation to [[Capability Model]] as Full, Partial, or Unsupported.

## Compatibility boundaries

- PyPy, GraalPy, MicroPython, and IronPython may receive parseable-source Static Analysis, but runtime instrumentation is Unsupported until independently tested and enabled.
- Native extensions are opaque boundaries; Python-visible calls, returns, and exceptions may still be recorded when an enabled instrumenter exposes them.
- Syntax failures preserve unaffected scan findings and produce per-file warnings. Parser-level recovery is used only where feasible.
- Namespace packages, safe zip-source archives, generated Python text, and notebook Python text are supported where source text is available. REPL/source-free contexts are Partial or Unsupported unless explicitly instrumented.
- `sys.monitoring` may fall back to other compatible tracing/profiling tools when unavailable; capability status must make the resulting event differences visible.

See [[v1.0 Product Decisions#Compatibility and source model]] and [[Instrumentation Research]].
