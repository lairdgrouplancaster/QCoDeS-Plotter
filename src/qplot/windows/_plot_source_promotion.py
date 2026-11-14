"""Rebuild a composition when adding a volume or removing its primary source."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal
from weakref import ref

from PyQt6 import QtWidgets as qtw

from ._plot2d_layers import DEFAULT_OVERLAY_OPACITY
from ._plot_drop_axes import DropAxisPlan, plan_drop_axes
from ._plot_title import inherit_plot_title
from ._refresh_interval import refresh_interval_value
from ._signal_blocking import blocked_signals


@dataclass(frozen=True, slots=True)
class _DatasetAppearance:
    kind: Literal["trace", "heatmap"]
    key: Any
    source: Any
    plan: DropAxisPlan
    style: Any = None
    opacity: float = 1.0
    visible: bool = True


def _remaining_datasets(
    owner: Any, selected: list[tuple[str, Any]],
) -> list[_DatasetAppearance]:
    datasets = []
    for kind, key in owner._plot_item_order:
        if (kind, key) in selected:
            continue
        if kind == "trace":
            item = owner.lines[key]
            style = deepcopy(owner._ensure_trace_styles()[key])
            is_primary = item is owner.__dict__.get("line")
            order = ("x", "y") if is_primary else item.source_axis_order() or ("x", "y")
            plan = DropAxisPlan(order, style.x_axis, style.y_axis, ())
            datasets.append(_DatasetAppearance(kind, key, owner if is_primary else item.from_win, plan, style))
        else:
            item = owner.heatmaps[key]
            sides = owner._heatmap_axis_sides(item)
            order = ("x", "y") if item is owner else item.source_axis_order() or ("x", "y")
            plan = DropAxisPlan(order, sides[0], sides[1], ())
            datasets.append(_DatasetAppearance(
                kind, key, owner if item is owner else item.from_win, plan,
                opacity=owner._heatmap_opacity(item), visible=owner._heatmap_is_visible(item),
            ))
    return datasets


def source_promotion_operation_error(
    source: Any, axis_order: tuple[str, ...],
) -> str | None:
    """A line's Y-only operations cannot be preserved on its displayed X axis."""
    if source.operation_kind != "plot1d" or axis_order != ("y", "x"):
        return None
    operations = source.oper_widget.list_order
    if any(not operations.item(index).isHidden() for index in range(operations.count())):
        return (
            "Swap this plot's X/Y axes before removing its primary dataset; "
            "the remaining transposed trace has data operations that must keep "
            "their original axis."
        )
    return None


def _copy_view(owner: Any, replacement: Any) -> None:
    for axis in ("x", "y", "x2", "y2"):
        if not replacement._axis_scale_axis_is_used(axis):
            continue
        old = owner._axis_scale_viewbox(axis)
        new = replacement._axis_scale_viewbox(axis)
        dimension = 0 if axis in ("x", "x2") else 1
        replacement._axis_scale_log_toggled(axis, owner._axis_scale_log_mode(axis))
        new.setRange(**{"xRange" if dimension == 0 else "yRange": old.viewRange()[dimension]}, padding=0)
        new.enableAutoRange(dimension, old.state["autoRange"][dimension])
        dimension_name = "x" if dimension == 0 else "y"
        new.setAutoVisible(**{dimension_name: old.state["autoVisibleOnly"][dimension]})
        new.setAutoPan(**{dimension_name: old.state["autoPan"][dimension]})
        new.setMouseEnabled(**{"x" if dimension == 0 else "y": old.state["mouseEnabled"][dimension]})
        if dimension == 0:
            new.invertX(old.state["xInverted"])
        else:
            new.invertY(old.state["yInverted"])
    replacement._axis_scale_custom_auto_axes = set(owner._axis_scale_custom_auto_axes)


