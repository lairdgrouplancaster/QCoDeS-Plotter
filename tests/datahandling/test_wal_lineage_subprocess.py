"""Process-isolated regressions for first-observed SQLite WAL lineage."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCENARIO_HELPER = Path(__file__).with_name("_wal_lineage_scenarios.py")


def _run_isolated_scenario(tmp_path, scenario, *arguments):
    work_directory = tmp_path / "child-source"
    environment = os.environ.copy()
    source_path = str(_PROJECT_ROOT / "src")
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not prior_pythonpath
        else os.pathsep.join((source_path, prior_pythonpath))
    )
    result = subprocess.run(
        [
            sys.executable,
            str(_SCENARIO_HELPER),
            scenario,
            str(work_directory),
            *arguments,
        ],
        cwd=_PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"Fresh-process WAL scenario {scenario!r} failed.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_fresh_process_rejects_independent_qcodes_main_and_wal(tmp_path):
    _run_isolated_scenario(tmp_path, "independent-main-and-wal")


def test_live_qcodes_wal_follows_documented_checkpoint_safe_path(tmp_path):
    _run_isolated_scenario(tmp_path, "live-qcodes-safe-path")


@pytest.mark.parametrize("artifact", ["main", "wal"])
def test_replacement_during_generated_wal_snapshot_is_rejected(
    tmp_path,
    artifact,
):
    _run_isolated_scenario(tmp_path, "snapshot-replacement", artifact)


def test_registered_same_process_replacement_history_still_works(tmp_path):
    _run_isolated_scenario(tmp_path, "registered-replacement-history")


def test_generated_database_provenance_works_in_fresh_process(tmp_path):
    _run_isolated_scenario(tmp_path, "generated-provenance")


def test_fresh_process_accepts_enabled_future_table_append_after_checkpoint(
    tmp_path,
):
    _run_isolated_scenario(tmp_path, "future-table-checkpoint-append")


def test_fresh_process_rejects_same_token_fork_wal(tmp_path):
    _run_isolated_scenario(tmp_path, "same-token-fork-wal")


@pytest.mark.parametrize("artifact", ["main", "wal"])
def test_replacement_during_generated_wal_provenance_validation_is_rejected(
    tmp_path,
    artifact,
):
    _run_isolated_scenario(
        tmp_path,
        "provenance-validation-replacement",
        artifact,
    )


def test_all_wal_policy_paths_preserve_every_source_artifact(tmp_path):
    _run_isolated_scenario(tmp_path, "source-invariance")
