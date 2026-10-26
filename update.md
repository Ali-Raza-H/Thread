# Thread roadmap: Python project reverse engineer

## Current implementation — v0.7.0-alpha.1

Thread v0.7.0-alpha.1 establishes the local reverse-engineering foundation and
the first deterministic beginner tour:

- Safe static AST scanning that never imports or executes the scanned project.
- Immutable SQLite scan snapshots, migrations, incremental reuse, comparison,
  source evidence, and confidence labels.
- Entry-point, relationship, component, resource, bounded data-flow, impact,
  architecture, and path reconstruction.
- CLI and JSON/Markdown/Mermaid/DOT output for automation and documentation.
- A prompt_toolkit explorer with overview, entry-point, architecture, resource,
  library, finding, source-inspector, full-definition, and guarded editor views.
- Optional local Ollama narration restricted to persisted Thread facts.
- Explicit opt-in runtime tracing, separately stored and never triggered by a
  normal scan or explorer launch.
- `threadtrace guide PROJECT` for a beginner-facing project starting point,
  ordered user-facing entry points, main parts, and important analysis limits.
- `threadtrace walk PROJECT ENTRYPOINT` for a numbered, evidence-backed
  explanation of one supported static path.
- `threadtrace map PROJECT [TARGET]` for project-wide or feature-scoped linked
  files, plus `threadtrace findings PROJECT` for friendly important findings.
- The explorer now opens in Guided mode; number keys choose a starting point,
  and guided `w`, `m`, and `c` open a walkthrough, linked files, and code.
- Full function/class definitions open in a dedicated source view. Definition
  saves splice only that definition after a file-hash conflict check; F3 opens
  a deliberate whole-file view.
- Interactive focused relationship graphs: File Graph for imports and
  cross-file calls, Code Graph for calls, class membership, and resolvable
  inheritance. Graph navigation recentres on linked nodes and supports hop,
  confidence, and relationship-type filters.
- `threadtrace runtime PROJECT -- COMMAND` opens a paused, step-by-step
  runtime dashboard. It explicitly starts one supplied subprocess only after a
  dashboard action and records project-level calls, lines, returns, exceptions,
  and safe variable/input/output summaries.
- Linux current-user process discovery: `threadtrace processes` lists live
  Python interpreters, while `threadtrace process [PID]` requires a confirmation
  screen before normal static scanning or an explicit terminate-and-restart
  under Runtime. Existing-process injection/attachment remains future work.
- Two-terminal interactive observation: `runtime-host PROJECT -- COMMAND`
  preserves a target TUI in its own terminal and publishes safe events; a
  separate `runtime-listen PROJECT` dashboard follows it without pausing or
  capturing its input/output. This is the supported runtime workflow for ARIA.
- Runtime protocol v1 now includes event ordinals, threads, call depth, and
  asyncio task identity where Python can provide it. The Debug dashboard offers
  step into/over/out, line breakpoints, and task focus; focusing a task filters
  observation without stopping background work.
- `threadtrace lesson PROJECT SYMBOL` provides a deterministic, evidence-backed
  teaching card for functions, methods, and classes. It recognises only local
  source patterns it can demonstrate, and the Static explorer opens it with
  `y` in a scrollable full-screen view.

This alpha is usable for deterministic beginner-oriented static exploration and
controlled runtime investigation. Data journeys, richer framework fixtures,
runtime resource events, static-vs-observed comparison, and Linux attachment
remain planned before a stable release.

## v0.7.0 implementation programme — Python reverse-engineering workspace

### Workspace model

Thread will retain the current evidence-backed static explorer, but organise the
product around four complementary workspaces:

1. **Static** — guided overview, files, symbols, entry points, framework facts,
   resources, source evidence, file/code graphs, and static data journeys.
2. **Debug** — an explicitly launched command paused under a controllable
   runtime agent, with breakpoints and step into/over/out.
3. **Live** — a passive listener for an interactive program started through
   Runtime Host; it never steals the target's terminal or pauses it by default.
4. **Compare** — static predictions beside observed calls/resources/data, with
   explicit evidence and uncertainty for both sides.

### Delivery order

1. Shared versioned runtime-event protocol: event IDs, thread identity, task
   identity, frame/call depth, source location, safe inputs/changes/outputs,
   exceptions, and resource observations.
2. Debugger controller: breakpoints, step into/over/out, source-level focus,
   thread selection, event caps, cancellation, and trace persistence.
3. Asyncio correlation: focused task, lanes, suspension/resumption events, and
   full/task-step/silent-background modes without patching private asyncio APIs.
4. Static expansion: deterministic function/class lessons, conservative
   pattern/algorithm recognisers, test-to-symbol links, configuration/plugin
   discovery, and stronger framework adapters.
5. TUI revision: Static/Debug/Live/Compare navigation, shared source inspector,
   graph overlays, runtime lanes, and static-vs-observed drill-down.
6. Linux process capabilities: current-user discovery, restart under Runtime,
   Host/Listener, attach diagnostics, shallow sampling, then an explicitly
   dangerous experimental CPython/GDB injection path where ptrace allows it.

