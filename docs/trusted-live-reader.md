# Trusted live QCoDeS reader

`qplot.datahandling.trusted_live` is the completed Stage 2 reader boundary for
inspecting an actively written QCoDeS WAL database without copying it. Stage 3
adds `qplot.datahandling.trusted_live_supervisor`, an application-independent
process boundary around that reader. It sees the latest committed transaction,
including pages that exist only in the WAL, and never exposes an APSW connection
or cursor to its caller.

Stage 4 connects that boundary to qPlot through one Qt-independent application
read broker per accepted database instance. Basic run loading, incremental
refresh, progressive run metadata, and the selected run's bounded detail use the
broker's one persistent supervisor and helper. The detail populates the Raw
key-value, Metadata, and Snapshot views ephemerally for the exact current
selection; it does not create a `DataSet`, private snapshot, or derived cache
entry. The broker serialises finite jobs and keeps every blocking wait,
cancellation, close, and process join away from the GUI thread.

Stage 5B adds a non-UI derived-work backend around the Stage 5A scheduler. It
uses the same broker/helper to capture bounded immutable result prefixes,
produces primitive metadata and deterministic PNG payloads, and performs
verified disk-cache I/O on its sole worker. Stage 5C connects those payloads to
Qt through one GUI-owned bridge, progressively displaying derived metadata,
thumbnails, and previews without reviving the legacy competing producers.
Explicit plots now acquire their metadata and result prefixes through the same
broker/helper. CSV actions still acquire action-owned private snapshots, and a
narrowly eligible snapshot fallback session retains its existing behavior.

## Explicit plotting

Plot-all, Run, indexed measurement, preview, and overlay actions request a
connection-free `TrustedPlotDataset` asynchronously. A bounded Qt polling timer
only consumes completed metadata requests; metadata queries, description
decoding, prefix capture, and processing run outside the GUI thread. Selection
changes invalidate pending selection-based actions. No trusted plot action,
completion check, or refresh calls the whole-database snapshot-copy path.
An accepted trusted-session failure remains an error, including a later native
backend failure; the initial-open fallback eligibility rules are unchanged.

The broker captures GUID/table/description identity, completion, and indexed
`MAX(id)` in one transaction. Small numeric plots and array plots stage only
that run's result table through the watermark in a private temporary SQLite
file, with at most 8,192 physical rows
and 65,536 value/length cells per page. Pages and BLOB chunks use separate
transactions capped at five seconds; the complete capture has a one-hour
deadline and exact broker cancellation. Appends beyond the watermark appear on
the next refresh. Current QCoDeS's append-only result-row contract makes the
captured prefix stable between those transactions. Source identity is checked
by the pinned reader at every transaction boundary and by the existing plot
publication fences.
The overall wall-clock horizon includes time yielded to other plots; it does
not extend any reader transaction. Explicit caller deadlines retain their
existing shorter limits.

Large numeric heatmaps instead reduce the captured prefix directly through
bounded SQL queries. A paged preflight counts only until the full-resolution
limit is crossed; it is skipped when the indexed watermark already proves the
run small enough. Summary and aggregation queries cover at most 65,536 row IDs
per transaction and yield between intervals. Each aggregate reply holds at
most 32,769 groups; dense intervals are split before accepting their data.
The pinned reader has a bounded 16 MiB SQLite page cache so summary queries over
the same interval can reuse pages. No source transaction survives a yield.

These heatmaps use the existing spatial bin geometry and configured display
limits. Page sums and counts produce weighted means over every valid sample,
never means of page means or sampled preview images. Mixed-sign, large-integer,
and near-overflow bins replay original values with exact rational sums.
Distinct-axis sets hold at most 131,072 values each; saturated cardinalities
are reported as unknown, as for bounded array heatmaps. A saturated axis uses
the observed full bounds and a bounded cardinality estimate to place bins.
Full-resolution transforms above their configured limit are still rejected.
The numeric aggregate's allocation guard rejects grids above two million
cells before allocating its sums, counts, extrema, or precision-replay state.

Captures are resumable broker operations. Numeric pages and 256 KiB BLOB chunks
yield with no source transaction open. The broker groups inexpensive steps into
a 50 ms time slice before returning the operation to the end of its priority
queue; a slower bounded query ends its slice as soon as it returns. This keeps
small runs from paying for another large query between every tiny page, while
allowing other plots to progress without waiting for a whole large run. Higher-priority
metadata and existing background fairness still apply. Suspended captures hold
only private spool resources, never source transactions; cancellation, expiry,
and shutdown close those resources on the dispatcher thread. The existing
bounded request queue also bounds the number of suspended captures.
Capture slices run between broker jobs, rather than nesting between every
query of a metadata job and consuming its deadline. Cheap interactive metadata
can still preempt at those query boundaries. A background inference job cannot
therefore lose its entire budget to repeated plot-capture slices.

Plot windows submit capture requests without occupying a processing-pool
thread. A GUI timer checks completion and starts decoding only when the broker
result is ready. Duplicate default-deadline requests for the same dataset share
one capture with independent cancellation; explicit deadlines retain their
existing coalescing rules. A small plot can therefore display and refresh while
another capture is in progress, even with one plot-processing thread.

`qplot_read_blob(table, column, rowid, offset, length)` is a direct-only scalar
function registered on the pinned reader connection. It reads at most 256 KiB
through SQLite's incremental BLOB API with `writeable=False`, from `main` only,
and closes the handle before returning bytes. It shares the normal operation's
deadline, cancellation, native source proofs, result budgets, and rollback.
Uncertain BLOB close retires the reader/helper. This avoids materialising an
entire large NPY value merely to evaluate SQL `substr()`, which would exceed the
reader's SQLite length limit. It adds no source-writing capability or VFS
exception. The exact permitted SHM operations are unchanged.

Existing QCoDeS decoding, precision-preserving array handling, acquisition
masks, transforms, overlays, and array-heatmap aggregation operate on the
private prefix. Full-resolution decoding has a conservative 1 GiB input/object
and planned-shape budget; larger heatmaps retain their existing bounded display
path. This is a logical allocation budget, not a process RSS ceiling. Interactive
plots never use cached preview PNGs as data. Completion is published only after
the final acquired data successfully reaches the plot.

