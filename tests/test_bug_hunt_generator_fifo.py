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


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO required")
@pytest.mark.parametrize("symlink", [False, True])
def test_csv_input_rejects_fifo_without_blocking_or_changing_it(tmp_path, symlink):
    source = tmp_path / "instructions.csv"
    fifo = tmp_path / "pipe" if symlink else source
    os.mkfifo(fifo)
    if symlink:
        source.symlink_to(fifo)
    before = {entry.name: entry.lstat() for entry in tmp_path.iterdir()}
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from qplot.testdata import read_specifications, SpecificationError
try:
    read_specifications(sys.argv[1])
except SpecificationError as error:
    assert 'not a regular file' in str(error)
else:
    raise AssertionError('Special CSV input accepted')
""", str(source)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert {entry.name: entry.lstat() for entry in tmp_path.iterdir()} == before
    assert fifo.is_fifo()


def test_regular_csv_input_symlink_still_loads(tmp_path):
    from qplot.testdata import read_specifications, write_example_csv

    source = write_example_csv(tmp_path / "instructions.csv")
    alias = tmp_path / "alias.csv"
    try:
        alias.symlink_to(source)
    except OSError:
        pytest.skip("Symbolic links unavailable")
    assert read_specifications(alias) == read_specifications(source)
