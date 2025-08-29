from PyQt5 import (
    QtWidgets as qtw,
    QtGui,
    QtCore,
    )

from qplot.tools.plot_tools import (
    subtract_mean,
    pass_filter,
    differentiate,
    fill_heatmap
    )
from .dropbox import expandingComboBox


def operations_widget(window):
    """
    Entry point for getting the operation options.
    Uses the window tupe to find the correct class to return.
    Window is also passed to the class
    
    PLEASE SEE BOTTOM OF FILE FOR WHICH OPTIONS ARE ADDED.

    Parameters
    ----------
    window : qplot.windows._plotWin.plotWidget
        The type of window: 1d, 2d, sweeper. Also serves as a reference inside
        the returned class

    Returns
    -------
    out : operations_options_base
        Returns the a QWidget containing QListWidgets, with options based on
        the inputted window type.

    """
    options_dict = {
        "plot1d" : operations_options_1d,
        "plot2d" : operations_options_2d,
        "sweeper": operations_options_sweep
        }
    
    out = options_dict[window.__class__.__name__](window)
    
    return out



class operations_options_base(qtw.QWidget):
    """
    Base class for all operation widgets, handles set up and all needed functions
    Classes inhertit this to set the options.
    
    """
    
    def __init__(self, main):
        super().__init__()
        self.parent = main
        
        # Make filter to propagate context menu to widgets.
        self.filter = self.parent.oper_dock.event_filter
        self.layout = self.parent.oper_dock.VBox_context(self.filter, self)

        self.layout.addWidget(qtw.QLabel("Data Operations"))

        # Controls order to perform and user inputs
        self.list_order = draggableListWidget()
        self.list_order.setDragDropMode(qtw.QAbstractItemView.InternalMove)
        self.list_order.setToolTip("Drag Items to Control Operation Order")
        self.layout.addWidget(self.list_order)

        # Buttons
        but_l = self.parent.oper_dock.HBox_context(self.filter, self)
        self.apply_but = qtw.QPushButton("Apply/Refresh")
        but_l.addWidget(self.apply_but)
        clear_but = qtw.QPushButton("Clear")
        clear_but.clicked.connect(self.hide_all)
        but_l.addWidget(clear_but)
        
        self.layout.addLayout(but_l)

        # Allows user to toggle options
        self.list_options = qtw.QListWidget()
        self.layout.addWidget(self.list_options)
        
        self.add_all_options()
        
        
    def add_option(self, name, func, input_type, default=""):
        """
        Adds an option to the list_options with a tickbox.
        Creates a row stored within the option, this holds the function and
        has a widget for the required input type. This data is read by 
        self.get_data()
        The secondary row is added or removed when the tickbox is clicked.

        Parameters
        ----------
        name : str
            Display name of the function.
        func : callable
            Function to be passed to do_operations in worker.
        input_type : type | list | tuple
            The required input type:
                bool - Makes checkbox
                str, int, float, - Makes Line edit (int/float only allow numbers)
                None - No input box made
            or a list/tuple of the options - Makes a dropbox with options

        """
        row = rowItem(name, None, bool) # Option with tick box
        self.list_options.addItem(row)
        self.list_options.setItemWidget(row, row.row_widget)
    
        # create item to add to active box. This data is fetched by self.get_data()
        row.operation_row = rowItem(name, func, input_type, default=default)
        row.input.stateChanged.connect(lambda state: 
                    self.add_or_remove_operation(state, row.operation_row)
                    )
        
            
    def add_or_remove_operation(self, add : int, item : "rowItem"):
        """
        Based on add value, adds or removes the item from the active operations
        and clears previous input on removal.

        Actually sets hidden or visible based to look as if removed.

        Parameters
        ----------
        add : int
            Whether to add or remove the box from the action operations
            For some ungodly reason, QTickBox.stateChanged emits 0 or 2 instead 
            of true or false. But if 0 -> False; if 2 -> True, so its fine.
        item : rowItem
            Which item is being worked on.

        """
        row = self.list_order.row(item)
        # Add item is not already there
        if row == -1:
            self.list_order.addItem(item)
            self.list_order.setItemWidget(item, item.row_widget)
            
        # Mimic complete removal (without garbage collector doing dumb things)
        if add:
            item.setHidden(False)
        else: # Remove previous input
            item.setHidden(True)
            item.reset()
    
    
    def add_all_options(self):
        """
        Fetches data from dict, self.operation_options, defined in children 
        classes.
        Then adds these to available options.

        """
        for key, subdict in self.common_operation_options.items():
            self.add_option(
                key, 
                subdict["func"], 
                subdict["input_type"],
                subdict.get("default", "")
                )
            
        for key, subdict in self.operation_options.items():
            self.add_option(
                key, 
                subdict["func"], 
                subdict["input_type"],
                subdict.get("default", "")
                )
        self.list_options.adjustSize()
            
    
    @QtCore.pyqtSlot()
    def hide_all(self):
        for i in range(self.list_options.count()):
            item = self.list_options.item(i)
            item.input.setChecked(False)
            
    
    def get_data(self):
        """
        Returns the function of the items in the active operation listWidget
        (self.list_order) from top to bottom. Also adds the user input to the
        functions ready for processing by qplot.tools.worker.loader.do_operations.

        Returns
        -------
        operations : list[callable]
            List of functions to be performed on the data during refresh.

        """
        operations = []
        for i in range(self.list_order.count()):
            item = self.list_order.item(i)
            if item.isHidden():
                continue
            
            output = item.output()
            if output == "": # Data not entered
                if hasattr(item.input, "placeholderText"):
                    output = item.input.placeholderText()
                    if output == "": # still blank
                        continue
                    
                    if isinstance(item.type, type):
                        output = item.type(output)
                else:
                    continue
            
            if output is None: # No input requried
                func = item.func
            else: # Add input
                # Some weird internal python stuff causes issues with lambda in loops
                func = func_with_input(item.func, output) 
                
            operations.append(func)
        return operations
 
    
