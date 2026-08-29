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
        'presentation.metadata.status == "truncated"',
        'presentation.raw.status == "truncated"',
        'no_snapshot.status == "empty"',
        'omitted_snapshot.status == "unavailable"',
        '"No snapshot was stored" not in omitted_snapshot.message',
        "isinstance(selected.presentation, TrustedSelectedRunPresentation)",
    ):
        assert expected_contract in smoke


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
