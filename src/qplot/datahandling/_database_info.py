"""Pure presentation of database diagnostics collected by the readers.

The worker owns source checks, read-only connections, and cancellation. This
module only converts an already collected summary to display groups or text.
"""

from datetime import datetime


def _database_info_groups(summary):
    latest_run = summary["latest_run"]
    latest_run_rows = [("Latest run", "None")]
    if latest_run:
        status = "completed" if latest_run.get("is_completed") else "running or incomplete"
        latest_run_rows = [
            ("Latest run ID", _display_value(latest_run.get("run_id"))),
            ("Latest run name", _display_value(latest_run.get("name"))),
            ("Latest run status", status),
            ("Latest run started", _timestamp_value(latest_run.get("run_timestamp"))),
            ("Latest run completed", _timestamp_value(latest_run.get("completed_timestamp"))),
            ("Latest run GUID", _display_value(latest_run.get("guid"))),
            ]

    page_bytes = None
    if summary["page_count"] is not None and summary["page_size"] is not None:
        page_bytes = int(summary["page_count"]) * int(summary["page_size"])

    return [
        [
            ("Database", summary["filename"]),
            ("Path", summary["path"]),
            ("Folder", summary["folder"]),
            ("File size", _format_bytes(summary["file_size"])),
            ("Last modified", _timestamp_value(summary["file_modified"])),
            ("SQLite allocated size", _format_bytes(page_bytes)),
            ],
        [
            ("Database schema version", _display_value(summary["user_version"])),
            ("SQLite application_id", _display_value(summary["application_id"])),
            ],
        [
            ("Tables", _display_value(summary["table_count"])),
            ("Experiments", _display_value(summary["experiment_count"])),
            ("Runs", _display_value(summary["run_count"])),
            ],
        latest_run_rows,
        ]


def _format_database_info(summary):
    groups = _database_info_groups(summary)
    sections = [
        "\n".join(f"{label}: {_display_value(value)}" for label, value in group)
        for group in groups
        ]
    return "\n\n".join(sections)


def _format_bytes(value):
    if value is None:
        return "Unknown"

    value = int(value)
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units[:-1]:
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{value} B"
        size /= 1024
    return f"{size:.1f} {units[-1]}"


def _timestamp_value(value):
    if value in (None, ""):
        return "Not recorded"

    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(value)


def _display_value(value):
    if value in (None, ""):
        return "Unknown"
    return str(value)
