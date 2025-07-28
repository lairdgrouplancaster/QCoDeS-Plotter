import pyqtgraph as pg

class light:
    main = """
        QMainWindow {
            background-color: #f0f0f0;
        }
        QDialog {
            background-color: #f0f0f0;
        }
        QColorDialog {
            background-color: #f0f0f0;
        }
        QTextEdit {
            background-color: #ffffff;
            color: #000000;
        }
        QPlainTextEdit {
            selection-background-color: #500a84ff;
            background-color: #ffffff;
            border-style: solid;
            border-top-color: transparent;
            border-right-color: transparent;
            border-left-color: transparent;
            border-bottom-color: transparent;
            border-width: 1px;
            color: #000000;
        }
        QPushButton {
            border-width: 1px; border-radius: 4px;
            border-color: rgb(160, 160, 160);
            border-style: solid;
            padding: 0 8px;
            color: #000000;
            padding: 2px;
            background-color: #f0f0f0;
        }
        QPushButton::default {
            border-style: inset;
            border-top-color: transparent;
            border-right-color: transparent;
            border-left-color: transparent;
            border-bottom-color: #0a84ff;
            border-width: 1px;
            color: #000000;
            padding: 2px;
            background-color: #f0f0f0;
        }
        QToolButton {
            border-style: solid;
            border-top-color: transparent;
            border-right-color: transparent;
            border-left-color: transparent;
            border-bottom-color: #500a84ff;
            border-bottom-width: 1px;
            border-style: solid;
            color: #000000;
            padding: 2px;
            background-color: #f0f0f0;
        }
        QToolButton:hover {
            border-style: solid;
            border-top-color: transparent;
            border-right-color: transparent;
            border-left-color: transparent;
            border-bottom-color: #9966cc;
            border-bottom-width: 2px;
            border-style: solid;
            color: #000000;
            padding-bottom: 1px;
            background-color: #f0f0f0;
        }
        QPushButton:hover {
            border-style: solid;
            border-top-color: #6666cc;
            border-right-color: #6666cc;
            border-left-color: #6666cc;
            border-bottom-color: #6666cc;
            border-bottom-width: 1px;
            border-style: solid;
            color: #000000;
            padding-bottom: 2px;
            background-color: #f0f0f0;
        }
        QPushButton:pressed {
            border-style: solid;
            border-top-color: transparent;
            border-right-color: transparent;
            border-left-color: transparent;
            border-bottom-color: #6666cc;
            border-bottom-width: 2px;
            border-style: solid;
            color: #404040;
            padding-bottom: 1px;
            background-color: #f0f0f0;
        }
        QPushButton:disabled {
            border-style: solid;
            border-top-color: transparent;
            border-right-color: transparent;
            border-left-color: transparent;
            border-bottom-color: #a0a0a0;
            border-bottom-width: 2px;
            border-style: solid;
            color: #a0a0a0;
            padding-bottom: 1px;
            background-color: #f0f0f0;
        }
        QLineEdit {
            border-width: 1px; border-radius: 4px;
            border-color: rgb(160, 160, 160);
            border-style: inset;
            padding: 0 8px;
            color: #000000;
            background: #ffffff;
            selection-background-color: #500a84ff;
            selection-color: #000000;
        }
        QLineEdit:disabled {
            background: #f0f0f0;
            color: #505050;
            border: 1px solid #a0a0a0;
        }
        QLabel {
            color: #000000;
        }
        QLCDNumber {
            color: #500a84ff;
        }
        QProgressBar {
            text-align: center;
            color: rgb(40, 40, 40);
            border-width: 1px;
            border-radius: 10px;
            border-color: rgb(160, 160, 160);
            border-style: inset;
            background-color: #ffffff;
        }
        QProgressBar::chunk {
            background-color: #500a84ff;
            border-radius: 5px;
        }
        QMenuBar {
            background-color: #f0f0f0;
        }
        QMenuBar::item {
            color: #000000;
            spacing: 3px;
            padding: 7px 4px;
            background: #f0f0f0;
        }
        QMenuBar::item:selected {
            background: #f0f0f0;
            color: #000000;
        }
        QMenu::item:selected {
            border-style: solid;
            border-top-color: transparent;
            border-right-color: transparent;
            border-left-color: #500a84ff;
            border-bottom-color: transparent;
            border-left-width: 2px;
            color: #000000;
            padding-left: 15px;
            padding-top: 4px;
            padding-bottom: 4px;
            padding-right: 7px;
            background-color: #e0e0e0;
        }
        QMenu::item {
            border-style: solid;
            border-top-color: transparent;
            border-right-color: transparent;
            border-left-color: transparent;
            border-bottom-color: transparent;
            border-bottom-width: 1px;
            border-style: solid;
            color: #000000;
            padding-left: 17px;
            padding-top: 4px;
            padding-bottom: 4px;
            padding-right: 17px;
            background-color: #f0f0f0;
        }
        QMenu {
            background-color: #f0f0f0;
        }
        QTabWidget {
            color: rgb(0, 0, 0);
            background-color: #f0f0f0;
        }
        QTabWidget::pane {
            border-color: rgb(140, 140, 140);
            background-color: #f0f0f0;
            border-style: solid;
            border-width: 1px;
            border-radius: 6px;
        }
        QTabBar::tab {
            border-style: solid;
            border-top-color: transparent;
            border-right-color: transparent;
            border-left-color: transparent;
            border-bottom-color: transparent;
            border-bottom-width: 1px;
            border-style: solid;
            color: #a0a0a0;
            padding: 3px;
            margin-left: 3px;
            background-color: #f0f0f0;
        }
        QTabBar::tab:selected, QTabBar::tab:last:selected, QTabBar::tab:hover {
            border-style: solid;
            border-top-color: transparent;
            border-right-color: transparent;
            border-left-color: transparent;
            border-bottom-color: #500a84ff;
            border-bottom-width: 2px;
            border-style: solid;
            color: #000000;
            padding-left: 3px;
            padding-bottom: 2px;
            margin-left: 3px;
            background-color: #f0f0f0;
        }
        QCheckBox {
            color: #000000;
            padding: 2px;
        }
        QCheckBox:disabled {
            color: #a0a0a0;
            padding: 2px;
        }
        QCheckBox:hover {
            border-radius: 4px;
            border-style: solid;
            padding-left: 1px;
            padding-right: 1px;
            padding-bottom: 1px;
            padding-top: 1px;
            border-width: 1px;
            border-color: rgb(140, 150, 160);
            background-color: #f0f0f0;
        }
        QCheckBox::indicator:checked {
            height: 10px;
            width: 10px;
            border-style: solid;
            border-width: 1px;
            border-color: #0a84ff;
            color: #000000;
            background-color: #0a84ff;
        }
        QCheckBox::indicator:unchecked {
            height: 10px;
            width: 10px;
            border-style: solid;
            border-width: 1px;
            border-color: #0a84ff;
            color: #000000;
            background-color: transparent;
        }
        QRadioButton {
            color: #000000;
            background-color: #f0f0f0;
            padding: 1px;
        }
        QRadioButton::indicator:checked {
            height: 10px;
            width: 10px;
            border-style: solid;
            border-radius: 5px;
            border-width: 1px;
            border-color: #500a84ff;
            color: #000000;
            background-color: #500a84ff;
        }
        QRadioButton::indicator:!checked {
            height: 10px;
            width: 10px;
            border-style: solid;
            border-radius: 5px;
            border-width: 1px;
            border-color: #500a84ff;
            color: #000000;
            background-color: transparent;
        }
        QStatusBar {
            color: #01664d;
        }
        QSpinBox {
            color: #000000;
            background-color: #ffffff;
        }
        QDoubleSpinBox {
            color: #000000;
            background-color: #ffffff;
            border: 1px solid #a0a0a0;
            border-radius: 4px;
        }
        QTimeEdit {
            color: #000000;
            background-color: #ffffff;
        }
        QDateTimeEdit {
            color: #000000;
            background-color: #ffffff;
        }
        QDateEdit {
            color: #000000;
            background-color: #ffffff;
        }
        QComboBox {
            color: #000000;
            background: #ffffff;
        }
        QComboBox:editable {
            background: #ffffff;
            color: #000000;
        }
        QComboBox:on {
            padding-top: 3px;
            padding-left: 4px;
        }
        QComboBox QAbstractItemView {
            border: 1px solid #500a84ff;
            background-color: #ffffff;
            selection-background-color: #500a84ff;
            selection-color: #000000;
            color: #000000;
        }
        QSlider::groove:horizontal {
            border: 1px solid #bbb;
            background: #f0f0f0;
            height: 10px;
            border-radius: 4px;
        }
        QSlider::handle:horizontal {
            background: #500a84ff;
            border: 1px solid #500a84ff;
            width: 18px;
            margin: -5px 0;
            border-radius: 6px;
        }
        QSlider::handle:horizontal:hover {
            background: #66cc99;
            border: 1px solid #66cc99;
        }
        QSlider::sub-page:horizontal {
            background: #500a84ff;
            border-radius: 4px;
        }
        QSlider::add-page:horizontal {
            background: #f0f0f0;
            border-radius: 4px;
        }
        QSlider::groove:vertical {
            border: 1px solid #bbb;
            background: #f0f0f0;
            width: 10px;
            border-radius: 4px;
        }
        QSlider::handle:vertical {
            background: #500a84ff;
            border: 1px solid #500a84ff;
            height: 18px;
            margin: 0 -5px;
            border-radius: 6px;
        }
        QSlider::handle:vertical:hover {
            background: #66cc99;
            border: 1px solid #66cc99;
        }
        QSlider::sub-page:vertical {
            background: #500a84ff;
            border-radius: 4px;
        }
        QSlider::add-page:vertical {
            background: #f0f0f0;
            border-radius: 4px;
        }
        QTreeView {
            background-color: #ffffff;
            alternate-background-color: #f0f0f0;
            color: #000000;
        }
        QTreeView::item:selected {
            background-color: #500a84ff;
            color: #000000;
        }
        QListView {
            background-color: #ffffff;
            alternate-background-color: #f0f0f0;
            color: #000000;
        }
        QListView::item:selected {
            background-color: #500a84ff;
            color: #000000;
        }
        QTableView {
            background-color: #ffffff;
            alternate-background-color: #f0f0f0;
            color: #000000;
            gridline-color: #cccccc;
        }
        QTableView::item:selected {
            background-color: #500a84ff;
            color: #000000;
        }
    """
    colors = [pg.mkColor(col) for col in ["red", "green", "blue", "black", "darkcyan", "darkorange"]]
    
    @classmethod
    def style_plotItem(cls, plot_win): 
        plot_item = plot_win.plot
        plot_win.widget.setBackground("w")
        
        pen = pg.mkPen("k")
        for side in ['left', 'bottom', 'right', 'top']:
            axis = plot_item.getAxis(side)
            axis.setPen(pen)
            axis.setTextPen(pen)
        
        plot_item.vb.gridPen = pg.mkPen(color='darkgray')  
        cls.set_line_colours(plot_item)
        
    @classmethod
    def set_line_colours(cls, plot_item):
        for itr, line in enumerate(plot_item.listDataItems()):
            index = itr - (itr // len(cls.colors))
            line.setPen(pg.mkPen(color=cls.colors[index]))
