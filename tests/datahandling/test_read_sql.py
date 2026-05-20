import json
import os
import sqlite3
import tempfile
import unittest

from qplot.datahandling import readSQL


class RunSizeTestCase(unittest.TestCase):
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
                    112000
                    )
            finally:
                cursor.close()
                conn.close()
