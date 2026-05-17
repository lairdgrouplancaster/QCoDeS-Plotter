from PyQt5 import QtCore
from PyQt5 import QtWidgets as qtw

from qplot.diagnostics import default_log_file


_OPEN_HELP_DIALOGS = []


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
      right-click for plot actions, and double-click axes for scale controls.</li>
  <li><b>Export data or plots.</b> Use the CSV button or preview context menu
      for measurement data, and <b>File -&gt; Export Plot...</b> or
      <b>Edit -&gt; Copy Plot Image</b> in plot windows.</li>
</ol>
<p>Plot windows may appear before their data has finished loading. Check the
status bar at the bottom of the window before assuming a load has failed.</p>
<p>Set the refresh interval to <b>0.0 s</b> for manual refresh only. Press
<b>R</b> to refresh the active window.</p>
"""


KEYBOARD_SHORTCUTS_HTML = """
<h2>Keyboard Shortcuts</h2>
<h3>General</h3>
<table cellspacing="4" cellpadding="3">
  <tr><td><b>F1</b></td><td>Show quick start help</td></tr>
  <tr><td><b>Ctrl+L</b></td><td>Load a database</td></tr>
  <tr><td><b>R</b></td><td>Refresh the current window</td></tr>
  <tr><td><b>Ctrl+W / Cmd+W</b></td><td>Close the current qPlot window</td></tr>
  <tr><td><b>Ctrl+Q / Cmd+Q</b></td><td>Quit qPlot</td></tr>
  <tr><td><b>Ctrl+M / Alt+Space, N</b></td><td>Minimize the current window</td></tr>
  <tr><td><b>Alt+Space, X / Alt+Space, R</b></td><td>Maximize or restore on Windows</td></tr>
  <tr><td><b>Ctrl+Cmd+F / F11</b></td><td>Enter or leave full screen</td></tr>
  <tr><td><b>Shift+F10</b></td><td>Open the focused widget's context menu</td></tr>
  <tr><td><b>Ctrl+Shift+D</b></td><td>Open the current database folder</td></tr>
  <tr><td><b>Ctrl+Shift+M</b></td><td>Bring the main window to front, or behind plot windows</td></tr>
  <tr><td><b>Ctrl+Return</b></td><td>Plot the requested run and measurement</td></tr>
  <tr><td><b>Ctrl+Shift+Return</b></td><td>Plot all measurements in the selected run</td></tr>
  <tr><td><b>Ctrl+1 to Ctrl+9</b></td><td>Plot measurements 1 to 9 in the selected run</td></tr>
  <tr><td><b>Ctrl+Shift+W</b></td><td>Close all plot windows</td></tr>
</table>

<h3>Plot Windows</h3>
<table cellspacing="4" cellpadding="3">
  <tr><td><b>Ctrl+0</b></td><td>Autoscale the plot view</td></tr>
  <tr><td><b>Ctrl+C / Cmd+C</b></td><td>Copy the plot image to the clipboard</td></tr>
  <tr><td><b>Ctrl+E</b></td><td>Export the plot</td></tr>
  <tr><td><b>Ctrl+Shift+O</b></td><td>Show or hide the operations panel</td></tr>
  <tr><td><b>Ctrl+Alt+R</b></td><td>Show or hide the refresh toolbar</td></tr>
  <tr><td><b>Ctrl+Alt+C</b></td><td>Show or hide the coordinate toolbar</td></tr>
  <tr><td><b>Ctrl+Alt+A</b></td><td>Show or hide the axis control panel</td></tr>
  <tr><td><b>Ctrl+Alt+O</b></td><td>Show or hide the operations dock</td></tr>
  <tr><td><b>Ctrl+Alt+S</b></td><td>Snap the 1D coordinate readout to the nearest trace point</td></tr>
</table>

