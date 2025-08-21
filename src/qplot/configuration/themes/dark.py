import pyqtgraph as pg

class dark:
    main = """
        QMainWindow {
        	background-color:#1e1d23;
        }
        QDialog {
        	background-color:#1e1d23;
        }
        QColorDialog {
        	background-color:#1e1d23;
        }
        QTextEdit {
        	background-color:#1e1d23;
        	color: #a9b7c6;
        }
        QPlainTextEdit {
        	selection-background-color:#007b50;
        	background-color:#1e1d23;
        	border-style: solid;
        	border-top-color: transparent;
        	border-right-color: transparent;
        	border-left-color: transparent;
        	border-bottom-color: transparent;
        	border-width: 1px;
        	color: #a9b7c6;
        }
        QPushButton{
        	border-width: 1px; border-radius: 4px;
        	border-color: rgb(58, 58, 58);
        	border-style: solid;
        	padding: 0 8px;
        	color: #a9b7c6;
        	padding: 2px;
        	background-color: #1e1d23;
        }
        QPushButton::default{
        	border-style: inset;
        	border-top-color: transparent;
        	border-right-color: transparent;
        	border-left-color: transparent;
        	border-bottom-color: #04b97f;
        	border-width: 1px;
        	color: #a9b7c6;
        	padding: 2px;
        	background-color: #1e1d23;
        }
        QToolButton {
        	border-style: solid;
        	border-top-color: transparent;
        	border-right-color: transparent;
        	border-left-color: transparent;
        	border-bottom-color: #04b97f;
        	border-bottom-width: 1px;
        	border-style: solid;
        	color: #a9b7c6;
        	padding: 2px;
        	background-color: #1e1d23;
        }
        QToolButton:hover{
        	border-style: solid;
        	border-top-color: transparent;
        	border-right-color: transparent;
        	border-left-color: transparent;
        	border-bottom-color: #37efba;
        	border-bottom-width: 2px;
        	border-style: solid;
        	color: #FFFFFF;
        	padding-bottom: 1px;
        	background-color: #1e1d23;
        }
        QPushButton:hover{
        	border-style: solid;
        	border-top-color: #37efba;
        	border-right-color: #37efba;
        	border-left-color: #37efba;
        	border-bottom-color: #37efba;
        	border-bottom-width: 1px;
        	border-style: solid;
        	color: #FFFFFF;
        	padding-bottom: 2px;
        	background-color: #1e1d23;
        }
        QPushButton:pressed{
        	border-style: solid;
        	border-top-color: transparent;
        	border-right-color: transparent;
        	border-left-color: transparent;
        	border-bottom-color: #37efba;
        	border-bottom-width: 2px;
        	border-style: solid;
        	color: #F0F0F0;
        	padding-bottom: 1px;
        	background-color: #1e1d23;
        }
        QPushButton:disabled{
        	border-style: solid;
        	border-top-color: transparent;
        	border-right-color: transparent;
        	border-left-color: transparent;
        	border-bottom-color: #808086;
        	border-bottom-width: 2px;
        	border-style: solid;
        	color: #808086;
        	padding-bottom: 1px;
        	background-color: #1e1d23;
        }
        QLineEdit {
        	border-width: 1px; border-radius: 4px;
        	border-color: rgb(58, 58, 58);
        	border-style: inset;
        	padding: 0 8px;
        	color: #a9b7c6;
        	background:#1e1d23;
        	selection-background-color:#007b50;
        	selection-color: #FFFFFF;
        }
        QLineEdit:disabled {
            background: #1e1d23;
            color: #808086;
            border: 1px solid #3a3a3a;
        }
        QLabel {
        	color: #a9b7c6;
        }
        QLCDNumber {
        	color: #37e6b4;
        }
        QProgressBar {
        	text-align: center;
        	color: rgb(240, 240, 240);
        	border-width: 1px; 
        	border-radius: 10px;
        	border-color: rgb(58, 58, 58);
        	border-style: inset;
        	background-color:#1e1d23;
        }
        QProgressBar::chunk {
        	background-color: #04b97f;
        	border-radius: 5px;
        }
        QMenuBar {
            border-bottom: 1px solid #50a0b0a0;
        	background-color: #1e1d23;
        }
        QMenuBar::item {
        	color: #a9b7c6;
          	spacing: 3px;
          	padding: 4px 4px;
          	background: #1e1d23;
        }
        
        QMenuBar::item:selected {
          	background:#1e1d23;
        	color: #FFFFFF;
        }
        QMenu::item:selected {
        	border-style: solid;
        	border-top-color: transparent;
        	border-right-color: transparent;
        	border-left-color: #04b97f;
        	border-bottom-color: transparent;
        	border-left-width: 2px;
        	color: #FFFFFF;
        	padding-left:15px;
        	padding-top:4px;
        	padding-bottom:4px;
        	padding-right:7px;
        	background-color: #1e1d23;
        }
        QMenu::item {
        	border-style: solid;
        	border-top-color: transparent;
        	border-right-color: transparent;
        	border-left-color: transparent;
        	border-bottom-color: transparent;
        	border-bottom-width: 1px;
        	border-style: solid;
        	color: #a9b7c6;
        	padding-left:17px;
        	padding-top:4px;
        	padding-bottom:4px;
        	padding-right:17px;
        	background-color: #1e1d23;
        }
        QMenu{
        	background-color:#1e1d23;
        }
        QTabWidget {
        	color:rgb(0,0,0);
        	background-color:#1e1d23;
        }
        QTabWidget::pane {
        		border-color: rgb(77,77,77);
        		background-color:#1e1d23;
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
        	color: #808086;
        	padding: 3px;
        	margin-left:3px;
        	background-color: #1e1d23;
        }
        QTabBar::tab:selected, QTabBar::tab:last:selected, QTabBar::tab:hover {
          	border-style: solid;
        	border-top-color: transparent;
        	border-right-color: transparent;
        	border-left-color: transparent;
        	border-bottom-color: #04b97f;
        	border-bottom-width: 2px;
        	border-style: solid;
        	color: #FFFFFF;
        	padding-left: 3px;
        	padding-bottom: 2px;
        	margin-left:3px;
        	background-color: #1e1d23;
        }
        
        QCheckBox {
        	color: #a9b7c6;
        	padding: 2px;
        }
        QCheckBox:disabled {
        	color: #808086;
        	padding: 2px;
        }
        
        QCheckBox:hover {
        	border-radius:4px;
        	border-style:solid;
        	padding-left: 1px;
        	padding-right: 1px;
        	padding-bottom: 1px;
        	padding-top: 1px;
        	border-width:1px;
        	border-color: rgb(87, 97, 106);
        	background-color:#1e1d23;
        }
        QCheckBox::indicator:checked {
        
        	height: 10px;
        	width: 10px;
        	border-style:solid;
        	border-width: 1px;
        	border-color: #04b97f;
        	color: #a9b7c6;
        	background-color: #04b97f;
        }
        QCheckBox::indicator:unchecked {
        
        	height: 10px;
        	width: 10px;
        	border-style:solid;
        	border-width: 1px;
        	border-color: #04b97f;
        	color: #a9b7c6;
        	background-color: transparent;
        }
        QRadioButton {
        	color: #a9b7c6;
        	background-color: #1e1d23;
        	padding: 1px;
        }
        QRadioButton::indicator:checked {
        	height: 10px;
        	width: 10px;
        	border-style:solid;
        	border-radius:5px;
        	border-width: 1px;
        	border-color: #04b97f;
        	color: #a9b7c6;
        	background-color: #04b97f;
        }
        QRadioButton::indicator:!checked {
        	height: 10px;
        	width: 10px;
        	border-style:solid;
        	border-radius:5px;
        	border-width: 1px;
        	border-color: #04b97f;
        	color: #a9b7c6;
        	background-color: transparent;
        }
        QStatusBar {
        	color:#027f7f;
        }
        QSpinBox {
        	color: #a9b7c6;	
        	background-color: #1e1d23;
        }
        QDoubleSpinBox {
            color: #a9b7c6;
            background-color: #1e1d23;
            border: 1px solid #3c3f41;
            border-radius: 4px;   
        }
        QTimeEdit {
        	color: #a9b7c6;	
        	background-color: #1e1d23;
        }
        QDateTimeEdit {
        	color: #a9b7c6;	
        	background-color: #1e1d23;
        }
        QDateEdit {
        	color: #a9b7c6;	
        	background-color: #1e1d23;
        }
        QComboBox {
        	color: #a9b7c6;	
        	background: #1e1d23;
        }
        
        QComboBox:editable {
        	background: #1e1d23;
        	color: #a9b7c6;
        	selection-background-color: #1e1d23;
        }
        QComboBox QAbstractItemView {
        	color: #a9b7c6;	
        	background: #1e1d23;
        	selection-color: #FFFFFF;
        	selection-background-color: #1e1d23;
        }
        QFontComboBox {
        	color: #a9b7c6;	
        	background-color: #1e1d23;
        }
        QToolBar {
            background-color: #1e1d23;
            border: 1px solid #3a3a3a;
        }
        QToolBox {
        	color: #a9b7c6;
        	background-color: #1e1d23;
        }
        QToolBox::tab {
        	color: #a9b7c6;
        	background-color: #1e1d23;
        }
        QToolBox::tab:selected {
        	color: #FFFFFF;
        	background-color: #1e1d23;
        }
        QScrollArea {
        	color: #ffffff;
        	background-color: #1e1d23;
            border: none;
        }
        QScrollArea QWidget {
            background-color: #1e1d23;
        }
        QSlider::groove:horizontal {
        	height: 5px;
        	background: #04b97f;
        }
        QSlider::groove:vertical {
        	width: 5px;
        	background: #04b97f;
        }
        QSlider::handle:horizontal {
        	background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #b4b4b4, stop:1 #8f8f8f);
        	border: 1px solid #5c5c5c;
        	width: 14px;
        	margin: -5px 0;
        	border-radius: 7px;
        }
        QSlider::handle:vertical {
        	background: qlineargradient(x1:1, y1:1, x2:0, y2:0, stop:0 #b4b4b4, stop:1 #8f8f8f);
        	border: 1px solid #5c5c5c;
        	height: 14px;
        	margin: 0 -5px;
        	border-radius: 7px;
        }
        QSlider::add-page:horizontal {
            background: white;
        }
        QSlider::add-page:vertical {
            background: white;
        }
        QSlider::sub-page:horizontal {
            background: #04b97f;
        }
        QSlider::sub-page:vertical {
            background: #04b97f;
        }
        QTreeWidget {
            background-color: #1e1d23;
            color: #a9b7c6;
            border: 1px solid #3a3a3a;
            alternate-background-color: #25242b;
            selection-background-color: #007b50;
            selection-color: #ffffff;
            show-decoration-selected: 1;
            font-size: 14px
        }
        QTreeWidget::item {
            padding: 2px 4px;
            margin: 0px;
            border: none;
            color: #a9b7c6;
        }
        QTreeWidget::item:selected {
            background-color: #007b50;
            color: #ffffff;
        }
        QTreeWidget::item:hover {
            background-color: #80007b50;
            color: #ffffff;
        }
        QTreeView::branch {
            background: transparent;
            border: none;
            margin: 0px;
        }
        QHeaderView::section {
            background-color: #2a2b30;
            color: #a9b7c6;
            padding: 2px 6px;
            border: 1px solid #3a3a3a;
        }
        QScrollBar:vertical {
            width: 8px;
            background: #2a2b30;
            margin: 0px;
            border: none;
        }
        QScrollBar:horizontal {
            height: 8px;
            background: #2a2b30;
            margin: 0px;
            border: none;
        }
        QScrollBar::groove:vertical, QScrollBar::groove:horizontal {
            background: #2a2b30;
        }
        QScrollBar::handle:vertical {
            background: #1e1d23;
            min-height: 16px;
            border: 1px solid #3a3a3a;
            border-radius: 2px;
        }
        QScrollBar::handle:horizontal {
            background: #1e1d23;
            min-width: 16px;
            border: 1px solid #3a3a3a;
            border-radius: 2px;
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
        QListWidget {
            background-color: #1e1d23;
            color: #a9b7c6;
            border: 1px solid #3a3a3a;
            outline: 0;
        }
        QListWidget::item {
            padding: 0px 0px;
            border: none;
        }
        QListWidget::item:selected {
            background-color: #80007b50;
            color: #ffffff;
        }
        QListWidget::item:hover {
            background-color: #50007b50;
            color: #ffffff;
        }
    """
    
    colors = [pg.mkColor(col) for col in ["red", "green", "blue", "white", "cyan", "yellow"]]
    
    @classmethod
    def style_plotItem(cls, plot_win):
        plot_item = plot_win.plot
        plot_win.widget.setBackground("k")
        
        pen = pg.mkPen("w")
        for side in ['left', 'bottom', 'right', 'top']:
            axis = plot_item.getAxis(side)
            axis.setPen(pen)
            axis.setTextPen(pen)
        plot_item.vb.gridPen = pg.mkPen(color='lightgray')  
        
        cls.set_line_colours(plot_item)
        
    @classmethod
    def set_line_colours(cls, plot_item):
        for itr, line in enumerate(plot_item.listDataItems()):
            index = itr % len(cls.colors)
            line.setPen(pg.mkPen(color=cls.colors[index]))
        
