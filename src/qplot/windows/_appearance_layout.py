"""Resizable dataset and property panes for the plot appearance editor."""

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw


class _AppearanceSplitterHandle(qtw.QSplitterHandle):
    """Draw a hairline in the centre of the native drag target."""

    def paintEvent(self, event: QtGui.QPaintEvent | None) -> None:
        palette = self.palette()
        if self.underMouse():
            colour = palette.highlight().color()
            colour.setAlphaF(0.70)
        else:
            colour = palette.windowText().color()
            colour.setAlphaF(0.20)
        line = self.rect()
        if self.orientation() == QtCore.Qt.Orientation.Horizontal:
            line = QtCore.QRect(line.center().x(), 0, 1, line.height())
        else:
            line = QtCore.QRect(0, line.center().y(), line.width(), 1)
        painter = QtGui.QPainter(self)
        painter.fillRect(line, colour)


class _AppearanceSplitter(qtw.QSplitter):
    def createHandle(self) -> qtw.QSplitterHandle:
        return _AppearanceSplitterHandle(self.orientation(), self)


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

    def hasHeightForWidth(self):
        page = self.currentWidget()
        return page is not None and page.hasHeightForWidth()

    def heightForWidth(self, width):
        page = self.currentWidget()
        if page is None:
            return -1
        return self.page_height_for_width(page, width)

    @staticmethod
    def page_height_for_width(page, width):
        height = page.heightForWidth(width)
        return height if height >= 0 else page.sizeHint().height()


