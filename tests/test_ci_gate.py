"""Exercise the real required-check shell gate with completed job results."""

import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.pure

CORE_JOBS = (
    "ci-plan", "lint", "application-artifacts", "native-linux", "native-windows",
    "native-macos-arm64", "native-macos-intel", "static-analysis",
    "platform-windows", "platform-macos-arm64", "platform-macos-intel",
)
FULL_JOBS = ("platform-linux", "coverage", "coverage-report", "package")


@pytest.fixture
def gate():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
    if not path.is_file():
        pytest.skip("Hosted workflow is not included in sdists")
    bash = shutil.which("bash")
    if sys.platform == "win32" or bash is None:
        pytest.skip("The required-check gate runs in Bash on Ubuntu")
    job = path.read_text().split("  required-checks:\n", 1)[1]
    assert "if: always()" in job
    script = textwrap.dedent(job.split("        run: |\n", 1)[1])

    def run(scope, overrides=None):
        results = {f"needs.{name}.result": "success" for name in CORE_JOBS}
        results.update({
            "needs.ci-plan.outputs.full-validation": scope,
            **{f"needs.{name}.result": "success" if scope == "true" else "skipped"
               for name in FULL_JOBS},
        })
        results.update(overrides or {})
        expanded = re.sub(
            r"\$\{\{\s*(.*?)\s*\}\}", lambda match: results[match[1]], script,
        )
        return subprocess.run(
            [bash, "-c", expanded], capture_output=True, text=True, timeout=10,
        )

    return run


@pytest.mark.parametrize("scope", ["true", "false"])
def test_gate_accepts_the_complete_selected_scope(gate, scope):
    result = gate(scope)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("scope", ["true", "false"])
@pytest.mark.parametrize("job", CORE_JOBS)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_core_checks_cannot_be_skipped_or_fail_in_either_scope(gate, scope, job, result):
    assert gate(scope, {f"needs.{job}.result": result}).returncode != 0


@pytest.mark.parametrize("scope", ["true", "false"])
@pytest.mark.parametrize("job", FULL_JOBS)
@pytest.mark.parametrize("result", ["failure", "cancelled", ""])
def test_optional_checks_never_hide_a_failure(gate, scope, job, result):
    assert gate(scope, {f"needs.{job}.result": result}).returncode != 0


@pytest.mark.parametrize("job", FULL_JOBS)
def test_full_validation_requires_both_linux_full_suites(gate, job):
    assert gate("true", {f"needs.{job}.result": "skipped"}).returncode != 0


@pytest.mark.parametrize("scope", ["", "unknown", "TRUE"])
def test_missing_or_invalid_scope_cannot_pass(gate, scope):
    assert gate(scope).returncode != 0
