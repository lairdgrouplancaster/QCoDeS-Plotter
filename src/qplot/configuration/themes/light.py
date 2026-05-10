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
            padding: 2px 10px;
            color: #000000;
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
            padding: 2px 10px;
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
            padding: 2px 10px;
            background-color: #f0f0f0;
        }
        QPushButton:pressed {
            border-style: solid;
            border-top-color: transparent;
            border-right-color: transparent;
            border-left-color: transparent;
            border-bottom-color: #6666cc;
            border-bottom-width: 1px;
            border-style: solid;
            color: #404040;
            padding: 2px 10px;
            background-color: #f0f0f0;
        }
        QPushButton:disabled {
            border-style: solid;
            border-top-color: transparent;
            border-right-color: transparent;
            border-left-color: transparent;
            border-bottom-color: #a0a0a0;
            border-bottom-width: 1px;
            border-style: solid;
            color: #a0a0a0;
            padding: 2px 10px;
            background-color: #f0f0f0;
        }
        QPushButton#closeAllPlotsButton {
            margin-right: 8px;
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
        QSplitter::handle:vertical {
            background-color: #c8c8c8;
            height: 6px;
            margin: 1px 0px;
        }
        QSplitter::handle:vertical:hover {
            background-color: #9a9a9a;
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
            border-bottom: 1px solid #50202060;
            background-color: #f0f0f0;
        }
        QMenuBar::item {
            color: #000000;
            spacing: 3px;
            padding: 4px 4px;
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
        QScrollArea {
            border: none;
        }
        QSlider::groove:horizontal {
        	height: 5px;
        	background: #500a84ff;
        }
        QSlider::groove:vertical {
        	height: 5px;
        	background: #500a84ff;
        }
        QSlider::handle:horizontal {
        	background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #b4b4b4, stop:1 #8f8f8f);
        	border: 1px solid #5c5c5c;
        	width: 14px;
        	margin: -5px 0;
        	border-radius: 7px;
        }                        
        QSlider::handle:vertical {
        	background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #b4b4b4, stop:1 #8f8f8f);
        	border: 1px solid #5c5c5c;
        	width: 14px;
        	margin: -5px 0;
        	border-radius: 7px;
        }
        QSlider::add-page:horizontal {
            background: #a0a0a0;
        }
        QSlider::add-page:vertical {
            background: #a0a0a0;
        }
        QSlider::sub-page:horizontal {
            background: #500a84ff;
        }
        QSlider::sub-page:vertical {
            background: #500a84ff;
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
        QListWidget {
            color: #a9b7c6;
            border: 1px solid #80202060;
            outline: 0;
        }
        QListWidget::item {
            padding: 0px 0px;
            border: none;
        }
        QListWidget::item:selected {
            background-color: #800a84ff;
            color: #ffffff;
        }
        QListWidget::item:hover {
            background-color: #500a84ff;
            color: #ffffff;
        }
        QMainWindow, QDialog, QWidget {
            font-size: 13px;
        }
        QToolBar {
            background-color: #eceef1;
            border: none;
            border-bottom: 1px solid #c9ced6;
            spacing: 6px;
            padding: 2px 8px;
        }
        QToolBar QLabel {
            color: #1f2933;
        }
        QPushButton {
            min-height: 20px;
            border: 1px solid #aeb4bd;
            border-radius: 5px;
            padding: 1px 10px;
            background-color: #f7f8fa;
            color: #111827;
        }
        QPushButton:hover {
            border: 1px solid #0a84ff;
            background-color: #ffffff;
            color: #111827;
        }
        QPushButton:pressed {
            background-color: #e6f1ff;
            color: #111827;
        }
        QToolButton#databaseIconButton, QToolButton#plotIconButton, QToolButton#exportIconButton {
            border: 1px solid #aeb4bd;
            border-radius: 5px;
            padding: 1px;
            background-color: #f7f8fa;
        }
        QToolButton#databaseIconButton:hover, QToolButton#plotIconButton:hover, QToolButton#exportIconButton:hover {
            border: 1px solid #0a84ff;
            background-color: #ffffff;
        }
        QToolButton#databaseIconButton:pressed, QToolButton#plotIconButton:pressed, QToolButton#exportIconButton:pressed {
            background-color: #e6f1ff;
        }
        QLineEdit, QDoubleSpinBox {
            min-height: 20px;
            border: 1px solid #b8bec7;
            border-radius: 5px;
            padding: 0 6px;
            background-color: #ffffff;
            color: #111827;
            selection-background-color: #0a84ff;
            selection-color: #ffffff;
        }
        QLineEdit#databasePathField {
            background-color: #f8f9fb;
            color: #3f4a59;
        }
        QCheckBox {
            spacing: 6px;
        }
        QCheckBox::indicator {
            width: 14px;
            height: 14px;
        }
        QCheckBox::indicator:checked {
            border: 1px solid #0a84ff;
            background-color: #0a84ff;
        }
        QCheckBox::indicator:unchecked {
            border: 1px solid #7a8491;
            background-color: #ffffff;
        }
        QSplitter::handle:vertical {
            background-color: #d7dbe1;
            height: 6px;
            margin: 2px 0px;
        }
        QSplitter::handle:vertical:hover {
            background-color: #0a84ff;
        }
        QTabWidget {
            background-color: #f0f2f5;
        }
        QTabWidget::pane {
            border: 1px solid #bfc5ce;
            border-radius: 6px;
            background-color: #ffffff;
            top: -1px;
        }
        QTabBar::tab {
            min-height: 24px;
            min-width: 84px;
            padding: 3px 10px;
            margin-left: 0px;
            border: 1px solid #c8ced6;
            border-left: none;
            background-color: #f7f8fa;
            color: #2d3745;
        }
        QTabBar::tab:first {
            border-left: 1px solid #c8ced6;
            border-top-left-radius: 6px;
            border-bottom-left-radius: 6px;
        }
        QTabBar::tab:last {
            border-top-right-radius: 6px;
            border-bottom-right-radius: 6px;
        }
        QTabBar::tab:selected {
            background-color: #dcecff;
            color: #075eb8;
            border: 1px solid #8dbdf4;
            padding: 3px 10px;
        }
        QTabBar::tab:hover {
            background-color: #eef6ff;
            color: #075eb8;
            border: 1px solid #8dbdf4;
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
        QTreeWidget, QTableView, QTableWidget {
            background-color: #ffffff;
            alternate-background-color: #f3f5f7;
            color: #111827;
            border: 1px solid #c8ced6;
            gridline-color: #d7dce3;
            outline: 0;
            font-size: 13px;
        }
        QTreeWidget::item, QTableView::item, QTableWidget::item {
            padding: 2px 6px;
            border: none;
        }
        QTreeWidget::item:selected, QTableView::item:selected, QTableWidget::item:selected {
            background-color: #0a84ff;
            color: #ffffff;
        }
        QTreeWidget::item:hover, QTableView::item:hover, QTableWidget::item:hover {
            background-color: #dcecff;
            color: #111827;
        }
        QHeaderView::section {
            background-color: #eef0f3;
            color: #1f2933;
            padding: 4px 8px;
            border: none;
            border-right: 1px solid #c8ced6;
            border-bottom: 1px solid #c8ced6;
        }
        QTableWidget#detailsTable::item {
            padding: 0px 6px;
        }
        QTableWidget#detailsTable QHeaderView::section {
            padding: 1px 6px;
            font-size: 12px;
        }
        QScrollBar:vertical {
            width: 10px;
            background: #eef0f3;
            margin: 0px;
            border: none;
        }
        QScrollBar:horizontal {
            height: 10px;
            background: #eef0f3;
            margin: 0px;
            border: none;
        }
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
            background: #8b929b;
            border-radius: 5px;
            min-height: 20px;
            min-width: 20px;
        }
        QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
            background: #66707c;
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
            index = itr % len(cls.colors)
            line.setPen(pg.mkPen(color=cls.colors[index]))