def func_with_input(func, value):
    return lambda data: func(value, data)
 

class draggableListWidget(qtw.QListWidget):
    """
    QListWidgets have a know issue, when dragging the last time in the list 
    below itself, the item contents gets deleted. This class impliments a work 
    around to prevent that bug.
    """
    def dragMoveEvent(self, event):
        target = self.row(self.itemAt(event.pos()))
        current = self.currentRow()
        # Block drop below itself when it's the last item
        if target == current + 1 or (current == self.count() - 1 and target == -1):
            event.ignore()
        else:
            super().dragMoveEvent(event)
             
    def addItem(self, item, *args, **kargs):
        super().addItem(item, *args, **kargs)
        item.setToolTip(self.toolTip())
   
class rowItem(qtw.QListWidgetItem):
    """
    A QListWidgetItem which can have different input boxes based on input_type.
    
    Has 5 key attributes
        label : str - Display name of item
        func : callable - copy of function to be used in do_operations
        input : QWidget - The input widget
        reset : callable - resets input to default
        output : callabel - returns current value of input
    """
    
    def __init__(self, label, func, input_type, default=""):
        super().__init__()
        
        if callable(func):
            self.func = func
        elif func is not None:
            raise AssertionError("Func is not callable")
            
        self._label = qtw.QLabel(label)
        self.type = input_type
        
        self.row_widget = qtw.QWidget()
        row_layout = qtw.QHBoxLayout()
        self.row_widget.setLayout(row_layout)
        
        row_layout.addWidget(self._label)
        row_layout.setContentsMargins(5, 5, 5, 5)
        
        if input_type is bool: # on/off tickbox
            self.input = qtw.QCheckBox()
            self.reset = lambda: self.input.setChecked(False)
            self.output = lambda: bool(self.input.isChecked())
        
        elif input_type in [int, float, str]: # Textbox input
            self.input = qtw.QLineEdit()
            self.reset = lambda: self.input.setText("")
            self.output = lambda: (input_type(self.input.text()) 
                                   if self.input.text() else "")
            # Restrict user input to reduce errors
            if input_type != str:
                validator = QtGui.QDoubleValidator()
                validator.setNotation(QtGui.QDoubleValidator.ScientificNotation)
                validator.setLocale(QtCore.QLocale("C"))  # Avoids locale issues like commas
                self.input.setValidator(validator)
        
        elif input_type is None: # No input needed
            self.input = None
            self.reset = lambda: None
            self.output = lambda: None
            
        elif isinstance(input_type, (list, tuple)): # Select from options
            self.input = expandingComboBox()
            self.input.addItems(input_type)
            self.reset = lambda: self.input.setCurrentIndex(-1)
            self.output = lambda: self.input.currentText()
            
        else:
            raise TypeError(
                f"Invalid input type: {input_type}, must be int, float"
                ", str, bool, None, or an array of values.")
            
        row_layout.addStretch() # push to edges
        if self.input is not None:
            row_layout.addWidget(self.input)
            if isinstance(self.input, qtw.QCheckBox):
                self.input.setChecked(True if default == True else False)
            else:
                self.input.setPlaceholderText(str(default))
            
        # pyqt defaults height of widget to 0, what?
        self.setSizeHint(self.row_widget.sizeHint())
        

    @property 
    def label(self):
        return self._label.text()



