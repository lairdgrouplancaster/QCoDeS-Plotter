from dataclasses import dataclass

from PyQt6 import QtCore


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
