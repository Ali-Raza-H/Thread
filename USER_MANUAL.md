# ThreadTrace user manual

ThreadTrace 0.7.0-alpha.1 is a local-first reverse-engineering tool for Python projects. It builds
an evidence-backed model of an unfamiliar codebase without importing or running
the project during a normal scan.

It can identify symbols, imports, likely entry points, components, direct call
paths, bounded data movement, external resources, dependencies, cycles, and
static-analysis gaps. Its optional AI feature uses a local Ollama service only.

## Contents

- [Install and start](#install-and-start)
- [Core workflow](#core-workflow)
- [Source discovery](#source-discovery)
- [Name resolution and references](#name-resolution-and-references)
- [Project metadata and staleness](#project-metadata-and-staleness)
- [Understanding results](#understanding-results)
- [Commands](#commands)
- [Terminal explorer](#terminal-explorer)
- [Local browser companion](#local-browser-companion)
- [Ollama explanations](#ollama-explanations)
- [Explicit runtime tracing](#explicit-runtime-tracing)
- [Experiments](#experiments)
- [Exports and automation](#exports-and-automation)
- [Storage and scan history](#storage-and-scan-history)
- [Safety and limitations](#safety-and-limitations)
- [Troubleshooting](#troubleshooting)

## Install and start

ThreadTrace requires Python 3.11 or later. From the ThreadTrace repository:

```console
uv venv
uv pip install -e .
```

Development extras add `pytest`: `uv pip install -e ".[dev]"`. ThreadTrace is
verified on CPython 3.11; newer CPython releases and other interpreters keep
their documented static-only status until they pass the same automated suite.

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

- Linux: `$XDG_CONFIG_HOME/Thread/thread.sqlite3`, or `~/.config/Thread/thread.sqlite3`
- macOS: `~/Library/Application Support/Thread/thread.sqlite3`
- Windows: `%LOCALAPPDATA%/Thread/thread.sqlite3`

Pass `--database PATH` to any command when you want a project-local, temporary,
or CI-specific database.

Create the optional nonsecret configuration file with:

```console
threadtrace config init
```

`config.yaml` is a versioned, nonsecret YAML subset (`schema_version: 1`). It
supports `storage` (database path, retention days, trace-saving policy, quota,
usage reporting), `discovery` include/exclude globs, `profiles.default_interpreter`,
`tui` preferences, loopback `browser` settings, `runtime` budgets and capture,
`exports.default_format`, and provider-independent `ai` settings (never
credentials). Duplicate keys, unknown
sections or fields, invalid types, secret-looking keys, and non-loopback
browser hosts are rejected. The legacy top-level `database_path` key is still
accepted and mapped to `storage.database_path`. Use `threadtrace config show` to
inspect the active values and `threadtrace config path` to locate the file.

Per-project preferences stay in the database and override the global defaults
for that project. Preference content is validated and merged:

```console
threadtrace project preferences example-api
threadtrace project preferences example-api \
  --content '{"discovery":{"exclude":["vendor/*"]},"tui":{"compact":true}}'
threadtrace project preferences example-api \
  --content '{"tui":{"keybindings":["overview=n","quit=z"]}}'
```

Discovery include/exclude globs are applied by `scan`. Project preferences cannot
change the global database path.

## Core workflow

Register a project once, then scan whenever its source changes:

```console
threadtrace project add ~/code/example-api --name example-api
threadtrace scan example-api
threadtrace overview example-api
threadtrace tui example-api
```

For an unfamiliar project, inspect the overview and detected entry points:

```console
threadtrace overview example-api
threadtrace entrypoints example-api
```

Choose an entry point and inspect static paths, linked files, and findings:

```console
threadtrace path example-api "POST /users"
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

## Source discovery

`threadtrace scan` walks the registered project with safe default exclusions
and accepts ordinary packages and PEP 420 namespace packages without requiring
`__init__.py`. Directory symlinks are never traversed, and file symlinks that
point outside the project are skipped with a reason. Use `discovery.include`
and `discovery.exclude` globs to narrow or extend the walk.

Explicit source forms are added with repeatable flags:

```console
threadtrace scan example-api \
  --source ../shared/generated_client.py \
  --source fixtures/sample.ipynb \
  --source fixtures/bundle.zip \
  --generated "synthetic=NAME = 'generated'"
```

- An explicit external **file** is read and stored with both its original and
  resolved path. An explicit directory is rejected rather than traversed.
- Zip sources accept only validated `.py` members. Absolute paths, `..`
  traversal, symlink members, oversized members, and excessive expansion are
  skipped with reasons.
- Notebook code cells become separate source units with stable cell identity and
  cell-local line numbers. Markdown cells are ignored; a malformed cell fails
  without discarding the rest of the scan.
- Generated text receives a stable `generated://NAME` virtual origin. REPL,
  source-free, and native-extension bodies remain Partial or Unsupported.

Every discovered file records `origin_kind`, `original_path`, `resolved_path`,
content hash, parser/version, capability, and category (`source`, `test`,
`generated`, `notebook`, and so on). Skipped source forms and discovery warnings
appear in the scan summary under `discovery_warnings`; one bad file or archive
member does not discard unaffected findings. `file_categories` in the same
summary reports counts for source, test, generated, notebook, archive, metadata,
resource, native, and unclassified files.

## Name resolution and references

Scans persist lexical scopes and definitions for modules, functions, classes,
comprehensions, parameters, locals, constants, imports, and `global`/`nonlocal`
declarations. Aliases, forwarding imports, re-exports, `__all__`, wildcard
imports, overload stubs, inherited attributes/descriptors, conditional imports,
and `TYPE_CHECKING` branches are retained.

Inspect references and usage for a symbol or visible definition:

```console
threadtrace references example-api app.helpers.format_name
threadtrace usage example-api app.helpers.format_name --format json
threadtrace boundaries example-api
```

Each reference/call records a binding path when it can be resolved. Explicit
outcomes include `unresolved`, `unresolved_attribute`, `conditional`, and
`type_checking`; these are uncertainty facts, not failures to hide. Inherited
and re-exported methods retain the prefix that supplied the binding.

Standard-library and installed module source locations are located from the
interpreter search paths without importing the target. Third-party bodies are
not traversed transitively by default. Dynamic imports, `eval`/`exec`,
reflection, monkey patching, and unresolved attributes are stored as separate
boundary facts.

## Project metadata and staleness

Scans parse recognised project metadata without executing it:

- `pyproject.toml`, including runtime, extra, dependency-group, and build
  requirements.
- `requirements*.txt`, including recursive/constraint includes, editable paths,
  direct URLs, extras, and environment markers.
- `setup.py` and `setup.cfg`, using literal AST/configuration values only.
- `Pipfile`, `Pipfile.lock`, `poetry.lock`, and `uv.lock`.
- Supported Conda `environment*.yml`, `environment*.yaml`, `conda*.yml`, and
  `*.conda.yml` files using a bounded line-based subset.

Use `threadtrace metadata PROJECT` to inspect metadata files and dependency
records: group, marker, extras, source file, direct URL/path, editable flag,
status, and limitations. Malformed metadata becomes a partial record instead of
failing the scan; unrecognised root metadata is preserved by hash with an
`unsupported` status.

Each scan stores hashes for its effective discovery configuration, interpreter
identity, metadata files, and source units. Staleness is reported by cause and
affected capability:

```console
threadtrace staleness example-api
threadtrace staleness example-api --format json
```

Causes include changed sources, changed metadata/lockfiles, changed discovery
rules, and a changed interpreter. Query commands warn with the cause list, and
rescans remain explicit. Generated-text inputs cannot be recreated from their
stored hash alone, so they are not rechecked by `staleness`.

## Understanding results

### Evidence

Every reconstructed relationship is backed by source evidence where ThreadTrace can
identify it. Evidence includes a kind such as `call`, `import`, `decorator`, or
`configuration`, along with a file and line number. Use it to verify a result
before relying on it.

Public JSON results also carry one versioned `evidence_record` with a `version`,
`class`, `mechanism`, `source`, optional `run`, `limitations`, and the original
`legacy_confidence`. The evidence classes are:

| Class | Meaning |
| --- | --- |
| `statically resolved` | Directly represented by parsed source or stored static facts. |
| `heuristically predicted` | Static or runtime evidence supports a likely result without proof. |
| `observed at runtime` | A runtime event actually occurred or completed; attempted or unverified resources do not qualify. |
| `unknown` | ThreadTrace cannot safely infer the result. |

Older confidence labels (`confirmed`, `high`, `possible`, `unknown`) remain
visible for compatibility and for `--confidence` filters, but they are mapped to
the classes above. The original label is preserved in each evidence record, and
scan migration records keep the historical label counts.

A call is confirmed as a direct relationship only when Python binding evidence
identifies one target: a local or enclosing definition, a module definition, a
class method reached through `self`/`cls`, an explicit import, or a re-export.
A same-named function in another module does not count. Records expose the
binding in the `basis` field (`lexical`, `module`, `class`, `import`,
`re-export`, `builtin`, `shadowed`, `heuristic`, `dynamic`, or `unresolved`).
Name-only matches are kept as `possible` candidate calls with a reason in their
evidence, and a name shadowed by a local binding is reported as unresolved.

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
scan, ThreadTrace prints a staleness warning; run `threadtrace scan PROJECT` to
refresh the snapshot.

### Project registration

```console
threadtrace project add PATH [--name NAME]
threadtrace project list
threadtrace project show PROJECT
threadtrace project remove PROJECT --yes
```

`PROJECT` can be its registered name or project ID. Removing a project deletes
its registration, stored scans, and private experiment workspaces; it never
deletes the registered source. Pass `--keep-experiments` to preserve the
workspaces and have their paths reported instead. If a workspace cannot be
safely validated or removed, removal stops and the database record is kept so
no residual data is silently orphaned; fix or preserve it, then retry.

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
```

Entry-point kinds include `script`, `module`, `console`, `argparse`, `cli`,
`route`, `worker`, `scheduled`, `middleware`, `lifecycle`, `plugin`, and `test`.
Entries also record their framework, declared version (when metadata provides
one), variant, handler, inputs, limits, and capability status.

ThreadTrace detects conventional evidence for:

- `if __name__ == "__main__"` scripts and package `__main__.py` files;
- package console scripts declared in `pyproject.toml`;
- argparse and optparse parsers with flags, positionals, subcommands, defaults,
  and handlers;
- Click/Typer commands and command registries;
- Flask, Quart, FastAPI, Starlette, and aiohttp routes bound to a recognised
  application, blueprint, or router;
- Django `path`/`re_path` URL patterns and middleware;
- Celery, RQ, Dramatiq, Huey, and APScheduler tasks or jobs;
- Flask/FastAPI lifecycle hooks and pluggy plugin hooks; and
- test functions separately from production entry points.

A decorator named `.get()` or `.post()` is not a route by itself. ThreadTrace
requires a matching framework import and a bound registration object; unrelated
decorators remain under generic Python analysis rather than becoming
high-confidence endpoints. Database resources distinguish DB-API, SQLAlchemy,
and Django ORM calls while retaining syntax-only evidence.

Resource kinds are `database`, `service`, `file`, `configuration`,
`structured-data`, `text`, `template`, `media`, `serialization`, `compression`, `package`,
`environment`, `interpreter`, `diagnostics`, `queue`, and `subprocess`.
`http` and `env` remain aliases for `service` and `environment`; `file` also
includes filesystem-derived path classes. Unresolved dynamic path expressions
are retained with `possible` confidence and `unresolved` evidence. They are
conservative recognitions of known syntactic patterns, not proof that an
external operation completed.

`lesson` is a local, deterministic teaching card for one function, method, or
class. It combines purpose, source range, inputs/outputs, direct calls, callers,
and only patterns visibly present in that definition (such as comprehensions,
sorting, aggregation, recursion, or error handling). It does not use Ollama and
does not claim a performance bound.

`map` lists the project files connected to an entry point, symbol, or module,
and why each file is relevant. Omit `TARGET` to see the project-level file map.

`findings` defaults to issues that can affect understanding of the program,
such as unreadable files, import cycles, dynamic operations, and unknown
imports. Routine unresolved-call records are left out of this beginner view.
Add `--details` for the full technical list.

### Paths, impact, and data flow

```console
threadtrace path PROJECT ENTRYPOINT [--depth N] [--confidence LEVEL]
threadtrace impact PROJECT SYMBOL [--depth N] [--direction both|upstream|downstream]
threadtrace dataflow PROJECT SYMBOL [--to RESOURCE_KIND]
threadtrace slice PROJECT SYMBOL VARIABLE [--direction both|backward|forward]
threadtrace control-flow PROJECT SYMBOL
threadtrace complexity PROJECT SYMBOL
threadtrace advanced PROJECT SYMBOL
```

`path` begins at a detected entry point and follows resolved calls. It stops at
the selected depth, recursion, unresolved/dynamic boundaries, or external
resources. Use `--confidence confirmed` to hide inferred branches, or
`--confidence possible` for the normal balanced view.

`impact` labels both directions through calls, import dependants, inheritance
users, resolved references, configured entry points, and affected resources.
Lists stop at the depth and record budget, with `truncated` set when more
relationships exist. `dataflow` connects resolved positional/keyword arguments
to callee parameters and return assignments and correlates literal resource
sinks. Sensitive-data taint analysis is outside the v1 scope.

`slice` builds a conservative source slice from scoped definitions and uses plus
resolved interprocedural data edges. Attributes, subscripts, known container
mutations, dynamic aliases, unknown indexes, reflection, and indirect calls stay
visible as boundaries. `control-flow` emits a bounded graph for branches, loops,
break/continue, return/raise, try/except/else/finally, with/async-with, match,
yield, and conservative call-exception edges. It does not claim runtime branch
selection.

`complexity` keeps the stored cyclomatic count separate from time and space
estimates. It explains assumptions, composes resolved project calls, treats
unknown external calls as constant, and returns `unknown` for recursion or
unsupported constructs. `advanced` reports decorators and their target,
generators, async constructs, context managers, closures with free names,
descriptors, properties, metaclasses, dynamic attributes, concurrency creation
sites, native boundaries, and structural algorithm hints.

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
`diagnostics --include-unresolved-calls` includes every call ThreadTrace could not
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

The explorer is built with `prompt_toolkit`, not Textual. The normal layout has
a left navigation pane, a result list, and an inspector for source evidence,
paths, library context, and AI-assisted explanations. Below 80 columns or 20
rows it switches to a compact single-pane layout; `Tab`/`Shift+Tab` switches
between results and inspector there.

| Key | Action |
| --- | --- |
| `o` | Overview |
| `e` | Entrypoints |
| `a` | Architecture |
| `u` | Resources |
| `f` | Functions |
| `m` | Modules, or the linked-file map for the selected entry point |
| `c` | Classes, or the selected source while browsing files or graphs |
| `p` | Open the interactive File Graph, centred on the selected file when available |
| `t` | Switch between File Graph and Code Graph while viewing a graph |
| `j`/`k` or `Up`/`Down` | In a graph, move to a directly linked node |
| `+`/`-` | In a graph, increase/decrease the number of visible relationship hops |
| `[`/`]` | In a graph, narrow/broaden the confidence filter |
| `,`/`.` | In a graph, cycle relationship types (import, direct call, contains, inheritance) |
| `d`, `l`, `x` | Findings, libraries, import cycles |
| `g` | Go to the selected symbol's stored definition |
| `G` | List references to the selected symbol |
| `/` | Search symbols and files |
| `:` or `Ctrl-P` | Command palette |
| `j`/`k` or arrows | Move selection |
| `Tab` | Move focus between search, results, and inspector |
| `PgUp`/`PgDn`, `Ctrl-B`/`Ctrl-F` | Scroll the focused inspector |
| `v` | Open the selected result's source file full screen at its recorded line |
| `z` | Open the selected result's complete analysis full screen |
| `y` | Open a lesson card for the selected function, method, or class |
| Mouse wheel | Scroll panes where supported by the terminal |
| `r` | Rescan and stay on the same view and selected result |
| `?` | Open the complete key, command, and display summary |
| `Backspace` | Return to overview |
| `q` or `Ctrl-C` | Quit |

The results and inspector panes show scrollbars. Press `Tab` until the inspector
has focus, then use arrows, `PgUp`/`PgDn`, `Ctrl-B`/`Ctrl-F`, or the mouse wheel
to read all of a long explanation.

Every important action also has a command. Press `:` or `Ctrl-P` to open the
palette; keep typing to narrow the list and press `Enter` to run the selected
command. Available commands include `:overview`, `:entrypoints`,
`:architecture`, `:resources`, `:modules`, `:functions`, `:classes`,
`:findings`, `:libraries`, `:cycles`, `:file-graph`, `:code-graph`, `:source`,
`:analysis`, `:lesson`, `:definition`, `:references`, `:search`, `:ai`,
`:scan`, `:compact`, `:ascii`, `:colour`, `:help`, and `:quit`.

### Layout, keys, and display modes

The view and navigation keys in the table can be rebound with `tui.keybindings`,
a list of `action=key` entries in the configuration file or per-project
preferences. Actions are the command names above plus `search`, `command`,
`rescan`, `help`, and `quit`. Unknown actions, malformed entries, and two
actions bound to the same key are rejected when the explorer starts. Graph
(`t`, `j`, `k`, `+`, `-`, `[`, `]`, `,`, `.`) and source-view keys (`F2`, `F3`,
`Ctrl-S`, `Ctrl-R`, `F10`) remain fixed.

```yaml
tui:
  compact: false
  ascii: false
  colour: true
  keybindings: [overview=n, quit=z]
```

`compact: true` always uses the single-pane layout. `ascii: true` replaces
Unicode graph and selection characters with ASCII, and `colour: false` removes
colour highlighting. Colour is never the only status cue: the status bar always
begins with `INFO`, `OK`, `WARN`, or `FAIL`, and the selected row has a visible
marker. The same modes can be toggled at runtime with `:compact`, `:ascii`, and
`:colour`.

`:ai` requests an Ollama explanation for the selected symbol, entry point,
library, or overview. The explorer never imports or executes the target project.

### Relationship graphs

The explorer has two focused graph pages designed for understanding a project
without displaying every relationship at once.

- **File Graph** (`p` or `:file-graph`) uses one node per parsed Python file.
  An arrow is an internal import or a resolved call that crosses a file boundary.
  Its label names the functions involved when ThreadTrace knows them.
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
class, ThreadTrace opens the **complete selected definition** in a full-screen,
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

ThreadTrace only enables editor writes for a file resolved inside the registered
project root. Definition saves splice only that definition back into the source
file. A save runs in four explicit steps:

1. The complete resulting file is parsed with Python's AST parser. A syntax
error refuses the save and reports its line; nothing is written.
2. The on-disk file is hashed and compared with the content opened earlier. If
another editor changed it, ThreadTrace refuses the save rather than overwriting
those changes.
3. A read-only unified diff is shown. Use `y` or `Enter` to confirm, or `n`/
`Esc` to cancel.
4. The confirmed content is written to a temporary file in the same directory,
flushed, and moved into place atomically. A failed write leaves the original
file untouched and removes the temporary file.

Saving never executes the target code, but it makes the current analysis
snapshot stale until you rescan. Use `Ctrl-R` to save, rescan, and return to the
view and selected result you were using. `r` also rescans in place, preserving
that location. `F10` discards unsaved edits, and `Esc` refuses to close a source
view that still has unsaved changes.

## Local browser companion

The browser companion is a read-only local view of the most recent completed
scan. It never runs, edits, or controls the selected project:

```console
threadtrace browser example-api
```

The command prints a URL such as `http://127.0.0.1:43127/`; open it in the
browser of your choice. It binds only to loopback. Use `--host ::1` for IPv6
loopback or `--port 8080` to request a particular local port. Press Ctrl-C in
the terminal that started it to stop the server. The local server handles one
request at a time so its SQLite connection always remains on the serving
thread; browser requests may briefly queue behind one another.

The page renders dependency/import, call, class/module, AST, data-flow,
control-flow, and evidence-supported execution-flow graphs from the latest
completed scan. Choose a family, enter a module, qualified symbol, or entry
point, and press **Load graph**. Drag to pan, use the wheel to zoom, and
double-click a node to focus on it. The label, relationship-type, and evidence
filters narrow the current view; **Load more** appends another bounded page
instead of stopping at a hard graph-size cutoff. **Find** searches stored
modules and symbols and can switch the graph family and target.

Select a node or relationship to see its evidence class, source location,
capability status, and staleness. **Open source** reads only a bounded window
from a file inside the registered project; it never executes the target.
**Export Mermaid** downloads the same filtered graph, with node and edge
references validated before rendering.

Requests must come from the bound loopback address with a matching `Host` and
same-origin `Origin`; write methods are rejected. The view is read-only and
never runs, edits, or controls the project.

Use `threadtrace capabilities example-api` to see which feature areas are
currently Full, Partial, or Unsupported before relying on a result. Scope the
report to stored evidence with `--scan SCAN_ID`, `--run TRACE_ID`,
`--interpreter PATH`, `--feature TEXT`, and `--entity NAME`. A capability is
`Full` only when its tested boundary is verified; documented limitations remain
attached to every capability item.

## AI explanations

AI narration is optional and disabled unless you explicitly request it. ThreadTrace
supports local Ollama and OpenAI-compatible providers. It never sends repository
files, target source, or the environment; each request receives only a bounded,
redacted subset of stored ThreadTrace facts.

Start Ollama for a local model:

```console
ollama serve
ollama pull llama3.2
threadtrace explain example-api app.routes.create_user --ai
threadtrace explain example-api app.routes.create_user --ai --ai-model qwen2.5:7b
```

Nonsecret provider settings live in `config.yaml`:

```yaml
ai:
  provider: ollama          # ollama or openai
  model: llama3.2
  endpoint: null            # provider URL; localhost defaults apply
  temperature: 0.2          # 0 to 2
  generation_limit: 900     # maximum generated tokens
  timeout_seconds: 45
  key_env: THREADTRACE_AI_API_KEY
  template_path: null       # optional template containing {facts}
  parameters: []            # for example [top_p=0.9, seed=7]
```

For an OpenAI-compatible endpoint, set the credential named by `key_env` in the
environment (or a mode-0600 `.env` file in ThreadTrace's application directory)
and point the endpoint at the provider:

```console
threadtrace explain example-api app.routes.create_user --ai \
  --ai-provider openai --ai-endpoint http://127.0.0.1:8080/v1/chat/completions \
  --ai-model local-model
```

Credentials are never printed, stored, exported, or included in a prompt. A
credential in an endpoint URL is rejected. The `.env` fallback must not be
readable by group or others.

Loopback endpoints (`127.0.0.1`, `::1`, `localhost`) are local. Every remote
request requires approval of its exact redacted payload:

```console
# Prints the payload, manifest, and SHA-256 digest to stderr; sends nothing.
threadtrace explain example-api app.routes.create_user --ai \
  --ai-provider openai --ai-endpoint https://api.example.com/v1/chat/completions --ai-show

# Sends only if the digest matches the reviewed payload.
threadtrace explain example-api app.routes.create_user --ai \
  --ai-provider openai --ai-endpoint https://api.example.com/v1/chat/completions \
  --ai-approve SHA256
```

On an interactive terminal a remote request prints the payload and asks you to
type `approve`. A non-interactive request without the matching digest is refused.
Redaction removes secret-looking fields and `password=`/`token:` style assignments
from the outbound context; the manifest reports the redaction count, section sizes,
provider, model, endpoint, and input limits.

`--ai-temperature`, `--ai-tokens`, `--ai-template`, and repeatable
`--ai-parameter NAME=VALUE` override the configured settings. Templates are
limited to 20 KB and must contain `{facts}` exactly once. Unsupported provider
parameters or invalid settings fail before any request.

The provider must return JSON with a summary, claims carrying `path:line` evidence,
and an optional Mermaid diagram. Every cited location must exist in the stored
facts and every diagram node label must match a stored symbol or module. Invalid
JSON, invented references, and malformed diagrams are rejected, so a provider
cannot silently fabricate evidence.

Each successful exchange persists the exact prompt, approved-context manifest,
digest, validated response, provider/model/settings, timestamp, and evidence links
in the local database. List and delete them with:

```console
threadtrace ai-history example-api
threadtrace delete report example-api REPORT_KIND --yes
```

Deleting a scan through retention also removes the AI reports attached to it. In
the explorer, `:ai` uses the configured provider but refuses remote endpoints
because it cannot show an approval prompt; use the CLI for remote requests. Every
AI answer is interpretation, not source of truth: verify it against the cited
stored evidence before changing production code.

## Explicit runtime tracing

Normal scans never run target code. Runtime tracing is a separate, explicit
feature for correlating static facts with a command you choose:

```console
threadtrace trace example-api --yes-run -- python -m app.cli import-data sample.csv
threadtrace traces example-api
```

To compare a stored trace with its associated static scan, use native
trace-derived coverage:

```console
threadtrace coverage example-api TRACE_ID
```

It reports executed and unexecuted AST statement-start lines, functions reached
by the trace, and directly observed loop/conditional bodies. A compatible SQLite
`.coverage` file is read separately with line granularity and explicit
provenance; it is never merged into native trace coverage. The report does not
infer a missing loop exit or `else` outcome or claim coverage for
generated/native code. Files changed since the associated scan are labelled so
you can rescan before relying on the comparison.

### Static API discovery

Use the API report to inspect recognised Flask, Quart, FastAPI, Starlette,
aiohttp, and Django route evidence without starting the web application:

```console
threadtrace api example-api
```

The report includes recognised routes, methods, handlers, middleware, and direct
syntax evidence for authentication, authorisation, validation, rate limiting,
and response serialisation. Framework registration, decorators, and calls are
reported as static or heuristic evidence; dynamic route construction, WebSockets,
and indirect registration remain unresolved.

When an explicit trace exits unsuccessfully or times out, inspect its stored
failure evidence with:

```console
threadtrace trace-failure example-api TRACE_ID
```

The report records the exit/timeout reason, bounded final traceback excerpt,
terminating exception, in-project source location and hash, matching call chain,
safe captured state, and timing. Earlier handled or propagated exceptions remain
in a bounded `observed_exceptions` timeline rather than replacing the fatal
exception. `SystemExit` and `KeyboardInterrupt` are retained where observable;
timeouts or conflicting trace/traceback evidence remain explicitly `unknown`.
A small bounded stderr tail is reserved for failure reconstruction even when
ordinary stored stderr is truncated. The report also includes runtime
side-effect events observed before the failure and their capture/redaction
policy.

Inspect the chronological resource and serialisation timeline for any saved
trace with:

```console
threadtrace trace-resources example-api TRACE_ID
```

Runtime adapters observe supported `urllib`, `urllib3`, Requests, HTTPX, and
aiohttp request boundaries; Python-level TCP connects and FTP/FTPS command
boundaries; SQLite, SQLAlchemy, and Django database operations; file opens,
reads, writes, creates, renames, deletions, and permission changes; and
JSON/pickle serialisation. TCP and FTP events are metadata boundaries only:
TLS is not intercepted and payloads are not decrypted. Safe wrappers emit a
final `completed` or `failed` event only after the operation returns or raises.
Audit-only evidence is labelled `attempted`; correlated audit and wrapper
events share an ID. Events include duration, initiating project frame, and the
available in-project call stack. Database transaction, savepoint, release,
commit, and rollback operations are visible where the driver exposes them;
nested savepoint semantics remain Partial. Database rows and streaming HTTP
response bodies are never consumed to capture them. `trace-resources` reports
counts by status and failures, plus each adapter's installed version, tested
minimum, support status, and observed operation count/duration.

Before an explicitly requested `trace`, `runtime`, or `runtime-host` run,
ThreadTrace prints a bounded static side-effect estimate from stored resource
syntax. It is a best-effort summary, not a security guarantee: dynamic paths,
indirect calls, and actual branch selection can add or remove effects. The same
summary is stored with explicit traces under `preflight`.

Inspect the concurrent timeline for any saved trace with:

```console
threadtrace trace-concurrency example-api TRACE_ID
```

The report keeps stable run, process, thread, and task identities; records
process lifecycle and parent links; shows thread starts/finishes and asyncio
task creation/completion where observable; and lists queue, join, future,
threading condition/semaphore, asyncio lock/event/semaphore/condition/queue,
and async wait operations. Long waits are reported from observed durations.
Overlapping blocked waits may be labelled `possible_deadlock` with low
confidence; ThreadTrace does not claim scheduler proof. Native locks,
uninstrumented or non-Python children, and lock ownership remain unobserved
boundaries. Race findings are not inferred from wait timing alone.

Options:

```console
threadtrace trace PROJECT --yes-run --timeout 30 --output-cap 100000 \
  --capture-level metadata --capture-limit 4096 \
  [--instrumentation auto|settrace|monitoring|profile|audit|all] \
  [--include-third-party] -- COMMAND [ARGUMENT ...]
```

The trace runner captures bounded Python call/return events within the project
root, associates the trace with the latest static scan when available, and
stores stdout/stderr with an output cap. Each persisted event carries the
versioned runtime event schema and its category. `--instrumentation` reports
which mechanisms and event types are Full, Partial, or Unsupported; a requested
`monitoring` path reports its `sys.settrace` fallback rather than pretending to
provide frame locals. `--include-third-party` expands the recording scope to
reached dependency frames; presentation filters remain separate. Each captured
event includes a wall-clock timestamp plus monotonic elapsed and process CPU
time; the latter two are shown in Runtime and Listener inspectors for ordering
and timing context.

Inspect per-function timings, call counts, hot paths, and supported child-process
memory with:

```console
threadtrace trace-profile example-api TRACE_ID
threadtrace trace-events example-api TRACE_ID --kind line --path app/
threadtrace trace-events example-api TRACE_ID --search "validation"
```

`--search` scans recorded event history without changing or replaying it; event
navigation is not process-state restoration. Durations include tracer overhead and are not presented as target-only profiles.
Memory is the supported child-process peak, not an allocation profile.

`--instrumentation` and `--include-third-party` also apply to `runtime-host`.
Runtime sessions can pause for inspection, extend a timeout, request a graceful
process-group stop, or escalate to an emergency forced stop. `--capture-level off` disables resource adapters. `metadata` is the default and
records paths, URLs, HTTP headers/status, SQL statements, operations, sizes,
and timing without payload/file/parameter previews. `content` additionally
records bounded values already crossing a supported Python boundary, including
rows returned by an observed SQLite fetch; it does not perform extra file,
response, stream, or cursor reads. `--capture-limit` controls the
per-value bound and every truncated preview is labelled. URL user information
and query values, headers, cookies, database parameters, sensitive environment
values, exception messages, bytes, and nested mappings are redacted before
events or process output are persisted. Matching is case-insensitive and also
handles percent-encoded URL credentials and malformed URLs. Local unredacted
capture requires both `--no-redact` and `--yes-sensitive`. The same capture
options apply to `runtime` and `runtime-host`.

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

The command is required—ThreadTrace does not guess how a project should be started.
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
| `B` | Set a conditional breakpoint at the selected line. Enter a read-only expression such as `value == 3 and not done`. Calls, assignments, imports, comprehensions, and dunder access are rejected. |
| `t` | Set a tracepoint at the selected line. It records a marker event without pausing the program. |
| `w` | Set a watchpoint at the selected line. Enter a read-only expression; ThreadTrace pauses when its bounded summary changes. |
| `f` | Focus the selected asyncio task. Other task events are hidden and never pause the dashboard; press it again to show all lanes. |
| `i` | Focus **Program input**. Type one line and press Enter to send it to a line-oriented child prompt such as `input()`; press Esc to cancel the entry. |
| `j`/`k` or Up/Down | Inspect an earlier/later recorded event. |
| `Tab` | Move between the event timeline and the scrollable data/source inspector. |
| `q` | Stop the controlled subprocess and close the dashboard. |

In the Static explorer, select a function, method, or class and press `y` to
open the same lesson card in a full-screen, scrollable view.

The dashboard observes Python frames whose files resolve inside the registered
project root. For each event it shows the active function, source location,
safe inputs, argument/global summaries, return-value summaries, exception
types, and elapsed/CPU timing. Per-run object IDs link observable bindings to the
same object, while cycles are marked rather than followed. Changes distinguish
new bindings, rebinding, in-place mutation, deletion, and native/opaque state;
configurable depth, item, string, and byte limits keep serialization bounded. Strings are
capped, secrets are redacted when their variable name is recognised, and
arbitrary objects are described by their stored attributes rather than calling
their `repr()`. This is deliberately useful for learning without indiscriminately
copying a program's memory into ThreadTrace's database.

Breakpoints carry the source hash, file, line, and optional symbol identity.
If the file changes before execution, revalidation removes the stale location
rather than silently pausing on a different line. Inspecting earlier events
with `j`/`k` is history navigation only: it never restores real process state.
The same applies in the read-only live listener.

Async projects label each event with its Python thread and, where Python can
identify one safely, its asyncio task. Use `f` on a task event to make a busy
event loop readable. This is an observation filter: it does not stop or alter
background tasks. Step and pause controls are addressed to the selected thread,
so another thread cannot consume them; a global continue releases all waiting
threads. Await-boundary-only stepping is not yet offered, because it needs
scheduler-level coordination to be correct.

Runtime mode can still execute untrusted code. It is not a sandbox, does not
isolate network/filesystem access, and cannot reliably observe native code,
other processes, or code that never enters a project-root Python frame.

For a full-screen or key-by-key interactive application, use `runtime-host` in
one terminal and `runtime-listen` in another. Direct Runtime's Program input is
intentionally limited to complete input lines so its own dashboard remains in
control of the terminal.

### Running-process discovery and restart debugging

On Linux, ThreadTrace can inspect the current user's already-running Python
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
the exact action before ThreadTrace does anything.

| Confirmed key | Result |
| --- | --- |
| `s` | Register the inferred project root if needed, run a new static scan, then open the ordinary ThreadTrace explorer. The selected process keeps running. |
| `r` | Scan the inferred root, stop the selected current-user process and its descendants, then relaunch the recorded command under the Runtime dashboard with its detected PATH/venv. |
| `Esc` | Return to selection without acting. |

Restart debugging validates the selected PID's Linux start-time token before
signalling it, so a recycled PID is never targeted. It stops descendants before
their parent to reduce orphaned workers. The confirmation screen is mandatory;
there is no automatic termination or restart.

ThreadTrace cannot attach to or inject into a Python process that is already running
in this release. It can statically scan it immediately, or explicitly restart
it under ThreadTrace Runtime. Native/debugger attachment is future work because it
is platform-specific and far more invasive.

### Live observation of an interactive TUI

Do not use restart debugging for a program whose own terminal interface you
need to operate, such as ARIA. Instead, start it through a **Runtime Host** in
the terminal where it should remain interactive, and use a separate listener
dashboard:

```console
# Terminal A: ARIA keeps stdin, stdout, stderr, colours, and its normal TUI.
threadtrace runtime-host ARIA --yes-run -- dev run aria

# Terminal B: ThreadTrace passively follows the live host session.
threadtrace runtime-listen ARIA
```

`runtime-host` explicitly gives the child its original interactive terminal
streams rather than capturing them. ARIA's Urwid screen therefore appears in
Terminal A and receives your keyboard, resizing, colours, and Ctrl-C exactly as
if you had used `dev run aria` directly. `--yes-run` explicitly confirms that
this command may execute target code; it is not a sandbox. Do not pipe or
redirect the host command. On Linux, it publishes safe project-root trace events
over a private current-user Unix-domain socket; the socket is inside a mode-0700
session directory and itself is mode 0600. Only one listener may follow a host
at a time; a new listener can take over after the previous one has been quiet
for two seconds. If a restricted environment forbids local sockets, the host
keeps its local trace journal but Listener mode is unavailable rather than
falling back to another transport. Host/Listener IPC is not yet available on
Windows; Windows hosts retain the local journal only.

`runtime-listen` opens the newest running session for the project by default.
Finished or crashed sessions are not selected, stale metadata can be removed,
and each session directory is deleted after 24 hours; running hosts refresh
their metadata every ten seconds. The host prints a session ID if you need to
choose one explicitly:

```console
threadtrace runtime-listen ARIA --session SESSION_ID
```

An explicit `--session` can identify a finished session, but the listener only
reads live IPC, so finished history cannot be followed and the status line will
say that the host is unavailable.

The listener is deliberately read-only: it never pauses, types into, restarts,
or otherwise interferes with the program. It shows a timeline of calls, lines,
returns and exceptions, with readable variable changes, frame variables, timing,
and source context for the selected event. Values are bounded/redacted summaries,
not complete object contents. A line event occurs *before* the line executes, so
its effects appear at a later event. Source context is read from the current
file and can differ if the file has changed since execution.

Events arrive in pages capped at 200 events and 256 KiB per IPC response, so a
session larger than 1 MB can be followed incrementally. The status line reports
pending pages, dropped events, an oversized event, journal truncation, and
transport errors. The journal is bounded by the Host event cap; once that cap is
reached, later events are counted as dropped rather than silently disappearing.

| Key | Action |
| --- | --- |
| `j`/`k` or Up/Down | Inspect events in the timeline; stops following new events. |
| `l` | Jump to the newest event and resume live following. |
| `Tab` | Move between timeline and details; use arrow keys or PgUp/PgDn to scroll details. |
| `q` | Quit from the timeline; Ctrl-C quits from either pane. |

Stopping or interrupting the Host sends SIGTERM to the target's process group,
waits up to two seconds, and then sends SIGKILL. Descendants that remain in that
process group are stopped with it; a descendant that deliberately creates its
own session can escape this cleanup.

Current-frame variables are reconstructed from recorded changes for that frame,
so older sessions without frame IDs show only the individual changes. A program
already started with plain `dev run aria` cannot be observed until a future
attach feature exists; exit it and start it through Runtime Host instead.

## Experiments

Experiments keep source changes out of the registered project. Creating one
makes a private copy in ThreadTrace's application directory:

```console
threadtrace experiment create example-api validation-change
threadtrace experiment patch example-api validation-change app/validation.py 12 12 "    return value.strip()"
threadtrace experiment diff example-api validation-change
threadtrace experiment run example-api validation-change --yes-run -- python -m app
```

`patch` replaces an inclusive line range in the copy only. `diff` is available
before execution so the change can be reviewed. An experiment command can still
affect network services, databases, or files outside the copied workspace; it
is not a security sandbox.

Callable experiments run selected copied-workspace functions and classes in a
fresh child process. Targets use `module:attribute` notation. Their inputs,
return value, exception, captured output, selected object state, and applied
interactions are JSON-safe records that can be replayed or compared:

```console
threadtrace experiment configure example-api validation-change --config '{"substitutions":[{"target":"app.validation:load_policy","mode":"return","value":{"allow":true}}],"seed":7}'
threadtrace experiment preflight example-api validation-change
threadtrace experiment call example-api validation-change app.validation:validate --args '[" Ada "]' --yes-run
threadtrace experiment compare example-api validation-change app.validation:validate --args '[" Ada "]' --yes-run
threadtrace experiment explore example-api validation-change app.validation:validate --yes-run
threadtrace experiment runs example-api validation-change --format json
threadtrace experiment replay example-api validation-change RUN_ID --yes-run
threadtrace experiment capability example-api validation-change app.validation:validate --yes-run
threadtrace experiment export example-api validation-change --output validation-change.json
threadtrace experiment remove example-api validation-change --yes
```

Configuration supports `substitutions`, `patches`, `mocks`, `disabled_modules`,
`overrides`, `argument_overrides`, `line_overrides`, `adapters`, `fixtures`,
`setup_code`, `allow_expressions`, `input_generators`, `yield_cap`, `event_cap`,
`environment`, `seed`, `freeze_time`, `uuid`, `float_tolerance`, `interpreter`,
and `budgets`. A
substitution can return `None` or a selected/recorded value, raise a built-in
exception, or pass through while recording arguments and the return.
Mocks replace named imports with a JSON attribute object; `disabled_modules`
simulates an unavailable import; a mock attribute may also use a substitution
rule. Overrides set selected module/class attributes for a run, while argument
overrides replace positional/keyword arguments for a target.
`patches` compile an explicitly supplied replacement function in the copied
workspace only; each patch must name a target and define either `replacement`
or the replaced attribute name. `freeze_time` and `uuid` provide deterministic
controls for random calls, `time.time()`, `datetime.now()`/`today()`, and
`uuid.uuid4()` calls made after they are applied. Imported project aliases are
updated when their identity can be resolved and every replacement is restored
when the child experiment ends. `capability` identifies the selected callable
form and reports interpreter-dependent limits such as closure-cell mutation or
native implementation replacement instead of silently accepting them.

Rule precedence is explicit per-call/per-experiment override, named
patch/substitution, adapter mock/replay, then original behaviour. An override
that shadows a lower rule is reported, and same-level collisions are rejected
before execution: duplicate patch or substitution targets, patch plus
substitution on one attribute, duplicate line overrides, and duplicate adapter
selectors. `experiment preflight` validates a saved definition without running
the project and reports that precedence, statically discovered pre-bound
`from ... import ...` aliases, active-frame limits, the selected interpreter,
and which replay inputs are controlled or remain uncontrolled. JSON runs
include the same report. Set `interpreter` to run harness workers with a
selected Python executable; comparison uses it for both sides and records the
resolved interpreter.

Inputs may be JSON literals or explicit typed/reusable values:

```json
{"$type":"tuple","value":[1,2]}
{"$fixture":"user"}
{"$instance":"service"}
{"$recorded":"0"}
{"$operation":0}
{"$expr":"Path('fixture.json')"}
```

Expressions are evaluated only when `allow_expressions` is enabled. `setup_code`
and `fixtures` supply harness context without changing source. The experimental
Python interface also accepts a `pause_handler` callback. For persisted CLI
runs, `line_overrides` pause on matching project source lines and replace named
locals/globals before the line executes; each edit and step is recorded.

For a persistent class instance, pass an operations list. All operations share
one fresh harness process; no instance is retained after it ends:

```console
threadtrace experiment call example-api validation-change --operations '[
  {"kind":"construct","name":"validator","target":"app.validation:Validator","args":["strict"]},
  {"kind":"set_instance","instance":"validator","attribute":"strict","value":false},
  {"kind":"call_instance","instance":"validator","method":"validate","args":["Ada"]},
  {"kind":"inspect_instance","instance":"validator"}
]' --yes-run
```

Operations also support `call`, `get_instance`, and `set_global`; later operations can consume a
prior result. Ordinary functions, instance/static/class methods, classes,
generators, coroutines, and async generators are completed by the harness.

`adapters` supplies repeatable rules for HTTP (urllib and requests), SQLite,
selected filesystem opens, socket connections, and subprocess starts. Rules
can return HTTP status/headers/body/JSON or database rows, replace read
content, simulate latency, and raise timeout, connection, permission, missing
file, or unexpected errors. A matched rule never silently performs the real
operation: set `"pass_through": true` to allow it explicitly. Unmatched
operations behave normally. Each adapter record uses runtime-compatible fields
(`resource_kind`, `status`, `adapter_version`, `duration_seconds`, and `error`)
so mocked, passed-through, completed, and failed operations remain
distinguishable. Every callable result also includes captured output,
exception/traceback, execution steps, return, argument/object/global changes,
workspace file creations/modifications/deletions, external operations, context,
capability, peak child memory, and elapsed time.

Input exploration derives deterministic nominal, boundary, empty, invalid,
negative, large, and unexpected-type values from the callable signature.
`--cases` accepts an explicit JSON list of `{label, args, kwargs}` records,
while syntax-checked `input_generators` are labelled as explicit experiment
execution. Cases producing distinct returns, exceptions, paths, crashes,
timeouts, or side effects are retained as minimal `{case, reasons, signature}`
records. `budgets` bounds exploration with `max_cases`, `global_timeout`,
`case_timeout`, `max_events`, `max_output`, `max_memory_bytes`, and
`max_storage_bytes`; the result reports executed/truncated counts, peak memory,
stored bytes, and why collection stopped. Coverage-guided and general fuzzing
remain later/experimental.

Replay uses the exact saved inputs and experiment controls, runs a confirmation
execution, and reports observed nondeterminism. Its `replay` record lists the
controlled inputs (random seed, frozen time, UUID, environment) and every input
that stays uncontrolled, such as thread scheduling, existing random instances,
OS entropy, monotonic time, external state, object identity, and native code.
Harness and workspace commands run in their own process group; a timeout kills
the group and keeps bounded partial output. Capture and collection caps mark
truncation, and a comparison reports `unknown` for truncated output, trace, or
return values instead of claiming equality. At experiment creation,
ThreadTrace records a content-hash manifest of the registered source. Comparison
first verifies that source has not changed, then copies the registered baseline
and selected modified workspace into separate temporary workspaces. Each side
runs in its own fresh child process. The result records each workspace role,
exact source revision, configuration, and resolved interpreter while comparing
returns, exceptions, output, calls, argument/object/global state, filesystem and
adapter activity structurally with configurable floating-point tolerance and
`unknown` for values Python cannot compare reliably. A comparison refuses to run
if the registered source changed after experiment creation; create a new
experiment or restore that revision. Experiments created before source-revision
tracking must be recreated before comparison.

The experimental Python interface exposes the same session engine when a
custom pause callback or programmatically supplied objects are needed:

```python
from pathlib import Path
from threadtrace import HybridHarness

harness = HybridHarness(Path("/private/experiment-copy"), {
    "seed": 7,
    "line_overrides": [{"path": "app/service.py", "line": 24, "locals": {"limit": 0}}],
})
result = harness.call("app.service:run", args=[{"$type": "tuple", "value": [1, 2]}])
```

Pass `pause_handler=` to receive each project-line snapshot and return a mapping
of local names to replace before that line runs. This interface deliberately
executes the selected copied project in the caller process; use the CLI/Engine
child-process interface when process isolation is required.

Stored results use a separate read-only interface that never scans, traces, or
runs experiments:

```python
from threadtrace import ThreadTrace

with ThreadTrace("/path/to/thread.sqlite3") as threadtrace:
    project = threadtrace.project("example-api")
    scan = threadtrace.latest_scan("example-api")
    functions = threadtrace.query("functions", "example-api")
    trace_ids = [trace["trace_id"] for trace in threadtrace.traces("example-api")]
    bundle = threadtrace.export("example-api")
```

`ThreadTrace` accepts a database path or uses the configured one and exposes
`projects`, `project`, `scans`, `latest_scan`, `traces`, `investigations`,
`investigation`, the documented read-only `query` names (overview,
architecture, findings, data flow, slices, traces, and similar), and `export`,
which returns the same bundle the CLI writes. Query names outside that list
raise an error instead of executing anything. Hybrid execution remains in
`HybridHarness`.

`experiment remove --yes` permanently removes only the private workspace and
its saved run history. It verifies that the workspace is not the registered
source directory before deletion.

Named launch commands can be saved without running them:

```console
threadtrace profile save example-api tests -- python -m unittest
threadtrace profile save example-api local --interpreter .venv/bin/python \
  --cwd backend --env APP_MODE=local --unset-env DEBUG -- python -m app
threadtrace profile run example-api tests --yes-run
```

`--interpreter` replaces the command's leading `python` token; a profile whose
command does not start with a Python executable is rejected so the choice is
never ambiguous. `--cwd` is resolved against the project root when relative.
`--env NAME=VALUE` stores nonsecret overrides and `--unset-env NAME` removes an
inherited variable. Secret-named overrides are rejected; supply those through
the inherited environment instead. The recorded trace includes the requested and
resolved command, executable path, interpreter version, working directory,
overridden and removed variable names, and any ThreadTrace instrumentation
collisions. Values are not recorded.

Investigations record what you found and where:

```console
threadtrace investigation save example-api auth-flow --content '{"notes":"route calls the limiter","symbols":[{"path":"app/api.py","line":12,"qualified_name":"app.api.list_items"}],"hypotheses":[{"text":"limit is never enforced"}]}'
threadtrace investigation list example-api
threadtrace investigation show example-api auth-flow
threadtrace investigation resume example-api auth-flow
threadtrace investigation export example-api auth-flow --output auth-flow.json
threadtrace investigation import example-api auth-flow.json --name auth-flow copy
```

An investigation stores `files`, `symbols`, `events`, `notes`, `hypotheses`,
`graphs`, `relationships`, `traces`, `experiments`, and `bookmarks`. Each
section is a list of objects or text; a bare note becomes `[{"text": ...}]`.
Every referenced file, symbol, or event is captured with the current file
hash, a bounded 40-line snippet, and a timestamp, so historical evidence
survives later edits. Saving again keeps earlier captures and adds new ones.

`resume` reopens the workspace against the latest completed scan and reports
each reference as `unchanged`, `changed`, `moved`, or `missing` (a trace is
reported as `available` or not) with warnings on stderr. Stored locations are
never moved or silently refreshed. `export` writes a portable JSON artifact
with the shared export envelope, the content, references, and warnings;
`import` accepts that artifact, rejects an unknown newer export version, and
warns when the saved source revision differs from the current project.

Use `threadtrace bookmark PROJECT TRACE_ID EVENT_NUMBER LABEL` to mark a
recorded runtime event.

## Exports and automation

Report commands support text, JSON, Markdown, CSV, and self-contained HTML
output:

```console
threadtrace overview example-api --format json
threadtrace explain example-api app.main --format markdown
threadtrace functions example-api --format csv > functions.csv
threadtrace overview example-api --format html > overview.html
```

Architecture additionally supports documentation-friendly graphs:

```console
threadtrace architecture example-api --format mermaid > architecture.mmd
threadtrace architecture example-api --format dot > architecture.dot
```

Produce one portable, versioned report of the latest completed scan:

```console
threadtrace export example-api --format json --output example-api.json
threadtrace export example-api --format html --output example-api.html
threadtrace export example-api --format mermaid
```

Every portable artifact starts with the same envelope: `format`,
`export_version`, application/database/trace versions, `payload_type`,
generation time, project and scan identifiers, run IDs, source file and
scan-input hashes, capability status, limitations, and stored truncation
markers. `export` defaults to JSON and supports JSON, Markdown, CSV, HTML,
Mermaid, and DOT. JSON is the machine-readable form; HTML is self-contained
and escapes target-controlled content; the other formats carry the envelope as
a metadata section or comment lines. Files written with `--output` use mode
0600 where supported. Experiment and investigation exports use the same
envelope.

Interactive commands such as `runtime`, `runtime-host`, `runtime-listen`,
`process`, `tui`, and `browser` accept only text; `experiment diff` accepts only
text or JSON. Any other command/format combination is rejected with exit code 2
instead of falling back to text, so scripts never receive a silently different
format. Unsupported formats, configuration errors, and expected database or
migration failures exit with code 2 and a concise message on stderr without a
traceback. Warnings go to stderr, while JSON, CSV, and graph output write only
their document to stdout.

The JSON output preserves confidence and evidence fields where the query has
them, making it suitable for CI artifacts or scripts. Mermaid and DOT should be
treated as views of the selected scan, not as replacement source evidence, and
labels are escaped for their renderer.

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

Report local usage and optionally enforce retention:

```console
threadtrace storage
threadtrace storage --prune --retention-days 30 --format json
```

`retention_days` defaults to 30. Pruning deletes old runtime traces and old
completed/failed scans while preserving the newest completed scan for each
project. `quota_mb` is reported as a configurable threshold; pruning is always
explicit. `trace_saving: none` runs `threadtrace trace` without persisting the
result; the default `explicit` saves explicitly requested traces.

To remove all stored data for one registered project, use:

```console
threadtrace project remove PROJECT --yes
```

To delete one trace, one report, or every local record:

```console
threadtrace delete trace PROJECT TRACE_ID --yes
threadtrace delete report PROJECT overview --yes
threadtrace delete all --yes
```

Deletion output lists removed and preserved experiment workspaces. Project
removal does not delete the SQLite database if it contains other projects, and
no delete command removes registered source files.

Database migrations are backed up to `threadtrace.sqlite3.bak` before an older
schema is upgraded. A failed migration restores the backup. The database file,
configuration file, exports, and live session directories use restrictive
permissions where supported. SQLite uses WAL, foreign keys, a five-second busy
timeout, and transactional writes for concurrent readers/writers.

Application, database schema, trace protocol, and export schema versions are
independent and included in every portable export envelope.

Experiment patches are syntax-checked before writing, written atomically, and
recorded in a patch history with before/after hashes. `experiment diff` reports
modified, created, deleted, renamed, binary, and unreadable files with hashes and
bounded previews; `--format json` returns the structured change report. Exports
include the same change report and patch history.

## Safety and limitations

ThreadTrace is designed to be conservative.

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

Run a new scan. ThreadTrace detected added, removed, or changed eligible Python
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
