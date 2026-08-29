"""Qt owner-thread integration for trusted derived work.

This bridge is the sole trusted-path producer for derived metadata, run-table
thumbnails, and selected-run previews.  The coordinator, helper, cache, and
retry threads can only request one coalesced queued wakeup; all polling, image
decoding, identity validation, and widget mutation happens on the Qt thread.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from PyQt6 import QtCore, QtGui, sip

from qplot.datahandling.file_identity import (
    DatabaseInstance,
    database_instance,
    database_instances_differ,
)
from qplot.datahandling.trusted_derived_rendering import (
    TRUSTED_DERIVED_RENDERER_VERSION,
)
from qplot.datahandling.trusted_live_queries import (
    TrustedParameterView,
    TrustedSetpointSummary,
    TrustedSourceRevision,
)
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.datahandling.trusted_work_coordinator import (
    TrustedDerivedErrorRecord,
    TrustedDerivedRun,
    TrustedWorkCoordinator,
)
from qplot.datahandling.trusted_work_scheduler import (
    RenderingOptions,
    ScheduledWork,
    TrustedWorkKind,
    WorkFormat,
    WorkPublication,
)

if TYPE_CHECKING:
    from qplot.windows.main import MainWindow


_THUMBNAIL_WIDTH = 160
_THUMBNAIL_HEIGHT = 96
_VIEWPORT_EVENTS = {
    QtCore.QEvent.Type.LayoutRequest,
    QtCore.QEvent.Type.Resize,
    QtCore.QEvent.Type.Show,
    QtCore.QEvent.Type.UpdateRequest,
    QtCore.QEvent.Type.Wheel,
}


class TrustedDerivedQtBridge(QtCore.QObject):
    """Own one active coordinator and transactionally publish into MainWindow."""

    _queuedWakeup = QtCore.pyqtSignal()
    _queuedDatabaseBinding = QtCore.pyqtSignal()

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window
        self._owner_thread_id = threading.get_ident()
        self._wakeup_lock = threading.Lock()
        self._wakeup_queued = False
        self._accepting_wakeups = False
        self._accepting_publications = False
        self._shutting_down = False
        self._binding_serial = 0
        self._pending_database_binding: tuple[int, str] | None = None
        self._database_instance: DatabaseInstance | None = None
        self._service: TrustedLiveReadService | None = None
        self._coordinator: TrustedWorkCoordinator | None = None
        self._coordinator_generation: int | None = None
        self._retiring: list[TrustedWorkCoordinator] = []
        self._run_ids: list[int] = []
        self._run_guids: list[str] = []
        self._index_by_guid: dict[str, int] = {}
        self._index_by_run_id: dict[int, int] = {}
        self._metadata_by_guid: dict[str, dict[str, Any]] = {}
        self._parameters_by_guid: dict[str, tuple[TrustedParameterView, ...]] = {}
        self._summaries_by_guid: dict[str, tuple[TrustedSetpointSummary, ...]] = {}
        self._last_errors: dict[tuple[str, TrustedWorkKind], str] = {}
        self._last_helper_incarnation = 0
        self._replacement_reload_queued = False
        self._formats = self._work_formats(window.preview_size)

        self._queuedWakeup.connect(  # type: ignore[call-arg]
            self._poll,
            QtCore.Qt.ConnectionType.QueuedConnection,
        )
        self._queuedDatabaseBinding.connect(  # type: ignore[call-arg]
            self._apply_queued_database_binding,
            QtCore.Qt.ConnectionType.QueuedConnection,
        )
        self._priority_timer = QtCore.QTimer(self)
        self._priority_timer.setSingleShot(True)
        self._priority_timer.setInterval(0)
        self._priority_timer.timeout.connect(self._apply_priority)
        self._retire_timer = QtCore.QTimer(self)
        self._retire_timer.setInterval(25)
        self._retire_timer.timeout.connect(self._reap_retiring)

        run_list = window.RunList
        run_list.installEventFilter(self)
        viewport = run_list.viewport()
        if viewport is not None:
            viewport.installEventFilter(self)
        header = run_list.header()
        if header is not None:
            header.installEventFilter(self)
            header.sortIndicatorChanged.connect(self.request_priority_update)
        run_list.itemSelectionChanged.connect(self.request_priority_update)
        model = run_list.model()
        if model is not None:
            model.modelReset.connect(self.request_priority_update)
            model.layoutChanged.connect(self.request_priority_update)
            model.rowsInserted.connect(self.request_priority_update)
            model.rowsRemoved.connect(self.request_priority_update)

    @property
    def coordinator(self) -> TrustedWorkCoordinator | None:
        """Expose the owner-thread coordinator for diagnostics and tests."""

        self._require_owner()
        return self._coordinator

    @property
    def accepting_publications(self) -> bool:
        return self._accepting_publications

    def eventFilter(self, watched, event):  # noqa: N802 - Qt API
        if event.type() in _VIEWPORT_EVENTS:
            self.request_priority_update()
        return super().eventFilter(watched, event)

    @QtCore.pyqtSlot()
    def request_priority_update(self, *_args) -> None:
        """Coalesce selection, sorting, filtering, scrolling, and resize noise."""

        if self._shutting_down or self._coordinator is None:
            return
        self._priority_timer.start()

    def suspend_publications(self) -> None:
        """Disarm queued work before a database publication transaction."""

        self._require_owner()
        self._pending_database_binding = None
        self._priority_timer.stop()
        with self._wakeup_lock:
            self._accepting_wakeups = False
        self._accepting_publications = False
        self._binding_serial += 1

    def queue_database_binding(self, generation: int, database_path: str) -> None:
        """Queue one durable, coalesced post-commit database bind request."""

        self._require_owner()
        if self._shutting_down:
            return
        self._pending_database_binding = (int(generation), str(database_path))
        self._queuedDatabaseBinding.emit()

    @QtCore.pyqtSlot()
    def _apply_queued_database_binding(self) -> None:
        """Apply only the latest binding request after Qt regains control."""

        self._require_owner()
        pending = self._pending_database_binding
        self._pending_database_binding = None
        if pending is None or self._shutting_down:
            return
        starter = getattr(self._window, "_start_trusted_derived_bridge", None)
        if callable(starter):
            starter(*pending)

    def resume_publications(self) -> None:
        """Resume an unchanged committed binding after transaction rollback."""

        self._require_owner()
        if self._shutting_down or self._coordinator is None:
            return
        self._accepting_publications = True
        with self._wakeup_lock:
            self._accepting_wakeups = True
        runs = self._window.RunList.all_run_metadata()
        preview = self._window.infoBox.preview
        refresh = getattr(preview, "refresh_trusted_derived_runs", None)
        if callable(refresh):
            refresh(runs)
        else:
            preview.set_trusted_derived_runs(runs)
        self.select_run(getattr(self._window, "_selected_run_guid", None))
        self.request_priority_update()
        self._request_wakeup()

    def refresh_active_database(
        self,
        database: DatabaseInstance,
        runs: Mapping[int, Mapping[str, object]],
        service: TrustedLiveReadService,
    ) -> bool:
        """Idempotently refresh one already-active trusted database binding."""

        self._require_owner()
        coordinator = self._coordinator
        if (
            self._shutting_down
            or not self._accepting_publications
            or coordinator is None
            or self._database_instance is None
            or self._service is not service
            or database_instances_differ(self._database_instance, database)
            or database_instances_differ(database, service.database_instance)
        ):
            return False
        _derived, run_ids, run_guids = self._normalise_runs(database, runs, service)
        if (
            run_ids[: len(self._run_ids)] != self._run_ids
            or run_guids[: len(self._run_guids)] != self._run_guids
        ):
            return False
        if len(run_ids) > len(self._run_ids):
            self.reconcile_runs(runs)
        self._database_instance = database
        preview = self._window.infoBox.preview
        refresh = getattr(preview, "refresh_trusted_derived_runs", None)
        if callable(refresh):
            refresh(runs)
        else:
            preview.set_trusted_derived_runs(runs)
        self.select_run(getattr(self._window, "_selected_run_guid", None))
        self.request_priority_update()
        self._request_wakeup()
        return True

    def bind_database(
        self,
        database: DatabaseInstance,
        runs: Mapping[int, Mapping[str, object]],
        service: TrustedLiveReadService,
    ) -> None:
        """Bind after the cheap run-list transaction has committed."""

        self._require_owner()
        if self._shutting_down:
            return
        if not isinstance(database, DatabaseInstance) or database.identity is None:
            raise ValueError("The trusted Qt bridge requires an exact database.")
        if not isinstance(service, TrustedLiveReadService):
            raise TypeError("service must be TrustedLiveReadService.")
        if database_instances_differ(database, service.database_instance):
            raise ValueError("The bridge service is bound to another database.")
        derived_runs, run_ids, run_guids = self._normalise_runs(database, runs, service)

        self.suspend_publications()
        coordinator = self._coordinator
        if coordinator is None:
            coordinator = TrustedWorkCoordinator(
                database,
                derived_runs,
                service,
                formats=self._formats,
                wakeup=self._request_wakeup,
                on_publish=self._publish,
                on_error=self._record_error,
            )
            self._coordinator = coordinator
        else:
            coordinator.switch_database(database, derived_runs, service)

        self._database_instance = database
        self._service = service
        self._run_ids = run_ids
        self._run_guids = run_guids
        self._index_by_guid = {
            guid: index for index, guid in enumerate(self._run_guids)
        }
        self._index_by_run_id = {
            run_id: index for index, run_id in enumerate(self._run_ids)
        }
        self._metadata_by_guid = {}
        self._parameters_by_guid = {}
        self._summaries_by_guid = {}
        self._last_errors = {}
        self._last_helper_incarnation = 0
        self._replacement_reload_queued = False
        self._coordinator_generation = coordinator.snapshot().generation
        preview = self._window.infoBox.preview
        preview.set_trusted_derived_runs(runs)
        self._accepting_publications = True
        with self._wakeup_lock:
            self._accepting_wakeups = True
        # A completion can race through while publications are suspended for
        # the baseline transaction. Its notifier is deliberately discarded,
        # so drain once after rearming to avoid stranding the sole active slot.
        coordinator.poll()
        self._apply_priority()
        self.select_run(getattr(self._window, "_selected_run_guid", None))
        coordinator.start()

    def reconcile_runs(
        self,
        runs: Mapping[int, Mapping[str, object]],
    ) -> None:
        """Append newly visible basic rows without rebuilding the coordinator."""

        self._require_owner()
        coordinator = self._coordinator
        database = self._database_instance
        service = self._service
        if (
            coordinator is None
            or database is None
            or service is None
            or not self._accepting_publications
        ):
            return
        _all, run_ids, run_guids = self._normalise_runs(database, runs, service)
        if run_ids[: len(self._run_ids)] != self._run_ids:
            raise ValueError("Trusted UI run reconciliation must be append-only.")
        if run_guids[: len(self._run_guids)] != self._run_guids:
            raise ValueError("Trusted UI run GUIDs must remain a stable prefix.")
        if len(run_ids) == len(self._run_ids):
            return
        additions = [
            self._derived_run(database, run_id, guid, service)
            for run_id, guid in zip(
                run_ids[len(self._run_ids) :],
                run_guids[len(self._run_guids) :],
                strict=True,
            )
        ]
        coordinator.reconcile_runs((*coordinator.runs, *additions))
        self._run_ids = run_ids
        self._run_guids = run_guids
        self._index_by_guid = {
            guid: index for index, guid in enumerate(self._run_guids)
        }
        self._index_by_run_id = {
            run_id: index for index, run_id in enumerate(self._run_ids)
        }
        self._window.infoBox.preview.add_trusted_derived_runs(runs)
        self.request_priority_update()

    def select_run(self, guid: str | None) -> None:
        """Publish retained state for the selection, then reprioritise pending work."""

        self._require_owner()
        if not self._accepting_publications:
            return
        exact_guid = str(guid or "")
        preview = self._window.infoBox.preview
        if not exact_guid or exact_guid not in self._index_by_guid:
            preview.clear_current_run()
            self.request_priority_update()
            return
        item = self._window.RunList._item_for_guid(exact_guid)
        run_metadata = dict(getattr(item, "run_metadata", {}) or {})
        metadata = self._metadata_by_guid.get(exact_guid)
        if metadata is not None:
            self._window.infoBox.set_trusted_derived_metadata(
                run_metadata,
                self._parameters_by_guid.get(exact_guid, ()),
                self._summaries_by_guid.get(exact_guid, ()),
                metadata,
            )
        else:
            self._window.infoBox.set_trusted_run_loading(run_metadata)
            preview.set_current_guid(exact_guid)
        if metadata is not None:
            preview.set_current_guid(exact_guid)
        needs_replay = getattr(preview, "trusted_preview_needs_replay", None)
        if callable(needs_replay) and needs_replay(exact_guid):
            self._request_completed_work(exact_guid, TrustedWorkKind.PREVIEW)
        self.request_priority_update()

    def source_changed(self, run_ids: Sequence[int]) -> None:
        """Coalesce live appends/completion changes through Stage 5B invalidation."""

        self._require_owner()
        coordinator = self._coordinator
        if coordinator is None or not self._accepting_publications:
            return
        seen: set[int] = set()
        for run_id in run_ids:
            try:
                exact = int(run_id)
            except (TypeError, ValueError):
                continue
            index = self._index_by_run_id.get(exact)
            if index is None or index in seen:
                continue
            seen.add(index)
            coordinator.source_changed(index)

    def helper_restarted(self) -> None:
        """Invalidate queued and cached work at an explicit helper boundary."""

        self._require_owner()
        coordinator = self._coordinator
        if coordinator is None or not self._accepting_publications:
            return
        self._last_helper_incarnation += 1
        coordinator.helper_restarted()
        self._coordinator_generation = coordinator.snapshot().generation

    def update_preview_size(self, preview_size: int) -> None:
        """Invalidate only full previews; run-table thumbnails stay current."""

        self._require_owner()
        preview_format = self._preview_format(preview_size)
        if self._formats[TrustedWorkKind.PREVIEW] == preview_format:
            return
        self._formats = {**self._formats, TrustedWorkKind.PREVIEW: preview_format}
        discard = getattr(
            self._window.infoBox.preview,
            "discard_trusted_previews",
            None,
        )
        if callable(discard):
            discard()
        coordinator = self._coordinator
        if coordinator is not None and self._accepting_publications:
            coordinator.update_format(TrustedWorkKind.PREVIEW, preview_format)

    def clear_database(self) -> None:
        """Stop publication immediately and retire work without a GUI wait."""

        self._require_owner()
        self.suspend_publications()
        coordinator = self._coordinator
        self._coordinator = None
        self._coordinator_generation = None
        self._database_instance = None
        self._service = None
        self._run_ids = []
        self._run_guids = []
        self._index_by_guid = {}
        self._index_by_run_id = {}
        self._metadata_by_guid = {}
        self._parameters_by_guid = {}
        self._summaries_by_guid = {}
        self._last_errors = {}
        self._replacement_reload_queued = False
        if coordinator is not None:
            coordinator.close_async()
            self._retiring.append(coordinator)
            self._retire_timer.start()

    def shutdown(self) -> None:
        self._require_owner()
        if self._shutting_down:
            return
        self._shutting_down = True
        self._priority_timer.stop()
        self.clear_database()
        with self._wakeup_lock:
            self._accepting_wakeups = False

    def background_active(self) -> bool:
        self._require_owner()
        self._reap_retiring()
        return bool(self._retiring)

    def shutdown_diagnostic(self) -> str | None:
        self._require_owner()
        if not self._retiring:
            return None
        return f"trusted_derived_coordinators_retiring={len(self._retiring)}"

    def escalate_cleanup(self) -> None:
        self._require_owner()
        if self._coordinator is not None:
            self._coordinator.close_async()
        for coordinator in self._retiring:
            coordinator.close_async()
        self._reap_retiring()

    @QtCore.pyqtSlot()
    def _poll(self) -> None:
        self._require_owner()
        with self._wakeup_lock:
            self._wakeup_queued = False
        if not self._accepting_publications or self._shutting_down:
            self._reap_retiring()
            return
        coordinator = self._coordinator
        if coordinator is not None:
            coordinator.poll()
        self._reap_retiring()

    def _request_wakeup(self) -> None:
        """Thread-safe notifier used by both worker and retry-timer threads."""

        with self._wakeup_lock:
            if (
                not self._accepting_wakeups
                or self._wakeup_queued
                or self._shutting_down
            ):
                return
            self._wakeup_queued = True
        try:
            self._queuedWakeup.emit()
        except RuntimeError:
            with self._wakeup_lock:
                self._wakeup_queued = False

    @QtCore.pyqtSlot()
    def _apply_priority(self) -> None:
        self._require_owner()
        coordinator = self._coordinator
        if coordinator is None or not self._accepting_publications:
            return
        selected_guid = str(getattr(self._window, "_selected_run_guid", "") or "")
        coordinator.select_run(self._index_by_guid.get(selected_guid))
        coordinator.set_visible_indices(self._visible_stable_indices())

    def _visible_stable_indices(self) -> tuple[int, ...]:
        run_list = self._window.RunList
        viewport = run_list.viewport()
        if viewport is None or viewport.height() <= 0:
            return ()
        first = run_list.itemAt(QtCore.QPoint(1, 1))
        if first is None:
            for y in range(0, viewport.height(), 8):
                first = run_list.itemAt(QtCore.QPoint(1, y))
                if first is not None:
                    break
        visible: list[int] = []
        item = first
        while item is not None:
            rect = run_list.visualItemRect(item)
            if rect.isValid() and rect.top() > viewport.rect().bottom():
                break
            guid = str(getattr(item, "guid", "") or "")
            index = self._index_by_guid.get(guid)
            if index is not None:
                visible.append(index)
            item = run_list.itemBelow(item)
        return tuple(dict.fromkeys(visible))

    def _publish(self, publication: WorkPublication) -> None:
        self._require_owner()
        if not self._publication_is_current(publication):
            return
        payload = publication.result
        if not isinstance(payload, dict):
            return
        kind = publication.key.kind
        if payload.get("kind") != kind.name.lower():
            return
        source = self._pairs(payload.get("source"))
        guid = publication.key.run_guid
        index = self._index_by_guid.get(guid)
        if index is None:
            return
        run_id = self._run_ids[index]
        if source.get("run_id") not in (None, run_id):
            return
        if source.get("run_guid") not in (None, guid):
            return
        incarnation = source.get("helper_incarnation")
        if type(incarnation) is int:
            if incarnation < self._last_helper_incarnation:
                return
            if (
                self._last_helper_incarnation
                and incarnation > self._last_helper_incarnation
            ):
                # The helper can retire itself after a bounded-read failure.
                # Treat its first replacement result as a boundary and let
                # the coordinator reschedule against the new namespace.
                self._last_helper_incarnation = incarnation
                coordinator = self._coordinator
                if coordinator is not None:
                    coordinator.helper_restarted()
                    self._coordinator_generation = coordinator.snapshot().generation
                return
            self._last_helper_incarnation = incarnation

        if kind is TrustedWorkKind.METADATA:
            self._publish_metadata(run_id, guid, payload)
            return
        if kind is TrustedWorkKind.THUMBNAIL:
            accepts = getattr(self._window.RunList, "accepts_run_preview", None)
            if callable(accepts) and not accepts(guid):
                return
        previews, decode_error = self._decode_images(guid, payload)
        if decode_error is not None:
            previews = [self._unavailable_preview(guid, decode_error)]
        status = str(payload.get("status") or "error")
        description = str(payload.get("description") or "Derived result unavailable.")
        if status not in {"ok", "empty"} and not previews:
            previews = [self._unavailable_preview(guid, description)]
        if kind is TrustedWorkKind.THUMBNAIL:
            self._window.RunList.set_run_previews(guid, previews)
            self._window.RunList.set_run_preview_generating(guid, False)
        elif kind is TrustedWorkKind.PREVIEW:
            self._window.infoBox.preview.publish_trusted_previews(
                guid,
                previews,
            )

    def _request_completed_work(
        self,
        guid: str,
        kind: TrustedWorkKind,
    ) -> bool:
        coordinator = self._coordinator
        database = self._database_instance
        generation = self._coordinator_generation
        index = self._index_by_guid.get(guid)
        if (
            coordinator is None
            or database is None
            or generation is None
            or index is None
            or not self._accepting_publications
        ):
            return False
        return coordinator.request_completed_work(
            index,
            kind,
            database_instance=database,
            generation=generation,
            run_guid=guid,
            prioritize=True,
        )

    def _publish_metadata(
        self,
        run_id: int,
        guid: str,
        payload: Mapping[str, object],
    ) -> None:
        status = str(payload.get("status") or "error")
        description = str(payload.get("description") or "Run metadata unavailable.")
        if status != "ok":
            if guid == getattr(self._window, "_selected_run_guid", None):
                item = self._window.RunList._item_for_guid(guid)
                run = dict(getattr(item, "run_metadata", {}) or {})
                self._window.infoBox.set_trusted_run_error(description, run)
                self._window.infoBox.preview.set_current_guid(guid)
            return
        metadata = {
            str(name): value
            for name, value in self._pairs(payload.get("metadata")).items()
            if isinstance(name, str)
        }
        if metadata.get("run_id") != run_id or metadata.get("guid") != guid:
            return
        raw_run_fields = self._pairs(metadata.get("run_fields"))
        run_fields: dict[str, object] = {
            str(name): value
            for name, value in raw_run_fields.items()
            if value is not None
        }
        run_fields["run_id"] = run_id
        run_fields["guid"] = guid
        item = self._window.RunList._item_for_guid(guid)
        current_run = dict(getattr(item, "run_metadata", {}) or {})
        run_fields = self._preserve_newer_live_facts(current_run, run_fields)
        parameters = self._parameter_views(metadata.get("parameters"))
        summaries = self._setpoint_summaries(metadata.get("setpoint_summaries"))
        self._metadata_by_guid[guid] = dict(metadata)
        self._parameters_by_guid[guid] = parameters
        self._summaries_by_guid[guid] = summaries
        self._window.RunList.updateRuns({run_id: run_fields})
        if guid != getattr(self._window, "_selected_run_guid", None):
            return
        item = self._window.RunList._item_for_guid(guid)
        current_run = dict(getattr(item, "run_metadata", {}) or run_fields)
        self._window.infoBox.set_trusted_derived_metadata(
            current_run,
            parameters,
            summaries,
            metadata,
        )

    @staticmethod
    def _preserve_newer_live_facts(
        current: Mapping[str, object],
        derived: dict[str, object],
    ) -> dict[str, object]:
        """Merge a captured prefix without rolling back later cheap refresh facts."""

        merged = dict(derived)
        current_count = current.get("result_count")
        derived_count = derived.get("result_count")
        if type(current_count) is int and (
            type(derived_count) is not int or current_count > derived_count
        ):
            merged["result_count"] = current_count
        if bool(current.get("is_completed")):
            merged["is_completed"] = True
        if current.get("completed_timestamp") is not None:
            merged["completed_timestamp"] = current["completed_timestamp"]
        if current.get("measurement_exception") is not None:
            merged["measurement_exception"] = current["measurement_exception"]
        return merged

    def _publication_is_current(self, publication: WorkPublication) -> bool:
        if (
            self._shutting_down
            or not self._accepting_publications
            or self._coordinator is None
            or self._database_instance is None
            or self._service is None
            or publication.generation != self._coordinator_generation
            or publication.key.database_instance != self._database_instance
            or publication.key.renderer_version
            != self._formats[publication.key.kind].renderer_version
            or publication.key.rendering_options
            != self._formats[publication.key.kind].options
        ):
            return False
        window = self._window
        if sip.isdeleted(window) or sip.isdeleted(window.RunList):
            return False
        if (
            getattr(window, "_shutdown_started", False)
            or getattr(window, "_shutdown_ready", False)
            or getattr(window, "_trusted_read_service", None) is not self._service
            or getattr(window, "_loaded_database_instance", None) is None
        ):
            return False
        loaded = window._loaded_database_instance
        if database_instances_differ(self._database_instance, loaded):
            return False
        current = database_instance(self._database_instance.logical_path)
        if database_instances_differ(
            self._database_instance, current
        ) or not self._database_instance.sidecar_identities.issubset(
            current.sidecar_identities
        ):
            self._queue_replacement_reload()
            return False
        item = window.RunList._item_for_guid(publication.key.run_guid)
        index = self._index_by_guid.get(publication.key.run_guid)
        if item is None or index is None:
            return False
        runs = self._coordinator.runs
        if (
            index >= len(runs)
            or runs[index].run_guid != publication.key.run_guid
            or runs[index].source_revision != publication.key.source_revision
        ):
            return False
        try:
            item_run_id = int(window.RunList._item_run_id(item))
        except (TypeError, ValueError):
            return False
        return item_run_id == self._run_ids[index]

    def _queue_replacement_reload(self) -> None:
        if self._replacement_reload_queued or self._database_instance is None:
            return
        expected_database = self._database_instance
        self._replacement_reload_queued = True
        path = expected_database.logical_path
        self.suspend_publications()
        QtCore.QTimer.singleShot(
            0,
            lambda: self._run_queued_replacement_reload(path, expected_database),
        )

    def _run_queued_replacement_reload(
        self,
        path: str,
        expected_database: DatabaseInstance,
    ) -> None:
        """Reload only while this bridge still owns the replacement claim."""

        self._require_owner()
        if (
            not self._replacement_reload_queued
            or self._shutting_down
            or self._database_instance is not expected_database
        ):
            return
        self._replacement_reload_queued = False
        self._window._reload_replaced_database(path)

    def _record_error(
        self,
        work: ScheduledWork,
        error: TrustedDerivedErrorRecord,
    ) -> None:
        self._require_owner()
        if not self._accepting_publications:
            return
        self._last_errors[(work.key.run_guid, work.key.kind)] = error.message

    def _decode_images(
        self,
        guid: str,
        payload: Mapping[str, object],
    ) -> tuple[list[dict[str, object]], str | None]:
        previews: list[dict[str, object]] = []
        parameters = {
            parameter.name: parameter
            for parameter in self._parameters_by_guid.get(guid, ())
        }
        images = payload.get("images")
        if not isinstance(images, tuple):
            return previews, "The derived image collection is invalid."
        for raw in images:
            image_fields = self._pairs(raw)
            encoded = image_fields.get("bytes")
            width = image_fields.get("width")
            height = image_fields.get("height")
            dependent = image_fields.get("dependent")
            if (
                not isinstance(encoded, bytes)
                or type(width) is not int
                or type(height) is not int
                or not isinstance(dependent, str)
            ):
                return [], "A derived image descriptor is invalid."
            image = QtGui.QImage.fromData(encoded, "PNG")
            if image.isNull() or image.width() != width or image.height() != height:
                return [], "A derived PNG could not be decoded safely."
            parameter = parameters.get(dependent)
            axes = list(parameter.depends_on) if parameter is not None else []
            title = dependent
            if axes:
                title += " vs " + ", ".join(axes)
            previews.append(
                {
                    "parameter": dependent,
                    "axes": axes,
                    "dimension_count": len(axes),
                    "title": title,
                    "image": image,
                }
            )
        return previews, None

    def _unavailable_preview(self, guid: str, description: str) -> dict[str, object]:
        parameters = self._parameters_by_guid.get(guid, ())
        parameter = next((item for item in parameters if item.depends_on), None)
        axes = list(parameter.depends_on) if parameter is not None else []
        return {
            "parameter": parameter.name if parameter is not None else "",
            "axes": axes,
            "dimension_count": len(axes),
            "title": description,
            "unsupported": True,
        }

    @staticmethod
    def _pairs(value: object) -> dict[object, object]:
        if not isinstance(value, tuple):
            return {}
        output: dict[object, object] = {}
        for item in value:
            if not isinstance(item, tuple) or len(item) != 2:
                return {}
            output[item[0]] = item[1]
        return output

    @staticmethod
    def _parameter_views(value: object) -> tuple[TrustedParameterView, ...]:
        if not isinstance(value, tuple):
            return ()
        output: list[TrustedParameterView] = []
        for item in value:
            if not isinstance(item, tuple) or len(item) != 5:
                return ()
            name, label, unit, depends_on, paramtype = item
            if not all(
                isinstance(part, str) for part in (name, label, unit, paramtype)
            ):
                return ()
            if not isinstance(depends_on, tuple) or not all(
                isinstance(axis, str) for axis in depends_on
            ):
                return ()
            output.append(
                TrustedParameterView(name, label, unit, depends_on, paramtype)
            )
        return tuple(output)

    @staticmethod
    def _setpoint_summaries(value: object) -> tuple[TrustedSetpointSummary, ...]:
        if not isinstance(value, tuple):
            return ()
        output: list[TrustedSetpointSummary] = []
        for item in value:
            if not isinstance(item, tuple) or len(item) != 4:
                return ()
            name, first, last, steps = item
            if not isinstance(name, str) or (
                steps is not None and type(steps) is not int
            ):
                return ()
            output.append(TrustedSetpointSummary(name, first, last, steps))
        return tuple(output)

    def _normalise_runs(
        self,
        database: DatabaseInstance,
        runs: Mapping[int, Mapping[str, object]],
        service: TrustedLiveReadService,
    ) -> tuple[tuple[TrustedDerivedRun, ...], list[int], list[str]]:
        ordered: list[tuple[int, str]] = []
        for raw_run_id, metadata in runs.items():
            try:
                run_id = int(raw_run_id)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "Trusted run ids must be positive integers."
                ) from error
            guid = str(metadata.get("guid") or "")
            if run_id <= 0 or not guid:
                raise ValueError("Every trusted run requires an id and GUID.")
            ordered.append((run_id, guid))
        ordered.sort(key=lambda item: item[0])
        run_ids = [item[0] for item in ordered]
        run_guids = [item[1] for item in ordered]
        if len(run_ids) != len(set(run_ids)) or len(run_guids) != len(set(run_guids)):
            raise ValueError("Trusted run ids and GUIDs must be unique.")
        derived = tuple(
            self._derived_run(database, run_id, guid, service)
            for run_id, guid in ordered
        )
        return derived, run_ids, run_guids

    @staticmethod
    def _derived_run(
        database: DatabaseInstance,
        run_id: int,
        guid: str,
        service: TrustedLiveReadService,
    ) -> TrustedDerivedRun:
        provisional = repr(
            (
                "qplot-stage5c-provisional-source-v1",
                database.logical_path,
                database.resolved_path,
                database.identity,
                service.source_revision_namespace.nonce,
                run_id,
                guid,
            )
        ).encode("utf-8", errors="surrogatepass")
        return TrustedDerivedRun(
            run_id,
            guid,
            TrustedSourceRevision(hashlib.sha256(provisional).digest()),
        )

    @staticmethod
    def _work_formats(preview_size: int) -> dict[TrustedWorkKind, WorkFormat]:
        return {
            TrustedWorkKind.METADATA: WorkFormat(TRUSTED_DERIVED_RENDERER_VERSION),
            TrustedWorkKind.THUMBNAIL: WorkFormat(
                TRUSTED_DERIVED_RENDERER_VERSION,
                RenderingOptions.from_mapping(
                    {"height": _THUMBNAIL_HEIGHT, "width": _THUMBNAIL_WIDTH}
                ),
            ),
            TrustedWorkKind.PREVIEW: TrustedDerivedQtBridge._preview_format(
                preview_size
            ),
        }

    @staticmethod
    def _preview_format(preview_size: int) -> WorkFormat:
        size = max(1, min(2_048, int(preview_size)))
        return WorkFormat(
            TRUSTED_DERIVED_RENDERER_VERSION,
            RenderingOptions.from_mapping({"height": size, "width": size}),
        )

    @QtCore.pyqtSlot()
    def _reap_retiring(self) -> None:
        self._require_owner()
        remaining: list[TrustedWorkCoordinator] = []
        for coordinator in self._retiring:
            coordinator.poll()
            if not coordinator.wait_closed(0.0):
                remaining.append(coordinator)
        self._retiring = remaining
        if not remaining:
            self._retire_timer.stop()

    def _require_owner(self) -> None:
        if (
            threading.get_ident() != self._owner_thread_id
            or QtCore.QThread.currentThread() != self.thread()
        ):
            raise RuntimeError(
                "TrustedDerivedQtBridge must mutate state on its Qt owner thread."
            )


__all__ = ["TrustedDerivedQtBridge"]
