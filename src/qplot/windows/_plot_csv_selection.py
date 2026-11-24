"""Identify plotted measurements for CSV without reading their databases."""

from dataclasses import dataclass
from os.path import basename
from typing import Any


@dataclass(frozen=True)
class CsvMeasurement:
    kind: str
    key: Any
    item: Any
    source: Any
    label: str


def csv_measurements(owner: Any) -> dict[str, CsvMeasurement]:
    result = {}
    lines = owner.__dict__.get("lines")
    if lines is None:
        lines = {index: item for index, item in enumerate(owner.plot.items)
                 if hasattr(item, "implements") and item.implements("plotData")}
    heatmaps = owner.__dict__.get("heatmaps", {})
    if not heatmaps and getattr(owner, "operation_kind", None) == "plot2d":
        heatmaps = {owner.__dict__.get("_primary_heatmap_key"): owner}
    labels: set[str] = set()
    for kind, registry in (("trace", lines), ("heatmap", heatmaps)):
        for index, (key, item) in enumerate(registry.items()):
            if item is None:
                continue
            source = owner if item is owner or item is owner.__dict__.get("line") else getattr(item, "from_win", owner)
            dataset = getattr(source, "ds", None)
            run = getattr(dataset, "run_id", None)
            parameter = getattr(source, "param", None)
            name = getattr(key, "parameter_name", None) or getattr(parameter, "name", None)
            if not name:
                name = item.name() if kind == "trace" and callable(getattr(item, "name", None)) else None
            name = name or getattr(key, "parameter_name", None) or f"{kind} {index + 1}"
            label = f"Run {run} — {name}" if run is not None else str(name)
            sweep = getattr(key, "sweep_id", None)
            if sweep is not None:
                label += f" · sweep {sweep}"
            label += f" ({kind})"
            if label in labels:
                dataset_key = getattr(key, "dataset_key", None) or getattr(source, "_dataset_key", None)
                database = basename(getattr(dataset_key, "database_path", ""))
                label += f" · {database}" if database else f" · {index + 1}"
            while label in labels:
                label += f" · {index + 1}"
            labels.add(label)
            result[f"{kind}:{index}"] = CsvMeasurement(kind, key, item, source, label)
    return result


def configure_csv_measurements(owner: Any, exporter: Any, *, previous: Any = None) -> None:
    measurements = csv_measurements(owner)
    exporter._qplot_csv_measurements = measurements
    traces = [token for token, entry in measurements.items() if entry.kind == "trace"]
    choices = {}
    if len(traces) > 1:
        choices[f"All traces ({len(traces)})"] = "all_traces"
    choices.update((entry.label, token) for token, entry in measurements.items())
    if not choices:
        choices["No measurements loaded"] = "empty"
    default = next(iter(choices.values()))
    if getattr(owner, "operation_kind", None) == "plot2d":
        default = next((token for token, entry in measurements.items()
                        if entry.kind == "heatmap" and entry.item is owner), default)
    # Tokens depend on list order. Restore the plotted item even if its
    # position changed while the user was editing figure settings.
    if previous == "all_traces" and len(traces) > 1:
        default = "all_traces"
    elif previous is not None:
        default = next((token for token, entry in measurements.items() if entry.item is previous), default)
    exporter.params.insertChild(0, {
        "name": "measurements", "title": "Data to export", "type": "list",
        "limits": choices, "value": default,
    })


def selected_csv_measurements(owner: Any, exporter: Any, *, validate=False) -> list[CsvMeasurement] | None:
    parameter = exporter.params.names.get("measurements")
    if parameter is None:
        return None  # Standalone exporters retain their existing capture scope.
    measurements = exporter._qplot_csv_measurements
    token = parameter.value()
    if token == "empty":
        if validate:
            raise ValueError("No measurements have finished loading in this plot.")
        return []
    selected = ([entry for entry in measurements.values() if entry.kind == "trace"]
                if token == "all_traces" else [measurements[token]])
    if validate:
        current = csv_measurements(owner)
        current_items = {id(entry.item) for entry in current.values()}
        if any(id(entry.item) not in current_items for entry in selected):
            raise ValueError("A selected measurement was removed. Reopen Export plot to update the measurement list.")
        if token == "all_traces" and {id(entry.item) for entry in selected} != {
            id(entry.item) for entry in current.values() if entry.kind == "trace"
        }:
            raise ValueError("The plot's traces changed. Reopen Export plot to update the measurement list.")
    return selected


def csv_exports_heatmap(owner: Any, exporter: Any) -> bool:
    selected = selected_csv_measurements(owner, exporter)
    return (bool(selected) and selected[0].kind == "heatmap" if selected is not None
            else getattr(owner, "operation_kind", None) == "plot2d")


def csv_selection_description(owner: Any, exporter: Any) -> str:
    selected = selected_csv_measurements(owner, exporter)
    return "; ".join(entry.label for entry in selected) if selected else ""
