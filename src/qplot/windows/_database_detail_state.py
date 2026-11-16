"""Metadata-loading presentation and snapshot detail channel state."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from qplot.datahandling.file_identity import DatabaseInstance

if TYPE_CHECKING:
    from ._database_detail_controller import DatabaseDetailController

BASIC_METADATA_COLUMNS = ("Started", "Status", "Duration", "Completed")
EXPENSIVE_METADATA_COLUMNS = ("Setpoints", "Size")


def set_run_metadata_loading(owner, guids=None, **kwargs):
    """Notify run-list views without adding presentation flags to run data."""
    setter = getattr(getattr(owner, "RunList", None), "set_metadata_loading", None)
    if callable(setter):
        setter(guids, **kwargs)


class DetailWorker(Protocol):
    def cancel(self) -> None: ...
    def prioritize_run_ids(self, run_ids: list[Any]) -> None: ...


@dataclass
class DetailLoadChannel:
    generation: int = 0
    active: bool = False
    worker: DetailWorker | None = None
    instance: DatabaseInstance | None = None
    path: str | None = None
    failed: bool = False
    publishing: bool = False
    pending_batches: deque[dict[int, dict[str, Any]]] = field(default_factory=deque)
    deferred_completion: tuple[object] | None = None

    def invalidate(self) -> None:
        self.generation += 1
        self.clear()

    def clear(self) -> None:
        self.active = False
        self.worker = None
        self.instance = None
        self.path = None
        self.failed = False
        self.publishing = False
        self.pending_batches.clear()
        self.deferred_completion = None

    def accepts(self, generation: int) -> bool:
        return self.active and self.generation == generation


@dataclass
class DatabaseDetailChannels:
    basic: DetailLoadChannel = field(default_factory=DetailLoadChannel)
    expensive: DetailLoadChannel = field(default_factory=DetailLoadChannel)


def detail_controller(owner: Any) -> DatabaseDetailController:
    """Return the owner's single controller, including for embedded window hosts."""
    from ._database_detail_controller import DatabaseDetailController
    from ._database_detail_ui import DatabaseDetailUi

    controller = owner.__dict__.get("_database_detail_loader")
    if controller is None:
        controller = DatabaseDetailController(DatabaseDetailUi(owner))
        owner._database_detail_loader = controller
    return controller


def detail_channel(owner: Any, expensive: bool = False) -> DetailLoadChannel:
    return detail_controller(owner).channel(expensive)