def _copy_window_layout(owner: Any, replacement: Any) -> None:
    for window in (owner, replacement):
        for widget_type in (qtw.QDockWidget, qtw.QToolBar):
            for widget in window.findChildren(widget_type):
                widget.setObjectName(f"{widget_type.__name__}:{widget.windowTitle()}")
    replacement.restoreState(owner.saveState())
    replacement.setGeometry(owner.geometry())
    inherit_plot_title(owner, replacement)


def _promotion_primary(datasets: list[_DatasetAppearance]) -> _DatasetAppearance:
    """Validate the surviving source before constructing a replacement."""
    # Cuts have their own linked controls and cannot host a mixed plot. Keep
    # their overlays in place while choosing the bottom eligible full source.
    primary = next((dataset for dataset in reversed(datasets)
                    if dataset.source.operation_kind in {"plot1d", "plot2d"}), None)
    if primary is None:
        raise ValueError("Keep a full line or heatmap dataset in this plot; cuts cannot be its primary source.")
    operation_error = source_promotion_operation_error(primary.source, primary.plan.axis_order)
    if operation_error:
        raise ValueError(operation_error)
    return primary


def _restore_source_controls(replacement: Any, primary: _DatasetAppearance) -> None:
    """Restore operations and axis choices from the selected source."""
    source = primary.source
    replacement.oper_widget.restore_state(source.oper_widget.capture_state(
        transpose=primary.plan.axis_order == ("y", "x"),
    ))
    order = primary.plan.axis_order
    axis_options = dict(source.axis_options)
    for dimension, source_axis in zip(("x", "y"), order, strict=True):
        combo = replacement.axis_dropdown[dimension]
        with blocked_signals(combo):
            combo.setCurrentText(axis_options[source_axis])
    replacement._axis_selection = replacement.axis_options


def _restore_datasets(
    replacement: Any,
    datasets: list[_DatasetAppearance],
    primary: _DatasetAppearance,
) -> None:
    """Rebuild retained layers from bottom to top, keeping their appearance."""
    remapped_order: list[tuple[str, Any]] = []
    for dataset in reversed(datasets):
        if dataset is primary:
            key = replacement.label if dataset.kind == "trace" else replacement._primary_heatmap_key
            item = replacement.line if dataset.kind == "trace" else replacement
        else:
            key = dataset.key
            if dataset.kind == "trace":
                replacement.add_preview_trace(dataset.source, dataset.plan)
                key = replacement._window_trace_key(dataset.source)
                item = replacement.lines[key]
            else:
                replacement.add_heatmap(dataset.source, axis_plan=dataset.plan)
                item = replacement.heatmaps[key]
        if dataset.kind == "trace":
            replacement._ensure_trace_styles()[key] = dataset.style
            replacement._apply_trace_style(key, item)
        else:
            replacement._set_layer_axes(item, dataset.plan.x_side, dataset.plan.y_side)
            replacement._set_layer_opacity(item, dataset.opacity)
            replacement._set_layer_visibility(item, dataset.visible)
        remapped_order.insert(0, (dataset.kind, key))
    replacement._plot_item_order = remapped_order
    replacement._sync_plot_item_order()


def _restore_display(owner: Any, replacement: Any) -> None:
    """Restore rendering and view settings before rebuilding derived curves.

    Averages depend on the final layer mappings and viewport. Trace limits
    follow averaging because they count generated curves as well as sources.
    """
    # Keep the retained curves' rendering choices, including settings the
    # user may have temporarily disabled. Copy individual controls rather
    # than PlotItem state, which would also replace axis ownership/views.
    for name in ("downsampleSpin", "alphaSlider", "gridAlphaSlider", "maxTracesSpin"):
        getattr(replacement.plot.ctrl, name).setValue(getattr(owner.plot.ctrl, name).value())
    for name in (
            "subtractMeanCheck", "phasemapCheck", "fftCheck", "derivativeCheck",
            "downsampleCheck", "autoDownsampleCheck", "subsampleRadio",
            "meanRadio", "peakRadio", "clipToViewCheck", "alphaGroup",
            "autoAlphaCheck", "xGridCheck", "yGridCheck",
            ):
        getattr(replacement.plot.ctrl, name).setChecked(getattr(owner.plot.ctrl, name).isChecked())
    if "_colorbar_colormap_name" in owner.__dict__:
        replacement._colorbar_colormap_name = owner._colorbar_colormap_name
        bar = replacement.__dict__.get("bar")
        if bar is not None:
            bar.setColorMap(replacement._colorbar_colormap())
    replacement._colorbar_manual_levels = owner.__dict__.get("_colorbar_manual_levels")
    replacement._sync_heatmap_colorbar_items(rescale=True)
    _copy_view(owner, replacement)
    # Rebuild averages only after the surviving curves have their final
    # mapping and viewport; never transfer the old generated average.
    replacement.plot.ctrl.averageGroup.setChecked(owner.plot.ctrl.averageGroup.isChecked())
    # Native trace limits include generated averages. Restore them only
    # after those curves exist, retaining the same visibility semantics.
    for name in ("forgetTracesCheck", "maxTracesCheck"):
        getattr(replacement.plot.ctrl, name).setChecked(getattr(owner.plot.ctrl, name).isChecked())


