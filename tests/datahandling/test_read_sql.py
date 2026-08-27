import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from qplot.datahandling import readSQL
from qplot.datahandling.trusted_presentation import (
    TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES,
    TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES,
    TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_PARAMETERS,
    TRUSTED_PRESENTATION_MAX_RENDERED_NODES,
    TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES,
)


class RunSizeTestCase(unittest.TestCase):
    def _read_only_sqlite_connection(self, database_path, **_kwargs):
        return sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)

    def _create_completed_status_database(
            self,
            database_path,
            row_count=4,
            is_completed=True,
            ):
        conn = sqlite3.connect(database_path)
        try:
            conn.execute(
                """
                CREATE TABLE runs (
                    guid TEXT,
                    run_timestamp REAL,
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
                "INSERT INTO runs VALUES (?, 100, ?, ?, ?, ?, ?)",
                (
                    "completed-guid",
                    123 if is_completed else None,
                    int(is_completed),
                    "results_1",
                    run_description,
                    "x,signal",
                    ),
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

    def test_selected_run_setpoint_summaries_are_computed_in_data_layer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "details.db")
            conn = sqlite3.connect(database_path)
            try:
                conn.execute(
                    'CREATE TABLE "results-1-1" ('
                    "id INTEGER PRIMARY KEY, gate REAL, bias REAL, signal REAL)"
                    )
                conn.executemany(
                    'INSERT INTO "results-1-1" VALUES (?, ?, ?, ?)',
                    [
                        (1, -1.0, -2.0, 1.0),
                        (2, -1.0, 0.0, 2.0),
                        (3, 0.0, 2.0, 3.0),
                        (4, 1.0, -2.0, 4.0),
                        (5, 1.0, 2.0, 5.0),
                        ],
                    )
                conn.commit()
            finally:
                conn.close()

            with patch.object(
                    readSQL,
                    "sqlite_read_only_connection",
                    side_effect=self._read_only_sqlite_connection,
                    ):
                summaries = readSQL.get_selected_run_setpoint_summaries(
                    database_path,
                    "results-1-1",
                    ("gate", "bias"),
                    5,
                    )

        self.assertEqual(summaries, {
            "gate": {"from": -1.0, "to": 1.0, "steps": 3},
            "bias": {"from": -2.0, "to": 2.0, "steps": 3},
            })

    def test_snapshot_selected_detail_is_plain_bounded_and_dataset_free(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "selected.db")
            conn = sqlite3.connect(database_path)
            try:
                conn.execute(
                    "CREATE TABLE experiments ("
                    "exp_id INTEGER, name TEXT, sample_name TEXT)"
                )
                conn.execute(
                    "CREATE TABLE runs ("
                    "run_id INTEGER PRIMARY KEY, exp_id INTEGER, name TEXT, "
                    "result_table_name TEXT, run_timestamp REAL, "
                    "completed_timestamp REAL, is_completed INTEGER, "
                    "parameters TEXT, guid TEXT, run_description TEXT, "
                    "snapshot TEXT, operator TEXT)"
                )
                conn.execute(
                    "CREATE TABLE layouts ("
                    "layout_id INTEGER PRIMARY KEY, run_id INTEGER, "
                    "parameter TEXT, label TEXT, unit TEXT, inferred_from TEXT)"
                )
                conn.execute(
                    'CREATE TABLE "results-7" (id INTEGER PRIMARY KEY, '
                    "gate REAL, signal REAL)"
                )
                description = json.dumps({
                    "interdependencies_": {
                        "parameters": {
                            "gate": {
                                "name": "gate",
                                "type": "numeric",
                                "label": "Gate",
                                "unit": "V",
                                },
                            "signal": {
                                "name": "signal",
                                "type": "numeric",
                                "label": "Current",
                                "unit": "A",
                                },
                            },
                        "dependencies": {"signal": ["gate"]},
                        },
                    "shapes": {"signal": [3]},
                    })
                conn.execute("INSERT INTO experiments VALUES (1, 'exp', 'sample')")
                conn.execute(
                    "INSERT INTO runs VALUES ("
                    "7, 1, 'selected', 'results-7', 100, 120, 1, "
                    "'gate,signal', 'guid-7', ?, ?, 'Ada')",
                    (
                        description,
                        "x" * (
                            readSQL.MAX_SNAPSHOT_SELECTED_RUN_SCALAR_BYTES + 1
                        ),
                    ),
                )
                conn.executemany(
                    'INSERT INTO "results-7" VALUES (?, ?, ?)',
                    ((1, -1.0, 10.0), (2, 0.0, 20.0), (3, 1.0, 30.0)),
                )
                conn.executemany(
                    "INSERT INTO layouts VALUES (?, 7, ?, ?, ?, NULL)",
                    ((1, "gate", "Gate", "V"), (2, "signal", "Current", "A")),
                )
                conn.commit()
            finally:
                conn.close()

            statements = []

            def read_only_connection(path, **_kwargs):
                connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                connection.set_trace_callback(statements.append)
                return connection

            with patch.object(
                    readSQL,
                    "qcodes_read_only_connection",
                    side_effect=read_only_connection,
                    ):
                detail = readSQL.get_snapshot_selected_run_detail(
                    database_path,
                    7,
                    "guid-7",
                    {
                        "result_count": 3,
                        "setpoint_shape": [3],
                        "measure_parameters": ["signal"],
                        "sweep_parameters": ["gate"],
                    },
                )

        self.assertEqual(detail.run.run_id, 7)
        self.assertEqual(detail.run.as_dict()["run_id"], 7)
        self.assertEqual(dict(detail.presentation.run_fields)["run_id"], 7)
        self.assertEqual(detail.run.as_dict()["guid"], "guid-7")
        self.assertEqual(detail.run.as_dict()["result_count"], 3)
        self.assertEqual(dict(detail.metadata), {"operator": "Ada"})
        self.assertEqual(detail.snapshot.status, "unavailable")
        self.assertIn("was stored", detail.snapshot.message)
        self.assertIn("exceeds", detail.snapshot.message)
        self.assertNotIn("No snapshot was stored", detail.snapshot.message)
        self.assertFalse(hasattr(detail, "snapshot_json"))
        self.assertIn("snapshot", detail.unavailable_fields)
        self.assertEqual(
            [(parameter.name, parameter.depends_on) for parameter in detail.parameters],
            [("gate", ()), ("signal", ("gate",))],
        )
        self.assertEqual(
            [
                (summary.name, summary.first, summary.last, summary.steps)
                for summary in detail.setpoint_summaries
            ],
            [("gate", -1.0, 1.0, 3)],
        )
        self.assertFalse(hasattr(detail, "dataset"))
        self.assertFalse(any(
            'SELECT * FROM "results-7"' in statement
            for statement in statements
        ))

    def test_snapshot_fallback_preserves_all_stored_payload_states(self):
        cases = (
            ("null", None, "empty", "No snapshot was stored"),
            (
                "valid",
                '{"station":{"valid":true}}',
                "available",
                "decoded within all limits",
            ),
            ("malformed", '{"station":', "malformed", "Malformed snapshot JSON"),
            (
                "oversized",
                "x" * (readSQL.MAX_SNAPSHOT_SELECTED_RUN_SCALAR_BYTES + 1),
                "unavailable",
                "was stored",
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            for label, stored_snapshot, expected_status, message_fragment in cases:
                with self.subTest(label=label):
                    database_path = os.path.join(temp_dir, f"snapshot-{label}.db")
                    conn = sqlite3.connect(database_path)
                    try:
                        conn.execute(
                            "CREATE TABLE runs ("
                            "run_id INTEGER PRIMARY KEY, guid TEXT, snapshot TEXT)"
                        )
                        conn.execute(
                            "CREATE TABLE layouts ("
                            "layout_id INTEGER PRIMARY KEY, run_id INTEGER, "
                            "parameter TEXT, label TEXT, unit TEXT, "
                            "inferred_from TEXT)"
                        )
                        conn.execute(
                            "INSERT INTO runs VALUES (7, 'guid-7', ?)",
                            (stored_snapshot,),
                        )
                        conn.commit()
                    finally:
                        conn.close()

                    with patch.object(
                            readSQL,
                            "qcodes_read_only_connection",
                            side_effect=self._read_only_sqlite_connection,
                            ):
                        detail = readSQL.get_snapshot_selected_run_detail(
                            database_path,
                            7,
                            "guid-7",
                        )

                    self.assertEqual(detail.snapshot.status, expected_status)
                    self.assertIn(message_fragment, detail.snapshot.message)
                    if label == "oversized":
                        self.assertIn("snapshot", detail.unavailable_fields)
                        self.assertIn("exceeds", detail.snapshot.message)
                        self.assertNotIn(
                            "No snapshot was stored",
                            detail.snapshot.message,
                        )
                        self.assertEqual(
                            detail.snapshot.input_bytes,
                            len(stored_snapshot.encode("utf-8")),
                        )

    def test_snapshot_selected_detail_discards_large_dynamic_and_raw_values(self):
        huge_label = "fallback-private-label-" * 30_000
        huge_metadata = "fallback-private-metadata-" * 30_000
        description = json.dumps({
            "interdependencies_": {
                "parameters": {
                    "gate": {
                        "name": "gate",
                        "label": huge_label,
                        "unit": "V",
                        "type": "numeric",
                    }
                },
                "dependencies": {},
            }
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "large-selected.db")
            conn = sqlite3.connect(database_path)
            try:
                conn.execute(
                    "CREATE TABLE runs ("
                    "run_id INTEGER PRIMARY KEY, guid TEXT, name TEXT, "
                    "result_table_name TEXT, parameters TEXT, "
                    "run_description TEXT, snapshot TEXT, operator TEXT)"
                )
                conn.execute(
                    "CREATE TABLE layouts ("
                    "layout_id INTEGER PRIMARY KEY, run_id INTEGER, "
                    "parameter TEXT, label TEXT, unit TEXT, inferred_from TEXT)"
                )
                conn.execute(
                    'CREATE TABLE "results-7" (id INTEGER PRIMARY KEY, gate REAL)'
                )
                conn.execute(
                    "INSERT INTO runs VALUES "
                    "(7, 'guid-7', 'large', 'results-7', 'gate', ?, '{}', ?)",
                    (description, huge_metadata),
                )
                conn.execute('INSERT INTO "results-7" VALUES (1, 1.0)')
                conn.commit()
            finally:
                conn.close()

            with patch.object(
                    readSQL,
                    "qcodes_read_only_connection",
                    side_effect=self._read_only_sqlite_connection,
                    ):
                detail = readSQL.get_snapshot_selected_run_detail(
                    database_path,
                    7,
                    "guid-7",
                    {
                        "result_count": 1,
                        "measure_parameters": [],
                        "sweep_parameters": ["gate"],
                    },
                )

        self.assertNotIn("run_description", detail.run.as_dict())
        self.assertLessEqual(
            len(dict(detail.metadata)["operator"].encode("utf-8")),
            TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES,
        )
        self.assertLessEqual(
            len(detail.parameters[0].label.encode("utf-8")),
            TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
        )
        self.assertNotIn("fallback-private-label-" * 100, repr(detail))
        self.assertNotIn("fallback-private-metadata-" * 100, repr(detail))
        for view in (detail.presentation.metadata, detail.presentation.raw):
            self.assertLessEqual(
                len(view.nodes),
                TRUSTED_PRESENTATION_MAX_RENDERED_NODES,
            )
            self.assertTrue(all(
                len(node.tooltip.encode("utf-8"))
                <= TRUSTED_PRESENTATION_MAX_TOOLTIP_BYTES
                for node in view.nodes
            ))

    def test_snapshot_many_tiny_description_parameters_are_source_bounded(self):
        names = tuple(f"parameter-{index}" for index in range(5_000))
        axes = tuple(f"axis-{index}" for index in range(5_000))
        specifications = {
            name: {"label": "label", "unit": "V", "type": "numeric"}
            for name in names
        }
        nested_secret = "nested-parameter-private-value-" * 2_000
        specifications[names[0]]["label"] = {
            "private": nested_secret
        }
        description = json.dumps(
            {
                "interdependencies_": {
                    "parameters": specifications,
                    "dependencies": {names[0]: axes},
                }
            },
            separators=(",", ":"),
        )
        self.assertLess(
            len(description.encode("utf-8")),
            readSQL.MAX_SNAPSHOT_SELECTED_RUN_SCALAR_BYTES,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "wide-description.db")
            conn = sqlite3.connect(database_path)
            try:
                conn.execute(
                    "CREATE TABLE runs ("
                    "run_id INTEGER PRIMARY KEY, guid TEXT, name TEXT, "
                    "result_table_name TEXT, parameters TEXT, "
                    "run_description TEXT, snapshot TEXT)"
                )
                conn.execute(
                    "CREATE TABLE layouts ("
                    "layout_id INTEGER PRIMARY KEY, run_id INTEGER, "
                    "parameter TEXT, label TEXT, unit TEXT, inferred_from TEXT)"
                )
                conn.execute('CREATE TABLE "results-7" (id INTEGER PRIMARY KEY)')
                conn.execute(
                    "INSERT INTO runs VALUES "
                    "(7, 'guid-7', 'wide', 'results-7', ?, ?, '{}')",
                    (",".join(names), description),
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(
                    readSQL,
                    "qcodes_read_only_connection",
                    side_effect=self._read_only_sqlite_connection,
                    ):
                detail = readSQL.get_snapshot_selected_run_detail(
                    database_path,
                    7,
                    "guid-7",
                    {"result_count": 0},
                )

        self.assertLessEqual(
            len(detail.parameters),
            TRUSTED_PRESENTATION_MAX_PARAMETERS,
        )
        self.assertLessEqual(
            max(len(parameter.depends_on) for parameter in detail.parameters),
            TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES,
        )
        self.assertTrue(detail.presentation.parameters_truncated)
        self.assertIn("parameters.presentation", detail.unavailable_fields)
        self.assertTrue(detail.run.as_dict()["parameters_truncated"])
        self.assertNotIn(names[-1], repr(detail))
        self.assertNotIn(axes[-1], repr(detail))
        self.assertNotIn(nested_secret, repr(detail))

    def test_snapshot_detail_skips_grouping_when_stale_count_hides_large_pk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "stale-count.db")
            conn = sqlite3.connect(database_path)
            try:
                conn.execute(
                    "CREATE TABLE runs ("
                    "run_id INTEGER PRIMARY KEY, guid TEXT, "
                    "result_table_name TEXT, parameters TEXT, "
                    "run_description TEXT)"
                )
                conn.execute(
                    "CREATE TABLE layouts ("
                    "layout_id INTEGER PRIMARY KEY, run_id INTEGER, "
                    "parameter TEXT, label TEXT, unit TEXT, inferred_from TEXT)"
                )
                conn.execute(
                    'CREATE TABLE "results-7" ('
                    "id INTEGER PRIMARY KEY, gate REAL, signal REAL)"
                )
                conn.execute(
                    "INSERT INTO runs VALUES (7, 'guid-7', 'results-7', "
                    "'gate,signal', '{}')"
                )
                conn.execute(
                    'INSERT INTO "results-7" VALUES (?, 1.0, 2.0)',
                    (readSQL.MAX_SELECTED_RUN_SETPOINT_SUMMARY_ROWS + 1,),
                )
                conn.commit()
            finally:
                conn.close()

            statements = []

            def read_only_connection(path, **_kwargs):
                connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                connection.set_trace_callback(statements.append)
                return connection

            with patch.object(
                    readSQL,
                    "qcodes_read_only_connection",
                    side_effect=read_only_connection,
                    ):
                detail = readSQL.get_snapshot_selected_run_detail(
                    database_path,
                    7,
                    "guid-7",
                    {
                        "result_count": 1,
                        "setpoint_shape": [1],
                        "measure_parameters": ["signal"],
                        "sweep_parameters": ["gate"],
                    },
                )

        self.assertEqual(
            [
                (summary.name, summary.first, summary.last, summary.steps)
                for summary in detail.setpoint_summaries
            ],
            [("gate", None, None, 1)],
        )
        self.assertTrue(any("SELECT MAX(\"id\")" in sql for sql in statements))
        self.assertFalse(any("GROUP BY" in sql.upper() for sql in statements))

    def test_snapshot_detail_skips_grouping_for_small_count_huge_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "huge-setpoint.db")
            conn = sqlite3.connect(database_path)
            try:
                conn.execute(
                    "CREATE TABLE runs ("
                    "run_id INTEGER PRIMARY KEY, guid TEXT, "
                    "result_table_name TEXT, parameters TEXT, "
                    "run_description TEXT)"
                )
                conn.execute(
                    "CREATE TABLE layouts ("
                    "layout_id INTEGER PRIMARY KEY, run_id INTEGER, "
                    "parameter TEXT, label TEXT, unit TEXT, inferred_from TEXT)"
                )
                conn.execute(
                    'CREATE TABLE "results-7" ('
                    "id INTEGER PRIMARY KEY, gate BLOB, signal REAL)"
                )
                conn.execute(
                    "INSERT INTO runs VALUES (7, 'guid-7', 'results-7', "
                    "'gate,signal', '{}')"
                )
                conn.execute(
                    'INSERT INTO "results-7" VALUES (1, zeroblob(?), 2.0)',
                    (
                        readSQL.MAX_SELECTED_RUN_SETPOINT_SUMMARY_SOURCE_BYTES
                        + 1,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertGreater(
                os.path.getsize(database_path),
                readSQL.MAX_SELECTED_RUN_SETPOINT_SUMMARY_SOURCE_BYTES,
            )
            statements = []

            def read_only_connection(path, **_kwargs):
                connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                connection.set_trace_callback(statements.append)
                return connection

            with patch.object(
                    readSQL,
                    "qcodes_read_only_connection",
                    side_effect=read_only_connection,
                    ):
                detail = readSQL.get_snapshot_selected_run_detail(
                    database_path,
                    7,
                    "guid-7",
                    {
                        "result_count": 1,
                        "setpoint_shape": [1],
                        "measure_parameters": ["signal"],
                        "sweep_parameters": ["gate"],
                    },
                )

        self.assertEqual(
            [
                (summary.name, summary.first, summary.last, summary.steps)
                for summary in detail.setpoint_summaries
            ],
            [("gate", None, None, 1)],
        )
        self.assertFalse(any("GROUP BY" in sql.upper() for sql in statements))

    def test_large_or_unknown_selected_run_uses_shape_without_opening_sqlite(self):
        with patch.object(
                readSQL,
                "sqlite_read_only_connection",
                side_effect=AssertionError("bounded summary opened SQLite"),
                ):
            for result_count in (
                    None,
                    readSQL.MAX_SELECTED_RUN_SETPOINT_SUMMARY_ROWS + 1,
                    ):
                with self.subTest(result_count=result_count):
                    summaries = readSQL.get_selected_run_setpoint_summaries(
                        "large.db",
                        "results",
                        ("gate",),
                        result_count,
                        setpoint_shape=(1000,),
                        )
                    self.assertEqual(summaries, {"gate": {"steps": 1000}})

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

    def test_running_status_counts_measured_setpoints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "running.db")
            self._create_completed_status_database(
                database_path,
                row_count=4,
                is_completed=False,
                )

            with patch.object(
                    readSQL,
                    "qcodes_read_only_connection",
                    side_effect=self._read_only_sqlite_connection,
                    ):
                status = readSQL.get_run_status(
                    "completed-guid",
                    database_path=database_path,
                    include_storage_bytes=False,
                    )

        self.assertFalse(status["is_completed"])
        self.assertEqual(status["setpoint_count"], 4)
        self.assertEqual(status["read_setpoint_count"], 4)

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

    def test_parameter_source_iterators_stop_after_limit_probe(self):
        class GuardedDependencies(dict):
            yielded = 0

            def items(self):
                for index in range(TRUSTED_PRESENTATION_MAX_PARAMETERS + 1):
                    self.yielded += 1
                    yield f"parameter-{index}", [f"axis-{index}"]
                raise AssertionError("dependency mapping traversed past MAX + 1")

        class GuardedAxes(list):
            yielded = 0

            def __iter__(self):
                for index in range(
                    TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES + 1
                ):
                    self.yielded += 1
                    yield f"axis-{index}"
                raise AssertionError("dependency sequence traversed past MAX + 1")

        class RecordingParameterText(str):
            split_limits = []

            def split(self, separator=None, maxsplit=-1):
                self.split_limits.append((separator, maxsplit))
                return super().split(separator, maxsplit)

        dependencies = GuardedDependencies({"present": ["axis"]})
        bounded, truncated = readSQL._bounded_parameter_dependencies({
            "interdependencies_": {"dependencies": dependencies}
        })
        self.assertTrue(truncated)
        self.assertEqual(
            dependencies.yielded,
            TRUSTED_PRESENTATION_MAX_PARAMETERS + 1,
        )
        self.assertEqual(len(bounded), TRUSTED_PRESENTATION_MAX_PARAMETERS)

        axes = GuardedAxes(["present"])
        bounded, truncated = readSQL._bounded_parameter_dependencies({
            "interdependencies_": {"dependencies": {"signal": axes}}
        })
        self.assertTrue(truncated)
        self.assertEqual(
            axes.yielded,
            TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES + 1,
        )
        self.assertEqual(
            len(bounded["signal"]),
            TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES,
        )

        parameter_text = RecordingParameterText("x,signal,temperature")
        readSQL._bounded_parameter_roles({}, parameter_text)
        self.assertEqual(
            parameter_text.split_limits,
            [(",", TRUSTED_PRESENTATION_MAX_PARAMETERS)],
        )

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
                        run_timestamp REAL,
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
                    "INSERT INTO runs VALUES (?, 100, NULL, 0, ?, ?, ?)",
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
                self.assertEqual(first_status["run_timestamp"], 100)
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
