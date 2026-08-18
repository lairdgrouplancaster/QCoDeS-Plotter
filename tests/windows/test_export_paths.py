import os
import stat
import unicodedata
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import qcodes
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw
from qcodes.dataset import initialise_or_create_database_at

from qplot.datahandling.file_identity import (
    SQLITE_SIDECAR_SUFFIXES,
    database_file_identity,
)
from qplot.windows._dataset_handle import DatasetKey
from qplot.windows._export_paths import (
    UnsafeExportDestinationError,
    prepare_export_destination,
    write_export_atomically,
)
from qplot.windows._plot_actions import PlotActionsMixin
from qplot.windows._plot_export import PlotExportMixin

_DATABASE_ARTIFACT_SUFFIXES = ("", *SQLITE_SIDECAR_SUFFIXES)


def _path_artifact_state(path: Path):
    """Capture an entry without losing final-link or platform identity details."""
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return None

    is_regular = stat.S_ISREG(file_stat.st_mode)
    return {
        "lstat_identity": (int(file_stat.st_dev), int(file_stat.st_ino)),
        "database_identity": database_file_identity(path),
        "mode": int(file_stat.st_mode),
        "nlink": int(file_stat.st_nlink),
        "size": int(file_stat.st_size),
        "mtime_ns": int(file_stat.st_mtime_ns),
        "bytes": path.read_bytes() if is_regular else None,
        "link_target": os.readlink(path) if stat.S_ISLNK(file_stat.st_mode) else None,
    }


def _database_artifact_state(database_path: Path):
    """Capture bytes, metadata, identities, and absence for every DB artifact."""
    return {
        suffix: _path_artifact_state(Path(f"{database_path}{suffix}"))
        for suffix in _DATABASE_ARTIFACT_SUFFIXES
    }


def _write_database_artifacts(
    database_path: Path,
    *,
    sidecars: tuple[str, ...] = (),
) -> None:
    """Create inert database-shaped bytes without ever opening SQLite."""
    database_path.write_bytes(b"SQLite format 3\x00qPlot protected input")
    for suffix in sidecars:
        Path(f"{database_path}{suffix}").write_bytes(
            f"protected SQLite artifact {suffix}".encode()
        )


class _DatabaseOwner:
    def __init__(self, database_path: Path):
        self._dataset_key = DatasetKey(str(database_path), f"guid-{database_path.name}")


def _assert_database_unchanged(database_path: Path, before) -> None:
    assert _database_artifact_state(database_path) == before


class _CsvFrame:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def to_csv(self, filename, *, index: bool):
        assert not index
        Path(filename).write_bytes(b"new,csv\n1,2\n")
        if self.fail:
            raise RuntimeError("CSV writer failed after opening its output")


class _CsvHarness(PlotActionsMixin):
    def __init__(self, selected_path: Path, *, fail: bool = False):
        self.selected_path = selected_path
        self.frame = _CsvFrame(fail=fail)
        self.status_messages = []
        self.errors = []

    def _default_export_filename(self, dataset, params):
        return str(self.selected_path)

    def _measurement_dataframe(self, dataset, params):
        return self.frame

    def show_status(self, message, timeout=5000):
        self.status_messages.append((message, timeout))

    def show_error(self, title, message, details=None):
        self.errors.append((title, message, details))


class _PdfHarness(PlotExportMixin):
    def __init__(self, selected_path: Path, *, fail: bool = False):
        self.selected_path = selected_path
        self.fail = fail
        self.status_messages = []
        self.errors = []

    def _default_plot_pdf_filename(self):
        return str(self.selected_path)

    def _write_plot_pdf(self, filename):
        Path(filename).write_bytes(b"%PDF-new")
        if self.fail:
            raise RuntimeError("PDF writer failed after opening its output")
        return True

    def show_status(self, message, timeout=5000):
        self.status_messages.append((message, timeout))

    def show_error(self, title, message, details=None):
        self.errors.append((title, message, details))


def _invoke_export(kind: str, selected_path: Path, *, fail: bool = False):
    if kind == "csv":
        harness = _CsvHarness(selected_path, fail=fail)
        harness._export_measurement_csv(object(), [object()])
        return harness

    harness = _PdfHarness(selected_path, fail=fail)
    harness.save_plot_pdf()
    return harness


