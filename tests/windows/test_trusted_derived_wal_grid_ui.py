"""Real-WAL MainWindow acceptance for a partial two-dependent grid."""

from __future__ import annotations

from functools import wraps
from pathlib import Path
from unittest.mock import patch

import pytest
from PyQt6 import QtGui, QtWidgets

from qplot.datahandling import trusted_work_coordinator as coordinator_module
from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
from qplot.windows import _database_actions as database_actions
from qplot.windows import main as main_window
from qplot.windows._widgets import preview as preview_module
from qplot.windows._widgets import treeWidgets as tree_widgets
from tests._window_lifecycle import close_main_window
from tests.datahandling.test_trusted_live import (
    _assert_protected_artifacts_unchanged,
    _stable_artifact_state,
)
from tests.windows._trusted_derived_wal_ui import (
    _prepare_live_database,
    _process_until,
    _start_partial_two_dependent_run,
)

pytestmark = pytest.mark.timeout(180)


def _tree_keys(tree: QtWidgets.QTreeWidget) -> set[str]:
    keys: set[str] = set()
    pending = [tree.topLevelItem(index) for index in range(tree.topLevelItemCount())]
    while pending:
        item = pending.pop()
        if item is None:
            continue
        keys.add(item.text(0))
        pending.extend(item.child(index) for index in range(item.childCount()))
    return keys


def _overview_value(window, key: str) -> str | None:
    table = window.infoBox.overview
    for row in range(table.rowCount()):
        field = table.item(row, 0)
        value = table.item(row, 1)
        if field is not None and field.text() == key:
            return None if value is None else value.text()
    return None


def _rgba(image: QtGui.QImage, x: int, y: int) -> tuple[int, int, int, int]:
    return image.pixelColor(x, y).getRgb()


