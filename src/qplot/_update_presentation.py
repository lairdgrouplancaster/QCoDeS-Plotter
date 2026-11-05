"""Update-check messages and copyable commands, independent of Qt and networking."""

import shlex
import subprocess
from dataclasses import dataclass
from html import escape

from qplot.updates import RELEASE_NOTES_URL, UpdateResult


@dataclass(frozen=True)
class UpdateMessage:
    html: str
    upgrade_command: str = ""


CHECKING_MESSAGE = UpdateMessage("<h2>Checking for updates…</h2><p>Contacting PyPI…</p>")


def _upgrade_command(
    result: UpdateResult | None, *, source_checkout: bool, executable: str, platform: str,
) -> str:
    if result is None or not result.update_available or source_checkout:
        return ""
    arguments = [executable, "-m", "pip", "install", "--upgrade",
                 "--only-binary=:all:", f"qplotter=={result.latest}"]
    return subprocess.list2cmdline(arguments) if platform == "win32" else shlex.join(arguments)


def update_message(
    result: UpdateResult | None,
    error: str,
    *,
    source_checkout: bool,
    executable: str,
    platform: str,
) -> UpdateMessage:
    """Build displayed instructions and clipboard text from the same command."""
    command = _upgrade_command(
        result, source_checkout=source_checkout, executable=executable, platform=platform,
    )
    if error:
        return UpdateMessage(
            f"<h2>Could not check for updates</h2><p>{escape(error)}</p>", command,
        )
    assert result is not None
    heading = "A qPlot update is available" if result.update_available else "No newer release available"
    html = f"<h2>{heading}</h2><p>You are running qPlot <b>{result.current}</b>.</p>"
    if result.latest is None:
        html += "<p>No release matching your update preferences and Python version was found on PyPI.</p>"
    else:
        html += f"<p>Latest matching published release: <b>{result.latest}</b>.</p>"
    html += f'<p><a href="{RELEASE_NOTES_URL}">Release notes</a> · '
    html += '<a href="https://pypi.org/project/qplotter/">qPlot on PyPI</a></p>'
    if result.update_available:
        if source_checkout:
            html += (
                "<p>This is a source checkout. Close qPlot and update your checkout "
                "using your usual Git workflow.</p>"
            )
        else:
            html += (
                "<p>Close qPlot, then run this command in a terminal to upgrade "
                "the Python environment used by this installation:</p>"
                f"<p><code>{escape(command)}</code></p>"
            )
    return UpdateMessage(html, command)
