"""Application-owned, cancellable update checks using Qt's asynchronous network API."""

import shlex
import subprocess
import sys
import time
import weakref
from html import escape
from typing import Any, cast

from packaging.version import InvalidVersion, Version
from PyQt6 import QtCore, QtNetwork, sip
from PyQt6 import QtWidgets as qtw

from qplot._metadata import version_info
from qplot.diagnostics import log_exception
from qplot.updates import (
    CHECK_ON_LAUNCH_KEY,
    CHECK_TIMEOUT_MS,
    INCLUDE_PRERELEASES_KEY,
    LAST_CHECK_KEY,
    MAX_RESPONSE_BYTES,
    PYPI_INDEX_URL,
    RELEASE_NOTES_URL,
    UpdateResult,
    check_due,
    latest_release,
)


class UpdateChecker(QtCore.QObject):
    """Share one finite request across Help menus; never occupy a data worker."""

    def __init__(self, app: qtw.QApplication, info: dict):
        super().__init__(app)
        self.info = info
        self._manager: QtNetwork.QNetworkAccessManager | None = None
        self._reply: QtNetwork.QNetworkReply | None = None
        self._buffer = bytearray()
        self._error = ""
        self._config: Any = None
        self._dialogs: list[weakref.ReferenceType[qtw.QDialog]] = []
        self._automatic_parent: weakref.ReferenceType[qtw.QMainWindow] | None = None
        self._session_last_check = 0.0
        self._stopping = False
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timed_out)
        app.aboutToQuit.connect(self.shutdown)

    def check(self, config: Any, *, dialog=None, automatic_parent=None) -> bool:
        if self._stopping:
            return False
        if dialog is not None:
            self._automatic_parent = None
        if dialog is not None and all(ref() is not dialog for ref in self._dialogs):
            self._dialogs.append(weakref.ref(dialog))
        if self._reply is not None:
            return True  # A manual request can join an automatic request.
        self._config = config
        now = time.time()
        if automatic_parent is not None:
            if (
                not config.get(CHECK_ON_LAUNCH_KEY)
                or not check_due(config.get(LAST_CHECK_KEY), now)
                or not check_due(self._session_last_check, now)
            ):
                return False
            self._automatic_parent = weakref.ref(automatic_parent)
        self._buffer.clear()
        self._error = ""
        try:
            if self.info["version"] == "0+unknown":
                raise InvalidVersion("Unknown installed version")
            Version(self.info["version"])
        except (InvalidVersion, KeyError, TypeError):
            self._present(None, "qPlot could not determine its running version.")
            return False

        if self._manager is None:
            self._manager = QtNetwork.QNetworkAccessManager(self)
        request = QtNetwork.QNetworkRequest(QtCore.QUrl(PYPI_INDEX_URL))
        request.setRawHeader(b"Accept", b"application/vnd.pypi.simple.v1+json")
        request.setRawHeader(b"User-Agent", f"qPlot/{Version(self.info['version'])}".encode("ascii"))
        request.setAttribute(
            QtNetwork.QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QtNetwork.QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setTransferTimeout(CHECK_TIMEOUT_MS)
        reply = self._manager.get(request)
        if reply is None:
            self._present(None, "Could not start the update check. Please try again later.")
            return False
        self._reply = reply
        reply.setReadBufferSize(MAX_RESPONSE_BYTES + 1)
        reply.readyRead.connect(self._read_ready)
        reply.finished.connect(self._finished)
        self._timer.start(CHECK_TIMEOUT_MS)
        self._session_last_check = now
        try:
            config.update(LAST_CHECK_KEY, now)
        except Exception as error:
            # This is a background convenience; a settings failure is not a
            # reason to interrupt startup with a modal dialog or retry storm.
            log_exception("Saving the update-check time failed", error, __name__)
        return True

    def _read_ready(self):
        reply = self._reply
        if reply is None or self._error:
            return
        length = reply.header(QtNetwork.QNetworkRequest.KnownHeaders.ContentLengthHeader)
        try:
            if length is not None and int(length) > MAX_RESPONSE_BYTES:
                self._error = "The update response is too large."
        except (TypeError, ValueError, OverflowError):
            self._error = "PyPI returned an invalid update response. Please try again later."
        if not self._error:
            chunk = reply.read(MAX_RESPONSE_BYTES - len(self._buffer) + 1)
            if chunk:
                self._buffer.extend(chunk)
            if len(self._buffer) > MAX_RESPONSE_BYTES:
                self._error = "The update response is too large."
        if self._error:
            reply.abort()

    def _timed_out(self):
        self._error = "The update check timed out. Check your Internet connection and try again."
        if self._reply is not None:
            self._reply.abort()

    def _finished(self):
        reply = self._reply
        if reply is None:
            return
        self._read_ready()
        # abort() can emit finished synchronously during the size check.
        if self._reply is not reply:
            return
        self._reply = None
        self._timer.stop()
        result = None
        error = self._error
        try:
            if not error:
                status = reply.attribute(QtNetwork.QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                if status == 404:
                    result = UpdateResult(Version(self.info["version"]), None)
                elif reply.error() != QtNetwork.QNetworkReply.NetworkError.NoError or status != 200:
                    error = "Could not contact PyPI. Check your Internet connection and try again."
                else:
                    result = UpdateResult(
                        Version(self.info["version"]),
                        latest_release(
                            bytes(self._buffer),
                            include_prereleases=self._config.get(INCLUDE_PRERELEASES_KEY),
                        ),
                    )
        except (ValueError, TypeError, RecursionError):
            error = "PyPI returned an invalid update response. Please try again later."
        finally:
            reply.deleteLater()
            self._buffer.clear()
        if not self._stopping:
            self._present(result, error)

    def _present(self, result: UpdateResult | None, error: str):
        html = self._result_html(result, error)
        dialogs, self._dialogs = self._dialogs, []
        shown = False
        for ref in dialogs:
            dialog = ref()
            if dialog is not None and not sip.isdeleted(dialog) and dialog.isVisible():
                browser = dialog.findChild(qtw.QTextBrowser, "qplotHelpBrowser")
                if browser is not None:
                    browser.setHtml(html)
                    self._set_upgrade_command(dialog, result)
                    shown = True
        parent = self._automatic_parent() if self._automatic_parent is not None else None
        self._automatic_parent = None
        if (
            not shown and not error and result is not None and result.update_available
            and parent is not None and not sip.isdeleted(parent) and parent.isVisible()
            and not getattr(parent, "_shutdown_started", False)
            and self._config.get(CHECK_ON_LAUNCH_KEY)
        ):
            self._new_dialog(parent, html, result)

    def _result_html(self, result: UpdateResult | None, error: str) -> str:
        if error:
            return f"<h2>Could not check for updates</h2><p>{escape(error)}</p>"
        assert result is not None
        heading = "A qPlot update is available" if result.update_available else "No newer release available"
        html = f"<h2>{heading}</h2><p>You are running qPlot <b>{result.current}</b>.</p>"
        if result.latest is None:
            html += "<p>No release matching your update preferences and Python version was found on PyPI.</p>"
        else:
            html += f"<p>Latest matching published release: <b>{result.latest}</b>.</p>"
        html += f'<p><a href="{RELEASE_NOTES_URL}">Release notes</a> · '
        html += '<a href="https://pypi.org/project/qplotter/">qPlot on PyPI</a></p>'
        if result.update_available:
            if self.info.get("checkout"):
                html += (
                    "<p>This is a source checkout. Close qPlot and update your checkout "
                    "using your usual Git workflow.</p>"
                )
            else:
                command = self._upgrade_command(result)
                html += (
                    "<p>Close qPlot, then run this command in a terminal to upgrade "
                    "the Python environment used by this installation:</p>"
                    f"<p><code>{escape(command)}</code></p>"
                )
        return html

    def _upgrade_command(self, result):
        if result is None or not result.update_available or self.info.get("checkout"):
            return ""
        arguments = [sys.executable, "-m", "pip", "install", "--upgrade",
                     "--only-binary=:all:", f"qplotter=={result.latest}"]
        return subprocess.list2cmdline(arguments) if sys.platform == "win32" else shlex.join(arguments)

    def _set_upgrade_command(self, dialog, result):
        command = self._upgrade_command(result)
        dialog.setProperty("qplotUpgradeCommand", command)
        button = dialog.findChild(qtw.QPushButton, "copyUpdateCommandButton")
        if button is not None:
            button.setVisible(bool(command))

    def _new_dialog(self, parent, html, result=None):
        from ._help import _show_help_dialog

        dialog = _show_help_dialog(
            parent, "Check for Updates", html, "qplotUpdatesDialog", size=(650, 430),
        )
        buttons = dialog.findChild(qtw.QDialogButtonBox)
        if buttons is not None:
            copy = buttons.addButton("Copy upgrade command", qtw.QDialogButtonBox.ButtonRole.ActionRole)
            if copy is not None:
                copy.setObjectName("copyUpdateCommandButton")

                def copy_command():
                    clipboard = qtw.QApplication.clipboard()
                    if clipboard is not None:
                        clipboard.setText(str(dialog.property("qplotUpgradeCommand")))

                copy.clicked.connect(copy_command)
        self._set_upgrade_command(dialog, result)
        return dialog

    def show_manual(self, parent):
        dialog = next((dialog for dialog in parent.findChildren(qtw.QDialog, "qplotUpdatesDialog")
                       if dialog.isVisible()), None)
        if dialog is None:
            dialog = self._new_dialog(parent, "<h2>Checking for updates…</h2><p>Contacting PyPI…</p>")
        else:
            dialog.show()
            dialog.raise_()
            browser = dialog.findChild(qtw.QTextBrowser, "qplotHelpBrowser")
            if browser is not None:
                browser.setHtml("<h2>Checking for updates…</h2><p>Contacting PyPI…</p>")
        self._set_upgrade_command(dialog, None)
        self.check(parent.config, dialog=dialog)
        return dialog

    def shutdown(self):
        self._stopping = True
        self._timer.stop()
        self._dialogs.clear()
        self._automatic_parent = None
        if self._reply is not None:
            self._reply.abort()


def update_checker(info: dict | None = None) -> UpdateChecker:
    app = cast(Any, qtw.QApplication.instance())
    checker = getattr(app, "_qplot_update_checker", None)
    if checker is None:
        checker = UpdateChecker(app, info if info is not None else version_info())
        app._qplot_update_checker = checker
    elif info is not None:
        checker.info = info
    return checker


def check_for_updates(window):
    return update_checker().show_manual(window)


def configure_launch_check(window, info):
    """Keep version lookup cached and run an opted-in check after the window opens."""
    checker = update_checker(info)
    if window.config.get(CHECK_ON_LAUNCH_KEY):
        timer = QtCore.QTimer(window)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: checker.check(window.config, automatic_parent=window))
        timer.start(1000)
        window._update_check_timer = timer
