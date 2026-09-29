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
    assert 600 <= int(budget[1]) <= 900
    # More time for the complete suite must not weaken individual-test bounds
    # or increase disk/thread contention by adding workers.
    assert '"--timeout=90"' in full_suite
    assert '"--timeout-method=thread"' in full_suite
    assert '"-n", "2", "--dist=loadfile"' in full_suite


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
