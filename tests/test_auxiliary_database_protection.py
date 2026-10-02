"""Settings and diagnostics must preserve databases at their fixed paths."""

import builtins
import io
import json
import os
from contextlib import closing

import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ManualParameter

from qplot import _auxiliary_paths, diagnostics
from qplot.configuration.config import config


@pytest.fixture
def protected_files(monkeypatch):
    """Intercept every destructive call before a regression can touch a DB."""
    protected = set()
    attempts = []
    def register(filename, streams=()):
        protected.add(os.path.normcase(os.path.abspath(os.fspath(filename))))
        for stream in streams:
            def block_stream_write(_text, _filename=filename):
                check("stream.write", _filename)
            monkeypatch.setattr(stream, "write", block_stream_write)
    def check(operation, *filenames):
        for filename in filenames:
            if isinstance(filename, (str, bytes, os.PathLike)) and os.path.normcase(
                os.path.abspath(os.fsdecode(filename)),
            ) in protected:
                attempts.append((operation, filename))
                raise AssertionError("Intercepted protected database mutation")
    for module in (builtins, io):
        original = module.open
        def guarded_open(filename, mode="r", *args, _original=original, **kwargs):
            if any(flag in mode for flag in "wa+x"):
                check("open", filename)
            return _original(filename, mode, *args, **kwargs)
        monkeypatch.setattr(module, "open", guarded_open)
    original_os_open = os.open
    def guarded_os_open(filename, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            check("os.open", filename)
        return original_os_open(filename, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded_os_open)
    for name in ("replace", "rename", "remove", "unlink"):
        original = getattr(os, name)
        def guarded_mutation(*args, _name=name, _original=original, **kwargs):
            check(_name, *args)
            return _original(*args, **kwargs)
        monkeypatch.setattr(os, name, guarded_mutation)
    yield register
    assert attempts == [], attempts


@pytest.fixture
def measurement_bytes(tmp_path):
    database = tmp_path / "measurement.db"
    initialise_or_create_database_at(str(database), journal_mode="DELETE")
    with closing(connect(database)) as connection:
        experiment = load_or_create_experiment(
            "auxiliary_guard", sample_name="test", conn=connection,
        )
        x, y = ManualParameter("x"), ManualParameter("y")
        measurement = Measurement(exp=experiment)
        measurement.register_parameter(x)
        measurement.register_parameter(y, setpoints=(x,))
        with measurement.run(write_in_background=False) as saver:
            saver.add_result((x, 1.0), (y, 2.0))
    return database.read_bytes()


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "default_path", str(tmp_path))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "config.json"))
    diagnostics.configure_logging(tmp_path / "qplot.log", force=True)
    yield
    diagnostics._remove_owned_handlers(diagnostics.get_logger())


def test_startup_keeps_real_qcodes_database_at_settings_path(
    tmp_path, measurement_bytes, isolated_config, protected_files,
):
    destination = tmp_path / "config.json"
    destination.write_bytes(measurement_bytes)
    protected_files(destination)
    before = destination.stat()
    settings = config()
    assert settings.config == settings.build_default_config()
    assert settings.startup_warning
    assert destination.read_bytes() == measurement_bytes
    assert destination.stat().st_mtime_ns == before.st_mtime_ns
    assert not list(tmp_path.glob("config.invalid*"))


@pytest.mark.parametrize("kind", ("main", "wal", "journal"))
def test_config_save_rejects_existing_sqlite_artifacts(
    tmp_path, measurement_bytes, isolated_config, kind, protected_files,
):
    settings = config()
    contents = {
        "main": measurement_bytes,
        "wal": b"\x37\x7f\x06\x82" + b"\0" * 32,
        "journal": b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\0" * 32,
    }[kind]
    destination = tmp_path / "output.json"
    destination.write_bytes(contents)
    protected_files(destination)
    with pytest.raises(OSError, match="SQLite"):
        settings.save_config(str(destination))
    assert destination.read_bytes() == contents
    assert not list(tmp_path.glob(".output.json.*"))


