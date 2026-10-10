"""Source checks for the generated installed-wheel validation program."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from scripts.validate_distribution import (
    ENTRYPOINT_DELEGATION_SITECUSTOMIZE,
    NATIVE_EXTENSION_MEMBERS,
    find_artifacts,
    install_wheels,
    validate_wheel,
    wheel_installation_audit_code,
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
        'initial_fields["measure_parameters"] == ["signal"]',
        'initial_fields["sweep_parameters"] == ["setpoint"]',
        'initial_fields["preview_dimensions"] == [1]',
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


@pytest.mark.parametrize(
    "incomplete_record,incomplete_pid",
    [
        (FileNotFoundError(), "33"),
        ("", "33"),
        ('{"gui_pid":', "33"),
        ("{}", "33"),
        ('{"gui_pid":11,"helper_pid":22}', ""),
        ('{"gui_pid":11,"helper_pid":22}', FileNotFoundError()),
    ],
)
@pytest.mark.parametrize("outcome", ["ready", "timeout", "caller_exit"])
def test_installed_caller_eof_waits_for_complete_readiness(
    incomplete_record, incomplete_pid, outcome,
):
    smoke = ast.parse(wheel_smoke_code())
    function = next(
        node for node in smoke.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "exercise_installed_public_api_caller_eof"
    )
    caller = Mock()
    caller.poll.return_value = None
    caller.communicate.return_value = ("child output", "child error")
    caller.kill.side_effect = lambda: setattr(caller.poll, "return_value", -1)
    record_path = Mock()
    record_path.read_text.side_effect = [
        incomplete_record, '{"gui_pid":11,"helper_pid":22}',
    ]
    launcher_path = Mock()
    launcher_path.read_text.side_effect = [incomplete_pid, "33"]
    clock = Mock()
    clock.monotonic.side_effect = [0.0, 11.0 if outcome == "timeout" else 0.0]
    wait_for_exit = Mock()
    namespace = {
        "sys": sys,
        "subprocess": SimpleNamespace(Popen=Mock(return_value=caller), DEVNULL=-3, PIPE=-1),
        "time": clock,
        "json": json,
        "VANISHING_API_CALLER_CODE": "unused mock child",
        "wait_for_process_exit": wait_for_exit,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<smoke-test>", "exec"), namespace)
    exercise = namespace[function.name]
    if outcome == "caller_exit":
        caller.poll.return_value = 1
        with pytest.raises(AssertionError, match="exited before readiness"):
            exercise("child.py", record_path, "test.db", launcher_path)
        caller.kill.assert_not_called()
        wait_for_exit.assert_not_called()
    elif outcome == "timeout":
        with pytest.raises(TimeoutError, match="did not become ready"):
            exercise("child.py", record_path, "test.db", launcher_path)
        caller.kill.assert_called_once()
        wait_for_exit.assert_not_called()
    else:
        exercise("child.py", record_path, "test.db", launcher_path)
        caller.kill.assert_called_once()
        clock.sleep.assert_called_once_with(0.01)
        assert wait_for_exit.call_args_list == [call(33), call(11), call(22)]


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


PACKAGING_SOURCE = {
    "src/qplot/__init__.py",
    "native/src/qplot_native/__init__.py",
    "native/src/qplot_native/_trusted_vfs_native.c",
}


def _split_wheel(tmp_path, *, native=False, extra_files=(), native_pin="1.0.0"):
    name = "qcodes_plotter_native" if native else "qcodes_plotter"
    version = "1.0.0" if native else "1.6.0b2"
    tag = "cp311-abi3-test_platform" if native else "py3-none-any"
    artifact = tmp_path / f"{name}-{version}-{tag}.whl"
    info = f"{name}-{version}.dist-info"
    metadata = (
        f"Name: {name.replace('_', '-')}\nVersion: {version}\n"
        "Requires-Dist: apsw==3.53.4.0\n"
    )
    if not native:
        metadata += f"Requires-Dist: qcodes-plotter-native=={native_pin}\n"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(f"{info}/METADATA", metadata)
        archive.writestr(
            f"{info}/WHEEL",
            f"Root-Is-Purelib: {'false' if native else 'true'}\nTag: {tag}\n",
        )
        package = "qplot_native" if native else "qplot"
        archive.writestr(f"{package}/__init__.py", "")
        if native:
            archive.writestr(sorted(NATIVE_EXTENSION_MEMBERS)[0], b"native fixture")
        for path in extra_files:
            archive.writestr(path, b"unexpected fixture")
    return artifact


def test_validator_accepts_both_split_wheels(tmp_path):
    app = _split_wheel(tmp_path)
    native = _split_wheel(tmp_path, native=True)
    assert validate_wheel(app, PACKAGING_SOURCE) == {"qplot/__init__.py"}
    assert "qplot_native/__init__.py" in validate_wheel(native, PACKAGING_SOURCE, native=True)
    assert find_artifacts([tmp_path], wheel_only=True) == {
        "qcodes_plotter_wheel": app,
        "qcodes_plotter_native_wheel": native,
    }


@pytest.mark.parametrize("extra_file", [
    "qplot/datahandling/_trusted_vfs_native.abi3.so",
    "qplot_native/__init__.py",
    "qplot/source.c",
])
def test_validator_rejects_native_files_in_app_wheel(tmp_path, extra_file):
    app = _split_wheel(tmp_path, extra_files=[extra_file])
    with pytest.raises(AssertionError):
        validate_wheel(app, PACKAGING_SOURCE)


def test_validator_rejects_incompatible_native_dependency(tmp_path):
    app = _split_wheel(tmp_path, native_pin="0.9.0")
    with pytest.raises(AssertionError):
        validate_wheel(app, PACKAGING_SOURCE)


def test_validator_requires_both_local_wheels(tmp_path):
    _split_wheel(tmp_path)
    with pytest.raises(AssertionError, match="qcodes_plotter_native wheel"):
        find_artifacts([tmp_path], wheel_only=True)


def test_validator_preserves_native_stable_abi_tag(tmp_path):
    native = _split_wheel(tmp_path, native=True)
    wrong_tag = native.with_name(native.name.replace("cp311-abi3", "cp312-cp312"))
    native.rename(wrong_tag)
    with pytest.raises(AssertionError, match="cp311-abi3"):
        validate_wheel(wrong_tag, PACKAGING_SOURCE, native=True)


def test_install_pair_cannot_fall_back_to_an_index(tmp_path, monkeypatch):
    app = _split_wheel(tmp_path)
    native = _split_wheel(tmp_path, native=True)
    run = Mock()
    monkeypatch.setattr('scripts.validate_distribution.run', run)
    install_wheels(tmp_path / 'python', app, native, with_dev_tools=True)
    first, second = [invocation.args[0] for invocation in run.call_args_list]
    assert '--no-index' in first and '--no-deps' in first
    assert '--force-reinstall' in first
    assert first[-2:] == [str(native.resolve()), str(app.resolve())]
    assert second[-2:] == [str(native.resolve()), str(app.resolve()) + '[dev]']
    assert second[second.index('--only-binary') + 1] == 'apsw'
    run.reset_mock()
    run.side_effect = subprocess.CalledProcessError(1, first)
    with pytest.raises(subprocess.CalledProcessError):
        install_wheels(tmp_path / 'python', app, native)
    assert run.call_count == 1, 'No dependency/index resolution after a missing local wheel'


@pytest.mark.parametrize('fault', [None, 'changed-native', 'checkout-native', 'checkout-app', 'old-version'])
def test_import_audit_detects_stale_files_and_shadow_packages(tmp_path, monkeypatch, fault):
    import importlib
    import importlib.metadata

    app = _split_wheel(tmp_path)
    native = _split_wheel(tmp_path, native=True)
    prefix = tmp_path / 'environment'
    prefix.mkdir()
    for artifact in (app, native):
        with zipfile.ZipFile(artifact) as archive:
            archive.extractall(prefix)
    module_paths = {
        'qplot': prefix / 'qplot/__init__.py',
        'qplot_native': prefix / 'qplot_native/__init__.py',
        'qplot_native._trusted_vfs_native': prefix / 'qplot_native/_trusted_vfs_native.abi3.so',
    }
    if fault == 'changed-native':
        module_paths['qplot_native._trusted_vfs_native'].write_bytes(b'stale binary')
    elif fault == 'checkout-native':
        module_paths['qplot_native._trusted_vfs_native'] = tmp_path / 'checkout/native.abi3.so'
    elif fault == 'checkout-app':
        module_paths['qplot'] = tmp_path / 'checkout/qplot/__init__.py'

    def installed_distribution(name):
        version = '1.0.0' if name == 'qcodes-plotter-native' else '1.6.0b2'
        if fault == 'old-version' and name == 'qcodes-plotter-native':
            version = '0.9.0'
        return SimpleNamespace(version=version, locate_file=lambda relative: prefix / relative)

    monkeypatch.setattr(importlib.metadata, 'distribution', installed_distribution)
    imports = Mock(side_effect=lambda name: SimpleNamespace(__file__=str(module_paths[name])))
    monkeypatch.setattr(importlib, 'import_module', imports)
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name: SimpleNamespace(origin=str(module_paths[name])))
    monkeypatch.setattr(sys, 'prefix', str(prefix))
    # The audit is normally run in a fresh interpreter. Avoid inspecting the
    # real packages already loaded by this test process's Qt fixtures.
    for name in list(sys.modules):
        if name == 'qplot' or name.startswith(('qplot.', 'qplot_native')):
            monkeypatch.delitem(sys.modules, name)
    code = wheel_installation_audit_code([app, native])
    if fault is None:
        exec(compile(code, '<wheel-audit>', 'exec'), {})
    else:
        with pytest.raises(AssertionError):
            exec(compile(code, '<wheel-audit>', 'exec'), {})
    if fault == 'checkout-native':
        assert call('qplot_native._trusted_vfs_native') not in imports.call_args_list
    elif fault == 'checkout-app':
        assert call('qplot') not in imports.call_args_list
