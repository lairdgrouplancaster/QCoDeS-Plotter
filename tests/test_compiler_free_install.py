"""Acceptance checks must prove binary dependencies and compiler rejection."""

import ast
import hashlib
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from scripts import validate_compiler_free_install as acceptance
from scripts import validate_distribution as validator


@pytest.mark.parametrize('editable', [False, True])
def test_install_resolves_every_dependency_as_binary_without_cache(tmp_path, editable):
    command = acceptance.installation_command(
        tmp_path / 'python', tmp_path / 'app', tmp_path / 'native.whl',
        tmp_path / 'report.json', editable=editable,
    )
    assert '--only-binary=:all:' in command
    assert '--no-cache-dir' in command
    assert '--no-deps' not in command
    assert str((tmp_path / 'native.whl').resolve()) in command
    if editable:
        assert command[-2:] == ['--editable', str((tmp_path / 'app').resolve()) + '[dev]']
    else:
        assert command[-1] == str((tmp_path / 'app').resolve())


@pytest.mark.parametrize('editable', [False, True])
def test_public_install_uses_exact_index_versions_and_no_local_native(tmp_path, editable):
    command = acceptance.installation_command(
        tmp_path / 'python', tmp_path / 'fresh-checkout', tmp_path / 'native.whl',
        tmp_path / 'report.json', editable=editable, public_version='1.6.0b3',
    )
    assert '--only-binary=:all:' in command and '--no-cache-dir' in command
    assert '--no-deps' not in command
    assert command[command.index('--index-url') + 1] == 'https://pypi.org/simple'
    assert str((tmp_path / 'native.whl').resolve()) not in command
    if editable:
        assert 'qplotter-native==1.0.0' in command
        assert command[-2:] == ['--editable', str((tmp_path / 'fresh-checkout').resolve()) + '[dev]']
    else:
        assert command[-1] == 'qplotter==1.6.0b3'


