"""Data-coordinate range constraints and their shared Plot settings controls."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from math import isclose, isfinite

from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from ._range_entry import CompactNumberEdit, _center_span, _compact_range_numbers

Limits = tuple[float, float]
_NAMES = {"min": "Minimum", "max": "Maximum", "centre": "Centre", "span": "Span"}
_PARTNER = {"min": "max", "max": "min", "centre": "span", "span": "centre"}
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?", re.ASCII | re.IGNORECASE)


class RangeFitError(ValueError):
    """The plot backend cannot represent the proposed automatic data range."""


def _values(limits: Limits) -> dict[str, float]:
    centre, span = _center_span(*limits)
    return {"min": limits[0], "max": limits[1], "centre": centre, "span": span}


def _valid(limits: Limits) -> bool:
    lower, upper = limits
    return lower < upper and isfinite(upper - lower)


def _check_constraint(limits: Limits, field: str, expected: float) -> None:
    # Rounding the endpoints can erase a small centre or significantly distort
    # a narrow span at a large offset. Allow rounding at the display precision.
    actual = _values(limits)[field]
    if not isclose(actual, expected, rel_tol=1e-6):
        raise ValueError(f"These limits cannot represent the requested {_NAMES[field].lower()} accurately.")


def _from_pair(first: str, value: float, second: str, kept: float) -> Limits:
    pair = {first: value, second: kept}
    if "min" in pair:
        low = pair["min"]
        if "max" in pair:
            return low, pair["max"]
        if "span" in pair:
            return low, low + pair["span"]
        return low, pair["centre"] + (pair["centre"] - low)
    if "max" in pair:
        high = pair["max"]
        if "span" in pair:
            return high - pair["span"], high
        return pair["centre"] - (high - pair["centre"]), high
    return pair["centre"] - pair["span"] / 2, pair["centre"] + pair["span"] / 2


@dataclass(frozen=True)
class RangeState:
    """One axis's committed values. History contains distinct user-edited field names."""

    limits: Limits = (0.0, 1.0)
    automatic: bool = True
    lock: str | None = None
    held: float | None = None
    history: tuple[str, ...] = ()

    def value(self, field: str) -> float:
        if field == self.lock and self.held is not None:
            return self.held
        return _values(self.limits)[field]

    def partner(self, field: str) -> str:
        if self.lock is not None and self.lock != field:
            return self.lock
        return next((other for other in self.history if other != field), _PARTNER[field])

    def toggle_lock(self, field: str) -> RangeState:
        if field == self.lock:
            return replace(self, lock=None, held=None)
        return replace(self, lock=field, held=self.value(field))

    def fit(self, target: Limits | None) -> RangeState:
        if target is None:
            raise ValueError("There are no eligible samples to fit.")
        low, high = target
        if not all(map(isfinite, target)) or high < low:
            raise ValueError("The data range exceeds the supported limits.")
        if self.lock is None:
            if low == high:
                margin = max(0.5, abs(low) * 0.01)
                low = low - margin if isfinite(low - margin) else low
                high = high + margin if isfinite(high + margin) else high
        else:
            held = self.value(self.lock)
            if self.lock == "min":
                low = held
            elif self.lock == "max":
                high = held
            elif self.lock == "span":
                centre, _ = _center_span(low, high)
                low, high = centre - held / 2, centre + held / 2
            else:
                distance = max(abs(held - low), abs(high - held)) or max(0.5, abs(low) * 0.01)
                low, high = held - distance, held + distance
            if high <= low:
                raise ValueError(f"The held {_NAMES[self.lock].lower()} prevents a positive-width fit.")
        if not _valid((low, high)):
            raise ValueError("The fitted range exceeds the supported limits.")
        if self.lock is not None:
            _check_constraint((low, high), self.lock, self.value(self.lock))
        return replace(self, limits=(low, high))

    def edit(self, field: str, value: float, target: Limits | None = None) -> RangeState:
        if not isfinite(value):
            raise ValueError("Enter a finite number.")
        if field == "span" and value <= 0:
            raise ValueError("Span must be greater than zero.")
        automatic = self.automatic and field == self.lock
        if value == self.value(field):
            return replace(self, automatic=automatic)
        result = replace(self, automatic=automatic, held=value if field == self.lock else self.held)
        if automatic:
            result = result.fit(target)
        else:
            partner = self.partner(field)
            kept = self.value(partner)
            limits = _from_pair(field, value, partner, kept)
            if not _valid(limits):
                raise ValueError("These values must give finite limits with Maximum greater than Minimum.")
            _check_constraint(limits, field, value)
            _check_constraint(limits, partner, kept)
            result = replace(result, limits=limits)
        return replace(result, history=(field, *(other for other in self.history if other != field)))


