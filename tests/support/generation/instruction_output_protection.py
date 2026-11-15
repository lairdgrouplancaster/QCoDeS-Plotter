"""Shared fixtures and assertions for instruction output protection."""

from pathlib import Path

import pytest

from qplot import testdata

__all__ = [
    'measurement_files',
]


@pytest.fixture
def measurement_files(tmp_path):
    database = tmp_path / "measurement.db"
    testdata.generate_database(
        [
            testdata.RunSpecification(
                (
                    testdata.MeasurementSpecification(
                        "current",
                        "Current",
                        "A",
                        (
                            testdata.AxisSpecification(
                                "V_SD", "Source-drain voltage", "V", 0.0, 1.0, 2
                            ),
                        ),
                    ),
                )
            )
        ],
        database,
    )
    artifacts = {
        "main": database,
        "wal": Path(f"{database}-wal"),
        "journal": Path(f"{database}-journal"),
        "shm": Path(f"{database}-shm"),
    }
    # Headers identify WAL/journal aliases; SHM deliberately has no unique
    # SQLite magic and needs the alias check regardless of its contents.
    artifacts["wal"].write_bytes(b"\x37\x7f\x06\x82" + bytes(64))
    artifacts["journal"].write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + bytes(64))
    artifacts["shm"].write_bytes(bytes(128))
    return artifacts
