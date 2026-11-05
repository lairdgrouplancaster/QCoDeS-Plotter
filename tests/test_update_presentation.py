import shlex
from html import escape

import pytest
from packaging.version import Version

from qplot._update_presentation import update_message
from qplot.updates import UpdateResult


@pytest.mark.parametrize("executable", [
    "/opt/qplot/bin/python",
    "/Users/A Person/qplot's & tools/bin/python",
    '/tmp/quotes"and $shell;characters/python',
])
def test_posix_command_preserves_executable_and_matches_displayed_instructions(executable):
    message = update_message(
        UpdateResult(Version("1.0"), Version("2.0rc1")), "",
        source_checkout=False, executable=executable, platform="darwin",
    )

    assert shlex.split(message.upgrade_command) == [
        executable, "-m", "pip", "install", "--upgrade", "--only-binary=:all:",
        "qplotter==2.0rc1",
    ]
    assert f"<code>{escape(message.upgrade_command)}</code>" in message.html


def test_windows_command_quotes_interpreter_path():
    message = update_message(
        UpdateResult(Version("1.0"), Version("2.0")), "",
        source_checkout=False, executable=r"C:\Program Files\qPlot\python.exe", platform="win32",
    )

    assert message.upgrade_command == (
        r'"C:\Program Files\qPlot\python.exe" -m pip install --upgrade '
        "--only-binary=:all: qplotter==2.0"
    )
    assert f"<code>{escape(message.upgrade_command)}</code>" in message.html


@pytest.mark.parametrize("latest", [None, "0.9", "1.0", "2.0"])
@pytest.mark.parametrize("source_checkout", [False, True])
def test_commands_are_only_offered_for_new_installed_releases(latest, source_checkout):
    message = update_message(
        UpdateResult(Version("1.0"), Version(latest) if latest is not None else None), "",
        source_checkout=source_checkout, executable="python", platform="linux",
    )

    assert bool(message.upgrade_command) == (latest == "2.0" and not source_checkout)
    assert ("source checkout" in message.html) == (latest == "2.0" and source_checkout)
    assert ("No release matching" in message.html) == (latest is None)
    assert ("No newer release available" in message.html) == (latest != "2.0")


def test_error_text_is_escaped_and_has_no_install_command():
    message = update_message(
        None, '<failure & "retry">', source_checkout=False,
        executable="python", platform="linux",
    )

    assert message.html == (
        "<h2>Could not check for updates</h2><p>&lt;failure &amp; &quot;retry&quot;&gt;</p>"
    )
    assert message.upgrade_command == ""
