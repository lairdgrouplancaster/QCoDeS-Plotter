"""Resolve repository-only Markdown links at the documentation source revision."""

import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from scripts.screenshot_support import SIZES, sha256


def on_page_markdown(markdown, *, page, config, files):
    # Read the Docs supplies the Git identifier, including slash-containing tags.
    # Local/CI builds use an immutable commit rather than guessing a version.
    ref = os.environ.get("READTHEDOCS_GIT_COMMIT_HASH") or os.environ.get("READTHEDOCS_GIT_IDENTIFIER")
    if not ref:
        ref = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, timeout=10).strip()
    base = f"{config['repo_url']}/blob/{quote(ref, safe='')}/"

    def replace(match):
        target = match[1]
        source = PurePosixPath(page.file.src_uri).parent / target
        parts = []
        for part in (PurePosixPath("docs") / source).parts:
            if part == "..":
                parts.pop()
            else:
                parts.append(part)
        return f"]({base}{'/'.join(parts)}{match[2] or ''})"

    return re.sub(r"\]\((\.\./[^)#]+|assets/branding/[^)#]+)(#[^)]*)?\)", replace, markdown)


def on_post_build(*, config):
    """Prove the built website contains the exact committed screenshot bytes."""
    for name in SIZES:
        source = Path(config["docs_dir"]) / "assets" / name
        built = Path(config["site_dir"]) / "assets" / name
        if not built.is_file() or sha256(source) != sha256(built):
            raise ValueError(f"Website omitted or altered approved screenshot: {name}")
