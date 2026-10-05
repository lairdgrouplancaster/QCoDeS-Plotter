"""Finite native pathname-proof reuse retains handle and publication checks."""

import os
import shutil
import sqlite3
import threading

import pytest

from qplot.datahandling.trusted_live import (
    TrustedLiveCancelledError,
    TrustedLiveDeadlineExceededError,
    TrustedLiveQueryError,
    TrustedLiveReader,
    TrustedLiveResultLimitError,
    TrustedLiveSourceChangedError,
    TrustedLiveUnsupportedSourceError,
)
from tests.datahandling.test_trusted_live import _assert_safe_audit
from tests.datahandling.test_trusted_plot import make_run, protected_state


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "scope.db"
    make_run(path)
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value INTEGER, pad TEXT)")
    writer.executemany("INSERT INTO probe VALUES (?, ?, ?)",
                       ((i, i * 3, "x" * 128) for i in range(8192)))
    writer.commit()
    try:
        yield path, writer
    finally:
        writer.close()


def test_page_reads_reuse_paths_but_keep_handle_proofs(source):
    path, _writer = source
    before = protected_state(path)
    with TrustedLiveReader.open(path) as reader:
        start = reader.audit().counters
        assert reader.query("SELECT COUNT(*), SUM(value) FROM probe").rows == (
            (8192, 3 * sum(range(8192))),)
        audit = reader.audit().counters
        reads = audit["source_read"] - start["source_read"]
        assert reads > 100
        assert audit["read_path_reused"] - start["read_path_reused"] == 2 * reads
        assert audit["identity_verified"] - start["identity_verified"] >= 2 * reads
        if os.name == "nt":
            assert audit["proof_open"] - start["proof_open"] < reads
        assert audit["read_validation_ns"] > start["read_validation_ns"]
        assert audit["read_io_ns"] > start["read_io_ns"]
        _assert_safe_audit(audit)
    assert protected_state(path) == before


@pytest.mark.parametrize("outcome", ["success", "cancel", "deadline", "limit", "sql"])
def test_scope_ends_on_every_operation_exit(source, monkeypatch, outcome):
    path, writer = source
    before = protected_state(path)
    with TrustedLiveReader.open(path) as reader:
        cancel = threading.Event()
        original = reader._query_spec_in_transaction

        def cancel_after_result(*args):
            result = original(*args)
            cancel.set()
            return result

        if outcome == "cancel":
            monkeypatch.setattr(reader, "_query_spec_in_transaction", cancel_after_result)
            with pytest.raises(TrustedLiveCancelledError):
                reader.query("SELECT SUM(value) FROM probe", cancel_event=cancel)
        elif outcome == "deadline":
            with pytest.raises(TrustedLiveDeadlineExceededError):
                reader.query("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 "
                             "FROM n WHERE x<100000000) SELECT SUM(x) FROM n", timeout=0.1)
        elif outcome == "limit":
            with pytest.raises(TrustedLiveResultLimitError):
                reader.query("SELECT zeroblob(5000000)")
        elif outcome == "sql":
            with pytest.raises(TrustedLiveQueryError):
                reader.query("SELECT missing_column FROM probe")
        else:
            reader.query("SELECT SUM(value) FROM probe")

        # Deliberately exercise a raw read outside the public operation scope.
        # The private connection remains protected by the native VFS and SQL
        # authorizer, but must perform full path checks for these page reads.
        assert not reader._connection.in_transaction
        reader._connection.release_memory()
        start = reader.audit().counters
        cursor = reader._connection.cursor()
        try:
            assert cursor.execute("SELECT COUNT(*) FROM probe").fetchone() == (8192,)
        finally:
            cursor.close()
        audit = reader.audit().counters
        assert audit["source_read"] > start["source_read"]
        assert audit["read_path_reused"] == start["read_path_reused"]
        _assert_safe_audit(audit)
        assert protected_state(path) == before
        # Only the test-owned writer checkpoints; qPlot has released its locks.
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)


def test_new_journal_after_materialisation_prevents_publication(source, monkeypatch):
    path, _writer = source
    before = protected_state(path)
    journal = path.with_name(path.name + "-journal")
    with TrustedLiveReader.open(path) as reader:
        original = reader._query_spec_in_transaction

        def introduce_journal(*args):
            result = original(*args)
            # Simulate an incompatible source transition in the disposable
            # fixture after reading succeeds, before the publication boundary.
            journal.write_bytes(bytes(512))
            return result

        monkeypatch.setattr(reader, "_query_spec_in_transaction", introduce_journal)
        try:
            with pytest.raises(TrustedLiveUnsupportedSourceError):
                reader.query("SELECT SUM(value) FROM probe")
            assert reader.closed
        finally:
            journal.unlink(missing_ok=True)
    assert protected_state(path) == before


@pytest.mark.skipif(os.name == "nt", reason="Open-file replacement requires POSIX")
@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_replacement_after_materialisation_is_rejected_before_publication(
        source, monkeypatch, suffix):
    path, _writer = source
    selected = path.with_name(path.name + suffix)
    parked = path.with_name("parked" + suffix)
    replacement = path.with_name("replacement" + suffix)
    with TrustedLiveReader.open(path) as reader:
        original = reader._query_spec_in_transaction

        def replace_after_result(*args):
            result = original(*args)
            shutil.copyfile(selected, replacement)
            os.rename(selected, parked)
            os.rename(replacement, selected)
            return result

        monkeypatch.setattr(reader, "_query_spec_in_transaction", replace_after_result)
        try:
            with pytest.raises(TrustedLiveSourceChangedError):
                reader.query("SELECT SUM(value) FROM probe")
            assert reader.closed
        finally:
            if parked.exists():
                os.replace(parked, selected)
