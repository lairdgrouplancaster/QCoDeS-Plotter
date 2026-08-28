import sys

from qplot._metadata import package_version
from qplot.diagnostics import (
    configure_logging,
    install_excepthook,
    log_event,
    log_exception,
)

QT_OPTIONS_WITH_VALUES = {
    "-display",
    "-font",
    "-geometry",
    "-name",
    "-platform",
    "-platformpluginpath",
    "-platformtheme",
    "-plugin",
    "-qwindowgeometry",
    "-qwindowicon",
    "-qwindowtitle",
    "-session",
    "-style",
    "-stylesheet",
    "-title",
    "-visual",
}


def _database_path_from_arguments(args):
    """
    Return the first database path passed on the command line.

    File managers pass the double-clicked file as a plain positional argument.
    Qt options are ignored here so they can still be handled by QApplication.

    """
    skip_next = False
    positional_only = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            positional_only = True
            continue
        if not positional_only and arg in QT_OPTIONS_WITH_VALUES:
            skip_next = True
            continue
        if not positional_only and arg.startswith("-"):
            continue
        return arg

    return None


def _configure_application_identity(app):
    """
    Sets the application name used by native desktop menus.

    """
    app.setApplicationName("qPlot")
    if hasattr(app, "setApplicationDisplayName"):
        app.setApplicationDisplayName("qPlot")


def _finalize_confirmed_process_shutdown(
    app,
    window,
    process_fail_safe,
    *,
    retain_objects,
):
    """Destroy quiescent Qt ownership while launcher containment stays armed."""

    from PyQt6 import sip

    from qplot.windows import MainWindow

    if not process_fail_safe.armed:
        return

    deadline_exhausted = bool(getattr(window, "_shutdown_deadline_exhausted", False))
    quiescent = False
    if not deadline_exhausted:
        try:
            quiescent = not MainWindow._shutdown_background_work_active(window)
        except BaseException:
            quiescent = False

    if retain_objects:
        if quiescent:
            process_fail_safe.disarm()
            return
        process_fail_safe.wait_for_forced_exit()
        return

    # Never enter a QThreadPool-owning destructor after the deadline or while
    # the final resource scan is non-quiescent.  sip.delete() may hold this
    # entry-point thread indefinitely, which would otherwise make its
    # hard-deadline os._exit fallback unreachable if the launcher failed.
    if deadline_exhausted or not quiescent:
        process_fail_safe.wait_for_forced_exit()
        return

    # The launcher remains armed through both destructors in the quiescent path.
    if not process_fail_safe.watchdog_operational():
        process_fail_safe.wait_for_forced_exit()
        return
    try:
        sip.delete(window)
        if not process_fail_safe.watchdog_operational():
            process_fail_safe.wait_for_forced_exit()
            return
        sip.delete(app)
    except BaseException:
        process_fail_safe.wait_for_forced_exit()
        raise
    process_fail_safe.disarm()


def _run_gui(
    return_objects=False,
    database_path=None,
    *,
    shutdown_supervisor_client=None,
    shutdown_supervisor_diagnostic=None,
):
    """Run Qt in this process, optionally with an authenticated launcher client."""

    from PyQt6 import QtWidgets as qtw

    from qplot.windows import MainWindow
    from qplot.windows.main import _ProcessShutdownFailSafe

    configure_logging()
    install_excepthook()
    log_event("Starting qPlot %s", package_version())
    print("Initialising GUI, this may take a few seconds.\n")

    try:
        app = qtw.QApplication(sys.argv)
        _configure_application_identity(app)
        if database_path is None:
            database_path = _database_path_from_arguments(sys.argv[1:])
        w = MainWindow(startup_database_path=database_path)
        process_fail_safe = None
        if hasattr(w, "_shutdown_process_fail_safe"):
            process_fail_safe = _ProcessShutdownFailSafe(
                shutdown_supervisor_client,
                startup_diagnostic=shutdown_supervisor_diagnostic,
            )
            w._shutdown_process_fail_safe = process_fail_safe
        exit_code = app.exec()
    except Exception as err:
        log_exception("qPlot startup failed", err)
        raise

    log_event("qPlot event loop exited with code %s", exit_code)

    if process_fail_safe is not None:
        _finalize_confirmed_process_shutdown(
            app,
            w,
            process_fail_safe,
            retain_objects=bool(return_objects),
        )

    if return_objects:
        return app, w
    return exit_code


def run(return_objects=False, database_path=None):
    """
    Entry point for opening the qplot app.

    Parameters
    ----------
    return_objects : bool, optional
        If true, returns the QApplication and MainWindow after the event loop
        exits. This deliberately runs in the caller's process, which owns the
        returned objects and their cleanup; it does not acquire the default
        launcher's process-tree containment or hard-deadline guarantee. The
        default is false so the command-line entry point uses that launcher and
        exits quietly and successfully.
    database_path : str, optional
        QCoDeS database path to load after the main window opens. When omitted,
        qPlot uses the first positional path passed on the command line, if any.

    Returns
    -------
    tuple[PyQt6.QtWidgets.QApplication, qplot.windows.main.MainWindow] | int
        Application objects when return_objects is true; otherwise the Qt
        event-loop exit status.

    """
    if return_objects:
        return _run_gui(return_objects=True, database_path=database_path)

    from qplot._shutdown_supervisor import launch_gui

    return launch_gui(original_argv=sys.argv, database_path=database_path)


def run_public(return_objects=False, database_path=None):
    """Run qPlot for library callers without containing the caller process.

    The default path delegates GUI ownership to a dedicated launcher process.
    It returns the GUI status normally; on POSIX, termination by signal is
    represented non-destructively as ``-signal_number``.  Unlike the command
    line entry point, this function never reproduces that signal or hard-exits
    the Python process that called :func:`qplot.run`.

    Caller control-flow exceptions are preserved while the dedicated launcher
    terminates and reaps its complete tree, then re-raised unchanged after the
    authenticated outcome and launcher EOF have been observed.

    ``return_objects=True`` intentionally retains the historical in-process,
    caller-owned behavior and does not provide the hard-deadline guarantee.
    """
    if return_objects:
        return _run_gui(return_objects=True, database_path=database_path)

    from qplot._shutdown_supervisor import launch_gui_for_api

    return launch_gui_for_api(original_argv=sys.argv, database_path=database_path)


if __name__ == "__main__":
    raise SystemExit(run())
