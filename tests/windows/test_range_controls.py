"""Range arithmetic, draft rollback and native field actions."""

from itertools import permutations

import pytest
from PyQt6 import QtCore, QtGui, QtTest
from PyQt6 import QtWidgets as qtw

from qplot.windows._range_controls import RangeControls, RangeFitError, RangeState


@pytest.mark.pure
@pytest.mark.parametrize(("field", "partner"), tuple(permutations(("min", "max", "centre", "span"), 2)))
def test_last_distinct_edit_keeps_its_current_value(field, partner):
    state = RangeState((0.0, 10.0), automatic=False, history=(partner,))
    value = {"min": 1, "max": 11, "centre": 6, "span": 8}[field]
    changed = state.edit(field, value)
    assert changed.value(partner) == state.value(partner)
    assert changed.value(field) == value
    assert changed.history == (field, partner)
    again = changed.edit(field, value + 0.5)
    assert again.history == changed.history
    assert again.value(partner) == state.value(partner)


@pytest.mark.pure
def test_lock_transfer_and_held_edit_keep_last_other_field():
    state = RangeState((0, 10), automatic=False).edit("span", 6)
    state = state.toggle_lock("min").edit("centre", 6)
    assert state.limits == (2, 10)
    moved = state.edit("min", 1)
    assert moved.limits == (1, 11)
    assert moved.held == 1
    state = moved.toggle_lock("span")
    assert state.lock == "span" and state.held == 10
    assert state.toggle_lock("span").lock is None


@pytest.mark.pure
@pytest.mark.parametrize(("field", "expected"), [
    ("min", (0, 8)), ("max", (2, 10)), ("centre", (2, 8)), ("span", (0, 10)),
])
def test_fit_with_one_constraint(field, expected):
    state = RangeState((0, 10), automatic=False).toggle_lock(field)
    fitted = state.fit((2, 8))
    assert fitted.limits == expected
    assert (fitted.automatic, fitted.lock, fitted.history) == (False, field, ())


@pytest.mark.pure
def test_auto_edit_rejection_and_locked_span_fit():
    state = RangeState((0, 4)).toggle_lock("span")
    # A narrow fixed span clips symmetrically about the target's midpoint.
    state = state.fit((0, 10))
    assert state.limits == (3, 7)
    assert state.edit("span", 2, (0, 10)).limits == (4, 6)
    with pytest.raises(ValueError):
        state.edit("span", -1, (0, 10))
    with pytest.raises(ValueError):
        state.toggle_lock("min").fit((-9, -3))
    with pytest.raises(ValueError):
        state.fit(None)
    assert state.limits == (3, 7) and state.history == ()


@pytest.mark.pure
def test_constraints_reject_precision_loss_in_derived_endpoints():
    # At this offset the rounded endpoints differ by 0.5 instead of 1.0.
    held_span = RangeState((-0.5, 0.5), automatic=False).toggle_lock("span")
    with pytest.raises(ValueError, match="span accurately"):
        held_span.edit("centre", float(2**52))
    with pytest.raises(ValueError, match="span accurately"):
        held_span.fit((float(2**52), float(2**52)))
    # Reflecting around a very small centre can erase its value completely.
    with pytest.raises(ValueError, match="centre accurately"):
        RangeState((-5, 5), history=("min",)).edit("centre", 1e-16)
    with pytest.raises(ValueError, match="centre accurately"):
        RangeState((0, 2)).toggle_lock("centre").fit((-1e16, 1e16))
    # Ordinary decimal widths remain usable away from zero.
    accepted = RangeState((0, 0.1), automatic=False).toggle_lock("span").edit("centre", 1024)
    assert accepted.limits == (1023.95, 1024.05)


@pytest.mark.pure
def test_retyping_same_value_changes_only_the_mode():
    state = RangeState((0, 10), history=("max", "span"))
    changed = state.edit("min", 0)
    assert not changed.automatic
    assert changed.limits == state.limits and changed.history == state.history
    held = state.toggle_lock("min")
    assert held.edit("min", 0) == held


@pytest.fixture
def editor(qapplication):
    widget = RangeControls("Bottom axis")
    widget.bind(lambda: (2, 8), lambda _state: None)
    widget.set_state(RangeState((0, 10)), "V")
    widget.resize(640, 235)
    widget.show()
    widget.activateWindow()
    qapplication.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def type_draft(widget, field, value):
    edit = widget.fields[field]
    edit.setFocus()
    edit.selectAll()
    QtTest.QTest.keyClicks(edit, value)


def test_each_label_has_its_lock_and_each_value_has_a_permanent_data_action(editor, qapplication):
    for field, edit in editor.fields.items():
        editor.auto_radio.setFocus()
        button = editor.use_buttons[field]
        lock = editor.locks[field]
        label = editor.labels[field]
        assert label.buddy() is edit
        assert label.geometry().right() < lock.x() <= label.geometry().right() + 6
        assert lock.geometry().bottom() < edit.y()
        assert label.x() == edit.x()
        assert button.isVisible() and button.toolTip() == f"Use data {label.text().lower()}"
        assert edit.geometry().right() + 1 == button.x()
        resting = edit.geometry(), button.geometry()
        edit.setFocus()
        qapplication.processEvents()
        assert button.isVisible()
        assert (edit.geometry(), button.geometry()) == resting


