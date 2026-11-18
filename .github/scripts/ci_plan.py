"""Keep routine desktop CI lean; validate every Python fully before releases."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

PYTHON_VERSIONS = ("3.12", "3.13", "3.14")
PROJECT_FILE = Path(__file__).resolve().parents[2] / "pyproject.toml"


def minimum_pyqt6_version(project_file: Path = PROJECT_FILE) -> str:
    """Test the runtime floor declared by the application, without a second pin."""
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]
    for requirement in project["dependencies"]:
        match = re.fullmatch(r"PyQt6>=([0-9]+(?:\.[0-9]+)+)", requirement)
        if match:
            return match[1]
    raise ValueError("CI requires an explicit PyQt6>=VERSION runtime dependency.")


def validation_mode(
    event_name: str,
    ref: str,
    full_validation: str,
) -> tuple[bool, str]:
    """Select full validation for release tags or an explicit request."""
    if ref.startswith("refs/tags/"):
        return True, "Release tag requires full validation."
    if full_validation == "true":
        return True, "Full validation explicitly requested."
    if full_validation != "false":
        return True, "Validation override unavailable or invalid."
    if event_name not in {"push", "pull_request", "workflow_dispatch", "workflow_call"}:
        return True, "Unknown event requires full validation."
    return False, "Routine desktop validation; full Python and Linux testing run before releases or on request."


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
                "suite": "full" if full_validation or version == "3.14" else "compatibility",
                "native-artifact": artifact,
                "qt-version": "latest",
                "qt-requirement": "PyQt6",
            }
            if entry["suite"] == "full":
                count = 4 if runner == "windows-2025" else 2
                entries.extend({**entry, "partition": f"{index}/{count}"} for index in range(1, count + 1))
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
            "qt-version": "latest",
            "qt-requirement": "PyQt6",
        } for version in PYTHON_VERSIONS)
    # Keep the existing full-suite partitions on latest Qt. Exercise the
    # advertised minimum with the compatibility suite on every Python/platform
    # pair too, without doubling all of the expensive full-suite partitions.
    minimum = minimum_pyqt6_version()
    platforms = sorted({(row["os"], row["native-artifact"]) for row in entries})
    entries.extend({
        "os": runner,
        "python-version": version,
        "suite": "compatibility",
        "native-artifact": artifact,
        "qt-version": minimum,
        "qt-requirement": f"PyQt6=={minimum}",
    } for runner, artifact in platforms for version in PYTHON_VERSIONS)
    for entry in entries:
        if entry["suite"] == "compatibility":
            entry["test-list"] = (
                ".github/pytest-compatibility.txt" if full_validation
                else ".github/pytest-compatibility-routine.txt"
            )
    return {"include": entries}


def application_only_pr(base_sha: str) -> bool:
    """Narrow installation coverage only for a known application, test or docs diff."""
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        return False
    # Disabling rename detection includes both paths when a file moves across
    # the boundary. NUL termination also preserves unusual Git filenames.
    try:
        result = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", base_sha, "HEAD", "--"],
            capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    paths = os.fsdecode(result.stdout).split("\0")[:-1]
    sensitive = (
        "src/qplot/datahandling/trusted_", "src/qplot/datahandling/_trusted_",
        "src/qplot/datahandling/readonly.py", "src/qplot/_version.py",
        "src/qplot/_metadata.py", "src/qplot/_build_info.json",
        "tests/infrastructure/", "tests/ci_timings.json", "tests/_ci_scheduling.py",
    )
    return bool(paths) and all(
        path.startswith(("src/qplot/", "tests/", "docs/")) and not path.startswith(sensitive)
        for path in paths
    )


def installation_matrix(full_validation: bool, *, application_only: bool = False) -> dict[str, list[dict[str, str]]]:
    arm64_versions = PYTHON_VERSIONS[-1:] if application_only and not full_validation else PYTHON_VERSIONS
    platforms = [
        ("macos-arm64", "macOS ARM64", "macos-latest", arm64_versions),
        ("macos-intel", "macOS Intel", "macos-15-intel", PYTHON_VERSIONS),
        ("windows-x64", "Windows x64", "windows-2025", PYTHON_VERSIONS),
    ]
    if full_validation:
        platforms.insert(0, ("linux-x86_64", "Linux x86_64", "ubuntu-latest", PYTHON_VERSIONS))
    return {"include": [{
        "platform": platform,
        "os-name": name,
        "os": runner,
        "native-artifact": f"qplotter-native-{platform}",
        "python-version": version,
    } for platform, name, runner, versions in platforms for version in versions]}


def platform_matrices(full_validation: bool, *, application_only: bool = False) -> dict[str, dict]:
    """Keep each consumer dependent only on its own platform's native build."""
    checks = check_matrix(full_validation)["include"]
    installations = installation_matrix(full_validation, application_only=application_only)["include"]
    return {
        platform: {
            "checks": {"include": [row for row in checks
                                   if row["native-artifact"] == f"qplotter-native-{platform}"]},
            "installations": {"include": [row for row in installations
                                          if row["platform"] == platform]},
        }
        for platform in ("linux-x86_64", "windows-x64", "macos-arm64", "macos-intel")
    }


def main() -> None:
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    full_validation, reason = validation_mode(
        event,
        os.environ.get("GITHUB_REF", ""),
        os.environ.get("QPLOT_FULL_VALIDATION", ""),
    )
    application_only = (
        not full_validation and event == "pull_request"
        and application_only_pr(os.environ.get("QPLOT_PR_BASE_SHA", ""))
    )
    if application_only:
        reason += f" ARM64 compiler-free installation runs on Python {PYTHON_VERSIONS[-1]} for this PR."
    platforms = platform_matrices(full_validation, application_only=application_only)
    outputs = {
        "full-validation": str(full_validation).lower(),
        "platforms": json.dumps(platforms, separators=(",", ":")),
        "reason": reason,
    }
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for key, value in outputs.items():
            output.write(f"{key}={value}\n")
    print(f"CI mode: {'full' if full_validation else 'routine'}. {reason}")


if __name__ == "__main__":
    main()
