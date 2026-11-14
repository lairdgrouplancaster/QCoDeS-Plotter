"""Compact, format-specific controls for the existing plot exporters."""

from typing import Any

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw
from pyqtgraph import ColorButton
from pyqtgraph.exporters import CSVExporter, ImageExporter, SVGExporter


class _AspectRatioLink(qtw.QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(20, 24)
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
        form = qtw.QFormLayout()
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.format_combo = qtw.QComboBox()
        for label, exporter in (("Image (PNG, TIFF, JPEG…)", ImageExporter),
                                ("SVG", SVGExporter), ("CSV data", CSVExporter)):
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

    def _format_changed(self):
        if self._syncing:
            return
        kind = self.format_combo.currentData()
        for row in range(self.dialog.ui.formatList.count()):
            if self.dialog.ui.formatList.item(row).expClass is kind:
                self.dialog.ui.formatList.setCurrentRow(row)
                return

    def set_exporter(self, exporter):
        for parameter in self._parameters:
            parameter.sigValueChanged.disconnect(self._sync_values)
        self._parameters.clear()
        self.fields.clear()
        self.exporter = exporter
        self.dialog.csv_scope.setParent(self.dialog)
        self.dialog.csv_scope.hide()
        previous = self.settings
        self.settings = qtw.QWidget()
        self.body_layout.replaceWidget(previous, self.settings)
        previous.hide()
        previous.deleteLater()
        if exporter is None:
            self.settings.setEnabled(False)
            return
        self._syncing = True
        try:
            self.format_combo.setCurrentIndex(self.format_combo.findData(type(exporter)))
        finally:
            self._syncing = False
        csv = type(exporter) is CSVExporter
        self.heading.setText("Data" if csv else "Image settings" if type(exporter) is ImageExporter else "SVG settings")
        form = qtw.QFormLayout(self.settings)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        if csv:
            self._combo(form, "Values", "values")
            self.dialog.csv_scope.setParent(self.settings)
            form.addRow("", self.dialog.csv_scope)
            self.dialog.csv_scope.show()
            if "columnMode" in exporter.params.names:
                self._combo(form, "Columns", "columnMode", [
                    ("X and Y for each trace", "(x,y) per plot"),
                    ("Shared X for all traces", "(x,y,y,y) for all plots"),
                ])
            self._combo(form, "Separator", "separator", [("Comma (,)", "comma"), ("Tab", "tab")])
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
            colour = ColorButton(self.settings, padding=4)
            colour.setFixedWidth(100)
            colour.setMinimumHeight(24)
            colour.setAccessibleName("Export background colour")
            colour.setToolTip("Choose the background colour and transparency.")
            colour.colorDialog.setParent(colour, QtCore.Qt.WindowType.Dialog)
            # Qt exposes the alpha editor through its label's buddy, rather
            # than an object name. Match Qt's own translated label.
            alpha_label = QtCore.QCoreApplication.translate("QColorDialog", "A&lpha channel:")
            for label in colour.colorDialog.findChildren(qtw.QLabel):
                if label.text() == alpha_label:
                    tip = ("Background opacity: 0 is fully transparent; 255 is fully opaque. "
                           "PNG and SVG retain transparency; JPEG does not.")
                    label.setToolTip(tip)
                    editor = label.buddy()
                    if editor is not None:
                        editor.setToolTip(tip)
                    break
            colour.sigColorChanging.connect(lambda button: self._write("background", button.color()))
            colour.sigColorChanged.connect(lambda button: self._write("background", button.color()))
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
        self.dialog.fit_export_controls()

    def _combo(self, form, label, name, choices=None):
        combo = qtw.QComboBox()
        combo.setAccessibleName(f"Export {label.lower()}")
        combo.setMinimumContentsLength(12)
        combo.setSizeAdjustPolicy(qtw.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        if choices is None:
            choices = [(value, value) for value in self.exporter.params.child(name).opts["limits"]]
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
                if isinstance(widget, ColorButton):
                    if widget.color() != QtGui.QColor(value):
                        widget.setColor(value)
                elif isinstance(widget, qtw.QComboBox):
                    widget.setCurrentIndex(widget.findData(value))
                elif isinstance(widget, qtw.QCheckBox):
                    widget.setChecked(value)
                else:
                    widget.setValue(value)
        finally:
            self._syncing = False
