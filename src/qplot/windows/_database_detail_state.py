"""State shared by the basic and expensive snapshot detail channels."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from qplot.datahandling.file_identity import DatabaseInstance


class DetailWorker(Protocol):
    def cancel(self) -> None: ...
    def prioritize_run_ids(self, run_ids: list[Any]) -> None: ...


@dataclass
class DetailLoadChannel:
    generation: int = 0
    active: bool = False
    worker: DetailWorker | None = None
    instance: DatabaseInstance | None = None

    def cancel_worker(self) -> None:
        if self.worker is not None:
            self.worker.cancel()

    def invalidate(self) -> None:
        self.generation += 1
        self.clear()

    def clear(self) -> None:
        self.active = False
        self.worker = None
        self.instance = None

    def accepts(self, generation: int) -> bool:
        return self.active and self.generation == generation


@dataclass
class DatabaseDetailChannels:
    basic: DetailLoadChannel = field(default_factory=DetailLoadChannel)
    expensive: DetailLoadChannel = field(default_factory=DetailLoadChannel)


def detail_channel(owner: Any, expensive: bool = False) -> DetailLoadChannel:
    channels = owner.__dict__.get("_database_detail_channels")
    if channels is None:
        channels = DatabaseDetailChannels()
        owner._database_detail_channels = channels
    return channels.expensive if expensive else channels.basic
