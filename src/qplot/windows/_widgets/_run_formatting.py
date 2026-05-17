import html
from datetime import datetime


def run_tooltip_text(metadata):
    """
    Builds the summary shown when hovering over a run table row.

    """
    sweep = format_parameter_list_html(metadata.get("sweep_parameters"))
    measure = format_parameter_list_html(metadata.get("measure_parameters"))

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
        "</table>"
        )


def run_tooltip_plain_text(metadata):
    sweep = format_parameter_list(metadata.get("sweep_parameters"))
    measure = format_parameter_list(metadata.get("measure_parameters"))

    return "\n".join([
        f"{'Sweep':<7}({sweep})",
        f"Measure ({measure})",
        ])


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
    return bool(exception and "KeyboardInterrupt" in str(exception))


def format_run_status(metadata):
    if run_was_interrupted(metadata):
        return f"Interrupted ({format_interrupted_progress_percent(metadata)})"
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
    return _progress_percent_value(metadata, count, expected, cap_completed=False)


def _progress_percent_value(metadata, count, expected, cap_completed=True):
    if not expected or count is None:
        return None

    try:
        maximum = 100 if cap_completed and run_is_complete(metadata) else 99.9
        return max(0, min(maximum, (float(count) / float(expected)) * 100))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def format_interrupted_progress_percent(metadata):
    percent = interrupted_progress_percent_value(metadata)
    if percent is None:
        return "unknown"
    return f"{percent:.2f}%"


def complete_cell_sort_value(metadata):
    if run_was_interrupted(metadata):
        return interrupted_progress_percent_value(metadata)
    if run_is_complete(metadata):
        return 100
    return progress_percent_value(metadata)


def format_complete_cell(metadata):
    if run_was_interrupted(metadata):
        return f"Interrupted ({format_interrupted_progress_percent(metadata)})"

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
    elif not run_is_complete(metadata) and metadata.get("database_modified_timestamp"):
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
            shape_parts = " x ".join(f"{int(size):,}" for size in shape)
        except (TypeError, ValueError):
            shape_parts = ""

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


def measured_parameter_count(metadata):
    return len(metadata.get("measure_parameters") or [])
