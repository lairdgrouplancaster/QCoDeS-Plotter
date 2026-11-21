"""Statistics over complete recorded measurements, independent of previews."""

from contextlib import ExitStack, closing
from dataclasses import dataclass

import numpy as np

from qplot._number_formatting import format_number
from qplot.datahandling.numeric_samples import numeric_isfinite
from qplot.tools.sample_statistics import (
    finite_mean,
    finite_standard_deviation,
    finite_sum_is_zero,
)

from ._plot_transform_labels import quantity_text


def _finite_chunk(values, parameter_name):
    values = np.asarray(values)
    if np.iscomplexobj(values) or (
        values.dtype.kind == "O" and any(np.iscomplexobj(value) for value in values.flat)
    ):
        raise ValueError(
            f"Statistics require real-valued data; {parameter_name!r} contains complex values."
        )
    values = values[numeric_isfinite(values)]
    # Keep the recorded samples for exact centering. Conversion here only
    # checks whether finite extended/object values would display as inf or 0.
    if values.dtype.kind == "O" or (values.dtype.kind == "f" and values.dtype.itemsize > 8):
        try:
            with np.errstate(over="ignore", under="ignore", invalid="ignore"):
                displayed = values.astype(float)
            supported = np.all(np.isfinite(displayed)) and not np.any(
                (displayed == 0) & (values != 0)
            )
        except OverflowError:
            supported = False
        if not supported:
            raise ValueError(
                f"Finite values in {parameter_name!r} are outside the statistics display range."
            )
    return values


def _finite_statistics_samples(values, parameter_name, check_cancelled):
    flat = np.asarray(values).ravel()
    # Mixed numeric object arrays require Python-level inspection. Bound that
    # work between cancellation checks even for one large acquisition.
    check_cancelled()
    if flat.dtype.kind != "O" or flat.size <= 65536:
        return _finite_chunk(flat, parameter_name)
    chunks = []
    for start in range(0, flat.size, 65536):
        check_cancelled()
        chunks.append(_finite_chunk(flat[start:start + 65536], parameter_name))
    return np.concatenate(chunks)


@dataclass(frozen=True)
class StatisticsSource:
    run_id: int
    guid: str
    paramspecs: dict


def dataset_statistics_text(columns, parameter_name, *, setpoint_names, dataset=None,
                            check_cancelled=lambda: None):
    values = _finite_statistics_samples(columns[parameter_name], parameter_name, check_cancelled)
    if not values.size:
        return None
    average = finite_mean(values, check_cancelled=check_cancelled)
    deviation = finite_standard_deviation(values, check_cancelled=check_cancelled)
    if not np.isfinite(average) or not np.isfinite(deviation) or (
        deviation == 0 and values.min() != values.max()
    ) or (
        average == 0 and not finite_sum_is_zero(values, check_cancelled=check_cancelled)
    ):
        raise ValueError(
            f"Statistics for {parameter_name!r} are outside the statistics display range."
        )
    lines = [f"{values.size} points"]
    if dataset is not None:
        lines.extend((
            f"Run: {dataset.run_id} ({dataset.guid})",
            f"Measurement: {quantity_text(dataset.paramspecs[parameter_name])}",
            f"Data: {values.size} finite values of {columns[parameter_name].size} recorded samples",
        ))
        if setpoint_names:
            lines.append("Ranges: Finite values of each quantity, evaluated independently")
    # Inferred measurements also contain basis columns. Only declared
    # dependencies define the plot axes; basis values need not be numeric.
    axes = list(setpoint_names)
    # For a line, Y is the measured value; for a map, both are setpoints.
    # Match the default plot axes: the inner (second) setpoint is X,
    # and the outer (first) setpoint is Y.
    if len(axes) > 2:
        ranges = [(name, name) for name in axes]
    else:
        range_names = axes[::-1] if len(axes) == 2 else (axes + [parameter_name])[:2]
        ranges = list(zip(("X", "Y"), range_names, strict=False))
    for axis, name in ranges:
        check_cancelled()
        if dataset is not None:
            lines.append(f"{axis} quantity: {quantity_text(dataset.paramspecs[name])}")
        samples = values if name == parameter_name else _finite_statistics_samples(columns[name], name, check_cancelled)
        if samples.size:
            lines.append(
                f"{axis} range: {format_number(samples.min())} to {format_number(samples.max())}"
            )
    lines.extend((
        f"Average: {format_number(average)}",
        f"Standard deviation: {format_number(deviation)}",
        f"Max: {format_number(float(values.max()))}",
        f"Min: {format_number(float(values.min()))}",
    ))
    return "\n".join(lines)


def read_measurement_statistics(key, parameter_name, service, cancelled):
    """Decode one protected capture, release its reader, then calculate."""
    from qplot.datahandling.export_data import csv_scalar_columns
    from qplot.datahandling.parameter_data import (
        get_parameter_data_for_one_paramtree,
        parameter_data_for_export,
    )
    from qplot.datahandling.readonly import (
        DatabaseInstanceChangedError,
        load_by_guid_read_only,
    )
    from qplot.datahandling.trusted_live_service import wait_for_trusted_read

    from ._dataset_handle import (
        close_dataset_connection,
        dataset_key_matches_current_source,
    )

    def check_cancelled():
        if cancelled():
            raise InterruptedError("Statistics cancelled.")

    check_cancelled()
    if not dataset_key_matches_current_source(key):
        raise DatabaseInstanceChangedError("The statistics source was replaced.")
    with ExitStack() as stack:
        if service is None:
            dataset = load_by_guid_read_only(
                key.guid, key.database_path, expected_database_identity=key.database_identity,
                cancelled_callback=cancelled,
            )
            stack.callback(close_dataset_connection, dataset)
            data = parameter_data_for_export(dataset, parameter_name, check_cancelled=check_cancelled)
        else:
            dataset = wait_for_trusted_read(service.submit_plot_dataset(key.guid), check_cancelled)
            prefix = wait_for_trusted_read(service.submit_plot_prefix(dataset), check_cancelled)
            conn = stack.enter_context(closing(prefix.connect(decode_arrays=True, cancelled=cancelled)))
            data, _ = get_parameter_data_for_one_paramtree(
                conn, dataset.table_name, dataset.description, parameter_name,
                check_cancelled=check_cancelled,
            )
        # Snapshot-backed DataSet properties can query SQLite. Capture the
        # small report description before releasing that private connection.
        metadata = StatisticsSource(dataset.run_id, dataset.guid, dataset.paramspecs)
    columns = csv_scalar_columns(data, parameter_name, check_cancelled=check_cancelled)
    return dataset_statistics_text(
        columns, parameter_name, setpoint_names=metadata.paramspecs[parameter_name].depends_on_,
        dataset=metadata, check_cancelled=check_cancelled,
    )
