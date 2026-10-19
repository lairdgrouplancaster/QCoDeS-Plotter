"""Acquisition defaults must not publish generator data outside its output."""

import sqlite3
from pathlib import Path

import pytest
import qcodes
from qcodes.dataset import initialise_or_create_database_at
from qcodes.parameters import Parameter
from qcodes.station import Station

from qplot.datahandling.readonly import load_by_id_read_only
from qplot.testdata import RunSpecification, SpecificationError, generate_database, main


@pytest.fixture
def isolated_acquisition_defaults(monkeypatch):
    monkeypatch.setitem(qcodes.config.dataset, "raw_data_backend", "sqlite_main_db")
    monkeypatch.setitem(qcodes.config.dataset, "export_automatic", False)
    monkeypatch.setitem(qcodes.config.subscription, "default_subscribers", [])


def _specification():
    return RunSpecification(1, "current", "Current", "A", 0.0, 1.0, 3)


def _tree_state(root):
    return {
        str(path.relative_to(root)): (
            path.stat().st_mtime_ns, path.read_bytes() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


def marker_subscriber_factory(_dataset, *, marker):
    """Harmless real QCoDeS subscriber proving inherited callbacks run."""
    Path(marker).write_text("inherited subscriber ran")
    return lambda _results, _length, _state: None


def _configure_external_storage(kind, root, monkeypatch):
    if kind == "split":
        monkeypatch.setitem(
            qcodes.config.dataset, "raw_data_backend", "sqlite_per_dataset_db",
        )
        monkeypatch.setitem(
            qcodes.config.dataset.raw_data_backend_config.sqlite_per_dataset_db,
            "raw_data_path", str(root / "unselected-raw"),
        )
        return "raw_data_backend"

    if kind == "subscriber":
        monkeypatch.setitem(
            qcodes.config.subscription, "default_subscribers", ["qplot_probe"],
        )
        monkeypatch.setitem(qcodes.config.subscription.subscribers, "qplot_probe", {
            "factory": f"{__name__}.marker_subscriber_factory",
            "factory_kwargs": {"marker": str(root / "unselected-marker")},
            "subscription_kwargs": {"min_wait": 0, "min_count": 1},
        })
        return "default_subscribers"

    external = root / "unselected-exports"
    external.mkdir()
    # A QCoDeS main may have any filename. Its content must be preserved even
    # when it occupies the filename QCoDeS automatic CSV export would choose.
    initialise_or_create_database_at(str(external / "qcodes_1.csv"), journal_mode="DELETE")
    for key, value in {
        "export_automatic": True,
        "export_type": "CSV",
        "export_path": str(external),
        "export_prefix": "qcodes_",
        "export_name_elements": ["captured_run_id"],
    }.items():
        monkeypatch.setitem(qcodes.config.dataset, key, value)
    return "export_automatic"


@pytest.mark.parametrize("kind", ["split", "automatic_export", "subscriber"])
@pytest.mark.parametrize("existing_output", [False, True])
def test_generator_rejects_external_acquisition_defaults_before_creating_files(
    tmp_path, monkeypatch, isolated_acquisition_defaults, kind, existing_output,
):
    output = tmp_path / "new-directory" / "selected.db"
    if existing_output:
        output.parent.mkdir()
        initialise_or_create_database_at(str(output), journal_mode="DELETE")
    setting = _configure_external_storage(kind, tmp_path, monkeypatch)
    configuration = repr(qcodes.config.current_config)
    before = _tree_state(tmp_path)

    with pytest.raises(SpecificationError, match=setting):
        generate_database([_specification()], output, overwrite=existing_output)

    assert _tree_state(tmp_path) == before
    assert repr(qcodes.config.current_config) == configuration


@pytest.mark.parametrize("kind", ["split", "automatic_export", "subscriber"])
def test_generator_cli_reports_external_storage_configuration(
    tmp_path, monkeypatch, isolated_acquisition_defaults, kind, capsys,
):
    source = tmp_path / "instructions.csv"
    source.write_text(
        "dimensions,measured_name,measured_label,measured_unit,v_sd_start,"
        "v_sd_stop,v_sd_points,v_g_start,v_g_stop,v_g_points\n"
        "1,current,Current,A,0,1,3,,,\n",
    )
    setting = _configure_external_storage(kind, tmp_path, monkeypatch)
    before = _tree_state(tmp_path)

    with pytest.raises(SystemExit) as raised:
        main([str(source), str(tmp_path / "output.db")])

    assert raised.value.code == 2
    assert setting in capsys.readouterr().err
    assert _tree_state(tmp_path) == before


def test_generator_main_backend_produces_self_contained_readable_output(
    tmp_path, monkeypatch, isolated_acquisition_defaults,
):
    monkeypatch.setitem(qcodes.config.dataset, "export_type", "CSV")
    monkeypatch.setitem(qcodes.config.dataset, "export_path", str(tmp_path / "unselected"))
    configuration = repr(qcodes.config.current_config)
    output = tmp_path / "selected.db"

    assert generate_database([_specification()], output) == output

    assert sorted(path.name for path in tmp_path.iterdir()) == ["selected.db"]
    assert repr(qcodes.config.current_config) == configuration
    with sqlite3.connect(output.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
        table = connection.execute("SELECT result_table_name FROM runs").fetchone()[0]
        assert connection.execute(f'SELECT count(*) FROM "{table}"').fetchone() == (3,)
    connection.close()
    dataset = load_by_id_read_only(1, output)
    try:
        values = dataset.get_parameter_data()["current"]
        assert values["V_SD"].tolist() == [0.0, 0.5, 1.0]
        assert values["current"].shape == (3,)
    finally:
        dataset.conn.close()


def test_generator_does_not_snapshot_or_read_the_default_acquisition_station(
    tmp_path, monkeypatch, isolated_acquisition_defaults,
):
    marker = tmp_path / "unselected-marker"
    reads = []

    def acquire_parameter():
        reads.append(True)
        marker.write_text("acquisition parameter read")
        return 1.0

    # Restore the original global even if the regression invokes this source.
    monkeypatch.setattr(Station, "default", Station.default)
    parameter = Parameter("acquisition_read", get_cmd=acquire_parameter)
    acquisition_station = Station(parameter, update_snapshot=False)
    assert not parameter.cache.valid
    assert not reads and not marker.exists()
    configuration = repr(qcodes.config.current_config)
    output = tmp_path / "synthetic.db"

    assert generate_database([_specification()], output) == output

    assert not reads and not marker.exists()
    assert Station.default is acquisition_station
    assert not parameter.cache.valid
    assert repr(qcodes.config.current_config) == configuration
    dataset = load_by_id_read_only(1, output)
    try:
        assert dataset.number_of_results == 3
        assert dataset.get_parameter_data()["current"]["V_SD"].tolist() == [0.0, 0.5, 1.0]
    finally:
        dataset.conn.close()
