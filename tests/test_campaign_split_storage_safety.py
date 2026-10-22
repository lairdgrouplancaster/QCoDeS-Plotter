"""Separate QCoDeS result databases must never escape qPlot's read boundary."""

import gc
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import qcodes

from qplot.datahandling.readonly import (
    ReadOnlyDatabaseAccessError,
    load_by_guid_read_only,
    load_by_id_read_only,
)
from qplot.datahandling.trusted_live import TrustedLiveQueryError
from qplot.datahandling.trusted_live_service import TrustedLiveReadService

_CREATE_MIXED_DATABASE = """
import json, os, sys
from pathlib import Path
import qcodes
from qcodes.dataset import Measurement, initialise_or_create_database_at, load_or_create_experiment

path = sys.argv[1]
qcodes.config.dataset.raw_data_backend = "sqlite_per_dataset_db"
qcodes.config.dataset.raw_data_backend_config.sqlite_per_dataset_db.raw_data_path = str(Path(path).parent / "raw")
initialise_or_create_database_at(path, journal_mode="DELETE")
experiment = load_or_create_experiment("split", sample_name="disposable regression")

def measure(name, split):
    measurement = Measurement(exp=experiment, name=name)
    measurement.register_custom_parameter("x")
    measurement.register_custom_parameter("y", setpoints=("x",))
    with measurement.run() as saver:
        dataset = saver.dataset
        if split:
            dataset._results_conn.execute("PRAGMA journal_mode=WAL")
            dataset._results_conn.execute("PRAGMA wal_autocheckpoint=0")
        for index in range(10):
            saver.add_result(("x", index), ("y", 2 * index))
        saver.flush_data_to_database(block=True)
    return dataset

split = measure("split", True)
qcodes.config.dataset.raw_data_backend = "sqlite_main_db"
ordinary = measure("ordinary", False)
print(json.dumps({"split_id": split.run_id, "split_guid": split.guid,
                  "ordinary_id": ordinary.run_id, "ordinary_guid": ordinary.guid,
                  "raw": split._results_backend.results_db_path}), flush=True)
# A stopped acquiring process can leave committed raw results in WAL. No
# synthetic headers or forged database metadata are used in this regression.
os._exit(0)
"""


def _family_state(path):
    result = {}
    for suffix in ("", "-wal", "-journal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            info = candidate.stat()
            result[suffix] = (
                info.st_size, info.st_mtime_ns,
                hashlib.sha256(candidate.read_bytes()).hexdigest(),
            )
    return result


@pytest.fixture
def mixed_database(tmp_path):
    backends = qcodes.config.defaults["dataset"].get("raw_data_backend_config", {})
    if "sqlite_per_dataset_db" not in backends:
        pytest.skip("The installed QCoDeS does not support the sqlite_per_dataset_db backend.")
    main = tmp_path / "mixed.db"
    created = subprocess.run(
        [sys.executable, "-c", _CREATE_MIXED_DATABASE, str(main)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    info = json.loads(created.stdout.strip().splitlines()[-1])
    raw = Path(info["raw"])
    assert Path(f"{raw}-wal").stat().st_size > 0
    return main, raw, info


@pytest.mark.parametrize("lookup", ["id", "guid"])
def test_split_storage_load_is_rejected_before_opening_raw_file(
    mixed_database, monkeypatch, lookup,
):
    main, raw, info = mixed_database
    before = _family_state(main), _family_state(raw)
    real_connect = sqlite3.connect
    opened = []

    def observe_connect(database, *args, **kwargs):
        opened.append(os.fspath(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", observe_connect)
    loader = load_by_id_read_only if lookup == "id" else load_by_guid_read_only
    with pytest.raises(ReadOnlyDatabaseAccessError, match="sqlite_per_dataset_db"):
        loader(info[f"split_{lookup}"], main)
    gc.collect()
    assert all(str(raw) not in path for path in opened)
    assert (_family_state(main), _family_state(raw)) == before

    # Presence of the optional column must not disable ordinary runs in the
    # same current-QCoDeS database, including each read-only loader.
    dataset = loader(info[f"ordinary_{lookup}"], main)
    try:
        assert dataset.number_of_results == 10
        assert dataset.get_parameter_data()["y"]["y"].tolist() == list(range(0, 20, 2))
    finally:
        dataset.conn.close()
    del dataset
    gc.collect()
    assert (_family_state(main), _family_state(raw)) == before


def test_trusted_mixed_database_rejects_only_external_measurement_actions(mixed_database):
    main, raw, info = mixed_database
    before = _family_state(main), _family_state(raw)
    service = TrustedLiveReadService(main)
    try:
        bootstrap = service.submit_bootstrap().wait(30)
        page = service.submit_basic_page(0, bootstrap.run_id_watermark).wait(30)
        assert len(page.runs) == 2
        assert service.submit_selected_run(info["split_id"]).wait(30).run.run_id == info["split_id"]
        for request in (
            service.submit_expensive_run(info["split_id"]),
            service.submit_derived_source(info["split_id"]),
            service.submit_plot_dataset(info["split_guid"]),
        ):
            with pytest.raises(TrustedLiveQueryError, match="sqlite_per_dataset_db"):
                request.wait(30)
        assert not service.closing and not service.closed
        assert service.submit_expensive_run(info["ordinary_id"]).wait(30).as_dict()["result_count"] == 10
        dataset = service.submit_plot_dataset(info["ordinary_guid"]).wait(30)
        prefix = service.submit_plot_prefix(dataset).wait(30)
        assert prefix.row_count == 10
    finally:
        service.close(timeout=30)
    gc.collect()
    assert (_family_state(main), _family_state(raw)) == before
