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
}
SDIST_TRACKED_PREFIXES = ("docs/", "scripts/", "src/", "tests/")
CONSOLE_SCRIPTS = {
    "qplot": "qplot.__main__:run",
    "qplot-cfg": "qplot.configuration.scripts:scripts",
    "qplot-generate-db": "qplot.testdata:main",
}


def run(command: list[str], *, cwd: Path | None = None, env=None) -> None:
    """Run a subprocess and show the exact command in CI output."""
    print(f"+ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def git_files(repository: Path) -> set[str]:
    """Return paths tracked by Git, using archive-style separators."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
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
            "distribution builds must start from a clean source tree:\n"
            f"{result.stdout}"
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
        raise AssertionError(f"sdist should have one top-level directory, found {roots}")
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
    return (
        name in IGNORED_FILE_NAMES
        or name.endswith((".pyc", ".pyo"))
    )


def assert_no_ignored_members(artifact: Path, members: set[str]) -> None:
    leaked = sorted(member for member in members if ignored_member(member))
    if leaked:
        raise AssertionError(
            f"{artifact.name} contains ignored or generated files:\n"
            + "\n".join(leaked)
        )


def validate_sdist(
    artifact: Path,
    tracked: set[str],
) -> tuple[str, set[str]]:
    """Check the sdist against the explicit tracked-file inclusion policy."""
    members = archive_files(artifact)
    assert_no_ignored_members(artifact, members)
    root, relative_members = relative_sdist_files(members)
    expected = REQUIRED_ROOT_FILES | {
        path for path in tracked if path.startswith(SDIST_TRACKED_PREFIXES)
    }
    missing = sorted(expected - relative_members)
    if missing:
        raise AssertionError(
            f"{artifact.name} is missing required tracked files:\n"
            + "\n".join(missing)
        )

    tracked_tests = sorted(
        path
        for path in tracked
        if path.startswith("tests/") and Path(path).name.startswith("test_")
        and path.endswith(".py")
    )
    if "tests/conftest.py" not in relative_members:
        raise AssertionError("sdist is missing tests/conftest.py")
    print(
        f"{artifact.name}: {len(relative_members)} files; "
        f"all {len(tracked_tests)} tracked test modules and conftest are present."
    )
    return root, relative_members


def validate_wheel(artifact: Path, tracked: set[str]) -> set[str]:
    """Check that wheel runtime files exactly match tracked package files."""
    members = archive_files(artifact)
    assert_no_ignored_members(artifact, members)
    expected_runtime = {
        path.removeprefix("src/")
        for path in tracked
        if path.startswith("src/qplot/")
    }
    actual_runtime = {path for path in members if path.startswith("qplot/")}
    missing = sorted(expected_runtime - actual_runtime)
    stale = sorted(actual_runtime - expected_runtime)
    if missing or stale:
        detail = []
        if missing:
            detail.append("missing runtime files:\n" + "\n".join(missing))
        if stale:
            detail.append("untracked/stale runtime files:\n" + "\n".join(stale))
        raise AssertionError(f"{artifact.name} runtime mismatch:\n" + "\n".join(detail))

    unexpected = sorted(
        path
        for path in members - actual_runtime
        if not PurePosixPath(path).parts[0].endswith(".dist-info")
    )
    if unexpected:
        raise AssertionError(
            f"{artifact.name} has unexpected top-level files:\n"
            + "\n".join(unexpected)
        )
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


def test_extracted_sdist(artifact: Path, temporary: Path) -> None:
    """Install and run all tests from the extracted source distribution."""
    source = extract_sdist(artifact, temporary / "sdist-source")
    environment = temporary / "sdist-venv"
    python = create_environment(environment)
    run([str(python), "-m", "pip", "install", f"{source}[dev]"])
    test_env = os.environ.copy()
    test_env.setdefault("QT_QPA_PLATFORM", "offscreen")
    test_env["MPLCONFIGDIR"] = str(temporary / "matplotlib")
    run([str(python), "-m", "pytest"], cwd=source, env=test_env)


def wheel_smoke_code() -> str:
    """Return isolated-Python checks run after installing the wheel."""
    return """
import json
import sys
from importlib.metadata import distribution, version
from importlib.resources import files

import qplot

expected_version = sys.argv[1]
resource_paths = json.loads(sys.argv[2])
expected_scripts = json.loads(sys.argv[3])
assert qplot.__version__ == expected_version == version("qplot")
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
print(f"qplot {qplot.__version__}: import, resources, and entry points passed")
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
        if not path.endswith(".py")
    )
    smoke_directory = temporary / "wheel-smoke"
    smoke_directory.mkdir()
    run(
        [
            str(python),
            "-I",
            "-c",
            wheel_smoke_code(),
            version,
            json.dumps(resources),
            json.dumps(CONSOLE_SCRIPTS),
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifacts",
        nargs="*",
        type=Path,
        help="artifact files or directories containing one sdist and one wheel",
    )
    parser.add_argument(
        "--check-clean",
        action="store_true",
        help="fail unless the Git working tree is clean",
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

    tracked = git_files(repository)
    sdist, wheel = find_artifacts(args.artifacts)
    validate_sdist(sdist, tracked)
    runtime_files = validate_wheel(wheel, tracked)
    with tempfile.TemporaryDirectory(prefix="qplot-artifacts-") as temporary_name:
        temporary = Path(temporary_name)
        test_extracted_sdist(sdist.resolve(), temporary)
        smoke_test_wheel(repository, wheel.resolve(), runtime_files, temporary)
    print("Distribution artifact validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
