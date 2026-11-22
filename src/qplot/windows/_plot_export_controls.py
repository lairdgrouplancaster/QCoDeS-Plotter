"""Compact, format-specific controls for the existing plot exporters."""

from typing import Any

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from pyqtgraph.exporters import CSVExporter, ImageExporter, SVGExporter

from ._plot_csv_selection import csv_selection_description, selected_csv_measurements

FIGURE_FORMATS = (
    ("PNG image", ImageExporter), ("SVG", SVGExporter), ("Plot-sized PDF", QtGui.QPdfWriter),
)


class _ExportBackgroundCombo(qtw.QComboBox):
    colourChanged = QtCore.pyqtSignal(QtGui.QColor)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._colour = QtGui.QColor("white")
        self.setAccessibleName("Export background colour")
        self.setIconSize(QtCore.QSize(38, 16))
        self.addItem(self._swatch(QtGui.QColor("white")), "White", "white")
        self.addItem(self._swatch(QtGui.QColor("transparent")), "Transparent", "transparent")
        self.addItem("Choose colour…", "choose")
        self.colour_dialog = qtw.QColorDialog(self)
        self.colour_dialog.setWindowTitle("Choose export background colour")
        self.colour_dialog.setOption(qtw.QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        self.colour_dialog.setOption(qtw.QColorDialog.ColorDialogOption.ShowAlphaChannel, True)
        # Qt exposes the alpha editor through its label's buddy, rather
        # than an object name. Match Qt's own translated label.
        alpha_label = QtCore.QCoreApplication.translate("QColorDialog", "A&lpha channel:")
        for label in self.colour_dialog.findChildren(qtw.QLabel):
            if label.text() == alpha_label:
                label.setText("&Opacity (0–255):")
                tip = ("Background opacity: 0 is fully transparent; 255 is fully opaque. "
                       "PNG and SVG retain transparency.")
                label.setToolTip(tip)
                editor = label.buddy()
                if editor is not None:
                    editor.setAccessibleName("Background opacity")
                    editor.setToolTip(tip)
                break
        self.activated.connect(self._activate)
        self.colour_dialog.colorSelected.connect(self._accept_colour)
        self.set_colour(self._colour)

    @staticmethod
    def _swatch(colour):
        pixmap = QtGui.QPixmap(38, 16)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        area = pixmap.rect().adjusted(1, 1, -1, -1)
        painter.fillRect(area, QtGui.QColor("white"))
        if colour.alpha() < 255:
            for y in range(area.top(), area.bottom() + 1, 4):
                for x in range(area.left(), area.right() + 1, 4):
                    if ((x - area.left()) // 4 + (y - area.top()) // 4) % 2:
                        painter.fillRect(QtCore.QRect(x, y, 4, 4).intersected(area), QtGui.QColor("#bbbbbb"))
        painter.fillRect(area, colour)
        painter.setPen(QtGui.QColor("#777777"))
        painter.drawRect(area)
        painter.end()
        return QtGui.QIcon(pixmap)

    def colour(self):
        return QtGui.QColor(self._colour)

    def set_colour(self, colour):
        self._colour = QtGui.QColor(colour)
        index = 1 if self._colour.alpha() == 0 else 0 if self._colour == QtGui.QColor("white") else 2
        with QtCore.QSignalBlocker(self):
            self.setCurrentIndex(index)
            if index == 2:
                self.setItemIcon(2, self._swatch(self._colour))
        description = self._description()
        self.setAccessibleDescription(description)
        self.setToolTip(f"Export background: {description}. Choose White, Transparent or a custom colour.")
        self.update()

    def _description(self):
        if self._colour.alpha() == 0:
            return "Transparent"
        if self._colour == QtGui.QColor("white"):
            return "White"
        mode = QtGui.QColor.NameFormat.HexArgb if self._colour.alpha() < 255 else QtGui.QColor.NameFormat.HexRgb
        return self._colour.name(mode)

    def _activate(self, index):
        choice = self.itemData(index)
        if choice == "choose":
            # Restore the displayed selection before opening the picker. Its
            # edits are committed only on acceptance, so Cancel changes nothing.
            self.set_colour(self._colour)
            self.colour_dialog.setCurrentColor(self._colour)
            self.colour_dialog.open()
        else:
            self._accept_colour(QtGui.QColor("white" if choice == "white" else "transparent"))

    def _accept_colour(self, colour):
        self.set_colour(colour)
        self.colourChanged.emit(self.colour())

    def paintEvent(self, event):
        option = qtw.QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentText = self._description()
        painter = qtw.QStylePainter(self)
        painter.drawComplexControl(qtw.QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(qtw.QStyle.ControlElement.CE_ComboBoxLabel, option)


class _AspectRatioLink(qtw.QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(20, 24)
        self.setObjectName("aspectRatioLink")
        self.setAccessibleName("Aspect ratio locked")
        self.setToolTip("Width and height stay linked to preserve the plot's aspect ratio.")

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(self.palette().color(QtGui.QPalette.ColorRole.WindowText), 1.4))
        painter.translate(QtCore.QRectF(self.rect()).center())
        painter.rotate(-35)
        painter.drawRoundedRect(QtCore.QRectF(-8, -3, 10, 6), 3, 3)
        painter.drawRoundedRect(QtCore.QRectF(-2, -3, 10, 6), 3, 3)
        painter.end()


class PlotExportControls(qtw.QWidget):
    def __init__(self, dialog: Any):
        super().__init__(dialog)
        self.dialog = dialog
        self.exporter: Any = None
        self._syncing = False
        self._parameters: list[Any] = []
        self.fields: dict[str, Any] = {}
        layout = qtw.QVBoxLayout(self)
        self.body_layout = layout
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        self.output_tabs = qtw.QTabBar()
        self.output_tabs.setAccessibleName("Export output")
        self.output_tabs.addTab("&Figure")
        self.output_tabs.addTab("Plotted &data")
        layout.addWidget(self.output_tabs)
        form = qtw.QFormLayout()
        self.format_form = form
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.format_combo = qtw.QComboBox()
        for label, exporter in FIGURE_FORMATS:
            self.format_combo.addItem(label, exporter)
        self.format_combo.setAccessibleName("Export format")
        form.addRow("&Format", self.format_combo)
        layout.addLayout(form)
        self.heading = qtw.QLabel()
        font = self.heading.font()
        font.setBold(True)
        self.heading.setFont(font)
        layout.addWidget(self.heading)
        self.settings = qtw.QWidget()
        layout.addWidget(self.settings)
        self.format_combo.currentIndexChanged.connect(self._format_changed)
        self.output_tabs.currentChanged.connect(self._format_changed)

    def _format_changed(self):
        if self._syncing:
            return
        kind = CSVExporter if self.output_tabs.currentIndex() == 1 else self.format_combo.currentData()
        for row in range(self.dialog.ui.formatList.count()):
            if self.dialog.ui.formatList.item(row).expClass is kind:
                self.dialog.ui.formatList.setCurrentRow(row)
                return

    def set_exporter(self, exporter, *, pdf=False):
        self.clear_bindings()
        self.fields.clear()
        self.exporter = exporter
        self.dialog.csv_scope.setParent(self.dialog)
        self.dialog.csv_scope.hide()
        previous = self.settings
        self.settings = qtw.QWidget()
        self.body_layout.replaceWidget(previous, self.settings)
        previous.hide()
        previous.deleteLater()
        if exporter is None and not pdf:
            self.settings.setEnabled(False)
            return
        csv = type(exporter) is CSVExporter
        with QtCore.QSignalBlocker(self.output_tabs), QtCore.QSignalBlocker(self.format_combo):
            self.output_tabs.setCurrentIndex(int(csv))
            if not csv:
                kind = QtGui.QPdfWriter if pdf else type(exporter)
                self.format_combo.setCurrentIndex(self.format_combo.findData(kind))
        self.format_form.setRowVisible(self.format_combo, not csv)
        self.heading.setText("Data" if csv else "Figure settings")
        self.heading.setVisible(not csv and not pdf)
        self.dialog.setMinimumHeight(150 if pdf else 230)
        form = qtw.QFormLayout(self.settings)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        if pdf:
            note = qtw.QLabel("One page sized to the visible plot canvas, with its current colours.")
            note.setWordWrap(True)
            form.addRow(note)
        elif csv:
            self._combo(form, "Data to export", "measurements")
            self.fields["measurements"].setAccessibleName("Data to export")
            self.fields["measurements"].setToolTip(
                "Exporting: " + csv_selection_description(self.dialog._qplot_owner, exporter)
                + "\nHidden measurements are included when selected."
            )
            self._combo(form, "Values", "values")
            self.dialog.csv_scope.setParent(self.settings)
            form.addRow("", self.dialog.csv_scope)
            self.dialog.csv_scope.show()
            selection = selected_csv_measurements(self.dialog._qplot_owner, exporter)
            if "columnMode" in exporter.params.names and selection and len(selection) > 1:
                self._combo(form, "Columns", "columnMode", [
                    ("X and Y for each trace", "(x,y) per plot"),
                    ("Shared X for all traces", "(x,y,y,y) for all plots"),
                ])
            self._combo(form, "File format", "separator", [("CSV (comma-separated)", "comma"),
                                                           ("TSV (tab-separated)", "tab")])
        else:
            sizes = qtw.QWidget()
            row = qtw.QHBoxLayout(sizes)
            row.setContentsMargins(0, 0, 0, 0)
            for name in ("width", "height"):
                if name == "height":
                    row.addWidget(_AspectRatioLink())
                    row.addWidget(qtw.QLabel("Height"))
                spin = qtw.QDoubleSpinBox() if type(exporter) is SVGExporter else qtw.QSpinBox()
                spin.setRange(1, 20_000)
                if isinstance(spin, qtw.QDoubleSpinBox):
                    spin.setDecimals(2)
                spin.setSuffix(" px")
                spin.setKeyboardTracking(False)
                spin.setAccessibleName(f"Export {name}")
                spin.setToolTip(f"Export {name} in pixels. Changing it also adjusts the other dimension.")
                spin.valueChanged.connect(lambda value, key=name: self._write(key, value))
                self.fields[name] = spin
                row.addWidget(spin, 1)
            form.addRow("Width", sizes)
            colour = _ExportBackgroundCombo(self.settings)
            colour.colourChanged.connect(lambda value: self._write("background", value))
            self.fields["background"] = colour
            form.addRow("Background", colour)
            if type(exporter) is ImageExporter:
                options = qtw.QHBoxLayout()
                options.setSpacing(18)
                options.addWidget(self._checkbox("Smooth edges", "antialias"))
                options.addWidget(self._checkbox("Invert brightness", "invertValue"))
                options.addStretch(1)
                form.addRow(options)
            else:
                self._check(form, "Scale line widths with image", "scaling stroke")
        for name in self.fields:
            parameter = exporter.params.child(name)
            parameter.sigValueChanged.connect(self._sync_values)
            self._parameters.append(parameter)
        self._sync_values()
        # New settings widgets otherwise remain hidden until the next event
        # delivery, so the dialog's first size hint can omit their contents.
        self.settings.show()
        self.settings.ensurePolished()
        self.body_layout.invalidate()
        self.dialog.fit_export_controls()

    def clear_bindings(self):
        for parameter in self._parameters:
            parameter.sigValueChanged.disconnect(self._sync_values)
        self._parameters.clear()

    def _combo(self, form, label, name, choices=None):
        combo = qtw.QComboBox()
        combo.setAccessibleName(f"Export {label.lower()}")
        combo.setMinimumContentsLength(12)
        combo.setSizeAdjustPolicy(qtw.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        if choices is None:
            limits = self.exporter.params.child(name).opts["limits"]
            choices = limits.items() if isinstance(limits, dict) else [(value, value) for value in limits]
        for text, value in choices:
            combo.addItem(text, value)
        combo.currentIndexChanged.connect(lambda _index: self._write(name, combo.currentData()))
        self.fields[name] = combo
        form.addRow(label, combo)

    def _check(self, form, label, name):
        form.addRow(self._checkbox(label, name))

    def _checkbox(self, label, name):
        check = qtw.QCheckBox(label)
        check.toggled.connect(lambda checked: self._write(name, checked))
        self.fields[name] = check
        return check

    def _write(self, name, value):
        if not self._syncing:
            self.exporter.params[name] = value

    def _sync_values(self, *_args):
        self._syncing = True
        try:
            for name, widget in self.fields.items():
                value = self.exporter.params[name]
                if isinstance(widget, _ExportBackgroundCombo):
                    if widget.colour() != QtGui.QColor(value):
                        widget.set_colour(value)
                elif isinstance(widget, qtw.QComboBox):
                    widget.setCurrentIndex(widget.findData(value))
                elif isinstance(widget, qtw.QCheckBox):
                    widget.setChecked(value)
                else:
                    widget.setValue(value)
        finally:
            self._syncing = False