The broker retains at most eight completed results totalling 512 MiB of private
prefix storage or numeric heatmap arrays for reuse by sibling plots and detail
reloads, revalidating the source
and watermark before reuse. This temporary cache is separate from the derived
image cache. Database switching retains an old broker while its plots still
need it; closing their dataset handles retires that broker off the GUI thread.
Source replacement and application shutdown still invalidate/close it.

Capture cost and temporary disk space scale with the selected run, not the
database. Large numeric heatmaps avoid staging raw rows but still scan the
selected prefix to include all valid observations. Very large individual runs
can still be expensive or reach the finite capture deadline; live refresh
currently recaptures that run's prefix or numerical reduction. The disk
cache cap limits retained completed prefixes, not the active capture file.
CSV export is unchanged.

`scripts/benchmark_plot_launch.py DATABASE RUN_ID` measures the actual
`MainWindow.openPlot` path through populated Qt painting, rejects whole-database
copy attempts, and reports protected main/WAL/journal size and timestamp checks.
Use `--action double-click` or `--action run` to exercise the actual run-table
signal or Run button. In particular, Qt converts the table signal's `None` to
an empty string; both forms must pass through asynchronous metadata loading.
Use `--preceding-run 33 --max-threads 1` to measure opening the selected run while
run 33 is still being captured. The report includes the preceding capture's
progress and pending state; that capture is cancelled after the measured plots
display, so this does not measure run 33's total plotting time.

## Source-file boundary

The reader uses pinned APSW 3.53.4.0 and its SQLite 3.53.4 build. A small native
VFS shim is loaded into that SQLite instance and delegates locking and WAL
coordination to the platform's base VFS. SQLite therefore uses the real,
colocated WAL index and the writer's native operating-system lock domain; the
reader does not use `immutable=1`, `nolock`, raw WAL parsing, a copied snapshot,
or a private SHM mirror.

The operating-system handles used for the main database and WAL are physically
read-only and non-creating. The same protection applies to any rollback journal:
qPlot never creates, modifies, truncates, renames, deletes, or recovers it. The
native boundary rejects source writes, truncation, deletion, writable mapping,
mutating file controls, journal creation, and any fallback to a read/write
connection. The SQL layer additionally enables `query_only`, defensive mode and
`trusted_schema=OFF`, disables checkpoint-on-close, keeps temporary state in
memory or a private temporary directory, and uses a deny-by-default authorizer.
Checkpoints, journal-mode changes, migrations, `VACUUM`, `ATTACH`, and other
mutating SQL are rejected.

qPlot does not checkpoint the selected database and does not write experimental
data. The main database, `-wal`, and `-journal` remain read-only for the lifetime
of the reader.

SQLite normally asks a WAL VFS to open `-wal` with create permission even when
the connection itself is read-only. If the real WAL is initially absent, the
native boundary instead creates a zero-length placeholder in qPlot's private
temporary directory and gives SQLite a read-only handle to that private file.
Immediately before each finite transaction it checks the real WAL pathname. If
a writer has created the WAL, the same native file object is promoted to a
proved, physically read-only handle for that exact WAL before the transaction
can begin. The real `-wal` is never created by qPlot, and the placeholder is not
a database or WAL copy.

## Sole SHM exception

`<database>-shm` is transient SQLite coordination state, not experimental data.
It is the only source-side file the trusted reader permits SQLite to mutate. For
that exact regular, colocated path, the pinned SQLite VFS may:

- create or open the file when a WAL transaction needs it;
- read and write WAL-index structures and reader marks;
- extend, resize, or truncate it as SQLite requires;
- create shared read/write mappings;
- take and release SQLite SHM locks and issue memory barriers; and
- recover or reconstruct WAL-index headers and hashes.

Consequently, reading a live database may change the SHM file's contents, size,
timestamps, permission mode, or other metadata. The pinned SQLite Unix VFS may,
for example, use `fchmod()` to normalise the exact SHM file's mode while opening
it. Those changes are expected and are not treated as source replacement. The
SHM pathname and filesystem object must still match the selected database
family. The native VFS never extends this permission to the main database, WAL,
rollback journal, or an arbitrary path.

Application code never writes or changes the SHM directly. Any permission-mode
normalisation is performed only inside the pinned SQLite VFS. Elevated POSIX
execution is rejected, preventing SQLite's root-only ownership normalisation.
Elevated Windows execution is rejected as well, so every trusted live session
runs in an unprivileged process.
qPlot does not rename or delete the SHM, and native `xShmUnmap` delegation
always uses `deleteFlag=0`, so closing the trusted reader does not request SHM
deletion. A writer or another ordinary SQLite owner may independently manage
its sidecars.

## File identity and accepted sources

The reader binds the expected source identity, a retained proof handle, and the
actual handle used by SQLite. Repeated pathname `stat()` results are not used as
a substitute for that proof:

- On POSIX, device and inode values come from `fstat()` on the retained proof
  descriptor and SQLite's real descriptor.
- On Windows, volume identity and file ID come from the retained proof handle
  and SQLite's real operating-system handle.

The binding is checked at open and transaction boundaries. A sidecar may
legitimately appear between operations, but an operation never silently changes
to another inode or file ID. Replacing the main database, WAL, or SHM invalidates
the reader and requires an explicit reopen. SHM content, size, `mtime`, and
`ctime` changes alone do not invalidate it.

Only regular files with correctly named, colocated sidecars on supported
same-host local filesystems are accepted. Symbolic links, Windows reparse-point
escapes or alternate data streams, non-regular files, multiply linked SHM
objects, malformed or non-colocated sidecars, UNC/network paths, unsupported
platforms or filesystems, incompatible APSW/SQLite builds, and elevated POSIX
root or Windows execution fail closed before a trusted session is established.

The current filesystem allowlist is deliberately explicit: local APFS and HFS
on macOS; NTFS on Windows; and ext2/3/4, XFS, btrfs, tmpfs, ramfs, overlayfs,
and OpenZFS on Linux. A filesystem being mounted locally is not by itself
sufficient. Expanding this list requires platform-specific evidence for file
identity, shared mapping, and SQLite locking semantics.

## Spawned supervisor boundary

