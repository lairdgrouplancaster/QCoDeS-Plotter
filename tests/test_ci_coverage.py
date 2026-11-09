"""Combine real partition data and require complete CI coverage inputs."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import coverage
import pytest

pytestmark = pytest.mark.pure


def _job(workflow: str, name: str) -> str:
    body = workflow.split(f"  {name}:\n", 1)[1]
    return re.split(r"\n  [a-z-]+:\n", body, maxsplit=1)[0]


def _step(job: str, name: str) -> str:
    body = job.split(f"      - name: {name}\n", 1)[1]
    return body.split("\n      - ", 1)[0]


@pytest.fixture
def workflow():
    repository = Path(__file__).resolve().parents[1]
    path = repository / ".github/workflows/ci.yml"
    if not path.is_file():
        pytest.skip("Hosted workflow is not included in sdists")
    return path.read_text(), repository


@pytest.fixture
def coverage_report_run(workflow, tmp_path):
    text, repository = workflow
    bash = shutil.which("bash")
    if sys.platform == "win32" or bash is None:
        pytest.skip("The coverage reporting job runs in Bash on Ubuntu")
    config = tmp_path / ".github/coverage-combine.toml"
    config.parent.mkdir()
    shutil.copyfile(repository / ".github/coverage-combine.toml", config)
    shutil.copyfile(repository / "pyproject.toml", tmp_path / "pyproject.toml")
    source = tmp_path / "src/qplot/branching.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        'def choose(flag):\n    if flag:\n        return "left"\n    return "right"\n',
        encoding="utf-8",
    )
    arcs = (
        {(-1, 1), (1, -1), (-1, 2), (2, 3), (3, -1)},
        {(-1, 1), (1, -1), (-1, 2), (2, 4), (4, -1)},
    )
    for partition, values in enumerate(arcs, start=1):
        installed = (
            tmp_path / f"runner-{partition}" / "lib/python3.14/site-packages/qplot/branching.py"
        )
        data = coverage.CoverageData(basename=str(tmp_path / f".coverage.{partition}"))
        data.add_arcs({str(installed): values})
        data.write()

    step = _step(_job(text, "coverage-report"), "Combine both complete partitions")
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    script = script.replace("python -m coverage", f"{shlex.quote(sys.executable)} -m coverage")
    environment = os.environ.copy()
    for key in ("COVERAGE_FILE", "COVERAGE_RCFILE", "COVERAGE_PROCESS_START"):
        environment.pop(key, None)

    def run():
        # Match GitHub's Bash error handling: a missing input must stop before
        # reporting, even if another valid coverage partition is present.
        return subprocess.run(
            [bash, "-e", "-o", "pipefail", "-c", script],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )

    return run, source, set.union(*arcs)


def test_coverage_merge_remaps_installed_paths_and_unions_branches(coverage_report_run, tmp_path):
    run, source, expected_arcs = coverage_report_run
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr

    combined = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    combined.read()
    assert combined.measured_files() == {str(source.resolve())}
    assert set(combined.arcs(str(source.resolve()))) == expected_arcs
    report = ET.parse(tmp_path / "coverage.xml").getroot()
    assert report.get("branches-valid") == "2"
    assert report.get("branches-covered") == "2"
    assert report.get("branch-rate") == "1"
    assert report.get("line-rate") == "1"
    # Report commands in recent coverage releases combine leftover shards
    # automatically. Consume these installed-path shards during the explicit
    # mapped combine so the later reports cannot reintroduce those paths.
    assert not (tmp_path / ".coverage.1").exists()
    assert not (tmp_path / ".coverage.2").exists()


@pytest.mark.parametrize("partition", [1, 2])
@pytest.mark.parametrize("fault", ["missing", "empty"])
def test_coverage_report_rejects_incomplete_partition_inputs(
    coverage_report_run, tmp_path, partition, fault,
):
    run, _, _ = coverage_report_run
    incomplete = tmp_path / f".coverage.{partition}"
    if fault == "missing":
        incomplete.unlink()
    else:
        incomplete.write_bytes(b"")
    result = run()
    assert result.returncode != 0
    assert not (tmp_path / ".coverage").exists()
    assert not (tmp_path / "coverage.xml").exists()