### Non-negotiable safety rules

- Static analysis never executes target code.
- Runtime launch, restart, Host, and attach remain visibly opt-in.
- Live values are bounded and redacted before crossing process boundaries or
  being persisted.
- Attach is Linux/CPython experimental, records only post-attach execution, and
  never changes ptrace/Yama policy automatically.
- The same runtime event protocol serves launched debugging, Host/Listener, and
  future attach; implementations must not silently diverge.

## Remaining v0.6.0 plan: Guided codebase tour

### Product outcome

After scanning a moderately complex Python repository, a beginner should be
able to select one plain-language question and understand a meaningful part of
the program without first learning AST terminology, call-graph theory, or the
project's framework.

The opening interaction should answer:

```text
What is this project?
How does it start?
What happens when this feature runs?
Which files and functions are involved?
What does this function do, in plain English?
What algorithm or programming pattern is it using?
What data enters, changes, and leaves the program?
What is proven by source, what was observed at runtime, and what is unknown?
```

Thread must remain evidence-first. Beginner-friendly language must explain the
analysis; it must never conceal uncertainty or replace source evidence.

### North-star experience

```text
$ threadtrace guide invoice-api

Welcome to invoice-api

What Thread found
  A FastAPI service that creates invoices and sends receipts. [high]

Where can this program start?
  1. POST /invoices              Create an invoice
  2. CLI: import-customers       Import customer data
  3. worker: send_receipt        Send a receipt asynchronously

Choose 1

POST /invoices — guided walkthrough
  1. api/routes/invoices.py:42  create_invoice
     Receives an HTTP request and validates the invoice fields. [confirmed]
  2. services/invoices.py:18    InvoiceService.create
     Applies the project’s invoice-creation rules. [confirmed]
  3. repositories/invoices.py:61 InvoiceRepository.insert
     Writes the new invoice to the database. [confirmed]
  4. workers/receipt.py:23      send_receipt.delay
     Schedules a receipt task. Thread cannot prove when a worker runs it. [possible]

Ask next
  [w] Why is this step needed?   [d] Show data movement
  [c] Show code                 [f] Explain this function
  [a] Explain the algorithm     [i] What could change if I edit this?
```

### Design principles

1. **Questions before tables.** The first screen offers useful questions, not
   modules, diagnostics, or database rows.
2. **Progressive disclosure.** A simple statement comes first; source evidence,
   graph branches, and raw facts remain one keypress away.
3. **Teach without pretending.** Explain Python/framework terminology inline;
   distinguish `confirmed`, `likely`, `possible`, and `unknown` in plain words.
4. **Show a story, not a hairball.** Paths are ordered narratives. Graphs are
   supplementary and scoped to the current question.
5. **Static and runtime are separate truths.** A static path is what source
   supports; a runtime timeline is what an explicit trace observed.
6. **No AI authority.** Ollama can rephrase recorded facts, but deterministic
   facts, source lines, and confidence remain the source of truth.

### User-facing concepts

| Beginner-facing term | Internal Thread concept | Example |
| --- | --- | --- |
| Starting point | Entry point | `POST /invoices`, CLI command, script |
| Step | Relationship/path step | `create_invoice → InvoiceService.create` |
| External system | Resource boundary | PostgreSQL, filesystem, HTTP API, queue |
| Data journey | Bounded data edge | request field → function parameter → database call |
| Risk or gap | Finding | dynamic import, unknown library, syntax failure |
| Why this matters | Impact traversal | downstream callers/resources affected by a change |

The detailed raw labels remain visible in an advanced/details drawer.

### New guided surfaces

#### 1. Guided overview

Add `threadtrace guide PROJECT` and make the explorer's default view a guided
tour instead of an architecture component list.

The overview contains:

- One evidence-backed project purpose sentence, or an explicit statement that
  Thread cannot infer a reliable purpose.
- “Start here” choices ordered by usefulness: user-facing routes/commands,
  scripts, workers, then tests.
- A short “main parts” list, using roles such as API, command line, business
  logic, persistence, integration, and presentation.
- A friendly health panel that groups important findings separately from normal
  unresolved-call noise.
- A “what should I inspect next?” recommendation based on available entry
  points and confidence.

Acceptance criteria:

- A first-time user can select a start point with arrows/number keys.
- No raw JSON or unresolved-call list appears by default.
- Every purpose/component claim exposes source evidence.

#### 2. Guided path walkthrough

Add `threadtrace walk PROJECT ENTRYPOINT` as a beginner-oriented counterpart to
the existing `path` command.

Each step shows:

- A numbered position in the path.
- Symbol name and source location.
- One concise deterministic explanation: role, input, output, side effect, or
  reason for the transition.
- Confidence translated into plain language, for example “Thread can see this
  call directly” or “this may happen through dynamic dispatch.”
- Direct resources and dynamic/unresolved stop points.
- Controls to expand candidate branches, skip utility functions, inspect code,
  inspect data flow, and inspect impact.