`TrustedLiveReaderSupervisor.open()` owns one persistent helper process for one
database instance and supports context-manager cleanup. Its small non-Qt API
opens an optionally expected `DatabaseInstance`, runs individual and batch
queries, reads `data_version`, waits for bounded results, cancels the active job,
and closes the session. Only one database job may be active in a helper at a
time.

The supervisor explicitly obtains the multiprocessing `spawn` context on every
platform. Its child target is a top-level function in the installed package, so
Windows never depends on fork state or a source checkout. The helper constructs,
queries, rolls back, and closes `TrustedLiveReader` on its main thread. A control
thread may set cancellation state and invoke the reader's cross-thread-safe
`interrupt()` method, but it never queries or closes the reader.

IPC is explicitly versioned and bounded. Every request and reply carries the
protocol version, a process/session incarnation, a monotonically increasing job
generation, an operation type, and a bounded payload. Message framing, nesting,
collection lengths, SQL length, binding counts, batch size, and reply size are
checked before use. Unknown versions, malformed or oversized messages, stale or
wrong-session job/reply generations, duplicates, and out-of-order application
traffic fail closed. The deliberately narrow cancellation race rule is
described below.

Generic JSON decoding rejects duplicate object keys, more than 4,500,000
aggregate collection items, and every untagged non-finite number. The latter
includes exponent overflow such as `1e9999`, which Python's JSON decoder would
otherwise produce as infinity without treating it as a non-standard JSON
constant. SQLite real values use a separate tagged canonical `float.hex()` text
representation, so supported finite and non-finite real round trips never rely
on an untagged JSON number.

All application startup configuration—the selected path, expected
`DatabaseInstance`, first job generation, and finite timeouts—is encoded in a
session-bound, generation-zero `startup` frame. The unavoidable multiprocessing
bootstrap arguments are limited to pipe handles, that bytes frame, and fixed
private test plumbing. Conservative incremental wire budgets account for
aggregate text, blob/base64, container, and envelope size before constructing
amplified payloads; the exact encoded frame cap is then checked as a final bound.
Raw single-query bindings and each batch specification are count-bounded before
materialisation; mutable blob bindings are snapshotted under the batch's shared
request budget before a job is published. The parent records submitted
query-batch cardinality and accepts a success only
when it contains exactly one result for every statement.

Result limits are also enforced while the Stage 2 reader advances the live
SQLite cursor, rather than only after a complete result has been materialised.
It validates column count and names before fetching rows and, before retaining
each next row, checks the per-result row count, the reply-wide cell count, every
scalar, and that row's conservative encoded-wire contribution. One aggregate
cell-count budget and one 32 MiB wire budget are shared by every statement in a
`query_batch()`.

The live cursor has a separate pre-yield row-materialisation bound. At the start
of an operation, the reader installs and verifies an operation-wide
`SQLITE_LIMIT_LENGTH` baseline equal to the smaller of SQLite's prior value and
4 MiB. After `apsw.ext.query_info()` supplies a statement's result width, and
before `cursor.execute()` can produce a row, the reader installs and verifies
this width-dependent per-value limit, where `w = max(1, result columns)`:

```text
min(
    operation baseline,
    floor(8 MiB / w),
    floor((32 MiB - 4096 - 512*w) / (4*w)),
)
```

Thus aggregate SQLite text/blob payload in one APSW row cannot exceed 8 MiB.
The last term separately defines a 33,554,432-byte conservative *logical*
Python-object/payload envelope for the ordinary APSW tuple and its standard
SQLite scalar objects. It charges four bytes for every permitted input byte to
cover worst-case UTF-8-to-PEP-393 Unicode payload expansion, 512 bytes per
column for scalar headers, terminators, tuple references, and fixed-size
integer/float/NULL objects on supported 64-bit CPython, and 4,096 bytes for the
tuple's logical object size. In tests, the corresponding observed logical size
is `sys.getsizeof(row) + sum(sys.getsizeof(value) for value in row)`. The owned
APSW connection requires its standard tuple/scalar conversion; row traces and
JSONB conversion are disabled and verified before execution.

The logical envelope is not an exact allocator or physical-memory ceiling.
Allocator size-class rounding and reserved bytes, process RSS, arenas,
fragmentation, and SQLite virtual-machine or intermediate allocations are
outside it and may be larger. No result-bound claim is made about those values.

A one-column blob of exactly 4 MiB remains valid. Four MiB is the absolute
single-scalar ceiling, however, not an allowance for every value in a wide row;
the effective limit decreases with result width. SQLite also applies
`SQLITE_LIMIT_LENGTH` to encoded records and some intermediate values, so a
materialised CTE or sorter can be rejected earlier than these payload bounds.
Such a failure is still reported distinctly as `TrustedLiveResultLimitError`.
These limits bound the raw payload and conservative logical size of the
ordinary row APSW can return, not allocator-reserved or arbitrary SQLite
virtual-machine working memory or the cumulative retained reply, which has its
own budgets.

After the cursor closes on every success, exception, cancellation, or timeout,
the per-statement limit is restored to and verified against the operation
baseline before another batch statement is inspected. Each statement therefore
recomputes its limit independently. Final operation cleanup restores and
verifies SQLite's original connection limit. Installation, verification, or
restoration uncertainty aborts the batch and retires the reader/helper rather
than leaving an unknown limit reusable.

Request frames are capped at 1 MiB, control frames at 4 KiB, and replies at
32 MiB. SQL is capped at 256 KiB; a batch at 128 statements; bindings at 4,096
per statement; and each result at 4,096 columns and 250,000 rows, with at most
1,000,000 cells in one reply. SQLite text/blob scalars have a 4 MiB absolute
ceiling and the lower width-dependent per-statement ceiling described above;
error messages at 16 KiB, paths at 32 KiB, JSON nesting at 12 levels, and JSON
collection fan-out at 4,500,000 aggregate items.

Only validated primitive values cross the application-frame boundary. SQL
bindings and SQLite result scalars use an explicit representation, including an
encoding for `bytes`; APSW objects, `TrustedQuery` objects, exceptions,
tracebacks, `pathlib.Path` instances, Qt objects, and arbitrary application
objects are not transported within the validated application protocol. The
helper reconstructs `TrustedQuery` values and the parent reconstructs the
public result or mapped exception.

