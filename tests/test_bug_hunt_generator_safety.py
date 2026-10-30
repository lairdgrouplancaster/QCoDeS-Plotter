"""Instruction CSV CLI output must never overwrite measurement artifacts."""

import os
from pathlib import Path

import pytest

from qplot import testdata


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


def _output(tmp_path, kind):
    directory = tmp_path / "instructions"
    directory.mkdir()
    filename = "example.csv" if kind == "example" else testdata.INSTRUCTION_FILE_NAMES[-1]
    return directory / filename


def _write(output, kind):
    arguments = ["--write-example", str(output)] if kind == "example" else [
        "--write-collection", str(output.parent),
    ]
    return testdata.main([*arguments, "--overwrite"])


@pytest.mark.parametrize("kind", ["example", "collection"])
@pytest.mark.parametrize("artifact", ["main", "wal", "journal", "shm"])
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_cli_instruction_overwrite_rejects_measurement_aliases(
    tmp_path, measurement_files, kind, artifact, alias
):
    output = _output(tmp_path, kind)
    target = measurement_files[artifact]
    try:
        if alias == "symlink":
            output.symlink_to(target)
        else:
            os.link(target, output)
    except OSError:
        pytest.skip(f"{alias} unavailable on this filesystem")
    before = {name: path.read_bytes() for name, path in measurement_files.items()}
    identities = {name: path.stat().st_ino for name, path in measurement_files.items()}
    with pytest.raises(SystemExit) as error:
        _write(output, kind)
    assert error.value.code == 2
    assert {name: path.read_bytes() for name, path in measurement_files.items()} == before
    assert {name: path.stat().st_ino for name, path in measurement_files.items()} == identities
    assert os.path.samefile(output, target)
    assert list(output.parent.iterdir()) == [output]


@pytest.mark.parametrize("kind", ["example", "collection"])
@pytest.mark.parametrize("artifact", ["main", "wal", "journal"])
def test_cli_instruction_overwrite_rejects_sqlite_content_named_csv(
    tmp_path, measurement_files, kind, artifact
):
    output = _output(tmp_path, kind)
    original = measurement_files[artifact].read_bytes()
    output.write_bytes(original)
    with pytest.raises(SystemExit) as error:
        _write(output, kind)
    assert error.value.code == 2
    assert output.read_bytes() == original
    assert list(output.parent.iterdir()) == [output]


@pytest.mark.parametrize("existing", [False, True])
def test_instruction_publication_rechecks_target_before_replace(tmp_path, monkeypatch, existing):
    output = tmp_path / "example.csv"
    if existing:
        output.write_bytes(b"old CSV")
    original_fsync = testdata.os.fsync
    database_bytes = b"SQLite format 3\x00" + bytes(64)

    def introduce_database(descriptor):
        original_fsync(descriptor)
        output.write_bytes(database_bytes)

    monkeypatch.setattr(testdata.os, "fsync", introduce_database)
    with pytest.raises(testdata.SpecificationError):
        testdata.write_example_csv(output, overwrite=True)
    assert output.read_bytes() == database_bytes
    assert not list(tmp_path.glob(".qplot-instructions-*"))


@pytest.mark.parametrize("kind", ["example", "collection"])
def test_cli_instruction_overwrite_still_replaces_ordinary_csv(tmp_path, kind):
    output = _output(tmp_path, kind)
    output.write_bytes(b"old CSV")
    assert _write(output, kind) == 0
    assert testdata.read_specifications(output)
    assert not list(output.parent.glob(".qplot-instructions-*"))