Path selection must avoid claiming one canonical path when branches are
unresolved. The interface should say “one supported path” where appropriate.

#### 3. Function and class lessons

Add `threadtrace lesson PROJECT SYMBOL` and a corresponding explorer action.

For a selected function or class, generate a deterministic lesson card before
using Ollama:

```text
Function: normalize_email(value)

Purpose
  Removes surrounding whitespace and normalizes casing.

Inputs
  value: str

Output
  str

Calls
  value.strip(), value.lower()

Used by
  create_user

Pattern detected
  Input normalization / sanitisation [high]
```

The card uses recorded signatures, returns, calls, flow events, decorators,
complexity, and source evidence. If the tool cannot identify a meaningful
pattern, it says so rather than assigning a fashionable label.

Initial explainable patterns:

- Input validation and normalization.
- Mapping/filtering/reduction loops and comprehensions.
- Search and lookup through dictionaries/sets/lists.
- Sorting and grouping.
- Recursion.
- Retry/error-handling loops.
- Producer/consumer queues.
- Repository/service/controller layering.
- Factory, strategy, observer/callback, and dependency-injection conventions
  only where direct structural evidence supports them.

Do **not** claim asymptotic complexity unless Thread can derive it from a small,
supported rule set. Present any estimate as a teaching hint with evidence.

#### 4. File-link map

Add `threadtrace map PROJECT [TARGET]` and a “Files involved” explorer panel.

The map starts from a selected entry point, symbol, or module and answers:

- Which files participate in this feature/path?
- Why is each file included: import, direct call, decorator registration,
  inheritance, resource boundary, or runtime observation?
- Which files are central versus utilities?
- Which links are confirmed versus possible?

Default output is a readable ordered list. Mermaid/DOT remains available for
users who want a graph artifact, but graphs never become the only explanation.

#### 5. Data journey

Build on current bounded data edges with a beginner presentation:

```text
request body.email
  → create_user(email)
  → UserService.register(email)
  → UserRepository.insert(email)
  → PostgreSQL write
```

For each edge, show the variable/parameter names and source locations. Clearly
end the journey at unknown transformations, unsupported container mutation, or
dynamic dispatch.

#### 6. Findings redesign

Replace the current beginner-facing findings list with two views:

- **Needs attention** (default): syntax failures, import cycles, unknown
  imports, dynamic boundaries, strongly coupled modules, and failed scans.
- **Analysis details** (advanced): unresolved built-ins/library methods,
  candidate calls, and all informational resolution records.

The default wording should explain impact, for example:

```text
Thread cannot follow this dynamic import, so paths past this line may be incomplete.
```

not:

```text
TB-DYNAMIC-001
```

### Analysis additions needed for v0.6

#### Semantic and explanatory facts

Persist a small, deterministic `explanations`/`teaching_facts` layer rather than
asking Ollama to infer basic function descriptions on every view:

```text
teaching_facts
  id, scan_id, symbol_id, kind, label, confidence, summary, evidence_json
```

Examples: `input_normalization`, `list_filtering`, `dictionary_lookup`,
`recursive_call`, `database_write`, `error_translation`.

Implement rules as narrow AST/semantic recognizers with fixtures. Every fact
must retain the supporting symbol/file/line evidence.

#### Better call resolution classification

Reduce informational unresolved-call noise by classifying calls into:

- Known built-in operation (`dict.get`, `str.strip`, `json.dumps`).
- Known standard-library operation.
- Known third-party boundary/library method.
- Dynamic/unknown call requiring an analysis-gap finding.

The tool does not need to resolve every library implementation. It needs to say
“this is a dictionary lookup” or “this calls the Requests HTTP library” instead
of presenting a beginner with an unresolved-call warning.

#### Runtime-to-static alignment

Enhance the existing explicit runtime trace with a comparison view:

- Mark static path steps observed in a trace.
- Show runtime-only frames as observed, not assumed architecture facts.
- Group repeated call frames into a collapsible count.
- Provide a plain-language reason when a static path and runtime path differ.

Runtime tracing remains opt-in and clearly warns about code execution.

### Ollama's role

Ollama should be an optional **teacher voice**, not an analyzer.

Use it for:

- Rephrasing a deterministic function lesson at the user's chosen experience
  level: beginner, intermediate, or expert.
- Answering bounded questions such as “why is this dependency here?” using only
  selected Thread facts.
- Producing a concise project briefing after the user sees the deterministic
  overview.

Do not use it for:

- Discovering call edges, entry points, algorithms, dependencies, or security
  claims from unrestricted source.
- Suppressing confidence labels or source evidence.
- Quietly contacting any hosted provider. v0.6 remains Ollama-only.

Add an experience-level setting:

```console
threadtrace lesson PROJECT SYMBOL --level beginner --ai
threadtrace walk PROJECT ENTRYPOINT --level beginner --ai
```

The deterministic lesson/walk remains available without Ollama.

### Explorer interaction plan

Retain prompt_toolkit and evolve the explorer around modes:

```text
Tour → Walkthrough → Function lesson → Code / data / impact / files
```

