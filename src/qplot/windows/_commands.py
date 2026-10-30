from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from html import escape

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from PyQt6.QtGui import QKeySequence

from ._shortcuts import key_sequences, platform_key_sequences, standard_key_sequences

ShortcutSource = (
    str
    | QKeySequence
    | Sequence[str | QKeySequence]
    | Callable[[], Sequence[str | QKeySequence]]
)


@dataclass(frozen=True)
class CommandSpec:
    """
    Shared metadata for a user-facing command.

    The registry keeps labels, shortcuts, status text, and shortcut-help text in
    one place so menus, tooltips, and help dialogs do not drift apart.

    """

    command_id: str
    text: str
    status_tip: str = ""
    shortcuts: ShortcutSource = ()
    shortcut_context: QtCore.Qt.ShortcutContext = (
        QtCore.Qt.ShortcutContext.WindowShortcut
    )
    object_name: str | None = None
    help_section: str | None = None
    help_shortcut: str | None = None
    help_text: str | None = None
    show_shortcut_in_context_menu: bool = True

    def resolved_shortcuts(self) -> list[QKeySequence]:
        """
        Returns this command's active shortcuts for the current platform.

        """
        shortcuts = self.shortcuts() if callable(self.shortcuts) else self.shortcuts
        if isinstance(shortcuts, (str, QKeySequence)):
            return key_sequences([shortcuts])
        return key_sequences(shortcuts)

    def shortcut_display_text(self) -> str:
        """
        Returns a native display string for the active shortcuts.

        """
        return shortcut_display_text(self.resolved_shortcuts())

    def help_row(self) -> tuple[str, str] | None:
        """
        Returns a `(shortcut, description)` row for shortcut help.

        """
        shortcut = self.help_shortcut or self.shortcut_display_text()
        if not shortcut:
            return None
        return shortcut, self.help_text or self.status_tip


def _standard_shortcuts(
        standard_key: QKeySequence.StandardKey,
        fallback: Sequence[str | QKeySequence],
        ) -> Callable[[], list[QKeySequence]]:
    return lambda: standard_key_sequences(standard_key, fallback)


def _platform_shortcuts(
        *,
        mac: Sequence[str | QKeySequence] | None = None,
        windows: Sequence[str | QKeySequence] | None = None,
        other: Sequence[str | QKeySequence] | None = None,
        ) -> Callable[[], list[QKeySequence]]:
    return lambda: platform_key_sequences(mac=mac, windows=windows, other=other)


