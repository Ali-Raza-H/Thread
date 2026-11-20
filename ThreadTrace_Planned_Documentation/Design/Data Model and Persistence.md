# Data Model and Persistence

## Core records

| Record                   | Purpose         |                                                 |
| ------------------------ | --------------- | ----------------------------------------------- |
| [[Glossary#Project       | Project]]       | Registered codebase and configuration.          |
| [[Glossary#Scan          | Scan]]          | Persisted static-analysis snapshot.             |
| [[Glossary#Run           | Run]]           | Execution of target code.                       |
| [[Glossary#Trace         | Trace]]         | Events recorded during an instrumented run.     |
| [[Glossary#Experiment    | Experiment]]    | Saved controlled behavioural changes.           |
| [[Glossary#Investigation | Investigation]] | Saved findings, notes, traces, and experiments. |

## Shared analysis model

Persist entities, relationships, events, state, and evidence as described in [[Shared Systems#Unified analysis model]]. Source revisions, file hashes, scan timestamps, configuration, capture limits, filters, and redaction state should be associated with relevant records.

## Integrity requirements

- Preserve historical scans and runs; mark stale results after relevant source changes.
- Associate runs with the source revision and scan used.
- Keep experiment configurations independent from source code and clearly distinguish recorded from live state.
- Store an experiment's creation-time baseline source manifest. Persist both
  compared source revisions, configurations, interpreter details, and workspace
  roles with every behavioural comparison.
- Apply capture/redaction policies before persisting sensitive values.
- Store sufficient provenance to show why a relationship or conclusion appears in the interface.
