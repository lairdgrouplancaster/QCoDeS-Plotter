"""Regression coverage for settings recovery discovered during the bug hunt."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


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
