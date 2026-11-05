"""Scoped signal suppression that preserves each control's previous state."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Protocol


class _SignalBlockable(Protocol):
    def blockSignals(self, block: bool, /) -> bool: ...


@contextmanager
def blocked_signals(*controls: _SignalBlockable) -> Iterator[None]:
    """Restore prior states on exit, including nested scopes and failures.

    Restore in reverse order so passing the same control more than once still
    respects its original state. Controls remain owned by the caller.
    """
    with ExitStack() as restore:
        for control in controls:
            previous = control.blockSignals(True)
            restore.callback(control.blockSignals, previous)
        yield
