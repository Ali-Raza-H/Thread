# Feature Research

> [!summary]
> Structured research references for product ideas that may inform ThreadTrace. These notes record applicability, not requirements.

Related: [[Shared Systems#Visualisation|Visualisation]], [[Shared Systems#AI integration|AI integration]], [[System Architecture]], [[Outstanding Decisions]].

## GitDiagram

### Source links

- [GitDiagram: architecture-diagram guide](https://gitdiagram.com/visualize-codebase)
- [GitDiagram official site](https://gitdiagram.com/)
- [GitDiagram source repository](https://github.com/ahmedkhaleel2004/gitdiagram)

### What it does

GitDiagram converts a repository into an AI-generated architecture overview and interactive diagram. Its documentation describes using repository structure, README material, and a limited selection of source files, then validating diagram components against the repository. It is an architecture-oriented summary rather than a complete dependency graph.

### Observed approach

#### 1. Ingestion

- Gather repository metadata, a recursive file tree, README, and related documentation.
- Exclude non-source or low-value paths before context construction.
- Store the gathered context for later analysis and diagram production.

#### 2. Source context

- Prioritise selected files to fit a limited model context.
- Deprioritise configuration, helpers, and tests where appropriate.
- Read selected materials in parallel and resolve imports according to project conventions.

#### 3. Generation and presentation

- Supply repository context to a language model for a written architectural explanation and diagram.
- Validate diagram references against known repository paths.
- Render a navigable graph and support diagram export.

### Applicability to ThreadTrace

| Idea | ThreadTrace use | Difference / constraint |
| --- | --- | --- |
| Repository ingestion | Extend a [[Glossary#Scan|scan]] with README and selected documentation discovery. | ThreadTrace analyses local Python projects, not only GitHub repositories. |
| Context selection | Rank files and entities for AI summaries or architecture views. | Prefer AST/relationship evidence over filename-only ranking; expose confidence. |
| Import resolution | Use Python-aware import relationships in dependency views. | Static analysis can resolve project-specific Python semantics more deeply, but must preserve uncertainty for dynamic imports. |
| AI diagrams | Generate optional architecture explanations/diagrams from shared analysis evidence. | AI output must be labelled as interpretation and not replace static/dynamic graphs. |
| Diagram validation | Check nodes/links against the analysis model before rendering. | Runtime and inferred relationships need their evidence class preserved. |

### Decisions informed

- Include project documentation as scan input where useful.
- Build visualisations from the shared analysis model first; AI may summarise or arrange evidence but must not invent verified relationships.
- Do not adopt GitDiagram's repository scope, hosting model, or filename-only ranking as ThreadTrace requirements.

### Follow-up research

- Compare Python AST and import-resolution libraries for evidence-backed repository ranking.
- Investigate Mermaid/graph validation and rendering options suitable for local HTML reports.
- Define a provenance format for AI-created summaries and diagrams.
