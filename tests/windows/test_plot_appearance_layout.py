"""Responsive appearance panes and independent dataset/whole-plot scope."""

from copy import deepcopy

import numpy as np
import pytest
from PyQt6 import QtCore, QtGui, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows import _appearance_layout
from qplot.windows._drawing_controls import drawing_controls_row
from tests.support.windows.mixed_plot_drops import _added, _drop, _wait_for, mixed_plot

__all__ = ["mixed_plot"]


@pytest.mark.parametrize("mixed_plot", ["line"], indirect=True)
def test_datasets_divider_can_be_dragged_beside_the_line(mixed_plot, qapplication):
    _main, plot, _runs = mixed_plot
    dialog = plot.open_plot_appearance_dialog()
    splitter = dialog.appearance_splitter
    for width, orientation, cursor, drag in (
        (1200, QtCore.Qt.Orientation.Horizontal, QtCore.Qt.CursorShape.SplitHCursor, QtCore.QPoint(24, 0)),
        (420, QtCore.Qt.Orientation.Vertical, QtCore.Qt.CursorShape.SplitVCursor, QtCore.QPoint(0, 24)),
    ):
        dialog.resize(width, 700)
        qapplication.processEvents()
        assert splitter.orientation() == orientation
        handle = splitter.handle(1)
        assert handle.cursor().shape() == cursor
        assert min(handle.width(), handle.height()) >= 9
        start = QtCore.QPoint(1, 1)
        assert splitter.childAt(handle.mapTo(splitter, start)) is handle
        before = splitter.sizes()[0]
        QtTest.QTest.mousePress(handle, QtCore.Qt.MouseButton.LeftButton, pos=start)
        QtTest.QTest.mouseMove(handle, start + drag)
        QtTest.QTest.mouseRelease(handle, QtCore.Qt.MouseButton.LeftButton, pos=start)
        qapplication.processEvents()
        assert splitter.sizes()[0] > before
    dialog.reject()


def test_properties_scrollbar_releases_width_when_drawing_row_fits(qapplication):
    buttons = [qtw.QPushButton(text) for text in ("Colour", "Width", "Style")]
    row = drawing_controls_row(*buttons)
    stack = _appearance_layout.AppearanceControlsStack()
    stack.addWidget(row)
    tall = qtw.QLabel("One\nTwo\nThree\nFour")
    stack.addWidget(tall)
    scroll = _appearance_layout.AppearanceControlsScroll(stack)
    fitted = row.sizeHint()
    assert tall.sizeHint().height() > fitted.height()

    def check_scrollbar(visible):
        qapplication.processEvents()
        assert scroll.verticalScrollBar().isVisible() == visible
        assert (scroll.verticalScrollBar().maximum() > 0) == visible
        if not visible:
            assert buttons[0].y() == buttons[-1].y()

    try:
        scroll.resize(fitted)
        scroll.show()
        check_scrollbar(False)
        scroll.resize(fitted.width(), fitted.height() - 1)
        check_scrollbar(True)
        scroll.resize(fitted)
        check_scrollbar(False)
        stack.setCurrentWidget(tall)
        check_scrollbar(True)
        stack.setCurrentWidget(row)
        check_scrollbar(False)
    finally:
        scroll.close()


