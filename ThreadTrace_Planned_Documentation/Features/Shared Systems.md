# Shared Systems

> [!summary]
> Common services used by Static, Dynamic, and Hybrid analysis. The engine remains independent from the TUI and browser interface.

Related: [[System Architecture]], [[Data Model and Persistence]], [[Safety and Performance]].

## Project and scan management

- Register Python projects by directory or entry file and associate an interpreter or virtual environment.
- Save project configuration and analysis preferences.
- Persist static scans with file hashes and timestamps; detect changed files, mark results stale, and support explicit rescans.
- Preserve scan and run history; associate each run with its source revision and scan.

## AI integration

- Support local models through Ollama and OpenAI Python SDK-compatible providers.
- Configure provider, model, and connection settings.
- On request, summarise functions/classes/modules/files; explain algorithms, code, call chains, execution behaviour, exceptions/traces, architecture, and experiment differences.
- Clearly distinguish AI interpretations from verified static or runtime evidence.

## Code editing

- Provide an integrated editor for full files and individual functions, methods, or classes without requiring the full file to be displayed.
- Support syntax highlighting, navigation to definitions/references, modification detection, and stale-scan marking.
- Keep real source edits distinct from [[Hybrid Analysis#Temporary code patches|temporary Hybrid patches]].

## Visualisation

- Module dependency, function call, class inheritance, control-flow, data-flow, architecture, and dependency-impact graphs.
- Process/thread and execution timelines, runtime-coverage views, and experiment comparisons.
- Graph filters and focused-subgraph exploration.

See [[Browser Interface]] for delivery of visualisations and [[TUI Design]] for primary control.

## Investigation workspaces

- Save investigations with bookmarked files, functions, events, notes, hypotheses, selected graphs, relationships, traces, and experiments.
- Resume investigations and export findings.

## Reporting and export

- Export static results, dynamic traces, experiment configurations/comparisons, structured JSON, diagrams, and visualisations.
- Generate HTML reports and compare source versions or scans.

## Unified analysis model

ThreadTrace represents findings through a model independent from either interface:

| Category | Examples |
| --- | --- |
| Entities | Modules, classes, functions, variables, objects, processes, resources |
| Relationships | Imports, calls, inherits, reads, writes, creates, modifies |
| Events | Calls, returns, exceptions, network requests, file accesses |
| State | Recorded values, object attributes, execution context |
| Evidence | Static findings, runtime observations, inferred relationships, AI explanations |

The model is elaborated in [[Data Model and Persistence]].
