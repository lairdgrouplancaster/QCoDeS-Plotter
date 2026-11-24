"""User-requested standalone database copies."""

import os

from PyQt6 import QtWidgets as qtw

from qplot.datahandling.database_export import export_database_copy, require_source
from qplot.datahandling.file_identity import SQLITE_SIDECAR_SUFFIXES
from qplot.datahandling.trusted_live_service import TRUSTED_LIVE_MODE

from ._export_paths import (
    UnsafeExportDestinationError,
    normalize_export_path,
    prepare_export_destination,
)
from ._export_tasks import start_export_task
from ._small_dialogs import confirm_action


def _require_new_destination(filename):
    if any(os.path.lexists(filename + suffix) for suffix in ("", *SQLITE_SIDECAR_SUFFIXES)):
        raise ValueError("Choose a new filename without an existing database or SQLite sidecars.")


def _choose_database_destination(window, title, suggested):
    while True:
        filename, _filter = qtw.QFileDialog.getSaveFileName(
            window, f"{title} — choose a new filename", suggested,
            "QCoDeS databases (*.db)", options=qtw.QFileDialog.Option.DontConfirmOverwrite,
        )
        if not filename:
            return None
        suggested = normalize_export_path(filename, ".db")
        try:
            _require_new_destination(suggested)
            return prepare_export_destination(window, suggested, existing_target_validator=_require_new_destination)
        except (ValueError, OSError, UnsafeExportDestinationError) as error:
            if not confirm_action(window, "Choose a new database filename",
                                  f"{suggested}\n\n{error}", affirmative="Choose another name…"):
                return None


def export_database(window, *, selected=False):
    if not window._generation_gate_allows_action(operation="exporting a database copy"):
        return
    instance = getattr(window, "_loaded_database_instance", None)
    if instance is None:
        window.show_status("Load a database before exporting.", 5000)
        return
    run_ids = tuple(window.RunList.selected_run_ids()) if selected else None
    if selected and not run_ids:
        window.show_status("Select runs in the table before exporting.", 5000)
        return
    title = f"Export {len(run_ids)} selected runs as database" if selected else "Save a copy of the database"
    generation = window._database_load_generation
    live = getattr(window, "_database_access_mode", None) == TRUSTED_LIVE_MODE
    try:
        require_source(instance)
        stem = os.path.splitext(instance.logical_path)[0]
        destination = _choose_database_destination(window, title, stem + ("-selected.db" if selected else "-copy.db"))
        if destination is None:
            return

        def validate():
            if (generation != window._database_load_generation
                    or instance != getattr(window, "_loaded_database_instance", None)):
                raise InterruptedError("The selected database changed during export.")
            require_source(instance)
            _require_new_destination(destination.filename)

        validate()

        def write(filename, cancelled, progress):
            return export_database_copy(
                filename, instance=instance, live=live,
                cancelled=cancelled, progress=progress,
                run_ids=run_ids,
            )

        start_export_task(
            window, title=(f"{len(run_ids)} selected runs" if selected else "Database copy")
            + f" · {os.path.basename(instance.logical_path)}",
            destination=destination, writer=write, before_publish=validate,
            on_success=lambda _result: window.show_status(
                f"Exported standalone database copy: {destination.filename}", 8000,
            ),
        )
    except Exception as error:
        window.show_error("Database Export Failed", "Could not export a database copy.", str(error))
