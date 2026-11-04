"""Directional source checks shared by loading and refresh publication."""

from qplot.datahandling.file_identity import DatabaseInstance, database_instances_differ


def database_instances_or_sidecars_differ(
    first: DatabaseInstance | None,
    second: DatabaseInstance | None,
) -> bool:
    """Reject replacement of an accepted main/sidecar identity.

    A writer or SQLite may create the exact WAL/SHM between finite reader
    operations. Later creation is compatible; removal or replacement of an
    already accepted identity is not. With no accepted observation there is
    no previous source to invalidate.
    """
    if first is None or second is None:
        return False
    return database_instances_differ(first, second) or not (
        first.sidecar_identities.issubset(second.sidecar_identities)
    )
