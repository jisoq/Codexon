"""Select and run CacheMonitor checks from the last verified file state."""

from __future__ import annotations

import argparse
import base64
import ast
import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REPORTS = ROOT / 'artifacts' / 'verification'
BASELINE = REPORTS / 'last-success.json'

from tools.check_diagnostics import (DIAGNOSTIC_EXIT, CheckOutput, DiagnosticFailure, diagnostic_io,
                                     read_json, reset, retain, status, write_json)

def test_files(*names):
    return tuple(f'tests/test_{name}.py' for name in names)


def test_nodes(module, *names):
    return tuple(f'tests/test_{module}.py::{name}' for name in names)


GROUPS = {
    'native_cpu': test_files('native_cpu', 'native_trends', 'native_usage', 'native_quota_series', 'native_index', 'native_analysis', 'build_native'),
    'notices': test_files('collect_notices'),
    "performance_trends": test_files("performance_trends"),
    'retirement': test_files('retired_cache'),
    'settings': test_files('settings'),
    'settings_wiring': test_nodes('theme_tokens', 'test_quick_theme_selection_updates_overlay_and_preserves_state') +
        test_nodes('ui', 'test_quota_and_settings_have_independent_scope') +
        test_nodes('app_errors', 'test_sidebar_errors_remain_visible_across_pages_and_do_not_show_main_banner'),
    'chatgpt_startup': test_files('chatgpt_startup'),
    'app_lifetime': test_files('app_services', 'service_exit', 'shutdown_state', 'shutdown_ui',
                              'observer_panel', 'windows_startup', 'proxy_update') + (
        'tests/test_model_proxy.py::test_drain_preserves_work_accepted_after_the_last_idle_probe',),
    'graphics_recovery': test_files('graphics_recovery', 'window_recovery', 'quick_ui', 'overlay_shadow'),
    'data': test_files('core', 'index', 'data_contract', 'subagent_collection', 'request_tier_snapshots', 'collection_defaults', 'collection_crash_recovery', 'collection_transport', 'collection_preparation'),
    'analysis': test_files('comparison', 'comparison_usage', 'overview', 'performance', 'data_contract', 'output_speed'),
    'prepared': test_files('prepared_analysis', 'analysis_query_actor',
                         'prepared_revision_reads', 'prepared_retention_weights',
                         'prepared_restart_incremental', 'prepared_reader_lifecycle',
                         'prepared_notification_heads', 'prepared_mode_dependencies',
                         'prepared_ledger_reuse', 'prepared_finance_reuse', 'prepared_read_optimization') + (
        'tests/test_collection_transport.py::test_analyzer_publication_wakes_before_timer_and_freeze_reclaims_waiter',
        'tests/test_collection_transport.py::test_quiet_notifications_preserve_staleness_and_ledger_retry_deadlines',
        'tests/test_overlay_collection_resilience.py::test_history_recovery_yields_to_details_and_collection'),
    'cost': test_files('pricing', 'mode_costs', 'session_costs', 'data_contract'),
    'cache': test_files('cache_health', 'cache_misses'),
    'speed': test_files('speed_health', 'output_speed'),
    'names': test_files('codex_names'),
    'modes': test_files('request_modes', 'request_tier_snapshots', 'mode_costs'),
    'quota_store': test_files('quota', 'quota_observation_storage', 'quota_tracking', 'quota_tracking_integration', 'quota_edit_reuse',
                             'quota_integrity', 'quota_accumulation', 'quota_value_history', 'quota_exclusions'),
    'quota_math': test_files('quota_attribution', 'quota_cycles', 'quota_resets', 'banked_resets'),
    'quota_poll': test_files('quota_polling', 'quota_backlog', 'quota_worker', 'quota_transport', 'overlay_collection_resilience'),
    'quota_ui': test_files('quota_page', 'quota_detail_card', 'quota_gap_axis', 'quota_share_theme', 'quota_exclusion_controls', 'quota_sidebar', 'quota_query', 'quota_waiting'),
    'quota_chart': test_files('quota_gap_axis', 'quota_chart_performance', 'quota_page', 'quota_chart_exclusions'),
    'dashboard': test_files('home', 'ui', 'comparison_workflow', 'dashboard_evidence', 'ui_value_fixes', 'history_explorer', 'history_timeline', 'history_timeline_data', 'dashboard_readability'),
    'comparison_ui': test_files('comparison_workflow', 'dashboard_readability') +
        test_nodes('dashboard_evidence', 'test_tab_loading_keeps_controls_and_ignores_stale_result',
                   'test_comparison_display_controls_keep_chart_without_new_query',
                   'test_refresh_indicator_does_not_cover_existing_chart') +
        test_nodes('quick_ui', 'test_reasoning_tokens_render_independently_of_cost_and_request_unit'),
    'dashboard_queries': test_files('dashboard_evidence', 'history_timeline_data') +
        test_nodes('home', 'test_home_today_projection_matches_prepared_calls_and_partial_coverage',
                   'test_home_loading_masks_only_today_and_readiness_uses_collection_evidence',
                   'test_home_scope_and_identity_ignore_other_page_filters_but_expire_at_midnight') +
        test_nodes('ui', 'test_sequential_drilldown_and_full_call_detail',
                   'test_request_statistics_in_current_level_and_filtered_scope',
                   'test_event_navigation_retains_conditions_and_exact_evidence_phases') +
        test_nodes('comparison_workflow', 'test_project_selection_preserves_filters_and_back_restores_exact_call',
                   'test_unmeasurable_pairs_hide_without_losing_zero_values_or_cached_metrics',
                   'test_custom_date_drafts_preserve_results_and_apply_exact_cohort',
                   'test_metric_drilldown_ignores_history_filters_and_restores_latest_scope') +
        test_nodes('history_explorer', 'test_temporary_history_locks_exact_population_and_escape_restores_scope',
                   'test_prepared_filter_choices_preserve_empty_values_and_project_aliases',
                   'test_session_chart_selection_detail_and_back_preserve_scope'),
    'details': test_files('ui_details', 'call_transport', 'model_ui'),
    'table': test_files('table_order', 'performance'),
    'shared_ui': test_files('quick_ui', 'display_scaling'),
    'theme': test_files('theme_tokens', 'quota_share_theme'),
    'overlay_data': test_files('overlay_call_data', 'overlay_model_summary', 'session_costs', 'speed_health'),
    'overlay_tracking': test_files('overlay', 'overlay_resilience', 'overlay_collection_resilience'),
    'overlay_controls': test_files('overlay_controls', 'overlay_layout_controller'),
    'overlay_navigation': test_files('overlay_navigation', 'speed_health') + (
        'tests/test_output_speed.py::test_rendered_summary_click_and_overlay_speed_navigation',),
    'overlay_render': test_files('overlay_monitor', 'overlay_presentation', 'overlay_column', 'completed_layout', 'overlay_shadow'),
    'overlay_appearance': test_files('overlay_appearance', 'theme_tokens'),
    'overlay_windows': test_files('overlay_windows'),
    'taskbar': test_files('taskbar', 'taskbar_clock'),
    'window': test_files('window_recovery', 'display_scaling'),
    'notifications': test_files('notifications', 'confirmed_notifications', 'app_errors'),
    'app_errors': test_files('app_errors', 'notifications', 'dashboard_evidence', 'window_recovery'),
    'evidence': test_files('model_evidence', 'proxy_observation', 'observation_delivery', 'call_transport'),
    'relay': test_files('model_proxy', 'proxy_websocket', 'proxy_http2', 'proxy_observation', 'observation_delivery'),
    'proxy_lifecycle': test_files('managed_proxy', 'proxy_update', 'proxy_supervisor', 'connection_recovery'),
    'observer': test_files('observer_control', 'observer_panel', 'observer_task_errors'),
    'install': test_files('install_activation', 'install_management', 'install_dispatch', 'install_cleanup', 'legacy_release', 'windows_startup'),
    'update': test_files('app_update','update_monitor','update_state'),
    'runtime': test_files('windows_startup', 'graphics_recovery'),
    'translation': ('tests/test_public_release.py::test_english_token_labels_do_not_change_stored_values',),
    'payload': ('tests/test_public_release.py::test_public_payload_rejects_local_paths_and_unneeded_qt',),
    'selector': test_files('verify_changes', 'check_environment', 'ci_plan'),
    'qa_ipc': test_files('qa_ipc'),
    'ui_runner': test_files('ui_check_runner', 'completed_layout'),
    'release': test_files('release_pipeline', 'prepare_sources', 'source_receipt'),
}

