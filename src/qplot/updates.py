"""Select published qPlot releases without downloading or installing packages."""

import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import Tag, sys_tags
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import Version

CHECK_ON_LAUNCH_KEY = "updates.check_on_launch"
INCLUDE_PRERELEASES_KEY = "updates.include_prereleases"
LAST_CHECK_KEY = "updates.last_check"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
CHECK_TIMEOUT_MS = 5000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PYPI_INDEX_URL = "https://pypi.org/simple/qplotter/"
RELEASE_NOTES_URL = "https://github.com/lairdgrouplancaster/QCoDeS-Plotter/releases"


@dataclass(frozen=True)
class UpdateResult:
    current: Version
    latest: Version | None

    @property
    def update_available(self) -> bool:
        return self.latest is not None and self.latest > self.current


def check_due(last_check: float, now: float) -> bool:
    """A clock adjustment must not suppress checks indefinitely."""
    return last_check == 0 or now < last_check or now - last_check >= CHECK_INTERVAL_SECONDS


def latest_release(
    data: bytes,
    *,
    include_prereleases: bool = False,
    python_version: str | None = None,
    supported_tags: Iterable[Tag] | None = None,
) -> Version | None:
    """Read PyPI's JSON Simple API, ignoring yanked/incompatible distributions."""
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("The update response is too large.")
    document = json.loads(data)
    if (
        not isinstance(document, dict)
        or document.get("name") != "qplotter"
        or not isinstance(document.get("files"), list)
        or len(document["files"]) > 10_000
    ):
        raise ValueError("PyPI returned an invalid qPlot release list.")
    python_version = python_version or ".".join(map(str, sys.version_info[:3]))
    tags = set(sys_tags() if supported_tags is None else supported_tags)
    versions: list[Version] = []
    for file in document["files"]:
        if not isinstance(file, dict) or file.get("yanked", False) is not False:
            continue
        filename = file.get("filename")
        if not isinstance(filename, str) or len(filename) > 512 or not filename.endswith(".whl"):
            continue
        try:
            name, version, _build, wheel_tags = parse_wheel_filename(filename)
        except InvalidWheelFilename:
            continue
        if canonicalize_name(name) != "qplotter" or not wheel_tags.intersection(tags):
            continue
        if version.local is not None or (version.is_prerelease and not include_prereleases):
            continue
        requires_python = file.get("requires-python")
        if requires_python is not None and not isinstance(requires_python, str):
            continue
        if requires_python:
            if len(requires_python) > 1024:
                continue
            try:
                if not SpecifierSet(requires_python).contains(python_version, prereleases=True):
                    continue
            except InvalidSpecifier:
                continue
        versions.append(version)
    return max(versions, default=None)
