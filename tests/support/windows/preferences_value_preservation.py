"""Shared fixtures and assertions for preferences value preservation."""

import pytest

from qplot.configuration.config import config

__all__ = [
    'precise_config',
]


@pytest.fixture
def precise_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "default_path", str(tmp_path))
    monkeypatch.setattr(config, "default_file", str(tmp_path / "config.json"))
    cfg = config()
    cfg.update_many({
        "user_preference.axis_tick_width": 1.25,
        "user_preference.default_refresh_rate": 0.049,
        "runtime_settings.del_grace_period": 10.125,
        "runtime_settings.cloud_sync_timeout": 120.125,
    })
    return cfg