Parent and child maintain separate finite deadlines. The child supplies a
finite deadline to every `TrustedLiveReader` operation, while the parent bounds
startup, result waiting, cancellation, shutdown, termination, kill, and join.
`Connection.poll()` can report that part of a multiprocessing frame is readable
without proving that its header and body are complete. Each helper incarnation
therefore has one persistent receiver thread that is the sole caller of
`recv_bytes()` on its reply connection. That receiver publishes only a complete
bounded frame or a terminal receive failure into a one-slot inbox; startup, job,
cancel, shutdown, close, restart, destructor, and `atexit` paths obtain reply
data only through that inbox and otherwise retain finite waits. An incomplete
header or body may block the receiver, but it cannot block a public supervisor
operation from reaching its deadline and retiring the exact incarnation.

Cancellation is bound to the current generation and first requests cooperative
SQLite interruption. The control channel retains a monotonic last-consumed
cancellation generation. An exact cancellation may arrive before its command or
once after that job completed, even while a later job runs, and remains scoped
to the intended generation; stale, duplicate, wrong-session, and out-of-order
cancellations fail closed. If the cancellation grace expires, the parent
terminates the helper, escalates to kill when supported, and performs bounded
process and receiver-thread joins, while closing the incarnation's endpoints.
If either the process or its sole receiver is still alive or unreaped, the whole
incarnation remains quarantined and replacement spawning is refused until later
zero-time joins prove both have exited. A timed-out or protocol-invalid
incarnation is never reused, and its work is never replayed. A helper also
monitors its parent endpoint so it interrupts active work and exits within a
bounded period if the parent disappears.

`close()` atomically enters a closing state before releasing the supervisor
lock. New submission, restart, and context-entry operations are then rejected,
while close may cancel and finish the active job it already captured. Abandoning
an owner triggers the same bounded retirement through destructor cleanup. A
weak-reference `atexit` callback handles supervisors that remain live until
interpreter shutdown without retaining otherwise unreachable owners; marking the
helper daemon is only the final interpreter-exit fallback.

Startup failure, unsupported source, source replacement, SQL rejection, result
limit, busy timeout, operation deadline, cancellation, source I/O failure,
cleanup quarantine, malformed IPC, unexpected exit, and forced termination
remain distinct outcomes. Existing trusted-reader errors are reconstructed
where they apply. A crash, protocol violation, source replacement, or cleanup
quarantine discards the process incarnation. A later explicit operation may
start a new helper, but the failed query is never silently retried or replayed.

Only SQL rejection, an allowed-query failure, a clean result-limit rejection,
busy timeout, and cooperative cancellation leave the current helper reusable.
A result-limit rejection is clean only after cursor close, per-statement
baseline restoration, transaction rollback, restoration of SQLite's original
operation limit, and the other cleanup have all been verified. Any uncertainty
in either limit tier becomes terminal `cleanup_quarantine`; that exact helper is
retired, and later explicit work may start a fresh process. Other child errors
also retire it. Every replacement helper must report the originally accepted
main `DatabaseInstance`; after that match, `source_identity` is refreshed to
report the replacement helper's current journal mode and WAL/SHM identities.

## Stage 4 application broker and query adapter

The broker owns exactly one `TrustedLiveReaderSupervisor` for one accepted
`DatabaseInstance`. It does not run a blocking `supervisor.wait()` in a Qt event
loop. An off-GUI execution path serialises startup, query, wait, cooperative
cancellation, forced retirement, close, and join. Public cancellation marks or
removes an exact queued request promptly while that control path performs any
synchronous supervisor cleanup.

Each request carries the database/session generation, a request identity,
priority, finite deadline, and cancellation state. The queue is bounded, applies
backpressure, coalesces deadline-compatible duplicates, and can promote
unfinished work when the selection or viewport changes. Default-deadline
duplicates share the first operation's bounded deadline; explicitly supplied
deadlines coalesce only when they are equal. Results are consumed through
request handles; the service exposes no completion callback or callback thread
whose lifetime could delay retirement. Public cancellation and asynchronous
close therefore only update bounded broker state and wake its control path. The
scheduling heap is compacted to one entry per queued operation, so physical
queue storage remains subject to backpressure as well as logical requests.
The control path expires queued subscribers at their own monotonic deadlines
even while the query dispatcher is waiting on another job. Initial supervisor
startup receives a copied option set whose startup timeout is capped by the
active request's remaining deadline. A failed, cancelled, timed-out,
pre-empted, or protocol-invalid request is complete; a retry is a new explicit
request rather than a replay of the old one.

`qplot.datahandling.trusted_live_queries.TrustedMetadataQueryAdapter` is the
fixed-query boundary above the supervisor. It uses only `query()`,
`query_batch()`, and `data_version()` and returns immutable primitive records.
It is parallel to, rather than a replacement for, the cursor-based snapshot
functions in `readSQL.py`. Pure metadata transformations are shared so both
paths produce compatible run dictionaries without pretending that the
supervisor is a DB-API connection.

The adapter targets the current QCoDeS database specification. It quotes every
database-derived identifier and validates the known schema with allowed
zero-row `SELECT` statements against the known tables and, where required, a
run's result table. It does not use `PRAGMA table_info`, `PRAGMA database_list`,
or a DB-API cursor. Several dependent results use `query_batch()` when they
must share a repeatable-read transaction. Basic-run pages advance by `run_id`;
selected-run layout pages advance through a captured `layout_id` watermark.
The at-most-1,000-row basic page omits `parameters` and `run_description` and
caps its six display-text fields before returning them. Per-run enrichment uses
`octet_length` preflight plus type/length-guarded statement groups, with one
4 MiB raw budget for the complete public detail. Selected layouts cap displayed
text, total accepted bytes, and rows; omitted oversized fields are named in the
plain view's `unavailable_fields` instead of triggering a result-limit replay.
Result-table rows are never fetched merely to calculate metadata, counts,
shapes, storage, or small setpoint summaries.

Expensive metadata uses the current QCoDeS result-table contract: an append-only
`id INTEGER PRIMARY KEY`. An indexed `MAX(id)` captures a stable physical
result-row prefix without scanning its payload. Physical rows and scientific
points are deliberately separate: dense/shared storage may use one physical row
per coordinate, while interleaved or dependent-blocked storage may use several.
Only a structurally verified current-QCoDeS layout publishes acquired logical
`read_setpoint_count`. A valid run-description shape may publish a planned
`setpoint_count`, which remains an expected rather than acquired value.

