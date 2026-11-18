"""Own preview jobs, priorities and deferred starts independently of Qt."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from qplot.diagnostics import log_exception

PreviewKey = tuple[int, str]

PREVIEW_REMAINING_PRIORITY = 0
PREVIEW_PLOTTED_PRIORITY = 50
PREVIEW_VISIBLE_PRIORITY = 100
PREVIEW_SELECTED_PRIORITY = 125
PREVIEW_SELECTED_THUMBNAIL_PRIORITY = 150


def is_thumbnail_priority(priority: int) -> bool:
    return priority in (PREVIEW_VISIBLE_PRIORITY, PREVIEW_SELECTED_THUMBNAIL_PRIORITY)


@dataclass(eq=False)
class PreviewJob:
    generation: int
    guid: str
    priority: int
    worker: Any = None
    starting: bool = True
    publishing: bool = False
    completion: tuple[Any, Any] | None = None
    is_thumbnail: bool = field(init=False)

    def __post_init__(self) -> None:
        # Selection can reprioritise a running job, but its output is already decided.
        self.is_thumbnail = is_thumbnail_priority(self.priority)

    @property
    def key(self) -> PreviewKey:
        return self.generation, self.guid


class PreviewHost(Protocol):
    def create_worker(self, job: PreviewJob) -> Any: ...
    def connect_worker(self, job: PreviewJob) -> None: ...
    def submit_worker(self, job: PreviewJob) -> None: ...
    def activity(self, job: PreviewJob, active: bool) -> None: ...
    def publish(self, job: PreviewJob, previews: Any, error: Any) -> None: ...
    def failed(self, job: PreviewJob, error: Exception) -> None: ...
    def schedule(self, callback: Callable[[], None]) -> None: ...
    def settled(self) -> None: ...


class PreviewController:
    def __init__(self, host: PreviewHost, *, max_workers: int) -> None:
        self.host = host
        self.max_workers = max_workers
        self.generation = 0
        self.closed = False
        self._queue: dict[str, int] = {}
        self._jobs: dict[PreviewKey, PreviewJob] = {}
        self._scheduled: object | None = None
        self._pumping = False

    @property
    def queue(self) -> Mapping[str, int]:
        return MappingProxyType(self._queue)

    @property
    def active(self) -> frozenset[PreviewKey]:
        return frozenset(key for key in self._jobs if key[0] == self.generation)

    @property
    def workers(self) -> Mapping[PreviewKey, Any]:
        # Retired generations remain visible to shutdown until their callbacks.
        return MappingProxyType({key: job.worker for key, job in self._jobs.items()
                                 if job.worker is not None})

    @property
    def priorities(self) -> Mapping[PreviewKey, int]:
        return MappingProxyType({key: job.priority for key, job in self._jobs.items()
                                 if key[0] == self.generation})

    @property
    def scheduled(self) -> bool:
        return self._scheduled is not None

    def accepts(self, job: PreviewJob) -> bool:
        return (not self.closed and job.generation == self.generation
                and self._jobs.get(job.key) is job)

    @staticmethod
    def cancel_worker(worker: Any) -> None:
        if worker is not None:
            try:
                worker.cancel()
            except Exception as error:
                log_exception("Preview cancellation failed", error, __name__)

    def cancel_workers(self) -> None:
        for job in tuple(self._jobs.values()):
            self.cancel_worker(job.worker)

    def reset(self, *, close: bool = False) -> None:
        self.generation += 1
        self.closed = self.closed or close
        self._queue.clear()
        self._scheduled = None
        # Fence all callbacks before invoking cancellation hooks.
        self.cancel_workers()

    def enqueue(self, guid: str, priority: int, *, allow_active: bool = False) -> None:
        if not self.closed and (allow_active or (self.generation, guid) not in self._jobs):
            self._queue[guid] = max(priority, self._queue.get(guid, priority))

    def retain_queued(self, guids: Iterable[str]) -> None:
        wanted = set(guids)
        self._queue = {guid: priority for guid, priority in self._queue.items()
                       if guid in wanted}

    def order_queued(self, guids: Iterable[str]) -> None:
        self._queue = {guid: self._queue[guid] for guid in guids if guid in self._queue}

    def set_priority(self, key: PreviewKey, priority: int) -> None:
        job = self._jobs.get(key)
        if job is not None and self.accepts(job):
            job.priority = priority

    def preempt(self) -> None:
        active = [job for job in self._jobs.values() if self.accepts(job)]
        if len(active) >= self.max_workers and self._queue:
            lowest = min(active, key=lambda job: job.priority)
            if max(self._queue.values()) > lowest.priority:
                self.cancel_worker(lowest.worker)

    def schedule(self) -> None:
        if self.closed or self._scheduled is not None:
            return
        token = self._scheduled = object()
        generation = self.generation

        def run() -> None:
            if self._scheduled is token and self.generation == generation:
                self._scheduled = None
                self.host.settled()

        try:
            self.host.schedule(run)
        except Exception:
            if self._scheduled is token:
                self._scheduled = None
            raise

    def pump(self) -> None:
        if self.closed or self._pumping:
            return
        self._pumping = True
        generation = self.generation
        try:
            while not self.closed and self.generation == generation:
                active = self.active
                if len(active) >= self.max_workers:
                    break
                available = [guid for guid in self._queue if (generation, guid) not in active]
                if not available:
                    break
                self.start(max(available, key=lambda guid: self._queue[guid]))
        finally:
            self._pumping = False
            if generation != self.generation and self._queue and not self.closed:
                self.host.settled()

    def _forget(self, job: PreviewJob) -> None:
        if self._jobs.get(job.key) is job:
            del self._jobs[job.key]
        job.completion = None

    def _failed(self, job: PreviewJob, error: Exception) -> None:
        log_exception("Preview job failed", error, __name__)
        if self.accepts(job):
            try:
                self.host.failed(job, error)
            except Exception as feedback_error:
                log_exception("Preview failure feedback failed", feedback_error, __name__)

    def start(self, guid: str) -> None:
        if self.closed or guid not in self._queue or (self.generation, guid) in self._jobs:
            return
        job = PreviewJob(self.generation, guid, self._queue.pop(guid))
        self._jobs[job.key] = job
        try:
            job.worker = self.host.create_worker(job)
            if job.worker is None or not self.accepts(job):
                self.cancel_worker(job.worker)
                self._forget(job)
                return
            self.host.connect_worker(job)
            if self.accepts(job):
                self.host.activity(job, True)
            if not self.accepts(job):
                self.cancel_worker(job.worker)
                self._forget(job)
                return
            self.host.submit_worker(job)
        except Exception as error:
            self.cancel_worker(job.worker)
            self._failed(job, error)
            if self.accepts(job):
                try:
                    self.host.activity(job, False)
                except Exception as feedback_error:
                    log_exception("Preview activity feedback failed", feedback_error, __name__)
            self._forget(job)
            return
        finally:
            job.starting = False
        if job.completion is not None:
            previews, completion_error = job.completion
            job.completion = None
            self.finish(job.generation, guid, job.worker, previews, completion_error)

    def finish(self, generation: int, guid: str, worker: Any,
               previews: Any, error: Any) -> None:
        job = self._jobs.get((generation, guid))
        if job is None or (worker is not None and worker is not job.worker):
            return
        if job.starting:
            if job.completion is None:
                job.completion = (previews, error)
            return
        if job.publishing:
            return
        job.publishing = True
        try:
            if self.accepts(job):
                self.host.activity(job, False)
            if self.accepts(job) and not job.worker.is_cancelled():
                self.host.publish(job, previews, error)
        except Exception as publish_error:
            self._failed(job, publish_error)
        finally:
            self._forget(job)
            self.host.settled()