@pytest.mark.parametrize("kind", ["csv", "pdf"])
def test_unsuffixed_existing_export_decline_preserves_exact_target(tmp_path, kind):
    selected_path = tmp_path / "report"
    final_path = tmp_path / f"report.{kind}"
    sentinel = b"existing export sentinel"
    final_path.write_bytes(sentinel)

    with (
        patch.object(
            qtw.QFileDialog,
            "getSaveFileName",
            return_value=(str(selected_path), ""),
        ) as save_dialog,
        patch.object(
            qtw.QMessageBox,
            "question",
            return_value=qtw.QMessageBox.StandardButton.No,
        ) as question,
    ):
        harness = _invoke_export(kind, selected_path)

    assert final_path.read_bytes() == sentinel
    assert not selected_path.exists()
    assert set(tmp_path.iterdir()) == {final_path}
    assert "cancelled" in harness.status_messages[-1][0].lower()
    assert harness.errors == []
    dialog_options = save_dialog.call_args.kwargs["options"]
    assert dialog_options & qtw.QFileDialog.Option.DontConfirmOverwrite
    assert dialog_options & qtw.QFileDialog.Option.DontUseNativeDialog
    assert str(final_path) in question.call_args.args[2]


@pytest.mark.parametrize("kind", ["csv", "pdf"])
def test_unsuffixed_existing_export_accept_replaces_exact_target(tmp_path, kind):
    selected_path = tmp_path / "report"
    final_path = tmp_path / f"report.{kind}"
    sentinel = b"existing export sentinel"
    final_path.write_bytes(sentinel)

    with (
        patch.object(
            qtw.QFileDialog,
            "getSaveFileName",
            return_value=(str(selected_path), ""),
        ),
        patch.object(
            qtw.QMessageBox,
            "question",
            return_value=qtw.QMessageBox.StandardButton.Yes,
        ) as question,
    ):
        harness = _invoke_export(kind, selected_path)

    assert final_path.read_bytes() != sentinel
    assert not selected_path.exists()
    assert set(tmp_path.iterdir()) == {final_path}
    assert str(final_path) in harness.status_messages[-1][0]
    assert harness.errors == []
    question.assert_called_once()


@pytest.mark.parametrize("kind", ["csv", "pdf"])
def test_export_dialog_cancel_creates_no_files(tmp_path, kind):
    selected_path = tmp_path / "report"

    with (
        patch.object(qtw.QFileDialog, "getSaveFileName", return_value=("", "")),
        patch.object(qtw.QMessageBox, "question") as question,
    ):
        harness = _invoke_export(kind, selected_path)

    assert list(tmp_path.iterdir()) == []
    assert "cancelled" in harness.status_messages[-1][0].lower()
    assert harness.errors == []
    question.assert_not_called()


@pytest.mark.parametrize(
    ("kind", "filename"),
    [
        ("csv", "report.csv"),
        ("csv", "report.CSV"),
        ("pdf", "report.pdf"),
        ("pdf", "report.PDF"),
    ],
)
def test_suffixed_export_preserves_suffix_without_confirmation(tmp_path, kind, filename):
    selected_path = tmp_path / filename

    with (
        patch.object(
            qtw.QFileDialog,
            "getSaveFileName",
            return_value=(str(selected_path), ""),
        ),
        patch.object(qtw.QMessageBox, "question") as question,
    ):
        harness = _invoke_export(kind, selected_path)

    assert selected_path.is_file()
    assert set(tmp_path.iterdir()) == {selected_path}
    assert str(selected_path) in harness.status_messages[-1][0]
    assert harness.errors == []
    question.assert_not_called()


@pytest.mark.parametrize("kind", ["csv", "pdf"])
def test_export_failure_preserves_existing_target(tmp_path, kind):
    selected_path = tmp_path / f"report.{kind}"
    sentinel = b"existing export sentinel"
    selected_path.write_bytes(sentinel)

    with (
        patch.object(
            qtw.QFileDialog,
            "getSaveFileName",
            return_value=(str(selected_path), ""),
        ),
        patch.object(
            qtw.QMessageBox,
            "question",
            return_value=qtw.QMessageBox.StandardButton.Yes,
        ),
    ):
        harness = _invoke_export(kind, selected_path, fail=True)

    assert selected_path.read_bytes() == sentinel
    assert set(tmp_path.iterdir()) == {selected_path}
    assert harness.errors[0][0] in {"CSV Export Failed", "PDF Export Failed"}


@pytest.mark.parametrize("kind", ["csv", "pdf"])
def test_unsuffixed_export_creates_final_target_when_absent(tmp_path, kind):
    selected_path = tmp_path / "new-report"
    final_path = tmp_path / f"new-report.{kind}"

    with (
        patch.object(
            qtw.QFileDialog,
            "getSaveFileName",
            return_value=(str(selected_path), ""),
        ),
        patch.object(qtw.QMessageBox, "question") as question,
        patch(
            "qplot.windows._export_paths.os.fsync",
            wraps=os.fsync,
        ) as fsync,
    ):
        harness = _invoke_export(kind, selected_path)

    assert final_path.is_file()
    assert not selected_path.exists()
    assert set(tmp_path.iterdir()) == {final_path}
    assert str(final_path) in harness.status_messages[-1][0]
    assert harness.errors == []
    question.assert_not_called()
    assert fsync.called


