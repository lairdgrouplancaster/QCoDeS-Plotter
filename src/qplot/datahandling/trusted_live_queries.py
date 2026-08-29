"""Fixed trusted-reader query plans for current QCoDeS databases.

This module is deliberately Qt- and DB-API-independent.  It consumes only the
bounded primitive results exposed by :class:`TrustedLiveReaderSupervisor` (or
the Stage 4 broker facade with the same three query methods), and it never
materialises result-table rows.  Dynamic QCoDeS identifiers are learned with
zero-row ``SELECT`` statements and are quoted before they are used.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
from dataclasses import dataclass
from itertools import islice
from typing import Any, Protocol, TypeAlias, cast

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.readSQL import (
    materialize_run_basic_fields,
    materialize_run_observation,
)
from qplot.datahandling.trusted_live import (
    TRUSTED_LIVE_MAX_SCALAR_BYTES,
    SqliteBindings,
    TrustedQuery,
    TrustedQueryResult,
)
from qplot.datahandling.trusted_presentation import (
    TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES,
    TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_PARAMETER_TOTAL_TEXT_BYTES,
    TRUSTED_PRESENTATION_MAX_PARAMETERS,
    TrustedSelectedRunPresentation,
    bounded_presentation_names,
    bounded_presentation_scalar,
    bounded_presentation_text,
    bounded_selected_run_fields,
    build_selected_run_presentation,
)
from qplot.datahandling.trusted_snapshot import (
    TrustedSnapshotOmission,
    TrustedSnapshotView,
    normalize_trusted_snapshot,
)

TRUSTED_RUN_PAGE_SIZE = 1_000
TRUSTED_RUN_PAGE_SIZE_MAX = TRUSTED_RUN_PAGE_SIZE

# Whole-table aggregates are useful for recovering shapes and exact distinct
# setpoint counts from small legacy-style acquisitions.  They are deliberately
# disabled once either bound is exceeded: a row limit alone is not sufficient
# when a result row may contain a large array/blob, and a file-size limit alone
# is not sufficient for a very high row count.  Large tables use only the
# integer-primary-key fast path and fixed-size edge windows below.
_TRUSTED_AGGREGATE_MAX_ROWS = 100_000
_TRUSTED_AGGREGATE_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_TRUSTED_AGGREGATE_BATCH_SIZE = 4
_TRUSTED_SETPOINT_EDGE_PAGE_ROWS = 4_096
_TRUSTED_SETPOINT_EDGE_MAX_PAGES = 32
_TRUSTED_SETPOINT_EDGE_GROUP_SIZE = 8
_TRUSTED_SETPOINT_VALUE_MAX_BYTES = 64 * 1024
_TRUSTED_SETPOINT_SUMMARY_MAX_PARAMETERS = 32
_TRUSTED_OBSERVATION_MAX_SETPOINTS = 32
_TRUSTED_BASIC_TEXT_MAX_BYTES = 512
_TRUSTED_SINGLE_RUN_PREFLIGHT_BATCH_SIZE = 4
_TRUSTED_SINGLE_RUN_GROUP_MAX_COLUMNS = 32
_TRUSTED_SINGLE_RUN_GROUP_MAX_RAW_BYTES = TRUSTED_LIVE_MAX_SCALAR_BYTES
_TRUSTED_SINGLE_RUN_GROUP_MAX_SHARED_VALUE_BYTES = 128 * 1024
_TRUSTED_SINGLE_RUN_MAX_PUBLIC_RAW_BYTES = TRUSTED_LIVE_MAX_SCALAR_BYTES
_TRUSTED_LAYOUT_TEXT_MAX_BYTES = 512
_TRUSTED_LAYOUT_MAX_ROWS = 4_096
_TRUSTED_RESULT_SCHEMA_SQL_MAX_BYTES = 64 * 1024
TRUSTED_DERIVED_MAX_SOURCE_COLUMNS = 32
TRUSTED_DERIVED_MAX_DEPENDENTS = 8
TRUSTED_DERIVED_SAMPLE_WINDOWS = 15
TRUSTED_DERIVED_ROWS_PER_WINDOW = 256
TRUSTED_DERIVED_MAX_SAMPLE_ROWS = (
    TRUSTED_DERIVED_SAMPLE_WINDOWS + 1
) * TRUSTED_DERIVED_ROWS_PER_WINDOW
TRUSTED_DERIVED_MAX_SAMPLE_CELLS = TRUSTED_DERIVED_MAX_SAMPLE_ROWS * (
    TRUSTED_DERIVED_MAX_SOURCE_COLUMNS + 1
)
TRUSTED_DERIVED_RUN_TEXT_MAX_BYTES = 256 * 1024
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

PrimitiveScalar: TypeAlias = None | bool | int | float | str | bytes
PrimitiveValue: TypeAlias = PrimitiveScalar | tuple["PrimitiveValue", ...]
FrozenFields: TypeAlias = tuple[tuple[str, PrimitiveValue], ...]


class TrustedQueryExecutor(Protocol):
    """The narrow supervisor surface used by trusted metadata plans."""

    @property
    def incarnation(self) -> int: ...

    def query(
        self,
        sql: str,
        bindings: SqliteBindings = None,
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> TrustedQueryResult: ...

    def query_batch(
        self,
        queries: tuple[TrustedQuery, ...],
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> tuple[TrustedQueryResult, ...]: ...

    def data_version(
        self,
        *,
        timeout: float | None = None,
        wait_timeout: float | None = None,
    ) -> int: ...


class TrustedMetadataQueryError(RuntimeError):
    """Raised when current-schema metadata cannot be safely materialised."""


class TrustedUnsupportedQcodesSchemaError(TrustedMetadataQueryError):
    """The selected database is not the current QCoDeS schema qPlot supports."""


@dataclass(frozen=True, slots=True)
class TrustedRunRecord:
    """One immutable run dictionary represented by primitive field pairs."""

    run_id: int
    fields: FrozenFields
    unavailable_fields: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {name: _thaw_value(value) for name, value in self.fields}


@dataclass(frozen=True, slots=True)
class TrustedSourceRevision:
    """Opaque, query-layer-owned fingerprint for one run's source data."""

    fingerprint: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.fingerprint, bytes) or not self.fingerprint:
            raise ValueError("A source revision must be non-empty bytes.")


