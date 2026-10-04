"""Invalid user JSON decoder limits must not prevent real GUI startup."""

import json
from pathlib import Path

import pytest

from qplot.configuration.config import config
from qplot.diagnostics import configure_logging
from qplot.windows.main import MainWindow
from tests._window_lifecycle import close_main_window


def _invalid_settings(kind):
    if kind == "integer_limit":
        return b'{"GUI":{"preview_size":' + b"9" * 5000 + b"}}"
    if kind == "decoder_nesting":
        return b"[" * 1500 + b"0" + b"]" * 1500
    # This parses successfully but fails schema validation. Diagnostics must
    # not recursively pretty-print its deeply nested invalid instance.
    return b'{"unexpected":' + b"[" * 600 + b"0" + b"]" * 600 + b"}"


def _decoder_overflow_json():
    """Choose a nesting depth rejected by this interpreter's JSON decoder."""
    for depth in (1500, 3000, 6000, 12000, 24000):
        document = b"[" * depth + b"0" + b"]" * depth
        try:
            json.loads(document)
        except RecursionError:
            return document
    raise AssertionError("JSON decoder accepted every tested nesting depth")


@pytest.mark.parametrize("kind", ["integer_limit", "decoder_nesting", "schema_nesting"])
def test_decoder_limits_recover_defaults_and_start_window(tmp_path, monkeypatch, capsys, kind):
    home = tmp_path / "settings"
    home.mkdir()
    settings = home / "config.json"
    original = _invalid_settings(kind)
    settings.write_bytes(original)
    monkeypatch.setattr(config, "default_path", str(home))
    monkeypatch.setattr(config, "default_file", str(settings))
    log_file = home / "qplot.log"
    configure_logging(log_file, force=True)

    window = MainWindow()
    try:
        window.startupDatabaseTimer.stop()
        window.monitor.stop()
        assert window.config.startup_warning
        assert window.config.config == window.config.build_default_config()
        assert window.config.get("GUI.preview_size") == 200
        backup = Path(window.config.invalid_config_backup_file)
        assert backup.read_bytes() == original
        assert json.loads(settings.read_text(encoding="utf-8")) == window.config.config
        assert not list(home.glob(".config.json.*.tmp"))
        assert "Invalid configuration" in log_file.read_text(encoding="utf-8")
        assert "--- Logging error ---" not in capsys.readouterr().err
    finally:
        close_main_window(window)


@pytest.mark.parametrize("kind,exception", [
    ("integer_limit", ValueError),
    ("decoder_nesting", RecursionError),
])
def test_packaged_schema_decoder_limits_are_not_hidden(tmp_path, monkeypatch, kind, exception):
    schema = tmp_path / "schema.json"
    schema.write_bytes(
        _decoder_overflow_json() if kind == "decoder_nesting" else _invalid_settings(kind)
    )
    settings = tmp_path / "config.json"
    settings.write_bytes(b"original user settings remain untouched")
    monkeypatch.setattr(config, "default__schema_file", str(schema))
    monkeypatch.setattr(config, "default_file", str(settings))
    with pytest.raises(exception):
        config()
    assert settings.read_bytes() == b"original user settings remain untouched"
    assert not list(tmp_path.glob("config.invalid*"))
