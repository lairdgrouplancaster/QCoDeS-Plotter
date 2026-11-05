"""Measurement selection and scalar columns for raw QCoDeS CSV export.

Export expands singleton fields within each acquisition record and preserves
nested object-cell samples. This differs from plotting's already-aligned
record flattening; joining the records must not round stored integer values.
"""

import numpy as np

from qplot.datahandling.numeric_samples import concatenate_record_samples


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