# First match wins: QML and shared helpers must not fall through to a broad UI gate.
RULES = (
    ('native/*', ('native_cpu', 'data', 'analysis', 'performance_trends', 'evidence', 'quota_math', 'quota_chart', 'notices')),
    ('rust-toolchain.toml', ('native_cpu',)),
    ('cachemonitor/native_backend.py', ('native_cpu', 'data', 'analysis', 'performance_trends', 'evidence', 'quota_math')),
    ('tools/build_native.py', ('native_cpu', 'payload')),
    ('tools/benchmark_cpu.py', ('native_cpu',)),
    ('cachemonitor/performance_trends.py', ('performance_trends', 'native_cpu', 'prepared')),
    ("cachemonitor/performance_*.py", ("performance_trends",)),
    ('cachemonitor/retired_cache.py', ('retirement','proxy_lifecycle','install')),
    ('cachemonitor/usage_archive.py', ('retirement','data','quota_store')),
    ('cachemonitor/cache_health.py', ('cache','notifications')),
    ('cachemonitor/cache_misses.py', ('cache',)),
    ('cachemonitor/cache_audit.py', ('cache',)),
    ('cachemonitor/cache_*', ('retirement',)),
    ('tools/cache_runtime.py', ('retirement',)),
    ('tools/verify_cache_retention.py', ('retirement',)),
    ('tools/verify_retired_worker.py', ('retirement','proxy_lifecycle','install','qa_ipc')),
    ('tools/qa_ipc.py', ('qa_ipc',)),
    ("tools/preview_overlay.py", ("overlay_render",)),
    ("tools/Preview-Overlay.ps1", ("overlay_render",)),
    ("tools/audit_request_modes.py", ("modes",)),
    ("cachemonitor/tooltips.py", ("dashboard", "quota_ui", "overlay_render", "translation")),
    ('cachemonitor/proxy_identity.py', ('proxy_lifecycle','install')),
    ('cachemonitor/proxy_target.py', ('proxy_lifecycle','install')),
    ('cachemonitor/session_costs.py', ('cost', 'overlay_data')),
    ('cachemonitor/workload.py', ('cost', 'analysis', 'overlay_data')),
    ('cachemonitor/core.py', ('data', 'analysis', 'cost', 'quota_store', 'modes', 'cache', 'speed', 'native_cpu')),
    ('cachemonitor/index.py', ('data', 'evidence', 'quota_store', 'native_cpu')),
    ('cachemonitor/event_store.py', ('data', 'evidence', 'quota_store', 'native_cpu', 'retirement')),
    ('cachemonitor/event_stream.py', ('data', 'evidence', 'native_cpu')),
    ('cachemonitor/analytics.py', ('analysis', 'cost', 'cache', 'speed', 'quota_math')),
    ('cachemonitor/category_usage.py', ('analysis', 'dashboard')),
    ('cachemonitor/comparison_usage.py', ('prepared', 'analysis', 'dashboard')),
    ('cachemonitor/usage_panel.py', ('dashboard', 'speed')),
    ('cachemonitor/home_panel.py', ('dashboard', 'quota_ui')),
    ('cachemonitor/qml/UsageDashboard.qml', ('comparison_ui',)),
    ('cachemonitor/qml/AnalysisLoading.qml', ('dashboard', 'shared_ui', 'details', 'speed')),
    ('cachemonitor/dashboard_views.py', ('analysis', 'dashboard', 'details', 'cost')),
    ('cachemonitor/analysis_engine.py', ('analysis', 'cost', 'cache', 'overlay_data', 'native_cpu')),
    ('cachemonitor/analysis_records.py', ('analysis', 'cost', 'cache', 'overlay_data', 'dashboard', 'details')),
    ('cachemonitor/analysis_worker.py', ('analysis', 'quota_poll', 'overlay_tracking', 'notifications')),
    ('cachemonitor/analysis_query_actor.py', ('prepared', 'analysis', 'quota_poll')),
    ('cachemonitor/prepared_store.py', ('prepared', 'analysis', 'data', 'quota_poll')),
    ('cachemonitor/prepared_changes.py', ('prepared', 'analysis', 'quota_poll')),
    ('cachemonitor/prepared_ingest.py', ('prepared', 'analysis', 'data', 'cache', 'modes', 'quota_poll')),
    ('cachemonitor/prepared_append.py', ('prepared', 'analysis', 'cache', 'overlay_data', 'quota_poll')),
    ('cachemonitor/prepared_runtime.py', ('prepared', 'analysis', 'quota_poll', 'overlay_tracking', 'notifications')),
    ('cachemonitor/prepared_query.py', ('prepared', 'analysis', 'dashboard_queries', 'details')),
    ('cachemonitor/prepared_analysis.py', ('prepared', 'analysis', 'dashboard_queries', 'details')),
    ('cachemonitor/prepared_history.py', ('prepared', 'analysis', 'dashboard_queries', 'details')),
    ('cachemonitor/prepared_category.py', ('prepared', 'analysis', 'dashboard')),
    ('cachemonitor/prepared_sums.py', ('prepared', 'analysis', 'dashboard')),
    ('cachemonitor/prepared_performance.py', ('prepared', 'performance_trends')),
    ('cachemonitor/prepared_trends.py', ('prepared', 'performance_trends')),
    ('cachemonitor/prepared_calendar.py', ('prepared', 'performance_trends')),
    ('cachemonitor/prepared_delivery.py', ('prepared', 'overlay_data', 'notifications', 'overlay_tracking')),
    ('cachemonitor/prepared_health.py', ('prepared', 'cache', 'notifications', 'overlay_data')),
    ('cachemonitor/prepared_summary.py', ('prepared', 'overlay_data', 'overlay_navigation')),
    ('cachemonitor/prepared_overlays.py', ('prepared', 'overlay_data', 'overlay_navigation')),
    ('cachemonitor/prepared_speed.py', ('prepared', 'speed')),
    ('cachemonitor/prepared_speed_writer.py', ('prepared', 'speed', 'quota_poll')),
    ('cachemonitor/speed_checkpoint.py', ('prepared', 'speed')),
    ('cachemonitor/prepared_ledger.py', ('prepared', 'quota_store', 'quota_poll', 'modes')),
    ('cachemonitor/quota_prepared_sync.py', ('prepared', 'quota_store', 'quota_poll')),
    ('cachemonitor/usage_collection.py', ('data', 'quota_poll', 'overlay_tracking', 'retirement')),
    ('cachemonitor/collection_replay.py', ('data', 'evidence', 'quota_poll', 'retirement')),
    ('cachemonitor/collection_transport.py', ('data', 'quota_poll', 'app_lifetime')),
    ('cachemonitor/collection_wake.py', ('data',)),
    ('cachemonitor/collection_stream.py', ('prepared', 'data', 'quota_poll')),
    ('cachemonitor/collection_append.py', ('prepared', 'data', 'quota_poll')),
    ('cachemonitor/collection_pipe.py', ('data', 'quota_poll', 'app_lifetime')),
    ('cachemonitor/usage_paths.py', ('data', 'quota_poll', 'evidence')),
    ('cachemonitor/analysis_delivery.py', ('analysis', 'overlay_tracking')),
    ('cachemonitor/pricing.py', ('cost', 'quota_store', 'quota_math')),
    ('cachemonitor/request_modes.py', ('modes', 'quota_store')),
    ('cachemonitor/speed_health.py', ('speed',)),
    ('cachemonitor/codex_names.py', ('names', 'overlay_tracking')),
    ('cachemonitor/quota_chart.py', ('quota_chart',)),
    ('cachemonitor/quota_cycle_overview.py', ('quota_ui', 'quota_chart')),
    ('cachemonitor/quota_share.py', ('quota_ui', 'quota_math')),
    ('cachemonitor/quota_attribution.py', ('quota_math', 'quota_store', 'native_cpu')),
    ('cachemonitor/quota_cycles.py', ('quota_math', 'quota_store', 'modes', 'native_cpu')),
    ('cachemonitor/quota_history_import.py', ('quota_math', 'quota_poll', 'data')),
    ('cachemonitor/quota_projection.py', ('quota_store', 'quota_poll', 'quota_ui')),
    ('cachemonitor/quota_archive.py', ('quota_store', 'quota_poll', 'quota_ui')),
    ('cachemonitor/banked_resets.py', ('quota_math', 'quota_ui')),
    ('cachemonitor/quota_view.py', ('quota_ui', 'quota_chart', 'native_cpu')),
    ('cachemonitor/quota_panel.py', ('quota_ui',)),
    ('cachemonitor/quota_layout.py', ('quota_ui',)),
    ('cachemonitor/quota_sidebar.py', ('quota_ui', 'dashboard')),
    ('cachemonitor/quota_exclusion_controls.py', ('quota_ui',)),
    ('cachemonitor/quota_exclusions.py', ('quota_store', 'quota_math', 'quota_ui')),
    ('cachemonitor/quota_service.py', ('quota_poll', 'quota_store')),
    ('cachemonitor/quota_payloads.py', ('quota_poll', 'quota_store', 'quota_math')),
    ('cachemonitor/quota_backlog.py', ('quota_poll', 'quota_store')),
    ('cachemonitor/quota_worker.py', ('quota_poll', 'quota_ui', 'quota_store')),
    ('cachemonitor/quota_cancellation.py', ('quota_poll', 'quota_store')),
    ('cachemonitor/quota_transport.py', ('quota_poll',)),
    # A diff against a supported old revision can still contain this deleted reader.
    ('cachemonitor/quota_reader.py', ('quota_poll', 'quota_store')),
    ('cachemonitor/quota_polling.py', ('quota_poll',)),
    ('cachemonitor/quota_live.py', ('quota_poll', 'quota_store')),
    ('cachemonitor/quota_diagnostics.py', ('quota_math',)),
    ('cachemonitor/quota_tracking*.py', ('quota_store', 'quota_math')),
    ('cachemonitor/quota.py', ('quota_store', 'quota_math')),
    ('cachemonitor/history_*.py', ('dashboard', 'cost')),
    ('cachemonitor/qml/HistoryWorkspace.qml', ('dashboard',)),
    ('cachemonitor/qml/HistoryFilters.qml', ('dashboard',)),
    ('cachemonitor/qml/PerformanceNavigator.qml', ('performance_trends',)),
    ('cachemonitor/qml/PerformanceControls.qml', ('performance_trends',)),
    ('cachemonitor/dashboard.py', ('dashboard', 'details', 'cost', 'speed')),
    ('cachemonitor/dashboard_presentation.py', ('dashboard', 'details')),
    ('cachemonitor/qml/Breadcrumbs.qml', ('dashboard',)),
    ('cachemonitor/qml/RecordFields.qml', ('dashboard', 'details')),
    ('cachemonitor/ui_details.py', ('details',)),
    ('cachemonitor/table_model.py', ('table',)),
    ('cachemonitor/lazy_table.py', ('table',)),
    ('cachemonitor/overlay_data.py', ('overlay_data', 'speed', 'evidence')),
    ('cachemonitor/overlay_navigation.py', ('overlay_navigation',)),
    ('cachemonitor/overlay_chrome.py', ('overlay_controls', 'overlay_navigation')),
    ('cachemonitor/overlay_view.py', ('overlay_render', 'overlay_navigation')),
    ('cachemonitor/overlay_icons.py', ('overlay_render',)),
    ('cachemonitor/overlay_appearance.py', ('overlay_appearance',)),
    ('cachemonitor/overlay_windows.py', ('overlay_windows', 'overlay_controls')),
    ('cachemonitor/overlay_shadow.py', ('overlay_render',)),
    ('cachemonitor/overlay_tracking.py', ('overlay_tracking',)),
    ('cachemonitor/overlay_monitor.py', ('overlay_render', 'overlay_navigation')),
    ('cachemonitor/overlay_layout.py', ('overlay_render', 'overlay_controls', 'overlay_navigation')),
    ('cachemonitor/overlay.py', ('overlay_tracking', 'overlay_controls', 'overlay_navigation', 'overlay_windows')),
    ('cachemonitor/qml/OverlayLinks.qml', ('overlay_navigation',)),
    ('cachemonitor/qml/OverlayDetail.qml', ('overlay_render', 'overlay_navigation')),
    ('cachemonitor/qml/OverlayControls.qml', ('overlay_controls',)),
    ('cachemonitor/qml/OverlayScene.qml', ('overlay_render',)),
    ('cachemonitor/qml/AgentHeartbeat.qml', ('overlay_render',)),
    ('cachemonitor/qml/QuotaDetail.qml', ('quota_ui',)),
    ('cachemonitor/qml/QuotaDetails.qml', ('quota_ui',)),
    ('cachemonitor/qml/Taskbar.qml', ('taskbar',)),
    ('cachemonitor/qml/DataTable.qml', ('table', 'dashboard')),
    ('cachemonitor/qml/DateField.qml', ('shared_ui', 'dashboard')),
    ('cachemonitor/qml/Ui*.qml', ('shared_ui',)),
    ('cachemonitor/qml/Node*.qml', ('shared_ui', 'dashboard', 'details', 'quota_ui', 'settings', 'overlay_render')),
    ('cachemonitor/qml/Main.qml', ('shared_ui', 'window')),
    ('cachemonitor/qml/PaintedScene.qml', ('shared_ui', 'overlay_render', 'quota_ui')),
    ('cachemonitor/quick_runtime.py', ('shared_ui', 'overlay_controls', 'overlay_render', 'overlay_navigation', 'overlay_windows', 'quota_ui', 'taskbar', 'window', 'translation', 'graphics_recovery')),
    ('tools/benchmark_resources.py', ('quota_math', 'quota_store', 'shared_ui')),
    ('cachemonitor/controls.py', ('shared_ui',)),
    ('cachemonitor/presentation.py', ('shared_ui', 'dashboard', 'overlay_render')),
    ('cachemonitor/charts.py', ('shared_ui', 'quota_chart')),
    ('cachemonitor/theme.py', ('theme',)),
    ('cachemonitor/token_colors.py', ('theme',)),
    ('cachemonitor/settings_page.py', ('settings', 'settings_wiring')),
    ('cachemonitor/taskbar*.py', ('taskbar',)),
    ('cachemonitor/screens.py', ('window', 'overlay_controls')),
    ('cachemonitor/notifications.py', ('notifications',)),
    ('cachemonitor/app_errors.py', ('app_errors',)),
    ('cachemonitor/error_panel.py', ('app_errors', 'settings')),
    ('cachemonitor/change_highlight.py', ('shared_ui', 'overlay_render')),
    ('cachemonitor/fonts.py', ('shared_ui', 'overlay_render')),
    ('cachemonitor/icons.py', ('shared_ui',)),
    ('cachemonitor/brand_icon.py', ('runtime',)),
    ('icons/*.ico', ('runtime', 'payload')),
    ('cachemonitor/assets/brand/*', ('runtime',)),
    ('cachemonitor/assets/agent-icons/*', ('overlay_render',)),
    ('cachemonitor/assets/fonts/*', ('shared_ui', 'overlay_render')),
    ('cachemonitor/i18n.py', ('translation', 'shared_ui')),
    ('cachemonitor/translation_catalog.py', ('translation',)),
    ('cachemonitor/assets/i18n/*.json', ('translation',)),
    ('cachemonitor/model_evidence.py', ('evidence', 'data')),
    ('cachemonitor/evidence_writer.py', ('evidence',)),
    ('cachemonitor/model_proxy.py', ('relay', 'retirement')),
    ('cachemonitor/proxy_websocket.py', ('relay', 'retirement')),
    ('cachemonitor/proxy_http.py', ('relay',)),
    ('cachemonitor/proxy_observation.py', ('relay', 'evidence', 'native_cpu')),
    ('cachemonitor/proxy_update.py', ('proxy_lifecycle',)),
    ('cachemonitor/proxy_supervisor.py', ('proxy_lifecycle',)),
    ('cachemonitor/managed_proxy.py', ('proxy_lifecycle',)),
    ('cachemonitor/proxy_runtime.py', ('relay', 'proxy_lifecycle', 'app_lifetime')),
    ('cachemonitor/proxy_listener.py', ('relay', 'proxy_lifecycle')),
    ('cachemonitor/connection_recovery.py', ('proxy_lifecycle', 'install')),
    ('cachemonitor/observer_panel.py', ('observer',)),
    ('cachemonitor/observer_control.py', ('observer', 'proxy_lifecycle')),
    ('cachemonitor/observer_state.py', ('observer', 'proxy_lifecycle')),
    ('cachemonitor/observer_task.py', ('observer', 'proxy_lifecycle')),
    ('cachemonitor/install*.py', ('install',)),
    ('cachemonitor/app_update.py', ('update', 'install')),
    ('cachemonitor/update_panel.py', ('update',)),
    ('cachemonitor/update_state.py', ('update','install','proxy_lifecycle')),
    ('cachemonitor/update_monitor.py', ('update',)),
    ('cachemonitor/app.py', ('runtime', 'install', 'observer', 'graphics_recovery')),
    ('cachemonitor/graphics_recovery.py', ('graphics_recovery',)),
    ('cachemonitor/version.py', ('runtime', 'proxy_lifecycle', 'update')),
    ('cachemonitor/__init__.py', ('runtime',)),
    ('cachemonitor/app_services.py', ('app_lifetime',)),
    ('cachemonitor/chatgpt_startup.py', ('chatgpt_startup', 'app_lifetime', 'settings')),
    ('cachemonitor/collection_lifecycle.py', ('app_lifetime','quota_poll')),
    ('tools/verify_app_services.py', ('app_lifetime','qa_ipc')),
    ('cachemonitor/app_shutdown.py', ('app_lifetime',)),
    ('cachemonitor/shutdown_state.py', ('app_lifetime',)),
    ('cachemonitor/proxy_drain.py', ('proxy_lifecycle', 'app_lifetime')),
    ('cachemonitor/tray.py', ('runtime', 'window', 'app_lifetime', 'graphics_recovery')),
    ('cachemonitor/launch_context.py', ('install', 'runtime')),
    ('cachemonitor/app_restart.py', ('runtime', 'install', 'payload')),
    ('cachemonitor/windows_integration.py', ('install', 'taskbar')),
    ('cachemonitor/shell_shortcut.py', ('install',)),
    ('cachemonitor/runtime_check.py', ('runtime',)),
    ('cachemonitor/quick_smoke.py', ('runtime',)),
    ('cachemonitor/quick_qa.py', ('runtime', 'shared_ui', 'overlay_controls')),
    ('cachemonitor/qa.py', ('runtime',)),
    ('tools/verify_changes.py', ('selector',)),
    ('tools/ci_plan.py', ('selector', 'release')),
    ('.github/workflows/windows.yml', ('selector', 'release')),
    ('tools/release_*.py', ('release',)),
    ('tools/source_receipt.py', ('release',)),
    ('tools/verify_release_tools.py', ('release', 'selector')),
    ('third-party-sources.lock.json', ('release', 'payload')),
    ('tools/run_ui_checks.py', ('ui_runner', 'runtime', 'overlay_controls')),
    ('tools/check_processes.py', ('ui_runner',)),
    ('tools/check_worker.py', ('ui_runner',)),
    ('tools/check_progress.py', ('ui_runner',)),
    ('tools/check_diagnostics.py', ('selector', 'ui_runner')),
    ('tools/clear_check_scopes.ps1', ('ui_runner', 'install')),
    ('tools/check_environment.py', ('selector',)),
    ('tools/demo_speed_overlay.py', ('speed',)),
    ('tools/*proxy*.py', ('relay', 'proxy_lifecycle')),
    ('tools/*overlay*.py', ('overlay_controls', 'overlay_render')),
    ('tools/*quota*.py', ('quota_store', 'quota_math')),
    ('tools/audit_analytics.py', ('analysis',)),
    ('tools/verify_record_pairs.py', ('data',)),
    ('cachemonitor/performance_probe.py', ('analysis', 'dashboard', 'table', 'quota_chart')),
    ('tools/benchmark_interactions.py', ('analysis', 'dashboard', 'table', 'quota_chart')),
    ('tools/verify_settings.py', ('dashboard',)),
    ('tools/verify_design.py', ('shared_ui',)),
    ('tools/*display_scaling.py', ('window',)),
    ('tools/font_render_probe.py', ('shared_ui',)),
    ('tools/verify_recovery.py', ('install',)),
    ('tools/verify_installation.py', ('install',)),
    ('tools/verify_msix_installation.ps1', ('install',)),
    ('tools/run_native_checks.py', ('install',)),
    ('tools/prepare_legacy_release.py', ('install',)),
    ('tools/verify_gui_handoff.py', ('install',)),
    ('tools/installer_identity.py', ('install',)),
    ('tools/prepare_bad_runtime.py', ('install',)),
    ('tools/collect_notices.py', ('payload', 'notices')),
    ('tools/package_release.py', ('payload',)),
    ('tools/prepare_sources.py', ('payload', 'release')),
    ('tools/Build-*.ps1', ('install', 'runtime', 'payload')),
    ('tools/configure-app-task.ps1', ('install',)),
    ('recovery_main.py', ('install',)),
    ('run.py', ('runtime',)),
    ('start.ps1', ('runtime',)),
    ('installer/*', ('install', 'payload')),
    ('build*.ps1', ('runtime', 'install', 'payload')),
    ('*.spec', ('runtime', 'payload')),
)

