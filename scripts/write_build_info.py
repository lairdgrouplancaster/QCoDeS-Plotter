"""Stamp application artifacts with the exact CI checkout before packaging."""

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qplot._metadata import checkout_info  # noqa: E402


def main():
    info = checkout_info(ROOT)
    if not info.get("commit") or info.get("dirty"):
        raise RuntimeError("Build provenance requires a clean Git checkout")
    info.pop("checkout", None)
    ref = os.environ.get("GITHUB_REF", "")
    info["released"] = ref in {
        f"refs/tags/v{info['version']}",
        f"refs/tags/native-and-app/v{info['version']}",
    } or info["released"]
    pull_request = re.fullmatch(r"refs/pull/(\d+)/(?:merge|head)", ref)
    if pull_request:
        info["pull_request"] = pull_request[1]
    (ROOT / "src/qplot/_build_info.json").write_text(json.dumps(info) + "\n")


if __name__ == "__main__":
    main()
