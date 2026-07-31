import os
from dataclasses import dataclass

from PyQt6 import QtCore


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


@dataclass
class DatasetHandle:
    """
    Tracks a dataset held open by one or more plot windows.

    """

    dataset: object
    users: int = 1
    delete_timer: QtCore.QTimer | None = None

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