COMMANDS: dict[str, CommandSpec] = {
    "help.quick_start": CommandSpec(
        "help.quick_start",
        "&Quick Start",
        "Show the basic qPlot workflow",
        "F1",
        object_name="quickStartHelpAction",
        help_section="Main and All Plot Windows",
    ),
    "database.load": CommandSpec(
        "database.load",
        "&Load Database...",
        "Load a QCoDeS database",
        "Ctrl+L",
        help_section="Main Window",
    ),
    "database.close": CommandSpec(
        "database.close",
        "&Close Database",
        "Close the current database and its plot windows",
        object_name="closeDatabaseAction",
    ),
    "window.refresh": CommandSpec(
        "window.refresh",
        "&Refresh",
        "Refresh the current window",
        "R",
        help_section="Main and All Plot Windows",
    ),
    "window.close": CommandSpec(
        "window.close",
        "&Close Window",
        "Close the current qPlot window",
        _standard_shortcuts(QKeySequence.StandardKey.Close, ["Ctrl+W"]),
        help_section="Main and All Plot Windows",
        help_shortcut="Ctrl+W / Cmd+W",
    ),
    "app.quit": CommandSpec(
        "app.quit",
        "&Quit qPlot",
        "Close the database and quit qPlot",
        _standard_shortcuts(QKeySequence.StandardKey.Quit, ["Ctrl+Q"]),
        QtCore.Qt.ShortcutContext.ApplicationShortcut,
        help_section="Main and All Plot Windows",
        help_shortcut="Ctrl+Q / Cmd+Q",
    ),
    "preferences.open": CommandSpec(
        "preferences.open",
        "&Preferences...",
        "Open qPlot preferences",
        "Ctrl+,",
        help_section="Main and All Plot Windows",
    ),
    "window.minimize": CommandSpec(
        "window.minimize",
        "&Minimize",
        "Minimize this window",
        _platform_shortcuts(mac=["Ctrl+M"], windows=["Alt+Space, N"]),
        help_section="Main and All Plot Windows",
        help_shortcut="Ctrl+M / Alt+Space, N",
    ),
    "window.maximize_restore": CommandSpec(
        "window.maximize_restore",
        "Ma&ximize / Restore",
        "Maximize or restore this window",
        _platform_shortcuts(windows=["Alt+Space, X", "Alt+Space, R"]),
        help_section="Main and All Plot Windows",
        help_shortcut="Alt+Space, X / Alt+Space, R",
        help_text="Maximize or restore the current window on Windows",
    ),
    "window.full_screen": CommandSpec(
        "window.full_screen",
        "&Full Screen",
        "Enter or leave full screen",
        lambda: standard_key_sequences(
            QKeySequence.StandardKey.FullScreen,
            platform_key_sequences(
                mac=["Ctrl+Meta+F"],
                windows=["F11", "Alt+Enter"],
            ),
        ),
        help_section="Main and All Plot Windows",
        help_shortcut="Ctrl+Cmd+F / F11",
    ),
    "copy.selection": CommandSpec(
        "copy.selection",
        "Copy Selection",
        "Copy selected cells or rows in the run details pane",
        _standard_shortcuts(QKeySequence.StandardKey.Copy, ["Ctrl+C"]),
        QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut,
        help_section="Main Window — Run Details Focused",
        help_shortcut="Ctrl+C / Cmd+C",
    ),
    "copy.cell": CommandSpec(
        "copy.cell",
        "Copy Cell",
        "Copy the current cell or value in the run details pane",
        "Ctrl+Shift+C",
        QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut,
        help_section="Main Window — Run Details Focused",
        help_shortcut="Ctrl+Shift+C / Cmd+Shift+C",
    ),
    "context.show": CommandSpec(
        "context.show",
        "Show Context Menu",
        "Open the focused widget's context menu",
        "Shift+F10",
        help_section="Context Menus",
    ),
    "database.open_folder": CommandSpec(
        "database.open_folder",
        "Open Database &Folder",
        "Open the current database folder",
        "Ctrl+Shift+D",
        help_section="Main Window",
    ),
    "testdata.open_bundled_folder": CommandSpec(
        "testdata.open_bundled_folder",
        "Open &Bundled CSV Folder",
        "Open the installed folder containing the bundled test-data CSV specifications",
        object_name="openBundledTestdataFolderAction",
    ),
    "testdata.create_csv": CommandSpec(
        "testdata.create_csv",
        "Create Example &CSV...",
        "Create an example CSV specification for generated test databases",
        object_name="createTestDatabaseCsvAction",
    ),
    "testdata.export_collection": CommandSpec(
        "testdata.export_collection",
        "Export CSV &Collection...",
        "Export the cumulative collection of test-database CSV specifications",
        object_name="exportTestDatabaseCsvCollectionAction",
    ),
    "testdata.generate_database": CommandSpec(
        "testdata.generate_database",
        "Generate &Database from CSV...",
        "Generate a QCoDeS test database from a CSV specification",
        object_name="generateTestDatabaseAction",
    ),
    "window.main_front_back": CommandSpec(
        "window.main_front_back",
        "Main Window &Front/Back",
        "Bring the main window to front, or behind the graph windows",
        "Ctrl+Shift+M",
        help_section="Main and All Plot Windows",
    ),
    "run.plot_entered": CommandSpec(
        "run.plot_entered",
        "Plot Entered Run and Measurement",
        "Plot the requested run and measurement",
        "Ctrl+Return",
        help_section="Main Window",
    ),
    "run.plot_selected_all": CommandSpec(
        "run.plot_selected_all",
        "Plot All Measurements in Selected Run",
        "Plot all measurements in the selected run",
        "Ctrl+Shift+Return",
        help_section="Main Window",
    ),
    "run.plot_selected_measurements": CommandSpec(
        "run.plot_selected_measurements",
        "Plot Measurements 1 to 9 in Selected Run",
        "Plot measurements 1 to 9 in the selected run",
        help_section="Main Window",
        help_shortcut="Ctrl+1 to Ctrl+9",
    ),
    "plots.close_all": CommandSpec(
        "plots.close_all",
        "Close All &Plot Windows",
        "Close all plot windows",
        "Ctrl+Shift+W",
        help_section="Main and All Plot Windows",
    ),
    "plot.autoscale": CommandSpec(
        "plot.autoscale",
        "Autoscale",
        "Return all plot axes to autoscale mode",
        "Ctrl+0",
        help_section="All Plot Windows (1D, 2D, and Cuts)",
    ),
    "plot.copy_image": CommandSpec(
        "plot.copy_image",
        "&Copy Plot Image",
        "Copy the plot image using the selected copy format or resolution",
        _standard_shortcuts(QKeySequence.StandardKey.Copy, ["Ctrl+C"]),
        object_name="copyPlotImageAction",
        help_section="All Plot Windows (1D, 2D, and Cuts)",
        help_shortcut="Ctrl+C / Cmd+C",
    ),
    "plot.print": CommandSpec(
        "plot.print",
        "&Print Plot...",
        "Print the visible plot area",
        _standard_shortcuts(QKeySequence.StandardKey.Print, ["Ctrl+P"]),
        object_name="printPlotAction",
        help_section="All Plot Windows (1D, 2D, and Cuts)",
        help_shortcut="Ctrl+P / Cmd+P",
    ),
    "plot.export": CommandSpec(
        "plot.export",
        "&Export Plot...",
        "Open the plot export dialog",
        "Ctrl+E",
        object_name="exportPlotAction",
        help_section="All Plot Windows (1D, 2D, and Cuts)",
        help_text="Export the plot",
    ),
    "plot.toggle_operations": CommandSpec(
        "plot.toggle_operations",
        "View data operations",
        "Show or hide the data operations panel",
    ),
    "toolbar.refresh": CommandSpec(
        "toolbar.refresh",
        "Refresh and status",
        "Show or hide the refresh and status toolbar",
        "Ctrl+Alt+R",
        help_section="All Plot Windows (1D, 2D, and Cuts)",
    ),
    "toolbar.coordinates": CommandSpec(
        "toolbar.coordinates",
        "Coordinates",
        "Show or hide the coordinates toolbar",
        "Ctrl+Alt+C",
        help_section="All Plot Windows (1D, 2D, and Cuts)",
    ),
    "toolbar.axis_control": CommandSpec(
        "toolbar.axis_control",
        "Cut controls",
        "Show or hide the cut controls panel",
        "Ctrl+Alt+A",
        help_section="Cut Windows",
    ),
    "toolbar.operations": CommandSpec(
        "toolbar.operations",
        "Data operations",
        "Show or hide the data operations panel",
        "Ctrl+Alt+O",
        help_section="All Plot Windows (1D, 2D, and Cuts)",
    ),
    "plot.snap_to_trace": CommandSpec(
        "plot.snap_to_trace",
        "Snap to &Trace",
        "Toggle snapping the coordinate readout to the nearest line-trace point (including overlays)",
        "S",
        help_section="All Plot Windows (1D, 2D, and Cuts)",
    ),
    "heatmap.autoscale_color": CommandSpec(
        "heatmap.autoscale_color",
        "Autoscale Color",
        "Autoscale the colour range",
        "C",
        help_section="2D Heatmap Windows",
    ),
    "heatmap.horizontal_cut": CommandSpec(
        "heatmap.horizontal_cut",
        "Horizontal Cut",
        "Open a horizontal cut",
        "H",
        help_section="2D Heatmap Windows",
    ),
    "heatmap.vertical_cut": CommandSpec(
        "heatmap.vertical_cut",
        "Vertical Cut",
        "Open a vertical cut",
        "V",
        help_section="2D Heatmap Windows",
    ),
    "plot.clear_selection": CommandSpec(
        "plot.clear_selection",
        "Clear Rectangular Selection",
        "Clear the rectangular plot selection, if present",
        help_section="All Plot Windows (1D, 2D, and Cuts)",
        help_shortcut="Escape",
    ),
    "preview.open": CommandSpec(
        "preview.open",
        "Open Preview",
        "Open the focused measurement preview as a plot",
        help_section="Main Window — Measurement Preview Focused",
        help_shortcut="Return / Enter / Space",
    ),
    "preview.context": CommandSpec(
        "preview.context",
        "Preview Context Menu",
        "Open the focused measurement preview's context menu",
        help_section="Main Window — Measurement Preview Focused",
        help_shortcut="Shift+F10 / Menu key",
    ),
    "heatmap.move_cut": CommandSpec(
        "heatmap.move_cut",
        "Move Selected Cut",
        "Move the selected cut cursor by one pixel (a cut must be open)",
        help_section="2D Heatmap Windows",
        help_shortcut="Arrow keys",
    ),
}

