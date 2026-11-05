"""Cloud hydration runs without the GUI or a writable database handle."""

import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qplot.datahandling import _cloud_prefetch, cloud_files


def test_prefetch_subprocess_accepts_leading_dash_filename(tmp_path, monkeypatch):
    database = tmp_path / "-measurement.db"
    contents = b"existing measurement data"
    database.write_bytes(contents)
    original_mtime = database.stat().st_mtime_ns
    monkeypatch.chdir(tmp_path)

    bytes_read = cloud_files.prefetch_database_file_with_timeout(database.name, timeout=5)

    assert bytes_read == len(contents)
    assert database.read_bytes() == contents
    assert database.stat().st_mtime_ns == original_mtime


class CloudDatabasePrefetchTestCase(unittest.TestCase):
    @patch.object(
        cloud_files, "database_cloud_storage_label", return_value="OneDrive"
    )
    @patch.object(cloud_files.os, "stat")
    def test_local_windows_cloud_file_is_not_treated_as_placeholder(
        self,
        stat_file,
        _cloud_label,
    ):
        stat_file.return_value = type(
            "FileInfo",
            (),
            {"st_size": 4096, "st_file_attributes": 0x20},
        )()

        self.assertFalse(
            cloud_files.database_is_likely_cloud_placeholder("OneDrive/local.db")
        )

    @patch.object(
        cloud_files, "database_cloud_storage_label", return_value="OneDrive"
    )
    @patch.object(cloud_files.os, "stat")
    def test_offline_windows_cloud_file_is_treated_as_placeholder(
        self,
        stat_file,
        _cloud_label,
    ):
        stat_file.return_value = type(
            "FileInfo",
            (),
            {
                "st_size": 4096,
                "st_file_attributes": cloud_files.WINDOWS_CLOUD_PLACEHOLDER_ATTRIBUTES,
            },
        )()

        self.assertTrue(
            cloud_files.database_is_likely_cloud_placeholder("OneDrive/offline.db")
        )

    @patch.object(
        cloud_files, "database_cloud_storage_label", return_value="OneDrive"
    )
    @patch.object(cloud_files.os, "stat")
    def test_cloud_file_without_allocation_metadata_is_not_assumed_placeholder(
        self,
        stat_file,
        _cloud_label,
    ):
        stat_file.return_value = type("FileInfo", (), {"st_size": 4096})()

        self.assertFalse(
            cloud_files.database_is_likely_cloud_placeholder("OneDrive/unknown.db")
        )

    @patch.object(
        cloud_files, "database_cloud_storage_label", return_value="OneDrive"
    )
    @patch.object(cloud_files.os, "stat")
    def test_sparse_cloud_file_uses_posix_allocation_metadata(
        self,
        stat_file,
        _cloud_label,
    ):
        stat_file.return_value = type(
            "FileInfo",
            (),
            {"st_size": 4096, "st_blocks": 0},
        )()

        self.assertTrue(
            cloud_files.database_is_likely_cloud_placeholder("OneDrive/sparse.db")
        )

    def test_prefetch_subprocess_retries_transient_timeout_errors(self):
        class Handle:
            def __init__(self):
                self.read_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self, _chunk_size):
                self.read_calls += 1
                if self.read_calls == 1:
                    raise OSError(errno.ECANCELED, "Operation canceled")
                if self.read_calls == 2:
                    return b"database"
                return b""

        handle = Handle()
        output = []
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "placeholder.db")
            Path(database_path).write_bytes(b"database")
            with (
                patch(
                    "builtins.open",
                    side_effect=[
                        TimeoutError(errno.ETIMEDOUT, "Operation timed out"),
                        handle,
                    ],
                ) as open_file,
                patch(
                    "builtins.print",
                    side_effect=lambda value, **_kwargs: output.append(value),
                ),
                patch("time.sleep"),
            ):
                _cloud_prefetch.main([database_path])

        self.assertEqual(open_file.call_count, 2)
        self.assertEqual(handle.read_calls, 3)
        self.assertEqual(output[-1], 8)

    def test_prefetch_database_file_reads_file_and_reports_cloud_sync_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "prefetch.db")
            Path(database_path).write_bytes(b"x" * 10)
            statuses = []
            old_label = cloud_files.database_cloud_storage_label
            cloud_files.database_cloud_storage_label = lambda _path: "OneDrive"
            try:
                bytes_read = cloud_files.prefetch_database_file(
                    database_path,
                    status_callback=statuses.append,
                    chunk_size=4,
                    status_interval=0,
                )
            finally:
                cloud_files.database_cloud_storage_label = old_label

        self.assertEqual(bytes_read, 10)
        self.assertTrue(statuses[0].startswith("Waiting for OneDrive sync..."))
        self.assertIn("100% available", statuses[-1])

    def test_prefetch_database_file_with_timeout_uses_subprocess(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "prefetch-timeout.db")
            Path(database_path).write_bytes(b"x" * 10)
            statuses = []
            old_label = cloud_files.database_cloud_storage_label
            cloud_files.database_cloud_storage_label = lambda _path: "OneDrive"
            try:
                bytes_read = cloud_files.prefetch_database_file_with_timeout(
                    database_path,
                    timeout=5,
                    status_callback=statuses.append,
                )
            finally:
                cloud_files.database_cloud_storage_label = old_label

        self.assertEqual(bytes_read, 10)
        self.assertIn("Waiting for OneDrive sync...", statuses)
        self.assertIn("Waiting for OneDrive sync... 100% available", statuses)

    def test_prefetch_database_file_with_timeout_kills_stalled_process(self):
        old_popen = cloud_files.subprocess.Popen
        killed = []

        class Pipe:
            def __iter__(self):
                return iter(())

            def close(self):
                pass

        class Process:
            stdout = Pipe()
            stderr = Pipe()
            returncode = None

            def poll(self):
                return None

            def kill(self):
                killed.append(True)

            def wait(self):
                self.returncode = -9

        cloud_files.subprocess.Popen = lambda *args, **kwargs: Process()
        try:
            with self.assertRaises(TimeoutError) as caught:
                cloud_files.prefetch_database_file_with_timeout(
                    "OneDrive/test.db",
                    timeout=0.01,
                    status_callback=lambda _message: None,
                )
        finally:
            cloud_files.subprocess.Popen = old_popen

        self.assertEqual(killed, [True])
        self.assertIn("Timed out after 0.01 s", str(caught.exception))
        self.assertIn("OneDrive", str(caught.exception))

    def test_prefetch_database_file_with_timeout_stops_when_cancelled(self):
        old_popen = cloud_files.subprocess.Popen
        killed = []

        class Pipe:
            def __iter__(self):
                return iter(())

            def close(self):
                pass

        class Process:
            stdout = Pipe()
            stderr = Pipe()
            returncode = None

            def poll(self):
                return None

            def kill(self):
                killed.append(True)

            def wait(self):
                self.returncode = -9

        cloud_files.subprocess.Popen = lambda *args, **kwargs: Process()
        try:
            with self.assertRaises(InterruptedError):
                cloud_files.prefetch_database_file_with_timeout(
                    "OneDrive/test.db",
                    timeout=5,
                    cancelled_callback=lambda: True,
                )
        finally:
            cloud_files.subprocess.Popen = old_popen

        self.assertEqual(killed, [True])
