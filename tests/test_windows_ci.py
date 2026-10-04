"""Regression checks for the checkout-only Windows CI launcher."""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def github_directory() -> Path:
    directory = Path(__file__).resolve().parents[1] / ".github"
    if not directory.is_dir():
        pytest.skip("CI launchers are not shipped in the source distribution")
    return directory


def test_full_windows_suite_has_its_own_bounded_time_budget(
    github_directory: Path,
) -> None:
    workflow = (github_directory / "workflows/ci.yml").read_text(encoding="utf-8")
    full_suite = workflow.split(
        "      - name: Run full test suite as a standard Windows user\n", 1
    )[1].split("      - name:", 1)[0]
    budget = re.search(r"-TimeoutSeconds (\d+)", full_suite)
    assert budget is not None, "The full suite must not use the 420-second default"
    assert 600 <= int(budget[1]) <= 1500
    checks_job = workflow.split("  checks:\n", 1)[1]
    job_minutes = re.search(r"timeout-minutes: (\d+)", checks_job)
    assert job_minutes is not None
    # Reserve two minutes for bounded cleanup and three for setup/diagnostics.
    assert int(budget[1]) + 120 + 180 <= int(job_minutes[1]) * 60
    # More time for the complete suite must not weaken individual-test bounds
    # or increase disk/thread contention by adding workers.
    assert '"--timeout=90"' in full_suite
    assert '"--timeout-method=thread"' in full_suite
    assert '"-n", "2", "--dist=loadfile"' in full_suite


def test_windows_full_matrix_runs_both_partitions_with_existing_containment(
    github_directory: Path,
) -> None:
    workflow = (github_directory / "workflows/ci.yml").read_text(encoding="utf-8")
    checks = workflow.split("  checks:\n", 1)[1].split("    env:\n", 1)[0]
    entries = re.findall(
        r'- os: ([^\n]+)\n\s+python-version: "([^"]+)"\n\s+suite: (\w+)'
        r'(?:\n\s+partition: "([^"]+)")?', checks,
    )
    windows_full = [(version, partition) for os_name, version, suite, partition in entries
                    if os_name == "windows-2025" and suite == "full"]
    assert sorted(windows_full) == [("3.11", "1/2"), ("3.11", "2/2"),
                                    ("3.14", "1/2"), ("3.14", "2/2")]
    mac_full = [(version, partition) for os_name, version, suite, partition in entries
                if os_name == "macos-latest" and suite == "full"]
    assert sorted(mac_full) == [("3.11", "1/2"), ("3.11", "2/2"),
                                ("3.14", "")]
    assert all(not partition for os_name, _version, suite, partition in entries
               if suite != "full")
    full_suite = workflow.split(
        "      - name: Run full test suite as a standard Windows user\n", 1,
    )[1].split("      - name:", 1)[0]
    assert '"--qplot-ci-partition=${{ matrix.partition }}"' in full_suite
    mac_partitioned = workflow.split(
        "      - name: Run partitioned full test suite\n", 1,
    )[1].split("      - name:", 1)[0]
    assert "--qplot-ci-partition=${{ matrix.partition }}" in mac_partitioned
    assert "--timeout=90 --timeout-method=thread" in mac_partitioned
    assert "-n 2 --dist=loadfile" in mac_partitioned
    assert "-TimeoutSeconds 1500" in full_suite
    assert "timeout-minutes: 30" in checks
    assert "invoke-bounded-unprivileged-windows.ps1" in full_suite
    # Every matrix entry remains required, and Linux coverage still runs all
    # tests with its ordinary instrumentation and no partition option.
    required = workflow.split("  required-checks:\n", 1)[1]
    assert 'needs.checks.result }}" != "success"' in required
    coverage = workflow.split("  coverage:\n", 1)[1].split("  package:\n", 1)[0]
    assert "python -m pytest -n 2 --dist=loadfile --no-loadscope-reorder" in coverage
    assert "--no-cov" not in coverage
    assert "--qplot-ci-partition" not in coverage


@pytest.mark.parametrize("suite", ["full", "compatibility"])
@pytest.mark.parametrize("python_version", ["3.12.10", "3.13.15"])
def test_windows_source_suite_fits_standard_user_command_line(
    github_directory: Path, suite: str, python_version: str
) -> None:
    workflow = (github_directory / "workflows/ci.yml").read_text(encoding="utf-8")
    step = workflow.split(
        f"      - name: Run {suite} test suite as a standard Windows user\n", 1
    )[1].split("      - name:", 1)[0]
    argument_array = re.search(r"\$arguments = @\((.*?)\n          \)", step, re.S)
    assert argument_array is not None
    arguments = re.findall(r'"([^"\n]*)"', argument_array[1])
    assert arguments
    # Require a literal array so future dynamic arguments cannot silently
    # evade this length check.
    assert not re.sub(r'"[^"\n]*"', "", argument_array[1]).strip(" ,\r\n\t")
    executable = (
        rf"C:\hostedtoolcache\windows\Python\{python_version}\x64\python.exe"
    )
    # .NET quotes the executable before appending its escaped argument list.
    # Credentialed Process.Start uses CreateProcessWithLogonW, whose command
    # line is limited to 1024 characters. Reserve the terminating NUL too.
    command_line = f'"{executable}" {subprocess.list2cmdline(arguments)}\0'
    assert len(command_line) <= 1024, (
        f"The {suite} suite needs {len(command_line)} command-line characters; "
        "the standard-user Windows launcher permits only 1024."
    )


@pytest.mark.parametrize(
    "message",
    [
        "The unprivileged qPlot CI process exceeded its 420-second direct-process deadline.",
        "The unprivileged qPlot CI process tree did not terminate within its deadline.",
        "Synthetic account setup failure",
    ],
)
def test_wrapper_persists_primary_error_before_cleanup(
    github_directory: Path, tmp_path: Path, message: str
) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required to exercise redirected CI diagnostics")
    source = (github_directory / "scripts/run-unprivileged-windows.ps1").read_text(
        encoding="utf-8"
    )
    marker = "\n} catch {\n    $primaryError = $_\n"
    catch_body = "    $primaryError = $_\n" + source.split(marker, 1)[1].split(
        "\n} finally {\n", 1
    )[0]
    log_path = tmp_path / "wrapper-phase.txt"
    log_literal = "'" + str(log_path).replace("'", "''") + "'"
    # Exercise the real catch body with the same redirected call and final
    # terminating Write-Error. Never run account creation, ACL edits or jobs.
    code = f"""
$ErrorActionPreference = 'Stop'
function Invoke-TestWrapper {{
    $primaryError = $null
    try {{
        throw '{message}'
    }} catch {{
{catch_body}
    }} finally {{
        Write-Host 'regression-cleanup'
    }}
    Write-Error $primaryError
}}
Invoke-TestWrapper *> {log_literal}
"""
    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(code.encode("utf-16-le")).decode("ascii"),
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode != 0
    raw_log = log_path.read_bytes()
    # Windows PowerShell 5 writes UTF-16; PowerShell 7 writes UTF-8.
    log = raw_log.decode("utf-16" if raw_log.startswith(b"\xff\xfe") else "utf-8-sig")
    assert message in log
    assert log.index(message) < log.index("regression-cleanup")