Required controls:

- Number keys select a suggested entry point.
- `Enter` advances/drills into the current guided item.
- In **guided mode**, `w` opens a walkthrough; `f` opens a function lesson;
  `d` opens data; `i` opens impact; `m` opens the file map; and `c` opens the
  full definition code. These bindings are intentionally context-specific, so
  the existing browse-mode keys remain available outside the guided flow.
- `z` opens full analysis; `v` remains a full-definition/source view.
- The inspector stays scrollable and uses a true text widget, not a clipped
  custom renderer.
- Editing is always explicit and remains restricted to project-root files. A
  definition editor saves by splicing only that selected definition back into
  the containing file, after confirming the file content hash has not changed
  since it was opened. It refuses a stale save instead of overwriting unrelated
  edits. A separate action opens the full file for broader edits.

### Delivery plan

#### v0.6.0-alpha — deterministic beginner tour

- Guided overview and `guide` CLI command.
- Friendly findings split.
- Guided `walk` command for existing entry-point paths.
- Full function/class definition view as the default source action.
- File-link map using existing relationships.
- Fixtures and golden outputs for a CLI app, a FastAPI app, and a worker app.

Acceptance demo:

```text
Scan a small FastAPI fixture.
Choose POST /users.
Follow route → service → repository → database write.
Open each full function definition and source evidence.
Explain the unobserved email queue branch as possible, not confirmed.
```

#### v0.6.0-beta — teaching facts and data journey

- Deterministic lesson cards.
- Initial pattern recognizers and exact evidence.
- Data journey UI/CLI over current direct-call data edges.
- Known-operation classification to reduce unresolved-call noise.
- Beginner/intermediate/expert wording profiles without Ollama.

Acceptance demo:

```text
Select normalize_email.
See its purpose, inputs, output, callers, normalization pattern, source, and
the direct user-registration data journey.
```

#### v0.6.0 — optional teacher voice and trace comparison

- Ollama teaching mode for guide, walk, lesson, and library explanations.
- Required citations and visible AI-assisted label.
- Runtime/static comparison view.
- Explorer usability test suite: focus, scrolling, source definition opening,
  edit/save/rescan, keyboard navigation, and no-data states.

### Quality and safety requirements

- Test every pattern recognizer against positive and negative fixtures.
- Golden-test beginner wording and source citations.
- Test that `guide`, `walk`, `lesson`, and `map` never require Ollama.
- Test no target code is executed by scanning or explaining.
- Test editor writes remain inside the project root and require explicit save.
- Test that runtime trace warnings appear before any execution.
- Measure a first useful guided session for 100 and 2,000 file projects.

### Confirmed v0.6 decisions

1. Web APIs and CLI programs receive equal fixture, onboarding, and guided-tour
   priority. Workers remain a required supporting fixture type.
2. Deterministic explanations always appear first. Ollama has no default
   affordance in the beginner tour and runs only after an explicit request.
3. In-app editing remains a lightweight convenience in v0.6. It supports
   definition-level editing with file-hash conflict protection, while a future
   release may add a more complete editor or external-editor handoff.
4. The beginner-facing command is `threadtrace guide PROJECT`; “guided” is the
   mode/experience name used in text and UI labels.

## Historical foundation plan

The remainder of this document records the original reconstruction foundation.
It remains useful design context, but the v0.6.0-alpha.1 status and v0.6.0 plan above
take precedence where they differ. Any later “confirmed” or “next release”
statements in this historical section are superseded by the active v0.6 plan.

## Product direction

Thread should evolve from a **static-analysis query tool** into a **local Python
project reverse engineer**.

The difference is important:

- The current tool answers low-level questions such as “which functions are in
  this module?” or “what imports this file?”
- The next version must reconstruct and explain the system: its entry points,
  subsystems, execution paths, data movement, external boundaries, risks, and
  confidence in every conclusion.

The primary outcome is that a developer can open an unfamiliar Python project
and understand its architecture and a meaningful execution path within minutes,
without blindly reading files in an arbitrary order.

## Assumptions and product boundaries

This plan assumes the following unless a later product decision changes them:

1. Thread remains local-first. Source code and analysis results stay on the
   developer's machine by default.
2. Static analysis is the default and never imports or runs the target project.
3. Runtime analysis, if added, is explicit, sandboxed where possible, and
   clearly labelled as potentially executing untrusted code.
4. The CLI remains useful for automation; the TUI becomes the primary human
   interface.
5. Python 3.11+ remains supported.
6. Exactness is more important than impressive-looking guesses. Every inferred
   relationship needs evidence and a confidence level.
7. The next implementation phase is static-only. Opt-in runtime tracing stays
   on the later roadmap and is not a prerequisite for reverse-engineering a
   project.
8. Thread targets all Python project families. Its generic reconstruction layer
   must be useful without a framework adapter; adapters progressively improve
   recognition for project-specific conventions.

This is not intended to become a decompiler, debugger, IDE, type checker, or
security scanner. It complements those tools by producing an understandable
model of a Python codebase.

