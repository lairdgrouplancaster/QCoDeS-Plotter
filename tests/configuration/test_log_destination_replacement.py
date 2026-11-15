"""A retained log descriptor must never append to a later QCoDeS database."""

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

from qplot import diagnostics


def _create_measurement(path):
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    with closing(connect(path)) as connection:
        experiment = load_or_create_experiment(
            "diagnostic_descriptor", sample_name="synthetic", conn=connection,
        )
        x, y = ManualParameter("x"), ManualParameter("y")
        measurement = Measurement(exp=experiment)
        measurement.register_parameter(x)
        measurement.register_parameter(y, setpoints=(x,))
        with measurement.run(write_in_background=False) as saver:
            saver.add_result((x, 1.0), (y, 2.0))


def _source_state(path):
    return path.read_bytes(), path.stat().st_size, path.stat().st_mtime_ns


@pytest.mark.parametrize("replacement_log", [False, True])
@pytest.mark.parametrize("rotate", [False, True])
@pytest.mark.skipif(os.name == "nt", reason="Windows does not allow renaming open logs.")
def test_detached_log_stream_cannot_append_to_real_qcodes_database(
    tmp_path, replacement_log, rotate,
):
    log_path = tmp_path / "qplot.log"
    database = tmp_path / "measurement.db"
    logger = diagnostics.configure_logging(
        log_path, force=True, max_bytes=1 if rotate else 1_000_000,
    )
    try:
        log_path.rename(database)
        _create_measurement(database)
        before = _source_state(database)
        if replacement_log:
            log_path.write_text("replacement diagnostics\n", encoding="utf-8")

        logger.info("The next diagnostic belongs to the current log")

        assert _source_state(database) == before
        assert log_path.read_text().endswith(
            "The next diagnostic belongs to the current log\n"
        )
    finally:
        diagnostics._remove_owned_handlers(logger)
    assert _source_state(database) == before


@pytest.mark.parametrize("operation", ["emit", "flush", "rollover", "close"])
@pytest.mark.parametrize("detached", [False, True])
def test_unsafe_log_stream_discards_pending_bytes_without_flushing(
    tmp_path, operation, detached,
):
    if detached and os.name == "nt":
        pytest.skip("Windows does not allow renaming open logs.")
    log_path = tmp_path / "qplot.log"
    database = tmp_path / "measurement.db" if detached else log_path
    logger = diagnostics.configure_logging(log_path, force=True)
    handler = next(
        handler for handler in logger.handlers
        if isinstance(handler, diagnostics._DatabaseSafeRotatingFileHandler)
    )
    original_stream = handler.stream
    assert original_stream is not None
    try:
        # Pending text can remain after a failed flush. Keep it in the real
        # Python buffer so discard/close exercises actual I/O, without mocks.
        original_stream.write("pending diagnostic that must be discarded\n")
        assert log_path.stat().st_size == 0
        if detached:
            log_path.rename(database)
        _create_measurement(database)
        before = _source_state(database)

        if operation == "emit":
            logger.info("A later diagnostic")
        elif operation == "flush":
            handler.flush()
        elif operation == "rollover":
            handler.doRollover()
        else:
            handler.close()

        assert _source_state(database) == before
        assert original_stream.closed
    finally:
        diagnostics._remove_owned_handlers(logger)
    assert _source_state(database) == before