<h3>Heatmaps</h3>
<table cellspacing="4" cellpadding="3">
  <tr><td><b>Ctrl+Shift+C</b></td><td>Autoscale the colour range</td></tr>
  <tr><td><b>H</b></td><td>Open a horizontal cut</td></tr>
  <tr><td><b>V</b></td><td>Open a vertical cut</td></tr>
  <tr><td><b>Arrow keys</b></td><td>Move the selected cut cursor by one pixel</td></tr>
</table>
"""


def add_help_menu(window):
    """
    Adds qPlot's shared Help menu to a main or plot window.

    """
    help_menu = window.menuBar().addMenu("&Help")

    quick_start_action = qtw.QAction("&Quick Start", window)
    quick_start_action.setObjectName("quickStartHelpAction")
    quick_start_action.setShortcut("F1")
    quick_start_action.setShortcutContext(QtCore.Qt.WindowShortcut)
    quick_start_action.setStatusTip("Show the basic qPlot workflow")
    quick_start_action.triggered.connect(lambda: show_quick_start(window))
    help_menu.addAction(quick_start_action)

    shortcuts_action = qtw.QAction("&Keyboard Shortcuts", window)
    shortcuts_action.setObjectName("keyboardShortcutsHelpAction")
    shortcuts_action.setStatusTip("Show qPlot keyboard shortcuts")
    shortcuts_action.triggered.connect(lambda: show_keyboard_shortcuts(window))
    help_menu.addAction(shortcuts_action)

    help_menu.addSeparator()

    copy_log_path_action = qtw.QAction("Copy &Diagnostic Log Path", window)
    copy_log_path_action.setObjectName("copyDiagnosticLogPathAction")
    copy_log_path_action.setStatusTip("Copy the qPlot diagnostic log file path")
    copy_log_path_action.triggered.connect(lambda: copy_diagnostic_log_path(window))
    help_menu.addAction(copy_log_path_action)

    return help_menu


def show_quick_start(parent=None):
    """
    Opens the quick-start help dialog.

    """
    return _show_help_dialog(
        parent,
        "qPlot Quick Start",
        QUICK_START_HTML,
        "qplotQuickStartDialog",
        )


def show_keyboard_shortcuts(parent=None):
    """
    Opens the keyboard-shortcuts help dialog.

    """
    return _show_help_dialog(
        parent,
        "qPlot Keyboard Shortcuts",
        KEYBOARD_SHORTCUTS_HTML,
        "qplotKeyboardShortcutsDialog",
        )


def copy_diagnostic_log_path(parent=None):
    """
    Copies qPlot's diagnostic log path to the clipboard.

    """
    path = str(default_log_file())
    qtw.QApplication.clipboard().setText(path)
    if parent is not None and hasattr(parent, "show_status"):
        parent.show_status(f"Copied diagnostic log path: {path}", 5000)
    return path


def _show_help_dialog(parent, title, html, object_name):
    dialog = qtw.QDialog(parent)
    dialog.setObjectName(object_name)
    dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
    dialog.setWindowTitle(title)
    dialog.resize(640, 520)

    layout = qtw.QVBoxLayout(dialog)
    browser = qtw.QTextBrowser()
    browser.setObjectName("qplotHelpBrowser")
    browser.setOpenExternalLinks(True)
    browser.setHtml(html)
    browser.setMinimumSize(520, 360)
    layout.addWidget(browser)

    buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.Close)
    buttons.rejected.connect(dialog.close)
    layout.addWidget(buttons)

    _remember_help_dialog(parent, dialog)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog


def _remember_help_dialog(parent, dialog):
    if parent is None:
        dialogs = _OPEN_HELP_DIALOGS
    else:
        dialogs = getattr(parent, "_help_dialogs", None)
        if dialogs is None:
            dialogs = []
            parent._help_dialogs = dialogs

    dialogs.append(dialog)

    def forget_dialog(*_args):
        if dialog in dialogs:
            dialogs.remove(dialog)

    dialog.destroyed.connect(forget_dialog)
