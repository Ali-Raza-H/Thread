# ThreadTrace Implementation Completion Plan

> [!abstract] Purpose
> This is the implementation playbook for completing the accepted ThreadTrace
> v1.0 scope. It translates the current code/documentation audit into small,
> testable tasks. [[v1.0 Product Decisions]] remains the product authority. If
> this plan conflicts with that document, the product decision wins.

## How to use this plan

Work on one task ID at a time. Do not combine unrelated tasks merely because
they touch the same file. A task is complete only when its implementation,
tests, capability status, and user-facing documentation agree.

For every task:

1. Read the linked requirements and the listed implementation files completely.
2. Reproduce the current failure or write a minimal fixture showing the missing
   behaviour.
3. Add or extend `unittest.TestCase` coverage in `tests/test_engine.py`. Split
   tests into another `tests/test_*.py` file only when the original file becomes
   difficult to navigate.
4. Implement the smallest clear change that satisfies the requirement. Static
   analysis must never import or execute the target project.
5. Preserve evidence and uncertainty. Do not upgrade an inferred relationship
   to a fact to make a test pass.
6. Update `capabilities.py`, [[Capability Audit]], `USER_MANUAL.md`, and the
   relevant feature/design document when behaviour or limits change.
7. Run the focused test, then the full suite. Also exercise the affected CLI
   with `--format json` when it has CLI output.
8. Inspect `git diff` and confirm that no unrelated user changes were altered.

## Ranking system

Complexity and size are separate:

| Value | Complexity: reasoning/risk | Size: implementation surface |
| --- | --- | --- |
| 1 / XS | Mechanical, isolated | One small helper or documentation-only change |
| 2 / S | Local behaviour with clear tests | A few functions in one subsystem |
| 3 / M | Multiple edge cases or files | A subsystem-sized change |
| 4 / L | Cross-cutting design or compatibility work | Several modules, schemas, and interfaces |
| 5 / XL | Interpreter/framework-sensitive architecture | Multi-stage work spanning most layers |

The size labels are relative to this repository. They are not time estimates.

## Execution order

The table is ordered by dependency and risk, not by convenience. Complete P0
correctness/privacy work before expanding the feature surface.

| Order | ID | Task | Priority | Complexity | Size | Main dependency |
| ---: | --- | --- | --- | ---: | --- | --- |
| 1 | TT-001 | Repair browser database threading | P0 | 2 | S | None |
| 2 | TT-002 | Correct Hybrid baseline comparison | P0 | 3 | S | None |
| 3 | TT-003 | Report the terminating exception | P0 | 3 | S | None |
| 4 | TT-004 | Make resource status and redaction truthful | P0 | 3 | M | None |
| 5 | TT-005 | Stop false confirmed call resolution | P0 | 4 | M | None |
| 6 | TT-006 | Honour launch interpreters and environments | P0 | 3 | M | None |
| 7 | TT-007 | Make experiment diffs and deletion complete | P0 | 3 | M | None |
| 8 | TT-008 | Make CLI formats and failures honest | P1 | 2 | S | None |
| 9 | TT-009 | Bound and clean up Host/Listener sessions | P0 | 4 | L | TT-004 |
| 10 | TT-010 | Complete evidence and capability migration | P0 | 4 | L | TT-005 |
| 11 | TT-011 | Expand configuration and persistence safety | P1 | 4 | L | TT-010 |
| 12 | TT-012 | Build the complete source-discovery model | P1 | 5 | XL | TT-011 |
| 13 | TT-013 | Build Python-aware import and symbol resolution | P1 | 5 | XL | TT-012 |
| 14 | TT-014 | Complete metadata recognition and staleness | P1 | 3 | M | TT-011, TT-012 |
| 15 | TT-015 | Add verified entry-point/framework analysers | P1 | 5 | XL | TT-013 |
| 16 | TT-016 | Correct impact, data flow, slicing, and CFG | P1 | 5 | XL | TT-013 |
| 17 | TT-017 | Add structural complexity, resources, and advanced Python facts | P2 | 5 | XL | TT-013, TT-016 |
| 18 | TT-018 | Expand runtime instrumentation and event schema | P1 | 5 | XL | TT-010, TT-011 |
| 19 | TT-019 | Add identity-aware values and debugger controls | P1 | 5 | XL | TT-018 |
| 20 | TT-020 | Add process, thread, task, and wait correlation | P1 | 5 | XL | TT-018, TT-019 |
| 21 | TT-021 | Complete runtime resource adapters and preflight | P1 | 5 | XL | TT-004, TT-018 |
| 22 | TT-022 | Make Hybrid precedence and replay deterministic | P1 | 4 | L | TT-002, TT-006, TT-007 |
| 23 | TT-023 | Complete Hybrid adapters and exploration budgets | P2 | 4 | L | TT-021, TT-022 |
| 24 | TT-024 | Build the documented browser graph interface | P1 | 5 | XL | TT-001, TT-010, TT-013, TT-016, TT-018 |
| 25 | TT-025 | Complete TUI fallbacks, editing, and accessibility | P1 | 4 | L | TT-010, TT-016, TT-018 |
| 26 | TT-026 | Implement safe provider-independent AI | P1 | 5 | L | TT-010, TT-011 |
| 27 | TT-027 | Complete exports, investigations, and Python API | P2 | 4 | L | TT-010, TT-011, TT-024 |
| 28 | TT-028 | Build compatibility, accuracy, and performance gates | P0 | 5 | XL | All feature tasks |
| 29 | TT-029 | Reconcile packaging and all documentation | P1 | 2 | M | Implemented tasks |
| 30 | TT-030 | Run the v1 release-capability gate | P0 | 4 | L | TT-001 through TT-029 |

## Phase 1 — correctness, privacy, and truthful output

### TT-001 — Repair browser database threading

**Implementation status:** Complete (2026-11-20).

**Goal:** Every browser page/API request returns stored data without sharing an
unsafe SQLite connection between threads.

