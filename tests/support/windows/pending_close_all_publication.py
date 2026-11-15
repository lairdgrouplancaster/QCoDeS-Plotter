"""Shared fixtures and assertions for pending close all publication."""

import hashlib
from pathlib import Path

from qcodes.dataset import (
    Measurement,
)

__all__ = [
    '_protected_artifacts',
    '_record_run',
]


def _protected_artifacts(database):
    result = {}
    for suffix in ("", "-wal", "-journal"):
        path = Path(f"{database}{suffix}")
        if not path.exists():
            result[suffix] = None
            continue
        metadata = path.stat()
        result[suffix] = (
            metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return result


def _record_run(writer, experiment, database, offset):
    assert Path(writer.execute("PRAGMA database_list").fetchone()[2]).resolve() == database
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("bias")
    measurement.register_custom_parameter("signal", setpoints=("bias",))
    with measurement.run(write_in_background=False) as saver:
        assert Path(
            saver.dataset.conn.execute("PRAGMA database_list").fetchone()[2]
        ).resolve() == database
        for bias in (-4., -1., 0., 3., 8.):
            saver.add_result(("bias", bias), ("signal", offset + bias * bias))
    return saver.dataset.guid
