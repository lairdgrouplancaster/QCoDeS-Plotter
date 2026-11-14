"""Resizable dataset and property panes for the plot appearance editor."""

from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw


class AppearanceControlsStack(qtw.QStackedWidget):
    """Reserve a stable width, but scroll only the active property page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout().setSizeConstraint(qtw.QLayout.SizeConstraint.SetNoConstraint)

    def sizeHint(self):
        size = super().sizeHint()
        if self.currentWidget() is not None:
            size.setHeight(self.currentWidget().sizeHint().height())
        return size

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        if self.currentWidget() is not None:
            size.setHeight(self.currentWidget().minimumSizeHint().height())
        return size


class AppearanceControlsScroll(qtw.QScrollArea):
    def __init__(self, controls):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(qtw.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(controls)
        controls.setAutoFillBackground(False)
        for child in controls.findChildren(qtw.QWidget):
            if child.focusPolicy() != QtCore.Qt.FocusPolicy.NoFocus:
                child.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.FocusIn and isinstance(watched, qtw.QWidget):
            self.ensureWidgetVisible(watched)
        return super().eventFilter(watched, event)

    def sizeHint(self):
        return self.widget().sizeHint().boundedTo(QtCore.QSize(400, 440))

    def minimumSizeHint(self):
        return QtCore.QSize(max(220, self.widget().minimumSizeHint().width()), 40)


def build_appearance_panes(dialog):
    datasets = dialog.table.parentWidget()
    datasets.setTitle("Datasets")
    dialog.appearance_body.removeWidget(datasets)
    dialog.appearance_body.removeWidget(dialog.controls_stack)
    dialog.selected_group = qtw.QGroupBox("Selected datasets")
    layout = qtw.QVBoxLayout(dialog.selected_group)
    layout.setContentsMargins(8, 12, 8, 8)
    dialog.layout().removeWidget(dialog.selection_summary)
    dialog.selection_summary.setSizePolicy(qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Preferred)
    layout.addWidget(dialog.selection_summary)
    dialog.controls_scroll = AppearanceControlsScroll(dialog.controls_stack)
    layout.addWidget(dialog.controls_scroll)
    properties = qtw.QWidget()
    properties_layout = qtw.QVBoxLayout(properties)
    properties_layout.setContentsMargins(0, 0, 0, 0)
    properties_layout.setSpacing(8)
    properties_layout.addWidget(dialog.selected_group, 1)
    properties_layout.addWidget(dialog.whole_plot_group)
    dialog.appearance_splitter = qtw.QSplitter(QtCore.Qt.Orientation.Horizontal)
    dialog.appearance_splitter.setObjectName("plotAppearanceSplitter")
    dialog.appearance_splitter.setChildrenCollapsible(False)
    dialog.appearance_splitter.addWidget(datasets)
    dialog.appearance_splitter.addWidget(properties)
    dialog.appearance_body.addWidget(dialog.appearance_splitter)


def resize_appearance_panes(dialog, width=None):
    splitter = dialog.__dict__.get("appearance_splitter")
    if splitter is None:
        return
    threshold = max(680, splitter.widget(0).minimumSizeHint().width()
                    + splitter.widget(1).minimumSizeHint().width() + splitter.handleWidth() + 20)
    orientation = (QtCore.Qt.Orientation.Vertical if (dialog.width() if width is None else width) < threshold
                   else QtCore.Qt.Orientation.Horizontal)
    if splitter.orientation() != orientation:
        splitter.setOrientation(orientation)
        extent = splitter.height() if orientation == QtCore.Qt.Orientation.Vertical else splitter.width()
        splitter.setSizes([extent // 2, extent // 2] if orientation == QtCore.Qt.Orientation.Horizontal
                          else [max(100, extent // 3), max(100, extent * 2 // 3)])


def available_appearance_geometry(dialog):
    parent = dialog.parentWidget()
    screen = parent.screen() if parent is not None else dialog.screen()
    return screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1024, 768)


def fit_appearance_dialog(dialog):
    available = available_appearance_geometry(dialog).adjusted(16, 16, -16, -16)
    frame = dialog.frameGeometry().size() - dialog.size()
    width = min(760, available.width() - max(0, frame.width()))
    resize_appearance_panes(dialog, width)
    dialog.layout().activate()
    dialog.resize(width,
                  min(480, available.height() - max(0, frame.height())))
    dialog.layout().activate()
    splitter = dialog.appearance_splitter
    if splitter.orientation() == QtCore.Qt.Orientation.Horizontal:
        splitter.setSizes([splitter.width() // 2, splitter.width() // 2])
    position = dialog.frameGeometry()
    position.moveCenter(available.center())
    dialog.move(position.topLeft())
