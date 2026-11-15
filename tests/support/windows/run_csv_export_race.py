"""Shared fixtures and assertions for run csv export race."""

from pathlib import Path

from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)
from qcodes.parameters import ManualParameter

__all__ = [
    '_create_run',
]


def _create_run(database_path: Path, row_count: int):
    initialise_or_create_database_at(
        str(database_path),
        journal_mode="DELETE",
    )
    experiment = load_or_create_experiment(
        f"csv_export_{database_path.stem}",
        sample_name="sample",
    )
    x = ManualParameter("x")
    signal = ManualParameter("signal")
    measurement = Measurement(exp=experiment, name="csv_export")
    measurement.register_parameter(x)
    measurement.register_parameter(signal, setpoints=(x,))
    with measurement.run(write_in_background=False) as datasaver:
        for index in range(row_count):
            datasaver.add_result((x, index), (signal, 10 + index))
        dataset = datasaver.dataset
        run_id = dataset.run_id
        guid = dataset.guid
    dataset.conn.close()
    # Measurement retains the experiment's independent connection.  Closing
    # only the dataset connection leaves the source database locked on
    # Windows, preventing the replacement race this test is meant to model.
    experiment.conn.close()
    return run_id, guid
