from PyQt5 import (
    QtWidgets as qtw,
    QtCore
    )


class QDock_context(qtw.QDockWidget):
    """
    A custom QDockWidget designed to emulate a side QToolbar.
    
    
    """
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
    
    ### SUB CLASS LAYOUT TO CARRY CONTEXT MENU THROUGH ###
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
        """
        Event handler for right-clicking on a QDock_context
        Produces a context menu for toggling display of toolbars and dockwidgets

        Parameters
        ----------
        obj :
            Unused by required slot.
        event : PyQt5.QtCore.QEvent.ContextMenu
            
        Returns
        -------
        bool
            Whether to show context menu.

        """
        if event.type() == QtCore.QEvent.ContextMenu:
            menu = qtw.QMenu(self.main_window)

            # Find toolbar and dock widgets to add to context menu
            for toolbar in self.main_window.findChildren(qtw.QToolBar):
                menu.addAction(toolbar.toggleViewAction())

            for dock in self.main_window.findChildren(qtw.QDockWidget):
                menu.addAction(dock.toggleViewAction())

            # Display context menu
            menu.exec_(event.globalPos())
            return True
        return False