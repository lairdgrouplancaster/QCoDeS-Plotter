import os
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from PyQt6 import (
    QtCore,
    QtGui,
)
from PyQt6 import (
    QtWidgets as qtw,
)

from qplot.configuration.fonts import FONT_SIZE_KEY
from qplot.configuration.trace import TRACE_COLOUR_KEY, TRACE_STYLE_KEY, TRACE_WIDTH_KEY
from qplot.datahandling.complex_data import (
    COMPLEX_1D_KEY,
    COMPLEX_1D_OPTIONS,
    COMPLEX_COMPONENT_OPTIONS,
    COMPLEX_ND_KEY,
)
from qplot.tools.heatmap_geometry import MAX_AGGREGATED_HEATMAP_CELLS
from qplot.updates import CHECK_ON_LAUNCH_KEY, INCLUDE_PRERELEASES_KEY

from ._colorbar import _colorbar_colormaps
from ._colour_map_picker import ColourMapButton, ColourMapChooser
from ._commands import create_action
from ._config_persistence import persist_config_values
from ._csv_filenames import CSV_FILENAME_FORMAT_KEY, CSV_FILENAME_FORMAT_OPTIONS
from ._drawing_controls import (
    TRACE_COLOR_PALETTE,
    ColourSwatchCombo,
    DrawingSizeSpinBox,
    IconPreviewCombo,
    LineStyleCombo,
    colour_swatch_icon,
)
from ._plot_annotation import SHOW_SOURCE_ANNOTATION_KEY
from ._plot_tiling import PLOT_TILE_COLUMNS_KEY, PLOT_TILE_ROWS_KEY
from ._signal_blocking import blocked_signals
from ._small_dialogs import DialogTabs, confirm_action
from ._window_controls import (
    CONFIRM_CLOSE_ALL_KEY,
    CONFIRM_INCOMPATIBLE_AXES_KEY,
    CONFIRM_QUIT_KEY,
)

THEME_OPTIONS = (
    ("Light", "light"),
    ("Dark", "dark"),
    ("PyQt", "pyqt"),
    )


COPY_PLOT_IMAGE_RESOLUTION_KEY = "user_preference.copy_plot_image_resolution"
COPY_PLOT_IMAGE_RESOLUTION_SCREEN = "screen"
COPY_PLOT_IMAGE_RESOLUTION_300_DPI = "dpi_300"
COPY_PLOT_IMAGE_RESOLUTION_SVG = "svg"
COPY_PLOT_IMAGE_RESOLUTION_OPTIONS = (
    ("Screen resolution", COPY_PLOT_IMAGE_RESOLUTION_SCREEN),
    ("300 dpi", COPY_PLOT_IMAGE_RESOLUTION_300_DPI),
    ("Vector SVG", COPY_PLOT_IMAGE_RESOLUTION_SVG),
)

CURSOR_SIGNIFICANT_FIGURES_KEY = "user_preference.cursor_significant_figures"

COLORBAR_WIDTH_KEY = "user_preference.colorbar_width"
AXIS_TICK_WIDTH_KEY = "user_preference.axis_tick_width"
AXIS_MAJOR_TICK_COUNT_KEY = "user_preference.axis_major_tick_count"

_PREFERENCE_CONTROLS = (
    ("user_preference.theme", "themeCombo"),
    (FONT_SIZE_KEY, "fontSizeSpin"),
    (SHOW_SOURCE_ANNOTATION_KEY, "showSourceAnnotationCheck"),
    (CURSOR_SIGNIFICANT_FIGURES_KEY, "cursorSignificantFiguresSpin"),
    (COMPLEX_1D_KEY, "complex1dCombo"),
    (COMPLEX_ND_KEY, "complexNdCombo"),
    (COLORBAR_WIDTH_KEY, "colorbarWidthSpin"),
    ("user_preference.bar_colour", "defaultColourMapButton"),
    (TRACE_COLOUR_KEY, "defaultTraceColourCombo"),
    (TRACE_WIDTH_KEY, "defaultTraceWidthSpin"),
    (TRACE_STYLE_KEY, "defaultTraceStyleCombo"),
    (AXIS_TICK_WIDTH_KEY, "axisTickWidthSpin"),
    (AXIS_MAJOR_TICK_COUNT_KEY, "axisMajorTickCountSpin"),
    ("GUI.preview_size", "previewSizeSpin"),
    (PLOT_TILE_ROWS_KEY, "plotTileRowsSpin"),
    (PLOT_TILE_COLUMNS_KEY, "plotTileColumnsSpin"),
    (COPY_PLOT_IMAGE_RESOLUTION_KEY, "copyPlotImageResolutionCombo"),
    (CSV_FILENAME_FORMAT_KEY, "csvFilenameFormatCombo"),
    ("file.default_load_path", "defaultLoadPathEdit"),
    ("user_preference.default_refresh_rate", "refreshRateSpin"),
    (CONFIRM_CLOSE_ALL_KEY, "confirmCloseAllCheck"),
    (CONFIRM_INCOMPATIBLE_AXES_KEY, "confirmIncompatibleAxesCheck"),
    (CONFIRM_QUIT_KEY, "confirmQuitCheck"),
    ("runtime_settings.max_threads", "maxThreadsSpin"),
    ("runtime_settings.max_full_heatmap_points", "maxFullHeatmapPointsSpin"),
    ("runtime_settings.max_heatmap_grid_cells", "maxHeatmapGridCellsSpin"),
    ("runtime_settings.del_grace_period", "delGracePeriodSpin"),
    ("runtime_settings.cloud_sync_timeout", "cloudSyncTimeoutSpin"),
    (CHECK_ON_LAUNCH_KEY, "checkUpdatesOnLaunchCheck"),
    (INCLUDE_PRERELEASES_KEY, "includePrereleasesCheck"),
)
PREFERENCE_KEYS = tuple(key for key, _control_name in _PREFERENCE_CONTROLS)


