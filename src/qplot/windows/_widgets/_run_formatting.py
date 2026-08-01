import html
from datetime import datetime


def run_tooltip_text(metadata):
    """
    Builds the summary shown when hovering over a run table row.

    """
    sweep = format_parameter_list_html(metadata.get("sweep_parameters"))
    measure = format_parameter_list_html(metadata.get("measure_parameters"))
    status = html.escape(format_run_status(metadata))
    exception_row = ""
    if run_failed(metadata):
        summary = html.escape(measurement_exception_summary(metadata))
        exception_row = (
            "<tr>"
            "<td style='padding:0 0.5em 0 0'>Exception</td>"
            f"<td style='padding:0'>{summary}</td>"
            "</tr>"
            )

    return (
        "<table style='margin:0; border-spacing:0; border-collapse:collapse'>"
        "<tr>"
        "<td style='padding:0 0.5em 0 0'>Sweep</td>"
        f"<td nowrap='nowrap' style='padding:0; white-space:nowrap'>({sweep})</td>"
        "</tr>"
        "<tr>"
        "<td style='padding:0 0.5em 0 0'>Measure</td>"
        f"<td nowrap='nowrap' style='padding:0; white-space:nowrap'>({measure})</td>"
        "</tr>"
        "<tr>"
        "<td style='padding:0 0.5em 0 0'>Status</td>"
        f"<td style='padding:0'>{status}</td>"
        "</tr>"
        f"{exception_row}"
        "</table>"
        )


def run_tooltip_plain_text(metadata):
    sweep = format_parameter_list(metadata.get("sweep_parameters"))
    measure = format_parameter_list(metadata.get("measure_parameters"))
    lines = [
        f"{'Sweep':<7}({sweep})",
        f"Measure ({measure})",
        f"Status  {format_run_status(metadata)}",
        ]
    if run_failed(metadata):
        lines.append(f"Exception {measurement_exception_summary(metadata)}")

    return "\n".join(lines)


def format_parameter_list(parameters):
    if not parameters:
        return "unknown"
    return ", ".join(str(parameter) for parameter in parameters)


def format_parameter_list_html(parameters):
    if not parameters:
        return "unknown"
    return ",&nbsp;".join(html.escape(str(parameter)) for parameter in parameters)


def run_is_complete(metadata):
    return bool(metadata.get("completed_timestamp") or metadata.get("is_completed"))


def run_was_interrupted(metadata):
    exception = metadata.get("measurement_exception")
    return exception is not None and "KeyboardInterrupt" in str(exception).strip()


def run_failed(metadata):
    exception = metadata.get("measurement_exception")
    return bool(
        exception is not None
        and str(exception).strip()
        and not run_was_interrupted(metadata)
        )


def measurement_exception_summary(metadata, maximum_length=200):
    exception = str(metadata.get("measurement_exception") or "")
    summary = next(
        (line.strip() for line in reversed(exception.splitlines()) if line.strip()),
        "",
        )
    if len(summary) > maximum_length:
        return f"{summary[:maximum_length - 1]}…"
    return summary


def format_run_status(metadata):
    if run_was_interrupted(metadata):
        return f"Interrupted ({format_interrupted_progress_percent(metadata)})"
    if run_failed(metadata):
        return f"Failed ({format_interrupted_progress_percent(metadata)})"
    if run_is_complete(metadata):
        return "Complete"
    return f"Incomplete ({format_progress_percent(metadata)})"


def format_progress(metadata):
    progress = format_progress_percent(metadata)
    if progress == "unknown":
        return "unknown% complete"
    return f"{progress} complete"


def format_progress_percent(metadata):
    if run_is_complete(metadata):
        return "100%"

    percent = progress_percent_value(metadata)
    if percent is None:
        return "unknown"

    return f"{percent:.1f}%"


def progress_percent_value(metadata):
    expected = metadata.get("expected_results")
    count = metadata.get("result_count")
    return _progress_percent_value(metadata, count, expected)


def interrupted_progress_percent_value(metadata):
    expected = metadata.get("setpoint_count") or metadata.get("expected_results")
    count = metadata.get("read_setpoint_count")
    if count is None:
        count = metadata.get("result_count")
    return _progress_percent_value(metadata, count, expected, maximum=100)