Bounded direct primary-key probes first classify and validate regular 1D/2D
layouts. When those probes remain inconclusive, a metadata-only verifier can
walk the captured prefix in resumable 4,096-row keyset pages. It retains only
constant-size counters, endpoints, direction/fingerprint state, and the prior
coordinate; it never retains a distinct-value set or an axis-sized row. A full
rectangular prefix becomes an exact observed shape and observed `setpoint_count`
only after that verification. A declared planned shape remains a provisional
expected extent, so its product may exceed the acquired logical
`read_setpoint_count` of an unfinished run.
Irregular or insufficiently proved shape/count fields remain unknown. An
incomplete dependent-blocked layout without a complete rectangular candidate
is not inferred from its plan. There is no global 8 MiB database-size or
100,000-row shape gate and no whole-prefix `COUNT`/`DISTINCT`/`GROUP BY` or
`dbstat` scan. The separate Stage 2 8 MiB raw row-payload materialisation bound
remains unchanged.

First/last setpoint summaries still use fixed 4,096-ID edge windows (at most 32
per edge), group at most eight scalar-capped parameters per transaction, and
mark locally calculated storage size as estimated. The maximum edge-plan fanout
is therefore 256 transactions.

### Loading, scheduling, and refresh

After necessary path validation, identity capture, and cloud-file hydration,
qPlot attempts a bounded trusted open and basic query before invoking the legacy
database access probe. Successful Stage 3 startup is the direct-access proof.
Snapshot fallback is eligible only when the exact type of the initial-open
failure reports that the native backend is genuinely unavailable or that the
source or filesystem is explicitly unsupported. It then still requires the
legacy probe to establish a safe snapshot view. The chosen access mode is
retained with the accepted database for diagnostics and tests.

Cancellation, deadline expiry, busy timeout, source replacement, SQL rejection,
query or result-limit failure, source I/O, invalid database, helper crash,
partial frame, protocol failure, forced termination, and cleanup quarantine are
not fallback eligible. They are reported without silently replaying work or
showing a stale main-file-only view. An ordinary active WAL that is unsupported
by trusted access and cannot pass the legacy snapshot proof produces actionable
owner/checkpoint guidance instead of omitting WAL-only commits.

The basic run list has the highest priority and is published before any result
table is queried. Pagination first captures a maximum-`run_id` watermark and
reads only through that watermark. The first incremental refresh begins after
it, so a commit during bootstrap is found once without being lost or duplicated.
Subsequent priority is lightweight commit discovery; selected-run cheap then
expensive detail; visible runs in viewport order; and all remaining runs in
stable table order. Promotion does not submit duplicate work, and the stable
drain ensures all metadata finishes when interaction stops. After at most eight
foreground dispatches, the oldest queued background operation receives a real
supervisor transaction. Between transactions, a multi-query background adapter
call may cooperatively run one strictly higher-priority queued operation on the
same dispatcher, then resumes its original stack without replay.

The same helper supplies `PRAGMA data_version`. If it is unchanged and the
application has accepted every discovered page, refresh avoids redundant
metadata queries. The application's accepted `run_id` cursor is passed back to
the adapter, so a page cancelled or rejected before GUI publication is
reconciled even when `data_version` is unchanged. If the version changes, qPlot
fetches only runs after that accepted cursor and refreshes the selected run plus
watched unfinished runs; visible watched rows retain viewport priority. Schema
facts are invalidated and revalidated after a version or helper-incarnation
change. `data_version` is scoped to a helper incarnation: replacement starts a
fresh baseline and explicit targeted reconciliation. Refresh samples the
incarnation immediately before and after `data_version`, so an idle helper that
is replaced while that probe is submitted cannot make an equal numeric version
look unchanged.

Ordinary selection performs no synchronous database access and no direct
QCoDeS-dataset or snapshot I/O. It updates the selected GUID and run ID from the
basic cache, displays basic values and loading placeholders immediately, then
submits one bounded asynchronous detail request in a trusted session. That
ephemeral view supplies the selected Raw key-value, Metadata, and Snapshot tabs
only if the database instance, exact sidecars, helper and coordinator
generations, source revision, selection generation, run ID, and GUID still
match. Reselection cancels or makes the old result stale; it is not written to
the derived disk cache or reused for another selection. A trusted detail error
does not trigger an automatic fallback copy.

The Metadata and Raw selected-detail presentations keep their trees bounded
without describing ordinary long scalars as missing. Locally shortened key or
value cells carry a bounded path, byte count, explicit marker, and
current-publication identifier; their view stays `available` and has a
`[display]` summary. Only upstream or protocol loss and their node, depth,
container, or aggregate-budget exhaustion create the structural `[truncated]`
row. Exception cells preserve a bounded head and tail so the terminal exception
remains visible.

Exact scalar backing is identity-deduplicated across Metadata and Raw under one
4 MiB current-detail cap. Qt items retain only identifiers. Activation opens one
reusable read-only viewer, and only its explicit Copy action writes to the
clipboard. Selection, source, database, coordinator-generation, helper-
incarnation, same-path replacement, and shutdown fences synchronously invalidate
the backing and viewer. Later publications for the same accepted run restore
bounded Raw expansion paths without recursively opening Snapshot.

Snapshot uses a separate bounded lazy scanner. Its complete source is accepted
only up to 4 MiB and validated iteratively without constructing complete Python
or Qt trees. The bridge removes that source from the retained selected-detail
publication and owns at most the exact current selection's source outside Qt;
it is neither stored in the derived disk cache nor accumulated for other runs.
The initial root and every expanded container materialise one direct-child page.
A continuing page has at most 127 data children and one `Load more…` child (128
total), a final page stays within the same 128-child ceiling, and every page
independently caps total displayed text, including the control child, at 32 KiB. Expansion
fetches children. The top-level QCoDeS `station` container expands automatically
and fetches only its first page; deeper containers stay lazy. `Load more…`
advances the parent-bound cursor, and a shortened scalar offers its own exact
**View Full** action.