@dataclass(frozen=True, slots=True)
class TrustedSourceRevisionNamespace:
    """Restart-unique service namespace for conservative derived identities.

    Stage 5A deliberately makes this namespace ephemeral.  A trusted-live
    service creates one namespace for its lifetime and supplies the current
    helper incarnation separately when deriving a revision.  Persisting this
    value, or treating revisions as reusable across qPlot sessions, is not
    supported until a durable QCoDeS-data fingerprint is proven.
    """

    nonce: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.nonce, bytes) or not self.nonce:
            raise ValueError("A source-revision namespace must be non-empty bytes.")

    @classmethod
    def create(cls) -> TrustedSourceRevisionNamespace:
        """Create a new unpredictable namespace without filesystem access."""

        return cls(secrets.token_bytes(32))


def trusted_source_revision(
    run: TrustedRunRecord,
    data_version: int,
    *,
    namespace: TrustedSourceRevisionNamespace,
    helper_incarnation: int,
) -> TrustedSourceRevision:
    """Fingerprint bounded run facts in an ephemeral reader incarnation.

    SQLite's connection-local ``data_version`` is useful only within the same
    helper incarnation.  The service namespace and helper incarnation prevent
    a repeated value after helper or qPlot restart from aliasing prior work.
    The exact database-instance namespace is carried separately by the cache
    key.  This policy intentionally provides no persistent cross-session hit.
    """

    if type(data_version) is not int or data_version < 0:
        raise ValueError("data_version must be a non-negative integer.")
    if not isinstance(namespace, TrustedSourceRevisionNamespace):
        raise TypeError("namespace must be a TrustedSourceRevisionNamespace.")
    if type(helper_incarnation) is not int or helper_incarnation < 0:
        raise ValueError("helper_incarnation must be a non-negative integer.")
    payload = repr(
        (
            "qplot-trusted-source-revision-v2",
            namespace.nonce,
            helper_incarnation,
            data_version,
            run.run_id,
            run.fields,
        )
    ).encode(
        "utf-8",
        errors="surrogatepass",
    )
    return TrustedSourceRevision(hashlib.sha256(payload).digest())


@dataclass(frozen=True, slots=True)
class TrustedBootstrapResult:
    """Bounded bootstrap header captured before any run page is queried."""

    run_id_watermark: int
    data_version: int
    helper_incarnation: int


@dataclass(frozen=True, slots=True)
class TrustedRefreshResult:
    """Bounded incremental header; rows are fetched by explicit page requests."""

    prior_run_id_watermark: int
    run_id_watermark: int
    data_version: int
    data_version_changed: bool
    helper_incarnation: int


@dataclass(frozen=True, slots=True)
class TrustedRunPage:
    """At most one configured page of monotonic basic run records."""

    runs: tuple[TrustedRunRecord, ...]
    after_run_id: int
    through_run_id: int
    next_run_id: int
    complete: bool


@dataclass(frozen=True, slots=True)
class TrustedParameterView:
    name: str
    label: str
    unit: str
    depends_on: tuple[str, ...]
    paramtype: str


@dataclass(frozen=True, slots=True)
class TrustedSetpointSummary:
    name: str
    first: PrimitiveScalar
    last: PrimitiveScalar
    steps: int | None


@dataclass(frozen=True, slots=True)
class _SingleRunColumnPreflight:
    name: str
    value_type: str
    value_bytes: int | None


@dataclass(frozen=True, slots=True)
class _SingleRunValues:
    fields: dict[str, Any]
    unavailable_fields: tuple[str, ...]
    accepted_raw_bytes: int
    snapshot_omission: TrustedSnapshotOmission | None = None


