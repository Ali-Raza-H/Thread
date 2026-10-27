# Thread user manual

Thread 0.7.0-alpha.1 is a local-first reverse-engineering tool for Python projects. It builds
an evidence-backed model of an unfamiliar codebase without importing or running
the project during a normal scan.

It can identify symbols, imports, likely entry points, components, direct call
paths, bounded data movement, external resources, dependencies, cycles, and
static-analysis gaps. Its optional AI feature uses a local Ollama service only.

## Contents

- [Install and start](#install-and-start)
- [Core workflow](#core-workflow)
- [Understanding results](#understanding-results)
- [Commands](#commands)
- [Terminal explorer](#terminal-explorer)
- [Ollama explanations](#ollama-explanations)
- [Explicit runtime tracing](#explicit-runtime-tracing)
- [Exports and automation](#exports-and-automation)
- [Storage and scan history](#storage-and-scan-history)
- [Safety and limitations](#safety-and-limitations)
- [Troubleshooting](#troubleshooting)

## Install and start

Thread requires Python 3.11 or later. From the Thread repository:

```console
uv venv
uv pip install -e .
```

Or, using regular Python tooling:

```console
python -m venv .venv
.venv/bin/python -m pip install -e .
```

Confirm the installation:

```console
threadtrace --version
threadtrace --help
```

The default database location follows the platform convention:

- Linux: `$XDG_CONFIG_HOME/thread/thread.sqlite3`, or `~/.config/thread/thread.sqlite3`
- macOS: `~/Library/Application Support/thread/thread.sqlite3`
- Windows: `%LOCALAPPDATA%/thread/thread.sqlite3`

Pass `--database PATH` to any command when you want a project-local, temporary,
or CI-specific database.

## Core workflow

Register a project once, then scan whenever its source changes:

```console
threadtrace project add ~/code/example-api --name example-api
threadtrace scan example-api
threadtrace overview example-api
threadtrace guide example-api
threadtrace tui example-api
```

For an unfamiliar project, start with the guided overview:

```console
threadtrace guide example-api
```

It presents likely starting points, the important parts of the project, and
findings that could limit analysis. Choose an entry-point name or ID from its
output and follow it without needing to interpret the lower-level tables:

```console
threadtrace walk example-api "POST /users"
threadtrace map example-api "POST /users"
threadtrace findings example-api
```

For a more technical investigation, use:

```console
threadtrace overview example-api --format markdown
threadtrace entrypoints example-api
threadtrace architecture example-api --format mermaid
threadtrace resources example-api
threadtrace diagnostics example-api
```

Then choose a route, command, worker, or script and follow it:

```console
threadtrace path example-api "POST /users"
threadtrace impact example-api app.services.users.register --depth 4
threadtrace dataflow example-api app.routes.create_user --to database
```

## Understanding results

### Evidence

Every reconstructed relationship is backed by source evidence where Thread can
identify it. Evidence includes a kind such as `call`, `import`, `decorator`, or
`configuration`, along with a file and line number. Use it to verify a result
before relying on it.

### Confidence

Thread uses four consistent confidence labels:

| Label | Meaning |
| --- | --- |
| `confirmed` | Directly represented in source or an explicit runtime trace. |
| `high` | Strong static evidence supports the relationship. |
| `possible` | Plausible but incomplete static resolution. |
| `unknown` | Thread cannot safely infer the relationship. |

For example, a directly imported helper call can be `confirmed`; a dynamic
dispatch branch should remain `possible` or `unknown`.

### What a scan does

A normal scan:

1. Recursively discovers eligible `.py` files while excluding virtual
   environments, build output, VCS data, caches, and common generated folders.
2. Parses files with Python's AST parser; it does **not** import target modules.
3. Stores immutable scan facts in SQLite.
4. Reconstructs entry points, relationships, components, resources, data edges,
   diagnostics, and an overview.

Unchanged parsed files are reused from the previous compatible scan, while
changed files are parsed again.

## Commands

All query commands use the latest completed scan. If files changed after the
scan, Thread prints a staleness warning; run `threadtrace scan PROJECT` to
refresh the snapshot.

### Project registration

```console
threadtrace project add PATH [--name NAME]
threadtrace project list
threadtrace project show PROJECT
threadtrace project remove PROJECT --yes
```

`PROJECT` can be its registered name or project ID. Removing a project only
removes Thread's registration and stored scans; it never deletes source code.

### Scanning and history

```console
threadtrace scan PROJECT
threadtrace scan-history PROJECT
threadtrace compare PROJECT SCAN_A SCAN_B
```

`scan` prints a summary including parsed, failed, skipped, and reused file
counts. `compare` reports added and removed entry points, components, resources,
and findings between two completed scan IDs.

### Project reconstruction

```console
threadtrace overview PROJECT
threadtrace entrypoints PROJECT [--kind KIND]
threadtrace architecture PROJECT
threadtrace resources PROJECT [--kind KIND]
threadtrace explain PROJECT TARGET
threadtrace lesson PROJECT SYMBOL
threadtrace guide PROJECT
```

Entry-point kinds include `script`, `module`, `console`, `argparse`, `cli`,
`route`, `worker`, and `test`.

Thread detects conventional evidence for:

- `if __name__ == "__main__"` scripts and package `__main__.py` files;
- package console scripts declared in `pyproject.toml`;
- argparse parsing sites;
- Click/Typer-style command decorators;
- FastAPI/Flask-style route decorators;
- simple Django `path` and `re_path` declarations;
- task/worker decorators; and
- test functions separately from production entry points.

Resource kinds are `database`, `http`, `file`, `queue`, `env`, and `subprocess`.
They are conservative recognitions of known syntactic patterns, not proof that
an external operation completed.

`lesson` is a local, deterministic teaching card for one function, method, or
class. It combines purpose, source range, inputs/outputs, direct calls, callers,
and only patterns visibly present in that definition (such as comprehensions,
sorting, aggregation, recursion, or error handling). It does not use Ollama and
does not claim a performance bound.

### Guided investigation

```console
threadtrace guide PROJECT
threadtrace walk PROJECT ENTRYPOINT [--depth N] [--confidence LEVEL]
threadtrace map PROJECT [TARGET] [--depth N]
threadtrace findings PROJECT [--details]
```

`guide` is the beginner-facing starting point. It uses only the latest static
scan to describe likely ways into the program, major components, and the most
important limits on the analysis. It does not call Ollama.

`walk` turns the existing static call graph into a numbered explanation of one
supported path. It states whether each connection is directly visible in source
or merely possible. It cannot prove dynamic dispatch, plugin loading, or a
runtime branch that the source does not expose.

`map` lists the project files connected to an entry point, symbol, or module,
and why each file is relevant. Omit `TARGET` to see the project-level file map.

`findings` defaults to issues that can affect understanding of the program,
such as unreadable files, import cycles, dynamic operations, and unknown
imports. Routine unresolved-call records are left out of this beginner view.
Add `--details` for the full technical list.

### Paths, impact, and data flow

```console
threadtrace path PROJECT ENTRYPOINT [--depth N] [--confidence LEVEL]
threadtrace impact PROJECT SYMBOL [--depth N]
threadtrace dataflow PROJECT SYMBOL [--to RESOURCE_KIND]
```

`path` begins at a detected entry point and follows resolved calls. It stops at
the selected depth, recursion, unresolved/dynamic boundaries, or external
resources. Use `--confidence confirmed` to hide inferred branches, or
`--confidence possible` for the normal balanced view.

`impact` follows outgoing static relationships from one exact, qualified symbol.
`dataflow` is intentionally bounded: it connects direct-call positional and
keyword arguments to resolved callee parameters, plus direct return assignments.
It is not whole-program taint analysis.

### Symbols and raw static facts

```console
threadtrace modules PROJECT
threadtrace deps PROJECT MODULE [--reverse] [--depth N]
threadtrace cycles PROJECT
threadtrace search PROJECT TERM [--kind KIND] [--module MODULE]
threadtrace symbol PROJECT SYMBOL
threadtrace callers PROJECT SYMBOL
threadtrace callees PROJECT SYMBOL [--depth N]
threadtrace functions PROJECT [--module MODULE]
threadtrace classes PROJECT [--module MODULE]
threadtrace inheritance PROJECT CLASS
threadtrace flow PROJECT SYMBOL
threadtrace libraries PROJECT
threadtrace diagnostics PROJECT [--severity LEVEL] [--rule RULE]
```

Use a qualified name for commands that require one exact symbol, such as:

```console
threadtrace symbol example-api app.services.users.UserService.register
```

`flow` shows intra-function parameters, reads, assignments, returns, and yields.
`diagnostics --include-unresolved-calls` includes every call Thread could not
resolve; this can be noisy in dynamic or dependency-heavy projects.

### Library context

`threadtrace libraries PROJECT` includes:

- the library name and category (`stdlib`, `third_party`, `declared`, or
  `unknown_external`);
- a short explanation of what the library generally does;
- declaration location where available; and
- each project file/import site using it.

Descriptions use a safe built-in catalogue for common Python libraries and
installed distribution metadata when available. An unavailable description is
reported plainly rather than guessed.

## Terminal explorer

Launch the full-screen explorer after scanning:

```console
threadtrace tui PROJECT
```

The explorer is built with `prompt_toolkit`, not Textual. It has a left
navigation pane, a result list, and an inspector for source evidence, paths,
library context, and AI-assisted explanations.

| Key | Action |
| --- | --- |
| `g` | Guided overview (the opening screen) |
| `1`–`9` | Choose one of the Guided mode starting points |
| `w` | In Guided mode, walk the selected entry point |
| `m` | In Guided mode, show files involved; otherwise modules |
| `c` | In Guided mode, open selected code; otherwise classes |
| `p` | Open the interactive File Graph, centred on the selected file when available |
| `t` | Switch between File Graph and Code Graph while viewing a graph |
| `j`/`k` or `Up`/`Down` | In a graph, move to a directly linked node |
| `+`/`-` | In a graph, increase/decrease the number of visible relationship hops |
| `[`/`]` | In a graph, narrow/broaden the confidence filter |
| `,`/`.` | In a graph, cycle relationship types (import, direct call, contains, inheritance) |
| `o` | Overview |
| `e` | Entrypoints |
| `a` | Architecture |
| `u` | Resources |
| `f` | Functions (and later guided function explanation) |
| `d`, `l`, `x` | Findings, libraries, import cycles |
| `/` | Search symbols and files |
| `:` | Command palette |
| `j`/`k` or arrows | Move selection |
| `Tab` | Move focus between search, results, and inspector |
| `PgUp`/`PgDn`, `Ctrl-B`/`Ctrl-F` | Scroll the focused inspector |
| `v` | Open the selected result's source file full screen |
| `z` | Open the selected result's complete analysis full screen |
| Mouse wheel | Scroll panes where supported by the terminal |
| `r` | Rescan |
| `?` | Show the key summary |
| `Backspace` | Return to overview |
| `q` or `Ctrl-C` | Quit |

The results and inspector panes show scrollbars. Press `Tab` until the inspector
has focus, then use arrows, `PgUp`/`PgDn`, `Ctrl-B`/`Ctrl-F`, or the mouse wheel
to read all of a long explanation. In the command palette, use:

```text
:overview
:guide
:entrypoints
:architecture
:resources
:scan
:ai
:quit
```

`Enter` keeps the selected item's inspector current. `:guide` returns to the
guided overview. `:ai` requests an Ollama explanation for the selected symbol,
entry point, library, or overview.

### Relationship graphs

The explorer has two focused graph pages designed for understanding a project
without displaying every relationship at once.

- **File Graph** (`p` or `:file-graph`) uses one node per parsed Python file.
  An arrow is an internal import or a resolved call that crosses a file boundary.
  Its label names the functions involved when Thread knows them.
- **Code Graph** (`t` from File Graph or `:code-graph`) uses one node per
  function, method, or class. Arrows show resolved/candidate calls, class-to-
  method membership, and statically recognisable base-class relationships.

Each graph is centred on the selected node. This acts like panning through the
project: follow a link with `j`/`k`, and the view recentres on that node. `+` and
`-` change how many hops around it are shown. The inspector explains the exact
edge type, confidence, source symbols, and recorded source evidence. Use the
filters before increasing scope on a large repository; this keeps the graph
useful instead of turning into an unreadable all-project diagram.

### Full-screen source and analysis views

The inspector is deliberately read-only. To study a result in more space, press
`v` when its selected row has a source location. For a function, method, or
class, Thread opens the **complete selected definition** in a full-screen,
scrollable code view—not just the inspector excerpt and not an arbitrary part
of its containing file. For a module/file selection it opens the full file.
Press `z` for a full-screen view of the inspector analysis when there is no
direct source file.

Source files open read-only by default. In a source view:

| Key | Action |
| --- | --- |
| `F2` | Enable/lock editing; a definition view edits only the selected definition |
| `F3` | Open the selected definition's containing file as a separate whole-file view |
| `Ctrl-S` | Save edited source to disk |
| `Ctrl-R` | Save and run a new static scan |
| `F10` | Discard unsaved edits and restore the originally opened content |
| `Esc` | Return to the explorer; protects against closing unsaved edits |

Thread only enables editor writes for a file resolved inside the registered
project root. Definition saves splice only that definition back into the source
file. Before every save, Thread checks that the on-disk file still has the same
content it opened; if another editor changed it, Thread refuses the save rather
than overwriting those changes. Saving never executes code, but it makes the
current analysis snapshot stale until you rescan. Use `Ctrl-R` to save and
refresh in one step.

## Ollama explanations

AI narration is optional and disabled unless you explicitly request it. Thread
uses a local Ollama server; it does not require an API key and does not send
source code to an external provider.

Start Ollama and ensure you have a model:

```console
ollama serve
ollama pull llama3.2
```

Then ask for an explanation:

```console
threadtrace explain example-api app.routes.create_user --ai
threadtrace explain example-api app.routes.create_user --ai --ai-model qwen2.5:7b
```

Configuration variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `THREADTRACE_OLLAMA_MODEL` | `llama3.2` | Default model when `--ai-model` is absent. |
| `THREADTRACE_OLLAMA_ENDPOINT` | `http://127.0.0.1:11434/api/chat` | Local Ollama chat endpoint. |

Thread gives Ollama only a bounded JSON representation of persisted Thread
facts: overview, the selected explanation target, library context, and recorded
evidence. The prompt requires concise Markdown, source citations where facts
have locations, and `unknown` for insufficient evidence.

An AI answer is interpretation, not source of truth. Verify it against the
linked source evidence before changing production code.

## Explicit runtime tracing

Normal scans never run target code. Runtime tracing is a separate, explicit
feature for correlating static facts with a command you choose:

```console
threadtrace trace example-api --yes-run -- python -m app.cli import-data sample.csv
threadtrace traces example-api
```

Options:

```console
threadtrace trace PROJECT --yes-run --timeout 30 --output-cap 100000 -- COMMAND [ARGUMENT ...]
```

The trace runner captures bounded Python call/return events within the project
root, associates the trace with the latest static scan when available, and
stores stdout/stderr with an output cap.

Runtime tracing is **not a security sandbox**. The command can execute code,
modify files, access credentials, or use the network according to its own
permissions. Only run commands you trust, preferably in an isolated environment.

### Step-by-step runtime dashboard

Use Runtime mode when you want to understand one execution slowly, rather than
only inspect a completed trace:

```console
threadtrace runtime PROJECT -- COMMAND [ARGUMENT ...]
threadtrace runtime example-api -- python -m app.cli import-data sample.csv
```

The command is required—Thread does not guess how a project should be started.
The dashboard opens without executing it. Controls are:

| Key | Action |
| --- | --- |
| `s` | Start the supplied command paused at its first project-level trace event. |
| `n` or `Enter` | Advance one call, source line, return, or exception event. |
| `o` | Step over nested calls and pause at the next event in the current frame. |
| `u` | Run until the current frame returns to its caller. |
| `c` | Continue until completion, a pause request, the configured timeout, or the event cap. |
| `p` | Request a pause at the next traced event. |
| `b` | Toggle a breakpoint at the selected source line. A breakpoint pauses a continued run before the next matching trace event. |
| `f` | Focus the selected asyncio task. Other task events are hidden and never pause the dashboard; press it again to show all lanes. |
| `i` | Focus **Program input**. Type one line and press Enter to send it to a line-oriented child prompt such as `input()`; press Esc to cancel the entry. |
| `j`/`k` or Up/Down | Inspect an earlier/later recorded event. |
| `Tab` | Move between the event timeline and the scrollable data/source inspector. |
| `q` | Stop the controlled subprocess and close the dashboard. |

In the Static explorer, select a function, method, or class and press `y` to
open the same lesson card in a full-screen, scrollable view.

The dashboard observes Python frames whose files resolve inside the registered
project root. For each event it shows the active function, source location,
safe inputs, variable creations/changes, return-value summaries, and exception
types. Strings are capped, secrets are redacted when their variable name is
recognised, and arbitrary objects are represented by type rather than calling
their `repr()`. This is deliberately useful for learning without indiscriminately
copying a program's memory into Thread's database.

Async projects label each event with its Python thread and, where Python can
identify one safely, its asyncio task. Use `f` on a task event to make a busy
event loop readable. This is an observation filter: it does not stop or alter
background tasks. Await-boundary-only stepping is not yet offered, because it
needs scheduler-level coordination to be correct.

Runtime mode can still execute untrusted code. It is not a sandbox, does not
isolate network/filesystem access, and cannot reliably observe native code,
other processes, or code that never enters a project-root Python frame.

For a full-screen or key-by-key interactive application, use `runtime-host` in
one terminal and `runtime-listen` in another. Direct Runtime's Program input is
intentionally limited to complete input lines so its own dashboard remains in
control of the terminal.

### Running-process discovery and restart debugging

On Linux, Thread can inspect the current user's already-running Python
interpreters without modifying them:

```console
threadtrace processes
threadtrace processes --format json
threadtrace process
threadtrace process PID
```

`processes` is read-only and lists the PID, interpreter, command, working
directory, inferred project root, and detected virtual environment. `process`
opens a confirmation-first selector. Select a process and press Enter to review
the exact action before Thread does anything.

| Confirmed key | Result |
| --- | --- |
| `s` | Register the inferred project root if needed, run a new static scan, then open the ordinary Thread explorer. The selected process keeps running. |
| `r` | Scan the inferred root, stop the selected current-user process and its descendants, then relaunch the recorded command under the Runtime dashboard with its detected PATH/venv. |
| `Esc` | Return to selection without acting. |

Restart debugging validates the selected PID's Linux start-time token before
signalling it, so a recycled PID is never targeted. It stops descendants before
their parent to reduce orphaned workers. The confirmation screen is mandatory;
there is no automatic termination or restart.

Thread cannot attach to or inject into a Python process that is already running
in this release. It can statically scan it immediately, or explicitly restart
it under Thread Runtime. Native/debugger attachment is future work because it
is platform-specific and far more invasive.

### Live observation of an interactive TUI

Do not use restart debugging for a program whose own terminal interface you
need to operate, such as ARIA. Instead, start it through a **Runtime Host** in
the terminal where it should remain interactive, and use a separate listener
dashboard:

```console
# Terminal A: ARIA keeps stdin, stdout, stderr, colours, and its normal TUI.
threadtrace runtime-host ARIA -- dev run aria

# Terminal B: Thread passively follows the host session.
threadtrace runtime-listen ARIA
```

`runtime-host` explicitly gives the child its original interactive terminal
streams rather than capturing them. ARIA's Urwid screen therefore appears in
Terminal A and receives your keyboard, resizing, colours, and Ctrl-C exactly as
if you had used `dev run aria` directly. Do not pipe or redirect the host
command. It publishes safe project-root trace events into a private current-user
local session directory. `runtime-listen` opens the newest matching session by
default; the host prints a session ID if you need to choose one:

```console
threadtrace runtime-listen ARIA --session SESSION_ID
```

The listener is deliberately read-only: it never pauses, types into, restarts,
or otherwise interferes with the program. It shows a timeline of calls, lines,
returns and exceptions, with readable variable changes, frame variables and
source context for the selected event. Values are bounded/redacted summaries,
not complete object contents. A line event occurs *before* the line executes, so
its effects appear at a later event. Source context is read from the current
file and can differ if the file has changed since execution.

| Key | Action |
| --- | --- |
| `j`/`k` or Up/Down | Inspect events in the timeline; stops following new events. |
| `l` | Jump to the newest event and resume live following. |
| `Tab` | Move between timeline and details; use arrow keys or PgUp/PgDn to scroll details. |
| `q` | Quit from the timeline; Ctrl-C quits from either pane. |

Current-frame variables are reconstructed from recorded changes for that frame,
so older sessions without frame IDs show only the individual changes. A program
already started with plain `dev run aria` cannot be observed until a future
attach feature exists; exit it and start it through Runtime Host instead.

## Exports and automation

Most commands support text or JSON output:

```console
threadtrace overview example-api --format json
threadtrace explain example-api app.main --format markdown
```

Architecture additionally supports documentation-friendly graphs:

```console
threadtrace architecture example-api --format mermaid > architecture.mmd
threadtrace architecture example-api --format dot > architecture.dot
```

The JSON output preserves confidence and evidence fields where the query has
them, making it suitable for CI artifacts or scripts. Mermaid and DOT should be
treated as views of the selected scan, not as replacement source evidence.

## Storage and scan history

Each completed scan is immutable. New scans do not overwrite old findings or
relationships; they create a new snapshot. This enables comparisons and lets
you inspect historical project structure.

To use a disposable database in a script or CI job:

```console
threadtrace project add . --name current --database /tmp/thread.sqlite3
threadtrace scan current --database /tmp/thread.sqlite3
threadtrace overview current --database /tmp/thread.sqlite3 --format json
```

To remove all stored data for one registered project, use:

```console
threadtrace project remove PROJECT --yes
```

It does not delete the SQLite database if it contains other projects, and it
never deletes the source project.

## Safety and limitations

Thread is designed to be conservative.

- It does not execute source code during ordinary scanning.
- It does not claim certainty for dynamic Python features such as `eval`,
  `getattr`, monkey patching, dynamic imports, plugins, and metaprogramming.
- It records dynamic boundaries as findings instead of making up call edges.
- Framework detection covers common static conventions; unusual registration
  patterns may be `possible`, `unknown`, or absent.
- Data-flow reconstruction covers direct resolved calls and returns only.
- Resource detection recognises common calls and imports; it does not prove a
  database write, request, or subprocess call succeeded at runtime.
- Ollama explanations are optional interpretations of recorded facts, not an
  authority over the evidence.
- Runtime traces cover application Python frames under the project root. Python
  virtual environments nested inside that root are excluded. Traces may be empty
  if the command bypasses Python startup hooks or runs only external/native code.

For reliable conclusions, start from `overview`, follow a path, and open the
source evidence for the claims you care about.

## Troubleshooting

### “Project has no completed scan”

Run:

```console
threadtrace scan PROJECT
```

### “Stored results may not match files on disk”

Run a new scan. Thread detected added, removed, or changed eligible Python
files.

### “The terminal explorer requires prompt_toolkit”

Install project dependencies again:

```console
uv pip install -e .
```

### Ollama request fails

Confirm that Ollama is running and the chosen model exists:

```console
ollama list
ollama serve
```

If Ollama runs on another local port or host, set
`THREADTRACE_OLLAMA_ENDPOINT` or use `--ai-endpoint`.

### A path has no steps

The entry point may have no resolved static handler, its next call may be
dynamic, or the selected confidence filter may hide candidate edges. Inspect
`threadtrace entrypoints PROJECT`, `threadtrace diagnostics PROJECT`, and the
entry point's source evidence.
