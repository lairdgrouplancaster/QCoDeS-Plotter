"""Immutable trusted-query results, budgets, and executor contract.

These values are shared by query plans, the broker, cache, rendering, and UI.
They do not import the metadata adapter or perform database I/O."""

from __future__ import annotations

import hashlib
import math
import secrets
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, cast

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_live import (
    SqliteBindings,
    TrustedQuery,
    TrustedQueryResult,
)
from qplot.datahandling.trusted_presentation import (
    TrustedSelectedRunPresentation,
)
from qplot.datahandling.trusted_snapshot import (
    TrustedSnapshotView,
)

TRUSTED_DERIVED_MAX_SOURCE_COLUMNS = 32
TRUSTED_DERIVED_MAX_DEPENDENTS = 8
TRUSTED_DERIVED_SAMPLE_WINDOWS = 15
TRUSTED_DERIVED_ROWS_PER_WINDOW = 256
TRUSTED_DERIVED_MAX_SAMPLE_ROWS = (
    TRUSTED_DERIVED_SAMPLE_WINDOWS + 1
) * TRUSTED_DERIVED_ROWS_PER_WINDOW
TRUSTED_DERIVED_MAX_GRID_SAMPLE_ROWS = TRUSTED_DERIVED_MAX_SAMPLE_ROWS - 1
TRUSTED_DERIVED_MAX_SAMPLE_CELLS = TRUSTED_DERIVED_MAX_SAMPLE_ROWS * (
    TRUSTED_DERIVED_MAX_SOURCE_COLUMNS + 1
)
TRUSTED_DERIVED_RUN_TEXT_MAX_BYTES = 256 * 1024
TRUSTED_DERIVED_ARRAY_ROWS_PER_WINDOW = 8
TRUSTED_DERIVED_MAX_ARRAY_BYTES = 1024 * 1024
# Current QCoDeS scalar complex NPY records fit in a header plus one sample.
TRUSTED_DERIVED_MAX_COMPLEX_CELL_BYTES = 256
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
class TrustedDatabaseInfo:
    """Current database facts from one bounded read-only transaction."""

    user_version: int
    application_id: int
    page_count: int
    page_size: int
    table_count: int
    experiment_count: int
    run_count: int
    latest_run: TrustedRunRecord | None


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
class Trusted2DDependentRowPattern:
    """Proven physical result-row progression for one 2D dependent."""

    dependent: str
    first_row_id: int
    fast_id_stride: int
    slow_id_stride: int

    def __post_init__(self) -> None:
        if not self.dependent:
            raise ValueError("A 2D row pattern requires a dependent name.")
        if type(self.first_row_id) is not int or self.first_row_id <= 0:
            raise ValueError("A 2D row pattern requires a positive first row id.")
        if (
            type(self.fast_id_stride) is not int
            or self.fast_id_stride <= 0
            or type(self.slow_id_stride) is not int
            or self.slow_id_stride <= 0
        ):
            raise ValueError("A 2D row pattern requires positive row-id strides.")


@dataclass(frozen=True, slots=True)
class Trusted2DLayoutProof:
    """Run-local proof shared by metadata and bounded derived extraction."""

    dependencies: tuple[str, str]
    shape: tuple[int, int]
    fast_axis_index: int
    dependent_rows: tuple[Trusted2DDependentRowPattern, ...]
    source: str

    def __post_init__(self) -> None:
        if (
            len(self.dependencies) != 2
            or len(set(self.dependencies)) != 2
            or any(not name for name in self.dependencies)
        ):
            raise ValueError("A 2D layout proof requires two dependency names.")
        if (
            len(self.shape) != 2
            or any(type(count) is not int or count <= 0 for count in self.shape)
            or math.prod(self.shape) > (1 << 63) - 1
        ):
            raise ValueError("A 2D layout proof requires a bounded positive shape.")
        if type(self.fast_axis_index) is not int or self.fast_axis_index not in (0, 1):
            raise ValueError("A 2D layout proof requires one fast dependency axis.")
        if (
            not self.dependent_rows
            or len(self.dependent_rows) > TRUSTED_DERIVED_MAX_DEPENDENTS
        ):
            raise ValueError("A 2D layout proof has an invalid dependent collection.")
        names = tuple(pattern.dependent for pattern in self.dependent_rows)
        if len(set(names)) != len(names):
            raise ValueError("A 2D layout proof repeats a dependent name.")
        if self.source not in {"planned", "observed"}:
            raise ValueError("A 2D layout proof has an invalid source.")
        slow_count = self.shape[1 - self.fast_axis_index]
        fast_count = self.shape[self.fast_axis_index]
        for pattern in self.dependent_rows:
            if (
                fast_count > 1
                and pattern.slow_id_stride <= (fast_count - 1) * pattern.fast_id_stride
            ):
                raise ValueError("A 2D row pattern overlaps adjacent slow rows.")
            last_row_id = (
                pattern.first_row_id
                + (slow_count - 1) * pattern.slow_id_stride
                + (fast_count - 1) * pattern.fast_id_stride
            )
            if last_row_id > (1 << 63) - 1:
                raise ValueError("A 2D row pattern exceeds SQLite integer ids.")


