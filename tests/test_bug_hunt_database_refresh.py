"""A failed status read must not consume the last acquisition commit."""

import time

import pytest

from qplot.datahandling import trusted_live_service as service_module
from qplot.datahandling.database import DatabaseRefreshWorker
from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_live import TrustedLiveBusyTimeoutError
from qplot.datahandling.trusted_live_queries import TrustedMetadataQueryAdapter
from qplot.datahandling.trusted_live_service import (
    TrustedLiveReadService,
    TrustedReadRequestDeadlineError,
)
from tests.datahandling.test_trusted_live_queries import _FakeExecutor
from tests.datahandling.test_trusted_live_service import _FakeSupervisor


class _MetadataSupervisor(_FakeSupervisor):
    def __init__(self, accepted, source):
        super().__init__(accepted)
        self.source = source
        self.fail_version = False

    def submit_query(self, sql, bindings=None, *, timeout=None):
        return self._new_job(sql, self.source.query(sql, bindings))

    def submit_query_batch(self, queries, *, timeout=None):
        return self._new_job("batch", self.source.query_batch(queries))

    def submit_data_version(self, *, timeout=None):
        job = self._new_job("version", self.source.current_data_version)
        if self.fail_version:
            self.fail_version = False
            job.failure = TrustedLiveBusyTimeoutError("temporary version contention")
        return job


@pytest.mark.parametrize("failed_kind", ["cheap", "expensive"])
@pytest.mark.parametrize(
    "failed_retry", [None, "version", "queued_deadline", "page", "cancel_after_page"]
)
def test_last_commit_status_is_retried_after_reusable_failure(
    tmp_path, monkeypatch, failed_kind, failed_retry
):
    path = tmp_path / "measurement.db"
    path.touch()
    accepted = database_instance(path)
    source = _FakeExecutor((1,))
    source.runs[1].update(is_completed=0, completed_timestamp=None)
    supervisor = _MetadataSupervisor(accepted, source)
    failure = [None]
    calls = []

    class Adapter(TrustedMetadataQueryAdapter):
        def basic_run_page(self, after_run_id, through_run_id):
            if failure[0] == "page":
                failure[0] = None
                raise TrustedLiveBusyTimeoutError("temporary page contention")
            return super().basic_run_page(after_run_id, through_run_id)

        def cheap_run(self, run_id):
            calls.append("cheap")
            if failure[0] == "cheap":
                failure[0] = None
                raise TrustedLiveBusyTimeoutError("temporary status contention")
            return super().cheap_run(run_id)

        def expensive_run(self, run_id):
            calls.append("expensive")
            if failure[0] == "expensive":
                failure[0] = None
                raise TrustedLiveBusyTimeoutError("temporary status contention")
            return super().expensive_run(run_id)

    monkeypatch.setattr(service_module, "TrustedMetadataQueryAdapter", Adapter)
    service = TrustedLiveReadService(
        path,
        expected_database_instance=accepted,
        supervisor_factory=lambda *args, **kwargs: supervisor,
    )
    guid = source.runs[1]["guid"]

    def refresh(*, cancel_after_page=False):
        worker = DatabaseRefreshWorker(
            1, str(path), 1, [(1, guid)],
            expected_database_instance=accepted,
            trusted_service=service,
        )
        outcomes = []
        worker.signals.finished.connect(lambda *args: outcomes.append(args))
        if cancel_after_page:
            wait = worker._wait_trusted_request

            def wait_then_cancel(request):
                result = wait(request)
                if request.identity.operation is service_module.TrustedReadOperation.BASIC_PAGE:
                    worker.cancel()
                return result

            worker._wait_trusted_request = wait_then_cancel
        worker.run()
        if cancel_after_page:
            assert outcomes == []
            return None
        assert len(outcomes) == 1
        return outcomes[0][3:]

    try:
        header = service.submit_bootstrap().wait(5)
        service.submit_basic_page(0, header.run_id_watermark).wait(5)
        statuses, error = refresh()
        assert error is None
        assert statuses[guid]["is_completed"] == 0
        baseline_calls = list(calls)
        assert refresh() == ({}, None)
        assert calls == baseline_calls

        # This is the writer's final commit: no later change can rescue a
        # prematurely accepted data_version baseline.
        source.runs[1].update(is_completed=1, completed_timestamp=2001.0)
        source.current_data_version += 1
        failure[0] = failed_kind
        statuses, error = refresh()
        assert statuses == {}
        assert "temporary status contention" in error
        assert not service.closed

        if failed_retry == "version":
            supervisor.fail_version = True
            assert "temporary version contention" in refresh()[1]
        elif failed_retry == "queued_deadline":
            request = service.submit_refresh(1, deadline=time.monotonic() - 1)
            with pytest.raises(TrustedReadRequestDeadlineError):
                request.wait(5)
        elif failed_retry == "page":
            failure[0] = "page"
            assert "temporary page contention" in refresh()[1]
        elif failed_retry == "cancel_after_page":
            refresh(cancel_after_page=True)

        statuses, error = refresh()
        assert error is None
        assert statuses[guid]["is_completed"] == 1
        assert statuses[guid]["completed_timestamp"] == 2001.0
        recovered_calls = list(calls)
        assert refresh() == ({}, None)
        assert calls == recovered_calls
    finally:
        service.close()
