"""Fixed metadata and preview query plans for current QCoDeS databases.

The adapter coordinates bounded primitive queries, run caching, and publication.
Result-layout verification, pure sampling, and shared value types have separate
owners. No cursor or QCoDeS dataset escapes this layer."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from typing import Any, cast

from qplot.datahandling._trusted_grid_sampling import (
    _trusted_2d_sample_plans,
    _validated_sampled_grid_layout,
)
from qplot.datahandling._trusted_query_metadata import (
    _json_object,
    array_parameter_names,
    bounded_parameter_presentation,
    bounded_parameter_views_from_run_metadata,
    bounded_setpoint_presentation,
    estimated_result_storage_bytes,
    parameter_dependency_sets,
    parameter_views_bounded,
    parameter_views_from_run_metadata,
    planned_setpoint_steps,
    quote_sqlite_identifier,
)
from qplot.datahandling._trusted_result_layout import (
    ResultLayoutQueries,
)
from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.readonly import SEPARATE_RAW_DATA_STORAGE_MESSAGE
from qplot.datahandling.run_metadata import (
    materialize_run_basic_fields,
    materialize_run_observation,
)
from qplot.datahandling.source_activity import source_activity_timestamp
from qplot.datahandling.trusted_live import (
    TRUSTED_LIVE_MAX_SCALAR_BYTES,
    TrustedLiveQueryError,
    TrustedQuery,
    TrustedQueryResult,
)
from qplot.datahandling.trusted_presentation import (
    TrustedSelectedRunPresentation,
    bounded_presentation_names,
    bounded_selected_run_fields,
    build_selected_run_presentation,
)
from qplot.datahandling.trusted_query_types import (
    TRUSTED_DERIVED_ARRAY_ROWS_PER_WINDOW,
    TRUSTED_DERIVED_MAX_ARRAY_BYTES,
    TRUSTED_DERIVED_MAX_COMPLEX_CELL_BYTES,
    TRUSTED_DERIVED_MAX_DEPENDENTS,
    TRUSTED_DERIVED_MAX_GRID_SAMPLE_ROWS,
    TRUSTED_DERIVED_MAX_SAMPLE_CELLS,
    TRUSTED_DERIVED_MAX_SAMPLE_ROWS,
    TRUSTED_DERIVED_MAX_SOURCE_COLUMNS,
    TRUSTED_DERIVED_ROWS_PER_WINDOW,
    TRUSTED_DERIVED_RUN_TEXT_MAX_BYTES,
    TRUSTED_DERIVED_SAMPLE_WINDOWS,
    FrozenFields,
    PrimitiveScalar,
    PrimitiveValue,
    Trusted2DDependentRowPattern,
    Trusted2DGridLayout,
    Trusted2DLayoutProof,
    TrustedBootstrapResult,
    TrustedDatabaseInfo,
    TrustedDerivedSourceObservation,
    TrustedMetadataQueryError,
    TrustedParameterView,
    TrustedQueryExecutor,
    TrustedRefreshResult,
    TrustedRunPage,
    TrustedRunRecord,
    TrustedSelectedRunDetail,
    TrustedSetpointSummary,
    TrustedSourceRevision,
    TrustedSourceRevisionNamespace,
    TrustedUnsupportedQcodesSchemaError,
    _freeze_fields,
    freeze_primitive_fields,
    run_records_as_dict,
    trusted_derived_source_revision,
    trusted_source_revision,
)
from qplot.datahandling.trusted_snapshot import (
    TRUSTED_SNAPSHOT_MAX_INPUT_BYTES,
    TrustedSnapshotOmission,
    TrustedSnapshotView,
    normalize_trusted_snapshot,
)

TRUSTED_RUN_PAGE_SIZE = 1_000
TRUSTED_RUN_PAGE_SIZE_MAX = TRUSTED_RUN_PAGE_SIZE

_TRUSTED_AGGREGATE_BATCH_SIZE = 4
_TRUSTED_SETPOINT_EDGE_PAGE_ROWS = 4_096
_TRUSTED_SETPOINT_EDGE_MAX_PAGES = 32
_TRUSTED_SETPOINT_EDGE_GROUP_SIZE = 8
_TRUSTED_SETPOINT_VALUE_MAX_BYTES = 64 * 1024
_TRUSTED_SETPOINT_SUMMARY_MAX_PARAMETERS = 32
_TRUSTED_BASIC_TEXT_MAX_BYTES = 512
_TRUSTED_SINGLE_RUN_PREFLIGHT_BATCH_SIZE = 4
_TRUSTED_SINGLE_RUN_GROUP_MAX_COLUMNS = 32
_TRUSTED_SINGLE_RUN_GROUP_MAX_RAW_BYTES = TRUSTED_LIVE_MAX_SCALAR_BYTES
_TRUSTED_SINGLE_RUN_GROUP_MAX_SHARED_VALUE_BYTES = 128 * 1024
_TRUSTED_SINGLE_RUN_MAX_NON_SNAPSHOT_RAW_BYTES = TRUSTED_LIVE_MAX_SCALAR_BYTES
_TRUSTED_SINGLE_RUN_MAX_SNAPSHOT_RAW_BYTES = min(
    TRUSTED_SNAPSHOT_MAX_INPUT_BYTES,
    TRUSTED_LIVE_MAX_SCALAR_BYTES,
)
_TRUSTED_LAYOUT_TEXT_MAX_BYTES = 512
_TRUSTED_LAYOUT_MAX_ROWS = 4_096
_TRUSTED_RESULT_SCHEMA_SQL_MAX_BYTES = 64 * 1024
_QCODES_RESULT_ID_SCHEMA = re.compile(
    r'(?:\(|,)\s*(?:"id"|`id`|\[id\]|id)\s+INTEGER\s+PRIMARY\s+KEY'
    r"(?:\s+AUTOINCREMENT)?\s*(?:,|\))",
    re.IGNORECASE,
)

_BASIC_RUN_COLUMNS = (
    "run_id",
    "exp_id",
    "name",
    "run_timestamp",
    "completed_timestamp",
    "is_completed",
    "guid",
    "result_table_name",
)
_DEFERRED_BASIC_RUN_COLUMNS = ("parameters", "run_description")
# Placeholder metadata keeps the same small reply budget for every run. For
# moderately larger descriptions, project just the dependencies inside SQLite
# so mixed-dimensional runs need not wait for their full metadata observation.
_TRUSTED_PLACEHOLDER_DESCRIPTION_MAX_BYTES = 2048
_TRUSTED_PLACEHOLDER_DESCRIPTION_SOURCE_MAX_BYTES = 16 * 1024
_OPTIONAL_BASIC_RUN_COLUMNS = ("measurement_exception",)
_OBSERVED_SHAPE_FIELDS = (
    "point_shape",
    "setpoint_shape",
    "setpoint_shape_source",
    "setpoint_count",
    "setpoint_count_source",
    "expected_results",
    "expected_results_source",
)
_BASIC_PAGED_TEXT_COLUMNS = frozenset(
    {
        "name",
        "guid",
        "result_table_name",
        "measurement_exception",
        "exp_name",
        "sample_name",
    }
)
_STANDARD_RUN_COLUMNS = frozenset(
    {
        "run_id",
        "exp_id",
        "name",
        "result_table_name",
        "result_counter",
        "run_timestamp",
        "completed_timestamp",
        "is_completed",
        "parameters",
        "guid",
        "run_description",
        "snapshot",
        "parent_datasets",
        "captured_run_id",
        "captured_counter",
    }
)
_RAW_RUN_FIELDS = frozenset(
    {"parameters", "run_description", "snapshot", "parent_datasets"}
)
_REQUIRED_RUN_COLUMNS = _STANDARD_RUN_COLUMNS
_REQUIRED_EXPERIMENT_COLUMNS = frozenset({"exp_id", "name", "sample_name"})


@dataclass(frozen=True, slots=True)
class _SingleRunColumnPreflight:
    name: str
    value_type: str
    value_bytes: int | None


@dataclass(frozen=True, slots=True)
class _SingleRunValues:
    fields: dict[str, Any]
    unavailable_fields: tuple[str, ...]
    accepted_non_snapshot_raw_bytes: int
    snapshot_omission: TrustedSnapshotOmission | None = None


def _bounded_public_run_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Discard raw SQL payloads and cap every field retained past a query plan."""

    bounded = dict(bounded_selected_run_fields(fields))
    if _RAW_RUN_FIELDS.intersection(bounded):
        raise TrustedMetadataQueryError(
            "A raw trusted run field escaped presentation normalisation."
        )
    return bounded


def _bounded_run_record(
    run_id: int,
    fields: dict[str, Any],
    unavailable_fields: tuple[str, ...] = (),
) -> TrustedRunRecord:
    public_unavailable, _truncated = bounded_presentation_names(unavailable_fields)
    return TrustedRunRecord(
        run_id,
        _freeze_fields(_bounded_public_run_fields(fields)),
        public_unavailable,
    )


def _single_integer(result: TrustedQueryResult, description: str) -> int:
    if len(result.rows) != 1 or len(result.rows[0]) != 1:
        raise TrustedMetadataQueryError(
            f"The trusted {description} query returned an invalid shape."
        )
    value = result.rows[0][0]
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise TrustedMetadataQueryError(
            f"The trusted {description} query returned an invalid integer."
        )
    return value


def _one_row_mapping(result: TrustedQueryResult) -> dict[str, Any] | None:
    if not result.rows:
        return None
    if len(result.rows) != 1:
        raise TrustedMetadataQueryError(
            "A single-run trusted query returned more than one row."
        )
    row = result.rows[0]
    if len(row) != len(result.columns):
        raise TrustedMetadataQueryError(
            "A trusted query row does not match its declared columns."
        )
    return dict(zip(result.columns, row, strict=True))


def _materialize_refreshed_basic_fields(
    metadata: dict[str, Any],
    prior: dict[str, Any],
) -> dict[str, Any]:
    """Refresh planned fields without regressing an observed shape/count."""

    if not any(name in metadata for name in _DEFERRED_BASIC_RUN_COLUMNS):
        return dict(metadata)

    observed = (
        prior.get("setpoint_shape_source") == "observed"
        or prior.get("setpoint_count_source") == "observed"
    )
    preserved = (
        {name: prior.get(name) for name in _OBSERVED_SHAPE_FIELDS} if observed else {}
    )
    materialized = materialize_run_basic_fields(metadata)
    materialized.update(preserved)
    return materialized