def test_atomic_export_before_publish_failure_preserves_target_and_cleans_stage(
        tmp_path,
        ):
    target = tmp_path / "report.pdf"
    sentinel = b"existing export sentinel"
    target.write_bytes(sentinel)
    staged_paths = []

    def writer(staged_filename):
        staged_path = Path(staged_filename)
        staged_paths.append(staged_path)
        staged_path.write_bytes(b"%PDF-new")
        return True

    def reject_publish():
        assert staged_paths[0].is_file()
        assert target.read_bytes() == sentinel
        raise RuntimeError("target became protected before publication")

    destination = prepare_export_destination(
        None,
        str(target),
        replacement_confirmed=True,
    )
    with pytest.raises(RuntimeError, match="became protected"):
        write_export_atomically(
            destination,
            writer,
            before_publish=reject_publish,
        )

    assert target.read_bytes() == sentinel
    assert len(staged_paths) == 1
    assert not staged_paths[0].exists()
    assert set(tmp_path.iterdir()) == {target}


@pytest.mark.parametrize("export_suffix", [".pdf", ".csv", ".png"])
def test_loaded_database_named_for_export_format_is_rejected(
    tmp_path,
    export_suffix,
):
    database_path = tmp_path / f"loaded-input{export_suffix}"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    source_before = _database_artifact_state(database_path)

    with pytest.raises(UnsafeExportDestinationError, match="input database"):
        prepare_export_destination(
            owner,
            str(database_path),
            required_suffix=export_suffix,
            replacement_confirmed=True,
        )

    _assert_database_unchanged(database_path, source_before)


@pytest.mark.parametrize("export_suffix", [".pdf", ".csv", ".png"])
def test_real_qcodes_database_named_for_export_format_is_rejected(
    tmp_path,
    export_suffix,
):
    database_path = tmp_path / f"real-qcodes-input{export_suffix}"
    original_database_path = qcodes.config.core.db_location
    try:
        initialise_or_create_database_at(
            str(database_path),
            journal_mode="DELETE",
        )
        source_before_owner = _database_artifact_state(database_path)
        assert source_before_owner[""] is not None
        assert all(
            source_before_owner[suffix] is None
            for suffix in SQLITE_SIDECAR_SUFFIXES
        )

        owner = _DatabaseOwner(database_path)
        _assert_database_unchanged(database_path, source_before_owner)

        with pytest.raises(UnsafeExportDestinationError, match="input database"):
            prepare_export_destination(
                owner,
                str(database_path),
                required_suffix=export_suffix,
                replacement_confirmed=True,
            )

        _assert_database_unchanged(database_path, source_before_owner)
    finally:
        qcodes.config.core.db_location = original_database_path


@pytest.mark.parametrize("kind", ["csv", "pdf"])
def test_pdf_and_measurement_csv_routes_reject_their_loaded_database(
    tmp_path,
    kind,
):
    database_path = tmp_path / f"loaded-input.{kind}"
    _write_database_artifacts(
        database_path,
        sidecars=SQLITE_SIDECAR_SUFFIXES,
    )
    source_before = _database_artifact_state(database_path)
    if kind == "csv":
        harness = _CsvHarness(database_path)
    else:
        harness = _PdfHarness(database_path)
    harness._dataset_key = DatasetKey(
        str(database_path),
        f"loaded-route-{kind}-guid",
    )

    with (
        patch.object(
            qtw.QFileDialog,
            "getSaveFileName",
            return_value=(str(database_path), ""),
        ),
        patch.object(qtw.QMessageBox, "question") as question,
    ):
        if kind == "csv":
            harness._export_measurement_csv(object(), [object()])
        else:
            assert not harness.save_plot_pdf()

    question.assert_not_called()
    _assert_database_unchanged(database_path, source_before)
    assert harness.errors
    assert not any(
        entry.name.startswith(f".{database_path.name}.")
        for entry in tmp_path.iterdir()
    )


@pytest.mark.parametrize("artifact_suffix", _DATABASE_ARTIFACT_SUFFIXES)
def test_loaded_database_and_present_sidecars_are_protected_exact_targets(
    tmp_path,
    artifact_suffix,
):
    database_path = tmp_path / "loaded-input.db"
    _write_database_artifacts(
        database_path,
        sidecars=SQLITE_SIDECAR_SUFFIXES,
    )
    owner = _DatabaseOwner(database_path)
    source_before = _database_artifact_state(database_path)
    target = Path(f"{database_path}{artifact_suffix}")

    with pytest.raises(UnsafeExportDestinationError, match="input database"):
        prepare_export_destination(
            owner,
            str(target),
            replacement_confirmed=True,
        )

    _assert_database_unchanged(database_path, source_before)