# These changes need frozen executable/install checks in addition to source tests.
PACKAGE_PATTERNS = (
    'native/*', 'rust-toolchain.toml', 'cachemonitor/native_backend.py', 'tools/build_native.py',
    'cachemonitor/overlay_icons.py', 'cachemonitor/assets/agent-icons/*',
    'cachemonitor/retired_cache.py','cachemonitor/usage_archive.py',
    'requirements*', 'build*.ps1', '*.spec', 'installer/*', 'recovery_main.py', 'icons/*.ico',
    'tools/verify_msix_installation.ps1', 'tools/run_native_checks.py',
    'run.py', 'start.ps1', 'tools/Build-*.ps1', 'tools/*install*.py', 'tools/prepare_bad_runtime.py',
    'tools/package_release.py', 'tools/collect_notices.py', 'tools/prepare_sources.py',
    'tools/configure-app-task.ps1', 'third-party-sources.lock.json', 'SOURCE-OFFER.md',
    'cachemonitor/install*.py', 'cachemonitor/app.py', 'cachemonitor/app_update.py', 'cachemonitor/app_restart.py',
    'cachemonitor/version.py', 'cachemonitor/runtime_check.py', 'cachemonitor/launch_context.py',
    'cachemonitor/quick_smoke.py',
    'cachemonitor/windows_integration.py', 'cachemonitor/shell_shortcut.py',
    'tools/prepare_legacy_release.py',
)
PROXY_PATTERNS = (
    'cachemonitor/retired_cache.py',
    'cachemonitor/model_proxy.py', 'cachemonitor/proxy*.py', 'cachemonitor/managed_proxy.py',
    'cachemonitor/observer_control.py', 'cachemonitor/observer_state.py', 'cachemonitor/observer_task.py',
    'cachemonitor/connection_recovery.py', 'cachemonitor/evidence_writer.py',
    'cachemonitor/version.py', 'requirements*', 'build*.ps1', '*.spec',
    'tools/Build-*.ps1', 'cachemonitor/install*.py', 'installer/*',
    'tools/prepare_legacy_release.py',
)
FULL_PATTERNS = ('tests/conftest.py', 'pytest.ini', 'pyproject.toml', 'requirements*')
QA_SMOKES = {
    'tools/verify_proxy_update.py': 'proxy_update',
    'tools/verify_app_services.py': 'app_services',
    'tools/verify_retired_worker.py': 'retired_worker',
    'tools/verify_gui_handoff.py': 'gui_handoff',
    'tools/verify_recovery.py': 'recovery',
}
DOC_PATTERNS = ('docs/*', 'releases/*', '*.md', '*.txt', 'LICENSE*', '.gitignore',
                '.gitattributes', '.github/ISSUE_TEMPLATE/*', '.agents/skills/*/agents/openai.yaml')


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def git_bytes(*args):
    result = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, check=True)
    return result.stdout


