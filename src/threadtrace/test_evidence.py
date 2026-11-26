"""Automated evidence behind each documented capability claim."""

from __future__ import annotations

VERIFIED_PYTHON = "CPython 3.11"

CAPABILITY_TESTS: dict[str, tuple[str, ...]] = {
    "Python source, symbols, imports, calls, entry points, and resources": (
        "test_scans_symbols_calls_classes_and_flow",
        "test_fixture_families_scan_offline_without_target_execution",
        "test_heuristic_corpus_measures_precision_and_recall",
        "test_reconstruction_detects_entry_points_resources_and_data_flow",
    ),
    "Source discovery and provenance": (
        "test_discovers_namespace_external_archive_notebook_and_generated_sources",
        "test_discovery_rejects_directory_symlinks_and_classifies_files",
        "test_scan_groups_module_file_and_package_name_collisions",
    ),
    "Project metadata and staleness": (
        "test_scan_reads_common_dependency_metadata_without_executing_it",
        "test_metadata_parsers_preserve_groups_markers_and_partial_failures",
        "test_staleness_reports_source_metadata_discovery_and_interpreter_causes",
    ),
    "Python-aware bindings, references, and usage": (
        "test_call_resolution_requires_python_binding_evidence",
        "test_python_aware_bindings_keep_shadowing_reexports_and_uncertainty",
        "test_heuristic_corpus_measures_precision_and_recall",
    ),
    "Static paths, impact, data flow, slices, architecture, and control flow": (
        "test_impact_traverses_upstream_downstream_imports_inheritance_and_references",
        "test_slices_use_scope_boundaries_mutation_and_resolved_interprocedural_edges",
        "test_control_flow_models_loops_exceptions_finally_match_and_generators",
        "test_file_and_code_graphs_retain_link_reasons",
    ),
    "Structural complexity, algorithms, and advanced Python facts": (
        "test_complexity_separates_cyclomatic_count_from_bounded_costs",
        "test_advanced_facts_and_expanded_resource_classes_are_evidence_backed",
        "test_bad_source_side_effects_cycles_and_libraries_are_reported",
    ),
    "Controlled local CPython tracing and debugging": (
        "test_runtime_event_schema_and_instrumentation_selection_are_bounded",
        "test_controlled_runtime_session_steps_and_summarises_values",
        "test_runtime_values_are_cycle_safe_and_debugger_expressions_are_read_only",
        "test_runtime_failure_report_preserves_traceback_evidence",
        "test_runtime_breakpoint_pauses_a_continued_project_run",
    ),
    "Process, thread, task, and wait correlation": (
        "test_runtime_concurrency_correlates_processes_threads_and_waits",
        "test_runtime_asyncio_tasks_record_creation_and_lifecycle",
        "test_runtime_step_controls_are_thread_specific",
    ),
    "Instrumentation selection, event schema, and profiling": (
        "test_runtime_event_schema_and_instrumentation_selection_are_bounded",
        "test_runtime_timeout_can_be_extended_or_gracefully_stopped",
        "test_trace_profile_and_coverage_py_import_keep_provenance",
    ),
    "Native trace-derived coverage": (
        "test_runtime_trace_coverage_marks_observed_source_lines",
        "test_trace_profile_and_coverage_py_import_keep_provenance",
    ),
    "Runtime resource adapters": (
        "test_runtime_resource_adapters_capture_redacted_side_effect_timeline",
        "test_runtime_resource_failures_never_appear_completed",
        "test_runtime_tcp_ftp_savepoints_status_and_static_preflight",
        "test_runtime_redacts_every_documented_credential_location_and_marks_truncation",
    ),
    "Local Host/Listener sessions": (
        "test_runtime_host_paginates_and_reports_dropped_events",
        "test_runtime_host_limits_pages_and_reports_journal_truncation",
        "test_host_listener_uses_one_current_user_socket",
        "test_runtime_host_stops_descendant_processes",
    ),
    "Existing-process attachment and child-process inheritance": (
        "test_process_discovery_lists_only_current_user_python_metadata",
    ),
    "Copy-based patches, callable harnesses, controlled substitutions, replay, and comparisons": (
        "test_full_hybrid_harness_supports_live_overrides_async_generators_typed_values_and_alias_patches",
        "test_full_hybrid_adapters_exploration_side_effects_and_deterministic_replay",
        "test_experiment_comparison_uses_pristine_baseline_and_records_revisions",
        "test_experiment_preflight_precedence_replay_controls_and_honest_truncation",
        "test_project_removal_cleans_or_preserves_experiment_workspaces_safely",
    ),
    "Configuration and persistence lifecycle": (
        "test_configuration_is_nonsecret_and_loopback_only",
        "test_storage_usage_pruning_and_explicit_deletion",
        "test_failed_migration_restores_the_previous_database",
        "test_database_permissions_and_busy_timeout",
        "test_corrupt_database_and_export_inputs_fail_cleanly",
    ),
    "Launch profiles, investigations, and trace bookmarks": (
        "test_profiles_and_investigations_are_project_scoped",
        "test_investigations_capture_resume_export_and_import_references",
        "test_saved_profile_executes_interpreter_environment_and_working_directory",
    ),
    "Compatibility and automated evidence": (
        "test_fixture_families_scan_offline_without_target_execution",
        "test_heuristic_corpus_measures_precision_and_recall",
        "test_advertised_metadata_matches_verified_interpreter",
        "test_capability_manifest_names_existing_offline_tests",
        "test_scan_query_export_and_storage_gates_are_bounded",
        "test_corrupt_database_and_export_inputs_fail_cleanly",
    ),
    "Portable exports and read-only Python API": (
        "test_exports_share_one_version_envelope_across_formats",
        "test_investigation_cli_and_read_only_python_api",
        "test_graph_exports_escape_project_controlled_labels",
        "test_experiment_diff_reports_every_workspace_change",
    ),
    "Optional provider-independent AI assistance": (
        "test_ai_provider_redaction_approval_and_validation",
        "test_ai_exchange_is_persisted_listed_and_deletable",
        "test_ai_cli_requires_digest_for_remote_and_shows_payload",
        "test_ai_configuration_validates_provider_settings",
        "test_ai_prompt_is_bounded_and_evidence_only",
    ),
    "Terminal explorer and local browser companion": (
        "test_tui_keybindings_modes_and_compact_threshold",
        "test_tui_source_editing_checks_syntax_diff_and_hash",
        "test_tui_rescan_preserves_view_and_selected_row",
        "test_loopback_browser_serves_json_endpoints",
        "test_browser_graph_families_paginate_filter_and_export_mermaid",
        "test_loopback_browser_prepares_read_only_analysis",
    ),
}
