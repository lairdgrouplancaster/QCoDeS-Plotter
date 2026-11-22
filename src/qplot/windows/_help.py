import sys
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any, cast

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

import qplot
from qplot._icons import application_icon
from qplot._metadata import version_info
from qplot.diagnostics import default_log_file

from ._commands import (
    COMMANDS,
    MOUSE_AND_SELECTION_HELP,
    SHORTCUT_HELP_SECTIONS,
    create_action,
    shortcut_help_html,
)
from ._small_dialogs import (
    close_default,
    copy_to_clipboard,
    dialog_layout,
    resize_dialog,
)

_OPEN_HELP_DIALOGS: list[qtw.QDialog] = []


class _HelpBrowser(qtw.QTextBrowser):
    """Keep document links legible when the application palette changes."""

    def setHtml(self, html: str | None) -> None:
        self.ensurePolished()
        self._source_html = html
        self._dark = self.palette().base().color().lightness() < 128
        link_colour = "#74cfff" if self._dark else "#075aa8"
        document = cast(QtGui.QTextDocument, self.document())
        document.setDefaultStyleSheet(f"a {{ color: {link_colour}; }}")
        super().setHtml(html)

    def changeEvent(self, event: QtCore.QEvent | None) -> None:
        super().changeEvent(event)
        if (event is not None and event.type() in (
            QtCore.QEvent.Type.PaletteChange, QtCore.QEvent.Type.StyleChange,
        ) and hasattr(self, "_source_html")
                and self._dark != (self.palette().base().color().lightness() < 128)):
            bar = self.verticalScrollBar()
            position = bar.value() if bar is not None else 0
            self.setHtml(self._source_html)
            if bar is not None:
                bar.setValue(position)


QUICK_START_HTML = """
<h2>Quick start</h2>
<ol>
  <li><b>Load a database.</b> Drop a QCoDeS .db file onto the database field,
      or use <b>File -&gt; Load Database...</b>.</li>
  <li><b>Select a run.</b> Click a row in the run table to see details,
      parameters, preview images, and metadata.</li>
  <li><b>Open a plot.</b> Double-click the measurement preview. You can also
      select a run and press Enter to plot its measurements.</li>
</ol>
<p>Use the mouse wheel to zoom. Double-click an axis to adjust its scale.
Find more gestures in <b>Help → Keyboard and mouse controls</b>.</p>
"""


def add_help_menu(
    window: qtw.QMainWindow, *, include_desktop_integration: bool = False,
) -> qtw.QMenu:
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

    shortcuts_action = QtGui.QAction("&Keyboard and mouse controls", window)
    shortcuts_action.setObjectName("keyboardShortcutsHelpAction")
    shortcuts_action.setStatusTip("Show all qPlot keyboard shortcuts and where they work")
    shortcuts_action.triggered.connect(lambda: show_keyboard_shortcuts(window))
    help_menu.addAction(shortcuts_action)

    help_menu.addSeparator()

    copy_log_path_action = QtGui.QAction("Copy Path to &Diagnostic Log", window)
    copy_log_path_action.setObjectName("copyDiagnosticLogPathAction")
    copy_log_path_action.setStatusTip("Copy the qPlot diagnostic log file path")
    copy_log_path_action.triggered.connect(lambda: copy_diagnostic_log_path(window))
    help_menu.addAction(copy_log_path_action)

    file_manager = (
        "Finder" if sys.platform == "darwin"
        else "File Explorer" if sys.platform == "win32"
        else "File Manager"
    )
    show_log_action = QtGui.QAction(f"&Show Diagnostic Log in {file_manager}", window)
    show_log_action.setObjectName("showDiagnosticLogAction")
    show_log_action.setStatusTip("Show the qPlot diagnostic log in your file manager")
    show_log_action.triggered.connect(lambda: show_diagnostic_log(window))
    help_menu.addAction(show_log_action)

    help_menu.addSeparator()
    update_action = QtGui.QAction("Check for &updates…", window)
    update_action.setObjectName("checkForUpdatesAction")
    update_action.setStatusTip("Check PyPI for a newer qPlot release")
    update_action.triggered.connect(lambda: check_for_updates(window))
    help_menu.addAction(update_action)

    if include_desktop_integration:
        desktop_action = QtGui.QAction("Desktop integration…", window)
        desktop_action.setObjectName("desktopIntegrationAction")
        desktop_action.triggered.connect(lambda: show_desktop_integration(window))
        settings_menu = next(
            (action.menu() for action in menu_bar.actions()
             if action.text().replace("&", "") == "Settings"),
            None,
        )
        if settings_menu is None:
            settings_menu = qtw.QMenu("&Settings", menu_bar)
            menu_bar.addMenu(settings_menu)
        settings_menu.addAction(desktop_action)

    about_action = QtGui.QAction("&About qPlot", window)
    about_action.setObjectName("aboutQplotAction")
    # Keep this window-owned action in Help. Cocoa's AboutRole rewrites its
    # title to "About python", while ApplicationSpecificRole accumulates
    # duplicate application-menu entries when switching between windows.
    about_action.setMenuRole(QtGui.QAction.MenuRole.NoRole)
    about_action.setStatusTip("Show qPlot version and build information")
    about_action.triggered.connect(lambda: show_about(window))
    help_menu.addAction(about_action)

    return help_menu


