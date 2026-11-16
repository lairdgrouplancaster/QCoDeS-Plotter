"""Own independent snapshot metadata jobs and fence their UI publication."""

from __future__ import annotations

from typing import Any, Protocol

from qplot.datahandling.file_identity import DatabaseInstance, logical_database_path
from qplot.datahandling.readonly import DatabaseInstanceChangedError
from qplot.diagnostics import log_exception

from ._database_detail_state import (
    BASIC_METADATA_COLUMNS,
    EXPENSIVE_METADATA_COLUMNS,
    DatabaseDetailChannels,
    DetailLoadChannel,
    DetailWorker,
)

RunMetadata = dict[int, dict[str, Any]]


class DatabaseDetailHost(Protocol):
    def create_worker(
        self, expensive: bool, generation: int, path: str, run_ids: list[Any],
        worker_kwargs: dict[str, Any],
    ) -> DetailWorker: ...
    def connect_worker(self, worker: DetailWorker, expensive: bool) -> None: ...
    def submit_worker(self, worker: DetailWorker, expensive: bool) -> None: ...
    def priority_run_ids(self, run_ids: Any = None) -> list[Any]: ...
    def set_loading(
        self, columns: tuple[str, ...], *, loading: bool,
        run_ids: list[Any] | None = None, guids: list[Any] | None = None,
    ) -> None: ...
    def can_publish(self, path: str) -> bool: ...
    def source_current(
        self, path: str, instance: DatabaseInstance | None, *, after_publication: bool = False,
    ) -> bool: ...
    def publish(self, runs: RunMetadata) -> None: ...
    def show_status(self, message: str, timeout: int) -> None: ...
    def reload(self, path: str) -> None: ...