def test_drawing_rows_share_the_widest_controls_size_hint(qapplication):
    short = qtw.QPushButton("Width")
    long = qtw.QPushButton("A much wider marker size")
    first = drawing_controls_row(short, width_controls=(long,))
    second = drawing_controls_row(long, width_controls=(short,))
    try:
        for row in (first, second):
            row.resize(row.sizeHint())
            row.show()
        qapplication.processEvents()
        assert short.width() == long.width() == long.sizeHint().width()
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("screen_size", [(900, 700), (560, 500)])
@pytest.mark.parametrize("font_size", [9, 14])
def test_appearance_fits_screen_and_keeps_actions_accessible(
    mixed_plot, monkeypatch, qapplication, screen_size, font_size,
):
    _main, target, _runs = mixed_plot
    long_name = "measurement_with_a_name_that_is_longer_than_the_available_column"
    for method in ("_trace_measurement_name", "_heatmap_measurement_name"):
        monkeypatch.setattr(target, method, lambda *_args: long_name)
    target.setStyleSheet(target.styleSheet() + f"QWidget {{ font-size: {font_size}pt; }}")
    screen = QtCore.QRect(30, 40, *screen_size)
    monkeypatch.setattr(_appearance_layout, "available_appearance_geometry", lambda _dialog: screen)
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    qapplication.processEvents()
    assert screen.contains(dialog.frameGeometry()), (screen, dialog.frameGeometry(), dialog.minimumSizeHint())
    assert dialog.table.horizontalScrollBar().maximum() == 0
    measurement = dialog.table.item(0, dialog._COL_MEASUREMENT)
    assert long_name in measurement.toolTip()
    assert len({button.width() for button in dialog.appearance_buttons.buttons()}) == 1
    for button in dialog.appearance_buttons.buttons():
        rect = QtCore.QRect(button.mapTo(dialog, QtCore.QPoint()), button.size())
        assert dialog.rect().contains(rect)
    if screen_size[0] == 560:
        assert dialog.appearance_splitter.orientation() == QtCore.Qt.Orientation.Vertical
    else:
        dialog.resize(1200, dialog.height())
        qapplication.processEvents()
        assert dialog.appearance_splitter.orientation() == QtCore.Qt.Orientation.Horizontal
    assert not dialog.controls_scroll.horizontalScrollBar().isVisible()
    assert dialog.controls_stack.width() <= dialog.controls_scroll.viewport().width()
    assert dialog.table.horizontalScrollBar().maximum() == 0
    selected = QtCore.QRect(dialog.selected_group.mapTo(dialog, QtCore.QPoint()), dialog.selected_group.size())
    whole = QtCore.QRect(dialog.whole_plot_group.mapTo(dialog, QtCore.QPoint()), dialog.whole_plot_group.size())
    datasets = dialog.appearance_splitter.widget(0)
    dataset_rect = QtCore.QRect(datasets.mapTo(dialog, QtCore.QPoint()), datasets.size())
    assert selected.left() == whole.left()
    assert selected.right() == whole.right()
    assert selected.bottom() < whole.top()
    assert dialog.rect().contains(whole)
    if dialog.appearance_splitter.orientation() == QtCore.Qt.Orientation.Horizontal:
        assert dataset_rect.bottom() >= whole.bottom()
        assert dataset_rect.right() < selected.left()
    else:
        assert dataset_rect.bottom() < selected.top()
    dialog.reject()


