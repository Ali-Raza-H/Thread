# Static Analysis

> [!summary]
> Analyse a Python project's structure, possible behaviour, dependencies, resources, and execution paths without executing its code.

Related: [[Dynamic Analysis]], [[Hybrid Analysis]], [[Shared Systems]], [[Python Compatibility]].

## Source-code analysis

- Discover Python files, packages, subpackages, and their relationships.
- Extract functions: names, definitions, parameters, annotations, return statements, decorators, and locations.
- Extract classes: methods, attributes, constructors, inheritance, and decorators.
- Identify variables, constants, assignments, scopes, references, and statically inferable values and types.
- Identify imports: modules, classes, functions, variables, aliases, and relative imports.
- Cross-reference symbol definitions, references, callers, and usages across the project.
- Categorise discovered entities into navigable views rather than requiring raw-file inspection.

## Execution and entry-point discovery

- Detect `if __name__ == "__main__":`, module entry points, package scripts, and likely application entry logic.
- Identify execution commands such as `python main.py` and `python -m package`.
- Discover CLI positional arguments, flags, subcommands, and their handlers; map them to processing functions and locations.
- Detect framework lifecycle hooks, event handlers, scheduled jobs, plugin registrations, and test entry points.

> [!note]
> Python executes top-level statements in the selected module. ThreadTrace should distinguish that interpreter behaviour from an application's meaningful entry-point logic (such as a `main()` call guarded by `__main__`).

## Web applications and APIs

- Detect supported web frameworks.
- Discover HTTP routes, methods, endpoints, handlers, templates, response generation, and dependencies.
- Identify authentication, authorisation, middleware, rate limiting, request validation, response serialisation, and supported WebSocket/framework entry points.
- Reconstruct route, service, and data-access relationships.

## Dependencies and relationships

- Build module dependency and function/method call graphs.
- Identify inheritance and composition relationships.
- Follow imported functions and variables from definitions to usage locations.
- Identify circular dependencies and dependencies on external resources.
- Estimate change impact for a function, class, or module.
- Identify potentially unreachable code from known entry points, with confidence warnings.

## Control flow and algorithms

- Generate control-flow graphs for functions and methods.
- Identify conditionals, loops, early returns, exception handlers, and possible paths.
- Recognise common algorithms and computational patterns.
- Estimate time and auxiliary-space complexity in Big O notation, explain the reasoning, and label uncertainty caused by dynamic behaviour or external dependencies.

## Static data flow

- Identify where values are defined, read, modified, and passed between functions.
- Construct possible data-flow relationships, including statically determinable parameter/return relationships, sources, and destinations.
- Support static program slicing for a selected value or operation.
- Identify possible sensitive-data paths into database queries, shell execution, and network requests.

## Resources and environment

- Discover JSON, YAML, XML, CSV, text, image, audio, and other referenced resources.
- Identify database connections, configured database resources, external services, and libraries.
- Track source-referenced paths and code that reads, writes, creates, deletes, or modifies files.
- Discover environment variables, configuration loading, interpreter requirements, and dependency requirements from project metadata.
- Identify serialisation, encoding, decoding, compression, and deserialisation operations.

## Advanced Python structure

- Identify decorators and their relationship to decorated objects.
- Analyse generators, async functions, context managers, closures, descriptors, properties, metaclasses, and dynamic attributes.
- Detect reflection (`getattr`, `setattr`), `eval`, `exec`, dynamic imports, and monkey patching.
- Identify thread, process, and asyncio-task creation sites.
- Identify native-extension boundaries and code Static Analysis cannot fully inspect.

## Limits

Static findings represent source evidence and potential behaviour; they do not prove that a path will execute. Refer to [[Capability Model]] and [[Safety and Performance#Instrumentation limitations]].
