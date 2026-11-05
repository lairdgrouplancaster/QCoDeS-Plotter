"""Ownership and delayed eviction for datasets retained by plot windows."""

from collections.abc import Callable
from typing import Any

from PyQt6 import QtCore

from qplot.diagnostics import log_exception

from ._dataset_handle import DatasetHandle, DatasetKey, close_dataset_connection


class DatasetRegistry:
    """Keep retained handles and the current selection from closing each other.

    The selected dataset is resolved at each close boundary. Database-view
    rollback can restore a selection without creating a second ownership copy.
    Source identity validation remains the action controller's responsibility.
    """

    def __init__(
        self,
        handles: dict[DatasetKey, DatasetHandle],
        selected_dataset: Callable[[], object | None],
    ) -> None:
        self.handles = handles
        self._selected_dataset = selected_dataset

    def holds(self, dataset: object | None) -> bool:
        return dataset is not None and any(
            handle.dataset is dataset for handle in self.handles.values()
        )

    def close_unowned(self, dataset: object | None, *, context: str) -> bool:
        if dataset is None or dataset is self._selected_dataset() or self.holds(dataset):
            return False
        try:
            return close_dataset_connection(dataset)
        except Exception as error:
            log_exception(context, error, __name__)
            return False

    def discard(self, key: DatasetKey, handle: DatasetHandle) -> None:
        """A stale source must close even when the selection still refers to it."""
        if self.handles.get(key) is handle:
            self.handles.pop(key, None)
        self._close_handle(handle, "Stale plot dataset cleanup failed")

    def add(self, key: DatasetKey, dataset: object) -> None:
        self.handles[key] = DatasetHandle(
            dataset=dataset,
            database_identity=key.database_identity,
            sidecar_identities=key.sidecar_identities,
        )

    def release(self, key: DatasetKey, grace_period: Callable[[], float]) -> bool:
        handle = self.handles.get(key)
        if handle is None:
            return False
        if handle.release() <= 0:
            delay = grace_period()
            if delay == 0:
                self.evict(key)
            elif handle.delete_timer is None:
                timer = QtCore.QTimer()
                timer.setSingleShot(True)
                handle.delete_timer = timer
                timer.timeout.connect(lambda: self.evict(key))
                timer.start(int(delay * 1000))
        return True

    def evict(self, key: DatasetKey) -> bool:
        handle = self.handles.pop(key, None)
        if handle is None:
            return False
        handle.cancel_delete_timer()
        if handle.dataset is not self._selected_dataset():
            self._close_handle(handle, "Plot dataset cleanup failed")
        return True

    def close_all(self) -> None:
        handles = list(self.handles.values())
        self.handles.clear()
        for handle in handles:
            self._close_handle(handle, "Plot dataset cleanup failed")

    @staticmethod
    def _close_handle(handle: DatasetHandle, context: str) -> None:
        try:
            handle.close()
        except Exception as error:
            log_exception(context, error, __name__)


def dataset_registry(owner: Any) -> DatasetRegistry:
    """Resolve the registry for the controller's current handle mapping."""
    registry = owner.__dict__.get("_dataset_registry")
    if registry is None or registry.handles is not owner.dataset_holder:
        registry = DatasetRegistry(owner.dataset_holder, lambda: getattr(owner, "ds", None))
        owner._dataset_registry = registry
    return registry
