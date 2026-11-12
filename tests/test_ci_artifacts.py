"""CI must reuse current-revision wheels throughout platform validation."""

import re
import runpy
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.pure


@pytest.fixture
def workflow():
    repository = Path(__file__).resolve().parents[1]
    path = repository / '.github/workflows/ci.yml'
    if not path.is_file():
        pytest.skip('Hosted workflow is not included in sdists')
    return {
        name: (path.parent / filename).read_text()
        for name, filename in (
            ('ci', 'ci.yml'), ('native', 'ci-native.yml'), ('platform', 'ci-platform.yml'),
        )
    }, repository


def job(text, name):
    body = text.split(f'\n  {name}:\n', 1)[1]
    return re.split(r'\n  [a-z-]+:\n', body, maxsplit=1)[0]


def test_application_wheel_is_built_once_and_consumed_by_all_jobs(workflow):
    workflows, _ = workflow
    text = '\n'.join(workflows.values())
    builds = re.findall(r'^\s+run: python -m build --outdir dist$', text, re.M)
    assert len(builds) == 1
    assert 'python -m build --wheel' not in text
    assert 'pip install ./native' not in text
    assert ' -e .' not in text
    for workflow_name, name in (
        ('ci', 'static-analysis'), ('ci', 'coverage'), ('ci', 'package'),
        ('platform', 'checks'), ('platform', 'platform-wheels'),
    ):
        consumer = job(workflows[workflow_name], name)
        assert 'name: qplotter-application' in consumer
        assert 'Download matching native artifacts from this revision' in consumer
        # Never select another run/branch's artifacts.
        assert 'run-id:' not in consumer and 'github-token:' not in consumer


@pytest.mark.parametrize('name,platform', [
    ('linux', 'linux-x86_64'), ('windows', 'windows-x64'),
    ('macos-arm64', 'macos-arm64'), ('macos-intel', 'macos-intel'),
])
def test_each_platform_waits_only_for_its_own_native_build(workflow, name, platform):
    workflows, _ = workflow
    native = job(workflows['ci'], f'native-{name}')
    assert 'uses: ./.github/workflows/ci-native.yml' in native
    assert 'needs:' not in native
    assert f'artifact-name: qplotter-native-{platform}' in native
    assert 'reuse-native: ${{ inputs.reuse-native || false }}' in native
    consumer = job(workflows['ci'], f'platform-{name}')
    dependencies = re.search(r'needs: \[([^]]+)\]', consumer)
    assert dependencies is not None
    assert set(part.strip() for part in dependencies[1].split(',')) == {
        'ci-plan', 'application-artifacts', f'native-{name}',
    }
    assert 'uses: ./.github/workflows/ci-platform.yml' in consumer
    for matrix in ('checks', 'installations'):
        expression = re.search(rf'^      {matrix}: (.+)$', consumer, re.M)
        assert expression is not None
        assert 'needs.ci-plan.outputs.platforms' in expression[1]
        assert platform in expression[1] and f'.{matrix}' in expression[1]
    if name == 'linux':
        assert "if: needs.ci-plan.outputs.full-validation == 'true'" in consumer
    else:
        assert '\n    if:' not in consumer


def test_linux_only_jobs_do_not_wait_for_desktop_builds(workflow):
    workflows, _ = workflow
    for name in ('static-analysis', 'coverage', 'package'):
        consumer = job(workflows['ci'], name)
        dependencies = re.search(r'needs: \[([^]]+)\]', consumer)
        assert dependencies is not None
        expected = {'application-artifacts', 'native-linux'}
        if name != 'static-analysis':
            expected.add('ci-plan')
        assert set(part.strip() for part in dependencies[1].split(',')) == expected


def test_reusable_workflows_keep_binary_dependency_policy_and_do_not_cancel_parent(workflow):
    workflows, _ = workflow
    for name in ('native', 'platform'):
        assert 'workflow_call:' in workflows[name]
        assert 'PIP_ONLY_BINARY: apsw' in workflows[name]
        assert 'contents: read' in workflows[name]
        assert 'concurrency:' not in workflows[name]


def test_coverage_partitions_are_required_and_their_hidden_data_is_uploaded(workflow):
    workflows, _ = workflow
    coverage = job(workflows['ci'], 'coverage')
    assert 'partition: [1, 2]' in coverage
    assert '--qplot-ci-partition=${{ matrix.partition }}/2' in coverage
    assert 'fail-fast: false' in coverage
    assert 'COVERAGE_FILE: ${{ github.workspace }}/.coverage.${{ matrix.partition }}' in coverage
    assert 'include-hidden-files: true' in coverage
    assert 'if-no-files-found: error' in coverage
    report = job(workflows['ci'], 'coverage-report')
    assert 'needs: [ci-plan, coverage]' in report
    for partition in (1, 2):
        assert f'name: qplot-coverage-{partition}' in report
        assert f'test -s .coverage.{partition}' in report
    assert 'coverage combine --rcfile=.github/coverage-combine.toml .coverage.1 .coverage.2' in report