class _RangeIcon(QtGui.QIconEngine):
    """Small palette-aware fit and padlock symbols that follow the display scale."""

    def __init__(self, symbol: str):
        super().__init__()
        self.symbol = symbol

    def clone(self) -> _RangeIcon:
        return _RangeIcon(self.symbol)

    def pixmap(self, size: QtCore.QSize, mode: QtGui.QIcon.Mode, state: QtGui.QIcon.State) -> QtGui.QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size: QtCore.QSize, mode: QtGui.QIcon.Mode,
                     state: QtGui.QIcon.State, scale: float) -> QtGui.QPixmap:
        image = QtGui.QPixmap(size * scale)
        image.fill(QtCore.Qt.GlobalColor.transparent)
        image.setDevicePixelRatio(scale)
        painter = QtGui.QPainter(image)
        self.paint(painter, QtCore.QRect(QtCore.QPoint(), size), mode, state)
        painter.end()
        return image

    def paint(self, painter: QtGui.QPainter | None, rect: QtCore.QRect, mode: QtGui.QIcon.Mode,
              state: QtGui.QIcon.State) -> None:
        if painter is None:
            return
        palette = qtw.QApplication.palette()
        group = QtGui.QPalette.ColorGroup.Disabled if mode == QtGui.QIcon.Mode.Disabled else QtGui.QPalette.ColorGroup.Active
        painter.save()
        painter.translate(QtCore.QRectF(rect).center())
        side = min(rect.width(), rect.height()) / 18
        painter.scale(side, side)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(palette.color(group, QtGui.QPalette.ColorRole.ButtonText), 1.5))
        if self.symbol == "fit":
            for x1, y1, x2, y2 in ((-8, -5, -8, 5), (8, -5, 8, 5), (-5, 0, 5, 0),
                                   (-5, 0, -2, -3), (-5, 0, -2, 3), (5, 0, 2, -3), (5, 0, 2, 3)):
                painter.drawLine(QtCore.QLineF(x1, y1, x2, y2))
        else:
            painter.drawRoundedRect(QtCore.QRectF(-5.5, -1, 11, 8), 1.2, 1.2)
            painter.drawLine(QtCore.QLineF(0, 2, 0, 4.5))
            shackle = QtGui.QPainterPath(QtCore.QPointF(-3.5, -1))
            shackle.lineTo(-3.5, -4)
            if state == QtGui.QIcon.State.On:
                shackle.cubicTo(-3.5, -8.5, 3.5, -8.5, 3.5, -4)
                shackle.lineTo(3.5, -1)
            else:
                shackle.cubicTo(-3.5, -8.5, -8, -8.5, -8, -4)
            painter.drawPath(shackle)
        painter.restore()


