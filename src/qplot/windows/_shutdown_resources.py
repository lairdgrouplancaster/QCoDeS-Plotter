"""Live inventory of the background resources owned by a main window.

Pools are registered at creation. Worker attributes are resolved when cancelling,
and reader membership is observed on every scan: retirement can change it while
shutdown is in progress. This inventory never owns database connections itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from PyQt6 import QtCore

from ._auto_plot import cancel_auto_plot

if TYPE_CHECKING:
    from .main import MainWindow


class WorkerPool(Protocol):
    def activeThreadCount(self) -> int: ...
    def clear(self) -> None: ...


@dataclass(frozen=True)
class ResourceStatus:
    active: bool
    diagnostics: tuple[str, ...]


class ShutdownResources:
    """Enumerate, cancel, and inspect resources without waiting on the GUI thread."""

    def __init__(self, window: MainWindow) -> None:
        self.window = window
        self._pools: list[tuple[str, WorkerPool]] = []
        self._workers: list[tuple[str, Callable[[], None] | None]] = []

    def register_pool(self, name: str, pool: WorkerPool) -> None:
        self._pools.append((name, pool))

    @property
    def pools(self) -> tuple[tuple[str, WorkerPool], ...]:
        """A stable view of the pools whose lifetimes belong to this window."""
        return tuple(self._pools)

    def watch_worker(
        self,
        attribute: str,
        *,
        cancel: Callable[[], None] | None = None,
    ) -> None:
        self._workers.append((attribute, cancel))

    def create_pool(
        self,
        name: str,
        *,
        parent: QtCore.QObject | None = None,
        max_threads: int = 1,
        worker_attribute: str | None = None,
    ) -> QtCore.QThreadPool:
        pool = QtCore.QThreadPool(parent)
        pool.setMaxThreadCount(max_threads)
        self.register_pool(name, pool)
        if worker_attribute is not None:
            self.watch_worker(worker_attribute)
        return pool

    def cancel_database_workers(self) -> None:
        # Registration order is the existing cancellation order. Resolve the
        # current worker, since each database action replaces it for a new job.
        for attribute, cancel in self._workers:
            if cancel is not None:
                cancel()
            else:
                worker = getattr(self.window, attribute, None)
                if worker is not None:
                    worker.cancel()

    def read_services(self):
        """Include active, pending, and retired readers, without duplicate probes."""
        window = self.window
        services = set(getattr(window, "_retired_trusted_read_services", ()))
        active_service = getattr(window, "_trusted_read_service", None)
        if active_service is not None:
            services.add(active_service)
        services.update(getattr(window, "_pending_trusted_read_services", {}).values())
        return services

    def cancel_plot_work(self) -> None:
        cancel_auto_plot(self.window)
        workers = set(getattr(self.window, "_plot_workers", ()))
        for window in list(getattr(self.window, "windows", ())):
            worker = getattr(window, "worker", None)
            if worker is not None:
                workers.add(worker)

        for worker in workers:
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()

        pool = getattr(self.window, "threadPool", None)
        clear = getattr(pool, "clear", None)
        if callable(clear):
            clear()

    def inspect(self) -> ResourceStatus:
        diagnostics = []
        active = False
        for pool_name, pool in self.pools:
            try:
                count = pool.activeThreadCount()
            except BaseException as error:
                active = True
                diagnostics.append(
                    f"pool {pool_name}: liveness raised {type(error).__name__}: {error}"
                )
                continue
            if count > 0:
                active = True
                diagnostics.append(f"pool {pool_name}: active_threads={count}")

        exports = getattr(self.window, "_export_task_manager", None)
        if exports is not None:
            # A finished worker still owns a stage until GUI publication or
            # cancellation cleanup has run. Do not destroy that owner early.
            exports.poll()
            if exports.tasks:
                active = True
                diagnostics.append(f"pending_csv_exports={len(exports.tasks)}")

        reap_services = getattr(
            self.window, "_reap_retired_trusted_read_services", None
        )
        if callable(reap_services):
            try:
                reap_services()
            except BaseException as error:
                active = True
                diagnostics.append(
                    f"retired-service reaper raised {type(error).__name__}: {error}"
                )
        diagnostics.extend(
            getattr(self.window, "_retired_service_reap_diagnostics", {}).values()
        )
        for service in self.read_services():
            service_label = (
                f"{type(service).__module__}.{type(service).__qualname__}"
                f"@{id(service):x}"
            )
            service_errors = []
            for error_name in ("fatal_error", "close_error"):
                try:
                    service_error = getattr(service, error_name, None)
                except BaseException as error:
                    active = True
                    service_errors.append(
                        f"{error_name} probe raised {type(error).__name__}: {error}"
                    )
                    continue
                if service_error is not None:
                    service_errors.append(
                        f"{error_name}={type(service_error).__name__}: {service_error}"
                    )
            try:
                liveness = service.liveness()
            except BaseException as error:
                active = True
                diagnostics.append(
                    f"service {service_label}: liveness raised "
                    f"{type(error).__name__}: {error}"
                    + ("; " + "; ".join(service_errors) if service_errors else "")
                )
                continue
            field_names = (
                "dispatcher_alive",
                "control_alive",
                "helper_alive",
                "helper_pid",
                "receiver_alive",
                "open_supervisor_endpoints",
                "unreaped_incarnations",
                "resource_cleanup_pending",
                "outstanding_requests",
                "closing",
                "closed",
            )
            values = {
                field_name: getattr(liveness, field_name, None)
                for field_name in field_names
            }
            service_active = bool(
                values["dispatcher_alive"]
                or values["control_alive"]
                or values["helper_alive"]
                or values["receiver_alive"]
                or values["open_supervisor_endpoints"]
                or values["unreaped_incarnations"]
                or values["resource_cleanup_pending"]
                or values["outstanding_requests"]
                or not values["closed"]
            )
            if service_active:
                active = True
                diagnostics.append(
                    f"service {service_label}: "
                    + ", ".join(
                        f"{field_name}={values[field_name]!r}"
                        for field_name in field_names
                    )
                    + ("; " + "; ".join(service_errors) if service_errors else "")
                )

        preview = getattr(getattr(self.window, "infoBox", None), "preview", None)
        preview_workers = getattr(preview, "_workers", {})
        if preview_workers:
            active = True
            diagnostics.append(f"preview_workers={len(preview_workers)}")
        derived_bridge = getattr(self.window, "_trusted_derived_bridge", None)
        if derived_bridge is not None:
            try:
                if derived_bridge.background_active():
                    active = True
                    diagnostic = derived_bridge.shutdown_diagnostic()
                    if diagnostic:
                        diagnostics.append(diagnostic)
            except BaseException as error:
                active = True
                diagnostics.append(
                    "trusted derived bridge liveness raised "
                    f"{type(error).__name__}: {error}"
                )
        if not active and callable(reap_services):
            # A broker can finish after the initial reap but before its
            # liveness snapshot. Reap that newly closed reader before the
            # shutdown path stops the reaper timer and accepts the close.
            try:
                reap_services()
            except BaseException as error:
                active = True
                diagnostics.append(
                    f"final retired-service reap raised {type(error).__name__}: {error}"
                )
        return ResourceStatus(active, tuple(diagnostics))

    def escalate_cleanup(self) -> tuple[str, ...]:
        escalation_diagnostics = []

        try:
            self.cancel_plot_work()
        except BaseException as error:
            escalation_diagnostics.append(
                f"plot cancellation raised {type(error).__name__}: {error}"
            )

        for pool_name, pool in self.pools:
            try:
                exports = getattr(self.window, "_export_task_manager", None)
                if pool is not getattr(exports, "pool", None):
                    pool.clear()
            except BaseException as error:
                escalation_diagnostics.append(
                    f"pool {pool_name} clear raised {type(error).__name__}: {error}"
                )

        preview = getattr(getattr(self.window, "infoBox", None), "preview", None)
        shutdown = getattr(preview, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except BaseException as error:
                escalation_diagnostics.append(
                    f"preview shutdown raised {type(error).__name__}: {error}"
                )

        derived_bridge = getattr(self.window, "_trusted_derived_bridge", None)
        if derived_bridge is not None:
            try:
                derived_bridge.escalate_cleanup()
            except BaseException as error:
                escalation_diagnostics.append(
                    f"trusted derived cleanup raised {type(error).__name__}: {error}"
                )

        for service in self.read_services():
            escalate = getattr(service, "escalate_cleanup_async", None)
            if not callable(escalate):
                escalate = getattr(service, "close_async", None)
            if not callable(escalate):
                continue
            try:
                escalate()
            except BaseException as error:
                escalation_diagnostics.append(
                    f"service cleanup escalation raised {type(error).__name__}: {error}"
                )

        reap_services = getattr(
            self.window, "_reap_retired_trusted_read_services", None
        )
        if callable(reap_services):
            try:
                reap_services()
            except BaseException as error:
                escalation_diagnostics.append(
                    "retired-service escalation reap raised "
                    f"{type(error).__name__}: {error}"
                )
        return tuple(escalation_diagnostics)
