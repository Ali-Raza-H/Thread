# Repository Guidelines

## Project Structure & Module Organization

ThreadTrace is a Python 3.11+ terminal-first static-analysis tool. Application
code lives in `src/threadtrace/`; `cli.py` defines the `threadtrace` command,
while `engine.py`, `parser.py`, `query.py`, `store.py`, and `models.py` contain
the analysis pipeline and persistence logic. Terminal interfaces are in the
`*_tui.py` modules. Runtime tracing support is isolated in
`src/threadtrace/runtime/`. Keep public package exports in `__init__.py` and
the module launcher in `__main__.py`.

Tests are in `tests/`, currently centered in `test_engine.py`. Product and
design notes belong in `ThreadTrace_Planned_Documentation/`; user-facing usage
documentation is `USER_MANUAL.md`.

## Build, Test, and Development Commands

Create an environment and install the project in editable mode:

```console
uv venv && uv pip install -e ".[dev]"
# or: .venv/bin/python -m pip install -e ".[dev]"
```

Run the full test suite with `python -m pytest` (or
`python -m unittest discover -s tests`). Run a focused test while iterating with
`python -m pytest tests/test_engine.py -k dataflow`. Check the CLI after
installation with `threadtrace --help`. The project has no configured
formatter or separate build command; avoid introducing one without documenting
it in `pyproject.toml`.

## Coding Style & Naming Conventions

Use four-space indentation, type hints, and `from __future__ import
annotations`, following the existing modules. Use `snake_case` for functions,
variables, and modules; `PascalCase` for classes; and uppercase names for
constants. Prefer small, explicit helpers and standard-library facilities.
Static-analysis results must preserve evidence and confidence rather than make
unsupported claims. Keep scan behavior non-executing: never import or run the
target project during static analysis.
Coding should be done as human. Made readable to humans and as simple as possible. If 5 lines of codes can be reduced to 1 it should be done (unless it makes code less readable)
DO NOT ADD ANY REFERENCES THAT THIS CODE WAS MADE WITH AI IT IS IMPERATIVE THAT IS DONE BY ME. And remove any references that this code was made with AI since I need to do it manually (Company policy for sum reason :/) wierd files and names should be removed and renamed. Every line should have a purpose. Simplicty over Complexity.

## Testing Guidelines

Add or extend `unittest.TestCase` tests for each behavior change. Name test
methods `test_<behavior>` and use temporary project fixtures, as in
`EngineTests.setUp`, so tests do not depend on local files or network access.
Cover CLI output where applicable, including `--format json`; keep JSON output
machine-readable. The `tests/test_gates.py` suite owns the offline fixture
corpus, capability manifest, accuracy corpus, safety failures, and measured
performance bounds; reference the tests it names in
`src/threadtrace/test_evidence.py` when changing a capability claim.

## Commit & Pull Request Guidelines

History uses short, imperative summaries (for example, `Git ignore for
pycache`); write concise present-tense subjects that describe one change.
Before opening a PR, run the full test suite, explain the user-visible impact,
link related issues when available, and include terminal output or screenshots
for CLI/TUI changes. Call out schema, runtime-safety, or documentation changes
explicitly.