def test_real_wal_partial_two_dependent_grid_populates_detail_before_images(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Exercise the reported current-QCoDeS live layout through MainWindow."""

    slow_count = 108
    fast_count = 861
    partial_fast_count = 437
    planned_count = slow_count * fast_count
    partial_count = (slow_count - 1) * fast_count + partial_fast_count
    physical_partial_count = partial_count * 2
    database_directory = tmp_path / "partial-two-dependent"
    database_directory.mkdir()
    database_path = database_directory / "live.db"
    writer = _prepare_live_database(database_path, "partial")
    live_run = _start_partial_two_dependent_run(
        writer,
        "stage5c_partial",
        slow_count=slow_count,
        fast_count=fast_count,
        acquired_count=partial_count,
    )
    slow, fast = live_run[2], live_run[3]
    signal, signal_b = live_run[4], live_run[5]
    context, datasaver, dataset = live_run[6], live_run[7], live_run[8]
    guid = str(dataset.guid)
    protected_before_reader = _stable_artifact_state(
        database_path,
        consecutive_observations=2,
        observation_interval=0.02,
    )

    qplot_home = tmp_path / ".qplot-partial"
    cache_root = tmp_path / "partial-derived-cache"
    monkeypatch.setattr(main_window.config, "default_path", str(qplot_home))
    monkeypatch.setattr(
        main_window.config,
        "default_file",
        str(qplot_home / main_window.config.config_file_name),
    )
    monkeypatch.setattr(
        coordinator_module,
        "TrustedDerivedDiskCache",
        lambda **_kwargs: TrustedDerivedDiskCache(cache_root),
    )

    events: list[tuple[str, object]] = []
    original_update_runs = tree_widgets.RunList.updateRuns
    original_detail = tree_widgets.moreInfo.set_snapshot_run_detail
    original_thumbnail = tree_widgets.RunList.set_run_previews
    original_preview = preview_module.PreviewTab.publish_trusted_previews

    def record_update_runs(widget, runs, *args, **kwargs):
        result = original_update_runs(widget, runs, *args, **kwargs)
        for run in runs.values():
            if str(run.get("guid") or "") != guid:
                continue
            acquired = run.get("read_setpoint_count")
            shape = tuple(run.get("setpoint_shape") or ())
            if acquired in {partial_count, planned_count} and shape == (
                slow_count,
                fast_count,
            ):
                events.append((f"exact-{acquired}", dict(run)))
        return result

    def record_detail(widget, detail):
        result = original_detail(widget, detail)
        if str(detail.run.as_dict().get("guid") or "") == guid:
            events.append(("detail", detail))
        return result

    # Preserve Qt's slot signature if this creates the first RunList instance.
    @wraps(original_thumbnail)
    def record_thumbnail(widget, published_guid, previews):
        result = original_thumbnail(widget, published_guid, previews)
        if str(published_guid or "") == guid:
            events.append(("thumbnail", tuple(previews or ())))
        return result

    def record_preview(widget, published_guid, previews, *, error=None):
        result = original_preview(
            widget,
            published_guid,
            previews,
            error=error,
        )
        if str(published_guid or "") == guid:
            events.append(("preview", tuple(previews or ())))
        return result

    monkeypatch.setattr(tree_widgets.RunList, "updateRuns", record_update_runs)
    monkeypatch.setattr(
        tree_widgets.moreInfo,
        "set_snapshot_run_detail",
        record_detail,
    )
    monkeypatch.setattr(
        tree_widgets.RunList,
        "set_run_previews",
        record_thumbnail,
    )
    monkeypatch.setattr(
        preview_module.PreviewTab,
        "publish_trusted_previews",
        record_preview,
    )

    errors = []
    logged_errors = []
    window = None
    context_open = True
    try:
        with (
            patch.object(
                main_window.MainWindow,
                "show_error",
                side_effect=lambda _owner, title, message, details=None: errors.append(
                    (title, message, details)
                ),
            ),
            patch.object(
                database_actions,
                "log_exception",
                side_effect=lambda label, error, *_args, **_kwargs: (
                    logged_errors.append((label, error))
                ),
            ),
            patch.object(
                database_actions,
                "DatabaseDetailWorker",
                wraps=database_actions.DatabaseDetailWorker,
            ) as legacy_cheap,
            patch.object(
                database_actions,
                "DatabaseExpensiveDetailWorker",
                wraps=database_actions.DatabaseExpensiveDetailWorker,
            ) as legacy_expensive,
            patch.object(
                database_actions,
                "DatabaseSelectedRunWorker",
                wraps=database_actions.DatabaseSelectedRunWorker,
            ) as legacy_selected,
            patch.object(
                preview_module,
                "PreviewWorker",
                wraps=preview_module.PreviewWorker,
            ) as legacy_preview,
        ):
            window = main_window.MainWindow()
            window.startupDatabaseTimer.stop()
            window.monitor.stop()
            window.config.config["user_preference"]["confirm_close"] = False
            window.config.config["user_preference"]["confirm_close_all"] = False

            window.close_database(status=False)
            assert window.load_database_path(str(database_path))
            _process_until(lambda: not window._database_load_active)
            assert window._database_access_mode == database_actions.TRUSTED_LIVE_MODE, (
                window._database_fallback_reason,
                errors,
            )
            assert window.RunList.topLevelItemCount() == 2
            item = window.RunList._item_for_guid(guid)
            assert item is not None
            # The committed basic table is visible before the 46-page exact
            # prefix verification has produced any derived image.
            assert not any(name in {"thumbnail", "preview"} for name, _ in events)
            assert item.run_metadata.get("read_setpoint_count") != partial_count

            window.RunList.clearSelection()
            window.RunList.setCurrentItem(item)
            item.setSelected(True)
            _process_until(lambda: window._selected_run_guid == guid)
            _process_until(lambda: any(name == "detail" for name, _ in events))
            _process_until(
                lambda: item.run_metadata.get("read_setpoint_count") == partial_count,
                timeout=120.0,
            )
            _process_until(
                lambda: (
                    window.RunList.run_preview_is_ready(guid)
                    and guid in window.infoBox.preview.cache
                    and len(window.infoBox.preview.cache[guid]) == 2
                ),
                timeout=120.0,
            )

            assert item.run_metadata["result_count"] == physical_partial_count
            assert item.run_metadata["setpoint_shape"] == [slow_count, fast_count]
            assert item.run_metadata["setpoint_shape_source"] == "planned"
            assert item.run_metadata["setpoint_count"] == planned_count
            assert item.run_metadata["read_setpoint_count"] == partial_count
            setpoints_column = window.RunList.cols.index("Setpoints")
            status_column = window.RunList.cols.index("Status")
            assert item.text(setpoints_column) == ("92,564 / 92,988 = 108 × 861")
            assert item.text(status_column) == "Running (99.5%)"
            assert _overview_value(window, "Data points") == (
                "92,564 / 92,988 = 108 × 861"
            )

            event_names = [name for name, _payload in events]
            detail_index = event_names.index("detail")
            exact_index = event_names.index(f"exact-{partial_count}")
            thumbnail_index = event_names.index("thumbnail")
            preview_index = event_names.index("preview")
            assert detail_index < min(thumbnail_index, preview_index)
            assert exact_index < min(thumbnail_index, preview_index)
            assert event_names.count("thumbnail") == 1
            assert event_names.count("preview") == 1

            raw_keys = _tree_keys(window.infoBox.raw)
            assert {
                "Run",
                "Metadata",
                "Snapshot",
                "Parameters",
                "Setpoint summaries",
                "Unavailable fields",
            } <= raw_keys
            assert "stage5c_operator" in _tree_keys(window.infoBox.metadata)
            snapshot_keys = _tree_keys(window.infoBox.snapshot)
            assert snapshot_keys
            assert "Snapshot unavailable" not in snapshot_keys
            assert window.infoBox.preview.current_guid == guid

            neutral = (230, 230, 230, 255)
            initial_previews = window.infoBox.preview.cache[guid]
            previews_by_parameter = {
                str(preview["parameter"]): preview for preview in initial_previews
            }
            assert set(previews_by_parameter) == {signal.name, signal_b.name}
            for preview in previews_by_parameter.values():
                image = preview["image"]
                assert isinstance(image, QtGui.QImage)
                assert _rgba(image, 0, 0) != neutral
                assert _rgba(image, image.width() - 1, 0) == neutral
                upper_left = _rgba(image, 0, 0)
                lower_right = _rgba(image, image.width() - 1, image.height() - 1)
                assert sum(upper_left[:3]) > sum(lower_right[:3])

            coordinator = window._trusted_derived_bridge.coordinator
            assert coordinator is not None
            _process_until(
                lambda: (
                    not coordinator.active and coordinator.snapshot().pending_count == 0
                ),
                timeout=120.0,
            )
            protected_after_reader = _stable_artifact_state(
                database_path,
                consecutive_observations=2,
                observation_interval=0.02,
            )
            _assert_protected_artifacts_unchanged(
                protected_before_reader,
                protected_after_reader,
            )

            append_event_index = len(events)
            for fast_index in range(partial_fast_count, fast_count):
                datasaver.add_result(
                    (slow, float(slow_count - 1)),
                    (fast, float(fast_index)),
                    (
                        signal,
                        float((slow_count - 1) * 1_000 + fast_index),
                    ),
                    (
                        signal_b,
                        float(1_000_000 + (slow_count - 1) * 1_000 + fast_index),
                    ),
                )
            datasaver.flush_data_to_database(block=True)
            protected_before_append_reader = _stable_artifact_state(
                database_path,
                consecutive_observations=2,
                observation_interval=0.02,
            )
            window.refreshMain()
            _process_until(
                lambda: item.run_metadata.get("read_setpoint_count") == planned_count,
                timeout=120.0,
            )
            _process_until(
                lambda: (
                    guid in window.infoBox.preview.cache
                    and window.infoBox.preview.cache[guid] is not initial_previews
                    and len(window.infoBox.preview.cache[guid]) == 2
                ),
                timeout=120.0,
            )

            assert item.run_metadata["result_count"] == planned_count * 2
            assert item.run_metadata["read_setpoint_count"] == planned_count
            assert item.text(setpoints_column) == "92,988 = 108 × 861"
            assert _overview_value(window, "Data points") == ("92,988 = 108 × 861")
            appended_names = [name for name, _payload in events[append_event_index:]]
            assert appended_names.index(f"exact-{planned_count}") < min(
                appended_names.index("thumbnail"),
                appended_names.index("preview"),
            )
            assert "detail" in appended_names
            assert appended_names.index("detail") < min(
                appended_names.index("thumbnail"),
                appended_names.index("preview"),
            )
            updated_previews = window.infoBox.preview.cache[guid]
            for preview in updated_previews:
                image = preview["image"]
                assert isinstance(image, QtGui.QImage)
                assert _rgba(image, 0, 0) != neutral
                assert _rgba(image, image.width() - 1, 0) != neutral
                assert sum(_rgba(image, 0, 0)[:3]) > sum(
                    _rgba(image, image.width() - 1, image.height() - 1)[:3]
                )

            _process_until(
                lambda: (
                    not coordinator.active and coordinator.snapshot().pending_count == 0
                ),
                timeout=120.0,
            )
            protected_after_append_reader = _stable_artifact_state(
                database_path,
                consecutive_observations=2,
                observation_interval=0.02,
            )
            _assert_protected_artifacts_unchanged(
                protected_before_append_reader,
                protected_after_append_reader,
            )
            assert writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[0] == 0
            assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
                0,
                0,
                0,
            )
            # A writer can continue immediately after the trusted reader and
            # checkpoint interaction.  This row is intentionally not refreshed
            # into qPlot because it lies beyond the declared acquisition plan.
            datasaver.add_result(
                (slow, float(slow_count)),
                (fast, 0.0),
                (signal, float(slow_count * 1_000)),
                (signal_b, float(1_000_000 + slow_count * 1_000)),
            )
            datasaver.flush_data_to_database(block=True)

            assert legacy_cheap.call_count == 0
            assert legacy_expensive.call_count == 0
            assert legacy_selected.call_count == 0
            assert legacy_preview.call_count == 0
            assert errors == []
            assert logged_errors == []

        assert not tuple(database_directory.glob("*.qdc"))
        assert tuple(cache_root.glob("*.qdc"))
    finally:
        if window is not None:
            window.close_database(status=False)
            _process_until(
                lambda: (
                    not window._trusted_derived_bridge.background_active()
                    and not window._retired_trusted_read_services
                ),
                timeout=30.0,
            )
            close_main_window(window, timeout_ms=12_000)
        if context_open:
            context.__exit__(None, None, None)
            context_open = False
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        writer.close()
