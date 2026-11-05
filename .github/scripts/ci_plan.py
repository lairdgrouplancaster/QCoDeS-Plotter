"""Select routine or full CI without trusting an incomplete changed-file list."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

PYTHON_VERSIONS = ("3.12", "3.13", "3.14")
SENSITIVE_PREFIXES = (
    ".github/",
    "native/",
    "scripts/",
    "src/qplot/datahandling/",
    "tests/datahandling/",
    "requirements/",
    "constraints/",
)
SENSITIVE_FILES = {
    "src/qplot/_auxiliary_paths.py",
    "src/qplot/testdata.py",
    "tests/conftest.py",
    "tests/_ci_scheduling.py",
    "tests/ci_timings.json",
    "tests/test_windows_ci.py",
    "tests/test_update_ci_timings.py",
    "tests/test_auxiliary_database_protection.py",
    "tests/test_bug_hunt_auxiliary_safety.py",
    "tests/test_bug_hunt_generator_fifo.py",
}
DEPENDENCY_NAMES = {
    "pyproject.toml", "MANIFEST.in", "setup.py", "setup.cfg",
    "Pipfile", "Pipfile.lock", "poetry.lock", "uv.lock", "pdm.lock",
    "pylock.toml",
}
DEPENDENCY_PATTERNS = (
    "requirements*.txt", "requirements*.in", "constraints*.txt", "constraints*.in",
    "environment*.yml", "environment*.yaml", "pylock.*.toml", "*.lock",
)
SENSITIVE_TEST_PATTERNS = (
    "test_distribution*.py", "test_packag*.py", "test_native*.py",
    "test_release*.py", "test_revalidate*.py", "test_ci_*.py",
    "test_compiler_free*.py", "test_testdata*.py",
)


def sensitive_path(path: str) -> bool:
    """Recognize changes requiring Linux safety and distribution acceptance."""
    if path.startswith(SENSITIVE_PREFIXES) or path in SENSITIVE_FILES:
        return True
    parsed = PurePosixPath(path)
    if parsed.name in DEPENDENCY_NAMES:
        return True
    if any(fnmatch.fnmatchcase(parsed.name, pattern) for pattern in DEPENDENCY_PATTERNS):
        return True
    if path.startswith("src/qplot/_testdata_") and path.endswith(".py"):
        return True
    return parsed.parent == PurePosixPath("tests") and any(
        fnmatch.fnmatchcase(parsed.name, pattern) for pattern in SENSITIVE_TEST_PATTERNS
    )


def _commit(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value):
        if value != "0" * 40:
            return value
    return None


def _comparison(event_name: str, event: dict) -> str | None:
    if event_name == "pull_request":
        pull_request = event.get("pull_request")
        if not isinstance(pull_request, dict):
            return None
        base, head = pull_request.get("base"), pull_request.get("head")
        if not isinstance(base, dict) or not isinstance(head, dict):
            return None
        before, after = _commit(base.get("sha")), _commit(head.get("sha"))
        separator = "..."
    elif event_name == "push":
        before, after = _commit(event.get("before")), _commit(event.get("after"))
        separator = ".."
    else:
        return None
    return f"{before}{separator}{after}" if before and after else None


def validation_mode(
    event_name: str,
    event: object,
    ref: str,
    full_validation: str,
    *,
    repository: Path,
) -> tuple[bool, str]:
    """Only a successful, complete comparison can select routine validation."""
    if full_validation == "true":
        return True, "Full validation explicitly requested."
    if full_validation != "false":
        return True, "Validation override unavailable or invalid."
    if event_name in {"schedule", "workflow_dispatch", "workflow_call"}:
        return True, "Scheduled or manually requested full validation."
    if ref.startswith("refs/tags/"):
        return True, "Release tag requires full validation."
    if not isinstance(event, dict):
        return True, "Event payload unavailable or invalid."
    comparison = _comparison(event_name, event)
    if comparison is None:
        return True, "Complete comparison revisions unavailable."
    try:
        result = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", comparison, "--"],
            cwd=repository,
            check=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return True, "Changed-file comparison unavailable."
    # Without rename detection, both the deleted source and added destination
    # are checked. NUL separators retain filenames containing newlines.
    changed_paths = [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]
    if any(sensitive_path(path) for path in changed_paths):
        return True, "Database, native, dependency, packaging or CI changes."
    return False, "Changes outside Linux-sensitive validation paths."


def check_matrix(full_validation: bool) -> dict[str, list[dict[str, str]]]:
    entries = []
    for runner, artifact in (
        ("windows-2025", "qplotter-native-windows-x64"),
        ("macos-latest", "qplotter-native-macos-arm64"),
    ):
        for version in PYTHON_VERSIONS:
            entry = {
                "os": runner,
                "python-version": version,
                "suite": "compatibility" if version == "3.13" else "full",
                "native-artifact": artifact,
            }
            if entry["suite"] == "full":
                entries.extend({**entry, "partition": partition} for partition in ("1/2", "2/2"))
            else:
                entries.append(entry)
    compatibility = [("macos-15-intel", "qplotter-native-macos-intel")]
    if full_validation:
        compatibility.append(("ubuntu-latest", "qplotter-native-linux-x86_64"))
    for runner, artifact in compatibility:
        entries.extend({
            "os": runner,
            "python-version": version,
            "suite": "compatibility",
            "native-artifact": artifact,
        } for version in PYTHON_VERSIONS)
    return {"include": entries}


def installation_matrix(full_validation: bool) -> dict[str, list[dict[str, str]]]:
    platforms = [
        ("macos-arm64", "macOS ARM64", "macos-latest"),
        ("macos-intel", "macOS Intel", "macos-15-intel"),
        ("windows-x64", "Windows x64", "windows-2025"),
    ]
    if full_validation:
        platforms.insert(0, ("linux-x86_64", "Linux x86_64", "ubuntu-latest"))
    return {"include": [{
        "platform": platform,
        "os-name": name,
        "os": runner,
        "native-artifact": f"qplotter-native-{platform}",
        "python-version": version,
    } for platform, name, runner in platforms for version in PYTHON_VERSIONS]}


def main() -> None:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    except (KeyError, OSError, ValueError):
        event = None
    full_validation, reason = validation_mode(
        os.environ.get("GITHUB_EVENT_NAME", ""),
        event,
        os.environ.get("GITHUB_REF", ""),
        os.environ.get("QPLOT_FULL_VALIDATION", ""),
        repository=Path.cwd(),
    )
    outputs = {
        "full-validation": str(full_validation).lower(),
        "checks": json.dumps(check_matrix(full_validation), separators=(",", ":")),
        "installations": json.dumps(installation_matrix(full_validation), separators=(",", ":")),
        "reason": reason,
    }
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for key, value in outputs.items():
            output.write(f"{key}={value}\n")
    print(f"CI mode: {'full' if full_validation else 'routine'}. {reason}")


if __name__ == "__main__":
    main()
