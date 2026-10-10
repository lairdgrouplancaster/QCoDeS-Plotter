"""CI must reuse current-revision wheels throughout platform validation."""

import re
import tomllib
from pathlib import Path

import pytest


@pytest.fixture
def workflow():
    repository = Path(__file__).resolve().parents[1]
    path = repository / '.github/workflows/ci.yml'
    if not path.is_file():
        pytest.skip('Hosted workflow is not included in sdists')
    return path.read_text(), repository


def test_application_wheel_is_built_once_and_consumed_by_all_jobs(workflow):
    text, _ = workflow
    builds = re.findall(r'^\s+run: python -m build --outdir dist$', text, re.M)
    assert len(builds) == 1
    assert 'python -m build --wheel' not in text
    assert 'pip install ./native' not in text
    assert ' -e .' not in text
    for name in ('static-analysis', 'checks', 'coverage', 'package', 'platform-wheels'):
        job = text.split(f'  {name}:\n', 1)[1].split('\n  required-checks:', 1)[0]
        job = re.split(r'\n  [a-z-]+:\n', job, maxsplit=1)[0]
        assert 'needs: [application-artifacts, native-wheels]' in job
        assert 'name: qplot-application' in job
        assert 'Download matching native artifacts from this revision' in job
        # Never select another run/branch's artifacts.
        assert 'run-id:' not in job and 'github-token:' not in job


def test_publication_native_builds_are_arch_specific_and_manylinux_repaired(workflow):
    text, _ = workflow
    native = text.split('  native-wheels:\n', 1)[1].split('  static-analysis:\n', 1)[0]
    for identifier in ('cp311-win_amd64', 'cp311-macosx_arm64',
                       'cp311-macosx_x86_64', 'cp311-manylinux_x86_64'):
        assert f'build: {identifier}' in native
    assert 'CIBW_MANYLINUX_X86_64_IMAGE: manylinux_2_28' in native
    assert 'python -m cibuildwheel native --output-dir dist' in native
    assert 'python -m build native --wheel' not in native
    assert 'macos-15-intel' in native


def test_every_advertised_python_runs_installed_smoke_on_every_platform(workflow):
    text, repository = workflow
    project = tomllib.loads((repository / 'pyproject.toml').read_text())['project']
    supported = {
        classifier.rsplit(' :: ', 1)[1]
        for classifier in project['classifiers']
        if re.fullmatch(r'Programming Language :: Python :: 3\.\d+', classifier)
    }
    native_project = tomllib.loads((repository / 'native/pyproject.toml').read_text())['project']
    assert project['requires-python'] == native_project['requires-python'] == '>=3.11'
    wheels = text.split('  platform-wheels:\n', 1)[1].split('  required-checks:\n', 1)[0]
    versions = re.search(r'python-version: \[([^\n]+)\]', wheels)
    assert versions is not None
    assert set(re.findall(r'"([^"]+)"', versions[1])) == supported
    assert 'platform: [linux-x86_64, macos-arm64, macos-intel, windows-x64]' in wheels
    assert 'python-version: ${{ matrix.python-version }}' in wheels
    assert 'run-unprivileged-windows.ps1' in wheels
    for mode in ('wheel', 'editable'):
        assert f'"scripts/validate_compiler_free_install.py", "--mode", "{mode}", "dist"' in wheels
        assert f'python scripts/validate_compiler_free_install.py --mode {mode} dist' in wheels
    assert 'cache: pip' not in wheels
    assert 'PIP_NO_CACHE_DIR: "1"' in wheels
    assert 'UV_NO_CACHE: "1"' in wheels


def test_pytest_cannot_shadow_the_installed_pair(workflow):
    text, _ = workflow
    for job_name, next_job in (('checks', 'coverage'), ('coverage', 'package')):
        job = text.split(f'  {job_name}:\n', 1)[1].split(f'  {next_job}:\n', 1)[0]
        assert 'QPLOT_CI_WHEEL_AUDIT:' in job
        assert '--audit-path dist/wheel-installation-audit.py' in job
        commands = re.findall(r'python -m pytest[^\n]+', job)
        assert commands and all('-o pythonpath=' in command for command in commands)
    windows = text.split('Run full test suite as a standard Windows user\n', 1)[1]
    assert '"-m", "pytest", "-o", "pythonpath="' in windows