@pytest.mark.parametrize("sidecar_suffix", SQLITE_SIDECAR_SUFFIXES)
def test_absent_sqlite_sidecar_logical_name_is_protected(tmp_path, sidecar_suffix):
    database_path = tmp_path / "loaded-input.db"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    source_before = _database_artifact_state(database_path)
    absent_sidecar = Path(f"{database_path}{sidecar_suffix}")

    assert not absent_sidecar.exists()
    with pytest.raises(UnsafeExportDestinationError, match="SQLite sidecars"):
        prepare_export_destination(owner, str(absent_sidecar))

    _assert_database_unchanged(database_path, source_before)
    assert not absent_sidecar.exists()


@pytest.mark.parametrize("sidecar_suffix", SQLITE_SIDECAR_SUFFIXES)
def test_logical_and_resolved_absent_sidecar_names_of_symlinked_db_are_protected(
    tmp_path,
    sidecar_suffix,
):
    database_path = tmp_path / "resolved-input.db"
    logical_path = tmp_path / "selected-input.db"
    _write_database_artifacts(database_path)
    try:
        logical_path.symlink_to(database_path)
    except OSError as error:
        pytest.skip(f"Symbolic links are unavailable: {error}")

    owner = _DatabaseOwner(logical_path)
    database_before = _database_artifact_state(database_path)
    logical_before = _path_artifact_state(logical_path)

    for base_path in (logical_path, database_path):
        sidecar_path = Path(f"{base_path}{sidecar_suffix}")
        assert not sidecar_path.exists()
        with pytest.raises(UnsafeExportDestinationError, match="SQLite sidecars"):
            prepare_export_destination(owner, str(sidecar_path))
        assert not sidecar_path.exists()

    _assert_database_unchanged(database_path, database_before)
    assert _path_artifact_state(logical_path) == logical_before


@pytest.mark.parametrize(
    "alias_transform",
    [
        pytest.param(str.upper, id="case"),
        pytest.param(
            lambda value: unicodedata.normalize("NFD", value),
            id="unicode-normalization",
        ),
    ],
)
def test_absent_sidecar_case_and_unicode_aliases_are_protected(
    tmp_path,
    alias_transform,
):
    database_path = tmp_path / "loaded-caf\N{LATIN SMALL LETTER E WITH ACUTE}.db"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    source_before = _database_artifact_state(database_path)
    protected_sidecar = Path(f"{database_path}-wal")
    alias_path = protected_sidecar.with_name(
        alias_transform(protected_sidecar.name)
    )

    assert str(alias_path) != str(protected_sidecar)
    assert not protected_sidecar.exists()
    with pytest.raises(UnsafeExportDestinationError, match="SQLite sidecars"):
        prepare_export_destination(owner, str(alias_path))

    _assert_database_unchanged(database_path, source_before)
    assert not protected_sidecar.exists()


def test_symlink_alias_to_loaded_database_is_rejected_without_source_changes(tmp_path):
    database_path = tmp_path / "loaded-input.db"
    alias_path = tmp_path / "export-output.pdf"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    try:
        alias_path.symlink_to(database_path)
    except OSError as error:
        pytest.skip(f"Symbolic links are unavailable: {error}")

    database_before = _database_artifact_state(database_path)
    alias_before = _path_artifact_state(alias_path)

    with pytest.raises(UnsafeExportDestinationError, match="Symbolic-link"):
        prepare_export_destination(
            owner,
            str(alias_path),
            replacement_confirmed=True,
        )

    _assert_database_unchanged(database_path, database_before)
    assert _path_artifact_state(alias_path) == alias_before


def test_hardlink_alias_to_loaded_database_is_rejected_without_source_changes(tmp_path):
    database_path = tmp_path / "loaded-input.db"
    alias_path = tmp_path / "export-output.csv"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    try:
        os.link(database_path, alias_path)
    except OSError as error:
        pytest.skip(f"Hard links are unavailable: {error}")

    database_before = _database_artifact_state(database_path)
    alias_before = _path_artifact_state(alias_path)

    with pytest.raises(UnsafeExportDestinationError, match="Hard-linked"):
        prepare_export_destination(
            owner,
            str(alias_path),
            replacement_confirmed=True,
        )

    _assert_database_unchanged(database_path, database_before)
    assert _path_artifact_state(alias_path) == alias_before


