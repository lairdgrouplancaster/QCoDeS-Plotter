"""One overlay's contribution to a shared plot source's lifetime.

Consumers own registration and interval connections here. The line and heatmap
owners retain their distinct monitor/reload/cancellation policies on release.
All operations run on the GUI thread.
"""

from typing import Any

from ._refresh_interval import refresh_interval_value, set_refresh_interval


class SourceConsumerRegistration:
    def __init__(self, source: Any) -> None:
        self.source = source
        self._registered = False
        self._interval_signal: Any = None
        self._interval_slot: Any = None

    def register(self, parent: Any) -> bool:
        """Retain once and follow a hidden source's host interval.

        Commit ownership before connecting: if setup raises, the consumer's
        existing cleanup can still release this registration.
        """
        if self._registered:
            return False
        source = self.source
        source._merged_trace_users = max(int(getattr(source, "_merged_trace_users", 0)), 0) + 1
        self._registered = True
        if not getattr(source, "visible", True):
            parent_spinbox = getattr(parent, "spinBox", None)
            source_spinbox = getattr(source, "spinBox", None)
            if parent_spinbox is not None and source_spinbox is not None:
                def sync_interval(_interval):
                    interval = refresh_interval_value(parent_spinbox)
                    set_refresh_interval(source_spinbox, interval)
                    source.monitorIntervalChanged(interval)

                sync_interval(refresh_interval_value(parent_spinbox))
                signal = getattr(parent_spinbox, "valueChanged", None)
                if signal is not None:
                    signal.connect(sync_interval)
                    self._interval_signal = signal
                    self._interval_slot = sync_interval
        return True

    def release(self) -> int | None:
        """Disconnect once; return remaining consumers, or None if already released."""
        if not self._registered:
            return None
        remaining = max(int(getattr(self.source, "_merged_trace_users", 1)) - 1, 0)
        self.source._merged_trace_users = remaining
        self._registered = False
        if self._interval_signal is not None:
            try:
                self._interval_signal.disconnect(self._interval_slot)
            except (TypeError, RuntimeError):
                # A deleted sender or already disconnected signal needs no work.
                pass
            self._interval_signal = None
            self._interval_slot = None
        return remaining
