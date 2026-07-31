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
    def test_add_runs_advances_run_id_cursor_past_missing_timestamp(self):
        old_isfile = treeWidgets.isfile
        treeWidgets.isfile = lambda _: False

        try:
            run_list = treeWidgets.RunList()
            run_list.addRuns({
                2: {"run_timestamp": None},
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
            self.assertEqual(run_list.topLevelItemCount(), 1)
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
        self.assertIn("Incomplete (25.0%)</td>", tooltip)
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

    def test_progress_uses_measured_row_count_while_setpoints_use_setpoint_count(self):
        metadata = {
            "setpoint_shape": [10, 100],
            "setpoint_count": 1000,
            "point_shape": [10, 100],
            "expected_results": 2000,
            "result_count": 1000,
            "is_completed": False,
            }

        self.assertEqual(treeWidgets.format_point_count(metadata), "1,000 = 10 × 100")
        self.assertEqual(treeWidgets.format_complete_cell(metadata), "50.0%")

    def test_incomplete_progress_never_formats_as_one_hundred_percent(self):
        self.assertEqual(
            treeWidgets.format_complete_cell({
                "expected_results": 100,
                "result_count": 100,
                "is_completed": False,
                }),
            "99.9%"
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
            "Interrupted"
            )
        self.assertEqual(
            treeWidgets.format_run_status(metadata),
            "Interrupted (40.00%)"
            )
        self.assertEqual(treeWidgets.complete_cell_sort_value(metadata), 40.0)

    def test_completed_non_keyboard_measurement_exception_is_failed(self):
        metadata = {
            "completed_timestamp": 12_345.6,
            "is_completed": True,
            "measurement_exception": "Traceback...\nValueError: bad value\n",
            "result_count": 40,
            "setpoint_count": 100,
            }

        self.assertEqual(treeWidgets.format_complete_cell(metadata), "Failed")
        self.assertEqual(treeWidgets.format_run_status(metadata), "Failed (40.00%)")
        self.assertEqual(treeWidgets.complete_cell_sort_value(metadata), 40.0)

    def test_completed_run_without_exception_still_uses_tick(self):
        self.assertEqual(
            treeWidgets.format_complete_cell({
                "completed_timestamp": 12_345.6,
                "is_completed": True,
                }),
            "✓",
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
                self.assertEqual(treeWidgets.format_complete_cell(metadata), "✓")

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
            "Interrupted",
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
        self.assertIn("Failed (40.00%)", tooltip)
        self.assertIn("ValueError: x &lt; 2 &amp; y &gt; 1", tooltip)
        self.assertNotIn("internal implementation detail", tooltip)

        plain_text = treeWidgets.run_tooltip_plain_text(metadata)
        self.assertIn("Status  Failed (40.00%)", plain_text)
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
            run_list.hide()
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
            "25.0%"
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
                QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
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
            self.assertIsInstance(
                run_list.itemWidget(items["unfinished-guid"], 1),
                treeWidgets.RunPreviewCell
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
            self.assertEqual(items["unfinished-guid"].text(4), "10.0%")
            self.assertRegex(items["unfinished-guid"].text(5), r"^[\d,]+\.\d s$")
            self.assertEqual(items["unfinished-guid"].text(6), "100 KB")
            self.assertEqual(items["finished-guid"].text(1), "")
            self.assertEqual(items["finished-guid"].data(1, QtCore.Qt.ItemDataRole.UserRole), 2)
            self.assertEqual(
                len(
                    run_list.itemWidget(
                        items["finished-guid"], 1
                        ).findChildren(qtw.QLabel, "measurementPreviewPlaceholder")
                    ),
                2
                )
            self.assertEqual(items["finished-guid"].text(2), "1,000 = 10 × 100")
            self.assertEqual(items["finished-guid"].text(4), "✓")
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
            self.assertIn("Complete</td>", items["finished-guid"].toolTip(0))

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
            self.assertEqual(item.text(run_list.cols.index("Status")), "✓")
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
                "Interrupted"
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
            self.assertIn("Failed (40.00%)", item.toolTip(0))
            self.assertIn("ValueError: bad &lt;value&gt;", item.toolTip(0))
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