def file_hashes():
    paths = {value.decode('utf-8', 'surrogateescape').replace('\\', '/')
             for value in git_bytes('ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
             if value}
    result = {}
    for path in sorted(paths):
        target = ROOT / path
        if target.is_file() and not target.is_symlink():
            result[path] = hashlib.sha256(target.read_bytes()).hexdigest()
    return result


def changed_files(current, baseline=None, base_ref=None):
    if base_ref:
        changed = {value.decode('utf-8', 'surrogateescape').replace('\\', '/')
                   for value in git_bytes('diff', '--name-only', '-z', base_ref, '--').split(b'\0') if value}
        untracked = {value.decode('utf-8', 'surrogateescape').replace('\\', '/')
                     for value in git_bytes('ls-files', '-z', '--others', '--exclude-standard').split(b'\0') if value}
        return changed | untracked
    if baseline is None:
        return None
    before = baseline.get('files', {})
    return {path for path in before.keys() | current.keys() if before.get(path) != current.get(path)}


def version_contract(source=None):
    """Fingerprint executable version/compatibility code except the app version literal."""
    source = source if source is not None else (ROOT/'cachemonitor/version.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == 'VERSION'
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            node.value = ast.Constant(value='app-version')
    return hashlib.sha256(ast.dump(tree).encode()).hexdigest()


def release_version_only(*, base_ref=None, baseline=None):
    if base_ref:
        try:
            before = git_bytes('show', f'{base_ref}:cachemonitor/version.py').decode('utf-8-sig')
            return version_contract(before) == version_contract()
        except (subprocess.CalledProcessError, SyntaxError, UnicodeError):
            return False
    return bool(baseline and baseline.get('version_contract') == version_contract())


def test_dependents(changed, root):
    """Include consumers of shared test fixtures, including imports inside functions."""
    dependencies = {}
    for path in (root / 'tests').rglob('*.py'):
        imports = set()
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imports.add(node.module or '')
                if node.module == 'tests':
                    imports.update('tests.' + alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
        dependencies[path.relative_to(root).as_posix()] = {
            (name.replace('.', '/') + '.py' if name.startswith('tests.') else
             'tests/' + name.replace('.', '/') + '.py') for name in imports}
    affected = set(changed)
    while True:
        consumers = {path for path, imports in dependencies.items() if imports & affected}
        if consumers <= affected:
            break
        affected.update(consumers)
    return {path for path in affected if Path(path).name.startswith('test_')
            and (root / path).is_file()}


def select_tests(changed, *, root=None, deleted=(), version_only=False):
    """Map changes to contracts; unmapped code is an actionable selection error."""
    root = root or ROOT
    first_run = changed is None
    paths = set(changed or ())
    full = first_run or any(matches(path, FULL_PATTERNS) for path in paths)
    proxy = first_run or any(matches(path, PROXY_PATTERNS) for path in paths
                            if not (version_only and path == 'cachemonitor/version.py'))
    package = proxy or first_run or any(matches(path, PACKAGE_PATTERNS) for path in paths)
    selected, groups, unmapped = set(), set(), set()
    reasons = {}
    removed_contract=False

    def add(test, reason):
        selected.add(test)
        reasons.setdefault(test, []).append(reason)

    test_changes = {path for path in paths if path.startswith('tests/') and path.endswith('.py')}
    if test_changes:
        for test in test_dependents(test_changes, root):
            add(test, '검사 또는 공유 fixture 변경')
    for path in sorted(paths):
        if path in test_changes or matches(path, FULL_PATTERNS):
            continue
        matched = next((scopes for pattern, scopes in RULES if fnmatch.fnmatchcase(path, pattern)), None)
        if version_only and path == 'cachemonitor/version.py':
            matched = ('payload', 'update')
        if matched is not None:
            groups.update(matched)
            for group in matched:
                for test in GROUPS[group]:
                    add(test, path)
        elif not matches(path, DOC_PATTERNS):
            if path in deleted and not (root/path).exists():
                # A removed subsystem has no current rule or executable tests.
                # Validate all remaining consumers and the frozen product.
                full=package=proxy=True
                removed_contract=True
            else:unmapped.add(path)
    # A file selection subsumes any specific node selections from other changes.
    selected = {test for test in selected if '::' not in test or test.split('::')[0] not in selected}
    affected_tests = tuple(sorted(selected))
    if full:
        selected = {'tests'}
        reasons = {'tests': ['검증 기준점 없음' if first_run else
                            '제거된 구성요소의 남은 참조와 패키지 검증' if removed_contract else
                            '공통 검사 설정 또는 의존성 변경']}
        groups.add('full')
    return dict(full=full, tests=tuple(sorted(selected)), affected_tests=affected_tests,
                reasons={test: reasons[test] for test in sorted(selected)},
                groups=tuple(sorted(groups)), unmapped=tuple(sorted(unmapped)),
                package_impact=package, proxy_impact=proxy,
                qa_smokes=sorted({QA_SMOKES[path] for path in paths if path in QA_SMOKES}))


def fixture_home(folder):
    home = folder / 'codex';home.mkdir()
    when = datetime.now(timezone.utc) - timedelta(seconds=30)
    parent = '11111111-1111-1111-1111-111111111111'
    child = '22222222-2222-2222-2222-222222222222'
    with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
        db.execute('create table threads(id text, rollout_path text, updated_at integer, name text, '
                   'title text, model text, model_provider text, cwd text)')
        for tid,title,parent_id in ((parent,'배포 검증 작업',None),(child,'하위 검증 작업',parent)):
            path = home / f'{tid}.jsonl'
            def event(kind, seconds, payload):
                return dict(type=kind,timestamp=(when+timedelta(seconds=seconds)).isoformat(),payload=payload)
            header = dict(id=tid,cwd='C:/cachemonitor-qa',source=(
                {'subagent':{'thread_spawn':{'parent_thread_id':parent_id,'depth':1,'agent_path':'/root/check'}}}
                if parent_id else 'cli'))
            if parent_id:header['parent_thread_id']=parent_id
            records = [event('session_meta',0,header),
                       event('turn_context',1,dict(turn_id='verify',model='gpt-6-astra',effort='high',service_tier='Standard')),
                       event('event_msg',2,dict(type='task_started',turn_id='verify')),
                       event('token_usage_record',3,dict(thread_id=tid,turn_id='verify',response_id=tid+'-response',
                           usage=dict(input_tokens=1000,cached_input_tokens=200,cache_write_input_tokens=0,
                                      output_tokens=100))),
                       event('event_msg',4,dict(type='task_complete',turn_id='verify'))]
            path.write_text(''.join(json.dumps(row)+'\n' for row in records),encoding='utf-8')
            db.execute('insert into threads values(?,?,?,?,?,?,?,?)',
                       (tid,str(path),int(when.timestamp())+4,title,title,'gpt-6-astra','openai','C:/cachemonitor-qa'))
    with closing(sqlite3.connect(home / 'logs_2.sqlite')) as db, db:
        db.execute('create table logs(id integer primary key, ts integer, ts_nanos integer, '
                   'target text, thread_id text, process_uuid text, feedback_log_body text)')
    return home


def package_check(executable, report_dir):
    executable = Path(executable).resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    runtime = report_dir / 'runtime.json'
    check = subprocess.run([str(executable), '--verify-runtime', str(runtime)],
                           capture_output=True, text=True, timeout=60)
    try:
        with diagnostic_io('read package runtime evidence', runtime):
            value = json.loads(runtime.read_text(encoding='utf-8')) if runtime.exists() else {}
        from cachemonitor.version import VERSION, PROXY_VERSION
        if (check.returncode or value.get('errors') != [] or value.get('version') != VERSION
                or value.get('proxy_version') != PROXY_VERSION
                or value.get('native', {}).get('abi_version') != 1):
            raise RuntimeError('배포 실행 파일 런타임 확인 실패: '+check.stderr[-1000:])
        with tempfile.TemporaryDirectory(prefix='cachemonitor-package-qa-') as temp:
            folder = Path(temp);home = fixture_home(folder)
            image = report_dir / 'smoke.png'
            check = subprocess.run([str(executable), '--codex-home', str(home),
                                    '--index-path', str(folder/'usage-index.sqlite'),
                                    '--smoke', str(image), '--smoke-depth', 'core'],
                                   capture_output=True, text=True, timeout=180)
            with diagnostic_io('read package smoke evidence', image.with_suffix('.json')):
                report = json.loads(image.with_suffix('.json').read_text(encoding='utf-8')) if image.with_suffix('.json').exists() else {}
            rollup = report.get('session_rollup') or {}
            if check.returncode or report.get('errors') != [] or rollup.get('descendants',0) < 1:
                raise RuntimeError('격리 패키지 화면 검사 실패: '+str(report.get('errors') or check.stderr[-1000:]))
        return dict(runtime=runtime.name, smoke=image.with_suffix('.json').name,
                    duration_seconds=report.get('duration_seconds'), session_rollup=rollup)
    except DiagnosticFailure as error:
        error.product_exit_code = check.returncode or None
        raise


# Audited modules have no Qt imports, including function-local imports and fixtures.
# The core runner's import guard continues to reject accidental Qt dependencies.
CORE_TESTS = frozenset(test_files(
    'pricing', 'data_contract', 'core', 'request_modes', 'collection_preparation',
    'banked_resets', 'comparison', 'quota', 'quota_tracking',
    'native_index', 'native_trends', 'native_usage', 'build_native',
    'verify_changes', 'check_environment', 'collect_notices', 'prepare_sources',
    'release_pipeline', 'ci_plan', 'source_receipt'))

# These contracts do not execute application CPU kernels. Unknown selections keep
# the native build gate, so adding a test cannot silently bypass native parity.
NON_NATIVE_TESTS = frozenset(test_files(
    'pricing', 'request_modes', 'build_native', 'verify_changes', 'check_environment',
    'collect_notices', 'prepare_sources', 'release_pipeline', 'ci_plan', 'source_receipt'))


def basic_tests():
    return tuple(sorted(CORE_TESTS))


def tests_require_native(tests):
    """Keep native parity for every selection except audited non-kernel contracts."""
    return any(test.split('::')[0] not in NON_NATIVE_TESTS for test in tests)


PROCESS_TESTS = frozenset(test_files('ui_check_runner', 'install_management', 'proxy_update', 'app_services'))


def pytest_commands(tests, *, integration=False, extended=False, report_dir=None, affected_tests=()):
    """Run audited core contracts without Qt, then GUI and process checks serially."""
    selection = tuple(tests)
    pytest = [sys.executable, '-u', '-B', '-m', 'pytest', '-v', '--tb=short', '--maxfail=1', '--durations=10',
              '-p', 'tools.check_progress',
              '-o', 'faulthandler_timeout=60']
    if extended:
        pytest += ['-m', 'extended or not extended']
    elif integration or tuple(tests) != ('tests',):
        # Scoped work runs the affected integrations, not only the fast sweep.
        pytest += ['-m', 'not extended']
    if report_dir is not None:
        pytest += ['-o', 'cache_dir=' + str(report_dir / 'pytest-cache')]
    runner = [sys.executable, str(ROOT / 'tools/run_ui_checks.py')]
    if selection == ('tests',):
        tests = sorted({path.relative_to(ROOT).as_posix() for pattern in ('test_*.py', '*_test.py')
                        for path in (ROOT/'tests').rglob(pattern)})
    groups = {'core': [], 'gui': [], 'installation': []}
    for test in tests:
        path = test.split('::')[0]
        kind = 'core' if path in CORE_TESTS else 'installation' if path in PROCESS_TESTS else 'gui'
        groups[kind].append(test)
    commands = []
    for kind, targets in groups.items():
        if not targets:
            continue
        target_paths = {target.split('::')[0] for target in targets}
        affected = [arg for test in affected_tests if test.split('::')[0] in target_paths
                    for arg in ('--affected-test', test)] if selection == ('tests',) else []
        junit = ['--junitxml=' + str(report_dir / f'tests-{kind}.xml')] if report_dir else []
        desktop = ['--no-desktop'] if kind == 'core' else []
        commands.append((kind, [*runner, '--kind', kind, *desktop, '--',
                                *pytest, '--check-kind', kind, *affected, *junit, *targets]))
    return commands


def validate_diagnostics_path(path):
    try:
        relative = path.resolve().relative_to(ROOT)
    except ValueError:
        return
    ignored = subprocess.run(['git', 'check-ignore', '--quiet', '--no-index',
                              (relative / '.diagnostic-output').as_posix()], cwd=ROOT)
    tracked = git_bytes('ls-files', '-z', '--cached', '--', relative.as_posix())
    if ignored.returncode or tracked:
        raise ValueError('Diagnostic output must be outside the checkout or in an untracked Git-ignored directory')


def run_logged(command, path):
    observed = CheckOutput()
    tail = ''
    with diagnostic_io('create subprocess capture', Path(tempfile.gettempdir())):
        validate_diagnostics_path(Path(tempfile.gettempdir()) / 'codexon-capture')
        temporary = tempfile.TemporaryDirectory(prefix='codexon-check-output-')
    capture_failed = False
    try:
        capture = Path(temporary.name) / 'stdout.log'
        with diagnostic_io('open subprocess capture', capture):
            output = capture.open('wb', buffering=0)
            reader = capture.open('r', encoding='utf-8', errors='replace')
        with output, reader:
            try:
                with diagnostic_io('open check log', path):
                    mirror = path.open('w', encoding='utf-8')
            except DiagnosticFailure:
                result = subprocess.CompletedProcess(command, DIAGNOSTIC_EXIT, stdout='')
                result.product_exit_code, result.diagnostics = None, status()
                return result
            try:
                process = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                    env=dict(os.environ, CODEXON_CHECK_LOG=str(capture)))
                while True:
                    completed = process.poll() is not None
                    try:
                        with diagnostic_io('read subprocess capture', capture):
                            chunk = reader.read(65536)
                    except DiagnosticFailure:
                        capture_failed = True
                        process.wait()
                        break
                    if chunk:
                        tail = (tail + chunk)[-1024 * 1024:]
                        observed.feed(chunk)
                        retain(observed.first_error)
                        print(chunk, end='', flush=True)
                        if mirror:
                            try:
                                with diagnostic_io('write check log', path):
                                    mirror.write(chunk)
                                    mirror.flush()
                            except DiagnosticFailure:
                                try:
                                    with diagnostic_io('close check log', path):
                                        mirror.close()
                                except DiagnosticFailure:
                                    pass
                                mirror = None
                    elif completed:
                        break
                    else:
                        time.sleep(.1)
            finally:
                if mirror:
                    try:
                        with diagnostic_io('close check log', path):
                            mirror.close()
                    except DiagnosticFailure:
                        pass
    finally:
        try:
            with diagnostic_io('remove subprocess capture', Path(temporary.name)):
                temporary.cleanup()
        except DiagnosticFailure:
            pass
    observed.feed('', final=True)
    retain(observed.first_error)
    product = observed.product_exit_code if observed.has_outcome else None if capture_failed else process.returncode
    code = process.returncode or (DIAGNOSTIC_EXIT if status()['first_error'] else 0)
    if code == DIAGNOSTIC_EXIT and product:
        code = product
    result = subprocess.CompletedProcess(command, code, stdout=tail)
    result.product_exit_code, result.diagnostics = product, status()
    return result


def baseline_input():
    with diagnostic_io('read verification baseline', BASELINE):
        raw = BASELINE.read_bytes() if BASELINE.exists() else None
        value = json.loads(raw.decode('utf-8')) if raw is not None else None
        if value is not None and (not isinstance(value, dict) or not isinstance(value.get('files', {}), dict)):
            raise ValueError('Verification baseline must contain a file hash mapping')
    return dict(path=str(BASELINE.resolve()), content=base64.b64encode(raw).decode() if raw is not None else None,
                sha256=hashlib.sha256(raw).hexdigest() if raw is not None else None, value=value)


def save_rerun(selection):
    with diagnostic_io('write rerun manifest', Path(tempfile.gettempdir())):
        validate_diagnostics_path(Path(tempfile.gettempdir()) / 'codexon-rerun.json')
        with tempfile.NamedTemporaryFile(prefix='codexon-rerun-', suffix='.json', delete=False, mode='w', encoding='utf-8') as stream:
            json.dump(selection, stream, ensure_ascii=False, indent=2)
            return Path(stream.name)


def load_rerun(path):
    value = read_json(path)
    with diagnostic_io('validate rerun manifest', path):
        if (not isinstance(value, dict) or not all(key in value for key in ('base_commit', 'version_contract', 'python_sha256')) or value.get('version') != 1 or value.get('root') != str(ROOT)
                or value.get('python') != sys.executable or not isinstance(value.get('source_files'), dict)
                or not isinstance(value.get('plan'), dict) or not isinstance(value.get('baseline'), dict)
                or not isinstance(value.get('options'), dict)):
            raise ValueError('Rerun manifest does not match this checkout and Python environment')
        plan, options, baseline = value['plan'], value['options'], value['baseline']
        if (not all(key in plan for key in ('tests', 'affected_tests', 'groups', 'unmapped', 'changed', 'full'))
                or not all(key in options for key in ('basic', 'full', 'integration', 'extended', 'package_only', 'package_exe', 'package_sha256'))
                or not all(key in baseline for key in ('path', 'content', 'sha256', 'value'))
                or baseline['path'] != str(BASELINE.resolve())):
            raise ValueError('Rerun manifest is incomplete or names a different baseline')
        if (not all(isinstance(plan.get(key), list) and all(isinstance(item, str) for item in plan[key])
                    for key in ('tests', 'affected_tests', 'groups', 'unmapped', 'qa_smokes'))
                or not all(isinstance(plan.get(key), bool) for key in ('full', 'package_impact', 'proxy_impact'))
                or not isinstance(plan.get('reasons'), dict) or not isinstance(plan.get('suite'), str)
                or not all(isinstance(options[key], bool) for key in ('basic', 'full', 'integration', 'extended', 'package_only'))
                or (options['package_exe'] is not None and not isinstance(options['package_exe'], str))
                or (baseline['content'] is not None and not isinstance(baseline['content'], str))
                or (baseline['value'] is not None and not isinstance(baseline['value'], dict))
                or (plan['changed'] is not None and not isinstance(plan['changed'], list))
                or not isinstance(value['version_contract'], str)):
            raise ValueError('Rerun manifest has invalid selection or baseline fields')
        raw = base64.b64decode(baseline['content'], validate=True) if baseline['content'] is not None else None
        if (hashlib.sha256(raw).hexdigest() if raw is not None else None) != baseline['sha256']:
            raise ValueError('Rerun baseline bytes do not match their digest')
        if (json.loads(raw.decode('utf-8')) if raw is not None else None) != baseline['value']:
            raise ValueError('Rerun baseline content does not match its bytes')
        if value['source_files'] != file_hashes():
            raise ValueError('Source changed since the original check; start a new verification')
        if value.get('python_sha256') != hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest():
            raise ValueError('Python executable changed since the original check')
        if options['package_exe'] and options['package_sha256'] != hashlib.sha256(Path(options['package_exe']).read_bytes()).hexdigest():
            raise ValueError('Package executable changed since the original check')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diagnostics', type=Path, help='Root directory for this invocation diagnostics')
    parser.add_argument('--rerun', type=Path, help='Replay a frozen verification manifest')
    parser.add_argument('--plan', action='store_true', help='Show checks without running them')
    parser.add_argument('--base', help='Git ref to compare when intentionally establishing a new baseline')
    parser.add_argument('--basic', action='store_true', help='Run the fixed audited core CI checks without advancing the local baseline')
    parser.add_argument('--full', action='store_true', help='Run all essential regression checks')
    parser.add_argument('--integration', action='store_true', help='Run all integration checks too (implies --full)')
    parser.add_argument('--extended', action='store_true', help='Also run large-data benchmarks (implies --full)')
    parser.add_argument('--package-exe', type=Path, help='Check a built executable with isolated records')
    parser.add_argument('--package-only', action='store_true', help='Check only the executable after source checks')
    args = parser.parse_args(argv)
    reset()
    if args.rerun and any((args.base, args.basic, args.full, args.integration, args.extended, args.package_exe, args.package_only)):
        parser.error('--rerun permits only --diagnostics and --plan')
    diagnostics_root = (args.diagnostics or REPORTS).resolve()
    try:
        validate_diagnostics_path(diagnostics_root)
    except ValueError as error:
        parser.error(str(error))
    if args.basic and (args.full or args.integration or args.extended or args.package_only):
        parser.error('--basic cannot be combined with another source-suite option or --package-only')
    if args.package_only and (not args.package_exe or args.full or args.integration or args.extended):
        parser.error('--package-only requires --package-exe and cannot be combined with a source-suite option')
    try:
        if args.rerun:
            selection = load_rerun(args.rerun.resolve())
            current = selection['source_files']
            current_contract = selection['version_contract']
            original_baseline = selection['baseline']
            public = selection['plan']
            plan = public
            changed = public['changed']
            for key, value in selection['options'].items():
                if key != 'package_sha256':
                    setattr(args, key, value)
            args.package_exe = Path(args.package_exe) if args.package_exe else None
        else:
            current = file_hashes()
            current_contract = version_contract()
            original_baseline = baseline_input()
            baseline = original_baseline['value']
            if args.base:
                args.base = git_bytes('rev-parse', '--verify', args.base + '^{commit}').decode().strip()
            changed = changed_files(current, baseline, args.base)
            plan = select_tests(changed,deleted=set(changed or ())-current.keys(),
                                version_only=release_version_only(base_ref=args.base, baseline=baseline))
            if args.basic:
                plan = {**plan, 'full':False, 'tests':basic_tests(), 'affected_tests':(), 'groups':('basic',),
                        'reasons':{test:['Audited basic CI contract'] for test in basic_tests()}}
            if args.full or args.integration or args.extended:
                plan = {**plan, 'full':True, 'tests':('tests',), 'groups':('full',),
                        'reasons':{'tests':['사용자가 전체 검사를 지정함']}, 'unmapped':()}
            if args.package_only:
                plan = {**plan, 'full':False, 'tests':(), 'affected_tests':(), 'groups':(), 'reasons':{}, 'unmapped':()}
            public = dict(changed=sorted(changed) if changed is not None else None,
                          suite='basic' if args.basic else 'extended' if args.extended else 'integration' if args.integration else
                          'essential+affected' if plan['full'] and plan['affected_tests'] else 'essential' if plan['full'] else 'affected',
                          full=plan['full'], unmapped=plan['unmapped'],
                          groups=plan['groups'], tests=plan['tests'], affected_tests=plan['affected_tests'], reasons=plan['reasons'],
                          package_impact=plan['package_impact'], proxy_impact=plan['proxy_impact'],
                          qa_smokes=plan['qa_smokes'],
                          package_exe=str(args.package_exe) if args.package_exe else None)
            if args.package_exe:
                args.package_exe = args.package_exe.resolve()
                if not args.package_exe.is_file():
                    parser.error('Package executable does not exist: ' + str(args.package_exe))
                public['package_exe'] = str(args.package_exe)
            selection = dict(version=1, root=str(ROOT), python=sys.executable,
                             python_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
                             source_files=current, version_contract=current_contract,
                             baseline=original_baseline, base_commit=args.base, plan=public,
                             options=dict(basic=args.basic, full=args.full, integration=args.integration,
                                          extended=args.extended, package_only=args.package_only,
                                          package_exe=str(args.package_exe) if args.package_exe else None,
                                          package_sha256=hashlib.sha256(args.package_exe.read_bytes()).hexdigest() if args.package_exe else None))
    except DiagnosticFailure:
        print(json.dumps(dict(result='diagnostics_failed', diagnostics=status(), diagnostics_root=str(diagnostics_root),
                              product_exit_code=None, report_persisted=False, rerun_command=None)))
        return DIAGNOSTIC_EXIT
    if args.plan:
        print(json.dumps(dict(public, diagnostics_root=str(diagnostics_root)),ensure_ascii=False,indent=2))
        return 2 if plan['unmapped'] else 0
    if plan['unmapped']:
        print(json.dumps(dict(result='unmapped', paths=plan['unmapped'],
                             action='tools/verify_changes.py의 RULES에 검사 연결을 추가하거나 --full을 지정하세요.'), ensure_ascii=False))
        return 2
    folder = diagnostics_root / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
    print('Diagnostics directory ' + str(folder), file=sys.stderr, flush=True)
    manifest = None
    rerun_command = None
    report = dict(public, logs=[], package=None, diagnostics_root=str(diagnostics_root),
                  diagnostics_directory=str(folder), baseline_path=str(BASELINE.resolve()),
                  base_commit=selection['base_commit'], product_exit_code=None)

    def finish(result, code, **details):
        report.update(diagnostics=status(), rerun_manifest=str(manifest) if manifest else None,
                      rerun_command=rerun_command, report_persisted=False)
        try:
            report['report_persisted'] = True
            write_json(folder / 'report.json', report)
        except DiagnosticFailure:
            report['report_persisted'] = False
        if code == 0 and not status()['first_error'] and not args.package_only and not args.basic:
            temporary = BASELINE.with_suffix('.tmp')
            try:
                with diagnostic_io('advance verification baseline', BASELINE):
                    raw = BASELINE.read_bytes() if BASELINE.exists() else None
                    expected = original_baseline['content']
                    if raw != (base64.b64decode(expected) if expected is not None else None):
                        raise ValueError('Verification baseline changed since the original invocation')
                    BASELINE.parent.mkdir(parents=True, exist_ok=True)
                    temporary.write_text(json.dumps(dict(version=1, files=current, report=str(folder/'report.json'),
                        version_contract=current_contract), ensure_ascii=False, indent=2), encoding='utf-8')
                    temporary.replace(BASELINE)
            except DiagnosticFailure:
                pass
        if status()['first_error']:
            code = code or DIAGNOSTIC_EXIT
            result = 'diagnostics_failed'
            report['diagnostics'] = status()
            try:
                write_json(folder / 'report.json', report)
            except DiagnosticFailure:
                report['report_persisted'] = False
        print(json.dumps(dict(result=result, **details, report=str(folder/'report.json'),
                              diagnostics_root=str(diagnostics_root), diagnostics_directory=str(folder),
                              diagnostics=status(), product_exit_code=report['product_exit_code'],
                              report_persisted=report['report_persisted'], rerun_command=rerun_command,
                              rerun_manifest=str(manifest) if manifest else None), ensure_ascii=False))
        return code

    try:
        manifest = args.rerun.resolve() if args.rerun else save_rerun(selection)
        rerun_command = '& ' + ' '.join("'" + value.replace("'", "''") + "'" for value in
            [sys.executable, str(ROOT/'tools/verify_changes.py'), '--rerun', str(manifest),
             '--diagnostics', str(diagnostics_root)])
        with diagnostic_io('create diagnostics directory', folder):
            folder.mkdir(parents=True, exist_ok=False)
    except DiagnosticFailure:
        return finish('diagnostics_failed', DIAGNOSTIC_EXIT, rerun_available=manifest is not None)
    if plan['tests']:
        from tools.check_environment import check_environment
        environment=check_environment()
        report['environment']=environment
        if environment['errors']:
            return finish('environment_failed', 2, **environment,
                          action='Use a dedicated environment installed from requirements-release.lock.')
        from tools.build_native import checked_extension, extension_path
        if extension_path().is_file() or os.environ.get('CODEXON_CPU_BACKEND') == 'rust':
            try:
                report['native'] = {'extension': str(checked_extension()), 'abi_version': 1}
            except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
                report['native_error'] = str(error)
                return finish('native_failed', 2, error=str(error),
                              action='Run tools/build_native.py with this Python environment before verifying.')
        started = time.monotonic()
        deadline = started + 820  # Leave the Actions 15-minute step time for evidence upload/exit.
        runs = []
        for name, command in pytest_commands(plan['tests'], integration=args.integration,
                                             extended=args.extended, report_dir=folder, affected_tests=plan['affected_tests']):
            remaining = deadline - time.monotonic() - 20
            if remaining <= 0:
                runs.append(dict(environment=name, exit_code=124, seconds=0, summary='Source budget exhausted'))
                break
            budget = min(remaining, {'core':180, 'gui':720, 'installation':540}[name])
            boundary = command.index('--')
            command[boundary:boundary] = ['--timeout', str(budget), '--diagnostics', str(folder / f'diagnostics-{name}')]
            run_started = time.monotonic()
            run_log = folder / f'pytest-{name}.log'
            try:
                result = run_logged(command, run_log)
            except DiagnosticFailure:
                return finish('diagnostics_failed', DIAGNOSTIC_EXIT)
            retain(getattr(result, 'diagnostics', {}).get('first_error'))
            report['product_exit_code'] = getattr(result, 'product_exit_code', result.returncode)
            report['logs'].append(str(run_log))
            runs.append(dict(environment=name, exit_code=result.returncode,
                             product_exit_code=report['product_exit_code'], diagnostics=getattr(result, 'diagnostics', status()),
                             seconds=round(time.monotonic()-run_started, 2),
                             summary=result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ''))
            if result.returncode:
                break
        log = folder / 'pytest.log'
        try:
            with diagnostic_io('aggregate check logs', log):
                with log.open('w', encoding='utf-8') as aggregate:
                    for name in report['logs']:
                        with Path(name).open(encoding='utf-8', errors='replace') as source:
                            shutil.copyfileobj(source, aggregate, length=65536)
        except DiagnosticFailure:
            pass
        report['logs'].append(str(log))
        exit_code = next((run['exit_code'] for run in runs if run['exit_code']), 0)
        report['pytest'] = dict(exit_code=exit_code, seconds=round(time.monotonic()-started,2),
                               summary=' | '.join(run['summary'] for run in runs), runs=runs)
        if exit_code or status()['first_error']:
            return finish('failed', exit_code or DIAGNOSTIC_EXIT, **report['pytest'], log=str(log))
    if args.package_exe:
        try:
            report['package'] = package_check(args.package_exe,folder)
        except DiagnosticFailure as error:
            report['product_exit_code'] = error.product_exit_code
            return finish('diagnostics_failed', error.product_exit_code or DIAGNOSTIC_EXIT)
        except Exception as error:
            report['package_error'] = str(error)
            report['product_exit_code'] = 1
            return finish('package_failed', 1, error=str(error))
    report['product_exit_code'] = 0
    return finish('passed', 0, changed=len(changed) if changed is not None else None,
                  groups=plan['groups'], pytest=report.get('pytest'), package=report['package'])


if __name__ == '__main__':
    raise SystemExit(main())
