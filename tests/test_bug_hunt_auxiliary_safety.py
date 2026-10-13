"""Auxiliary writes must not reach SQLite SHM through a hard-link alias."""

import os

import pytest

from qplot import diagnostics
from qplot.configuration.config import config
from tests.test_bug_hunt_generator_safety import measurement_files as _measurement_files

measurement_files = _measurement_files


def _link(source, destination):
    try:
        os.link(source, destination)
    except OSError:
        pytest.skip("Hard links unavailable on this filesystem")


def _state(paths):
    return {str(path): (path.stat().st_ino, path.read_bytes()) for path in paths}


@pytest.mark.parametrize("already_open", [False, True])
def test_logging_never_appends_to_shm_hardlink(tmp_path, measurement_files, already_open):
    target = tmp_path / "qplot.log"
    shm = measurement_files["shm"]
    if already_open:
        # The logger is initially legitimate; a later hard link makes its
        # already-open inode the database's exact SHM artifact.
        target.write_bytes(shm.read_bytes())
        logger = diagnostics.configure_logging(log_file=target, force=True)
        shm.unlink()
        _link(target, shm)
    else:
        _link(shm, target)
        logger = diagnostics.configure_logging(log_file=target, force=True)
    before = _state([*measurement_files.values(), target])
    try:
        logger.info("Must not append through the SHM alias")
        for handler in logger.handlers:
            handler.flush()
        assert _state([*measurement_files.values(), target]) == before
    finally:
        diagnostics._reset_logging_for_tests()


@pytest.mark.parametrize("backup_index", [1, 2])
def test_logging_rotation_preserves_shm_alias_entries(tmp_path, measurement_files, backup_index):
    target = tmp_path / "qplot.log"
    target.write_text("ordinary log\n")
    first = tmp_path / "qplot.log.1"
    last = tmp_path / "qplot.log.2"
    alias = first if backup_index == 1 else last
    other = last if backup_index == 1 else first
    other.write_text("ordinary backup\n")
    _link(measurement_files["shm"], alias)
    before = _state([*measurement_files.values(), target, first, last])
    handler = diagnostics._DatabaseSafeRotatingFileHandler(target, backupCount=2)
    try:
        handler.doRollover()
        assert _state([*measurement_files.values(), target, first, last]) == before
    finally:
        handler.close()


def test_settings_recovery_preserves_shm_hardlink(tmp_path, measurement_files, monkeypatch):
    target = tmp_path / "config.json"
    _link(measurement_files["shm"], target)
    monkeypatch.setattr(config, "default_path", str(tmp_path))
    monkeypatch.setattr(config, "default_file", str(target))
    before = _state([*measurement_files.values(), target])
    cfg = config()
    cfg.validate(cfg.config)
    assert cfg.startup_warning
    assert _state([*measurement_files.values(), target]) == before
    assert not list(tmp_path.glob("config.invalid*"))
    with pytest.raises(OSError, match="hard links"):
        cfg.save_config(str(target))
    assert _state([*measurement_files.values(), target]) == before
