from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol, TypeAlias, cast, overload

from PyQt6 import (
    QtCore,
    QtGui,
)
from PyQt6 import (
    QtWidgets as qtw,
)

from qplot.tools.operation_registry import operation_specs_for

from .dropbox import expandingComboBox

OperationKind: TypeAlias = Literal["plot1d", "plot2d", "sweeper"]
OperationFunc: TypeAlias = Callable[..., Any]
OperationInputType: TypeAlias = type | Sequence[str] | None


class OperationsDock(Protocol):
    event_filter: QtCore.QObject

    def VBox_context(self, *args: Any) -> qtw.QVBoxLayout: ...

    def HBox_context(self, *args: Any) -> qtw.QHBoxLayout: ...


class OperationsWindow(Protocol):
    oper_dock: OperationsDock


def operations_widget(window: Any) -> "operations_options_base":
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
    options_dict: dict[str, type[operations_options_base]] = {
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
    
    operation_kind: OperationKind

    def __init__(self, main: OperationsWindow):
        super().__init__()
        self._window = main
        
        # Make filter to propagate context menu to widgets.
        self.filter = self._window.oper_dock.event_filter
        self.main_layout = self._window.oper_dock.VBox_context(self.filter, self)

        self.main_layout.addWidget(qtw.QLabel("Data Operations"))

        # Controls order to perform and user inputs
        self.list_order = draggableListWidget()
        self.list_order.setDragDropMode(qtw.QAbstractItemView.DragDropMode.InternalMove)
        self.list_order.setToolTip("Drag Items to Control Operation Order")
        self.main_layout.addWidget(self.list_order)

        # Buttons
        but_l = self._window.oper_dock.HBox_context(self.filter)
        self.apply_but = qtw.QPushButton("Apply/Refresh")
        self.apply_but.setToolTip("Apply selected operations and refresh the plot")
        but_l.addWidget(self.apply_but)
        clear_but = qtw.QPushButton("Clear")
        clear_but.setToolTip("Clear all selected operations")
        clear_but.clicked.connect(self.hide_all)
        but_l.addWidget(clear_but)
        
        self.main_layout.addLayout(but_l)

        # Allows user to toggle options
        self.list_options = qtw.QListWidget()
        self.main_layout.addWidget(self.list_options)
        
        self.add_all_options()
        
        
    def add_option(
        self,
        name: str,
        func: OperationFunc,
        input_type: OperationInputType,
        default: object = "",
        ) -> None:
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
        row = optionToggleRowItem(name, None, bool) # Option with tick box
        self.list_options.addItem(row)
        self.list_options.setItemWidget(row, row.row_widget)
    
        # create item to add to active box. This data is fetched by self.get_data()
        row.operation_row = rowItem(name, func, input_type, default=default)
        row.input.stateChanged.connect(lambda state: 
                    self.add_or_remove_operation(state, row.operation_row)
                    )
        
            
    def add_or_remove_operation(self, add : int, item : "rowItem") -> None:
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
    
    
    def add_all_options(self) -> None:
        """
        Fetches data from dict, self.operation_options, defined in children 
        classes.
        Then adds these to available options.

        """
        for spec in operation_specs_for(self.operation_kind):
            self.add_option(
                spec.name,
                spec.func,
                cast(OperationInputType, spec.input_type),
                spec.default,
                )
        self.list_options.adjustSize()
            
    
    @QtCore.pyqtSlot()
    def hide_all(self) -> None:
        for i in range(self.list_options.count()):
            item = cast(optionToggleRowItem, self.list_options.item(i))
            item.input.setChecked(False)
            
    
    def get_data(self) -> list[OperationFunc]:
        """
        Returns the function of the items in the active operation listWidget
        (self.list_order) from top to bottom. Also adds the user input to the
        functions ready for processing by qplot.tools.worker.loader.do_operations.

        Returns
        -------
        operations : list[callable]
            List of functions to be performed on the data during refresh.

        """
        operations: list[OperationFunc] = []
        for i in range(self.list_order.count()):
            item = cast(rowItem, self.list_order.item(i))
            if item.isHidden():
                continue

            input_widget = item.input
            if (
                    isinstance(input_widget, qtw.QLineEdit)
                    and input_widget.text()
                    and not input_widget.hasAcceptableInput()
                    ):
                continue

            try:
                output = item.output()
            except (TypeError, ValueError, OverflowError):
                continue

            if output == "": # Data not entered
                if hasattr(input_widget, "placeholderText"):
                    output = cast(Any, input_widget).placeholderText()
                    if output == "": # still blank
                        continue
                    
                    if isinstance(item.input_type, type):
                        try:
                            output = item.input_type(output)
                        except (TypeError, ValueError, OverflowError):
                            continue
                else:
                    continue
            
            if output is None: # No input requried
                func = item.func
            else: # Add input
                # Some weird internal python stuff causes issues with lambda in loops
                func = func_with_input(item.func, output) 
                
            operations.append(func)
        return operations
 
    
def func_with_input(func: OperationFunc, value: object) -> OperationFunc:
    return lambda data: func(value, data)
 

class draggableListWidget(qtw.QListWidget):
    """
    QListWidgets have a know issue, when dragging the last time in the list 
    below itself, the item contents gets deleted. This class impliments a work 
    around to prevent that bug.
    """
    def dragMoveEvent(self, event: QtGui.QDragMoveEvent | None) -> None:
        if event is None:
            return

        target = self.row(self.itemAt(cast(Any, event).pos()))
        current = self.currentRow()
        # Block drop below itself when it's the last item
        if target == current + 1 or (current == self.count() - 1 and target == -1):
            event.ignore()
        else:
            super().dragMoveEvent(event)
             
    @overload
    def addItem(self, item: qtw.QListWidgetItem | None) -> None: ...

    @overload
    def addItem(self, item: str | None) -> None: ...

    def addItem(self, item: qtw.QListWidgetItem | str | None) -> None:
        super().addItem(item)
        if isinstance(item, qtw.QListWidgetItem):
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
    
    def __init__(
        self,
        label: str,
        func: OperationFunc | None,
        input_type: OperationInputType,
        default: object = "",
        ):
        super().__init__()
        
        self.func: OperationFunc
        if callable(func):
            self.func = func
        elif func is not None:
            raise AssertionError("Func is not callable")
            
        self._label = qtw.QLabel(label)
        self.input_type = input_type
        self.input: qtw.QCheckBox | qtw.QLineEdit | expandingComboBox | None
        self.reset: Callable[[], None]
        self.output: Callable[[], object]
        
        self.row_widget = qtw.QWidget()
        row_layout = qtw.QHBoxLayout()
        self.row_widget.setLayout(row_layout)
        
        row_layout.addWidget(self._label)
        row_layout.setContentsMargins(5, 5, 5, 5)
        
        if input_type is bool: # on/off tickbox
            checkbox = qtw.QCheckBox()
            self.input = checkbox
            checkbox.setToolTip(f"Enable or disable {label}")
            self.reset = lambda: checkbox.setChecked(False)
            self.output = lambda: bool(checkbox.isChecked())
        
        elif input_type in [int, float, str]: # Textbox input
            line_edit = qtw.QLineEdit()
            self.input = line_edit
            self.reset = lambda: line_edit.setText("")
            scalar_type = cast(type, input_type)
            self.output = lambda: (scalar_type(line_edit.text()) 
                                   if line_edit.text() else "")
            # Restrict user input to reduce errors
            if input_type is int:
                line_edit.setValidator(QtGui.QIntValidator())
            elif input_type is float:
                validator = QtGui.QDoubleValidator()
                validator.setNotation(QtGui.QDoubleValidator.Notation.ScientificNotation)
                validator.setLocale(QtCore.QLocale("C"))  # Avoids locale issues like commas
                line_edit.setValidator(validator)
        
        elif input_type is None: # No input needed
            self.input = None
            self.reset = lambda: None
            self.output = lambda: None
            
        elif isinstance(input_type, (list, tuple)): # Select from options
            combo_box = expandingComboBox()
            self.input = combo_box
            combo_box.addItems(list(input_type))
            self.reset = lambda: combo_box.setCurrentIndex(-1)
            self.output = lambda: combo_box.currentText()
            
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
    def label(self) -> str:
        return self._label.text()



class optionToggleRowItem(rowItem):
    operation_row: rowItem
    input: qtw.QCheckBox


class operations_options_common(operations_options_base):
    operation_kind: OperationKind


class operations_options_1d(operations_options_common):
    operation_kind = "plot1d"

class operations_options_2d(operations_options_common):
    operation_kind = "plot2d"
    
class operations_options_sweep(operations_options_common):
    operation_kind = "sweeper"
