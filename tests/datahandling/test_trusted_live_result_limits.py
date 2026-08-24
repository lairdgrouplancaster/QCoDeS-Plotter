"""Live-cursor result-limit regressions for the trusted reader boundary."""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from pathlib import Path

import apsw
import pytest

from qplot.datahandling import trusted_live as trusted_live_module
from qplot.datahandling.trusted_live import (
    TRUSTED_LIVE_MAX_CELLS_PER_REPLY,
    TRUSTED_LIVE_MAX_ROWS_PER_RESULT,
    TRUSTED_LIVE_MAX_SCALAR_BYTES,
    TRUSTED_LIVE_MAX_TRANSIENT_PYTHON_ROW_BYTES,
    TRUSTED_LIVE_MAX_TRANSIENT_RAW_ROW_BYTES,
    TrustedLiveCleanupError,
    TrustedLiveReader,
    TrustedLiveResultLimitError,
    TrustedLiveSourceChangedError,
    TrustedQuery,
)
from qplot.datahandling.trusted_live_supervisor import TrustedLiveReaderSupervisor
from tests.datahandling.test_trusted_live_supervisor import (
    _ApswWalWriter,
    _assert_helper_stopped,
    _protected_artifact_contents,
)

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def wal_writer(tmp_path: Path) -> Iterator[_ApswWalWriter]:
    writer = _ApswWalWriter.start(tmp_path / "trusted-result-limits.db")
    try:
        assert writer.request("barrier") == 1
        assert Path(f"{writer.database_path}-wal").is_file()
        assert Path(f"{writer.database_path}-shm").is_file()
        yield writer
    finally:
        writer.close()


def _commit_before_rejection(writer: _ApswWalWriter) -> None:
    writer.request("commit", value="before-result-limit", payload=b"x")


def _assert_reader_reusable_and_checkpointable(
    reader: TrustedLiveReader,
    writer: _ApswWalWriter,
) -> None:
    assert reader.query("SELECT 7 AS value", timeout=5.0).rows == ((7,),)
    writer.request(
        "commit",
        value=f"after-result-limit-{time.monotonic_ns()}",
        payload=b"writer-progress",
    )
    assert writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
    audit = reader.audit().counters
    assert {
        name: audit[name]
        for name in {
            "source_open_readwrite",
            "source_open_create",
            "source_write",
            "source_truncate",
            "source_sync",
            "source_delete",
            "source_fetch",
            "source_writable_map",
        }
    } == {
        "source_open_readwrite": 0,
        "source_open_create": 0,
        "source_write": 0,
        "source_truncate": 0,
        "source_sync": 0,
        "source_delete": 0,
        "source_fetch": 0,
        "source_writable_map": 0,
    }


def _recursive_rows_sql(projection: str) -> str:
    return (
        "WITH RECURSIVE rows_(n) AS ("
        "VALUES(1) UNION ALL SELECT n + 1 FROM rows_ WHERE n < ?"
        f") SELECT {projection} FROM rows_"
    )


def _wide_zeroblob_sql(column_count: int, *, materialized: bool = False) -> str:
    projection = ", ".join(
        f"zeroblob(?) AS payload_{index}" for index in range(column_count)
    )
    if not materialized:
        return f"SELECT {projection}"
    return f"WITH packed AS MATERIALIZED (SELECT {projection}) SELECT * FROM packed"


def _small_width_sql(column_count: int) -> str:
    values = ("NULL", "7", "3.25", "'qplot'")
    return "SELECT " + ", ".join(
        f"{values[index % len(values)]} AS value_{index}"
        for index in range(column_count)
    )


