from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol, TypeAlias, cast, overload

from PyQt6 import (
    QtCore,
    QtGui,
)
from PyQt6 import (
    QtWidgets as qtw,
)

from qplot.tools.operation_registry import (
    OperationCall,
    OperationSpec,
    OperationValidationError,
    operation_specs_for,
)

from .dropbox import expandingComboBox

OperationKind: TypeAlias = Literal["plot1d", "plot2d", "sweeper"]
OperationFunc: TypeAlias = Callable[..., Any]
OperationInputType: TypeAlias = type | Sequence[str] | None


@dataclass(frozen=True)
class OperationEntry:
    key: str
    value: str | bool | None


@dataclass(frozen=True)
class OperationsState:
    """Ordered enabled operations, preserving exact editor text and Apply identity."""

    entries: tuple[OperationEntry, ...]
    apply_generation: int


class OperationsDock(Protocol):
    event_filter: QtCore.QObject

    def VBox_context(self, *args: Any) -> qtw.QVBoxLayout: ...

    def HBox_context(self, *args: Any) -> qtw.QHBoxLayout: ...


class OperationsWindow(Protocol):
    oper_dock: OperationsDock


def operations_widget(window: Any) -> "operations_options_base":
    """
    Entry point for getting the operation options.
    Uses the plot kind declared by the window class to select the correct
    options widget. Subclasses inherit this declaration, so instrumented or
    otherwise specialised plot windows get the same operations as their base
    plot type. Window is also passed to the class.
    
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
    options_dict: dict[OperationKind, type[operations_options_base]] = {
        "plot1d" : operations_options_1d,
        "plot2d" : operations_options_2d,
        "sweeper": operations_options_sweep
        }

    operation_kind = getattr(window, "operation_kind", None)
    if operation_kind not in options_dict:
        raise TypeError(
            "Plot windows must declare operation_kind as "
            "'plot1d', 'plot2d', or 'sweeper'"
            )
    out = options_dict[cast(OperationKind, operation_kind)](window)
    
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
        self.list_order.setToolTip(
            "Drag operations up or down to change the order in which they are applied."
        )
        self.main_layout.addWidget(self.list_order)

        # Buttons
        but_l = self._window.oper_dock.HBox_context(self.filter)
        self.apply_but = qtw.QPushButton("Apply/Refresh")
        self.apply_but.setToolTip("Apply selected operations and refresh the plot")
        self._apply_generation = 0
        self.apply_but.clicked.connect(self._record_apply_request)
        but_l.addWidget(self.apply_but)
        clear_but = qtw.QPushButton("Clear")
        clear_but.setToolTip("Clear all selected operations")
        clear_but.clicked.connect(self.hide_all)
        but_l.addWidget(clear_but)
        
        self.main_layout.addLayout(but_l)

        # Allows user to toggle options
        self.list_options = qtw.QListWidget()
        self.list_options.setToolTip("Tick an operation to add it to the processing order, then use Apply/Refresh.")
        # Some platform styles draw the vertical scrollbar as an overlay.  Keep
        # the option-row controls clear of that area, rather than allowing the
        # checkboxes to extend underneath it.
        scrollbar_extent = max(
            self.list_options.verticalScrollBar().sizeHint().width(),
            self.style().pixelMetric(
                qtw.QStyle.PixelMetric.PM_ScrollBarExtent,
                ),
            )
        self.list_options.setViewportMargins(0, 0, scrollbar_extent, 0)
        self.main_layout.addWidget(self.list_options)
        
        self.add_all_options()


    def _record_apply_request(self) -> None:
        """Identify refreshes explicitly requested from the operations panel."""

        self._apply_generation += 1
        
        
    def add_option(self, spec: OperationSpec) -> None:
        """Create an enable toggle and editor for one registered operation."""
        row = optionToggleRowItem(spec.name, None, bool)
        self.list_options.addItem(row)
        self.list_options.setItemWidget(row, row.row_widget)
        row.operation_row = rowItem(
            spec.name, spec.func, cast(OperationInputType, spec.input_type),
            default=spec.default,
        )
        row.operation_row.spec = spec
        row.operation_row.derivative_axis = spec.derivative_axis
        row.input.stateChanged.connect(
            lambda state: self.add_or_remove_operation(state, row.operation_row)
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
        """Create the available editors from this panel's operation registry."""
        for spec in operation_specs_for(self.operation_kind):
            self.add_option(spec)
        self.list_options.adjustSize()
            
    
    def capture_state(self, *, transpose: bool = False) -> OperationsState:
        entries = []
        for index in range(self.list_order.count()):
            row = cast(rowItem, self.list_order.item(index))
            if row.isHidden():
                continue
            key = row.spec.transposed_key if transpose else None
            entries.append(OperationEntry(key or row.spec.key, row.editor_value()))
        return OperationsState(tuple(entries), self._apply_generation)

    def restore_state(self, state: OperationsState) -> None:
        """Replace panel contents without parsing or rounding input text."""
        specs = {spec.key: spec for spec in operation_specs_for(self.operation_kind)}
        if any(entry.key not in specs for entry in state.entries):
            raise ValueError("The target plot does not support an operation in this state.")
        # Rebuild the rows so prior hidden/reordered entries cannot change the
        # restored execution order. No caller owns the panel's individual rows.
        self.list_order.clear()
        self.list_options.clear()
        self.add_all_options()
        options = {}
        for index in range(self.list_options.count()):
            item = cast(optionToggleRowItem, self.list_options.item(index))
            options[item.operation_row.spec.key] = item
        for entry in state.entries:
            option = options[entry.key]
            option.input.setChecked(True)
            option.operation_row.restore_editor_value(entry.value)
        self._apply_generation = state.apply_generation

    @QtCore.pyqtSlot()
    def hide_all(self) -> None:
        for i in range(self.list_options.count()):
            item = cast(optionToggleRowItem, self.list_options.item(i))
            item.input.setChecked(False)
            
    
    def get_data(self) -> list[OperationCall]:
        """
        Returns the function of the items in the active operation listWidget
        (self.list_order) from top to bottom. Also adds the user input to the
        functions ready for processing by qplot.tools.worker.loader.do_operations.

        Returns
        -------
        operations : list[callable]
            List of functions to be performed on the data during refresh.

        """
        operations: list[OperationCall] = []
        for i in range(self.list_order.count()):
            item = cast(rowItem, self.list_order.item(i))
            if item.isHidden():
                continue

            output = item.validated_output()
            
            if output is None: # No input requried
                func = item.func
            else: # Add input
                # Bind this row's value independently of later loop iterations.
                func = func_with_input(item.func, output) 
                
            operations.append(OperationCall(
                item.label,
                func,
                getattr(item, "derivative_axis", None),
                cooperative=True,
                ))
        return operations
 
    