@pytest.mark.parametrize("mixed_plot", ["line"], indirect=True)
@pytest.mark.parametrize("first_page", ["Datasets", "Axis scales"])
def test_default_size_fits_two_named_traces_and_both_axis_modes(
    mixed_plot, monkeypatch, qapplication, first_page,
):
    _, plot, runs = mixed_plot
    _drop(plot, runs["other_line"])
    overlay = _added(plot, runs["other_line"])
    names = ("uhfli_demods4_time_trace_5_x", "uhfli_demods4_time_trace_5_y")
    monkeypatch.setattr(plot, "_trace_measurement_name", lambda _key, line: names[1] if line is overlay else names[0])
    desktop = QtCore.QRect(0, 0, 1600, 1000)
    monkeypatch.setattr(_appearance_layout, "available_appearance_geometry", lambda _dialog: desktop)
    if first_page == "Axis scales":
        plot.open_axis_scale_dialog("x")
    else:
        plot.open_plot_appearance_dialog()
    dialog = plot._plot_appearance_dialog
    qapplication.processEvents()
    assert dialog.pages.tabText(dialog.pages.currentIndex()) == first_page
    opening_height = dialog.height()
    if first_page == "Axis scales":
        assert dialog.axes_page.verticalScrollBar().maximum() == 0

    dialog.show_page("Datasets")
    qapplication.processEvents()
    assert desktop.contains(dialog.frameGeometry())
    assert dialog.appearance_splitter.orientation() == QtCore.Qt.Orientation.Horizontal
    table = dialog.table
    column = dialog._COL_MEASUREMENT
    assert {table.item(row, column).text() for row in range(table.rowCount())} == set(names)
    assert table.columnWidth(column) >= table.sizeHintForColumn(column)
    for bar in (table.horizontalScrollBar(), table.verticalScrollBar(), dialog.controls_scroll.verticalScrollBar()):
        assert bar.maximum() == 0
    properties = dialog.appearance_splitter.widget(1)
    bottom = dialog.whole_plot_group.mapTo(properties, dialog.whole_plot_group.rect().bottomLeft()).y()
    assert 0 <= properties.rect().bottom() - bottom <= 2 * dialog.fontMetrics().height()
    datasets_size = dialog.size()

    plot.open_axis_scale_dialog("x")
    qapplication.processEvents()
    assert dialog.pages.currentWidget() is dialog.axes_page
    assert dialog.height() < datasets_size.height()
    if first_page == "Axis scales":
        assert dialog.height() == opening_height
    controls = plot._axis_scale_controls["x"]
    for mode, field in ((controls.autoRadio, controls.minText), (controls.manualRadio, controls.maxText)):
        mode.click()
        qapplication.processEvents()
        assert field.isVisible()
        assert dialog.axes_page.verticalScrollBar().maximum() == 0
        assert dialog.axes_page.horizontalScrollBar().maximum() == 0
        viewport = dialog.axes_page.viewport()
        rect = QtCore.QRect(controls.linkCombo.mapTo(viewport, QtCore.QPoint()), controls.linkCombo.size())
        assert viewport.rect().contains(rect)
        assert viewport.rect().bottom() - rect.bottom() <= 3 * dialog.fontMetrics().height()
    dialog.show_page("Datasets")
    qapplication.processEvents()
    assert dialog.size() == datasets_size
    dialog.reject()


def _assert_drawing_row_fits(viewport, toggle, *controls):
    option = qtw.QStyleOptionButton()
    toggle.initStyleOption(option)
    contents = toggle.style().subElementRect(qtw.QStyle.SubElement.SE_CheckBoxContents, option, toggle)
    assert contents.width() >= toggle.fontMetrics().horizontalAdvance(toggle.text())
    previous = QtCore.QRect(toggle.mapTo(viewport, QtCore.QPoint()), toggle.size())
    assert viewport.rect().contains(previous)
    for control in controls:
        rect = QtCore.QRect(control.mapTo(viewport, QtCore.QPoint()), control.size())
        assert viewport.rect().contains(rect)
        assert rect.width() >= control.sizeHint().width()
        assert rect.left() > previous.right()
        assert previous.top() <= rect.center().y() <= previous.bottom()
        previous = rect


