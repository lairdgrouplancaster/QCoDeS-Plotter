"""Plot grid preferences persist and control sizing, placement, and wrapping."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from jsonschema import ValidationError
from PyQt6 import QtCore
from PyQt6 import QtWidgets as qtw

from qplot.configuration.config import config
from qplot.windows._plot_tiling import (
    PLOT_TILE_COLUMNS_KEY,
    PLOT_TILE_ROWS_KEY,
    place_plot_window,
    plot_tile_grid,
    plot_tile_size,
)
from qplot.windows._preferences import PreferencesDialog


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "default_path", str(tmp_path))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "config.json"))
    return config()


def test_default_grid_preserves_window_size(cfg):
    assert plot_tile_grid(cfg) == (2, 2)
    assert plot_tile_size(cfg, QtCore.QRect(0, 0, 2000, 1000)) == (940, 470)


@pytest.mark.parametrize("rows, columns, size, positions", [
    (1, 3, (626, 940), [(100, 50), (766, 50), (1433, 50), (100, 50)]),
    (3, 1, (1880, 313), [(100, 50), (100, 383), (100, 716), (100, 50)]),
    (2, 2, (940, 470), [(100, 50), (1100, 50), (100, 550), (1100, 550), (100, 50)]),
])
def test_grid_sizes_and_places_windows_in_order(cfg, rows, columns, size, positions):
    cfg.update_many({PLOT_TILE_ROWS_KEY: rows, PLOT_TILE_COLUMNS_KEY: columns})
    rect = QtCore.QRect(100, 50, 2000, 1000)
    assert plot_tile_size(cfg, rect) == size
    owner = SimpleNamespace(config=cfg, screenrect=rect)
    window = Mock()
    for position in positions:
        place_plot_window(owner, window)
        window.move.assert_called_with(*position)


def test_changed_grid_restarts_placement(cfg):
    owner = SimpleNamespace(config=cfg, screenrect=QtCore.QRect(-2000, 50, 2000, 1000))
    window = Mock()
    place_plot_window(owner, window)
    place_plot_window(owner, window)
    cfg.update(PLOT_TILE_COLUMNS_KEY, 3)
    place_plot_window(owner, window)
    window.move.assert_called_with(-2000, 50)
    place_plot_window(owner, window)
    window.move.assert_called_with(-1334, 50)


def test_preferences_persist_and_restore_grid(cfg, monkeypatch):
    dialog = PreferencesDialog(cfg)
    try:
        assert dialog.plotTileRowsSpin.value() == 2
        assert dialog.plotTileColumnsSpin.value() == 2
        dialog.plotTileRowsSpin.setValue(1)
        dialog.plotTileColumnsSpin.setValue(3)
        assert dialog.apply_preferences()
        assert plot_tile_grid(config()) == (1, 3)
        monkeypatch.setattr(
            qtw.QMessageBox, "question",
            lambda *args, **kwargs: qtw.QMessageBox.StandardButton.Yes,
        )
        assert dialog.restore_defaults()
        assert plot_tile_grid(config()) == (1, 3)
        assert dialog.apply_preferences()
        assert plot_tile_grid(config()) == (2, 2)
    finally:
        dialog.close()


@pytest.mark.parametrize("key", [PLOT_TILE_ROWS_KEY, PLOT_TILE_COLUMNS_KEY])
@pytest.mark.parametrize("value", [0, -1, 11, 1.5, "2", True])
def test_grid_rejects_invalid_values(cfg, key, value):
    with pytest.raises(ValidationError):
        cfg.update(key, value)
    assert plot_tile_grid(config()) == (2, 2)
