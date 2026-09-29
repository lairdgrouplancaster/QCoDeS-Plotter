"""Source checks for the generated installed-wheel validation program."""

from __future__ import annotations

import ast

from scripts.validate_distribution import (
    ENTRYPOINT_DELEGATION_SITECUSTOMIZE,
    wheel_smoke_code,
)


def _assigned_string(module: ast.Module, name: str) -> str:
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == name
            for target in statement.targets
        ):
            continue
        value = ast.literal_eval(statement.value)
        assert isinstance(value, str)
        return value
    raise AssertionError(f"generated smoke code does not assign {name}")


def test_wheel_smoke_uses_current_qcodes_results_and_progressive_details() -> None:
    smoke = wheel_smoke_code()
    compile(smoke, "<qplot-wheel-smoke>", "exec")
    smoke_module = ast.parse(smoke)
    writer = _assigned_string(smoke_module, "WRITER_CODE")
    compile(writer, "<qplot-wheel-writer>", "exec")

    assert "id INTEGER PRIMARY KEY, setpoint REAL, signal REAL" in writer
    assert "(setpoint, signal) VALUES (?, ?)" in writer

    called_names = {
        node.func.id
        for node in ast.walk(smoke_module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(smoke_module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "submit_expensive_run" in called_attributes
    assert "submit_selected_run" in called_attributes
    assert smoke.count("assert_stage4_run_detail(") == 4
    assert "exercise_stage5c_qt_bridge" in called_names
    assert "qplot.windows.main" in smoke
    assert "cache_miss_state" in smoke
    assert "window.infoBox.preview._workers" in smoke
    for asserted_field in (
        'expensive_fields["result_count"] == 2',
        'expensive_fields["point_shape"] == [2]',
        'expensive_fields["setpoint_shape_source"] == "planned"',
        'expensive_fields["storage_bytes_estimated"] is True',
        "selected.snapshot.source.page(station.container_handle, None)",
        '(("run_id", str(run_id), None),)',
        "summary.first == 0.0",
        "summary.last == 1.0",
        "summary.steps == 2",
    ):
        assert asserted_field in smoke


def test_wheel_smoke_exercises_latest_bounded_detail_contract() -> None:
    smoke = wheel_smoke_code()
    compile(smoke, "<qplot-wheel-smoke>", "exec")
    smoke_module = ast.parse(smoke)

    called_names = {
        node.func.id
        for node in ast.walk(smoke_module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(smoke_module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "build_selected_run_presentation" in called_names
    assert "normalize_trusted_snapshot" in called_names
    assert "assert_repaired_bounded_views" in called_names
    assert "reprioritize" in called_attributes
    for installed_module in (
        "qplot.datahandling.trusted_presentation",
        "qplot.datahandling.trusted_snapshot",
        "qplot.datahandling.trusted_live_service",
    ):
        assert installed_module in smoke
    for expected_contract in (
        'scalar_presentation.metadata.status == "available"',
        'scalar_presentation.raw.status == "available"',
        "scalar_presentation.metadata.shortened_value_count == 1",
        "scalar_presentation.raw.shortened_value_count == 2",
        'node.key == "[display]"',
        "description_node.source_value_bytes == 1_258",
        "measurement_exception_node.source_value_bytes == 1_255",
        '"KeyboardInterrupt" in measurement_exception_node.value',
        "full_values[description_node.full_value_id].text == run_description",
        'structural_presentation.metadata.status == "truncated"',
        'structural_presentation.raw.status == "truncated"',
        'node.key == "[truncated]"',
        'no_snapshot.status == "empty"',
        'omitted_snapshot.status == "unavailable"',
        '"No snapshot was stored" not in omitted_snapshot.message',
        "isinstance(selected.presentation, TrustedSelectedRunPresentation)",
    ):
        assert expected_contract in smoke


def test_wheel_smoke_exercises_mixed_size_live_wal_priority() -> None:
    smoke = wheel_smoke_code()
    compile(smoke, "<qplot-wheel-smoke>", "exec")
    smoke_module = ast.parse(smoke)
    function_name = "exercise_stage5c_mixed_size_wal_priority"
    function = next(
        (
            node
            for node in smoke_module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ),
        None,
    )
    assert function is not None, (
        "the installed-wheel smoke must define a real public-QCoDeS mixed-size "
        "Stage 5C WAL acceptance"
    )
    called_names = {
        node.func.id
        for node in ast.walk(smoke_module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert function_name in called_names
    function_source = ast.get_source_segment(smoke, function)
    assert function_source is not None
    for required_contract in (
        "Measurement",
        "ManualParameter",
        ".set_shapes(",
        ".add_result(",
        ".flush_data_to_database(",
        "RunList.run_preview_is_ready",
        "infoBox.preview.cache",
        "_metadata_by_guid",
        "_detail_display_guid",
        "measurement_exception",
        "run_description",
        "stage5c_operator",
        "FULL_VALUE_PATH_ROLE",
        "FULL_VALUE_ID_ROLE",
        "itemActivated.emit",
        "itemDoubleClicked.emit",
        "_full_value_dialog",
        "prior_dialog._exact_text is None",
        "not prior_dialog.isVisible()",
        "toPlainText()",
        "parameters_truncated",
        "selected_snapshot_parameters",
        "station=selected_station",
        '138 * 1024 <= len(stored_snapshot.encode("utf-8")) <= 150 * 1024',
        "stored_snapshot.count",
        "> 1_024",
        'snapshot_view.status == "available"',
        '"loaded on demand" in snapshot_view.message',
        "snapshot_view.source is None",
        "len(initial_items) <= 128",
        "station_item.isExpanded()",
        '"/Snapshot/station/parameters"',
        '"Load more…"',
        "snapshot_tree.itemActivated.emit",
        '"/Snapshot/station/parameters/lazy_final_parameter/name"',
        'final_name.text(1) == "lazy_final_parameter"',
        "next_writer_index = remaining_count",
        "continuation_pages += 1",
        'remaining_run["datasaver"].add_result',
        'remaining_run["datasaver"].flush_data_to_database',
        "reader_baseline = protected_artifact_state",
        'checkpoint_until_idle("PASSIVE")',
        'checkpoint_until_idle("TRUNCATE")',
        "protected_artifact_state",
        "assert_source_policy",
        "live_helper_is_observable",
        "live_liveness.helper_pid is not None",
        "helper_alive",
        "background_active",
    ):
        assert required_contract in function_source


def test_wheel_smoke_exercises_installed_exact_process_supervision() -> None:
    smoke = wheel_smoke_code()
    compile(smoke, "<qplot-wheel-smoke>", "exec")
    smoke_module = ast.parse(smoke)
    launcher = _assigned_string(smoke_module, "LAUNCHER_DRIVER_CODE")
    normal_child = _assigned_string(
        smoke_module,
        "NORMAL_SUPERVISED_CHILD_CODE",
    )
    forced_child = _assigned_string(
        smoke_module,
        "FORCED_SUPERVISED_CHILD_CODE",
    )
    interrupted_child = _assigned_string(
        smoke_module,
        "INTERRUPTED_PUBLIC_CHILD_CODE",
    )
    vanishing_caller = _assigned_string(
        smoke_module,
        "VANISHING_API_CALLER_CODE",
    )
    sentinel = _assigned_string(smoke_module, "SENTINEL_CODE")
    for name, source in (
        ("launcher", launcher),
        ("normal child", normal_child),
        ("forced child", forced_child),
        ("interrupted child", interrupted_child),
        ("vanishing caller", vanishing_caller),
        ("sentinel", sentinel),
    ):
        compile(source, f"<qplot-wheel-{name}>", "exec")

    assert "from qplot import _shutdown_supervisor as shutdown_supervisor" in smoke
    assert "shutdown_supervisor._supervise_child(" in launcher
    assert "ShutdownSupervisorClient.from_environment().connect()" in normal_child
    assert "client.arm(hard_deadline)" in normal_child
    assert "raise SystemExit(17)" in normal_child
    assert '"arm_acknowledged": client.arm_acknowledged' in normal_child

    assert "TrustedLiveReaderSupervisor.open(" in forced_child
    assert '_test_fault="hang_before_operation"' in forced_child
    assert 'b"operation_hang"' in forced_child
    assert "ctypes.PyDLL" in forced_child
    assert "hold_python_gil()" in forced_child
    assert "raise AssertionError(" in forced_child
    assert "TrustedLiveReaderSupervisor.open(" in interrupted_child
    assert 'b"operation_hang"' in interrupted_child
    assert "qplot.run(database_path=database_path)" in vanishing_caller

    called_names = {
        node.func.id
        for node in ast.walk(smoke_module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "assert_installed_qplot_entrypoint_delegation" in called_names
    assert "exercise_installed_shutdown_supervision" in called_names
    assert "assert_installed_concurrent_cancellation_sender" in called_names
    assert "assert_installed_cancellation_owner_loss" in called_names
    assert "run_installed_public_api_interruption" in called_names
    assert "exercise_installed_public_api_caller_eof" in called_names
    assert "wait_for_process_exit" in called_names
    assert "assert_source_policy" in called_names
    assert "exact_first = SystemExit(37)" in smoke
    assert "installed second interrupt after SIGINT guard installation" in smoke
    assert "caught is exact_first" in smoke
    assert "second_injected" in smoke
    assert "len(created_workers) == 1" in smoke
    assert "len(start_calls) == 1" in smoke
    assert "bytes(sent_bytes) == frame" in smoke
    assert "installed interruption after {interrupted_commit} commit" in smoke
    assert "len(created_workers) <= 1" in smoke
    assert "len(start_calls) <= 1" in smoke
    assert "signal.getsignal(signal.SIGINT) is custom_sigint_handler" in smoke
    assert "signal.raise_signal(signal.SIGINT)" in smoke
    for process_survival_check in (
        'writer.poll() is None, "external WAL writer was terminated"',
        'sentinel.poll() is None, "external sentinel was terminated"',
    ):
        assert process_survival_check in smoke
    assert "shutdown_supervisor.launch_gui = capture_launch" in smoke
    assert (
        'qplot_entrypoint.run(database_path="explicit installed path.db") == 17'
        in smoke
    )
    immediate_helper_assertion = (
        'assert not process_is_running(forced_record["helper_pid"])'
    )
    cleanup_helper_wait = 'wait_for_process_exit(forced_record["helper_pid"])'
    assert immediate_helper_assertion in smoke
    assert cleanup_helper_wait in smoke
    assert smoke.index(immediate_helper_assertion) < smoke.index(cleanup_helper_wait)


def test_actual_installed_entrypoint_hook_captures_launcher_delegation() -> None:
    compile(
        ENTRYPOINT_DELEGATION_SITECUSTOMIZE,
        "<qplot-installed-entrypoint-sitecustomize>",
        "exec",
    )
    assert "from qplot import _shutdown_supervisor as shutdown_supervisor" in (
        ENTRYPOINT_DELEGATION_SITECUSTOMIZE
    )
    assert "shutdown_supervisor.launch_gui = capture_launch" in (
        ENTRYPOINT_DELEGATION_SITECUSTOMIZE
    )
    assert '"argv": list(original_argv)' in ENTRYPOINT_DELEGATION_SITECUSTOMIZE
    assert '"database_path": database_path' in ENTRYPOINT_DELEGATION_SITECUSTOMIZE
    assert "return 17" in ENTRYPOINT_DELEGATION_SITECUSTOMIZE
