"""Require every application module to be checked or explicitly deferred."""

import argparse
import sys
import tomllib
from collections import Counter
from pathlib import Path


def inventory_errors(root: Path) -> list[str]:
    """Compare exact source paths with mypy targets and reasoned deferrals."""
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    checked = config["tool"]["mypy"]["files"]
    deferred = config["tool"]["qplot"]["typing"]["deferred"]
    if not isinstance(checked, list) or not all(isinstance(p, str) for p in checked):
        raise ValueError("tool.mypy.files must be an array of exact source paths")
    if not isinstance(deferred, dict):
        raise ValueError("tool.qplot.typing.deferred must map source paths to reasons")

    modules = {
        path.relative_to(root).as_posix() for path in (root / "src/qplot").rglob("*.py")
    }
    if not modules:
        return ["No application modules found in src/qplot"]
    errors = []
    for path, count in sorted(Counter(checked).items()):
        if count > 1:
            errors.append(f"Duplicate mypy target: {path}")
    for path, reason in sorted(deferred.items()):
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"Missing deferral reason: {path}")
    checked_paths = set(checked)
    deferred_paths = set(deferred)
    for path in sorted(checked_paths & deferred_paths):
        errors.append(f"Checked module still deferred: {path}")
    for path in sorted((checked_paths | deferred_paths) - modules):
        errors.append(f"Stale or non-canonical inventory path: {path}")
    for path in sorted(modules - checked_paths - deferred_paths):
        errors.append(f"Module needs a mypy target or deferral reason: {path}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args(argv)
    try:
        errors = inventory_errors(args.root)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Invalid typing inventory: {error}", file=sys.stderr)
        return 1
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Typing inventory covers every application module.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
