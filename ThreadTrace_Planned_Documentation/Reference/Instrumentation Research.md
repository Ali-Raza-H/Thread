# Instrumentation Research

> [!summary]
> A structured record of the maintainer's instrumentation research, distilled from [[Archive/Ali Spec Research|Ali Spec Research]]. It informs capability selection; it does not by itself guarantee support or performance.

Related: [[Dynamic Analysis]], [[Hybrid Analysis]], [[Python Compatibility]], [[Capability Model]], and [[v1.0 Product Decisions]].

## Mechanism comparison

| Mechanism | Strengths | Limits and v1.0 role |
| --- | --- | --- |
| `sys.monitoring` | Low-level interpreter event monitoring with configurable event categories. | CPython 3.12+ only. It does not by itself provide complete object/value capture or behavioural modification. Prefer where compatible and benchmarked. |
| `sys.settrace` | Access to Python frames; supports line-level debugging, pausing, and stepping. | High overhead, especially at fine event granularity. Current alpha baseline for controllable debugging. |
| `sys.setprofile` | Lower-overhead function entry/exit and profiling events. | No normal line-level visibility; use for profiling/call-oriented modes, not source stepping. |
| Audit hooks | Surface supported audited operations and raw arguments; hooks can raise to block actions. | Coverage is event-dependent and blocking changes target behaviour. Use for observation/policy only with explicit semantics. |
| AST transformation | Rewrites source AST before compiling it for an experiment. | Requires safe source parsing, traversal, location repair, compilation, and source mapping. Limited to explicit Hybrid workspaces. |
| Bytecode transformation | Can alter executable behaviour at a lower level. | Version-sensitive and difficult to map to source. Experimental/Partial unless an adapter proves reliable. |
| Library adapters | Provide high-level protocol/resource semantics without tracing irrelevant internals. | Adapter-specific, version-sensitive, and must be independently tested and maintained. |

## Design implications

- Choose instrumentation by requested event type, interpreter, target context, overhead budget, and required control semantics.
- Keep observation, filtering, blocking, and behavioural modification distinct. Blocking an audit event or transformed patch is not passive tracing.
- Use library adapters to improve useful runtime facts, while preserving a generic evidence boundary when no adapter exists.
- Benchmark each mechanism and adapter. Report Full, Partial, or Unsupported capability rather than claiming uniform coverage.
- AST/bytecode work must execute only in the temporary [[Hybrid Analysis]] workspace, keep source mappings, and never silently change the original project.