def promote_plot_source(
    main: Any, owner: Any, selected: list[tuple[str, Any]], *, volume=None,
) -> Any:
    """Rebuild the source-specific controls and release the old plot owner.

    Construct the replacement before closing the old owner, so every surviving
    dataset stays retained and a construction failure leaves the plot intact.
    A newly added volume owns its controls and keeps all existing datasets as
    overlays. Removing it promotes an ordinary source, removing slice controls.
    """
    datasets = _remaining_datasets(owner, selected)
    primary = _promotion_primary(datasets) if volume is None else None
    source = primary.source if primary is not None else None
    dialog = owner.__dict__.get("_plot_appearance_dialog")
    show_dialog = dialog is not None and dialog.isVisible()
    dialog_geometry = dialog.saveGeometry() if show_dialog else None
    previous_windows = set(main.windows)
    next_position = main._plot_tile_grid, main._next_plot_tile
    replacement = None
    try:
        if volume is None:
            assert source is not None
            widget, key, param = type(source), source._dataset_key, source.param
        else:
            from .plot3d import plot3d
            widget, (key, param) = plot3d, volume
        main.openWin(widget, key, param,
                     refrate=refresh_interval_value(owner.spinBox), show=True)
        replacement = next(win for win in main.windows if win not in previous_windows)
        _copy_window_layout(owner, replacement)
        replacement.snap_to_trace_action.setChecked(owner.snap_to_trace_action.isChecked())
        if volume is not None:
            plan = plan_drop_axes(owner, replacement)
            if not plan.approve(owner, replacement):
                replacement.close()
                replacement.deleteLater()
                return None
            primary = _DatasetAppearance(
                "heatmap", replacement._primary_heatmap_key, replacement, plan,
                opacity=DEFAULT_OVERLAY_OPACITY,
            )
            datasets.insert(0, primary)
        assert primary is not None
        _restore_source_controls(replacement, primary)
        _restore_datasets(replacement, datasets, primary)
        _restore_display(owner, replacement)
        if volume is not None and replacement._colorbar_manual_levels is not None:
            replacement._set_locked_mode()
        replacement.refreshWindow(force=True)
    except Exception:
        for win in list(main.windows):
            if win not in previous_windows:
                win.close()
                win.deleteLater()
        raise
    finally:
        main._plot_tile_grid, main._next_plot_tile = next_position
    owner._preview_drop_replacement = ref(replacement)
    if (dialog is not None and getattr(dialog, "_session_active", False)
            and volume is not None and getattr(dialog, "_pending_added_entry", None) is not None
            and not getattr(dialog, "_session_finishing", False)):
        dialog.adopt_preview(replacement)
        replacement.show()
        return replacement
    owner.close()
    if owner._merged_trace_users <= 0:
        owner.deleteLater()
    replacement.show()
    if show_dialog:
        replacement.open_plot_appearance_dialog()
        replacement._plot_appearance_dialog.restoreGeometry(dialog_geometry)
    return replacement