@pytest.mark.parametrize("mixed_plot", ["line"], indirect=True)
@pytest.mark.parametrize("font_size", [9, 14])
@pytest.mark.parametrize("first_page", ["Datasets", "Axis scales"])
def test_opening_width_fits_drawing_controls_and_measurement_names(
    mixed_plot, monkeypatch, qapplication, font_size, first_page,
):
    main, plot, runs = mixed_plot
    desktop = QtCore.QRect(0, 0, 1920, 1080)
    monkeypatch.setattr(_appearance_layout, "available_appearance_geometry", lambda _dialog: desktop)
    main.openPlot(guid=runs["other_line"].guid)
    _wait_for(lambda: main.windows[-1] is not plot and not main.windows[-1].worker.running)
    widths = []
    for target, name in ((plot, "S11"), (main.windows[-1], "uhfli_demods4_time_trace_5_x_amplitude")):
        target.setStyleSheet(target.styleSheet() + f"QWidget {{ font-size: {font_size}pt; }}")
        monkeypatch.setattr(target, "_trace_measurement_name", lambda *_args, name=name: name)
        # Some styles add a border to the indicator only after a checked marker is switched off.
        next(iter(target._trace_styles.values())).markers_enabled = True
        if first_page == "Axis scales":
            target.open_axis_scale_dialog("x")
        dialog = target.open_plot_appearance_dialog()
        qapplication.processEvents()
        splitter = dialog.appearance_splitter
        assert splitter.orientation() == QtCore.Qt.Orientation.Horizontal
        widths.append(dialog.width())
        properties = splitter.widget(1)
        assert properties.width() <= properties.sizeHint().width() + dialog.fontMetrics().height()
        table = dialog.table
        column = dialog._COL_MEASUREMENT
        assert table.columnWidth(column) >= table.sizeHintForColumn(column)
        label_width = max(table.sizeHintForColumn(column), table.horizontalHeader().sectionSizeHint(column))
        datasets = splitter.widget(0)
        list_width = datasets.width() - table.columnWidth(column) + label_width
        allowance = table.verticalScrollBar().sizeHint().width() + dialog.fontMetrics().height()
        assert datasets.width() <= max(datasets.sizeHint().width(), list_width) + allowance
        viewport = dialog.controls_scroll.viewport()
        drawing = dialog.marker_enable.parentWidget()
        for extra_width in (0, 160):
            if extra_width:
                datasets_width, properties_width = splitter.sizes()
                dialog.resize(dialog.width() + extra_width, dialog.height())
                splitter.setSizes([datasets_width, properties_width + extra_width])
            for checked in (True, False):
                dialog.marker_enable.setChecked(checked)
                qapplication.processEvents()
                _assert_drawing_row_fits(viewport, dialog.line_enable, dialog.line_color,
                                         dialog.line_width, dialog.line_style)
                _assert_drawing_row_fits(viewport, dialog.marker_enable, dialog.marker_color,
                                         dialog.marker_size, dialog.marker_symbol)
                for line, marker in ((dialog.line_color, dialog.marker_color), (dialog.line_width, dialog.marker_size),
                                     (dialog.line_style, dialog.marker_symbol)):
                    assert line.width() == marker.width()
                    assert line.mapTo(drawing, QtCore.QPoint()).x() == marker.mapTo(drawing, QtCore.QPoint()).x()
                assert dialog.line_color.width() == dialog.line_style.width()
                assert dialog.line_width.width() >= dialog.line_color.width()
                assert dialog.controls_scroll.verticalScrollBar().maximum() == 0
                vertical_right = dialog.y_axis.mapTo(viewport, dialog.y_axis.rect().topRight()).x()
                for control in (dialog.line_style, dialog.marker_symbol):
                    assert control.mapTo(viewport, control.rect().topRight()).x() == vertical_right
                line_bottom = dialog.line_width.mapTo(viewport, dialog.line_width.rect().bottomLeft()).y()
                assert dialog.marker_size.mapTo(viewport, QtCore.QPoint()).y() > line_bottom
        dialog.reject()
    assert widths[0] < widths[1]


