"""Choose physical axes for a dropped dataset before mutating the plot."""

from dataclasses import dataclass
from typing import Any

from PyQt6 import QtWidgets as qtw

from ._config_persistence import persist_config_value
from ._plot_transform_labels import native_axis_quantities
from ._window_controls import CONFIRM_INCOMPATIBLE_AXES_KEY


def displayed_axis_parameter(window: Any, axis: str) -> Any:
    parameters = getattr(window, "axis_param", {})
    parameter = parameters.get(axis)
    if parameter is not None:
        return parameter
    name = window.axis_options.get(axis)
    if name == getattr(window.param, "name", None):
        return getattr(window, "display_param", None) or window.param
    return getattr(window, "param_dict", {}).get(name)


def _axis_identity(window: Any, axis: str, parameter: Any) -> tuple[Any, ...]:
    """Coordinates match by name and unit; measured values match by unit."""
    unit = getattr(parameter, "unit", None)
    name = window.axis_options.get(axis)
    independent = name in getattr(window.param, "depends_on_", ())
    return (
        name,
        None if unit is None else str(unit or "").strip(),
        independent,
    )


def _axes_match(existing, incoming):
    return (existing is not None and incoming[1] is not None
            and existing[1] == incoming[1]
            and (existing[0] == incoming[0] or not (existing[2] or incoming[2])))


def dataset_axis_entries(target: Any, kind: str, key: Any):
    """Yield the axes currently assigned to one dataset."""
    if kind == "trace":
        trace = target.lines[key]
        style = target._ensure_trace_styles()[key]
        source = target if trace is target.__dict__.get("line") else trace.from_win
        order = getattr(trace, "choose_from", None) or ("x", "y")
        for axis, side, source_axis in zip(
            ("x", "y"), (style.x_axis, style.y_axis), order, strict=True,
        ):
            parameter = target._trace_axis_parameter(trace, axis)
            yield side, _axis_identity(source, source_axis, parameter), parameter
    else:
        layer = target.heatmaps[key]
        source = target if layer is target else layer.from_win
        order = ("x", "y") if layer is target else layer.source_axis_order()
        if order is not None:
            for side, source_axis in zip(target._heatmap_axis_sides(layer), order, strict=True):
                parameter = displayed_axis_parameter(source, source_axis)
                yield side, _axis_identity(source, source_axis, parameter), parameter


def plot_axis_entries(target: Any):
    """Yield each dataset's physical axis, identity and displayed metadata."""
    def line_entries():
        for key in target.__dict__.get("lines", {}):
            yield from dataset_axis_entries(target, "trace", key)

    def heatmap_entries():
        for key in target.__dict__.get("heatmaps", {}):
            yield from dataset_axis_entries(target, "heatmap", key)

    # Follow the same renderer priority and registry order as the axis labels.
    generators = (line_entries, heatmap_entries) if target.operation_kind == "plot1d" else (
        heatmap_entries, line_entries,
    )
    for entries in generators:
        yield from entries()


def dataset_axis_warnings(target: Any, kind: str, key: Any) -> tuple[str, ...]:
    """Explain mismatches with the quantities labelling the assigned plot axes."""
    references: dict[str, tuple[tuple[Any, ...], Any]] = {}
    for side, identity, parameter in plot_axis_entries(target):
        references.setdefault(side, (identity, parameter))

    def quantity(parameter):
        name = getattr(parameter, "label", None) or getattr(parameter, "name", None) or "Unknown quantity"
        unit = getattr(parameter, "unit", None)
        return f"{name} ({'unknown units' if unit is None else str(unit).strip() or 'unitless'})"

    reasons = []
    for side, identity, parameter in dataset_axis_entries(target, kind, key):
        reference, plot_parameter = references[side]
        if not _axes_match(reference, identity):
            reason = f"{side} axis: dataset {quantity(parameter)} does not match plot {quantity(plot_parameter)}."
            if identity[0] != reference[0] and (identity[2] or reference[2]):
                reason += f" Coordinate names differ (dataset: {identity[0]}; plot: {reference[0]})."
            reasons.append(reason)
    if kind == "heatmap":
        layer = target.heatmaps[key]
        source = target if layer is target else layer.from_win
        parameter = getattr(source, "display_param", None) or source.param
        plot_parameter = target._colorbar_display_parameter()
        unit = getattr(parameter, "unit", None)
        plot_unit = getattr(plot_parameter, "unit", None)
        if unit is None or plot_unit is None or str(unit).strip() != str(plot_unit).strip():
            reasons.append(f"Colorscale: dataset {quantity(parameter)} does not match plot {quantity(plot_parameter)}.")
    return tuple(reasons)


def _occupied_axes(target: Any) -> dict[str, tuple[Any, ...]]:
    occupied: dict[str, tuple[Any, ...]] = {}
    for side, identity, _parameter in plot_axis_entries(target):
        occupied.setdefault(side, identity)
    return occupied


