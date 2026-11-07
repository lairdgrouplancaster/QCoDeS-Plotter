"""Application-level Quit must pass through qPlot's bounded shutdown."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_NATIVE_QUIT_PROBE = textwrap.dedent(
    """
    import sys
    import threading
    import traceback
    from pathlib import Path

    from PyQt6 import QtCore, QtGui, QtWidgets, sip
    from qplot.configuration.config import config
    from qplot.diagnostics import configure_logging
    from qplot.windows import main as main_module

    directory, mode = Path(sys.argv[1]), sys.argv[2]
    config.default_path = str(directory)
    config.default_file = str(directory / 'config.json')
    configure_logging(log_file=directory / 'qplot.log', force=True)
    app = QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    window = main_module.MainWindow()
    window.startupDatabaseTimer.stop()
    window.config.config['user_preference']['confirm_close'] = False
    armed = []
    errors = []
    quit_commands = []
    gate = threading.Event()
    window._qplot_quit_action.triggered.connect(lambda: quit_commands.append(True))

    class FailSafe:
        def arm(self, **deadlines):
            armed.append(deadlines)
        def update_diagnostics(self, diagnostics):
            pass
        def persist_async(self):
            raise AssertionError('Unexpected shutdown deadline exhaustion')

    window._shutdown_process_fail_safe = FailSafe()

    def failed_callback(kind, error, tb):
        errors.append(''.join(traceback.format_exception(kind, error, tb)))
        traceback.print_exception(kind, error, tb)
        if not sip.isdeleted(app):
            app.exit(90)

    sys.excepthook = failed_callback

    def native_quit():
        event = QtCore.QEvent(QtCore.QEvent.Type.Quit)
        QtCore.QCoreApplication.sendEvent(app, event)
        return event.isAccepted()

    def shortcut_quit():
        combination = window._qplot_quit_action.shortcuts()[0][0]
        event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
                               combination.key(), combination.keyboardModifiers())
        QtCore.QCoreApplication.sendEvent(window, event)
        return event.isAccepted()

    if mode in {'hidden', 'pending'}:
        window.hide()

    if mode == 'modal':
        class PendingDialog(QtWidgets.QDialog):
            def closeEvent(self, event):
                if not window._shutdown_ready:
                    event.ignore()
                else:
                    event.accept()
        dialog = PendingDialog(window)
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        dialog.show()

    if mode == 'pending':
        started = threading.Event()
        class DelayedWork(QtCore.QRunnable):
            def run(self):
                started.set()
                gate.wait(3)
        window.threadPool.start(DelayedWork())
        assert started.wait(2), 'The synthetic worker did not start'

    confirmations = []
    if mode.startswith('cancel'):
        window.config.config['user_preference']['confirm_close'] = True
        def reject_confirmation(*args):
            confirmations.append(True)
            assert len(confirmations) == 1, 'Quit re-entered confirmation'
            if mode == 'cancel_native_shortcut':
                shortcut_quit()
            else:
                assert not native_quit(), 'Nested Quit escaped the confirmation guard'
            return QtWidgets.QMessageBox.StandardButton.No
        main_module.ask_confirmation_with_dont_ask_again = reject_confirmation

    def request_quit():
        accepted = shortcut_quit() if mode == 'cancel_shortcut_native' else native_quit()
        if mode.startswith('cancel'):
            if mode != 'cancel_shortcut_native':
                assert not accepted
            assert confirmations == [True]
            assert quit_commands == [True], 'Nested input retriggered the Quit action'
            assert not armed
            assert window.isVisible()
            assert not window._shutdown_started and not window._shutdown_ready
            # The event loop must remain usable after declining Quit.
            QtCore.QTimer.singleShot(25, accept_later)
        elif mode == 'pending':
            assert not accepted
            assert window._shutdown_started and not window._shutdown_ready
            assert len(armed) == 1
            deadline = window._shutdown.state.hard_deadline
            def repeated_request():
                assert not native_quit()
                assert len(armed) == 1
                assert window._shutdown.state.hard_deadline == deadline
                gate.set()
            QtCore.QTimer.singleShot(50, repeated_request)
        else:
            assert window._shutdown_ready, 'Native Quit bypassed MainWindow shutdown'
            assert len(armed) == 1

    def accept_later():
        window.config.config['user_preference']['confirm_close'] = False
        native_quit()

    QtCore.QTimer.singleShot(0, request_quit)
    QtCore.QTimer.singleShot(5000, lambda: app.exit(91))
    try:
        result = app.exec()
        assert not errors, '\\n'.join(errors)
        assert result == 0, f'Quit did not finish: {result}'
        assert window._shutdown_ready
        assert len(armed) == 1
        assert not window._shutdown_background_work_active()
        if mode.startswith('cancel'):
            assert quit_commands == [True, True]
        print('NATIVE_QUIT_COMPLETED', mode, flush=True)
    finally:
        gate.set()
        window.threadPool.waitForDone(4000)
        window.config.config['user_preference']['confirm_close'] = False
        window.quit_application()
        sip.delete(window)
        sip.delete(app)
    """
)


@pytest.mark.parametrize(
    "mode",
    ["hidden", "modal", "pending", "cancel", "cancel_native_shortcut", "cancel_shortcut_native"],
)
def test_native_quit_uses_main_window_shutdown(tmp_path, mode):
    result = subprocess.run(
        [sys.executable, "-c", _NATIVE_QUIT_PROBE, str(tmp_path), mode],
        cwd=Path(__file__).resolve().parents[2],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONFAULTHANDLER="1"),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"NATIVE_QUIT_COMPLETED {mode}" in result.stdout
