import os
import sqlite3
import tempfile
import unittest

from qplot.datahandling import readSQL


class RunSizeTestCase(unittest.TestCase):
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