@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("screen_width", [800, 1600])
def test_trace_controls_fit_after_heatmap_selection(mixed_plot, qapplication, monkeypatch, reopen, screen_width):
    _, plot, runs = mixed_plot
    monkeypatch.setattr(_appearance_layout, "available_appearance_geometry",
                        lambda _dialog: QtCore.QRect(0, 0, screen_width, 900))
    overlay = runs["heatmap" if plot.operation_kind == "plot1d" else "line"]
    _drop(plot, overlay)
    _added(plot, overlay)
    heatmap = next(iter(plot.heatmaps))
    trace = next(iter(plot.lines))
    plot.open_heatmap_appearance_dialog(heatmap)
    dialog = plot._plot_appearance_dialog
    qapplication.processEvents()

    def layout_positions():
        return (dialog.size(), dialog.whole_plot_group.mapTo(dialog, QtCore.QPoint()),
                dialog.selected_group.mapTo(dialog, QtCore.QPoint()),
                dialog.appearance_buttons.mapTo(dialog, QtCore.QPoint()), dialog.table.size())

    initial = layout_positions()
    properties = dialog.appearance_splitter.widget(1)
    assert dialog.whole_plot_group.geometry().bottom() == properties.contentsRect().bottom()
    if reopen:
        dialog.accept()
        plot.open_trace_appearance_dialog(trace)
    else:
        dialog.select_trace(trace)
    qapplication.processEvents()
    assert dialog._selected_entries() == [("trace", trace)]
    assert layout_positions() == initial
    viewport = dialog.controls_scroll.viewport()
    _wait_for(lambda: dialog.controls_scroll.verticalScrollBar().maximum() == 0)
    _assert_drawing_row_fits(viewport, dialog.line_enable, dialog.line_color, dialog.line_width, dialog.line_style)
    _assert_drawing_row_fits(viewport, dialog.marker_enable, dialog.marker_color, dialog.marker_size, dialog.marker_symbol)
    assert viewport.rect().contains(dialog.y_axis.mapTo(viewport, dialog.y_axis.rect().bottomRight()))
    fitted = dialog.size()
    dialog.select_heatmap(heatmap)
    qapplication.processEvents()
    assert layout_positions() == initial
    custom = fitted - QtCore.QSize(0, 25)
    dialog.resize(custom)
    dialog.select_trace(trace)
    qapplication.processEvents()
    assert dialog.size() == custom
    dialog.reject()


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
def test_heatmap_only_dialog_reserves_trace_space_when_a_trace_is_added(mixed_plot, qapplication):
    _, plot, runs = mixed_plot
    dialog = plot.open_plot_appearance_dialog()
    qapplication.processEvents()
    compact = dialog.size()
    _drop(plot, runs["line"])
    _added(plot, runs["line"])
    _wait_for(lambda: dialog.height() > compact.height())
    assert dialog.width() == compact.width()
    dialog.reject()


def test_page_changes_preserve_a_user_resized_window(mixed_plot, qapplication):
    _, plot, _runs = mixed_plot
    plot.open_plot_appearance_dialog()
    dialog = plot._plot_appearance_dialog
    qapplication.processEvents()
    resized = dialog.size() + QtCore.QSize(57, 43)
    dialog.resize(resized)
    pages = ["Axis scales", "Datasets"]
    if dialog.pages.isTabEnabled(dialog.pages.indexOf(dialog.colour_page)):
        pages += ["Colour scale", "Axis scales", "Datasets"]
    for page in pages:
        dialog.show_page(page)
        qapplication.processEvents()
        assert dialog.pages.tabText(dialog.pages.currentIndex()) == page
        assert dialog.size() == resized
    dialog.reject()
    plot.open_axis_scale_dialog("x")
    qapplication.processEvents()
    assert dialog.isVisible() and dialog.size() == resized
    dialog.reject()


@pytest.mark.parametrize("mixed_plot", ["heatmap"], indirect=True)
def test_range_pages_match_at_the_datasets_default_width(mixed_plot, qapplication):
    _, plot, _runs = mixed_plot
    dialog = plot.open_plot_appearance_dialog()
    qapplication.processEvents()
    default_width = dialog.width()
    plot.open_axis_scale_dialog("x")
    positions = []
    for page, editor in (("Axis scales", plot._axis_scale_controls["x"].rangeEntry),
                         ("Colour scale", plot.colorbar_range_entry)):
        dialog.show_page(page)
        qapplication.processEvents()
        assert dialog.width() == default_width
        assert dialog.pages.currentWidget().horizontalScrollBar().maximum() == 0
        positions.append([(field.mapTo(dialog, field.rect().topLeft()).x(),
                           editor.locks[name].mapTo(dialog, editor.locks[name].rect().topRight()).x())
                          for name, field in editor.fields.items()])
    assert positions[0] == positions[1]
    dialog.reject()