###############################################################################
### AVAILABLE OPERATIONS ###

class operations_options_common(operations_options_base):
    # For common between all 3 window types.
    common_operation_options = {
        # display Name : {"func" : lambda input, data: function_to_run(input, data), 
        #                 "input_type" : input_type_needed,
        #                 "default" : (optional) None | default_value},
        "Limit Maxiumum" : {"func": lambda limit, data: pass_filter("low", limit, data),
                             "input_type": float},
        "Limit Minimum" : {"func": lambda limit, data: pass_filter("high", limit, data),
                             "input_type": float},
        }


class operations_options_1d(operations_options_common):
    operation_options = {
        # display Name : {"func" : lambda input, data: function_to_run(input, data), 
        #                 "input_type" : input_type_needed,
        #                 "default" : (optional) None | default_value},
        "dy/dx" : {"func" : lambda data: differentiate("x", data), 
                   "input_type" : None},
        }

class operations_options_2d(operations_options_common):
    operation_options = {
        # display Name : {"func" : lambda input, data: function_to_run(input, data), 
        #                 "input_type" : input_type_needed,
        #                 "default" : (optional) None | default_value},
        "Subtract Row Mean" : {"func" : lambda data: subtract_mean("x", data),
                               "input_type" : None},
        "Subtract Column Mean" : {"func" : lambda data: subtract_mean("y", data),
                                  "input_type" : None},
        "dz/dx" : {"func" : lambda data: differentiate("x", data), 
                   "input_type" : None},
        "dz/dy" : {"func" : lambda data: differentiate("y", data),
                   "input_type" : None},
        "Fill Below" : {"func" : lambda value, data: fill_heatmap("below", data, max_depth=value),
                        "input_type" : int,
                        "default" : 10},
        "Fill Right" : {"func" : lambda value, data: fill_heatmap("right", data, max_depth=value),
                        "input_type" : int,
                        "default" : 10},
        
        }
    
class operations_options_sweep(operations_options_common):
    operation_options = {
        # display Name : {"func" : lambda input, data: function_to_run(input, data), 
        #                 "input_type" : input_type_needed,
        #                 "default" : (optional) None | default_value},
        "Subtract Sweep Mean" : {"func" : lambda data: subtract_mean("x", data),
                                  "input_type" : None},
        "Subtract Fixed Mean" : {"func" : lambda data: subtract_mean("y", data),
                               "input_type" : None},
        "Differentiate Sweep" : {"func" : lambda data: differentiate("x", data), 
                                 "input_type" : None},
        "Differentiate Fixed" : {"func" : lambda data: differentiate("y", data),
                                 "input_type" : None},
        }