SHORTCUT_HELP_SECTIONS = (
    "Main and All Plot Windows",
    "Main Window",
    "Main Window — Run Details Focused",
    "Main Window — Measurement Preview Focused",
    "All Plot Windows (1D, 2D, and Cuts)",
    "Cut Windows",
    "2D Heatmap Windows",
    "Context Menus",
)


def command_spec(command_id: str) -> CommandSpec:
    """
    Returns a registered command specification.

    """
    return COMMANDS[command_id]


def plot_measurement_command_spec(index: int) -> CommandSpec:
    """
    Returns the indexed run-measurement shortcut command.

    """
    number = index + 1
    return CommandSpec(
        f"run.plot_selected_measurement_{number}",
        f"Plot Measurement {number} in Selected Run",
        f"Plot measurement {number} in the selected run",
        f"Ctrl+{number}",
    )


def toolbar_toggle_command_spec(title: str) -> CommandSpec | None:
    """
    Returns the command used for a plot toolbar or dock toggle action.

    """
    by_title = {
        "Refresh and status": "toolbar.refresh",
        "Coordinates": "toolbar.coordinates",
        "Cut controls": "toolbar.axis_control",
        "Data operations": "toolbar.operations",
    }
    command_id = by_title.get(title)
    if command_id is None:
        return None
    return command_spec(command_id)


