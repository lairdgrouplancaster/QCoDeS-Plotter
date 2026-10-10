"""Accept wheel and editable application installs with compilers disabled."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlparse

if __package__:
    from . import validate_distribution as validator
else:
    import validate_distribution as validator


COMPILERS = (
    'cc', 'c++', 'gcc', 'g++', 'clang', 'clang++', 'clang-cl', 'cl',
    'icc', 'icx', 'link', 'ld', 'ar', 'gfortran', 'rustc', 'cargo',
)
EDIT_BEFORE = 'before-edit'
EDIT_AFTER = 'after-edit-without-reinstall'


def install_compiler_audit_guard(python: Path, env: dict[str, str]) -> None:
    """Reject even absolute compiler paths in pip's isolated build processes."""
    site_packages = subprocess.check_output(
        [str(python), '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
        env=env, text=True,
    ).strip()
    guard = f'''\
import os
import shlex
import sys
from pathlib import Path

blocked = {COMPILERS!r}
def audit(event, arguments):
    if event in ('subprocess.Popen', 'os.exec', 'os.posix_spawn'):
        targets = [arguments[0]]
        if event == 'subprocess.Popen':
            argv = arguments[1]
            if isinstance(argv, (str, bytes)):
                targets += shlex.split(os.fsdecode(argv))
            else:
                targets += list(argv)
    elif event == 'os.system':
        targets = shlex.split(os.fsdecode(arguments[0]))
    elif event == 'os.spawn':
        targets = [arguments[1]]
    else:
        return
    for target in targets:
        if not isinstance(target, (str, bytes, os.PathLike)):
            continue
        name = Path(os.fsdecode(target).strip('"')).name.lower()
        for suffix in ('.exe', '.cmd', '.bat'):
            name = name.removesuffix(suffix)
        if name in blocked:
            with open(os.environ['QPLOT_COMPILER_LOG'], 'a') as log:
                # pip probes rustc for its HTTP user-agent whenever the tool
                # appears on PATH. Fail that probe too; it is not a build.
                version_probe = (event == 'subprocess.Popen' and name == 'rustc'
                                 and not isinstance(arguments[1], (str, bytes))
                                 and len(arguments[1]) == 2
                                 and arguments[1][1] == '--version')
                log.write('audit blocked optional version probe rustc\\n' if version_probe
                          else 'audit blocked ' + os.fsdecode(target) + '\\n')
            raise RuntimeError('Compiler invocation forbidden during acceptance: ' + os.fsdecode(target))

sys.addaudithook(audit)
'''
    directory = Path(site_packages)
    (directory / '_qplot_ci_compiler_guard.py').write_text(guard)
    # .pth files execute before sitecustomize. pip's build-isolation
    # sitecustomize removes venv package paths afterward; the audit hook remains
    # registered and does not depend on those paths for subsequent imports.
    (directory / 'qplot-ci-compiler-guard.pth').write_text('import _qplot_ci_compiler_guard\n')


def compiler_free_environment(temporary: Path) -> tuple[dict[str, str], Path]:
    """Block compiler selection and PATH lookup; record every attempted call."""
    blockers = temporary / 'disabled-compilers'
    blockers.mkdir()
    log = temporary / 'compiler-invocations.log'
    log.touch()
    for name in COMPILERS:
        path = blockers / (name + '.cmd' if os.name == 'nt' else name)
        if os.name == 'nt':
            path.write_text('@echo off\necho %~nx0>>"%QPLOT_COMPILER_LOG%"\nexit /b 86\n')
        else:
            path.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >> "$QPLOT_COMPILER_LOG"\nexit 86\n')
            path.chmod(0o755)
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('PIP_') or key in ('PYTHONPATH', 'PYTHONHOME'):
            env.pop(key, None)
    env.update(
        QPLOT_COMPILER_LOG=str(log), PIP_NO_CACHE_DIR='1', UV_NO_CACHE='1',
        PIP_CACHE_DIR=str(temporary / 'unused-pip-cache'),
        UV_CACHE_DIR=str(temporary / 'unused-uv-cache'),
        PIP_CONFIG_FILE=os.devnull, PIP_ONLY_BINARY=':all:', PIP_NO_BINARY=':none:',
        PYTHONNOUSERSITE='1', QT_QPA_PLATFORM='offscreen',
    )
    env.setdefault('MPLCONFIGDIR', str(temporary / 'matplotlib'))
    # MSVC normally discovers absolute tool paths via Visual Studio. Force it
    # to use this deliberately unusable SDK environment instead of discovery.
    if os.name == 'nt':
        env.update(DISTUTILS_USE_SDK='1', MSSdk='1',
                   VCINSTALLDIR=str(blockers), VSINSTALLDIR=str(blockers),
                   VCToolsInstallDir=str(blockers), INCLUDE='', LIB='', LIBPATH='')
        paths = [directory for directory in env.get('PATH', '').split(os.pathsep)
                 if directory and not any((Path(directory) / (name + '.exe')).is_file()
                                          for name in COMPILERS)]
    else:
        paths = env.get('PATH', '').split(os.pathsep)
    env['PATH'] = os.pathsep.join([str(blockers), *paths])
    suffix = '.cmd' if os.name == 'nt' else ''
    for variable, tool in (('CC', 'cc'), ('CXX', 'c++'), ('CPP', 'cc'),
                           ('LDSHARED', 'cc'), ('AR', 'ar'), ('RUSTC', 'rustc'),
                           ('CARGO', 'cargo')):
        target = str(blockers / (tool + suffix))
        env[variable] = target if os.name == 'nt' else shlex.quote(target)
    return env, log


def verify_compilers_disabled(python: Path, env: dict[str, str], log: Path) -> str:
    """Prove the guard fails, then retain its log as the accepted baseline."""
    for variable in ('CC', 'CXX'):
        command = ([env.get('COMSPEC', 'cmd.exe'), '/d', '/c', env[variable], '--version']
                   if os.name == 'nt' else [*shlex.split(env[variable]), '--version'])
        result = subprocess.run(command, env=env, check=False, timeout=10)
        assert result.returncode == 86, (command, result.returncode)
    # A .pth audit hook must also block an absolute compiler path, even when
    # isolation/user-code removes its module directory from sys.path.
    absolute = str(log.parent / ('cl.exe' if os.name == 'nt' else 'cc'))
    code = f'''\
import subprocess
import sys
sys.path[:] = []
try:
    subprocess.run([{absolute!r}, '--version'])
except RuntimeError as error:
    assert 'Compiler invocation forbidden' in str(error), error
else:
    raise AssertionError('Absolute compiler invocation was accepted')
'''
    validator.run([str(python), '-I', '-c', code], env=env)
    baseline = log.read_text()
    assert len(baseline.splitlines()) == 3, baseline
    print('C/C++ guards reject invocation; Python audit also blocks absolute tool paths.', flush=True)
    return baseline


def installation_command(python: Path, app: Path, native: Path, report: Path,
                         *, editable: bool, public_version: str | None = None) -> list[str]:
    command = [str(python), '-m', 'pip', 'install', '--only-binary=:all:',
               '--no-cache-dir', '--report', str(report)]
    if public_version is not None:
        command += ['--index-url', 'https://pypi.org/simple']
        if not editable:
            return command + [f'qplotter=={public_version}']
        command += [f'qplotter-native=={validator.PINNED_NATIVE_VERSION}']
    else:
        command += [str(native.resolve())]
    command += ['--editable', f'{app.resolve()}[dev]'] if editable else [str(app.resolve())]
    return command


def validate_install_report(report: Path, *, editable: bool,
                            public_artifacts: dict[str, Path] | None = None) -> None:
    """Every resolved runtime/dev dependency must have come from a wheel."""
    installed = json.loads(report.read_text())['install']
    names = set()
    for item in installed:
        name = item['metadata']['name'].lower().replace('_', '-')
        names.add(name)
        info = item['download_info']
        if editable and name == 'qplotter':
            assert info.get('dir_info', {}).get('editable') is True, info
        else:
            assert urlparse(info['url']).path.endswith('.whl'), info
        if public_artifacts is not None and not (editable and name == 'qplotter'):
            url = urlparse(info['url'])
            assert url.scheme == 'https' and url.hostname == 'files.pythonhosted.org', info
            assert item['is_direct'] is False, item
            if name in ('qplotter', 'qplotter-native'):
                wheel = public_artifacts[name.replace('-', '_') + '_wheel']
                assert Path(url.path).name == wheel.name, info
                expected = hashlib.sha256(wheel.read_bytes()).hexdigest()
                assert info['archive_info']['hashes']['sha256'] == expected, info
        elif name in ('qplotter', 'qplotter-native'):
            assert item['is_direct'] is True, item
            assert urlparse(info['url']).scheme == 'file', info
    assert {'qplotter', 'qplotter-native', 'apsw'} <= names, names
    if editable:
        assert {'pytest', 'ruff', 'mypy', 'build', 'twine'} <= names, names
    print(f'{len(names)} packages resolved; all dependencies are wheels.', flush=True)


def assert_no_compiler_builds(log: Path, baseline: str) -> None:
    observed = log.read_text()
    assert observed.startswith(baseline), 'Compiler guard log was changed'
    attempts = observed[len(baseline):].splitlines()
    assert all(line == 'audit blocked optional version probe rustc' for line in attempts), (
        'A compiler build was attempted during acceptance:\n' + '\n'.join(attempts)
    )
    print(f'No compiler builds attempted; {len(attempts)} optional rustc version probes rejected.', flush=True)


def editable_audit_code(source: Path) -> str:
    return f'''\
import json
from importlib.metadata import distribution
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname
import qplot

source = Path({str(source.resolve())!r})
assert Path(qplot.__file__).resolve() == source / 'src/qplot/__init__.py'
direct = json.loads(distribution('qplotter').read_text('direct_url.json'))
assert direct['dir_info']['editable'] is True, direct
assert Path(url2pathname(urlparse(direct['url']).path)).resolve() == source, direct
print('Application imports resolve to the PEP 660 editable source.')
'''


def prove_editable_edit(python: Path, source: Path, temporary: Path,
                       audit: str, env: dict[str, str]) -> None:
    """Change an existing Python module without another pip/build command."""
    module = source / 'src/qplot/_version.py'
    original = module.read_text()
    module.write_text(original + f'\n\ndef _compiler_free_edit_probe():\n    return {EDIT_BEFORE!r}\n')
    probe = temporary / 'editable-edit-probe.py'
    for expected in (EDIT_BEFORE, EDIT_AFTER):
        probe.write_text(audit + f'''\
from qplot._version import _compiler_free_edit_probe
assert _compiler_free_edit_probe() == {expected!r}
print('Editable Python edit observed:', _compiler_free_edit_probe())
''')
        validator.run([str(python), '-I', str(probe)], cwd=temporary, env=env)
        if expected == EDIT_BEFORE:
            # Change size as well as content so Python cannot reuse bytecode
            # from a same-second edit with an unchanged source size.
            module.write_text(module.read_text().replace(repr(EDIT_BEFORE), repr(EDIT_AFTER)))


STARTUP_HOOK = '''\
import json
import os
from pathlib import Path

# Apply only to the real GUI child, keeping the actual console launcher intact.
if '_QPLOT_SHUTDOWN_SUPERVISOR_V1' in os.environ:
    import qplot.__main__ as entrypoint
    original_run = entrypoint._run_gui
    def observed_run(*args, **kwargs):
        # Wait until the real child has connected to its launcher before Qt
        # imports, preserving the production bootstrap/readiness ordering.
        from PyQt6 import QtCore, QtWidgets
        from qplot import diagnostics
        from qplot.configuration.config import config

        root = Path(os.environ['QPLOT_STARTUP_ROOT'])
        diagnostics.default_log_file = lambda: root / 'qplot.log'
        config.default_path = str(root / 'settings')
        config.default_file = str(root / 'settings/config.json')
        from qplot.windows.main import MainWindow

        original_init = MainWindow.__init__
        def observed_init(self, *init_args, **init_kwargs):
            original_init(self, *init_args, **init_kwargs)
            self.config.config['user_preference']['confirm_close'] = False
            self.config.config['user_preference']['confirm_close_all'] = False
            def observe_and_quit():
                app = QtWidgets.QApplication.instance()
                assert app.applicationName() == 'qPlot'
                assert self.isVisible()
                (root / 'startup.json').write_text(json.dumps({'visible': True, 'event_loop': True}))
                self.quit_application()
            QtCore.QTimer.singleShot(0, observe_and_quit)
        MainWindow.__init__ = observed_init
        return original_run(*args, **kwargs)
    entrypoint._run_gui = observed_run
'''


def start_qplot(environment: Path, temporary: Path, env: dict[str, str]) -> None:
    """Launch the installed console command and observe its real Qt event loop."""
    hook = temporary / 'startup-hook'
    hook.mkdir()
    (hook / 'sitecustomize.py').write_text(STARTUP_HOOK)
    startup_env = dict(env, PYTHONPATH=str(hook), QPLOT_STARTUP_ROOT=str(hook))
    result = subprocess.run([str(validator.console_script(environment, 'qplot'))],
                            cwd=hook, env=startup_env, check=True, timeout=60)
    assert result.returncode == 0
    assert json.loads((hook / 'startup.json').read_text()) == {'visible': True, 'event_loop': True}
    print('Installed qplot command displayed its MainWindow, entered Qt, and shut down cleanly.', flush=True)


def accept_installation(repository: Path, artifacts: dict[str, Path],
                        temporary: Path, *, editable: bool, public_pypi: bool = False) -> None:
    assert not temporary.resolve().is_relative_to(repository.resolve())
    app_wheel = artifacts['qplotter_wheel']
    native = artifacts['qplotter_native_wheel']
    source_inventory = validator.source_files(repository)
    runtime = validator.validate_wheel(app_wheel, source_inventory)
    validator.validate_wheel(native, source_inventory, native=True)
    if editable:
        if public_pypi:
            # Fetch only the tested revision into a new checkout outside the
            # runner's original checkout, without reusing its working files.
            app = temporary / 'fresh-checkout'
            revision = subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], cwd=repository, text=True,
            ).strip()
            validator.run(['git', 'init', str(app)])
            validator.run(['git', 'fetch', '--depth', '1', str(repository), revision], cwd=app)
            validator.run(['git', 'checkout', '--detach', 'FETCH_HEAD'], cwd=app)
            assert subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], cwd=app, text=True,
            ).strip() == revision
        else:
            sdist = artifacts['qplotter_sdist']
            validator.validate_sdist(sdist, source_inventory)
            app = validator.extract_sdist(sdist, temporary / 'editable-source')
    else:
        app = app_wheel
    environment = temporary / 'venv'
    python = validator.create_environment(environment)
    env, log = compiler_free_environment(temporary)
    install_compiler_audit_guard(python, env)
    baseline = verify_compilers_disabled(python, env, log)
    report = temporary / 'install-report.json'
    version = tomllib.loads((repository / 'pyproject.toml').read_text())['project']['version']
    assert app_wheel.name.startswith(f'qplotter-{version}-'), app_wheel
    validator.run(installation_command(python, app, native, report, editable=editable,
                                      public_version=version if public_pypi else None),
                  cwd=temporary, env=env)
    validate_install_report(report, editable=editable,
                            public_artifacts=artifacts if public_pypi else None)
    validator.run([str(python), '-m', 'pip', 'check'], cwd=temporary, env=env)
    reported_version = subprocess.check_output(
        [str(validator.console_script(environment, 'qplot-cfg')), '-version'],
        cwd=temporary, env=env, text=True, timeout=30,
    ).strip()
    assert reported_version == version, (reported_version, version)
    print(f'Installed qplot-cfg reports {version}.', flush=True)
    audit = validator.wheel_installation_audit_code([native] if editable else [app_wheel, native])
    if editable:
        audit += editable_audit_code(app)
    # The native receipt hashes the exact artifact each time. Preserve its file
    # identity and timestamp as additional proof that no rebuild occurred.
    snapshot = temporary / 'native-stat.json'
    native_probe = temporary / 'native-stat.py'
    native_probe.write_text(audit + f'''\
import json
from pathlib import Path
import qplot_native._trusted_vfs_native as native
stat = Path(native.__file__).stat()
observed = [stat.st_ino, stat.st_size, stat.st_mtime_ns]
snapshot = Path({str(snapshot)!r})
if snapshot.exists():
    assert json.loads(snapshot.read_text()) == observed, 'Native file was rebuilt or replaced'
else:
    snapshot.write_text(json.dumps(observed))
''')
    validator.run([str(python), '-I', str(native_probe)], cwd=temporary, env=env)
    if editable:
        prove_editable_edit(python, app, temporary, audit, env)
    start_qplot(environment, temporary, env)
    validator.smoke_test_installation(repository, app_wheel, native, runtime,
                                     temporary, environment, audit_code=audit, env=env)
    validator.run([str(python), '-I', str(native_probe)], cwd=temporary, env=env)
    assert_no_compiler_builds(log, baseline)
    print(f'{"Editable" if editable else "Wheel"} installation works without a compiler; '
          'trusted live reader remains operational.', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=('wheel', 'editable'))
    parser.add_argument('--public-pypi', action='store_true',
                        help='Resolve the exact release from public PyPI; editable uses a fresh checkout')
    parser.add_argument('artifacts', nargs='+', type=Path)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    artifacts = validator.find_artifacts(args.artifacts, wheel_only=True)
    if args.mode == 'editable' and not args.public_pypi:
        # The app sdist comes from this run's single application build; a native
        # sdist is deliberately unnecessary and is never installed here.
        paths = [file for path in args.artifacts
                 for file in (path.glob('qplotter-*.tar.gz') if path.is_dir() else [path])
                 if file.name.startswith('qplotter-') and file.name.endswith('.tar.gz')]
        assert len(paths) == 1, paths
        artifacts['qplotter_sdist'] = paths[0]
    with tempfile.TemporaryDirectory(prefix=f'qplot-compiler-free-{args.mode}-') as name:
        accept_installation(repository, artifacts, Path(name), editable=args.mode == 'editable',
                            public_pypi=args.public_pypi)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
