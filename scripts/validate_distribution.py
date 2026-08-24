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
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from importlib.metadata import distribution, version
from importlib.resources import files
from pathlib import Path

import apsw
import qplot
from qplot.datahandling.trusted_live import (
    TRUSTED_LIVE_MAX_SCALAR_BYTES,
    TrustedLiveCleanupError,
    TrustedLiveResultLimitError,
    TrustedLiveSqlRejectedError,
    TrustedQuery,
)
from qplot.datahandling.trusted_live_supervisor import TrustedLiveReaderSupervisor


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

connection = apsw.Connection(sys.argv[1])
try:
    journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    assert journal_mode is not None
    assert str(journal_mode[0]).casefold() == "wal", journal_mode
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE smoke(value TEXT NOT NULL)")
    connection.execute("INSERT INTO smoke(value) VALUES('committed in WAL')")
    print(json.dumps({"ready": True}), flush=True)
    while True:
        command = sys.stdin.readline().strip()
        if command == "commit":
            connection.execute("INSERT INTO smoke(value) VALUES('later commit')")
            print(json.dumps({"committed": True}), flush=True)
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
    connection.close(True)
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


def assert_installed_package():
    expected_version = sys.argv[1]
    resource_paths = json.loads(sys.argv[2])
    expected_scripts = json.loads(sys.argv[3])
    expected_apsw_version = sys.argv[4]
    native_module_name = sys.argv[5]
    native_file_names = set(json.loads(sys.argv[6]))
    assert qplot.__version__ == expected_version == version("qplot")
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


def exercise_spawned_supervisor():
    with tempfile.TemporaryDirectory(prefix="qplot-wheel-smoke-") as temporary:
        # macOS may spell the temporary root through the /var -> /private/var
        # system symlink. Exercise the reader with the canonical local path.
        database_path = Path(temporary).resolve() / "trusted-live.db"
        writer = subprocess.Popen(
            [sys.executable, "-I", "-u", "-c", WRITER_CODE, str(database_path)],
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

                later = supervisor.query("SELECT value FROM smoke ORDER BY rowid")
                assert later.rows == (
                    ("committed in WAL",),
                    ("later commit",),
                )
                assert supervisor.data_version() > before_version
                assert supervisor.helper_pid == helper_pid
                after_later_reads = protected_artifact_state(database_path)
                assert_source_policy(
                    after_writer_commit,
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
    exercise_spawned_supervisor()
    print(
        f"qplot {qplot.__version__}: import, native extension, resources, "
        "entry points, installed spawned trusted WAL helper, result-limit "
        "recovery, fail-closed limit cleanup, and writer checkpoint passed"
    )


if __name__ == "__main__":
    main()
"""


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
