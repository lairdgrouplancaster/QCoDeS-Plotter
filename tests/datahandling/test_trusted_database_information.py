"""Database information uses only bounded reads through the pinned VFS."""

import pytest

from qplot.datahandling.trusted_live import (
    TrustedLiveReader,
    TrustedLiveSqlRejectedError,
)
from qplot.datahandling.trusted_live_queries import TrustedMetadataQueryAdapter
from tests.datahandling.test_trusted_live import (
    _artifact_state,
    _assert_protected_artifacts_unchanged,
    _assert_safe_audit,
)
from tests.datahandling.test_trusted_live import (
    live_writer as live_writer,
)


def test_metadata_pragmas_read_only_and_fixed_plan(live_writer):
    live_writer.request("barrier")
    before = _artifact_state(live_writer.database_path)
    reader = TrustedLiveReader.open(live_writer.database_path)
    try:
        for name in ("user_version", "application_id", "page_count", "page_size"):
            result = reader.query(f"PRAGMA main.{name}")
            assert result.columns == (name,)
            assert len(result.rows) == 1
            assert type(result.rows[0][0]) is int
            for assignment in (f"PRAGMA main.{name}=1", f"PRAGMA main.{name}(1)"):
                with pytest.raises(TrustedLiveSqlRejectedError):
                    reader.query(assignment)
        for sql in (
            "PRAGMA temp.user_version", "PRAGMA journal_mode",
            "PRAGMA journal_mode=DELETE", "PRAGMA wal_checkpoint",
            "PRAGMA wal_checkpoint(TRUNCATE)", "PRAGMA writable_schema=ON",
        ):
            with pytest.raises(TrustedLiveSqlRejectedError):
                reader.query(sql)

        info = TrustedMetadataQueryAdapter(reader, live_writer.database_path).database_info()
        assert info.user_version == live_writer.startup["user_version"]
        assert info.run_count == info.experiment_count == 1
        assert info.page_count > 0 and info.page_size > 0
        assert info.latest_run.run_id == 1
        assert not info.latest_run.as_dict()["is_completed"]
        _assert_safe_audit(reader.audit().counters)
    finally:
        reader.close()
    _assert_safe_audit(reader.audit().counters)
    _assert_protected_artifacts_unchanged(before, _artifact_state(live_writer.database_path))
