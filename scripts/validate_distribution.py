"""Validate qPlot source and wheel distributions in isolated environments."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tarfile
import tempfile
import tomllib
import venv
import zipfile
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
    "setup.py",
}
SDIST_SOURCE_PREFIXES = ("docs/", "scripts/", "src/", "tests/")
REQUIRED_NATIVE_SOURCE_FILES = {
    "src/qplot/datahandling/_trusted_vfs_native.c",
    "src/qplot/datahandling/_trusted_vfs_sqlite_abi.h",
}
NATIVE_BUILD_SUFFIXES = {".c", ".h"}
COMPILED_NATIVE_SUFFIXES = {".dll", ".dylib", ".pyd", ".so"}
NATIVE_EXTENSION_MODULE = "qplot.datahandling._trusted_vfs_native"
NATIVE_EXTENSION_STEM = "qplot/datahandling/_trusted_vfs_native"
NATIVE_EXTENSION_MEMBERS = {
    f"{NATIVE_EXTENSION_STEM}.abi3.so",
    f"{NATIVE_EXTENSION_STEM}.pyd",
}
PINNED_APSW_VERSION = "3.53.4.0"
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
        if path
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
    setuptools_inventory = path.endswith("src/qplot.egg-info/SOURCES.txt")
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
) -> tuple[str, set[str]]:
    """Check the sdist against the explicit source-tree inclusion policy."""
    members = archive_files(artifact)
    assert_no_ignored_members(artifact, members)
    root, relative_members = relative_sdist_files(members)
    assert_no_compiled_native_members(artifact, relative_members)
    expected = (
        REQUIRED_ROOT_FILES
        | REQUIRED_NATIVE_SOURCE_FILES
        | {
            path
            for path in source
            if path.startswith(SDIST_SOURCE_PREFIXES)
            and PurePosixPath(path).suffix.casefold() not in COMPILED_NATIVE_SUFFIXES
        }
    )
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
    if "tests/conftest.py" not in relative_members:
        raise AssertionError("sdist is missing tests/conftest.py")
    print(
        f"{artifact.name}: {len(relative_members)} files; "
        f"all {len(source_tests)} source test modules and conftest are present."
    )
    return root, relative_members


def validate_wheel(artifact: Path, source: set[str]) -> set[str]:
    """Check that wheel runtime files exactly match source package files."""
    if "-cp311-abi3-" not in artifact.name:
        raise AssertionError(
            f"{artifact.name} is not tagged for the CPython 3.11 stable ABI"
        )
    members = archive_files(artifact)
    assert_no_ignored_members(artifact, members)
    actual_runtime = {path for path in members if path.startswith("qplot/")}
    native_members = actual_runtime & NATIVE_EXTENSION_MEMBERS
    if len(native_members) != 1:
        raise AssertionError(
            f"{artifact.name} must contain exactly one abi3 native extension; "
            f"found {sorted(native_members)}"
        )
    expected_runtime = {
        path.removeprefix("src/")
        for path in source
        if path.startswith("src/qplot/")
        and PurePosixPath(path).suffix.casefold()
        not in NATIVE_BUILD_SUFFIXES | COMPILED_NATIVE_SUFFIXES
    }
    expected_runtime.update(native_members)
    missing = sorted(expected_runtime - actual_runtime)
    stale = sorted(actual_runtime - expected_runtime)
    if missing or stale:
        detail = []
        if missing:
            detail.append("missing runtime files:\n" + "\n".join(missing))
        if stale:
            detail.append("unexpected/stale runtime files:\n" + "\n".join(stale))
        raise AssertionError(f"{artifact.name} runtime mismatch:\n" + "\n".join(detail))

    unexpected = sorted(
        path
        for path in members - actual_runtime
        if not PurePosixPath(path).parts[0].endswith(".dist-info")
    )
    if unexpected:
        raise AssertionError(
            f"{artifact.name} has unexpected top-level files:\n" + "\n".join(unexpected)
        )
    print(
        f"{artifact.name}: {len(members)} files; {len(actual_runtime)} runtime files."
    )
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


def test_extracted_sdist(artifact: Path, temporary: Path) -> None:
    """Install and run all tests from the extracted source distribution."""
    source = extract_sdist(artifact, temporary / "sdist-source")
    environment = temporary / "sdist-venv"
    python = create_environment(environment)
    run([str(python), "-m", "pip", "install", f"{source}[dev]"])
    test_env = os.environ.copy()
    test_env.setdefault("QT_QPA_PLATFORM", "offscreen")
    test_env["MPLCONFIGDIR"] = str(temporary / "matplotlib")
    # Exercise the installed sdist, including compiled extensions.  The
    # repository's normal ``pythonpath = ["src"]`` setting would otherwise
    # shadow that installation with the unbuilt extracted source tree.
    run(
        [str(python), "-m", "pytest", "-o", "pythonpath="],
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
from qplot.datahandling.trusted_live import (
    TRUSTED_LIVE_MAX_SCALAR_BYTES,
    TrustedLiveCleanupError,
    TrustedLiveResultLimitError,
    TrustedLiveSqlRejectedError,
    TrustedQuery,
)
from qplot.datahandling.trusted_live_queries import TrustedMetadataQueryAdapter
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
        while not record_path.exists() or not launcher_record_path.exists():
            if caller.poll() is not None:
                stdout, stderr = caller.communicate()
                raise AssertionError(
                    "installed vanishing API caller exited before readiness: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError("installed caller-EOF tree did not become ready")
            time.sleep(0.01)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        launcher_pid = int(launcher_record_path.read_text(encoding="utf-8"))
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
    normal_record = json.loads(normal_record_path.read_text(encoding="utf-8"))
    assert normal_result["returncode"] == 17, normal_result
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
    assert qplot.__version__ == expected_version == version("qplot")
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
        for entry in distribution("qplot").entry_points
        if entry.group == "console_scripts"
    }
    assert scripts == expected_scripts
    for entry in distribution("qplot").entry_points:
        if entry.group == "console_scripts":
            assert callable(entry.load()), entry.name


def assert_repaired_bounded_views():
    oversized = "installed-wheel-presentation-value-" * 16_384
    nested = oversized
    for _depth in range(32):
        nested = {"child": nested}
    presentation = build_selected_run_presentation(
        run_fields={"run_id": 7, "run_description": oversized},
        metadata_fields={"oversized": oversized, "nested": nested},
        parameters=(),
        snapshot_summary={"Status": "available"},
        setpoint_summaries=(),
        unavailable_fields=(),
    )
    assert isinstance(presentation, TrustedSelectedRunPresentation)
    assert presentation.metadata.status == "truncated"
    assert presentation.raw.status == "truncated"
    assert oversized not in dict(presentation.metadata_fields).values()
    for view in (presentation.metadata, presentation.raw):
        assert len(view.nodes) <= TRUSTED_PRESENTATION_MAX_RENDERED_NODES
        assert view.rendered_text_bytes <= TRUSTED_PRESENTATION_MAX_RENDERED_TEXT_BYTES
        assert view.tooltip_text_bytes <= TRUSTED_PRESENTATION_MAX_TOOLTIP_TEXT_BYTES
        assert all(
            len(node.key.encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_KEY_BYTES
            and len(node.value.encode("utf-8"))
            <= TRUSTED_PRESENTATION_MAX_VALUE_BYTES
            and len(node.tooltip.encode("utf-8"))
            <= TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES
            and oversized not in node.value
            and oversized not in node.tooltip
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
        ("run_id", str(run_id), 0),
    )
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


def exercise_spawned_supervisor():
    with tempfile.TemporaryDirectory(prefix="qplot-wheel-smoke-") as temporary:
        # macOS may spell the temporary root through the /var -> /private/var
        # system symlink. Exercise the reader with the canonical local path.
        database_path = Path(temporary).resolve() / "trusted-live.db"
        switch_database_path = Path(temporary).resolve() / "trusted-live-switch.db"
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
                    assert initial_fields["measure_parameters"] == []
                    assert initial_fields["sweep_parameters"] == []
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
        "installed Stage 4 live refresh/database switch, persistent helpers across "
        "a writer TRUNCATE checkpoint, result-limit recovery, fail-closed limit "
        "cleanup, and writer checkpoint passed"
    )


if __name__ == "__main__":
    main()
"""