def test_numeric_events_and_rejected_blur_restore_automatic(editor, qapplication):
    requests = []
    editor.bind(lambda: (2, 8), requests.append)
    type_draft(editor, "min", "-")
    assert requests[-1].limits == (0, 10) and not requests[-1].automatic
    assert editor.fields["min"].text() == "-"
    QtTest.QTest.keyClick(editor.fields["min"], QtCore.Qt.Key.Key_Return)
    assert editor.error_label.isVisible()
    editor.fields["max"].setFocus()
    qapplication.processEvents()
    assert editor.state.automatic and editor.state.limits == (2, 8)
    assert editor.error_label.isVisible()
    type_draft(editor, "min", "3")
    QtTest.QTest.keyClick(editor.fields["min"], QtCore.Qt.Key.Key_Return)
    assert not editor.state.automatic and editor.state.limits == (3, 8)
    assert not editor.error_label.isVisible()


@pytest.mark.parametrize("rejection", ["data", "backend"])
def test_cancelling_draft_resumes_automatic_at_last_valid_range(editor, rejection):
    original = editor.state
    def data():
        if rejection == "data":
            raise RangeFitError("Unsupported data range.")
        return (100, 200)
    def accept(state):
        if state.limits == (100, 200):
            raise ValueError("Unsupported fitted range.")
    editor.bind(data, accept)
    type_draft(editor, "min", "-")
    assert not editor.state.automatic
    QtTest.QTest.keyClick(editor.fields["min"], QtCore.Qt.Key.Key_Escape)
    assert editor.state == original
    assert editor.auto_radio.isChecked() and editor.error_label.isVisible()
    editor.manual_radio.click()
    editor.auto_radio.click()
    assert not editor.state.automatic and editor.error_label.isVisible()


def test_whole_fit_and_use_data_span_differ_with_centre_held(editor):
    editor.set_state(RangeState((-10, 10), automatic=False).toggle_lock("centre"), "V")
    editor.use_buttons["span"].click()
    assert editor.state.limits == (-3, 3)
    editor.fit_button.click()
    assert editor.state.limits == (-8, 8)
    assert not editor.state.automatic and editor.state.lock == "centre"


def test_held_value_stays_automatic_and_uses_exact_raw_value(editor):
    editor.set_state(RangeState((0, 10)).toggle_lock("min"))
    editor.use_buttons["min"].click()
    assert editor.state.automatic and editor.state.held == 2
    assert editor.state.limits == (2, 8)
    editor.use_buttons["max"].click()
    assert not editor.state.automatic and editor.state.lock == "min"


def test_range_icons_have_transparent_backgrounds_at_both_scales(editor):
    for button in (editor.locks["min"], editor.use_buttons["span"], editor.fit_button):
        for scale in (1.0, 2.0):
            image = button.icon().pixmap(QtCore.QSize(16, 16), scale).toImage()
            assert image.width() == round(16 * scale)
            assert image.pixelColor(0, 0).alpha() == 0
            assert any(image.pixelColor(x, y).alpha() > 0
                       for x in range(image.width()) for y in range(image.height()))


def test_range_icons_follow_the_controls_local_text_colour(editor, qapplication):
    editor.setStyleSheet("QToolButton { color: #ddeeff; background: #171717; }")
    qapplication.processEvents()
    for button in (editor.locks["min"], editor.use_buttons["span"]):
        for state in (QtGui.QIcon.State.Off, QtGui.QIcon.State.On):
            image = button.icon().pixmap(button.iconSize(), QtGui.QIcon.Mode.Normal, state).toImage()
            assert any(image.pixelColor(x, y).name() == "#ddeeff" and image.pixelColor(x, y).alpha() > 200
                       for x in range(image.width()) for y in range(image.height()))


def test_keyboard_context_menu_preserves_text_commands(editor, qapplication):
    field = editor.fields["span"]
    field.setFocus()
    QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_F10, QtCore.Qt.KeyboardModifier.ShiftModifier)
    qapplication.processEvents()
    menu = qtw.QApplication.activePopupWidget()
    assert isinstance(menu, qtw.QMenu)
    try:
        actions = [action.text().replace("&", "") for action in menu.actions()]
        captions = [text.partition("\t")[0] for text in actions]
        assert "Copy" in captions and "Paste" in captions and "Copy exact limits" in captions
        assert any(text.startswith("Use data span") and "6" in text for text in actions)
    finally:
        menu.close()


def test_owner_synchronisation_wins_after_plot_clamps_view(editor):
    def apply(_proposed):
        editor.set_state(RangeState((1.5, 7.5), automatic=False), "V")
    editor.bind(lambda: (2, 8), apply)
    editor.use_buttons["min"].click()
    assert editor.state.limits == (1.5, 7.5)
    assert float(editor.fields["max"].text()) == 7.5
