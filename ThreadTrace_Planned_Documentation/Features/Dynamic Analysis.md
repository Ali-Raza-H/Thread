# Dynamic Analysis

> [!summary]
> Execute and observe Python programs, inspect runtime state, and record behaviour without intentionally changing target semantics.

Related: [[Static Analysis]], [[Hybrid Analysis]], [[TUI Design]], [[Safety and Performance]].

## Execution management

- Launch scripts, packages, modules, and configurable commands under ThreadTrace instrumentation.
- Pass CLI arguments and environment configuration; use the project's chosen interpreter or virtual environment.
- Run through the TUI, in host mode, or in listener mode where another ThreadTrace instance connects to an instrumented host.
- Provide an interactive terminal for CLI/TUI applications while keeping target input/output separate from ThreadTrace's UI.
- Record execution start, stop, exit status, and failure information.

## Execution tracing

- Track executed Python lines, function/method calls, arguments, returns, observed return values, and call-stack changes.
- Record exceptions raised, caught, propagated, and—where observable—suppressed.
- Failure reports select the final traceback's terminating exception and
  correlate its innermost project frame with trace evidence. Earlier handled
  and propagated exceptions remain a bounded chronological evidence list.
- `SystemExit` and `KeyboardInterrupt` remain visible where observable. A
  timeout or unreconciled trace/traceback relationship is reported as
  `unknown`, not inferred from an unrelated earlier event.
- Identify code that actually executed and correlate events with static entities.
- Trace relevant third-party functions without indiscriminately tracing whole libraries.
- Record generated or dynamically imported code where instrumentation permits.

## Interactive debugging and navigation

- Pause/resume and step into, over, and out of functions.
- Set/remove normal and conditional breakpoints; support tracepoints that record without pausing.
- Support watchpoints for selected state changes where technically feasible.
- Move backward/forward through recorded history, inspect historical stacks and recorded values, and jump to selected events.

> [!warning]
> History navigation reconstructs recorded information; it does not necessarily restore the target process to an earlier real state.

## Variables and objects

- Inspect local/global variables, arguments, return values, and recorded changes.
- Inspect identity, type, attributes, relationships, nested collections, custom objects, class instances, and observable mutations.
- Track the observed lineage of selected values and associate values with producing/modifying events and functions.

## Timeline and reconstruction

- Maintain a chronological event timeline with timestamps, process/thread identifiers, and source locations.
- Reconstruct recorded call chains and state surrounding events.
- Search events by function, exception, variable, or type; bookmark moments and save selected state snapshots.
- Reconstruct the recorded sequence leading to a failure.

## Runtime control flow and coverage

- Record taken branches, observed loop iterations, executed lines/functions, and branch coverage where supported.
- Overlay observed paths on static control-flow graphs, highlight code not executed in a run, and compare coverage across runs.

## Concurrency and asynchronous execution

- Trace Python threads, creation/termination, asyncio tasks and coroutine suspension/resumption where observable.
- Track Python subprocesses, parent-child relationships, task/process relationships, and concurrent timelines.
- Identify observable locks, waits, and synchronisation events; provide diagnostics relevant to races and deadlocks.

## Network, database, and filesystem activity

- Track supported outbound HTTP requests/responses: URL, method, headers, payload, status, response data, timing, failures, initiating function, and call stack—subject to capture/redaction settings.
- Support other protocols through instrumentation adapters and detect associated serialisation/deserialisation.
- Track supported database queries, parameters where permitted, transactions, commits, rollbacks, and origin events.
- Track filesystem reads, writes, creations, renames, deletions, paths, metadata, and originating call chains.
- Audit-hook observations are `attempted` evidence unless a safe wrapper can
  observe the return or exception. Wrapper events use `completed`/`failed`, and
  an explicit correlation ID relates audit and final records without treating
  an attempt as success.
- Default persistence redacts URL user information and query values, headers,
  cookies, database parameters, sensitive environment-derived values,
  exception messages, bytes, and nested mappings. Every bounded preview makes
  truncation explicit; unredacted local capture requires confirmation.

## Runtime profiling

- Measure duration, call count, and cumulative execution time by function.
- Identify frequently executed functions and expensive paths.
- Track memory allocation/usage where supported, show performance alongside call graphs, and compare results across runs.

## Filters and limits

- Ignore selected modules, functions, libraries, or packages in tracing output.
- Default-filter standard-library and third-party code while allowing overrides.
- Configure depth, categories, duration, event-count, and storage limits.

> [!important]
> Ignoring code affects recording or presentation, not execution. Behavioural suppression belongs to [[Hybrid Analysis#Function disabling and substitution]].
