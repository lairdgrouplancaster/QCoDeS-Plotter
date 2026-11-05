"""Control updates must preserve enclosing signal suppression on every exit."""

from types import SimpleNamespace

import pytest
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.windows._config_persistence import set_widget_value_without_signals
from qplot.windows._signal_blocking import blocked_signals
from qplot.windows._widgets.dropbox import picker_1d


@pytest.mark.parametrize("initially_blocked", [False, True])
def test_nested_scopes_and_duplicate_controls_restore_prior_state(initially_blocked):
    control = QtCore.QObject()
    control.blockSignals(initially_blocked)

    with blocked_signals(control, control):
        assert control.signalsBlocked()
        with blocked_signals(control):
            assert control.signalsBlocked()
        assert control.signalsBlocked()

    assert control.signalsBlocked() is initially_blocked


def test_partial_setup_failure_restores_already_blocked_controls():
    control = QtCore.QObject()

    class UnavailableControl:
        def blockSignals(self, _block):
            raise RuntimeError("control unavailable")

    with pytest.raises(RuntimeError, match="control unavailable"), blocked_signals(
        control, UnavailableControl(),
    ):
        pytest.fail("A failed blocker must not enter its body")

    assert not control.signalsBlocked()


@pytest.mark.parametrize("initially_blocked", [False, True])
def test_setter_failure_restores_prior_signal_state(initially_blocked):
    control = qtw.QSpinBox()
    control.blockSignals(initially_blocked)
    values = []
    control.valueChanged.connect(values.append)

    def update_then_fail(value):
        control.setValue(value)
        raise ValueError("update failed")

    try:
        with pytest.raises(ValueError, match="update failed"):
            set_widget_value_without_signals(control, update_then_fail, 4)
        assert control.signalsBlocked() is initially_blocked
        assert values == []
        control.setValue(5)
        assert values == ([] if initially_blocked else [5])
    finally:
        control.deleteLater()


@pytest.mark.parametrize("initially_blocked", [False, True])
@pytest.mark.parametrize("invalid_identities", [False, True])
def test_picker_population_preserves_outer_block_on_success_and_failure(
    initially_blocked, invalid_identities,
):
    combo = qtw.QComboBox()
    combo.addItem("old")
    combo.blockSignals(initially_blocked)
    changed = []
    combo.currentIndexChanged.connect(changed.append)
    host = SimpleNamespace(option_box=combo)
    try:
        if invalid_identities:
            with pytest.raises(ValueError, match="zip"):
                picker_1d.reset_box(host, ["new"], item_data=[])
        else:
            picker_1d.reset_box(host, ["new"], item_data=["new-key"])
            assert combo.itemData(0) == "new-key"
            assert combo.currentIndex() == -1
        assert combo.signalsBlocked() is initially_blocked
        assert changed == []
    finally:
        combo.deleteLater()