def create_action(
        command: str | CommandSpec,
        parent: qtw.QWidget,
        *,
        text: str | None = None,
        object_name: str | None = None,
        status_tip: str | None = None,
        checkable: bool = False,
        ) -> QtGui.QAction:
    """
    Creates a QAction and applies the shared command metadata.

    """
    spec = command_spec(command) if isinstance(command, str) else command
    if status_tip is not None:
        spec = replace(spec, status_tip=status_tip)
    action = QtGui.QAction(text or spec.text, parent)
    action.setCheckable(checkable)
    configure_action(action, spec, object_name=object_name)
    return action


def configure_action(
        action: QtGui.QAction,
        command: str | CommandSpec,
        *,
        object_name: str | None = None,
        add_to: qtw.QWidget | None = None,
        set_text: bool = False,
        set_tooltip: bool = True,
        ) -> QtGui.QAction:
    """
    Applies command shortcuts, status text, tooltip, and object name to an action.

    """
    spec = command_spec(command) if isinstance(command, str) else command

    if set_text:
        action.setText(spec.text)

    shortcuts = spec.resolved_shortcuts()
    if shortcuts:
        action.setShortcuts(shortcuts)

    action.setShortcutContext(spec.shortcut_context)
    if hasattr(action, "setShortcutVisibleInContextMenu"):
        action.setShortcutVisibleInContextMenu(spec.show_shortcut_in_context_menu)

    if spec.status_tip:
        action.setStatusTip(spec.status_tip)
        if set_tooltip:
            tooltip = spec.status_tip
            shortcut_text = shortcut_display_text(shortcuts)
            if shortcut_text:
                tooltip = f"{tooltip} ({shortcut_text})"
            action.setToolTip(tooltip)

    resolved_object_name = object_name or spec.object_name
    if resolved_object_name:
        action.setObjectName(resolved_object_name)

    if add_to is not None and action not in add_to.actions():
        add_to.addAction(action)

    return action


def shortcut_display_text(shortcuts: Iterable[QKeySequence]) -> str:
    """
    Formats shortcuts for status tips and generated help.

    """
    return " / ".join(
        shortcut.toString(QKeySequence.SequenceFormat.NativeText)
        for shortcut in shortcuts
        if not shortcut.isEmpty()
    )


def shortcut_help_html() -> str:
    """
    Builds the keyboard shortcut help HTML from registered command metadata.

    """
    parts = [
        "<h2>Keyboard Shortcuts</h2>",
        "<p>Use these shortcuts in the active window shown by each heading. "
        "Cut windows are the 1D plots opened from a 2D heatmap. "
        "Shortcuts for focused widgets require keyboard focus in that widget.</p>",
        "<p>Keys use the current platform's notation. On macOS, Qt displays "
        "the Command key as ⌘ and the Control key as ⌃. "
        "Text fields and other controls may handle typing, copying, or arrow "
        "keys themselves. Quit also works while a dialog is open.</p>",
    ]
    for section in SHORTCUT_HELP_SECTIONS:
        rows: list[tuple[str, str]] = []
        for spec in COMMANDS.values():
            if spec.help_section != section:
                continue
            row = spec.help_row()
            if row is not None:
                rows.append(row)
        if not rows:
            continue

        parts.append(f"<h3>{escape(section)}</h3>")
        parts.append('<table width="100%" cellspacing="4" cellpadding="3">')
        for shortcut, description in rows:
            parts.append(
                "  <tr>"
                f'<td width="30%" valign="top"><b>{escape(shortcut)}</b></td>'
                f'<td width="70%" valign="top">{escape(description)}</td>'
                "</tr>"
            )
        parts.append("</table>")

    parts.append(
        "<p><b>Context menus:</b> Shift+F10 opens the run-list context menu "
        "in the main window or the plot context menu in a plot window. "
        "With a menu open, press an entry's underlined letter or number "
        "to activate it. Standard Qt controls also support their usual "
        "text editing and keyboard navigation keys.</p>"
    )
    return "\n".join(parts)


def command_with_status(command_id: str, status_tip: str) -> CommandSpec:
    """
    Returns a registered command with a context-specific status tip.

    """
    spec = command_spec(command_id)
    return replace(spec, status_tip=status_tip)
