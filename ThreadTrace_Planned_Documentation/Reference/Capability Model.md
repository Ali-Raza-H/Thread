# Capability Model

ThreadTrace exposes support levels per capability, interpreter, and target context.

| Level | Meaning |
| --- | --- |
| Full | Expected to work with documented constraints. |
| Partial | Some data or target contexts are unavailable, approximate, or adapter-dependent. |
| Unsupported | Not currently available or technically inaccessible in the selected context. |

Support status should accompany features affected by Python version, interpreter choice, native extensions, dynamic code, concurrency, subprocess boundaries, unavailable adapters, and configured capture levels. It complements—not replaces—evidence labels in [[System Architecture#Evidence and confidence]]. Every feature also records its supported frameworks, fallbacks, known limits, and acceptance tests.

The verified v1 baseline (CPython 3.11, 2026-11-26) is recorded in [[Capability Audit]].