class _PreferenceColourMapButton(ColourMapButton):
    """Preview a pending preference without saving until Preferences applies it."""

    valueChanged = QtCore.pyqtSignal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.clicked.connect(self._choose_map)

    def value(self):
        return self._name

    def set_colormap(self, name):
        super().set_colormap(name)
        self.setAccessibleName(f"Default colour map for new plots: {self.text()}. Choose colour map.")
        self.setToolTip(f"{self.text()}: choose the colour map used by new plots. Existing plots keep their own colour maps.")

    def setValue(self, name):
        if name != self._name:
            self.set_colormap(name)
            self.valueChanged.emit(name)

    def _choose_map(self):
        opening_map = self.value()
        dialog = ColourMapChooser(_colorbar_colormaps(), opening_map, self)
        dialog.picker.table.setToolTip("Select a colour map to preview it as the default for new plots.")
        dialog.picker.mapSelected.connect(self.setValue)
        accepted = False
        try:
            accepted = dialog.exec() == qtw.QDialog.DialogCode.Accepted
        finally:
            if not accepted:
                self.setValue(opening_map)
            dialog.deleteLater()


class _PreferenceTraceColourCombo(ColourSwatchCombo):
    """Keep custom colours as ordinary pending preference values."""

    def __init__(self):
        super().__init__("Default trace colour", preview_renderer=colour_swatch_icon)
        self._colour = TRACE_COLOR_PALETTE[0]
        for colour in TRACE_COLOR_PALETTE:
            self.addItem(colour_swatch_icon(colour), colour, colour)
        self.addItem("Custom", "custom")
        self.currentIndexChanged.connect(self._colour_changed)

    def set_colour(self, colour):
        with blocked_signals(self):
            index = self.findData(colour)
            if index < 0:
                index = self.count() - 1
                self.insertItem(index, colour_swatch_icon(colour), colour, colour)
            self.setCurrentIndex(index)
            self._colour = colour
        self.refresh_tooltip()

    def _colour_changed(self):
        colour = self.currentData()
        if colour == "custom":
            selected = qtw.QColorDialog.getColor(QtGui.QColor(self._colour), self, "Select default trace colour")
            self.set_colour(selected.name() if selected.isValid() else self._colour)
        elif colour is not None:
            self._colour = colour


_PreferenceControl = (
    qtw.QComboBox | qtw.QSpinBox | qtw.QDoubleSpinBox | qtw.QCheckBox | qtw.QLineEdit
    | _PreferenceColourMapButton
)


@dataclass(frozen=True)
class _PreferenceBinding:
    """Keep a preference's control, value conversion, and edit tracking together."""

    key: str
    control: _PreferenceControl

    def read(self) -> Any:
        control = self.control
        if isinstance(control, qtw.QComboBox):
            return control.currentData()
        if isinstance(control, qtw.QCheckBox):
            return control.isChecked()
        if isinstance(control, qtw.QLineEdit):
            return control.text().strip()
        return control.value()

    def write(self, value: Any) -> None:
        control = self.control
        if isinstance(control, _PreferenceTraceColourCombo):
            control.set_colour(value)
        elif isinstance(control, qtw.QComboBox):
            control.setCurrentIndex(max(control.findData(value), 0))
            if isinstance(control, IconPreviewCombo):
                control.refresh_tooltip()
        elif isinstance(control, qtw.QCheckBox):
            control.setChecked(bool(value))
        elif isinstance(control, qtw.QLineEdit):
            control.setText(str(value))
        elif isinstance(control, qtw.QSpinBox):
            control.setValue(int(value))
        elif isinstance(control, _PreferenceColourMapButton):
            control.setValue(value)
        else:
            control.setValue(float(value))

    def track_edits(self, on_edit) -> None:
        def mark_edited(_value):
            on_edit(self.key)

        control = self.control
        if isinstance(control, _PreferenceColourMapButton):
            control.valueChanged.connect(mark_edited)
        elif isinstance(control, (qtw.QSpinBox, qtw.QDoubleSpinBox)):
            control.valueChanged.connect(mark_edited)
            line_edit = control.lineEdit()
            if line_edit is not None:
                # The same rounded text is still an explicit edit even when
                # valueChanged remains silent.
                line_edit.textEdited.connect(mark_edited)
        elif isinstance(control, qtw.QComboBox):
            control.currentIndexChanged.connect(mark_edited)
        elif isinstance(control, qtw.QLineEdit):
            control.textChanged.connect(mark_edited)
        else:
            control.toggled.connect(mark_edited)


