"""Invalid settings recover to current defaults while preserving the original."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import ANY, patch

import pytest

from qplot.configuration.config import config


@pytest.mark.parametrize("occupied_names", [
    ["config.invalid.json"],
    ["config.invalid.json", "config.invalid.1.json"],
])
def test_invalid_config_recovery_skips_dangling_backup_links(tmp_path, occupied_names):
    settings = tmp_path / "config.json"
    original = b"invalid JSON settings"
    settings.write_bytes(original)
    for name in occupied_names:
        try:
            (tmp_path / name).symlink_to(tmp_path / "absent-target")
        except OSError:
            pytest.skip("Symbolic links unavailable")

    # A subprocess deadline turns the original infinite retry into a bounded
    # failure without leaving a busy recovery thread in the test process.
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from qplot.configuration.config import config
from qplot.diagnostics import configure_logging
config.default_path = sys.argv[1]
config.default_file = sys.argv[2]
configure_logging(sys.argv[3], force=True)
recovered = config()
recovered.validate(recovered.config)
print(recovered.invalid_config_backup_file)
""", str(tmp_path), str(settings), str(tmp_path / "diagnostics.log")],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    backup = Path(result.stdout.strip())
    assert backup.name == f"config.invalid.{len(occupied_names)}.json"
    assert backup.read_bytes() == original
    assert settings.read_bytes() != original
    for name in occupied_names:
        assert (tmp_path / name).is_symlink()
    assert not (tmp_path / "absent-target").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO required")
@pytest.mark.parametrize("symlink", [False, True])
def test_config_fifo_uses_defaults_without_blocking_or_modifying_source(tmp_path, symlink):
    settings_directory = tmp_path / "settings"
    settings_directory.mkdir()
    settings = settings_directory / "config.json"
    fifo = settings_directory / "pipe" if symlink else settings
    os.mkfifo(fifo)
    if symlink:
        settings.symlink_to(fifo)
    def artifact_state():
        # Resolving a symlink may update its access time on Linux. Identity,
        # permissions, contents and modification/change times must stay intact.
        fields = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid",
                  "st_size", "st_mtime_ns", "st_ctime_ns")
        return {entry.name: tuple(getattr(entry.lstat(), field) for field in fields)
                for entry in settings_directory.iterdir()}

    before = artifact_state()

    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from qplot.configuration.config import config
from qplot.diagnostics import configure_logging
config.default_path = sys.argv[1]
config.default_file = sys.argv[2]
configure_logging(sys.argv[3], force=True)
recovered = config()
recovered.validate(recovered.config)
assert recovered.config == recovered.build_default_config()
assert "Could not read configuration" in recovered.startup_warning
""", str(settings_directory), str(settings), str(tmp_path / "diagnostics.log")],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert artifact_state() == before
    assert fifo.is_fifo()
    assert settings.is_symlink() == symlink
    if symlink:
        assert os.readlink(settings) == str(fifo)


@pytest.fixture(params=[
    "missing-section", "missing-plot-settings", "missing-heatmap-limits",
    "missing-preview-size", "missing-csv-format", "unknown-setting",
])
def incompatible_settings(tmp_path, monkeypatch, request):
    """Exercise current-schema rejection without maintaining old settings schemas."""
    monkeypatch.setattr(config, "default_path", str(tmp_path))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "config.json"))
    cfg = config()
    defaults = cfg.build_default_config()
    stored = cfg.config
    stored["user_preference"].update({
        "theme": "dark", "bar_colour": "CET-L1", "default_refresh_rate": 4.0,
    })
    stored["file"]["default_load_path"] = "/custom/qcodes/path"
    match request.param:
        case "missing-section":
            del stored["updates"]
        case "missing-plot-settings":
            for key in ("colorbar_width", "axis_tick_width", "axis_major_tick_count"):
                del stored["user_preference"][key]
            stored["user_preference"]["axis_tick_density"] = 0.1
        case "missing-heatmap-limits":
            del stored["runtime_settings"]["max_heatmap_grid_cells"]
            del stored["runtime_settings"]["max_heatmap_grid_side"]
        case "missing-preview-size":
            del stored["GUI"]["preview_size"]
        case "missing-csv-format":
            del stored["user_preference"]["csv_filename_format"]
        case "unknown-setting":
            stored["file"]["last_file_path"] = "/old/database.db"
        case _:
            raise AssertionError(request.param)
    path = Path(cfg.default_file)
    original = json.dumps(stored).encode("utf-8")
    path.write_bytes(original)
    return path, original, defaults


def test_incompatible_settings_are_backed_up_and_reset_to_current_defaults(incompatible_settings):
    path, original, defaults = incompatible_settings

    recovered = config()

    assert recovered.config == defaults
    recovered.validate(recovered.config)
    assert Path(recovered.invalid_config_backup_file).read_bytes() == original
    assert json.loads(path.read_text(encoding="utf-8")) == defaults
    assert config().config == defaults
    assert not list(path.parent.glob(".config.json.*.tmp"))


def test_failed_recovery_retains_defaults_in_memory_and_original_on_disk(incompatible_settings):
    path, original, defaults = incompatible_settings
    with (
        patch.object(config, "save_config", side_effect=PermissionError("simulated recovery failure")) as save,
        patch("qplot.configuration.config.log_exception") as logged,
    ):
        recovered = config()

    assert recovered.config == defaults
    recovered.validate(recovered.config)
    assert recovered.startup_warning
    assert Path(recovered.invalid_config_backup_file).read_bytes() == original
    assert path.read_bytes() == original
    save.assert_called_once()
    logged.assert_any_call(
        f"Could not replace invalid configuration at {path}", ANY, "qplot.configuration.config",
    )
    assert not list(path.parent.glob(".config.json.*.tmp"))