def _progress_percent_value(metadata, count, expected, maximum=None):
    if not expected or count is None:
        return None

    try:
        if maximum is None:
            maximum = 100 if run_is_complete(metadata) else 99.9
        return max(0, min(maximum, (float(count) / float(expected)) * 100))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def format_interrupted_progress_percent(metadata):
    percent = interrupted_progress_percent_value(metadata)
    if percent is None:
        return "unknown"
    return f"{percent:.2f}%"


def complete_cell_sort_value(metadata):
    if run_was_interrupted(metadata) or run_failed(metadata):
        return interrupted_progress_percent_value(metadata)
    if run_is_complete(metadata):
        return 100
    return progress_percent_value(metadata)


def format_complete_cell(metadata):
    if run_was_interrupted(metadata):
        return "Interrupted"

    if run_failed(metadata):
        return "Failed"

    if run_is_complete(metadata):
        return "✓"

    progress = format_progress_percent(metadata)
    if progress == "unknown":
        return "unknown"
    return progress


def format_timestamp(timestamp):
    if not timestamp:
        return "unknown"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def time_taken_seconds(metadata):
    started = metadata.get("run_timestamp")
    if not started:
        return None

    completed = metadata.get("completed_timestamp")
    if completed:
        end = completed
    elif run_is_complete(metadata):
        return None
    elif metadata.get("database_modified_timestamp"):
        end = metadata.get("database_modified_timestamp")
    else:
        end = datetime.now().timestamp()
    try:
        return max(0, float(end) - float(started))
    except (TypeError, ValueError):
        return None


def format_time_taken_seconds(metadata):
    seconds = time_taken_seconds(metadata)
    if seconds is None:
        return "unknown"
    return f"{seconds:,.1f} s"


def format_run_duration(metadata):
    seconds = time_taken_seconds(metadata)
    if seconds is None:
        return "unknown"

    if seconds < 10:
        return f"{seconds:.2f} s"
    if seconds < 100:
        return f"{seconds:.1f} s"
    return f"{seconds:.0f} s"


def format_duration_dhms(seconds):
    total_seconds = int(round(seconds))
    days, remainder = divmod(total_seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m {seconds}s"


def format_storage_size(bytes_value):
    if bytes_value is None:
        return "unknown"

    try:
        size = float(bytes_value)
    except (TypeError, ValueError):
        return "unknown"

    if size < 0:
        return "unknown"

    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1

    if unit_index == 0:
        return f"{int(size)} B"
    if size < 10:
        return f"{size:.1f} {units[unit_index]}"
    return f"{size:.0f} {units[unit_index]}"


def format_point_count(metadata):
    expected = metadata.get("setpoint_count", metadata.get("expected_results"))
    shape = metadata.get("setpoint_shape") or metadata.get("point_shape")
    if shape:
        try:
            shape_values = [int(size) for size in shape]
            shape_parts = " × ".join(f"{size:,}" for size in shape_values)
        except (TypeError, ValueError):
            shape_values = []
            shape_parts = ""

        duplicate_count = one_dimensional_duplicate_point_count(metadata, shape_values)
        if duplicate_count is not None:
            return duplicate_count

        if expected:
            return f"{int(expected):,} = {shape_parts}"
        if shape_parts:
            return shape_parts

    if expected:
        return f"{int(expected):,}"

    count = metadata.get("result_count")
    if count is not None:
        try:
            return f"{int(count):,}"
        except (TypeError, ValueError):
            pass

    return "unknown"


def one_dimensional_duplicate_point_count(metadata, shape_values=None):
    expected = metadata.get("setpoint_count", metadata.get("expected_results"))
    shape = metadata.get("setpoint_shape") or metadata.get("point_shape")
    if shape_values is None:
        if not shape:
            return None
        try:
            shape_values = [int(size) for size in shape]
        except (TypeError, ValueError):
            return None

    if len(shape_values) != 1:
        return None

    try:
        expected_count = int(expected)
    except (TypeError, ValueError):
        return None

    if expected_count != shape_values[0]:
        return None
    return f"{expected_count:,}"


def measured_parameter_count(metadata):
    return len(metadata.get("measure_parameters") or [])
