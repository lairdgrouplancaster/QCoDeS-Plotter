from pathlib import Path
from types import SimpleNamespace

import pytest

from qplot.windows._csv_filenames import CSV_FILENAME_FORMAT_KEY, csv_filename
from qplot.windows._dataset_handle import DatasetKey
from qplot.windows._plot_actions import PlotActionsMixin
from tests.windows.test_preferences import FakeConfig


@pytest.mark.parametrize("style", ["current", "qcodes"])
def test_csv_suggestions_use_captured_id_for_qcodes(style, tmp_path):
    cfg = FakeConfig()
    cfg.values[CSV_FILENAME_FORMAT_KEY] = style
    guid = "12345678-0000-0000-0000-0123456789ab"
    metadata = {"captured_run_id": 42}
    host = SimpleNamespace(
        config=cfg,
        fileTextbox=SimpleNamespace(text=lambda: str(tmp_path / "data.db")),
        RunList=SimpleNamespace(
            _item_for_guid=lambda guid: SimpleNamespace(run_metadata=metadata),
            run_id_for_guid=lambda guid: 7,
        ),
        _safe_filename=lambda name: name,
    )
    dataset = SimpleNamespace(run_id=7, captured_run_id=42, guid=guid)
    key = DatasetKey(str(tmp_path / "data.db"), guid)
    params = [SimpleNamespace(name="signal")]
    expected = f"qcodes_42_{guid}.csv" if style == "qcodes" else "run_7_signal.csv"
    suggestions = [
        PlotActionsMixin._default_export_filename(host, dataset, params),
        PlotActionsMixin._default_run_csv_export_filename(host, key, ["signal"]),
        PlotActionsMixin._default_preview_export_filename(host, key, "signal"),
    ]
    assert all(Path(suggestion) == tmp_path / expected for suggestion in suggestions)


def test_missing_metadata_preserves_current_suggestion():
    cfg = FakeConfig()
    cfg.values[CSV_FILENAME_FORMAT_KEY] = "qcodes"
    assert csv_filename(cfg, "run_7_signal.csv", guid="guid", captured_run_id=None) == "run_7_signal.csv"
