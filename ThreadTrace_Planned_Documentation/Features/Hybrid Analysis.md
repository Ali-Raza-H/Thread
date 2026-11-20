# Hybrid Analysis

> [!summary]
> Experiment with program behaviour through controlled execution, temporary modifications, substituted inputs, and comparisons.

Related: [[Static Analysis]], [[Dynamic Analysis]], [[Safety and Performance]], [[Data Model and Persistence]].

## Isolated function and class execution

- Select individual functions/methods, provide typed positional/keyword inputs, reuse recorded arguments, and execute step by step.
- Inspect returns, exceptions, side effects, and dependencies/context supplied by execution harnesses.
- Construct class instances from supplied arguments, invoke methods on persistent instances, and inspect attributes between calls.

## Function disabling and substitution

- Temporarily disable selected functions or methods, substitute stubs, return `None`/a selected value/a recorded return, raise a chosen exception, or pass through with logging.
- Record intercepted calls and restore original behaviour after each experiment.

## Library disabling and dependency substitution

- Simulate unavailable imports, replace modules with mocks, stub imported functions/methods, test fallback paths, consider transitive effects, and substitute selected external integrations.

## Runtime overrides

- Pause execution to modify selected variables and override function arguments.
- Define repeatable value-substitution rules and edge-case presets (`None`, empty strings, negative values, large integers, unexpected types).
- Observe subsequent behaviour and save overrides with experiments.

## Temporary code patches

- Replace selected function/method implementations temporarily, including feasible class-level patches.
- Compile replacement code without writing original source; map patches to displayed source.
- Account for decorators, closures, bound methods, and imported references; restore original implementations at experiment end.

> [!important]
> [[Shared Systems#Code editing|Source editing]] and temporary patches are separate operations. Experiments must not silently overwrite source.

## External dependency mocking

- Mock HTTP responses, database responses/operations, and selected filesystem interactions.
- Simulate network failures, timeouts, and unexpected responses.
- Replace external-service integrations and record mocked operations used in a run.

## Automated input exploration

- Generate inputs for selected functions, apply boundary/invalid presets, explore branches, detect crashes/unexpected exceptions, and preserve inputs that produce distinct behaviour as reproducible cases.

> [!todo] Later implementation
> Coverage-guided fuzzing is an advanced capability.

## Deterministic and controlled replay

- Replay recorded inputs, control randomness with seeds, freeze/simulate time where supported, reuse recorded external responses, and compare controlled executions.
- Identify nondeterminism that prevents reliable replay.

## Experiments and comparisons

- Create named, repeatable [[Glossary#Experiment|experiments]] containing disabled functions, overrides, patches, mocks, and inputs.
- Compare baseline and modified runs: return values, exceptions, output, side effects, state, filesystem/database/network activity.
- Produce structured behavioural-difference reports and persist experiment definitions independently of source code.
- Record the registered source revision when an experiment is created. A
  comparison refuses a changed registered source instead of silently using a
  mismatched baseline.
- Execute fresh temporary copies of the verified registered baseline and the
  selected modified workspace in separate child processes. Record each role,
  source hash manifest, configuration, and interpreter in the result.