class TrustedMetadataQueryAdapter:
    """Stateful fixed-query adapter for one persistent helper incarnation."""

    def __init__(
        self,
        executor: TrustedQueryExecutor,
        database_path: str | os.PathLike[str],
        *,
        page_size: int = TRUSTED_RUN_PAGE_SIZE,
    ) -> None:
        if (
            type(page_size) is not int
            or not 1 <= page_size <= TRUSTED_RUN_PAGE_SIZE_MAX
        ):
            raise ValueError(
                f"page_size must be from 1 through {TRUSTED_RUN_PAGE_SIZE_MAX}."
            )
        self._executor = executor
        self._database_path = os.fspath(database_path)
        self._page_size = page_size
        self._run_columns: tuple[str, ...] | None = None
        self._result_columns: dict[str, tuple[str, ...]] = {}
        self._runs: dict[int, dict[str, Any]] = {}
        self._unavailable_run_fields: dict[int, tuple[str, ...]] = {}
        self._setpoint_summaries: dict[int, tuple[TrustedSetpointSummary, ...]] = {}
        self._preview_array_columns: dict[int, tuple[str, ...]] = {}
        self._preview_complex_columns: dict[int, tuple[str, ...]] = {}
        self._setpoint_summaries_truncated: dict[int, bool] = {}
        self._setpoint_summary_watermarks: dict[int, int] = {}
        self._setpoint_summary_names: dict[int, tuple[str, ...]] = {}
        self._layouts = ResultLayoutQueries(executor)
        self._last_run_id = 0
        self._data_version: int | None = None
        self._data_version_incarnation: int | None = None
        self._bootstrap_reconciliation_pending = False
        self._pending_watermark: int | None = None

    @property
    def last_run_id(self) -> int:
        return self._last_run_id

    @property
    def data_version(self) -> int | None:
        return self._data_version

    def bind_executor(self, executor: TrustedQueryExecutor) -> None:
        """Bind the next broker attempt while retaining session metadata state."""

        self._executor = executor
        self._layouts.bind_executor(executor)

    def bootstrap(self) -> TrustedBootstrapResult:
        """Establish a watermark without querying any result table or run page."""

        self._ensure_current_schema()
        # Capture before the watermark and pages.  A writer commit during any
        # later bootstrap query therefore changes the next data_version and is
        # reconciled by the first incremental refresh.
        baseline = self._executor.data_version()
        watermark = self._maximum_run_id()
        self._runs.clear()
        self._unavailable_run_fields.clear()
        self._result_columns.clear()
        self._setpoint_summaries.clear()
        self._preview_array_columns.clear()
        self._preview_complex_columns.clear()
        self._setpoint_summaries_truncated.clear()
        self._setpoint_summary_watermarks.clear()
        self._setpoint_summary_names.clear()
        self._layouts.clear()
        self._last_run_id = 0
        self._pending_watermark = watermark
        self._data_version = baseline
        self._data_version_incarnation = self._executor.incarnation
        self._bootstrap_reconciliation_pending = True
        return TrustedBootstrapResult(
            watermark,
            baseline,
            self._executor.incarnation,
        )

    def database_info(self) -> TrustedDatabaseInfo:
        """Read counts, header facts and bounded latest-run text together."""

        self._ensure_current_schema()
        results = self._executor.query_batch((
            TrustedQuery("PRAGMA main.user_version"),
            TrustedQuery("PRAGMA main.application_id"),
            TrustedQuery("PRAGMA main.page_count"),
            TrustedQuery("PRAGMA main.page_size"),
            TrustedQuery(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ),
            TrustedQuery("SELECT COUNT(*) FROM experiments"),
            TrustedQuery("SELECT COUNT(*) FROM runs"),
            TrustedQuery(
                "SELECT run_id, substr(name, 1, 512) AS name, run_timestamp, "
                "completed_timestamp, is_completed, substr(guid, 1, 512) AS guid "
                "FROM runs ORDER BY run_id DESC LIMIT 1"
            ),
        ))
        counts = tuple(int(result.rows[0][0]) for result in results[:-1])
        latest = results[-1]
        latest_run = None
        if latest.rows:
            fields = tuple(zip(latest.columns, latest.rows[0], strict=True))
            latest_run = TrustedRunRecord(int(latest.rows[0][0]), fields)
        return TrustedDatabaseInfo(
            user_version=counts[0], application_id=counts[1],
            page_count=counts[2], page_size=counts[3], table_count=counts[4],
            experiment_count=counts[5], run_count=counts[6], latest_run=latest_run,
        )

    def refresh_new_runs(
        self,
        accepted_run_id: int | None = None,
    ) -> TrustedRefreshResult:
        """Use data_version and the application's last published run cursor."""

        if accepted_run_id is not None and (
            type(accepted_run_id) is not int or accepted_run_id < 0
        ):
            raise ValueError("accepted_run_id must be a non-negative integer or None.")

        self._ensure_current_schema()
        incarnation_before = self._executor.incarnation
        current_version = self._executor.data_version()
        incarnation = self._executor.incarnation
        incarnation_changed_during_probe = incarnation_before != incarnation
        same_baseline = (
            not incarnation_changed_during_probe
            and self._data_version_incarnation == incarnation_before
            and self._data_version_incarnation == incarnation
        )
        schema_may_have_changed = (
            not same_baseline or current_version != self._data_version
        )
        if schema_may_have_changed:
            # QCoDeS can add run metadata or result columns while acquisition
            # continues. Revalidate through bounded zero-row SELECTs after a
            # commit or helper-incarnation change; never retain schema facts
            # across those boundaries.
            self._run_columns = None
            self._result_columns.clear()
            self._ensure_current_schema()
        prior = self._last_run_id if accepted_run_id is None else accepted_run_id
        publication_reconciliation = prior < self._last_run_id or (
            self._pending_watermark is not None and prior < self._pending_watermark
        )
        changed = (
            self._bootstrap_reconciliation_pending
            or publication_reconciliation
            or not same_baseline
            or current_version != self._data_version
        )
        if not changed:
            return TrustedRefreshResult(
                prior, prior, current_version, False, incarnation
            )

        watermark = self._maximum_run_id()
        self._pending_watermark = max(prior, watermark)
        # Keep the value captured before paging.  A commit during pagination
        # remains observable on the next explicit refresh and cannot be lost.
        self._data_version = current_version
        self._data_version_incarnation = incarnation
        self._bootstrap_reconciliation_pending = False
        return TrustedRefreshResult(
            prior,
            max(prior, watermark),
            current_version,
            True,
            incarnation,
        )

    def basic_run_page(
        self,
        after_run_id: int,
        through_run_id: int,
    ) -> TrustedRunPage:
        """Fetch one bounded page and advance the accepted monotonic cursor."""

        if type(after_run_id) is not int or after_run_id < 0:
            raise ValueError("after_run_id must be a non-negative integer.")
        if type(through_run_id) is not int or through_run_id < 0:
            raise ValueError("through_run_id must be a non-negative integer.")
        if after_run_id > through_run_id:
            raise ValueError("after_run_id cannot exceed through_run_id.")
        self._ensure_current_schema()
        if after_run_id == through_run_id:
            self._last_run_id = max(self._last_run_id, through_run_id)
            return TrustedRunPage((), after_run_id, through_run_id, after_run_id, True)
        result = self._executor.query(
            self._run_select_sql(
                where='runs."run_id" > ? AND runs."run_id" <= ?',
                order_limit=True,
                include_dynamic=False,
            ),
            (after_run_id, through_run_id, self._page_size),
        )
        records = tuple(
            self._merge_basic_page_record(record)
            for record in self._materialize_basic_page(result)
        )
        next_run_id = records[-1].run_id if records else after_run_id
        complete = (
            not records
            or next_run_id >= through_run_id
            or len(records) < self._page_size
        )
        if records and next_run_id <= after_run_id:
            raise TrustedMetadataQueryError(
                "Trusted run pagination did not advance its monotonic cursor."
            )
        for record in records:
            self._runs[record.run_id] = record.as_dict()
            self._cache_unavailable_fields(
                record.run_id,
                record.unavailable_fields,
            )
        if complete:
            self._last_run_id = max(self._last_run_id, through_run_id)
            if (
                self._pending_watermark is not None
                and through_run_id >= self._pending_watermark
            ):
                self._pending_watermark = None
        else:
            self._last_run_id = max(self._last_run_id, next_run_id)
        return TrustedRunPage(
            records,
            after_run_id,
            through_run_id,
            next_run_id,
            complete,
        )

    def _merge_basic_page_record(
        self,
        record: TrustedRunRecord,
    ) -> TrustedRunRecord:
        """Refresh paged columns without erasing already-enriched run data."""

        fresh = record.as_dict()
        cached = self._runs.get(record.run_id)
        if cached is None:
            return record
        if any(
            cached.get(name) != fresh.get(name)
            for name in ("guid", "result_table_name")
        ):
            # A run identity changing in place invalidates every observation
            # tied to its former result table.
            self._setpoint_summaries.pop(record.run_id, None)
            self._preview_array_columns.pop(record.run_id, None)
            self._preview_complex_columns.pop(record.run_id, None)
            self._setpoint_summaries_truncated.pop(record.run_id, None)
            self._setpoint_summary_watermarks.pop(record.run_id, None)
            self._setpoint_summary_names.pop(record.run_id, None)
            self._layouts.discard(record.run_id)
            self._unavailable_run_fields.pop(record.run_id, None)
            return record

        merged = dict(cached)
        authoritative_fields = (
            *_BASIC_RUN_COLUMNS,
            *_OPTIONAL_BASIC_RUN_COLUMNS,
            "exp_name",
            "sample_name",
            "database_modified_timestamp",
        )
        for name in authoritative_fields:
            if name != "run_id" and name in fresh:
                merged[name] = fresh[name]
        if fresh.get("preview_dimensions"):
            for name in (
                "preview_dimensions",
                "measure_parameters",
                "sweep_parameters",
            ):
                merged[name] = fresh[name]
        merged = _materialize_refreshed_basic_fields(merged, cached)
        unavailable_fields = tuple(
            dict.fromkeys(
                (
                    *self._unavailable_run_fields.get(record.run_id, ()),
                    *record.unavailable_fields,
                )
            )
        )
        return _bounded_run_record(
            record.run_id,
            merged,
            unavailable_fields,
        )

    def _cache_unavailable_fields(
        self,
        run_id: int,
        *field_groups: tuple[str, ...],
        observed_fields: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Replace observed omissions, retaining bounded names not fetched again."""

        observed = frozenset(observed_fields)
        merged = tuple(
            dict.fromkeys(
                (
                    *(
                        name for name in self._unavailable_run_fields.get(run_id, ())
                        if name not in observed
                    ),
                    *(name for group in field_groups for name in group),
                )
            )
        )
        public_fields, _truncated = bounded_presentation_names(merged)
        self._unavailable_run_fields[run_id] = public_fields
        return public_fields

    def cheap_run(self, run_id: int) -> TrustedRunRecord:
        """Refresh only run-table/experiment fields; never touch result tables."""

        self._require_cached_run(run_id)
        run_columns = self._ensure_current_schema()
        selected = tuple(
            name
            for name in (
                *_BASIC_RUN_COLUMNS,
                *_DEFERRED_BASIC_RUN_COLUMNS,
                *_OPTIONAL_BASIC_RUN_COLUMNS,
            )
            if name in run_columns
        )
        loaded = self._single_run_columns(run_id, selected)
        if loaded is None:
            raise TrustedMetadataQueryError(
                f"Run {run_id} disappeared during trusted metadata loading."
            )
        refreshed = dict(loaded.fields)
        refreshed.pop("run_id", None)
        # The broker may run a higher-priority selected/expensive operation at
        # a transaction boundary inside `_single_run_columns`.  Merge this
        # newer basic-column observation into that operation's latest cache
        # entry rather than restoring the entry snapshot and dropping its
        # counts, shapes, summaries, or storage observation.
        latest = self._require_cached_run(run_id)
        merged = dict(latest)
        merged.update(refreshed)
        merged["database_modified_timestamp"] = source_activity_timestamp(
            self._database_path
        )
        observed = _materialize_refreshed_basic_fields(merged, latest)
        self._runs[run_id] = _bounded_public_run_fields(observed)
        unavailable_fields = self._cache_unavailable_fields(
            run_id,
            loaded.unavailable_fields,
            observed_fields=tuple(loaded.fields),
        )
        return _bounded_run_record(
            run_id,
            observed,
            unavailable_fields,
        )

    def expensive_run(self, run_id: int) -> TrustedRunRecord:
        """Read counts, shapes, summaries, and storage without a long scan.

        Current QCoDeS result tables are append-only and use ``id INTEGER
        PRIMARY KEY``.  Capturing ``MAX(id)`` therefore gives both the accepted
        result count and an immutable prefix watermark through which later
        metadata transactions can read consistently.  Completed regular sweeps
        are inferred with fixed-size primary-key projections and logarithmic
        direct-ID probes.  Setpoint summaries use fixed-size ID windows, and
        storage size remains explicitly estimated.
        """

        metadata = dict(self._require_cached_run(run_id))
        if "raw_data_db_path" in self._ensure_current_schema():
            storage = self._executor.query(
                "SELECT raw_data_db_path IS NOT NULL FROM runs WHERE run_id=? LIMIT 1",
                (run_id,),
            ).rows
            if storage and storage[0][0]:
                raise TrustedLiveQueryError(SEPARATE_RAW_DATA_STORAGE_MESSAGE)
        if any(name not in metadata for name in _DEFERRED_BASIC_RUN_COLUMNS):
            selected = tuple(
                name
                for name in (*_DEFERRED_BASIC_RUN_COLUMNS, *_OPTIONAL_BASIC_RUN_COLUMNS)
                if name in self._ensure_current_schema()
            )
            loaded = self._single_run_columns(run_id, selected)
            if loaded is None:
                raise TrustedMetadataQueryError(
                    f"Run {run_id} disappeared during trusted metadata loading."
                )
            # A higher-priority operation can run immediately before the
            # deferred-column transaction.  Merge its result into the newest
            # cached record instead of restoring this method's entry snapshot.
            metadata = dict(self._require_cached_run(run_id))
            metadata.update(loaded.fields)
            metadata = materialize_run_basic_fields(metadata)
            self._runs[run_id] = _bounded_public_run_fields(metadata)
            self._cache_unavailable_fields(
                run_id,
                loaded.unavailable_fields,
                observed_fields=tuple(loaded.fields),
            )
        table_name = self._result_table_name(run_id, metadata)
        result_columns = self._ensure_result_columns(table_name)
        measure_parameters = tuple(metadata.get("measure_parameters") or ())
        sweep_parameters = tuple(metadata.get("sweep_parameters") or ())
        run_description = _json_object(metadata.get("run_description"))
        array_parameters = array_parameter_names(run_description)
        preview_columns = tuple(
            name for name in result_columns if name != "id"
        )[:TRUSTED_DERIVED_MAX_SOURCE_COLUMNS]
        complex_parameters = {
            parameter.name for parameter in parameter_views_from_run_metadata(metadata)
            if parameter.paramtype == "complex"
        }
        blob_parameters = array_parameters | complex_parameters
        self._preview_array_columns[run_id] = tuple(name for name in preview_columns if name in blob_parameters)
        self._preview_complex_columns[run_id] = tuple(name for name in preview_columns if name in complex_parameters)
        has_array_records = bool(
            array_parameters.intersection((*measure_parameters, *sweep_parameters))
        )
        dependency_sets = parameter_dependency_sets(
            run_description,
            measure_parameters,
            sweep_parameters,
            result_columns,
        )

        result_watermark = self._result_id_watermark(table_name)
        layout = self._layouts.observe_run(
            run_id, table_name, metadata, run_description, result_columns,
            result_watermark, dependency_sets, has_array_records,
        )
        observations = layout.observations
        nd_observations = layout.nd_observations
        nd_steps = layout.nd_steps
        regular_observation = layout.regular

        summary_names = tuple(
            name for name in sweep_parameters if name in result_columns
        )[:_TRUSTED_SETPOINT_SUMMARY_MAX_PARAMETERS]
        planned_steps = planned_setpoint_steps(metadata, summary_names)
        if not run_description.get("shapes"):
            planned_steps.update(nd_steps)
        if regular_observation is not None:
            planned_steps.update(
                dict(
                    zip(
                        regular_observation.dependencies,
                        regular_observation.shape,
                        strict=True,
                    )
                )
            )
        if self._setpoint_summary_watermarks.get(run_id) != result_watermark:
            summaries = list(
                self._bounded_setpoint_summaries(
                    table_name,
                    tuple(name for name in summary_names if name not in array_parameters),
                    result_watermark,
                    planned_steps,
                )
            )
            # Serialized NumPy records are not scalar first/last coordinates.
            # Retain a declared step count without exposing their blob bytes.
            summaries.extend(
                TrustedSetpointSummary(name, None, None, planned_steps[name])
                for name in summary_names
                if name in array_parameters and name in planned_steps
            )
            by_name = {summary.name: summary for summary in summaries}
            summaries = [by_name[name] for name in summary_names if name in by_name]
            public_summaries, summaries_truncated = bounded_setpoint_presentation(
                tuple(summaries)
            )
            self._setpoint_summaries[run_id] = public_summaries
            self._setpoint_summaries_truncated[run_id] = summaries_truncated
            self._setpoint_summary_watermarks[run_id] = result_watermark
            # Preserve source identity separately from presentation names,
            # which may be truncated.  Only parameters with both edges have
            # a summary, so this tuple follows the actual summaries' order.
            self._setpoint_summary_names[run_id] = tuple(
                summary.name for summary in summaries
            )
        else:
            # Progressive grid verification can prove counts without a new
            # result row.  Reconcile steps independently of the edge-value
            # watermark, retaining the bounded first/last values.  Replacing
            # rather than filling unknowns also clears now-ambiguous counts.
            self._setpoint_summaries[run_id] = tuple(
                replace(summary, steps=planned_steps.get(name))
                if summary.steps != planned_steps.get(name)
                else summary
                for name, summary in zip(
                    self._setpoint_summary_names[run_id],
                    self._setpoint_summaries[run_id],
                    strict=True,
                )
            )

        # Cooperative broker scheduling may execute a higher-priority cheap or
        # selected operation on this same adapter between any two supervisor
        # transactions above.  Merge these expensive observations into the
        # newest cached run only after the final query boundary; publishing the
        # entry snapshot would otherwise regress fresher run-table fields when
        # the outer operation resumes.
        latest_metadata = self._require_cached_run(run_id)
        read_setpoint_count = max((count for _, count, _ in observations), default=None)
        setpoint_shape: tuple[int, ...] | None = None
        setpoint_count: int | None = None
        if latest_metadata.get("setpoint_shape_source") != "planned" and observations:
            dependency_names = {names for names, _, _ in observations}
            shapes = [shape for _, _, shape in observations]
            if (
                len(dependency_names) == 1
                and shapes[0] is not None
                and all(shape == shapes[0] for shape in shapes)
            ):
                setpoint_shape = shapes[0]
            elif nd_observations:
                # The run count represents its largest measurement. Retain
                # that measurement's proven shape for the same breakdown as
                # ordinary grids, without multiplying the union of all axes.
                largest_shapes = [
                    shape for _names, count, shape in nd_observations
                    if count == read_setpoint_count
                ]
                if largest_shapes and all(
                    shape == largest_shapes[0] for shape in largest_shapes
                ):
                    setpoint_shape = largest_shapes[0]
            setpoint_count = read_setpoint_count

        storage_bytes = estimated_result_storage_bytes(
            result_watermark,
            result_columns,
            run_description,
        )
        observation_metadata = dict(latest_metadata)
        for name in _DEFERRED_BASIC_RUN_COLUMNS:
            if name in metadata:
                observation_metadata[name] = metadata[name]
        observed = materialize_run_observation(
            observation_metadata,
            result_count=result_watermark,
            setpoint_shape=setpoint_shape,
            setpoint_count=setpoint_count,
            read_setpoint_count=read_setpoint_count,
            storage_bytes=storage_bytes,
            storage_bytes_estimated=True,
        )
        # No current proof means an unavailable logical count, including when
        # a previously regular prefix has grown irregular or verification is
        # still pending. Publish the explicit None above and the regenerated
        # basic shape fields; retaining an older proof would misdescribe the
        # newly observed watermark. Planned shapes remain separate evidence.
        self._runs[run_id] = _bounded_public_run_fields(observed)
        return _bounded_run_record(
            run_id,
            observed,
            self._unavailable_run_fields.get(run_id, ()),
        )

    def derived_source_observation(
        self,
        run_id: int,
        *,
        database_instance: DatabaseInstance,
        namespace: TrustedSourceRevisionNamespace,
    ) -> TrustedDerivedSourceObservation:
        """Capture one renderable, bounded result prefix in a short transaction.

        The adapter cache is used only to choose a conservative set of columns
        and indexed window starts.  The authoritative run identity, schema,
        watermark, run-description facts, and samples are all re-read in one
        repeatable-read batch.  Every sampling statement is a primary-key
        keyset window; database size does not alter its row or cell budget.
        """

        if not isinstance(database_instance, DatabaseInstance):
            raise TypeError("database_instance must be a DatabaseInstance.")
        if database_instance.identity is None:
            raise ValueError("Derived extraction requires an exact database instance.")
        if not isinstance(namespace, TrustedSourceRevisionNamespace):
            raise TypeError("namespace must be TrustedSourceRevisionNamespace.")

        # Stage 5B metadata is a self-contained observation, not a consumer of
        # a separately scheduled Stage 4 enrichment. Refresh mutable run-table
        # fields first, then reuse the existing bounded expensive plan for
        # dimensions, setpoint summaries, count and storage observations.
        self.cheap_run(run_id)
        self.expensive_run(run_id)
        cached = dict(self._require_cached_run(run_id))
        table_name = self._result_table_name(run_id, cached)
        result_columns = self._ensure_result_columns(table_name)
        source_columns = tuple(name for name in result_columns if name != "id")
        retained_columns = source_columns[:TRUSTED_DERIVED_MAX_SOURCE_COLUMNS]
        table = quote_sqlite_identifier(table_name)
        cached_measure_parameters = tuple(cached.get("measure_parameters") or ())
        # A declared shape alone cannot prove physical row order.
        layout_proof = self._layouts.grid_proof(run_id)
        if layout_proof is not None and any(
            name not in retained_columns
            for name in (
                *layout_proof.dependencies,
                *(pattern.dependent for pattern in layout_proof.dependent_rows),
            )
        ):
            layout_proof = None
        independent = self._layouts.independent_state(run_id)
        layout_proofs = (
            independent.proofs
            if independent is not None
            else ((layout_proof,) if layout_proof is not None else ())
        )
        if independent is not None and layout_proof is not None:
            independently_proven = {
                pattern.dependent
                for proof in independent.proofs
                for pattern in proof.dependent_rows
            }
            compatible_rows = tuple(
                pattern
                for pattern in layout_proof.dependent_rows
                if pattern.dependent not in independently_proven
            )
            if compatible_rows:
                # Full-prefix shared verification can establish ownership of
                # NULL cells that an isolated dependent cannot infer safely.
                layout_proofs = (
                    *layout_proofs,
                    replace(layout_proof, dependent_rows=compatible_rows),
                )
        layout_proofs = tuple(
            proof
            for proof in layout_proofs
            if all(
                name in retained_columns
                for name in (
                    *proof.dependencies,
                    *(pattern.dependent for pattern in proof.dependent_rows),
                )
            )
        )
        grid_dependents = {
            pattern.dependent
            for proof in layout_proofs
            for pattern in proof.dependent_rows
        }
        needs_other_samples = bool(layout_proofs) and any(
            dependent not in grid_dependents for dependent in cached_measure_parameters
        )
        grid_budget = TRUSTED_DERIVED_MAX_GRID_SAMPLE_ROWS - (
            TRUSTED_DERIVED_ROWS_PER_WINDOW if needs_other_samples else 0
        )
        # Public cached run fields omit the full description. Retain the
        # codec names captured by expensive_run before it discards that text.
        complex_columns = set(self._preview_complex_columns.get(run_id, ())).intersection(retained_columns)
        if complex_columns:
            # Reserve a quarter of the byte budget for edge/window samples.
            # Bound grid BLOBs before they cross the trusted helper boundary.
            grid_budget = min(grid_budget, (TRUSTED_DERIVED_MAX_ARRAY_BYTES * 3 // 4)
                              // (len(complex_columns) * TRUSTED_DERIVED_MAX_COMPLEX_CELL_BYTES))
        grid_sample_plans = tuple(
            plan
            for proof in layout_proofs
            for plan in _trusted_2d_sample_plans(
                proof,
                maximum_rows=grid_budget // len(layout_proofs),
            )
        )

        numeric_expressions = tuple(
            "CASE WHEN typeof({column}) IN ('integer', 'real') "
            "THEN {column} ELSE NULL END AS {column}".format(
                column=quote_sqlite_identifier(name)
            )
            for name in retained_columns
        )
        selected = ", ".join(('"id"', *numeric_expressions))
        grid_selected = ", ".join(('"id"', *(
            f"CASE WHEN typeof({quote_sqlite_identifier(name)})='blob' AND "
            f"length({quote_sqlite_identifier(name)}) <= {TRUSTED_DERIVED_MAX_COMPLEX_CELL_BYTES} "
            f"THEN {quote_sqlite_identifier(name)} ELSE NULL END AS {quote_sqlite_identifier(name)}"
            if name in complex_columns else numeric
            for name, numeric in zip(retained_columns, numeric_expressions, strict=True)
        )))
        array_columns = set(self._preview_array_columns.get(run_id, ())).intersection(
            retained_columns
        )
        array_selected = selected
        if array_columns:
            # Bound the whole capture, including overlapping sampling windows,
            # before any BLOB crosses the helper boundary.
            window_budget = (TRUSTED_DERIVED_MAX_ARRAY_BYTES // 4
                             if complex_columns and grid_sample_plans else TRUSTED_DERIVED_MAX_ARRAY_BYTES)
            blob_limit = window_budget // (
                len(array_columns) * TRUSTED_DERIVED_ARRAY_ROWS_PER_WINDOW
                * (TRUSTED_DERIVED_SAMPLE_WINDOWS + 1)
            )
            expressions = []
            for name, numeric in zip(retained_columns, numeric_expressions, strict=True):
                column = quote_sqlite_identifier(name)
                expressions.append(
                    f"CASE WHEN typeof({column})='blob' AND length({column}) <= "
                    f"{blob_limit} THEN {column} ELSE NULL END AS {column}"
                    if name in array_columns else numeric
                )
            array_selected = ", ".join(('"id"', *expressions))
        maximum_expression = f'(SELECT COALESCE(MAX("id"), 0) FROM {table})'
        if grid_sample_plans:
            sampled_grid_ids = tuple(
                sorted(
                    {row_id for plan in grid_sample_plans for row_id in plan.row_ids}
                )
            )
            if len(sampled_grid_ids) > TRUSTED_DERIVED_MAX_GRID_SAMPLE_ROWS:
                raise TrustedMetadataQueryError(
                    "A derived grid sample exceeds its row budget."
                )
            placeholders = ", ".join("?" for _ in sampled_grid_ids)
            sample_queries = [
                TrustedQuery(
                    f"SELECT {grid_selected} FROM {table} "
                    f'WHERE "id" IN ({placeholders}) ORDER BY "id" '
                    f"LIMIT {TRUSTED_DERIVED_MAX_GRID_SAMPLE_ROWS}",
                    sampled_grid_ids,
                )
            ]
        else:
            sample_queries = []
        if not grid_sample_plans or needs_other_samples or array_columns:
            window_rows = (
                TRUSTED_DERIVED_ROWS_PER_WINDOW // (TRUSTED_DERIVED_SAMPLE_WINDOWS + 1)
                if grid_sample_plans
                else TRUSTED_DERIVED_ROWS_PER_WINDOW
            )
            if array_columns:
                window_rows = TRUSTED_DERIVED_ARRAY_ROWS_PER_WINDOW
            sample_queries.extend(
                TrustedQuery(
                    f"SELECT {array_selected} FROM {table} "
                    f'WHERE "id" > MAX(0, (({maximum_expression} * ?) / '
                    f"{TRUSTED_DERIVED_SAMPLE_WINDOWS}) - 1) "
                    f'AND "id" <= {maximum_expression} '
                    f'ORDER BY "id" LIMIT {window_rows}',
                    (index,),
                )
                for index in range(TRUSTED_DERIVED_SAMPLE_WINDOWS)
            )
            # Preserve the established 1D newest-edge sample even when the
            # cached count was stale before this transaction began.
            sample_queries.append(
                TrustedQuery(
                    f"SELECT {array_selected} FROM {table} "
                    f'WHERE "id" <= {maximum_expression} ORDER BY "id" DESC '
                    f"LIMIT {window_rows}"
                )
            )

        incarnation = self._executor.incarnation
        data_version = self._executor.data_version()
        identity_sql = (
            'SELECT "run_id", '
            f'CASE WHEN octet_length("guid") <= {_TRUSTED_BASIC_TEXT_MAX_BYTES} '
            'THEN "guid" ELSE NULL END AS "guid", '
            f'CASE WHEN octet_length("result_table_name") '
            f'<= {_TRUSTED_RESULT_SCHEMA_SQL_MAX_BYTES} THEN "result_table_name" '
            'ELSE NULL END AS "result_table_name", '
            f'CASE WHEN octet_length("parameters") '
            f'<= {TRUSTED_DERIVED_RUN_TEXT_MAX_BYTES} THEN "parameters" '
            'ELSE NULL END AS "parameters", '
            f'CASE WHEN octet_length("run_description") '
            f'<= {TRUSTED_DERIVED_RUN_TEXT_MAX_BYTES} THEN "run_description" '
            'ELSE NULL END AS "run_description" '
            'FROM "runs" WHERE "run_id" = ?'
        )
        schema_sql = (
            "SELECT CASE WHEN octet_length(sql) <= ? THEN sql ELSE NULL END AS sql, "
            "octet_length(sql) AS sql_bytes FROM sqlite_schema "
            "WHERE type = 'table' AND name = ?"
        )
        results = self._executor.query_batch(
            (
                TrustedQuery(identity_sql, (run_id,)),
                TrustedQuery(f"SELECT * FROM {table} WHERE 0"),
                TrustedQuery(
                    schema_sql,
                    (_TRUSTED_RESULT_SCHEMA_SQL_MAX_BYTES, table_name),
                ),
                TrustedQuery(
                    f'SELECT COALESCE(MAX("id"), 0) AS result_watermark FROM {table}'
                ),
                *sample_queries,
            )
        )
        if self._executor.incarnation != incarnation:
            raise TrustedMetadataQueryError(
                "The trusted helper incarnation changed during derived extraction."
            )

        identity = _one_row_mapping(results[0])
        if identity is None:
            raise TrustedMetadataQueryError(
                f"Run {run_id} disappeared during derived extraction."
            )
        guid = identity.get("guid")
        observed_table = identity.get("result_table_name")
        if not isinstance(guid, str) or not guid:
            raise TrustedMetadataQueryError("The run GUID is unavailable or oversized.")
        if observed_table != table_name:
            raise TrustedMetadataQueryError(
                "The run result-table identity changed during derived extraction."
            )

        observed_columns = tuple(results[1].columns)
        schema_result = results[2]
        if len(schema_result.rows) != 1 or len(schema_result.rows[0]) != 2:
            raise TrustedMetadataQueryError("The result-table schema disappeared.")
        raw_schema, raw_schema_bytes = schema_result.rows[0]
        if (
            not isinstance(raw_schema, str)
            or type(raw_schema_bytes) is not int
            or raw_schema_bytes < 0
            or raw_schema_bytes > _TRUSTED_RESULT_SCHEMA_SQL_MAX_BYTES
            or observed_columns != result_columns
            or "id" not in observed_columns
            or _QCODES_RESULT_ID_SCHEMA.search(raw_schema) is None
        ):
            raise TrustedUnsupportedQcodesSchemaError(
                "The result-table schema changed or exceeds derived-work bounds."
            )
        watermark = _single_integer(results[3], "derived result watermark")

        rows_by_id: dict[int, tuple[PrimitiveScalar, ...]] = {}
        expected_columns = ("id", *retained_columns)
        for result in results[4:]:
            if tuple(result.columns) != expected_columns:
                raise TrustedMetadataQueryError(
                    "A derived sample window returned unexpected columns."
                )
            for row in result.rows:
                if len(row) != len(expected_columns):
                    raise TrustedMetadataQueryError(
                        "A derived sample row has an invalid cell count."
                    )
                row_id = row[0]
                if type(row_id) is not int or not 0 < row_id <= watermark:
                    raise TrustedMetadataQueryError(
                        "A derived sample escaped its captured result prefix."
                    )
                if any(
                    value is not None
                    and not isinstance(value, (bool, int, float, str, bytes))
                    for value in row
                ):
                    raise TrustedMetadataQueryError(
                        "A derived sample contains a non-primitive scalar."
                    )
                rows_by_id[row_id] = cast(tuple[PrimitiveScalar, ...], tuple(row))
        sample_rows = tuple(rows_by_id[key] for key in sorted(rows_by_id))[
            :TRUSTED_DERIVED_MAX_SAMPLE_ROWS
        ]

        # Every executor transaction above is a cooperative broker yield
        # boundary. A higher-priority operation may therefore have published a
        # newer or more complete adapter entry while this extraction was
        # sampling. Merge the captured prefix onto that latest entry; never
        # restore the method-entry snapshot and erase completion/dimension
        # facts or unavailable-field provenance.
        latest_table_name = self._result_table_name(
            run_id, self._require_cached_run(run_id)
        )
        latest_metadata = self._require_cached_run(run_id)
        if (
            latest_metadata.get("guid") != guid
            or latest_table_name != table_name
        ):
            raise TrustedMetadataQueryError(
                "The accepted run identity changed during derived extraction."
            )
        raw_metadata = {
            **latest_metadata,
            "run_id": run_id,
            "guid": guid,
            "result_table_name": table_name,
            "parameters": identity.get("parameters"),
            "run_description": identity.get("run_description"),
        }
        materialized = materialize_run_basic_fields(raw_metadata)
        # The captured raw description supplies fields that were unavailable
        # from the bounded adapter cache, but a cooperatively scheduled
        # operation may already have published newer public facts.  Those
        # facts (notably observed/planned dimensions and completion) win over
        # a rematerialization of this extraction's older description.
        materialized.update(latest_metadata)
        run_description = _json_object(identity.get("run_description"))
        measure_parameters = tuple(materialized.get("measure_parameters") or ())
        sweep_parameters = tuple(materialized.get("sweep_parameters") or ())
        dependency_sets = parameter_dependency_sets(
            run_description,
            measure_parameters,
            sweep_parameters,
            observed_columns,
        )
        dependent_parameters = tuple(
            dependent
            for _setpoints, dependent in dependency_sets
            if dependent is not None
        )[:TRUSTED_DERIVED_MAX_DEPENDENTS]
        parameter_views, _parameter_truncated = parameter_views_bounded(
            materialized,
            TrustedQueryResult((), ()),
        )
        bounded_parameters, _presentation_truncated = bounded_parameter_presentation(
            parameter_views
        )
        # Setpoint shape is the scientific grid.  ``point_shape`` may append a
        # measured-parameter row factor (for example 251 x 16 x 2) and must
        # never be mistaken for a third sweep dependency.
        raw_shape = materialized.get("setpoint_shape") or materialized.get(
            "point_shape"
        )
        planned_shape = (
            tuple(value for value in raw_shape if type(value) is int and value > 0)
            if isinstance(raw_shape, (list, tuple))
            else None
        )
        unsupported_reason = None
        if len(source_columns) > TRUSTED_DERIVED_MAX_SOURCE_COLUMNS:
            unsupported_reason = (
                "The result has more source columns than the bounded renderer supports."
            )

        storage_bytes = estimated_result_storage_bytes(
            watermark,
            observed_columns,
            run_description,
        )
        observed_run = materialize_run_observation(
            materialized,
            result_count=watermark,
            storage_bytes=storage_bytes,
            storage_bytes_estimated=True,
        )
        for name, value in latest_metadata.items():
            if name not in {"result_count", "storage_bytes", "storage_bytes_estimated"}:
                observed_run[name] = value
        bounded_run = _bounded_public_run_fields(observed_run)
        self._runs[run_id] = bounded_run

        validated_layouts: tuple[Trusted2DGridLayout, ...] = ()
        if grid_sample_plans:
            authoritative_dependencies = {
                (setpoints, dependent)
                for setpoints, dependent in dependency_sets
                if dependent is not None
            }
            completed = bool(materialized.get("is_completed"))
            layouts = []
            for plan in grid_sample_plans:
                proof = plan.proof
                proof_matches = (
                    proof.dependencies,
                    plan.pattern.dependent,
                ) in authoritative_dependencies
                if independent is not None and proof in independent.proofs:
                    proof_matches = proof_matches and (
                        independent.result_watermark == watermark
                        and independent.signature
                        == self._layouts.independent_signature(
                            dependency_sets,
                            run_description,
                        )
                    )
                else:
                    public_shape = materialized.get("setpoint_shape")
                    slow_count = proof.shape[1 - proof.fast_axis_index]
                    fast_count = proof.shape[proof.fast_axis_index]
                    last_pattern_ids = tuple(
                        pattern.first_row_id
                        + (slow_count - 1) * pattern.slow_id_stride
                        + (fast_count - 1) * pattern.fast_id_stride
                        for pattern in (
                            layout_proof.dependent_rows
                            if layout_proof is not None
                            else proof.dependent_rows
                        )
                    )
                    proof_matches = proof_matches and (
                        isinstance(public_shape, (list, tuple))
                        and tuple(public_shape) == proof.shape
                        and materialized.get("setpoint_shape_source") == proof.source
                        and watermark <= max(last_pattern_ids)
                        and (not completed or watermark == max(last_pattern_ids))
                    )
                layout = (
                    _validated_sampled_grid_layout(
                        plan,
                        sample_columns=expected_columns,
                        sample_rows=sample_rows,
                        result_watermark=watermark,
                        completed=completed,
                    )
                    if proof_matches
                    else None
                )
                if layout is not None:
                    layouts.append(layout)
            validated_layouts = tuple(layouts)

        progress = self._layouts.pending_state(run_id)
        progressive_layout_cursor = progress.after_row_id if progress is not None else 0
        independent_pending = (
            independent is not None
            and independent.after_row_id < independent.result_watermark
        )
        if independent_pending:
            assert independent is not None
            progressive_layout_cursor = independent.after_row_id
        nd_counts = self._layouts.nd_state(run_id)
        nd_pending = (
            nd_counts is not None and nd_counts.after_row_id < nd_counts.result_watermark
        )
        if nd_pending:
            assert nd_counts is not None
            progressive_layout_cursor = nd_counts.after_row_id

        return TrustedDerivedSourceObservation(
            format_version=1,
            database_instance=database_instance,
            run_id=run_id,
            run_guid=guid,
            service_namespace=namespace.nonce,
            helper_incarnation=incarnation,
            data_version=data_version,
            result_table_name=table_name,
            result_columns=observed_columns,
            result_schema_sha256=hashlib.sha256(raw_schema.encode("utf-8")).digest(),
            result_watermark=watermark,
            parameters=bounded_parameters,
            dependent_parameters=dependent_parameters,
            planned_shape=planned_shape,
            sample_columns=expected_columns,
            sample_rows=sample_rows,
            setpoint_summaries=self._setpoint_summaries.get(run_id, ()),
            unsupported_reason=unsupported_reason,
            run_fields=_freeze_fields(bounded_run),
            validated_2d_layouts=validated_layouts,
            progressive_layout_pending=(
                progress is not None or independent_pending or nd_pending
            ),
            progressive_layout_cursor=progressive_layout_cursor,
        )

    def selected_run_detail(self, run_id: int) -> TrustedSelectedRunDetail:
        """Materialise overview/parameter/metadata/snapshot view primitives."""

        cached = self._require_cached_run(run_id)
        run_columns = self._ensure_current_schema()
        loaded = self._single_run_columns(run_id, run_columns)
        if loaded is None:
            raise TrustedMetadataQueryError(
                f"Run {run_id} disappeared during selected-detail loading."
            )
        # Grouped column reads can themselves yield to a newer cheap refresh.
        # Reconcile only observations still used by the newest-cache merge,
        # then let later layout-boundary refreshes retain their omission state.
        latest_after_columns = self._require_cached_run(run_id)
        cache_unchanged = latest_after_columns == cached
        observed_fields = tuple(
            name for name, value in loaded.fields.items()
            if cache_unchanged
            or name not in latest_after_columns
            or latest_after_columns[name] == value
        )
        self._cache_unavailable_fields(
            run_id,
            tuple(name for name in loaded.unavailable_fields if name in observed_fields),
            observed_fields=observed_fields,
        )
        layout_watermark_result = self._executor.query(
            'SELECT COALESCE(MAX("layout_id"), 0) FROM "layouts" WHERE "run_id" = ?',
            (run_id,),
        )
        layout_watermark = _single_integer(
            layout_watermark_result,
            "selected-run layout watermark",
        )
        layout_result, layout_unavailable = self._selected_layout_pages(
            run_id,
            layout_watermark,
            max_raw_bytes=max(
                0,
                _TRUSTED_SINGLE_RUN_MAX_NON_SNAPSHOT_RAW_BYTES
                - loaded.accepted_non_snapshot_raw_bytes,
            ),
        )
        full = dict(loaded.fields)
        full.pop("run_id", None)
        latest_cached = self._require_cached_run(run_id)
        selected_standard = {
            key: value for key, value in full.items() if key in _STANDARD_RUN_COLUMNS
        }
        if latest_cached == cached:
            # No nested operation updated this run, so the selected-column
            # transaction is the newest complete standard-field observation.
            merged = dict(cached)
            merged.update(selected_standard)
        else:
            # Layout watermark/pages are cooperative yield boundaries.  If a
            # higher-priority cheap refresh updated the run after the selected
            # column transaction, retain every field it already knows and add
            # only standard fields that the lightweight cache does not carry.
            merged = dict(latest_cached)
            for key, value in selected_standard.items():
                merged.setdefault(key, value)
        merged["database_modified_timestamp"] = source_activity_timestamp(
            self._database_path
        )
        merged = _materialize_refreshed_basic_fields(merged, latest_cached)
        self._runs[run_id] = _bounded_public_run_fields(merged)

        metadata = {
            key: value
            for key, value in full.items()
            if key not in _STANDARD_RUN_COLUMNS and value is not None
        }
        snapshot_raw = full.get("snapshot")
        snapshot = normalize_trusted_snapshot(
            snapshot_raw,
            omission=loaded.snapshot_omission,
        )
        raw_parameters, source_parameters_truncated = parameter_views_bounded(
            merged,
            layout_result,
        )
        parameters, parameters_truncated = bounded_parameter_presentation(
            raw_parameters
        )
        parameters_truncated = bool(
            parameters_truncated
            or source_parameters_truncated
            or merged.get("parameters_truncated")
        )
        setpoint_summaries, summaries_truncated = bounded_setpoint_presentation(
            self._setpoint_summaries.get(run_id, ())
        )
        summaries_truncated = bool(
            summaries_truncated or self._setpoint_summaries_truncated.get(run_id, False)
        )
        presentation_unavailable = (
            *(("parameters.presentation",) if parameters_truncated else ()),
            *(("setpoint_summaries.presentation",) if summaries_truncated else ()),
        )
        public_unavailable_fields = self._cache_unavailable_fields(
            run_id,
            layout_unavailable,
            presentation_unavailable,
        )
        presentation = build_selected_run_presentation(
            run_fields={**merged, "run_id": run_id},
            metadata_fields=metadata,
            parameters=tuple(
                {
                    "name": parameter.name,
                    "label": parameter.label,
                    "unit": parameter.unit,
                    "depends_on": parameter.depends_on,
                    "type": parameter.paramtype,
                }
                for parameter in parameters
            ),
            snapshot_summary={
                "Status": (
                    "available — loaded on demand"
                    if snapshot.status == "available"
                    else snapshot.status
                ),
                "Message": snapshot.message,
                "Input bytes": snapshot.input_bytes,
                "Initial page nodes": len(snapshot.nodes),
            },
            setpoint_summaries=tuple(
                {
                    "name": summary.name,
                    "from": summary.first,
                    "to": summary.last,
                    "steps": summary.steps,
                }
                for summary in setpoint_summaries
            ),
            unavailable_fields=public_unavailable_fields,
            parameters_truncated=parameters_truncated,
        )
        return TrustedSelectedRunDetail(
            run=TrustedRunRecord(
                run_id,
                _freeze_fields(dict(presentation.run_fields)),
                public_unavailable_fields,
            ),
            parameters=parameters,
            metadata=_freeze_fields(dict(presentation.metadata_fields)),
            snapshot=snapshot,
            setpoint_summaries=setpoint_summaries,
            presentation=presentation,
            unavailable_fields=public_unavailable_fields,
        )

    def _selected_layout_pages(
        self,
        run_id: int,
        through_layout_id: int,
        *,
        max_raw_bytes: int,
    ) -> tuple[TrustedQueryResult, tuple[str, ...]]:
        """Drain fixed-size selected-run layout pages through one watermark."""

        columns = ("layout_id", "parameter", "label", "unit", "inferred_from")
        if through_layout_id == 0:
            return TrustedQueryResult(columns, ()), ()

        if max_raw_bytes <= 0:
            return TrustedQueryResult(columns, ()), ("layouts",)

        sql = (
            "SELECT layout_id, "
            f"CASE WHEN octet_length(parameter) <= {_TRUSTED_LAYOUT_TEXT_MAX_BYTES} "
            "THEN parameter ELSE NULL END AS parameter, "
            f"CASE WHEN octet_length(label) <= {_TRUSTED_LAYOUT_TEXT_MAX_BYTES} "
            "THEN label ELSE NULL END AS label, "
            f"CASE WHEN octet_length(unit) <= {_TRUSTED_LAYOUT_TEXT_MAX_BYTES} "
            "THEN unit ELSE NULL END AS unit, "
            f"CASE WHEN octet_length(inferred_from) <= {_TRUSTED_LAYOUT_TEXT_MAX_BYTES} "
            "THEN inferred_from ELSE NULL END AS inferred_from, "
            "octet_length(parameter), octet_length(label), octet_length(unit), "
            "octet_length(inferred_from) "
            "FROM layouts WHERE run_id = ? AND layout_id > ? "
            "AND layout_id <= ? ORDER BY layout_id LIMIT ?"
        )
        rows: list[tuple[Any, ...]] = []
        unavailable: list[str] = []
        accepted_raw_bytes = 0
        cursor = 0
        while cursor < through_layout_id and len(rows) < _TRUSTED_LAYOUT_MAX_ROWS:
            remaining_rows = _TRUSTED_LAYOUT_MAX_ROWS - len(rows)
            page = self._executor.query(
                sql,
                (
                    run_id,
                    cursor,
                    through_layout_id,
                    min(self._page_size, remaining_rows),
                ),
            )
            if len(page.columns) != 9:
                raise TrustedMetadataQueryError(
                    "A trusted selected-run layout page has invalid columns."
                )
            prior = cursor
            for row in page.rows:
                if len(row) != 9:
                    raise TrustedMetadataQueryError(
                        "A trusted selected-run layout row has an invalid column count."
                    )
                layout_id = row[0]
                if (
                    type(layout_id) is not int
                    or layout_id <= prior
                    or layout_id > through_layout_id
                ):
                    raise TrustedMetadataQueryError(
                        "A trusted selected-run layout page is not strictly ordered "
                        "by layout_id."
                    )
                prior = layout_id
                raw_sizes = row[5:]
                if any(
                    size is not None and (type(size) is not int or size < 0)
                    for size in raw_sizes
                ):
                    raise TrustedMetadataQueryError(
                        "A trusted selected-run layout row has invalid byte lengths."
                    )
                oversized_names = tuple(
                    name
                    for name, value, size in zip(
                        columns[1:], row[1:5], raw_sizes, strict=True
                    )
                    if size is not None
                    and (size > _TRUSTED_LAYOUT_TEXT_MAX_BYTES or value is None)
                )
                unavailable.extend(
                    f"layouts.{layout_id}.{name}" for name in oversized_names
                )
                row_bytes = sum(
                    size
                    for value, size in zip(row[1:5], raw_sizes, strict=True)
                    if value is not None and type(size) is int
                )
                if row_bytes > max_raw_bytes - accepted_raw_bytes:
                    unavailable.append("layouts")
                    return (
                        TrustedQueryResult(columns, tuple(rows)),
                        tuple(dict.fromkeys(unavailable)),
                    )
                accepted_raw_bytes += row_bytes
                rows.append(row[:5])
            if not page.rows or len(page.rows) < min(self._page_size, remaining_rows):
                break
            cursor = prior

        if rows and rows[-1][0] < through_layout_id:
            unavailable.append("layouts")
        return (
            TrustedQueryResult(columns, tuple(rows)),
            tuple(dict.fromkeys(unavailable)),
        )

    def _ensure_current_schema(self) -> tuple[str, ...]:
        if self._run_columns is not None:
            return self._run_columns
        run_schema, experiment_schema, layout_schema = self._executor.query_batch(
            (
                TrustedQuery('SELECT * FROM "runs" WHERE 0'),
                TrustedQuery('SELECT * FROM "experiments" WHERE 0'),
                TrustedQuery('SELECT * FROM "layouts" WHERE 0'),
            )
        )
        run_columns = tuple(run_schema.columns)
        missing_runs = sorted(_REQUIRED_RUN_COLUMNS.difference(run_columns))
        missing_experiments = sorted(
            _REQUIRED_EXPERIMENT_COLUMNS.difference(experiment_schema.columns)
        )
        required_layouts = {"layout_id", "run_id", "parameter", "label", "unit"}
        missing_layouts = sorted(required_layouts.difference(layout_schema.columns))
        if missing_runs or missing_experiments or missing_layouts:
            missing = ", ".join(
                [
                    *(f"runs.{name}" for name in missing_runs),
                    *(f"experiments.{name}" for name in missing_experiments),
                    *(f"layouts.{name}" for name in missing_layouts),
                ]
            )
            raise TrustedUnsupportedQcodesSchemaError(
                "The database is not the current supported QCoDeS schema; "
                f"missing {missing}."
            )
        self._run_columns = run_columns
        return run_columns

    def _maximum_run_id(self) -> int:
        result = self._executor.query('SELECT COALESCE(MAX("run_id"), 0) FROM "runs"')
        return _single_integer(result, "run-id watermark")

    def _run_select_sql(
        self,
        *,
        where: str,
        order_limit: bool,
        include_dynamic: bool,
    ) -> str:
        run_columns = self._ensure_current_schema()
        if include_dynamic:
            raise TrustedMetadataQueryError(
                "Dynamic run columns require the collision-free selected-run plan."
            )
        selected = tuple(
            name
            for name in (*_BASIC_RUN_COLUMNS, *_OPTIONAL_BASIC_RUN_COLUMNS)
            if name in run_columns
        )
        run_expressions: list[str] = []
        for name in selected:
            quoted = quote_sqlite_identifier(name)
            source = f"runs.{quoted}"
            if name in _BASIC_PAGED_TEXT_COLUMNS:
                source = (
                    "CASE WHEN octet_length("
                    f"{source}) <= {_TRUSTED_BASIC_TEXT_MAX_BYTES} "
                    f"THEN {source} ELSE NULL END"
                )
            run_expressions.append(f"{source} AS {quoted}")
        run_select = ", ".join(run_expressions)
        if "run_description" in run_columns:
            compact_description = (
                "json_object('interdependencies_', json_object('dependencies', "
                "json_extract(runs.\"run_description\", "
                "'$.interdependencies_.dependencies')))"
            )
            run_select += (
                ', CASE WHEN octet_length(runs."run_description") <= '
                f"{_TRUSTED_PLACEHOLDER_DESCRIPTION_MAX_BYTES} "
                'THEN runs."run_description" '
                'WHEN octet_length(runs."run_description") <= '
                f"{_TRUSTED_PLACEHOLDER_DESCRIPTION_SOURCE_MAX_BYTES} "
                'THEN CASE WHEN json_valid(runs."run_description") '
                f"THEN CASE WHEN octet_length({compact_description}) <= "
                f"{_TRUSTED_PLACEHOLDER_DESCRIPTION_MAX_BYTES} "
                f"THEN {compact_description} END END "
                'ELSE NULL END AS "run_description"'
            )
        experiment_expressions = []
        for source_name, alias in (
            ("name", "exp_name"),
            ("sample_name", "sample_name"),
        ):
            source = f"experiments.{quote_sqlite_identifier(source_name)}"
            quoted_alias = quote_sqlite_identifier(alias)
            experiment_expressions.append(
                "CASE WHEN octet_length("
                f"{source}) <= {_TRUSTED_BASIC_TEXT_MAX_BYTES} "
                f"THEN {source} ELSE NULL END AS {quoted_alias}"
            )
        experiment_select = ", ".join(experiment_expressions)
        suffix = ' ORDER BY runs."run_id" LIMIT ?' if order_limit else ""
        return (
            f'SELECT {run_select}, {experiment_select} FROM "runs" AS runs '
            'LEFT JOIN "experiments" AS experiments '
            'ON runs."exp_id" = experiments."exp_id" '
            f"WHERE {where}{suffix}"
        )

    def _single_run_columns(
        self,
        run_id: int,
        columns: tuple[str, ...],
    ) -> _SingleRunValues | None:
        """Fetch one run through payload-preflighted, guarded statement groups.

        The preflight uses SQLite's non-materialising ``octet_length`` metadata
        operation.  Each later SELECT repeats the observed type and byte length
        as a guard, so a concurrently enlarged value becomes NULL instead of
        crossing the supervisor's scalar, row, Python-object, or wire limits.
        Groups contain at most 4 MiB of observed raw payload and 32 columns;
        values above 128 KiB receive a one-column statement so Stage 3's
        width-derived per-value limit is also satisfied. The selected Snapshot
        has its own at-most-4-MiB source budget instead of consuming the bounded
        aggregate for non-Snapshot detail fields. Even worst-case JSON escaping
        therefore remains below the 32 MiB wire envelope for every query.
        """

        if not columns:
            return _SingleRunValues({}, (), 0)
        preflight_queries = tuple(
            TrustedQuery(
                "SELECT "
                f'typeof({quote_sqlite_identifier(name)}) AS "value_type", '
                f'octet_length({quote_sqlite_identifier(name)}) AS "value_bytes" '
                'FROM "runs" WHERE "run_id" = ?',
                (run_id,),
            )
            for name in columns
        )
        preflight_results = self._bounded_query_batches(
            preflight_queries,
            batch_size=_TRUSTED_SINGLE_RUN_PREFLIGHT_BATCH_SIZE,
        )
        preflight: list[_SingleRunColumnPreflight] = []
        for name, result in zip(columns, preflight_results, strict=True):
            if not result.rows:
                return None
            if (
                tuple(result.columns) != ("value_type", "value_bytes")
                or len(result.rows) != 1
                or len(result.rows[0]) != 2
            ):
                raise TrustedMetadataQueryError(
                    "A trusted single-run scalar preflight returned an invalid shape."
                )
            value_type, value_bytes = result.rows[0]
            if value_type == "null" and value_bytes is None:
                preflight.append(_SingleRunColumnPreflight(name, value_type, None))
                continue
            if (
                value_type not in {"integer", "real", "text", "blob"}
                or type(value_bytes) is not int
                or value_bytes < 0
            ):
                raise TrustedMetadataQueryError(
                    "A trusted single-run scalar preflight returned invalid metadata."
                )
            preflight.append(_SingleRunColumnPreflight(name, value_type, value_bytes))

        values: dict[str, Any] = {}
        unavailable: list[str] = []
        group: list[_SingleRunColumnPreflight] = []
        group_bytes = 0
        accepted_non_snapshot_raw_bytes = 0
        snapshot_omission: TrustedSnapshotOmission | None = None

        def flush_group() -> bool:
            nonlocal group, group_bytes, snapshot_omission
            if not group:
                return True
            expressions: list[str] = []
            bindings: list[PrimitiveScalar] = []
            for item in group:
                quoted = quote_sqlite_identifier(item.name)
                expressions.append(
                    "CASE WHEN "
                    f"typeof({quoted}) = ? AND octet_length({quoted}) = ? "
                    f"THEN {quoted} ELSE NULL END AS {quoted}"
                )
                bindings.extend((item.value_type, item.value_bytes))
            result = self._executor.query(
                f'SELECT {", ".join(expressions)} FROM "runs" WHERE "run_id" = ?',
                (*bindings, run_id),
            )
            mapping = _one_row_mapping(result)
            if mapping is None:
                return False
            values.update(mapping)
            unavailable.extend(
                item.name for item in group if mapping.get(item.name) is None
            )
            for item in group:
                if item.name == "snapshot" and mapping.get(item.name) is None:
                    snapshot_omission = TrustedSnapshotOmission(
                        "changed_during_read",
                        item.value_bytes,
                    )
            group = []
            group_bytes = 0
            return True

        for item in preflight:
            if item.value_bytes is None:
                values[item.name] = None
                continue
            is_snapshot = item.name == "snapshot"
            if item.value_bytes > (
                _TRUSTED_SINGLE_RUN_MAX_SNAPSHOT_RAW_BYTES
                if is_snapshot
                else TRUSTED_LIVE_MAX_SCALAR_BYTES
            ) or (
                not is_snapshot
                and item.value_bytes
                > _TRUSTED_SINGLE_RUN_MAX_NON_SNAPSHOT_RAW_BYTES
                - accepted_non_snapshot_raw_bytes
            ):
                values[item.name] = None
                unavailable.append(item.name)
                if is_snapshot:
                    snapshot_omission = TrustedSnapshotOmission(
                        "payload_limit",
                        item.value_bytes,
                    )
                continue
            if item.value_bytes > _TRUSTED_SINGLE_RUN_GROUP_MAX_SHARED_VALUE_BYTES:
                if not flush_group():
                    return None
                group.append(item)
                group_bytes = item.value_bytes
                if not is_snapshot:
                    accepted_non_snapshot_raw_bytes += item.value_bytes
                if not flush_group():
                    return None
                continue
            if group and (
                len(group) >= _TRUSTED_SINGLE_RUN_GROUP_MAX_COLUMNS
                or group_bytes + item.value_bytes
                > _TRUSTED_SINGLE_RUN_GROUP_MAX_RAW_BYTES
            ):
                if not flush_group():
                    return None
            group.append(item)
            group_bytes += item.value_bytes
            if not is_snapshot:
                accepted_non_snapshot_raw_bytes += item.value_bytes
        if not flush_group():
            return None
        return _SingleRunValues(
            values,
            tuple(dict.fromkeys(unavailable)),
            accepted_non_snapshot_raw_bytes,
            snapshot_omission,
        )

    def _materialize_basic_page(
        self,
        result: TrustedQueryResult,
    ) -> list[TrustedRunRecord]:
        modified = source_activity_timestamp(self._database_path)
        records: list[TrustedRunRecord] = []
        prior = 0
        for row in result.rows:
            if len(row) != len(result.columns):
                raise TrustedMetadataQueryError(
                    "A trusted basic-run row has an invalid column count."
                )
            values = dict(zip(result.columns, row, strict=True))
            raw_run_id = values.pop("run_id", None)
            if type(raw_run_id) is not int or raw_run_id <= prior:
                raise TrustedMetadataQueryError(
                    "A trusted basic-run page is not strictly ordered by run_id."
                )
            prior = raw_run_id
            values["database_modified_timestamp"] = modified
            materialized = materialize_run_basic_fields(values)
            description = _json_object(values.get("run_description"))
            interdependencies = description.get("interdependencies_", {})
            dependencies = (
                interdependencies.get("dependencies", {})
                if isinstance(interdependencies, dict)
                else {}
            )
            dimensions = (
                {
                    name: len(axes)
                    for name, axes in dependencies.items()
                    if name in materialized.get("measure_parameters", ())
                    and isinstance(axes, list)
                    and axes
                    and all(isinstance(axis, str) for axis in axes)
                }
                if isinstance(dependencies, dict)
                else {}
            )
            materialized["preview_dimensions"] = [
                dimensions.get(name)
                for name in materialized.get("measure_parameters", ())
            ]
            # Retain only compact derived labels, not per-run source documents.
            materialized.pop("run_description", None)
            records.append(TrustedRunRecord(raw_run_id, _freeze_fields(materialized)))
        return records

    def _bounded_query_batches(
        self,
        queries: tuple[TrustedQuery, ...],
        *,
        batch_size: int = _TRUSTED_AGGREGATE_BATCH_SIZE,
    ) -> tuple[TrustedQueryResult, ...]:
        """Keep multi-statement transactions small and cancellable.

        Aggregate callers bind every result-table query to one captured ``id``
        watermark. QCoDeS only appends result rows, so splitting that work does
        not sacrifice consistency and gives the writer checkpoint opportunities
        between batches. Scalar preflight callers use the same boundary for
        small fixed groups of metadata statements.
        """

        results: list[TrustedQueryResult] = []
        for offset in range(0, len(queries), batch_size):
            results.extend(
                self._executor.query_batch(queries[offset : offset + batch_size])
            )
        return tuple(results)

    def _require_cached_run(self, run_id: int) -> dict[str, Any]:
        if type(run_id) is not int or run_id <= 0:
            raise ValueError("run_id must be a positive integer.")
        metadata = self._runs.get(run_id)
        if metadata is None:
            raise TrustedMetadataQueryError(
                f"Run {run_id} is not part of this accepted service session."
            )
        return dict(metadata)

    def _result_table_name(self, run_id: int, metadata: dict[str, Any]) -> str:
        table_name = metadata.get("result_table_name")
        # The retained run cache is a presentation model: paging can omit a
        # long name and later observations shorten it with an ellipsis. Never
        # use that display spelling as a SQL identifier. Read the exact source
        # value through the existing payload-preflighted selected-column plan,
        # without replacing the display value. A genuine ellipsis spelling is
        # re-read too, so no display marker is interpreted as source identity.
        if table_name is None or (
            isinstance(table_name, str) and table_name.endswith("...")
        ):
            loaded = self._single_run_columns(run_id, ("result_table_name",))
            table_name = (
                loaded.fields.get("result_table_name") if loaded is not None else None
            )
        if (
            not isinstance(table_name, str)
            or not table_name
            or len(table_name.encode("utf-8")) > _TRUSTED_RESULT_SCHEMA_SQL_MAX_BYTES
        ):
            raise TrustedMetadataQueryError(
                "A run does not identify a bounded QCoDeS result table."
            )
        return table_name

    def _ensure_result_columns(self, table_name: str) -> tuple[str, ...]:
        cached = self._result_columns.get(table_name)
        if cached is not None:
            return cached
        result, schema = self._executor.query_batch(
            (
                TrustedQuery(
                    f"SELECT * FROM {quote_sqlite_identifier(table_name)} WHERE 0"
                ),
                TrustedQuery(
                    "SELECT CASE WHEN octet_length(sql) <= ? THEN sql ELSE NULL END "
                    "AS sql, octet_length(sql) AS sql_bytes FROM sqlite_schema "
                    "WHERE type = 'table' AND name = ?",
                    (_TRUSTED_RESULT_SCHEMA_SQL_MAX_BYTES, table_name),
                ),
            )
        )
        columns = tuple(result.columns)
        schema_sql = (
            schema.rows[0][0]
            if len(schema.rows) == 1 and len(schema.rows[0]) == 2
            else None
        )
        schema_bytes = (
            schema.rows[0][1]
            if len(schema.rows) == 1 and len(schema.rows[0]) == 2
            else None
        )
        if (
            type(schema_bytes) is not int
            or schema_bytes < 0
            or schema_bytes > _TRUSTED_RESULT_SCHEMA_SQL_MAX_BYTES
        ):
            raise TrustedUnsupportedQcodesSchemaError(
                "A QCoDeS result-table schema is too large for the bounded "
                "trusted metadata plan."
            )
        if (
            "id" not in columns
            or not isinstance(schema_sql, str)
            or _QCODES_RESULT_ID_SCHEMA.search(schema_sql) is None
        ):
            raise TrustedUnsupportedQcodesSchemaError(
                "A QCoDeS result table does not expose its required integer "
                "primary-key id column."
            )
        self._result_columns[table_name] = columns
        return columns

    def _result_id_watermark(self, table_name: str) -> int:
        table = quote_sqlite_identifier(table_name)
        result = self._executor.query(
            f'SELECT COALESCE(MAX("id"), 0) AS result_count FROM {table}'
        )
        return _single_integer(result, "result-count watermark")


    def _bounded_setpoint_summaries(
        self,
        table_name: str,
        parameters: tuple[str, ...],
        through_result_id: int,
        planned_steps: dict[str, int],
    ) -> tuple[TrustedSetpointSummary, ...]:
        """Find independent parameter edges in grouped fixed-ID windows.

        Eight scalar subqueries share each supervisor transaction.  Each value
        is capped at 64 KiB and each subquery remains inside one 4,096-ID
        window, so the grouped result has a small fixed wire envelope while
        reducing the maximum large-run fanout from 2,048 to 256 transactions.
        """

        if through_result_id <= 0 or not parameters:
            return ()
        table = quote_sqlite_identifier(table_name)
        edges: dict[bool, dict[str, PrimitiveScalar]] = {}
        for ascending in (True, False):
            found: dict[str, PrimitiveScalar] = {}
            ordering = "ASC" if ascending else "DESC"
            for page_index in range(_TRUSTED_SETPOINT_EDGE_MAX_PAGES):
                if ascending:
                    lower = page_index * _TRUSTED_SETPOINT_EDGE_PAGE_ROWS
                    if lower >= through_result_id:
                        break
                    upper = min(
                        through_result_id,
                        lower + _TRUSTED_SETPOINT_EDGE_PAGE_ROWS,
                    )
                else:
                    upper = through_result_id - (
                        page_index * _TRUSTED_SETPOINT_EDGE_PAGE_ROWS
                    )
                    if upper <= 0:
                        break
                    lower = max(0, upper - _TRUSTED_SETPOINT_EDGE_PAGE_ROWS)
                unresolved = tuple(name for name in parameters if name not in found)
                if not unresolved:
                    break
                for offset in range(
                    0, len(unresolved), _TRUSTED_SETPOINT_EDGE_GROUP_SIZE
                ):
                    group = unresolved[
                        offset : offset + _TRUSTED_SETPOINT_EDGE_GROUP_SIZE
                    ]
                    expressions: list[str] = []
                    bindings: list[int] = []
                    for index, name in enumerate(group):
                        column = quote_sqlite_identifier(name)
                        expressions.append(
                            "(SELECT "
                            f"{column} FROM {table} "
                            f'WHERE "id" > ? AND "id" <= ? '
                            f"AND {column} IS NOT NULL AND "
                            f"octet_length({column}) <= "
                            f"{_TRUSTED_SETPOINT_VALUE_MAX_BYTES} "
                            f'ORDER BY "id" {ordering} LIMIT 1) '
                            f'AS "value_{index}"'
                        )
                        bindings.extend((lower, upper))
                    result = self._executor.query(
                        f"SELECT {', '.join(expressions)}",
                        tuple(bindings),
                    )
                    if (
                        len(result.rows) != 1
                        or len(result.rows[0]) != len(group)
                        or len(result.columns) != len(group)
                    ):
                        raise TrustedMetadataQueryError(
                            "A grouped trusted setpoint-edge query returned an "
                            "invalid shape."
                        )
                    for name, value in zip(group, result.rows[0], strict=True):
                        if value is None:
                            continue
                        if not isinstance(value, (bool, int, float, str, bytes)):
                            raise TrustedMetadataQueryError(
                                "A bounded trusted setpoint-edge value was not "
                                "primitive."
                            )
                        found[name] = cast(PrimitiveScalar, value)
            edges[ascending] = found

        firsts = edges[True]
        lasts = edges[False]
        return tuple(
            TrustedSetpointSummary(
                name,
                firsts[name],
                lasts[name],
                planned_steps.get(name),
            )
            for name in parameters
            if name in firsts and name in lasts
        )


__all__ = [
    "FrozenFields",
    "PrimitiveScalar",
    "PrimitiveValue",
    "TRUSTED_DERIVED_ARRAY_ROWS_PER_WINDOW",
    "TRUSTED_DERIVED_MAX_ARRAY_BYTES",
    "TRUSTED_DERIVED_MAX_DEPENDENTS",
    "TRUSTED_DERIVED_MAX_GRID_SAMPLE_ROWS",
    "TRUSTED_DERIVED_MAX_SAMPLE_CELLS",
    "TRUSTED_DERIVED_MAX_SAMPLE_ROWS",
    "TRUSTED_DERIVED_MAX_SOURCE_COLUMNS",
    "TRUSTED_DERIVED_ROWS_PER_WINDOW",
    "TRUSTED_DERIVED_RUN_TEXT_MAX_BYTES",
    "TRUSTED_DERIVED_SAMPLE_WINDOWS",
    "TRUSTED_RUN_PAGE_SIZE",
    "TRUSTED_RUN_PAGE_SIZE_MAX",
    "Trusted2DDependentRowPattern",
    "Trusted2DGridLayout",
    "Trusted2DLayoutProof",
    "TrustedBootstrapResult",
    "TrustedDatabaseInfo",
    "TrustedDerivedSourceObservation",
    "TrustedMetadataQueryAdapter",
    "TrustedMetadataQueryError",
    "TrustedParameterView",
    "TrustedQueryExecutor",
    "TrustedRefreshResult",
    "TrustedRunPage",
    "TrustedRunRecord",
    "TrustedSelectedRunDetail",
    "TrustedSelectedRunPresentation",
    "TrustedSetpointSummary",
    "TrustedSnapshotView",
    "TrustedSourceRevision",
    "TrustedSourceRevisionNamespace",
    "TrustedUnsupportedQcodesSchemaError",
    "bounded_parameter_presentation",
    "bounded_parameter_views_from_run_metadata",
    "bounded_setpoint_presentation",
    "freeze_primitive_fields",
    "parameter_views_from_run_metadata",
    "quote_sqlite_identifier",
    "run_records_as_dict",
    "trusted_derived_source_revision",
    "trusted_source_revision",
]
