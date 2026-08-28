import os
from dataclasses import dataclass, field

from PyQt6 import QtCore

from qplot.datahandling.file_identity import (
    DatabaseFileIdentity,
    DatabaseInstance,
    canonical_database_path,
    database_file_identity,
    database_instance,
    database_sidecar_identities,
    logical_database_path,
)

_CAPTURE_CURRENT_SIDECARS: frozenset[DatabaseFileIdentity] = frozenset()


def close_dataset_connection(dataset: object) -> bool:
    """Close a dataset's backing connection when it exposes one."""

    connection = getattr(dataset, "conn", None)
    close = getattr(connection, "close", None)
    if not callable(close):
        return False
    close()
    return True


@dataclass(frozen=True, slots=True)
class DatasetKey:
    """Identifies a dataset within one particular database file."""

    database_path: str
    guid: str
    database_identity: DatabaseFileIdentity | None = None
    resolved_database_path: str | None = None
    sidecar_identities: frozenset[DatabaseFileIdentity] = field(
        default=_CAPTURE_CURRENT_SIDECARS,
        compare=False,
        hash=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        logical_path = logical_database_path(self.database_path)
        object.__setattr__(self, "database_path", logical_path)
        object.__setattr__(self, "guid", str(self.guid))
        if self.resolved_database_path is None:
            object.__setattr__(
                self,
                "resolved_database_path",
                canonical_database_path(logical_path),
            )
        else:
            object.__setattr__(
                self,
                "resolved_database_path",
                canonical_database_path(self.resolved_database_path),
            )
        if self.database_identity is None:
            resolved_path = self.resolved_database_path
            assert resolved_path is not None
            object.__setattr__(
                self,
                "database_identity",
                database_file_identity(resolved_path),
            )
        # Identity distinguishes an omitted argument from an explicitly bound
        # empty sidecar set.  The latter must stay empty: recapturing a WAL/SHM
        # that appeared later would silently rebind an existing request.
        if self.sidecar_identities is _CAPTURE_CURRENT_SIDECARS:
            object.__setattr__(
                self,
                "sidecar_identities",
                database_sidecar_identities(
                    logical_path,
                    self.resolved_database_path,
                ),
            )


def dataset_key_matches_database_instance(
        dataset_key: DatasetKey,
        current_instance: DatabaseInstance,
        *,
        allow_new_sidecars: bool = False,
        ) -> bool:
    """Return whether an observation still represents ``dataset_key``.

    A transient explicit-action key may allow the first WAL/SHM appearance
    while its read-only DataSet is being materialised.  A key retained by a
    plot is final and therefore requires the exact observed sidecar set.
    """

    if current_instance.logical_path != dataset_key.database_path:
        return False
    if current_instance.resolved_path != dataset_key.resolved_database_path:
        return False

    expected_identity = dataset_key.database_identity
    if expected_identity is None:
        if current_instance.identity is not None or os.path.isfile(
                dataset_key.database_path
                ):
            return False
    elif current_instance.identity != expected_identity:
        return False

    expected_sidecars = dataset_key.sidecar_identities
    if allow_new_sidecars:
        return expected_sidecars.issubset(current_instance.sidecar_identities)
    return expected_sidecars == current_instance.sidecar_identities


def dataset_key_matches_current_source(
        dataset_key: DatasetKey,
        *,
        allow_new_sidecars: bool = False,
        ) -> bool:
    """Inspect only filesystem identity metadata for a dataset source."""

    return dataset_key_matches_database_instance(
        dataset_key,
        database_instance(dataset_key.database_path),
        allow_new_sidecars=allow_new_sidecars,
    )


def bind_dataset_key_to_current_source(
        dataset_key: DatasetKey,
        ) -> DatasetKey | None:
    """Promote compatible first sidecars into an exact retained-plot key."""

    current_instance = database_instance(dataset_key.database_path)
    if not dataset_key_matches_database_instance(
            dataset_key,
            current_instance,
            allow_new_sidecars=True,
            ):
        return None

    bound_key = DatasetKey(
        current_instance.logical_path,
        dataset_key.guid,
        current_instance.identity,
        current_instance.resolved_path,
        sidecar_identities=current_instance.sidecar_identities,
    )
    # Close the metadata-only capture race before returning a key that callers
    # may retain for automatic refreshes.
    if not dataset_key_matches_current_source(bound_key):
        return None
    return bound_key


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
    database_identity: DatabaseFileIdentity | None = None
    sidecar_identities: frozenset[DatabaseFileIdentity] | None = None

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
