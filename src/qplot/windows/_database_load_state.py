"""State shared by the database load worker and its GUI publication transaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Any

from qplot.datahandling.file_identity import DatabaseFileIdentity, DatabaseInstance

if TYPE_CHECKING:
    from qplot.datahandling.database import DatabaseLoadWorker
    from qplot.datahandling.trusted_live_service import TrustedLiveReadService

    from ._dataset_handle import DatasetKey


@dataclass
class DatabaseLoadSession:
    """One pending load; ownership stays reachable until publication finishes."""

    abspath: str
    load_instance: DatabaseInstance
    load_started_at: float = field(default_factory=perf_counter)
    reload_same_path: bool = False
    replacement_reload: bool = False
    generation_recovery: bool = False
    worker: DatabaseLoadWorker | None = None
    trusted_service: TrustedLiveReadService | None = None
    owns_trusted_service: bool = False


@dataclass(frozen=True)
class DatabaseViewSnapshot:
    """The committed view to restore if staging another database fails."""

    database_path: str
    runs: dict[int, dict[str, Any]]
    run_id_text: str
    measurement_text: str
    selected_run_id: int | None
    selected_run_guid: str | None
    selected_dataset: object | None
    selected_dataset_key: DatasetKey | None
    local_last_file: str | None
    preview_path: str
    trusted_service: TrustedLiveReadService | None
    access_mode: str | None
    fallback_reason: str | None
    database_identity: DatabaseFileIdentity | None
    database_instance: DatabaseInstance | None
    qcodes_database: str
