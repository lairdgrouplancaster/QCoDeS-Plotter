"""Session-owned Auto-plot requests, independent of row selection/refresh jobs."""

from dataclasses import dataclass
from time import monotonic

from PyQt6 import QtCore

from qplot.datahandling.trusted_live_service import (
    TRUSTED_LIVE_MODE,
    TrustedLiveReadService,
    TrustedReadRequest,
)
from qplot.datahandling.trusted_plot import TrustedPlotDataset
from qplot.diagnostics import log_exception

from ._dataset_handle import DatasetKey

RETRY_DELAYS = (1, 2, 4, 8, 16, 30, 30)


def _revision(metadata):
    # Counts/timestamps of database writes are deliberately excluded: an
    # unrelated acquisition must not repeatedly retry a broken description.
    return repr(tuple(metadata.get(name) for name in (
        "run_description", "parameters", "measure_parameters",
        "preview_dimensions", "run_timestamp", "is_completed",
    )))


@dataclass
class _Run:
    key: DatasetKey
    revision: str
    attempts: int = 0
    due: float = 0


def cancel_auto_plot(window):
    queue = getattr(window, "_auto_plot_queue", None)
    if queue is not None:
        window._auto_plot_queue = None
        queue.cancel()
        queue.deleteLater()


def update_auto_plot(window, runs):
    queue = getattr(window, "_auto_plot_queue", None)
    if queue is not None and runs:
        queue.update(runs)


def enqueue_auto_plot(window, guids):
    if getattr(window, "_database_access_mode", None) != TRUSTED_LIVE_MODE:
        for guid in guids:
            window.openPlot(guid)
        return
    service = getattr(window, "_trusted_read_service", None)
    queue = getattr(window, "_auto_plot_queue", None)
    if queue is not None and queue.service is not service:
        cancel_auto_plot(window)
        queue = None
    if service is None or service.closing or service.closed:
        return
    if queue is None:
        queue = window._auto_plot_queue = AutoPlotQueue(window, service)
    queue.enqueue(guids)


class AutoPlotQueue(QtCore.QObject):
    """One bounded broker request at a time; every detected GUID stays owned.

    Metadata retries stop after eight attempts per observed run revision. A
    later description/start/completion update wakes dormant work. Successful
    GUIDs stay remembered until disable or source retirement, so refresh replay
    cannot reopen windows the user has already closed.
    """

    def __init__(self, window, service: TrustedLiveReadService):
        super().__init__(window)
        self.window = window
        self.service = service
        self.runs: dict[str, _Run] = {}
        self.finished: set[str] = set()
        self.active: tuple[str, TrustedReadRequest[TrustedPlotDataset]] | None = None
        self.cancelled = False
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)

    def cancel(self):
        self.cancelled = True
        self.timer.stop()
        if self.active is not None:
            self.active[1].cancel()
            self.active = None
        self.runs.clear()
        self.finished.clear()

    def enqueue(self, guids):
        metadata = {
            run.get("guid"): run
            for run in self.window.RunList.all_run_metadata().values()
        }
        for guid in guids:
            if not guid or guid in self.runs or guid in self.finished:
                continue
            self.runs[guid] = _Run(
                self.window._current_dataset_key(guid),
                _revision(metadata.get(guid, {})),
            )
        if self.runs:
            self.timer.start()

    def update(self, runs):
        for run_id, metadata in runs.items():
            run = self.runs.get(metadata.get("guid"))
            if run is None:
                continue
            # Use the merged table record, never a partial publication, to
            # avoid oscillation between cheap/expensive metadata updates.
            merged = self.window._run_metadata_for_id(run_id)
            revision = _revision(merged)
            if revision != run.revision:
                run.revision = revision
                run.attempts = 0
                run.due = 0
                self.timer.start()

    def _current(self):
        window = self.window
        return (not self.cancelled
                and not getattr(window, "_shutdown_started", False)
                and window.autoPlotBox.isChecked()
                and self.service is getattr(window, "_trusted_read_service", None)
                and not self.service.closing and not self.service.closed)

    def _failed(self, guid, error):
        run = self.runs[guid]
        if run.attempts == 0:
            # A newer metadata publication arrived during this attempt.
            run.due = 0
        elif run.attempts <= len(RETRY_DELAYS):
            run.due = monotonic() + RETRY_DELAYS[run.attempts - 1]
        else:
            run.due = float("inf")
            log_exception("Auto-plot metadata unavailable", error, __name__)
            self.window.show_status(
                "Auto-plot is waiting for updated run metadata. "
                "You can also open the run manually.", 8000,
            )

    def poll(self):
        if self.cancelled:
            return
        # addRuns can pump events while another database is staged. Preserve
        # the old requests until the controller either commits or rolls back.
        if (getattr(self.window, "_database_load_publication_active", False)
                and not getattr(self.window, "_shutdown_started", False)
                and self.window.autoPlotBox.isChecked()):
            return
        if not self._current():
            cancel_auto_plot(self.window)
            return
        if self.active is not None:
            guid, request = self.active
            run = self.runs[guid]
            if not self.window._dataset_key_is_current(run.key):
                cancel_auto_plot(self.window)
                return
            if not request.done:
                return
            self.active = None
            try:
                dataset = request.wait(0)
            except Exception as error:
                self._failed(guid, error)
            else:
                # Retire before opening: nested Qt events must not replay it.
                del self.runs[guid]
                self.finished.add(guid)
                self.window._resume_trusted_plot_action(
                    run.key, dataset, lambda: self.window.openPlot(guid),
                )
            return
        now = monotonic()
        for guid, run in self.runs.items():
            if run.due > now:
                continue
            if not self.window._dataset_key_is_current(run.key):
                cancel_auto_plot(self.window)
                return
            run.attempts += 1
            try:
                request = self.service.submit_plot_dataset(guid)
            except Exception as error:
                self._failed(guid, error)
            else:
                self.active = (guid, request)
            return
        if all(run.due == float("inf") for run in self.runs.values()):
            self.timer.stop()
