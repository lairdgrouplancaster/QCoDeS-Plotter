"""Validate qPlot source and wheel distributions in isolated environments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
import tomllib
import venv
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath

IGNORED_DIRECTORY_NAMES = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "other",
}
IGNORED_FILE_NAMES = {".coverage", ".DS_Store", "coverage.xml"}
REQUIRED_ROOT_FILES = {
    "Agents.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "pyproject.toml",
}
SDIST_SOURCE_PREFIXES = ("docs/", "scripts/", "src/", "tests/")
REQUIRED_NATIVE_SOURCE_FILES = {
    "src/qplot_native/_trusted_vfs_native.c",
    "src/qplot_native/_trusted_vfs_sqlite_abi.h",
}
NATIVE_BUILD_SUFFIXES = {".c", ".h"}
COMPILED_NATIVE_SUFFIXES = {".dll", ".dylib", ".pyd", ".so"}
NATIVE_EXTENSION_MODULE = "qplot_native._trusted_vfs_native"
NATIVE_EXTENSION_STEM = "qplot_native/_trusted_vfs_native"
NATIVE_EXTENSION_MEMBERS = {
    f"{NATIVE_EXTENSION_STEM}.abi3.so",
    f"{NATIVE_EXTENSION_STEM}.pyd",
}
PINNED_APSW_VERSION = "3.53.4.0"
PINNED_NATIVE_VERSION = "1.0.0"
CONSOLE_SCRIPTS = {
    "qplot": "qplot.__main__:run",
    "qplot-cfg": "qplot.configuration.scripts:scripts",
    "qplot-generate-db": "qplot.testdata:main",
}
ENTRYPOINT_DELEGATION_SITECUSTOMIZE = """\
import json
import os
from pathlib import Path

from qplot import _shutdown_supervisor as shutdown_supervisor