A complete validated source is reported as `available — loaded on demand`, even
when only its initial page exists as Qt items. Page limits never mean source
truncation. Malformed JSON, the 4 MiB source cap, excessive nesting or scalar
size, selected-detail acquisition limits, and a changed-during-read source keep
their precise `malformed`, `truncated`, or `unavailable` diagnostics.

Lazy pages and full values use one worker and allow at most two submitted or
queued requests in total. Each request and result carries the exact database
instance and bridge binding, helper incarnation and source revision, coordinator
and selection generation, run ID and GUID, Snapshot-session token, parent handle
and continuation cursor, and deadline. The bridge checks the complete identity
before submission and before GUI publication. Selection changes, database
switching or same-path replacement, helper restart, revision change, timeout,
and shutdown therefore reject late pages and dialogs. All widget mutation and
image or dialog creation remains on the GUI thread.

The initial source fetch is still a short, bounded, cancellable transaction
through the pinned trusted reader. Lazy browsing performs no further database
access: main, WAL, and rollback-journal handles remain physically read-only, no
checkpoint or journal-mode/schema operation is introduced, and SQLite's exact
colocated SHM coordination remains the sole permitted source-side mutation.

Snapshot-fallback selection is deliberately basic-only. It displays cached
run-list fields and an unavailable detail state without starting a
selected-detail worker, opening SQLite for detail, or preparing an additional
selected-detail snapshot. This narrow guarantee applies only to ordinary row
selection: fallback metadata and retained preview paths can still create
private snapshots. Plotting and CSV export address the selected identity
directly and materialise a QCoDeS dataset only after the explicit action.

### Pending, active, and retired services

When database B is loaded while A is displayed, A and its broker remain active
until B succeeds. B owns a separate pending broker. Failure, cancellation, or a
stale B completion closes that pending broker and leaves A usable; a successful
load revalidates B, atomically promotes its broker and UI generation, and then
retires A. A stale callback still owns and retires its pending service before it
returns.

Retired brokers remain strongly owned while asynchronous shutdown finishes.
Close, reload, source/sidecar replacement, synthetic database generation, and
application quit invalidate the correct broker and prevent late results from
crossing instances. Shutdown liveness includes query-dispatch and control
threads, the helper process and receiver, every parent pipe endpoint,
quarantined/unreaped incarnations, and outstanding requests. Broker `closed`
means query dispatch has ended; it is not sufficient to release ownership while
`resource_cleanup_pending` remains true. A 100 ms runtime timer performs only
zero-wait reaps while the retired set is non-empty and stops itself when cleanup
finishes. Application quit has one 15-second monotonic overall deadline. Its
first deferred poll repeats cancellation and zero-wait cleanup. The last 250 ms
of the same bound is reserved for diagnostic persistence. Escalation exceptions
remain separate from the newest liveness scan so neither can overwrite the
other. Diagnostic log persistence runs on an independent pre-deadline path, so
blocked logging or fsync cannot delay process termination. A lightweight
launcher establishes full qPlot-tree containment before Qt or reader helpers
can start. The CLI process is itself that launcher; public `qplot.run()` first
starts a dedicated Python launcher so the acquisition caller, its writer thread,
and unrelated children remain outside qPlot's termination boundary. POSIX gives
the GUI a dedicated session/process group and retains its
unreaped leader; Windows atomically assigns the suspended GUI to a retained
kill-on-close Job Object at process creation and keeps the job and process
handles. Signal guards and the bounded absolute startup interval are installed
before spawn. A setup, containment, authentication, or never-`HELLO` failure
before authenticated `READY` tears down and reaps that contained tree without
extending the startup bound. After `READY`, pre-`ARM` EOF or malformed traffic
is observe-only.

After confirmation, the GUI sends one authenticated `ARM` message carrying the
unchanged absolute deadline. The launcher commits the first authenticated
finite deadline immediately after decoding it, before acknowledgement
construction or transmission, and has no `DISARM` transition. After that
commit, EOF, invalid or duplicate traffic, and a lost or unconstructable
acknowledgement cannot cancel or extend the deadline. Exact startup, protocol,
termination, and reap errors remain present alongside the newest final resource
state whenever diagnostics can be written.

The launcher stays armed across `app.exec()` return and explicit destruction of
qPlot's window, owned thread pools, and application after a fresh complete
liveness scan proves normal cleanup. An exhausted deadline or non-quiescent
final scan reaches the process-boundary wait before any potentially blocking Qt
destructor. Otherwise the launcher kills first and then reaps the same child at
the original 15-second total deadline rather than relying on
`QApplication.quit()`. POSIX kills only the dedicated group, retains its
unreaped leader through every positive-signal retry, reaps the leader's exact
status, and thereafter uses only signal-zero observation until the group is gone.
Windows terminates only through the retained job/process handles and closes
them only after the direct process is signalled and the job is empty. Neither
path releases live containment after a transient kill or reap failure. The
GUI's same-deadline `_exit` fallback is a backstop, while the independent
launcher remains the guarantee when native Qt code indefinitely holds the
Python GIL. POSIX launcher termination signals are re-raised after tree cleanup
so the launcher keeps true signal semantics. QCoDeS writers and test sentinels
created outside the qPlot group or job remain outside its termination boundary.

The public launcher authenticates a separate bounded result channel before it
can spawn the GUI. No GUI or helper inherits its endpoint. It publishes the
normal status, forced status 70, retained diagnostics, or a non-destructive
negative POSIX signal number only after whole-tree cleanup, then keeps the
endpoint open until launcher process death. EOF therefore remains authoritative
when a caller thread has already reaped the launcher with `waitpid(-1)`. A
protocol or unexpected-launcher failure returns 70 rather than signalling or
hard-exiting the QCoDeS acquisition process.