**Requirements:** [[Browser Interface]], [[v1.0 Product Decisions#UI, browser, safety, and quality]].

**Current files:** `src/threadtrace/browser.py`, `store.py`, `engine.py`,
`query.py`, `tests/test_engine.py`.

**Steps:**

1. Add an integration test that starts `BrowserServer` on loopback, requests
   `/api/overview`, `/api/architecture`, and `/api/capabilities`, and parses each
   JSON response. Skip only when the environment forbids loopback sockets.
2. Give each request/thread its own `Store`/`QueryService`, or replace the
   threaded server with a deliberately single-threaded server. Do not set
   `check_same_thread=False` while continuing to share one unprotected
   connection.
3. Ensure request-owned connections close even when payload construction fails.
4. Return bounded JSON errors instead of terminating the request handler with a
   traceback.
5. Test concurrent requests if the threaded design remains.

**Done when:** The real HTTP test passes, no SQLite object crosses its creating
thread, and the manual accurately describes any concurrency limit.

### TT-002 — Correct Hybrid baseline comparison

**Implementation status:** Complete (2026-11-20).

**Goal:** A comparison must execute an unmodified baseline and the selected
modified experiment, including workspace-file patches.

**Requirements:** [[Hybrid Analysis#Experiments and comparisons]],
[[v1.0 Product Decisions#Hybrid experiments]].

**Current files:** `src/threadtrace/engine.py`, `hybrid.py`, `store.py`.

**Steps:**

1. Add a regression test that patches a copied file from one return value to
   another and requires a comparison difference.
2. Store the registered source root or a pristine experiment snapshot as the
   baseline source. Never infer a baseline by passing empty configuration to an
   already modified workspace.
3. Run baseline and modified executions in separate fresh child processes.
4. Record the exact source hashes, configuration, interpreter, and workspace
   role (`baseline` or `modified`) in the comparison result.
5. Detect and report when the registered source changed after experiment
   creation instead of silently comparing mismatched revisions.
6. Add tests for configuration-only changes, file patches, created/deleted
   files, exceptions, side effects, and incomparable values.

**Done when:** Every documented change type can affect the modified side only,
and the result identifies both compared source revisions.

### TT-003 — Report the terminating exception

**Implementation status:** Complete (2026-11-20).

**Goal:** Failure reports identify the exception that caused termination while
retaining earlier handled exceptions as timeline evidence.

**Requirements:** [[Dynamic Analysis#Execution tracing]],
[[v1.0 Product Decisions#Dynamic execution and tracing]].

**Current files:** `src/threadtrace/runtime/failure.py`, `launcher.py`,
`session.py`.

**Steps:**

1. Add fixtures for a single fatal exception, handled-then-fatal exceptions,
   nested propagation, re-raise, `SystemExit`, `KeyboardInterrupt`, and timeout.
2. Parse the final traceback chain and correlate its innermost fatal location
   with trace events.
3. Use trace events for captured state/call-chain detail only after correlation;
   do not select the first exception merely because it is earliest.
4. Add separate fields for `terminating_exception` and bounded
   `observed_exceptions` if needed for clarity.
5. Preserve `unknown` when trace and stderr cannot be reconciled.

**Done when:** The named exception, message, location, traceback, and call chain
refer to the same terminating failure.

### TT-004 — Make resource status and redaction truthful

**Implementation status:** Complete (2026-11-20).

**Goal:** Persist successful, failed, and attempted resource operations
accurately, and redact every documented credential location.

**Requirements:** [[Dynamic Analysis#Network, database, and filesystem activity]],
[[Safety and Performance]], [[v1.0 Product Decisions#Runtime resource observation]].

**Current files:** `src/threadtrace/runtime/adapters.py`, `failure.py`,
`query.py`, `hybrid_adapters.py`.

**Steps:**

1. Add failure tests for delete, rename, chmod, mkdir, link, symlink, truncate,
   import, HTTP, database, serialization, and file-descriptor operations.
2. Treat audit-hook events as `attempted` unless a wrapper can observe success.
   Never publish an audit event as `completed` before the operation returns.
3. Where safe, wrap the operation and publish one final completed/failed event.
   Avoid duplicate audit and wrapper records using an explicit correlation ID.
4. Redact URL userinfo, query values, headers, cookies, database parameters,
   environment-derived values, exception messages, and nested structures.
5. Add tests for percent-encoded credentials, mixed-case keys, nested mappings,
   long values, bytes, malformed URLs, and redaction-disabled confirmation.
6. Make truncation explicit in the persisted representation.

**Done when:** A failed operation cannot appear completed and no known secret
fixture appears in default stored/exported JSON.

### TT-005 — Stop false confirmed call resolution

**Implementation status:** Complete (2026-11-20).

**Goal:** A call is statically resolved only through valid Python name binding
evidence.

**Requirements:** [[Static Analysis#Dependencies and relationships]],
[[System Architecture#Evidence and confidence]],
[[v1.0 Product Decisions#Static-analysis requirements]].

**Current files:** `src/threadtrace/engine.py`, `parser.py`,
`reconstruction.py`, `models.py`.

**Steps:**

1. Add regression fixtures where two modules contain the same function name,
   one module contains an unimported unique name, a local shadows an import, and
   an alias/re-export changes the visible binding.
2. Remove the project-wide bare-name fallback from confirmed resolution.
3. Resolve bare names only from lexical scope, module definitions, explicit
   imports, builtins, supported class scope, or documented re-export evidence.
4. Store unresolved and ambiguous candidates without creating confirmed direct
   call relationships.
5. Emit candidate relationships only with heuristic evidence and a reason.
6. Re-run graph, path, data-flow, impact, diagnostics, and architecture tests,
   because each consumes call relationships.

**Done when:** Undefined names never resolve to unrelated project symbols and
all confirmed calls have inspectable binding evidence.

### TT-006 — Honour launch interpreters and environments

**Implementation status:** Complete (2026-11-20).

**Goal:** Saved profiles execute exactly the configured interpreter, arguments,
working directory, and environment policy.

**Requirements:** [[Dynamic Analysis#Execution management]],
[[Shared Systems#Project and scan management]],
[[v1.0 Product Decisions#Installation, registration, and configuration]].

**Current files:** `src/threadtrace/cli.py`, `engine.py`, `runtime/launcher.py`,
`runtime/session.py`, `store.py`.

**Steps:**

1. Define whether `interpreter` replaces a leading `python` token or prefixes a
   module/script target; document the rule and reject ambiguous profiles.
2. Add CLI input for nonsecret environment overrides. Secret values must come
   from inherited environment or a restrictive supported secret source, not
   ordinary YAML/database display.
3. Resolve executable paths with the run environment's `PATH` before launch.
4. Record requested command, resolved executable, interpreter version,
   arguments, working directory, inherited/overridden variable names, and
   instrumentation collisions. Do not record secret values.
5. Pass the profile environment/interpreter through trace, Runtime, Host, and
   Hybrid entry points where the selected profile is used.
6. Test spaces in paths, missing interpreters, virtual environments, relative
   working directories, environment deletion, and PATH collisions.

**Done when:** A profile using a deliberately different Python interpreter and
environment proves that those exact settings reached the child.

### TT-007 — Make experiment diffs and deletion complete

**Implementation status:** Complete (2026-11-20).

**Goal:** Experiment history shows all workspace changes and cleanup never
leaves untracked private copies.

**Requirements:** [[Hybrid Analysis#Temporary code patches]],
[[Data Model and Persistence]], [[v1.0 Product Decisions#Hybrid experiments]].

**Current files:** `src/threadtrace/hybrid.py`, `engine.py`, `store.py`.

**Steps:**

1. Make workspace diff report modified, created, deleted, renamed, binary, and
   unreadable files. Bound previews while retaining hashes and metadata.
2. Syntax-check Python file patches before writing. Return diagnostics without
   destroying the previous valid workspace file.
3. Write patches atomically and keep a patch-history record with before/after
   hashes.
4. On project removal, enumerate its experiment paths, validate every path with
   the same safety checks as `remove_experiment`, then remove or explicitly
   preserve them according to a documented user choice.
5. If filesystem cleanup fails, keep enough database state to retry; do not
   silently delete the only record pointing to residual data.
6. Add crash/interruption and symlink safety tests.

**Done when:** Diff/export account for every workspace change and project
deletion cannot orphan an experiment silently.

### TT-008 — Make CLI formats and failures honest

**Implementation status:** Complete (2026-11-20).

**Goal:** Every accepted CLI option either works for that command or fails with
a concise documented error.

**Requirements:** [[Shared Systems#Reporting and export]],
[[v1.0 Product Decisions#Persistence, export, and investigations]].

**Current files:** `src/threadtrace/cli.py`, `browser.py`, `store.py`.

**Steps:**

1. Move format choices to command-specific parsers, or add a capability map
   validated before rendering.
2. Reject unsupported Mermaid/DOT output instead of falling through to text.
3. Escape Mermaid/DOT labels, identifiers, quotes, backslashes, line breaks,
   and renderer control syntax.
4. Construct `Store` inside the CLI exception boundary and convert expected
   database/configuration/migration failures to exit code 2 without traceback.
5. Ensure stdout remains machine-readable for JSON/CSV/graph output and place
   warnings only on stderr.
6. Add CLI tests for every command/format pair advertised in the manual.

**Done when:** The parser help, renderer, exit code, stdout, and manual agree.

### TT-009 — Bound and clean up Host/Listener sessions

**Implementation status:** Complete (2026-11-20).

**Goal:** Host/Listener remains responsive and private for long sessions and
always selects a valid active host.

**Requirements:** [[Dynamic Analysis#Execution management]],
[[v1.0 Product Decisions#Concurrency, processes, and local Host/Listener]],
[[v1.0 Product Decisions#UI, browser, safety, and quality]].

**Current files:** `src/threadtrace/runtime/host.py`, `live_tui.py`, `cli.py`.

**Steps:**

1. Require explicit execution confirmation consistent with `trace` and
   experiment commands.
2. Add paginated event responses by event ordinal and encoded-byte limit. Never
   return all remaining events in one IPC message.
3. Report journal truncation, dropped events, and listener lag explicitly.
4. Filter default listener selection to running sessions. Let users explicitly
   select finished history if supported.
5. Render transport/status errors in the Listener UI.
6. Remove expired session directories using validated current-user paths and a
   documented retention policy.
7. On stop/interrupt, signal the process group gracefully, wait, then provide a
   bounded emergency kill path.
8. Implement and test the Windows loopback-only IPC fallback before claiming
   Windows Host/Listener support.
9. Test listener takeover expiry, malformed messages, wrong session identity,
   permissions, oversized events, reconnection, and host termination.

**Done when:** A session exceeding 1 MB can be followed incrementally without
message failure, stale sessions are not selected as live, and descendants are
not left running after stop.

## Phase 2 — shared foundations

### TT-010 — Complete evidence and capability migration

**Implementation status:** Complete (2026-11-20).

**Goal:** Every user-visible fact has the v1 evidence vocabulary and contextual
support status without losing original provenance.

**Requirements:** [[System Architecture#Evidence and confidence]],
[[Capability Model]], [[v1.0 Product Decisions#Evidence and confidence]].

**Current files:** `src/threadtrace/models.py`, `store.py`, `query.py`,
`capabilities.py`, `reconstruction.py`, all UI/export modules.

**Steps:**

1. Define one versioned evidence record containing class, mechanism, source/run
   reference, limitations, and optional legacy label.
2. Migrate `confirmed/high/possible/unknown` to `statically resolved`,
   `heuristically predicted`, or `unknown`; runtime evidence becomes `observed
   at runtime` only when an event actually completed/occurred.
3. Preserve legacy confidence in migration metadata until old databases no
   longer require it.
4. Attach evidence records consistently to modules, symbols, calls, imports,
   entry points, resources, data edges, findings, graphs, coverage, comparisons,
   and AI interpretations.
5. Replace the global capability list with queries accepting project, scan,
   interpreter/run, feature, and optional entity.
6. Derive status from tested mechanism availability and observed limitations;
   do not claim `Full` because a code path exists.
7. Add schema migration and old-database tests.

**Done when:** No public output needs a caller to guess whether a value is an
observation, static resolution, heuristic prediction, or unknown.

### TT-011 — Expand configuration and persistence safety

**Implementation status:** Complete (2026-11-20).

**Goal:** Provide the nonsecret configuration, storage controls, migrations,
permissions, and deletion lifecycle accepted for v1.

**Requirements:** [[Data Model and Persistence]],
[[v1.0 Product Decisions#Installation, registration, and configuration]],
[[v1.0 Product Decisions#Persistence, export, and investigations]].

**Current files:** `src/threadtrace/config.py`, `store.py`, `engine.py`, `cli.py`.

**Steps:**

1. Publish a versioned YAML schema for database/storage, discovery rules,
   interpreters/profiles, TUI preferences, browser settings, runtime budgets,
   retention, export defaults, and nonsecret AI settings.
2. Use a real safe YAML parser or deliberately document and test the accepted
   subset. Reject duplicate keys, unknown fields, secrets, and invalid types.
3. Store per-project preferences separately from global defaults.
4. Add configurable storage location, retention, trace-saving policy, quotas,
   and usage reporting.
5. Make migrations transactional. Back up before destructive conversion and
   retain/recover the backup when migration fails.
6. Add busy timeout/locking coordination for readers versus scans/writes.
7. Create application directories and database/export files with restrictive
   permissions where the platform supports them.
8. Add confirmed deletion for trace/report/project/all-data records and report
   exactly which external workspaces/files were removed or retained.
9. Independently version application, database, trace protocol, and export
   schemas.

**Done when:** An old database can be safely migrated or restored, concurrent
read/write behaviour is deterministic, and all retained data has a visible
lifecycle.

## Phase 3 — static-analysis completion

### TT-012 — Build the complete source-discovery model

**Implementation status:** Complete (2026-11-20).

**Goal:** Discover every accepted source form without broad or unsafe traversal.

**Requirements:** [[Python Compatibility]],
[[v1.0 Product Decisions#Compatibility and source model]].

**Current files:** `src/threadtrace/parser.py`, `engine.py`, `models.py`,
`store.py`.

**Steps:**

1. Introduce a source-unit abstraction with logical module, origin kind,
   original/resolved path, content hash, parser/version, and capability status.
2. Apply configurable include/exclude globs and explicit paths after safe
   default exclusions.
3. Support ordinary and PEP 420 namespace packages without requiring
   `__init__.py`.
4. Follow explicit external files only; never recursively traverse an external
   directory reached through a symlink. Detect loops and store both paths.
5. Read safe zip-source members with archive/member path validation and bounded
   decompression.
6. Extract Python notebook cells with cell identity and line/source mapping.
7. Accept explicitly supplied generated Python text with a stable virtual
   origin. Keep REPL/source-free/native boundaries Partial or Unsupported.
8. Preserve per-file failures and partial scans for every source kind.
9. Classify tests, metadata, resources, generated source, native boundaries,
   and unclassified files.

**Done when:** Deterministic fixtures cover all accepted source forms, traversal
cannot escape through an unapproved directory, and every skipped source has a
reason.

### TT-013 — Build Python-aware import and symbol resolution

**Implementation status:** Complete (2026-11-20).

**Goal:** Resolve names using Python semantics while preserving uncertainty for
dynamic behaviour.

**Requirements:** [[Static Analysis#Source-code analysis]],
[[v1.0 Product Decisions#Static-analysis requirements]].

**Current files:** `src/threadtrace/parser.py`, `engine.py`, `models.py`,
`reconstruction.py`, `query.py`.

**Steps:**

1. Persist lexical scopes and definitions for modules, functions, classes,
   comprehensions, parameters, locals, globals, nonlocals, constants, and
   imports.
2. Preserve aliases, qualified definitions, references, assignments,
   decorators, forwarding imports, re-exports, and `__all__`.
3. Resolve local/relative imports first, then configured editable/installed
   packages, standard library, and namespace portions using the selected
   interpreter's import metadata without importing target modules.
4. Represent wildcard imports as a known export set or explicit uncertainty.
5. Follow reached third-party symbols and necessary transitive calls only;
   never scan an entire environment by default.
6. Add symbol-reference and usage queries, including original and visible names.
7. Model reflection, dynamic imports, `eval`, `exec`, monkey patching, and
   unresolved attributes as separate boundary facts.
8. Add precision fixtures for shadowing, overloads, descriptors, inherited
   attributes, namespace packages, editable installs, conditional imports, and
   `TYPE_CHECKING`.

**Done when:** Every resolved import/call/reference has a binding path and every
unresolved dynamic case remains explicitly uncertain.

### TT-014 — Complete metadata recognition and staleness

**Implementation status:** Complete (2026-11-20).

**Goal:** Recognise all accepted project metadata and stale results when any
analysis input changes.

**Requirements:** [[Shared Systems#Project and scan management]],
[[v1.0 Product Decisions#Installation, registration, and configuration]].

**Current files:** `src/threadtrace/engine.py`, `query.py`, `store.py`.

**Steps:**

1. Parse `pyproject.toml`, `requirements*.txt`, `setup.py`, `setup.cfg`,
   `Pipfile`, `Pipfile.lock`, `poetry.lock`, `uv.lock`, and supported Conda
   environment files without executing them.
2. Preserve unsupported/unrecognised metadata as unclassified files with hashes.
3. Store dependency groups, markers, extras, direct URLs/paths, source file, and
   parse limitations rather than only distribution names.
4. Hash effective discovery configuration, selected interpreter identity,
   metadata files, and relevant source units into the scan input record.
5. Report staleness by cause and affected capability instead of one Boolean.
6. Test malformed, partial, platform-specific, included, and recursively
   referenced requirements.

**Done when:** Changing a lockfile, interpreter, include rule, or source file
produces an evidence-backed stale reason.

### TT-015 — Add verified entry-point/framework analysers

**Implementation status:** Complete (2026-11-20).

**Goal:** Replace broad decorator/name matching with framework-specific,
evidence-tested analysers and a safe generic fallback.

**Requirements:** [[Static Analysis#Execution and entry-point discovery]],
[[Static Analysis#Web applications and APIs]],
[[v1.0 Product Decisions#Static-analysis requirements]].

**Current files:** `src/threadtrace/reconstruction.py`, `query.py`, `parser.py`.

**Steps:**

1. Define an analyser interface returning framework, version/variant,
   registration evidence, handler, inputs, limits, and capability status.
2. Implement fixtures for argparse, Click, Typer, optparse, and common command
   registries, including flags, positionals, subcommands, defaults, and handlers.
3. Implement Flask, Django, FastAPI/Starlette, and aiohttp routes, middleware,
   dependencies, validation, auth/authz, rate limiting, and serialization where
   static evidence permits.
4. Implement raw DB-API/SQLite, SQLAlchemy, and Django ORM query/data-access
   relationships without importing frameworks.
5. Add lifecycle hook, worker/task, scheduled job, plugin, test, and module/script
   entry models.
6. Remove the rule that treats any `.get()`/`.post()` decorator as a route.
7. Keep unknown frameworks under generic Python analysis with unknown/heuristic
   status.

**Done when:** Precision/recall fixtures exist per claimed framework and an
unrelated decorator cannot become a high-confidence HTTP route.

### TT-016 — Correct impact, data flow, slicing, and CFG

**Implementation status:** Complete (2026-11-20).

**Goal:** Provide conservative but semantically meaningful program-relationship
analyses.

**Requirements:** [[Static Analysis#Dependencies and relationships]],
[[Static Analysis#Control flow and algorithms]],
[[Static Analysis#Static data flow]].

**Current files:** `src/threadtrace/query.py`, `flowgraph.py`, `reconstruction.py`,
`parser.py`, `models.py`, `store.py`.

**Steps:**

1. Make change impact traverse callers, import dependants, inheritance users,
   references, configured entry points, and affected resources. Label direction.
2. Build CFG basic blocks/edges for branches, loops, break/continue,
   return/raise, try/except/else/finally, with/async-with, match, yield, and
   conservative call-exception edges.
3. Build def-use records per scope and direct interprocedural parameter/return
   edges only when binding is resolved.
4. Model attributes/subscripts and mutation conservatively; do not merge values
   solely because their text names match.
5. Implement forward/backward slices from graph reachability with explicit
   alias/dynamic boundaries.
6. Correlate resource sources/sinks with data edges where supported.
7. Add graph-size budgets and truncation metadata.

**Done when:** Directional impact and slices match hand-checked fixtures and CFG
output represents normal and exceptional exits without claiming runtime paths.

### TT-017 — Add structural complexity, resources, and advanced Python facts

**Implementation status:** Complete (2026-11-20).

**Goal:** Finish the remaining static catalogue without using AI to invent
answers.

**Requirements:** [[Static Analysis#Control flow and algorithms]],
[[Static Analysis#Resources and environment]],
[[Static Analysis#Advanced Python structure]],
[[v1.0 Product Decisions#Static-analysis requirements]].

**Current files:** `src/threadtrace/parser.py`, `advanced.py`, `reconstruction.py`,
`query.py`.

**Steps:**

1. Keep cyclomatic complexity separate from time/space estimates.
2. Implement deterministic rules for straight-line work, loops/nested loops,
   selected collection operations, recursion, and resolvable called-function
   costs. Record assumptions and return unknown when bounds are unsupported.
3. Detect literal/config/f-string/template/package-resource paths and preserve
   unresolved dynamic expressions.
4. Expand resource classes to configuration, structured data, text, media,
   serialization, compression, database, service, environment, and interpreter
   requirements.
5. Record decorators, generators, async constructs, context managers, closures,
   descriptors, properties, metaclasses, dynamic attributes, native boundaries,
   and concurrency creation sites.
6. Add algorithm-pattern results only when structural evidence is defined and
   tested. Keep them heuristic.
7. Do not implement sensitive-data taint analysis; it is explicitly outside v1.

**Done when:** Complexity output explains its bound/assumptions, and every
advanced/resource fact is evidence-backed or explicitly unknown.

## Phase 4 — runtime and Hybrid completion

### TT-018 — Expand runtime instrumentation and event schema

**Implementation status:** Complete (2026-11-20).

**Goal:** Select compatible tracing mechanisms per interpreter and record the
required event categories with versioned, bounded output.

**Requirements:** [[Dynamic Analysis#Execution tracing]],
[[Dynamic Analysis#Runtime control flow and coverage]],
[[Dynamic Analysis#Runtime profiling]], [[Instrumentation Research]].

**Current files:** `src/threadtrace/runtime/launcher.py`, `schema.py`, `session.py`,
`host.py`, `store.py`, `query.py`.

**Steps:**

1. Define a versioned event schema for call, line, return, exception lifecycle,
   yield/resume, import, C call, audit, process/thread/task, resource, and
   profiling events.
2. Add an instrumentation capability selector for `sys.settrace`, supported
   `sys.monitoring`, `sys.setprofile`, audit hooks, and adapters.
3. Keep project code as the default recording scope; add explicit reached
   third-party expansion and separate recording/presentation filters.
4. Record instrumentation environment additions/collisions without leaking
   values.
5. Implement timeout states: pause/inspect, continue with new timeout, graceful
   stop, and emergency process-group termination.
6. Add compatible `coverage.py` import with granularity/provenance labels.
7. Add per-function duration, call count, cumulative time, hot path, and
   supported memory metrics without presenting instrumentation overhead as
   target time.
8. Stream events rather than reading whole journals; enforce event/byte/storage
   budgets with visible truncation.

**Done when:** Each interpreter/run reports which mechanisms and event types are
Full, Partial, or Unsupported, and all persisted events conform to the schema.

### TT-019 — Add identity-aware values and debugger controls

**Goal:** Make state history and debugging safe, deterministic, and explicit.

**Requirements:** [[Dynamic Analysis#Interactive debugging and navigation]],
[[Dynamic Analysis#Variables and objects]],
[[v1.0 Product Decisions#Dynamic execution and tracing]].

**Current files:** `src/threadtrace/runtime/session.py`, `runtime/values.py`,
`runtime_tui.py`, `live_tui.py`, `models.py`.

**Steps:**

1. Implement cycle-aware per-run object IDs and configurable depth/item/byte
   budgets.
2. Distinguish binding creation, rebinding, deletion, observed in-place
   mutation, and unknown/native state.
3. Store locals, globals, arguments, returns, attributes, and collection
   summaries with identity links where observable.
4. Parse breakpoint conditions with a restricted read-only AST grammar. Reject
   calls, assignments, comprehensions, imports, dunder traversal, and arbitrary
   evaluation.
5. Add tracepoints and feasible watchpoints using the same safe expression
   rules.
6. Bind breakpoints to source hash plus file/line/symbol identity and
   invalidate/revalidate them after changes.
7. Make stepping/pause state thread/task-specific instead of one global mode.
8. Add event search/history navigation and clearly state that history does not
   restore real process state.

**Done when:** Cycles and mutations cannot crash serialization, unsafe
conditions cannot execute, and two threads can be stepped without consuming
each other's controls.

**Implementation status:** Complete (2026-11-20).

### TT-020 — Add process, thread, task, and wait correlation

**Goal:** Produce one coherent concurrent timeline across supported Python
execution contexts.

**Requirements:** [[Dynamic Analysis#Concurrency and asynchronous execution]],
[[v1.0 Product Decisions#Concurrency, processes, and local Host/Listener]].

**Current files:** `src/threadtrace/runtime/session.py`, `host.py`, `waits.py`,
`concurrency.py`, `processes.py`, `query.py`, runtime UIs.

**Steps:**

1. Record PID, PPID, executable, argv, lifecycle, run ID, and instrumentation
   status for the root and child processes.
2. Propagate instrumentation to supported Python multiprocessing/subprocess
   children while clearly labelling uninstrumented Python and non-Python
   children.
3. Record thread creation/termination and stable per-run thread IDs.
4. Record asyncio task creation/completion and coroutine suspend/resume where
   the mechanism supports it.
5. Add adapters/events for standard locks, conditions, semaphores, joins,
   queues, futures, task waits, and async waits.
6. Build heuristic wait/deadlock/race findings only from documented observable
   patterns; never label them deterministic without proof.
7. Add concurrent timeline queries and UI lanes with bounded buffering.
8. Test threads, asyncio, multiprocessing, subprocess trees, crashes, timeouts,
   and mixed instrumentable/uninstrumentable children.

**Done when:** Parent/child and thread/task identities remain consistent for a
run and capability output explains every unobserved boundary.

**Implementation status:** Complete (2026-11-20).

### TT-021 — Complete runtime resource adapters and preflight

**Goal:** Provide verified, semantics-preserving resource visibility for the v1
adapter list.

**Requirements:** [[Dynamic Analysis#Network, database, and filesystem activity]],
[[v1.0 Product Decisions#Runtime resource observation]].

**Current files:** `src/threadtrace/runtime/adapters.py`, `query.py`,
`failure.py`.

**Steps:**

1. Create adapter-specific fixtures/version bounds for Requests, HTTPX, aiohttp,
   urllib/urllib3, sqlite3, supported DB-API, SQLAlchemy, and Django ORM.
2. Capture metadata by default and content only when explicitly selected.
   Never consume an unread stream/cursor or change iterator behaviour.
3. Record transactions, commit, rollback, and Partial savepoint/nested semantics.
4. Add supported FTP and generic Python-level TCP boundary events without TLS
   interception claims.
5. Cover filesystem open/read/write/create/rename/delete/stat/permission/import/
   resource operations with originating call stacks.
6. Implement a static best-effort pre-run side-effect summary and clearly state
   that it is not a security guarantee.
7. Apply TT-004 redaction/truncation consistently before persistence and export.
8. Measure semantic and performance impact per adapter and expose unsupported
   versions.

**Done when:** Every adapter claim has an acceptance fixture proving both the
captured evidence and preservation of target stream/cursor behaviour.

**Implementation status:** Complete (2026-11-25).

### TT-022 — Make Hybrid precedence and replay deterministic

**Goal:** Enforce documented rule precedence, conflicts, interpreter selection,
and reproducible controls.

**Requirements:** [[Hybrid Analysis#Deterministic and controlled replay]],
[[v1.0 Product Decisions#Hybrid experiments]].

**Current files:** `src/threadtrace/hybrid.py`, `engine.py`, `hybrid_adapters.py`.

**Steps:**

1. Encode precedence as: per-call/per-experiment override, named patch, adapter
   mock/replay, original behaviour.
2. Validate conflicts across patches, substitutions, overrides, aliases,
   line-local/global edits, and adapters. Reject same-level collisions.
3. Report unresolved pre-bound aliases and active-frame limits before execution.
4. Run harness workers with the selected profile/interpreter.
5. Control random seeds, time/date, UUID, environment, and supported scheduling,
   including already imported aliases where safely discoverable.
6. Record which nondeterministic inputs remain uncontrolled.
7. Make collection/value truncation explicit and comparable; return `unknown`
   rather than equality when data was omitted.
8. Use process groups and bounded cleanup for harness/workspace commands.
9. Bound filesystem snapshots by streaming hashes rather than holding complete
   files in memory.

**Done when:** The same supported controlled run reproduces structurally, every
conflict is diagnosed before execution, and omitted data never produces false
equality.

**Implementation status:** Complete (2026-11-25).

### TT-023 — Complete Hybrid adapters and exploration budgets

**Goal:** Finish controlled dependency replay and keep automated exploration
bounded and reproducible.

**Requirements:** [[Hybrid Analysis#External dependency mocking]],
[[Hybrid Analysis#Automated input exploration]],
[[v1.0 Product Decisions#Hybrid experiments]].

**Current files:** `src/threadtrace/hybrid_adapters.py`, `hybrid.py`, `engine.py`.

**Steps:**

1. Reuse verified runtime adapter semantics where possible instead of creating
   incompatible Hybrid-only records.
2. Add HTTP/database/filesystem replay for the verified adapters, including
   failures, latency where supported, transactions, and explicit pass-through.
3. Ensure mocked filesystem/network/process behaviour cannot silently perform
   the real operation when a rule claims replacement or denial.
4. Add per-case and global timeout, case count, event count, output, memory, and
   storage budgets.
5. Keep boundary presets deterministic. Validate reviewed user generators and
   label arbitrary-expression generators as explicit experiment execution.
6. Preserve minimal reproducible cases for distinct returns, exceptions, paths,
   timeouts, crashes, and side effects.
7. Keep coverage-guided fuzzing and general fuzzing marked later/experimental.

**Done when:** Adapter replay cannot leak through unexpectedly and exploration
always stops at visible global limits with reproducible retained inputs.

**Implementation status:** Complete (2026-11-25).

## Phase 5 — interfaces, AI, and portability

### TT-024 — Build the documented browser graph interface

**Implementation status:** Complete (2026-11-25).

**Goal:** Deliver the required read-only local graph explorer on top of the
shared analysis model.

**Requirements:** [[Browser Interface]], [[Shared Systems#Visualisation]],
[[v1.0 Product Decisions#UI, browser, safety, and quality]].

**Current files:** `src/threadtrace/browser.py`, `query.py`, `cli.py`.

**Steps:**

1. Define paginated JSON endpoints for dependency/import, call, class/module,
   AST, data-flow, control-flow, and evidence-supported execution-flow graphs.
2. Generate Mermaid from the same graph model and validate node/edge references.
3. Add pan, zoom, search, evidence/type filters, focus, hop expansion, source
   navigation, and export.
4. Use progressive/paginated rendering for large graphs rather than a hard
   cutoff.
5. Render evidence class, capability status, truncation, stale state, and source
   location for selected nodes/edges.
6. Keep the browser read-only and loopback-only; add appropriate local access
   controls/session token if threat modelling shows same-user cross-process
   requests need protection.
7. Keep CSP/no-store headers and avoid injecting unescaped project strings.
8. Add real browser/API rendering tests, not only payload-construction tests.

**Done when:** Every required graph family can be searched, filtered, navigated,
and exported from a real browser request without executing the target.

### TT-025 — Complete TUI fallbacks, editing, and accessibility

**Implementation status:** Complete (2026-11-25).

**Goal:** Make terminal operation reliable across terminal sizes and display
capabilities while making source edits safe.

**Requirements:** [[TUI Design]], [[Shared Systems#Code editing]],
[[v1.0 Product Decisions#UI, browser, safety, and quality]].

**Current files:** `src/threadtrace/tui.py`, `runtime_tui.py`, `live_tui.py`,
`process_tui.py`, `config.py`.

**Steps:**

1. Define and test the minimum normal layout and a compact single-pane fallback.
2. Add configurable keybindings with collision validation and a complete command
   palette so every important action is discoverable without memorized keys.
3. Add Unicode/ASCII and colour/no-colour modes with non-colour status cues.
4. Syntax-check Python source before save, show the diff, request explicit
   confirmation, verify the open-file hash, and write atomically.
5. Refresh all affected views after save/rescan and preserve the user's location
   where possible.
6. Add syntax highlighting plus definition/reference navigation backed by the
   shared model.
7. Remove dead Guided Tour/Walk paths from production code while keeping the
   archive document historical.
8. Test resize, focus, keybindings, command palette, screen-reader-friendly text,
   no-colour output, ASCII output, editing conflicts, and unsaved changes.

**Done when:** The TUI remains usable below the normal size, all actions have a
discoverable route, and invalid/conflicting source cannot be silently saved.

### TT-026 — Implement safe provider-independent AI

**Implementation status:** Complete (2026-11-25).

**Goal:** Support optional Ollama and OpenAI-compatible providers without
weakening privacy or evidence rules.

**Requirements:** [[Shared Systems#AI integration]],
[[v1.0 Product Decisions#AI behaviour]].

**Current files:** `src/threadtrace/ai.py`, `query.py`, `cli.py`, `config.py`,
`store.py`.

**Steps:**

1. Define provider configuration: type, base URL, model, nonsecret settings,
   temperature, token limit, and supported parameters.
2. Read credentials from environment or an explicitly restrictive local `.env`;
   never print, store, export, or include them in prompts.
3. Distinguish loopback/local from remote endpoints. Require fresh confirmation
   showing the exact redacted payload before every remote request.
4. Apply bounded best-effort redaction to selected context and prohibit whole
   environment/repository submission by default.
5. Persist exact prompt, approved context manifest, response, provider/model,
   settings, timestamps, and evidence links with manual deletion/retention.
6. Support custom templates with validation and clear unsupported-option errors.
7. Require machine-validated structured output for explanations/diagrams.
   Validate referenced symbols and graph syntax against stored analysis.
8. Label every AI statement as interpretation and keep AI read-only against
   source and execution.
9. Add fake-provider tests; automated tests must not require network access.

**Done when:** A remote request cannot occur without exact per-request approval,
and malformed or invented structured references are rejected.

### TT-027 — Complete exports, investigations, and Python API

**Implementation status:** Complete (2026-11-25).

**Goal:** Produce portable, versioned artifacts and resumable investigation
workspaces.

**Requirements:** [[Shared Systems#Investigation workspaces]],
[[Shared Systems#Reporting and export]],
[[v1.0 Product Decisions#Persistence, export, and investigations]].

**Current files:** `src/threadtrace/cli.py`, `engine.py`, `query.py`, `store.py`,
`__init__.py`.

**Steps:**

1. Define one export envelope with export version, application version, project/
   scan/run IDs, source hashes, generation time, capability/evidence metadata,
   truncation, and payload type.
2. Apply it consistently to JSON, Markdown, CSV, HTML, Mermaid, and experiment
   exports while keeping JSON machine-readable.
3. Make promised HTML self-contained and escape all target-controlled content.
4. Store investigations with files, symbols, events, notes, hypotheses, graphs,
   relationships, traces, experiments, and bookmarks.
5. Add investigation resume/export/import with source-revision mismatch warnings.
6. Retain required historical source snippets/snapshots with hashes; never move
   historical locations silently.
7. Document and test a selected experimental read-only Python API for projects,
   scans, queries, traces, and exports. Keep Hybrid execution APIs explicitly
   separate.

**Done when:** An exported investigation can be opened independently, identifies
its source revision and limitations, and round-trips without losing references.

## Phase 6 — verification, packaging, and release

### TT-028 — Build compatibility, accuracy, and performance gates

**Implementation status:** Complete (2026-11-25).

**Goal:** Make every support claim traceable to deterministic automated evidence.

**Requirements:** [[Testing and Acceptance Criteria]], [[Python Compatibility]],
[[Capability Model]], [[v1.0 Product Decisions#UI, browser, safety, and quality]].

**Current files:** `tests/`, `pyproject.toml`, capability documentation.

**Steps:**

1. Add a documented development/test dependency group so the documented pytest
   command works after setup.
2. Test every supported CPython version before advertising it in classifiers or
   capability output. Remove unverified classifiers.
3. Build deterministic fixtures for scripts, packages, namespaces, malformed
   projects, CLI frameworks, web frameworks, databases, threads, asyncio,
   multiprocessing, imports, exceptions, patches, and adapters.
4. Separate exact-feature tests from heuristic precision/recall corpora. Record
   expected false-positive/false-negative policy.
5. Add safety tests for redaction, confirmation, path traversal, symlinks,
   cleanup, process groups, local access controls, malicious labels, and corrupt
   databases/exports.
6. Add browser/TUI interaction tests and resize/accessibility cases.
7. Benchmark scan, query, trace, adapter, UI, storage, and large-graph behaviour
   before setting numerical thresholds.
8. Run tests offline and prohibit target imports/execution in static fixtures.
9. Produce a test-to-capability manifest used by capability reporting.

**Done when:** Every Full claim names passing fixtures on a verified interpreter
and every Partial/Unsupported claim names its tested boundary.

### TT-029 — Reconcile packaging and all documentation

**Goal:** Remove contradictions and make installation artifacts match the actual
verified product.

**Requirements:** [[ThreadTrace]], [[Capability Audit]],
[[v1.0 Product Decisions#Product boundary]].

**Current files:** `README.md`, `USER_MANUAL.md`, `CHANGELOG.md`, `LICENSE`,
`pyproject.toml`, `.dev.json`, `src/thread_trace.egg-info/`, all planned docs.

**Steps:**

1. Replace the short licence notice with the complete GPL-3.0-only text and keep
   package metadata consistent.
2. Describe ThreadTrace as static, dynamic, and Hybrid; state clearly that only
   static scans never execute the target.
3. Use the v1 evidence vocabulary everywhere and explain legacy fields only in
   migration notes.
4. Regenerate package metadata from the final README/build rather than manually
   maintaining stale `PKG-INFO`.
5. Reconcile brand/name/version across `.dev.json`, package metadata, CLI,
   application directories, docs, and migration guidance. Correct spelling and
   stale command examples.
6. Change every Full/Partial/Unsupported statement to match TT-028 evidence.
7. Document configuration schema, data locations, permissions, retention,
   deletion, safety, exact execution confirmations, and unsupported boundaries.
8. Keep archived proposals clearly historical and remove dead public references
   to archived commands.
9. Verify every manual command automatically for parsing/help and selected
   deterministic output.

**Done when:** README, manual, changelog, package metadata, capability audit, CLI
help, and code report the same name, version, behaviour, limits, and licence.

### TT-030 — Run the v1 release-capability gate

**Goal:** Decide release readiness from evidence rather than feature labels.

**Requirements:** [[Capability Audit]], [[Capability Model]],
[[v1.0 Product Decisions]].

**Steps:**

1. Start from a clean checkout and install only through the documented process.
2. Run the full offline suite on every supported interpreter/platform target.
3. Run migration tests from every retained schema/trace/export version.
4. Run redaction, deletion, process cleanup, local-interface, and confirmation
   safety suites.
5. Run heuristic accuracy corpora and publish their measured boundaries.
6. Run performance benchmarks and confirm configured bounds/truncation under
   large inputs.
7. Execute every README/manual quick-start command on deterministic fixtures.
8. Review each capability row. Mark Full only when all named acceptance tests
   pass; otherwise mark Partial or Unsupported with the exact boundary.
9. Confirm source distributions/wheels contain the correct licence,
   documentation, modules, and no private/generated development data.
10. Record the final evidence in [[Capability Audit]] and the changelog before
    creating a release tag.

**Done when:** There are no undocumented Full claims, no P0 defects, no failing
supported-interpreter fixtures, and all release artifacts reproduce the same
verified capability report.

## Explicit non-tasks for v1

Do not expand the plan with these unless the product authority changes:

- Sensitive-data static taint analysis.
- Remote process attachment or remote public server APIs.
- TLS interception or packet decryption.
- General coverage-guided fuzzing.
- Portable PID control or runtime state checkpoints.
- A mandatory cross-platform OS sandbox. Optional sandbox/container integration
  may be added, but ordinary execution must continue to state that it is
  unsandboxed.
- WebSockets and complex protocols beyond generic Python analysis.

## Complexity ranking

From hardest to easiest:

1. **Complexity 5:** TT-012, TT-013, TT-015 through TT-021, TT-024, TT-026,
   TT-028.
2. **Complexity 4:** TT-005, TT-009 through TT-011, TT-022, TT-023, TT-025,
   TT-027, TT-030.
3. **Complexity 3:** TT-002 through TT-004, TT-006, TT-007, TT-014.
4. **Complexity 2:** TT-001, TT-008, TT-029.

## Size ranking

From largest to smallest:

1. **XL:** TT-012, TT-013, TT-015 through TT-021, TT-024, TT-028.
2. **L:** TT-009 through TT-011, TT-022, TT-023, TT-025 through TT-027,
   TT-030.
3. **M:** TT-004 through TT-007, TT-014, TT-029.
4. **S:** TT-001 through TT-003, TT-008.

## Completion checklist for each task

- [ ] Requirement and current implementation were read completely.
- [ ] A failing regression or missing-capability fixture was added first.
- [ ] Static analysis did not import or execute target code.
- [ ] Evidence class and capability status are accurate.
- [ ] Error, truncation, stale, and unsupported cases are visible.
- [ ] CLI JSON remains valid and warnings remain on stderr.
- [ ] Relevant focused tests pass.
- [ ] The complete test suite passes offline.
- [ ] User manual and capability audit match the implementation.
- [ ] No unrelated user changes were overwritten.