@dataclass(frozen=True, slots=True)
class TrustedSelectedRunDetail:
    """Plain selected-run view data; never a fake QCoDeS DataSet."""

    run: TrustedRunRecord
    parameters: tuple[TrustedParameterView, ...]
    metadata: FrozenFields
    snapshot: TrustedSnapshotView
    setpoint_summaries: tuple[TrustedSetpointSummary, ...]
    presentation: TrustedSelectedRunPresentation
    unavailable_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TrustedDerivedSourceObservation:
    """One bounded, immutable QCoDeS result-prefix observation.

    ``sample_rows`` always begins with the integer result ``id``.  The schema,
    run identity, result watermark, and every sampled row are captured by one
    repeatable-read ``query_batch`` transaction.  No cursor or database object
    escapes the trusted helper.
    """

    format_version: int
    database_instance: DatabaseInstance
    run_id: int
    run_guid: str
    service_namespace: bytes
    helper_incarnation: int
    data_version: int
    result_table_name: str
    result_columns: tuple[str, ...]
    result_schema_sha256: bytes
    result_watermark: int
    parameters: tuple[TrustedParameterView, ...]
    dependent_parameters: tuple[str, ...]
    planned_shape: tuple[int, ...] | None
    sample_columns: tuple[str, ...]
    sample_rows: tuple[tuple[PrimitiveScalar, ...], ...]
    unsupported_reason: str | None = None
    run_fields: FrozenFields = ()
    setpoint_summaries: tuple[TrustedSetpointSummary, ...] = ()

    def __post_init__(self) -> None:
        if self.format_version != 1:
            raise ValueError("Unsupported trusted derived observation version.")
        if self.database_instance.identity is None:
            raise ValueError("A derived observation requires an exact database.")
        if type(self.run_id) is not int or self.run_id <= 0:
            raise ValueError("A derived observation requires a positive run id.")
        if not self.run_guid or not self.service_namespace:
            raise ValueError("A derived observation requires source namespaces.")
        if self.result_watermark < 0:
            raise ValueError("A result watermark cannot be negative.")
        if len(self.sample_columns) > TRUSTED_DERIVED_MAX_SOURCE_COLUMNS + 1:
            raise ValueError("A derived observation contains too many columns.")
        if len(self.sample_rows) > TRUSTED_DERIVED_MAX_SAMPLE_ROWS:
            raise ValueError("A derived observation contains too many rows.")
        if sum(len(row) for row in self.sample_rows) > TRUSTED_DERIVED_MAX_SAMPLE_CELLS:
            raise ValueError("A derived observation contains too many cells.")


def trusted_derived_source_revision(
    observation: TrustedDerivedSourceObservation,
) -> TrustedSourceRevision:
    """Fingerprint exactly the immutable source prefix carried by an observation."""

    if not isinstance(observation, TrustedDerivedSourceObservation):
        raise TypeError("observation must be TrustedDerivedSourceObservation.")
    payload = repr(
        (
            "qplot-trusted-derived-source-v1",
            observation.database_instance.logical_path,
            observation.database_instance.resolved_path,
            observation.database_instance.identity,
            observation.run_id,
            observation.run_guid,
            observation.service_namespace,
            observation.helper_incarnation,
            observation.data_version,
            observation.result_table_name,
            observation.result_columns,
            observation.result_schema_sha256,
            observation.result_watermark,
            observation.parameters,
            observation.dependent_parameters,
            observation.planned_shape,
            observation.sample_columns,
            observation.sample_rows,
            observation.setpoint_summaries,
            observation.unsupported_reason,
            observation.run_fields,
        )
    ).encode("utf-8", errors="surrogatepass")
    return TrustedSourceRevision(hashlib.sha256(payload).digest())


def bounded_parameter_presentation(
    parameters: tuple[TrustedParameterView, ...],
) -> tuple[tuple[TrustedParameterView, ...], bool]:
    truncated = len(parameters) > TRUSTED_PRESENTATION_MAX_PARAMETERS
    output: list[TrustedParameterView] = []
    retained_text_bytes = 0
    for parameter in parameters[:TRUSTED_PRESENTATION_MAX_PARAMETERS]:
        name, name_truncated = bounded_presentation_text(parameter.name)
        label, label_truncated = bounded_presentation_text(parameter.label)
        unit, unit_truncated = bounded_presentation_text(parameter.unit)
        paramtype, type_truncated = bounded_presentation_text(parameter.paramtype)
        dependencies = parameter.depends_on[
            :TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES
        ]
        bounded_dependencies: list[str] = []
        dependency_truncated = (
            len(parameter.depends_on) > TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES
        )
        for dependency in dependencies:
            bounded, was_truncated = bounded_presentation_text(dependency)
            bounded_dependencies.append(bounded)
            dependency_truncated = dependency_truncated or was_truncated
        bounded_parameter = TrustedParameterView(
            name,
            label,
            unit,
            tuple(bounded_dependencies),
            paramtype,
        )
        parameter_text_bytes = sum(
            len(value.encode("utf-8"))
            for value in (
                bounded_parameter.name,
                bounded_parameter.label,
                bounded_parameter.unit,
                bounded_parameter.paramtype,
                *bounded_parameter.depends_on,
            )
        )
        if (
            retained_text_bytes + parameter_text_bytes
            > TRUSTED_PRESENTATION_MAX_PARAMETER_TOTAL_TEXT_BYTES
        ):
            truncated = True
            break
        output.append(bounded_parameter)
        retained_text_bytes += parameter_text_bytes
        truncated = truncated or any(
            (
                name_truncated,
                label_truncated,
                unit_truncated,
                type_truncated,
                dependency_truncated,
            )
        )
    return tuple(output), truncated


def bounded_setpoint_presentation(
    summaries: tuple[TrustedSetpointSummary, ...],
) -> tuple[tuple[TrustedSetpointSummary, ...], bool]:
    output: list[TrustedSetpointSummary] = []
    truncated = False
    for summary in summaries:
        name, name_truncated = bounded_presentation_text(summary.name)
        first, first_truncated = bounded_presentation_scalar(summary.first)
        last, last_truncated = bounded_presentation_scalar(summary.last)
        output.append(
            TrustedSetpointSummary(
                name,
                cast(PrimitiveScalar, first),
                cast(PrimitiveScalar, last),
                summary.steps,
            )
        )
        truncated = truncated or any((name_truncated, first_truncated, last_truncated))
    return tuple(output), truncated


def quote_sqlite_identifier(identifier: object) -> str:
    """Quote one database-derived SQLite identifier without interpretation."""

    if not isinstance(identifier, str) or not identifier or "\x00" in identifier:
        raise TrustedMetadataQueryError(
            "A database-derived table or column identifier is invalid."
        )
    return '"' + identifier.replace('"', '""') + '"'


