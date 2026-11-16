"""Volume and cut dependent views remain inside publication ownership."""

import numpy as np
import pytest

from qplot.windows._plot_publication import PublicationOutcome
from tests.support.windows.plot3d import build_volume, loaded
from tests.support.windows.plot3d import main as main
from tests.support.windows.plot_integration import (
    assert_database_artifacts_unchanged_except_trusted_shm,
    trusted_database_artifact_state,
    wait_for,
)


@pytest.mark.parametrize("profile", [False, True], ids=["volume", "cut"])
def test_dependent_view_failure_restores_metadata_and_allows_retry(tmp_path, main, monkeypatch, profile):
    window, errors = main
    path = tmp_path / "publication.db"
    guid = build_volume(path)
    before = trusted_database_artifact_state(path)
    assert window.load_file(str(path))
    wait_for(lambda: not window._database_load_active)
    window.openPlot(guid)
    wait_for(lambda: bool(window.windows))
    volume = window.windows[0]
    wait_for(lambda: loaded(volume))
    if profile:
        volume.openSweep("d")
        wait_for(lambda: len(window.windows) == 2)
    target = window.windows[-1]
    wait_for(lambda: loaded(target))
    previous_axes = target.axis_data
    prior_metadata = {name: target.__dict__.get(name) for name in target._extra_publication_fields}
    records = []
    owner = target._publication_owner()
    begin = owner.begin

    def capture(*args, **kwargs):
        record = begin(*args, **kwargs)
        records.append(record)
        return record

    monkeypatch.setattr(owner, "begin", capture)
    complete = target._complete_plot_publication

    def fail(worker):
        assert owner.current is not None
        assert owner.current.completing
        complete(worker)
        raise RuntimeError("injected dependent-view failure")

    with monkeypatch.context() as fault:
        fault.setattr(target, "_complete_plot_publication", fail)
        assert target.load_data()
        wait_for(lambda: loaded(target))
    assert records[-1].outcome is PublicationOutcome.RENDER_FAILED
    assert owner.current is None
    assert target.axis_data is previous_axes
    for name, value in prior_metadata.items():
        assert target.__dict__.get(name) is value
    assert not target._qplot_display_synchronized
    assert not target._refresh_controller().busy
    assert target.worker not in window._plot_workers
    assert target.load_data()
    wait_for(lambda: loaded(target))
    assert records[-1].outcome is PublicationOutcome.COMMITTED
    np.testing.assert_array_equal(target.axis_data["y"], previous_axes["y"])
    assert not errors
    assert_database_artifacts_unchanged_except_trusted_shm(path, before)
