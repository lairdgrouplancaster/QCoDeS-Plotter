import json
import re
import subprocess
import time
import tomllib
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path


def package_version():
    """
    Return the installed qPlot package version.

    """
    try:
        return version("qplotter")
    except PackageNotFoundError:
        return "0+unknown"


def _git(root: Path, *arguments: str, deadline: float) -> str:
    """Read bounded local Git metadata without refreshing the index."""
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise subprocess.TimeoutExpired("git", timeout=0)
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *arguments],
        capture_output=True, text=True, check=True, timeout=timeout,
    ).stdout.strip()


def checkout_info(root: Path) -> dict:
    """Identify this source tree, never an enclosing or unrelated repository."""
    if not (root / ".git").exists() or not (root / "pyproject.toml").is_file():
        return {}
    info: dict = {}
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        if project["name"] != "qplotter":
            return {}
        info = {"version": project["version"], "checkout": True}
        # Cold Git startup can exceed one second on Windows. Share one budget
        # across all three reads so startup and About remain bounded.
        deadline = time.monotonic() + 5
        commit = _git(root, "rev-parse", "HEAD", deadline=deadline)
        dirty = bool(_git(
            root, "status", "--porcelain", "--untracked-files=normal", deadline=deadline,
        ))
        tags = _git(root, "tag", "--points-at", "HEAD", deadline=deadline).splitlines()
        release_tags = {f"v{project['version']}", f"native-and-app/v{project['version']}"}
        info.update(commit=commit, dirty=dirty, released=bool(release_tags.intersection(tags)) and not dirty)
        return info
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        # Git is optional for source installations; the source version still helps.
        return info


def version_info() -> dict:
    """Return version and available provenance for the code actually being used.

    Checkouts take precedence over installed metadata, which can be stale for
    editable installs. Wheels carry CI provenance; pip VCS installs can instead
    supply a commit in direct_url.json. No network access is needed.
    """
    source = checkout_info(Path(__file__).resolve().parents[2])
    if source:
        return source
    info = {"version": package_version()}
    try:
        build = json.loads(Path(__file__).with_name("_build_info.json").read_text())
        if isinstance(build, dict) and build.get("version") == info["version"]:
            info.update(build)
            return info
    except (OSError, ValueError):
        # Missing or malformed build metadata leaves the installed version as the
        # authoritative fallback.
        pass
    try:
        direct = json.loads(distribution("qplotter").read_text("direct_url.json") or "{}")
        vcs = direct.get("vcs_info", {})
        commit = vcs.get("commit_id", "")
        if vcs.get("vcs") == "git" and re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
            info["commit"] = commit
    except (PackageNotFoundError, ValueError, AttributeError, TypeError):
        # Missing editable-install metadata leaves the installed version as the
        # authoritative fallback.
        pass
    return info