## What the finished product should answer

On one scan, a user should be able to answer:

```text
What is this repository for?
How can this program start?
Which entry point handles a given CLI command, route, worker job, or script?
What happens next, from the entry point through its important calls?
Which modules form the database, HTTP, CLI, domain, UI, and integration layers?
What data enters a path, where is it transformed, and where does it leave?
What external systems does it use?
Which components would be affected by changing a symbol or module?
What could Thread prove, and what is only an inference?
```

## Current state and reusable foundation

The existing code already provides useful raw evidence:

- Safe recursive Python discovery that avoids importing target code.
- AST extraction for modules, classes, functions, methods, decorators,
  docstrings, imports, syntactic calls, attributes, and local flow events.
- SQLite snapshots, projects, scan history, and query services.
- Conservative import/call resolution and structural diagnostics.
- CLI queries plus a prompt_toolkit-based full-screen browsing interface.

The main gap is not storage or basic parsing. It is the missing **reconstruction
layer** between raw facts and human understanding.

## Core concepts for the new model

### Evidence

Every result should carry source evidence rather than only a generated label.

```python
Evidence(
    kind="decorator" | "import" | "call" | "assignment" | "configuration" | "runtime",
    path="src/app/routes.py",
    line_start=24,
    line_end=28,
    excerpt="@app.get('/users/{id}')",
)
```

The UI must make evidence one keypress away from every claim.

### Confidence

Use a small, consistent vocabulary:

| Level | Meaning |
|---|---|
| `confirmed` | Directly represented in source or a captured runtime trace. |
| `high` | Multiple static facts strongly support the relationship. |
| `possible` | Plausible inference with incomplete static resolution. |
| `unknown` | Thread cannot safely infer this relationship. |

Do not silently convert `possible` into `confirmed` in diagrams or summaries.

### Architecture component

A component is a module or a cohesive group of modules with a role, such as
`CLI`, `HTTP API`, `domain/service`, `persistence`, `background worker`,
`configuration`, `integration`, or `presentation`.

Roles start as evidence-backed labels, not arbitrary directory names. For
example, an HTTP role might be supported by route decorators and web-framework
imports; a persistence role by database library imports and query calls.

### Execution path

An execution path is a directed path from an entry point through calls,
imports, framework registrations, and optionally runtime events.

```text
CLI command: threadtrace scan PROJECT
  confirmed → threadtrace.cli.main
  confirmed → Engine.scan
  confirmed → discover_python_files
  confirmed → parse_file
  confirmed → Engine._persist_scan
  confirmed → Store transaction / SQLite writes
```

Each edge records its mechanism (`direct_call`, `import`, `route_registration`,
`callback`, `queue_consumer`, `runtime_trace`) and confidence.

## Proposed architecture

Keep the existing package and extend it instead of replacing it:

```text
src/threadtrace/
  cli.py                 # Scriptable commands and export entry points
  tui.py                 # Full-screen exploration UI
  engine.py              # Project lifecycle and scan orchestration
  store.py               # SQLite schema, migrations, transactions
  parser.py              # Safe discovery and AST extraction
  models.py              # Raw scan-time records

  semantic.py            # Scope/name binding, richer symbol relationships
  entrypoints.py         # Entry-point detectors and registration evidence
  architecture.py        # Subsystem/component inference and graph clustering
  paths.py               # Call-path reconstruction and confidence scoring
  dataflow.py            # Interprocedural, bounded data-flow analysis
  adapters/              # Framework-specific static recognizers
    base.py
    argparse.py
    click.py
    fastapi.py
    flask.py
    django.py
    celery.py
  explain.py             # Evidence-grounded summaries and reports
  export.py              # JSON, Mermaid, DOT, Markdown exports
  runtime/               # Optional, explicit instrumentation only
    launcher.py
    collector.py
    protocol.py
```

No adapter may import a scanned project. Adapters only inspect AST/configuration
and known framework conventions.

## Analysis pipeline

### Phase A: discovery and raw parse

Retain the current safe scanner, with these additions:

- Discover `pyproject.toml`, `setup.cfg`, `setup.py`, `requirements*.txt`,
  `tox.ini`, `.env.example`, Dockerfiles, Compose files, and common CI files as
  configuration evidence. They should be read as text, never executed.
- Record package roots, namespace-package candidates, test roots, and source
  roots (`src/`, `lib/`, configured build roots).
- Distinguish production code, tests, generated code, vendored code, examples,
  migrations, and tools. Users must be able to include/exclude each category.
- Fingerprint files to support incremental scans later.

### Phase B: semantic index

Improve from syntactic names to scoped relationships:

- Resolve local assignments, imports, aliases, qualified references, class
  methods, inherited members where bases are known, and `super()` calls.
- Model nested scopes, closures, comprehensions, decorators, properties,
  classmethods, staticmethods, and async functions.
- Capture call arguments, keyword arguments, return annotations, yielded values,
  exception types, context managers, and important side-effect calls.
- Capture module-level constants/configuration and environment-variable access.
- Keep unresolved candidates instead of discarding them.

