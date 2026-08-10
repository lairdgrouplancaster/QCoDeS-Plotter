from collections.abc import Callable, Mapping
from typing import Any

from PyQt6 import QtWidgets as qtw

from qplot.diagnostics import log_exception


def set_widget_value_without_signals(
        widget: Any,
        setter: Callable[[Any], None],
        value: Any,
        ) -> None:
    """Set a Qt control value while preserving its signal-blocking state."""

    signals_were_blocked = widget.blockSignals(True)
    try:
        setter(value)
    finally:
        widget.blockSignals(signals_were_blocked)


def persist_config_action(
        owner: Any,
        operation: Callable[[], None],
        description: str,
        rollback: Callable[[], None] | None = None,
        ) -> bool:
    """Run one config write, rolling back GUI state and reporting one error."""

    try:
        operation()
    except Exception as error:
        log_exception(
            f"Persisting {description} failed",
            error,
            __name__,
            )
        if rollback is not None:
            try:
                rollback()
            except Exception as rollback_error:
                log_exception(
                    f"Rolling back {description} controls failed",
                    rollback_error,
                    __name__,
                    )
        _show_config_error(owner, description, error)
        return False

    return True


def persist_config_value(
        owner: Any,
        config: Any,
        key: str,
        value: Any,
        description: str,
        rollback: Callable[[], None] | None = None,
        ) -> bool:
    """Persist one config value through the shared GUI failure policy."""

    return persist_config_action(
        owner,
        lambda: config.update(key, value),
        description,
        rollback,
        )


def persist_config_values(
        owner: Any,
        config: Any,
        values: Mapping[str, Any],
        description: str,
        rollback: Callable[[], None] | None = None,
        ) -> bool:
    """Persist related config values as one atomic GUI action."""

    updates = dict(values)
    return persist_config_action(
        owner,
        lambda: config.update_many(updates),
        description,
        rollback,
        )


def _show_config_error(owner: Any, description: str, error: Exception) -> None:
    title = "Settings Not Saved"
    message = (
        f"Could not save {description}. Your previous setting is still active. "
        "Check that the qPlot configuration folder is writable, then try again."
        )
    show_error = getattr(owner, "show_error", None)
    if callable(show_error):
        try:
            show_error(title, message, str(error))
        except Exception as notification_error:
            log_exception(
                "Showing the configuration error failed",
                notification_error,
                __name__,
                )
        return

    if not isinstance(owner, qtw.QWidget):
        return

    try:
        box = qtw.QMessageBox(owner)
        box.setIcon(qtw.QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(message)
        box.setDetailedText(str(error))
        box.exec()
    except Exception as notification_error:
        log_exception(
            "Showing the configuration error failed",
            notification_error,
            __name__,
            )