@pytest.mark.parametrize("mixed_plot", ["line"], indirect=True)
@pytest.mark.parametrize(("theme", "font_size"), [("light", 9), ("dark", 14), ("pyqt", 9)])
def test_axis_tabs_fill_inset_panel_above_selected_quantity(mixed_plot, qapplication, theme, font_size):
    main, plot, runs = mixed_plot
    _drop(plot, runs["other_line"])
    _added(plot, runs["other_line"])
    main.config.update("user_preference.theme", theme)
    main.apply_current_settings()
    plot.setStyleSheet(plot.styleSheet() + f"QWidget {{ font-size: {font_size}pt; }}")
    plot.open_axis_scale_dialog("left")
    dialog = plot._plot_appearance_dialog
    primary = dialog.pages.tabBar()
    axes = plot._axis_scale_tabs
    panel = dialog.axes_page.findChild(qtw.QFrame, "axisScalePanel")
    heading = plot._axis_scale_quantity
    assert [primary.tabText(i) for i in range(primary.count())] == ["Datasets", "Axis scales", "Colour scale"]
    assert [axes.tabText(i) for i in range(axes.count())] == ["Bottom", "Left", "Top", "Right"]
    assert panel.isAncestorOf(axes) and panel.isAncestorOf(heading)
    assert dialog.pages.currentWidget() is dialog.axes_page
    assert axes.tabText(axes.currentIndex()) == "Left"
    for i, quantity in enumerate(("x (V)", "y (A)", "u (K)", "w (Hz)")):
        QtTest.QTest.mouseClick(axes, QtCore.Qt.MouseButton.LeftButton, pos=axes.tabRect(i).center())
        qapplication.processEvents()
        assert axes.currentIndex() == i
        controls = plot._axis_scale_controls[axes.tabData(i)]
        assert panel.isAncestorOf(controls) and controls.isVisible()
        assert heading.isVisible() and heading.text() == quantity
        assert axes.mapTo(panel, axes.rect().bottomLeft()).y() < heading.mapTo(panel, QtCore.QPoint()).y()
        assert heading.mapTo(panel, heading.rect().bottomLeft()).y() < controls.mapTo(panel, QtCore.QPoint()).y()

    plot.open_axis_scale_dialog("left")
    axis_widths = []
    page = dialog.axes_page
    width_around_editor = dialog.width() - page.viewport().width()
    fitted_width = max(dialog.width(), page.widget().minimumSizeHint().width() + width_around_editor)
    for width in (fitted_width, fitted_width + 300):
        dialog.resize(width, dialog.height())
        plot.plot.setLabel("left", "Amplitude", units="V")
        plot._sync_axis_scale_controls("y")
        qapplication.processEvents()
        scroll_extent = dialog.axes_page.horizontalScrollBar().maximum()
        quantity = "demodulator_output_" * 14 + "amplitude"
        plot.plot.setLabel("left", quantity, units="V")
        plot._sync_axis_scale_controls("y")
        qapplication.processEvents()
        assert plot._axis_scale_quantity.toolTip() == f"{quantity} (V)"
        assert dialog.axes_page.horizontalScrollBar().maximum() == scroll_extent
        rects = [primary.tabRect(i) for i in range(primary.count())]
        assert max(rect.width() for rect in rects) - min(rect.width() for rect in rects) <= 1
        first = primary.mapTo(dialog.pages, rects[0].topLeft())
        last = primary.mapTo(dialog.pages, rects[-1].bottomRight())
        assert first.x() <= 1
        assert abs(last.x() - dialog.pages.rect().right()) <= 1

        axis_rects = [axes.tabRect(i) for i in range(axes.count())]
        widths = [rect.width() for rect in axis_rects]
        assert max(widths) - min(widths) <= 1
        assert axis_rects[axes.currentIndex()].height() < rects[primary.currentIndex()].height()
        assert all(axes.rect().contains(rect) for rect in axis_rects)
        assert axes.mapTo(panel, axis_rects[0].topLeft()) == panel.contentsRect().topLeft()
        assert axes.mapTo(panel, axis_rects[-1].topRight()) == panel.contentsRect().topRight()
        axis_first = axes.mapTo(dialog.pages, axis_rects[0].topLeft())
        axis_last = axes.mapTo(dialog.pages, axis_rects[-1].bottomRight())
        assert first.x() < axis_first.x() < axis_last.x() < last.x()
        assert axis_first.y() > last.y()
        axis_widths.append(sum(widths))
    assert axis_widths[1] > axis_widths[0]
    dialog.reject()