def test_ruff_runs_before_artifact_builds_and_remains_required(workflow):
    workflows, _ = workflow
    text = workflows['ci']
    lint = text.split('  lint:\n', 1)[1].split('\n  application-artifacts:', 1)[0]
    assert 'needs:' not in lint
    assert 'python -m ruff check .' in lint
    assert 'download-artifact' not in lint
    required = text.split('  required-checks:\n', 1)[1]
    assert 'needs: [lint,' in required
    assert 'needs.lint.result }}" != "success"' in required


def test_all_source_suites_publish_portable_timing_reports(workflow):
    workflows, _ = workflow
    checks = job(workflows['platform'], 'checks')
    for step in ('Run partitioned full test suite', 'Run full test suite as a standard Windows user',
                 'Run compatibility test suite', 'Run compatibility test suite as a standard Windows user'):
        command = checks.split(f'      - name: {step}\n', 1)[1].split('      - name:', 1)[0]
        assert 'junit_family=xunit1' in command and '--junitxml=' in command
        assert '--no-loadscope-reorder' in command
        if 'compatibility' in step:
            assert '@${{ matrix.test-list }}' in command
    assert 'path: ${{ env.QPLOT_CI_TEST_REPORT }}' in checks
    assert 'QPLOT_CI_TEST_REPORT=$(Join-Path $standardTemp' in checks


def test_source_test_cache_matches_the_wheel_dependency_installer(workflow):
    workflows, repository = workflow
    checks = job(workflows['platform'], 'checks')
    assert 'cache: pip' in checks
    assert 'cache-dependency-path: pyproject.toml' in checks
    assert 'setup-uv' not in checks and 'uv pip install' not in checks
    installer = (repository / 'scripts/validate_distribution.py').read_text()
    install_wheels = installer.split('def install_wheels(', 1)[1].split('\ndef ', 1)[0]
    assert "'-m', 'pip', 'install'" in install_wheels
    assert "'--no-index', '--no-deps'" in install_wheels
    assert '--audit-path dist/wheel-installation-audit.py' in checks


def test_publication_native_builds_are_arch_specific_and_manylinux_repaired(workflow):
    workflows, _ = workflow
    text = workflows['ci']
    native = workflows['native']
    for identifier in ('cp311-win_amd64', 'cp311-macosx_arm64',
                       'cp311-macosx_x86_64', 'cp311-manylinux_x86_64'):
        assert f'build: {identifier}' in text
    assert 'CIBW_MANYLINUX_X86_64_IMAGE: manylinux_2_28' in native
    assert 'python -m cibuildwheel native --output-dir dist' in native
    assert 'python -m build native --wheel' not in native
    assert 'macos-15-intel' in text


@pytest.mark.parametrize('full_validation', [False, True], ids=['routine', 'full'])
def test_every_advertised_python_runs_installed_smoke_on_selected_platforms(
    workflow, full_validation,
):
    workflows, repository = workflow
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
    wheels = job(workflows['platform'], 'platform-wheels')
    assert 'matrix: ${{ fromJSON(inputs.installations) }}' in wheels
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
    workflows, _ = workflow
    for workflow_name, job_name in (('platform', 'checks'), ('ci', 'coverage')):
        consumer = job(workflows[workflow_name], job_name)
        assert 'QPLOT_CI_WHEEL_AUDIT:' in consumer
        assert '--audit-path dist/wheel-installation-audit.py' in consumer
        commands = re.findall(r'python -m pytest[^\n]+', consumer)
        assert commands and all('-o pythonpath=' in command for command in commands)
    windows = workflows['platform'].split('Run full test suite as a standard Windows user\n', 1)[1]
    assert '"-m", "pytest", "-o", "pythonpath="' in windows


@pytest.mark.parametrize('full_validation', [False, True], ids=['routine', 'full'])
def test_application_matrices_drop_311_but_keep_native_abi_build(workflow, full_validation):
    workflows, repository = workflow
    checks = job(workflows['platform'], 'checks')
    assert 'matrix: ${{ fromJSON(inputs.checks) }}' in checks
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
    assert '\n'.join(workflows.values()).count('python-version: "3.11"') == 1
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
