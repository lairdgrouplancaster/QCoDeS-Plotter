import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from qplot.datahandling import readSQL


class RunSizeTestCase(unittest.TestCase):
    def _read_only_sqlite_connection(self, database_path):
        return sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)

    def _create_completed_status_database(self, database_path, row_count=4):
        conn = sqlite3.connect(database_path)
        try:
            conn.execute(
                """
                CREATE TABLE runs (
                    guid TEXT,
                    completed_timestamp REAL,
                    is_completed INTEGER,
                    result_table_name TEXT,
                    run_description TEXT,
                    parameters TEXT
                )
                """
                )
            conn.execute("CREATE TABLE results_1 (x REAL, signal REAL)")
            conn.executemany(
                "INSERT INTO results_1 VALUES (?, ?)",
                ((index, index + 1) for index in range(row_count)),
                )
            run_description = json.dumps({
                "interdependencies_": {
                    "dependencies": {"signal": ["x"]},
                    },
                "shapes": {"signal": [row_count]},
                })
            conn.execute(
                "INSERT INTO runs VALUES (?, 123, 1, ?, ?, ?)",
                ("completed-guid", "results_1", run_description, "x,signal"),
                )
            conn.commit()
        finally:
            conn.close()

    def test_cancel_progress_handler_interrupts_a_running_sql_statement(self):
        conn = sqlite3.connect(":memory:")
        callback_count = 0

        def cancelled():
            nonlocal callback_count
            callback_count += 1
            return callback_count > 5

        try:
            readSQL._install_cancel_progress_handler(conn, cancelled)
            with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
                conn.execute(
                    "WITH RECURSIVE values_(n) AS ("
                    "SELECT 1 UNION ALL SELECT n + 1 FROM values_ WHERE n < 10000000"
                    ") SELECT SUM(n) FROM values_"
                    ).fetchone()
        finally:
            conn.close()

        self.assertGreater(callback_count, 5)

    def test_cancel_progress_handler_rejects_already_cancelled_read(self):
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaisesRegex(InterruptedError, "cancelled"):
                readSQL._install_cancel_progress_handler(conn, lambda: True)
        finally:
            conn.close()

    def test_completed_status_excludes_every_storage_query_when_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "completed.db")
            self._create_completed_status_database(database_path)
            statements = []

            def trace_connection(connection):
                if connection is not None:
                    connection.set_trace_callback(statements.append)

            with (
                patch.object(
                    readSQL,
                    "qcodes_read_only_connection",
                    side_effect=self._read_only_sqlite_connection,
                    ),
                patch.object(
                    readSQL,
                    "_table_storage_bytes",
                    side_effect=AssertionError("storage helper must not run"),
                    ) as storage_size,
                ):
                status = readSQL.get_run_status(
                    "completed-guid",
                    database_path=database_path,
                    include_storage_bytes=False,
                    connection_callback=trace_connection,
                    )

        storage_size.assert_not_called()
        self.assertTrue(status["is_completed"])
        self.assertNotIn("storage_bytes", status)
        self.assertNotIn("storage_bytes_estimated", status)
        self.assertFalse(any("DBSTAT" in sql.upper() for sql in statements))
        self.assertFalse(any(
            "TABLE_INFO(\"RESULTS_1\")" in sql.upper()
            for sql in statements
            ))

    def test_status_includes_storage_calculation_when_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "completed.db")
            self._create_completed_status_database(database_path)
            expected = readSQL._StorageSize(4096, "exact")
            with (
                patch.object(
                    readSQL,
                    "qcodes_read_only_connection",
                    side_effect=self._read_only_sqlite_connection,
                    ),
                patch.object(
                    readSQL,
                    "_table_storage_bytes",
                    return_value=expected,
                    ) as storage_size,
                ):
                status = readSQL.get_run_status(
                    "completed-guid",
                    database_path=database_path,
                    include_storage_bytes=True,
                    )

        storage_size.assert_called_once()
        self.assertEqual(storage_size.call_args.args[1], "results_1")
        self.assertEqual(storage_size.call_args.kwargs["result_count"], 4)
        self.assertEqual(status["storage_bytes"], 4096)
        self.assertFalse(status["storage_bytes_estimated"])

    def test_successful_dbstat_storage_size_is_exact(self):
        cursor = Mock()
        cursor.fetchone.return_value = (8192, )

        storage_size = readSQL._table_storage_bytes(cursor, "results_1")
        metadata = {}
        readSQL._add_storage_size_fields(metadata, storage_size)

        self.assertEqual(storage_size, readSQL._StorageSize(8192, "exact"))
        self.assertEqual(metadata, {
            "storage_bytes": 8192,
            "storage_bytes_estimated": False,
            })
        cursor.execute.assert_called_once_with(
            "SELECT SUM(pgsize) FROM dbstat WHERE name = ?",
            ("results_1", ),
            )

    def test_missing_and_failing_dbstat_storage_sizes_are_estimated(self):
        for dbstat_failure in (None, sqlite3.OperationalError("no such table: dbstat")):
            with self.subTest(dbstat_failure=dbstat_failure):
                cursor = Mock()
                if dbstat_failure is None:
                    cursor.fetchone.return_value = (None, )
                else:
                    cursor.execute.side_effect = [dbstat_failure, None]
                cursor.fetchall.return_value = [
                    (0, "signal", "REAL", 0, None, 0),
                    ]

                storage_size = readSQL._table_storage_bytes(
                    cursor,
                    "results_1",
                    result_count=10,
                    )
                metadata = {}
                readSQL._add_storage_size_fields(metadata, storage_size)

                self.assertEqual(
                    storage_size,
                    readSQL._StorageSize(110, "estimated"),
                    )
                self.assertEqual(metadata, {
                    "storage_bytes": 110,
                    "storage_bytes_estimated": True,
                    })

    def test_unavailable_storage_sizes_have_consistent_metadata(self):
        cursor = Mock()
        cursor.execute.side_effect = [
            sqlite3.OperationalError("no such table: dbstat"),
            None,
            ]
        cursor.fetchall.return_value = []

        storage_size = readSQL._table_storage_bytes(
            cursor,
            "missing_results",
            result_count=10,
            )
        metadata = {}
        readSQL._add_storage_size_fields(metadata, storage_size)

        self.assertEqual(storage_size, readSQL._StorageSize(None, "unavailable"))
        self.assertEqual(metadata, {
            "storage_bytes": None,
            "storage_bytes_estimated": None,
            })

    def test_interrupted_exact_storage_query_is_not_converted_to_an_estimate(self):
        cursor = Mock()
        cursor.execute.side_effect = sqlite3.OperationalError("interrupted")

        with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
            readSQL._table_storage_bytes(cursor, "results_1", result_count=10)

    def test_large_completed_status_does_not_scan_storage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "large.db")
            self._create_completed_status_database(database_path, row_count=50_000)
            statements = []

            def trace_connection(connection):
                if connection is not None:
                    connection.set_trace_callback(statements.append)

            with patch.object(
                    readSQL,
                    "qcodes_read_only_connection",
                    side_effect=self._read_only_sqlite_connection,
                    ):
                status = readSQL.get_run_status(
                    "completed-guid",
                    database_path=database_path,
                    include_storage_bytes=False,
                    connection_callback=trace_connection,
                    )

        self.assertEqual(status["result_count"], 50_000)
        self.assertFalse(any("DBSTAT" in sql.upper() for sql in statements))
        self.assertFalse(any(
            "TABLE_INFO(\"RESULTS_1\")" in sql.upper()
            for sql in statements
            ))

    def test_has_finished_returns_optional_timestamp_scalar(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "runs.db")
            conn = sqlite3.connect(database_path)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "CREATE TABLE runs (guid TEXT, completed_timestamp REAL)"
                    )
                cursor.executemany(
                    "INSERT INTO runs VALUES (?, ?)",
                    [
                        ("finished-guid", 123.5),
                        ("unfinished-guid", None),
                        ],
                    )
                conn.commit()
            finally:
                cursor.close()
                conn.close()

            old_connection = readSQL.qcodes_read_only_connection
            readSQL.qcodes_read_only_connection = (
                lambda _database_path: sqlite3.connect(database_path)
                )
            try:
                self.assertEqual(readSQL.has_finished("finished-guid"), 123.5)
                self.assertIsNone(readSQL.has_finished("unfinished-guid"))
                self.assertIsNone(readSQL.has_finished("missing-guid"))
            finally:
                readSQL.qcodes_read_only_connection = old_connection

    def test_find_new_runs_uses_run_id_when_timestamps_are_missing_or_equal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "runs.db")
            conn = sqlite3.connect(database_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE experiments (
                        exp_id INTEGER,
                        name TEXT,
                        sample_name TEXT
                    )
                    """
                    )
                cursor.execute(
                    """
                    CREATE TABLE runs (
                        run_id INTEGER,
                        exp_id INTEGER,
                        name TEXT,
                        run_timestamp REAL,
                        completed_timestamp REAL,
                        is_completed INTEGER,
                        guid TEXT,
                        result_table_name TEXT,
                        parameters TEXT,
                        run_description TEXT
                    )
                    """
                    )
                cursor.execute("INSERT INTO experiments VALUES (1, 'exp', 'sample')")
                for run_id, run_timestamp in ((1, 100.0), (2, None), (3, 100.0)):
                    table_name = f"results_{run_id}"
                    cursor.execute(f"CREATE TABLE {table_name} (signal REAL)")
                    cursor.execute(
                        "INSERT INTO runs VALUES (?, 1, 'run', ?, NULL, 0, ?, ?, "
                        "'signal', '{}')",
                        (run_id, run_timestamp, f"guid-{run_id}", table_name),
                        )
                conn.commit()
            finally:
                conn.close()

            old_connection = readSQL.qcodes_read_only_connection
            readSQL.qcodes_read_only_connection = (
                lambda _database_path: sqlite3.connect(database_path)
                )
            try:
                runs = readSQL.find_new_runs(1)
            finally:
                readSQL.qcodes_read_only_connection = old_connection

        self.assertEqual(set(runs), {2, 3})

    def test_fetch_basic_run_rows_does_not_scan_result_tables(self):
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE experiments (
                    exp_id INTEGER,
                    name TEXT,
                    sample_name TEXT
                )
                """
                )
            cursor.execute(
                """
                CREATE TABLE runs (
                    run_id INTEGER,
                    exp_id INTEGER,
                    name TEXT,
                    run_timestamp REAL,
                    completed_timestamp REAL,
                    is_completed INTEGER,
                    guid TEXT,
                    result_table_name TEXT,
                    parameters TEXT,
                    run_description TEXT
                )
                """
                )
            run_description = json.dumps({
                "interdependencies_": {
                    "dependencies": {
                        "signal": ["x"],
                        "current": ["x"],
                        }
                    },
                "shapes": {
                    "signal": [10],
                    "current": [10],
                    },
                })
            cursor.execute(
                """
                INSERT INTO runs VALUES (
                    1, 1, 'run', 100.0, 110.0, 1, 'guid',
                    'missing_results', 'x,signal,current', ?
                )
                """,
                (run_description, )
                )

            runs = readSQL._fetch_run_rows(
                cursor,
                empty_as_none=False,
                include_details=False,
                )

            self.assertEqual(runs[1]["measure_parameters"], ["signal", "current"])
            self.assertEqual(runs[1]["sweep_parameters"], ["x"])
            self.assertEqual(runs[1]["setpoint_count"], 10)
            self.assertEqual(runs[1]["expected_results"], 20)
            self.assertNotIn("result_count", runs[1])
            self.assertNotIn("storage_bytes", runs[1])
        finally:
            conn.close()

    def test_background_detail_rows_skip_distinct_shape_and_storage_scans(self):
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE experiments (
                    exp_id INTEGER,
                    name TEXT,
                    sample_name TEXT
                )
                """
                )
            cursor.execute(
                """
                CREATE TABLE runs (
                    run_id INTEGER,
                    exp_id INTEGER,
                    name TEXT,
                    run_timestamp REAL,
                    completed_timestamp REAL,
                    is_completed INTEGER,
                    guid TEXT,
                    result_table_name TEXT,
                    parameters TEXT,
                    run_description TEXT,
                    measurement_exception TEXT
                )
                """
                )
            cursor.execute("CREATE TABLE results_1 (x REAL, y REAL, signal REAL)")
            cursor.executemany(
                "INSERT INTO results_1 VALUES (?, ?, ?)",
                [
                    (0.0, 0.0, 1.0),
                    (0.0, 1.0, 2.0),
                    (1.0, 0.0, 3.0),
                    (1.0, 1.0, 4.0),
                    ]
                )
            run_description = json.dumps({
                "interdependencies_": {
                    "dependencies": {
                        "signal": ["x", "y"],
                        }
                    },
                })
            cursor.execute(
                """
                INSERT INTO runs VALUES (
                    1, 1, 'run', 100.0, 110.0, 1, 'guid',
                    'results_1', 'x,y,signal', ?, ?
                )
                """,
                (
                    run_description,
                    "Traceback (most recent call last):\nKeyboardInterrupt\n",
                    )
                )

            statements = []
            conn.set_trace_callback(statements.append)
            runs = readSQL._fetch_run_rows(
                cursor,
                empty_as_none=False,
                infer_missing_shapes=False,
                include_storage_bytes=False,
                include_read_setpoint_count=False,
                )
            conn.set_trace_callback(None)

            self.assertEqual(runs[1]["result_count"], 4)
            self.assertIsNone(runs[1]["setpoint_count"])
            self.assertNotIn("storage_bytes", runs[1])
            self.assertNotIn("read_setpoint_count", runs[1])
            self.assertFalse(any("DISTINCT" in statement.upper() for statement in statements))
            self.assertFalse(any("DBSTAT" in statement.upper() for statement in statements))
        finally:
            conn.close()

    def test_background_detail_rows_can_include_cheap_storage_estimate(self):
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE experiments (
                    exp_id INTEGER,
                    name TEXT,
                    sample_name TEXT
                )
                """
                )
            cursor.execute(
                """
                CREATE TABLE runs (
                    run_id INTEGER,
                    exp_id INTEGER,
                    name TEXT,
                    run_timestamp REAL,
                    completed_timestamp REAL,
                    is_completed INTEGER,
                    guid TEXT,
                    result_table_name TEXT,
                    parameters TEXT,
                    run_description TEXT
                )
                """
                )
            cursor.execute("CREATE TABLE results_1 (x REAL, y REAL, signal REAL)")
            cursor.executemany(
                "INSERT INTO results_1 VALUES (?, ?, ?)",
                [
                    (0.0, 0.0, 1.0),
                    (0.0, 1.0, 2.0),
                    (1.0, 0.0, 3.0),
                    (1.0, 1.0, 4.0),
                    ]
                )
            run_description = json.dumps({
                "interdependencies_": {
                    "dependencies": {
                        "signal": ["x", "y"],
                        }
                    },
                })
            cursor.execute(
                """
                INSERT INTO runs VALUES (
                    1, 1, 'run', 100.0, 110.0, 1, 'guid',
                    'results_1', 'x,y,signal', ?
                )
                """,
                (run_description, )
                )

            statements = []
            conn.set_trace_callback(statements.append)
            runs = readSQL._fetch_run_rows(
                cursor,
                empty_as_none=False,
                infer_missing_shapes=False,
                include_storage_bytes=False,
                include_storage_estimate=True,
                include_read_setpoint_count=False,
                )
            conn.set_trace_callback(None)

            self.assertEqual(runs[1]["result_count"], 4)
            self.assertEqual(runs[1]["storage_bytes"], 116)
            self.assertTrue(runs[1]["storage_bytes_estimated"])
            self.assertFalse(any("DBSTAT" in statement.upper() for statement in statements))
            self.assertFalse(any("SUM(" in statement.upper() for statement in statements))
        finally:
            conn.close()

    def test_fetch_run_rows_includes_keyboard_interrupt_metadata(self):
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE experiments (
                    exp_id INTEGER,
                    name TEXT,
                    sample_name TEXT
                )
                """
                )
            cursor.execute(
                """
                CREATE TABLE runs (
                    run_id INTEGER,
                    exp_id INTEGER,
                    name TEXT,
                    run_timestamp REAL,
                    completed_timestamp REAL,
                    is_completed INTEGER,
                    guid TEXT,
                    result_table_name TEXT,
                    parameters TEXT,
                    run_description TEXT,
                    measurement_exception TEXT
                )
                """
                )
            cursor.execute("CREATE TABLE results_1 (x REAL, y REAL, signal REAL, other REAL)")
            cursor.executemany(
                "INSERT INTO results_1 VALUES (?, ?, ?, ?)",
                [
                    (0.0, 0.0, 1.0, 2.0),
                    (0.0, 1.0, 3.0, 4.0),
                    ]
                )
            run_description = json.dumps({
                "interdependencies_": {
                    "dependencies": {
                        "signal": ["x", "y"],
                        "other": ["x", "y"],
                        }
                    },
                "shapes": {
                    "signal": [2, 2],
                    "other": [2, 2],
                    },
                })
            cursor.execute(
                """
                INSERT INTO runs VALUES (
                    1, 1, 'run', 100.0, 110.0, 1, 'guid',
                    'results_1', 'x,y,signal,other', ?, ?
                )
                """,
                (
                    run_description,
                    "Traceback (most recent call last):\nKeyboardInterrupt\n",
                    )
                )

            runs = readSQL._fetch_run_rows(cursor, empty_as_none=False)

            self.assertEqual(
                runs[1]["measurement_exception"],
                "Traceback (most recent call last):\nKeyboardInterrupt\n"
                )
            self.assertEqual(runs[1]["setpoint_count"], 4)
            self.assertEqual(runs[1]["expected_results"], 8)
            self.assertEqual(runs[1]["result_count"], 2)
            self.assertEqual(runs[1]["read_setpoint_count"], 2)
        finally:
            conn.close()

    def test_point_shape_uses_largest_measured_parameter_shape(self):
        self.assertEqual(
            readSQL._point_shape(
                {
                    "shapes": {
                        "dmm_v1": [10, 100],
                        "dmm_v2": [10],
                        }
                    },
                ["dmm_v1", "dmm_v2"]
                ),
            [10, 100]
            )

    def test_expected_results_sums_all_measured_parameter_shapes(self):
        self.assertEqual(
            readSQL._expected_results_from_shapes(
                {
                    "shapes": {
                        "dmm_v1": [10, 100],
                        "dmm_v2": [10, 100],
                        }
                    },
                ["dmm_v1", "dmm_v2"]
                ),
            2000
            )

    def test_expected_results_handles_different_measured_shapes(self):
        self.assertEqual(
            readSQL._expected_results_from_shapes(
                {
                    "shapes": {
                        "dmm_v1": [10, 100],
                        "dmm_v2": [10],
                        }
                    },
                ["dmm_v1", "dmm_v2"]
                ),
            1010
            )

    def test_parameter_roles_include_axisless_standalone_measurements(self):
        run_description = {
            "interdependencies_": {
                "dependencies": {"signal": ["x"]},
                "standalones": ["temperature"],
                },
            }

        measure_parameters, sweep_parameters = readSQL._parameter_roles(
            run_description,
            "x,signal,temperature",
            )

        self.assertEqual(measure_parameters, ["signal", "temperature"])
        self.assertEqual(sweep_parameters, ["x"])

    def test_live_unshaped_status_refreshes_observed_count_until_completion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "runs.db")
            conn = sqlite3.connect(database_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE runs (
                        guid TEXT,
                        completed_timestamp REAL,
                        is_completed INTEGER,
                        result_table_name TEXT,
                        run_description TEXT,
                        parameters TEXT
                    )
                    """
                    )
                cursor.execute("CREATE TABLE results_1 (x REAL, signal REAL)")
                run_description = json.dumps({
                    "interdependencies_": {
                        "dependencies": {"signal": ["x"]},
                        },
                    })
                cursor.execute(
                    "INSERT INTO runs VALUES (?, NULL, 0, ?, ?, ?)",
                    ("guid", "results_1", run_description, "x,signal"),
                    )
                cursor.execute("INSERT INTO results_1 VALUES (0, 1)")
                conn.commit()

                old_connection = readSQL.qcodes_read_only_connection
                readSQL.qcodes_read_only_connection = (
                    lambda _database_path: sqlite3.connect(database_path)
                    )
                try:
                    first_status = readSQL.get_run_status("guid")
                    cursor.executemany(
                        "INSERT INTO results_1 VALUES (?, ?)",
                        [(index, index + 1) for index in range(1, 5)],
                        )
                    conn.commit()
                    growing_status = readSQL.get_run_status("guid")
                    cursor.execute(
                        "UPDATE runs SET completed_timestamp = 123, is_completed = 1"
                        )
                    conn.commit()
                    completed_status = readSQL.get_run_status("guid")
                finally:
                    readSQL.qcodes_read_only_connection = old_connection

                self.assertEqual(first_status["setpoint_shape"], [1])
                self.assertEqual(first_status["setpoint_count"], 1)
                self.assertEqual(first_status["setpoint_count_source"], "observed")
                self.assertIsNone(first_status["expected_results"])

                self.assertEqual(growing_status["setpoint_shape"], [5])
                self.assertEqual(growing_status["setpoint_count"], 5)
                self.assertEqual(growing_status["result_count"], 5)
                self.assertIsNone(growing_status["expected_results"])

                self.assertEqual(completed_status["setpoint_shape"], [5])
                self.assertEqual(completed_status["setpoint_count"], 5)
                self.assertEqual(completed_status["expected_results"], 5)
                self.assertEqual(
                    completed_status["expected_results_source"],
                    "observed",
                    )
            finally:
                conn.close()

    def test_heterogeneous_dependencies_do_not_form_a_cartesian_shape(self):
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE results (x REAL, t REAL, a REAL, b REAL)")
            cursor.executemany(
                "INSERT INTO results VALUES (?, ?, ?, ?)",
                [
                    (0, None, 10, None),
                    (1, None, 11, None),
                    (2, None, 12, None),
                    (3, None, 13, None),
                    (None, 0, None, 20),
                    (None, 1, None, 21),
                    (None, 2, None, 22),
                    (None, 3, None, 23),
                    ],
                )
            metadata = {
                "completed_timestamp": 123.0,
                "is_completed": 1,
                "parameters": "x,t,a,b",
                "result_table_name": "results",
                "run_description": json.dumps({
                    "interdependencies_": {
                        "dependencies": {
                            "a": ["x"],
                            "b": ["t"],
                            },
                        },
                    }),
                }

            readSQL._add_run_basic_fields(metadata)
            readSQL._add_run_detail_fields(
                cursor,
                metadata,
                include_storage_bytes=False,
                )

            self.assertEqual(metadata["sweep_parameters"], ["x", "t"])
            self.assertIsNone(metadata["setpoint_shape"])
            self.assertIsNone(metadata["point_shape"])
            self.assertEqual(metadata["setpoint_count"], 4)
            self.assertEqual(metadata["setpoint_count_source"], "observed")
            self.assertEqual(metadata["expected_results"], 8)
            self.assertEqual(metadata["expected_results_source"], "observed")
        finally:
            conn.close()

    def test_sparse_setpoints_do_not_form_a_cartesian_shape(self):
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE results (x REAL, y REAL, signal REAL)")
            cursor.executemany(
                "INSERT INTO results VALUES (?, ?, ?)",
                [(0, 0, 10), (1, 1, 11), (2, 2, 12)],
                )

            self.assertIsNone(
                readSQL._setpoint_shape_from_result_table(
                    cursor,
                    "results",
                    ["x", "y"],
                    )
                )
            self.assertEqual(
                readSQL._read_setpoint_count(cursor, "results", ["x", "y"]),
                3,
                )
        finally:
            conn.close()

    def test_shape_batches_keep_sparse_runs_with_an_observed_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "runs.db")
            conn = sqlite3.connect(database_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "CREATE TABLE experiments (exp_id INTEGER, name TEXT, sample_name TEXT)"
                    )
                cursor.execute(
                    """
                    CREATE TABLE runs (
                        run_id INTEGER,
                        exp_id INTEGER,
                        name TEXT,
                        run_timestamp REAL,
                        completed_timestamp REAL,
                        is_completed INTEGER,
                        guid TEXT,
                        result_table_name TEXT,
                        parameters TEXT,
                        run_description TEXT
                    )
                    """
                    )
                cursor.execute("CREATE TABLE results_1 (x REAL, y REAL, signal REAL)")
                cursor.executemany(
                    "INSERT INTO results_1 VALUES (?, ?, ?)",
                    [(0, 0, 10), (1, 1, 11), (2, 2, 12)],
                    )
                run_description = json.dumps({
                    "interdependencies_": {
                        "dependencies": {"signal": ["x", "y"]},
                        },
                    })
                cursor.execute("INSERT INTO experiments VALUES (1, 'exp', 'sample')")
                cursor.execute(
                    """
                    INSERT INTO runs VALUES (
                        1, 1, 'run', 100, 123, 1, 'guid',
                        'results_1', 'x,y,signal', ?
                    )
                    """,
                    (run_description, ),
                    )
                conn.commit()

                old_connection = readSQL.qcodes_read_only_connection
                readSQL.qcodes_read_only_connection = (
                    lambda _database_path: sqlite3.connect(database_path)
                    )
                try:
                    batches = list(readSQL.iter_run_shape_batches_via_sql(
                        database_path,
                        [1],
                        ))
                finally:
                    readSQL.qcodes_read_only_connection = old_connection

                self.assertEqual(len(batches), 1)
                self.assertIsNone(batches[0][1]["setpoint_shape"])
                self.assertEqual(batches[0][1]["setpoint_count"], 3)
            finally:
                conn.close()

    def test_read_setpoint_count_is_aggregated_by_sql(self):
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE results (x REAL, y REAL)")
            cursor.executemany(
                "INSERT INTO results VALUES (?, ?)",
                [(index, index % 3) for index in range(100)],
                )
            statements = []
            conn.set_trace_callback(statements.append)

            count = readSQL._read_setpoint_count(
                cursor,
                "results",
                ["x", "y"],
                )
            conn.set_trace_callback(None)

            normalized_statements = [
                " ".join(statement.upper().split())
                for statement in statements
                ]
            self.assertEqual(count, 100)
            self.assertTrue(any(
                "SELECT COUNT(*) FROM ( SELECT DISTINCT" in statement
                for statement in normalized_statements
                ))
            self.assertFalse(any(
                statement.startswith("SELECT DISTINCT")
                for statement in normalized_statements
                ))
        finally:
            conn.close()

    def test_point_shape_falls_back_to_distinct_sweep_values(self):
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE results (x REAL, y REAL, signal_a REAL, signal_b REAL)")
            cursor.executemany(
                "INSERT INTO results VALUES (?, ?, ?, ?)",
                [
                    (0.0, 0.0, 1.0, 2.0),
                    (0.0, 1.0, 3.0, 4.0),
                    (1.0, 0.0, 5.0, 6.0),
                    (1.0, 1.0, 7.0, 8.0),
                    ]
                )

            self.assertEqual(
                readSQL._point_shape_from_result_table(
                    cursor,
                    "results",
                    ["x", "y"],
                    ["signal_a", "signal_b"],
                    4,
                    ),
                [2, 2]
                )
        finally:
            conn.close()

    def test_point_shape_fallback_includes_measured_row_factor(self):
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE results (x REAL, y REAL, signal_a REAL, signal_b REAL)")
            cursor.executemany(
                "INSERT INTO results VALUES (?, ?, ?, ?)",
                [
                    (0.0, 0.0, 1.0, None),
                    (0.0, 0.0, None, 2.0),
                    (0.0, 1.0, 3.0, None),
                    (0.0, 1.0, None, 4.0),
                    (1.0, 0.0, 5.0, None),
                    (1.0, 0.0, None, 6.0),
                    (1.0, 1.0, 7.0, None),
                    (1.0, 1.0, None, 8.0),
                    ]
                )

            self.assertEqual(
                readSQL._point_shape_from_result_table(
                    cursor,
                    "results",
                    ["x", "y"],
                    ["signal_a", "signal_b"],
                    8,
                    ),
                [2, 2, 2]
                )
            self.assertEqual(
                readSQL._setpoint_shape_from_result_table(
                    cursor,
                    "results",
                    ["x", "y"],
                    ),
                [2, 2]
                )
        finally:
            conn.close()

    def test_storage_size_falls_back_to_schema_estimate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "storage.db")
            conn = sqlite3.connect(database_path)
            try:
                cursor = conn.cursor()
                cursor.execute("""
                  CREATE TABLE "results-1-4" (
                      id INTEGER,
                      timestamp REAL,
                      dac_ch1 REAL,
                      dac_ch2 REAL,
                      dmm_v1 REAL,
                      dmm_v2 REAL
                  )
                """)
                cursor.executemany(
                    'INSERT INTO "results-1-4" VALUES (?, ?, ?, ?, ?, ?)',
                    [(i, i * 0.01, 1.0, 2.0, 3.0, 4.0) for i in range(2000)]
                    )
                conn.commit()

                self.assertEqual(
                    readSQL._estimated_table_storage_bytes(cursor, "results-1-4"),
                    112_000
                    )
            finally:
                cursor.close()
                conn.close()
