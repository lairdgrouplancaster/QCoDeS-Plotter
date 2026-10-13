"""Every print attempt releases its disposable native dialog's Qt ownership."""

import numpy as np
import pytest
from PyQt6 import QtCore, QtPrintSupport, QtWidgets, sip

from tests.windows.test_numeric_sample_preservation import array_plot


@pytest.mark.parametrize("outcome", ["cancel", "printed", "modal_error", "render_error", "deleted"])
def test_print_dialog_is_destroyed_after_each_attempt(
        tmp_path, monkeypatch, qapplication, outcome):
    dialogs, destroyed, rendered, errors = [], [], [], []

    def modal_result(dialog):
        # Native macOS print panels cannot execute using Qt's offscreen
        # backend. Keep the real dialog/parent ownership and supply its result.
        dialogs.append(dialog)
        dialog.destroyed.connect(lambda: destroyed.append(True))
        if outcome == "modal_error":
            raise RuntimeError("Print dialog failed")
        if outcome == "deleted":
            sip.delete(dialog)
        return (QtWidgets.QDialog.DialogCode.Rejected if outcome in {"cancel", "deleted"}
                else QtWidgets.QDialog.DialogCode.Accepted)

    def render(printer):
        assert not sip.isdeleted(dialogs[-1])
        assert dialogs[-1].printer() is printer
        rendered.append(printer)
        if outcome == "render_error":
            raise RuntimeError("Printing failed")
        return True

    monkeypatch.setattr(QtPrintSupport.QPrintDialog, "exec", modal_result)
    values = np.arange(3.)
    with array_plot(tmp_path, monkeypatch, [{"x": values, "signal": values}]) as (_main, plot):
        monkeypatch.setattr(plot, "_render_plot_to_printer", render)
        monkeypatch.setattr(plot, "show_error", lambda *args: errors.append(args))
        for iteration in range(3):
            assert plot.print_plot() is (outcome == "printed")
            qapplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            qapplication.processEvents()
            assert len(destroyed) == iteration + 1
            assert sip.isdeleted(dialogs[-1])
            assert plot.findChildren(QtPrintSupport.QPrintDialog) == []
        assert len(rendered) == (3 if outcome in {"printed", "render_error"} else 0)
        assert len(errors) == (3 if outcome.endswith("error") else 0)
