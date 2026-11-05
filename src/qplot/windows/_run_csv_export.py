"""Main-window raw CSV requests, including an explicit multi-run workflow."""

from __future__ import annotations

import os
from functools import partial

from PyQt6 import QtWidgets as qtw

from qplot.datahandling.file_identity import logical_database_path
from qplot.datahandling.readonly import DatabaseInstanceChangedError
from qplot.datahandling.trusted_live_service import TRUSTED_LIVE_MODE
from qplot.diagnostics import log_exception

from ._export_names import suggested_export_filename
from ._export_paths import approve_export_path
from ._export_tasks import export_task_manager, start_export_task


def run_export_request(window):
    """Capture the target and selector without reading any measurement data.

    None selects all quantities; a positive integer is resolved against the
    freshly captured descriptor in the background, never partial row metadata.
    """
    database_path = window.fileTextbox.text()
    if not database_path:
        window.show_status("Load a database before exporting.", 5000)
        return None
    run_id = window.selected_run_id
    if run_id is None:
        window.show_status("Select or enter a Run ID before exporting.", 5000)
        return None
    reload_if_changed = getattr(window, "_reload_if_database_instance_changed", None)
    if callable(reload_if_changed) and reload_if_changed(database_path):
        window.show_status("Database was replaced; reloading before export.", 5000)
        return None
    metadata = window._run_metadata_for_id(run_id)
    guid = metadata.get("guid")
    dataset = getattr(window, "ds", None)
    if not guid and dataset is not None and getattr(dataset, "run_id", None) == run_id:
        guid = dataset.guid
    if not guid:
        window.show_status(f"Run {run_id} is not available in the loaded database.", 5000)
        return None
    selection = window.measurementBox.text().strip()
    if selection in ("", "*"):
        selector = None
    else:
        try:
            selector = int(selection)
            if selector < 1:
                raise ValueError
        except ValueError:
            window.show_status("Measurement must be a positive number or *.", 5000)
            return None
    return window._current_dataset_key(str(guid)), selector, run_id


def _filename_names(window, key, selector, run_id):
    if isinstance(selector, (tuple, list)):
        return tuple(selector)
    names = tuple(window._run_metadata_for_id(run_id).get("measure_parameters") or ())
    if isinstance(selector, int):
        return (names[selector - 1],) if selector <= len(names) else (f"measurement_{selector}",)
    return names or ("all",)


def _writer_for(window, key, selector):
    from qplot.datahandling.csv_export import export_raw_csv

    service = None
    if getattr(window, "_database_access_mode", None) == TRUSTED_LIVE_MODE:
        service = getattr(window, "_trusted_read_service", None)
        if service is None or service.database_instance.identity != key.database_identity:
            raise RuntimeError("The live reader for this export is no longer available.")
    return partial(
        export_raw_csv, database_path=key.database_path, guid=key.guid,
        parameter_names=tuple(selector) if isinstance(selector, (tuple, list)) else None,
        measurement_index=selector if isinstance(selector, int) else None,
        service=service, expected_database_identity=key.database_identity,
    )


def queue_raw_export(window, key, selector, *, run_id=None, destination=None):
    """Choose first, then capture the live source in a cancellable worker."""
    if isinstance(selector, (tuple, list)) and not selector:
        window.show_status("No measurements to export for this run.", 5000)
        return None
    if run_id is None:
        run_id = window.RunList.run_id_for_guid(key.guid)
    generation = getattr(window, "_database_load_generation", None)
    try:
        if destination is None:
            name = window._default_run_csv_export_filename(
                key, _filename_names(window, key, selector, run_id), run_id=run_id,
            )
            destination = window._choose_csv_export_filename(name)
        if destination is None:
            return None
        if (generation != getattr(window, "_database_load_generation", None)
                or logical_database_path(window.fileTextbox.text()) != key.database_path):
            window.show_status("CSV export cancelled because the selected database changed.", 5000)
            return None
        key = window._bind_one_shot_dataset_key(key)
        window._require_run_csv_source_current(key)
        write_raw = _writer_for(window, key, selector)

        def validate():
            if generation != getattr(window, "_database_load_generation", None):
                raise InterruptedError("The database view changed during export.")
            window._require_run_csv_source_current(key)

        def write(filename, cancelled, progress):
            return write_raw(filename, cancelled=cancelled, progress=progress)

        def on_failure(error):
            if isinstance(error, DatabaseInstanceChangedError):
                window._handle_run_csv_source_replaced(key)
                return True
            return False

        title = f"Run {run_id} · raw measurement values"
        if isinstance(selector, (tuple, list)) and len(selector) == 1:
            title = f"Run {run_id} · {selector[0]} · raw values"
        return start_export_task(
            window, title=title,
            destination=destination, writer=write, before_publish=validate,
            source_keys=(key,),
            on_failure=on_failure,
            on_success=lambda receipt: window.show_status(
                f"Exported run {run_id}: {receipt.row_count:,} CSV rows · {destination}", 7000,
            ),
        )
    except DatabaseInstanceChangedError:
        window._handle_run_csv_source_replaced(key)
        return None
    except Exception as error:
        log_exception("CSV export request failed", error, __name__)
        window.show_error("CSV Export Failed", "Could not start the raw measurement export.", str(error))
        return None


