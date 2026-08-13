import unittest

import numpy as np
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as qtw

from qplot.windows._widgets import treeWidgets
from qplot.windows._widgets.preview import (
    DraggablePreviewImageLabel,
    render_sparkline_preview,
)


class RunListTooltipTestCase(unittest.TestCase):
    def test_format_timestamp_tolerates_malformed_database_values(self):
        self.assertEqual(treeWidgets.format_timestamp("not-a-timestamp"), "unknown")
        self.assertEqual(treeWidgets.format_timestamp(float("nan")), "unknown")
        self.assertEqual(treeWidgets.format_timestamp(10**30), "unknown")

    def test_add_runs_displays_run_with_missing_timestamp(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                2: {
                    "run_timestamp": None,
                    "completed_timestamp": None,
                    "is_completed": False,
                    "guid": "guid-2",
                    "sweep_parameters": [],
                    "measure_parameters": [],
                    "result_count": 0,
                    },
                3: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "guid-3",
                    "sweep_parameters": [],
                    "measure_parameters": ["signal"],
                    "result_count": 1,
                    },
                })

            self.assertEqual(run_list.maxRunId, 3)
            self.assertEqual(run_list.topLevelItemCount(), 2)
            missing_timestamp_item = next(
                run_list.topLevelItem(index)
                for index in range(run_list.topLevelItemCount())
                if run_list.topLevelItem(index).guid == "guid-2"
                )
            self.assertEqual(
                missing_timestamp_item.text(run_list.cols.index("Started")),
                "unknown",
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_run_tooltip_summarises_parameters(self):
        tooltip = treeWidgets.run_tooltip_text({
            "sweep_parameters": ["dac_ch1", "dac_ch2"],
            "measure_parameters": ["dmm_v1", "dmm_v2"],
            "run_timestamp": 100.0,
            "completed_timestamp": None,
            "is_completed": False,
            "result_count": 25,
            "expected_results": 100,
            "setpoint_count": 100,
            "setpoint_count_source": "planned",
            "read_setpoint_count": 25,
            })

        self.assertTrue(tooltip.startswith("<table"))
        self.assertEqual(tooltip.count("<tr>"), 3)
        self.assertIn("<td style='padding:0 0.5em 0 0'>Sweep</td>", tooltip)
        self.assertIn(
            "<td nowrap='nowrap' style='padding:0; white-space:nowrap'>"
            "(dac_ch1,&nbsp;dac_ch2)</td>",
            tooltip
            )
        self.assertIn("<td style='padding:0 0.5em 0 0'>Measure</td>", tooltip)
        self.assertIn(
            "<td nowrap='nowrap' style='padding:0; white-space:nowrap'>"
            "(dmm_v1,&nbsp;dmm_v2)</td>",
            tooltip
            )
        self.assertIn("Status</td>", tooltip)
        self.assertIn("Running (25.0%)</td>", tooltip)
        self.assertNotIn("Duration", tooltip)

    def test_format_point_count_summarises_multidimensional_sweeps(self):
        self.assertEqual(
            treeWidgets.format_point_count({
                "point_shape": [10, 100],
                "expected_results": 1000,
                }),
            "1,000 = 10 × 100"
            )

    def test_format_point_count_suppresses_duplicate_one_dimensional_shape(self):
        self.assertEqual(
            treeWidgets.format_point_count({
                "setpoint_shape": [10],
                "setpoint_count": 10,
                "point_shape": [10],
                "expected_results": 10,
                }),
            "10"
            )

    def test_format_point_count_keeps_non_duplicate_one_dimensional_shape(self):
        self.assertEqual(
            treeWidgets.format_point_count({
                "point_shape": [10],
                "expected_results": 20,
                }),
            "20 = 10"
            )

    def test_format_point_count_uses_setpoint_shape_without_measurement_factor(self):
        self.assertEqual(
            treeWidgets.format_point_count({
                "setpoint_shape": [108, 861],
                "setpoint_count": 92_988,
                "point_shape": [108, 861, 2],
                "expected_results": 185_976,
                }),
            "92,988 = 108 × 861"
            )

    def test_running_status_uses_measured_setpoints_not_result_rows(self):
        metadata = {
            "setpoint_shape": [10, 100],
            "setpoint_count": 1000,
            "point_shape": [10, 100],
            "expected_results": 2000,
            "result_count": 1000,
            "read_setpoint_count": 600,
            "is_completed": False,
            }

        self.assertEqual(treeWidgets.format_point_count(metadata), "1,000 = 10 × 100")
        self.assertEqual(
            treeWidgets.format_complete_cell(metadata),
            "Running (60.0%)",
            )

    def test_running_status_can_report_all_planned_setpoints_as_measured(self):
        self.assertEqual(
            treeWidgets.format_complete_cell({
                "expected_results": 100,
                "result_count": 100,
                "setpoint_count": 100,
                "read_setpoint_count": 100,
                "is_completed": False,
                }),
            "Running (100.0%)"
            )

    def test_running_status_uses_result_progress_while_setpoints_load(self):
        self.assertEqual(
            treeWidgets.format_complete_cell({
                "expected_results": 185_976,
                "result_count": 185_976,
                "is_completed": False,
                }),
            "Running (100.0%)",
            )

    def test_interrupted_completed_run_reports_setpoint_progress(self):
        metadata = {
            "completed_timestamp": 12_345.6,
            "is_completed": True,
            "measurement_exception": "Traceback...\nKeyboardInterrupt\n",
            "result_count": 800,
            "read_setpoint_count": 400,
            "setpoint_count": 1000,
            "expected_results": 2000,
            }

        self.assertEqual(
            treeWidgets.format_complete_cell(metadata),
            "Interrupted (40.0%)"
            )
        self.assertEqual(
            treeWidgets.format_run_status(metadata),
            "Interrupted (40.0%)"
            )
        self.assertEqual(treeWidgets.complete_cell_sort_value(metadata), 40.0)

    def test_interrupted_run_can_use_observed_setpoint_total(self):
        metadata = {
            "is_completed": True,
            "measurement_exception": "KeyboardInterrupt",
            "result_count": 5,
            "read_setpoint_count": 5,
            "setpoint_count": 5,
            "setpoint_count_source": "observed",
            "expected_results": 5,
            "expected_results_source": "observed",
            }

        self.assertEqual(
            treeWidgets.format_run_status(metadata),
            "Interrupted (100.0%)",
            )
        self.assertEqual(treeWidgets.complete_cell_sort_value(metadata), 100.0)

    def test_completed_non_keyboard_measurement_exception_is_failed(self):
        metadata = {
            "completed_timestamp": 12_345.6,
            "is_completed": True,
            "measurement_exception": "Traceback...\nValueError: bad value\n",
            "result_count": 40,
            "setpoint_count": 100,
            }

        self.assertEqual(treeWidgets.format_complete_cell(metadata), "Failed")
        self.assertEqual(treeWidgets.format_run_status(metadata), "Failed (40.0%)")
        self.assertEqual(treeWidgets.complete_cell_sort_value(metadata), 40.0)

    def test_completed_run_without_exception_uses_completed_label(self):
        self.assertEqual(
            treeWidgets.format_complete_cell({
                "completed_timestamp": 12_345.6,
                "is_completed": True,
                }),
            "Completed",
            )

    def test_empty_measurement_exceptions_are_not_failures(self):
        for exception in (None, "", " \n\t "):
            with self.subTest(exception=exception):
                metadata = {
                    "completed_timestamp": 12_345.6,
                    "is_completed": True,
                    "measurement_exception": exception,
                    }
                self.assertFalse(treeWidgets.run_failed(metadata))
                self.assertEqual(
                    treeWidgets.format_complete_cell(metadata),
                    "Completed",
                    )

    def test_exception_state_takes_precedence_over_completed_state(self):
        base_metadata = {
            "completed_timestamp": 12_345.6,
            "is_completed": True,
            }

        self.assertEqual(
            treeWidgets.format_complete_cell({
                **base_metadata,
                "measurement_exception": "KeyboardInterrupt",
                }),
            "Interrupted (unknown)",
            )
        self.assertEqual(
            treeWidgets.format_complete_cell({
                **base_metadata,
                "measurement_exception": "ValueError: bad value",
                }),
            "Failed",
            )

    def test_failed_run_tooltip_has_escaped_concise_exception(self):
        metadata = {
            "completed_timestamp": 12_345.6,
            "is_completed": True,
            "measurement_exception": (
                "Traceback (most recent call last):\n"
                "  internal implementation detail\n"
                "ValueError: x < 2 & y > 1\n"
                ),
            "result_count": 40,
            "setpoint_count": 100,
            }

        tooltip = treeWidgets.run_tooltip_text(metadata)
        self.assertIn("Failed (40.0%)", tooltip)
        self.assertIn("ValueError: x &lt; 2 &amp; y &gt; 1", tooltip)
        self.assertNotIn("internal implementation detail", tooltip)

        plain_text = treeWidgets.run_tooltip_plain_text(metadata)
        self.assertIn("Status  Failed (40.0%)", plain_text)
        self.assertIn("Exception ValueError: x < 2 & y > 1", plain_text)
        self.assertNotIn("internal implementation detail", plain_text)

    def test_duration_uses_commas(self):
        self.assertEqual(
            treeWidgets.format_time_taken_seconds({
                "run_timestamp": 100.0,
                "completed_timestamp": 12_345.6,
                "is_completed": True,
                }),
            "12,245.6 s"
            )

    def test_setpoints_delegate_uses_normal_text_color_for_selection(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            delegate = run_list.itemDelegateForColumn(
                run_list.cols.index("Setpoints")
                )
            option = qtw.QStyleOptionViewItem()
            option.widget = run_list
            option.state = qtw.QStyle.StateFlag.State_Selected | qtw.QStyle.StateFlag.State_Enabled

            self.assertEqual(
                delegate._text_color(option),
                option.palette.color(QtGui.QPalette.ColorRole.Text)
                )

            option.state |= qtw.QStyle.StateFlag.State_Active | qtw.QStyle.StateFlag.State_HasFocus
            self.assertEqual(
                delegate._text_color(option),
                option.palette.color(QtGui.QPalette.ColorRole.Text)
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_setpoints_delegate_left_aligns_shape_text(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            delegate = run_list.itemDelegateForColumn(
                run_list.cols.index("Setpoints")
                )

            self.assertEqual(
                int(delegate.right_text_alignment),
                (QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter).value
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_setpoints_delegate_treats_zero_as_left_count_text(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "sweep-guid",
                    "sweep_parameters": ["x", "y"],
                    "measure_parameters": ["signal"],
                    "setpoint_count": 100,
                    "setpoint_shape": [10, 10],
                    "result_count": 100,
                    },
                2: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "empty-guid",
                    "sweep_parameters": [],
                    "measure_parameters": ["signal"],
                    "result_count": 0,
                    },
                })
            delegate = run_list.itemDelegateForColumn(
                run_list.cols.index("Setpoints")
                )
            items = {
                run_list.topLevelItem(row).guid: run_list.topLevelItem(row)
                for row in range(run_list.topLevelItemCount())
                }
            zero_item = items["empty-guid"]
            setpoints_col = run_list.cols.index("Setpoints")
            metrics = QtGui.QFontMetrics(run_list.font())

            self.assertEqual(zero_item.text(setpoints_col), "0")
            self.assertEqual(delegate._display_sections("0"), ("0", None))
            self.assertGreater(
                delegate._max_right_width(
                    run_list.indexFromItem(zero_item, setpoints_col),
                    metrics,
                    ),
                0
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_setpoints_delegate_uses_widest_shape_text_for_equals_alignment(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "small-guid",
                    "sweep_parameters": ["x", "y"],
                    "measure_parameters": ["signal"],
                    "setpoint_count": 100,
                    "setpoint_shape": [10, 10],
                    "result_count": 100,
                    },
                2: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "medium-guid",
                    "sweep_parameters": ["x", "y"],
                    "measure_parameters": ["signal"],
                    "setpoint_count": 10_000,
                    "setpoint_shape": [100, 100],
                    "result_count": 10_000,
                    },
                3: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "large-guid",
                    "sweep_parameters": ["x", "y"],
                    "measure_parameters": ["signal"],
                    "setpoint_count": 1_000_000,
                    "setpoint_shape": [1000, 1000],
                    "result_count": 1_000_000,
                    },
                })
            delegate = run_list.itemDelegateForColumn(
                run_list.cols.index("Setpoints")
                )
            item = run_list.topLevelItem(0)
            setpoints_col = run_list.cols.index("Setpoints")
            metrics = QtGui.QFontMetrics(run_list.font())

            self.assertEqual(
                delegate._max_right_width(
                    run_list.indexFromItem(item, setpoints_col),
                    metrics,
                    ),
                metrics.horizontalAdvance("1,000 × 1,000")
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_setpoints_delegate_reserves_space_for_one_dimensional_count(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "trace-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal"],
                    "setpoint_count": 1000,
                    "setpoint_shape": [1000],
                    "result_count": 1000,
                    },
                })
            delegate = run_list.itemDelegateForColumn(
                run_list.cols.index("Setpoints")
                )
            item = run_list.topLevelItem(0)
            setpoints_col = run_list.cols.index("Setpoints")
            metrics = QtGui.QFontMetrics(run_list.font())

            self.assertEqual(item.text(setpoints_col), "1,000")
            self.assertEqual(
                delegate._max_right_width(
                    run_list.indexFromItem(item, setpoints_col),
                    metrics,
                    ),
                metrics.horizontalAdvance("1,000")
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_resize_columns_keeps_narrow_width_inside_viewport(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.resize(583, 300)
            run_list.show()
            qtw.QApplication.processEvents()
            run_list._resize_columns()

            widths = {
                name: run_list.columnWidth(col)
                for col, name in enumerate(run_list.cols)
                }

            self.assertLessEqual(sum(widths.values()), run_list.viewport().width())
            self.assertGreaterEqual(
                widths["Started"],
                treeWidgets.RunList.readable_column_widths["Started"],
                )
            self.assertGreaterEqual(
                widths["Measurements"],
                treeWidgets.RunList.readable_column_widths["Measurements"],
                )
            run_list.hide()
        finally:
            treeWidgets.isfile = old_isfile

    def test_resize_columns_uses_roomy_main_window_widths(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.resize(780, 300)
            run_list.show()
            qtw.QApplication.processEvents()
            preferred_widths = run_list._preferred_column_widths()
            frame_width = run_list.width() - run_list.viewport().width()
            run_list.resize(sum(preferred_widths.values()) + frame_width, 300)
            qtw.QApplication.processEvents()
            run_list._resize_columns()

            widths = {
                name: run_list.columnWidth(col)
                for col, name in enumerate(run_list.cols)
                }

            for name, width in treeWidgets.RunList.column_widths.items():
                self.assertGreaterEqual(widths[name], width)
            for name, width in treeWidgets.RunList.elastic_column_widths.items():
                self.assertGreaterEqual(widths[name], width)
            self.assertGreater(widths["Setpoints"], widths["Started"])

            metrics = QtGui.QFontMetrics(run_list.font())
            for name, value in run_list.representative_column_values.items():
                self.assertGreaterEqual(
                    widths[name],
                    metrics.horizontalAdvance(value) + 12,
                    )
            run_list.hide()
        finally:
            treeWidgets.isfile = old_isfile

    def test_manual_column_widths_persist_until_reset(self):
        class MemoryConfig:
            def __init__(self):
                self.values = {treeWidgets.RUN_TABLE_COLUMN_WIDTHS_KEY: []}

            def get(self, key):
                return self.values[key]

            def update(self, key, value):
                self.values[key] = value

        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            cfg = MemoryConfig()
            first = treeWidgets.RunList(config=cfg)
            saved_widths = [48, 112, 205, 154, 146, 104, 68]
            for column, width in enumerate(saved_widths):
                first.setColumnWidth(column, width)
            first._persist_column_widths()

            restored = treeWidgets.RunList(config=cfg)
            self.assertTrue(restored._manual_column_widths)
            self.assertEqual(
                [restored.columnWidth(column) for column in range(len(restored.cols))],
                saved_widths,
                )

            self.assertTrue(restored.reset_column_widths())
            self.assertEqual(cfg.values[treeWidgets.RUN_TABLE_COLUMN_WIDTHS_KEY], [])
            self.assertFalse(restored._manual_column_widths)

            defaults = treeWidgets.RunList(config=cfg)
            self.assertFalse(defaults._manual_column_widths)
            self.assertNotEqual(
                [defaults.columnWidth(column) for column in range(len(defaults.cols))],
                saved_widths,
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_resize_columns_preserves_manual_width_until_reset(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.resize(780, 300)
            run_list.show()
            qtw.QApplication.processEvents()
            column = run_list.cols.index("Started")
            manual_width = run_list.columnWidth(column) + 37

            run_list.setColumnWidth(column, manual_width)
            run_list.resize(900, 300)
            qtw.QApplication.processEvents()
            run_list._resize_columns()

            self.assertEqual(run_list.columnWidth(column), manual_width)

            run_list.reset_column_widths()

            self.assertFalse(run_list._manual_column_widths)
            self.assertNotEqual(run_list.columnWidth(column), manual_width)
            run_list.hide()
        finally:
            treeWidgets.isfile = old_isfile

    def test_columns_do_not_shrink_below_declared_minimums(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            widths = run_list._grow_column_widths(
                run_list.minimum_column_widths,
                run_list.readable_column_widths,
                available_width=100,
                order=run_list.compact_growth_order,
                )

            self.assertEqual(widths, run_list.minimum_column_widths)
            self.assertEqual(
                run_list.horizontalScrollBarPolicy(),
                QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded,
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_guid_index_tracks_added_and_cleared_rows(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "indexed-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal"],
                    "result_count": 1,
                    },
                })
            item = run_list.topLevelItem(0)

            self.assertIs(run_list._item_for_guid("indexed-guid"), item)

            run_list.clear()

            self.assertIsNone(run_list._item_for_guid("indexed-guid"))
            self.assertEqual(run_list._items_by_guid, {})
        finally:
            treeWidgets.isfile = old_isfile

    def test_unknown_completion_duration_uses_database_modified_time(self):
        self.assertEqual(
            treeWidgets.format_complete_cell({
                "run_timestamp": 100.0,
                "completed_timestamp": None,
                "is_completed": None,
                "result_count": 185_976,
                "expected_results": None,
                }),
            "unknown"
            )
        self.assertEqual(
            treeWidgets.format_time_taken_seconds({
                "run_timestamp": 100.0,
                "completed_timestamp": None,
                "is_completed": None,
                "result_count": 185_976,
                "expected_results": None,
                "database_modified_timestamp": 12_345.6,
                }),
            "12,245.6 s"
            )

    def test_incomplete_duration_uses_database_modified_time(self):
        self.assertEqual(
            treeWidgets.format_complete_cell({
                "run_timestamp": 100.0,
                "completed_timestamp": None,
                "is_completed": False,
                "result_count": 25,
                "expected_results": 100,
                }),
            "Running (25.0%)"
            )
        self.assertEqual(
            treeWidgets.format_time_taken_seconds({
                "run_timestamp": 100.0,
                "completed_timestamp": None,
                "is_completed": False,
                "result_count": 25,
                "expected_results": 100,
                "database_modified_timestamp": 12_345.6,
                }),
            "12,245.6 s"
            )

    def test_add_runs_only_watches_unfinished_rows(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": None,
                    "is_completed": False,
                    "exp_name": "exp",
                    "sample_name": "sample",
                    "name": "unfinished",
                    "result_table_name": "results_1",
                    "guid": "unfinished-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["y"],
                    "result_count": 1,
                "expected_results": 10,
                "point_shape": [10],
                "setpoint_count": 10,
                "setpoint_count_source": "planned",
                "read_setpoint_count": 1,
                "storage_bytes": 102_400,
                    },
                2: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "exp_name": "exp",
                    "sample_name": "sample",
                    "name": "finished",
                    "result_table_name": "results_2",
                    "guid": "finished-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["z", "w"],
                    "result_count": 10,
                    "expected_results": 1000,
                    "point_shape": [10, 100],
                    "storage_bytes": 1536,
                    },
                })

            self.assertEqual(
                [run_list.headerItem().text(col) for col in range(run_list.columnCount())],
                ["ID", "Measurements", "Setpoints", "Started", "Status", "Duration", "Size"]
                )
            self.assertIsInstance(
                run_list.itemDelegateForColumn(2),
                treeWidgets.EqualsAlignedDelegate
                )
            self.assertFalse(run_list.rootIsDecorated())
            self.assertEqual(run_list.indentation(), 0)
            self.assertEqual(
                run_list.horizontalScrollBarPolicy(),
                QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
                )
            self.assertTrue(
                all(
                    run_list.header().sectionResizeMode(col) == qtw.QHeaderView.ResizeMode.Interactive
                    for col in range(run_list.columnCount())
                    )
                )
            items = {
                run_list.topLevelItem(row).guid: run_list.topLevelItem(row)
                for row in range(run_list.topLevelItemCount())
                }

            self.assertEqual([item.guid for item in run_list.watching], ["unfinished-guid"])
            self.assertEqual(items["unfinished-guid"].text(1), "")
            self.assertEqual(items["unfinished-guid"].data(1, QtCore.Qt.ItemDataRole.UserRole), 1)
            self.assertEqual(
                items["unfinished-guid"].data(
                    1,
                    QtCore.Qt.ItemDataRole.AccessibleTextRole,
                    ),
                "1 measurement: y",
                )
            self.assertIsInstance(
                run_list.itemWidget(items["unfinished-guid"], 1),
                treeWidgets.RunPreviewCell
                )
            self.assertEqual(
                run_list.itemWidget(items["unfinished-guid"], 1).accessibleName(),
                "1 measurement: y",
                )
            self.assertEqual(
                len(
                    run_list.itemWidget(
                        items["unfinished-guid"], 1
                        ).findChildren(qtw.QLabel, "measurementPreviewPlaceholder")
                    ),
                1
                )
            self.assertEqual(items["unfinished-guid"].text(2), "10")
            self.assertEqual(items["unfinished-guid"].text(4), "Running (10.0%)")
            self.assertRegex(items["unfinished-guid"].text(5), r"^[\d,]+\.\d s$")
            self.assertEqual(items["unfinished-guid"].text(6), "100 KB")
            self.assertEqual(items["finished-guid"].text(1), "")
            self.assertEqual(items["finished-guid"].data(1, QtCore.Qt.ItemDataRole.UserRole), 2)
            self.assertEqual(
                items["finished-guid"].data(
                    1,
                    QtCore.Qt.ItemDataRole.AccessibleTextRole,
                    ),
                "2 measurements: z, w",
                )
            self.assertEqual(
                len(
                    run_list.itemWidget(
                        items["finished-guid"], 1
                        ).findChildren(qtw.QLabel, "measurementPreviewPlaceholder")
                    ),
                2
                )
            self.assertEqual(items["finished-guid"].text(2), "1,000 = 10 × 100")
            self.assertEqual(items["finished-guid"].text(4), "Completed")
            self.assertEqual(items["finished-guid"].text(5), "10.0 s")
            self.assertEqual(
                int(items["finished-guid"].textAlignment(0)),
                (QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter).value
                )
            self.assertEqual(
                int(items["finished-guid"].textAlignment(2)),
                (QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter).value
                )
            self.assertEqual(
                int(items["finished-guid"].textAlignment(4)),
                QtCore.Qt.AlignmentFlag.AlignCenter.value
                )
            self.assertEqual(
                int(items["finished-guid"].textAlignment(5)),
                (QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter).value
                )
            self.assertEqual(
                int(items["finished-guid"].textAlignment(6)),
                (QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter).value
                )
            self.assertIn("Measure</td>", items["unfinished-guid"].toolTip(0))
            self.assertIn("(y)</td>", items["unfinished-guid"].toolTip(0))
            self.assertIn("Status</td>", items["finished-guid"].toolTip(0))
            self.assertIn("Completed</td>", items["finished-guid"].toolTip(0))

            run_list.sortItems(1, QtCore.Qt.SortOrder.DescendingOrder)
            self.assertEqual(
                [run_list.topLevelItem(row).guid for row in range(run_list.topLevelItemCount())],
                ["finished-guid", "unfinished-guid"]
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_update_runs_merges_background_detail_metadata(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": None,
                    "is_completed": False,
                    "guid": "run-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal"],
                    },
                })

            item = run_list.topLevelItem(0)
            updated = run_list.updateRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "run-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal"],
                    "result_count": 10,
                    "setpoint_count": 10,
                    "setpoint_shape": [10],
                    "storage_bytes": 2048,
                    },
                })

            self.assertEqual(run_list.topLevelItemCount(), 1)
            self.assertIs(run_list.topLevelItem(0), item)
            self.assertEqual(updated[1]["result_count"], 10)
            self.assertEqual(item.text(run_list.cols.index("Setpoints")), "10")
            self.assertEqual(item.text(run_list.cols.index("Status")), "Completed")
            self.assertEqual(item.text(run_list.cols.index("Duration")), "10.0 s")
            self.assertEqual(item.text(run_list.cols.index("Size")), "2.0 KB")
            self.assertEqual(run_list.watching, [])
        finally:
            treeWidgets.isfile = old_isfile

    def test_update_runs_does_not_replace_exact_size_with_later_estimate(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "run-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal"],
                    },
                })

            run_list.updateRuns({
                1: {
                    "guid": "run-guid",
                    "storage_bytes": 4096,
                    "storage_bytes_estimated": False,
                    },
                })
            updated = run_list.updateRuns({
                1: {
                    "guid": "run-guid",
                    "result_count": 10,
                    "storage_bytes": 1024,
                    "storage_bytes_estimated": True,
                    },
                })

            item = run_list.topLevelItem(0)
            self.assertEqual(updated[1]["storage_bytes"], 4096)
            self.assertFalse(updated[1]["storage_bytes_estimated"])
            self.assertEqual(item.text(run_list.cols.index("Size")), "4.0 KB")
        finally:
            treeWidgets.isfile = old_isfile

    def test_update_runs_replaces_prompt_estimate_with_later_exact_size(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "run-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal"],
                    },
                })

            estimated = run_list.updateRuns({
                1: {
                    "guid": "run-guid",
                    "storage_bytes": 1024,
                    "storage_bytes_estimated": True,
                    },
                })
            exact = run_list.updateRuns({
                1: {
                    "guid": "run-guid",
                    "storage_bytes": 4096,
                    "storage_bytes_estimated": False,
                    },
                })

            item = run_list.topLevelItem(0)
            self.assertEqual(estimated[1]["storage_bytes"], 1024)
            self.assertTrue(estimated[1]["storage_bytes_estimated"])
            self.assertEqual(exact[1]["storage_bytes"], 4096)
            self.assertFalse(exact[1]["storage_bytes_estimated"])
            self.assertEqual(item.text(run_list.cols.index("Size")), "4.0 KB")
        finally:
            treeWidgets.isfile = old_isfile

    def test_check_watching_reports_finished_interrupted_run(self):
        old_isfile = treeWidgets.isfile
        old_get_run_status = treeWidgets.get_run_status
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": None,
                    "is_completed": False,
                    "exp_name": "exp",
                    "sample_name": "sample",
                    "name": "interrupted",
                    "result_table_name": "results_1",
                    "guid": "interrupted-guid",
                    "sweep_parameters": ["x", "y"],
                    "measure_parameters": ["signal", "other"],
                    "result_count": 100,
                    "read_setpoint_count": 100,
                    "setpoint_count": 1000,
                    "expected_results": 2000,
                    "point_shape": [10, 100],
                    "setpoint_shape": [10, 100],
                    }
                })
            item = run_list.topLevelItem(0)

            treeWidgets.get_run_status = lambda guid: {
                "completed_timestamp": 120.0,
                "is_completed": True,
                "result_count": 800,
                "read_setpoint_count": 400,
                "measurement_exception": "Traceback...\nKeyboardInterrupt\n",
                "database_modified_timestamp": 120.0,
                }

            updated_runs = run_list.checkWatching()

            self.assertEqual(
                item.text(run_list.cols.index("Status")),
                "Interrupted (40.0%)"
                )
            self.assertEqual(
                item.data(run_list.cols.index("Status"), QtCore.Qt.ItemDataRole.UserRole),
                40.0
                )
            self.assertEqual(run_list.watching, [])
            self.assertEqual(
                updated_runs[1]["measurement_exception"],
                "Traceback...\nKeyboardInterrupt\n"
                )
        finally:
            treeWidgets.isfile = old_isfile
            treeWidgets.get_run_status = old_get_run_status

    def test_check_watching_replaces_stale_observed_live_shape(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": None,
                    "is_completed": False,
                    "guid": "growing-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal"],
                    "result_count": 1,
                    "point_shape": [1],
                    "setpoint_shape": [1],
                    "setpoint_shape_source": "observed",
                    "setpoint_count": 1,
                    "setpoint_count_source": "observed",
                    "expected_results": 1,
                    "expected_results_source": "observed",
                    },
                })
            item = run_list.topLevelItem(0)

            run_list.checkWatching({
                "growing-guid": {
                    "is_completed": False,
                    "result_count": 5,
                    "read_setpoint_count": 5,
                    "point_shape": [5],
                    "setpoint_shape": [5],
                    "setpoint_shape_source": "observed",
                    "setpoint_count": 5,
                    "setpoint_count_source": "observed",
                    "expected_results": None,
                    "expected_results_source": None,
                    },
                })

            self.assertEqual(item.text(run_list.cols.index("Setpoints")), "5")
            self.assertEqual(
                item.text(run_list.cols.index("Status")),
                "Running (100.0%)",
                )
            self.assertIsNone(item.run_metadata["expected_results"])

            run_list.checkWatching({
                "growing-guid": {
                    "completed_timestamp": 120.0,
                    "is_completed": True,
                    "result_count": 5,
                    "point_shape": [5],
                    "setpoint_shape": [5],
                    "setpoint_shape_source": "observed",
                    "setpoint_count": 5,
                    "setpoint_count_source": "observed",
                    "expected_results": 5,
                    "expected_results_source": "observed",
                    },
                })

            self.assertEqual(item.text(run_list.cols.index("Setpoints")), "5")
            self.assertEqual(item.text(run_list.cols.index("Status")), "Completed")
            self.assertEqual(item.run_metadata["expected_results"], 5)
            self.assertEqual(run_list.watching, [])
        finally:
            treeWidgets.isfile = old_isfile

    def test_check_watching_updates_finished_failed_run(self):
        old_isfile = treeWidgets.isfile
        old_get_run_status = treeWidgets.get_run_status
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": None,
                    "is_completed": False,
                    "guid": "failed-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal"],
                    "result_count": 10,
                    "setpoint_count": 100,
                    "expected_results": 100,
                    }
                })
            item = run_list.topLevelItem(0)

            treeWidgets.get_run_status = lambda guid: {
                "completed_timestamp": 120.0,
                "is_completed": True,
                "result_count": 40,
                "measurement_exception": "Traceback...\nValueError: bad <value>\n",
                "database_modified_timestamp": 120.0,
                }

            updated_runs = run_list.checkWatching()

            status_col = run_list.cols.index("Status")
            self.assertEqual(item.text(status_col), "Failed")
            self.assertEqual(
                item.data(status_col, QtCore.Qt.ItemDataRole.UserRole),
                40.0,
                )
            self.assertEqual(run_list.watching, [])
            self.assertEqual(
                updated_runs[1]["measurement_exception"],
                "Traceback...\nValueError: bad <value>\n",
                )
            self.assertIn("Status</td>", item.toolTip(0))
            self.assertIn("Failed (40.0%)", item.toolTip(0))
            self.assertIn("ValueError: bad &lt;value&gt;", item.toolTip(0))
        finally:
            treeWidgets.isfile = old_isfile
            treeWidgets.get_run_status = old_get_run_status

    def test_check_watching_stops_completed_run_without_completion_timestamp(self):
        old_isfile = treeWidgets.isfile
        old_get_run_status = treeWidgets.get_run_status
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": None,
                    "is_completed": False,
                    "guid": "completed-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal"],
                    "result_count": 10,
                    "setpoint_count": 100,
                    "expected_results": 100,
                    }
                })
            item = run_list.topLevelItem(0)

            treeWidgets.get_run_status = lambda guid: {
                "completed_timestamp": None,
                "is_completed": True,
                "result_count": 100,
                "database_modified_timestamp": 120.0,
                }

            updated_runs = run_list.checkWatching()

            self.assertEqual(run_list.watching, [])
            self.assertTrue(updated_runs[1]["is_completed"])
            self.assertIsNone(updated_runs[1]["completed_timestamp"])
            self.assertEqual(
                item.text(run_list.cols.index("Status")),
                "Completed",
                )
            self.assertEqual(
                item.text(run_list.cols.index("Duration")),
                "unknown",
                )
            self.assertIn("Completed</td>", item.toolTip(0))
        finally:
            treeWidgets.isfile = old_isfile
            treeWidgets.get_run_status = old_get_run_status

    def test_run_table_measurement_previews_use_preview_metadata(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            requested = []
            run_list.previewPlotRequested.connect(
                lambda guid, parameter: requested.append((guid, parameter))
                )
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "result_table_name": "results_1",
                    "guid": "run-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal", "other"],
                    "result_count": 2,
                    "expected_results": 2,
                    "storage_bytes": 2048,
                    },
                })

            item = run_list.topLevelItem(0)
            cell = run_list.itemWidget(item, 1)
            self.assertEqual(
                len(cell.findChildren(qtw.QLabel, "measurementPreviewPlaceholder")),
                2
                )

            run_list.set_run_previews("run-guid", [{
                "parameter": "signal",
                "axes": ["x"],
                "title": "signal vs x",
                "image": render_sparkline_preview(
                    np.array([0, 1], dtype=float),
                    np.array([1, 2], dtype=float),
                    size=40,
                    ),
                }])

            images = cell.findChildren(qtw.QLabel, "measurementPreviewImage")
            placeholders = cell.findChildren(qtw.QLabel, "measurementPreviewPlaceholder")
            self.assertEqual(len(images), 1)
            self.assertEqual(len(placeholders), 1)
            self.assertIsInstance(images[0], DraggablePreviewImageLabel)
            self.assertEqual(images[0].guid, "run-guid")
            self.assertEqual(images[0].parameter, "signal")
            self.assertEqual(images[0].axes, ["x"])
            self.assertEqual(images[0].toolTip(), "signal vs x")
            self.assertEqual(images[0].width(), treeWidgets.MEASUREMENT_PREVIEW_SIZE)
            self.assertEqual(images[0].height(), treeWidgets.MEASUREMENT_PREVIEW_SIZE)

            event = QtGui.QMouseEvent(
                QtCore.QEvent.Type.MouseButtonDblClick,
                QtCore.QPointF(5, 5),
                QtCore.Qt.MouseButton.LeftButton,
                QtCore.Qt.MouseButton.LeftButton,
                QtCore.Qt.KeyboardModifier.NoModifier,
                )
            qtw.QApplication.sendEvent(images[0], event)

            self.assertEqual(requested, [("run-guid", "signal")])
            self.assertIs(run_list.currentItem(), item)

            export_requested = []
            run_list.previewExportRequested.connect(
                lambda guid, parameter: export_requested.append((guid, parameter))
                )
            images[0].exportRequested.emit("signal")

            self.assertEqual(export_requested, [("run-guid", "signal")])
            self.assertIs(run_list.currentItem(), item)

            run_list.set_run_previews("run-guid", [{
                "parameter": "signal",
                "axes": ["x", "y", "z"],
                "dimension_count": 3,
                "title": "signal has 3 independent axes",
                "unsupported": True,
                }])
            unsupported = cell.findChildren(
                qtw.QLabel,
                "measurementPreviewUnsupported",
                )
            self.assertEqual(len(unsupported), 1)
            self.assertEqual(unsupported[0].text(), "3D")
            self.assertEqual(
                unsupported[0].accessibleName(),
                "3D measurement unsupported",
                )
            self.assertIn("3 independent axes", unsupported[0].toolTip())
        finally:
            treeWidgets.isfile = old_isfile

    def test_run_table_placeholders_use_subtle_tints_while_generating(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "result_table_name": "results_1",
                    "guid": "run-guid",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["a", "b", "c"],
                    "result_count": 2,
                    "expected_results": 2,
                    "storage_bytes": 2048,
                    },
                })

            item = run_list.topLevelItem(0)
            cell = run_list.itemWidget(item, 1)

            run_list.set_run_preview_generating("run-guid", True)
            placeholders = cell.findChildren(qtw.QLabel, "measurementPreviewPlaceholder")
            styles = [placeholder.styleSheet() for placeholder in placeholders]

            self.assertEqual(len(placeholders), 3)
            self.assertTrue(all("background-color" in style for style in styles))
            self.assertGreater(len(set(styles)), 1)

            run_list.set_run_preview_generating("run-guid", False)

            self.assertTrue(
                all(
                    placeholder.styleSheet() == ""
                    for placeholder in cell.findChildren(qtw.QLabel, "measurementPreviewPlaceholder")
                    )
                )
        finally:
            treeWidgets.isfile = old_isfile

    def test_large_run_list_uses_compact_cells_instead_of_widgets_per_run(self):
        old_isfile = treeWidgets.isfile
        old_limit = treeWidgets.MAX_RUN_PREVIEW_WIDGETS
        treeWidgets.isfile = lambda _: False
        treeWidgets.MAX_RUN_PREVIEW_WIDGETS = 2

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                run_id: {
                    "run_timestamp": 100.0 + run_id,
                    "completed_timestamp": 110.0 + run_id,
                    "is_completed": True,
                    "guid": f"guid-{run_id}",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal"],
                    "result_count": 10,
                    }
                for run_id in range(1, 4)
                })

            self.assertFalse(run_list._preview_widgets_enabled)
            self.assertEqual(run_list.preview_cells, {})
            self.assertTrue(run_list.uniformRowHeights())
            for row in range(run_list.topLevelItemCount()):
                item = run_list.topLevelItem(row)
                self.assertIsNone(run_list.itemWidget(item, 1))
                self.assertEqual(item.text(1), "1")
        finally:
            treeWidgets.MAX_RUN_PREVIEW_WIDGETS = old_limit
            treeWidgets.isfile = old_isfile

    def test_setpoint_width_scan_is_cached_until_run_data_changes(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": 110.0,
                    "is_completed": True,
                    "guid": "guid-1",
                    "sweep_parameters": ["x", "y"],
                    "measure_parameters": ["signal"],
                    "setpoint_count": 100,
                    "setpoint_shape": [10, 10],
                    "result_count": 100,
                    },
                })
            item = run_list.topLevelItem(0)
            column = run_list.cols.index("Setpoints")
            index = run_list.indexFromItem(item, column)
            delegate = run_list.itemDelegateForColumn(column)
            metrics = QtGui.QFontMetrics(run_list.font())

            first_width = delegate._max_right_width(index, metrics)
            item.setText(column, "100 = 1000000 × 1000000")
            cached_width = delegate._max_right_width(index, metrics)
            delegate.invalidate_width_cache()
            refreshed_width = delegate._max_right_width(index, metrics)

            self.assertEqual(cached_width, first_width)
            self.assertGreater(refreshed_width, first_width)
        finally:
            treeWidgets.isfile = old_isfile

    def test_detail_batches_do_not_resort_when_sort_key_is_unchanged(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        class RecordingRunList(treeWidgets.RunList):
            def __init__(self):
                self.sorting_changes = []
                super().__init__()

            def setSortingEnabled(self, enabled):
                self.sorting_changes.append(enabled)
                super().setSortingEnabled(enabled)

        try:
            run_list = RecordingRunList()
            run_list.addRuns({
                1: {
                    "run_timestamp": 100.0,
                    "completed_timestamp": None,
                    "is_completed": False,
                    "guid": "guid-1",
                    "sweep_parameters": ["x"],
                    "measure_parameters": ["signal"],
                    "result_count": 1,
                    },
                })
            run_list.sortItems(0, QtCore.Qt.SortOrder.DescendingOrder)
            run_list.sorting_changes.clear()

            run_list.updateRuns({
                1: {
                    "guid": "guid-1",
                    "result_count": 2,
                    },
                })

            self.assertEqual(run_list.sorting_changes, [])
            self.assertTrue(run_list.isSortingEnabled())
        finally:
            treeWidgets.isfile = old_isfile

    def test_large_selected_run_skips_full_table_setpoint_grouping(self):
        details = treeWidgets.moreInfo(preview_size=100)
        sql_calls = []
        details._setpoint_summaries_from_sql = (
            lambda *args: sql_calls.append(args) or {"gate": {"steps": 5}}
            )

        summaries = details._setpoint_summaries(
            None,
            ["gate"],
            run_metadata={
                "result_table_name": "results",
                "result_count": (
                    treeWidgets.MAX_SYNCHRONOUS_SETPOINT_SUMMARY_ROWS + 1
                    ),
                "setpoint_shape": [1000],
                },
            database_path="large.db",
            )

        self.assertEqual(sql_calls, [])
        self.assertEqual(summaries, {"gate": {"steps": 1000}})
        details.deleteLater()

    def test_unknown_selected_run_size_skips_full_table_setpoint_grouping(self):
        details = treeWidgets.moreInfo(preview_size=100)
        sql_calls = []
        details._setpoint_summaries_from_sql = (
            lambda *args: sql_calls.append(args) or {"gate": {"steps": 5}}
            )

        summaries = details._setpoint_summaries(
            None,
            ["gate"],
            run_metadata={
                "result_table_name": "results",
                "setpoint_shape": [1000],
                },
            database_path="large.db",
            )

        self.assertEqual(sql_calls, [])
        self.assertEqual(summaries, {"gate": {"steps": 1000}})
        details.deleteLater()

    def test_selected_run_setpoint_summary_query_is_cached(self):
        details = treeWidgets.moreInfo(preview_size=100)
        sql_calls = []
        details._setpoint_summaries_from_sql = (
            lambda *args: sql_calls.append(args) or {"gate": {"steps": 5}}
            )
        metadata = {
            "result_table_name": "results",
            "result_count": 50,
            }

        first = details._setpoint_summaries(
            None,
            ["gate"],
            run_metadata=metadata,
            database_path="small.db",
            )
        second = details._setpoint_summaries(
            None,
            ["gate"],
            run_metadata=metadata,
            database_path="small.db",
            )

        self.assertEqual(first, second)
        self.assertEqual(len(sql_calls), 1)
        details.deleteLater()
