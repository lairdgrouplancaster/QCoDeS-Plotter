"""The configuration CLI exposes only its documented commands."""

import importlib

import pytest

cli = importlib.import_module("qplot.configuration.scripts")

COMMANDS = ["-dump", "-find", "-info", "-reset", "-set_value", "-version"]


@pytest.fixture
def forbid_settings_access(monkeypatch):
    def unexpected_config():
        raise AssertionError("Command discovery and help must not access settings")

    monkeypatch.setattr(cli, "config", unexpected_config)


def test_public_helpers_do_not_become_commands(monkeypatch, capsys, forbid_settings_access):
    def unrelated_helper(self):
        raise AssertionError("An unregistered helper must not be dispatched")

    monkeypatch.setattr(cli.sysHandle, "helper", unrelated_helper, raising=False)

    handler = cli.sysHandle("-info")

    assert handler.valid_args == COMMANDS
    assert capsys.readouterr().out == (
        f"Valid Commands:\n\t{COMMANDS}\n"
        "Use 'qplot-cfg -info <command>' for more info\n"
    )
    with pytest.raises(KeyError) as error:
        cli.sysHandle("-helper")
    assert error.value.args == (f"Command: (-helper), not found. Valid options: {COMMANDS}",)

    with pytest.raises(KeyError, match="Command: \\(helper\\), not found"):
        cli.sysHandle("-info", "helper")


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("prefix", ["", "-"])
def test_help_accepts_each_command_with_or_without_dash(
    command, prefix, capsys, forbid_settings_access,
):
    cli.sysHandle("-info", prefix + command[1:])

    assert command in capsys.readouterr().out


def test_unknown_command_help_preserves_error_details(forbid_settings_access):
    with pytest.raises(KeyError) as error:
        cli.sysHandle("-info", "unknown")

    assert error.value.args == (
        f"Command: (unknown), not found. Valid options: {COMMANDS}, '-' may be ommited",
    )
