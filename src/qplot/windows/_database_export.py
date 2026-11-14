"""User-requested standalone database copies."""

import os

from qplot.datahandling.database_export import export_database_copy, require_source
from qplot.datahandling.file_identity import SQLITE_SIDECAR_SUFFIXES
from qplot.datahandling.trusted_live_service import TRUSTED_LIVE_MODE

from ._export_paths import choose_export_path
from ._export_tasks import start_export_task


def _require_new_destination(filename):
    if any(os.path.lexists(filename + suffix) for suffix in ("", *SQLITE_SIDECAR_SUFFIXES)):
        raise ValueError("Choose a new filename without an existing database or SQLite sidecars.")


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
    title = f"Export {len(run_ids)} Selected Runs as Database" if selected else "Export All Runs as Database"
    generation = window._database_load_generation
    live = getattr(window, "_database_access_mode", None) == TRUSTED_LIVE_MODE
    try:
        require_source(instance)
        stem = os.path.splitext(instance.logical_path)[0]
        destination = choose_export_path(
            window, caption=title, suggested_path=stem + ("-selected.db" if selected else "-copy.db"),
            name_filter="QCoDeS databases (*.db)", required_suffix=".db",
            replace_title=title, file_description="database copy",
            existing_target_validator=_require_new_destination,
        )
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