def check_for_updates(window):
    from ._updates import check_for_updates as check

    return check(window)


def show_desktop_integration(parent: qtw.QWidget | None = None) -> qtw.QDialog:
    from ._desktop_integration import DesktopIntegrationDialog

    dialog = DesktopIntegrationDialog(parent)
    dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
    _remember_help_dialog(parent, dialog)
    dialog.show()
    return dialog


def show_about(parent: qtw.QWidget | None = None) -> qtw.QDialog:
    """Show the running installation and selectable build information."""
    info = version_info()
    heading = (
        '<table cellspacing="0" cellpadding="0"><tr>'
        '<td width="80" valign="middle"><img src="qplot:application-icon" width="64" height="64" '
        'alt="qPlot application icon"></td>'
        '<td valign="middle"><h2 style="margin: 0">qPlot</h2>'
        '<p style="margin-top: 8px; margin-bottom: 0">A viewer for QCoDeS measurement data.</p>'
        '</td></tr></table>'
    )
    rows = [f"<p><b>Version:</b> {escape(str(info['version']))}</p>"]
    if "released" in info:
        build = "Release" if info["released"] else "Unreleased development build"
        if info.get("dirty"):
            build += " (local changes)"
        rows.append(f"<p><b>Build:</b> {build}</p>")
    elif info.get("checkout"):
        rows.append("<p><b>Build:</b> Source checkout</p>")
    if info.get("commit"):
        rows.append(f"<p><b>Commit:</b> {escape(str(info['commit']))}</p>")
        try:
            committed = datetime.fromisoformat(str(info.get("commit_date", "")))
        except ValueError:
            committed = None
        if committed is not None and committed.tzinfo is not None:
            date = committed.astimezone(UTC).strftime("%d %B %Y, %H:%M:%S UTC")
            rows.append(f"<p><b>Committed:</b> {escape(date)}</p>")
    if info.get("pull_request"):
        rows.append(f"<p><b>Pull request:</b> #{escape(str(info['pull_request']))}</p>")
    # Keep the environment path rather than resolving interpreter symlinks to
    # a shared base installation. Editable installs can load qPlot elsewhere.
    locations = (
        ("Python environment", Path(sys.prefix).absolute()),
        ("qPlot location", Path(qplot.__file__).absolute().parent),
    )
    location_rows = []
    for label, folder in locations:
        url = QtCore.QUrl.fromLocalFile(str(folder)).toString(
            QtCore.QUrl.ComponentFormattingOption.FullyEncoded,
        )
        location_rows.append(
            f'<p><b>{label}:</b> &nbsp; <a href="{escape(url)}" '
            f'title="Open the {label} folder">Open folder</a><br>'
            f'{escape(str(folder))}</p>'
        )
    html = (
        '<style>p { margin-top: 6px; margin-bottom: 6px; }</style>'
        + heading
        + rows[0]
        + '<p><a href="https://github.com/lairdgrouplancaster/QCoDeS-Plotter">View project on GitHub</a></p>'
    )
    dialog = _show_help_dialog(
        parent, "About qPlot", html, "qplotAboutDialog", size=(560, 250), icon=application_icon(),
    )
    dialog.setProperty("qplotUpdatePrefixHtml", html + "<hr>")
    layout = cast(qtw.QVBoxLayout, dialog.layout())
    details_row = qtw.QHBoxLayout()
    details_toggle = qtw.QToolButton()
    details_toggle.setText("Technical details")
    details_toggle.setCheckable(True)
    details_toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow)
    details_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    details_row.addWidget(details_toggle)
    details_row.addStretch(1)
    copy = qtw.QPushButton("Copy details")
    copy.setObjectName("aboutCopyDetails")
    copy.hide()
    details_row.addWidget(copy)
    layout.insertLayout(1, details_row)
    technical = _HelpBrowser(dialog)
    technical.setAccessibleName("Technical details")
    technical.setObjectName("aboutTechnicalDetails")
    technical.setTabChangesFocus(True)
    technical.setWordWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    technical.setHtml("".join(rows + location_rows))
    technical.hide()
    layout.insertWidget(2, technical)
    copy.clicked.connect(lambda: copy_to_clipboard(technical.toPlainText()))

    def toggle_details(opened):
        technical.setVisible(opened)
        copy.setVisible(opened)
        details_toggle.setArrowType(QtCore.Qt.ArrowType.DownArrow if opened else QtCore.Qt.ArrowType.RightArrow)
        if opened:
            resize_dialog(dialog, 600, 440)

    details_toggle.toggled.connect(toggle_details)

    def open_link(url):
        if not QtGui.QDesktopServices.openUrl(url) and url.isLocalFile():
            qtw.QMessageBox.warning(dialog, "Could not open folder",
                                    "Copy the path from Technical details and open it in your file manager.\n\n"
                                    + url.toLocalFile())

    for browser in (technical, dialog.findChild(qtw.QTextBrowser, "qplotHelpBrowser")):
        browser.setOpenLinks(False)
        browser.setOpenExternalLinks(False)
        browser.anchorClicked.connect(open_link)

    def check_about_updates():
        from ._updates import update_checker

        owner = parent
        while owner is not None and not hasattr(owner, "config"):
            owner = owner.parentWidget()
        if owner is None:
            from qplot.configuration.config import config

            settings = config()
        else:
            settings = cast(Any, owner).config
        update_checker().show_in_about(dialog, settings)

    from ._updates import UpdateChecker

    UpdateChecker.add_controls(dialog, check_about_updates, about=True)
    return dialog


