from typing import Any, cast

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.diagnostics import default_log_file

from ._commands import create_action, shortcut_help_html

_OPEN_HELP_DIALOGS: list[qtw.QDialog] = []


QUICK_START_HTML = """
<h2>Quick Start</h2>
<ol>
  <li><b>Load a database.</b> Drop a QCoDeS .db file onto the database field,
      or use <b>File -&gt; Load Database...</b>.</li>
  <li><b>Select a run.</b> Click a row in the run table to see details,
      parameters, preview images, and metadata.</li>
  <li><b>Open a plot.</b> Double-click a preview, right-click a run, or enter
      a run ID and measurement number at the top of the main window.</li>
  <li><b>Inspect the plot.</b> Use the mouse wheel to zoom, left-drag to pan,
  and Shift-drag to constrain panning to one axis,
      right-click for plot actions, and double-click axes for scale controls.</li>
  <li><b>Export or print data and plots.</b> Use the CSV button or preview
      context menu for measurement data. In plot windows, use
      <b>File -&gt; Export Plot...</b> for images or data, or
      <b>File -&gt; Save Plot as PDF...</b> for a plot-sized PDF. Use
      <b>File -&gt; Print Plot...</b> for a printer or, when the system dialog
      offers a concrete PDF destination, a page-formatted PDF. qPlot stages and
      atomically publishes PDF file output.</li>
</ol>
<p>Plot windows may appear before their data has finished loading. Check the
status bar at the bottom of the window before assuming a load has failed.</p>
<p>Set the refresh interval to <b>0.0 s</b> for manual refresh only. Press
<b>R</b> to refresh the active window.</p>
"""


KEYBOARD_SHORTCUTS_HTML = shortcut_help_html()


def add_help_menu(window: qtw.QMainWindow) -> qtw.QMenu:
    """
    Adds qPlot's shared Help menu to a main or plot window.

    """
    menu_bar = window.menuBar()
    if menu_bar is None:
        raise RuntimeError("Help menu requires a menu bar.")
    help_menu = menu_bar.addMenu("&Help")
    if help_menu is None:
        raise RuntimeError("Help menu could not be created.")

    quick_start_action = create_action("help.quick_start", window)
    quick_start_action.triggered.connect(lambda: show_quick_start(window))
    help_menu.addAction(quick_start_action)

    shortcuts_action = QtGui.QAction("&Keyboard Shortcuts", window)
    shortcuts_action.setObjectName("keyboardShortcutsHelpAction")
    shortcuts_action.setStatusTip("Show qPlot keyboard shortcuts")
    shortcuts_action.triggered.connect(lambda: show_keyboard_shortcuts(window))
    help_menu.addAction(shortcuts_action)

    help_menu.addSeparator()

    copy_log_path_action = QtGui.QAction("Copy &Diagnostic Log Path", window)
    copy_log_path_action.setObjectName("copyDiagnosticLogPathAction")
    copy_log_path_action.setStatusTip("Copy the qPlot diagnostic log file path")
    copy_log_path_action.triggered.connect(lambda: copy_diagnostic_log_path(window))
    help_menu.addAction(copy_log_path_action)

    return help_menu


def show_quick_start(parent: qtw.QWidget | None = None) -> qtw.QDialog:
    """
    Opens the quick-start help dialog.

    """
    return _show_help_dialog(
        parent,
        "qPlot Quick Start",
        QUICK_START_HTML,
        "qplotQuickStartDialog",
        )


def show_keyboard_shortcuts(parent: qtw.QWidget | None = None) -> qtw.QDialog:
    """
    Opens the keyboard-shortcuts help dialog.

    """
    return _show_help_dialog(
        parent,
        "qPlot Keyboard Shortcuts",
        KEYBOARD_SHORTCUTS_HTML,
        "qplotKeyboardShortcutsDialog",
        )


def copy_diagnostic_log_path(parent: Any | None = None) -> str:
    """
    Copies qPlot's diagnostic log path to the clipboard.

    """
    path = str(default_log_file())
    clipboard = qtw.QApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(path)
    if parent is not None and hasattr(parent, "show_status"):
        parent.show_status(f"Copied diagnostic log path: {path}", 5000)
    return path


def _show_help_dialog(
        parent: qtw.QWidget | None,
        title: str,
        html: str,
        object_name: str,
        ) -> qtw.QDialog:
    dialog = qtw.QDialog(parent)
    dialog.setObjectName(object_name)
    dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
    dialog.setWindowTitle(title)
    dialog.resize(640, 520)

    layout = qtw.QVBoxLayout(dialog)
    browser = qtw.QTextBrowser()
    browser.setObjectName("qplotHelpBrowser")
    browser.setOpenExternalLinks(True)
    browser.setHtml(html)
    browser.setMinimumSize(520, 360)
    layout.addWidget(browser)

    buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dialog.close)
    layout.addWidget(buttons)

    _remember_help_dialog(parent, dialog)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


def _remember_help_dialog(
        parent: qtw.QWidget | None,
        dialog: qtw.QDialog,
        ) -> None:
    dialogs: list[qtw.QDialog] | None
    if parent is None:
        dialogs = _OPEN_HELP_DIALOGS
    else:
        owner = cast(Any, parent)
        dialogs = getattr(owner, "_help_dialogs", None)
        if dialogs is None:
            dialogs = []
            owner._help_dialogs = dialogs

    if dialogs is None:
        return
    dialogs.append(dialog)

    def forget_dialog(*_args: object) -> None:
        if dialog in dialogs:
            dialogs.remove(dialog)

    dialog.destroyed.connect(forget_dialog)
