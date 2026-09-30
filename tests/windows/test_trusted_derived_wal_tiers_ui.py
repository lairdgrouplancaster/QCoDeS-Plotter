"""Real-WAL MainWindow acceptance for selected and background size tiers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets
from qcodes.dataset import load_by_id

from qplot.datahandling import trusted_work_coordinator as coordinator_module
from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
from qplot.windows import _database_actions as database_actions
from qplot.windows import _trusted_derived_qt as bridge_module
from qplot.windows import main as main_window
from qplot.windows._widgets import preview as preview_module
from qplot.windows._widgets import treeWidgets as tree_widgets
from tests._window_lifecycle import close_main_window
from tests.datahandling.test_trusted_live import (
    _assert_protected_artifacts_unchanged,
    _stable_artifact_state,
)
from tests.windows._trusted_derived_wal_ui import (
    _bridge_complete,
    _prepare_live_database,
    _process_until,
    _start_partial_two_dependent_run,
)

pytestmark = pytest.mark.timeout(180)


def test_real_wal_mixed_size_tiers_do_not_starve_selected_images(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Selected conclusive output must outrun background progressive pages."""

    slow_count = 48
    fast_count = 257
    visible_count = 5_000
    remaining_count = 11_000
    database_directory = tmp_path / "mixed-size-tiers"
    database_directory.mkdir()
    database_path = database_directory / "live.db"
    writer = _prepare_live_database(database_path, "mixed")
    small_dataset = load_by_id(1, conn=writer)
    visible_run = _start_partial_two_dependent_run(
        writer,
        "mixed_visible",
        slow_count=slow_count,
        fast_count=fast_count,
        acquired_count=visible_count,
    )
    remaining_run = _start_partial_two_dependent_run(
        writer,
        "mixed_remaining",
        slow_count=slow_count,
        fast_count=fast_count,
        acquired_count=remaining_count,
    )
    open_contexts = [visible_run[6], remaining_run[6]]
    small_guid = str(small_dataset.guid)
    visible_guid = str(visible_run[8].guid)
    remaining_guid = str(remaining_run[8].guid)
    protected_before_reader = _stable_artifact_state(
        database_path,
        consecutive_observations=2,
        observation_interval=0.02,
    )

    qplot_home = tmp_path / ".qplot-mixed"
    cache_root = tmp_path / "mixed-derived-cache"
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

    real_coordinator = bridge_module.TrustedWorkCoordinator

    class _GatedCoordinator(real_coordinator):
        """Delay only initial dispatch until selection and viewport are exact."""

        def __init__(self, *args, **kwargs) -> None:
            self._test_work_released = False
            super().__init__(*args, **kwargs)

        def _pump(self, *args, **kwargs) -> None:
            if self._test_work_released:
                super()._pump(*args, **kwargs)

        def release_test_work(self) -> None:
            self._test_work_released = True
            super()._pump()

    monkeypatch.setattr(bridge_module, "TrustedWorkCoordinator", _GatedCoordinator)

    events: list[tuple[str, str, object]] = []
    original_metadata = bridge_module.TrustedDerivedQtBridge._publish_metadata
    original_detail = tree_widgets.moreInfo.set_snapshot_run_detail
    original_thumbnail = tree_widgets.RunList.set_run_previews
    original_preview = preview_module.PreviewTab.publish_trusted_previews

    def record_metadata(bridge, publication, run_id, guid, payload):
        result = original_metadata(bridge, publication, run_id, guid, payload)
        marker = ("metadata", str(guid))
        if guid in bridge._metadata_by_guid and not any(
            (kind, published_guid) == marker
            for kind, published_guid, _payload in events
        ):
            events.append(("metadata", str(guid), dict(payload)))
        return result

    def record_detail(widget, detail):
        result = original_detail(widget, detail)
        guid = str(detail.run.as_dict().get("guid") or "")
        events.append(("detail", guid, detail))
        return result

    def record_thumbnail(widget, guid, previews):
        result = original_thumbnail(widget, guid, previews)
        events.append(("thumbnail", str(guid or ""), tuple(previews or ())))
        return result

    def record_preview(widget, guid, previews, *, error=None):
        result = original_preview(widget, guid, previews, error=error)
        events.append(("preview", str(guid or ""), tuple(previews or ())))
        return result

    monkeypatch.setattr(
        bridge_module.TrustedDerivedQtBridge,
        "_publish_metadata",
        record_metadata,
    )
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

    def event_index(kind: str, guid: str) -> int:
        return next(
            index
            for index, (event_kind, event_guid, _payload) in enumerate(events)
            if event_kind == kind and event_guid == guid
        )

    def nonempty_image_event(kind: str, guid: str) -> bool:
        for event_kind, event_guid, previews in events:
            if event_kind != kind or event_guid != guid or not previews:
                continue
            if all(
                isinstance(preview.get("image"), QtGui.QImage)
                and not preview["image"].isNull()
                and not preview.get("unsupported")
                for preview in previews
            ):
                return True
        return False

    errors = []
    logged_errors = []
    window = None
    active_service = None
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
            window.resize(900, 500)
            window.show()

            window.close_database(status=False)
            assert window.load_database_path(str(database_path))
            _process_until(lambda: not window._database_load_active)
            assert window._database_access_mode == database_actions.TRUSTED_LIVE_MODE, (
                window._database_fallback_reason,
                errors,
            )
            assert window.RunList.topLevelItemCount() == 3
            assert events == []

            small_item = window.RunList._item_for_guid(small_guid)
            visible_item = window.RunList._item_for_guid(visible_guid)
            remaining_item = window.RunList._item_for_guid(remaining_guid)
            assert small_item is not None
            assert visible_item is not None
            assert remaining_item is not None
            id_column = window.RunList.cols.index("ID")
            window.RunList.sortItems(id_column, QtCore.Qt.SortOrder.AscendingOrder)
            row = window.RunList.indexOfTopLevelItem(visible_item)
            row_height = max(24, window.RunList.sizeHintForRow(row))
            header = window.RunList.header()
            assert header is not None
            # Leave only the upper half of one row in the viewport.  The
            # bridge deliberately treats partial rows as visible, while the
            # following large run remains unambiguously off-screen.
            window.RunList.setFixedHeight(header.height() + max(12, row_height // 2))
            window.RunList.clearSelection()
            window.RunList.setCurrentItem(small_item)
            small_item.setSelected(True)
            _process_until(lambda: window._selected_run_guid == small_guid)
            window.RunList.scrollToItem(
                visible_item,
                QtWidgets.QAbstractItemView.ScrollHint.PositionAtTop,
            )
            QtWidgets.QApplication.processEvents()

            bridge = window._trusted_derived_bridge
            bridge._apply_priority()
            visible_indices = bridge._visible_stable_indices()
            assert bridge._index_by_guid[visible_guid] in visible_indices
            assert bridge._index_by_guid[remaining_guid] not in visible_indices
            coordinator = bridge.coordinator
            assert isinstance(coordinator, _GatedCoordinator)
            assert not coordinator.active
            coordinator.release_test_work()

            _process_until(
                lambda: (
                    small_guid in bridge._metadata_by_guid
                    and any(
                        kind == "detail" and guid == small_guid
                        for kind, guid, _payload in events
                    )
                    and nonempty_image_event("thumbnail", small_guid)
                    and nonempty_image_event("preview", small_guid)
                ),
                timeout=60.0,
            )
            # The selected conclusive result must not wait for every background
            # layout page.  Neither large run has publishable metadata yet.
            background_metadata_ready = {
                guid
                for guid in (visible_guid, remaining_guid)
                if guid in bridge._metadata_by_guid
            }
            assert background_metadata_ready == set(), [
                (kind, guid) for kind, guid, _payload in events
            ]
            assert event_index("metadata", small_guid) < event_index(
                "thumbnail", small_guid
            )
            assert event_index("metadata", small_guid) < event_index(
                "preview", small_guid
            )
            assert event_index("detail", small_guid) < event_index(
                "thumbnail", small_guid
            )
            assert event_index("detail", small_guid) < event_index(
                "preview", small_guid
            )
            assert window.infoBox.preview.current_guid == small_guid

            _process_until(lambda: _bridge_complete(window, visible_guid), timeout=90.0)
            _process_until(
                lambda: _bridge_complete(window, remaining_guid),
                timeout=90.0,
            )
            assert event_index("metadata", visible_guid) < event_index(
                "metadata", remaining_guid
            )
            _process_until(
                lambda: (
                    not coordinator.active and coordinator.snapshot().pending_count == 0
                ),
                timeout=90.0,
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

            prior_visible_preview = window.infoBox.preview.cache[visible_guid]
            visible_slow, visible_fast = visible_run[2], visible_run[3]
            visible_signal, visible_signal_b = visible_run[4], visible_run[5]
            visible_datasaver = visible_run[7]
            for logical_index in range(visible_count, visible_count + fast_count):
                slow_index, fast_index = divmod(logical_index, fast_count)
                visible_datasaver.add_result(
                    (visible_slow, float(slow_index)),
                    (visible_fast, float(fast_index)),
                    (visible_signal, float(slow_index * 1_000 + fast_index)),
                    (
                        visible_signal_b,
                        float(1_000_000 + slow_index * 1_000 + fast_index),
                    ),
                )
            visible_datasaver.flush_data_to_database(block=True)
            window.refreshMain()
            _process_until(
                lambda: (
                    visible_item.run_metadata.get("read_setpoint_count")
                    == visible_count + fast_count
                ),
                timeout=90.0,
            )
            _process_until(
                lambda: (
                    window.infoBox.preview.cache.get(visible_guid)
                    is not prior_visible_preview
                ),
                timeout=90.0,
            )
            _process_until(
                lambda: (
                    not coordinator.active and coordinator.snapshot().pending_count == 0
                ),
                timeout=90.0,
            )

            assert writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[0] == 0
            assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (
                0,
                0,
                0,
            )
            next_index = visible_count + fast_count
            slow_index, fast_index = divmod(next_index, fast_count)
            visible_datasaver.add_result(
                (visible_slow, float(slow_index)),
                (visible_fast, float(fast_index)),
                (visible_signal, float(slow_index * 1_000 + fast_index)),
                (
                    visible_signal_b,
                    float(1_000_000 + slow_index * 1_000 + fast_index),
                ),
            )
            visible_datasaver.flush_data_to_database(block=True)
            assert Path(f"{database_path}-wal").stat().st_size > 0

            assert legacy_cheap.call_count == 0
            assert legacy_expensive.call_count == 0
            assert legacy_selected.call_count == 0
            assert legacy_preview.call_count == 0
            assert errors == []
            assert logged_errors == []
            assert not tuple(database_directory.glob("*.qdc"))
            assert tuple(cache_root.glob("*.qdc"))

            active_service = window._trusted_read_service
            assert active_service is not None
            assert active_service.liveness().helper_alive
            window.close_database(status=False)
            _process_until(
                lambda: (
                    not window._trusted_derived_bridge.background_active()
                    and not window._retired_trusted_read_services
                ),
                timeout=30.0,
            )
            assert active_service.closed
            closed_liveness = active_service.liveness()
            assert not closed_liveness.dispatcher_alive
            assert not closed_liveness.control_alive
            assert not closed_liveness.helper_alive
            close_main_window(window, timeout_ms=12_000)
            window = None
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
        for context in reversed(open_contexts):
            context.__exit__(None, None, None)
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        writer.close()
