"""CI must reuse current-revision wheels throughout platform validation."""

import re
import runpy
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
        dependencies = ('application-artifacts, native-wheels' if name == 'static-analysis'
                        else 'ci-plan, application-artifacts, native-wheels')
        assert f'needs: [{dependencies}]' in job
        assert 'name: qplotter-application' in job
        assert 'Download matching native artifacts from this revision' in job
        # Never select another run/branch's artifacts.
        assert 'run-id:' not in job and 'github-token:' not in job


def test_ruff_runs_before_artifact_builds_and_remains_required(workflow):
    text, _ = workflow
    lint = text.split('  lint:\n', 1)[1].split('\n  application-artifacts:', 1)[0]
    assert 'needs:' not in lint
    assert 'python -m ruff check .' in lint
    assert 'download-artifact' not in lint
    required = text.split('  required-checks:\n', 1)[1]
    assert 'needs: [lint,' in required
    assert 'needs.lint.result }}" != "success"' in required


def test_all_source_suites_publish_portable_timing_reports(workflow):
    text, _ = workflow
    checks = text.split('  checks:\n', 1)[1].split('\n  coverage:', 1)[0]
    for step in ('Run partitioned full test suite', 'Run full test suite as a standard Windows user',
                 'Run compatibility test suite', 'Run compatibility test suite as a standard Windows user'):
        command = checks.split(f'      - name: {step}\n', 1)[1].split('      - name:', 1)[0]
        assert 'junit_family=xunit1' in command and '--junitxml=' in command
    assert 'path: ${{ env.QPLOT_CI_TEST_REPORT }}' in checks
    assert 'QPLOT_CI_TEST_REPORT=$(Join-Path $standardTemp' in checks


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


@pytest.mark.parametrize('full_validation', [False, True], ids=['routine', 'full'])
def test_every_advertised_python_runs_installed_smoke_on_selected_platforms(
    workflow, full_validation,
):
    text, repository = workflow
    project = tomllib.loads((repository / 'pyproject.toml').read_text())['project']
    supported = {
        classifier.rsplit(' :: ', 1)[1]
        for classifier in project['classifiers']
        if re.fullmatch(r'Programming Language :: Python :: 3\.\d+', classifier)
    }
    native_project = tomllib.loads((repository / 'native/pyproject.toml').read_text())['project']
    assert project['requires-python'] == '>=3.12'
    # The unchanged native release still uses the CPython 3.11 stable ABI.
    assert native_project['requires-python'] == '>=3.11'
    assert supported == {'3.12', '3.13', '3.14'}
    wheels = text.split('  platform-wheels:\n', 1)[1].split('  required-checks:\n', 1)[0]
    assert 'matrix: ${{ fromJSON(needs.ci-plan.outputs.installations) }}' in wheels
    planner = runpy.run_path(str(repository / '.github/scripts/ci_plan.py'))
    entries = planner['installation_matrix'](full_validation)['include']
    platforms = {'macos-arm64', 'macos-intel', 'windows-x64'}
    if full_validation:
        platforms.add('linux-x86_64')
    expected = {(platform, version) for platform in platforms for version in supported}
    assert len(entries) == len(expected)
    assert {(entry['platform'], entry['python-version']) for entry in entries} == expected
    for entry in entries:
        assert entry['native-artifact'] == f"qplotter-native-{entry['platform']}"
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


@pytest.mark.parametrize('full_validation', [False, True], ids=['routine', 'full'])
def test_application_matrices_drop_311_but_keep_native_abi_build(workflow, full_validation):
    text, repository = workflow
    checks = text.split('  checks:\n', 1)[1].split('    env:\n', 1)[0]
    assert 'matrix: ${{ fromJSON(needs.ci-plan.outputs.checks) }}' in checks
    planner = runpy.run_path(str(repository / '.github/scripts/ci_plan.py'))
    entries = planner['check_matrix'](full_validation)['include']
    platforms = {'windows-2025', 'macos-latest', 'macos-15-intel'}
    if full_validation:
        platforms.add('ubuntu-latest')
    assert {(entry['os'], entry['python-version']) for entry in entries} == {
        (platform, version)
        for platform in platforms
        for version in ('3.12', '3.13', '3.14')
    }
    assert text.count('python-version: "3.11"') == 1  # native build only
    for filename in ('release.yml', 'revalidate-published.yml'):
        release = (repository / '.github/workflows' / filename).read_text()
        assert '"3.11"' not in release
        assert 'python-version: ["3.12", "3.13", "3.14"]' in release


def test_published_recheck_has_no_publishing_permissions_or_steps(workflow):
    _, repository = workflow
    text = (repository / '.github/workflows/revalidate-published.yml').read_text()
    assert 'workflow_dispatch:' in text
    assert 'contents: read' in text and 'actions: read' in text
    assert 'id-token:' not in text and 'pypi-publish' not in text
    assert 'environment:' not in text
    assert 'path: release-source' in text and 'ref: ${{ inputs.release-tag }}' in text
    assert 'run-id: ${{ inputs.release-run-id }}' in text
    assert 'run-unprivileged-windows.ps1' in text
