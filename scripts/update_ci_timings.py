"""Refresh the scheduling baseline from complete, successful JUnit reports."""

from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path


def report_timings(path: Path) -> dict[str, dict[str, float | int]]:
    root = ET.parse(path).getroot()
    if root.find(".//failure") is not None or root.find(".//error") is not None:
        raise ValueError(f"{path}: use successful runs, not partial/failing reports")
    durations: dict[str, float] = {}
    tests: dict[str, set[tuple[str, str]]] = {}
    for case in root.iter("testcase"):
        filename = case.get("file", "").replace("\\", "/")
        if (
            not filename.startswith("tests/")
            or not filename.endswith(".py")
            or ".." in filename.split("/")
        ):
            raise ValueError(f"{path}: missing relative test file; use -o junit_family=xunit1")
        seconds = float(case.get("time", "0"))
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"{path}: invalid duration for {filename}")
        durations[filename] = durations.get(filename, 0.0) + seconds
        tests.setdefault(filename, set()).add((case.get("classname", ""), case.get("name", "")))
    if not durations:
        raise ValueError(f"{path}: no test results")
    return {
        filename: {"seconds": round(seconds, 3), "tests": len(tests[filename])}
        for filename, seconds in durations.items()
    }


def merge_reports(paths: list[Path]) -> dict:
    """Use the slowest observed file cost across platforms/repeated runs."""
    files: dict[str, dict[str, float | int]] = {}
    for path in paths:
        for filename, record in report_timings(path).items():
            previous = files.get(filename)
            if previous is None or record["seconds"] > previous["seconds"]:
                files[filename] = record
    return {
        "version": 1,
        "source": {
            "description": "Maximum file setup/call/teardown total from successful JUnit reports.",
            "reports": sorted(path.name for path in paths),
        },
        "files": dict(sorted(files.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path, nargs="+", help="Include both full-suite partitions")
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parents[1] / "tests/ci_timings.json",
    )
    args = parser.parse_args()
    try:
        profile = merge_reports(args.reports)
    except (OSError, ET.ParseError, ValueError) as error:
        parser.error(str(error))
    args.output.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded {len(profile['files'])} file timings in {args.output}")


if __name__ == "__main__":
    main()
