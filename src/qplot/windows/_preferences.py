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
from qplot.datahandling.complex_data import (
    COMPLEX_1D_KEY,
    COMPLEX_1D_OPTIONS,
    COMPLEX_COMPONENT_OPTIONS,
    COMPLEX_ND_KEY,
)
from qplot.tools.heatmap_geometry import MAX_AGGREGATED_HEATMAP_CELLS
from qplot.updates import CHECK_ON_LAUNCH_KEY, INCLUDE_PRERELEASES_KEY

from ._colorbar import (
    _colorbar_colormap_label,
    _colorbar_colormap_preview,
    _colorbar_colormaps,
)
from ._commands import create_action
from ._config_persistence import persist_config_values
from ._csv_filenames import CSV_FILENAME_FORMAT_KEY, CSV_FILENAME_FORMAT_OPTIONS
from ._plot_annotation import SHOW_SOURCE_ANNOTATION_KEY
from ._plot_tiling import PLOT_TILE_COLUMNS_KEY, PLOT_TILE_ROWS_KEY
from ._signal_blocking import blocked_signals
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
    ("user_preference.bar_colour", "defaultColourMapCombo"),
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

_PreferenceControl = (
    qtw.QComboBox | qtw.QSpinBox | qtw.QDoubleSpinBox | qtw.QCheckBox | qtw.QLineEdit
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
        if isinstance(control, qtw.QComboBox):
            control.setCurrentIndex(max(control.findData(value), 0))
        elif isinstance(control, qtw.QCheckBox):
            control.setChecked(bool(value))
        elif isinstance(control, qtw.QLineEdit):
            control.setText(str(value))
        elif isinstance(control, qtw.QSpinBox):
            control.setValue(int(value))
        else:
            control.setValue(float(value))

    def track_edits(self, edited_keys: set[str]) -> None:
        def mark_edited(_value):
            edited_keys.add(self.key)

        control = self.control
        if isinstance(control, (qtw.QSpinBox, qtw.QDoubleSpinBox)):
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


class _PreferencesTabBar(qtw.QTabBar):
    """Fill the tab strip while retaining Qt's normal overflow buttons."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        parent = self.parentWidget()
        if parent is not None and self.width() != parent.width():
            self.resize(parent.width(), self.height())


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
        if page is None or not isinstance(page.layout(), qtw.QFormLayout):
            return
        form = page.layout()
        margins = form.contentsMargins()
        available = self.viewport().width() - margins.left() - margins.right()
        for child in page.findChildren(qtw.QWidget, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly):
            child.setMaximumWidth(max(1, available))
        needed = 0
        for row in range(form.rowCount()):
            label = form.itemAt(row, qtw.QFormLayout.ItemRole.LabelRole)
            field = form.itemAt(row, qtw.QFormLayout.ItemRole.FieldRole)
            if label is not None and field is not None:
                needed = max(needed, label.sizeHint().width() + field.minimumSize().width()
                             + max(0, form.horizontalSpacing()))
        policy = (qtw.QFormLayout.RowWrapPolicy.WrapAllRows if available < needed
                  else qtw.QFormLayout.RowWrapPolicy.WrapLongRows)
        if form.rowWrapPolicy() != policy:
            form.setRowWrapPolicy(policy)

    def sizeHint(self):
        return self.widget().sizeHint().boundedTo(QtCore.QSize(600, 440))

    def minimumSizeHint(self):
        return QtCore.QSize(160, 80)


class _PreferencesLabel(qtw.QLabel):
    """Prefer a single line while still allowing native word wrapping."""

    def sizeHint(self):
        rect = self.fontMetrics().boundingRect(
            QtCore.QRect(0, 0, 10_000, 10_000),
            QtCore.Qt.TextFlag.TextSingleLine | QtCore.Qt.TextFlag.TextShowMnemonic, self.text(),
        )
        return QtCore.QSize(rect.width(), self.fontMetrics().height())


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
        return self.style().subElementRect(qtw.QStyle.SubElement.SE_CheckBoxContents, option, self)

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
        if not self.style().styleHint(qtw.QStyle.StyleHint.SH_UnderlineShortcut, option, self):
            flags |= QtCore.Qt.TextFlag.TextHideMnemonic
        self.style().drawItemText(painter, rect, flags, option.palette, self.isEnabled(),
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
            binding.track_edits(self._edited_preference_keys)
        self.load_from_config()

    def _build_ui(self):
        layout = qtw.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        tabs = qtw.QTabWidget(self)
        tabs.setObjectName("preferencesTabs")
        self.tabs = tabs
        tab_bar = _PreferencesTabBar(tabs)
        tabs.setTabBar(tab_bar)
        tab_bar.setExpanding(True)
        tab_bar.setUsesScrollButtons(True)
        for title, build in (("General", self._general_tab), ("Appearance", self._appearance_tab),
                             ("Plotting", self._plotting_tab), ("Files && export", self._files_tab),
                             ("Performance", self._performance_tab), ("Updates", self._updates_tab)):
            page = _PreferencesPage(build())
            page.setObjectName("preferencesPage" + str(tabs.count()))
            tabs.addTab(page, title)
        tabs.setElideMode(QtCore.Qt.TextElideMode.ElideNone)
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
        self.restoreDefaultsButton.setAccessibleName("Restore preference defaults")
        self.restoreDefaultsButton.setToolTip(
            "Restore the preferences shown here to their defaults"
            )
        self.restoreDefaultsButton.clicked.connect(self.restore_defaults)
        self.buttonBox.setSizePolicy(qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Fixed)
        layout.addWidget(self.buttonBox)

    def _available_geometry(self):
        parent = self.parentWidget()
        screen = parent.screen() if parent is not None else self.screen()
        return screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1024, 768)

    def _match_action_button_widths(self):
        buttons = [self.buttonBox.button(standard) for standard in (
            qtw.QDialogButtonBox.StandardButton.Ok, qtw.QDialogButtonBox.StandardButton.Cancel,
            qtw.QDialogButtonBox.StandardButton.Apply,
        )]
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
        margins = self.layout().contentsMargins()
        bar = self.tabs.tabBar()
        tabs_width = sum(bar.tabSizeHint(index).width() for index in range(bar.count()))
        tabs_width += (margins.left() + margins.right()
                       + 2 * self.style().pixelMetric(qtw.QStyle.PixelMetric.PM_DefaultFrameWidth))
        self.resize(min(max(600, hint.width(), tabs_width), available.width() - max(0, frame.width())),
                    min(max(420, hint.height()), available.height() - max(0, frame.height())))
        self.layout().activate()
        position = self.frameGeometry()
        position.moveCenter(available.center())
        self.move(position.topLeft())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "buttonBox"):
            return
        self._match_action_button_widths()
        margins = self.layout().contentsMargins()
        spacing = self.style().pixelMetric(qtw.QStyle.PixelMetric.PM_LayoutHorizontalSpacing)
        buttons = self.buttonBox.buttons()
        needed = (sum(max(button.sizeHint().width(), button.minimumWidth()) for button in buttons)
                  + max(0, spacing) * (len(buttons) - 1))
        available = self.width() - margins.left() - margins.right()
        orientation = QtCore.Qt.Orientation.Vertical if available < needed else QtCore.Qt.Orientation.Horizontal
        if self.buttonBox.orientation() != orientation:
            self.buttonBox.setOrientation(orientation)

    def _general_tab(self):
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

        self._add_section(form, "New window grid")

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
        grid_widget = qtw.QWidget(tab)
        grid_layout = qtw.QHBoxLayout(grid_widget)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(8)
        grid_layout.addWidget(self.plotTileRowsSpin, 1)
        columns_label = qtw.QLabel("&Columns:", grid_widget)
        columns_label.setBuddy(self.plotTileColumnsSpin)
        grid_layout.addWidget(columns_label)
        grid_layout.addWidget(self.plotTileColumnsSpin, 1)
        self._add_row(form, "&Rows:", grid_widget, self.plotTileRowsSpin)

        self._add_section(form, "Live data")

        self.refreshRateSpin = qtw.QDoubleSpinBox(tab)
        self.refreshRateSpin.setObjectName("refreshRatePreferenceSpin")
        self.refreshRateSpin.setToolTip("Default interval between automatic refreshes, in seconds. Zero disables automatic refresh.")
        self.refreshRateSpin.setAccessibleName("Default refresh interval")
        self.refreshRateSpin.setRange(0.0, 10_000.0)
        self.refreshRateSpin.setSingleStep(0.1)
        self.refreshRateSpin.setDecimals(1)
        self.refreshRateSpin.setSuffix(" s")
        self._add_row(form, "&Default refresh interval:", self.refreshRateSpin)

        self._add_section(form, "Closing windows")

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

        return tab

    def _appearance_tab(self):
        tab = qtw.QWidget(self)
        form = qtw.QFormLayout(tab)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(8)

        self._add_section(form, "Text and annotations")

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

        self.defaultColourMapCombo = qtw.QComboBox(tab)
        self.defaultColourMapCombo.setObjectName("defaultColourMapPreferenceCombo")
        self.defaultColourMapCombo.setAccessibleName("Default colour map for new plots")
        self.defaultColourMapCombo.setToolTip("Colour map used by new plots. Existing plots keep their own colour maps.")
        for name in _colorbar_colormaps():
            self.defaultColourMapCombo.addItem(_colorbar_colormap_label(name), name)
        self._add_row(form, "Default colour &map for new plots:", self.defaultColourMapCombo)
        self.defaultColourMapPreview = qtw.QLabel(tab)
        self.defaultColourMapPreview.setAccessibleName("Default colour map preview")
        self.defaultColourMapCombo.currentIndexChanged.connect(self._update_default_colour_preview)
        form.addRow(self.defaultColourMapPreview)

        return tab

    def _plotting_tab(self):
        tab = qtw.QWidget(self)
        form = qtw.QFormLayout(tab)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(8)

        self._add_section(form, "Navigation and readout")

        self.cursorSignificantFiguresSpin = qtw.QSpinBox(tab)
        self.cursorSignificantFiguresSpin.setObjectName("cursorSignificantFiguresPreferenceSpin")
        self.cursorSignificantFiguresSpin.setAccessibleName("Cursor precision (significant figures)")
        self.cursorSignificantFiguresSpin.setRange(1, 17)
        self.cursorSignificantFiguresSpin.setToolTip(
            "Significant figures for the x, y, and z cursor coordinates, including snapped values"
            )
        self._add_row(form, "Cursor &precision (significant figures):", self.cursorSignificantFiguresSpin)

        self._add_section(form, "Complex data")

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

        return tab

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

    def _updates_tab(self):
        tab = qtw.QWidget(self)
        layout = qtw.QVBoxLayout(tab)
        self.checkUpdatesOnLaunchCheck = _PreferencesCheckBox(
            "Check for updates on launch (at most once a day)", tab,
        )
        self.checkUpdatesOnLaunchCheck.setObjectName("checkUpdatesOnLaunchPreferenceCheck")
        self.checkUpdatesOnLaunchCheck.setToolTip("Check PyPI for newer qPlot releases in the background, at most once a day. Requires an Internet connection.")
        self.checkUpdatesOnLaunchCheck.setAccessibleName("Check for updates on launch")
        layout.addWidget(self.checkUpdatesOnLaunchCheck)
        self.includePrereleasesCheck = _PreferencesCheckBox("Include pre-release versions", tab)
        self.includePrereleasesCheck.setObjectName("includePrereleasesPreferenceCheck")
        self.includePrereleasesCheck.setToolTip("Include development and preview releases when checking for updates.")
        layout.addWidget(self.includePrereleasesCheck)
        hint = qtw.QLabel(
            "Launch checks run in the background and notify you only about newer releases. "
            "You can check at any time from Help → Check for Updates. "
            "Updates are listed on PyPI; an Internet connection is required.", tab,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        return tab

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

    def set_preference_values(self, values):
        """Load values without recording programmatic updates as edits."""
        with blocked_signals(*(binding.control for binding in self._bindings)):
            for binding in self._bindings:
                binding.write(values[binding.key])
        self._update_default_colour_preview()

    def _update_default_colour_preview(self):
        self.defaultColourMapPreview.setPixmap(
            _colorbar_colormap_preview(self.defaultColourMapCombo.currentData() or "viridis"),
        )

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

    def apply_preferences(self):
        """
        Persists the dialog values and emits preferencesApplied on success.

        """
        control_values = self.preference_values()
        # Display precision and text normalization may differ from valid saved
        # values. Only an actual edit may replace those exact saved values.
        changed_values = {
            key: value
            for key, value in control_values.items()
            if self.config.get(key) != value and (
                key in self._edited_preference_keys
                or value != self._loaded_control_values[key]
                )
            }
        if changed_values and not persist_config_values(
                self,
                self.config,
                changed_values,
                "the preferences",
                self.load_from_config,
                ):
            return False

        self._loaded_control_values = control_values
        self._edited_preference_keys.clear()
        self.preferencesApplied.emit()
        return True

    def restore_defaults(self):
        """
        Restores the preferences shown in this dialog to schema defaults.

        """
        reply = qtw.QMessageBox.question(
            self,
            "Restore Preference Defaults",
            "Restore the preferences shown in this dialog to their defaults?",
            qtw.QMessageBox.StandardButton.Yes | qtw.QMessageBox.StandardButton.No,
            qtw.QMessageBox.StandardButton.No,
            )
        if reply != qtw.QMessageBox.StandardButton.Yes:
            return False

        self.set_preference_values(self.default_preference_values())
        # Reset is explicit consent to defaults, even when the old value had
        # the same rounded representation in its control.
        self._edited_preference_keys.update(PREFERENCE_KEYS)
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
