from PyQt6 import QtCore, QtGui, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.database import _bounded_run_publication
from qplot.windows._widgets.preview import (
    PreviewImageLabel,
    PreviewTab,
    preview_placeholder_dimensions,
)
from qplot.windows._widgets.run_list_items import RunPreviewCell


def test_placeholder_selection_and_actions_work_before_thumbnail_arrives():
    cell = RunPreviewCell("guid", 2)
    cell.update_placeholder_metadata({
        "measure_parameters": ["a", "b"], "preview_dimensions": [2, 2],
    })
    cell.show()
    plots = []
    exports = []
    cell.plotRequested.connect(lambda *args: plots.append(args))
    cell.exportRequested.connect(lambda *args: exports.append(args))
    try:
        labels = cell.findChildren(PreviewImageLabel)
        QtTest.QTest.mouseClick(labels[0], QtCore.Qt.MouseButton.LeftButton)
        assert [label._selected for label in labels] == [True, False]
        QtTest.QTest.mouseClick(labels[1], QtCore.Qt.MouseButton.LeftButton)
        assert [label._selected for label in labels] == [False, True]
        QtTest.QTest.mouseDClick(labels[1], QtCore.Qt.MouseButton.LeftButton)
        QtTest.QTest.keyClick(labels[1], QtCore.Qt.Key.Key_Return)
        labels[1].exportRequested.emit(labels[1].parameter)
        assert plots == [("guid", "b"), ("guid", "b")]
        assert exports == [("guid", "b")]

        cell.set_generating(True)
        assert [label._selected for label in cell.findChildren(PreviewImageLabel)] == [False, True]
        image = QtGui.QImage(22, 22, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtCore.Qt.GlobalColor.white)
        cell.show_previews([
            {"parameter": parameter, "axes": ["x", "y"], "image": image}
            for parameter in ("a", "b")
        ])
        assert [label._selected for label in cell.findChildren(PreviewImageLabel)] == [False, True]
    finally:
        cell.close()
        cell.deleteLater()


def test_placeholder_parameter_updates_even_when_dimensions_are_unchanged():
    cell = RunPreviewCell("guid", 1)
    try:
        for parameter in ("a", "b"):
            cell.update_placeholder_metadata({"measure_parameters": [parameter]})
            label = cell.findChildren(PreviewImageLabel)[0]
            assert label.parameter == parameter
            assert parameter in label.accessibleName()
    finally:
        cell.deleteLater()


def test_initial_dimensions_survive_bounded_worker_publication():
    metadata = _bounded_run_publication({
        "measure_parameters": ["a", "b"], "preview_dimensions": [1, 2],
    })
    cell = RunPreviewCell("guid", 2)
    cell.update_placeholder_metadata(metadata)
    labels = cell.findChildren(qtw.QLabel, "measurementPreviewPlaceholder")
    assert [label.text() for label in labels] == ["1D", "2D"]
    cell.deleteLater()


def test_dimensions_are_per_parameter_and_not_guessed():
    metadata = {
        "measure_parameters": ["a", "b", "c"],
        "sweep_parameters": ["x", "y"],
        "run_description": {"interdependencies_": {
            "dependencies": {"a": ["x"], "b": ["x", "y"]},
        }},
    }
    assert preview_placeholder_dimensions(metadata) == [1, 2, None]
    del metadata["run_description"]
    assert preview_placeholder_dimensions(metadata) == [None, None, None]


def test_thumbnail_placeholder_dimensions_update_without_changing_count():
    cell = RunPreviewCell("guid", 2)
    cell.set_generating(True)
    cell.update_placeholder_metadata({
        "measure_parameters": ["a", "b"], "preview_dimensions": {"a": 1, "b": 2},
    })
    labels = cell.findChildren(qtw.QLabel, "measurementPreviewPlaceholder")
    assert [label.text() for label in labels] == ["1D", "2D"]
    assert all(label.width() == label.height() == 22 for label in labels)
    cell.deleteLater()


def test_preview_pending_squares_follow_selection_and_disappear_on_result():
    tab = PreviewTab()
    tab.set_trusted_derived_runs({
        1: {"guid": "one", "measure_parameters": ["a", "b"],
            "preview_dimensions": {"a": 1, "b": 2}},
        2: {"guid": "two", "measure_parameters": ["c"]},
    })

    def placeholders():
        return [tab.content_layout.itemAt(i).widget()
                for i in range(tab.content_layout.count())
                if tab.content_layout.itemAt(i).widget() is not None
                and tab.content_layout.itemAt(i).widget().objectName() == "previewPlaceholder"]

    tab.set_current_guid("one")
    assert [label.text() for label in placeholders()] == ["1D", "2D"]
    assert all(label.width() == label.height() == tab.preview_size for label in placeholders())
    tab.set_current_guid("two")
    assert [label.text() for label in placeholders()] == [""]
    tab.publish_trusted_previews("two", [])
    assert placeholders() == []
    tab.deleteLater()
