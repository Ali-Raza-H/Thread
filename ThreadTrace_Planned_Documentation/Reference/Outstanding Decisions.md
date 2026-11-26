# Outstanding Decisions

[[v1.0 Product Decisions]] resolves the broad product questions. The items below are grouped by mode so they can be audited and implemented in the right subsystem. Unless a choice is explicitly marked **maintainer decision**, the coding agent may decide it while documenting the outcome under [[v1.0 Product Decisions#Implementation discretion]].

## Static Analysis

1. Capability audit completed in [[Capability Audit]]; update it with each newly verified feature.
2. Define the exact verified CPython/parser matrix and the v1.0 static-framework/metadata adapter baseline.
3. Record the fallback handling for syntax recovery, zip/notebook source, namespace packages, external symlink files, and incomplete metadata.

## Dynamic Analysis

1. Current Listener mode is read-only: it shows recorded state but cannot pause, step, or modify the target. Selected dynamic controls require a later audited capability change.
2. Reconciled: Host/Listener now uses a current-user Unix-domain socket where supported. A Host in an environment that prohibits local sockets retains a journal but does not offer Listener mode; no remote transport is permitted.
3. Define default tracing/capture budgets, trace-saving behaviour, and feature-specific Full/Partial/Unsupported status after measurement.
4. Define the tested event/adapter support matrix for CPython versions, threads, asyncio, multiprocessing, child processes, network/database/filesystem adapters, and coverage import.

## Hybrid Analysis

1. Reconciled: the capability matrix is recorded in [[Capability Audit]] and callable-specific status is available through `threadtrace experiment capability`.
2. Reconciled: ordinary functions/classes/methods, generators, and coroutines are Full in an explicit Hybrid child process; closure-cell mutation and native implementation replacement report Partial when Python does not expose them.
3. Reconciled: named private workspaces retain reviewed diffs, definitions, and run history; exports contain the definition, diff, and results. Source is never overwritten, execution requires consent, and external state is observed rather than falsely promised to roll back.

## Shared systems and interfaces

1. **ThreadTrace** is the current public brand and `threadtrace` remains the CLI command. Branding is editable; any future approved rename must include a documented compatibility/migration plan.
2. Reconciled: `LICENSE` contains the complete GPL-3.0-only text, and package metadata declares the same SPDX identifier.
3. Reconciled: `0.7.0a1` is the alpha version in package metadata and user-facing documentation. The distribution, import package, and CLI command are all named `threadtrace`. [CHANGELOG.md](../../CHANGELOG.md) is the canonical release history.
4. Implement/configure the YAML schema, keybindings, command palette, search modes, editor behaviour, browser delivery sequence, terminal fallback, and benchmark thresholds under implementation discretion.
5. v1.0 includes the read-only loopback live server. Self-contained HTML reports are supported as exports; a native webview remains later work.
