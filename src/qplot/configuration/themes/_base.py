from __future__ import annotations

from dataclasses import asdict, dataclass
from string import Template

import pyqtgraph as pg


@dataclass(frozen=True)
class ThemePalette:
    window_bg: str
    panel_bg: str
    panel_alt_bg: str
    field_bg: str
    field_alt_bg: str
    button_bg: str
    button_hover_bg: str
    button_pressed_bg: str
    text: str
    text_strong: str
    muted_text: str
    disabled_text: str
    selection_bg: str
    selection_text: str
    accent: str
    accent_hover: str
    accent_soft: str
    accent_pressed: str
    border: str
    border_strong: str
    menu_hover_bg: str
    tab_bg: str
    tab_selected_bg: str
    tab_hover_bg: str
    table_bg: str
    table_alt_bg: str
    table_hover_bg: str
    header_bg: str
    status_text: str
    progress_text: str
    splitter: str
    scrollbar_bg: str
    scrollbar_handle: str
    scrollbar_handle_hover: str
    danger: str
    danger_bg: str
    danger_pressed_bg: str


_STYLESHEET_TEMPLATE = Template(
    """
    QMainWindow, QDialog, QColorDialog {
        background-color: $window_bg;
    }
    QMainWindow, QDialog, QWidget {
        font-size: 10pt;
    }
    QLabel {
        color: $text;
    }
    QLabel#mainEmptyStateTitle {
        color: $text_strong;
        font-weight: 600;
    }
    QLabel#mainEmptyStateDetail {
        color: $muted_text;
    }
    QTextEdit, QPlainTextEdit {
        background-color: $field_bg;
        color: $text;
        selection-background-color: $selection_bg;
        selection-color: $selection_text;
        border: 1px solid transparent;
    }
    QFrame#mainEmptyState {
        background-color: $panel_alt_bg;
        border-top: 1px solid $border;
        border-bottom: 1px solid $border;
    }
    QMenuBar {
        background-color: $window_bg;
        border-bottom: 1px solid $border;
    }
    QMenuBar::item {
        color: $text;
        spacing: 3px;
        padding: 4px 4px;
        background-color: $window_bg;
    }
    QMenuBar::item:selected {
        background-color: $menu_hover_bg;
        color: $text_strong;
    }
    QMenu {
        background-color: $window_bg;
    }
    QMenu::item {
        border-style: solid;
        border-color: transparent;
        border-left-width: 2px;
        color: $text;
        padding: 4px 17px;
        background-color: $window_bg;
    }
    QMenu::item:selected {
        border-left-color: $accent;
        color: $text_strong;
        padding-left: 15px;
        padding-right: 7px;
        background-color: $menu_hover_bg;
    }
    QStatusBar {
        background-color: $window_bg;
        color: $status_text;
        border-top: 1px solid $border;
    }
    QToolBar {
        background-color: $panel_bg;
        border: none;
        border-bottom: 1px solid $border;
        spacing: 6px;
        padding: 2px 8px;
    }
    QToolBar QLabel {
        color: $text;
    }
    QPushButton {
        min-height: 20px;
        border: 1px solid $border_strong;
        border-radius: 5px;
        padding: 1px 10px;
        color: $text;
        background-color: $button_bg;
    }
    QPushButton::default {
        border-bottom: 1px solid $accent;
    }
    QPushButton:hover {
        border: 1px solid $accent;
        color: $text_strong;
        background-color: $button_hover_bg;
    }
    QPushButton:pressed {
        color: $text_strong;
        background-color: $button_pressed_bg;
    }
    QPushButton:disabled {
        border: 1px solid $border;
        color: $disabled_text;
        background-color: $panel_bg;
    }
    QToolButton {
        border: 1px solid transparent;
        border-bottom: 1px solid $accent;
        color: $text;
        padding: 2px;
        background-color: $panel_bg;
    }
    QToolButton:hover {
        border: 1px solid transparent;
        border-bottom: 2px solid $accent_hover;
        color: $text_strong;
        padding-bottom: 1px;
        background-color: $panel_bg;
    }
    QToolButton#databaseIconButton,
    QToolButton#plotIconButton,
    QToolButton#exportIconButton,
    QToolButton#closeAllPlotsButton {
        border: 1px solid $border_strong;
        border-radius: 5px;
        padding: 1px;
        background-color: $button_bg;
    }
    QToolButton#databaseIconButton:hover,
    QToolButton#plotIconButton:hover,
    QToolButton#exportIconButton:hover {
        border: 1px solid $accent;
        background-color: $button_hover_bg;
    }
    QToolButton#databaseIconButton:pressed,
    QToolButton#plotIconButton:pressed,
    QToolButton#exportIconButton:pressed {
        background-color: $button_pressed_bg;
    }
    QToolButton#closeAllPlotsButton {
        margin-right: 8px;
    }
    QToolButton#closeAllPlotsButton:hover {
        border: 1px solid $danger;
        background-color: $danger_bg;
    }
    QToolButton#closeAllPlotsButton:pressed {
        background-color: $danger_pressed_bg;
    }
    QLineEdit,
    QSpinBox,
    QDoubleSpinBox,
    QTimeEdit,
    QDateTimeEdit,
    QDateEdit,
    QComboBox,
    QFontComboBox {
        min-height: 20px;
        border: 1px solid $border_strong;
        border-radius: 5px;
        padding: 0 6px;
        color: $text;
        background-color: $field_bg;
        selection-background-color: $selection_bg;
        selection-color: $selection_text;
    }
    QLineEdit:disabled,
    QSpinBox:disabled,
    QDoubleSpinBox:disabled,
    QComboBox:disabled {
        background-color: $panel_bg;
        color: $disabled_text;
        border: 1px solid $border;
    }
    QLineEdit#databasePathField {
        background-color: $field_alt_bg;
        color: $muted_text;
    }
    QComboBox:editable {
        background-color: $field_bg;
        color: $text;
    }
    QComboBox:on {
        padding-top: 3px;
        padding-left: 4px;
    }
    QComboBox QAbstractItemView {
        border: 1px solid $accent;
        background-color: $field_bg;
        selection-background-color: $selection_bg;
        selection-color: $selection_text;
        color: $text;
    }
    QProgressBar {
        text-align: center;
        color: $progress_text;
        border: 1px inset $border_strong;
        border-radius: 10px;
        background-color: $field_bg;
    }
    QProgressBar::chunk {
        background-color: $accent;
        border-radius: 5px;
    }
    QLCDNumber {
        color: $accent_hover;
    }
    QCheckBox {
        color: $text;
        padding: 2px;
        spacing: 6px;
    }
    QCheckBox:disabled {
        color: $disabled_text;
    }
    QCheckBox:hover {
        border-radius: 4px;
        border: 1px solid $border_strong;
        padding: 1px;
        background-color: $panel_bg;
    }
    QCheckBox::indicator {
        width: 14px;
        height: 14px;
    }
    QCheckBox::indicator:checked {
        border: 1px solid $accent;
        background-color: $accent;
    }
    QCheckBox::indicator:unchecked {
        border: 1px solid $border_strong;
        background-color: transparent;
    }
    QRadioButton {
        color: $text;
        background-color: $window_bg;
        padding: 1px;
    }
    QRadioButton::indicator:checked,
    QRadioButton::indicator:!checked {
        height: 10px;
        width: 10px;
        border: 1px solid $accent;
        border-radius: 5px;
    }
    QRadioButton::indicator:checked {
        background-color: $accent;
    }
    QRadioButton::indicator:!checked {
        background-color: transparent;
    }
    QSplitter::handle:vertical {
        background-color: $splitter;
        height: 6px;
        margin: 2px 0px;
    }
    QSplitter::handle:vertical:hover {
        background-color: $accent;
    }
    QTabWidget {
        color: $text;
        background-color: $window_bg;
    }
    QTabWidget::pane {
        border: 1px solid $border_strong;
        border-radius: 6px;
        background-color: $panel_bg;
        top: -1px;
    }
    QTabBar::tab {
        min-height: 24px;
        min-width: 84px;
        padding: 3px 10px;
        margin-left: 0px;
        border: 1px solid $border_strong;
        border-left: none;
        background-color: $tab_bg;
        color: $muted_text;
    }
    QTabBar::tab:first {
        border-left: 1px solid $border_strong;
        border-top-left-radius: 6px;
        border-bottom-left-radius: 6px;
    }
    QTabBar::tab:last {
        border-top-right-radius: 6px;
        border-bottom-right-radius: 6px;
    }
    QTabBar::tab:selected {
        background-color: $tab_selected_bg;
        color: $text_strong;
        border: 1px solid $accent_hover;
        padding: 3px 10px;
    }
    QTabBar::tab:hover {
        background-color: $tab_hover_bg;
        color: $text_strong;
        border: 1px solid $accent_hover;
        padding: 3px 10px;
    }
    QTabWidget#runDetailsTabs QTabBar::tab {
        min-height: 18px;
        min-width: 74px;
        padding: 1px 8px;
    }
    QTabWidget#runDetailsTabs QTabBar::tab:selected,
    QTabWidget#runDetailsTabs QTabBar::tab:hover {
        padding: 1px 8px;
    }
    QTreeView,
    QListView,
    QTreeWidget,
    QTableView,
    QTableWidget {
        background-color: $table_bg;
        alternate-background-color: $table_alt_bg;
        color: $text;
        border: 1px solid $border_strong;
        gridline-color: $border;
        outline: 0;
        selection-background-color: $selection_bg;
        selection-color: $selection_text;
        font-size: 10pt;
    }
    QTreeView::item,
    QListView::item,
    QTreeWidget::item,
    QTableView::item,
    QTableWidget::item {
        padding: 2px 6px;
        border: none;
        color: $text;
    }
    QTreeView::item:selected,
    QListView::item:selected,
    QTreeWidget::item:selected,
    QTableView::item:selected,
    QTableWidget::item:selected {
        background-color: $selection_bg;
        color: $selection_text;
    }
    QTreeView::item:hover,
    QListView::item:hover,
    QTreeWidget::item:hover,
    QTableView::item:hover,
    QTableWidget::item:hover {
        background-color: $table_hover_bg;
        color: $text_strong;
    }
    QTreeView::branch {
        background: transparent;
        border: none;
        margin: 0px;
    }
    QHeaderView::section {
        background-color: $header_bg;
        color: $text;
        padding: 4px 8px;
        border: none;
        border-right: 1px solid $border_strong;
        border-bottom: 1px solid $border_strong;
    }
    QTableWidget#detailsTable::item {
        padding: 0px 6px;
    }
    QTableWidget#detailsTable QHeaderView::section {
        padding: 1px 6px;
        font-size: 9pt;
    }
    QListWidget {
        background-color: $table_bg;
        color: $text;
        border: 1px solid $border_strong;
        outline: 0;
    }
    QListWidget::item {
        padding: 0px 0px;
        border: none;
    }
    QListWidget::item:selected {
        background-color: $selection_bg;
        color: $selection_text;
    }
    QListWidget::item:hover {
        background-color: $table_hover_bg;
        color: $text_strong;
    }
    QScrollArea {
        color: $text;
        background-color: $window_bg;
        border: none;
    }
    QScrollArea QWidget {
        background-color: $window_bg;
    }
    QScrollBar:vertical {
        width: 10px;
        background: $scrollbar_bg;
        margin: 0px;
        border: none;
    }
    QScrollBar:horizontal {
        height: 10px;
        background: $scrollbar_bg;
        margin: 0px;
        border: none;
    }
    QScrollBar::handle:vertical,
    QScrollBar::handle:horizontal {
        background: $scrollbar_handle;
        border-radius: 5px;
        min-height: 20px;
        min-width: 20px;
        border: none;
    }
    QScrollBar::handle:vertical:hover,
    QScrollBar::handle:horizontal:hover {
        background: $scrollbar_handle_hover;
    }
    QScrollBar::add-line:vertical,
    QScrollBar::sub-line:vertical,
    QScrollBar::add-line:horizontal,
    QScrollBar::sub-line:horizontal,
    QScrollBar::up-arrow:vertical,
    QScrollBar::down-arrow:vertical,
    QScrollBar::left-arrow:horizontal,
    QScrollBar::right-arrow:horizontal {
        width: 0;
        height: 0;
        background: none;
        border: none;
    }
    QSlider::groove:horizontal {
        height: 5px;
        background: $accent;
    }
    QSlider::groove:vertical {
        width: 5px;
        background: $accent;
    }
    QSlider::handle:horizontal,
    QSlider::handle:vertical {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #b4b4b4, stop:1 #8f8f8f);
        border: 1px solid #5c5c5c;
        width: 14px;
        height: 14px;
        margin: -5px 0;
        border-radius: 7px;
    }
    QSlider::add-page:horizontal,
    QSlider::add-page:vertical {
        background: $disabled_text;
    }
    QSlider::sub-page:horizontal,
    QSlider::sub-page:vertical {
        background: $accent;
    }
    """
)