class AppearanceControlsScroll(qtw.QScrollArea):
    def __init__(self, controls):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(qtw.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(controls)
        controls.setAutoFillBackground(False)
        controls.currentChanged.connect(lambda _index: self.updateGeometry())
        for child in controls.findChildren(qtw.QWidget):
            if child.focusPolicy() != QtCore.Qt.FocusPolicy.NoFocus:
                child.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.FocusIn and isinstance(watched, qtw.QWidget):
            self.ensureWidgetVisible(watched)
        return super().eventFilter(watched, event)

    def viewportEvent(self, event):
        result = super().viewportEvent(event)
        if event.type() in (QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.LayoutRequest):
            controls = self.widget()
            if controls is not None:
                # A scrollbar can make a drawing row wrap and thereby keep itself visible.
                # Decide whether the page fits at the width it has with the bar hidden.
                available = self.maximumViewportSize()
                fits = controls.heightForWidth(available.width()) <= available.height()
                policy = (QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff if fits
                          else QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
                if self.verticalScrollBarPolicy() != policy:
                    self.setVerticalScrollBarPolicy(policy)
        return result

    def sizeHint(self):
        size = self.widget().sizeHint()
        size.setHeight(self.heightForWidth(size.width()))
        return size

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return max(40, self.widget().heightForWidth(width))

    def minimumSizeHint(self):
        return QtCore.QSize(max(220, self.widget().minimumSizeHint().width()), self.fontMetrics().height() + 4)


def build_appearance_panes(dialog):
    datasets = dialog.table.parentWidget()
    datasets.setTitle("Datasets")
    dialog.appearance_body.removeWidget(datasets)
    dialog.appearance_body.removeWidget(dialog.controls_stack)
    dialog.selected_group = qtw.QGroupBox("Selected datasets")
    layout = qtw.QVBoxLayout(dialog.selected_group)
    layout.setContentsMargins(8, 8, 8, 6)
    dialog.layout().removeWidget(dialog.selection_summary)
    dialog.selection_summary.setSizePolicy(qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Preferred)
    layout.addWidget(dialog.selection_summary)
    dialog.controls_scroll = AppearanceControlsScroll(dialog.controls_stack)
    layout.addWidget(dialog.controls_scroll)
    properties = qtw.QWidget()
    properties_layout = qtw.QVBoxLayout(properties)
    properties_layout.setContentsMargins(0, 0, 0, 0)
    properties_layout.setSpacing(8)
    properties_layout.addWidget(dialog.selected_group)
    properties_layout.addStretch(1)
    dialog.whole_plot_group.setSizePolicy(qtw.QSizePolicy.Policy.Preferred, qtw.QSizePolicy.Policy.Fixed)
    properties_layout.addWidget(dialog.whole_plot_group)
    dialog.appearance_splitter = _AppearanceSplitter(QtCore.Qt.Orientation.Horizontal)
    dialog.appearance_splitter.setObjectName("plotAppearanceSplitter")
    dialog.appearance_splitter.setHandleWidth(9)
    dialog.appearance_splitter.setChildrenCollapsible(False)
    dialog.appearance_splitter.addWidget(datasets)
    dialog.appearance_splitter.addWidget(properties)
    dialog.appearance_body.addWidget(dialog.appearance_splitter)


def resize_appearance_panes(dialog):
    splitter = dialog.__dict__.get("appearance_splitter")
    if splitter is None:
        return
    margins = dialog.layout().contentsMargins() + dialog.appearance_body.contentsMargins()
    space = dialog.contentsRect().width() - margins.left() - margins.right() - splitter.handleWidth()
    properties_width = splitter.widget(1).sizeHint().width()
    horizontal = space >= _dataset_pane_width(dialog, fit_names=False) + properties_width
    orientation = QtCore.Qt.Orientation.Horizontal if horizontal else QtCore.Qt.Orientation.Vertical
    if splitter.orientation() != orientation:
        splitter.setOrientation(orientation)
        if horizontal:
            splitter.setSizes([space - properties_width, properties_width])
        else:
            extent = splitter.height()
            splitter.setSizes([max(100, extent // 3), max(100, extent * 2 // 3)])


def available_appearance_geometry(dialog):
    parent = dialog.parentWidget()
    screen = parent.screen() if parent is not None else dialog.screen()
    return screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1024, 768)


def _dataset_pane_width(dialog, *, fit_names):
    datasets = dialog.appearance_splitter.widget(0)
    if fit_names and not datasets.isVisible():
        # Opening on Axes or Colour scale can leave the hidden Add/Remove row's size cached.
        datasets.layout().invalidate()
        datasets.layout().activate()
    table = dialog.table
    measurement = dialog._COL_MEASUREMENT
    header = table.horizontalHeader()
    # Keep the header readable beside the controls. Fit full names on opening.
    column_width = header.sectionSizeHint(measurement)
    if fit_names:
        column_width = max(column_width, table.sizeHintForColumn(measurement))
    other_columns = sum(table.columnWidth(i) for i in range(table.columnCount()) if i != measurement)
    margins = datasets.contentsMargins() + datasets.layout().contentsMargins()
    width = (column_width + other_columns + margins.left() + margins.right()
             + 2 * table.frameWidth() + table.verticalScrollBar().sizeHint().width())
    hint = datasets.sizeHint() if fit_names else datasets.minimumSizeHint()
    return max(width, hint.width())


def _dataset_pane_height(dialog, visible_rows):
    datasets = dialog.appearance_splitter.widget(0)
    table = dialog.table
    rows_height = sum(table.rowHeight(row) for row in range(min(visible_rows, table.rowCount())))
    return (datasets.sizeHint().height() - table.sizeHint().height()
            + table.horizontalHeader().height() + rows_height + 2 * table.frameWidth())


def fit_appearance_dialog(dialog, *, initial):
    available = available_appearance_geometry(dialog).adjusted(16, 16, -16, -16)
    frame = dialog.frameGeometry().size() - dialog.size()
    max_width = available.width() - max(0, frame.width())
    max_height = available.height() - max(0, frame.height())
    splitter = dialog.appearance_splitter
    properties = splitter.widget(1)
    body_margins = dialog.appearance_body.contentsMargins()
    page = dialog.pages.currentWidget()
    page_chrome = dialog.size() - page.size()
    chrome_width = page_chrome.width() + body_margins.left() + body_margins.right()
    chrome_height = page_chrome.height() + body_margins.top() + body_margins.bottom()
    if initial:
        list_width = _dataset_pane_width(dialog, fit_names=True)
        properties_width = properties.sizeHint().width()
        ideal = list_width + properties_width + chrome_width + splitter.handleWidth()
        dialog.resize(min(max_width, ideal), dialog.height())
        dialog.layout().activate()
        resize_appearance_panes(dialog)
        if splitter.orientation() == QtCore.Qt.Orientation.Horizontal:
            space = dialog.width() - chrome_width - splitter.handleWidth()
            splitter.setSizes([max(0, space - properties_width), properties_width])

    horizontal = splitter.orientation() == QtCore.Qt.Orientation.Horizontal
    stacked_panes = not horizontal and not isinstance(page, qtw.QScrollArea)
    if isinstance(page, qtw.QScrollArea) and page.widget() is not None:
        height = page_chrome.height() + page.widget().sizeHint().height()
    else:
        list_height = _dataset_pane_height(dialog, visible_rows=8)
        prop_width = properties.width() if horizontal else dialog.width() - chrome_width
        prop_height = properties.layout().totalHeightForWidth(prop_width)
        if prop_height < 0:
            prop_height = properties.sizeHint().height()
        if len(dialog._dataset_kinds) > 1:
            # Reserve the taller editor at this width without stretching the
            # shorter editor or giving its scroll area an unnecessary scrollbar.
            group = dialog.selected_group
            margins = group.contentsMargins() + group.layout().contentsMargins()
            width = prop_width - margins.left() - margins.right()
            stack = dialog.controls_stack
            tallest = max(stack.page_height_for_width(stack.widget(i), width) for i in range(stack.count()))
            prop_height += max(0, tallest - dialog.controls_scroll.heightForWidth(width))
        if horizontal:
            height = chrome_height + max(list_height, prop_height)
        else:
            height = chrome_height + list_height + splitter.handleWidth() + prop_height
    dialog.resize(dialog.width(), min(max_height, height))
    if stacked_panes:
        space = dialog.height() - chrome_height - splitter.handleWidth()
        two_rows = _dataset_pane_height(dialog, visible_rows=2)
        top = min(list_height, max(two_rows, space - prop_height))
        splitter.setSizes([top, max(0, space - top)])
    position = dialog.frameGeometry()
    if initial:
        position.moveCenter(available.center())
    elif position.bottom() > available.bottom():
        position.moveBottom(available.bottom())
    dialog.move(position.topLeft())