class _PreferencesPage(qtw.QScrollArea):
    """Let page contents grow vertically without imposing their full size."""

    def __init__(self, page):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(qtw.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(page)
        page.setAutoFillBackground(False)
        form = page.layout()
        assert form is not None
        form.setSizeConstraint(qtw.QLayout.SizeConstraint.SetNoConstraint)
        page.setMinimumWidth(0)
        page.setSizePolicy(qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Preferred)
        if isinstance(form, qtw.QFormLayout):
            form.setRowWrapPolicy(qtw.QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setFormAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        for child in page.findChildren(qtw.QWidget):
            if child.focusPolicy() != QtCore.Qt.FocusPolicy.NoFocus:
                child.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.FocusIn and isinstance(watched, qtw.QWidget):
            self.ensureWidgetVisible(watched)
        elif watched is self.widget() and event.type() == QtCore.QEvent.Type.LayoutRequest:
            self._reflow_form()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow_form()

    def _reflow_form(self):
        page = self.widget()
        if page is None:
            return
        form = page.layout()
        if not isinstance(form, qtw.QFormLayout):
            return
        margins = form.contentsMargins()
        viewport = self.viewport()
        assert viewport is not None
        available = viewport.width() - margins.left() - margins.right()
        labels = set()
        label_width = field_width = 0
        for row in range(form.rowCount()):
            label = form.itemAt(row, qtw.QFormLayout.ItemRole.LabelRole)
            field = form.itemAt(row, qtw.QFormLayout.ItemRole.FieldRole)
            if label is not None and field is not None:
                labels.add(label.widget())
                label_width = max(label_width, label.minimumSize().width())
                field_width = max(field_width, field.minimumSize().width())
        label_space = available - max(0, form.horizontalSpacing()) - field_width
        stack_rows = label_space < label_width
        for child in page.findChildren(qtw.QWidget, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly):
            # Wrap long labels within their column while leaving room for every field.
            width = label_space if child in labels and not stack_rows else available
            child.setMaximumWidth(max(1, width))
        policy = (qtw.QFormLayout.RowWrapPolicy.WrapAllRows if stack_rows
                  else qtw.QFormLayout.RowWrapPolicy.DontWrapRows)
        if form.rowWrapPolicy() != policy:
            form.setRowWrapPolicy(policy)

    def sizeHint(self):
        page = self.widget()
        assert page is not None
        return page.sizeHint().boundedTo(QtCore.QSize(600, 440))

    def minimumSizeHint(self):
        return QtCore.QSize(160, 80)


class _PreferencesLabel(qtw.QLabel):
    """Prefer a single line while still allowing native word wrapping."""

    def sizeHint(self):
        rect = self.fontMetrics().boundingRect(
            QtCore.QRect(0, 0, 10_000, 10_000),
            QtCore.Qt.TextFlag.TextSingleLine | QtCore.Qt.TextFlag.TextShowMnemonic, self.text(),
        )
        return QtCore.QSize(rect.width() + 6, super().sizeHint().height())


class _PreferencesCheckBox(qtw.QCheckBox):
    """Keep native checkbox interaction while wrapping long labels."""

    def __init__(self, *args):
        super().__init__(*args)
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(qtw.QSizePolicy.Policy.Preferred)
        policy.setVerticalPolicy(qtw.QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def _text_rect(self, width):
        option = qtw.QStyleOptionButton()
        self.initStyleOption(option)
        option.rect = QtCore.QRect(0, 0, width, self.height())
        style = self.style()
        assert style is not None
        return style.subElementRect(qtw.QStyle.SubElement.SE_CheckBoxContents, option, self)

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        text_width = max((self.fontMetrics().horizontalAdvance(word) for word in self.text().split()), default=0)
        size.setWidth(self.width() - self._text_rect(self.width()).width() + text_width)
        return size

    def heightForWidth(self, width):
        rect = QtCore.QRect(0, 0, max(1, self._text_rect(width).width()), 10_000)
        text = self.fontMetrics().boundingRect(
            rect, QtCore.Qt.TextFlag.TextWordWrap | QtCore.Qt.TextFlag.TextShowMnemonic, self.text(),
        )
        return max(super().sizeHint().height(), text.height() + 4)

    def hitButton(self, point):
        return self.rect().contains(point)

    def paintEvent(self, event):
        option = qtw.QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        painter = qtw.QStylePainter(self)
        painter.drawControl(qtw.QStyle.ControlElement.CE_CheckBox, option)
        rect = self._text_rect(self.width())
        flags = (QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
                 | QtCore.Qt.TextFlag.TextWordWrap | QtCore.Qt.TextFlag.TextShowMnemonic)
        style = self.style()
        assert style is not None
        if not style.styleHint(qtw.QStyle.StyleHint.SH_UnderlineShortcut, option, self):
            flags |= QtCore.Qt.TextFlag.TextHideMnemonic
        style.drawItemText(painter, rect, flags, option.palette, self.isEnabled(),
                                  self.text(), QtGui.QPalette.ColorRole.WindowText)
        if self.hasFocus():
            focus = qtw.QStyleOptionFocusRect()
            focus.initFrom(self)
            focus.rect = rect
            painter.drawPrimitive(qtw.QStyle.PrimitiveElement.PE_FrameFocusRect, focus)


class PreferencesDialog(qtw.QDialog):
    """
    Dialog for editing common config-backed qPlot preferences.

    """
    preferencesApplied = QtCore.pyqtSignal()

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config

        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.setMinimumWidth(300)
        self._initial_geometry_set = False

        self._build_ui()
        self._bindings = tuple(
            _PreferenceBinding(key, getattr(self, control_name))
            for key, control_name in _PREFERENCE_CONTROLS
        )
        self._edited_preference_keys: set[str] = set()
        for binding in self._bindings:
            binding.track_edits(self._preference_edited)
        self.load_from_config()

    def _build_ui(self):
        layout = qtw.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        tabs = DialogTabs(self)
        tabs.setObjectName("preferencesTabs")
        self.tabs = tabs
        for title, build in (("Application", self._application_tab), ("Plots", self._plots_tab),
                             ("Files && export", self._files_tab), ("Performance", self._performance_tab)):
            page = _PreferencesPage(build())
            page.setObjectName("preferencesPage" + str(tabs.count()))
            tabs.addTab(page, title)
        layout.addWidget(tabs)

        self.buttonBox = qtw.QDialogButtonBox(
            qtw.QDialogButtonBox.StandardButton.Ok
            | qtw.QDialogButtonBox.StandardButton.Cancel
            | qtw.QDialogButtonBox.StandardButton.Apply
            | qtw.QDialogButtonBox.StandardButton.RestoreDefaults,
            self,
            )
        self.buttonBox.accepted.connect(self._accept_preferences)
        self.buttonBox.rejected.connect(self.reject)
        apply_button = self.buttonBox.button(qtw.QDialogButtonBox.StandardButton.Apply)
        if apply_button is None:
            raise RuntimeError("Preferences dialog Apply button is not available.")
        self.apply_button = apply_button
        apply_button.clicked.connect(self.apply_preferences)
        apply_button.setToolTip("Save and apply these preferences without closing this dialog.")
        for standard_button, tooltip in (
            (qtw.QDialogButtonBox.StandardButton.Ok,
             "Save and apply these preferences, then close this dialog."),
            (qtw.QDialogButtonBox.StandardButton.Cancel,
             "Close without applying further changes. Changes already applied are kept."),
        ):
            button = self.buttonBox.button(standard_button)
            if button is not None:
                button.setToolTip(tooltip)
        self.restoreDefaultsButton = self.buttonBox.button(
            qtw.QDialogButtonBox.StandardButton.RestoreDefaults
            )
        if self.restoreDefaultsButton is None:
            raise RuntimeError(
                "Preferences dialog Restore Defaults button is not available."
                )
        self.restoreDefaultsButton.setObjectName("restorePreferenceDefaultsButton")
        self.restoreDefaultsButton.setText("Reset this page")
        self.restoreDefaultsButton.setAccessibleName("Reset this preference page")
        self.restoreDefaultsButton.setToolTip("Stage the defaults for the current page. Apply to save them.")
        self.restoreDefaultsButton.clicked.connect(self.restore_defaults)
        self.buttonBox.setSizePolicy(qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Fixed)
        layout.addWidget(self.buttonBox)

    def _available_geometry(self):
        parent = self.parentWidget()
        screen = parent.screen() if parent is not None else self.screen()
        return screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1024, 768)

    def _match_action_button_widths(self):
        buttons = [button for standard in (
            qtw.QDialogButtonBox.StandardButton.Ok, qtw.QDialogButtonBox.StandardButton.Cancel,
            qtw.QDialogButtonBox.StandardButton.Apply,
        ) if (button := self.buttonBox.button(standard)) is not None]
        width = max(button.sizeHint().width() for button in buttons)
        for button in buttons:
            button.setFixedWidth(width)

    def showEvent(self, event):
        super().showEvent(event)
        self._match_action_button_widths()
        if self._initial_geometry_set:
            return
        self._initial_geometry_set = True
        available = self._available_geometry().adjusted(16, 16, -16, -16)
        frame = self.frameGeometry().size() - self.size()
        hint = self.sizeHint()
        layout = self.layout()
        assert layout is not None
        margins = layout.contentsMargins()
        bar = self.tabs.tabBar()
        assert bar is not None
        tabs_width = sum(bar.tabSizeHint(index).width() for index in range(bar.count()))
        style = self.style()
        assert style is not None
        tabs_width += (margins.left() + margins.right()
                       + 2 * style.pixelMetric(qtw.QStyle.PixelMetric.PM_DefaultFrameWidth))
        self.resize(min(max(600, hint.width(), tabs_width), available.width() - max(0, frame.width())),
                    min(max(420, hint.height()), available.height() - max(0, frame.height())))
        layout.activate()
        position = self.frameGeometry()
        position.moveCenter(available.center())
        self.move(position.topLeft())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "buttonBox"):
            return
        self._match_action_button_widths()
        layout = self.layout()
        style = self.style()
        assert layout is not None and style is not None
        margins = layout.contentsMargins()
        spacing = style.pixelMetric(qtw.QStyle.PixelMetric.PM_LayoutHorizontalSpacing)
        buttons = self.buttonBox.buttons()
        needed = (sum(max(button.sizeHint().width(), button.minimumWidth()) for button in buttons)
                  + max(0, spacing) * (len(buttons) - 1))
        available = self.width() - margins.left() - margins.right()
        orientation = QtCore.Qt.Orientation.Vertical if available < needed else QtCore.Qt.Orientation.Horizontal
        if self.buttonBox.orientation() != orientation:
            self.buttonBox.setOrientation(orientation)

    def _application_tab(self):
        tab = qtw.QWidget(self)
        form = qtw.QFormLayout(tab)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(8)

        self._add_section(form, "Interface")

        self.themeCombo = qtw.QComboBox(tab)
        self.themeCombo.setObjectName("themePreferenceCombo")
        self.themeCombo.setToolTip("Choose the colour theme for qPlot windows and plots.")
        self.themeCombo.setAccessibleName("Theme")
        for label, value in THEME_OPTIONS:
            self.themeCombo.addItem(label, value)
        self._add_row(form, "&Theme:", self.themeCombo)

        self.previewSizeSpin = qtw.QSpinBox(tab)
        self.previewSizeSpin.setObjectName("previewSizePreferenceSpin")
        self.previewSizeSpin.setToolTip(
            "Width and height of images in the Preview tab, in logical pixels. "
            "Run-list thumbnails have a separate fixed size."
        )
        self.previewSizeSpin.setAccessibleName("Preview image size")
        self.previewSizeSpin.setRange(50, 1000)
        self.previewSizeSpin.setSingleStep(50)
        self.previewSizeSpin.setSuffix(" px")
        self._add_row(form, "&Preview image size:", self.previewSizeSpin)

        self._add_section(form, "Live data")
        self._add_refresh_interval(tab, form)

        self._add_section(form, "Closing windows")
        self._add_close_preferences(tab, form)

        self._add_section(form, "Updates")
        self._add_update_preferences(tab, form)
        return tab

    def _add_window_grid(self, tab, form):
        self._add_section(form, "New plot windows")

        self.plotTileRowsSpin = qtw.QSpinBox(tab)
        self.plotTileRowsSpin.setObjectName("plotTileRowsPreferenceSpin")
        self.plotTileRowsSpin.setAccessibleName("Window grid rows")
        self.plotTileRowsSpin.setRange(1, 10)
        self.plotTileRowsSpin.setToolTip(
            "Rows in the screen grid used to size and place newly opened plots"
            )

        self.plotTileColumnsSpin = qtw.QSpinBox(tab)
        self.plotTileColumnsSpin.setObjectName("plotTileColumnsPreferenceSpin")
        self.plotTileColumnsSpin.setAccessibleName("Window grid columns")
        self.plotTileColumnsSpin.setRange(1, 10)
        self.plotTileColumnsSpin.setToolTip(
            "Columns in the screen grid; plots fill left to right, then top "
            "to bottom, wrapping after the last cell. Existing plots stay in place."
            )
        grid_layout = qtw.QHBoxLayout()
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(6)
        rows_label = qtw.QLabel("&Rows:", tab)
        rows_label.setBuddy(self.plotTileRowsSpin)
        grid_layout.addWidget(rows_label)
        grid_layout.addWidget(self.plotTileRowsSpin)
        grid_layout.addSpacing(12)
        columns_label = qtw.QLabel("&Columns:", tab)
        columns_label.setBuddy(self.plotTileColumnsSpin)
        grid_layout.addWidget(columns_label)
        grid_layout.addWidget(self.plotTileColumnsSpin)
        grid_layout.addStretch(1)
        form.addRow(grid_layout)

    def _add_refresh_interval(self, tab, form):
        self.refreshRateSpin = qtw.QDoubleSpinBox(tab)
        self.refreshRateSpin.setObjectName("refreshRatePreferenceSpin")
        self.refreshRateSpin.setToolTip("Default interval between automatic refreshes, in seconds. Zero disables automatic refresh.")
        self.refreshRateSpin.setAccessibleName("Default refresh interval")
        self.refreshRateSpin.setRange(0.0, 10_000.0)
        self.refreshRateSpin.setSingleStep(0.1)
        self.refreshRateSpin.setDecimals(1)
        self.refreshRateSpin.setSuffix(" s")
        self.refreshRateSpin.setSpecialValueText("Manual")
        self._add_row(form, "&Default refresh interval:", self.refreshRateSpin)

    def _add_close_preferences(self, tab, form):
        self.confirmCloseAllCheck = _PreferencesCheckBox(
            "Confirm before closing all plot windows",
            tab,
            )
        self.confirmCloseAllCheck.setObjectName("confirmCloseAllPreferenceCheck")
        self.confirmCloseAllCheck.setToolTip("Ask for confirmation before closing all plot windows.")
        self.confirmCloseAllCheck.setAccessibleName(
            "Confirm before closing all plot windows"
            )
        form.addRow(self.confirmCloseAllCheck)

        self.confirmQuitCheck = _PreferencesCheckBox("Confirm before quitting qPlot", tab)
        self.confirmQuitCheck.setObjectName("confirmQuitPreferenceCheck")
        self.confirmQuitCheck.setToolTip("Ask for confirmation before quitting qPlot.")
        self.confirmQuitCheck.setAccessibleName("Confirm before quitting qPlot")
        form.addRow(self.confirmQuitCheck)

    def _plots_tab(self):
        tab = qtw.QWidget(self)
        form = qtw.QFormLayout(tab)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(8)

        self._add_section(form, "All plots")

        self.fontSizeSpin = qtw.QSpinBox(tab)
        self.fontSizeSpin.setObjectName("fontSizePreferenceSpin")
        self.fontSizeSpin.setAccessibleName("Plot font size")
        self.fontSizeSpin.setRange(6, 36)
        self.fontSizeSpin.setSuffix(" pt")
        self.fontSizeSpin.setToolTip(
            "Font size for graph axis labels, tick numbers, and colour scales"
            )
        self._add_row(form, "Plot &font size:", self.fontSizeSpin)

        self.showSourceAnnotationCheck = _PreferencesCheckBox("Show database and run details above plots", tab)
        self.showSourceAnnotationCheck.setObjectName("showSourceAnnotationPreferenceCheck")
        self.showSourceAnnotationCheck.setToolTip(
            "Show the database, run IDs and parameters above plots, including in exports"
            )
        form.addRow(self.showSourceAnnotationCheck)

        self._add_section(form, "Axes and colour scales")

        self.axisTickWidthSpin = qtw.QDoubleSpinBox(tab)
        self.axisTickWidthSpin.setObjectName("axisTickWidthPreferenceSpin")
        self.axisTickWidthSpin.setAccessibleName("Axis and tick width")
        self.axisTickWidthSpin.setRange(0.5, 10.0)
        self.axisTickWidthSpin.setSingleStep(0.5)
        self.axisTickWidthSpin.setDecimals(1)
        self.axisTickWidthSpin.setSuffix(" px")
        self.axisTickWidthSpin.setToolTip(
            "Line width for every plot axis and major and minor tick mark, "
            "including colour scales"
            )
        self._add_row(form, "Axis and tick &width:", self.axisTickWidthSpin)

        self.axisMajorTickCountSpin = qtw.QSpinBox(tab)
        self.axisMajorTickCountSpin.setObjectName("axisMajorTickCountPreferenceSpin")
        self.axisMajorTickCountSpin.setAccessibleName("Target labels per axis")
        self.axisMajorTickCountSpin.setRange(2, 10)
        self.axisMajorTickCountSpin.setToolTip(
            "Target number of labelled major ticks on each axis, including "
            "the heatmap colour scale"
            )
        self._add_row(form, "&Target labels per axis:", self.axisMajorTickCountSpin)

        self.colorbarWidthSpin = qtw.QSpinBox(tab)
        self.colorbarWidthSpin.setObjectName("colorbarWidthPreferenceSpin")
        self.colorbarWidthSpin.setAccessibleName("Colour scale width")
        self.colorbarWidthSpin.setRange(1, 500)
        self.colorbarWidthSpin.setSuffix(" px")
        self.colorbarWidthSpin.setToolTip(
            "Width of heatmap colour scale bars"
            )
        self._add_row(form, "Colour scale &width:", self.colorbarWidthSpin)

        self._add_plot_interaction(tab, form)
        self._add_window_grid(tab, form)
        self._add_section(form, "New traces and heatmaps")

        self.defaultTraceColourCombo = _PreferenceTraceColourCombo()
        self.defaultTraceColourCombo.setObjectName("defaultTraceColourPreferenceCombo")
        self.defaultTraceColourCombo.setIconSize(QtCore.QSize(54, 16))
        self.defaultTraceWidthSpin = DrawingSizeSpinBox()
        self.defaultTraceWidthSpin.setObjectName("defaultTraceWidthPreferenceSpin")
        self.defaultTraceWidthSpin.setAccessibleName("Default trace line width")
        self.defaultTraceWidthSpin.setRange(0.5, 15)
        self.defaultTraceWidthSpin.setDecimals(1)
        self.defaultTraceWidthSpin.setSingleStep(0.5)
        self.defaultTraceWidthSpin.setSuffix(" px")
        self.defaultTraceWidthSpin.setSizePolicy(qtw.QSizePolicy.Policy.Fixed, qtw.QSizePolicy.Policy.Fixed)
        self.defaultTraceWidthSpin.setToolTip("Line width for newly created traces, including traces added to existing plots.")
        self.defaultTraceStyleCombo = LineStyleCombo("Default trace line style")
        self.defaultTraceStyleCombo.setObjectName("defaultTraceStylePreferenceCombo")
        self.defaultTraceStyleCombo.setIconSize(self.defaultTraceColourCombo.iconSize())
        self.defaultTraceRow = qtw.QWidget(tab)
        trace_layout = qtw.QHBoxLayout(self.defaultTraceRow)
        trace_layout.setContentsMargins(0, 0, 0, 0)
        trace_layout.setSpacing(6)
        trace_layout.addWidget(self.defaultTraceColourCombo)
        trace_layout.addWidget(self.defaultTraceWidthSpin)
        trace_layout.addWidget(self.defaultTraceStyleCombo)
        trace_layout.addStretch()
        self.defaultTraceRow.setSizePolicy(qtw.QSizePolicy.Policy.Fixed, qtw.QSizePolicy.Policy.Fixed)
        self.defaultTraceRow.setToolTip("Appearance for newly created traces, including traces added to existing plots.")
        self._add_row(form, "Trace &appearance:", self.defaultTraceRow, buddy=self.defaultTraceColourCombo)

        self.defaultColourMapButton = _PreferenceColourMapButton(tab)
        self.defaultColourMapButton.setObjectName("defaultColourMapPreferenceButton")
        self._add_row(form, "Heatmap colour &map:", self.defaultColourMapButton)

        return tab

    def _add_plot_interaction(self, tab, form):
        self._add_section(form, "Navigation and readout")

        self.cursorSignificantFiguresSpin = qtw.QSpinBox(tab)
        self.cursorSignificantFiguresSpin.setObjectName("cursorSignificantFiguresPreferenceSpin")
        self.cursorSignificantFiguresSpin.setAccessibleName("Cursor precision (significant figures)")
        self.cursorSignificantFiguresSpin.setRange(1, 17)
        self.cursorSignificantFiguresSpin.setToolTip(
            "Significant figures for the x, y, and z cursor coordinates, including snapped values"
            )
        self._add_row(form, "Cursor &precision (significant figures):", self.cursorSignificantFiguresSpin)

        self._add_section(form, "New complex plots")

        for attribute, label, options in (
            ("complex1dCombo", "1D complex plots:", COMPLEX_1D_OPTIONS),
            ("complexNdCombo", "Higher-dimensional complex plots:", COMPLEX_COMPONENT_OPTIONS),
        ):
            combo = qtw.QComboBox(tab)
            combo.setObjectName(attribute + "Preference")
            combo.setAccessibleName(label.rstrip(":"))
            combo.setToolTip(
                "Choose how complex measurements are plotted. The cursor readout "
                "retains the original complex value and setpoints."
            )
            for text, value in options:
                combo.addItem(text, value)
            setattr(self, attribute, combo)
            self._add_row(form, label, combo)

        self._add_section(form, "Adding data")

        self.confirmIncompatibleAxesCheck = _PreferencesCheckBox(
            "Warn before adding data with incompatible axes", tab,
        )
        self.confirmIncompatibleAxesCheck.setObjectName("confirmIncompatibleAxesPreferenceCheck")
        self.confirmIncompatibleAxesCheck.setToolTip("Warn when added data has coordinate names or units that differ from the current plot.")
        self.confirmIncompatibleAxesCheck.setAccessibleName(
            "Warn before adding data with incompatible axes"
        )
        form.addRow(self.confirmIncompatibleAxesCheck)

    def _files_tab(self):
        tab = qtw.QWidget(self)
        form = qtw.QFormLayout(tab)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(8)

        self._add_section(form, "Opening databases")

        path_widget = qtw.QWidget(tab)
        path_layout = qtw.QHBoxLayout(path_widget)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(6)

        self.defaultLoadPathEdit = qtw.QLineEdit(tab)
        self.defaultLoadPathEdit.setObjectName("defaultLoadPathPreferenceEdit")
        self.defaultLoadPathEdit.setToolTip("Folder initially offered when loading a database. Leave blank to use the normal file-dialog location.")
        self.defaultLoadPathEdit.setAccessibleName("Default database folder")
        self.defaultLoadPathEdit.setPlaceholderText("No default folder")
        path_layout.addWidget(self.defaultLoadPathEdit, 1)

        self.defaultLoadPathButton = qtw.QToolButton(tab)
        self.defaultLoadPathButton.setObjectName("defaultLoadPathPreferenceButton")
        style = self.style()
        if style is not None:
            self.defaultLoadPathButton.setIcon(
                style.standardIcon(qtw.QStyle.StandardPixmap.SP_DirOpenIcon)
                )
        self.defaultLoadPathButton.setToolTip("Choose default database folder")
        self.defaultLoadPathButton.setAccessibleName(
            "Choose default database folder"
            )
        self.defaultLoadPathButton.clicked.connect(self.choose_default_load_path)
        path_layout.addWidget(self.defaultLoadPathButton)

        self._add_row(
            form,
            "Default database &folder:",
            path_widget,
            self.defaultLoadPathEdit,
            )

        self.cloudSyncTimeoutSpin = qtw.QDoubleSpinBox(tab)
        self.cloudSyncTimeoutSpin.setObjectName("cloudSyncPreferenceSpin")
        self.cloudSyncTimeoutSpin.setToolTip("Maximum time to wait for a cloud-stored file to become available locally.")
        self.cloudSyncTimeoutSpin.setAccessibleName("Cloud file wait timeout")
        self.cloudSyncTimeoutSpin.setRange(1.0, 3600.0)
        self.cloudSyncTimeoutSpin.setSingleStep(10.0)
        self.cloudSyncTimeoutSpin.setDecimals(1)
        self.cloudSyncTimeoutSpin.setSuffix(" s")
        self._add_row(form, "Cloud file &wait timeout:", self.cloudSyncTimeoutSpin)

        self._add_section(form, "Export and clipboard")

        self.csvFilenameFormatCombo = qtw.QComboBox(tab)
        self.csvFilenameFormatCombo.setObjectName("csvFilenameFormatPreferenceCombo")
        self.csvFilenameFormatCombo.setAccessibleName("CSV filename format")
        for label, value in CSV_FILENAME_FORMAT_OPTIONS:
            self.csvFilenameFormatCombo.addItem(label, value)
        self.csvFilenameFormatCombo.setToolTip(
            "Suggested filename when exporting to CSV."
        )
        self._add_row(form, "CSV &filename format:", self.csvFilenameFormatCombo)

        self.copyPlotImageResolutionCombo = qtw.QComboBox(tab)
        self.copyPlotImageResolutionCombo.setObjectName(
            "copyPlotImageResolutionPreferenceCombo"
            )
        self.copyPlotImageResolutionCombo.setToolTip("Choose the resolution of plot images copied to the clipboard.")
        self.copyPlotImageResolutionCombo.setAccessibleName(
            "Copied plot image quality"
            )
        for label, value in COPY_PLOT_IMAGE_RESOLUTION_OPTIONS:
            self.copyPlotImageResolutionCombo.addItem(label, value)
        self._add_row(
            form,
            "Copied plot image &quality:",
            self.copyPlotImageResolutionCombo,
            )

        return tab

    def _performance_tab(self):
        tab = qtw.QWidget(self)
        form = qtw.QFormLayout(tab)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(8)

        self.maxThreadsSpin = qtw.QSpinBox(tab)
        self.maxThreadsSpin.setObjectName("maxThreadsPreferenceSpin")
        self.maxThreadsSpin.setToolTip("Maximum number of background worker threads used to load and process data.")
        self.maxThreadsSpin.setAccessibleName("Maximum worker threads")
        self.maxThreadsSpin.setRange(1, 999)
        self._add_row(form, "&Maximum worker threads:", self.maxThreadsSpin)

        self.maxFullHeatmapPointsSpin = qtw.QSpinBox(tab)
        self.maxFullHeatmapPointsSpin.setObjectName(
            "maxFullHeatmapPointsPreferenceSpin"
            )
        self.maxFullHeatmapPointsSpin.setAccessibleName(
            "Maximum full-resolution heatmap points"
            )
        self.maxFullHeatmapPointsSpin.setRange(1, 2_000_000_000)
        self.maxFullHeatmapPointsSpin.setSingleStep(100_000)
        self.maxFullHeatmapPointsSpin.setSuffix(" points")
        if hasattr(self.maxFullHeatmapPointsSpin, "setGroupSeparatorShown"):
            self.maxFullHeatmapPointsSpin.setGroupSeparatorShown(True)
        self.maxFullHeatmapPointsSpin.setToolTip(
            "Use full-resolution heatmap loading up to this many points; "
            "larger heatmaps use SQL spatial aggregation."
            )
        self._add_row(
            form,
            "Full-resolution &heatmap limit:",
            self.maxFullHeatmapPointsSpin,
            )

        self.maxHeatmapGridCellsSpin = qtw.QSpinBox(tab)
        self.maxHeatmapGridCellsSpin.setObjectName(
            "maxHeatmapGridCellsPreferenceSpin"
            )
        self.maxHeatmapGridCellsSpin.setAccessibleName(
            "Maximum heatmap display grid cells"
            )
        self.maxHeatmapGridCellsSpin.setRange(1, MAX_AGGREGATED_HEATMAP_CELLS)
        self.maxHeatmapGridCellsSpin.setSingleStep(10_000)
        self.maxHeatmapGridCellsSpin.setSuffix(" cells")
        if hasattr(self.maxHeatmapGridCellsSpin, "setGroupSeparatorShown"):
            self.maxHeatmapGridCellsSpin.setGroupSeparatorShown(True)
        self.maxHeatmapGridCellsSpin.setToolTip(
            "Maximum total cells in an aggregated heatmap display grid. Rectangular grids share this budget across both axes."
            )
        self._add_row(
            form,
            "Maximum heatmap grid &cells:",
            self.maxHeatmapGridCellsSpin,
            )
        hint = qtw.QLabel("Above the full-resolution limit, qPlot averages samples into a smaller grid. "
                          "The grid cell limit counts all rows × columns.", tab)
        hint.setWordWrap(True)
        form.addRow(hint)

        self.delGracePeriodSpin = qtw.QDoubleSpinBox(tab)
        self.delGracePeriodSpin.setObjectName("deleteGracePreferenceSpin")
        self.delGracePeriodSpin.setToolTip("Time to retain unused datasets in memory before releasing them. Reopening them during this period can be quicker.")
        self.delGracePeriodSpin.setAccessibleName("Unused dataset retention time")
        self.delGracePeriodSpin.setRange(0.0, 300.0)
        self.delGracePeriodSpin.setSingleStep(1.0)
        self.delGracePeriodSpin.setDecimals(1)
        self.delGracePeriodSpin.setSuffix(" s")
        self._add_row(
            form,
            "Unused dataset &retention time:",
            self.delGracePeriodSpin,
            )

        return tab

    def _add_update_preferences(self, tab, form):
        self.checkUpdatesOnLaunchCheck = _PreferencesCheckBox(
            "Check for updates on launch (at most once a day)", tab,
        )
        self.checkUpdatesOnLaunchCheck.setObjectName("checkUpdatesOnLaunchPreferenceCheck")
        self.checkUpdatesOnLaunchCheck.setToolTip("Check PyPI for newer qPlot releases in the background, at most once a day. Requires an Internet connection.")
        self.checkUpdatesOnLaunchCheck.setAccessibleName("Check for updates on launch")
        form.addRow(self.checkUpdatesOnLaunchCheck)
        self.includePrereleasesCheck = _PreferencesCheckBox("Include pre-release versions", tab)
        self.includePrereleasesCheck.setObjectName("includePrereleasesPreferenceCheck")
        self.includePrereleasesCheck.setToolTip("Include development and preview releases when checking for updates.")
        form.addRow(self.includePrereleasesCheck)

    @staticmethod
    def _add_section(form, title):
        heading = qtw.QLabel(title)
        heading.setWordWrap(True)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        if form.rowCount():
            heading.setContentsMargins(0, 8, 0, 0)
        form.addRow(heading)

    def _add_row(self, form, label_text, widget, buddy=None):
        label = _PreferencesLabel(label_text, self)
        label.setBuddy(buddy or widget)
        label.setWordWrap(True)
        if isinstance(widget, qtw.QComboBox):
            widget.setMinimumContentsLength(12)
            widget.setSizeAdjustPolicy(qtw.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        form.addRow(label, widget)
        if isinstance(widget, qtw.QAbstractSpinBox):
            widget.setSizePolicy(qtw.QSizePolicy.Policy.Fixed, qtw.QSizePolicy.Policy.Fixed)
            form.setAlignment(widget, QtCore.Qt.AlignmentFlag.AlignLeft)

    def load_from_config(self):
        """
        Loads current config values into the dialog widgets.

        """
        self.set_preference_values({
            key: self.config.get(key)
            for key in PREFERENCE_KEYS
            })
        self._loaded_control_values = self.preference_values()
        self._edited_preference_keys.clear()
        self.apply_button.setEnabled(False)
        self.apply_button.setText("Apply")

    def _preference_edited(self, key):
        self._edited_preference_keys.add(key)
        self._sync_apply_button()

    def _sync_apply_button(self):
        dirty = bool(self._changed_values())
        self.apply_button.setEnabled(dirty)
        if not dirty:
            self.apply_button.setText("Apply")

    def set_preference_values(self, values):
        """Load values without recording programmatic updates as edits."""
        with blocked_signals(*(binding.control for binding in self._bindings)):
            for binding in self._bindings:
                binding.write(values[binding.key])

    def preference_values(self):
        """Return the current control values in preference registry order."""
        return {binding.key: binding.read() for binding in self._bindings}

    def default_preference_values(self):
        """
        Returns schema defaults for the preferences shown in this dialog.

        """
        return {
            key: self._schema_default(key)
            for key in PREFERENCE_KEYS
            }

    def _schema_default(self, key):
        section, name = key.split(".")
        schema = self.config.schema["properties"][section]["properties"][name]
        return deepcopy(schema["default"])

    def _changed_values(self):
        # Display precision and text normalization may differ from valid saved
        # values. Only an actual edit may replace those exact saved values.
        return {
            key: value
            for key, value in self.preference_values().items()
            if self.config.get(key) != value and (
                key in self._edited_preference_keys
                or value != self._loaded_control_values[key]
                )
            }

    def apply_preferences(self):
        changed_values = self._changed_values()
        if not changed_values:
            return True
        if not persist_config_values(
                self,
                self.config,
                changed_values,
                "the preferences",
                ):
            self.apply_button.setText("Retry")
            return False

        self.apply_button.setEnabled(False)
        self.apply_button.setText("Apply")
        self._loaded_control_values = self.preference_values()
        self._edited_preference_keys.clear()
        self.preferencesApplied.emit()
        return True

    def restore_defaults(self):
        """
        Stage defaults for controls on the selected page only.

        """
        page = self.tabs.currentWidget()
        if page is None:
            return False
        page_title = self.tabs.tabText(self.tabs.currentIndex()).replace("&&", "&")
        if not confirm_action(
            self,
            "Reset preference page",
            f"Restore the defaults on the {page_title} page? Choose Apply or OK to save them.",
            affirmative="Reset this page",
            ):
            return False

        for binding in self._bindings:
            if page.isAncestorOf(binding.control):
                with blocked_signals(binding.control):
                    binding.write(self._schema_default(binding.key))
                self._edited_preference_keys.add(binding.key)
        self._sync_apply_button()
        return True

    def choose_default_load_path(self):
        current_path = self.defaultLoadPathEdit.text().strip()
        if not os.path.isdir(current_path):
            current_path = ""

        folder = qtw.QFileDialog.getExistingDirectory(
            self,
            "Default Load Location",
            current_path,
            )
        if folder:
            self.defaultLoadPathEdit.setText(folder)

    def _accept_preferences(self):
        if self.apply_preferences():
            self.accept()


def create_preferences_action(window, triggered):
    """
    Creates the shared Preferences action used by main and plot windows.

    """
    action = create_action("preferences.open", window)
    action.setMenuRole(QtGui.QAction.MenuRole.PreferencesRole)
    action.triggered.connect(triggered)
    return action