def show_quick_start(parent: qtw.QWidget | None = None) -> qtw.QDialog:
    """
    Opens the quick-start help dialog.

    """
    return _show_help_dialog(
        parent,
        "qPlot quick start",
        QUICK_START_HTML,
        "qplotQuickStartDialog",
        size=(520, 285),
        )


def show_keyboard_shortcuts(parent: qtw.QWidget | None = None) -> qtw.QDialog:
    """
    Opens the keyboard-shortcuts help dialog.

    """
    font = QtGui.QFont(parent.font() if parent is not None else qtw.QApplication.font())
    font.setBold(True)
    metrics = QtGui.QFontMetrics(font)
    labels = [row[0]
        for command in COMMANDS.values()
        if (row := command.help_row()) is not None
    ]
    labels.extend(label for rows in MOUSE_AND_SELECTION_HELP.values() for label, _ in rows)
    column_width = max(metrics.horizontalAdvance(label) for label in labels) + 24
    plot_kind = getattr(parent, "operation_kind", None)
    applicable = ["Main and All Plot Windows", "Context Menus"]
    if plot_kind:
        applicable.insert(0, "All Plot Windows")
        if plot_kind == "plot2d":
            applicable.insert(0, "2D Heatmap Windows")
        if plot_kind == "sweeper" or hasattr(parent, "cut_toolbar"):
            applicable.insert(0, "Cut Windows")
        if hasattr(parent, "slice_toolbar"):
            applicable.insert(0, "3D Slice Windows")
    else:
        applicable = [s for s in SHORTCUT_HELP_SECTIONS if s.startswith("Main Window")] + applicable
    ordered = applicable + [s for s in SHORTCUT_HELP_SECTIONS if s not in applicable]
    dialog = _show_help_dialog(
        parent,
        "qPlot keyboard and mouse controls",
        shortcut_help_html(column_width, sections=ordered),
        "qplotKeyboardShortcutsDialog",
        size=(max(800, column_width + 460), 600),
        )
    search = qtw.QLineEdit(dialog)
    search.setObjectName("shortcutSearch")
    search.setPlaceholderText("Find an action or gesture…")
    search.setAccessibleName("Find an action or gesture")
    search.setClearButtonEnabled(True)
    find = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Find, dialog)
    find.activated.connect(search.setFocus)
    search.setToolTip(f"Find an action or gesture ({find.key().toString(QtGui.QKeySequence.SequenceFormat.NativeText)}).")
    scope = qtw.QComboBox(dialog)
    scope.setAccessibleName("Shortcut reference scope")
    scope.addItems(["All windows", "This window"])
    filters = qtw.QHBoxLayout()
    filters.addWidget(search, 1)
    filters.addWidget(scope)
    cast(qtw.QVBoxLayout, dialog.layout()).insertLayout(0, filters)
    browser = dialog.findChild(qtw.QTextBrowser, "qplotHelpBrowser")
    assert browser is not None

    def filter_rows():
        sections = applicable if scope.currentIndex() else ordered
        browser.setHtml(shortcut_help_html(column_width, query=search.text(), sections=sections))

    search.textChanged.connect(filter_rows)
    scope.currentIndexChanged.connect(filter_rows)
    return dialog


