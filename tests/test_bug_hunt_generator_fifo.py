"""Synthetic publication must reject special destinations without blocking."""

import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO required")
@pytest.mark.parametrize("symlink", [False, True])
def test_generator_rejects_fifo_destination_before_opening(tmp_path, symlink):
    fifo = tmp_path / ("pipe" if symlink else "output.db")
    os.mkfifo(fifo)
    destination = tmp_path / "output.db"
    if symlink:
        destination.symlink_to(fifo)
    before = {path.name: path.lstat() for path in tmp_path.iterdir()}

    # Opening a FIFO for reading waits indefinitely for a writer. Keep this
    # public-workflow regression bounded even if validation regresses.
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from qplot.testdata import RunSpecification, SpecificationError, generate_database
try:
    generate_database(
        [RunSpecification(1, "current", "Current", "nA", -1., 1., 5)],
        sys.argv[1], overwrite=True,
    )
except SpecificationError:
    pass
else:
    raise AssertionError("Special destination was accepted")
""", str(destination)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    after = {path.name: path.lstat() for path in tmp_path.iterdir()}
    assert after == before
    assert fifo.is_fifo()
    assert destination.is_symlink() == symlink