That channel also has one authenticated caller-to-launcher control direction.
If the API caller is interrupted while waiting, its first `KeyboardInterrupt`,
`SystemExit`, or other control-flow exception is retained while an immutable
cancellation record causes the existing group/Job owner to terminate and reap
the GUI and trusted-reader helper. Repeated interruption cannot release
containment or replace the first exception, including during cancellation lock
entry, deadline setup, partial sending, EOF fallback, result processing,
launcher waiting, or final diagnostic publication. One dedicated writer owns
the cancellation direction. A serialized one-time lifecycle makes concurrent
cleanup and result-reader requests share one worker object/start attempt, or one
committed EOF fallback when startup fails. It persists its send offset and
resolves to a full authenticated record or irreversible write-side EOF without
holding its lifecycle lock during socket I/O. The temporary SIGINT guard saves
the exact caller handler once and transactionally verifies both installation and
restoration if interruption lands after the real signal side effect. The
exception is re-raised only after an authenticated final outcome, launcher EOF,
and exit/reap observation; caller disappearance
before the outcome is equivalent to cancellation. Pre-`READY` POSIX cleanup
closes the listener and waits for the bounded bootstrap self-exit without
signalling a potentially stale PID.

`qplot.run(return_objects=True)` is intentionally different: it runs inside the
caller's process and returns caller-owned Qt objects. It does not acquire this
launcher containment or process hard-deadline guarantee.

### Stage 5C Qt derived-work bridge

Trusted live loading, automatic default selection, scrolling, and metadata
completion do not launch legacy snapshot-backed detail, preview, or thumbnail
workers. Snapshot fallback sessions retain their separately permitted behavior.
For a trusted session, `TrustedDerivedQtBridge` owns one Stage 5B coordinator
and is the sole producer of derived run metadata, dimensions, run-table
thumbnails, and selected-run preview cards. The cheap run list is committed
first; the base priority is `(run tier, work kind, stable index)`. The selected
run gets metadata (with ephemeral detail), thumbnail, then preview; visible runs
get metadata for all stable indices, thumbnails for all, then previews for all;
remaining runs follow the same three lanes.

Coordinator and retry threads only request one coalesced queued Qt wakeup. The
bridge alone polls on the GUI thread, decodes PNG payloads into Qt images, and
updates `MainWindow`, `RunList`, and `PreviewTab`. It derives visible stable run
slots from the actual viewport after scrolling, sorting, filtering, resizing,
and model changes. Publications are accepted only while the exact database,
service, generation, GUID/run identity, source revision, helper incarnation,
and renderer/options format remain current. No-longer-selected derived metadata
or images may be retained for later selection, but cannot replace the current
selected detail or preview; selected detail itself is ephemeral.

Progressive full-prefix verification consumes at most one 4,096-row page per
owner turn, then the coordinator re-evaluates the exact priority at the page
boundary. Progressive metadata is a dependency only for that run. Strictly
higher-tier work and a ready selected thumbnail or preview always win. Within
the same tier, ordinary work receives at most three claims (one sibling
metadata/thumbnail/preview sequence) before one paced continuation page is aged
in. Eligible pages rotate, and newly selected work pre-empts at the next
boundary. When no selected image is ready, bounded aging admits a remaining-run
metadata claim within eight foreground claims. A dedicated 10 ms coalesced
continuation wakeup prevents
zero-delay polling while preserving full drain. An inconclusive page is parked
as metadata-only work and causes no render, payload publication, or cache write;
only that run's image kinds remain blocked. Once verification is conclusive,
the normal metadata, thumbnail, and preview claims each render, publish, and
attempt a cache write only once for that captured prefix. The final output is
therefore published exactly once rather than once per page. Verification of a
later append resumes from accepted constant-memory state and its prior physical
cursor when the candidate pattern and fast extent remain compatible.
Incompatible or insufficient new evidence fails closed rather than being
accepted as proof.

Runs with different dependency tuples use one shared physical page and a
constant-size verifier for each supported 2D dependent. Each verifier learns
its own first row, physical strides, fast axis, and rectangular extent, using
that dependent's declared dependency order and shape. Compatible reordered
dependencies retain their own axis order. A malformed or unsupported group
does not invalidate another group's proof. The run cursor and independent
proofs are cached through the accepted watermark, so repeated requests finish
verification and later appends resume without rescanning the accepted prefix.

Decoded image ownership is intentionally outside the bridge. `PreviewTab` owns
the only retained full-preview cache and applies independent 512-entry and
128 MiB limits. Inline run-list cells own the thumbnails they display; when
inline previews are disabled above 500 runs, Qt skips thumbnail decoding even
though Stage 5B extraction and disk-cache population may continue. Selecting an
evicted preview clears only that run's completed preview bit through an exact
database/generation/GUID-aware scheduler operation, so metadata and thumbnail
completion remain intact and the coordinator can replay the PNG from disk.
Changing selection may independently request ephemeral detail with selected
metadata.

The derived extraction does not require an expensive-run request first. It
uses the bounded basic run entry to locate the result table, validates its
current schema, and captures GUID/table identity, an authoritative indexed
physical `MAX(id)` watermark, and guarded run-description facts in one
repeatable-read batch. A proved 2D grid uses at most 4,095 representative rows
spread over both complete logical axes. The sample keeps exact endpoints, full
small axes, mirror symmetry and adjacent parity/direction evidence while
honouring separate physical row patterns for multiple dependents. Without a
grid proof, the bounded generic fallback remains 15 keyset windows of 256 rows
plus the newest 256-row edge. Multiple proofs share the same total grid-row
budget; mixed 1D/2D extraction reserves 256 rows for generic windows within
that budget. Sampled orientation is verified separately against each layout's
Cartesian sample. Every observation retains at most 4,096 rows, 33
columns, and 135,168 cells, with no OFFSET or full-result
COUNT/DISTINCT/GROUP BY aggregate. Ordinary appends cannot invalidate the
captured prefix because every sampled id is at or below that transaction's
watermark. Repeated active-run invalidations are coalesced, including source
changes observed during final rendering and terminal failures.

Payloads contain only bounded primitive structures and deterministic PNG bytes.
Renderer version 3 is Qt- and Matplotlib-independent. For heatmaps, declared
dependency 0 is vertical and dependency 1 horizontal regardless of the
physical fast axis; reversed and serpentine acquisition is restored to
scientific orientation. Representative source-domain bins preserve both full
extents, finite cells use the embedded Viridis table, and missing or
not-yet-measured cells stay neutral. Each dependent is rendered independently,
and thumbnail and preview sizes share the same scientific mapping. A singleton
finite 1D point is visible. At most eight dependent images are retained; each
dimension is at most 2,048 pixels, decoded image bytes are capped at 16 MiB,
aggregate encoded image bytes at 8 MiB, and the complete retained payload at
16 MiB.
One immutable absolute job deadline and cancellation check spans lookup,
broker acquisition, rasterisation, encoding, payload validation, cache writing,
and eviction. Derived cache format version 3 uses cryptographic filenames,
canonical type tags, bounded JSON preflight/materialisation, complete-key
verification, declared lengths, SHA-256 payload checks, same-directory 0600
temporary files, and atomic replacement beneath a 0700 application-cache
directory. Read, corruption, permission, and capacity failures are misses;
unsafe roots at or above/below the selected database directory disable disk
caching and retain memory-only operation.

