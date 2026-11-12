"""Keep routine desktop CI lean; validate every Python fully before releases."""

from __future__ import annotations

import json
import os
from pathlib import Path

PYTHON_VERSIONS = ("3.12", "3.13", "3.14")


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
        } for version in PYTHON_VERSIONS)
    for entry in entries:
        if entry["suite"] == "compatibility":
            entry["test-list"] = (
                ".github/pytest-compatibility.txt" if full_validation
                else ".github/pytest-compatibility-routine.txt"
            )
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


def platform_matrices(full_validation: bool) -> dict[str, dict]:
    """Keep each consumer dependent only on its own platform's native build."""
    checks = check_matrix(full_validation)["include"]
    installations = installation_matrix(full_validation)["include"]
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
    full_validation, reason = validation_mode(
        os.environ.get("GITHUB_EVENT_NAME", ""),
        os.environ.get("GITHUB_REF", ""),
        os.environ.get("QPLOT_FULL_VALIDATION", ""),
    )
    outputs = {
        "full-validation": str(full_validation).lower(),
        "platforms": json.dumps(platform_matrices(full_validation), separators=(",", ":")),
        "reason": reason,
    }
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for key, value in outputs.items():
            output.write(f"{key}={value}\n")
    print(f"CI mode: {'full' if full_validation else 'routine'}. {reason}")


if __name__ == "__main__":
    main()