def test_global_settings_remain_available_across_selection_types(mixed_plot, qapplication):
    _main, target, runs = mixed_plot
    foreign = runs["heatmap" if target.operation_kind == "plot1d" else "line"]
    _drop(target, foreign)
    _added(target, foreign)
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    dialog.resize(1000, 800)
    button = dialog.heatmap_controls.color_scale_button
    assert dialog.whole_plot_group.isAncestorOf(button)
    assert dialog.whole_plot_group.isAncestorOf(dialog.swap_axes)
    assert not dialog.selected_group.isAncestorOf(button)
    for row in range(dialog.table.rowCount()):
        dialog.table.selectRow(row)
        qapplication.processEvents()
        def section_fits():
            controls = (dialog.heatmap_controls
                        if dialog.controls_stack.currentWidget() is dialog.heatmap_controls else dialog)
            axes = controls.x_axis.parentWidget()
            bottom = axes.mapTo(dialog.selected_group, axes.rect().bottomLeft()).y()
            gap = dialog.selected_group.rect().bottom() - bottom
            return not dialog.controls_scroll.verticalScrollBar().isVisible() and 0 <= gap <= 24
        _wait_for(section_fits)
        assert button.isVisible() and button.isEnabled()
    dialog.table.clearSelection()
    qapplication.processEvents()
    assert button.isVisible() and button.isEnabled()
    assert not dialog.line_enable.isEnabled()
    assert not dialog.heatmap_controls.opacity.isEnabled()
    before = target._current_colorbar_colormap_name()
    button.click()
    qapplication.processEvents()
    chooser = target.colorbar_map_dialog
    assert chooser.isVisible() and chooser.parentWidget() is dialog
    chooser.picker.table.selectRow(chooser.picker.row_for_name("plasma"))
    assert target._current_colorbar_colormap_name() == "plasma"
    dialog.reject()
    assert target._current_colorbar_colormap_name() == before