def test_dataset_key_retains_replaced_main_identity(tmp_path):
    database_path = tmp_path / "loaded-input.db"
    alias_path = tmp_path / "retained-input.pdf"
    replacement_path = tmp_path / "replacement.db"
    _write_database_artifacts(database_path)
    try:
        os.link(database_path, alias_path)
    except OSError as error:
        pytest.skip(f"Hard links are unavailable: {error}")

    owner = _DatabaseOwner(database_path)
    replacement_path.write_bytes(b"replacement database instance")
    os.replace(replacement_path, database_path)
    assert alias_path.stat().st_nlink == 1
    database_before = _database_artifact_state(database_path)
    alias_before = _path_artifact_state(alias_path)

    with pytest.raises(UnsafeExportDestinationError, match="input database"):
        prepare_export_destination(
            owner,
            str(alias_path),
            replacement_confirmed=True,
        )

    _assert_database_unchanged(database_path, database_before)
    assert _path_artifact_state(alias_path) == alias_before


@pytest.mark.parametrize("sidecar_suffix", SQLITE_SIDECAR_SUFFIXES)
def test_dataset_key_retains_replaced_sidecar_identity(tmp_path, sidecar_suffix):
    database_path = tmp_path / "loaded-input.db"
    sidecar_path = Path(f"{database_path}{sidecar_suffix}")
    alias_path = tmp_path / f"retained-{sidecar_suffix[1:]}.pdf"
    replacement_path = tmp_path / f"replacement{sidecar_suffix}"
    _write_database_artifacts(database_path, sidecars=(sidecar_suffix,))
    try:
        os.link(sidecar_path, alias_path)
    except OSError as error:
        pytest.skip(f"Hard links are unavailable: {error}")

    owner = _DatabaseOwner(database_path)
    replacement_path.write_bytes(b"replacement SQLite sidecar instance")
    os.replace(replacement_path, sidecar_path)
    assert alias_path.stat().st_nlink == 1
    database_before = _database_artifact_state(database_path)
    alias_before = _path_artifact_state(alias_path)

    with pytest.raises(UnsafeExportDestinationError, match="input database"):
        prepare_export_destination(
            owner,
            str(alias_path),
            replacement_confirmed=True,
        )

    _assert_database_unchanged(database_path, database_before)
    assert _path_artifact_state(alias_path) == alias_before


def test_database_owned_only_by_another_top_level_window_is_protected(tmp_path):
    database_path = tmp_path / "other-window.csv"
    _write_database_artifacts(database_path, sidecars=SQLITE_SIDECAR_SUFFIXES)
    database_before = _database_artifact_state(database_path)
    database_window = qtw.QWidget()
    database_window._dataset_key = DatasetKey(str(database_path), "other-window-guid")
    database_window.show()
    qtw.QApplication.processEvents()

    try:
        with pytest.raises(UnsafeExportDestinationError, match="input database"):
            prepare_export_destination(
                object(),
                str(database_path),
                replacement_confirmed=True,
            )
        _assert_database_unchanged(database_path, database_before)
    finally:
        database_window.close()
        database_window.deleteLater()
        qtw.QApplication.sendPostedEvents(
            None,
            QtCore.QEvent.Type.DeferredDelete,
        )
        qtw.QApplication.processEvents()


@pytest.mark.parametrize("target_existed", [False, True])
def test_target_change_after_approval_is_rejected_before_staging(
    tmp_path,
    target_existed,
):
    database_path = tmp_path / "loaded-input.db"
    target = tmp_path / "report.pdf"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    if target_existed:
        target.write_bytes(b"original approved output")
    destination = prepare_export_destination(
        owner,
        str(target),
        replacement_confirmed=target_existed,
    )
    source_before = _database_artifact_state(database_path)

    if target_existed:
        replacement_path = tmp_path / "concurrent-output.pdf"
        replacement_path.write_bytes(b"concurrently replaced output")
        os.replace(replacement_path, target)
    else:
        target.write_bytes(b"concurrently created output")
    raced_target = _path_artifact_state(target)
    writer_calls = []

    with pytest.raises(UnsafeExportDestinationError, match="changed"):
        write_export_atomically(
            destination,
            lambda _staging_path: writer_calls.append(True),
        )

    assert writer_calls == []
    assert _path_artifact_state(target) == raced_target
    _assert_database_unchanged(database_path, source_before)
    assert not any(path.name.startswith(f".{target.name}.") for path in tmp_path.iterdir())


def test_parent_change_after_approval_is_rejected_before_staging(tmp_path):
    database_path = tmp_path / "loaded-input.db"
    output_parent = tmp_path / "exports"
    moved_parent = tmp_path / "approved-exports"
    output_parent.mkdir()
    target = output_parent / "report.pdf"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    destination = prepare_export_destination(owner, str(target))
    source_before = _database_artifact_state(database_path)

    output_parent.rename(moved_parent)
    output_parent.mkdir()
    target.write_bytes(b"replacement-directory output")
    raced_target = _path_artifact_state(target)
    writer_calls = []

    with pytest.raises(UnsafeExportDestinationError, match="folder changed"):
        write_export_atomically(
            destination,
            lambda _staging_path: writer_calls.append(True),
        )

    assert writer_calls == []
    assert _path_artifact_state(target) == raced_target
    assert list(moved_parent.iterdir()) == []
    _assert_database_unchanged(database_path, source_before)


