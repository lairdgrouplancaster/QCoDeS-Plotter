"""Shared fixtures and assertions for preferences."""

from qplot.windows._preferences import (
    AXIS_MAJOR_TICK_COUNT_KEY,
    AXIS_TICK_WIDTH_KEY,
    COLORBAR_WIDTH_KEY,
    COPY_PLOT_IMAGE_RESOLUTION_KEY,
    COPY_PLOT_IMAGE_RESOLUTION_SCREEN,
)
from qplot.windows._window_controls import (
    CONFIRM_CLOSE_ALL_KEY,
    CONFIRM_INCOMPATIBLE_AXES_KEY,
    CONFIRM_QUIT_KEY,
)

__all__ = [
    'FakeConfig',
]


class FakeConfig:
    def __init__(self):
        self.defaults = {
            "user_preference.theme": "light",
            "user_preference.bar_colour": "viridis",
            "user_preference.trace_colour": "#1f77b4",
            "user_preference.trace_line_width": 2.0,
            "user_preference.trace_line_style": "Solid",
            "user_preference.font_size": 10,
            "user_preference.show_source_annotation": True,
            "user_preference.cursor_significant_figures": 6,
            "user_preference.complex_1d": "magnitude",
            "user_preference.complex_nd": "magnitude",
            COLORBAR_WIDTH_KEY: 15,
            AXIS_TICK_WIDTH_KEY: 2.0,
            AXIS_MAJOR_TICK_COUNT_KEY: 3,
            "GUI.preview_size": 200,
            "user_preference.plot_tile_rows": 2,
            "user_preference.plot_tile_columns": 2,
            COPY_PLOT_IMAGE_RESOLUTION_KEY: COPY_PLOT_IMAGE_RESOLUTION_SCREEN,
            "user_preference.csv_filename_format": "current",
            "file.default_load_path": "",
            "user_preference.default_refresh_rate": 1.0,
            CONFIRM_CLOSE_ALL_KEY: True,
            CONFIRM_INCOMPATIBLE_AXES_KEY: True,
            CONFIRM_QUIT_KEY: True,
            "runtime_settings.max_threads": 4,
            "runtime_settings.max_full_heatmap_points": 2_000_000,
            "runtime_settings.max_heatmap_grid_cells": 250_000,
            "runtime_settings.max_heatmap_grid_side": 800,
            "runtime_settings.del_grace_period": 10.0,
            "runtime_settings.cloud_sync_timeout": 120.0,
            "updates.check_on_launch": False,
            "updates.include_prereleases": False,
            }
        self.values = dict(self.defaults)
        self.updates = []
        self.schema = self._schema_for_defaults()

    def get(self, key):
        return self.values[key]

    def update(self, key, value):
        self.updates.append((key, value))
        self.values[key] = value

    def update_many(self, values):
        self.updates.extend(values.items())
        self.values.update(values)

    def _schema_for_defaults(self):
        schema = {"properties": {}}
        for key, value in self.defaults.items():
            section, name = key.split(".")
            section_schema = schema["properties"].setdefault(
                section,
                {"properties": {}},
                )
            section_schema["properties"][name] = {"default": value}
        return schema
