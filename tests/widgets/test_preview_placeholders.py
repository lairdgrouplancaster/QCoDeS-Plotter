from PyQt6 import QtWidgets as qtw

from qplot.datahandling.database import _bounded_run_publication
from qplot.windows._widgets.preview import PreviewTab, preview_placeholder_dimensions
from qplot.windows._widgets.run_list_items import RunPreviewCell


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
