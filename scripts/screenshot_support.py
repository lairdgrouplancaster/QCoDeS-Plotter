"""Screenshot identity, input freshness and PNG checks (no Qt imports)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = "qplot-screenshots.json"
SIZES = {
    "qplot-main-window.png": (1120, 760),
    "qplot-line-plot.png": (920, 620),
    "qplot-heatmap.png": (980, 660),
    "qplot-color-scale-dialog.png": (700, 700),
}
# Conservative scope: all application/native sources and resources, dependency
# specifications and the capture implementation/environment. Never docs output.
INPUT_PATHS = (
    "src/qplot", "native", "pyproject.toml", "requirements/screenshots.txt",
    "scripts/capture_demo_screenshots.py", "scripts/screenshot_support.py",
    ".github/workflows/release-preparation.yml",
)


def git(root, *arguments):
    return subprocess.check_output(
        ["git", *arguments], cwd=root, timeout=30,
    ).decode().strip()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def input_sha256(path):
    """Canonicalise UTF-8 text line endings across Git checkout platforms."""
    data = Path(path).read_bytes()
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        if b"\0" not in data:
            data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def fingerprint(root=ROOT):
    """Hash sorted repo-relative names and bytes, including uncommitted inputs.

    Git's non-ignored file inventory avoids caches, build output and native
    binaries. Missing tracked files count as deletions. NUL handles all names.
    """
    inventory = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z",
         "--", *INPUT_PATHS], cwd=root, timeout=30,
    )
    names = sorted({os.fsdecode(name) for name in inventory.split(b"\0") if name})
    files = {name: input_sha256(root / name) for name in names if (root / name).is_file()}
    if not files:
        raise ValueError("No screenshot inputs found; use a Git checkout")
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"algorithm": "qplot-screenshot-inputs-v1", "sha256": digest, "files": files}


def application_version(root=ROOT):
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


def validate_images(assets, *, exact_dimensions=False):
    """Fully decode PNGs; detect gross blankness/clipping without golden pixels."""
    from PIL import Image, ImageStat

    results = {}
    for name, expected in SIZES.items():
        path = assets / name
        with Image.open(path) as image:
            if image.format != "PNG":
                raise ValueError(f"{name} is not a PNG")
            image.verify()
        with Image.open(path) as image:
            image.load()  # verify alone does not decode the compressed pixels
            width, height = image.size
            if exact_dimensions:
                valid_size = image.size == expected
            else:
                # Also accept the previous approved set in ordinary PR checks.
                valid_size = 600 <= width <= 2400 and 450 <= height <= 1800
            if not valid_size:
                raise ValueError(f"Unexpected dimensions for {name}: {image.size}")
            rgb = image.convert("RGB")
            if image.mode == "RGBA" and image.getextrema()[3][0] != 255:
                raise ValueError(f"Unexpected transparency in {name}")
            # Check the whole window and each quadrant. This rejects empty or
            # partly missing captures, while tolerating fonts and antialiasing.
            for box in (None, (0, 0, width // 2, height // 2),
                        (width // 2, 0, width, height // 2),
                        (0, height // 2, width // 2, height),
                        (width // 2, height // 2, width, height)):
                sample = (rgb if box is None else rgb.crop(box)).resize((80, 60))
                stats = ImageStat.Stat(sample)
                if max(stats.stddev) < 2 or len(sample.getcolors(4800) or []) < 8:
                    raise ValueError(f"Blank or incomplete screenshot: {name}")
            results[name] = {"sha256": sha256(path), "dimensions": [width, height]}
    return results


def validate_references(root=ROOT):
    text = (root / "docs/demo-data.md").read_text()
    for name in SIZES:
        if f"](assets/{name})" not in text:
            raise ValueError(f"Documentation does not reference {name}")


def verify_release(root=ROOT, assets=None, *, generation_id=None, require_clean=False):
    """Verify final candidate inputs and reviewed bytes, independent of SHA."""
    assets = assets or root / "docs/assets"
    if require_clean and git(root, "status", "--porcelain"):
        raise ValueError("Pre-tag verification requires a clean, committed release candidate")
    record = json.loads((assets / RECORD).read_text())
    if record["schema"] != 1 or record["application_version"] != application_version(root):
        raise ValueError("Screenshot record has an incompatible schema or version")
    if record["inputs"] != fingerprint(root):
        raise ValueError("Screenshot inputs changed: regenerate and review all screenshots")
    if record["images"] != validate_images(assets, exact_dimensions=True):
        raise ValueError("Screenshot files differ from the generated, reviewed set")
    validate_references(root)
    required = {"selected_data", "rendered", "shutdown", "database_unchanged"}
    if not all(record["checks"].get(name) is True for name in required):
        raise ValueError("Capture did not report all required application checks")
    if generation_id is not None and record["generation_id"] != generation_id:
        raise ValueError("Record is not from the intended release preparation cycle")
    if not record["environment"] or not record["source_commit"] or not record["generation_id"]:
        raise ValueError("Incomplete screenshot provenance")
    return record


def install_set(staged, destination):
    """Publish a complete validated set; restore prior files on write failure.

    Same-directory staging makes each individual replacement atomic. A series
    of files cannot be power-loss atomic; the provenance hashes detect a torn
    set. No destination is touched until every safety preflight has passed.
    """
    from qplot._auxiliary_paths import ensure_safe_auxiliary_path

    names = [*SIZES, RECORD]
    payload = staged if isinstance(staged, dict) else {
        name: (staged / name).read_bytes() for name in names
    }
    destination.mkdir(parents=True, exist_ok=True)
    for name in names:
        ensure_safe_auxiliary_path(destination / name)
    previous = {}
    replaced = []
    try:
        with tempfile.TemporaryDirectory(prefix=".qplot-screenshots-", dir=destination) as work:
            work = Path(work)
            for name in names:
                path = destination / name
                previous[name] = path.read_bytes() if path.exists() else None
                (work / name).write_bytes(payload[name])
            for name in names:
                ensure_safe_auxiliary_path(destination / name)
                os.replace(work / name, destination / name)
                replaced.append(name)
    except BaseException:
        # This also covers failure to clean the staging directory after writes.
        for name in reversed(replaced):
            path = destination / name
            ensure_safe_auxiliary_path(path)
            if previous[name] is None:
                path.unlink()
            else:
                descriptor, backup = tempfile.mkstemp(prefix=".qplot-rollback-", dir=destination)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(previous[name])
                    os.replace(backup, path)
                finally:
                    Path(backup).unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "verify-release"))
    parser.add_argument("--assets", type=Path, default=ROOT / "docs/assets")
    parser.add_argument("--generation-id", help="Expected workflow run/attempt or local generation ID")
    parser.add_argument("--require-clean", action="store_true", help="Require a committed final candidate")
    args = parser.parse_args()
    if args.command == "verify-release":
        record = verify_release(assets=args.assets, generation_id=args.generation_id,
                                require_clean=args.require_clean)
        print(f"Fresh screenshots for {record['application_version']}: {record['generation_id']}")
        print("Visual review and verification of the intended release commit remain required.")
    else:
        validate_images(args.assets)
        validate_references()
        print("All four PNGs and documentation references are valid.")


if __name__ == "__main__":
    main()
