"""Raw exports preserve captured samples while writing bounded CSV chunks."""

import csv
import threading
import time
from contextlib import closing, contextmanager

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.dataset.sqlite.database import connect

from qplot.datahandling import csv_export, readonly, trusted_plot
from qplot.datahandling.csv_export import CsvExportCancelled, export_raw_csv
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from tests.datahandling.test_trusted_plot import protected_state


@contextmanager
def source(tmp_path, parameters, records, *, trusted=True, complete=True):
    path = tmp_path / "source.db"
    initialise_or_create_database_at(str(path), journal_mode="WAL" if trusted else "DELETE")
    with closing(connect(str(path))) as writer:
        experiment = load_or_create_experiment("CSV", "synthetic", conn=writer)
        measurement = Measurement(exp=experiment)
        for name, kind, axes, basis in parameters:
            measurement.register_custom_parameter(name, paramtype=kind, setpoints=axes, basis=basis)
        with measurement.run(write_in_background=False, in_memory_cache=False) as saver:
            if records:
                saver.dataset.add_results(records)
            saver.flush_data_to_database(block=True)
            if complete:
                saver.dataset.mark_completed()
            service = TrustedLiveReadService(path) if trusted else None
            before = protected_state(path)
            try:
                yield path, saver, service, before
            finally:
                if service is not None:
                    service.close()


