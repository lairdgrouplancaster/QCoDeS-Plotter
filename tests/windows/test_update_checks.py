import json
import time

import pytest
from PyQt6 import QtCore, QtNetwork, QtTest, sip
from PyQt6 import QtWidgets as qtw

from qplot.configuration.config import config
from qplot.updates import (
    CHECK_ON_LAUNCH_KEY,
    INCLUDE_PRERELEASES_KEY,
    LAST_CHECK_KEY,
    MAX_RESPONSE_BYTES,
)
from qplot.windows import _updates
from qplot.windows._help import add_help_menu
from qplot.windows._preferences import PreferencesDialog
from qplot.windows._updates import UpdateChecker, configure_launch_check


class FakeReply(QtNetwork.QNetworkReply):
    def __init__(self, request, parent):
        super().__init__(parent)
        self._data = bytearray()
        self._done = False
        self.setUrl(request.url())
        self.open(QtCore.QIODevice.OpenModeFlag.ReadOnly)

    def readData(self, size):
        chunk = bytes(self._data[:size])
        del self._data[:size]
        return chunk

    def bytesAvailable(self):
        return len(self._data) + super().bytesAvailable()

    def abort(self):
        if not self._done:
            self._done = True
            self.setError(self.NetworkError.OperationCanceledError, "aborted")
            self.setFinished(True)
            self.finished.emit()

    def deliver(self, data, *, status=200, finish=True, length=None):
        self.setAttribute(QtNetwork.QNetworkRequest.Attribute.HttpStatusCodeAttribute, status)
        if length is not None:
            self.setHeader(QtNetwork.QNetworkRequest.KnownHeaders.ContentLengthHeader, length)
        self._data.extend(data)
        self.readyRead.emit()
        if finish and not self._done:
            self._done = True
            self.setFinished(True)
            self.finished.emit()


class FakeManager(QtCore.QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.requests = []
        self.replies = []

    def get(self, request):
        self.requests.append(request)
        reply = FakeReply(request, self)
        self.replies.append(reply)
        return reply


@pytest.fixture
def updates(tmp_path, monkeypatch, qapplication):
    monkeypatch.setattr(config, "default_path", str(tmp_path))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "settings.json"))
    cfg = config()
    checker = UpdateChecker(qapplication, {"version": "1.0"})
    checker._manager = FakeManager(checker)
    monkeypatch.setattr(qapplication, "_qplot_update_checker", checker, raising=False)
    window = qtw.QMainWindow()
    window.config = cfg
    window.show()
    try:
        yield checker, cfg, window
    finally:
        checker.shutdown()
        if not sip.isdeleted(window):
            window.close()
            sip.delete(window)
        sip.delete(checker)


def body(version="2.0"):
    return json.dumps({"name": "qplotter", "files": [{
        "filename": f"qplotter-{version}-py3-none-any.whl", "yanked": False,
    }]}).encode()


def text(dialog):
    return dialog.findChild(qtw.QTextBrowser, "qplotHelpBrowser").toPlainText()


def test_help_command_shares_one_request_and_provides_upgrade_instructions(updates):
    checker, cfg, window = updates
    menu = add_help_menu(window)
    action = next(action for action in menu.actions() if action.objectName() == "checkForUpdatesAction")
    action.trigger()
    first = window.findChild(qtw.QDialog, "qplotUpdatesDialog")
    assert "Checking for updates" in text(first)
    action.trigger()
    assert len(window.findChildren(qtw.QDialog, "qplotUpdatesDialog")) == 1
    assert len(checker._manager.requests) == 1
    request = checker._manager.requests[0]
    assert request.url().toString() == "https://pypi.org/simple/qplotter/"
    assert b"application/vnd.pypi.simple.v1+json" == bytes(request.rawHeader(b"Accept"))
    assert cfg.get(LAST_CHECK_KEY) > 0
    checker._manager.replies[-1].deliver(body())
    assert "update is available" in text(first)
    assert "qplotter==2.0" in text(first)
    assert "Release notes" in text(first)
    copy = first.findChild(qtw.QPushButton, "copyUpdateCommandButton")
    assert copy.isVisible()
    copy.click()
    assert "qplotter==2.0" in qtw.QApplication.clipboard().text()
    assert "--only-binary=:all:" in qtw.QApplication.clipboard().text()
    assert not checker._timer.isActive()
    assert checker._reply is None