def build_stylesheet(palette: ThemePalette) -> str:
    """
    Builds qPlot's shared application stylesheet from a palette.

    """
    return _STYLESHEET_TEMPLATE.substitute(asdict(palette))


def color_list(names: list[str]) -> list:
    """
    Converts pyqtgraph color names into reusable QColor instances.

    """
    return [pg.mkColor(color) for color in names]


class PlotTheme:
    """
    Shared pyqtgraph styling for qPlot themes.

    """

    plot_background = "w"
    plot_foreground = "k"
    plot_grid = "darkgray"
    colors = color_list(["red", "green", "blue", "black", "darkcyan", "darkorange"])

    @classmethod
    def style_plotItem(cls, plot_win):
        plot_item = plot_win.plot
        plot_win.widget.setBackground(cls.plot_background)

        pen = pg.mkPen(cls.plot_foreground)
        for side in ["left", "bottom", "right", "top"]:
            axis = plot_item.getAxis(side)
            axis.setPen(pen)
            axis.setTextPen(pen)

        plot_item.vb.gridPen = pg.mkPen(color=cls.plot_grid)
        cls.set_line_colours(plot_item)

    @classmethod
    def set_line_colours(cls, plot_item):
        for index, line in enumerate(plot_item.listDataItems()):
            color = cls.colors[index % len(cls.colors)]
            line.setPen(pg.mkPen(color=color))