class _RangeEdit(CompactNumberEdit):
    accepted = QtCore.pyqtSignal()
    abandoned = QtCore.pyqtSignal()
    left = QtCore.pyqtSignal()

    def keyPressEvent(self, event: QtGui.QKeyEvent | None) -> None:
        if event is not None:
            if (event.key() == QtCore.Qt.Key.Key_Menu or
                    (event.key() == QtCore.Qt.Key.Key_F10
                     and event.modifiers() == QtCore.Qt.KeyboardModifier.ShiftModifier)):
                self.customContextMenuRequested.emit(self.rect().center())
                event.accept()
                return
            if event.key() in (QtCore.Qt.Key.Key_Enter, QtCore.Qt.Key.Key_Return):
                self.accepted.emit()
                event.accept()
                return
            if event.key() == QtCore.Qt.Key.Key_Escape and self.isModified():
                self.abandoned.emit()
                event.accept()
                return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QtGui.QFocusEvent | None) -> None:
        super().focusOutEvent(event)
        if event is None or event.reason() == QtCore.Qt.FocusReason.PopupFocusReason:
            return
        # Keep changes may take focus before Qt delivers its click. Treat the
        # field and that button as one focus group until the user decides.
        button = qtw.QApplication.focusWidget()
        if isinstance(button, qtw.QPushButton):
            box = button.parentWidget()
            if isinstance(box, qtw.QDialogButtonBox) and box.standardButton(button) == box.StandardButton.Ok:
                button.installEventFilter(self)
                return
        self.left.emit()

    def eventFilter(self, watched: QtCore.QObject | None, event: QtCore.QEvent | None) -> bool:
        if (isinstance(watched, qtw.QPushButton) and event is not None
                and event.type() == QtCore.QEvent.Type.FocusOut):
            watched.removeEventFilter(self)
            if qtw.QApplication.focusWidget() is not self:
                self.left.emit()
        return super().eventFilter(watched, event)


