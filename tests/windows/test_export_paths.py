import builtins
from pathlib import Path
from unittest.mock import patch

import pytest
from PyQt6 import QtWidgets as qtw

from qplot.windows._export_paths import write_export_atomically
from qplot.windows._plot_actions import PlotActionsMixin
from qplot.windows._plot_export import PlotExportMixin


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
        patch("builtins.open", wraps=builtins.open) as open_file,
    ):
        harness = _invoke_export(kind, selected_path)

    assert final_path.is_file()
    assert not selected_path.exists()
    assert set(tmp_path.iterdir()) == {final_path}
    assert str(final_path) in harness.status_messages[-1][0]
    assert harness.errors == []
    question.assert_not_called()
    assert any(call.args[1] == "r+b" for call in open_file.call_args_list)


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

    with pytest.raises(RuntimeError, match="became protected"):
        write_export_atomically(
            str(target),
            writer,
            before_publish=reject_publish,
        )

    assert target.read_bytes() == sentinel
    assert len(staged_paths) == 1
    assert not staged_paths[0].exists()
    assert set(tmp_path.iterdir()) == {target}
