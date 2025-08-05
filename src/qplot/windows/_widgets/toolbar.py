from PyQt5 import (
    QtWidgets as qtw,
    QtCore
    )


class QDock_context(qtw.QDockWidget):
    def __init__(self, *args, **kargs):
        super().__init__(*args, **kargs)
        
        self.setTitleBarWidget(qtw.QWidget(self))
        
        self.event_filter = contextMenuFilter(self, self.parent())
        self.installEventFilter(self.event_filter)
        
        core_widget = qtw.QFrame()
        self.setWidget(core_widget)
        
        self.layout = self.VBox_context(self.event_filter, core_widget)
        
        
    def addLayout(self, *args, **kargs):
        layout = self.HBox_context(self.event_filter, *args, **kargs)
        self.layout.addLayout(layout)
        return layout
        
    def addWidget(self, *args, **kargs):
        self.layout.addWidget(*args, **kargs)
    
    class VBox_context(qtw.QVBoxLayout):
        def __init__(self, event_filter, *args, **kargs):
            super().__init__(*args, **kargs)
            self.event_filter = event_filter
            
        def addWidget(self, widget, *args, **kargs):
            widget.installEventFilter(self.event_filter)
            for child in self.findChildren(qtw.QWidget):
                child.installEventFilter(self.event_filter)

            super().addWidget(widget, *args, **kargs)
            
    class HBox_context(qtw.QHBoxLayout):
        def __init__(self, event_filter, *args, **kargs):
            super().__init__(*args, **kargs)
            self.event_filter = event_filter
            
        def addWidget(self, widget, *args, **kargs):
            widget.installEventFilter(self.event_filter)
            for child in self.findChildren(qtw.QWidget):
                child.installEventFilter(self.event_filter)
            
            super().addWidget(widget, *args, **kargs)
            
                
class contextMenuFilter(QtCore.QObject):
    """Filter to show toggle menu from any widget inside the dock."""
    def __init__(self, parent, main_window):
        super().__init__(parent)
        self.main_window = main_window

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.ContextMenu:
            menu = qtw.QMenu(self.main_window)

            for toolbar in self.main_window.findChildren(qtw.QToolBar):
                menu.addAction(toolbar.toggleViewAction())

            for dock in self.main_window.findChildren(qtw.QDockWidget):
                menu.addAction(dock.toggleViewAction())

            menu.exec_(event.globalPos())
            return True
        return False