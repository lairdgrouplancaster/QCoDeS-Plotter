"""Ordinary CI keeps full Python and Linux testing for explicit runs and releases."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.pure
SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/ci_plan.py"
if not SCRIPT.is_file():
    pytest.skip("CI policy is not shipped in source distributions", allow_module_level=True)
spec = importlib.util.spec_from_file_location("ci_plan", SCRIPT)
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


@pytest.mark.parametrize("event_name,ref", [
    ("push", "refs/heads/main"),
    ("pull_request", "refs/pull/1/merge"),
    ("workflow_dispatch", "refs/heads/topic"),
    ("workflow_call", "refs/heads/main"),
])
def test_ordinary_events_use_routine_validation_without_an_override(event_name, ref):
    full, reason = plan.validation_mode(event_name, ref, "false")
    assert not full and reason


@pytest.mark.parametrize("event_name", [
    "push", "pull_request", "workflow_dispatch", "workflow_call", "unexpected",
])
def test_explicit_full_validation_is_honoured_for_every_event(event_name):
    full, reason = plan.validation_mode(event_name, "refs/heads/main", "true")
    assert full and reason


@pytest.mark.parametrize("event_name", ["push", "workflow_dispatch", "workflow_call"])
@pytest.mark.parametrize("ref", ["refs/tags/v1.7.0", "refs/tags/native-and-app/v1.7.0"])
def test_release_tags_cannot_disable_full_validation(event_name, ref):
    assert plan.validation_mode(event_name, ref, "false")[0]


@pytest.mark.parametrize("override", ["", "TRUE", "False", "unexpected"])
@pytest.mark.parametrize("event_name", ["push", "workflow_dispatch", "workflow_call"])
def test_missing_or_invalid_override_selects_full_validation(event_name, override):
    assert plan.validation_mode(event_name, "refs/heads/main", override)[0]


@pytest.mark.parametrize("event_name", ["", "schedule", "unexpected"])
def test_unknown_events_default_to_full_validation(event_name):
    assert plan.validation_mode(event_name, "refs/heads/main", "false")[0]


@pytest.mark.parametrize("full", [False, True])
def test_check_matrix_selects_full_versions_and_keeps_complete_partitions(full):
    entries = [row for row in plan.check_matrix(full)["include"]
               if row["qt-version"] == "latest"]
    actual = {(row["os"], row["python-version"], row["suite"], row.get("partition")) for row in entries}
    expected = {
        (runner, version, "full", partition)
        for runner in ("windows-2025", "macos-latest")
        for version in (("3.12", "3.13", "3.14") if full else ("3.14",))
        for partition in (("1/4", "2/4", "3/4", "4/4") if runner == "windows-2025" else ("1/2", "2/2"))
    }
    if not full:
        expected |= {(runner, version, "compatibility", None)
                     for runner in ("windows-2025", "macos-latest")
                     for version in ("3.12", "3.13")}
    compatibility = ("macos-15-intel", "ubuntu-latest") if full else ("macos-15-intel",)
    expected |= {(runner, version, "compatibility", None)
                 for runner in compatibility for version in ("3.12", "3.13", "3.14")}
    assert actual == expected
    assert len(entries) == len(expected) == (24 if full else 13)
    artifacts = {
        "windows-2025": "qplotter-native-windows-x64",
        "macos-latest": "qplotter-native-macos-arm64",
        "macos-15-intel": "qplotter-native-macos-intel",
        "ubuntu-latest": "qplotter-native-linux-x86_64",
    }
    assert all(row["native-artifact"] == artifacts[row["os"]] for row in entries)
    assert all(row["qt-requirement"] == "PyQt6" for row in entries)
    for row in entries:
        if row["suite"] == "compatibility":
            expected_list = (
                ".github/pytest-compatibility.txt" if full
                else ".github/pytest-compatibility-routine.txt"
            )
            assert row["test-list"] == expected_list
            assert (SCRIPT.parents[2] / row["test-list"]).is_file()
        else:
            assert "test-list" not in row


@pytest.mark.parametrize("full", [False, True])
def test_minimum_qt_covers_every_python_platform_pair_without_duplicate_jobs(full):
    entries = plan.check_matrix(full)["include"]
    minimum = plan.minimum_pyqt6_version()
    minimum_entries = [row for row in entries if row["qt-version"] == minimum]
    latest_pairs = {(row["os"], row["python-version"]) for row in entries
                    if row["qt-version"] == "latest"}
    assert {(row["os"], row["python-version"]) for row in minimum_entries} == latest_pairs
    assert len(minimum_entries) == len(latest_pairs) == (12 if full else 9)
    assert all(row["qt-requirement"] == f"PyQt6=={minimum}" for row in minimum_entries)
    assert all(row["suite"] == "compatibility" and "partition" not in row
               for row in minimum_entries)
    expected_list = (".github/pytest-compatibility.txt" if full
                     else ".github/pytest-compatibility-routine.txt")
    assert all(row["test-list"] == expected_list for row in minimum_entries)
    identities = {(row["os"], row["python-version"], row["qt-version"],
                   row["suite"], row.get("partition")) for row in entries}
    assert len(identities) == len(entries) == (36 if full else 22)


def test_minimum_qt_is_read_from_application_metadata(tmp_path):
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\ndependencies = ["numpy>=2", "PyQt6>=6.10.2"]\n')
    assert plan.minimum_pyqt6_version(project) == "6.10.2"


@pytest.mark.parametrize("requirements", [[], ["PyQt6"], ["PyQt6~=6.9"], ["PyQt6>=6.9,<7"]])
def test_ambiguous_qt_floor_fails_planning_instead_of_silently_omitting_coverage(tmp_path, requirements):
    project = tmp_path / "pyproject.toml"
    project.write_text(f'[project]\ndependencies = {json.dumps(requirements)}\n')
    with pytest.raises(ValueError, match="explicit PyQt6>=VERSION"):
        plan.minimum_pyqt6_version(project)


@pytest.mark.parametrize("full", [False, True])
def test_installation_matrix_keeps_each_platform_and_python_pair(full):
    entries = plan.installation_matrix(full)["include"]
    platforms = {"macos-arm64", "macos-intel", "windows-x64"}
    if full:
        platforms.add("linux-x86_64")
    assert {(row["platform"], row["python-version"]) for row in entries} == {
        (platform, version) for platform in platforms for version in ("3.12", "3.13", "3.14")
    }
    assert len(entries) == (12 if full else 9)
    for row in entries:
        assert set(row) == {"platform", "os-name", "os", "native-artifact", "python-version"}
        assert row["native-artifact"] == f"qplotter-native-{row['platform']}"


@pytest.mark.parametrize("event_name,ref,override,full", [
    ("push", "refs/heads/main", "false", False),
    ("pull_request", "refs/pull/1/merge", "false", False),
    ("workflow_dispatch", "refs/heads/main", "false", False),
    ("workflow_call", "refs/heads/main", "false", False),
    ("workflow_dispatch", "refs/heads/main", "true", True),
    ("workflow_call", "refs/heads/main", "true", True),
    ("push", "refs/tags/v1.7.0", "false", True),
    ("unexpected", "refs/heads/main", "false", True),
    ("push", "refs/heads/main", "", True),
])
def test_cli_emits_complete_matrices_without_git_or_event_payload(
    tmp_path, event_name, ref, override, full,
):
    output = tmp_path / "outputs"
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, check=True, capture_output=True, text=True,
        env={**os.environ, "PATH": "", "GITHUB_EVENT_NAME": event_name,
             "GITHUB_EVENT_PATH": str(tmp_path / "missing-event.json"),
             "GITHUB_REF": ref, "QPLOT_FULL_VALIDATION": override,
             "GITHUB_OUTPUT": str(output)},
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(outputs) == {"full-validation", "platforms", "reason"}
    assert outputs["full-validation"] == str(full).lower()
    assert outputs["reason"]
    platforms = json.loads(outputs["platforms"])
    assert platforms == plan.platform_matrices(full)
    assert sum(len(row["checks"]["include"]) for row in platforms.values()) == (36 if full else 22)
    assert sum(len(row["installations"]["include"]) for row in platforms.values()) == (12 if full else 9)
    assert f"CI mode: {'full' if full else 'routine'}." in result.stdout


@pytest.mark.parametrize("full", [False, True])
def test_platform_matrices_preserve_every_check_once_and_bind_matching_artifacts(full):
    platforms = plan.platform_matrices(full)
    assert set(platforms) == {"linux-x86_64", "windows-x64", "macos-arm64", "macos-intel"}
    for suite, expected in (("checks", plan.check_matrix(full)),
                            ("installations", plan.installation_matrix(full))):
        actual = []
        for platform, matrices in platforms.items():
            rows = matrices[suite]["include"]
            assert bool(rows) == (full or platform != "linux-x86_64")
            assert all(row["native-artifact"] == f"qplotter-native-{platform}" for row in rows)
            actual.extend(rows)
        canonical = lambda row: json.dumps(row, sort_keys=True)
        assert sorted(actual, key=canonical) == sorted(expected["include"], key=canonical)


def test_workflow_offers_explicit_full_validation_and_keeps_release_gates():
    workflows = SCRIPT.parents[1] / "workflows"
    ci = (workflows / "ci.yml").read_text()
    assert "\n  schedule:" not in ci
    dispatch = ci.split("  workflow_dispatch:\n", 1)[1].split("  workflow_call:\n", 1)[0]
    assert "full-validation:" in dispatch
    assert "type: boolean" in dispatch and "default: true" in dispatch
    planning = ci.split("\n  ci-plan:\n", 1)[1].split("\n  lint:\n", 1)[0]
    assert "QPLOT_FULL_VALIDATION: ${{ inputs.full-validation || false }}" in planning
    assert "fetch-depth: 0" not in planning
    release = (workflows / "release.yml").read_text()
    validation = release.split("  validate:\n", 1)[1].split("  prepare:\n", 1)[0]
    assert "uses: ./.github/workflows/ci.yml" in validation
    assert "full-validation: true" in validation


def test_runtime_qt_selection_preserves_wheel_audit_and_separate_analysis_pin():
    workflows = SCRIPT.parents[1] / "workflows"
    platform = (workflows / "ci-platform.yml").read_text()
    selection = platform.split("- name: Select runtime Qt version\n", 1)[1].split("- name:", 1)[0]
    assert 'python -m pip install --upgrade --only-binary=:all:' in selection
    assert '"${{ matrix.qt-requirement }}"' in selection
    installation = platform.split("- name: Install package and test tools\n", 1)[1].split("- name:", 1)[0]
    assert '--extra-requirement "${{ matrix.qt-requirement }}"' in installation
    assert '--audit-path dist/wheel-installation-audit.py dist' in installation
    assert platform.index("Select runtime Qt version") < platform.index("Install package and test tools")
    assert "QPLOT_CI_WHEEL_AUDIT:" in platform
    assert "python -m pip show PyQt6 PyQt6-Qt6 PyQt6-sip" in platform
    ci = (workflows / "ci.yml").read_text()
    analysis = ci.split("\n  static-analysis:\n", 1)[1].split("\n  coverage:\n", 1)[0]
    assert 'python -m pip install "PyQt6==6.11.0"' in analysis
    assert "qt-requirement" not in analysis