def copy_diagnostic_log_path(parent: Any | None = None) -> str:
    """
    Copies qPlot's diagnostic log path to the clipboard.

    """
    path = str(default_log_file())
    copy_to_clipboard(path)
    if parent is not None and hasattr(parent, "show_status"):
        parent.show_status(f"Copied diagnostic log path: {path}", 5000)
    return path


def show_diagnostic_log(parent: Any | None = None) -> bool:
    """Show the diagnostic log in the system file manager."""
    from ._file_manager import reveal_file_in_file_manager

    path = str(default_log_file())
    opened = reveal_file_in_file_manager(path)
    if opened:
        if parent is not None and hasattr(parent, "show_status"):
            parent.show_status(f"Showing diagnostic log: {path}", 5000)
    elif parent is not None and hasattr(parent, "show_error"):
        parent.show_error(
            "Show diagnostic log failed",
            "Could not open the diagnostic log location in the file manager.",
            path,
        )
    return opened


def _show_help_dialog(
        parent: qtw.QWidget | None,
        title: str,
        html: str,
        object_name: str,
        *,
        size: tuple[int, int] = (640, 520),
        icon: QtGui.QIcon | None = None,
        ) -> qtw.QDialog:
    dialog = qtw.QDialog(parent)
    dialog.setObjectName(object_name)
    dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
    dialog.setWindowTitle(title)
    layout = dialog_layout(dialog)
    browser = _HelpBrowser(dialog)
    browser.setObjectName("qplotHelpBrowser")
    browser.setOpenExternalLinks(True)
    browser.setAccessibleName(title)
    browser.setTabChangesFocus(True)
    if icon is not None:
        cast(QtGui.QTextDocument, browser.document()).addResource(
            QtGui.QTextDocument.ResourceType.ImageResource,
            QtCore.QUrl("qplot:application-icon"),
            icon.pixmap(QtCore.QSize(64, 64), dialog.devicePixelRatioF()),
        )
    browser.setHtml(html)
    browser.setMinimumSize(280, 140)
    layout.addWidget(browser, 1)

    buttons = qtw.QDialogButtonBox(qtw.QDialogButtonBox.StandardButton.Close)
    if object_name == "qplotAboutDialog":
        desktop_button = qtw.QPushButton("Desktop integration…")
        buttons.addButton(desktop_button, qtw.QDialogButtonBox.ButtonRole.ActionRole)
        desktop_button.setObjectName("desktopIntegrationButton")
        desktop_button.clicked.connect(lambda: show_desktop_integration(dialog))
    buttons.rejected.connect(dialog.close)
    layout.addWidget(buttons)
    close_default(buttons)
    resize_dialog(dialog, *size)

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