def contents(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def forbid_snapshot(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Trusted CSV export attempted a whole-database snapshot")
    monkeypatch.setattr(readonly, "_copy_file_cooperatively", forbidden)
    monkeypatch.setattr(csv_export, "load_by_guid_read_only", forbidden)


@pytest.mark.parametrize("outcome", ["success", "deadline", "failure"])
def test_capture_completion_racing_poll_timeout_reads_terminal_result(outcome):
    result = object()
    terminal_error = {
        "success": None,
        "deadline": TimeoutError("capture deadline expired"),
        "failure": RuntimeError("capture failed"),
    }[outcome]
    waits = []

    class CompletingRequest:
        done = False
        progress = None

        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                # The timed wait expires, then the broker publishes before
                # the caller can inspect done. Its terminal outcome is ready.
                self.done = True
                raise TimeoutError("polling wait expired")
            if terminal_error is not None:
                raise terminal_error
            return result

        def cancel(self):
            pytest.fail("A completed capture must not be cancelled")

    request = CompletingRequest()
    if terminal_error is None:
        assert csv_export._wait_request(request, lambda: None, lambda *_args: None) is result
    else:
        with pytest.raises(type(terminal_error)) as caught:
            csv_export._wait_request(request, lambda: None, lambda *_args: None)
        assert caught.value is terminal_error
    assert waits == [.05, 0]


@pytest.mark.parametrize("trusted", [False, True])
def test_wide_export_aligns_independent_trees_and_standalones(tmp_path, monkeypatch, trusted):
    parameters = [("x", "numeric", (), ()), ("signal", "numeric", ("x",), ()),
                  ("reference", "numeric", ("x",), ()), ("note", "text", (), ())]
    records = [{"x": 2, "signal": 7}, {"x": 3, "reference": 11},
               {"x": 4, "signal": 13}, {"note": "quoted, with\nnewline"}]
    with source(tmp_path, parameters, records, trusted=trusted) as (path, saver, service, before):
        if trusted:
            forbid_snapshot(monkeypatch)
        target = tmp_path / "wide.csv"
        receipt = export_raw_csv(target, database_path=path, guid=saver.dataset.guid, service=service)
        rows = contents(target)
        assert len(rows) == receipt.row_count == 2
        assert set(receipt.parameter_names) == {"signal", "reference", "note"}
        assert rows[0]["signal.signal"] == "7.0"
        assert rows[0]["signal.x"] == "2.0"
        assert rows[0]["reference.reference"] == "11.0"
        assert rows[0]["reference.x"] == "3.0"
        assert rows[0]["note.note"] == "quoted, with\nnewline"
        assert rows[1]["signal.signal"] == "13.0"
        assert rows[1]["reference.reference"] == rows[1]["reference.x"] == rows[1]["note.note"] == ""
        assert protected_state(path) == before


@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize("dtype", [np.int64, np.uint64, np.float32, np.float64, np.longdouble])
def test_array_records_keep_precision_order_and_singleton_expansion(tmp_path, monkeypatch, trusted, dtype):
    signal = np.arange(12).reshape(3, 4).astype(dtype)
    if np.dtype(dtype).kind in "iu":
        signal += np.array(2**63 + 1 if dtype is np.uint64 else 2**60 + 1, dtype=dtype)
    else:
        signal = np.asarray("0.1", dtype=dtype) + signal * np.finfo(dtype).eps
    signal = np.asfortranarray(signal)
    parameters = [("x", "array", (), ()), ("signal", "array", ("x",), ())]
    records = [{"x": np.array([3.]), "signal": signal},
               {"x": np.array([4.]), "signal": np.array([], dtype=dtype)},
               {"x": np.array([5.]), "signal": np.array([7], dtype=dtype)}]
    with source(tmp_path, parameters, records, trusted=trusted) as (path, saver, service, before):
        if trusted:
            forbid_snapshot(monkeypatch)
        monkeypatch.setattr(csv_export, "_ROW_PAGE_SIZE", 1)
        monkeypatch.setattr(csv_export, "_SAMPLE_CHUNK_SIZE", 3)
        target = tmp_path / "array.csv"
        receipt = export_raw_csv(target, database_path=path, guid=saver.dataset.guid,
                                 parameter_names=("signal",), service=service)
        rows = contents(target)
        expected = list(signal.ravel()) + [dtype(7)]
        if np.dtype(dtype).kind in "iu":
            assert [int(row["signal"]) for row in rows] == [int(v) for v in expected]
        else:
            read_dtype = np.longdouble if dtype is np.longdouble else np.float64
            np.testing.assert_array_equal(np.array([r["signal"] for r in rows], dtype=read_dtype),
                                          np.asarray(expected, dtype=read_dtype))
        assert [float(row["x"]) for row in rows] == [3.] * 12 + [5.]
        assert receipt.row_count == 13
        assert protected_state(path) == before


@pytest.mark.parametrize("other_kind,other_value", [
    ("array", np.array([3+4j, 5+6j])), ("complex", 7+8j),
    ("text", 'value, "quoted"'), ("numeric", 9.5),
])
def test_missing_inferred_fields_and_complex_text_are_preserved(tmp_path, other_kind, other_value):
    parameters = [("x", "array", (), ()), ("signal", "array", ("x",), ()),
                  ("other", other_kind, ("x",), ()),
                  ("converted", "array", ("x",), ("signal", "other"))]
    records = [{"x": np.array([1., 2.]), "signal": np.array([11., 12.]),
                "converted": np.array([21., 22.])},
               {"x": np.array([3., 4.]), "other": other_value,
                "converted": np.array([23., 24.])}]
    with source(tmp_path, parameters, records) as (path, saver, service, before):
        target = tmp_path / "inferred.csv"
        receipt = export_raw_csv(target, database_path=path, guid=saver.dataset.guid,
                                 parameter_names=("converted",), service=service)
        rows = contents(target)
        assert receipt.row_count == 4
        assert [float(r["converted"]) for r in rows] == [21, 22, 23, 24]
        assert [float(r["x"]) for r in rows] == [1, 2, 3, 4]
        assert [r["signal"] for r in rows] == ["11.0", "12.0", "", ""]
        assert [r["other"] for r in rows[:2]] == ["", ""]
        if other_kind in {"array", "complex"}:
            expected = list(other_value) if other_kind == "array" else [other_value] * 2
            assert [complex(r["other"]) for r in rows[2:]] == expected
        else:
            assert [r["other"] for r in rows[2:]] == [str(other_value)] * 2
        assert protected_state(path) == before


def test_capture_boundary_excludes_later_appends_without_source_changes(tmp_path, monkeypatch):
    parameters = [("x", "numeric", (), ()), ("signal", "numeric", ("x",), ())]
    with source(tmp_path, parameters, [{"x": 1, "signal": 10}], complete=False) as source_info:
        path, saver, service, before = source_info
        forbid_snapshot(monkeypatch)
        after_write = None
        def progress(phase, completed, total):
            nonlocal after_write
            if phase == "Writing CSV" and completed == 0:
                saver.dataset.add_results([{"x": 2, "signal": 20}])
                saver.flush_data_to_database(block=True)
                after_write = protected_state(path)
        target = tmp_path / "capture.csv"
        receipt = export_raw_csv(target, database_path=path, guid=saver.dataset.guid,
                                 service=service, progress=progress)
        assert receipt.watermark == 1 and receipt.row_count == 1
        assert contents(target) == [{"signal": "10.0", "x": "1.0"}]
        assert after_write != before
        assert protected_state(path) == after_write
        latest = tmp_path / "latest.csv"
        later = export_raw_csv(latest, database_path=path, guid=saver.dataset.guid, service=service)
        assert later.watermark == 2 and later.row_count == 2
        assert protected_state(path) == after_write


def test_measurement_index_is_resolved_from_fresh_description(tmp_path):
    parameters = [("x", "numeric", (), ()), ("signal", "numeric", ("x",), ()),
                  ("note", "text", (), ())]
    with source(tmp_path, parameters, [{"x": 1, "signal": 2}, {"note": "recorded"}]) as data:
        path, saver, service, before = data
        target = tmp_path / "index.csv"
        receipt = export_raw_csv(target, database_path=path, guid=saver.dataset.guid,
                                 service=service, measurement_index=2)
        assert receipt.parameter_names == ("note",)
        assert contents(target) == [{"note": "recorded"}]
        with pytest.raises(ValueError, match="between 1 and 2"):
            export_raw_csv(tmp_path / "invalid.csv", database_path=path, guid=saver.dataset.guid,
                           service=service, measurement_index=3)
        assert not (tmp_path / "invalid.csv").exists()
        assert protected_state(path) == before


def test_cancelled_capture_releases_its_request_and_preserves_shared_prefix(tmp_path, monkeypatch):
    parameters = [("x", "array", (), ()), ("signal", "array", ("x",), ())]
    with source(tmp_path, parameters, [{"x": np.arange(20.), "signal": np.arange(20.)}],
                complete=False) as data:
        path, saver, service, before = data
        forbid_snapshot(monkeypatch)
        original = trusted_plot.plot_prefix
        entered, cancel = threading.Event(), threading.Event()
        def suspended(executor, dataset, *args):
            iterator = original(executor, dataset, *args)
            try:
                next(iterator)
                entered.set()
                while True:
                    executor.check_cancelled()
                    time.sleep(.002)
                    yield
            finally:
                iterator.close()
        monkeypatch.setattr(trusted_plot, "plot_prefix", suspended)
        errors = []
        guid = saver.dataset.guid
        def export():
            try:
                export_raw_csv(tmp_path / "cancelled.csv", database_path=path,
                               guid=guid, service=service, cancelled=cancel.is_set)
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=export)
        thread.start()
        try:
            assert entered.wait(5), errors
            cancel.set()
            thread.join(3)
            assert not thread.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], CsvExportCancelled)
            assert not (tmp_path / "cancelled.csv").exists()
            assert protected_state(path) == before
        finally:
            cancel.set()
            thread.join(5)
        monkeypatch.setattr(trusted_plot, "plot_prefix", original)
        dataset = service.submit_plot_dataset(saver.dataset.guid).wait()
        prefix = service.submit_plot_prefix(dataset).wait()
        export_raw_csv(tmp_path / "retry.csv", database_path=path, guid=saver.dataset.guid, service=service)
        with closing(prefix.connect()) as conn:
            assert conn.execute(f'SELECT COUNT(*) FROM "{dataset.table_name}"').fetchone()[0] == 1
        assert protected_state(path) == before


