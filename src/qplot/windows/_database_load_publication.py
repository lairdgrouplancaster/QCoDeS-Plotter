"""Own the GUI transaction that stages, commits, or restores a database view.

Run-list insertion can pump Qt events. Keep the pending reader reachable by
close/shutdown until the transaction commits, and revalidate both sources at
every publication boundary. No database contents are read or written here.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum, auto
from typing import TYPE_CHECKING

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.readonly import DatabaseInstanceChangedError
from qplot.datahandling.trusted_live_service import TRUSTED_LIVE_MODE

from ._auto_plot import cancel_auto_plot
from ._database_load_controller import AcceptedDatabaseLoad
from ._database_observation import database_instances_or_sidecars_differ

if TYPE_CHECKING:
    from .main import MainWindow


class _PublicationAborted(RuntimeError):
    """A nested Qt event closed the view while rows were being inserted."""


class PublicationState(Enum):
    STAGING = auto()
    COMMITTED = auto()
    RESTORED = auto()
    ABORTED = auto()


class DatabaseLoadPublication:
    """Own one publication and its reader and signal lifetimes."""

    def __init__(
        self,
        window: MainWindow,
        result: AcceptedDatabaseLoad,
        *,
        observe_instance: Callable[[str], DatabaseInstance],
    ) -> None:
        self.window = window
        self.result = result
        self.generation = result.generation
        self.session = result.session
        self.accepted_instance = result.instance
        self.observe_instance = observe_instance
        self.runs = result.runs
        self.pending_service = result.service
        self.access_mode = result.access_mode
        self.fallback_reason = result.fallback_reason
        self.snapshot = window._capture_database_load_publication()
        self.old_service = self.snapshot.trusted_service
        self.new_service = result.service if result.access_mode == TRUSTED_LIVE_MODE else None
        self.derived_bridge = getattr(window, "_trusted_derived_bridge", None)
        self.staged_instance = result.instance
        self.error: Exception | None = None
        self.rollback_error: Exception | None = None
        self.rollback_source_changed = False
        self.state = PublicationState.STAGING

    @property
    def committed(self) -> bool:
        return self.state is PublicationState.COMMITTED

    @property
    def restored(self) -> bool:
        return self.state is PublicationState.RESTORED

    @property
    def aborted(self) -> bool:
        return self.state is PublicationState.ABORTED

    @property
    def source_changed(self) -> bool:
        return isinstance(self.error, DatabaseInstanceChangedError)

    def is_current(self) -> bool:
        window = self.window
        return bool(
            window._database_load_publication_active
            and self.generation == window._database_load_generation
            and not getattr(window, "_shutdown_started", False)
            and not getattr(window, "_shutdown_ready", False)
        )

    def run(self) -> None:
        try:
            with self.window._database_loader.publication(
                self.result, committed=lambda: self.committed,
            ) as current:
                if current:
                    self._run_view_transaction()
                else:
                    self.state = PublicationState.ABORTED
        finally:
            self.window._sync_database_generation_controls()

    def _run_view_transaction(self) -> None:
        window = self.window
        # Old metadata jobs must not merge into a staged model during Qt yields.
        window._cancel_database_detail_load()
        window._cancel_selected_run_detail()
        window._cancel_database_info()
        if self.derived_bridge is not None:
            self.derived_bridge.suspend_publications()
        window._sync_database_generation_controls()
        run_id_blocked = window.run_idBox.blockSignals(True)
        run_list_blocked = window.RunList.blockSignals(True)
        preview_was_suspended = False
        preview_gate_set = False
        try:
            preview_was_suspended = window.RunList.set_preview_publication_suspended(
                True
            )
            preview_gate_set = True
            try:
                self._stage_and_commit()
            except _PublicationAborted:
                self.state = PublicationState.ABORTED
            except Exception as error:
                self.error = error

            if self.committed:
                self._transfer_services()
            if self.error is not None:
                self._rollback()
            if self.aborted:
                # A nested close may leave a final row inserted before cancellation.
                window.RunList.clearSelection()
                window.RunList.clear()
                window.RunList.watching = []
                window.RunList.maxRunId = 0
        finally:
            try:
                window.RunList.blockSignals(run_list_blocked)
                window.run_idBox.blockSignals(run_id_blocked)
            finally:
                if preview_gate_set:
                    window.RunList.set_preview_publication_suspended(preview_was_suspended)

    def _stage_and_commit(self) -> None:
        # Stage B's run model while every action remains gated and A is
        # still the committed service/instance.  Only after staging and
        # one final source observation do the plain controller fields
        # advance together to B.
        self.window._prepare_database_load_ui(
            self.session.abspath,
            release_selected=False,
            clear_details=False,
            update_path=False,
        )
        staged = self.window.RunList.addRuns(
            self.runs,
            continue_loading=self.is_current,
        )
        if staged is False or not self.is_current():
            raise _PublicationAborted(
                "The database view was closed while B was staged."
            )
        self.staged_instance = self.observe_instance(self.session.abspath)
        if database_instances_or_sidecars_differ(
            self.accepted_instance,
            self.staged_instance,
        ):
            raise DatabaseInstanceChangedError(
                "The database changed while its loaded view was staged."
            )

        self.window._commit_database_load_view(
            self.snapshot,
            self.session.abspath,
            self.runs,
            self.new_service,
            self.access_mode,
            self.fallback_reason,
            self.staged_instance,
        )
        self.state = PublicationState.COMMITTED

    def _transfer_services(self) -> None:
        # The controller fields now name B.  Transfer every remaining
        # service immediately, before signal restoration or control
        # synchronisation in the enclosing finally can raise.
        if self.old_service is not self.new_service:
            cancel_auto_plot(self.window)
            # The old coordinator may still own a bounded read against
            # A.  Retire it at the same commit boundary that transfers
            # the trusted service, before the queued Stage 5C bind
            # creates B's coordinator.  This keeps stale work harmless
            # and promptly releases Windows reader locks.
            clear_derived = getattr(
                self.derived_bridge,
                "clear_database",
                None,
            )
            if callable(clear_derived):
                clear_derived()
            self.window._retire_trusted_read_service(
                self.old_service,
                force=False,
            )
        if self.access_mode != TRUSTED_LIVE_MODE and self.session.owns_trusted_service:
            self.window._retire_trusted_read_service(
                self.pending_service,
                force=True,
            )

    def _rollback(self) -> None:
        try:
            rollback_path = self.snapshot.database_path
            rollback_instance = self.snapshot.database_instance
            rollback_current_instance = None
            if rollback_path and isinstance(rollback_instance, DatabaseInstance):
                rollback_current_instance = self.observe_instance(rollback_path)
                if database_instances_or_sidecars_differ(
                    rollback_instance,
                    rollback_current_instance,
                ):
                    self.rollback_source_changed = True
                    raise DatabaseInstanceChangedError(
                        "The previously displayed database changed while "
                        "the replacement view was staged."
                    )
            restored = self.window._restore_database_load_publication(
                self.snapshot,
                continue_loading=self.is_current,
            )
            if restored is False or not self.is_current():
                self.error = None
                self.state = PublicationState.ABORTED
            else:
                if rollback_current_instance is not None:
                    rollback_final_instance = self.observe_instance(rollback_path)
                    if database_instances_or_sidecars_differ(
                        rollback_current_instance,
                        rollback_final_instance,
                    ):
                        self.rollback_source_changed = True
                        raise DatabaseInstanceChangedError(
                            "The previously displayed database changed "
                            "while its view was restored."
                        )
                    self.window._advance_loaded_database_sidecar_baseline(
                        rollback_final_instance,
                    )
                self.state = PublicationState.RESTORED
        except Exception as rollback_exception:
            self.rollback_error = rollback_exception
            self.error = None
            self.state = PublicationState.ABORTED
            # A partially restored controller must not retain either
            # reader or publish a mixture of A and B.  This path uses
            # only normal controller teardown and filesystem identity
            # observations; it performs no database read.
            self.window._retire_trusted_read_service(
                self.old_service,
                force=True,
            )
            self.window.close_database(status=False)