def test_automatic_checks_are_opt_in_daily_and_manual_checks_override_throttle(updates, monkeypatch):
    checker, cfg, window = updates
    now = [100000.0]
    monkeypatch.setattr(_updates.time, "time", lambda: now[0])
    assert not checker.check(cfg, automatic_parent=window)
    assert not checker._manager.requests
    cfg.update(CHECK_ON_LAUNCH_KEY, True)
    assert checker.check(cfg, automatic_parent=window)
    checker._manager.replies[-1].deliver(body("1.0"))
    assert not window.findChildren(qtw.QDialog, "qplotUpdatesDialog")
    assert not checker.check(cfg, automatic_parent=window)
    dialog = checker.show_manual(window)
    assert len(checker._manager.requests) == 2
    checker._manager.replies[-1].deliver(body("1.0"))
    assert "No newer release" in text(dialog)
    now[0] += 86400
    assert checker.check(cfg, automatic_parent=window)


def test_auto_failures_are_quiet_and_manual_failures_are_visible(updates):
    checker, cfg, window = updates
    cfg.update(CHECK_ON_LAUNCH_KEY, True)
    checker.check(cfg, automatic_parent=window)
    checker._manager.replies[-1].deliver(b"", status=503)
    assert not window.findChildren(qtw.QDialog)
    dialog = checker.show_manual(window)
    checker._manager.replies[-1].deliver(b"broken json")
    assert "Could not check for updates" in text(dialog)
    assert "invalid update response" in text(dialog)


def test_manual_join_and_dismiss_does_not_reopen_automatic_notification(updates):
    checker, cfg, window = updates
    cfg.update(CHECK_ON_LAUNCH_KEY, True)
    checker.check(cfg, automatic_parent=window)
    dialog = checker.show_manual(window)
    assert len(checker._manager.requests) == 1
    sip.delete(dialog)
    checker._manager.replies[-1].deliver(body())
    assert not window.findChildren(qtw.QDialog)


def test_prerelease_preference_and_source_checkout_instructions(updates):
    checker, cfg, window = updates
    checker.info["checkout"] = True
    dialog = checker.show_manual(window)
    checker._manager.replies[-1].deliver(body("2.0rc1"))
    assert "No newer release" in text(dialog)
    cfg.update(INCLUDE_PRERELEASES_KEY, True)
    checker.show_manual(window)
    checker._manager.replies[-1].deliver(body("2.0rc1"))
    assert "2.0rc1" in text(dialog)
    assert "source checkout" in text(dialog)
    assert "qplotter==" not in text(dialog)
    assert not dialog.findChild(qtw.QPushButton, "copyUpdateCommandButton").isVisible()


def test_unpublished_project_and_deleted_window_are_handled(updates):
    checker, cfg, window = updates
    dialog = checker.show_manual(window)
    checker._manager.replies[-1].deliver(b"not found", status=404)
    assert "No release matching" in text(dialog)
    checker.show_manual(window)
    window.deleteLater()
    qtw.QApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    checker._manager.replies[-1].deliver(body())
    assert checker._reply is None


def test_network_failure_without_readable_body_is_reported(updates, monkeypatch):
    checker, cfg, window = updates
    dialog = checker.show_manual(window)
    reply = checker._manager.replies[-1]
    monkeypatch.setattr(reply, "read", lambda _size: None)
    reply.setError(QtNetwork.QNetworkReply.NetworkError.ConnectionRefusedError, "offline")
    reply.deliver(b"", status=0)
    assert "Could not contact PyPI" in text(dialog)
    assert checker._reply is None