class _FieldBox(qtw.QWidget):
    """Keep the hold toggle with the label and the data action with the value."""

    def __init__(self, prefix: str, field: str, parent: qtw.QWidget):
        super().__init__(parent)
        layout = qtw.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        heading = qtw.QHBoxLayout()
        heading.setSpacing(3)
        layout.addLayout(heading)
        self.label = qtw.QLabel(_NAMES[field], self)
        heading.addWidget(self.label)
        self.lock = qtw.QToolButton(self)
        self.lock.setObjectName("rangeFieldLock")
        self.lock.setAutoRaise(True)
        self.lock.setCheckable(True)
        self.lock.setIcon(QtGui.QIcon(_RangeIcon("lock")))
        self.lock.setAccessibleName(f"Hold {_NAMES[field].lower()}")
        self.lock.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.lock.setIconSize(QtCore.QSize(15, 15))
        self.lock.setFixedSize(22, max(17, self.label.sizeHint().height()))
        heading.addWidget(self.lock)
        heading.addStretch()

        value = qtw.QHBoxLayout()
        value.setSpacing(0)
        value.setAlignment(QtCore.Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(value)
        self.edit = _RangeEdit(self)
        self.label.setBuddy(self.edit)
        self.edit.setObjectName("rangeFieldValue")
        self.edit.setAccessibleName(f"{prefix} {_NAMES[field].lower()}")
        self.edit.setSizePolicy(qtw.QSizePolicy.Policy.Ignored, qtw.QSizePolicy.Policy.Fixed)
        self.edit.setMinimumWidth(self.fontMetrics().horizontalAdvance("0.00"))
        value.addWidget(self.edit, 1)
        self.use = qtw.QToolButton(self)
        self.use.setObjectName("rangeFieldUseData")
        self.use.setIcon(QtGui.QIcon(_RangeIcon("fit")))
        self.use.setAccessibleName(f"Use data {_NAMES[field].lower()}")
        self.use.setToolTip(self.use.accessibleName())
        self.use.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.use.setIconSize(QtCore.QSize(15, 15))
        self.use.setSizePolicy(qtw.QSizePolicy.Policy.Fixed, qtw.QSizePolicy.Policy.Ignored)
        value.addWidget(self.use)


class RangeControls(qtw.QWidget):
    """Shared Range section. The owner validates and applies each proposed state."""

    def __init__(self, prefix: str, *, colour: bool = False, source_controls: qtw.QWidget | None = None,
                 parent: qtw.QWidget | None = None):
        super().__init__(parent)
        self.state = RangeState()
        self._units = ""
        self._data_range: Callable[[], Limits | None] = lambda: None
        self._fit_range: Callable[[], Limits | None] = self._data_range
        self._apply_state: Callable[[RangeState], None] = lambda _state: None
        self._draft: str | None = None
        self._origin: RangeState | None = None
        self._revision = 0
        self.fields: dict[str, _RangeEdit] = {}
        self.locks: dict[str, qtw.QToolButton] = {}
        self.use_buttons: dict[str, qtw.QToolButton] = {}
        self.labels: dict[str, qtw.QLabel] = {}

        form = qtw.QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(0)
        form.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(qtw.QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        self.range_heading = qtw.QLabel("Range", self)
        font = self.range_heading.font()
        font.setBold(True)
        self.range_heading.setFont(font)
        self.range_heading.setMinimumWidth(self.range_heading.fontMetrics().horizontalAdvance("Axis behaviour"))
        # Put the gutter in the label column. QFormLayout otherwise indents
        # wrapped fields by different amounts depending on the options below.
        label_column = qtw.QWidget(self)
        label_layout = qtw.QHBoxLayout(label_column)
        label_layout.setContentsMargins(0, 0, 12, 0)
        label_layout.addWidget(self.range_heading)
        range_fields = qtw.QWidget(self)
        range_fields.setSizePolicy(qtw.QSizePolicy.Policy.Expanding, qtw.QSizePolicy.Policy.Preferred)
        body = qtw.QVBoxLayout(range_fields)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(5)
        form.addRow(label_column, range_fields)

        modes = qtw.QHBoxLayout()
        modes.setSpacing(6)
        self.auto_radio = qtw.QRadioButton("Automatic", self)
        self.manual_radio = qtw.QRadioButton("Manual", self)
        self.fit_button = qtw.QPushButton(QtGui.QIcon(_RangeIcon("fit")), "Fit once to data", self)
        self.fit_button.setAutoDefault(False)
        self.fit_button.setToolTip("Fit this range to the data once, keeping any held value.")
        modes.addWidget(self.auto_radio)
        modes.addWidget(self.manual_radio)
        modes.addStretch()
        modes.addWidget(self.fit_button)
        body.addLayout(modes)
        self.auto_radio.clicked.connect(partial(self._choose_mode, True))
        self.manual_radio.clicked.connect(partial(self._choose_mode, False))
        self.fit_button.clicked.connect(self._fit_once)

        grid = qtw.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(2)
        for i, field in enumerate(_NAMES):
            row, column = divmod(i, 2)
            box = _FieldBox(prefix, field, self)
            grid.addWidget(box, row, column)
            grid.setColumnStretch(column, 1)
            self.fields[field] = box.edit
            self.locks[field] = box.lock
            self.use_buttons[field] = box.use
            self.labels[field] = box.label
            box.edit.textEdited.connect(partial(self._begin, field))
            box.edit.accepted.connect(partial(self._finish, field, False))
            box.edit.left.connect(partial(self._finish, field, True))
            box.edit.abandoned.connect(self._cancel)
            box.use.clicked.connect(partial(self._use_data, field))
            box.lock.clicked.connect(partial(self._toggle_lock, field))
            box.edit.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            box.edit.customContextMenuRequested.connect(partial(self._context_menu, field))
        body.addLayout(grid)
        if source_controls is not None:
            source_label = qtw.QLabel("Use data from", self)
            source_label.setFont(font)
            source_label.setBuddy(source_controls)
            form.addRow(source_label, source_controls)
        self.filter_row = qtw.QWidget(self)
        options = qtw.QFormLayout(self.filter_row)
        options.setContentsMargins(0, 0, 0, 0)
        options.setRowWrapPolicy(qtw.QFormLayout.RowWrapPolicy.WrapLongRows)
        options.setFieldGrowthPolicy(qtw.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.visible_only = qtw.QCheckBox("Visible area only" if colour else "Autoscale from visible data", self)
        self.visible_only.setToolTip("Use samples inside the current horizontal and vertical limits." if colour else
                                    "Use samples within the current view on the other axes.")
        self.percent = qtw.QSpinBox(self)
        self.percent.setRange(1, 100)
        self.percent.setValue(100)
        self.percent.setSuffix("%")
        self.percent.setAccessibleName("Central percentage")
        self.percent.setToolTip("Use the central percentage of eligible colour values.")
        self.percent.setKeyboardTracking(False)
        if colour:
            options.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
            percentage = qtw.QHBoxLayout()
            percentage.addStretch()
            percent_label = qtw.QLabel("Central percentage", self)
            percent_label.setBuddy(self.percent)
            percentage.addWidget(percent_label)
            percentage.addWidget(self.percent)
            options.addRow(self.visible_only, percentage)
            form.addRow("", self.filter_row)
        else:
            options.addRow(self.visible_only)
            self.percent.hide()
            body.addWidget(self.filter_row)
        self.error_label = qtw.QLabel(self)
        self.error_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.error_label.setObjectName("rangeEntryError")
        self.error_label.setWordWrap(True)
        self.error_label.setAccessibleName("Range message")
        body.addWidget(self.error_label)
        self._sync()

    def bind(self, data_range: Callable[[], Limits | None], apply_state: Callable[[RangeState], None],
             *, fit_range: Callable[[], Limits | None] | None = None) -> None:
        self._data_range = data_range
        self._fit_range = fit_range or data_range
        self._apply_state = apply_state

    def set_state(self, state: RangeState, units: str = "") -> None:
        self._revision += 1
        self.state = state
        self._units = units
        self._sync()

    def _sync(self) -> None:
        automatic = self.state.automatic and (self._draft is None or self._draft == self.state.lock)
        for button, checked in ((self.auto_radio, automatic), (self.manual_radio, not automatic)):
            with QtCore.QSignalBlocker(button):
                button.setChecked(checked)
        self.fit_button.setEnabled(not automatic)
        values = {field: self.state.value(field) for field in _NAMES}
        if self._draft is None:
            self.show_error("")
        compact = (*_compact_range_numbers((values["min"], values["max"])),
                   *_compact_range_numbers((values["centre"], values["span"])))
        for (field, widget), short in zip(self.fields.items(), compact, strict=True):
            if field != self._draft:
                text = repr(float(values[field]))
                if widget.text() != text or widget.isModified():
                    widget.setText(text)
                    widget.setModified(False)
                    widget.setCursorPosition(0)
                widget.set_compact_text(short)
            held = field == self.state.lock
            lock = self.locks[field]
            with QtCore.QSignalBlocker(lock):
                lock.setChecked(held)
            lock.setToolTip(f"Release {_NAMES[field]}" if held else f"Hold {_NAMES[field]} when the range changes")
            partner = self.state.partner(field)
            kept = f"{values[partner]:.7g} {self._units}".rstrip()
            tooltip = (f"Editing {_NAMES[field]} keeps Automatic on and refits the range."
                       if automatic and held else
                       f"Changing {_NAMES[field]} keeps {_NAMES[partner]} at {kept}.")
            if automatic and not held:
                tooltip += " Editing switches to Manual."
            widget.setToolTip(tooltip)

    def show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def restore(self) -> None:
        self._draft = None
        self._origin = None
        self._sync()

    def _publish(self, state: RangeState) -> None:
        revision = self._revision
        self._apply_state(state)
        if self._revision == revision:
            self.set_state(state, self._units)

    def _fit_and_track(self, state: RangeState, keep_tracking: bool) -> None:
        try:
            fitted = state.fit(self._fit_range())
        except ValueError as exc:
            if keep_tracking or not isinstance(exc, RangeFitError):
                self._publish(state)
            raise
        try:
            self._publish(fitted)
        except ValueError:
            # A held value can also make the result invalid for a log axis.
            if keep_tracking and fitted != state:
                self._publish(state)
            raise

    def _apply_mode_or_lock(self, state: RangeState, *, resume: bool = False) -> None:
        self.restore()
        message = ""
        try:
            if state.automatic:
                self._fit_and_track(state, self.state.automatic or resume)
            else:
                self._publish(state)
        except ValueError as exc:
            message = str(exc)
        self._sync()
        self.show_error(message)

    def _choose_mode(self, automatic: bool, _checked: bool = False) -> None:
        self._apply_mode_or_lock(replace(self.state, automatic=automatic))

    def _toggle_lock(self, field: str, _checked: bool = False) -> None:
        self._apply_mode_or_lock(self.state.toggle_lock(field))

    def _fit_once(self) -> None:
        try:
            self._publish(self.state.fit(self._fit_range()))
        except ValueError as exc:
            self.show_error(str(exc))

    def _begin(self, field: str, _text: str) -> None:
        if self._draft != field:
            self._origin = self.state
            self._draft = field
            if self.state.automatic and self.state.lock != field:
                try:
                    self._publish(replace(self.state, automatic=False))
                except ValueError as exc:
                    self.show_error(str(exc))
                    return
        self.show_error("")

    def _cancel(self) -> None:
        origin = self._origin
        self.restore()
        if origin is not None and origin.automatic and not self.state.automatic:
            self._apply_mode_or_lock(origin, resume=True)

    def _apply_value(self, field: str, value: float) -> None:
        target = self._fit_range() if self.state.automatic and field == self.state.lock else None
        self._publish(self.state.edit(field, value, target))

    def commit_pending(self) -> bool:
        field = self._draft
        if field is None or self._finish(field, leaving=False):
            return True
        self.fields[field].setFocus()
        self.fields[field].selectAll()
        return False

    def _finish(self, field: str, leaving: bool) -> bool:
        widget = self.fields[field]
        if self._draft != field and widget.text() == repr(float(self.state.value(field))):
            return True
        text = widget.text().strip()
        try:
            if not _NUMBER.fullmatch(text):
                raise ValueError("Enter a finite number using a decimal point.")
            self._apply_value(field, float(text))
        except ValueError as exc:
            if leaving:
                self._cancel()
            self.show_error(str(exc))
            return False
        self.restore()
        return True

    def _use_data(self, field: str, _checked: bool = False) -> None:
        try:
            target = self._data_range()
            if target is None:
                raise ValueError("There are no eligible samples to fit.")
            self._apply_value(field, _values(target)[field])
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.restore()

    def _context_menu(self, field: str, pos: QtCore.QPoint) -> None:
        widget = self.fields[field]
        menu = widget.createStandardContextMenu()
        if menu is None:
            return
        menu.setParent(widget, menu.windowFlags())
        menu.addSeparator()
        caption = self.use_buttons[field].accessibleName()
        # Keep the native text commands accessible even when this data
        # source cannot supply usable bounds. Invoking Use data reports it.
        try:
            target = self._data_range()
        except ValueError:
            target = None
        if target is not None:
            caption += f"\t{_values(target)[field]:.7g} {self._units}".rstrip()
        use = menu.addAction(self.use_buttons[field].icon(), caption)
        assert use is not None
        use.triggered.connect(partial(self._use_data, field))
        # Keep copying both canonical limits available from each field.
        copy = menu.addAction("Copy exact limits")
        assert copy is not None
        copy.triggered.connect(self._copy_limits)
        menu.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(widget.mapToGlobal(pos if widget.rect().contains(pos) else widget.rect().center()))

    def _copy_limits(self) -> None:
        clipboard = qtw.QApplication.clipboard()
        if clipboard is not None:
            lower, upper = self.state.limits
            clipboard.setText(f"{lower!r}\t{upper!r}")