@pytest.mark.parametrize('editable', [False, True])
@pytest.mark.parametrize('fault', [None, 'old-native', 'wrong-bytes', 'private-index'])
def test_public_report_requires_validated_wheels_from_public_pypi(tmp_path, editable, fault):
    report = install_report(tmp_path, editable=editable)
    data = json.loads(report.read_text())
    artifacts = {}
    for name in ('qplotter', 'qplotter-native'):
        wheel = tmp_path / (name.replace('-', '_') + '-validated.whl')
        wheel.write_bytes(name.encode())
        artifacts[name.replace('-', '_') + '_wheel'] = wheel
    for item in data['install']:
        name = item['metadata']['name']
        if editable and name == 'qplotter':
            continue
        info = item['download_info']
        info['url'] = 'https://files.pythonhosted.org/' + name + '.whl'
        item['is_direct'] = False
        if name.startswith('qplotter'):
            wheel = artifacts[name.replace('-', '_') + '_wheel']
            info['url'] = 'https://files.pythonhosted.org/' + wheel.name
            info['archive_info'] = {'hashes': {'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}}
        if name == 'qplotter-native':
            if fault == 'old-native':
                info['url'] = 'https://files.pythonhosted.org/older-native.whl'
            elif fault == 'wrong-bytes':
                info['archive_info']['hashes']['sha256'] = '0' * 64
            elif fault == 'private-index':
                info['url'] = info['url'].replace('files.pythonhosted.org', 'private.example')
    report.write_text(json.dumps(data))
    if fault:
        with pytest.raises(AssertionError):
            acceptance.validate_install_report(report, editable=editable, public_artifacts=artifacts)
    else:
        acceptance.validate_install_report(report, editable=editable, public_artifacts=artifacts)


def install_report(tmp_path, *, editable, fault=None):
    names = ['qplotter', 'qplotter-native', 'apsw', 'numpy']
    if editable:
        names += ['pytest', 'ruff', 'mypy', 'build', 'twine']
    items = []
    for name in names:
        project = name.startswith('qplotter')
        info = {'url': f'{"file:///artifacts" if project else "https://pypi.example"}/{name}.whl'}
        if editable and name == 'qplotter':
            info = {'url': 'file:///editable-source', 'dir_info': {'editable': True}}
        if name == 'numpy' and fault == 'source-dependency':
            info['url'] = 'https://pypi.example/numpy.tar.gz'
        if name == 'qplotter-native' and fault == 'index-native':
            info['url'] = 'https://pypi.example/native.whl'
        items.append({'metadata': {'name': name}, 'download_info': info, 'is_direct': project})
    report = tmp_path / 'report.json'
    report.write_text(json.dumps({'install': items}))
    return report


@pytest.mark.parametrize('editable', [False, True])
@pytest.mark.parametrize('fault', [None, 'source-dependency', 'index-native'])
def test_report_rejects_source_builds_and_index_native(tmp_path, editable, fault):
    report = install_report(tmp_path, editable=editable, fault=fault)
    if fault:
        with pytest.raises(AssertionError):
            acceptance.validate_install_report(report, editable=editable)
    else:
        acceptance.validate_install_report(report, editable=editable)


def test_pip_report_unicode_is_decoded_as_utf8_under_a_non_utf8_locale(tmp_path):
    report = install_report(tmp_path, editable=False)
    data = json.loads(report.read_text())
    data['install'][0]['metadata']['description'] = 'Unicode dependency description: “wheel”'
    report.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    scripts = Path(acceptance.__file__).parent
    code = (f'import sys; sys.path.insert(0, {str(scripts)!r}); '
            'from pathlib import Path; '
            'from validate_compiler_free_install import validate_install_report; '
            f'validate_install_report(Path({str(report)!r}), editable=False)')
    env = dict(os.environ, LC_ALL='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0')
    subprocess.run([sys.executable, '-I', '-X', 'utf8=0', '-c', code],
                   env=env, check=True)


def test_compilers_fail_even_in_an_isolated_python_build_process(tmp_path):
    environment = tmp_path / 'venv'
    venv.EnvBuilder(with_pip=False).create(environment)
    python = validator.environment_python(environment)
    env, log = acceptance.compiler_free_environment(tmp_path)
    acceptance.install_compiler_audit_guard(python, env)
    baseline = acceptance.verify_compilers_disabled(python, env, log)
    assert env['PIP_NO_CACHE_DIR'] == env['UV_NO_CACHE'] == '1'
    assert env['PIP_ONLY_BINARY'] == ':all:'
    assert env['PIP_CONFIG_FILE'] == os.devnull
    assert not Path(env['PIP_CACHE_DIR']).exists()
    isolation = tmp_path / 'build-isolation'
    isolation.mkdir()
    (isolation / 'sitecustomize.py').write_text(
        'import sys\nsys.path[:] = [p for p in sys.path if "site-packages" not in p]\n'
    )
    code = '''
import subprocess
import sys
assert '_qplot_ci_compiler_guard' in sys.modules
try:
    subprocess.run(['/absolute/toolchain/cl.exe', '/c', 'extension.c'])
except RuntimeError as error:
    assert 'Compiler invocation forbidden' in str(error)
else:
    raise AssertionError('Compiler was accepted')
'''
    subprocess.run([str(python), '-c', code], env=dict(env, PYTHONPATH=str(isolation)), check=True)
    assert log.read_text() == baseline + 'audit blocked /absolute/toolchain/cl.exe\n'


def test_windows_compiler_filter_removes_runneradmin_private_paths(tmp_path, monkeypatch):
    private = tmp_path / 'runneradmin/.cargo/bin'
    compiler = tmp_path / 'toolchain'
    compiler.mkdir()
    (compiler / 'cl.exe').touch()
    usable = tmp_path / 'python'
    usable.mkdir()
    original = Path.is_file

    def accessible(path):
        if path.is_relative_to(private):
            raise PermissionError('standard user cannot inspect runneradmin')
        return original(path)

    monkeypatch.setattr(Path, 'is_file', accessible)
    assert acceptance.windows_paths_without_compilers(
        os.pathsep.join(map(str, (private, compiler, usable))),
    ) == [str(usable)]


@pytest.mark.skipif(os.name != 'nt', reason='Exercises the Windows command-line API')
def test_audit_rejects_windows_command_lines_with_backslashes_and_spaces(tmp_path):
    environment = tmp_path / 'venv'
    venv.EnvBuilder(with_pip=False).create(environment)
    python = validator.environment_python(environment)
    env, log = acceptance.compiler_free_environment(tmp_path)
    acceptance.install_compiler_audit_guard(python, env)
    # Never install the audit hook into pytest's process, where hooks cannot
    # be removed. Exercise the actual Windows API in a disposable interpreter.
    code = r'''
import _qplot_ci_compiler_guard as guard

for command in (r'C:\toolchain\cl.exe /c extension.c',
                r'"C:\Program Files\LLVM\bin\clang.exe" -c extension.c'):
    try:
        guard.audit('subprocess.Popen', (None, command, None, {}))
    except RuntimeError as error:
        assert 'Compiler invocation forbidden' in str(error)
    else:
        raise AssertionError('Windows absolute compiler path was accepted: ' + command)
try:
    guard.audit('subprocess.Popen', (None, 'rustc --version', None, {}))
except RuntimeError:
    pass
else:
    raise AssertionError('The optional Windows rustc probe did not fail')
'''
    subprocess.run([str(python), '-I', '-c', code], env=env, check=True)
    assert len(log.read_text().splitlines()) == 3
    assert log.read_text().endswith('audit blocked optional version probe rustc\n')


@pytest.mark.skipif(os.name != 'nt', reason='Exercises the Windows command-line API')
def test_windows_parser_roundtrips_quoted_inline_python_and_backslashes():
    arguments = [r'C:\Program Files\Python\python.exe', '-c',
                 'import sqlite3\nprint("writer", {"path": r"C:\\db\\data.db"})\n',
                 '', 'trailing-backslash\\']
    assert acceptance.split_command_line(subprocess.list2cmdline(arguments)) == arguments


def test_python_edit_changes_import_without_another_install(tmp_path):
    source = tmp_path / 'application'
    package = source / 'src/qplot'
    package.mkdir(parents=True)
    (package / '__init__.py').touch()
    (package / '_version.py').write_text('def package_version():\n    return "fixture"\n')
    environment = tmp_path / 'venv'
    venv.EnvBuilder(with_pip=False).create(environment)
    python = validator.environment_python(environment)
    site_packages = subprocess.check_output(
        [str(python), '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True,
    ).strip()
    (Path(site_packages) / 'editable.pth').write_text(str(source / 'src') + '\n')
    acceptance.prove_editable_edit(python, source, tmp_path, '', os.environ.copy())
    assert acceptance.EDIT_AFTER in (package / '_version.py').read_text()


def test_fresh_checkout_uses_committed_revision_and_ignores_working_files(tmp_path):
    repository = tmp_path / 'original'
    repository.mkdir()
    subprocess.run(['git', 'init', str(repository)], check=True)
    committed = repository / 'application.py'
    committed.write_text('committed Python code\n')
    subprocess.run(['git', 'add', 'application.py'], cwd=repository, check=True)
    subprocess.run(['git', '-c', 'user.name=Acceptance fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-m', 'fixture'],
                   cwd=repository, check=True)
    committed.write_text('stale uncommitted Python code\n')
    (repository / 'stale-native.so').touch()
    fresh = acceptance.fresh_checkout(repository, tmp_path / 'fresh')
    assert (fresh / 'application.py').read_text() == 'committed Python code\n'
    assert not (fresh / 'stale-native.so').exists()


def test_only_rejected_pip_rust_version_probes_are_allowed(tmp_path):
    log = tmp_path / 'compiler.log'
    baseline = 'self-test\n'
    log.write_text(baseline + 'audit blocked optional version probe rustc\n')
    acceptance.assert_no_compiler_builds(log, baseline)
    log.write_text(baseline + 'audit blocked rustc\n')
    with pytest.raises(AssertionError, match='compiler build was attempted'):
        acceptance.assert_no_compiler_builds(log, baseline)


def test_startup_observer_preserves_launcher_readiness_before_qt_imports():
    hook = ast.parse(acceptance.STARTUP_HOOK)
    compile(hook, '<compiler-free-qplot-startup>', 'exec')
    conditional = next(node for node in hook.body if isinstance(node, ast.If))
    direct_imports = [node for node in conditional.body if isinstance(node, ast.ImportFrom)]
    assert not direct_imports, 'Qt/application imports must wait until _run_gui is called'
    observed_run = next(node for node in conditional.body
                        if isinstance(node, ast.FunctionDef) and node.name == 'observed_run')
    assert any(isinstance(node, ast.ImportFrom) and node.module == 'PyQt6'
               for node in observed_run.body)
    assert 'return original_run(*args, **kwargs)' in acceptance.STARTUP_HOOK
    assert 'assert self.isVisible()' in acceptance.STARTUP_HOOK