Resolution must remain bounded and explainable. Dynamic features such as
`getattr`, `importlib`, monkey-patching, metaclasses, and `eval` should produce
an explicit dynamic-boundary finding, not invented call edges.

### Phase C: entry-point detection

Create an `entry_points` table and detectors with evidence.

| Entry-point type | Static evidence |
|---|---|
| Module script | `if __name__ == '__main__'` |
| `python -m` | package `__main__.py` |
| Package console command | `[project.scripts]`, setuptools entry points, Poetry scripts |
| Argparse | parser construction plus `parse_args` and handler dispatch |
| Click/Typer | decorators and command/group registration |
| FastAPI/Flask | route decorators and app/router registration |
| Django | URL patterns, settings, views, management commands, ASGI/WSGI |
| Celery/RQ/worker | task decorators, queue registration, worker configuration |
| Tests | pytest/unittest test functions and fixtures, labelled separately |
| Scheduled jobs | known framework/decorator/configuration patterns |

Each entry point includes its input contract where visible: command name, route,
HTTP method, parameter names, environment/config dependencies, and handler.

### Phase D: architecture reconstruction

Build a weighted directed graph from:

- Internal imports.
- Resolved and candidate calls.
- Class inheritance/composition relationships.
- Entry-point registrations.
- Data/resource boundaries: files, databases, HTTP clients, queues, subprocesses,
  standard input/output, environment variables, and serialization.

Infer components using a mix of directory/package structure and graph cohesion.
Do not force a module into exactly one component when it genuinely bridges two
areas; show a shared/boundary role instead.

For each component generate:

- Name and role.
- Contained modules and key symbols.
- Incoming/outgoing relationships.
- External dependencies/resources.
- Main entry points that reach it.
- Coupling/risk indicators.
- Evidence and confidence for the role label.

### Phase E: execution-path reconstruction

Given an entry point or selected symbol, calculate useful paths without trying
to enumerate every possible Python execution:

- Follow resolved calls first.
- Include ambiguous candidates as expandable branches.
- Stop at configurable depth, recursion boundary, dynamic boundary, or external
  boundary.
- Collapse utility-heavy paths by default and allow expansion.
- Highlight side effects: database query/write, network request, filesystem
  access, queue publish/consume, subprocess, logging, and process exit.
- Show a narrative explanation generated only from recorded facts.

Example path output:

```text
POST /users
  confirmed  api.routes.create_user
  confirmed  services.users.register
  high       repositories.users.insert
  possible   integrations.mailer.send_welcome_email

External boundaries
  confirmed  PostgreSQL write: repositories/users.py:81
  possible   SMTP client call: integrations/mailer.py:42
```

### Phase F: bounded data-flow reconstruction

Start small and preserve accuracy:

1. Keep the existing intra-function events.
2. Connect a direct call's argument expressions to the callee's parameters when
   the callee resolves uniquely.
3. Connect return expressions to the receiving assignment at direct call sites.
4. Track common container field access and dataclass/Pydantic attributes where
   direct evidence exists.
5. Mark unknown/dynamic transformations as boundaries.

The first release should answer “which input reaches this database/network/file
boundary through direct calls?” for straightforward code. It should not claim
whole-program taint analysis or runtime-complete data flow.

## Storage and migration design

Existing scan snapshots remain immutable. Add a schema migration rather than
breaking the current database.

Suggested tables:

```text
entry_points      id, scan_id, kind, name, qualified_symbol_id, input_json,
                  confidence, evidence_json
relationships     id, scan_id, source_id, target_id, kind, confidence,
                  evidence_json
components        id, scan_id, name, role, confidence, summary, evidence_json
component_members component_id, symbol_id/module_id, role
resources         id, scan_id, kind, name, operation, confidence, evidence_json
execution_paths   id, scan_id, entry_point_id, name, summary, confidence
path_steps        path_id, ordinal, relationship_id, symbol_id, resource_id
data_edges        id, scan_id, source_symbol_id, target_symbol_id,
                  source_name, target_name, kind, confidence, evidence_json
reports           id, scan_id, report_kind, content_json, generated_at
```

Use foreign keys and scan-scoped indexes. Persist both the machine-readable
facts and evidence locations; generated summaries should be reproducible from a
scan, not become the only source of truth.

## TUI design

The TUI should open on an overview, not a raw module table.

### Overview screen

```text
Project: example-api             Latest scan: current

Purpose
  HTTP service for managing accounts and notifications. [high]

Entrypoints (4)                  Architecture (6 components)
  POST /users                      API → Services → Persistence
  GET /users/{id}                  CLI → Services → Integrations
  worker: send_email
  CLI: migrate

Risks / unanswered questions
  2 dynamic call boundaries        1 import cycle
  3 unresolved external deps       4 likely entry points without paths
```

The “Purpose” text must link to the modules, symbols, and configuration facts
that support it. If no reliable summary is possible, say so.

### Main navigation