class BatchCsvDialog(qtw.QDialog):
    """Small review step before a batch's exact filenames are approved."""

    def __init__(self, parent, requests, common_parameters):
        super().__init__(parent)
        self.setWindowTitle("Export selected runs as CSV")
        self.resize(470, 260)
        layout = qtw.QVBoxLayout(self)
        label = qtw.QLabel(
            f"Export {len(requests)} selected runs, one CSV per run.\n"
            "Values are raw measurements, without plot operations.\n"
            "Each run captures the committed data available when its export starts."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        runs = qtw.QLabel("Runs: " + ", ".join(str(run_id) for _key, run_id in requests))
        runs.setWordWrap(True)
        layout.addWidget(runs)
        form = qtw.QFormLayout()
        self.parameters = qtw.QComboBox()
        self.parameters.addItem("All measurements in each run", None)
        for name in sorted(common_parameters):
            self.parameters.addItem(name, (name,))
        form.addRow("Measurements:", self.parameters)
        layout.addLayout(form)
        note = qtw.QLabel("Exports run in the background. Cancelling keeps any files already completed.")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = qtw.QDialogButtonBox(
            qtw.QDialogButtonBox.StandardButton.Ok | qtw.QDialogButtonBox.StandardButton.Cancel,
        )
        choose_folder = buttons.button(qtw.QDialogButtonBox.StandardButton.Ok)
        assert choose_folder is not None
        choose_folder.setText("Choose folder…")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def export_selected_runs(window):
    selected = window.RunList.selected_run_ids()
    if not selected:
        window.show_status("Select runs in the table before batch export.", 5000)
        return
    if not window._generation_gate_allows_action(operation="exporting selected runs"):
        return
    generation = getattr(window, "_database_load_generation", None)
    requests = []
    common = None
    for run_id in selected:
        metadata = window._run_metadata_for_id(run_id)
        guid = metadata.get("guid")
        if not guid:
            window.show_status("Wait for the selected runs to finish loading.", 5000)
            return
        requests.append((window._current_dataset_key(str(guid)), run_id))
        names = set(metadata.get("measure_parameters") or ())
        common = names if common is None else common & names
    dialog = BatchCsvDialog(window, requests, common or ())
    try:
        if dialog.exec() != qtw.QDialog.DialogCode.Accepted:
            return
        selector = dialog.parameters.currentData()
    finally:
        dialog.deleteLater()
    folder = qtw.QFileDialog.getExistingDirectory(
        window, "Export raw measurement CSV files", os.path.dirname(requests[0][0].database_path),
    )
    if not folder:
        return
    _queue_csv_batch(
        window, [(key, run_id, selector) for key, run_id in requests],
        folder, generation,
    )


def export_selected_previews(window, targets):
    """Export a captured thumbnail group, one raw measurement per file."""
    if not window._generation_gate_allows_action(operation="exporting selected thumbnails"):
        return
    generation = getattr(window, "_database_load_generation", None)
    requests = []
    seen = set()
    for guid, parameter in tuple(targets):
        target = (str(guid or ""), str(parameter or ""))
        if target in seen:
            continue
        seen.add(target)
        run_id = window.RunList.run_id_for_guid(target[0])
        if not all(target) or run_id is None:
            window.show_status("The selected thumbnails are no longer available.", 5000)
            return
        requests.append((window._current_dataset_key(target[0]), run_id, (target[1],)))
    if not requests:
        return
    folder = qtw.QFileDialog.getExistingDirectory(
        window, f"Export {len(requests)} selected thumbnails as raw CSV files",
        os.path.dirname(requests[0][0].database_path),
    )
    if not folder:
        return
    _queue_csv_batch(window, requests, folder, generation, name_measurements=True)


def _queue_csv_batch(window, requests, folder, generation, *, name_measurements=False):
    """Approve every exact output and bind every source before queuing work."""
    planned = []
    names_seen = set()
    try:
        if generation != getattr(window, "_database_load_generation", None):
            window.show_status("Batch CSV export cancelled because the database changed.", 5000)
            return
        for key, run_id, selector in requests:
            default = window._default_run_csv_export_filename(
                key, _filename_names(window, key, selector, run_id), run_id=run_id,
            )
            # The optional QCoDeS filename format identifies only a run. Give
            # each selected measurement a name when sharing that run prefix.
            if name_measurements:
                measurement = suggested_export_filename(selector[0])
                stem, ext = os.path.splitext(default)
                if not stem.endswith(f"_{measurement}"):
                    default = f"{stem}_{measurement}{ext}"
            filename = os.path.join(folder, os.path.basename(default))
            normalized = os.path.normcase(os.path.realpath(filename)).casefold()
            stem, ext = os.path.splitext(filename)
            ordinal = 2
            while normalized in names_seen:
                filename = f"{stem}_{ordinal}{ext}"
                normalized = os.path.normcase(os.path.realpath(filename)).casefold()
                ordinal += 1
            names_seen.add(normalized)
            destination = approve_export_path(
                window, filename, replace_title="Replace CSV File?", file_description="CSV file",
            )
            if destination is None:
                window.show_status("Batch CSV export cancelled before starting.", 5000)
                return
            planned.append((key, run_id, selector, destination))
        if generation != getattr(window, "_database_load_generation", None):
            window.show_status("Batch CSV export cancelled because the database changed.", 5000)
            return
        # Bind every source before queuing any output. A failure in preparation
        # must not silently export a different selection after a modal dialog.
        bound = []
        for key, run_id, selector, destination in planned:
            key = window._bind_one_shot_dataset_key(key)
            window._require_run_csv_source_current(key)
            bound.append((key, run_id, selector, destination))
        for key, run_id, selector, destination in bound:
            queue_raw_export(window, key, selector, run_id=run_id, destination=destination)
    except Exception as error:
        log_exception("Batch CSV export request failed", error, __name__)
        window.show_error("CSV Export Failed", "Could not prepare the selected exports.", str(error))


def show_exports(window):
    export_task_manager(window).dialog.show()