@pytest.mark.parametrize("target_existed", [False, True])
def test_target_appearance_or_replacement_during_writer_aborts_publication(
    tmp_path,
    target_existed,
):
    database_path = tmp_path / "loaded-input.db"
    target = tmp_path / "report.pdf"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    if target_existed:
        target.write_bytes(b"original approved output")
    destination = prepare_export_destination(
        owner,
        str(target),
        replacement_confirmed=target_existed,
    )
    source_before = _database_artifact_state(database_path)
    staged_paths = []
    raced_target = []

    def writer(staging_filename):
        staging_path = Path(staging_filename)
        staged_paths.append(staging_path)
        staging_path.write_bytes(b"staged export")
        if target_existed:
            replacement_path = tmp_path / "concurrent-output.pdf"
            replacement_path.write_bytes(b"concurrently replaced output")
            os.replace(replacement_path, target)
        else:
            target.write_bytes(b"concurrently created output")
        raced_target.append(_path_artifact_state(target))
        return True

    with pytest.raises(UnsafeExportDestinationError, match="changed"):
        write_export_atomically(destination, writer)

    assert len(staged_paths) == 1
    assert not staged_paths[0].exists()
    assert _path_artifact_state(target) == raced_target[0]
    _assert_database_unchanged(database_path, source_before)


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows prevents renaming a directory containing the open stage",
)
def test_parent_rename_during_writer_preserves_new_entries_and_cleans_owned_stage(
    tmp_path,
):
    database_path = tmp_path / "loaded-input.db"
    output_parent = tmp_path / "exports"
    moved_parent = tmp_path / "approved-exports"
    output_parent.mkdir()
    target = output_parent / "report.pdf"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    destination = prepare_export_destination(owner, str(target))
    source_before = _database_artifact_state(database_path)
    staged_names = []
    decoy_state = []
    target_state = []

    def writer(staging_filename):
        staging_path = Path(staging_filename)
        staging_path.write_bytes(b"owned staged export")
        staged_names.append(staging_path.name)
        output_parent.rename(moved_parent)
        output_parent.mkdir()
        target.write_bytes(b"new-parent target")
        decoy_path = output_parent / staging_path.name
        decoy_path.write_bytes(b"new-parent decoy must survive")
        target_state.append(_path_artifact_state(target))
        decoy_state.append(_path_artifact_state(decoy_path))
        return True

    with pytest.raises(UnsafeExportDestinationError, match="folder changed"):
        write_export_atomically(destination, writer)

    assert len(staged_names) == 1
    assert not (moved_parent / staged_names[0]).exists()
    assert _path_artifact_state(target) == target_state[0]
    assert _path_artifact_state(output_parent / staged_names[0]) == decoy_state[0]
    _assert_database_unchanged(database_path, source_before)


@pytest.mark.parametrize("failure_kind", ["false", "exception"])
def test_writer_failure_preserves_confirmed_target_and_source(
    tmp_path,
    failure_kind,
):
    database_path = tmp_path / "loaded-input.db"
    target = tmp_path / "report.csv"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    target.write_bytes(b"original export")
    destination = prepare_export_destination(
        owner,
        str(target),
        replacement_confirmed=True,
    )
    source_before = _database_artifact_state(database_path)
    target_before = _path_artifact_state(target)
    staged_paths = []

    def writer(staging_filename):
        staging_path = Path(staging_filename)
        staged_paths.append(staging_path)
        staging_path.write_bytes(b"incomplete export")
        if failure_kind == "exception":
            raise RuntimeError("writer failed")
        return False

    if failure_kind == "exception":
        with pytest.raises(RuntimeError, match="writer failed"):
            write_export_atomically(destination, writer)
    else:
        assert not write_export_atomically(destination, writer)

    assert _path_artifact_state(target) == target_before
    assert len(staged_paths) == 1
    assert not staged_paths[0].exists()
    _assert_database_unchanged(database_path, source_before)


def test_unconfirmed_existing_target_is_rejected_without_staging(tmp_path):
    database_path = tmp_path / "loaded-input.db"
    target = tmp_path / "report.pdf"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    target.write_bytes(b"existing output")
    approved = prepare_export_destination(
        owner,
        str(target),
        replacement_confirmed=True,
    )
    unconfirmed = replace(approved, replacement_confirmed=False)
    source_before = _database_artifact_state(database_path)
    target_before = _path_artifact_state(target)
    writer_calls = []

    with pytest.raises(UnsafeExportDestinationError, match="not confirmed"):
        write_export_atomically(
            unconfirmed,
            lambda _staging_path: writer_calls.append(True),
        )

    assert writer_calls == []
    assert _path_artifact_state(target) == target_before
    _assert_database_unchanged(database_path, source_before)
    assert not any(path.name.startswith(f".{target.name}.") for path in tmp_path.iterdir())