| View | Purpose |
|---|---|
| Overview | Architecture summary, scan health, risks, entry points. |
| Entrypoints | Routes, CLI commands, scripts, workers, and tests. |
| Architecture | Component graph/tree with dependencies and boundaries. |
| Paths | Step-by-step execution paths with confidence and branches. |
| Data | Bounded input-to-boundary data movement view. |
| Symbols | Searchable classes/functions/modules, preserving current detail capability. |
| Resources | Databases, HTTP APIs, queues, filesystem, environment, subprocesses. |
| Findings | Diagnostics plus dynamic boundaries and inference gaps. |
| Scan history | Snapshot comparison and regressions. |

### Symbol detail

Selecting a symbol should show, in one screen:

- Purpose, signature, decorators, source excerpt, and evidence.
- Containing component and role.
- Incoming callers and outgoing calls.
- Entry points that can reach it.
- Direct resources/side effects it touches.
- Input/output data relationships, with confidence.
- “Impact” view: reachable downstream components and reverse dependants.

### Interaction principles

- Keyboard-first; mouse support is additive.
- `Enter` drills in; `Backspace` returns to prior context; `?` displays help.
- Every diagram/table can be filtered to `confirmed`, `high`, or all evidence.
- Long analyses must use background workers and show progress/cancellation.
- An explicit “source evidence” panel prevents black-box-looking explanations.
- Source navigation should work in-terminal first; external-editor launch is an
  optional user-configured action, never automatic.

## CLI and export surface

Retain existing commands and add stable automation-focused commands:

```console
threadtrace overview PROJECT [--format text|json|markdown]
threadtrace entrypoints PROJECT [--kind cli|route|script|worker|test]
threadtrace architecture PROJECT [--format text|json|mermaid|dot]
threadtrace path PROJECT ENTRYPOINT [--depth N] [--confidence LEVEL]
threadtrace impact PROJECT SYMBOL [--depth N]
threadtrace dataflow PROJECT SYMBOL [--to database|http|file|queue]
threadtrace resources PROJECT [--kind database|http|file|queue|env|subprocess]
threadtrace explain PROJECT TARGET [--format markdown|json]
threadtrace compare PROJECT SCAN_A SCAN_B
```

Exports should include source locations and confidence, not only a decorative
diagram. Mermaid and DOT are useful for documentation and CI artifacts; JSON is
the stable integration format.

## Framework adapters and extensibility

Use a small adapter protocol:

```python
class FrameworkAdapter(Protocol):
    name: str

    def matches(self, project: ProjectFacts) -> bool: ...
    def entry_points(self, facts: SemanticFacts) -> list[EntryPoint]: ...
    def relationships(self, facts: SemanticFacts) -> list[Relationship]: ...
    def resources(self, facts: SemanticFacts) -> list[Resource]: ...
```

Start with adapters selected by evidence rather than a large plugin system.
The generic Python, import/call, configuration, resource, and entry-point
analysis must always work first. Then broaden adapter coverage so Thread is
useful across CLI tools, web APIs, automation scripts, data/ML pipelines,
Django applications, and other Python project types. Initial adapter order:

1. Standard Python / `argparse` / console-script configuration.
2. Click and Typer.
3. FastAPI and Flask.
4. Django.
5. Celery and common worker patterns.

Add adapters only with fixtures from realistic projects and a documented
confidence policy.

## Optional runtime tracing (later, never default)

Static analysis cannot fully reconstruct a highly dynamic project. A later
runtime mode can fill gaps only after explicit user action.

```console
threadtrace trace PROJECT -- python -m app.cli import-data sample.csv
```

Rules for this capability:

- It must display a clear execution warning before launch.
- It runs only a command supplied by the user.
- It should support a container/sandbox command template, timeout, output cap,
  working-directory control, environment allowlist, and network isolation where
  the host supports it.
- Trace facts are stored separately from static facts and labelled `runtime`.
- It must never be triggered by scan, TUI startup, or ordinary query commands.

Runtime tracing is a future enhancement, not a dependency for the first reverse
engineering release.

## Future recommendation: evidence-grounded local AI narrative

Do not add an AI model in the next implementation step. Once the static
evidence, confidence model, architecture graph, and export format are stable,
consider an optional future feature that turns **only recorded Thread facts**
into a narrative project briefing.

It should be a recommendation layer, never the source of truth:

- Fully optional and disabled by default.
- Local-model capable, preserving Thread's local-first posture.
- Given structured scan facts, evidence excerpts, and confidence levels rather
  than unrestricted repository access where possible.
- Required to cite the symbols/files/lines supporting every architectural
  statement in its output.
- Required to say “unknown” when scan evidence is insufficient.
- Exported separately from deterministic reports and visibly labelled as an
  AI-assisted interpretation.

This can make the overview feel more like a human reverse-engineering report
without weakening the deterministic static-analysis foundation.

## Delivery roadmap

### v0.2 — Reconstruction foundation

Goal: turn a scan into a credible project overview.

