"""Cancellable raw CSV writing from one immutable QCoDeS run capture.

The caller owns and approves the staging filename. This module never publishes
an export destination and never modifies a source database. Trusted sources use
the existing paged run-prefix broker; other sources retain the read-only private
snapshot path. Independent parameter trees are flattened separately, then
joined side by side in the same wide layout as the original dataframe exporter.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Sequence
from contextlib import ExitStack, closing
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

import numpy as np

from qplot._auxiliary_paths import ensure_safe_auxiliary_path

from ._numeric_arrays import NumericArrayReader, read_npy_header
from ._sqlite_identifiers import quote_identifier_text as identifier
from .export_data import exportable_measurement_params
from .file_identity import (
    database_instance,
    database_instances_differ,
    logical_database_path,
)
from .readonly import DatabaseInstanceChangedError, load_by_guid_read_only

_ROW_PAGE_SIZE = 128
_SAMPLE_CHUNK_SIZE = 8192
_BUFFER_BYTES = 8 * 1024 * 1024


class CsvExportCancelled(InterruptedError):
    """A cancelled export must discard its unpublished staging file."""


@dataclass(frozen=True)
class RawCsvExportReceipt:
    run_id: int
    guid: str
    parameter_names: tuple[str, ...]
    row_count: int
    watermark: int


class _CsvArrayReader(NumericArrayReader):
    """Use the existing bounded C-order reader for non-pickled CSV arrays."""

    def __init__(self, blob):
        self.blob = blob
        header = read_npy_header(blob)
        self.shape = header.shape
        self.fortran_order = header.fortran_order
        self.dtype = header.dtype
        self.offset = header.offset
        self.size = header.size
        if self.dtype.hasobject or self.dtype.fields or self.dtype.subdtype:
            raise ValueError("CSV export does not support object or structured array storage.")
        if header.total_bytes != len(blob):
            raise ValueError("An array payload does not match its stored shape.")


def _noop_progress(_phase: str, _completed: int, _total: int | None) -> None:
    pass


def _never_cancelled() -> bool:
    return False


def _wait_request(request, check_cancelled, progress):
    previous = None
    try:
        while True:
            check_cancelled()
            observed = request.progress
            if observed is not None and observed != previous:
                progress(observed.phase.replace("plot", "export"),
                         observed.completed, observed.total)
                previous = observed
            try:
                result = request.wait(0.05)
            except TimeoutError:
                # A finished request can itself report a deadline failure.
                # Do not turn that failure into another wait or a fallback.
                if request.done:
                    raise
            else:
                check_cancelled()
                return result
    finally:
        if not request.done:
            request.cancel()


def _selected_parameters(dataset, parameter_names, measurement_index):
    parameters = exportable_measurement_params(dataset)
    if measurement_index is not None:
        if parameter_names is not None:
            raise ValueError("Select measurements by name or index, not both.")
        if type(measurement_index) is not int or not 1 <= measurement_index <= len(parameters):
            raise ValueError(f"Measurement index must be between 1 and {len(parameters)}.")
        return (parameters[measurement_index - 1],)
    if parameter_names is None:
        return tuple(parameters)
    by_name = {parameter.name: parameter for parameter in parameters}
    requested = tuple(parameter_names)
    if len(set(requested)) != len(requested):
        raise ValueError("A measurement was selected more than once.")
    try:
        return tuple(by_name[name] for name in requested)
    except KeyError as error:
        raise ValueError(f"Measurement {error.args[0]!r} is not exportable in this run.") from None


def _tree_specs(dataset, parameter):
    interdeps = dataset.description.interdeps
    output = interdeps._id_to_paramspec[parameter.name]
    top, dependencies, inferences = interdeps.all_parameters_in_tree_by_group(output)
    return (top, *dependencies, *inferences)


def _scalar(value, kind, *, has_array):
    if value is None:
        return None if kind == "text" else complex(np.nan, np.nan) if kind == "complex" else np.nan
    if kind == "numeric":
        # Match the existing QCoDeS numeric-column decoding convention.
        return np.float64(value)
    if kind == "complex" and has_array:
        # QCoDeS expands scalar complex storage to complex128 array cells.
        return np.complex128(value)
    return value


def _record_samples(conn, table_name, row, specs, check_cancelled, buffer_bytes):
    """Yield flattened record rows using bounded slices of each array BLOB."""
    row_id = row[0]
    with ExitStack() as stack:
        values = []
        is_array = []
        for index, spec in enumerate(specs):
            check_cancelled()
            value, storage = row[1 + 2 * index:3 + 2 * index]
            array = spec.type == "array" and storage != "null"
            if storage == "blob":
                blob = stack.enter_context(conn.blobopen(
                    table_name, spec.name, row_id, readonly=True,
                ))
                reader = _CsvArrayReader(blob)
                if spec.type == "complex":
                    if reader.size != 1:
                        raise ValueError("Scalar complex storage must contain exactly one value.")
                    value = reader.read(0, 1, check_cancelled)[0]
                elif array:
                    value = reader
                else:
                    raise ValueError(f"Unexpected binary storage for {spec.name!r}.")
            elif array:
                raise ValueError(f"Array parameter {spec.name!r} has non-array storage.")
            values.append(value)
            is_array.append(array)

        arrays = [value for value, array in zip(values, is_array, strict=True) if array]
        # Numeric/text/complex scalars expand to the first present array's
        # shape, then all singletons expand to the largest present array.
        first_size = arrays[0].size if arrays else 1
        largest = max((array.size for array in arrays), default=1)
        sizes = [value.size if array else first_size
                 for value, array in zip(values, is_array, strict=True)]
        sizes = [largest if size == 1 and largest > 1 else size for size in sizes]
        sample_count = max(sizes) if sizes[0] else 0
        if any(size not in (1, sample_count) for size in sizes):
            raise ValueError("Cannot align the setpoint and measurement samples in a CSV record.")
        if not sample_count:
            return
        item_bytes = sum(max(1, value.dtype.itemsize) if array else 16
                         for value, array in zip(values, is_array, strict=True))
        chunk_size = max(1, min(_SAMPLE_CHUNK_SIZE, buffer_bytes // max(1, item_bytes)))
        for start in range(0, sample_count, chunk_size):
            check_cancelled()
            stop = min(sample_count, start + chunk_size)
            columns = []
            for value, array, spec in zip(values, is_array, specs, strict=True):
                if array:
                    if value.size == 1:
                        scalar = value.read(0, 1, check_cancelled)[0]
                        columns.append((scalar,) * (stop - start))
                    else:
                        columns.append(value.read(start, stop, check_cancelled))
                else:
                    scalar = _scalar(value, spec.type, has_array=bool(arrays))
                    columns.append((scalar,) * (stop - start))
            yield from zip(*columns, strict=True)


def _tree_rows(conn, table_name, specs, watermark, check_cancelled, buffer_bytes):
    """Page by physical row ID, avoiding repeated growing OFFSET scans."""
    table = identifier(table_name)
    output = identifier(specs[0].name)
    fields = ", ".join(
        f"CASE WHEN typeof({identifier(spec.name)})='blob' THEN NULL "
        f"ELSE {identifier(spec.name)} END, typeof({identifier(spec.name)})"
        for spec in specs
    )
    last_id = 0
    while last_id < watermark:
        check_cancelled()
        rows = conn.execute(
            f"SELECT id, {fields} FROM {table} "
            f"WHERE id>? AND id<=? AND {output} IS NOT NULL ORDER BY id LIMIT ?",
            (last_id, watermark, _ROW_PAGE_SIZE),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            check_cancelled()
            yield from _record_samples(
                conn, table_name, row, specs, check_cancelled, buffer_bytes,
            )
        last_id = rows[-1][0]


def csv_sample_text(value: Any) -> str:
    """Retain exact integers/extended floats; missing values remain blank."""
    if value is None:
        return ""
    if isinstance(value, (float, complex, np.inexact)):
        if np.isnan(value):
            return ""
    if isinstance(value, np.floating):
        if value.dtype.itemsize > 8:
            return np.format_float_scientific(value, unique=True, trim="-")
        return repr(float(value))
    if isinstance(value, np.complexfloating) and value.dtype.itemsize <= 16:
        return str(complex(value))
    if isinstance(value, (np.datetime64, np.timedelta64)) and np.isnat(value):
        return ""
    return str(value)


def _write_rows(staging_path, conn, dataset, parameters, watermark,
                check_cancelled, progress, separator):
    specs = [_tree_specs(dataset, parameter) for parameter in parameters]
    prefix_columns = len(parameters) > 1
    header = tuple(
        f"{parameter.name}.{spec.name}" if prefix_columns else spec.name
        for parameter, tree in zip(parameters, specs, strict=True) for spec in tree
    )
    buffer_bytes = max(1, _BUFFER_BYTES // len(parameters))
    row_count = 0
    ensure_safe_auxiliary_path(staging_path)
    check_cancelled()
    with ExitStack() as stack:
        streams = [stack.enter_context(closing(_tree_rows(
            conn, dataset.table_name, tree, watermark, check_cancelled, buffer_bytes,
        ))) for tree in specs]
        output = stack.enter_context(open(staging_path, "w", encoding="utf-8", newline=""))
        writer = csv.writer(output, delimiter=separator)
        writer.writerow(header)
        progress("Writing CSV", 0, None)
        for trees in zip_longest(*streams):
            if row_count % 1024 == 0:
                check_cancelled()
            writer.writerow([
                csv_sample_text(value)
                for tree, tree_specs in zip(trees, specs, strict=True)
                for value in (tree if tree is not None else (None,) * len(tree_specs))
            ])
            row_count += 1
            if row_count % 8192 == 0:
                progress("Writing CSV", row_count, None)
        check_cancelled()
    progress("CSV ready", row_count, row_count)
    return row_count


def export_raw_csv(
    staging_path,
    *,
    database_path,
    guid: str,
    parameter_names: Sequence[str] | None = None,
    measurement_index: int | None = None,
    service=None,
    expected_database_identity=None,
    cancelled: Callable[[], bool] = _never_cancelled,
    progress: Callable[[str, int, int | None], None] = _noop_progress,
    separator: str = ",",
) -> RawCsvExportReceipt:
    """Write raw values to an approved stage; publication remains the caller's.

    A supplied trusted service is authoritative: its errors never trigger a
    snapshot fallback. ``None`` explicitly selects the existing fallback route.
    Cancellation is checked during capture, record decoding and serialization.
    The returned watermark is a physical row-ID boundary, not a sample count.
    """
    if separator not in (",", "\t"):
        raise ValueError("CSV separator must be comma or tab.")

    def check_cancelled():
        if cancelled():
            raise CsvExportCancelled("CSV export cancelled.")

    check_cancelled()
    accepted = database_instance(database_path)
    if expected_database_identity is not None and accepted.identity != expected_database_identity:
        raise DatabaseInstanceChangedError("The export source database was replaced.")

    def require_current_source():
        check_cancelled()
        if database_instances_differ(accepted, database_instance(database_path)):
            raise DatabaseInstanceChangedError("The export source database was replaced.")

    with ExitStack() as stack:
        progress("Reading run description", 0, None)
        if service is not None:
            if (service.database_instance.logical_path != logical_database_path(database_path)
                    or database_instances_differ(service.database_instance, accepted)):
                raise DatabaseInstanceChangedError("The export reader belongs to another database instance.")
            dataset = _wait_request(service.submit_plot_dataset(guid), check_cancelled, progress)
            parameters = _selected_parameters(dataset, parameter_names, measurement_index)
            if not parameters:
                raise ValueError("This run has no measurement parameters to export.")
            prefix = _wait_request(service.submit_plot_prefix(dataset), check_cancelled, progress)
            # Completed prefixes can be shared with plots and other subscribers.
            # Only release this connection/reference; never clean their directory.
            conn = stack.enter_context(closing(prefix.connect(cancelled=cancelled)))
            watermark = prefix.watermark
        else:
            dataset = load_by_guid_read_only(
                guid, database_path,
                expected_database_identity=expected_database_identity,
                cancelled_callback=cancelled,
            )
            stack.callback(dataset.conn.close)
            conn = dataset.conn
            parameters = _selected_parameters(dataset, parameter_names, measurement_index)
            if not parameters:
                raise ValueError("This run has no measurement parameters to export.")
            watermark = conn.execute(
                f"SELECT MAX(id) FROM {identifier(dataset.table_name)}",
            ).fetchone()[0] or 0
        require_current_source()
        count = _write_rows(staging_path, conn, dataset, parameters, watermark,
                            check_cancelled, progress, separator)
        require_current_source()
        return RawCsvExportReceipt(
            int(dataset.run_id), str(dataset.guid),
            tuple(parameter.name for parameter in parameters), count, int(watermark),
        )
