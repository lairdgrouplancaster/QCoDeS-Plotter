"""Measurement selection, scalar alignment, and raw QCoDeS CSV tables.

Export expands singleton fields within each acquisition record and preserves
nested object-cell samples. This differs from plotting's already-aligned
record flattening; joining the records must not round stored integer values.
"""

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

import numpy as np

from qplot.datahandling.numeric_samples import concatenate_record_samples

if TYPE_CHECKING:
    import pandas as pd


def csv_dataframe(
    measurements: Iterable[tuple[str, Mapping[str, np.ndarray]]],
    *,
    prefix_columns: bool,
) -> "pd.DataFrame":
    """Assemble lazy measurement columns without rounding their stored samples.

    Consume one measurement at a time so callers can retain their read and
    failure boundaries without materialising every source column up front.
    """
    import pandas as pd

    frames = []
    for parameter_name, measurement_columns in measurements:
        columns = {}
        for name, values in measurement_columns.items():
            column_name = f"{parameter_name}.{name}" if prefix_columns else name
            # Object samples may need both uint64 and negative int64, or
            # exact integers alongside floats. Disable pandas' inference.
            dtype: str | type[object] | None = object if values.dtype.hasobject else None
            if values.dtype.kind in "iu":
                # Alignment adds blanks to shorter columns. Nullable
                # integers retain exact values instead of becoming floats.
                prefix = "U" if values.dtype.kind == "u" else ""
                dtype = f"{prefix}Int{values.dtype.itemsize * 8}"
            elif values.dtype.kind == "f" and values.dtype.itemsize < 8:
                # Untyped CSV is normally read as binary64. Widen exactly
                # before pandas formats narrow floats to their short text.
                dtype = "float64"
            columns[column_name] = pd.Series(values, dtype=dtype)
        frames.append(pd.DataFrame(columns))

    return pd.concat(frames, axis=1) if frames else pd.DataFrame()


def exportable_measurement_params(dataset):
    """Return dependent values, then standalone values, in run order."""
    parameters = dataset.get_parameters()
    setpoint_names = {
        name
        for param in parameters
        for name in param.depends_on_
    }
    dependent = [param for param in parameters if param.depends_on_]
    standalone = [
        param for param in parameters
        if not param.depends_on_ and param.name not in setpoint_names
    ]
    return dependent + standalone


def _csv_scalar_samples(values):
    """Flatten a record, including arrays held in NumPy object cells."""
    array = np.asarray(values)
    if not array.dtype.hasobject:
        return array.ravel()
    samples = []
    for value in array.ravel():
        if isinstance(value, (np.ndarray, list, tuple)):
            samples.extend(_csv_scalar_samples(value))
        else:
            samples.append(value)
    # These cells can mix signed/unsigned integers (or integers and floats).
    # Inferring a dtype again would undo extraction's precision protection.
    return np.asarray(samples, dtype=object)


def csv_scalar_columns(param_data, output_param=None):
    """Align samples within each QCoDeS record before joining records."""
    arrays = {name: np.asarray(values) for name, values in param_data.items()}
    if not arrays:
        return {}
    # QCoDeS trees are output-first; export callers also supply the selected
    # output explicitly so column presentation order cannot define acquisition.
    if output_param is None:
        output_param = next(iter(arrays))
    # Regular, already aligned columns need no record-by-record expansion.
    if (
            len({values.shape for values in arrays.values()}) == 1
            and all(not values.dtype.hasobject for values in arrays.values())
            ):
        return {name: values.ravel() for name, values in arrays.items()}

    chunks: dict[str, list[np.ndarray]] = {name: [] for name in arrays}
    for record in zip(*arrays.values(), strict=True):
        samples = {
            name: _csv_scalar_samples(values)
            for name, values in zip(arrays, record, strict=True)
        }
        sample_count = (max(values.size for values in samples.values())
                        if samples[output_param].size else 0)
        for name, values in samples.items():
            if values.size == sample_count:
                chunks[name].append(values)
            elif values.size == 1:
                chunks[name].append(np.repeat(values, sample_count))
            else:
                raise ValueError(
                    f"Cannot align CSV record column {name}: "
                    f"{values.size} samples instead of {sample_count}."
                )
    return {
        name: concatenate_record_samples(parts) if parts else np.array([], dtype=arrays[name].dtype)
        for name, parts in chunks.items()
    }