def test_config_rechecks_destination_before_publication(
    tmp_path, measurement_bytes, isolated_config, monkeypatch, protected_files,
):
    settings = config()
    destination = tmp_path / "config.json"
    original_dump = json.dump
    def insert_database(*args, **kwargs):
        original_dump(*args, **kwargs)
        # Replace only this test-owned JSON file, before it becomes protected.
        destination.write_bytes(measurement_bytes)
        protected_files(destination)
    monkeypatch.setattr(json, "dump", insert_database)
    with pytest.raises(OSError, match="SQLite"):
        settings.update("GUI.preview_size", 250)
    assert destination.read_bytes() == measurement_bytes
    assert settings.get("GUI.preview_size") == 200
    assert not list(tmp_path.glob(".config.json.*"))


def test_logging_never_opens_real_qcodes_database_for_append(
    tmp_path, measurement_bytes, isolated_config, protected_files,
):
    destination = tmp_path / "qplot.log"
    diagnostics._remove_owned_handlers(diagnostics.get_logger())
    destination.write_bytes(measurement_bytes)
    protected_files(destination)
    logger = diagnostics.configure_logging(destination, force=True)
    logger.info("Starting qPlot")
    assert destination.read_bytes() == measurement_bytes


@pytest.mark.parametrize("rotation_index", (1, 2, 3))
@pytest.mark.parametrize("appears_after_setup", (False, True))
def test_logging_preserves_database_in_every_rotation_destination(
    tmp_path, measurement_bytes, isolated_config, rotation_index, appears_after_setup,
    protected_files,
):
    destination = tmp_path / "qplot.log"
    backup = tmp_path / f"qplot.log.{rotation_index}"
    if not appears_after_setup:
        backup.write_bytes(measurement_bytes)
        protected_files(backup)
    logger = diagnostics.configure_logging(destination, force=True, max_bytes=1)
    if appears_after_setup:
        backup.write_bytes(measurement_bytes)
        protected_files(backup)
    logger.info("This record requests log rotation")
    assert backup.read_bytes() == measurement_bytes
    assert destination.read_text().endswith("This record requests log rotation\n")


def test_logging_rechecks_existing_base_before_each_append(
    tmp_path, measurement_bytes, isolated_config, protected_files,
):
    destination = tmp_path / "qplot.log"
    logger = diagnostics.configure_logging(destination, force=True)
    logger.info("Ordinary log before database appeared")
    # Change only this ordinary test log into a protected database fixture.
    destination.write_bytes(measurement_bytes)
    protected_files(destination, streams=(
        handler.stream for handler in logger.handlers
        if isinstance(handler, diagnostics._DatabaseSafeRotatingFileHandler)
        and handler.stream is not None
    ))
    logger.info("This later record must not be appended")
    assert all(handler.stream is None for handler in logger.handlers
               if isinstance(handler, diagnostics._DatabaseSafeRotatingFileHandler))
    diagnostics._remove_owned_handlers(logger)
    assert destination.read_bytes() == measurement_bytes


@pytest.mark.parametrize("suffix", ("-wal", "-journal", "-shm"))
def test_empty_sidecar_reserved_by_real_database_is_protected(
    tmp_path, measurement_bytes, suffix,
):
    database = tmp_path / "measurement.db"
    database.write_bytes(measurement_bytes)
    sidecar = tmp_path / ("measurement.db" + suffix)
    sidecar.write_bytes(b"")
    with pytest.raises(OSError, match="sidecar"):
        _auxiliary_paths.ensure_safe_auxiliary_path(sidecar)
    assert sidecar.read_bytes() == b""


def test_unreadable_existing_auxiliary_file_fails_closed(tmp_path, monkeypatch):
    destination = tmp_path / "qplot.log"
    destination.write_text("old diagnostics")
    original_open = os.open
    def deny_inspection(filename, *args, **kwargs):
        if os.fspath(filename) == str(destination):
            raise PermissionError("unreadable existing destination")
        return original_open(filename, *args, **kwargs)
    monkeypatch.setattr(os, "open", deny_inspection)
    with pytest.raises(PermissionError):
        _auxiliary_paths.ensure_safe_auxiliary_path(destination)
    logger = diagnostics.configure_logging(destination, force=True)
    logger.info("Do not append")
    diagnostics._remove_owned_handlers(logger)
    assert destination.read_text() == "old diagnostics"