def smoke_test_installed_qplot_entrypoint(
    environment: Path,
    temporary: Path,
) -> None:
    """Prove the actual installed qplot executable delegates to launch_gui."""

    hook_directory = temporary / "entrypoint-hook"
    hook_directory.mkdir()
    (hook_directory / "sitecustomize.py").write_text(
        ENTRYPOINT_DELEGATION_SITECUSTOMIZE,
        encoding="utf-8",
    )
    record_path = hook_directory / "delegation.json"
    entrypoint_environment = os.environ.copy()
    existing_pythonpath = entrypoint_environment.get("PYTHONPATH")
    entrypoint_environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(hook_directory), existing_pythonpath) if item
    )
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
    runtime_files: set[str],
    temporary: Path,
) -> None:
    """Install only the wheel into a fresh venv and exercise installed files."""
    environment = temporary / "wheel-venv"
    python = create_environment(environment)
    run([str(python), "-m", "pip", "install", str(artifact.resolve())])
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
    smoke_script.write_text(wheel_smoke_code(), encoding="utf-8")
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
        ],
        cwd=smoke_directory,
    )
    for name in CONSOLE_SCRIPTS:
        path = console_script(environment, name)
        if not path.is_file():
            raise AssertionError(f"installed console script is missing: {path}")
    smoke_test_installed_qplot_entrypoint(environment, temporary)
    run([str(console_script(environment, "qplot-generate-db")), "--help"])


