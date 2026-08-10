"""Compatibility helpers for QCoDeS dataset implementation details."""

from qcodes.dataset.data_set import DataSet


def result_owns_supplied_connection(result: object) -> bool:
    """Return whether QCoDeS transferred a supplied connection to ``result``.

    QCoDeS' database-backed ``DataSet`` owns the connection passed to its
    loader. Other dataset protocol implementations, including ``DataSetInMem``,
    do not. Keep this implementation-specific type knowledge in one place.
    """

    return isinstance(result, DataSet)