def capture_launch(original_argv=None, *, database_path=None):
    Path(os.environ["_QPLOT_ENTRYPOINT_DELEGATION_RECORD"]).write_text(
        json.dumps(
            {
                "argv": list(original_argv),
                "database_path": database_path,
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 17


shutdown_supervisor.launch_gui = capture_launch
"""


def run(command: list[str], *, cwd: Path | None = None, env=None) -> None:
    """Run a subprocess and show the exact command in CI output."""
    print(f"+ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def source_files(repository: Path) -> set[str]:
    """Return versioned and untracked source paths, using archive separators."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return {
        path.decode().replace(os.sep, "/")
        for path in result.stdout.split(b"\0")
        if path and (repository / path.decode()).is_file()
    }


def check_clean(repository: Path) -> None:
    """Require a clean tracked and untracked source tree before CI builds."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        raise AssertionError(
            f"distribution builds must start from a clean source tree:\n{result.stdout}"
        )
    print("Source tree is clean.")


def archive_files(artifact: Path) -> set[str]:
    """Return regular-file member names from a supported archive."""
    if artifact.name.endswith(".tar.gz"):
        with tarfile.open(artifact, "r:gz") as archive:
            return {member.name for member in archive.getmembers() if member.isfile()}
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            return {item.filename for item in archive.infolist() if not item.is_dir()}
    raise AssertionError(f"unsupported distribution artifact: {artifact}")


def relative_sdist_files(members: set[str]) -> tuple[str, set[str]]:
    """Strip and return the sdist's single top-level directory."""
    roots = {PurePosixPath(member).parts[0] for member in members}
    if len(roots) != 1:
        raise AssertionError(
            f"sdist should have one top-level directory, found {roots}"
        )
    root = roots.pop()
    prefix = f"{root}/"
    return root, {member.removeprefix(prefix) for member in members}


def ignored_member(path: str) -> bool:
    """Return whether an artifact path is forbidden by the sdist policy."""
    parts = PurePosixPath(path).parts
    setuptools_inventory = any(
        path.endswith(f"src/{name}.egg-info/SOURCES.txt")
        for name in ("qplotter", "qplotter_native")
    )
    for part in parts:
        if part in IGNORED_DIRECTORY_NAMES:
            return True
        if part == ".venv" or part.startswith(".venv-"):
            return True
        if part.endswith(".egg-info") and not setuptools_inventory:
            return True
    name = parts[-1]
    return name in IGNORED_FILE_NAMES or name.endswith((".pyc", ".pyo"))


def assert_no_ignored_members(artifact: Path, members: set[str]) -> None:
    leaked = sorted(member for member in members if ignored_member(member))
    if leaked:
        raise AssertionError(
            f"{artifact.name} contains ignored or generated files:\n"
            + "\n".join(leaked)
        )


def assert_no_compiled_native_members(artifact: Path, members: set[str]) -> None:
    """Reject stale native binaries from a source distribution."""
    leaked = sorted(
        member
        for member in members
        if PurePosixPath(member).suffix.casefold() in COMPILED_NATIVE_SUFFIXES
    )
    if leaked:
        raise AssertionError(
            f"{artifact.name} contains compiled native files; wheels must build "
            "the extension from source:\n" + "\n".join(leaked)
        )


def validate_sdist(
    artifact: Path,
    source: set[str],
    *, native: bool = False,
) -> tuple[str, set[str]]:
    """Check the sdist against the explicit source-tree inclusion policy."""
    members = archive_files(artifact)
    assert_no_ignored_members(artifact, members)
    root, relative_members = relative_sdist_files(members)
    assert_no_compiled_native_members(artifact, relative_members)
    expected = (
        REQUIRED_ROOT_FILES
        | {
            path
            for path in source
            if path.startswith(SDIST_SOURCE_PREFIXES)
            and PurePosixPath(path).suffix.casefold() not in COMPILED_NATIVE_SUFFIXES
        }
    )
    if native:
        expected = {"LICENSE", "README.md", "MANIFEST.in", "pyproject.toml", "setup.py"}
        expected |= REQUIRED_NATIVE_SOURCE_FILES
        expected |= {
            path.removeprefix("native/") for path in source
            if path.startswith("native/src/")
            and not ignored_member(path)
            and PurePosixPath(path).suffix.casefold() not in COMPILED_NATIVE_SUFFIXES
        }
    missing = sorted(expected - relative_members)
    if missing:
        raise AssertionError(
            f"{artifact.name} is missing required source files:\n" + "\n".join(missing)
        )

    source_tests = sorted(
        path
        for path in source
        if path.startswith("tests/")
        and Path(path).name.startswith("test_")
        and path.endswith(".py")
    )
    if not native and "tests/conftest.py" not in relative_members:
        raise AssertionError("sdist is missing tests/conftest.py")
    detail = (
        "native build sources are present."
        if native else f"all {len(source_tests)} source test modules and conftest are present."
    )
    print(f"{artifact.name}: {len(relative_members)} files; {detail}")
    return root, relative_members


def validate_wheel(
    artifact: Path, source: set[str], *, native: bool = False,
) -> set[str]:
    """Check each distribution's tags, metadata, and exact runtime inventory."""
    tag = "-cp311-abi3-" if native else "-py3-none-any.whl"
    if tag not in artifact.name:
        raise AssertionError(f"{artifact.name} must use {tag}")
    members = archive_files(artifact)
    assert_no_ignored_members(artifact, members)
    package = "qplot_native" if native else "qplot"
    distribution_name = "qplotter-native" if native else "qplotter"
    actual_runtime = {path for path in members if path.startswith(f"{package}/")}
    native_members = actual_runtime & NATIVE_EXTENSION_MEMBERS
    if native and len(native_members) != 1:
        raise AssertionError(
            f"{artifact.name} must contain exactly one abi3 native extension; "
            f"found {sorted(native_members)}"
        )
    if not native:
        assert_no_compiled_native_members(artifact, members)
    source_prefix = "native/src/" if native else "src/"
    expected_runtime = {
        path.removeprefix(source_prefix)
        for path in source
        if path.startswith(f"{source_prefix}{package}/")
        and PurePosixPath(path).suffix.casefold()
        not in NATIVE_BUILD_SUFFIXES | COMPILED_NATIVE_SUFFIXES
        and not ignored_member(path)
    } | native_members
    missing = sorted(expected_runtime - actual_runtime)
    stale = sorted(actual_runtime - expected_runtime)
    if missing or stale:
        raise AssertionError(
            f"{artifact.name} runtime mismatch: missing {missing}; unexpected {stale}"
        )
    unexpected = sorted(
        path for path in members - actual_runtime
        if not PurePosixPath(path).parts[0].endswith(".dist-info")
    )
    if unexpected:
        raise AssertionError(f"{artifact.name} has unexpected files: {unexpected}")
    with zipfile.ZipFile(artifact) as archive:
        metadata_paths = [path for path in members if path.endswith(".dist-info/METADATA")]
        assert len(metadata_paths) == 1, metadata_paths
        metadata = Parser().parsestr(archive.read(metadata_paths[0]).decode())
        assert metadata["Name"] == distribution_name
        requirements = metadata.get_all("Requires-Dist", [])
        assert f"apsw=={PINNED_APSW_VERSION}" in requirements, requirements
        if native:
            assert metadata["Version"] == PINNED_NATIVE_VERSION
        else:
            assert f"qplotter-native=={PINNED_NATIVE_VERSION}" in requirements
        wheel_path = metadata_paths[0].removesuffix("METADATA") + "WHEEL"
        wheel_metadata = Parser().parsestr(archive.read(wheel_path).decode())
        assert wheel_metadata["Root-Is-Purelib"] == ("false" if native else "true")
        tags = wheel_metadata.get_all("Tag", [])
        assert tags and all(
            value.startswith("cp311-abi3-") if native else value == "py3-none-any"
            for value in tags
        ), tags
    print(f"{artifact.name}: {len(members)} files; {len(actual_runtime)} runtime files.")
    return expected_runtime


def extract_sdist(artifact: Path, destination: Path) -> Path:
    """Safely extract an sdist and return its source root."""
    destination = destination.resolve()
    with tarfile.open(artifact, "r:gz") as archive:
        roots = set()
        for member in archive.getmembers():
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise AssertionError(f"unsafe sdist member: {member.name}")
            if member.issym() or member.islnk():
                raise AssertionError(f"sdist links are not permitted: {member.name}")
            roots.add(member_path.parts[0])
        if len(roots) != 1:
            raise AssertionError(f"sdist should have one top-level directory: {roots}")
        archive.extractall(destination)
    return destination / roots.pop()


def environment_python(environment: Path) -> Path:
    """Return the Python executable for a venv on any supported platform."""
    scripts = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return environment / scripts / executable


def console_script(environment: Path, name: str) -> Path:
    """Return a console-script path for a venv on any supported platform."""
    scripts = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return environment / scripts / f"{name}{suffix}"


def create_environment(path: Path) -> Path:
    """Create a fresh venv and return its Python executable."""
    venv.EnvBuilder(with_pip=True).create(path)
    return environment_python(path)


def wheel_installation_audit_code(artifacts: list[Path]) -> str:
    """Bind imported packages to the exact bytes in the supplied local wheels."""
    expected = {}
    for artifact in artifacts:
        with zipfile.ZipFile(artifact) as archive:
            metadata_path = next(
                name for name in archive.namelist() if name.endswith('.dist-info/METADATA')
            )
            metadata = Parser().parsestr(archive.read(metadata_path).decode())
            package = 'qplot_native' if metadata['Name'] == 'qplotter-native' else 'qplot'
            expected[metadata['Name']] = {
                'version': metadata['Version'],
                'package': package,
                'files': {
                    item.filename: hashlib.sha256(archive.read(item)).hexdigest()
                    for item in archive.infolist()
                    if not item.is_dir() and item.filename.startswith(f'{package}/')
                },
            }
    return f'''\
import hashlib
import importlib
import importlib.util
import sys
from importlib.metadata import distribution
from pathlib import Path

expected = {expected!r}
for name, record in expected.items():
    installed = distribution(name)
    assert installed.version == record['version'], (name, installed.version)
    paths = {{
        relative: Path(installed.locate_file(relative)).resolve()
        for relative in record['files']
    }}
    for relative, digest in record['files'].items():
        path = paths[relative]
        assert path.is_relative_to(Path(sys.prefix).resolve()), ('outside environment', path)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, ('wheel hash mismatch', path)
    package_spec = importlib.util.find_spec(record['package'])
    assert package_spec is not None and package_spec.origin is not None, record['package']
    assert Path(package_spec.origin).resolve() == paths[record['package'] + '/__init__.py'], ('checkout package origin', package_spec.origin)
    package = importlib.import_module(record['package'])
    assert Path(package.__file__).resolve() == paths[record['package'] + '/__init__.py'], ('checkout package', package.__file__)
    if record['package'] == 'qplot_native':
        extension = next(relative for relative in paths if relative.endswith(('.so', '.pyd')))
        native_spec = importlib.util.find_spec('qplot_native._trusted_vfs_native')
        assert native_spec is not None and native_spec.origin is not None, 'missing native extension'
        assert Path(native_spec.origin).resolve() == paths[extension], ('stale native origin', native_spec.origin)
        native = importlib.import_module('qplot_native._trusted_vfs_native')
        assert Path(native.__file__).resolve() == paths[extension], ('stale native extension', native.__file__)
    for module_name, module in list(sys.modules.items()):
        if module_name.startswith(record['package'] + '.') and getattr(module, '__file__', None):
            assert Path(module.__file__).resolve() in paths.values(), ('checkout module', module_name, module.__file__)
print('Installed application and native files match the supplied wheels.')
'''


def install_wheels(
    python: Path, artifact: Path, native_artifact: Path, *,
    with_dev_tools: bool = False, extra_requirements: tuple[str, ...] = (),
) -> None:
    """Install the local pair first without consulting an index, then dependencies."""
    local = [str(native_artifact.resolve()), str(artifact.resolve())]
    run([str(python), '-m', 'pip', 'install', '--no-index', '--no-deps',
         '--force-reinstall', *local])
    # Keep explicit wheel arguments during dependency resolution too. A missing
    # native wheel is an error before pip can consider a release from an index.
    if with_dev_tools:
        local[1] += '[dev]'
    run([str(python), '-m', 'pip', 'install', '--only-binary', 'apsw',
         *local, *extra_requirements])


def test_extracted_sdist(
    artifact: Path, native_artifact: Path, temporary: Path,
) -> None:
    """Install and run all tests from the extracted source distribution."""
    source = extract_sdist(artifact, temporary / "sdist-source")
    environment = temporary / "sdist-venv"
    python = create_environment(environment)
    native_source = extract_sdist(native_artifact, temporary / "native-sdist-source")
    run([str(python), "-m", "pip", "install", str(native_source), f"{source}[dev]"])
    test_env = os.environ.copy()
    test_env.setdefault("QT_QPA_PLATFORM", "offscreen")
    test_env.setdefault("MPLCONFIGDIR", str(temporary / "matplotlib"))
    test_env.pop("PYTHONPATH", None)
    # Exercise both installed sdists, including the separate extension. The
    # repository's normal ``pythonpath = ["src"]`` setting would otherwise
    # shadow that installation with the unbuilt extracted source tree.
    # Match CI's two-worker, per-file scheduling; coverage has its own CI job.
    run(
        [
            str(python), "-m", "pytest", "--no-cov",
            "-n", "2", "--dist=loadfile", "--no-loadscope-reorder",
            "-o", "pythonpath=", "--timeout=90", "--timeout-method=thread",
        ],
        cwd=source,
        env=test_env,
    )


def wheel_smoke_code() -> str:
    """Return isolated-Python checks run after installing the wheel."""
    return """
import ctypes
import importlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from importlib.metadata import distribution, version
from importlib.resources import files
from pathlib import Path

import apsw
import qplot
from qplot import _shutdown_supervisor as shutdown_supervisor
from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
from qplot.datahandling.trusted_live import (
    TRUSTED_LIVE_MAX_SCALAR_BYTES,
    TrustedLiveCleanupError,
    TrustedLiveResultLimitError,
    TrustedLiveSqlRejectedError,
    TrustedQuery,
)
from qplot.datahandling.trusted_live_queries import (
    TrustedMetadataQueryAdapter,
    trusted_source_revision,
)
from qplot.datahandling.trusted_live_service import (
    TrustedLiveReadService,
    TrustedReadPriority,
)
from qplot.datahandling.trusted_live_supervisor import TrustedLiveReaderSupervisor
from qplot.datahandling.trusted_presentation import (
    TRUSTED_PRESENTATION_MAX_KEY_BYTES,
    TRUSTED_PRESENTATION_MAX_RENDERED_NODES,
    TRUSTED_PRESENTATION_MAX_RENDERED_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES,
    TRUSTED_PRESENTATION_MAX_TOOLTIP_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_VALUE_BYTES,
    TrustedSelectedRunPresentation,
    build_selected_run_presentation,
)
from qplot.datahandling.trusted_snapshot import (
    TRUSTED_SNAPSHOT_MAX_INPUT_BYTES,
    TrustedSnapshotOmission,
    TrustedSnapshotView,
    normalize_trusted_snapshot,
)
from qplot.datahandling.trusted_work_coordinator import (
    TrustedDerivedRun,
    TrustedWorkCoordinator,
)
from qplot.datahandling.trusted_work_scheduler import (
    TrustedWorkKind,
    trusted_derived_cache_root,
)


ARTIFACT_AUDIT_CODE = r'''\
import hashlib
import json
import os
import stat
import sys

result = {}
for name in json.loads(sys.argv[1]):
    try:
        path_status = os.lstat(name)
    except FileNotFoundError:
        result[name] = None
        continue
    if stat.S_ISLNK(path_status.st_mode):
        raise AssertionError(f"protected artifact became a symlink: {name}")
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if getattr(path_status, "st_file_attributes", 0) & reparse_flag:
        raise AssertionError(f"protected artifact became a reparse point: {name}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags)
    try:
        opened_status = os.fstat(descriptor)
        if not stat.S_ISREG(opened_status.st_mode):
            raise AssertionError(f"protected artifact is not regular: {name}")
        if (opened_status.st_dev, opened_status.st_ino) != (
            path_status.st_dev,
            path_status.st_ino,
        ):
            raise AssertionError(f"protected artifact changed during open: {name}")

        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        final_status = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_gid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(opened_status, field) != getattr(final_status, field)
            for field in stable_fields
        ):
            raise AssertionError(f"protected artifact changed while hashing: {name}")
        result[name] = {
            field: getattr(final_status, field) for field in stable_fields
        }
        result[name]["st_flags"] = getattr(final_status, "st_flags", None)
        result[name]["st_file_attributes"] = getattr(
            final_status, "st_file_attributes", None
        )
        result[name]["sha256"] = digest.hexdigest()
    finally:
        os.close(descriptor)
print(json.dumps(result, sort_keys=True))
'''


WRITER_CODE = r'''\
import json
import sys

import apsw


def insert_qcodes_run(connection, run_id):
    table_name = f"results_{run_id}"
    description = json.dumps(
        {
            "interdependencies_": {
                "parameters": {
                    "setpoint": {
                        "label": "Setpoint",
                        "unit": "V",
                        "type": "numeric",
                    },
                    "signal": {
                        "label": "Signal",
                        "unit": "A",
                        "type": "numeric",
                    },
                },
                "dependencies": {"signal": ["setpoint"]},
            },
            "shapes": {"signal": [2]},
        },
        separators=(",", ":"),
    )
    connection.execute(
        f'CREATE TABLE "{table_name}" ('
        "id INTEGER PRIMARY KEY, setpoint REAL, signal REAL)"
    )
    connection.executemany(
        f'INSERT INTO "{table_name}" (setpoint, signal) VALUES (?, ?)',
        ((0.0, run_id * 10.0), (1.0, run_id * 10.0 + 1.0)),
    )
    connection.execute(
        "INSERT INTO runs ("
        "run_id, exp_id, name, result_table_name, result_counter, "
        "run_timestamp, completed_timestamp, is_completed, parameters, "
        "guid, run_description, snapshot, parent_datasets, "
        "captured_run_id, captured_counter, operator"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            1,
            f"run-{run_id}",
            table_name,
            2,
            1000.0 + run_id,
            2000.0 + run_id,
            1,
            "setpoint,signal",
            f"00000000-0000-0000-0000-{run_id:012d}",
            description,
            json.dumps({"station": {"run_id": run_id}}),
            "[]",
            run_id,
            run_id,
            f"operator-{run_id}",
        ),
    )
    connection.executemany(
        "INSERT INTO layouts ("
        "layout_id, run_id, parameter, label, unit, inferred_from"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (
            (run_id * 10 + 1, run_id, "setpoint", "Setpoint", "V", None),
            (run_id * 10 + 2, run_id, "signal", "Signal", "A", None),
        ),
    )


connection = apsw.Connection(sys.argv[1])
switch_connection = None
try:
    journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    assert journal_mode is not None
    assert str(journal_mode[0]).casefold() == "wal", journal_mode
    connection.execute("PRAGMA wal_autocheckpoint=0")
    with connection:
        connection.execute("CREATE TABLE smoke(value TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE experiments ("
            "exp_id INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, "
            "sample_name TEXT, "
            "format_string TEXT, "
            "run_counter INTEGER, "
            "start_time INTEGER, "
            "end_time INTEGER"
            ")"
        )
        connection.execute(
            "CREATE TABLE runs ("
            "run_id INTEGER PRIMARY KEY, "
            "exp_id INTEGER, "
            "name TEXT, "
            "result_table_name TEXT, "
            "result_counter INTEGER, "
            "run_timestamp REAL, "
            "completed_timestamp REAL, "
            "is_completed INTEGER, "
            "parameters TEXT, "
            "guid TEXT, "
            "run_description TEXT, "
            "snapshot TEXT, "
            "parent_datasets TEXT, "
            "captured_run_id INTEGER, "
            "captured_counter INTEGER, "
            "operator TEXT"
            ")"
        )
        connection.execute(
            "CREATE TABLE layouts ("
            "layout_id INTEGER PRIMARY KEY, "
            "run_id INTEGER, "
            "parameter TEXT, "
            "label TEXT, "
            "unit TEXT, "
            "inferred_from TEXT"
            ")"
        )
        connection.execute(
            "INSERT INTO experiments ("
            "exp_id, name, sample_name, format_string, run_counter, "
            "start_time, end_time"
            ") VALUES (1, 'wheel smoke', 'installed package', "
            "'{}-{}-{}', 1, 1, NULL)"
        )
        connection.execute("INSERT INTO smoke VALUES ('committed in WAL')")
        insert_qcodes_run(connection, 1)

    # Keep a second, independent WAL source open so the smoke can exercise the
    # application service's database-switch retirement boundary deterministically.
    connection.execute("VACUUM INTO ?", (sys.argv[2],))
    switch_connection = apsw.Connection(sys.argv[2])
    switch_journal_mode = switch_connection.execute(
        "PRAGMA journal_mode=WAL"
    ).fetchone()
    assert switch_journal_mode is not None
    assert str(switch_journal_mode[0]).casefold() == "wal", switch_journal_mode
    switch_connection.execute("PRAGMA wal_autocheckpoint=0")
    with switch_connection:
        switch_connection.execute(
            "UPDATE experiments SET name = 'wheel smoke switch', "
            "sample_name = 'second installed source' WHERE exp_id = 1"
        )
        switch_connection.execute(
            "UPDATE runs SET run_id = 101, name = 'switch-run-101', "
            "guid = '00000000-0000-0000-0000-000000000101', "
            "captured_run_id = 101, captured_counter = 101, "
            "operator = 'operator-101', snapshot = ? WHERE run_id = 1",
            (json.dumps({"station": {"run_id": 101}}),),
        )
        switch_connection.execute(
            "UPDATE layouts SET layout_id = layout_id + 1000, run_id = 101 "
            "WHERE run_id = 1"
        )
    print(json.dumps({"ready": True}), flush=True)
    while True:
        command = sys.stdin.readline().strip()
        if command == "commit":
            with connection:
                connection.execute("INSERT INTO smoke VALUES ('later commit')")
                connection.execute(
                    "UPDATE experiments SET run_counter = 2 WHERE exp_id = 1"
                )
                insert_qcodes_run(connection, 2)
            print(json.dumps({"committed": True}), flush=True)
        elif command == "truncate":
            checkpoint = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            assert checkpoint is not None
            print(json.dumps({"truncate": list(checkpoint)}), flush=True)
        elif command == "checkpoint":
            checkpoint = connection.execute(
                "PRAGMA wal_checkpoint(PASSIVE)"
            ).fetchone()
            assert checkpoint is not None
            print(json.dumps({"checkpoint": list(checkpoint)}), flush=True)
        elif command == "close":
            break
        else:
            raise AssertionError(f"unexpected writer command: {command!r}")
finally:
    if switch_connection is not None:
        switch_connection.close(True)
    connection.close(True)
print(json.dumps({"closed": True}), flush=True)
'''


LAUNCHER_DRIVER_CODE = r'''\
import os
import sys

from qplot import _shutdown_supervisor as shutdown_supervisor


child_argv = [sys.executable, "-I", "-u", sys.argv[1], *sys.argv[2:]]
raise SystemExit(
    shutdown_supervisor._supervise_child(
        child_argv,
        env=os.environ,
        startup_timeout=10.0,
    )
)
'''


NORMAL_SUPERVISED_CHILD_CODE = r'''\
import json
import os
import sys
import time
from pathlib import Path

from qplot._shutdown_supervisor import ShutdownSupervisorClient


def main():
    record_path = Path(sys.argv[1])
    client = ShutdownSupervisorClient.from_environment().connect()
    hard_deadline = time.monotonic() + 2.0
    arm_error = client.arm(hard_deadline)
    if arm_error is not None:
        raise AssertionError(arm_error)
    record_path.write_text(
        json.dumps(
            {
                "gui_pid": os.getpid(),
                "hard_deadline": hard_deadline,
                "arm_acknowledged": client.arm_acknowledged,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    raise SystemExit(17)


if __name__ == "__main__":
    main()
'''


FORCED_SUPERVISED_CHILD_CODE = r'''\
import ctypes
import json
import os
import sys
import time
from pathlib import Path

from qplot._shutdown_supervisor import ShutdownSupervisorClient
from qplot.datahandling.trusted_live_supervisor import TrustedLiveReaderSupervisor


def hold_python_gil():
    if os.name == "nt":
        sleep = ctypes.PyDLL("kernel32", use_last_error=True).Sleep
        sleep.argtypes = (ctypes.c_ulong,)
        sleep.restype = None
        sleep(30_000)
        return
    sleep = ctypes.PyDLL(None).sleep
    sleep.argtypes = (ctypes.c_uint,)
    sleep.restype = ctypes.c_uint
    sleep(30)


def main():
    record_path = Path(sys.argv[1])
    database_path = Path(sys.argv[2])
    client = ShutdownSupervisorClient.from_environment().connect()
    reader_supervisor = TrustedLiveReaderSupervisor.open(
        database_path,
        shutdown_timeout_seconds=0.25,
        _test_fault="hang_before_operation",
    )
    helper_pid = reader_supervisor.helper_pid
    if helper_pid is None:
        raise AssertionError("installed stuck reader helper has no PID")
    reader_supervisor.submit_query("SELECT 1", timeout=20.0)
    reader_supervisor._wait_for_test_notification(b"operation_started", 10.0)
    reader_supervisor._wait_for_test_notification(b"operation_hang", 10.0)

    hard_deadline = time.monotonic() + 0.65
    arm_error = client.arm(hard_deadline)
    if arm_error is not None:
        raise AssertionError(arm_error)
    record_path.write_text(
        json.dumps(
            {
                "gui_pid": os.getpid(),
                "helper_pid": helper_pid,
                "hard_deadline": hard_deadline,
                "arm_acknowledged": client.arm_acknowledged,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    hold_python_gil()
    raise AssertionError("external launcher did not kill the GIL-holding GUI")


if __name__ == "__main__":
    main()
'''


SIGNALLED_SUPERVISED_CHILD_CODE = r'''\
import os
import signal
import sys
from pathlib import Path

from qplot._shutdown_supervisor import ShutdownSupervisorClient


ShutdownSupervisorClient.from_environment().connect()
Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
signal.signal(signal.SIGTERM, signal.SIG_DFL)
os.kill(os.getpid(), signal.SIGTERM)
raise AssertionError("installed GUI SIGTERM was not delivered")
'''


INTERRUPTED_PUBLIC_CHILD_CODE = r'''\
import ctypes
import json
import os
import sys
from pathlib import Path

from qplot._shutdown_supervisor import ShutdownSupervisorClient
from qplot.datahandling.trusted_live_supervisor import TrustedLiveReaderSupervisor


def hold_python_gil():
    if os.name == "nt":
        sleep = ctypes.PyDLL("kernel32", use_last_error=True).Sleep
        sleep.argtypes = (ctypes.c_ulong,)
        sleep.restype = None
        sleep(30_000)
        return
    sleep = ctypes.PyDLL(None).sleep
    sleep.argtypes = (ctypes.c_uint,)
    sleep.restype = ctypes.c_uint
    sleep(30)


def main():
    ShutdownSupervisorClient.from_environment().connect()
    record_path = Path(sys.argv[1])
    database_path = Path(sys.argv[2])
    reader = TrustedLiveReaderSupervisor.open(
        database_path,
        reply_timeout_seconds=20.0,
        shutdown_timeout_seconds=20.0,
        terminate_timeout_seconds=20.0,
        kill_timeout_seconds=20.0,
        _test_fault="hang_before_operation",
    )
    helper_pid = reader.helper_pid
    if helper_pid is None:
        raise AssertionError("installed interrupted helper has no PID")
    reader.submit_query("SELECT 1", timeout=20.0)
    reader._wait_for_test_notification(b"operation_started", 10.0)
    reader._wait_for_test_notification(b"operation_hang", 10.0)
    record_path.write_text(
        json.dumps(
            {"gui_pid": os.getpid(), "helper_pid": helper_pid}, sort_keys=True
        ),
        encoding="utf-8",
    )
    hold_python_gil()
    raise AssertionError("installed interrupted GUI returned")


if __name__ == "__main__":
    main()
'''


VANISHING_API_CALLER_CODE = r'''\
import os
import sys
from pathlib import Path

import qplot
from qplot import _shutdown_supervisor as shutdown_supervisor


child_script = Path(sys.argv[1])
child_record = Path(sys.argv[2])
database_path = Path(sys.argv[3])
launcher_record = Path(sys.argv[4])
original_spawn = shutdown_supervisor._spawn_public_api_launcher
shutdown_supervisor._public_api_gui_child_argv = lambda _argv: [
    sys.executable,
    "-I",
    "-u",
    str(child_script),
    str(child_record),
    str(database_path),
]


def capture_spawn(argv, environment):
    launcher = original_spawn(argv, environment)
    launcher_record.write_text(str(launcher.pid), encoding="utf-8")
    return launcher


shutdown_supervisor._spawn_public_api_launcher = capture_spawn
qplot.run(database_path=database_path)
raise AssertionError("vanishing installed API caller unexpectedly returned")
'''


ABRUPT_API_LAUNCHER_CODE = r'''\
import os

from qplot import _shutdown_supervisor as shutdown_supervisor


bootstrap = shutdown_supervisor._api_launcher_bootstrap_from_environment()
shutdown_supervisor._connect_public_api_result_channel(bootstrap)
os._exit(23)
'''


SENTINEL_CODE = r'''\
import json
import sys

print(json.dumps({"ready": True}), flush=True)
command = sys.stdin.readline().strip()
if command != "close":
    raise AssertionError(f"unexpected sentinel command: {command!r}")
print(json.dumps({"closed": True}), flush=True)
'''


def protected_artifact_state(database_path):
    paths = [
        str(database_path),
        f"{database_path}-wal",
        f"{database_path}-shm",
        f"{database_path}-journal",
    ]
    completed = subprocess.run(
        [sys.executable, "-I", "-c", ARTIFACT_AUDIT_CODE, json.dumps(paths)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def assert_source_policy(before, after, database_path):
    for suffix in ("", "-wal", "-journal"):
        path = f"{database_path}{suffix}"
        assert after[path] == before[path], (path, before[path], after[path])

    shm_path = f"{database_path}-shm"
    before_shm = before[shm_path]
    after_shm = after[shm_path]
    assert before_shm is not None and after_shm is not None
    assert stat.S_ISREG(after_shm["st_mode"])
    for field in ("st_dev", "st_ino", "st_nlink", "st_uid", "st_gid"):
        assert after_shm[field] == before_shm[field], (
            field,
            before_shm,
            after_shm,
        )


def process_is_running(pid):
    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return wait_for_single_object(handle, 0) == 0x00000102
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_process_exit(pid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while process_is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not process_is_running(pid), f"process {pid} remained alive"


def run_installed_supervised_child(script_path, *arguments, timeout=20.0):
    started_at = time.monotonic()
    launcher = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-u",
            "-c",
            LAUNCHER_DRIVER_CODE,
            str(script_path),
            *(str(argument) for argument in arguments),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = launcher.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        launcher.kill()
        stdout, stderr = launcher.communicate()
        raise AssertionError(
            "installed shutdown launcher did not terminate boundedly: "
            f"stdout={stdout!r}, stderr={stderr!r}"
        ) from error
    completed_at = time.monotonic()
    assert launcher.returncode is not None
    assert not process_is_running(launcher.pid)
    return {
        "returncode": launcher.returncode,
        "launcher_pid": launcher.pid,
        "stdout": stdout,
        "stderr": stderr,
        "elapsed": completed_at - started_at,
        "completed_at": completed_at,
    }


def run_installed_public_api_child(
    script_path,
    *arguments,
    database_path=None,
    foreign_reaper=False,
):
    original_child_argv = shutdown_supervisor._public_api_gui_child_argv
    original_spawn = shutdown_supervisor._spawn_public_api_launcher
    original_wait = shutdown_supervisor._wait_for_public_api_launcher_exit
    launchers = []
    reaped = {}
    reaped_lock = threading.Lock()
    stop_reaper = threading.Event()
    wait_gate_entered = threading.Event()

    def installed_child_argv(_preserved_argv):
        return [
            sys.executable,
            "-I",
            "-u",
            str(script_path),
            *(str(argument) for argument in arguments),
        ]

    def capture_spawn(argv, environment):
        child = original_spawn(argv, environment)
        launchers.append(child)
        return child

    def reap_every_child():
        while not stop_reaper.is_set():
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                time.sleep(0.001)
                continue
            if pid == 0:
                time.sleep(0.001)
                continue
            with reaped_lock:
                reaped[pid] = status

    def require_foreign_reap(child):
        wait_gate_entered.set()
        deadline = time.monotonic() + 5.0
        while True:
            with reaped_lock:
                launcher_was_reaped = child.pid in reaped
            if launcher_was_reaped:
                break
            if time.monotonic() >= deadline:
                raise AssertionError(
                    "installed foreign reaper did not collect the API launcher"
                )
            time.sleep(0.001)
        original_wait(child)

    reaper = None
    shutdown_supervisor._public_api_gui_child_argv = installed_child_argv
    shutdown_supervisor._spawn_public_api_launcher = capture_spawn
    if foreign_reaper:
        if os.name == "nt":
            raise AssertionError("waitpid foreign reaper is POSIX-only")
        shutdown_supervisor._wait_for_public_api_launcher_exit = (
            require_foreign_reap
        )
        reaper = threading.Thread(target=reap_every_child, daemon=True)
        reaper.start()
    started_at = time.monotonic()
    try:
        return_code = qplot.run(database_path=database_path)
    finally:
        completed_at = time.monotonic()
        stop_reaper.set()
        if reaper is not None:
            reaper.join(timeout=2.0)
        shutdown_supervisor._public_api_gui_child_argv = original_child_argv
        shutdown_supervisor._spawn_public_api_launcher = original_spawn
        shutdown_supervisor._wait_for_public_api_launcher_exit = original_wait
    assert len(launchers) == 1, launchers
    launcher = launchers[0]
    assert launcher.returncode is not None
    assert not process_is_running(launcher.pid)
    if foreign_reaper:
        with reaped_lock:
            assert launcher.pid in reaped
        assert wait_gate_entered.is_set()
    return {
        "returncode": return_code,
        "launcher_pid": launcher.pid,
        "elapsed": completed_at - started_at,
        "completed_at": completed_at,
    }


def run_installed_public_api_interruption(
    script_path,
    record_path,
    database_path,
):
    original_child_argv = shutdown_supervisor._public_api_gui_child_argv
    original_spawn = shutdown_supervisor._spawn_public_api_launcher
    original_wait = shutdown_supervisor._wait_for_public_api_result_completion
    original_guard_boundary = shutdown_supervisor._public_api_interrupt_guard_boundary
    launchers = []
    exact_first = SystemExit(37)
    exact_second = KeyboardInterrupt(
        "installed second interrupt after SIGINT guard installation"
    )
    first_injected = False
    second_injected = False
    prior_sigint_handler = signal.getsignal(signal.SIGINT)
    followup_sigints = []

    def custom_sigint_handler(signum, _frame):
        followup_sigints.append(signum)

    def installed_child_argv(_preserved_argv):
        return [
            sys.executable,
            "-I",
            "-u",
            str(script_path),
            str(record_path),
            str(database_path),
        ]

    def capture_spawn(argv, environment):
        launcher = original_spawn(argv, environment)
        launchers.append(launcher)
        return launcher

    def interrupt_result_wait(completed):
        nonlocal first_injected
        if record_path.exists() and not first_injected:
            first_injected = True
            raise exact_first
        original_wait(completed)

    def interrupt_guard_after_install(name):
        nonlocal second_injected
        if (
            first_injected
            and name == "installation_signal_after"
            and not second_injected
        ):
            second_injected = True
            raise exact_second
        return original_guard_boundary(name)

    signal.signal(signal.SIGINT, custom_sigint_handler)
    shutdown_supervisor._public_api_gui_child_argv = installed_child_argv
    shutdown_supervisor._spawn_public_api_launcher = capture_spawn
    shutdown_supervisor._wait_for_public_api_result_completion = (
        interrupt_result_wait
    )
    shutdown_supervisor._public_api_interrupt_guard_boundary = (
        interrupt_guard_after_install
    )
    caught = None
    guard_restored = False
    followup_delivered = False
    try:
        qplot.run(database_path=database_path)
    except BaseException as error:
        caught = error
    finally:
        guard_restored = (
            signal.getsignal(signal.SIGINT) is custom_sigint_handler
        )
        if guard_restored:
            before_followup = len(followup_sigints)
            signal.raise_signal(signal.SIGINT)
            followup_delivered = len(followup_sigints) == before_followup + 1
        shutdown_supervisor._public_api_gui_child_argv = original_child_argv
        shutdown_supervisor._spawn_public_api_launcher = original_spawn
        shutdown_supervisor._wait_for_public_api_result_completion = original_wait
        shutdown_supervisor._public_api_interrupt_guard_boundary = (
            original_guard_boundary
        )
        signal.signal(signal.SIGINT, prior_sigint_handler)
    assert caught is exact_first, caught
    assert caught.code == 37
    assert first_injected
    assert second_injected
    assert guard_restored
    assert followup_delivered
    assert len(launchers) == 1, launchers
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert not process_is_running(launchers[0].pid)
    assert not process_is_running(record["gui_pid"])
    assert not process_is_running(record["helper_pid"])
    return {
        "launcher_pid": launchers[0].pid,
        "gui_pid": record["gui_pid"],
        "helper_pid": record["helper_pid"],
    }


def assert_installed_concurrent_cancellation_sender():
    caller_channel, launcher_channel = socket.socketpair()
    launcher_channel.settimeout(2.0)
    frame = bytes(range(shutdown_supervisor._FRAME_SIZE))
    sender = shutdown_supervisor._ApiLauncherCancellationSender(
        caller_channel,
        frame,
    )
    original_boundary = shutdown_supervisor._public_api_cancellation_boundary
    original_factory = shutdown_supervisor._new_public_api_cancellation_worker
    original_send = shutdown_supervisor._public_api_cancellation_send
    lookup_barrier = threading.Barrier(2)
    lookup_lock = threading.Lock()
    lookup_threads = set()
    phase_barriers = {
        name: threading.Barrier(2)
        for name in ("worker_creation", "worker_assignment", "worker_start")
    }
    created_workers = []
    start_calls = []
    send_threads = []
    sent_bytes = bytearray()
    completion_events = []
    completions = []

    def synchronize_startup(name):
        if name == "worker_lookup":
            thread_id = threading.get_ident()
            with lookup_lock:
                first_lookup = thread_id not in lookup_threads
                lookup_threads.add(thread_id)
            if first_lookup:
                lookup_barrier.wait(timeout=2.0)
        elif name in phase_barriers:
            phase_barriers[name].wait(timeout=2.0)
        return original_boundary(name)

    class InstalledObservedWorker(threading.Thread):
        def start(self):
            start_calls.append(threading.get_ident())
            return super().start()

    def create_worker(owner):
        worker = InstalledObservedWorker(
            target=owner._run,
            name="qplot-public-api-cancellation-sender",
            daemon=True,
        )
        created_workers.append(worker)
        return worker

    def observe_send(channel, data):
        written = original_send(channel, data)
        send_threads.append(threading.get_ident())
        sent_bytes.extend(data[:written])
        return written

    def requester():
        sender.request()
        completion_events.append(id(sender.completed))
        completions.append(sender.completed.wait(2.0))

    def release_phases():
        for barrier in phase_barriers.values():
            barrier.wait(timeout=2.0)

    shutdown_supervisor._public_api_cancellation_boundary = synchronize_startup
    shutdown_supervisor._new_public_api_cancellation_worker = create_worker
    shutdown_supervisor._public_api_cancellation_send = observe_send
    coordinator = threading.Thread(target=release_phases, daemon=True)
    result_reader = threading.Thread(
        target=requester,
        name="qplot-public-api-result-reader",
    )
    try:
        coordinator.start()
        result_reader.start()
        requester()
        result_reader.join(timeout=2.0)
        coordinator.join(timeout=2.0)
        assert not result_reader.is_alive()
        assert not coordinator.is_alive()
        assert sender.completed.is_set()
        assert len(created_workers) == 1
        assert len(start_calls) == 1
        assert len(set(send_threads)) == 1
        assert len(set(completion_events)) == 1
        assert completions == [True, True]
        assert bytes(sent_bytes) == frame
        assert launcher_channel.recv(len(frame)) == frame
        launcher_channel.settimeout(0.05)
        try:
            duplicate = launcher_channel.recv(1)
        except TimeoutError:
            duplicate = None
        assert duplicate is None
    finally:
        shutdown_supervisor._public_api_cancellation_boundary = original_boundary
        shutdown_supervisor._new_public_api_cancellation_worker = original_factory
        shutdown_supervisor._public_api_cancellation_send = original_send
        caller_channel.close()
        launcher_channel.close()


def assert_installed_cancellation_owner_loss():
    original_factory = shutdown_supervisor._new_public_api_cancellation_worker
    original_send = shutdown_supervisor._public_api_cancellation_send
    original_shutdown = shutdown_supervisor._public_api_cancellation_shutdown
    for interrupted_commit in (
        "starting_state",
        "worker_assignment",
        "start_attempt",
    ):
        caller_channel, launcher_channel = socket.socketpair()
        launcher_channel.settimeout(2.0)
        frame = bytes(range(shutdown_supervisor._FRAME_SIZE))

        class InstalledInterruptedSender(
            shutdown_supervisor._ApiLauncherCancellationSender
        ):
            injection_armed = False
            injected = False

            def __setattr__(self, name, value):
                super().__setattr__(name, value)
                if not self.injection_armed or self.injected:
                    return
                matching_commit = (
                    interrupted_commit == "starting_state"
                    and name == "_worker_state"
                    and value is shutdown_supervisor._CancellationWorkerState.STARTING
                ) or (
                    interrupted_commit == "worker_assignment"
                    and name == "_thread"
                    and value is not None
                ) or (
                    interrupted_commit == "start_attempt"
                    and name == "_start_attempted"
                    and value is True
                )
                if matching_commit:
                    self.injected = True
                    raise KeyboardInterrupt(
                        f"installed interruption after {interrupted_commit} commit"
                    )

        sender = InstalledInterruptedSender(caller_channel, frame)
        sender.injection_armed = True
        created_workers = []
        start_calls = []
        send_threads = []
        sent_bytes = bytearray()
        shutdown_threads = []
        completion_ids = []
        completion_results = []
        requester_barrier = threading.Barrier(3)

        class InstalledOwnerWorker(threading.Thread):
            def start(self):
                start_calls.append(threading.get_ident())
                return super().start()

        def create_worker(owner):
            worker = InstalledOwnerWorker(
                target=owner._run,
                name="qplot-public-api-cancellation-sender",
                daemon=True,
            )
            created_workers.append(worker)
            return worker

        def observe_send(channel, data):
            written = original_send(channel, data)
            send_threads.append(threading.get_ident())
            sent_bytes.extend(data[:written])
            return written

        def observe_shutdown(channel):
            shutdown_threads.append(threading.get_ident())
            return original_shutdown(channel)

        def requester():
            requester_barrier.wait(timeout=2.0)
            sender.request()
            completion_ids.append(id(sender.completed))
            completion_results.append(sender.completed.wait(2.0))

        shutdown_supervisor._new_public_api_cancellation_worker = create_worker
        shutdown_supervisor._public_api_cancellation_send = observe_send
        shutdown_supervisor._public_api_cancellation_shutdown = observe_shutdown
        requesters = [
            threading.Thread(target=requester, daemon=True)
            for _index in range(2)
        ]
        try:
            for requester_thread in requesters:
                requester_thread.start()
            requester_barrier.wait(timeout=2.0)
            for requester_thread in requesters:
                requester_thread.join(timeout=2.0)
            assert sender.injected
            assert not any(thread.is_alive() for thread in requesters)
            assert completion_results == [True, True]
            assert len(set(completion_ids)) == 1
            assert sender.completed.is_set()
            assert len(created_workers) <= 1
            assert len(start_calls) <= 1
            for worker in created_workers:
                if worker.ident is not None:
                    worker.join(timeout=2.0)
                assert not worker.is_alive()
            if interrupted_commit == "start_attempt":
                assert launcher_channel.recv(len(frame)) == b""
                assert not send_threads
                assert len(shutdown_threads) == 1
            else:
                assert launcher_channel.recv(len(frame)) == frame
                assert bytes(sent_bytes) == frame
                assert len(set(send_threads)) == 1
                assert not shutdown_threads
                launcher_channel.settimeout(0.05)
                try:
                    duplicate = launcher_channel.recv(1)
                except TimeoutError:
                    duplicate = None
                assert duplicate is None
            assert sender.diagnostic is not None
            assert (
                f"installed interruption after {interrupted_commit} commit"
                in sender.diagnostic
            )
            sender.request()
            assert not any(
                thread.name == "qplot-public-api-cancellation-sender"
                for thread in threading.enumerate()
            )
        finally:
            shutdown_supervisor._new_public_api_cancellation_worker = (
                original_factory
            )
            shutdown_supervisor._public_api_cancellation_send = original_send
            shutdown_supervisor._public_api_cancellation_shutdown = (
                original_shutdown
            )
            caller_channel.close()
            launcher_channel.close()


def exercise_installed_public_api_caller_eof(
    script_path,
    record_path,
    database_path,
    launcher_record_path,
):
    caller = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-u",
            "-c",
            VANISHING_API_CALLER_CODE,
            str(script_path),
            str(record_path),
            str(database_path),
            str(launcher_record_path),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10.0
        while True:
            # Creating a readiness file does not publish its contents atomically.
            # Wait for both complete records before killing the public caller.
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                launcher_pid = int(launcher_record_path.read_text(encoding="utf-8"))
                if not isinstance(record, dict) or not all(
                    type(record.get(name)) is int and record[name] > 0
                    for name in ("gui_pid", "helper_pid")
                ) or launcher_pid <= 0:
                    raise ValueError("incomplete installed caller readiness")
            except (FileNotFoundError, ValueError):
                pass
            else:
                break
            if caller.poll() is not None:
                stdout, stderr = caller.communicate()
                raise AssertionError(
                    "installed vanishing API caller exited before readiness: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError("installed caller-EOF tree did not become ready")
            time.sleep(0.01)
        caller.kill()
        caller.wait(timeout=5.0)
        wait_for_process_exit(launcher_pid)
        wait_for_process_exit(record["gui_pid"])
        wait_for_process_exit(record["helper_pid"])
    finally:
        if caller.poll() is None:
            caller.kill()
            caller.wait(timeout=5.0)


def assert_installed_qplot_entrypoint_delegation():
    import qplot.__main__ as qplot_entrypoint

    captured = []
    original_launch_gui = shutdown_supervisor.launch_gui
    original_argv = list(sys.argv)

    def capture_launch(original_argv=None, *, database_path=None):
        captured.append((list(original_argv), database_path))
        return 17

    shutdown_supervisor.launch_gui = capture_launch
    sys.argv[:] = ["installed-qplot", "database path.db", "--platform", "offscreen"]
    try:
        assert qplot_entrypoint.run(database_path="explicit installed path.db") == 17
    finally:
        sys.argv[:] = original_argv
        shutdown_supervisor.launch_gui = original_launch_gui
    assert captured == [
        (
            ["installed-qplot", "database path.db", "--platform", "offscreen"],
            "explicit installed path.db",
        )
    ]


def exercise_installed_shutdown_supervision(database_path, writer, temporary):
    temporary = Path(temporary)
    normal_script = temporary / "installed-normal-supervised-child.py"
    forced_script = temporary / "installed-forced-supervised-child.py"
    normal_record_path = temporary / "installed-normal-supervision.json"
    forced_record_path = temporary / "installed-forced-supervision.json"
    normal_script.write_text(NORMAL_SUPERVISED_CHILD_CODE, encoding="utf-8")
    forced_script.write_text(FORCED_SUPERVISED_CHILD_CODE, encoding="utf-8")

    normal_result = run_installed_supervised_child(
        normal_script,
        normal_record_path,
    )
    assert normal_result["returncode"] == 17, normal_result
    assert normal_record_path.is_file(), normal_result
    normal_record = json.loads(normal_record_path.read_text(encoding="utf-8"))
    assert normal_record["arm_acknowledged"] is True
    assert normal_result["completed_at"] < normal_record["hard_deadline"]
    assert not process_is_running(normal_record["gui_pid"]), (
        "normally reaped installed GUI remained alive"
    )

    sentinel = subprocess.Popen(
        [sys.executable, "-I", "-u", "-c", SENTINEL_CODE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    forced_record = None
    try:
        assert sentinel.stdout is not None
        ready_line = sentinel.stdout.readline()
        if not ready_line:
            assert sentinel.stderr is not None
            raise AssertionError(
                f"installed shutdown sentinel did not start: {sentinel.stderr.read()}"
            )
        assert json.loads(ready_line) == {"ready": True}
        before_forced_shutdown = protected_artifact_state(database_path)

        forced_result = run_installed_supervised_child(
            forced_script,
            forced_record_path,
            database_path,
        )
        assert forced_result["returncode"] == 70, forced_result
        assert forced_record_path.is_file(), forced_result
        forced_record = json.loads(forced_record_path.read_text(encoding="utf-8"))
        assert forced_record["arm_acknowledged"] is True
        assert (
            forced_record["hard_deadline"] - 0.03
            <= forced_result["completed_at"]
            < forced_record["hard_deadline"] + 0.75
        ), forced_result
        # The launcher is already complete here.  Do not poll away a leaked
        # descendant: installed acceptance requires the GUI and its real stuck
        # trusted-reader helper to be absent at that exact observation point.
        assert not process_is_running(forced_record["gui_pid"]), (
            "forced installed GUI remained alive after launcher completion"
        )
        assert not process_is_running(forced_record["helper_pid"]), (
            "stuck installed trusted-reader helper outlived launcher completion"
        )
        assert writer.poll() is None, "external WAL writer was terminated"
        assert sentinel.poll() is None, "external sentinel was terminated"

        after_forced_shutdown = protected_artifact_state(database_path)
        assert_source_policy(
            before_forced_shutdown,
            after_forced_shutdown,
            database_path,
        )
    finally:
        # Cleanup polling is deliberately after the immediate assertions above;
        # it must never turn delayed orphan exit into an acceptance pass.
        if forced_record is not None:
            wait_for_process_exit(forced_record["gui_pid"])
            wait_for_process_exit(forced_record["helper_pid"])
        if sentinel.poll() is None:
            assert sentinel.stdin is not None
            sentinel.stdin.write("close\\n")
            sentinel.stdin.flush()
        sentinel_stdout, sentinel_stderr = sentinel.communicate(timeout=10.0)
        assert sentinel.returncode == 0, sentinel_stderr
        if sentinel_stdout:
            assert json.loads(sentinel_stdout.splitlines()[-1]) == {"closed": True}


def exercise_installed_public_api_boundary(temporary):
    temporary = Path(temporary).resolve()
    normal_script = temporary / "installed-public-normal.py"
    forced_script = temporary / "installed-public-forced.py"
    signal_script = temporary / "installed-public-signal.py"
    interrupted_script = temporary / "installed-public-interrupted.py"
    normal_record_path = temporary / "installed-public-normal.json"
    forced_record_path = temporary / "installed-public-forced.json"
    signal_pid_path = temporary / "installed-public-signal.pid"
    interrupted_record_path = temporary / "installed-public-interrupted.json"
    eof_record_path = temporary / "installed-public-caller-eof.json"
    eof_launcher_path = temporary / "installed-public-caller-eof-launcher.pid"
    database_path = temporary / "installed-public-writer.db"
    normal_script.write_text(NORMAL_SUPERVISED_CHILD_CODE, encoding="utf-8")
    forced_script.write_text(FORCED_SUPERVISED_CHILD_CODE, encoding="utf-8")
    signal_script.write_text(SIGNALLED_SUPERVISED_CHILD_CODE, encoding="utf-8")
    interrupted_script.write_text(
        INTERRUPTED_PUBLIC_CHILD_CODE,
        encoding="utf-8",
    )

    normal_result = run_installed_public_api_child(
        normal_script,
        normal_record_path,
        foreign_reaper=os.name != "nt",
    )
    normal_record = json.loads(normal_record_path.read_text(encoding="utf-8"))
    assert normal_result["returncode"] == 17, normal_result
    assert normal_record["arm_acknowledged"] is True
    assert normal_result["completed_at"] < normal_record["hard_deadline"]
    assert not process_is_running(normal_record["gui_pid"])

    if os.name != "nt":
        signal_result = run_installed_public_api_child(
            signal_script,
            signal_pid_path,
        )
        signal_pid = int(signal_pid_path.read_text(encoding="utf-8"))
        assert signal_result["returncode"] == -signal.SIGTERM, signal_result
        assert not process_is_running(signal_pid)

    original_spawn = shutdown_supervisor._spawn_public_api_launcher
    original_report = shutdown_supervisor._report_launcher_failure
    abrupt_launchers = []
    eof_diagnostics = []

    def spawn_abrupt_launcher(_argv, environment):
        child = subprocess.Popen(
            [sys.executable, "-I", "-u", "-c", ABRUPT_API_LAUNCHER_CODE],
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        abrupt_launchers.append(child)
        return child

    shutdown_supervisor._spawn_public_api_launcher = spawn_abrupt_launcher
    shutdown_supervisor._report_launcher_failure = eof_diagnostics.append
    try:
        assert qplot.run() == 70
    finally:
        shutdown_supervisor._spawn_public_api_launcher = original_spawn
        shutdown_supervisor._report_launcher_failure = original_report
    assert len(abrupt_launchers) == 1
    assert abrupt_launchers[0].returncode == 23
    assert not process_is_running(abrupt_launchers[0].pid)
    assert any(
        "public-API launcher result channel closed before an outcome" in detail
        for detail in eof_diagnostics
    ), eof_diagnostics

    writer = apsw.Connection(str(database_path))
    journal_mode = writer.execute("PRAGMA journal_mode=WAL").fetchone()
    assert journal_mode is not None
    assert str(journal_mode[0]).casefold() == "wal"
    writer.execute("PRAGMA wal_autocheckpoint=0")
    with writer:
        writer.execute(
            "CREATE TABLE acquisition_writer ("
            "seq INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        writer.execute(
            "INSERT INTO acquisition_writer VALUES(1, 'before qplot.run')"
        )
    sentinel = subprocess.Popen(
        [sys.executable, "-I", "-u", "-c", SENTINEL_CODE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    forced_record = None
    try:
        assert sentinel.stdout is not None
        sentinel_ready = sentinel.stdout.readline()
        if not sentinel_ready:
            assert sentinel.stderr is not None
            raise AssertionError(
                "installed public-API sentinel did not start: "
                f"{sentinel.stderr.read()}"
            )
        assert json.loads(sentinel_ready) == {"ready": True}
        before = protected_artifact_state(database_path)
        forced_result = run_installed_public_api_child(
            forced_script,
            forced_record_path,
            database_path,
            database_path=database_path,
        )
        assert forced_result["returncode"] == 70, forced_result
        forced_record = json.loads(
            forced_record_path.read_text(encoding="utf-8")
        )
        assert forced_record["arm_acknowledged"] is True
        assert (
            forced_record["hard_deadline"] - 0.03
            <= forced_result["completed_at"]
            < forced_record["hard_deadline"] + 0.75
        ), forced_result
        assert not process_is_running(forced_record["gui_pid"])
        assert not process_is_running(forced_record["helper_pid"])
        assert sentinel.poll() is None
        after = protected_artifact_state(database_path)
        assert_source_policy(before, after, database_path)

        # This write is deliberately after the protected-artifact audit.  It
        # proves qplot.run returned normally to the still-live acquisition
        # process and its physically writable connection.
        with writer:
            writer.execute(
                "INSERT INTO acquisition_writer VALUES(2, 'after qplot.run')"
            )
        assert writer.execute(
            "SELECT COUNT(*) FROM acquisition_writer"
        ).fetchone() == (2,)

        before_interruption = protected_artifact_state(database_path)
        run_installed_public_api_interruption(
            interrupted_script,
            interrupted_record_path,
            database_path,
        )
        assert sentinel.poll() is None
        after_interruption = protected_artifact_state(database_path)
        assert_source_policy(
            before_interruption,
            after_interruption,
            database_path,
        )
        with writer:
            writer.execute(
                "INSERT INTO acquisition_writer "
                "VALUES(3, 'after interrupted qplot.run')"
            )

        before_caller_eof = protected_artifact_state(database_path)
        exercise_installed_public_api_caller_eof(
            interrupted_script,
            eof_record_path,
            database_path,
            eof_launcher_path,
        )
        assert sentinel.poll() is None
        after_caller_eof = protected_artifact_state(database_path)
        assert_source_policy(
            before_caller_eof,
            after_caller_eof,
            database_path,
        )
        with writer:
            writer.execute(
                "INSERT INTO acquisition_writer "
                "VALUES(4, 'after caller EOF cleanup')"
            )
        assert writer.execute(
            "SELECT COUNT(*) FROM acquisition_writer"
        ).fetchone() == (4,)
    finally:
        if forced_record is not None:
            wait_for_process_exit(forced_record["gui_pid"])
            wait_for_process_exit(forced_record["helper_pid"])
        writer.close(True)
        if sentinel.poll() is None:
            assert sentinel.stdin is not None
            sentinel.stdin.write("close\\n")
            sentinel.stdin.flush()
        sentinel_stdout, sentinel_stderr = sentinel.communicate(timeout=10.0)
        assert sentinel.returncode == 0, sentinel_stderr
        if sentinel_stdout:
            assert json.loads(sentinel_stdout.splitlines()[-1]) == {"closed": True}


def assert_installed_package():
    expected_version = sys.argv[1]
    resource_paths = json.loads(sys.argv[2])
    expected_scripts = json.loads(sys.argv[3])
    expected_apsw_version = sys.argv[4]
    native_module_name = sys.argv[5]
    native_file_names = set(json.loads(sys.argv[6]))
    assert qplot.__version__ == expected_version == version("qplotter")
    assert shutdown_supervisor.ShutdownSupervisorClient.__module__ == (
        "qplot._shutdown_supervisor"
    )
    assert TrustedMetadataQueryAdapter.__module__ == (
        "qplot.datahandling.trusted_live_queries"
    )
    assert TrustedLiveReadService.__module__ == (
        "qplot.datahandling.trusted_live_service"
    )
    assert TrustedSelectedRunPresentation.__module__ == (
        "qplot.datahandling.trusted_presentation"
    )
    assert TrustedSnapshotOmission.__module__ == (
        "qplot.datahandling.trusted_snapshot"
    )
    assert version("qplotter-native") == sys.argv[7]
    assert version("apsw") == expected_apsw_version
    assert apsw.apsw_version() == expected_apsw_version
    native_module = importlib.import_module(native_module_name)
    native_file = Path(native_module.__file__).resolve()
    assert native_file.is_file(), native_file
    assert native_file.name in native_file_names, native_file
    for resource_path in resource_paths:
        resource = files("qplot").joinpath(*resource_path.split("/"))
        assert resource.is_file(), resource_path
        assert resource.read_bytes(), resource_path
    scripts = {
        entry.name: entry.value
        for entry in distribution("qplotter").entry_points
        if entry.group == "console_scripts"
    }
    assert scripts == expected_scripts
    for entry in distribution("qplotter").entry_points:
        if entry.group == "console_scripts":
            assert callable(entry.load()), entry.name


def assert_repaired_bounded_views():
    description_prefix = '{"interdependencies_":{"payload":"'
    description_suffix = '"}}'
    run_description = (
        description_prefix
        + "r"
        * (
            1_258
            - len(description_prefix.encode("utf-8"))
            - len(description_suffix.encode("utf-8"))
        )
        + description_suffix
    )
    exception_prefix = "Traceback (most recent call last):\\n"
    exception_suffix = "\\nKeyboardInterrupt"
    measurement_exception = (
        exception_prefix
        + "x"
        * (
            1_255
            - len(exception_prefix.encode("utf-8"))
            - len(exception_suffix.encode("utf-8"))
        )
        + exception_suffix
    )
    assert len(run_description.encode("utf-8")) == 1_258
    assert len(measurement_exception.encode("utf-8")) == 1_255

    scalar_presentation = build_selected_run_presentation(
        run_fields={"run_id": 7, "run_description": run_description},
        metadata_fields={
            "measurement_exception": measurement_exception,
            "operator": "Ada",
        },
        parameters=(),
        snapshot_summary={"Status": "available"},
        setpoint_summaries=(),
        unavailable_fields=(),
    )
    assert isinstance(scalar_presentation, TrustedSelectedRunPresentation)
    assert scalar_presentation.metadata.status == "available"
    assert scalar_presentation.raw.status == "available"
    assert scalar_presentation.metadata.shortened_value_count == 1
    assert scalar_presentation.raw.shortened_value_count == 2
    for view in (scalar_presentation.metadata, scalar_presentation.raw):
        assert not any(node.key == "[truncated]" for node in view.nodes)
        assert any(node.key == "[display]" for node in view.nodes)
        assert len(view.nodes) <= TRUSTED_PRESENTATION_MAX_RENDERED_NODES
        assert view.rendered_text_bytes <= TRUSTED_PRESENTATION_MAX_RENDERED_TEXT_BYTES
        assert view.tooltip_text_bytes <= TRUSTED_PRESENTATION_MAX_TOOLTIP_TEXT_BYTES
        assert all(
            len(node.key.encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_KEY_BYTES
            and len(node.value.encode("utf-8"))
            <= TRUSTED_PRESENTATION_MAX_VALUE_BYTES
            and len(node.tooltip.encode("utf-8"))
            <= TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES
            and run_description not in node.value
            and measurement_exception not in node.value
            and run_description not in node.tooltip
            and measurement_exception not in node.tooltip
            for node in view.nodes
        )

    full_values = {
        value.identifier: value for value in scalar_presentation.full_values
    }
    assert len(full_values) == 2
    description_node = next(
        node
        for node in scalar_presentation.raw.nodes
        if node.path == "/Raw/Run/run_description"
    )
    measurement_exception_node = next(
        node
        for node in scalar_presentation.metadata.nodes
        if node.path == "/Metadata/measurement_exception"
    )
    assert description_node.value_shortened
    assert measurement_exception_node.value_shortened
    assert description_node.source_value_bytes == 1_258
    assert measurement_exception_node.source_value_bytes == 1_255
    assert "[view full]" in description_node.value
    assert "KeyboardInterrupt" in measurement_exception_node.value
    assert "activate" in description_node.tooltip.casefold()
    assert "activate" in measurement_exception_node.tooltip.casefold()
    assert full_values[description_node.full_value_id].text == run_description
    assert (
        full_values[measurement_exception_node.full_value_id].text
        == measurement_exception
    )

    nested = "bounded terminal value"
    for _depth in range(32):
        nested = {"child": nested}
    structural_presentation = build_selected_run_presentation(
        run_fields={"run_id": 8},
        metadata_fields={"nested": nested},
        parameters=(),
        snapshot_summary={"Status": "available"},
        setpoint_summaries=(),
        unavailable_fields=(),
    )
    assert structural_presentation.metadata.status == "truncated"
    assert structural_presentation.raw.status == "truncated"
    for view in (structural_presentation.metadata, structural_presentation.raw):
        assert any(node.key == "[truncated]" for node in view.nodes)
        assert len(view.nodes) <= TRUSTED_PRESENTATION_MAX_RENDERED_NODES
        assert view.rendered_text_bytes <= TRUSTED_PRESENTATION_MAX_RENDERED_TEXT_BYTES
        assert view.tooltip_text_bytes <= TRUSTED_PRESENTATION_MAX_TOOLTIP_TEXT_BYTES
        assert all(
            len(node.key.encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_KEY_BYTES
            and len(node.value.encode("utf-8"))
            <= TRUSTED_PRESENTATION_MAX_VALUE_BYTES
            and len(node.tooltip.encode("utf-8"))
            <= TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES
            for node in view.nodes
        )

    no_snapshot = normalize_trusted_snapshot(None)
    omitted_snapshot = normalize_trusted_snapshot(
        None,
        omission=TrustedSnapshotOmission(
            "payload_limit",
            TRUSTED_SNAPSHOT_MAX_INPUT_BYTES + 1,
        ),
    )
    assert no_snapshot.status == "empty"
    assert "No snapshot was stored" in no_snapshot.message
    assert isinstance(omitted_snapshot, TrustedSnapshotView)
    assert omitted_snapshot.status == "unavailable"
    assert "was stored" in omitted_snapshot.message
    assert "exceeds" in omitted_snapshot.message
    assert "No snapshot was stored" not in omitted_snapshot.message


def assert_stage4_run_detail(service, run_id):
    expensive = service.submit_expensive_run(run_id).wait(20.0)
    expensive_fields = expensive.as_dict()
    assert expensive.run_id == run_id
    assert expensive_fields["result_count"] == 2
    assert expensive_fields["point_shape"] == [2]
    assert expensive_fields["setpoint_shape"] == [2]
    assert expensive_fields["setpoint_shape_source"] == "planned"
    assert expensive_fields["storage_bytes"] > 0
    assert expensive_fields["storage_bytes_estimated"] is True

    selected_request = service.submit_selected_run(run_id)
    assert isinstance(
        selected_request.reprioritize(TrustedReadPriority.REMAINING_EXPENSIVE),
        bool,
    )
    selected_request.reprioritize(TrustedReadPriority.SELECTED_EXPENSIVE)
    selected = selected_request.wait(20.0)
    selected_fields = selected.run.as_dict()
    assert selected.run.run_id == run_id
    assert selected_fields["result_count"] == 2
    assert selected_fields["point_shape"] == [2]
    assert selected_fields["storage_bytes_estimated"] is True
    assert isinstance(selected.presentation, TrustedSelectedRunPresentation)
    assert dict(selected.presentation.run_fields)["run_id"] == run_id
    assert dict(selected.presentation.metadata_fields)["operator"] == (
        f"operator-{run_id}"
    )
    assert selected.presentation.metadata.nodes
    assert selected.presentation.raw.nodes
    assert isinstance(selected.snapshot, TrustedSnapshotView)
    assert selected.snapshot.status == "available"
    assert tuple(
        (node.key, node.value, node.parent_index)
        for node in selected.snapshot.nodes
    ) == (
        ("station", "", None),
    )
    station = selected.snapshot.nodes[0]
    assert station.container_handle is not None
    assert selected.snapshot.source is not None
    station_page = selected.snapshot.source.page(station.container_handle, None)
    assert station_page.continuation is None
    assert tuple(
        (node.key, node.value, node.parent_index) for node in station_page.nodes
    ) == (("run_id", str(run_id), None),)
    assert dict(selected.metadata)["operator"] == f"operator-{run_id}"
    assert tuple(parameter.name for parameter in selected.parameters) == (
        "setpoint",
        "signal",
    )
    summaries = {summary.name: summary for summary in selected.setpoint_summaries}
    assert set(summaries) == {"setpoint"}
    summary = summaries["setpoint"]
    assert summary.first == 0.0
    assert summary.last == 1.0
    assert summary.steps == 2


def exercise_stage5b_backend(service, record, bootstrap, database_directory):
    publications = []
    cache = TrustedDerivedDiskCache()
    assert cache.root == Path(trusted_derived_cache_root())
    assert cache.root != database_directory
    assert database_directory not in cache.root.parents
    revision = trusted_source_revision(
        record,
        bootstrap.data_version,
        namespace=service.source_revision_namespace,
        helper_incarnation=bootstrap.helper_incarnation,
    )
    fields = record.as_dict()
    coordinator = TrustedWorkCoordinator(
        service.database_instance,
        (TrustedDerivedRun(record.run_id, fields["guid"], revision),),
        service,
        cache=cache,
        on_publish=publications.append,
    )
    try:
        assert cache.enabled
        coordinator.select_run(0)
        coordinator.set_visible_range(0, 1)
        coordinator.start()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            coordinator.poll()
            snapshot = coordinator.snapshot()
            if snapshot.pending_count == 0 and not coordinator.active:
                break
            time.sleep(0.005)
        else:
            raise AssertionError("installed Stage 5B backend did not drain")
    finally:
        coordinator.close(timeout=20.0)
    assert [publication.key.kind for publication in publications] == list(
        TrustedWorkKind
    )
    assert all(
        publication.result["status"] in {"ok", "unsupported"}
        for publication in publications
    )
    assert all(
        dict(publication.result["source"])["result_watermark"] == 2
        for publication in publications
    )
    assert tuple(cache.root.glob("*.qdc"))
    assert not any(
        thread.name.startswith("qplot-trusted-derived")
        for thread in threading.enumerate()
    )


def exercise_stage5c_qt_bridge(service, record):
    from PyQt6 import QtCore, QtWidgets
    from qplot.datahandling import trusted_work_coordinator as coordinator_module
    from qplot.windows.main import MainWindow

    cache_root = Path(trusted_derived_cache_root())
    for artifact in cache_root.glob("*.qdc"):
        artifact.unlink()

    cache_gets = []
    cache_puts = []
    cache_type = coordinator_module.TrustedDerivedDiskCache

    class RecordingCache(cache_type):
        def get(self, *args, **kwargs):
            result = super().get(*args, **kwargs)
            cache_gets.append(result is not None)
            return result

        def put(self, *args, **kwargs):
            result = super().put(*args, **kwargs)
            cache_puts.append(result)
            return result

    coordinator_module.TrustedDerivedDiskCache = RecordingCache
    application = QtWidgets.QApplication.instance()
    if application is None:
        application = QtWidgets.QApplication(["qplot-stage5c-wheel-smoke"])
    window = MainWindow()
    window.startupDatabaseTimer.stop()
    window.monitor.stop()
    window.config.config["user_preference"]["confirm_close"] = False
    window.config.config["user_preference"]["confirm_close_all"] = False
    window.resize(800, 500)
    window.show()
    fields = record.as_dict()
    guid = str(fields["guid"])
    runs = {record.run_id: fields}
    bridge = window._trusted_derived_bridge

    def process_until(predicate, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            application.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError("installed Stage 5C Qt bridge did not settle")

    def bind_and_wait():
        window.RunList.setRuns(runs)
        window._loaded_database_instance = service.database_instance
        window._trusted_read_service = service
        window._selected_run_guid = guid
        window.selected_run_id = record.run_id
        bridge.bind_database(service.database_instance, runs, service)
        bridge.select_run(guid)
        process_until(
            lambda: (
                guid in bridge._metadata_by_guid
                and window.RunList.run_preview_is_ready(guid)
                and guid in window.infoBox.preview.cache
                and bridge.coordinator is not None
                and not bridge.coordinator.active
                and bridge.coordinator.snapshot().pending_count == 0
            )
        )

    def image_signature(previews):
        signature = []
        for preview in previews:
            image = preview.get("image")
            signature.append(
                (
                    preview.get("parameter"),
                    preview.get("title"),
                    bool(preview.get("unsupported")),
                    image.width() if image is not None else None,
                    image.height() if image is not None else None,
                )
            )
        return tuple(signature)

    def ui_snapshot():
        metadata = dict(bridge._metadata_by_guid[guid])
        run_fields = dict(metadata["run_fields"])
        return (
            run_fields["result_count"],
            tuple(parameter.name for parameter in bridge._parameters_by_guid[guid]),
            window.RunList.run_preview_is_ready(guid),
            image_signature(window.infoBox.preview.cache[guid]),
        )

    try:
        bind_and_wait()
        cache_miss_state = ui_snapshot()
        first_put_count = len(cache_puts)
        assert first_put_count >= 3
        assert not window.infoBox.preview._workers
        assert window._database_detail_worker is None
        assert window._database_expensive_detail_worker is None
        assert window._database_selected_run_worker is None

        coordinator_before_reselection = bridge.coordinator
        timers_before_reselection = tuple(bridge.findChildren(QtCore.QTimer))
        cached_preview_before_reselection = window.infoBox.preview.cache[guid]
        assert bridge.refresh_active_database(
            service.database_instance,
            runs,
            service,
        )
        assert bridge.coordinator is coordinator_before_reselection
        assert tuple(bridge.findChildren(QtCore.QTimer)) == timers_before_reselection
        assert len(timers_before_reselection) == 2
        assert window.infoBox.preview.cache[guid] is cached_preview_before_reselection
        assert window.infoBox.preview._trusted_derived_mode

        bridge.clear_database()
        process_until(lambda: not bridge.background_active())
        second_get_start = len(cache_gets)
        bind_and_wait()
        assert ui_snapshot() == cache_miss_state
        assert any(cache_gets[second_get_start:])
        assert len(cache_puts) == first_put_count

        prior_preview = window.infoBox.preview.cache[guid]
        bridge.update_preview_size(window.preview_size + 13)
        process_until(
            lambda: window.infoBox.preview.cache.get(guid) is not prior_preview
        )
        assert window.RunList.run_preview_is_ready(guid)
    finally:
        bridge.clear_database()
        process_until(lambda: not bridge.background_active())
        window._trusted_read_service = None
        window._loaded_database_instance = None
        window.infoBox.preview.shutdown()
        window.hide()
        window.deleteLater()
        application.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        application.processEvents()
        coordinator_module.TrustedDerivedDiskCache = cache_type

    assert not any(
        thread.name.startswith("qplot-trusted-derived")
        for thread in threading.enumerate()
    )


def exercise_stage5c_mixed_size_wal_priority(temporary):
    import qcodes
    from PyQt6 import QtCore, QtGui, QtWidgets
    from qcodes import Station
    from qcodes.dataset import (
        Measurement,
        initialise_or_create_database_at,
        load_or_create_experiment,
    )
    from qcodes.dataset.sqlite.database import connect
    from qcodes.parameters import ManualParameter
    from qplot.datahandling import trusted_work_coordinator as coordinator_module
    from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
    from qplot.windows import _database_actions as database_actions
    from qplot.windows import _trusted_derived_qt as bridge_module
    from qplot.windows import main as main_window
    from qplot.windows._widgets import details_tables
    from qplot.windows._widgets import treeWidgets as tree_widgets

    slow_count = 48
    fast_count = 257
    visible_count = 5_000
    remaining_count = 11_000
    database_directory = Path(temporary).resolve() / "mixed-size-wal"
    database_directory.mkdir()
    database_path = database_directory / "live.db"
    cache_root = Path(temporary).resolve() / "mixed-size-derived-cache"
    qplot_home = Path(temporary).resolve() / "mixed-size-qplot-home"
    qplot_home.mkdir()

    prior_qcodes_database = qcodes.config.core.db_location
    prior_default_path = main_window.config.default_path
    prior_default_file = main_window.config.default_file
    cache_type = coordinator_module.TrustedDerivedDiskCache
    coordinator_type = bridge_module.TrustedWorkCoordinator
    original_metadata = bridge_module.TrustedDerivedQtBridge._publish_metadata
    original_thumbnail = tree_widgets.RunList.set_run_previews
    original_show_error = main_window.MainWindow.show_error
    writer = None
    open_contexts = []
    window = None
    application = None
    normal_close_completed = False
    cleanup_errors = []

    class GatedCoordinator(coordinator_type):
        # Release production scheduling after selection and viewport settle.

        def __init__(self, *args, **kwargs):
            self._smoke_work_released = False
            super().__init__(*args, **kwargs)

        def _pump(self, *args, **kwargs):
            if self._smoke_work_released:
                super()._pump(*args, **kwargs)

        def release_smoke_work(self):
            self._smoke_work_released = True
            super()._pump()

    metadata_order = []
    thumbnail_previews = {}
    errors = []

    def record_metadata(bridge, publication, run_id, guid, payload):
        result = original_metadata(bridge, publication, run_id, guid, payload)
        exact_guid = str(guid or "")
        if (
            exact_guid in bridge._metadata_by_guid
            and exact_guid not in metadata_order
        ):
            metadata_order.append(exact_guid)
        return result

    def record_thumbnail(widget, guid, previews):
        result = original_thumbnail(widget, guid, previews)
        thumbnail_previews[str(guid or "")] = tuple(previews or ())
        return result

    def record_error(_window, title, message, details=None):
        errors.append((title, message, details))

    def process_until(predicate, timeout=90.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            application.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError(
            "installed mixed-size Stage 5C WAL condition did not settle: "
            f"errors={errors!r}, metadata_order={metadata_order!r}"
        )

    def checkpoint_until_idle(mode, timeout=5.0):
        deadline = time.monotonic() + timeout
        while True:
            checkpoint = writer.execute(
                f"PRAGMA wal_checkpoint({mode})"
            ).fetchone()
            assert checkpoint is not None
            if checkpoint[0] == 0:
                if mode == "TRUNCATE":
                    assert checkpoint == (0, 0, 0)
                return checkpoint
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"writer {mode} checkpoint remained busy: {checkpoint!r}"
                )
            application.processEvents()
            time.sleep(0.005)

    def valid_images(previews):
        return bool(previews) and all(
            isinstance(preview.get("image"), QtGui.QImage)
            and not preview["image"].isNull()
            and not preview.get("unsupported")
            for preview in previews
        )

    def bridge_complete(bridge, guid):
        return bool(
            guid in bridge._metadata_by_guid
            and window.RunList.run_preview_is_ready(guid)
            and guid in window.infoBox.preview.cache
        )

    def tree_items(tree):
        iterator = QtWidgets.QTreeWidgetItemIterator(tree)
        while iterator.value() is not None:
            yield iterator.value()
            iterator += 1

    def tree_item_for_path(tree, path):
        return next(
            item
            for item in tree_items(tree)
            if str(item.data(0, details_tables.FULL_VALUE_PATH_ROLE) or "") == path
        )

    def assert_selected_exact_values(prior_dialog=None):
        publication = bridge._selected_detail_publication
        assert publication is not None
        assert publication.run_guid == selected_guid
        detail = publication.detail
        assert detail.unavailable_fields == ()
        assert not detail.presentation.parameters_truncated
        assert detail.presentation.metadata.status == "available"
        assert detail.presentation.raw.status == "available"
        assert detail.presentation.metadata.shortened_value_count == 1
        assert detail.presentation.raw.shortened_value_count >= 2
        assert not any(
            item.text(0) == "[truncated]"
            for tree in (window.infoBox.metadata, window.infoBox.raw)
            for item in tree_items(tree)
        )

        metadata_summary = tree_item_for_path(
            window.infoBox.metadata,
            "/Metadata/[display]",
        )
        assert "1 value shortened for display" in metadata_summary.text(1)
        raw_summary = tree_item_for_path(window.infoBox.raw, "/Raw/[display]")
        assert (
            f"{detail.presentation.raw.shortened_value_count} values shortened "
            "for display"
            in raw_summary.text(1)
        )

        operator_item = tree_item_for_path(
            window.infoBox.metadata,
            "/Metadata/stage5c_operator",
        )
        assert operator_item.text(1) == selected_operator
        description_item = tree_item_for_path(
            window.infoBox.raw,
            "/Raw/Run/run_description",
        )
        exception_item = tree_item_for_path(
            window.infoBox.metadata,
            "/Metadata/measurement_exception",
        )
        assert "[view full]" in description_item.text(1)
        assert "KeyboardInterrupt" in exception_item.text(1)
        assert f"{len(expected_run_description.encode('utf-8'))} UTF-8 bytes" in (
            description_item.toolTip(1)
        )
        assert "1255 UTF-8 bytes" in exception_item.toolTip(1)

        description_identifier = str(
            description_item.data(1, details_tables.FULL_VALUE_ID_ROLE) or ""
        )
        exception_identifier = str(
            exception_item.data(1, details_tables.FULL_VALUE_ID_ROLE) or ""
        )
        assert description_identifier
        assert exception_identifier
        assert (
            window.infoBox._trusted_full_values[description_identifier].text
            == expected_run_description
        )
        assert (
            window.infoBox._trusted_full_values[exception_identifier].text
            == selected_measurement_exception
        )

        window.infoBox.raw.itemActivated.emit(description_item, 1)
        application.processEvents()
        dialog = window.infoBox._full_value_dialog
        assert dialog is not None
        if prior_dialog is not None and dialog is not prior_dialog:
            # A selected-detail/generation boundary deliberately discards the
            # prior backing and dialog.  Re-publication may therefore create a
            # replacement, but the retired dialog must hold no exact value.
            assert prior_dialog._exact_text is None
            try:
                assert not prior_dialog.isVisible()
            except RuntimeError:
                # DeferredDelete may already have destroyed the Qt object.
                pass
        assert dialog.text_edit.isReadOnly()
        assert dialog.text_edit.toPlainText() == expected_run_description
        assert expected_run_description in dialog.text_edit.toPlainText()

        window.infoBox.metadata.itemDoubleClicked.emit(exception_item, 1)
        application.processEvents()
        assert window.infoBox._full_value_dialog is dialog
        assert dialog.text_edit.toPlainText() == selected_measurement_exception
        assert dialog.text_edit.toPlainText().endswith("KeyboardInterrupt")
        return dialog

    def assert_lazy_snapshot_access():
        publication = bridge._selected_detail_publication
        assert publication is not None
        assert publication.run_guid == selected_guid
        snapshot_view = publication.detail.snapshot
        assert snapshot_view.status == "available"
        assert "loaded on demand" in snapshot_view.message
        assert snapshot_view.source is None

        snapshot_tree = window.infoBox.snapshot
        initial_items = tuple(tree_items(snapshot_tree))
        assert 0 < len(initial_items) <= 128
        assert not any(
            item.text(0) in {"[truncated]", "Snapshot unavailable"}
            for item in initial_items
        )
        station_item = tree_item_for_path(snapshot_tree, "/Snapshot/station")
        assert station_item.isExpanded()
        process_until(lambda: station_item.childCount() > 0)
        parameters_path = "/Snapshot/station/parameters"
        parameters_item = tree_item_for_path(snapshot_tree, parameters_path)
        assert parameters_item.childCount() == 0
        parameters_item.setExpanded(True)
        process_until(lambda: parameters_item.childCount() > 0)
        assert parameters_item.childCount() <= 128

        # Everything through the initial page was reader-only.  Subsequent
        # continuation pages deliberately alternate with real QCoDeS writer
        # commits/checkpoints; each reader interval receives a fresh protected
        # family baseline after the writer has finished its permitted changes.
        assert_source_policy(
            protected_before_reader,
            protected_artifact_state(database_path),
            database_path,
        )
        next_writer_index = remaining_count
        continuation_pages = 0
        passive_checkpoints = 0
        truncate_checkpoints = 0
        reader_baseline = protected_artifact_state(database_path)

        while True:
            parameters_item = tree_item_for_path(snapshot_tree, parameters_path)
            load_more = next(
                (
                    parameters_item.child(index)
                    for index in range(parameters_item.childCount())
                    if parameters_item.child(index).text(0) == "Load more…"
                ),
                None,
            )
            if load_more is None:
                break
            slow_index, fast_index = divmod(next_writer_index, fast_count)
            remaining_run["datasaver"].add_result(
                (remaining_run["slow"], float(slow_index)),
                (remaining_run["fast"], float(fast_index)),
                (
                    remaining_run["signal"],
                    float(slow_index * 1_000 + fast_index),
                ),
                (
                    remaining_run["signal_b"],
                    float(1_000_000 + slow_index * 1_000 + fast_index),
                ),
            )
            remaining_run["datasaver"].flush_data_to_database(block=True)
            writer.commit()
            if continuation_pages % 2 == 0:
                checkpoint_until_idle("PASSIVE")
                passive_checkpoints += 1
            else:
                checkpoint_until_idle("TRUNCATE")
                truncate_checkpoints += 1
            reader_baseline = protected_artifact_state(database_path)
            prior_count = parameters_item.childCount()
            snapshot_tree.itemActivated.emit(load_more, 0)
            process_until(
                lambda expected_count=prior_count: (
                    tree_item_for_path(
                        snapshot_tree,
                        parameters_path,
                    ).childCount()
                    > expected_count
                )
            )
            parameters_item = tree_item_for_path(snapshot_tree, parameters_path)
            assert 0 < parameters_item.childCount() - prior_count <= 127
            assert_source_policy(
                reader_baseline,
                protected_artifact_state(database_path),
                database_path,
            )
            next_writer_index += 1
            continuation_pages += 1

        parameters_item = tree_item_for_path(snapshot_tree, parameters_path)
        loaded_names = [
            parameters_item.child(index).text(0)
            for index in range(parameters_item.childCount())
        ]
        assert loaded_names == selected_snapshot_parameter_names
        assert len(loaded_names) == len(set(loaded_names))
        final_item = tree_item_for_path(
            snapshot_tree,
            "/Snapshot/station/parameters/lazy_final_parameter",
        )
        final_item.setExpanded(True)
        process_until(lambda: final_item.childCount() > 0)
        final_name = tree_item_for_path(
            snapshot_tree,
            "/Snapshot/station/parameters/lazy_final_parameter/name",
        )
        assert final_name.text(1) == "lazy_final_parameter"
        assert continuation_pages >= 2
        assert passive_checkpoints > 0
        assert truncate_checkpoints > 0
        assert_source_policy(
            reader_baseline,
            protected_artifact_state(database_path),
            database_path,
        )
        assert not any(
            item.text(0) == "[truncated]" for item in tree_items(snapshot_tree)
        )

    def start_partial_run(name, acquired_count):
        experiment = load_or_create_experiment(
            f"{name}_experiment",
            sample_name=f"{name}_sample",
            conn=writer,
        )
        slow = ManualParameter(f"{name}_slow")
        fast = ManualParameter(f"{name}_fast")
        signal = ManualParameter(f"{name}_signal")
        signal_b = ManualParameter(f"{name}_signal_b")
        station = Station(slow, fast, signal, signal_b)
        measurement = Measurement(
            exp=experiment,
            name=f"{name}_run",
            station=station,
        )
        measurement.write_period = 3_600
        measurement.register_parameter(slow)
        measurement.register_parameter(fast)
        measurement.register_parameter(signal, setpoints=(slow, fast))
        measurement.register_parameter(signal_b, setpoints=(slow, fast))
        measurement.set_shapes(
            {
                signal.name: (slow_count, fast_count),
                signal_b.name: (slow_count, fast_count),
            }
        )
        context = measurement.run(write_in_background=False)
        datasaver = context.__enter__()
        open_contexts.append(context)
        for logical_index in range(acquired_count):
            slow_index, fast_index = divmod(logical_index, fast_count)
            datasaver.add_result(
                (slow, float(slow_index)),
                (fast, float(fast_index)),
                (signal, float(slow_index * 1_000 + fast_index)),
                (
                    signal_b,
                    float(1_000_000 + slow_index * 1_000 + fast_index),
                ),
            )
        datasaver.flush_data_to_database(block=True)
        datasaver.dataset.add_metadata("stage5c_operator", "Ada")
        return {
            "slow": slow,
            "fast": fast,
            "signal": signal,
            "signal_b": signal_b,
            "datasaver": datasaver,
            "dataset": datasaver.dataset,
        }

    try:
        initialise_or_create_database_at(database_path, journal_mode="WAL")
        writer = connect(database_path)
        writer.execute("PRAGMA wal_autocheckpoint = 0")

        seed_experiment = load_or_create_experiment(
            "mixed_seed_experiment",
            sample_name="mixed_seed_sample",
            conn=writer,
        )
        seed_setpoint = ManualParameter(
            "mixed_seed_setpoint",
            label="S" * 250,
        )
        seed_signal = ManualParameter(
            "mixed_seed_signal",
            label="D" * 250,
        )
        selected_snapshot_parameters = [
            ManualParameter(f"p{index:03d}") for index in range(576)
        ]
        selected_snapshot_parameters.extend(
            [seed_setpoint, seed_signal, ManualParameter("lazy_final_parameter")]
        )
        selected_snapshot_parameter_names = [
            parameter.name for parameter in selected_snapshot_parameters
        ]
        selected_station = Station(*selected_snapshot_parameters)
        exception_prefix = "Traceback (most recent call last):\\n"
        exception_suffix = "\\nKeyboardInterrupt"
        selected_measurement_exception = (
            exception_prefix
            + "x"
            * (
                1_255
                - len(exception_prefix.encode("utf-8"))
                - len(exception_suffix.encode("utf-8"))
            )
            + exception_suffix
        )
        assert len(selected_measurement_exception.encode("utf-8")) == 1_255
        selected_operator = "Ada"
        seed_measurement = Measurement(
            exp=seed_experiment,
            name="mixed_seed_run",
            station=selected_station,
        )
        seed_measurement.register_parameter(seed_setpoint)
        seed_measurement.register_parameter(
            seed_signal,
            setpoints=(seed_setpoint,),
        )
        with seed_measurement.run(write_in_background=False) as seed_saver:
            for index in range(3):
                seed_saver.add_result(
                    (seed_setpoint, float(index)),
                    (seed_signal, float(index * 2)),
                )
            seed_saver.flush_data_to_database(block=True)
            selected_dataset = seed_saver.dataset
            selected_dataset.add_metadata(
                "measurement_exception",
                selected_measurement_exception,
            )
            selected_dataset.add_metadata("stage5c_operator", selected_operator)

        selected_source_values = writer.execute(
            'SELECT "run_description", "measurement_exception", '
            '"stage5c_operator", "snapshot" FROM "runs" WHERE "run_id" = ?',
            (selected_dataset.run_id,),
        ).fetchone()
        assert selected_source_values is not None
        expected_run_description, stored_exception, stored_operator, stored_snapshot = (
            selected_source_values
        )
        assert isinstance(expected_run_description, str)
        assert len(expected_run_description.encode("utf-8")) > 1_024
        assert stored_exception == selected_measurement_exception
        assert stored_operator == selected_operator
        assert isinstance(stored_snapshot, str)
        assert 138 * 1024 <= len(stored_snapshot.encode("utf-8")) <= 150 * 1024
        assert '"lazy_final_parameter"' in stored_snapshot
        assert (
            stored_snapshot.count('"full_name"')
            + stored_snapshot.count('"raw_value"')
            >= 2 * len(selected_snapshot_parameter_names)
            > 1_024
        )

        visible_run = start_partial_run("mixed_visible", visible_count)
        remaining_run = start_partial_run("mixed_remaining", remaining_count)
        selected_guid = str(selected_dataset.guid)
        visible_guid = str(visible_run["dataset"].guid)
        remaining_guid = str(remaining_run["dataset"].guid)
        protected_before_reader = protected_artifact_state(database_path)

        main_window.config.default_path = str(qplot_home)
        main_window.config.default_file = str(
            qplot_home / main_window.config.config_file_name
        )
        coordinator_module.TrustedDerivedDiskCache = (
            lambda **_kwargs: TrustedDerivedDiskCache(cache_root)
        )
        bridge_module.TrustedWorkCoordinator = GatedCoordinator
        bridge_module.TrustedDerivedQtBridge._publish_metadata = record_metadata
        tree_widgets.RunList.set_run_previews = record_thumbnail
        main_window.MainWindow.show_error = record_error

        application = QtWidgets.QApplication.instance()
        if application is None:
            application = QtWidgets.QApplication(
                ["qplot-stage5c-mixed-size-wheel-smoke"]
            )
        application.setQuitOnLastWindowClosed(False)
        window = main_window.MainWindow()
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        window.config.config["user_preference"]["confirm_close"] = False
        window.config.config["user_preference"]["confirm_close_all"] = False
        window.resize(900, 500)
        window.show()

        window.close_database(status=False)
        assert window.load_database_path(str(database_path))
        process_until(lambda: not window._database_load_active, timeout=30.0)
        window.monitor.stop()
        assert window._database_access_mode == database_actions.TRUSTED_LIVE_MODE, (
            window._database_fallback_reason,
            errors,
        )
        assert window.RunList.topLevelItemCount() == 3
        assert metadata_order == []

        selected_item = window.RunList._item_for_guid(selected_guid)
        visible_item = window.RunList._item_for_guid(visible_guid)
        remaining_item = window.RunList._item_for_guid(remaining_guid)
        assert selected_item is not None
        assert visible_item is not None
        assert remaining_item is not None
        id_column = window.RunList.cols.index("ID")
        window.RunList.sortItems(id_column, QtCore.Qt.SortOrder.AscendingOrder)
        visible_row = window.RunList.indexOfTopLevelItem(visible_item)
        row_height = max(24, window.RunList.sizeHintForRow(visible_row))
        header = window.RunList.header()
        assert header is not None
        window.RunList.setFixedHeight(header.height() + max(12, row_height // 2))
        window.RunList.clearSelection()
        window.RunList.setCurrentItem(selected_item)
        selected_item.setSelected(True)
        process_until(lambda: window._selected_run_guid == selected_guid)
        window.RunList.scrollToItem(
            visible_item,
            QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop,
        )
        application.processEvents()

        bridge = window._trusted_derived_bridge
        bridge._apply_priority()
        visible_indices = bridge._visible_stable_indices()
        assert bridge._index_by_guid[visible_guid] in visible_indices
        assert bridge._index_by_guid[remaining_guid] not in visible_indices
        coordinator = bridge.coordinator
        assert isinstance(coordinator, GatedCoordinator)
        assert not coordinator.active
        coordinator.release_smoke_work()

        process_until(
            lambda: (
                selected_guid in bridge._metadata_by_guid
                and bridge._detail_display_guid == selected_guid
                and bridge._selected_detail_publication is not None
                and window.RunList.run_preview_is_ready(selected_guid)
                and valid_images(thumbnail_previews.get(selected_guid))
                and valid_images(window.infoBox.preview.cache.get(selected_guid))
            ),
            timeout=90.0,
        )
        assert visible_guid not in bridge._metadata_by_guid, metadata_order
        assert remaining_guid not in bridge._metadata_by_guid, metadata_order
        assert metadata_order[0] == selected_guid
        assert window.infoBox.preview.current_guid == selected_guid
        selected_value_dialog = assert_selected_exact_values()

        process_until(
            lambda: bridge_complete(bridge, visible_guid),
            timeout=120.0,
        )
        process_until(
            lambda: bridge_complete(bridge, remaining_guid),
            timeout=120.0,
        )
        assert metadata_order.index(visible_guid) < metadata_order.index(
            remaining_guid
        )
        process_until(
            lambda: (
                not coordinator.active
                and coordinator.snapshot().pending_count == 0
            ),
            timeout=120.0,
        )
        assert window.infoBox.preview.current_guid == selected_guid
        assert_selected_exact_values(selected_value_dialog)
        assert_lazy_snapshot_access()

        prior_visible_preview = window.infoBox.preview.cache[visible_guid]
        for logical_index in range(visible_count, visible_count + fast_count):
            slow_index, fast_index = divmod(logical_index, fast_count)
            visible_run["datasaver"].add_result(
                (visible_run["slow"], float(slow_index)),
                (visible_run["fast"], float(fast_index)),
                (
                    visible_run["signal"],
                    float(slow_index * 1_000 + fast_index),
                ),
                (
                    visible_run["signal_b"],
                    float(1_000_000 + slow_index * 1_000 + fast_index),
                ),
            )
        visible_run["datasaver"].flush_data_to_database(block=True)
        window.refreshMain()
        process_until(
            lambda: (
                visible_item.run_metadata.get("read_setpoint_count")
                == visible_count + fast_count
            ),
            timeout=120.0,
        )
        process_until(
            lambda: (
                bridge_complete(bridge, visible_guid)
                and window.infoBox.preview.cache.get(visible_guid)
                is not prior_visible_preview
            ),
            timeout=120.0,
        )
        process_until(
            lambda: (
                not coordinator.active
                and coordinator.snapshot().pending_count == 0
            ),
            timeout=120.0,
        )
        assert window.infoBox.preview.current_guid == selected_guid
        assert_selected_exact_values(selected_value_dialog)

        checkpoint_until_idle("PASSIVE")
        checkpoint_until_idle("TRUNCATE")
        next_index = visible_count + fast_count
        slow_index, fast_index = divmod(next_index, fast_count)
        visible_run["datasaver"].add_result(
            (visible_run["slow"], float(slow_index)),
            (visible_run["fast"], float(fast_index)),
            (
                visible_run["signal"],
                float(slow_index * 1_000 + fast_index),
            ),
            (
                visible_run["signal_b"],
                float(1_000_000 + slow_index * 1_000 + fast_index),
            ),
        )
        visible_run["datasaver"].flush_data_to_database(block=True)
        writer.commit()
        assert Path(f"{database_path}-wal").stat().st_size > 0
        assert not tuple(database_directory.glob("*.qdc"))
        assert tuple(cache_root.glob("*.qdc"))
        assert errors == []

        active_service = window._trusted_read_service
        assert active_service is not None
        live_liveness = None

        def live_helper_is_observable():
            nonlocal live_liveness
            live_liveness = active_service.liveness()
            return (
                live_liveness.helper_alive
                and live_liveness.helper_pid is not None
            )

        # A zero-wait supervisor liveness probe conservatively reports an
        # owned helper as alive with an unknown PID while its lock is held.
        # Wait for one coherent observable snapshot instead of treating that
        # transient lock-contention sentinel as a dead or missing helper.
        process_until(live_helper_is_observable)
        assert live_liveness is not None
        assert live_liveness.helper_alive
        assert live_liveness.helper_pid is not None
        window.close()
        process_until(
            lambda: (
                window._shutdown_ready
                and not window._shutdown_started
                and not window._trusted_derived_bridge.background_active()
                and not window._retired_trusted_read_services
            ),
            timeout=45.0,
        )
        normal_close_completed = True
        assert not window._shutdown_deadline_exhausted
        assert window._shutdown_diagnostics == ()
        assert window._trusted_read_service is None
        assert not window._pending_trusted_read_services
        assert not window._retired_trusted_read_services
        assert active_service.closed
        closed_liveness = active_service.liveness()
        assert closed_liveness.closed
        assert not closed_liveness.dispatcher_alive
        assert not closed_liveness.control_alive
        assert not closed_liveness.helper_alive
        assert not closed_liveness.receiver_alive
        assert not closed_liveness.open_supervisor_endpoints
        assert not closed_liveness.unreaped_incarnations
        assert not closed_liveness.resource_cleanup_pending
        assert not closed_liveness.outstanding_requests
        assert not any(
            thread.name.startswith("qplot-trusted-derived")
            for thread in threading.enumerate()
        )
    finally:
        if window is not None:
            if not normal_close_completed:
                try:
                    window._trusted_derived_bridge.shutdown()
                    window.close_database(status=False)
                    process_until(
                        lambda: (
                            not window._trusted_derived_bridge.background_active()
                            and not window._retired_trusted_read_services
                        ),
                        timeout=45.0,
                    )
                except BaseException as error:
                    cleanup_errors.append(error)
                window.infoBox.preview.shutdown()
                window.hide()
            window.deleteLater()
            if application is not None:
                application.sendPostedEvents(
                    None,
                    QtCore.QEvent.Type.DeferredDelete,
                )
                application.processEvents()

        main_window.MainWindow.show_error = original_show_error
        tree_widgets.RunList.set_run_previews = original_thumbnail
        bridge_module.TrustedDerivedQtBridge._publish_metadata = original_metadata
        bridge_module.TrustedWorkCoordinator = coordinator_type
        coordinator_module.TrustedDerivedDiskCache = cache_type
        main_window.config.default_path = prior_default_path
        main_window.config.default_file = prior_default_file

        for context in reversed(open_contexts):
            try:
                context.__exit__(None, None, None)
            except BaseException as error:
                cleanup_errors.append(error)
        if writer is not None:
            try:
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                writer.close()
            except BaseException as error:
                cleanup_errors.append(error)
        qcodes.config.core.db_location = prior_qcodes_database
        if cleanup_errors and sys.exc_info()[0] is None:
            raise cleanup_errors[0]


def exercise_spawned_supervisor():
    with tempfile.TemporaryDirectory(prefix="qplot-wheel-smoke-") as temporary:
        # macOS may spell the temporary root through the /var -> /private/var
        # system symlink. Exercise the reader with the canonical local path.
        database_directory = Path(temporary).resolve() / "database"
        database_directory.mkdir()
        database_path = database_directory / "trusted-live.db"
        switch_database_path = database_directory / "trusted-live-switch.db"
        writer = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-u",
                "-c",
                WRITER_CODE,
                str(database_path),
                str(switch_database_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert writer.stdout is not None
            ready_line = writer.stdout.readline()
            if not ready_line:
                assert writer.stderr is not None
                raise AssertionError(
                    f"WAL writer failed before its barrier: {writer.stderr.read()}"
                )
            assert json.loads(ready_line) == {"ready": True}

            wal_path = Path(f"{database_path}-wal")
            shm_path = Path(f"{database_path}-shm")
            assert wal_path.is_file() and wal_path.stat().st_size > 0
            assert shm_path.is_file()
            before = protected_artifact_state(database_path)
            switch_wal_path = Path(f"{switch_database_path}-wal")
            switch_shm_path = Path(f"{switch_database_path}-shm")
            assert switch_wal_path.is_file() and switch_wal_path.stat().st_size > 0
            assert switch_shm_path.is_file()
            switch_before = protected_artifact_state(switch_database_path)

            exercise_installed_shutdown_supervision(
                database_path,
                writer,
                temporary,
            )
            exercise_installed_public_api_boundary(temporary)

            with TrustedLiveReaderSupervisor.open(database_path) as supervisor:
                helper_pid = supervisor.helper_pid
                assert helper_pid is not None and helper_pid != os.getpid()
                assert supervisor.helper_alive
                before_version = supervisor.data_version()
                result = supervisor.query("SELECT value FROM smoke")
                assert result.columns == ("value",)
                assert result.rows == (("committed in WAL",),)
                try:
                    supervisor.query(
                        "INSERT INTO smoke(value) VALUES('forbidden')"
                    )
                except TrustedLiveSqlRejectedError:
                    pass
                else:
                    raise AssertionError("trusted supervisor accepted mutating SQL")
                try:
                    wide_sql = "SELECT " + ", ".join(
                        f"zeroblob(?) AS payload_{index}" for index in range(9)
                    )
                    supervisor.query(
                        wide_sql,
                        (TRUSTED_LIVE_MAX_SCALAR_BYTES,) * 9,
                    )
                except TrustedLiveResultLimitError:
                    pass
                else:
                    raise AssertionError(
                        "trusted supervisor materialised an oversized live result"
                    )
                assert supervisor.query("SELECT count(*) FROM smoke").rows == ((1,),)
                assert supervisor.helper_pid == helper_pid

                after_initial_reads = protected_artifact_state(database_path)
                assert_source_policy(before, after_initial_reads, database_path)

                service = TrustedLiveReadService(
                    database_path,
                    session_generation=1,
                    queue_capacity=16,
                    request_timeout_seconds=20.0,
                )
                try:
                    bootstrap = service.submit_bootstrap().wait(20.0)
                    assert bootstrap.run_id_watermark == 1
                    assert bootstrap.data_version > 0
                    initial_liveness = service.liveness()
                    service_helper_pid = initial_liveness.helper_pid
                    assert service.accepted
                    assert initial_liveness.helper_alive
                    assert service_helper_pid is not None
                    assert service_helper_pid != os.getpid()

                    initial_page = service.submit_basic_page(
                        0,
                        bootstrap.run_id_watermark,
                    ).wait(20.0)
                    assert initial_page.complete
                    assert tuple(record.run_id for record in initial_page.runs) == (1,)
                    initial_fields = initial_page.runs[0].as_dict()
                    assert initial_fields["guid"].endswith("000000000001")
                    assert initial_fields["measure_parameters"] == ["signal"]
                    assert initial_fields["sweep_parameters"] == ["setpoint"]
                    assert initial_fields["preview_dimensions"] == [1]
                    application_home = Path(temporary) / "application-home"
                    application_cache = application_home / "cache"
                    application_home.mkdir()
                    application_cache.mkdir()
                    cache_environment = {
                        "HOME": str(application_home),
                        "USERPROFILE": str(application_home),
                        "LOCALAPPDATA": str(application_cache),
                        "XDG_CACHE_HOME": str(application_cache),
                    }
                    prior_cache_environment = {
                        name: os.environ.get(name) for name in cache_environment
                    }
                    os.environ.update(cache_environment)
                    try:
                        exercise_stage5b_backend(
                            service,
                            initial_page.runs[0],
                            bootstrap,
                            database_directory,
                        )
                        exercise_stage5c_qt_bridge(
                            service,
                            initial_page.runs[0],
                        )
                    finally:
                        for name, prior_value in prior_cache_environment.items():
                            if prior_value is None:
                                os.environ.pop(name, None)
                            else:
                                os.environ[name] = prior_value
                    cheap = service.submit_cheap_run(1).wait(20.0)
                    assert cheap.run_id == 1
                    cheap_fields = cheap.as_dict()
                    assert cheap_fields["measure_parameters"] == ["signal"]
                    assert cheap_fields["sweep_parameters"] == ["setpoint"]
                    assert_stage4_run_detail(service, 1)
                    after_stage4_initial_reads = protected_artifact_state(database_path)
                    assert_source_policy(
                        after_initial_reads,
                        after_stage4_initial_reads,
                        database_path,
                    )

                    # A completed broker request leaves no reader transaction
                    # behind.  Prove the writer can checkpoint and truncate its
                    # WAL while the persistent application service/helper exists.
                    between_transactions = service.liveness()
                    assert between_transactions.outstanding_requests == 0
                    assert between_transactions.helper_alive
                    assert between_transactions.helper_pid == service_helper_pid
                    assert wal_path.stat().st_size > 0
                    assert writer.stdin is not None
                    writer.stdin.write("truncate\\n")
                    writer.stdin.flush()
                    truncate_line = writer.stdout.readline()
                    if not truncate_line:
                        assert writer.stderr is not None
                        raise AssertionError(
                            "WAL writer failed before TRUNCATE checkpoint: "
                            f"{writer.stderr.read()}"
                        )
                    truncate = json.loads(truncate_line)["truncate"]
                    assert len(truncate) == 3
                    assert truncate[0] == 0
                    assert wal_path.stat().st_size == 0
                    assert service.submit_cheap_run(1).wait(20.0).run_id == 1
                    after_truncate_liveness = service.liveness()
                    assert after_truncate_liveness.helper_alive
                    assert after_truncate_liveness.helper_pid == service_helper_pid

                    assert writer.stdin is not None
                    writer.stdin.write("commit\\n")
                    writer.stdin.flush()
                    commit_line = writer.stdout.readline()
                    if not commit_line:
                        assert writer.stderr is not None
                        raise AssertionError(
                            f"WAL writer failed before commit: {writer.stderr.read()}"
                        )
                    assert json.loads(commit_line) == {"committed": True}
                    after_writer_commit = protected_artifact_state(database_path)

                    refresh = service.submit_refresh().wait(20.0)
                    assert refresh.data_version_changed
                    assert refresh.prior_run_id_watermark == 1
                    assert refresh.run_id_watermark == 2
                    later_page = service.submit_basic_page(
                        refresh.prior_run_id_watermark,
                        refresh.run_id_watermark,
                    ).wait(20.0)
                    assert later_page.complete
                    assert tuple(record.run_id for record in later_page.runs) == (2,)
                    assert later_page.runs[0].as_dict()["guid"].endswith(
                        "000000000002"
                    )
                    assert_stage4_run_detail(service, 2)
                    unchanged = service.submit_refresh().wait(20.0)
                    assert not unchanged.data_version_changed
                    assert unchanged.run_id_watermark == 2
                    later_liveness = service.liveness()
                    assert later_liveness.helper_alive
                    assert later_liveness.helper_pid == service_helper_pid
                    after_stage4_later_reads = protected_artifact_state(database_path)
                    assert_source_policy(
                        after_writer_commit,
                        after_stage4_later_reads,
                        database_path,
                    )
                finally:
                    service.close(timeout=20.0)
                assert service.closed
                closed_liveness = service.liveness()
                assert not closed_liveness.dispatcher_alive
                assert not closed_liveness.control_alive
                assert not closed_liveness.helper_alive
                after_stage4_close = protected_artifact_state(database_path)
                assert_source_policy(
                    after_stage4_later_reads,
                    after_stage4_close,
                    database_path,
                )

                # Model the application service switch: a fresh accepted A stays
                # alive while pending B starts and reads its basic page. Only
                # after B is accepted is A retired; B must remain usable.
                switch_from_service = TrustedLiveReadService(
                    database_path,
                    session_generation=2,
                    queue_capacity=8,
                    request_timeout_seconds=20.0,
                )
                switch_service = TrustedLiveReadService(
                    switch_database_path,
                    session_generation=3,
                    queue_capacity=8,
                    request_timeout_seconds=20.0,
                )
                try:
                    switch_from_bootstrap = (
                        switch_from_service.submit_bootstrap().wait(20.0)
                    )
                    assert switch_from_bootstrap.run_id_watermark == 2
                    assert switch_from_service.accepted
                    assert switch_from_service.liveness().helper_alive

                    switch_bootstrap = switch_service.submit_bootstrap().wait(20.0)
                    assert switch_bootstrap.run_id_watermark == 101
                    switch_page = switch_service.submit_basic_page(
                        0,
                        switch_bootstrap.run_id_watermark,
                    ).wait(20.0)
                    assert switch_page.complete
                    assert tuple(record.run_id for record in switch_page.runs) == (101,)
                    switch_fields = switch_page.runs[0].as_dict()
                    assert switch_fields["sample_name"] == "second installed source"
                    assert switch_fields["guid"].endswith("000000000101")
                    assert_stage4_run_detail(switch_service, 101)
                    switch_liveness = switch_service.liveness()
                    assert switch_service.accepted
                    assert switch_liveness.helper_alive
                    assert switch_liveness.outstanding_requests == 0

                    switch_from_service.close(timeout=20.0)
                    assert switch_from_service.closed
                    switch_from_closed = switch_from_service.liveness()
                    assert not switch_from_closed.dispatcher_alive
                    assert not switch_from_closed.control_alive
                    assert not switch_from_closed.helper_alive
                    surviving_switch = switch_service.liveness()
                    assert switch_service.accepted
                    assert surviving_switch.helper_alive
                    assert surviving_switch.outstanding_requests == 0

                    switch_after_reads = protected_artifact_state(
                        switch_database_path
                    )
                    assert_source_policy(
                        switch_before,
                        switch_after_reads,
                        switch_database_path,
                    )
                finally:
                    if not switch_from_service.closed:
                        switch_from_service.close(timeout=20.0)
                    switch_service.close(timeout=20.0)
                assert switch_from_service.closed
                assert switch_service.closed
                switch_closed_liveness = switch_service.liveness()
                assert not switch_closed_liveness.dispatcher_alive
                assert not switch_closed_liveness.control_alive
                assert not switch_closed_liveness.helper_alive
                switch_after_close = protected_artifact_state(switch_database_path)
                assert_source_policy(
                    switch_after_reads,
                    switch_after_close,
                    switch_database_path,
                )
                after_stage4_switch = protected_artifact_state(database_path)
                assert_source_policy(
                    after_stage4_close,
                    after_stage4_switch,
                    database_path,
                )

                later = supervisor.query("SELECT value FROM smoke ORDER BY rowid")
                assert later.rows == (
                    ("committed in WAL",),
                    ("later commit",),
                )
                assert supervisor.data_version() > before_version
                assert supervisor.helper_pid == helper_pid
                after_later_reads = protected_artifact_state(database_path)
                assert_source_policy(
                    after_stage4_switch,
                    after_later_reads,
                    database_path,
                )

                writer.stdin.write("checkpoint\\n")
                writer.stdin.flush()
                checkpoint_line = writer.stdout.readline()
                if not checkpoint_line:
                    assert writer.stderr is not None
                    raise AssertionError(
                        "WAL writer failed before checkpoint: "
                        f"{writer.stderr.read()}"
                    )
                checkpoint = json.loads(checkpoint_line)["checkpoint"]
                assert len(checkpoint) == 3
                assert checkpoint[0] == 0
                assert checkpoint[1] == checkpoint[2]
                assert checkpoint[2] > 0
                after_writer_checkpoint = protected_artifact_state(database_path)

            assert not supervisor.helper_alive
            with TrustedLiveReaderSupervisor.open(
                database_path,
                _test_fault="statement_limit_restore",
            ) as fault_supervisor:
                faulted_pid = fault_supervisor.helper_pid
                faulted_incarnation = fault_supervisor.incarnation
                assert faulted_pid is not None
                try:
                    fault_supervisor.query_batch(
                        (
                            TrustedQuery("SELECT 1 AS unpublished_value"),
                            TrustedQuery("SELECT 2 AS unreachable_value"),
                        )
                    )
                except TrustedLiveCleanupError:
                    pass
                else:
                    raise AssertionError(
                        "uncertain statement-limit restoration was reusable"
                    )
                assert not fault_supervisor.helper_alive
                assert fault_supervisor.query("SELECT 3").rows == ((3,),)
                replacement_pid = fault_supervisor.helper_pid
                assert replacement_pid is not None
                assert replacement_pid != faulted_pid
                assert fault_supervisor.incarnation != faulted_incarnation

            assert not fault_supervisor.helper_alive
            exercise_stage5c_mixed_size_wal_priority(temporary)
            after = protected_artifact_state(database_path)
            assert_source_policy(after_writer_checkpoint, after, database_path)
            assert shm_path.is_file()
            switch_after = protected_artifact_state(switch_database_path)
            assert_source_policy(
                switch_after_close,
                switch_after,
                switch_database_path,
            )
            assert switch_shm_path.is_file()
        finally:
            if writer.poll() is None:
                assert writer.stdin is not None
                writer.stdin.write("close\\n")
                writer.stdin.flush()
            try:
                stdout, stderr = writer.communicate(timeout=15)
            except subprocess.TimeoutExpired as error:
                writer.kill()
                _stdout, stderr = writer.communicate()
                raise AssertionError(f"WAL writer did not close: {stderr}") from error
            assert writer.returncode == 0, stderr
            if stdout:
                assert json.loads(stdout.splitlines()[-1]) == {"closed": True}


def main():
    assert_installed_package()
    assert_installed_qplot_entrypoint_delegation()
    assert_repaired_bounded_views()
    assert_installed_concurrent_cancellation_sender()
    assert_installed_cancellation_owner_loss()
    exercise_spawned_supervisor()
    print(
        f"qplot {qplot.__version__}: import, native extension, resources, "
        "entry-point launcher delegation, authenticated normal and forced shutdown "
        "supervision, public-API foreign-reaper/status/signal/EOF/cancellation "
        "containment, durable owner-resumable sole-writer cancellation, "
        "transactional SIGINT-guard "
        "restoration, repeated-interrupt retention, and caller-disappearance cleanup, "
        "stuck-reader orphan cleanup, acquisition-caller/writer/sentinel survival, "
        "installed Stage 4 live refresh/database switch, installed Stage 5C "
        "mixed-size WAL tier priority, exact long-metadata viewing, and normal "
        "Qt/helper shutdown, persistent "
        "helpers across a writer TRUNCATE checkpoint, result-limit recovery, "
        "fail-closed limit cleanup, and writer checkpoint passed"
    )


if __name__ == "__main__":
    main()
"""


def smoke_test_installed_qplot_entrypoint(
    environment: Path,
    temporary: Path,
    *, env=None,
) -> None:
    """Prove the actual installed qplot executable delegates to launch_gui."""

    hook_directory = temporary / "entrypoint-hook"
    hook_directory.mkdir()
    (hook_directory / "sitecustomize.py").write_text(
        ENTRYPOINT_DELEGATION_SITECUSTOMIZE,
        encoding="utf-8",
    )
    record_path = hook_directory / "delegation.json"
    entrypoint_environment = dict(os.environ if env is None else env)
    entrypoint_environment["PYTHONPATH"] = str(hook_directory)
    entrypoint_environment["_QPLOT_ENTRYPOINT_DELEGATION_RECORD"] = str(record_path)
    database_argument = "installed database path.db"
    completed = subprocess.run(
        [
            str(console_script(environment, "qplot")),
            database_argument,
            "--platform",
            "offscreen",
        ],
        env=entrypoint_environment,
        cwd=hook_directory,
        check=False,
        capture_output=True,
        text=True,
        timeout=20.0,
    )
    assert completed.returncode == 17, completed.stdout + completed.stderr
    delegation = json.loads(record_path.read_text(encoding="utf-8"))
    assert delegation["argv"][1:] == [
        database_argument,
        "--platform",
        "offscreen",
    ]
    assert delegation["database_path"] is None


def smoke_test_wheel(
    repository: Path,
    artifact: Path,
    native_artifact: Path,
    runtime_files: set[str],
    temporary: Path,
) -> None:
    """Install both local wheels into a fresh venv and exercise installed files."""
    environment = temporary / "wheel-venv"
    python = create_environment(environment)
    install_wheels(python, artifact, native_artifact)
    smoke_test_installation(
        repository, artifact, native_artifact, runtime_files, temporary, environment,
    )


def smoke_test_installation(
    repository: Path, artifact: Path, native_artifact: Path,
    runtime_files: set[str], temporary: Path, environment: Path, *,
    audit_code: str | None = None, env=None,
) -> None:
    """Exercise an already installed application and native wheel pair."""
    python = environment_python(environment)
    version = tomllib.loads((repository / "pyproject.toml").read_text())["project"][
        "version"
    ]
    resources = sorted(
        path.removeprefix("qplot/")
        for path in runtime_files
        if not path.endswith(".py") and path not in NATIVE_EXTENSION_MEMBERS
    )
    smoke_directory = temporary / "wheel-smoke"
    smoke_directory.mkdir()
    if smoke_directory.resolve().is_relative_to(repository.resolve()):
        raise AssertionError("installed-wheel smoke must run outside the repository")
    smoke_script = smoke_directory / "installed_wheel_smoke.py"
    smoke_script.write_text(
        (audit_code if audit_code is not None else
         wheel_installation_audit_code([artifact, native_artifact])) + wheel_smoke_code(),
        encoding="utf-8",
    )
    smoke_environment = dict(os.environ if env is None else env)
    smoke_environment.pop("PYTHONPATH", None)
    run(
        [
            str(python),
            "-I",
            str(smoke_script),
            version,
            json.dumps(resources),
            json.dumps(CONSOLE_SCRIPTS),
            PINNED_APSW_VERSION,
            NATIVE_EXTENSION_MODULE,
            json.dumps(
                sorted(PurePosixPath(path).name for path in NATIVE_EXTENSION_MEMBERS)
            ),
            PINNED_NATIVE_VERSION,
        ],
        cwd=smoke_directory,
        env=smoke_environment,
    )
    for name in CONSOLE_SCRIPTS:
        path = console_script(environment, name)
        if not path.is_file():
            raise AssertionError(f"installed console script is missing: {path}")
    smoke_test_installed_qplot_entrypoint(environment, temporary, env=smoke_environment)
    run([str(console_script(environment, "qplot-generate-db")), "--help"],
        cwd=smoke_directory, env=smoke_environment)


def find_artifacts(paths: list[Path], *, wheel_only: bool = False) -> dict[str, Path]:
    """Require one local wheel (and optionally sdist) for each distribution."""
    artifacts: set[Path] = set()
    for path in paths:
        if path.is_dir():
            artifacts.update(path.glob("*.tar.gz"))
            artifacts.update(path.glob("*.whl"))
        else:
            artifacts.add(path)
    result = {}
    for name in ("qplotter", "qplotter_native"):
        for kind, suffix in (("wheel", ".whl"), ("sdist", ".tar.gz")):
            if wheel_only and kind == "sdist":
                continue
            candidates = [
                path for path in artifacts
                if path.name.startswith((f"{name}-", f"{name.replace('_', '-')}-"))
                and path.name.endswith(suffix)
            ]
            if len(candidates) != 1:
                raise AssertionError(f"expected one {name} {kind}, found {candidates}")
            result[f"{name}_{kind}"] = candidates[0]
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifacts",
        nargs="*",
        type=Path,
        help=(
            "artifact files or directories containing both distributions, "
            "or both wheels with --wheel-only"
        ),
    )
    parser.add_argument(
        "--install-only", action="store_true",
        help="validate and install the local wheel pair into this interpreter for CI",
    )
    parser.add_argument("--with-dev-tools", action="store_true")
    parser.add_argument("--extra-requirement", action="append", default=[])
    parser.add_argument(
        "--audit-path", type=Path,
        help="write an import/hash audit for pytest to run in its own process",
    )
    parser.add_argument(
        "--check-clean",
        action="store_true",
        help="fail unless the Git working tree is clean",
    )
    parser.add_argument(
        "--wheel-only",
        action="store_true",
        help="validate and smoke-test both wheels without requiring sdists",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = Path(__file__).resolve().parents[1]
    if args.check_clean:
        check_clean(repository)
    if not args.artifacts:
        if args.check_clean:
            return 0
        raise AssertionError("provide an artifact file or directory")

    source = source_files(repository)
    with tempfile.TemporaryDirectory(prefix="qplot-artifacts-") as temporary_name:
        temporary = Path(temporary_name)
        artifacts = find_artifacts(
            args.artifacts, wheel_only=args.wheel_only or args.install_only,
        )
        wheel = artifacts["qplotter_wheel"]
        native_wheel = artifacts["qplotter_native_wheel"]
        runtime_files = validate_wheel(wheel, source)
        validate_wheel(native_wheel, source, native=True)
        if args.install_only:
            import sys

            install_wheels(
                Path(sys.executable), wheel, native_wheel,
                with_dev_tools=args.with_dev_tools,
                extra_requirements=tuple(args.extra_requirement),
            )
            audit = wheel_installation_audit_code([wheel, native_wheel])
            audit_path = args.audit_path or temporary / "wheel-installation-audit.py"
            audit_path.write_text(audit, encoding="utf-8")
            run([sys.executable, "-I", str(audit_path.resolve())])
            return 0
        if not args.wheel_only:
            sdist = artifacts["qplotter_sdist"]
            native_sdist = artifacts["qplotter_native_sdist"]
            validate_sdist(sdist, source)
            validate_sdist(native_sdist, source, native=True)
            test_extracted_sdist(sdist.resolve(), native_sdist.resolve(), temporary)
        smoke_test_wheel(
            repository, wheel.resolve(), native_wheel.resolve(), runtime_files, temporary,
        )
    print("Distribution artifact validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