def test_new_release_notification_and_disabling_an_inflight_check(updates):
    checker, cfg, window = updates
    cfg.update(CHECK_ON_LAUNCH_KEY, True)
    checker.check(cfg, automatic_parent=window)
    checker._manager.replies[-1].deliver(body())
    dialog = window.findChild(qtw.QDialog, "qplotUpdatesDialog")
    assert "update is available" in text(dialog)
    sip.delete(dialog)
    cfg.update(LAST_CHECK_KEY, 0)
    checker._session_last_check = 0
    checker.check(cfg, automatic_parent=window)
    cfg.update(CHECK_ON_LAUNCH_KEY, False)
    checker._manager.replies[-1].deliver(body("3.0"))
    assert not window.findChildren(qtw.QDialog)


def test_timeout_does_not_block_event_loop_and_shutdown_ignores_late_results(updates):
    checker, cfg, window = updates
    dialog = checker.show_manual(window)
    ticks = []
    QtCore.QTimer.singleShot(0, lambda: ticks.append(True))
    checker._timer.start(5)
    deadline = time.monotonic() + 2
    while "timed out" not in text(dialog) and time.monotonic() < deadline:
        QtTest.QTest.qWait(10)
    assert ticks == [True]
    assert "timed out" in text(dialog)
    assert checker._reply is None
    checker.show_manual(window)
    checker.shutdown()
    assert not checker._timer.isActive()
    assert checker._reply is None


@pytest.mark.parametrize("advertised", [True, False])
def test_oversized_response_is_aborted_and_reported(updates, advertised):
    checker, cfg, window = updates
    dialog = checker.show_manual(window)
    reply = checker._manager.replies[-1]
    reply.deliver(b"" if advertised else b"x" * (MAX_RESPONSE_BYTES + 1),
                  length=MAX_RESPONSE_BYTES + 1 if advertised else None)
    assert "too large" in text(dialog)
    assert checker._reply is None


def test_deleted_parent_and_settings_write_failure_do_not_break_callback(updates, monkeypatch):
    checker, cfg, window = updates
    monkeypatch.setattr(cfg, "update", lambda *_args: (_ for _ in ()).throw(OSError("read only")))
    dialog = checker.show_manual(window)
    sip.delete(dialog)
    checker._manager.replies[-1].deliver(body())
    assert checker._reply is None


def test_startup_schedules_only_when_enabled_and_uses_cached_running_version(updates):
    checker, cfg, window = updates
    configure_launch_check(window, {"version": "1.2"})
    assert not hasattr(window, "_update_check_timer")
    cfg.update(CHECK_ON_LAUNCH_KEY, True)
    configure_launch_check(window, {"version": "1.2"})
    timer = window._update_check_timer
    assert timer.parent() is window
    assert timer.isActive()
    assert not checker._manager.requests
    timer.stop()
    timer.timeout.emit()
    assert len(checker._manager.requests) == 1
    assert checker.info["version"] == "1.2"


def test_update_preferences_persist_cancel_and_restore_defaults(updates, monkeypatch):
    checker, cfg, window = updates
    dialog = PreferencesDialog(cfg)
    try:
        assert not dialog.checkUpdatesOnLaunchCheck.isChecked()
        assert not dialog.includePrereleasesCheck.isChecked()
        dialog.checkUpdatesOnLaunchCheck.setChecked(True)
        dialog.includePrereleasesCheck.setChecked(True)
        assert dialog.apply_preferences()
        assert config().get(CHECK_ON_LAUNCH_KEY)
        assert config().get(INCLUDE_PRERELEASES_KEY)
        dialog.checkUpdatesOnLaunchCheck.setChecked(False)
        dialog.reject()
        assert config().get(CHECK_ON_LAUNCH_KEY)
        monkeypatch.setattr(qtw.QMessageBox, "question", lambda *_args: qtw.QMessageBox.StandardButton.Yes)
        assert dialog.restore_defaults()
        assert dialog.apply_preferences()
        assert not cfg.get(CHECK_ON_LAUNCH_KEY)
        assert not cfg.get(INCLUDE_PRERELEASES_KEY)
    finally:
        dialog.close()
