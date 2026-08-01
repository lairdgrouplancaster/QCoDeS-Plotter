import os
from dataclasses import dataclass

from PyQt6 import QtCore


def close_dataset_connection(dataset: object) -> bool:
    """Close a dataset's backing connection when it exposes one."""

    connection = getattr(dataset, "conn", None)
    close = getattr(connection, "close", None)
    if not callable(close):
        return False
    close()
    return True


def canonical_database_path(database_path: str | os.PathLike[str]) -> str:
    """Return a stable, platform-normalised identity for a database file."""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(database_path))))


@dataclass(frozen=True, slots=True)
class DatasetKey:
    """Identifies a dataset within one particular database file."""

    database_path: str
    guid: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "database_path", canonical_database_path(self.database_path))
        object.__setattr__(self, "guid", str(self.guid))


@dataclass(frozen=True, slots=True)
class TraceKey:
    """Identifies one trace source in one database-backed dataset."""

    dataset_key: DatasetKey
    parameter_name: str
    sweep_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameter_name", str(self.parameter_name))
        if self.sweep_id is not None:
            object.__setattr__(self, "sweep_id", int(self.sweep_id))


@dataclass
class DatasetHandle:
    """
    Tracks a dataset held open by one or more plot windows.

    """

    dataset: object
    users: int = 1
    delete_timer: QtCore.QTimer | None = None
    closed: bool = False

    def retain(self):
        """
        Records another active user and cancels pending delayed deletion.

        """
        self.users += 1
        self.cancel_delete_timer()


    def release(self):
        """
        Records that one active user has closed.

        """
        self.users -= 1
        return self.users


    def cancel_delete_timer(self):
        """
        Stops any pending delayed deletion timer.

        """
        if self.delete_timer is not None:
            self.delete_timer.stop()
            self.delete_timer = None


    def close(self):
        """Cancel pending eviction and deterministically close the dataset."""

        self.cancel_delete_timer()
        if self.closed:
            return False
        closed_connection = close_dataset_connection(self.dataset)
        self.closed = True
        return closed_connection