def test_normal_new_export_uses_same_directory_stage_and_preserves_source(tmp_path):
    database_path = tmp_path / "loaded-input.db"
    target = tmp_path / "report.png"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    source_before = _database_artifact_state(database_path)
    destination = prepare_export_destination(owner, str(target))
    staged_paths = []

    def writer(staging_filename):
        staging_path = Path(staging_filename)
        staged_paths.append(staging_path)
        assert staging_path.parent == target.parent
        assert staging_path.suffix == target.suffix
        assert not target.exists()
        staging_path.write_bytes(b"new image export")
        return True

    assert write_export_atomically(destination, writer)

    assert target.read_bytes() == b"new image export"
    assert len(staged_paths) == 1
    assert not staged_paths[0].exists()
    _assert_database_unchanged(database_path, source_before)
    assert set(tmp_path.iterdir()) == {database_path, target}


def test_explicitly_confirmed_replacement_is_atomic_and_preserves_source(tmp_path):
    database_path = tmp_path / "loaded-input.db"
    target = tmp_path / "report.pdf"
    _write_database_artifacts(database_path)
    owner = _DatabaseOwner(database_path)
    target.write_bytes(b"old export")
    target_before = _path_artifact_state(target)
    source_before = _database_artifact_state(database_path)
    destination = prepare_export_destination(
        owner,
        str(target),
        replacement_confirmed=True,
    )
    staged_paths = []

    def writer(staging_filename):
        staging_path = Path(staging_filename)
        staged_paths.append(staging_path)
        assert staging_path.parent == target.parent
        assert target.read_bytes() == b"old export"
        staging_path.write_bytes(b"new export")
        return True

    assert destination.target_existed
    assert destination.replacement_confirmed
    assert write_export_atomically(destination, writer)

    assert target.read_bytes() == b"new export"
    assert _path_artifact_state(target)["lstat_identity"] != target_before["lstat_identity"]
    assert len(staged_paths) == 1
    assert not staged_paths[0].exists()
    _assert_database_unchanged(database_path, source_before)
    assert set(tmp_path.iterdir()) == {database_path, target}


def test_destination_newly_protected_after_approval_is_rejected_before_staging(
    tmp_path,
):
    database_path = tmp_path / "loaded-input.db"
    target = tmp_path / "approved-output.pdf"
    _write_database_artifacts(
        database_path,
        sidecars=SQLITE_SIDECAR_SUFFIXES,
    )
    target.write_bytes(b"existing approved output")
    owner = _DatabaseOwner(database_path)
    destination = prepare_export_destination(
        owner,
        str(target),
        replacement_confirmed=True,
    )
    source_before = _database_artifact_state(database_path)
    target_before = _database_artifact_state(target)
    approved_signature = destination.target_signature

    # Acquiring this otherwise unchanged file as an input database after the
    # dialog must revoke the earlier replacement approval.
    owner._selected_dataset_key = DatasetKey(str(target), "newly-loaded-guid")
    assert destination.target_signature == approved_signature
    assert _path_artifact_state(target)["lstat_identity"] == (
        approved_signature[0],
        approved_signature[1],
    )
    writer_calls = []

    with pytest.raises(UnsafeExportDestinationError, match="input database"):
        write_export_atomically(
            destination,
            lambda _staging_path: writer_calls.append(True),
        )

    assert writer_calls == []
    _assert_database_unchanged(database_path, source_before)
    _assert_database_unchanged(target, target_before)
    assert not any(path.name.startswith(f".{target.name}.") for path in tmp_path.iterdir())


def test_destination_newly_protected_during_writer_aborts_publication(tmp_path):
    database_path = tmp_path / "loaded-input.db"
    target = tmp_path / "approved-output.csv"
    _write_database_artifacts(
        database_path,
        sidecars=SQLITE_SIDECAR_SUFFIXES,
    )
    target.write_bytes(b"existing approved output")
    owner = _DatabaseOwner(database_path)
    destination = prepare_export_destination(
        owner,
        str(target),
        replacement_confirmed=True,
    )
    source_before = _database_artifact_state(database_path)
    target_before = _database_artifact_state(target)
    approved_signature = destination.target_signature
    staged_paths = []

    def writer(staging_filename):
        staging_path = Path(staging_filename)
        staged_paths.append(staging_path)
        staging_path.write_bytes(b"staged export")
        owner._selected_dataset_key = DatasetKey(
            str(target),
            "newly-loaded-during-writer-guid",
        )
        assert destination.target_signature == approved_signature
        return True

    with pytest.raises(UnsafeExportDestinationError, match="input database"):
        write_export_atomically(destination, writer)

    assert len(staged_paths) == 1
    assert not staged_paths[0].exists()
    _assert_database_unchanged(database_path, source_before)
    _assert_database_unchanged(target, target_before)