def func_with_input(func: OperationFunc, value: object) -> OperationFunc:
    return lambda data, cancelled_callback=None: func(
        value,
        data,
        cancelled_callback=cancelled_callback,
        )
 

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
    
    Each row owns its editor and its read/reset/validation behavior.

    Key attributes:
        label : str - Display name of item
        func : callable - copy of function to be used in do_operations
        input : QWidget - The input widget
    """
    
    spec: OperationSpec

    def __init__(
        self,
        label: str,
        func: OperationFunc | None,
        input_type: OperationInputType,
        default: object = "",
        ):
        super().__init__()
        
        self.func: OperationFunc
        self.derivative_axis: str | None = None
        if callable(func):
            self.func = func
        elif func is not None:
            raise AssertionError("Func is not callable")
            
        self._label = qtw.QLabel(label)
        self.input_type = input_type
        self.input: qtw.QCheckBox | qtw.QLineEdit | expandingComboBox | None
        
        self.row_widget = qtw.QWidget()
        row_layout = qtw.QHBoxLayout()
        self.row_widget.setLayout(row_layout)
        
        row_layout.addWidget(self._label)
        row_layout.setContentsMargins(5, 5, 5, 5)
        
        if input_type is bool: # on/off tickbox
            checkbox = qtw.QCheckBox()
            self.input = checkbox
            checkbox.setToolTip(f"Enable or disable {label}")
        
        elif input_type in [int, float, Decimal, str]: # Textbox input
            line_edit = qtw.QLineEdit()
            self.input = line_edit
            input_help = {
                "Limit Maximum": "Replace values above this limit with the limit.",
                "Limit Minimum": "Replace values below this limit with the limit.",
                "Fill Below": "Fill gaps down each column with the preceding value, up to this many cells. Only gaps bounded by data are filled; zero disables filling.",
                "Fill Right": "Fill gaps along each row with the preceding value, up to this many cells. Only gaps bounded by data are filled; zero disables filling.",
            }
            line_edit.setToolTip(
                input_help.get(label, f"Enter the value for {label}.")
                + " Use Apply/Refresh to update the plot."
            )
            # Restrict user input to reduce errors
            if input_type is int:
                line_edit.setValidator(QtGui.QIntValidator())
            elif input_type in (float, Decimal):
                validator = QtGui.QDoubleValidator()
                validator.setNotation(QtGui.QDoubleValidator.Notation.ScientificNotation)
                validator.setLocale(QtCore.QLocale("C"))  # Avoids locale issues like commas
                line_edit.setValidator(validator)
        
        elif input_type is None: # No input needed
            self.input = None
            
        elif isinstance(input_type, (list, tuple)): # Select from options
            combo_box = expandingComboBox()
            self.input = combo_box
            combo_box.addItems(list(input_type))
            combo_box.setToolTip(f"Choose the setting for {label}, then use Apply/Refresh.")
            
        else:
            raise TypeError(
                f"Invalid input type: {input_type}, must be int, float, Decimal"
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
        

    def reset(self) -> None:
        """Clear the editor while retaining its configured placeholder default."""
        if isinstance(self.input, qtw.QCheckBox):
            self.input.setChecked(False)
        elif isinstance(self.input, qtw.QLineEdit):
            self.input.setText("")
        elif isinstance(self.input, qtw.QComboBox):
            self.input.setCurrentIndex(-1)

    def output(self) -> object:
        """Read the current editor value without applying a placeholder default."""
        if isinstance(self.input, qtw.QCheckBox):
            return bool(self.input.isChecked())
        if isinstance(self.input, qtw.QLineEdit):
            text = self.input.text()
            return cast(type, self.input_type)(text) if text else ""
        if isinstance(self.input, qtw.QComboBox):
            return self.input.currentText()
        return None

    def validated_output(self) -> object:
        """Resolve an enabled operation's input or report a labelled error."""
        if (
            isinstance(self.input, qtw.QLineEdit)
            and self.input.text()
            and not self.input.hasAcceptableInput()
        ):
            raise OperationValidationError(f"{self.label}: enter a valid value.")

        try:
            value = self.output()
        except (TypeError, ValueError, OverflowError) as error:
            raise OperationValidationError(
                f"{self.label}: enter a valid value."
            ) from error

        if value != "":
            return value
        if not isinstance(self.input, (qtw.QLineEdit, qtw.QComboBox)):
            raise OperationValidationError(f"{self.label}: a value is required.")
        placeholder = self.input.placeholderText()
        if not placeholder:
            raise OperationValidationError(f"{self.label}: a value is required.")
        if not isinstance(self.input_type, type):
            return placeholder
        try:
            return self.input_type(placeholder)
        except (TypeError, ValueError, OverflowError) as error:
            raise OperationValidationError(
                f"{self.label}: enter a valid value."
            ) from error


    def editor_value(self) -> str | bool | None:
        if isinstance(self.input, qtw.QLineEdit):
            return self.input.text()
        if isinstance(self.input, qtw.QComboBox):
            return self.input.currentText()
        if isinstance(self.input, qtw.QCheckBox):
            return self.input.isChecked()
        return None

    def restore_editor_value(self, value: str | bool | None) -> None:
        if isinstance(self.input, qtw.QLineEdit) and isinstance(value, str):
            self.input.setText(value)
        elif isinstance(self.input, qtw.QComboBox) and isinstance(value, str):
            self.input.setCurrentText(value)
        elif isinstance(self.input, qtw.QCheckBox) and isinstance(value, bool):
            self.input.setChecked(value)

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