def find_artifacts(paths: list[Path]) -> tuple[Path, Path]:
    """Resolve exactly one sdist and one wheel from files or directories."""
    artifacts: set[Path] = set()
    for path in paths:
        if path.is_dir():
            artifacts.update(path.glob("*.tar.gz"))
            artifacts.update(path.glob("*.whl"))
        else:
            artifacts.add(path)
    sdists = sorted(path for path in artifacts if path.name.endswith(".tar.gz"))
    wheels = sorted(path for path in artifacts if path.suffix == ".whl")
    if len(sdists) != 1 or len(wheels) != 1:
        raise AssertionError(
            f"expected one sdist and one wheel, found {len(sdists)} and {len(wheels)}"
        )
    return sdists[0], wheels[0]


def find_wheel(paths: list[Path]) -> Path:
    """Resolve exactly one wheel from files or directories."""
    wheels: set[Path] = set()
    for path in paths:
        if path.is_dir():
            wheels.update(path.glob("*.whl"))
        elif path.suffix == ".whl":
            wheels.add(path)
    if len(wheels) != 1:
        raise AssertionError(f"expected one wheel, found {len(wheels)}")
    return wheels.pop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifacts",
        nargs="*",
        type=Path,
        help=(
            "artifact files or directories containing one sdist and one wheel, "
            "or one wheel with --wheel-only"
        ),
    )
    parser.add_argument(
        "--check-clean",
        action="store_true",
        help="fail unless the Git working tree is clean",
    )
    parser.add_argument(
        "--wheel-only",
        action="store_true",
        help="validate and smoke-test one platform wheel without requiring an sdist",
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
        if args.wheel_only:
            wheel = find_wheel(args.artifacts)
            runtime_files = validate_wheel(wheel, source)
            smoke_test_wheel(repository, wheel.resolve(), runtime_files, temporary)
            print("Wheel artifact validation passed.")
            return 0

        sdist, wheel = find_artifacts(args.artifacts)
        validate_sdist(sdist, source)
        runtime_files = validate_wheel(wheel, source)
        test_extracted_sdist(sdist.resolve(), temporary)
        smoke_test_wheel(repository, wheel.resolve(), runtime_files, temporary)
    print("Distribution artifact validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