def _one_blob_result_wire_bytes(blob_bytes: int, column: str = "payload") -> int:
    base64_bytes = 4 * ((blob_bytes + 2) // 3)
    return (
        512  # Reply envelope reserve.
        + 32  # Results collection.
        + 32  # One result.
        + 16
        + len(column)
        + 2  # JSON quotes around the ASCII column name.
        + 32
        + 16  # One row and one field.
        + 16
        + base64_bytes  # Tagged blob scalar.
    )


def test_sqlite_scalar_limit_rejects_before_materialisation_and_restores(
    wal_writer: _ApswWalWriter,
) -> None:
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        connection = reader._connection
        assert connection is not None
        original_length_limit = connection.limit(apsw.SQLITE_LIMIT_LENGTH)

        boundary = reader.query(
            "SELECT zeroblob(?) AS payload",
            (TRUSTED_LIVE_MAX_SCALAR_BYTES,),
            timeout=15.0,
        )
        assert len(boundary.rows[0][0]) == TRUSTED_LIVE_MAX_SCALAR_BYTES
        assert connection.limit(apsw.SQLITE_LIMIT_LENGTH) == original_length_limit
        del boundary

        _commit_before_rejection(wal_writer)
        started = time.monotonic()
        with pytest.raises(TrustedLiveResultLimitError, match="runtime length limit"):
            reader.query(
                "SELECT zeroblob(?) AS payload",
                (TRUSTED_LIVE_MAX_SCALAR_BYTES + 1,),
                timeout=10.0,
            )
        assert time.monotonic() - started < 10.0
        assert connection.limit(apsw.SQLITE_LIMIT_LENGTH) == original_length_limit
        _assert_reader_reusable_and_checkpointable(reader, wal_writer)


def test_wide_four_mebibyte_values_reject_before_apsw_yields_row(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        connection = reader._connection
        assert connection is not None
        original_length_limit = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
        _commit_before_rejection(wal_writer)
        protected_before = _protected_artifact_contents(wal_writer.database_path)
        retain_calls = 0
        real_retain_row = trusted_live_module._TrustedResultBudget.retain_row

        def record_retain_row(*args: object, **kwargs: object) -> tuple[object, ...]:
            nonlocal retain_calls
            retain_calls += 1
            return real_retain_row(*args, **kwargs)

        monkeypatch.setattr(
            trusted_live_module._TrustedResultBudget,
            "retain_row",
            record_retain_row,
        )
        with pytest.raises(TrustedLiveResultLimitError) as caught:
            reader.query(
                _wide_zeroblob_sql(9),
                (TRUSTED_LIVE_MAX_SCALAR_BYTES,) * 9,
                timeout=10.0,
            )

        assert retain_calls == 0
        assert isinstance(caught.value.__cause__, apsw.TooBigError)
        assert connection.limit(apsw.SQLITE_LIMIT_LENGTH) == original_length_limit
        assert (
            _protected_artifact_contents(wal_writer.database_path) == protected_before
        )
        _assert_reader_reusable_and_checkpointable(reader, wal_writer)


def test_packed_row_limit_rejects_aggregate_before_retain_row(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        connection = reader._connection
        assert connection is not None
        original_length_limit = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
        monkeypatch.setattr(
            trusted_live_module,
            "TRUSTED_LIVE_MAX_TRANSIENT_RAW_ROW_BYTES",
            4_096,
        )
        retained_limits: list[int] = []
        real_retain_row = trusted_live_module._TrustedResultBudget.retain_row

        def record_retain_row(*args: object, **kwargs: object) -> tuple[object, ...]:
            retained_limits.append(connection.limit(apsw.SQLITE_LIMIT_LENGTH))
            return real_retain_row(*args, **kwargs)

        monkeypatch.setattr(
            trusted_live_module._TrustedResultBudget,
            "retain_row",
            record_retain_row,
        )
        valid = reader.query(
            "WITH packed AS MATERIALIZED ("
            "SELECT zeroblob(64) AS blob_value, "
            "printf('%.*c', 64, 'x') AS text_value, 7 AS integer_value, "
            "NULL AS null_value) SELECT * FROM packed",
            timeout=5.0,
        )
        assert valid.rows == ((b"\x00" * 64, "x" * 64, 7, None),)
        assert retained_limits == [1_024]
        retained_limits.clear()

        _commit_before_rejection(wal_writer)
        protected_before = _protected_artifact_contents(wal_writer.database_path)
        with pytest.raises(TrustedLiveResultLimitError) as caught:
            reader.query(
                _wide_zeroblob_sql(4, materialized=True),
                (512,) * 4,
                timeout=5.0,
            )
        assert retained_limits == []
        assert isinstance(caught.value.__cause__, apsw.TooBigError)
        assert connection.limit(apsw.SQLITE_LIMIT_LENGTH) == original_length_limit
        assert (
            _protected_artifact_contents(wal_writer.database_path) == protected_before
        )
        _assert_reader_reusable_and_checkpointable(reader, wal_writer)


def test_one_byte_over_width_derived_limit_rejects_cleanly(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        connection = reader._connection
        assert connection is not None
        original_length_limit = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
        monkeypatch.setattr(
            trusted_live_module,
            "TRUSTED_LIVE_MAX_TRANSIENT_RAW_ROW_BYTES",
            12 * 1_024,
        )
        exact = reader.query(
            "SELECT zeroblob(?) AS payload, NULL AS a, NULL AS b",
            (4_096,),
            timeout=5.0,
        )
        assert len(exact.rows[0][0]) == 4_096
        del exact

        _commit_before_rejection(wal_writer)
        protected_before = _protected_artifact_contents(wal_writer.database_path)
        with pytest.raises(TrustedLiveResultLimitError) as caught:
            reader.query(
                "SELECT zeroblob(?) AS payload, NULL AS a, NULL AS b",
                (4_097,),
                timeout=5.0,
            )
        assert isinstance(caught.value.__cause__, apsw.TooBigError)
        assert connection.limit(apsw.SQLITE_LIMIT_LENGTH) == original_length_limit
        assert (
            _protected_artifact_contents(wal_writer.database_path) == protected_before
        )
        _assert_reader_reusable_and_checkpointable(reader, wal_writer)


def test_many_small_mixed_columns_remain_supported(
    wal_writer: _ApswWalWriter,
) -> None:
    column_count = 512
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        result = reader.query(_small_width_sql(column_count), timeout=10.0)
        assert len(result.columns) == column_count
        assert len(result.rows[0]) == column_count
        assert result.rows[0][:8] == (
            None,
            7,
            3.25,
            "qplot",
            None,
            7,
            3.25,
            "qplot",
        )


def test_astral_text_row_respects_logical_python_object_envelope(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    column_count = 1_000
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        connection = reader._connection
        assert connection is not None
        original_length_limit = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
        operation_baseline = min(
            original_length_limit,
            TRUSTED_LIVE_MAX_SCALAR_BYTES,
        )
        effective = trusted_live_module._trusted_statement_length_limit(
            operation_baseline,
            column_count,
        )
        installed: list[tuple[int, int]] = []
        real_install = TrustedLiveReader._install_statement_result_length_limit

        def record_install(
            active_reader: TrustedLiveReader,
            active_connection: apsw.Connection,
            control: object,
            active_column_count: int,
        ) -> trusted_live_module._TrustedSqliteLengthLimit:
            result = real_install(
                active_reader,
                active_connection,
                control,
                active_column_count,
            )
            installed.append((active_column_count, result.effective))
            return result

        monkeypatch.setattr(
            TrustedLiveReader,
            "_install_statement_result_length_limit",
            record_install,
        )

        # One astral character forces PEP 393's four-byte representation while
        # the ASCII portion keeps each value's UTF-8 payload exactly at the
        # current width-derived SQLite limit.  Every prefix is distinct so all
        # 1,000 standard APSW scalar objects coexist in the yielded row.
        values = tuple(
            f"{index:04d}" + ("a" * (effective - 8)) + "\U0001f680"
            for index in range(column_count)
        )
        sql = "SELECT " + ", ".join(
            f"? AS value_{index}" for index in range(column_count)
        )

        assert connection.row_trace is None
        assert connection.convert_jsonb is None
        result = reader.query(sql, values, timeout=30.0)
        assert connection.row_trace is None
        assert connection.convert_jsonb is None
        assert installed == [(column_count, effective)]
        assert connection.limit(apsw.SQLITE_LIMIT_LENGTH) == original_length_limit

        row = result.rows[0]
        assert len(row) == column_count
        assert len({id(value) for value in row}) == column_count
        assert all(isinstance(value, str) for value in row)
        assert all(len(value.encode("utf-8")) == effective for value in row)
        assert all(sys.getsizeof(value) >= 4 * len(value) for value in row)

        utf8_payload_size = sum(len(value.encode("utf-8")) for value in row)
        logical_python_size = sys.getsizeof(row) + sum(
            sys.getsizeof(value) for value in row
        )
        assert utf8_payload_size <= TRUSTED_LIVE_MAX_TRANSIENT_RAW_ROW_BYTES
        assert logical_python_size <= TRUSTED_LIVE_MAX_TRANSIENT_PYTHON_ROW_BYTES


def test_statement_limits_recompute_and_restore_across_query_batch(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        connection = reader._connection
        assert connection is not None
        original_length_limit = connection.limit(apsw.SQLITE_LIMIT_LENGTH)
        installed: list[tuple[int, int]] = []
        restored: list[int] = []
        metadata_limits: list[int] = []
        real_install = TrustedLiveReader._install_statement_result_length_limit
        real_restore = TrustedLiveReader._restore_statement_result_length_limit
        real_query_info = apsw.ext.query_info

        def record_install(
            active_reader: TrustedLiveReader,
            active_connection: apsw.Connection,
            control: object,
            column_count: int,
        ) -> trusted_live_module._TrustedSqliteLengthLimit:
            result = real_install(
                active_reader,
                active_connection,
                control,
                column_count,
            )
            installed.append((column_count, result.effective))
            return result

        def record_restore(
            active_reader: TrustedLiveReader,
            active_connection: apsw.Connection,
            control: object,
            limit: trusted_live_module._TrustedSqliteLengthLimit,
        ) -> TrustedLiveCleanupError | None:
            error = real_restore(active_reader, active_connection, control, limit)
            restored.append(active_connection.limit(apsw.SQLITE_LIMIT_LENGTH))
            return error

        def record_query_info(*args: object, **kwargs: object) -> object:
            metadata_limits.append(connection.limit(apsw.SQLITE_LIMIT_LENGTH))
            return real_query_info(*args, **kwargs)

        monkeypatch.setattr(
            TrustedLiveReader,
            "_install_statement_result_length_limit",
            record_install,
        )
        monkeypatch.setattr(
            TrustedLiveReader,
            "_restore_statement_result_length_limit",
            record_restore,
        )
        monkeypatch.setattr(apsw.ext, "query_info", record_query_info)
        queries = tuple(TrustedQuery(_small_width_sql(width)) for width in (1, 4, 2, 9))
        results = reader.query_batch(queries, timeout=10.0)

        operation_baseline = min(
            original_length_limit,
            TRUSTED_LIVE_MAX_SCALAR_BYTES,
        )
        assert [len(result.columns) for result in results] == [1, 4, 2, 9]
        assert installed == [
            (
                width,
                trusted_live_module._trusted_statement_length_limit(
                    operation_baseline,
                    width,
                ),
            )
            for width in (1, 4, 2, 9)
        ]
        assert metadata_limits == [operation_baseline] * 4
        assert restored == [operation_baseline] * 4
        assert installed[0][1] == TRUSTED_LIVE_MAX_SCALAR_BYTES
        assert installed[0][1] > installed[2][1] > installed[1][1] > installed[3][1]
        assert connection.limit(apsw.SQLITE_LIMIT_LENGTH) == original_length_limit

        for width, effective in installed:
            assert width * effective <= TRUSTED_LIVE_MAX_TRANSIENT_RAW_ROW_BYTES
            assert (
                trusted_live_module._TRUSTED_LIVE_PYTHON_ROW_FIXED_BYTES
                + trusted_live_module._TRUSTED_LIVE_PYTHON_FIXED_BYTES_PER_COLUMN
                * width
                + trusted_live_module._TRUSTED_LIVE_PYTHON_TEXT_EXPANSION
                * width
                * effective
                <= TRUSTED_LIVE_MAX_TRANSIENT_PYTHON_ROW_BYTES
            )


def test_result_limit_cannot_mask_cleanup_identity_failure(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        real_validate = reader._validate_native_source
        validation_calls = 0

        def fail_during_cleanup(_reader: TrustedLiveReader) -> None:
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls == 2:
                raise TrustedLiveSourceChangedError(
                    "Injected source-identity failure during result-limit cleanup."
                )
            real_validate()

        monkeypatch.setattr(
            TrustedLiveReader,
            "_validate_native_source",
            fail_during_cleanup,
        )
        with pytest.raises(
            TrustedLiveSourceChangedError,
            match="source-identity failure",
        ) as caught:
            reader.query(
                "SELECT zeroblob(?) AS payload",
                (TRUSTED_LIVE_MAX_SCALAR_BYTES + 1,),
                timeout=10.0,
            )

        assert validation_calls == 2
        assert isinstance(caught.value.__cause__, TrustedLiveResultLimitError)
        assert reader.closed
        assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


def test_more_than_maximum_rows_is_rejected_while_iterating(
    wal_writer: _ApswWalWriter,
) -> None:
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        _commit_before_rejection(wal_writer)
        started = time.monotonic()
        with pytest.raises(TrustedLiveResultLimitError, match="rows"):
            reader.query(
                _recursive_rows_sql("NULL AS value"),
                (TRUSTED_LIVE_MAX_ROWS_PER_RESULT + 1,),
                timeout=30.0,
            )
        assert time.monotonic() - started < 30.0
        _assert_reader_reusable_and_checkpointable(reader, wal_writer)


def test_exact_row_and_aggregate_cell_boundaries_succeed(
    wal_writer: _ApswWalWriter,
) -> None:
    assert TRUSTED_LIVE_MAX_CELLS_PER_REPLY == (TRUSTED_LIVE_MAX_ROWS_PER_RESULT * 4)
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        operation_timeout_seconds=45.0,
        reply_timeout_seconds=60.0,
    )
    helper_pid = supervisor.helper_pid
    try:
        started = time.monotonic()
        result = supervisor.query(
            _recursive_rows_sql("NULL AS a, NULL AS b, NULL AS c, NULL AS d"),
            (TRUSTED_LIVE_MAX_ROWS_PER_RESULT,),
            timeout=45.0,
            wait_timeout=60.0,
        )
        assert time.monotonic() - started < 60.0
        assert len(result.rows) == TRUSTED_LIVE_MAX_ROWS_PER_RESULT
        assert result.rows[0] == result.rows[-1] == (None, None, None, None)
        assert supervisor.helper_pid == helper_pid
        assert supervisor.query("SELECT 8", timeout=5.0).rows == ((8,),)
        assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
    finally:
        supervisor.close()
    assert not supervisor.helper_alive


def test_aggregate_cells_are_shared_across_query_batch(
    wal_writer: _ApswWalWriter,
) -> None:
    query = TrustedQuery(
        _recursive_rows_sql("NULL AS a, NULL AS b, NULL AS c, NULL AS d, NULL AS e"),
        (100_001,),
    )
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        _commit_before_rejection(wal_writer)
        started = time.monotonic()
        with pytest.raises(TrustedLiveResultLimitError, match="result cells"):
            reader.query_batch((query, query), timeout=30.0)
        assert time.monotonic() - started < 30.0
        _assert_reader_reusable_and_checkpointable(reader, wal_writer)


def test_aggregate_wire_budget_rejects_across_live_rows(
    wal_writer: _ApswWalWriter,
) -> None:
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        _commit_before_rejection(wal_writer)
        started = time.monotonic()
        with pytest.raises(TrustedLiveResultLimitError, match="wire budget"):
            reader.query(
                _recursive_rows_sql("zeroblob(900000) AS payload"),
                (28,),
                timeout=30.0,
            )
        assert time.monotonic() - started < 30.0
        _assert_reader_reusable_and_checkpointable(reader, wal_writer)


def test_aggregate_wire_budget_is_shared_across_query_batch(
    wal_writer: _ApswWalWriter,
) -> None:
    query = TrustedQuery(
        _recursive_rows_sql("zeroblob(900000) AS payload"),
        (14,),
    )
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        _commit_before_rejection(wal_writer)
        started = time.monotonic()
        with pytest.raises(TrustedLiveResultLimitError, match="wire budget"):
            reader.query_batch((query, query), timeout=30.0)
        assert time.monotonic() - started < 30.0
        _assert_reader_reusable_and_checkpointable(reader, wal_writer)


def test_exact_conservative_wire_boundary_succeeds_then_next_byte_rejects(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blob_bytes = 1_024
    exact_wire_bytes = _one_blob_result_wire_bytes(blob_bytes)
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        monkeypatch.setattr(
            trusted_live_module,
            "TRUSTED_LIVE_MAX_REPLY_BYTES",
            exact_wire_bytes,
        )
        result = reader.query(
            "SELECT zeroblob(?) AS payload",
            (blob_bytes,),
            timeout=5.0,
        )
        assert len(result.rows[0][0]) == blob_bytes

        monkeypatch.setattr(
            trusted_live_module,
            "TRUSTED_LIVE_MAX_REPLY_BYTES",
            exact_wire_bytes - 1,
        )
        _commit_before_rejection(wal_writer)
        with pytest.raises(TrustedLiveResultLimitError, match="wire budget"):
            reader.query(
                "SELECT zeroblob(?) AS payload",
                (blob_bytes,),
                timeout=5.0,
            )
        _assert_reader_reusable_and_checkpointable(reader, wal_writer)


def test_live_column_limit_rejects_before_rows_are_retained(
    wal_writer: _ApswWalWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TrustedLiveReader.open(wal_writer.database_path) as reader:
        monkeypatch.setattr(
            trusted_live_module,
            "TRUSTED_LIVE_MAX_COLUMNS_PER_RESULT",
            1,
        )
        assert reader.query("SELECT 1 AS one", timeout=5.0).rows == ((1,),)
        _commit_before_rejection(wal_writer)
        with pytest.raises(TrustedLiveResultLimitError, match="columns"):
            reader.query(
                "SELECT 1 AS one, abs(-9223372036854775808) AS two",
                timeout=5.0,
            )
        _assert_reader_reusable_and_checkpointable(reader, wal_writer)


@pytest.mark.parametrize(
    "fault",
    [
        "statement_limit_install",
        "statement_limit_verify",
        "statement_limit_restore",
    ],
)
def test_statement_limit_uncertainty_retires_direct_reader(
    wal_writer: _ApswWalWriter,
    fault: str,
) -> None:
    _commit_before_rejection(wal_writer)
    reader = TrustedLiveReader.open(
        wal_writer.database_path,
        _test_statement_limit_fault=fault,
    )
    protected_before = _protected_artifact_contents(wal_writer.database_path)
    try:
        with pytest.raises(TrustedLiveCleanupError, match="reader connection"):
            reader.query_batch(
                (
                    TrustedQuery("SELECT 1 AS first_value"),
                    TrustedQuery("SELECT 2 AS unpublished_value"),
                ),
                timeout=5.0,
            )
        assert reader.closed
        assert (
            _protected_artifact_contents(wal_writer.database_path) == protected_before
        )
        audit = reader.audit().counters
        assert audit["source_write"] == 0
        assert audit["source_truncate"] == 0
        assert audit["source_delete"] == 0
    finally:
        reader.close()

    with TrustedLiveReader.open(wal_writer.database_path) as replacement:
        assert replacement.query("SELECT 12", timeout=5.0).rows == ((12,),)
        _assert_reader_reusable_and_checkpointable(replacement, wal_writer)


def test_statement_limit_cleanup_overrides_reusable_result_limit(
    wal_writer: _ApswWalWriter,
) -> None:
    _commit_before_rejection(wal_writer)
    reader = TrustedLiveReader.open(
        wal_writer.database_path,
        _test_statement_limit_fault="statement_limit_restore",
    )
    try:
        with pytest.raises(TrustedLiveCleanupError) as caught:
            reader.query(
                _wide_zeroblob_sql(9),
                (TRUSTED_LIVE_MAX_SCALAR_BYTES,) * 9,
                timeout=10.0,
            )
        assert isinstance(caught.value.__cause__, TrustedLiveResultLimitError)
        assert isinstance(caught.value.__cause__.__cause__, apsw.TooBigError)
        assert reader.closed
    finally:
        reader.close()
    assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)


@pytest.mark.parametrize(
    "fault",
    [
        "statement_limit_install",
        "statement_limit_verify",
        "statement_limit_restore",
    ],
)
def test_statement_limit_uncertainty_retires_helper_incarnation(
    wal_writer: _ApswWalWriter,
    fault: str,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        _test_fault=fault,
    )
    replacement_pid: int | None = None
    try:
        first_pid = supervisor.helper_pid
        first_incarnation = supervisor.incarnation
        assert first_pid is not None
        _commit_before_rejection(wal_writer)
        protected_before = _protected_artifact_contents(wal_writer.database_path)
        with pytest.raises(TrustedLiveCleanupError):
            supervisor.query_batch(
                (
                    TrustedQuery("SELECT 1 AS first_value"),
                    TrustedQuery("SELECT 2 AS unpublished_value"),
                ),
                timeout=5.0,
            )
        _assert_helper_stopped(supervisor, first_pid)
        assert (
            _protected_artifact_contents(wal_writer.database_path) == protected_before
        )

        assert supervisor.query("SELECT 13", timeout=5.0).rows == ((13,),)
        replacement_pid = supervisor.helper_pid
        assert replacement_pid is not None
        assert replacement_pid != first_pid
        assert supervisor.incarnation != first_incarnation
        wal_writer.request(
            "commit",
            value=f"after-helper-retirement-{fault}",
            payload=b"writer-progress",
        )
        assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
    finally:
        supervisor.close()
    assert replacement_pid is not None
    _assert_helper_stopped(supervisor, replacement_pid)


def test_helper_reuses_clean_incarnation_after_live_result_limit(
    wal_writer: _ApswWalWriter,
) -> None:
    supervisor = TrustedLiveReaderSupervisor.open(
        wal_writer.database_path,
        startup_timeout_seconds=10.0,
        shutdown_timeout_seconds=5.0,
    )
    helper_pid = supervisor.helper_pid
    try:
        assert helper_pid is not None
        _commit_before_rejection(wal_writer)
        protected_before = _protected_artifact_contents(wal_writer.database_path)
        started = time.monotonic()
        with pytest.raises(TrustedLiveResultLimitError, match="runtime length limit"):
            supervisor.query(
                _wide_zeroblob_sql(9),
                (TRUSTED_LIVE_MAX_SCALAR_BYTES,) * 9,
                timeout=10.0,
            )
        assert time.monotonic() - started < 10.0
        assert supervisor.helper_alive
        assert supervisor.helper_pid == helper_pid
        assert supervisor.query("SELECT 9", timeout=5.0).rows == ((9,),)
        assert (
            _protected_artifact_contents(wal_writer.database_path) == protected_before
        )
        wal_writer.request(
            "commit",
            value="after-clean-wide-helper-rejection",
            payload=b"writer-progress",
        )
        assert wal_writer.request("checkpoint", mode="TRUNCATE") == (0, 0, 0)
    finally:
        supervisor.close()
    assert not supervisor.helper_alive
    assert supervisor.helper_pid is None