def test_one_marker_editor_changes_symbols_without_changing_samples(mixed_plot, qapplication):
    _main, target, runs = mixed_plot
    if target.operation_kind != "plot1d":
        _drop(target, runs["line"])
        _added(target, runs["line"])
    key, line = next(iter(target.lines.items()))
    data = tuple(np.array(values, copy=True) for values in line.getData())
    style = deepcopy(target._trace_styles[key])
    target.open_plot_appearance_dialog()
    dialog = target._plot_appearance_dialog
    assert dialog.select_trace(key)
    # Settle native show/focus events before editing the selected dataset.
    qapplication.processEvents()
    assert not hasattr(dialog, "dots_enable")
    dialog.marker_enable.setChecked(True)
    dialog.marker_symbol.setCurrentIndex(dialog.marker_symbol.findData("s"))
    dialog.marker_size.setValue(6.5)
    qapplication.processEvents()
    assert line.opts["symbol"] == "s"
    assert line.opts["symbolSize"] == 6.5
    combo = dialog.marker_symbol
    with_symbol = combo.grab().toImage()
    blank = QtGui.QPixmap(combo.iconSize())
    blank.fill(QtCore.Qt.GlobalColor.transparent)
    combo.setItemIcon(combo.currentIndex(), QtGui.QIcon(blank))
    without_symbol = combo.grab().toImage()
    ink = QtCore.QRect()
    for y in range(with_symbol.height()):
        for x in range(with_symbol.width()):
            if with_symbol.pixel(x, y) != without_symbol.pixel(x, y):
                ink |= QtCore.QRect(x, y, 1, 1)
    assert not ink.isEmpty()
    assert (ink.center() - with_symbol.rect().center()).manhattanLength() <= 2
    for expected, actual in zip(data, line.getData(), strict=True):
        np.testing.assert_array_equal(expected, actual)
    dialog.reject()
    assert target._trace_styles[key] == style


def test_dialog_colours_follow_plot_theme_over_light_system_palette(mixed_plot, qapplication):
    from PyQt6 import QtGui

    from qplot.configuration.themes.dark import _PALETTE as dark
    from qplot.configuration.themes.light import _PALETTE as light
    from qplot.windows._preferences import PreferencesDialog

    main, plot, runs = mixed_plot
    overlay = runs["other_line"]
    _drop(plot, overlay)
    _added(plot, overlay)
    original_palette = qapplication.palette()
    palette = QtGui.QPalette(original_palette)
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor("white"))
    palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor("black"))
    qapplication.setPalette(palette)
    plot.open_plot_appearance_dialog()
    appearance = plot._plot_appearance_dialog
    plot.open_export_dialog()
    export = plot.widget.scene().exportDialog
    preferences = PreferencesDialog(main.config, main)
    preferences.tabs.setCurrentIndex(1)
    preferences.show()
    try:
        for theme, colours in (("dark", dark), ("light", light)):
            main.config.update("user_preference.theme", theme)
            main.apply_current_settings()
            qapplication.processEvents()
            assert export.isVisible() and export.parentWidget() is plot
            assert export.controls.heading.palette().windowText().color().name() == colours.text
            assert export.palette().window().color().name() == colours.window_bg
            table = appearance.table
            assert table.rowCount() == 2
            table.selectRow(0)
            table.setFocus()
            assert table.palette().base().color().name() == colours.table_bg
            assert table.palette().alternateBase().color().name() == colours.table_alt_bg
            assert table.palette().text().color().name() == colours.text
            assert table.palette().highlightedText().color().name() == colours.selection_text
            assert appearance.selected_group.palette().windowText().color().name() == colours.text
            style = preferences.defaultTraceStyleCombo
            sample = style._preview("Solid", 32).pixmap(32, 16).toImage()
            assert sample.pixelColor(16, 8).name() == colours.text
            assert not preferences.apply_button.isEnabled()
            disabled_ink = preferences.apply_button.palette().color(
                QtGui.QPalette.ColorGroup.Disabled, QtGui.QPalette.ColorRole.ButtonText,
            )
            assert disabled_ink.name() == colours.disabled_text
    finally:
        preferences.reject()
        export.close()
        appearance.reject()
        qapplication.setPalette(original_palette)


def test_overlay_marquee_report_names_the_host_measurement(mixed_plot):
    _, plot, runs = mixed_plot
    _drop(plot, runs["other_line"])
    _added(plot, runs["other_line"])
    plot.set_marquee_rect(QtCore.QRectF(-1, -1, 40, 40))
    report = plot._marquee_stats_text()
    assert f"Run: {plot.ds.run_id} ({plot.ds.guid})" in report
    assert f"Measurement: {plot.param.name} [{plot.param.unit}]" in report
    assert runs["other_line"].guid not in report
