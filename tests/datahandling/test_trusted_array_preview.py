"""Array-valued QCoDeS runs must work without a prior writer connection."""

import io
import sqlite3
from dataclasses import replace

import numpy as np
import pytest

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_array_preview import _decode
from qplot.datahandling.trusted_derived_rendering import (
    _UnsupportedNumericData,
    _viridis_rgba,
    render_trusted_derived_payload,
)
from qplot.datahandling.trusted_live_queries import (
    TRUSTED_DERIVED_MAX_ARRAY_BYTES,
    TrustedMetadataQueryAdapter,
    TrustedSourceRevisionNamespace,
)
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.datahandling.trusted_work_scheduler import TrustedWorkKind
from tests.datahandling.test_stage5c_real_qcodes import (
    _load_basic_runs,
    _ReadOnlySqliteExecutor,
)
from tests.datahandling.test_trusted_derived_rendering import (
    _decode_png_rgba,
    _observation,
)
from tests.datahandling.test_trusted_plot import make_run, protected_state, work


def _blob(values, version=(3, 0)):
    stream = io.BytesIO()
    np.lib.format.write_array(stream, np.asarray(values), version=version)
    return stream.getvalue()


def test_plot_initializes_converters_without_a_writer(tmp_path, monkeypatch):
    path = tmp_path / "arrays.db"
    _, guid, _ = make_run(path, arrays=True)
    before = protected_state(path)
    # Creating the fixture registers global SQLite converters, masking the
    # fresh-viewer bug unless they are removed before opening the reader.
    for name in ("ARRAY", "NUMERIC", "COMPLEX"):
        monkeypatch.delitem(sqlite3.converters, name, raising=False)
    service = TrustedLiveReadService(path)
    try:
        dataset = service.submit_plot_dataset(guid).wait()
        result = work(dataset)
        np.testing.assert_array_equal(result.dataGrid, np.arange(3)[:, None] * 10 + np.arange(4))
    finally:
        service.close()
    assert protected_state(path) == before


def test_real_array_capture_and_heatmap(tmp_path):
    path = tmp_path / "preview.db"
    run_id, _, _ = make_run(path, arrays=True)
    # This test executor uses stock SQLite; put its synthetic source in DELETE
    # mode so opening it does not create WAL sidecars.
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
    writer.close()
    before = protected_state(path)
    executor = _ReadOnlySqliteExecutor(path)
    try:
        adapter = TrustedMetadataQueryAdapter(executor, path)
        _load_basic_runs(adapter)
        observation = adapter.derived_source_observation(
            run_id, database_instance=database_instance(path),
            namespace=TrustedSourceRevisionNamespace.create(),
        )
        assert sum(len(value) for row in observation.sample_rows for value in row
                   if isinstance(value, bytes)) <= TRUSTED_DERIVED_MAX_ARRAY_BYTES
        payload = render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL)
        assert payload["status"] == "ok"
        image = dict(payload["images"][0])
        assert image["sampled_points"] == 12
        width, height, rgba = _decode_png_rgba(image["bytes"])
        # Declared first dependency is vertical, increasing upwards.
        assert tuple(rgba[:4]) == _viridis_rgba(20, 0, 23)
        assert tuple(rgba[(height - 1) * width * 4:][:4]) == _viridis_rgba(0, 0, 23)
    finally:
        executor.close()
    assert protected_state(path) == before


@pytest.mark.parametrize("version", [(1, 0), (2, 0), (3, 0)])
def test_array_line_preview_and_format_versions(version):
    observation = _observation()
    observation = replace(
        observation,
        parameters=tuple(replace(param, paramtype="array") for param in observation.parameters),
        sample_rows=((1, _blob([3., 2., 1.], version), _blob([9., 4., 1.], version)),),
    )
    payload = render_trusted_derived_payload(observation, TrustedWorkKind.PREVIEW)
    assert payload["status"] == "ok"
    assert dict(payload["images"][0])["sampled_points"] == 3


@pytest.mark.parametrize("blob", [b"invalid", _blob(np.array([object()], dtype=object)),
                                  _blob([1., 2.])[:-1]])
def test_invalid_arrays_are_rejected_without_pickle(blob):
    with pytest.raises(_UnsupportedNumericData):
        _decode(blob)


def test_missing_array_column_retains_its_coordinate_extent():
    observation = _observation(dimensions=2)
    observation = replace(
        observation,
        parameters=tuple(replace(param, paramtype="array") for param in observation.parameters),
        sample_rows=((1, _blob([1., 1., 0., 0.]), _blob([0., 1., 0., 1.]),
                      _blob([np.nan, 3., np.nan, 1.])),),
        validated_2d_layouts=(),
    )
    payload = render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL)
    image = dict(payload["images"][0])
    width, _, rgba = _decode_png_rgba(image["bytes"])
    assert tuple(rgba[:4]) == (230, 230, 230, 255)
    assert tuple(rgba[(width - 1) * 4:][:4]) == _viridis_rgba(3, 1, 3)
    assert image["sampled_points"] == 2


def test_array_setpoint_length_mismatch_is_not_broadcast():
    observation = _observation()
    observation = replace(
        observation,
        parameters=tuple(replace(param, paramtype="array") for param in observation.parameters),
        sample_rows=((1, _blob([1.]), _blob([2., 3.])),),
    )
    payload = render_trusted_derived_payload(observation, TrustedWorkKind.THUMBNAIL)
    assert payload["status"] == "unsupported"
    assert "sizes differ" in payload["description"]


def test_oversized_array_headers_rejected_before_allocation():
    stream = io.BytesIO()
    np.lib.format.write_array_header_2_0(
        stream, {"descr": "<f8", "fortran_order": False, "shape": (10**12,)},
    )
    with pytest.raises(_UnsupportedNumericData):
        _decode(stream.getvalue())