@dataclass(frozen=True, slots=True)
class Trusted2DGridLayout:
    """Validated sampled layout for one dependent image.

    Shape follows declared QCoDeS dependency order.  ``fast_axis_index`` records
    which dependency actually changes within a physical acquisition row; it is
    row-decoding metadata only.  Rendering retains the established convention
    that declared dependency 0 is vertical and dependency 1 is horizontal.
    Independent physical strides ensure sparse, interleaved, and
    dependent-blocked rows never inherit another dependent's samples.
    """

    dependent: str
    dependencies: tuple[str, str]
    shape: tuple[int, int]
    fast_axis_index: int
    first_row_id: int
    fast_id_stride: int
    slow_id_stride: int
    sample_slow_indexes: tuple[int, ...]
    sample_fast_indexes: tuple[int, ...]
    slow_reversed: bool
    fast_reversed: bool
    serpentine: bool
    complete: bool
    source: str

    def __post_init__(self) -> None:
        proof = Trusted2DLayoutProof(
            self.dependencies,
            self.shape,
            self.fast_axis_index,
            (
                Trusted2DDependentRowPattern(
                    self.dependent,
                    self.first_row_id,
                    self.fast_id_stride,
                    self.slow_id_stride,
                ),
            ),
            self.source,
        )
        del proof
        slow_count = self.shape[1 - self.fast_axis_index]
        fast_count = self.shape[self.fast_axis_index]
        if (
            not self.sample_slow_indexes
            or not self.sample_fast_indexes
            or tuple(sorted(set(self.sample_slow_indexes))) != self.sample_slow_indexes
            or tuple(sorted(set(self.sample_fast_indexes))) != self.sample_fast_indexes
            or any(
                type(index) is not int
                for index in (*self.sample_slow_indexes, *self.sample_fast_indexes)
            )
            or len(self.sample_slow_indexes) * len(self.sample_fast_indexes)
            > TRUSTED_DERIVED_MAX_GRID_SAMPLE_ROWS
            or self.sample_slow_indexes[0] < 0
            or self.sample_slow_indexes[-1] >= slow_count
            or self.sample_fast_indexes[0] < 0
            or self.sample_fast_indexes[-1] >= fast_count
        ):
            raise ValueError("A 2D sampled layout has invalid grid indexes.")
        if any(
            type(value) is not bool
            for value in (
                self.slow_reversed,
                self.fast_reversed,
                self.serpentine,
                self.complete,
            )
        ):
            raise ValueError("A 2D sampled layout has invalid flags.")


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
    validated_2d_layouts: tuple[Trusted2DGridLayout, ...] = ()
    progressive_layout_pending: bool = False
    progressive_layout_cursor: int = 0

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
        if type(self.progressive_layout_pending) is not bool:
            raise ValueError("A progressive-layout pending flag must be boolean.")
        if (
            type(self.progressive_layout_cursor) is not int
            or self.progressive_layout_cursor < 0
            or (self.progressive_layout_pending and self.progressive_layout_cursor <= 0)
            or (
                not self.progressive_layout_pending
                and self.progressive_layout_cursor != 0
            )
        ):
            raise ValueError("A derived observation has an invalid progress cursor.")
        if len(self.sample_columns) > TRUSTED_DERIVED_MAX_SOURCE_COLUMNS + 1:
            raise ValueError("A derived observation contains too many columns.")
        if len(self.sample_rows) > TRUSTED_DERIVED_MAX_SAMPLE_ROWS:
            raise ValueError("A derived observation contains too many rows.")
        if sum(len(row) for row in self.sample_rows) > TRUSTED_DERIVED_MAX_SAMPLE_CELLS:
            raise ValueError("A derived observation contains too many cells.")
        if (
            not isinstance(self.validated_2d_layouts, tuple)
            or len(self.validated_2d_layouts) > TRUSTED_DERIVED_MAX_DEPENDENTS
            or any(
                not isinstance(layout, Trusted2DGridLayout)
                for layout in self.validated_2d_layouts
            )
            or len({layout.dependent for layout in self.validated_2d_layouts})
            != len(self.validated_2d_layouts)
        ):
            raise ValueError("A derived observation has invalid 2D layouts.")


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
            observation.validated_2d_layouts,
            observation.progressive_layout_pending,
            observation.progressive_layout_cursor,
            observation.unsupported_reason,
            observation.run_fields,
        )
    ).encode("utf-8", errors="surrogatepass")
    return TrustedSourceRevision(hashlib.sha256(payload).digest())


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


def freeze_primitive_fields(fields: dict[str, Any]) -> FrozenFields:
    """Freeze plain selected-run fields for either accepted read mode."""
    return _freeze_fields(fields)
