"""Arrange an accepted resolution report through real publication ownership."""

from types import SimpleNamespace


def publish_resolution(window, result):
    worker = object()
    owner = window._publication_owner()
    record = owner.begin(worker, window.__dict__, cache=None, parameter_name="signal")
    window._stage_heatmap_resolution(result)
    assert owner.commit(record, worker)
    window._sync_heatmap_resolution_warning()
    return window._heatmap_resolution_report


def set_resolution(window, metadata):
    return publish_resolution(window, SimpleNamespace(heatmap_downsample_info=metadata))