@dataclass(frozen=True)
class DropAxisPlan:
    axis_order: tuple[str, str]
    x_side: str
    y_side: str
    incompatible: tuple[str, ...]

    def approve(self, target: Any, source: Any) -> bool:
        if not self.incompatible or not target.config.get(CONFIRM_INCOMPATIBLE_AXES_KEY):
            return True
        reasons = "\n".join(f"• {reason}" for reason in self.incompatible)
        box = qtw.QMessageBox(
            qtw.QMessageBox.Icon.Warning,
            "Incompatible axes",
            f"The axes are incompatible with {source.label}:\n\n{reasons}\n\n"
            f"Add the dataset using the {self.x_side.lower()} and "
            f"{self.y_side.lower()} axes?",
            qtw.QMessageBox.StandardButton.Ok | qtw.QMessageBox.StandardButton.Cancel,
            target,
        )
        box.setDefaultButton(qtw.QMessageBox.StandardButton.Cancel)
        do_not_show_again = qtw.QCheckBox("Do not show again", box)
        box.setCheckBox(do_not_show_again)
        result = box.exec()
        if result == qtw.QMessageBox.StandardButton.Ok and do_not_show_again.isChecked():
            persist_config_value(
                target, target.config, CONFIRM_INCOMPATIBLE_AXES_KEY, False,
                "the incompatible-axis warning preference",
            )
        return result == qtw.QMessageBox.StandardButton.Ok


def plan_drop_axes(target: Any, source: Any) -> DropAxisPlan:
    occupied = _occupied_axes(target)
    candidates = []
    for order in (("x", "y"), ("y", "x")):
        sides = []
        incompatible = []
        compatible_count = unused_count = 0
        parameters = {
            axis: displayed_axis_parameter(source, source_axis)
            for axis, source_axis in zip(("x", "y"), order, strict=True)
        }
        if getattr(source, "operation_kind", None) == "plot1d" and target.__dict__.get("lines"):
            # New traces inherit the host's native processing controls. Match
            # their resulting quantities rather than the source's raw units.
            parameters = native_axis_quantities(
                parameters["x"], parameters["y"], next(iter(target.lines.values())).opts,
            )
        for axis, source_axis, choices in zip(
            ("x", "y"), order, (("Bottom", "Top"), ("Left", "Right")), strict=True,
        ):
            parameter = parameters[axis]
            identity = _axis_identity(source, source_axis, parameter)
            matching = next((side for side in choices if _axes_match(occupied.get(side), identity)), None)
            unused = next((side for side in choices if side not in occupied), None)
            if matching is not None:
                sides.append(matching)
                compatible_count += 1
            elif unused is not None:
                sides.append(unused)
                unused_count += 1
            else:
                sides.append(choices[0])
                name = getattr(parameter, "label", None) or source.axis_options[source_axis]
                incompatible.append(f"{name} does not match the {choices[0].lower()} axis.")
        candidates.append((compatible_count, unused_count, DropAxisPlan(
            order, sides[0], sides[1], tuple(incompatible),
        )))
    # Stable ties keep the source's existing orientation.
    plan = max(candidates, key=lambda candidate: candidate[:2])[2]
    heatmaps = target.__dict__.get("heatmaps", {})
    if getattr(source, "operation_kind", None) == "plot2d" and heatmaps:
        first = next(iter(heatmaps.values()))
        color_source = target if first is target else first.from_win
        target_parameter = getattr(color_source, "display_param", None) or color_source.param
        source_parameter = getattr(source, "display_param", None) or source.param
        target_unit = getattr(target_parameter, "unit", None)
        source_unit = getattr(source_parameter, "unit", None)
        if target_unit is None or source_unit is None or str(target_unit).strip() != str(source_unit).strip():
            plan = DropAxisPlan(plan.axis_order, plan.x_side, plan.y_side, plan.incompatible + (
                f"The colorscale units do not match (source: {source_unit or 'unitless'}; "
                f"plot: {target_unit or 'unitless'}).",
            ))
    return plan


class DroppedAxisMapping:
    """Retain an approved mapping through source refreshes and X/Y swaps."""

    def __init__(self, target: Any, source: Any, order: tuple[str, str]):
        self.target = target
        self.source = source
        self.target_names = tuple(target.axis_options[axis] for axis in ("x", "y"))
        options = self._source_axis_options()
        self.source_names = tuple(options[axis] for axis in order)

    def _source_axis_options(self) -> dict[str, str]:
        options = dict(self.source.axis_options)
        if getattr(self.source, "operation_kind", None) == "sweeper":
            # A cut selects its fixed coordinate in axis_options['y'], but
            # its plotted Y samples remain the measured signal. A cut-axis
            # change must never transpose that signal into the old coordinate.
            options["y"] = self.source.param.name
        return options

    def axis_order(self) -> tuple[str, str] | None:
        options = self._source_axis_options()
        names = self.source_names
        target_names = tuple(self.target.axis_options[axis] for axis in ("x", "y"))
        if target_names == self.target_names[::-1]:
            names = names[::-1]
        axes = tuple(next((axis for axis in ("x", "y") if options[axis] == name), "")
                     for name in names)
        return (axes[0], axes[1]) if all(axes) else None
