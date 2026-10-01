from PyQt6 import QtCore, QtGui, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.datahandling.database import _bounded_run_publication
from qplot.windows._dragdrop import make_run_preview_mime, run_preview_payload_from_mime
from qplot.windows._widgets.preview import (
    DraggablePreviewImageLabel,
    PreviewImageLabel,
    PreviewTab,
    preview_placeholder_dimensions,
)
from qplot.windows._widgets.run_list_items import RunPreviewCell


def _white_preview(parameter, *, axes=("x",)):
    image = QtGui.QImage(22, 22, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtCore.Qt.GlobalColor.white)
    return {
        "parameter": parameter,
        "axes": list(axes),
        "title": f"{parameter} preview",
        "image": image,
    }


def _preview_widgets(cell):
    return [
        cell.content_layout.itemAt(index).widget()
        for index in range(cell.content_layout.count())
        if cell.content_layout.itemAt(index).widget() is not None
    ]


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


def test_partial_previews_keep_missing_first_and_middle_parameter_slots():
    cell = RunPreviewCell("guid", 3)
    cell.update_placeholder_metadata({
        "measure_parameters": ["first", "middle", "signal"],
        "preview_dimensions": [1, 2, 1],
    })
    plots = []
    exports = []
    cell.plotRequested.connect(lambda *args: plots.append(args))
    cell.exportRequested.connect(lambda *args: exports.append(args))
    try:
        cell.show_previews([_white_preview("signal")])
        widgets = _preview_widgets(cell)
        assert [widget.parameter for widget in widgets] == [
            "first", "middle", "signal",
        ]
        assert [widget.objectName() for widget in widgets] == [
            "measurementPreviewPlaceholder",
            "measurementPreviewPlaceholder",
            "measurementPreviewImage",
        ]
        assert [widget.text() for widget in widgets[:2]] == ["1D", "2D"]
        widgets[0].plotRequested.emit(widgets[0].parameter)
        widgets[0].exportRequested.emit(widgets[0].parameter)
        assert plots == [("guid", "first")]
        assert exports == [("guid", "first")]

        cell.show_previews([
            _white_preview("signal"),
            _white_preview("first"),
        ])
        widgets = _preview_widgets(cell)
        assert [widget.parameter for widget in widgets] == [
            "first", "middle", "signal",
        ]
        assert [widget.objectName() for widget in widgets] == [
            "measurementPreviewImage",
            "measurementPreviewPlaceholder",
            "measurementPreviewImage",
        ]
        assert widgets[1].text() == "2D"
    finally:
        cell.deleteLater()


def test_out_of_order_incremental_previews_preserve_selection_and_targets():
    cell = RunPreviewCell("run-guid", 3)
    cell.update_placeholder_metadata({
        "measure_parameters": ["first", "middle", "signal"],
        "preview_dimensions": [1, 1, 1],
    })
    plots = []
    exports = []
    cell.plotRequested.connect(lambda *args: plots.append(args))
    cell.exportRequested.connect(lambda *args: exports.append(args))
    try:
        cell.show_previews([_white_preview("signal")])
        first, middle, signal = _preview_widgets(cell)
        signal.select_preview()

        cell.show_previews([
            _white_preview("signal"),
            _white_preview("first"),
        ])
        first, middle, signal = _preview_widgets(cell)
        assert [widget.parameter for widget in (first, middle, signal)] == [
            "first", "middle", "signal",
        ]
        assert [widget._selected for widget in (first, middle, signal)] == [
            False, False, True,
        ]
        assert isinstance(first, DraggablePreviewImageLabel)
        assert isinstance(middle, PreviewImageLabel)
        assert isinstance(signal, DraggablePreviewImageLabel)
        assert first.guid == signal.guid == "run-guid"
        assert first.axes == signal.axes == ["x"]
        assert run_preview_payload_from_mime(
            make_run_preview_mime(signal.guid, signal.parameter, signal.axes)
        ) == {
            "guid": "run-guid",
            "parameter": "signal",
            "axes": ["x"],
        }

        first.plotRequested.emit(first.parameter)
        middle.exportRequested.emit(middle.parameter)
        signal.plotRequested.emit(signal.parameter)
        signal.exportRequested.emit(signal.parameter)
        assert plots == [
            ("run-guid", "first"),
            ("run-guid", "signal"),
        ]
        assert exports == [
            ("run-guid", "middle"),
            ("run-guid", "signal"),
        ]

        cell.show_previews([_white_preview("first")])
        first, middle, signal = _preview_widgets(cell)
        assert [widget.parameter for widget in (first, middle, signal)] == [
            "first", "middle", "signal",
        ]
        assert [widget._selected for widget in (first, middle, signal)] == [
            False, False, True,
        ]
        assert signal.objectName() == "measurementPreviewPlaceholder"
    finally:
        cell.deleteLater()


def test_unsupported_preview_keeps_its_declared_position():
    cell = RunPreviewCell("guid", 3)
    cell.update_placeholder_metadata({
        "measure_parameters": ["first", "unsupported", "signal"],
    })
    try:
        cell.show_previews([
            _white_preview("signal"),
            {
                "parameter": "unsupported",
                "axes": ["x", "y", "z"],
                "dimension_count": 3,
                "title": "unsupported has 3 independent axes",
                "unsupported": True,
            },
        ])
        widgets = _preview_widgets(cell)
        assert [widget.objectName() for widget in widgets] == [
            "measurementPreviewPlaceholder",
            "measurementPreviewUnsupported",
            "measurementPreviewImage",
        ]
        assert widgets[0].parameter == "first"
        assert widgets[1].text() == "3D"
        assert widgets[2].parameter == "signal"
    finally:
        cell.deleteLater()


def test_preview_selection_outline_covers_entire_widget_perimeter():
    label = PreviewImageLabel("signal")
    label.setFixedSize(22, 22)
    image = QtGui.QPixmap(label.size())
    image.fill(QtCore.Qt.GlobalColor.white)
    label.setPixmap(image)
    label.set_selected(True)
    label.show()
    qtw.QApplication.processEvents()
    try:
        rendered = label.grab().toImage()
        highlight = label.palette().color(QtGui.QPalette.ColorRole.Highlight)
        edge_points = [
            QtCore.QPoint(x, y)
            for x in range(label.width())
            for y in range(label.height())
            if x in (0, label.width() - 1) or y in (0, label.height() - 1)
        ]
        assert all(rendered.pixelColor(point) == highlight for point in edge_points)
    finally:
        label.close()
        label.deleteLater()


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
