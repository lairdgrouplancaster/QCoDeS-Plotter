import unittest

from PyQt5 import QtCore
from PyQt5 import QtWidgets as qtw
import pyqtgraph as pg

from qplot.windows.plot2d import plot2d
from qplot.windows._plotWin import plotWidget
from qplot.windows._widgets import treeWidgets


class PlotWindowRefreshTestCase(unittest.TestCase):
    class Timer:
        def __init__(self):
            self.stopped = 0

        def stop(self):
            self.stopped += 1

    class SpinBox:
        def value(self):
            return 0.2

    class Dataset:
        def __init__(self, number_of_results=10, running=True):
            self.number_of_results = number_of_results
            self.running = running

    class Worker:
        def __init__(self, running):
            self.running = running

    def _window(self, *, worker_running):
        window = plotWidget.__new__(plotWidget)
        window.monitor = self.Timer()
        window.spinBox = self.SpinBox()
        window._guid = "guid"
        window._dataset_holder = {
            "guid": {
                "dataset": self.Dataset(),
                "del_timer": None,
                }
            }
        window.worker = self.Worker(worker_running)
        window.last_ds_len = 0
        window.load_calls = []
        window.restart_intervals = []
        window.load_data = lambda: window.load_calls.append("load")
        window.monitorIntervalChanged = lambda interval: (
            window.restart_intervals.append(interval)
            )
        return window

    def test_refresh_keeps_pending_row_count_when_worker_is_busy(self):
        window = self._window(worker_running=True)

        plotWidget.refreshWindow(window)

        self.assertEqual(window.load_calls, [])
        self.assertEqual(window.last_ds_len, 0)
        self.assertEqual(window.restart_intervals, [0.2])

    def test_refresh_records_row_count_when_worker_is_started(self):
        window = self._window(worker_running=False)

        plotWidget.refreshWindow(window)

        self.assertEqual(window.load_calls, ["load"])
        self.assertEqual(window.last_ds_len, 10)
        self.assertEqual(window.restart_intervals, [0.2])