Live appends and incomplete-to-complete changes are coalesced behind the active
captured prefix, then regenerated without starving selected or visible work.
Changing preview size invalidates only preview work. Database switching and
same-path replacement disarm or reject obsolete publications, while a helper
incarnation boundary invalidates the previous coordinator generation. Close
disarms wakeups and publications first and retires the coordinator through the
existing bounded application shutdown lifecycle, leaving no extra Qt watchdog,
per-run task, timer, or worker architecture.

Selecting the exact active database path again refreshes this binding in place.
The bridge retains its coordinator, exactly two fixed timers, selected/viewport
priority, and bounded preview cache, while later publications remain eligible.
It does not start a helper, coordinator, timer pair, or legacy worker.

## Finite operations and errors

`TrustedLiveReader.open()` returns an owner-thread-bound reader with a finite
five-second operation default. `query()` incrementally materialises one
read-only statement under the cursor-time result budget inside a promptly ended
transaction; callers can supply `bindings`, `timeout`, `deadline`, and
`cancel_event`. `query_batch()` accepts an already bounded sequence of immutable
`TrustedQuery` specifications, shares one result budget across them, and
materialises them in one repeatable-read transaction; it accepts no callback or
cursor that could hold the transaction idle. Transactions are intentionally
short so they do not indefinitely delay writer checkpoints or WAL resets.

Relative timeouts, absolute monotonic deadlines, cancellation events, and the
cross-thread `interrupt()` method cooperatively interrupt SQLite work. Busy
handling is separately bounded and cannot outlive the operation deadline. On
every success, policy rejection, timeout, cancellation, query error, source
change, close, and partial-open failure, the reader attempts rollback and checks
every wrapper-visible cleanup result. If complete lock and proof-state release
cannot be established, it force-closes what it can, raises
`TrustedLiveCleanupError`, and retires that reader. If forced final resource
release is proved, a new direct reader may be opened; only unproved final native
or handle release permanently quarantines the in-process singleton. Stage 3
nevertheless retires the whole helper process for every cleanup error, so
process exit remains its fail-closed recovery boundary.

The public surface does not return connections, cursors, writable row objects,
or an indefinitely idle transaction. Its error taxonomy distinguishes:

- policy rejection (`TrustedLiveSqlRejectedError`);
- a proven identity replacement (`TrustedLiveSourceChangedError`);
- bounded lock expiry (`TrustedLiveBusyTimeoutError`);
- cancellation and deadline expiry (`TrustedLiveCancelledError` and
  `TrustedLiveDeadlineExceededError`);
- unsupported sources and ordinary source I/O
  (`TrustedLiveUnsupportedSourceError` and `TrustedLiveSourceIOError`);
- an unavailable pinned runtime or native boundary
  (`TrustedLiveReaderUnavailableError`);
- corrupt or invalid databases (`TrustedLiveInvalidDatabaseError`);
- a live result exceeding its cursor-time materialisation or wire budget
  (`TrustedLiveResultLimitError`);
- ordinary allowed-query failures (`TrustedLiveQueryError`); and
- cleanup that cannot be proved complete (`TrustedLiveCleanupError`).

A source-change error is reported only after an identity check proves a change.

SQLite interruption can stop SQLite virtual-machine work and unwind the reader's
transaction. In-process Python cannot forcibly pre-empt arbitrary caller code
that has stalled outside SQLite. The Stage 3 supervisor therefore treats the
reader operation as a finite job and supplies the hard process-level recovery
boundary described above.

The reader token and captured source identity are available for future callback
binding. Normally, `close()` or the context manager releases the session
cleanly; the quarantine rule above applies if that release cannot be proved.
The native audit counters separate prohibited main/WAL/journal operations from
the allowed SHM coordination operations used by tests and diagnostics.

## Deliberate limits

- The native control channel permits one trusted reader per process. Close that
  reader before opening another. Each Stage 3 helper owns exactly that one
  reader, and each supervisor serialises its finite jobs. Independent supervisors
  provide isolation in independent helper processes. Stage 4 creates exactly one
  supervisor for each pending or active application broker and serialises all
  access to it.
- The supervisor does not silently replay a job after cancellation, timeout,
  crash, source replacement, cleanup quarantine, or a protocol error. Recovery
  requires a later explicit operation or session, which starts a fresh process
  incarnation.
- The row, cell, scalar, and wire limits bound raw payload and conservative
  logical standard-object accounting during live result materialisation and
  IPC construction. They do not bound allocator reservation or rounding, RSS,
  fragmentation, arenas, or arbitrary internal computation performed by
  SQLite's virtual machine.
- The reader is a trusted same-host viewer, not a sandbox against a hostile
  process continuously replacing files or redirecting parent path components.
  Preflight, retained-proof, actual-handle, and operation-boundary checks detect
  observed replacements; they are not a security boundary against adversarial
  namespace substitution. The selected local path hierarchy is assumed trusted.
- A writer may legitimately remove and recreate WAL sidecars between
  transactions. A bound reader fails closed on an identity change; callers must
  close it and explicitly open the new source incarnation.
- Windows and POSIX native behavior must pass platform CI before a source
  revision is accepted across platforms. The workflow exercises Linux, ARM64
  macOS, Intel macOS, and unprivileged Windows, but coverage is not considered
  passed until all four hosted jobs succeed for that exact revision. Windows
  and macOS remain qPlot's supported GUI desktop platforms; the Linux wheel is
  a reader and packaging reference rather than a declaration of Linux GUI
  support.
- Changing the APSW/SQLite pin requires reviewing the platform VFS integration
  and rerunning native audit, replacement-race, concurrent-writer, and installed
  wheel tests on every supported platform.