def test_non_regular_export_destination_is_rejected_without_source_changes(tmp_path):
    database_path = tmp_path / "loaded-input.db"
    target = tmp_path / "directory-output.png"
    _write_database_artifacts(
        database_path,
        sidecars=SQLITE_SIDECAR_SUFFIXES,
    )
    target.mkdir()
    owner = _DatabaseOwner(database_path)
    source_before = _database_artifact_state(database_path)
    target_before = _path_artifact_state(target)

    with pytest.raises(UnsafeExportDestinationError, match="not a regular file"):
        prepare_export_destination(
            owner,
            str(target),
            replacement_confirmed=True,
        )

    _assert_database_unchanged(database_path, source_before)
    assert _path_artifact_state(target) == target_before
    assert list(target.iterdir()) == []


def test_new_target_appearance_at_no_clobber_publication_edge_is_not_clobbered(
    tmp_path,
):
    database_path = tmp_path / "loaded-input.db"
    target = tmp_path / "new-output.pdf"
    _write_database_artifacts(
        database_path,
        sidecars=SQLITE_SIDECAR_SUFFIXES,
    )
    owner = _DatabaseOwner(database_path)
    destination = prepare_export_destination(owner, str(target))
    source_before = _database_artifact_state(database_path)
    staged_paths = []
    raced_target_state = []
    publication_primitive = "os.rename" if os.name == "nt" else "os.link"
    real_publish = os.rename if os.name == "nt" else os.link

    def writer(staging_filename):
        staging_path = Path(staging_filename)
        staged_paths.append(staging_path)
        staging_path.write_bytes(b"staged export")
        return True

    def publish_after_concurrent_creation(source, destination_name, *args, **kwargs):
        target.write_bytes(b"concurrently created at publication edge")
        raced_target_state.append(_database_artifact_state(target))
        return real_publish(source, destination_name, *args, **kwargs)

    with (
        patch(
            f"qplot.windows._export_paths.{publication_primitive}",
            side_effect=publish_after_concurrent_creation,
        ) as publish,
        pytest.raises(UnsafeExportDestinationError, match="appeared before publication"),
    ):
        write_export_atomically(destination, writer)

    publish.assert_called_once()
    assert len(raced_target_state) == 1
    _assert_database_unchanged(target, raced_target_state[0])
    assert len(staged_paths) == 1
    assert not staged_paths[0].exists()
    _assert_database_unchanged(database_path, source_before)


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows prevents replacing an open private staging file",
)
def test_stage_replaced_by_protected_hardlink_is_not_unlinked(tmp_path):
    database_path = tmp_path / "loaded-input.db"
    protected_alias = tmp_path / "protected-database-alias"
    target = tmp_path / "new-output.png"
    _write_database_artifacts(
        database_path,
        sidecars=SQLITE_SIDECAR_SUFFIXES,
    )
    try:
        os.link(database_path, protected_alias)
    except OSError as error:
        pytest.skip(f"Hard links are unavailable: {error}")

    owner = _DatabaseOwner(database_path)
    destination = prepare_export_destination(owner, str(target))
    source_before = _database_artifact_state(database_path)
    alias_before = _path_artifact_state(protected_alias)
    staged_paths = []
    validate_calls = 0

    def writer(staging_filename):
        staging_path = Path(staging_filename)
        staged_paths.append(staging_path)
        staging_path.write_bytes(b"staged export")
        return True

    real_validate = type(destination).validate

    def validate_then_replace_stage(current_destination, *, parent_fd=None):
        nonlocal validate_calls
        real_validate(current_destination, parent_fd=parent_fd)
        validate_calls += 1
        if validate_calls == 2:
            staging_path = staged_paths[0]
            staging_path.unlink()
            protected_alias.rename(staging_path)
            assert _path_artifact_state(staging_path) == alias_before

    with (
        patch.object(type(destination), "validate", validate_then_replace_stage),
        pytest.raises(
            UnsafeExportDestinationError,
            match="private export staging file changed unexpectedly",
        ),
    ):
        write_export_atomically(destination, writer)

    assert validate_calls == 2
    assert len(staged_paths) == 1
    assert not protected_alias.exists()
    assert _path_artifact_state(staged_paths[0]) == alias_before
    assert not target.exists()
    _assert_database_unchanged(database_path, source_before)
