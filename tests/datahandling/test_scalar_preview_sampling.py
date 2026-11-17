"""Dense scalar traces must not acquire gaps from preview sampling."""

import math

import numpy as np
import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.trusted_derived_rendering import render_trusted_derived_payload
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.datahandling.trusted_query_types import TRUSTED_DERIVED_MAX_SAMPLE_ROWS
from qplot.datahandling.trusted_work_scheduler import RenderingOptions, TrustedWorkKind
from tests.support.datahandling.trusted_derived_rendering import _decode_png_rgba
from tests.support.datahandling.trusted_plot import protected_state


@pytest.mark.parametrize("output_count", [2, 3])
@pytest.mark.parametrize("storage", ["blocks", "interleaved"])
@pytest.mark.parametrize("count", [7, 13_730])
def test_dense_noise_preview_covers_each_trace_without_artificial_gaps(
    tmp_path, output_count, storage, count,
):
    path = tmp_path / "noise.db"
    initialise_or_create_database_at(str(path), journal_mode="WAL")
    experiment = load_or_create_experiment("noise", sample_name="synthetic")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("time")
    names = tuple(f"signal_{index}" for index in range(output_count))
    for name in names:
        measurement.register_custom_parameter(name, setpoints=("time",))
    coordinates = np.arange(count, dtype=float)
    signals = np.random.default_rng(20261112).normal(size=(output_count, count))
    dataset = None
    service = None
    try:
        with measurement.run(write_in_background=False) as saver:
            dataset = saver.dataset
            if storage == "blocks":
                # Numeric vector acquisition creates one physical block per
                # dependent, as in the reported time-trace measurement.
                saver.add_result(("time", coordinates), *zip(names, signals, strict=True))
            else:
                for index, coordinate in enumerate(coordinates):
                    saver.add_result(("time", coordinate), *(
                        (name, signal[index]) for name, signal in zip(names, signals, strict=True)
                    ))
            saver.flush_data_to_database(block=True)
        before = protected_state(path)
        service = TrustedLiveReadService(path)
        service.submit_bootstrap().wait()
        service.submit_basic_page(0, dataset.run_id).wait()
        observation = service.submit_derived_source(dataset.run_id).wait()
        assert observation.result_watermark == count * output_count
        assert len(observation.sample_rows) <= TRUSTED_DERIVED_MAX_SAMPLE_ROWS
        assert observation.sample_rows[0][0] == 1
        assert observation.sample_rows[-1][0] == observation.result_watermark
        time_column = observation.sample_columns.index("time")
        max_gap = 2 * output_count * math.ceil(
            count * output_count / TRUSTED_DERIVED_MAX_SAMPLE_ROWS
        )
        for name in names:
            column = observation.sample_columns.index(name)
            acquired = [row[time_column] for row in observation.sample_rows
                        if row[column] is not None]
            if count == 7:
                np.testing.assert_array_equal(acquired, coordinates)
            assert acquired[0] <= max_gap
            assert acquired[-1] >= count - 1 - max_gap
            assert max(np.diff(acquired)) <= max_gap

        payload = render_trusted_derived_payload(
            observation, TrustedWorkKind.PREVIEW,
            RenderingOptions.from_mapping({"width": 200, "height": 200}),
        )
        assert payload["status"] == "ok", payload
        assert len(payload["images"]) == output_count
        for image in payload["images"]:
            width, height, rgba = _decode_png_rgba(dict(image)["bytes"])
            pixels = np.frombuffer(rgba, dtype=np.uint8).reshape(height, width, 4)
            ink = np.any(pixels[:, :, :3] != 255, axis=2)
            # A noise envelope spans many vertical pixels throughout the
            # image. Joining sparse windows instead produces long thin lines.
            spans = [np.ptp(np.flatnonzero(ink[:, column]))
                     for column in range(11, width - 11)]
            if count > 7:
                assert np.mean(np.asarray(spans) > height // 10) > 0.95
        service.close()
        service = None
        assert protected_state(path) == before
    finally:
        if service is not None:
            service.close()
        if dataset is not None:
            dataset.conn.close()
        experiment.conn.close()