def run_records_as_dict(
    records: tuple[TrustedRunRecord, ...],
) -> dict[int, dict[str, Any]]:
    """Thaw immutable service records for the existing Qt run-list widgets."""

    return {record.run_id: record.as_dict() for record in records}


def _freeze_value(value: Any) -> PrimitiveValue:
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return cast(PrimitiveScalar, value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    raise TrustedMetadataQueryError(
        f"A trusted metadata field was not primitive ({type(value).__name__})."
    )


def _thaw_value(value: PrimitiveValue) -> Any:
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


def _freeze_fields(fields: dict[str, Any]) -> FrozenFields:
    return tuple((str(name), _freeze_value(value)) for name, value in fields.items())


def _bounded_public_run_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Discard raw SQL payloads and cap every field retained past a query plan."""

    bounded = dict(bounded_selected_run_fields(fields))
    if _RAW_RUN_FIELDS.intersection(bounded):
        raise TrustedMetadataQueryError(
            "A raw trusted run field escaped presentation normalization."
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


def freeze_primitive_fields(fields: dict[str, Any]) -> FrozenFields:
    """Freeze plain selected-run fields for either accepted read mode."""
    return _freeze_fields(fields)


def parameter_views_from_run_metadata(
    metadata: dict[str, Any],
    layout_rows: tuple[tuple[Any, ...], ...] = (),
) -> tuple[TrustedParameterView, ...]:
    """Build immutable parameter views from bounded run/layout primitives."""
    result = TrustedQueryResult(
        ("layout_id", "parameter", "label", "unit", "inferred_from"),
        tuple(tuple(row) for row in layout_rows),
    )
    return TrustedMetadataQueryAdapter._parameter_views(dict(metadata), result)


def bounded_parameter_views_from_run_metadata(
    metadata: dict[str, Any],
    layout_rows: tuple[tuple[Any, ...], ...] = (),
) -> tuple[tuple[TrustedParameterView, ...], bool]:
    """Build parameter rows and report every source-presentation truncation."""

    result = TrustedQueryResult(
        ("layout_id", "parameter", "label", "unit", "inferred_from"),
        tuple(tuple(row) for row in layout_rows),
    )
    return TrustedMetadataQueryAdapter._parameter_views_bounded(
        dict(metadata),
        result,
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


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _bounded_parameter_view_identifier(value: object) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", value is not None
    return bounded_presentation_text(
        value,
        limit=TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
    )


def _bounded_parameter_view_text(
    value: object,
    *,
    fallback: str = "",
) -> tuple[str, bool]:
    if value is None or value == "":
        value = fallback
    if isinstance(value, str):
        return bounded_presentation_text(
            value,
            limit=TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
        )
    if isinstance(value, bytes):
        return f"[binary value omitted: {len(value)} bytes]", True
    if isinstance(value, (bool, int, float)):
        return bounded_presentation_text(
            str(value),
            limit=TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
        )
    return f"[{type(value).__name__} value unavailable]", True


def _positive_int(value: object) -> int | None:
    if type(value) is not int:
        return None
    return value if value > 0 else None


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
        self._setpoint_summaries_truncated: dict[int, bool] = {}
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
        self._setpoint_summaries_truncated.clear()
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
            self._setpoint_summaries_truncated.pop(record.run_id, None)
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
    ) -> tuple[str, ...]:
        """Merge unavailable names without ever growing retained adapter state."""

        merged = tuple(
            dict.fromkeys(
                (
                    *self._unavailable_run_fields.get(run_id, ()),
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
        observed = _materialize_refreshed_basic_fields(merged, latest)
        self._runs[run_id] = _bounded_public_run_fields(observed)
        unavailable_fields = self._cache_unavailable_fields(
            run_id,
            loaded.unavailable_fields,
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
        metadata transactions can read consistently.  Optional whole-prefix
        aggregates are used only for conservatively small sources.  Large
        sources retain planned shapes, use fixed-size ID windows for small
        first/last summaries, and receive an explicitly estimated storage size.
        """

        metadata = dict(self._require_cached_run(run_id))
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
            )
        table_name = self._result_table_name(metadata)
        result_columns = self._ensure_result_columns(table_name)
        measure_parameters = tuple(metadata.get("measure_parameters") or ())
        sweep_parameters = tuple(metadata.get("sweep_parameters") or ())
        run_description = _json_object(metadata.get("run_description"))
        dependency_sets = self._dependency_sets(
            run_description,
            measure_parameters,
            sweep_parameters,
            result_columns,
        )

        result_watermark = self._result_id_watermark(table_name)
        aggregate_prefix = self._bounded_aggregate_prefix(result_watermark)
        queries: list[TrustedQuery] = []
        observation_keys: list[tuple[tuple[str, ...], str | None]] = []
        if aggregate_prefix and self._needs_observed_setpoints(metadata):
            for setpoints, dependent in dependency_sets:
                queries.append(
                    self._setpoint_observation_query(
                        table_name,
                        setpoints,
                        dependent,
                        result_watermark,
                    )
                )
                observation_keys.append((setpoints, dependent))

        summary_names = tuple(
            name for name in sweep_parameters if name in result_columns
        )[:_TRUSTED_SETPOINT_SUMMARY_MAX_PARAMETERS]
        if aggregate_prefix:
            for name in summary_names:
                queries.append(
                    self._setpoint_summary_query(
                        table_name,
                        name,
                        result_watermark,
                    )
                )

        results = self._bounded_query_batches(tuple(queries))
        result_index = 0
        observations: list[tuple[tuple[str, ...], int, tuple[int, ...] | None]] = []
        for setpoints, _dependent in observation_keys:
            observation = results[result_index]
            result_index += 1
            if len(observation.rows) != 1:
                continue
            values = observation.rows[0]
            if len(values) != 1 + len(setpoints):
                continue
            count = _positive_int(values[0])
            axis_counts = tuple(
                count_value
                for value in values[1:]
                if (count_value := _positive_int(value)) is not None
            )
            shape = (
                axis_counts
                if count is not None
                and len(axis_counts) == len(setpoints)
                and math.prod(axis_counts) == count
                else None
            )
            if count is not None:
                observations.append((setpoints, count, shape))

        summaries: list[TrustedSetpointSummary] = []
        if aggregate_prefix:
            for name in summary_names:
                summary_result = results[result_index]
                result_index += 1
                summary = self._materialize_summary(name, summary_result)
                if summary is not None:
                    summaries.append(summary)
        else:
            planned_steps = self._planned_setpoint_steps(metadata, summary_names)
            summaries.extend(
                self._bounded_setpoint_summaries(
                    table_name,
                    summary_names,
                    result_watermark,
                    planned_steps,
                )
            )
        public_summaries, summaries_truncated = bounded_setpoint_presentation(
            tuple(summaries)
        )
        self._setpoint_summaries[run_id] = public_summaries
        self._setpoint_summaries_truncated[run_id] = summaries_truncated

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
        if not latest_metadata.get("point_shape") and observations:
            dependency_names = {names for names, _, _ in observations}
            shapes = [shape for _, _, shape in observations]
            if (
                len(dependency_names) == 1
                and shapes[0] is not None
                and all(shape == shapes[0] for shape in shapes)
            ):
                setpoint_shape = shapes[0]
            setpoint_count = read_setpoint_count

        storage_bytes = self._estimated_result_storage_bytes(
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
        table_name = self._result_table_name(cached)
        result_columns = self._ensure_result_columns(table_name)
        source_columns = tuple(name for name in result_columns if name != "id")
        retained_columns = source_columns[:TRUSTED_DERIVED_MAX_SOURCE_COLUMNS]
        table = quote_sqlite_identifier(table_name)

        numeric_expressions = tuple(
            "CASE WHEN typeof({column}) IN ('integer', 'real') "
            "THEN {column} ELSE NULL END AS {column}".format(
                column=quote_sqlite_identifier(name)
            )
            for name in retained_columns
        )
        selected = ", ".join(('"id"', *numeric_expressions))
        maximum_expression = f'(SELECT COALESCE(MAX("id"), 0) FROM {table})'
        sample_queries = [
            TrustedQuery(
                f"SELECT {selected} FROM {table} "
                f'WHERE "id" > MAX(0, (({maximum_expression} * ?) / '
                f"{TRUSTED_DERIVED_SAMPLE_WINDOWS}) - 1) "
                f'AND "id" <= {maximum_expression} '
                f'ORDER BY "id" LIMIT {TRUSTED_DERIVED_ROWS_PER_WINDOW}',
                (index,),
            )
            for index in range(TRUSTED_DERIVED_SAMPLE_WINDOWS)
        ]
        # Always retain the newest captured edge, even when the cached count
        # was stale before this transaction began.
        sample_queries.append(
            TrustedQuery(
                f"SELECT {selected} FROM {table} "
                f'WHERE "id" <= {maximum_expression} ORDER BY "id" DESC '
                f"LIMIT {TRUSTED_DERIVED_ROWS_PER_WINDOW}"
            )
        )

        incarnation = self._executor.incarnation
        data_version = self._executor.data_version()
        identity_sql = (
            'SELECT "run_id", '
            f'CASE WHEN octet_length("guid") <= {_TRUSTED_BASIC_TEXT_MAX_BYTES} '
            'THEN "guid" ELSE NULL END AS "guid", '
            f'CASE WHEN octet_length("result_table_name") '
            f'<= {_TRUSTED_BASIC_TEXT_MAX_BYTES} THEN "result_table_name" '
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
        latest_metadata = self._require_cached_run(run_id)
        if (
            latest_metadata.get("guid") != guid
            or latest_metadata.get("result_table_name") != table_name
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
        dependency_sets = self._dependency_sets(
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
        parameter_views, _parameter_truncated = self._parameter_views_bounded(
            materialized,
            TrustedQueryResult((), ()),
        )
        bounded_parameters, _presentation_truncated = bounded_parameter_presentation(
            parameter_views
        )
        raw_shape = materialized.get("point_shape") or materialized.get(
            "setpoint_shape"
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
        elif len(sweep_parameters) > 2:
            unsupported_reason = (
                "Results with more than two sweep dimensions are unsupported."
            )

        storage_bytes = self._estimated_result_storage_bytes(
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
        )

    def selected_run_detail(self, run_id: int) -> TrustedSelectedRunDetail:
        """Materialise overview/parameter/metadata/snapshot view primitives."""

        cached = self._require_cached_run(run_id)
        run_columns = self._ensure_current_schema()
        loaded = self._single_run_columns(run_id, run_columns)
        layout_watermark_result = self._executor.query(
            'SELECT COALESCE(MAX("layout_id"), 0) FROM "layouts" WHERE "run_id" = ?',
            (run_id,),
        )
        layout_watermark = _single_integer(
            layout_watermark_result,
            "selected-run layout watermark",
        )
        if loaded is None:
            raise TrustedMetadataQueryError(
                f"Run {run_id} disappeared during selected-detail loading."
            )
        layout_result, layout_unavailable = self._selected_layout_pages(
            run_id,
            layout_watermark,
            max_raw_bytes=max(
                0,
                _TRUSTED_SINGLE_RUN_MAX_PUBLIC_RAW_BYTES - loaded.accepted_raw_bytes,
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
        raw_parameters, source_parameters_truncated = self._parameter_views_bounded(
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
            loaded.unavailable_fields,
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
                "Status": snapshot.status,
                "Message": snapshot.message,
                "Input bytes": snapshot.input_bytes,
                "Rendered nodes": len(snapshot.nodes),
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
        width-derived per-value limit is also satisfied. Even worst-case JSON
        escaping therefore remains below the 32 MiB wire envelope.
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
        accepted_raw_bytes = 0
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
            if (
                item.value_bytes > TRUSTED_LIVE_MAX_SCALAR_BYTES
                or item.value_bytes
                > _TRUSTED_SINGLE_RUN_MAX_PUBLIC_RAW_BYTES - accepted_raw_bytes
            ):
                values[item.name] = None
                unavailable.append(item.name)
                if item.name == "snapshot":
                    snapshot_omission = TrustedSnapshotOmission(
                        (
                            "payload_limit"
                            if item.value_bytes > TRUSTED_LIVE_MAX_SCALAR_BYTES
                            else "detail_budget"
                        ),
                        item.value_bytes,
                    )
                continue
            if item.value_bytes > _TRUSTED_SINGLE_RUN_GROUP_MAX_SHARED_VALUE_BYTES:
                if not flush_group():
                    return None
                group.append(item)
                group_bytes = item.value_bytes
                accepted_raw_bytes += item.value_bytes
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
            accepted_raw_bytes += item.value_bytes
        if not flush_group():
            return None
        return _SingleRunValues(
            values,
            tuple(dict.fromkeys(unavailable)),
            accepted_raw_bytes,
            snapshot_omission,
        )

    def _materialize_basic_page(
        self,
        result: TrustedQueryResult,
    ) -> list[TrustedRunRecord]:
        try:
            modified = os.path.getmtime(self._database_path)
        except OSError:
            modified = None
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

    @staticmethod
    def _result_table_name(metadata: dict[str, Any]) -> str:
        table_name = metadata.get("result_table_name")
        if not isinstance(table_name, str) or not table_name:
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

    def _bounded_aggregate_prefix(self, result_watermark: int) -> bool:
        """Whether whole-prefix aggregates have a conservative physical bound."""

        if result_watermark > _TRUSTED_AGGREGATE_MAX_ROWS:
            return False
        observations: list[tuple[tuple[str, int, int, int, int, int] | None, ...]] = []
        for _ in range(2):
            artifacts: list[tuple[str, int, int, int, int, int] | None] = []
            total_size = 0
            for suffix in ("", "-wal", "-journal"):
                path = f"{self._database_path}{suffix}"
                try:
                    status = os.stat(path, follow_symlinks=False)
                except FileNotFoundError:
                    if not suffix:
                        return False
                    artifacts.append(None)
                    continue
                except OSError:
                    return False
                if not stat.S_ISREG(status.st_mode) or status.st_size < 0:
                    return False
                artifacts.append(
                    (
                        path,
                        int(status.st_dev),
                        int(status.st_ino),
                        status.st_size,
                        status.st_mtime_ns,
                        status.st_ctime_ns,
                    )
                )
                total_size += status.st_size
                if total_size > _TRUSTED_AGGREGATE_MAX_SOURCE_BYTES:
                    return False
            observations.append(tuple(artifacts))

        # A single main-then-WAL observation can undercount during a checkpoint
        # (small main observed before the transfer, truncated WAL afterwards).
        # Require two identical identity/size observations. A changing writer
        # simply takes the fixed-window large-source path; no sleep, hash, open,
        # or retry is performed here.
        return observations[0] == observations[1]

    @staticmethod
    def _needs_observed_setpoints(metadata: dict[str, Any]) -> bool:
        if not metadata.get("point_shape"):
            return True
        if not bool(metadata.get("is_completed")):
            return True
        return "KeyboardInterrupt" in str(metadata.get("measurement_exception") or "")

    @staticmethod
    def _dependency_sets(
        run_description: dict[str, Any],
        measure_parameters: tuple[str, ...],
        sweep_parameters: tuple[str, ...],
        result_columns: tuple[str, ...],
    ) -> tuple[tuple[tuple[str, ...], str | None], ...]:
        available = set(result_columns)
        dependencies = (
            run_description.get("interdependencies_", {}).get("dependencies", {})
            if isinstance(run_description.get("interdependencies_"), dict)
            else {}
        )
        planned: list[tuple[tuple[str, ...], str | None]] = []
        if isinstance(dependencies, dict):
            for dependent in measure_parameters:
                names = dependencies.get(dependent)
                if not isinstance(names, (list, tuple)) or not names:
                    continue
                raw_setpoints = tuple(
                    islice(names, _TRUSTED_OBSERVATION_MAX_SETPOINTS + 1)
                )
                if len(raw_setpoints) > _TRUSTED_OBSERVATION_MAX_SETPOINTS:
                    continue
                setpoints_list: list[str] = []
                invalid = False
                for raw_name in raw_setpoints:
                    if not isinstance(raw_name, str):
                        invalid = True
                        break
                    name, was_truncated = bounded_presentation_text(raw_name)
                    if was_truncated:
                        invalid = True
                        break
                    setpoints_list.append(name)
                if invalid:
                    continue
                setpoints = tuple(setpoints_list)
                if dependent in available and all(
                    name in available for name in setpoints
                ):
                    planned.append((setpoints, dependent))
        if (
            not planned
            and sweep_parameters
            and len(sweep_parameters) <= _TRUSTED_OBSERVATION_MAX_SETPOINTS
            and all(name in available for name in sweep_parameters)
        ):
            planned.append((sweep_parameters, None))
        return tuple(planned)

    @staticmethod
    def _setpoint_conditions(
        setpoints: tuple[str, ...],
        dependent: str | None,
    ) -> str:
        names = (*setpoints, *((dependent,) if dependent is not None else ()))
        return " AND ".join(
            f"{quote_sqlite_identifier(name)} IS NOT NULL" for name in names
        )

    def _setpoint_observation_query(
        self,
        table_name: str,
        setpoints: tuple[str, ...],
        dependent: str | None,
        through_result_id: int,
    ) -> TrustedQuery:
        table = quote_sqlite_identifier(table_name)
        columns = ", ".join(quote_sqlite_identifier(name) for name in setpoints)
        conditions = self._setpoint_conditions(setpoints, dependent)
        bounded_conditions = f'"id" <= ? AND {conditions}'
        distinct_counts = ", ".join(
            f"COUNT(DISTINCT {quote_sqlite_identifier(name)}) AS "
            f"{quote_sqlite_identifier(f'axis_{index}')}"
            for index, name in enumerate(setpoints)
        )
        return TrustedQuery(
            "SELECT "
            f"(SELECT COUNT(*) FROM (SELECT DISTINCT {columns} FROM {table} "
            f"WHERE {bounded_conditions})) AS tuple_count, {distinct_counts} "
            f"FROM {table} WHERE {bounded_conditions}",
            (through_result_id, through_result_id),
        )

    @staticmethod
    def _setpoint_summary_query(
        table_name: str,
        parameter: str,
        through_result_id: int,
    ) -> TrustedQuery:
        table = quote_sqlite_identifier(table_name)
        column = quote_sqlite_identifier(parameter)
        return TrustedQuery(
            "WITH distinct_values(value, first_rowid) AS ("
            f"SELECT {column}, MIN(rowid) FROM {table} "
            f'WHERE "id" <= ? AND {column} IS NOT NULL '
            f"AND octet_length({column}) <= {_TRUSTED_SETPOINT_VALUE_MAX_BYTES} "
            f"GROUP BY {column}) "
            "SELECT "
            "(SELECT value FROM distinct_values ORDER BY first_rowid ASC LIMIT 1), "
            "(SELECT value FROM distinct_values ORDER BY first_rowid DESC LIMIT 1), "
            "(SELECT COUNT(*) FROM distinct_values)",
            (through_result_id,),
        )

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

    @staticmethod
    def _planned_setpoint_steps(
        metadata: dict[str, Any],
        summary_names: tuple[str, ...],
    ) -> dict[str, int]:
        raw_shape = metadata.get("setpoint_shape") or metadata.get("point_shape")
        if not isinstance(raw_shape, (list, tuple)):
            return {}
        return {
            name: step
            for name, raw_step in zip(summary_names, raw_shape, strict=False)
            if (step := _positive_int(raw_step)) is not None
        }

    @staticmethod
    def _estimated_result_storage_bytes(
        result_count: int,
        result_columns: tuple[str, ...],
        run_description: dict[str, Any],
    ) -> int:
        interdependencies = run_description.get("interdependencies_")
        parameters = (
            interdependencies.get("parameters")
            if isinstance(interdependencies, dict)
            else None
        )
        if not isinstance(parameters, dict):
            parameters = {}

        # Mirror the snapshot reader's deliberately rough per-row estimate,
        # but derive types from the already-loaded run description instead of
        # issuing PRAGMA table_info.  Variable-width fields stay conservative
        # heuristics and the public flag always labels this value as estimated.
        row_bytes = len(result_columns) + 2
        for name in result_columns:
            specification = parameters.get(name)
            raw_parameter_type = (
                specification.get("type") if isinstance(specification, dict) else None
            )
            parameter_type = (
                raw_parameter_type.lower()
                if isinstance(raw_parameter_type, str)
                else ""
            )
            if name == "id" or parameter_type == "numeric":
                row_bytes += 8
            elif parameter_type == "complex":
                row_bytes += 16
            else:
                row_bytes += 32
        return max(0, result_count) * row_bytes

    @staticmethod
    def _materialize_summary(
        name: str,
        result: TrustedQueryResult,
    ) -> TrustedSetpointSummary | None:
        if len(result.rows) != 1 or len(result.rows[0]) != 3:
            return None
        first, last, raw_steps = result.rows[0]
        steps = _positive_int(raw_steps)
        if steps is None or first is None or last is None:
            return None
        if not isinstance(first, (bool, int, float, str, bytes)):
            return None
        if not isinstance(last, (bool, int, float, str, bytes)):
            return None
        return TrustedSetpointSummary(
            name,
            cast(PrimitiveScalar, first),
            cast(PrimitiveScalar, last),
            steps,
        )

    @staticmethod
    def _parameter_views(
        metadata: dict[str, Any],
        layout_result: TrustedQueryResult,
    ) -> tuple[TrustedParameterView, ...]:
        views, _truncated = TrustedMetadataQueryAdapter._parameter_views_bounded(
            metadata,
            layout_result,
        )
        return views

    @staticmethod
    def _parameter_views_bounded(
        metadata: dict[str, Any],
        layout_result: TrustedQueryResult,
    ) -> tuple[tuple[TrustedParameterView, ...], bool]:
        run_description = _json_object(metadata.get("run_description"))
        interdependencies = run_description.get("interdependencies_")
        if not isinstance(interdependencies, dict):
            interdependencies = {}
        raw_parameters = interdependencies.get("parameters")
        dependencies = interdependencies.get("dependencies")
        if not isinstance(raw_parameters, dict):
            raw_parameters = {}
        if not isinstance(dependencies, dict):
            dependencies = {}

        truncated = False
        layouts: dict[str, tuple[str, str]] = {}
        for index, row in enumerate(
            islice(layout_result.rows, TRUSTED_PRESENTATION_MAX_PARAMETERS + 1)
        ):
            if index >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
                truncated = True
                break
            if len(row) < 4 or not isinstance(row[1], str):
                truncated = True
                continue
            name, name_truncated = bounded_presentation_text(
                row[1],
                limit=TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
            )
            label, label_truncated = _bounded_parameter_view_text(row[2])
            unit, unit_truncated = _bounded_parameter_view_text(row[3])
            truncated = bool(
                truncated or name_truncated or label_truncated or unit_truncated
            )
            layouts.setdefault(name, (label, unit))

        ordered: list[tuple[str, dict[str, Any], object]] = []
        seen_names: set[str] = set()

        def add_parameter(raw_name: object, specification: object) -> None:
            nonlocal truncated
            if not isinstance(raw_name, str):
                truncated = truncated or raw_name is not None
                return
            name, name_truncated = bounded_presentation_text(
                raw_name,
                limit=TRUSTED_PRESENTATION_MAX_PARAMETER_TEXT_BYTES,
            )
            truncated = truncated or name_truncated
            if not name or name in seen_names:
                return
            if len(ordered) >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
                truncated = True
                return
            bounded_specification = (
                specification if isinstance(specification, dict) else {}
            )
            ordered.append((name, bounded_specification, dependencies.get(raw_name)))
            seen_names.add(name)

        for index, (raw_name, specification) in enumerate(
            islice(
                raw_parameters.items(),
                TRUSTED_PRESENTATION_MAX_PARAMETERS + 1,
            )
        ):
            if index >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
                truncated = True
                break
            add_parameter(raw_name, specification)

        for role_name in ("sweep_parameters", "measure_parameters"):
            names = metadata.get(role_name) or ()
            if not isinstance(names, (list, tuple)):
                truncated = truncated or bool(names)
                continue
            for index, raw_name in enumerate(
                islice(names, TRUSTED_PRESENTATION_MAX_PARAMETERS + 1)
            ):
                if index >= TRUSTED_PRESENTATION_MAX_PARAMETERS:
                    truncated = True
                    break
                specification = (
                    raw_parameters.get(raw_name) if isinstance(raw_name, str) else None
                )
                add_parameter(raw_name, specification)

        views: list[TrustedParameterView] = []
        for name, specification, raw_dependencies in ordered:
            layout_label, layout_unit = layouts.get(name, ("", ""))
            depends_on: list[str] = []
            dependency_seen: set[str] = set()
            if isinstance(raw_dependencies, (list, tuple)):
                for dependency_index, raw_dependency in enumerate(
                    islice(
                        raw_dependencies,
                        TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES + 1,
                    )
                ):
                    if (
                        dependency_index
                        >= TRUSTED_PRESENTATION_MAX_PARAMETER_DEPENDENCIES
                    ):
                        truncated = True
                        break
                    dependency, dependency_truncated = (
                        _bounded_parameter_view_identifier(raw_dependency)
                    )
                    truncated = truncated or dependency_truncated
                    if dependency and dependency not in dependency_seen:
                        depends_on.append(dependency)
                        dependency_seen.add(dependency)
            elif raw_dependencies:
                truncated = True

            label, label_truncated = _bounded_parameter_view_text(
                specification.get("label"),
                fallback=layout_label,
            )
            unit, unit_truncated = _bounded_parameter_view_text(
                specification.get("unit"),
                fallback=layout_unit,
            )
            paramtype, type_truncated = _bounded_parameter_view_text(
                specification.get("type"),
                fallback="numeric",
            )
            truncated = bool(
                truncated or label_truncated or unit_truncated or type_truncated
            )
            views.append(
                TrustedParameterView(
                    name=name,
                    label=label,
                    unit=unit,
                    depends_on=tuple(depends_on),
                    paramtype=paramtype,
                )
            )
        return tuple(views), truncated


__all__ = [
    "TRUSTED_RUN_PAGE_SIZE",
    "TRUSTED_RUN_PAGE_SIZE_MAX",
    "TrustedBootstrapResult",
    "TrustedMetadataQueryAdapter",
    "TrustedMetadataQueryError",
    "TrustedParameterView",
    "TrustedQueryExecutor",
    "TrustedRefreshResult",
    "TrustedRunRecord",
    "TrustedRunPage",
    "TrustedSelectedRunDetail",
    "TrustedSetpointSummary",
    "TrustedSelectedRunPresentation",
    "TrustedSnapshotView",
    "TrustedUnsupportedQcodesSchemaError",
    "bounded_parameter_presentation",
    "bounded_parameter_views_from_run_metadata",
    "bounded_setpoint_presentation",
    "freeze_primitive_fields",
    "parameter_views_from_run_metadata",
    "quote_sqlite_identifier",
    "run_records_as_dict",
]
