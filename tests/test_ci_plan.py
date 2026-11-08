"""Routine CI is allowed only after a complete, non-sensitive Git comparison."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.pure
SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/ci_plan.py"
if not SCRIPT.is_file():
    pytest.skip("CI policy is not shipped in source distributions", allow_module_level=True)
spec = importlib.util.spec_from_file_location("ci_plan", SCRIPT)
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


@pytest.fixture
def git_repository(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*arguments):
        return subprocess.run(
            ["git", "-c", "user.name=CI policy tests", "-c", "user.email=ci@example.invalid",
             "-c", "commit.gpgsign=false", *arguments],
            cwd=repository, check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init")
    (repository / "README.md").write_text("Initial documentation\n")
    (repository / "pyproject.toml").write_text("# Initial dependencies\n")
    git("add", ".")
    git("commit", "-m", "Initial fixture")
    return repository, git


def pushed_mode(repository, before, after):
    return plan.validation_mode(
        "push", {"before": before, "after": after}, "refs/heads/main", "false",
        repository=repository,
    )[0]


@pytest.mark.parametrize("event_name,ref,override", [
    ("pull_request", "refs/pull/1/merge", "true"),
    ("push", "refs/heads/main", "true"),
    ("schedule", "refs/heads/main", "false"),
    ("workflow_dispatch", "refs/heads/topic", "false"),
    ("push", "refs/tags/v1.7.0", "false"),
    ("push", "refs/tags/native-and-app/v1.7.0", "false"),
    ("push", "refs/heads/main", ""),
    ("push", "refs/heads/main", "unexpected"),
])
def test_full_triggers_do_not_need_comparison(monkeypatch, tmp_path, event_name, ref, override):
    def forbidden(*args, **kwargs):
        pytest.fail("Full validation must not need Git history")

    monkeypatch.setattr(plan.subprocess, "run", forbidden)
    full, reason = plan.validation_mode(event_name, None, ref, override, repository=tmp_path)
    assert full and reason


@pytest.mark.parametrize("event_name,event", [
    ("push", None), ("push", []), ("push", {}),
    ("push", {"before": "0" * 40, "after": "a" * 40}),
    ("push", {"before": "a" * 40, "after": "0" * 40}),
    ("push", {"before": "--name-only", "after": "a" * 40}),
    ("pull_request", {}), ("pull_request", {"pull_request": []}),
    ("pull_request", {"pull_request": {"base": None, "head": {"sha": "a" * 40}}}),
    ("pull_request", {"pull_request": {"base": {"sha": "b" * 40}, "head": {}}}),
    ("unexpected", {"before": "b" * 40, "after": "a" * 40}),
])
def test_unusable_event_selects_full(tmp_path, event_name, event):
    assert plan.validation_mode(event_name, event, "refs/heads/main", "false", repository=tmp_path)[0]


@pytest.mark.parametrize("error", [
    OSError("Git unavailable"),
    subprocess.CalledProcessError(128, "git"),
    subprocess.TimeoutExpired("git", 60),
])
def test_comparison_failure_selects_full(monkeypatch, tmp_path, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(plan.subprocess, "run", fail)
    assert pushed_mode(tmp_path, "a" * 40, "b" * 40)


@pytest.mark.parametrize("path", [
    ".github/workflows/ci.yml", ".github/scripts/ci_plan.py", "native/src/reader.c",
    "scripts/release.py", "scripts/validate_distribution.py",
    "src/qplot/datahandling/readonly.py", "tests/datahandling/test_trusted_live.py",
    "src/qplot/_auxiliary_paths.py", "src/qplot/testdata.py", "src/qplot/_testdata_samples.py",
    "pyproject.toml", "MANIFEST.in", "setup.cfg", "requirements.txt",
    "requirements-dev.in", "constraints-test.txt", "uv.lock", "poetry.lock", "Pipfile.lock",
    "requirements/dev.txt", "constraints/linux.txt", "requirements.lock",
    "environment.yml", "pylock.ci.toml", "subproject/pyproject.toml",
    "tests/test_distribution_validator.py", "tests/test_packaging_split.py",
    "tests/test_package_import.py", "tests/test_native_reader.py", "tests/test_release.py",
    "tests/test_ci_plan.py", "tests/test_compiler_free_install.py", "tests/test_testdata.py",
    "tests/test_testdata_specifications.py", "tests/conftest.py", "tests/_ci_scheduling.py",
    "tests/ci_timings.json", "tests/test_windows_ci.py", "tests/test_update_ci_timings.py",
    "tests/test_auxiliary_database_protection.py", "tests/test_bug_hunt_auxiliary_safety.py",
    "tests/test_bug_hunt_generator_fifo.py",
])
def test_safety_distribution_dependencies_and_ci_are_sensitive(path):
    assert plan.sensitive_path(path)


@pytest.mark.parametrize("path", [
    "README.md", "CONTRIBUTING.md", "docs/architecture.md", "src/qplot/windows/_plot_actions.py",
    "src/qplot/tools/plot_tools.py", "tests/windows/test_plot_csv_precision.py",
    "tests/test_config.py", "tests/test_desktop.py", "docs/requirements-explanation.md",
    "src/qplot/assets/qplot.svg",
])
def test_other_changes_keep_routine_validation(path):
    assert not plan.sensitive_path(path)


@pytest.mark.parametrize("change", ["delete", "rename_out", "rename_in", "edit"])
def test_git_comparison_keeps_deleted_and_renamed_sensitive_paths(git_repository, change):
    repository, git = git_repository
    before = git("rev-parse", "HEAD")
    if change == "delete":
        (repository / "pyproject.toml").unlink()
    elif change == "rename_out":
        (repository / "pyproject.toml").rename(repository / "ordinary.md")
    elif change == "rename_in":
        (repository / "README.md").rename(repository / "requirements.txt")
    else:
        (repository / "pyproject.toml").write_text("# Changed dependency\n")
    git("add", "--all")
    git("commit", "-m", "Sensitive change")
    assert pushed_mode(repository, before, git("rev-parse", "HEAD"))


def test_push_compares_entire_commit_range(git_repository):
    repository, git = git_repository
    before = git("rev-parse", "HEAD")
    (repository / "pyproject.toml").write_text("# Actual dependency change\n")
    git("add", ".")
    git("commit", "-m", "Dependency")
    (repository / "README.md").write_text("Changed documentation\n")
    git("add", ".")
    git("commit", "-m", "Documentation")
    assert pushed_mode(repository, before, git("rev-parse", "HEAD"))


@pytest.mark.skipif(os.name == "nt", reason="Windows filenames cannot contain newlines")
def test_newline_filename_is_not_split_into_sensitive_paths(git_repository):
    repository, git = git_repository
    before = git("rev-parse", "HEAD")
    # A line-based parser could invent a pyproject.toml change from this name.
    (repository / "notes\npyproject.toml").write_text("More documentation\n")
    git("add", ".")
    git("commit", "-m", "Unusual filename")
    assert not pushed_mode(repository, before, git("rev-parse", "HEAD"))


def test_pull_request_compares_from_merge_base_not_changed_target_branch(git_repository):
    repository, git = git_repository
    original = git("rev-parse", "HEAD")
    (repository / "README.md").write_text("Feature documentation\n")
    git("add", ".")
    git("commit", "-m", "Feature")
    head = git("rev-parse", "HEAD")
    git("checkout", "--detach", original)
    (repository / "pyproject.toml").write_text("# Already accepted base change\n")
    git("add", ".")
    git("commit", "-m", "Target branch advanced")
    base = git("rev-parse", "HEAD")
    event = {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}
    assert not plan.validation_mode(
        "pull_request", event, "refs/pull/1/merge", "false", repository=repository,
    )[0]


def test_missing_git_revision_selects_full(git_repository):
    repository, git = git_repository
    assert pushed_mode(repository, "f" * 40, git("rev-parse", "HEAD"))


@pytest.mark.parametrize("full", [False, True])
def test_check_matrix_keeps_all_desktop_partitions_and_python_versions(full):
    entries = plan.check_matrix(full)["include"]
    actual = {(row["os"], row["python-version"], row["suite"], row.get("partition")) for row in entries}
    expected = {
        (runner, version, "full", partition)
        for runner in ("windows-2025", "macos-latest")
        for version in ("3.12", "3.14")
        for partition in (("1/4", "2/4", "3/4", "4/4") if runner == "windows-2025" else ("1/2", "2/2"))
    } | {(runner, "3.13", "compatibility", None) for runner in ("windows-2025", "macos-latest")}
    compatibility = ("macos-15-intel", "ubuntu-latest") if full else ("macos-15-intel",)
    expected |= {(runner, version, "compatibility", None)
                 for runner in compatibility for version in ("3.12", "3.13", "3.14")}
    assert actual == expected
    assert len(entries) == len(expected) == (20 if full else 17)
    artifacts = {
        "windows-2025": "qplotter-native-windows-x64",
        "macos-latest": "qplotter-native-macos-arm64",
        "macos-15-intel": "qplotter-native-macos-intel",
        "ubuntu-latest": "qplotter-native-linux-x86_64",
    }
    assert all(row["native-artifact"] == artifacts[row["os"]] for row in entries)


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


@pytest.mark.parametrize("payload", ["invalid JSON", "[]", None])
def test_cli_missing_or_invalid_event_emits_safe_complete_outputs(tmp_path, payload):
    event = tmp_path / "event.json"
    if payload is not None:
        event.write_text(payload)
    output = tmp_path / "outputs"
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, check=True, capture_output=True, text=True,
        env={**os.environ, "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event),
             "GITHUB_REF": "refs/heads/main", "QPLOT_FULL_VALIDATION": "false",
             "GITHUB_OUTPUT": str(output)},
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(outputs) == {"full-validation", "checks", "installations", "reason"}
    assert outputs["full-validation"] == "true"
    assert len(json.loads(outputs["checks"])["include"]) == 20
    assert len(json.loads(outputs["installations"])["include"]) == 12
    assert "CI mode: full." in result.stdout


def test_cli_emits_routine_outputs_only_for_successful_safe_comparison(git_repository, tmp_path):
    repository, git = git_repository
    before = git("rev-parse", "HEAD")
    (repository / "README.md").write_text("Changed documentation\n")
    git("add", ".")
    git("commit", "-m", "Documentation")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": before, "after": git("rev-parse", "HEAD")}))
    output = tmp_path / "outputs"
    subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=repository, check=True, capture_output=True, text=True,
        env={**os.environ, "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event),
             "GITHUB_REF": "refs/heads/main", "QPLOT_FULL_VALIDATION": "false",
             "GITHUB_OUTPUT": str(output)},
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert outputs["full-validation"] == "false"
    assert len(json.loads(outputs["checks"])["include"]) == 17
    assert len(json.loads(outputs["installations"])["include"]) == 9
