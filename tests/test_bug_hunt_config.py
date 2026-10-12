"""Regression coverage for settings recovery discovered during the bug hunt."""

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