class DatabaseDetailController:
    def __init__(self, host: DatabaseDetailHost) -> None:
        self.host = host
        self.channels = DatabaseDetailChannels()

    def channel(self, expensive: bool = False) -> DetailLoadChannel:
        return self.channels.expensive if expensive else self.channels.basic

    @staticmethod
    def columns(expensive: bool) -> tuple[str, ...]:
        return EXPENSIVE_METADATA_COLUMNS if expensive else BASIC_METADATA_COLUMNS

    @staticmethod
    def _cancel(worker: DetailWorker | None) -> None:
        if worker is not None:
            try:
                worker.cancel()
            except Exception as error:
                log_exception("Database detail cancellation failed", error, __name__)

    def invalidate(self, *, cancel: bool = True, clear_loading: bool = False) -> None:
        retired = []
        for expensive in (False, True):
            channel = self.channel(expensive)
            worker = channel.worker
            channel.invalidate()
            retired.append((expensive, channel.generation, worker))
        # Both channels must reject queued callbacks before calling either worker.
        for _, _, worker in retired:
            if cancel:
                self._cancel(worker)
        if clear_loading:
            for expensive, generation, _ in retired:
                self._clear_loading(expensive, generation)

    def _clear_loading(self, expensive: bool, generation: int) -> None:
        if self.channel(expensive).generation == generation:
            try:
                self.host.set_loading(self.columns(expensive), loading=False)
            except Exception as error:
                log_exception("Database detail loading indicator recovery failed", error, __name__)

    def cancel(self) -> None:
        self.invalidate(clear_loading=True)

    def _failed(self, expensive: bool, error: object) -> None:
        diagnostic = "Expensive database detail load failed" if expensive else "Database detail load failed"
        message = "Setpoint and size loading failed" if expensive else "Run detail loading failed"
        log_exception(diagnostic, error, __name__)
        self.host.show_status(f"{message}: {error}", 5000)

    def start_all(
        self, path: str, run_ids: list[Any], instance: DatabaseInstance | None,
    ) -> bool:
        expected = [self.channel(stage).generation for stage in (False, True)]
        for expensive in (False, True):
            expected[int(expensive)] += 1
            self.start(
                path, run_ids, instance, {"expected_database_instance": instance}, expensive=expensive,
            )
            if expected != [self.channel(stage).generation for stage in (False, True)]:
                # A nested selection/cancellation owns both stages from here.
                return False
        return True

    def start(
        self, path: str, run_ids: list[Any], instance: DatabaseInstance | None,
        worker_kwargs: dict[str, Any], *, expensive: bool,
    ) -> bool:
        channel = self.channel(expensive)
        previous = channel.worker
        channel.invalidate()
        generation = channel.generation
        channel.active = True
        channel.instance = instance
        channel.path = path
        self._cancel(previous)
        worker = None

        def current() -> bool:
            return channel.accepts(generation)

        try:
            if not current():
                return False
            worker = self.host.create_worker(expensive, generation, path, run_ids, worker_kwargs)
            if not current():
                self._cancel(worker)
                return False
            channel.worker = worker
            priority = self.host.priority_run_ids()
            if current():
                worker.prioritize_run_ids(priority)
            if current():
                self.host.connect_worker(worker, expensive)
            if current():
                self.host.set_loading(self.columns(expensive), loading=False)
            if current():
                self.host.set_loading(self.columns(expensive), run_ids=run_ids, loading=True)
            if not current():
                self._cancel(worker)
                return False
            self.host.submit_worker(worker, expensive)
        except Exception as error:
            if current():
                channel.clear()
                channel.failed = True
                self._cancel(worker)
                self._clear_loading(expensive, generation)
                if channel.generation == generation:
                    self._failed(expensive, error)
            else:
                self._cancel(worker)
            return False
        return True

    def prioritize(self, run_ids: Any = None) -> None:
        if not any(self.channel(expensive).active for expensive in (False, True)):
            return
        priority = self.host.priority_run_ids(run_ids)
        for expensive in (False, True):
            channel = self.channel(expensive)
            if channel.active and channel.worker is not None:
                channel.worker.prioritize_run_ids(priority)

    def status(self, generation: int, message: str, *, expensive: bool) -> None:
        if self.channel(expensive).accepts(generation):
            self.host.show_status(message, 0)

    def _accepts(self, channel: DetailLoadChannel, generation: int, path: str) -> bool:
        return (
            channel.accepts(generation)
            and (channel.path is None or logical_database_path(path) == logical_database_path(channel.path))
            and self.host.can_publish(path)
            and channel.accepts(generation)
        )

    def publish(self, generation: int, path: str, runs: RunMetadata, *, expensive: bool) -> None:
        channel = self.channel(expensive)
        if not self._accepts(channel, generation, path):
            return
        if channel.publishing:
            channel.pending_batches.append(runs)
            return
        channel.publishing = True
        try:
            self._publish_batch(generation, path, runs, expensive=expensive)
            while channel.accepts(generation) and channel.pending_batches:
                self._publish_batch(generation, path, channel.pending_batches.popleft(), expensive=expensive)
        except Exception as error:
            if channel.accepts(generation):
                self._cancel(channel.worker)
                if channel.accepts(generation):
                    channel.deferred_completion = (error,)
        finally:
            if channel.accepts(generation):
                channel.publishing = False
                deferred = channel.deferred_completion
                channel.deferred_completion = None
                if deferred is not None:
                    self.finish(generation, path, deferred[0], expensive=expensive)

    def _publish_batch(self, generation: int, path: str, runs: RunMetadata, *, expensive: bool) -> None:
        channel = self.channel(expensive)
        instance = channel.instance
        if not self._accepts(channel, generation, path):
            return
        if not self.host.source_current(path, instance) or not channel.accepts(generation):
            return
        self.host.publish(runs)
        if not channel.accepts(generation):
            return
        if not self.host.source_current(path, instance, after_publication=True):
            return
        if not self._accepts(channel, generation, path):
            return
        if expensive:
            # Shape and size arrive in separate batches.
            for name, fields in (
                ("Setpoints", {"setpoint_shape", "point_shape", "setpoint_count", "expected_results"}),
                ("Size", {"storage_bytes"}),
            ):
                if not channel.accepts(generation):
                    return
                guids = [run.get("guid") for run in runs.values() if fields.intersection(run)]
                self.host.set_loading((name,), guids=guids, loading=False)
        else:
            self.host.set_loading(
                BASIC_METADATA_COLUMNS, guids=[run.get("guid") for run in runs.values()], loading=False,
            )

    def finish(self, generation: int, path: str, error: object, *, expensive: bool) -> None:
        channel = self.channel(expensive)
        if not self._accepts(channel, generation, path):
            return
        if channel.publishing:
            channel.deferred_completion = (error,)
            return
        instance = channel.instance
        channel.clear()
        channel.failed = error is not None
        if isinstance(error, DatabaseInstanceChangedError):
            expected = [self.channel(stage).generation + 1 for stage in (False, True)]
            self.cancel()
            if expected == [self.channel(stage).generation for stage in (False, True)]:
                self.host.reload(path)
            return
        if not self.host.source_current(path, instance) or channel.generation != generation:
            return
        self.host.set_loading(self.columns(expensive), loading=False)
        if channel.generation != generation:
            return
        if error is not None:
            self._failed(expensive, error)
        elif not any(self.channel(stage).active or self.channel(stage).failed for stage in (False, True)):
            self.host.show_status("Run details loaded.", 5000)
