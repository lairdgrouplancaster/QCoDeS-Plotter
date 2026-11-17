"""Preview startup faults must release the slot and permit an explicit retry."""

import gc
import weakref
from time import monotonic
from types import SimpleNamespace

import pytest
from PyQt6 import QtCore

from qplot.windows._widgets import preview as preview_module
from tests.support.widgets.preview_database import _artifact_state, _preview_database
from tests.support.widgets.preview_jobs import seed_preview_job


@pytest.mark.parametrize("stage", ["construct", "connect", "submit"])
def test_preview_startup_failure_releases_slot_and_retries(monkeypatch, stage):
    tab = preview_module.PreviewTab()
    tab.set_database_runs("preview-startup.db", {1: {"guid": "run"}})
    changes = []
    tab.previewGenerationChanged.connect(lambda *args: changes.append(args))
    workers = []
    submitted = []
    failing = True

    class Worker:
        def __init__(self, generation, binding, guid, metadata, size):
            if failing and stage == "construct":
                raise RuntimeError("injected construction failure")
            self.generation = generation
            self.guid = guid
            self.preview_size = size
            self.cancelled = False
            self.signals = SimpleNamespace(finished=SimpleNamespace(connect=self.connect))
            workers.append(self)

        def connect(self, callback):
            if failing and stage == "connect":
                raise RuntimeError("injected connection failure")

        def cancel(self):
            self.cancelled = True

        def is_cancelled(self):
            return self.cancelled

        def require_current_database_instance(self):
            return tab.database_instance

    def submit(worker):
        if failing and stage == "submit":
            raise RuntimeError("injected submission failure")
        submitted.append(worker)

    monkeypatch.setattr(preview_module, "PreviewWorker", Worker)
    tab.thread_pool = SimpleNamespace(start=submit)
    try:
        tab.request_guids(["run"])
        assert not tab.active
        assert not tab._workers
        assert not tab.queue
        assert "run" in tab.errors
        assert not changes or changes[-1] == ("run", False)
        assert all(worker.cancelled for worker in workers)

        failing = False
        tab.request_guids(["run"])
        assert len(submitted) == 1
        worker = submitted[0]
        tab._worker_finished(tab.generation, "run", [], None, worker)
        assert tab.cache["run"] == []
        assert not tab.active
        assert not tab._workers
        assert changes[-1] == ("run", False)
    finally:
        tab.shutdown()
        tab.deleteLater()


def test_real_preview_retries_submission_failure_without_source_changes(tmp_path, qapplication):
    path = tmp_path / "preview.db"
    metadata, _, _ = _preview_database(path, 72)
    before = _artifact_state(path)
    tab = preview_module.PreviewTab()
    tab.set_database_runs(path, {metadata["run_id"]: metadata})
    guid = metadata["guid"]
    pool = tab.thread_pool
    submitted = []

    def fail_once(worker):
        submitted.append(worker)
        if len(submitted) == 1:
            raise RuntimeError("injected pool submission failure")
        pool.start(worker)

    tab.thread_pool = SimpleNamespace(start=fail_once)
    try:
        tab.request_guids([guid])
        assert submitted[0].is_cancelled()
        assert not tab.active
        tab.request_guids([guid])
        deadline = monotonic() + 10
        while tab._workers and monotonic() < deadline:
            qapplication.processEvents()
        assert not tab._workers
        assert guid not in tab.errors
        assert tab.cache[guid][0]["parameter"] == "current"
        assert not tab.cache[guid][0]["image"].isNull()
        assert _artifact_state(path) == before
    finally:
        tab.shutdown()
        pool.waitForDone(10_000)
        tab.deleteLater()


def test_deleting_widget_discards_queued_preview_start(qapplication):
    tab = preview_module.PreviewTab()
    starts = []
    tab._start_next = lambda: starts.append(True)
    tab._schedule_start_next()
    tab.deleteLater()
    qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    qapplication.processEvents()
    assert starts == []


def test_deleting_widget_cancels_retained_preview_work(qapplication):
    tab = preview_module.PreviewTab()
    cancelled = []
    controller = tab._preview_controller
    seed_preview_job(tab, "retained", SimpleNamespace(cancel=lambda: cancelled.append(True)))
    tab.deleteLater()
    qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert controller.closed
    assert cancelled == [True]


def test_preview_controller_does_not_retain_its_widget(qapplication):
    tab = preview_module.PreviewTab()
    controller = tab._preview_controller
    cancelled = []
    seed_preview_job(tab, "retained", SimpleNamespace(cancel=lambda: cancelled.append(True)))
    reference = weakref.ref(tab)
    del tab
    gc.collect()
    assert reference() is None
    assert controller.closed
    assert cancelled == [True]
