"""Shared fixtures and assertions for trusted live service."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_live import (
    TrustedLiveCancelledError,
    TrustedQuery,
    TrustedQueryResult,
)
from qplot.datahandling.trusted_live_supervisor import (
    TrustedLiveProtocolError,
    TrustedLiveSupervisorLiveness,
)

__all__ = [
    '_FakeSupervisor',
]


def _wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for deterministic broker state")


@dataclass(slots=True)
class _FakeJob:
    label: str
    result: object
    blocked: bool
    failure: BaseException | None
    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if not self.blocked:
            self.release.set()


class _FakeSupervisor:
    """Thread-safe spawned-supervisor stand-in with exact fake jobs."""

    def __init__(self, accepted_instance: DatabaseInstance) -> None:
        self.database_instance = accepted_instance
        self.incarnation = 1
        self.helper_pid = 424_242
        self._helper_alive = True
        self._receiver_alive = True
        self._open_endpoints = 3
        self._unreaped_incarnation = False
        self._retain_unreaped_on_close = False
        self._closed = False
        self._lock = threading.Lock()
        self._blocked_labels: set[str] = set()
        self._failures: dict[str, BaseException] = {}
        self._cancel_error: BaseException | None = None
        self.jobs: list[_FakeJob] = []
        self.submit_threads: list[tuple[str, int, str]] = []
        self.wait_threads: list[tuple[str, int, str]] = []
        self.cancel_threads: list[tuple[str, int, str]] = []
        self.close_threads: list[tuple[int, str]] = []
        self.cancel_entered = threading.Event()
        self.cancel_release = threading.Event()
        self.cancel_release.set()
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.close_release.set()

    @property
    def helper_alive(self) -> bool:
        with self._lock:
            return self._helper_alive

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def resource_liveness(self) -> TrustedLiveSupervisorLiveness:
        with self._lock:
            active_incarnation = self._helper_alive
            unreaped_incarnation = self._unreaped_incarnation
            return TrustedLiveSupervisorLiveness(
                helper_pid=(
                    self.helper_pid
                    if active_incarnation or unreaped_incarnation
                    else None
                ),
                process_alive=self._helper_alive,
                receiver_alive=self._receiver_alive,
                open_endpoints=self._open_endpoints,
                active_incarnation=active_incarnation,
                unreaped_incarnation=unreaped_incarnation,
                active_job=False,
                closing=False,
                closed=self._closed,
            )

    def reap_closed_resources(self) -> TrustedLiveSupervisorLiveness:
        return self.resource_liveness()

    def retain_unreaped_on_close(self) -> None:
        with self._lock:
            self._retain_unreaped_on_close = True

    def release_unreaped_resources(self) -> None:
        with self._lock:
            self._retain_unreaped_on_close = False
            self._receiver_alive = False
            self._open_endpoints = 0
            self._unreaped_incarnation = False

    def block(self, label: str) -> None:
        with self._lock:
            self._blocked_labels.add(label)

    def fail_fatally(self, label: str) -> None:
        self.fail_with(
            label, TrustedLiveProtocolError(f"Fake fatal failure for {label}")
        )

    def fail_with(self, label: str, error: BaseException) -> None:
        with self._lock:
            self._failures[label] = error

    def fail_cancel_with(self, error: BaseException) -> None:
        with self._lock:
            self._cancel_error = error

    def pause_cancel(self) -> None:
        self.cancel_release.clear()

    def pause_close(self) -> None:
        self.close_release.clear()

    def labels_submitted(self) -> list[str]:
        with self._lock:
            return [job.label for job in self.jobs]

    def cancel_count(self) -> int:
        with self._lock:
            return len(self.cancel_threads)

    def job(self, label: str) -> _FakeJob | None:
        with self._lock:
            return next((job for job in self.jobs if job.label == label), None)

    def wait_until_started(self, label: str) -> _FakeJob:
        selected: list[_FakeJob] = []

        def started() -> bool:
            job = self.job(label)
            if job is None or not job.started.is_set():
                return False
            selected.append(job)
            return True

        _wait_for(started)
        return selected[0]

    def release_job(self, label: str) -> None:
        job = self.job(label)
        if job is None:
            raise AssertionError(f"No fake supervisor job {label!r} was submitted")
        job.release.set()

    def release_everything(self) -> None:
        with self._lock:
            jobs = tuple(self.jobs)
        for job in jobs:
            job.release.set()
        self.cancel_release.set()
        self.close_release.set()
        self.release_unreaped_resources()

    def _new_job(self, label: str, result: object) -> _FakeJob:
        current = threading.current_thread()
        with self._lock:
            job = _FakeJob(
                label,
                result,
                label in self._blocked_labels,
                self._failures.get(label),
            )
            self.jobs.append(job)
            self.submit_threads.append((label, threading.get_ident(), current.name))
        return job

    def submit_query(
        self,
        sql: str,
        bindings: object = None,
        *,
        timeout: float | None = None,
    ) -> _FakeJob:
        del bindings, timeout
        return self._new_job(sql, TrustedQueryResult(("value",), ((sql,),)))

    def submit_query_batch(
        self,
        queries: tuple[TrustedQuery, ...],
        *,
        timeout: float | None = None,
    ) -> _FakeJob:
        del timeout
        label = "batch:" + "|".join(query.sql for query in queries)
        results = tuple(
            TrustedQueryResult(("value",), ((query.sql,),)) for query in queries
        )
        return self._new_job(label, results)

    def submit_data_version(self, *, timeout: float | None = None) -> _FakeJob:
        del timeout
        return self._new_job("data-version", 1)

    def wait(self, job: _FakeJob, *, timeout: float | None = None) -> Any:
        current = threading.current_thread()
        with self._lock:
            self.wait_threads.append((job.label, threading.get_ident(), current.name))
        job.started.set()
        wait_timeout = 5.0 if timeout is None else min(5.0, max(0.01, timeout))
        if not job.release.wait(wait_timeout):
            raise AssertionError(f"Fake job {job.label!r} was not released")
        if job.cancelled.is_set():
            raise TrustedLiveCancelledError(f"Fake job {job.label!r} was cancelled")
        if job.failure is not None:
            raise job.failure
        return job.result

    def cancel(self, job: _FakeJob) -> bool:
        current = threading.current_thread()
        with self._lock:
            self.cancel_threads.append((job.label, threading.get_ident(), current.name))
            cancel_error = self._cancel_error
        self.cancel_entered.set()
        job.cancelled.set()
        if cancel_error is None:
            job.release.set()
        if not self.cancel_release.wait(5.0):
            raise AssertionError("Fake supervisor cancellation was not released")
        if cancel_error is not None:
            raise cancel_error
        return True

    def close(self) -> None:
        current = threading.current_thread()
        with self._lock:
            self.close_threads.append((threading.get_ident(), current.name))
        self.close_entered.set()
        if not self.close_release.wait(5.0):
            raise AssertionError("Fake supervisor close was not released")
        with self._lock:
            self._helper_alive = False
            self._closed = True
            if self._retain_unreaped_on_close:
                self._receiver_alive = True
                self._open_endpoints = 1
                self._unreaped_incarnation = True
            else:
                self._receiver_alive = False
                self._open_endpoints = 0
                self._unreaped_incarnation = False
