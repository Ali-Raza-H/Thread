# ThreadTrace

ThreadTrace 0.7.0-alpha.1 is a terminal-first, local static analysis tool for Python projects. It reads source with Python's AST parser and never imports or runs the project being analysed.

See the complete [user manual](USER_MANUAL.md) for installation, every command,
the terminal explorer, local Ollama explanations, runtime-tracing safety, and
troubleshooting.

ThreadTrace is licensed under [GPL-3.0-only](LICENSE).
See [CHANGELOG.md](CHANGELOG.md) for the canonical release history.

It reconstructs evidence-backed entry points, architecture components, static
call paths, direct argument/return flow, and external resource boundaries. A
relationship is always labelled `confirmed`, `high`, `possible`, or `unknown`;
ThreadTrace does not turn a dynamic Python guess into a fact.

## Quick start

```console
threadtrace project add ~/code/example
threadtrace scan example
threadtrace modules example
threadtrace functions example
threadtrace deps example app.core --depth 2
threadtrace diagnostics example
threadtrace overview example --format markdown
threadtrace entrypoints example
threadtrace architecture example --format mermaid
threadtrace path example "POST /users"
threadtrace resources example --kind database
threadtrace impact example app.services.users.register --direction both
threadtrace dataflow example app.routes.create_user --to database
threadtrace slice example app.routes.create_user token --direction backward
threadtrace control-flow example app.routes.create_user
threadtrace complexity example app.core.Service.process
threadtrace advanced example app.core.Service.process
threadtrace map example "POST /users"
threadtrace references example app.core.Service.process
threadtrace usage example app.core.Service.process
threadtrace boundaries example
threadtrace metadata example
threadtrace staleness example
threadtrace trace-profile example TRACE_ID
threadtrace trace-concurrency example TRACE_ID
threadtrace trace-events example TRACE_ID --kind line --path app/
threadtrace findings example
threadtrace capabilities example
threadtrace browser example
```

Scans include ordinary and namespace packages, and can explicitly add external Python files, safe zip members, notebook code cells, and named generated text with `--source` and `--generated`. Each source records its origin, resolved path, parser version, capability, and category; rejected sources keep a reason in the scan summary. References and calls carry lexical/import binding paths, with conditional, type-checking, dynamic, and unresolved cases kept explicit. Directional impact follows calls, imports, inheritance, references, entries, and resources; slices use scoped def-use records; CFGs cover normal and exceptional control flow under explicit budgets; and structural complexity explains its assumptions separately from cyclomatic count. Advanced records cover decorators, generators, async/context-manager constructs, closures, descriptors, properties, metaclasses, dynamic attributes, concurrency, and native boundaries; resources distinguish configuration, structured data, text, media, serialization, compression, services, environment, and interpreter use. Runtime events use a versioned schema with instrumentation-plan reporting, separate recording/presentation scope, optional reached-third-party capture, per-function timing, supported memory metrics, and separate coverage.py provenance. Framework routes and commands require a matching import and bound registration object, so unrelated decorators stay generic. Scans also parse project metadata/lockfiles and record input hashes so staleness is reported by cause and affected capability.

Use `--format json` with query commands for machine-readable output. Scans are explicit; query commands use the most recently completed scan. Public results carry a versioned `evidence_record` with its class, mechanism, source/run reference, limitations, and legacy confidence label.

## Terminal explorer

Use `explain` for a concise evidence-grounded description and `compare` to
compare two completed scan IDs. JSON is the stable integration format.
Reports can also be exported as Markdown, CSV, self-contained HTML, Mermaid,
or DOT where the selected result supports a graph. Unsupported command/format
pairs fail with exit code 2 rather than falling back to text.

## Local browser companion

After a scan, start a read-only browser view with:

```console
threadtrace browser example
```

ThreadTrace binds only to `127.0.0.1` by default and prints a local URL for
you to open in a browser of your choice. Use `--host ::1` for IPv6 loopback or
`--port PORT` to choose a specific local port. The view presents stored static
results only; it cannot run or control the target program.

Run `threadtrace config init` to create the versioned nonsecret configuration
for storage, discovery, TUI/browser preferences, runtime budgets, exports, and
local AI settings. Per-project overrides use `threadtrace project preferences`.
Use `threadtrace storage` for usage and explicit retention pruning.

Runtime correlation is optional and never happens during a scan or TUI launch.
It runs only a command you explicitly provide, for example
`threadtrace trace example --yes-run -- python -m app`; it is bounded by a
timeout/output cap but is **not** a security sandbox. Captured runtime events
are stored separately and labelled `runtime`.

Hybrid experiments create private project copies for explicit function/class
sessions, temporary patches and substitutions, live value overrides, dependency
adapters, generated inputs, deterministic replay, and behavioural comparison:

```console
threadtrace experiment create example trial
threadtrace experiment configure example trial --config '{"seed":7,"freeze_time":0}'
threadtrace experiment call example trial app.core:run --args '["input"]' --yes-run
threadtrace experiment compare example trial app.core:run --args '["input"]' --yes-run
```

See the user manual for typed inputs, persistent instances, HTTP/SQLite/file
adapters, line overrides, run replay, capability inspection, and export.

## Optional AI explanations

ThreadTrace can ask a small local Ollama model to explain its *stored evidence*;
it never automatically sends repository files or runs AI during scans. The
response is labelled AI-assisted and instructed to cite source locations.

```console
# Local Ollama (default model: llama3.2)
threadtrace explain example app.routes.create_user --ai
threadtrace explain example app.routes.create_user --ai --ai-model qwen2.5:7b
```

Set `THREADTRACE_OLLAMA_ENDPOINT` to override Ollama's local URL. In the
explorer, `:ai` requests an explanation for the selected result using Ollama.

## Library context

`threadtrace libraries PROJECT` now includes a concise local description plus
the exact project files and import sites that use each library.

Open a keyboard-first, full-screen explorer for a completed scan:

```console
threadtrace tui example
```

It opens on the documented static overview. Use entry points, architecture,
paths, source inspection, and focused graphs to investigate a project. Press
`/` to search, `:` for commands, `r` to rescan, and `q` to quit. The explorer
is built with `prompt_toolkit`, not Textual.

Press `p` for the **File Graph**: files are nodes and labelled edges represent
imports or cross-file calls. In the graph, press `t` to switch to the **Code
Graph**, where functions/classes are nodes and links show calls, class-method
membership, and resolvable inheritance. Graphs stay centred on the selected
node; use `j`/`k` to follow links, `+`/`-` to change the hop scope, `[`/`]` to
filter confidence, and `,`/`.` to filter relationship type.

## Step-by-step runtime dashboard

For a controlled view of one real execution, give ThreadTrace the exact command that
starts the project:

```console
threadtrace runtime example -- python -m app.cli import-data sample.csv
```

The dashboard opens before the command runs. Press `s` to start paused, `n` to
advance one project-level execution event, or `c` to continue. If a
line-oriented program calls `input()`, press `i`, type into **Program input**,
and press Enter to send a line without handing over the dashboard terminal. It
shows calls, source lines, cycle-safe and identity-linked value summaries,
exceptions, and bounded process output. `B` adds a read-only conditional
breakpoint, `t` a tracepoint, and `w` a watchpoint; unsafe expressions are
rejected. Breakpoints carry a source hash and step controls target the selected
thread. Supported HTTP, SQLite/database, filesystem, and JSON/pickle boundaries
appear in the same timeline with redacted metadata by default. Use `threadtrace trace-resources PROJECT TRACE_ID` for a saved
side-effect timeline, or opt into bounded payload previews with
`--capture-level content`. `trace-concurrency` correlates stable process,
thread, task, and observed wait identities across instrumented Python children;
native locks, uninstrumented/non-Python children, and races remain explicitly
unobserved. Runtime mode executes code and is not a security sandbox.

## Running-process discovery

On Linux, ThreadTrace can safely discover Python interpreters owned by your current
user:

```console
threadtrace processes --format json
threadtrace process
threadtrace process 12345
```

`process` opens a confirmation screen with the PID, command, inferred project
root, detected venv, and descendants. From there you can scan the selected
project normally, or explicitly stop its process tree and restart the exact
command under the Runtime dashboard. ThreadTrace does not attach to or inject into
an already-running interpreter in this release.

## Live observation for interactive TUIs

For an interactive Python TUI, use Runtime Host in the terminal that should own
the program, then open a passive listener in a second terminal:

```console
# Terminal A — ARIA retains its real TUI and input/output.
threadtrace runtime-host ARIA --yes-run -- dev run aria

# Terminal B — ThreadTrace follows it without pausing or hijacking ARIA.
threadtrace runtime-listen ARIA
```

On Linux, the host publishes safe project-root trace events through a private
current-user Unix-domain socket in bounded pages and reports dropped events or
listener lag. Sessions expire after 24 hours, and Windows Host/Listener IPC is
not yet available. The listener is read-only, so ARIA continues
normally while ThreadTrace shows live calls, source lines, bounded
identity-aware variable summaries, returns, and exceptions. History navigation
never restores the process to an earlier state.

## Scope and limits

ThreadTrace reports static evidence: definitions, imports, syntactic calls, class relationships, and local data movement. Python can select values and call targets dynamically, so uncertain calls are shown as ambiguous or unresolved rather than guessed. A data-flow view is intra-function and does not claim to track values through arbitrary runtime behaviour.