- Schema migration framework and evidence/confidence model.
- Source-root/configuration discovery.
- Semantic symbol/call improvements that retain candidates.
- Console-script, `__main__`, main-guard, and argparse entry points.
- Architecture graph and first component clustering.
- `overview`, `entrypoints`, and `architecture` CLI queries.
- TUI overview, entry-point, architecture, and source-evidence views.
- Mermaid/DOT/JSON architecture export.

Acceptance demo:

```text
Scan Thread itself and show:
  CLI entry point → main dispatcher → Engine → Parser / Store / QueryService
  components: CLI, scan engine, AST parsing, persistence, query/TUI
  direct evidence for every displayed relationship
```

### v0.3 — Paths, impact, and resources

Goal: explain how a selected action travels through the system.

- Path search and cycle-safe graph traversal.
- Caller/callee candidate branches and confidence filtering.
- Impact analysis for a module or symbol.
- Resource-boundary recognition for filesystem, HTTP clients, subprocesses,
  environment reads, and common database patterns.
- TUI path explorer and impact panel.
- `path`, `impact`, and `resources` CLI commands.

Acceptance demo:

```text
Select a CLI command or web route and inspect its direct execution path,
including its external boundary and unresolved/dynamic points.
```

### v0.4 — Framework awareness and bounded data flow

Goal: make common real-world Python applications understandable.

- Click/Typer and FastAPI/Flask adapters.
- Bounded direct-call parameter/return data edges.
- Data-boundary view from entry-point inputs to resource operations.
- Test/fixture indexing separate from production architecture.
- `dataflow` and `explain` CLI commands.

Acceptance demo:

```text
For a FastAPI sample, select a route and see request fields flow through a
service into a repository/database call, with evidence for each direct edge.
```

### v0.5 — Change intelligence and optional runtime traces

Goal: support ongoing maintenance after initial understanding.

- Incremental scans based on content hashes.
- Scan comparison: added/removed entry points, components, paths, resources,
  and diagnostics.
- Optional trace-runner design and a guarded initial implementation.
- Static-versus-runtime path comparison.

## Quality plan

### Test fixtures

Create small, focused fixture projects for:

- `src/` layouts, namespace packages, relative imports, aliases, and cycles.
- Console scripts, `__main__`, argparse, Click/Typer.
- FastAPI/Flask/Django/Celery patterns once adapters are added.
- Dynamic dispatch and intentionally unresolved edges.
- Data flow through direct calls and intentionally unsupported boundaries.
- Bad syntax, unreadable files, non-UTF-8 source, symlink escapes, and large
  generated/vendor directories.

### Test layers

- Unit tests for AST/semantic extraction and confidence assignment.
- SQLite migration tests from every supported schema version.
- Golden-file tests for JSON, Mermaid, DOT, and Markdown reports.
- TUI tests using prompt_toolkit's application test support for navigation, search, filtering,
  rescan state, and evidence drill-down.
- Integration tests against fixture projects.
- Performance tests for 1k, 10k, and 50k Python-file synthetic repositories.

### Safety tests

Continue proving that ordinary scans do not execute target code. Include source
fixtures with import-time writes, subprocess calls, network calls, and malicious
configuration-looking text; scans must only record evidence.

## Performance and usability targets

Initial targets, to be measured and revised with real fixtures:

| Scenario | Target |
|---|---|
| Small project, 100 Python files | First static scan under 3 seconds on a typical laptop. |
| Medium project, 2,000 Python files | First static scan under 30 seconds with visible progress. |
| Re-scan after one file changes | Re-analyse affected facts rather than every file where safe. |
| TUI navigation | Common view changes under 100 ms after scan data is loaded. |
| Export | Deterministic output from the selected completed scan. |

The tool should prefer a partial, clearly labelled result over freezing while
attempting unbounded global inference.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Python dynamism causes false certainty | Evidence, confidence labels, unresolved boundaries, no guessed edges. |
| Framework support becomes endless | Adapter protocol, narrow supported patterns, fixtures before feature claims. |
| Graphs become unreadable | Progressive disclosure: overview → component → path → symbol. |
| Large repos are slow | Incremental content hashes, indexed SQLite queries, background UI work, configurable limits. |
| TUI hides important uncertainty | Show confidence and source evidence as first-class UI elements. |
| Runtime tracing executes unsafe code | Separate command, explicit warning, sandbox configuration, never automatic. |
| New dependencies undermine simple CLI usage | Keep imports lazy where possible and preserve JSON/CLI operations. |

## Confirmed scope decisions

1. Thread should eventually support any Python project family. Generic static
   reconstruction comes first; adapters expand framework-specific precision.
2. The next release is strictly static-only. Runtime tracing remains a clearly
   separated, opt-in future capability.
3. Do not add an AI model in the next step. Keep an evidence-grounded local AI
   narrative as a future recommendation after the deterministic foundation is
   proven.

## Decisions to make before implementation starts

1. Should Mermaid/DOT/Markdown reports be a first-class deliverable, or is the
   TUI the only required human-facing output initially? TUI Mainly

2. Should Thread analyse tests by default, or focus the architecture model on
   production code and provide tests as a separate view? Tests as separate view


