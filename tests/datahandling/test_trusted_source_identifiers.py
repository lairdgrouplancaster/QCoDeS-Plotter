"""Source SQL identifiers remain exact when their display values are shortened."""

import hashlib

import pytest
from qcodes.dataset.data_set import new_data_set
from qcodes.dataset.descriptions.dependencies import InterDependencies_
from qcodes.dataset.experiment_container import new_experiment
from qcodes.dataset.sqlite.database import connect
from qcodes.parameters import ParamSpecBase

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_live import TrustedLiveReader, TrustedQueryResult
from qplot.datahandling.trusted_live_queries import (
    TrustedMetadataQueryAdapter,
    TrustedMetadataQueryError,
    TrustedSourceRevisionNamespace,
)
from qplot.datahandling.trusted_live_service import TrustedLiveReadService
from qplot.datahandling.trusted_presentation import (
    TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES,
)


def _create_source(path, prefix):
    writer = connect(path)
    try:
        experiment = new_experiment(
            "audit", "sample", format_string=prefix + "_{}_{:d}_{:d}", conn=writer
        )
        dataset = new_data_set("source", exp_id=experiment.exp_id, conn=writer)
        x = ParamSpecBase("x", "numeric", label="X", unit="V")
        y = ParamSpecBase("y", "numeric", label="Y", unit="A")
        dataset.prepare(snapshot={}, interdeps=InterDependencies_(dependencies={y: (x,)}))
        dataset.add_results([{"x": 1.0, "y": 2.0}])
        dataset.mark_completed()
        run_id, table_name = dataset.run_id, dataset.table_name
    finally:
        writer.close()
    return run_id, table_name


def _protected_state(path):
    state = {}
    for suffix in ("", "-wal", "-journal"):
        member = path.with_name(path.name + suffix)
        state[suffix] = (
            (hashlib.sha256(member.read_bytes()).digest(), member.stat().st_mtime_ns)
            if member.exists()
            else None
        )
    return state


@pytest.mark.parametrize("prefix", ["q" * 499, "q" * 500, "q" * 600, "μ" * 260])
def test_qcodes_table_identity_survives_bounded_run_presentation(tmp_path, prefix):
    path = tmp_path / "source.db"
    run_id, table_name = _create_source(path, prefix)
    before = _protected_state(path)

    service = TrustedLiveReadService(path)
    try:
        service.submit_bootstrap().wait(10)
        service.submit_basic_page(0, run_id).wait(10)
        # A direct expensive request also works before cheap detail has filled
        # in the table-name placeholder omitted by the basic page.
        assert service.submit_expensive_run(run_id).wait(10).as_dict()["result_count"] == 1
        cheap = service.submit_cheap_run(run_id).wait(10).as_dict()
        display_name = cheap["result_table_name"]
        assert len(display_name.encode("utf-8")) <= TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES
        if len(table_name.encode("utf-8")) > TRUSTED_PRESENTATION_MAX_FIELD_VALUE_BYTES:
            assert display_name != table_name
        assert service.submit_expensive_run(run_id).wait(10).as_dict()["result_count"] == 1
        service.submit_selected_run(run_id).wait(10)
        source = service.submit_derived_source(run_id).wait(10)
        assert source.result_table_name == table_name
        assert source.sample_rows == ((1, 1.0, 2.0),)
        assert source.result_watermark == 1
    finally:
        service.close(timeout=10)
    assert _protected_state(path) == before


def test_derived_identity_recheck_rejects_equal_display_prefixes(tmp_path):
    path = tmp_path / "source.db"
    run_id, first_table = _create_source(path, "q" * 600)
    second_run_id, second_table = _create_source(path, "q" * 600)
    assert run_id != second_run_id and first_table != second_table
    assert first_table[:509] == second_table[:509]
    before = _protected_state(path)

    class Executor:
        incarnation = 1
        sampled = False

        def query(self, *args, **kwargs):
            result = reader.query(*args, **kwargs)
            if self.sampled and result.columns == ("result_table_name",):
                # Deterministically simulate observing another exact table
                # after the extraction batch, without changing either source.
                return TrustedQueryResult(result.columns, ((second_table,),))
            return result

        def query_batch(self, queries, **kwargs):
            results = reader.query_batch(queries, **kwargs)
            if results[0].columns == (
                "run_id", "guid", "result_table_name", "parameters", "run_description"
            ):
                self.sampled = True
            return results

        def data_version(self, **kwargs):
            return reader.data_version(**kwargs)

    with TrustedLiveReader(path) as reader:
        executor = Executor()
        adapter = TrustedMetadataQueryAdapter(executor, path)
        header = adapter.bootstrap()
        adapter.basic_run_page(0, header.run_id_watermark)
        with pytest.raises(TrustedMetadataQueryError, match="accepted run identity changed"):
            adapter.derived_source_observation(
                run_id,
                database_instance=database_instance(path),
                namespace=TrustedSourceRevisionNamespace.create(),
            )
        assert executor.sampled
    assert _protected_state(path) == before
