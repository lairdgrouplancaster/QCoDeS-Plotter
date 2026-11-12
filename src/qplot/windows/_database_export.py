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


def export_database(window):
    if not window._generation_gate_allows_action(operation="exporting a database copy"):
        return
    instance = getattr(window, "_loaded_database_instance", None)
    if instance is None:
        window.show_status("Load a database before exporting.", 5000)
        return
    generation = window._database_load_generation
    live = getattr(window, "_database_access_mode", None) == TRUSTED_LIVE_MODE
    try:
        require_source(instance)
        stem = os.path.splitext(instance.logical_path)[0]
        destination = choose_export_path(
            window, caption="Export Database Copy", suggested_path=stem + "-copy.db",
            name_filter="QCoDeS databases (*.db)", required_suffix=".db",
            replace_title="Export Database Copy", file_description="database copy",
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
            )

        start_export_task(
            window, title=f"Database copy · {os.path.basename(instance.logical_path)}",
            destination=destination, writer=write, before_publish=validate,
            on_success=lambda _result: window.show_status(
                f"Exported standalone database copy: {destination.filename}", 8000,
            ),
        )
    except Exception as error:
        window.show_error("Database Export Failed", "Could not export a database copy.", str(error))
