# Safety and Performance

## Execution safety

- Treat unknown target programs as potentially untrusted.
- Require appropriate confirmation before executing unfamiliar code and display likely side effects before experimental runs.
- Support timeouts, termination, subprocess isolation, and scratch copies of data files where appropriate.
- Restrict access to local analysis interfaces.
- Redact secrets and sensitive values in captured traces and process output,
  including nested values, URL credentials/query values, headers/cookies,
  database parameters, sensitive environment-derived values, and exception
  messages. Truncation remains explicit. Unredacted local capture requires a
  separate confirmation.
- Document the real limits of isolation and sandboxing.

Normal runs are child subprocesses with the invoking user's privileges; they are not sandboxed. Pre-run side-effect detection and Python-level network hooks are best-effort only. Optional OS/container sandboxing is the enforcement mechanism for untrusted code, filesystem restrictions, or reliable network denial.

## Instrumentation limitations

ThreadTrace cannot guarantee every feature for every Python program. Limitations include native C extensions, generated code, heavily concurrent programs, process-isolation boundaries, and code that modifies its own environment. The interface should show Full, Partial, or Unsupported capability rather than implying certainty; see [[Capability Model]].

## Performance controls

- Instrumentation level, filters, duration, event-count limits, and storage limits should be configurable.
- Trace only selected third-party behaviour rather than whole libraries by default.
- Distinguish filtering a record from changing target execution.
- Explain capture cost and possible behavioural effects in run metadata.

Benchmark representative fixtures before setting release thresholds for scan speed, tracing overhead, memory, graph rendering, search, and UI response. Long traces use streaming storage, pagination, and bounded UI buffers rather than a fixed project-size limit.

## Behavioural integrity

Instrumentation aims to avoid intentional semantic changes, but timing, scheduling, and performance can still change. Hybrid mode makes controlled changes explicitly, and must restore original behaviour after experiments. See [[System Architecture#Behavioural integrity]] and [[Hybrid Analysis]].
