"""CSV filename preferences; no database access is needed to suggest a name."""

CSV_FILENAME_FORMAT_KEY = "user_preference.csv_filename_format"
CSV_FILENAME_FORMAT_OPTIONS = (
    ("Current format", "current"),
    ("QCoDeS format (includes GUID)", "qcodes"),
)


def csv_filename(config, current_filename, *, guid, captured_run_id):
    """Use QCoDeS' default export naming when captured metadata is available."""
    try:
        use_qcodes = config.get(CSV_FILENAME_FORMAT_KEY) == "qcodes"
    except (AttributeError, KeyError):
        use_qcodes = False
    if use_qcodes and guid and captured_run_id is not None:
        filename = f"qcodes_{captured_run_id}_{guid}.csv"
        return "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)
    return current_filename