class RunListParentLookupTestCase(unittest.TestCase):
    def test_main_window_lookup_works_through_splitter(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False
        main = None

        try:
            main = qtw.QMainWindow()
            main.ds = object()
            main.openPlot = lambda *args, **kwargs: None

            frame = qtw.QFrame()
            layout = qtw.QVBoxLayout(frame)
            splitter = qtw.QSplitter(QtCore.Qt.Vertical)
            run_list = treeWidgets.RunList()
            splitter.addWidget(run_list)
            splitter.addWidget(qtw.QTreeWidget())
            layout.addWidget(splitter)
            main.setCentralWidget(frame)

            self.assertIs(run_list.main_window(), main)
        finally:
            treeWidgets.isfile = old_isfile
            if main is not None:
                main.deleteLater()

    def test_run_context_menu_keeps_plot_actions_without_add_actions(self):
        old_isfile = treeWidgets.isfile
        old_exec = qtw.QMenu.exec_
        treeWidgets.isfile = lambda _: False
        captured = []
        main = None

        class Param:
            def __init__(self, name, depends_on):
                self.name = name
                self.depends_on = depends_on
                self.depends_on_ = (depends_on,)

        class Dataset:
            def get_parameters(self):
                return [Param("signal", "x"), Param("image", "y")]

        def capture_menu(menu, *_args, **_kwargs):
            captured.extend(action.text() for action in menu.actions())

        try:
            qtw.QMenu.exec_ = capture_menu
            main = qtw.QMainWindow()
            main.ds = Dataset()
            main.windows = []
            main.openPlot = lambda *args, **kwargs: None
            main.open_selected_run_all = lambda: None
            main.show_status = lambda *args, **kwargs: None

            run_list = treeWidgets.RunList()
            main.setCentralWidget(run_list)

            run_list.prepareMenu(QtCore.QPoint(0, 0))

            self.assertEqual(captured[0], "&Plot all")
            self.assertIn("&Plot all", captured)
            self.assertIn("  - signal", captured)
            self.assertIn("  - image", captured)
            self.assertNotIn("Add to open plot", captured)
            self.assertFalse(any(action.startswith("Add ") for action in captured))
            self.assertFalse(any(action.startswith("  - Add ") for action in captured))
        finally:
            qtw.QMenu.exec_ = old_exec
            treeWidgets.isfile = old_isfile
            if main is not None:
                main.deleteLater()

    def test_plot_options_menu_includes_preferences_and_excludes_confirmation_duplicates(self):
        class Host(qtw.QMainWindow):
            initMenu = plotWidget.initMenu
            createPopupMenu = plotWidget.createPopupMenu
            register_shortcut = plotWidget.register_shortcut

            def request_close_all_plots(self):
                pass

            def request_application_quit(self):
                pass

            def refreshWindow(self, force=False):
                pass

            def show_preferences_dialog(self):
                pass

        host = Host()
        host.vbMenu = qtw.QMenu(host)
        host.mouseModeAction = qtw.QAction("Mouse Mode", host)
        host.vbMenu.addAction(host.mouseModeAction)

        try:
            host.initMenu()
            menus = {
                action.text().replace("&", ""): action.menu()
                for action in host.menuBar().actions()
                }
            option_texts = [
                action.text().replace("&", "")
                for action in menus["Options"].actions()
                if not action.isSeparator()
                ]
            preferences_action = next(
                action for action in menus["Options"].actions()
                if action.text().replace("&", "") == "Preferences..."
                )

            self.assertIn("Preferences...", option_texts)
            self.assertEqual(
                preferences_action.menuRole(),
                qtw.QAction.PreferencesRole,
                )
            self.assertIn("Mouse Mode", option_texts)
            self.assertNotIn("Confirm Before Closing All Plot Windows", option_texts)
            self.assertNotIn("Confirm Before Quit", option_texts)
        finally:
            host.deleteLater()

    def test_plot_export_is_removed_from_scene_context_menu(self):
        widget = pg.GraphicsLayoutWidget()
        fake_plot = type("FakePlot", (), {"widget": widget})()

        try:
            self.assertIn(
                "Export...",
                [action.text().replace("&", "") for action in widget.scene().contextMenu],
                )

            plotWidget._remove_scene_export_context_menu(fake_plot)

            self.assertNotIn(
                "Export...",
                [action.text().replace("&", "") for action in widget.scene().contextMenu],
                )
        finally:
            widget.deleteLater()

    def test_plot_export_action_has_keyboard_shortcut(self):
        class Host(qtw.QMainWindow):
            initContextMenu = plotWidget.initContextMenu
            register_shortcut = plotWidget.register_shortcut
            _remove_scene_export_context_menu = plotWidget._remove_scene_export_context_menu
            _context_menu_action = plotWidget._context_menu_action
            _connect_mouse_mode_menu_to_preferences = (
                plotWidget._connect_mouse_mode_menu_to_preferences
                )

            def _init_axis_scale_dialogs(self):
                pass

            def open_context_menu(self):
                pass

            def open_export_dialog(self):
                self.export_opened = True

        widget = pg.GraphicsLayoutWidget()
        host = Host()
        host.widget = widget
        host.plot = widget.addPlot()
        host.vb = host.plot.vb
        host.oper_dock = qtw.QDockWidget()
        host.export_opened = False

        try:
            host.initContextMenu()

            self.assertEqual(
                host.exportPlotAction.shortcut().toString(),
                "Ctrl+E",
                )
            self.assertEqual(
                host.exportPlotAction.shortcutContext(),
                QtCore.Qt.WindowShortcut,
                )
            self.assertIn(host.exportPlotAction, host.actions())

            host.exportPlotAction.trigger()

            self.assertTrue(host.export_opened)
        finally:
            host.deleteLater()
            widget.deleteLater()

    def test_mouse_mode_preference_updates_viewbox_mode(self):
        class Config:
            def __init__(self):
                self.values = {"user_preference.mouse_mode": "pan"}
                self.updates = []

            def get(self, key):
                return self.values[key]

            def update(self, key, value):
                self.values[key] = value
                self.updates.append((key, value))

        window = plotWidget.__new__(plotWidget)
        window.config = Config()
        window.vb = pg.ViewBox()

        try:
            self.assertTrue(window.change_mouse_mode("rect"))

            self.assertEqual(
                window.config.updates,
                [("user_preference.mouse_mode", "rect")],
                )
            self.assertEqual(
                window.vb.getState(copy=False)["mouseMode"],
                pg.ViewBox.RectMode,
                )
        finally:
            window.vb.deleteLater()

    def test_axis_scale_controls_move_from_context_menu_to_dialogs(self):
        class Host(qtw.QMainWindow):
            initContextMenu = plotWidget.initContextMenu
            _init_axis_scale_dialogs = plotWidget._init_axis_scale_dialogs
            _menu_control_widget = plotWidget._menu_control_widget
            _connect_mouse_mode_menu_to_preferences = (
                plotWidget._connect_mouse_mode_menu_to_preferences
                )
            _install_axis_scale_double_click_handlers = (
                plotWidget._install_axis_scale_double_click_handlers
                )
            _axis_scale_dialog_title = plotWidget._axis_scale_dialog_title
            _axis_scale_axis_number = plotWidget._axis_scale_axis_number
            _axis_scale_axis_constant = plotWidget._axis_scale_axis_constant
            _new_axis_scale_controls = plotWidget._new_axis_scale_controls
            _sync_axis_scale_controls = plotWidget._sync_axis_scale_controls
            _sync_axis_scale_link_combo = plotWidget._sync_axis_scale_link_combo
            _axis_scale_mouse_toggled = plotWidget._axis_scale_mouse_toggled
            _axis_scale_manual_clicked = plotWidget._axis_scale_manual_clicked
            _axis_scale_range_text_changed = plotWidget._axis_scale_range_text_changed
            _axis_scale_auto_clicked = plotWidget._axis_scale_auto_clicked
            _axis_scale_auto_spin_changed = plotWidget._axis_scale_auto_spin_changed
            _axis_scale_link_changed = plotWidget._axis_scale_link_changed
            _axis_scale_auto_pan_toggled = plotWidget._axis_scale_auto_pan_toggled
            _axis_scale_visible_only_toggled = plotWidget._axis_scale_visible_only_toggled
            _axis_scale_invert_toggled = plotWidget._axis_scale_invert_toggled
            open_axis_scale_dialog = plotWidget.open_axis_scale_dialog
            _remove_scene_export_context_menu = plotWidget._remove_scene_export_context_menu
            _context_menu_action = plotWidget._context_menu_action

            def register_shortcut(self, *_args, **_kwargs):
                pass

            def open_context_menu(self):
                pass

            def open_export_dialog(self):
                pass

        widget = pg.GraphicsLayoutWidget()
        host = Host()
        host.widget = widget
        host.plot = widget.addPlot()
        host.vb = host.plot.vb
        host.oper_dock = qtw.QDockWidget()

        try:
            host.initContextMenu()
            action_texts = [action.text().replace("&", "") for action in host.vbMenu.actions()]

            self.assertNotIn("X axis", action_texts)
            self.assertNotIn("Y axis", action_texts)
            self.assertEqual(host.plot.ctrlMenu.title(), "Options")
            self.assertEqual(host.plot.ctrlMenu.menuAction().text(), "Options")

            host.open_axis_scale_dialog("x")

            dialog = host._axis_scale_dialogs["x"]
            self.assertEqual(dialog.windowTitle(), "X axis scaling")
            self.assertIn("x", host._axis_scale_controls)
            self.assertEqual(host._axis_scale_controls["x"].manualRadio.text(), "Manual")
            self.assertEqual(host._axis_scale_controls["x"].autoRadio.text(), "Auto")
            self.assertEqual(host._axis_scale_controls["x"].invertCheck.text(), "Invert Axis")
        finally:
            host.deleteLater()
            widget.deleteLater()

    def test_colorbar_scale_action_opens_dialog_without_nested_menu(self):
        class Bar:
            def __init__(self):
                self.color_map = None

            def levels(self):
                return 1.0, 2.0

            def setColorMap(self, color_map):
                self.color_map = color_map

        class Config:
            def __init__(self):
                self.values = {
                "user_preference.bar_colour": "viridis",
                "user_preference.bar_colour_include_cet": True,
                "user_preference.bar_colour_include_matplotlib": True,
                "user_preference.bar_colour_include_local": True,
                "user_preference.bar_colour_include_custom": True,
                "user_preference.bar_colour_excluded": [],
                "user_preference.bar_colour_excluded_prefixes": [],
                }
                self.updates = []

            def get(self, key):
                self.get_key = key
                return self.values[key]

            def update(self, key, value):
                self.values[key] = value
                self.updates.append((key, value))

        class Host(qtw.QMainWindow):
            _current_colorbar_levels = plot2d._current_colorbar_levels
            _current_colorbar_colormap_name = plot2d._current_colorbar_colormap_name
            _available_colorbar_colormaps = plot2d._available_colorbar_colormaps
            _fallback_colorbar_colormap_name = plot2d._fallback_colorbar_colormap_name
            _colorbar_colormap = plot2d._colorbar_colormap
            _init_colorbar_scale_controls = plot2d._init_colorbar_scale_controls
            _init_colorbar_filter_controls = plot2d._init_colorbar_filter_controls
            _init_colorbar_colormap_table = plot2d._init_colorbar_colormap_table
            _populate_colorbar_colormap_table = plot2d._populate_colorbar_colormap_table
            _sync_colorbar_filter_controls = plot2d._sync_colorbar_filter_controls
            _set_colorbar_filter_setting = plot2d._set_colorbar_filter_setting
            _set_colorbar_subtype_filter_setting = (
                plot2d._set_colorbar_subtype_filter_setting
                )
            _colorbar_include_cet_changed = plot2d._colorbar_include_cet_changed
            _colorbar_include_matplotlib_changed = (
                plot2d._colorbar_include_matplotlib_changed
                )
            _colorbar_include_local_changed = plot2d._colorbar_include_local_changed
            _colorbar_include_custom_changed = plot2d._colorbar_include_custom_changed
            _colorbar_include_subtype_changed = plot2d._colorbar_include_subtype_changed
            _install_colorbar_scale_bar_handlers = plot2d._install_colorbar_scale_bar_handlers
            _install_colorbar_scale_axis_handlers = plot2d._install_colorbar_scale_axis_handlers
            _install_colorbar_scale_double_click_handler = (
                plot2d._install_colorbar_scale_double_click_handler
                )
            _suppress_colorbar_right_click_menu = plot2d._suppress_colorbar_right_click_menu
            _install_colorbar_level_sync_handlers = (
                plot2d._install_colorbar_level_sync_handlers
                )
            _install_colorbar_alt_range_drag_handler = (
                plot2d._install_colorbar_alt_range_drag_handler
                )
            _install_colorbar_alt_handle_drag_handler = (
                plot2d._install_colorbar_alt_handle_drag_handler
                )
            _colorbar_alt_range_drag_event = plot2d._colorbar_alt_range_drag_event
            _colorbar_alt_range_drag_axis_position = (
                plot2d._colorbar_alt_range_drag_axis_position
                )
            _set_colorbar_alt_range_drag_visual = (
                plot2d._set_colorbar_alt_range_drag_visual
                )
            _colorbar_alt_range_drag_levels = plot2d._colorbar_alt_range_drag_levels
            _colorbar_levels_from_bar = plot2d._colorbar_levels_from_bar
            _colorbar_interactive_levels_changed = (
                plot2d._colorbar_interactive_levels_changed
                )
            _colorbar_interactive_levels_finished = (
                plot2d._colorbar_interactive_levels_finished
                )
            _sync_colorbar_scale_controls = plot2d._sync_colorbar_scale_controls
            _sync_colorbar_level_fields = plot2d._sync_colorbar_level_fields
            _colorbar_colormap_row = plot2d._colorbar_colormap_row
            _select_colorbar_colormap = plot2d._select_colorbar_colormap
            _colorbar_colormap_selection_changed = (
                plot2d._colorbar_colormap_selection_changed
                )
            _colorbar_colormap_changed = plot2d._colorbar_colormap_changed
            open_colorbar_scale_dialog = plot2d.open_colorbar_scale_dialog
            _apply_colorbar_manual_fields = plot2d._apply_colorbar_manual_fields
            setColorbarColorMap = plot2d.setColorbarColorMap
            setColorbarManualRange = plot2d.setColorbarManualRange
            setColorbarAuto = plot2d.setColorbarAuto
            scaleColorbar = plot2d.scaleColorbar

            def show_status(self, *_args, **_kwargs):
                pass

        host = Host()
        host.vbMenu = qtw.QMenu(host)
        host.autoscaleSep = host.vbMenu.addSeparator()
        host.bar = Bar()
        host.config = Config()
        host._colorbar_manual_levels = None

        class Axis:
            pass

        class MouseEvent:
            def __init__(self, button):
                self._button = button
                self.accepted = False

            def button(self):
                return self._button

            def accept(self):
                self.accepted = True

        try:
            host._init_colorbar_scale_controls()
            action_texts = [action.text().replace("&", "") for action in host.vbMenu.actions()]

            self.assertNotIn("Color Scale...", action_texts)
            self.assertGreater(host._colorbar_colormap_row("Greys"), -1)
            self.assertGreater(host._colorbar_colormap_row("Purples"), -1)
            self.assertGreater(host._colorbar_colormap_row("CET-C1"), -1)
            self.assertGreater(host._colorbar_colormap_row("PAL-relaxed"), -1)
            self.assertEqual(host.colorbar_colormap_table.columnCount(), 3)
            self.assertTrue(host.colorbar_colormap_table.isSortingEnabled())
            self.assertEqual(
                host.colorbar_colormap_table.rowCount(),
                len(host._available_colorbar_colormaps()),
                )
            type_item = host.colorbar_colormap_table.item(
                host._colorbar_colormap_row("viridis"),
                2,
                )
            self.assertEqual(type_item.text(), "Matplotlib - Perceptual")
            preview_item = host.colorbar_colormap_table.item(
                host._colorbar_colormap_row("viridis"),
                1,
                )
            self.assertFalse(preview_item.icon().isNull())
            host.colorbar_colormap_table.sortItems(2, QtCore.Qt.AscendingOrder)
            self.assertGreater(host._colorbar_colormap_row("viridis"), -1)

            host.open_colorbar_scale_dialog()

            self.assertEqual(host.colorbar_scale_dialog.windowTitle(), "Color scale")
            self.assertIs(host.colorbar_scale_controls.parent(), host.colorbar_scale_dialog)

            host.colorbar_colormap_table.setCurrentCell(
                host._colorbar_colormap_row("Purples"),
                0,
                )
            self.assertEqual(
                host.config.updates,
                [("user_preference.bar_colour", "Purples")],
                )
            self.assertIsInstance(host.bar.color_map, pg.ColorMap)

            host.colorbar_include_local_check.setChecked(False)
            self.assertEqual(
                host.config.updates[-1],
                ("user_preference.bar_colour_include_local", False),
                )
            self.assertEqual(host._colorbar_colormap_row("PAL-relaxed"), -1)

            host.colorbar_include_custom_check.setChecked(False)
            self.assertEqual(
                host.config.updates[-1],
                ("user_preference.bar_colour_include_custom", False),
                )
            self.assertEqual(host._colorbar_colormap_row("Greys"), -1)

            host.colorbar_cet_subtype_checks["linear"].setChecked(False)
            self.assertEqual(
                host.config.updates[-1],
                ("user_preference.bar_colour_include_cet_linear", False),
                )
            self.assertEqual(host._colorbar_colormap_row("CET-L1"), -1)
            self.assertGreater(host._colorbar_colormap_row("CET-C1"), -1)

            host.colorbar_include_cet_check.setChecked(False)
            self.assertEqual(
                host.config.updates[-1],
                ("user_preference.bar_colour_include_cet", False),
                )
            self.assertEqual(host._colorbar_colormap_row("CET-C1"), -1)

            double_click_calls = []
            host.open_colorbar_scale_dialog = lambda: double_click_calls.append(True)
            axis = Axis()
            host._install_colorbar_scale_axis_handlers(axis)
            event = MouseEvent(QtCore.Qt.LeftButton)
            axis.mouseDoubleClickEvent(event)

            self.assertTrue(event.accepted)
            self.assertEqual(double_click_calls, [True])
            self.assertFalse(hasattr(axis, "contextMenuEvent"))

            previous_bar_clicks = []
            bar = Axis()
            bar.mouseClickEvent = lambda event: previous_bar_clicks.append(event.button())
            host._install_colorbar_scale_bar_handlers(bar)
            double_click_calls.clear()

            event = MouseEvent(QtCore.Qt.LeftButton)
            bar.mouseDoubleClickEvent(event)

            self.assertTrue(event.accepted)
            self.assertEqual(double_click_calls, [True])

            event = MouseEvent(QtCore.Qt.RightButton)
            bar.mouseClickEvent(event)

            self.assertTrue(event.accepted)
            self.assertEqual(previous_bar_clicks, [])
        finally:
            host.deleteLater()