def test_cancel_during_serialization_stops_at_bounded_chunk(tmp_path):
    parameters = [("x", "array", (), ()), ("signal", "array", ("x",), ())]
    with source(tmp_path, parameters, [{"x": np.arange(20_000.), "signal": np.arange(20_000.)}]) as data:
        path, saver, service, before = data
        cancel = threading.Event()
        def progress(phase, completed, total):
            if phase == "Writing CSV" and completed >= 8192:
                cancel.set()
        target = tmp_path / "cancelled-stage.csv"
        with pytest.raises(CsvExportCancelled):
            export_raw_csv(target, database_path=path, guid=saver.dataset.guid, service=service,
                           cancelled=cancel.is_set, progress=progress)
        assert 0 < len(contents(target)) <= 8192
        assert protected_state(path) == before


def test_empty_run_exports_header_without_inventing_values(tmp_path):
    parameters = [("x", "numeric", (), ()), ("signal", "numeric", ("x",), ())]
    with source(tmp_path, parameters, []) as (path, saver, service, before):
        target = tmp_path / "empty.csv"
        receipt = export_raw_csv(target, database_path=path, guid=saver.dataset.guid, service=service)
        assert receipt.row_count == receipt.watermark == 0
        assert target.read_text() == "signal,x\n"
        assert protected_state(path) == before
