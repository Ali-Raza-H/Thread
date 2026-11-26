# System Architecture

## Core-engine independence

The static analyser, dynamic instrumentation system, experiment engine, and persistence layer expose reusable interfaces. The [[TUI Design|TUI]], [[Browser Interface|browser companion]], and future integrations consume those interfaces rather than owning analysis logic.

## Progressive instrumentation

Tracing detail is configurable to control overhead and storage:

1. Function-level events only.
2. Line-level tracing.
3. Variable and object-state capture.
4. External-resource tracing.
5. Detailed selected-library tracing.

Capturing every detail continuously is not a product guarantee. See [[Safety and Performance]].

Instrumentation can combine `sys.monitoring` where compatible, `sys.settrace`, `sys.setprofile`, audit hooks, library adapters, and explicit Hybrid AST/bytecode transformations. Their roles, limitations, and maintenance constraints are documented in [[Instrumentation Research]].

## Evidence and confidence

Every presented finding must indicate its evidence class:

| Class | Meaning |
| --- | --- |
| observed at runtime | Directly recorded during a run. |
| statically resolved | Determined from source or project metadata. |
| heuristically predicted | A conservative analysis result, including complexity or impact estimates; not proof of execution. |
| unknown | ThreadTrace cannot safely infer the result. |
| AI explanation | Generated interpretation, separate from analysis evidence. |

ThreadTrace must not present an inference as observed behaviour. The alpha's
`confirmed/high/possible/unknown` values survive only in compatibility fields
and filters; see [[v1.0 Product Decisions#Evidence and confidence]].

## Behavioural integrity

Dynamic instrumentation aims to preserve target behaviour, but can affect timing, performance, scheduling, and other observable characteristics. Therefore “does not modify program behaviour” is a design goal, not an absolute guarantee.

## Source protection

Hybrid patches, mocks, and overrides remain separate from source edits. Saved experiment configurations must not modify the target codebase. See [[Hybrid Analysis]] and [[Safety and Performance]].
