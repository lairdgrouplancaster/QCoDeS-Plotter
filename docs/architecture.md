# Architecture

This document is a working map of the current codebase. It is intentionally
short: update it when module responsibilities move.

## Distributions

The root `pyproject.toml` builds the pure-Python `qplotter` distribution
from `src/qplot`. Its console commands and public `qplot` imports are unchanged.
`native/pyproject.toml` and `native/setup.py` independently build
`qplotter-native` from `native/src/qplot_native`, retaining the CPython
3.11 stable ABI. The C source and SQLite ABI header live in that package.
The application pins a compatible native version; `trusted_live.py` locates
its extension and checks native metadata and the pinned APSW/SQLite runtime.

## Entry Points

`src/qplot/__main__.py` defines the `qplot` command and `qplot.run()`. It creates
the `QApplication`, opens `MainWindow`, and starts the Qt event loop.

`src/qplot/__init__.py` exposes the public package imports used by scripts and
interactive users.

`_shutdown_supervisor.py` owns process containment and the launcher lifecycle.
`_shutdown_protocol.py` owns its authenticated byte layouts, frame validation,
bootstrap schemas/codecs, and immutable outcomes, without sockets, threads, or
Qt. Keep protocol changes there so both endpoints use the same rules. The
supervisor retains environment-marker consumption and socket/path extraction.

`_process_shutdown.py` owns the GUI process's deadline fail-safe and diagnostic
persistence. The entry point supplies the persistence callback explicitly;
the controller can run and be tested without importing the main window.

`diagnostics.py` recognizes its guarded file handler and unavailable-log fallback
by concrete types. Reconfiguration and cleanup share one ownership snapshot,
leaving handlers installed by callers attached and open.

`desktop.py` provisions launchers and delegates Windows Open With ownership,
registry preflight, installation, and rollback to `_desktop_associations.py`.
`_desktop_sidecars.py` separately manages optional sidecar icons. It converts
persisted JSON backups into named validated records before registry mutation;
installation rollback and removal share restoration of those records.

`_macos_identity.py` shares one bounded interpreter-identity preflight between
desktop installation and runtime-bundle creation. Each caller keeps its own
signing and publication sequence.

## Main Window

`src/qplot/windows/main.py` owns the top-level application window. It handles
layout, menus, shortcuts, themes, global status messages, and coordination of
the extracted main-window action mixins.

`src/qplot/windows/_database_actions.py` contains the database-facing main
window actions. It handles:

* loading and remembering QCoDeS database paths
* refreshing the run list
* recent database menus
* database-load progress, cancellation, and restore handling

`_database_load_state.py` defines the pending `DatabaseLoadSession` and immutable
`DatabaseViewSnapshot`. The snapshot describes the committed view, including its
reader and database observation; it is separate from the pending worker's state.
Export protection also includes the pending session's source.

`_database_load_publication.py` owns staging, commit, rollback, and Qt publication
gates. Run-list insertion may process nested close/shutdown events, so the pending
reader stays reachable by normal teardown throughout publication. A failed stage
restores the previous view only after revalidating its source; failed restoration
closes the view. The action mixin handles result validation, user feedback, retries,
and restarting progressive work after the transaction has released its gates.
`_database_observation.py` shares the directional source check: newly created
WAL/SHM files are allowed, but an already accepted sidecar cannot disappear or be
replaced. These comparisons use `DatabaseInstance` throughout.

`_database_detail_state.py` groups generation, worker, source instance, and
activity for the basic and expensive snapshot-detail channels. Their callbacks
share lifecycle operations while retaining source checks around publication.

`src/qplot/windows/_plot_actions.py` contains the plot-facing main window
actions. It handles:

* opening 1D and 2D plot windows
* exporting measurement data
* preview plot/export actions
* adding compatible preview traces to existing 1D plots
* tracking datasets currently used by plot windows

Its `openPlot` path selects the 1D/2D widget class before the shared duplicate
check and launch sequence, keeping dataset ownership and status handling common.

`src/qplot/windows/_run_controls.py` contains the run-selection and refresh
controls owned by the main window. It handles:

* the run ID and measurement entry widgets
* refresh interval controls and persistence
* run-list and selected-run detail widget creation
* the empty-database prompt
* run-action keyboard shortcuts

When adding a new top-level command, menu action, or workflow that coordinates
multiple windows, start in `main.py`; put database-specific behavior in
`_database_actions.py`, plot-opening/export behavior in `_plot_actions.py`, and
run-selection or refresh-control behavior in `_run_controls.py`.

`_refresh_interval.py` keeps each control's accepted interval, displayed value,
and pending text edit in one `_RefreshIntervalState` record. The state is
transient and works with ordinary Qt spin boxes as well as the custom control;
rounded display values do not silently replace the accepted timer precision.

`windows/_updates.py` owns asynchronous update requests and dialog lifetimes.
`_update_presentation.py` builds immutable messages without Qt or networking;
the displayed instructions and copy button use the same platform-quoted command.
`updates.py` selects compatible releases from the bounded PyPI response.

`_shutdown_coordinator.py` owns confirmed GUI shutdown: one typed state tracks
the phase, fixed deadlines, cleanup escalation, and diagnostic snapshots.
`MainWindow` retains exit confirmation and Qt entry points, with read-only status
properties for existing publication guards. The process fail-safe and launcher
remain independent owners of termination at the process boundary.

`_shutdown_resources.py` is the background-resource inventory. Pools register
when created, and cancellation resolves the current worker for each registered
binding. Liveness and escalation share that inventory. Reader membership is
observed afresh from active, pending, and retired services on each scan; readers
that finish during a liveness probe get a final zero-wait reap. Register new
background resources at creation so shutdown can account for them. Test window
teardown also uses this inventory instead of maintaining another pool list.

`src/qplot/windows/_commands.py` is the shared command and shortcut registry.
Use it when adding menu actions, context-menu actions, keyboard shortcuts, or
shortcut-help rows so labels, status tips, tooltips, and in-app shortcut help
stay in sync.

## Plot Windows

`src/qplot/windows/_plotWin.py` is the shared base for plot windows. It owns
common plotting behavior such as refresh timers, axis selection controls,
context menus, plot-area resizing, and operation panels.

`_axis_ticks.py` owns bounded tick placement, label formatting, and installation
on plot and colorbar axes. Named candidate records make the priority of count,
coverage, centring, and spacing explicit while preserving deterministic ties.

`qplot/_number_formatting.py` owns summary-number formatting and cursor field
widths without importing Qt. Statistics use it directly; plot windows keep
their existing formatting methods as delegates and supply the current cursor
precision preference.

`src/qplot/windows/_plot_refresh.py` contains shared worker-backed plot refresh
orchestration: deciding whether to read from the database or cached data,
starting workers, applying worker results back to plot-window state, and
surfacing worker failures.

The plot window owns one typed `RefreshPublication` rollback record for the
latest callback. It captures the prior cache parameter and plot fields, records
the cache values actually published, and conditionally restores them if the
source changes before rendering finishes. A nested newer callback replaces the
record; an older callback cannot restore or discard the newer state. Source
checks remain at the cache and renderer publication boundaries. Worker result
data and completion signals remain separate from this GUI-owned rollback state.

`src/qplot/windows/_dataset_handle.py` defines the small `DatasetHandle`
structure used by the main window and plot windows to track an open dataset,
its active plot-window user count, and any delayed-release timer.
`_dataset_registry.py` owns retained-handle creation, release, eviction, and
unowned connection cleanup. It resolves the current selected dataset at each
close boundary, including after database-view rollback; action methods retain
source identity validation and selection/UI changes.

`src/qplot/windows/_plot_export.py` contains shared plot output behavior:
native printing, pyqtgraph export-dialog setup, PDF rendering, clipboard image
copies, high-DPI copies, and SVG clipboard output.

`windows/_export_names.py` owns suggested-name sanitization and filename suffix
rules, including selected-filter precedence, without Qt or filesystem access.
`_export_paths.py` resolves the name before approving its destination and retains
all publication protections.

`_plot_source_promotion.py` validates the surviving primary source and restores
its controls, retained datasets, then rendering settings through named phases.
The outer operation retains replacement-window ownership and rollback. Restore
averages after layer mappings and viewport, and trace limits after averages.

`src/qplot/windows/_plot_feedback.py` contains shared plot-window status,
state-overlay, error-dialog, and shortcut helpers. Keep common plot-window user
feedback there instead of adding more status or message-box methods to
`_plotWin.py`.

`src/qplot/windows/_plot_axis_scaling.py` contains the shared plot-axis scaling
mixin and the custom axis item used for power-of-ten unit labels. It owns the
X/Y axis scaling dialogs opened by double-clicking plot axes.
Its immutable axis specifications define names, aliases, dimensions, sides,
assignment attributes, and tab help together.

`tools/native_transform_math.py` owns sample differences, secants, interpolation,
and FFT coordinate arithmetic. `windows/_native_transforms.py`
retains the PyQtGraph item, display checks, and existing helper entry points.

`src/qplot/windows/_range_entry.py` shares the Min / Max and Center / Span
input selector between axis and color-scale dialogs. It retains canonical
endpoints when changing the display format; each dialog continues to validate
and apply range edits through its existing plotting methods.

`src/qplot/windows/_plot_marquee.py` contains the shared marquee selection
mixin used by plot windows. It owns marquee drawing, dragging, zooming, stats
dialogs, and base context-menu actions. Plot-type-specific snapping and stats
live in `plot1d.py` and `plot2d.py`.

`src/qplot/windows/_plot_state.py` contains the plot-area state overlay used
for loading, waiting-for-data, and worker-error messages. Keep state-display
styling there rather than embedding overlay widgets in individual plot types.

`src/qplot/windows/plot1d.py` extends the shared plot window for line plots. It
owns main line rendering and line-plot marquee statistics.

`src/qplot/windows/_plot1d_snap.py` contains the line-plot snap-to-trace mixin.
It owns the snap shortcut/menu action, nearest-point lookup, snap status
readout, and snap marker display.

`src/qplot/windows/_plot1d_traces.py` contains the line-plot trace mixin. It
owns secondary trace controls, added-trace refresh handling, right-axis viewbox
synchronization, and cleanup of hidden trace windows.

`_plot_appearance.py` shares the row-order calculation used by trace, heatmap,
and mixed appearance dialogs. Each dialog still owns registry mutation and its
refresh policy. `_plot_drop_axes.py` uses named axis identities and candidate
priorities to select compatible axes before mutating the target plot.

`src/qplot/windows/plot2d.py` extends the shared plot window for heatmaps. It
owns heatmap rendering, hover pixel display, and marquee color scaling.

`src/qplot/windows/_plot2d_layers.py` owns secondary heatmap layers added by
preview drag-and-drop, including per-layer renderers and opacity controls,
hidden source-window refresh ownership, shared-colorbar membership, and layer
cleanup.

`src/qplot/windows/_source_consumer.py` owns each secondary trace or heatmap's
shared source-consumer registration and hidden-source interval connection.
Release is idempotent. The line and heatmap owners retain their distinct final
monitor, reload-timer, and worker-cancellation policies.

`src/qplot/windows/_plot2d_colorbar.py` contains the heatmap colorbar mixin. It
owns color autoscaling, colorbar interaction handlers, and color-map selection
state used by `plot2d.py`.

`src/qplot/windows/_plot2d_colorbar_dialog.py` owns the color scale dialog,
color-map chooser table, and persistent color-map filter controls used by the
heatmap colorbar mixin.

`src/qplot/windows/_plot2d_sweeps.py` contains the heatmap sweep/cut mixin. It
owns horizontal and vertical cut creation, cut-line cursor behavior, keyboard
movement, grouped dragging, and synchronization with 1D sweep windows.

`src/qplot/windows/_colorbar.py` contains the heatmap color-map catalog,
filtering helpers, preview rendering, and colorbar table items used by
`_plot2d_colorbar.py`.

Its filter specification registry is also the source of broad filter labels,
settings keys, control names, and catalogue groups for the color scale dialog.
The existing subtype rules and transactional settings persistence stay separate.

Use the shared base only for behavior that should apply to both line plots and
heatmaps. Keep plot-type-specific interaction details in `plot1d.py` or
`plot2d.py`.

## Main Window Widgets

`src/qplot/windows/_widgets/treeWidgets.py` contains the run table, run details
tabs, copyable metadata tables, and delegates used by the main window.

`src/qplot/windows/_widgets/run_list_items.py` contains run-table support
widgets and items: measurement preview cells, setpoint-count delegates, and the
sortable tree item used by the run list.

`src/qplot/windows/_widgets/details_tables.py` contains copyable table/tree
widgets, wrapped-value delegates, and helpers for rendering and copying nested
metadata values in run details and statistics dialogs.

`src/qplot/windows/_widgets/_run_formatting.py` contains pure run-list and
run-detail formatting helpers. Prefer adding display formatting there so it can
be tested without constructing Qt widgets.

`src/qplot/windows/_widgets/preview.py` creates and renders run preview
thumbnails. It also handles preview selection and drag payloads. Trusted
sessions receive decoded Stage 5C images from the bridge; retained compatibility
paths may still use the bounded legacy preview queue.

`src/qplot/windows/_widgets/operations.py` defines the operation panel widgets
that collect user-selected data operations before refresh processing. Operation
identity and heatmap transposition pairs live in `tools/operation_registry.py`;
promotion copies ordered panel state through `capture_state`/`restore_state`,
including exact editor text and Apply generation, without using display labels
as operation identifiers. This state is transient, not a settings format.
Each operation row owns explicit read, reset, and validation methods. The panel
compiles their validated values into ordered calls; it does not parse editors
or resolve placeholder defaults itself.

`src/qplot/windows/_widgets/dropbox.py` and `toolbar.py` contain smaller
reusable UI controls used inside plot windows.

## Data Loading

`datahandling/export_data.py` selects exportable measurements and expands CSV
columns within each acquisition record. Keep its singleton broadcasting and
nested object-cell handling separate from plot flattening: the two paths have
different input contracts. GUI actions retain destination approval and publication.
The same module assembles dataframes with precision-preserving pandas dtypes.
The controller supplies aligned measurement columns lazily, so each frame is
assembled before the next measurement is read.

`datahandling/csv_export.py` streams raw CSV from a fixed run prefix. Trusted
exports request the descriptor and prefix from the existing broker; fallback
exports acquire and close their own read-only dataset. Physical row-ID paging
and bounded NPY slices avoid materialising every measurement as a dataframe.
Each parameter tree expands acquisition records independently before the trees
are joined side by side. Exact integers, extended floats, complex values,
singleton broadcasting, and missing trailing cells retain their CSV semantics.

`windows/_run_csv_export.py` captures the run identity and measurement selector
without a GUI-thread data read. It prepares single or batch destinations before
queuing work. Batch requests retain the selected GUIDs through modal dialogs;
each run gets its own committed prefix when its worker starts. Switching or
closing the main database cancels its pending raw exports.

Thumbnail group exports capture `(GUID, parameter name)` targets from the run
table or selected-run Preview pane. They use the same batch destination
approval and source validation as row exports, with one measurement per file.
Filename allocation disambiguates collisions before any writer starts,
including measurements sharing the optional QCoDeS run-based filename format.

`windows/_export_tasks.py` owns one bounded serialization pool and a nonmodal
progress window. Writers receive only frozen inputs, cancellation, progress,
and the owned stage path. `StagedExport` in `_export_paths.py` separates worker
serialization from GUI-thread publication: source, destination, and active
database protections are revalidated before the atomic replace. Cancellation
discards the stage and retains an existing destination. Shutdown counts tasks
until GUI cleanup finishes, including cancelled queued tasks; completed history
releases callbacks and Qt/data owners.

Plot CSV captures copies of loaded arrays and operation flags on the GUI thread.
`tools/plot_csv.py` transforms and serializes those detached values in the worker.
Line exports explicitly select underlying or native-transformed samples;
heatmaps export their current primary grid. Neither mode triggers a source read.

`_numeric_arrays.py` shares NPY header inspection and bounded numeric BLOB
slices. The worker retains ownership of its read-only SQLite handle; preview
decoding retains its stricter real-only dtype and sample-budget policy. Header
inspection does not allocate or deserialize payloads.

`datahandling/database.py` owns background database workers and reports.
Its access check launches `_database_probe.py`, which accepts the source path,
quarantined-WAL flag, time budget, and observed identity and delegates to the
read-only reader. The parent retains timeout, cancellation, and process reaping.
`_database_info.py` formats already collected diagnostic summaries; SQL reads,
source checks, and worker publication stay in `database.py`.
`cloud_files.py` owns cloud-placeholder detection and cancellable hydration;
it runs the standard-library-only `_cloud_prefetch.py` helper in a subprocess
so a stalled OS read cannot block the GUI. The helper opens files only as `rb`.

`_sqlite_identifiers.py` shares literal identifier escaping. The trusted
metadata boundary retains its stricter validation, while snapshot callers
retain their existing conversion to text. This helper neither opens databases
nor determines what a reader may query.

`src/qplot/datahandling/readSQL.py` contains the cursor-based snapshot query
path. Snapshot callers still use its dynamic cursor and QCoDeS-compatible
operations; the trusted reader is not presented as a DB-API connection.
`run_metadata.py` owns shared bounded parameter/shape interpretation and derived
run fields. Both readers use its observed-setpoint publication rules; trusted
queries no longer import the snapshot reader to materialize primitive results.

`src/qplot/datahandling/trusted_live_queries.py` coordinates the trusted
metadata and preview query plans, run cache, and primitive-result publication.
It delegates to four modules with separate responsibilities:

* `trusted_query_types.py` defines the immutable records, source revisions,
  shared result budgets, and executor contract used by the broker, cache,
  rendering, and UI. Those consumers import the types directly instead of
  importing the metadata adapter. The adapter retains its public re-exports.
* `_trusted_query_metadata.py` contains pure parameter normalization,
  dependency/shape interpretation, and presentation limits. Snapshot readers
  use these functions directly; they do not call private adapter methods.
* `_trusted_grid_sampling.py` chooses bounded grid samples and validates their
  axis orientation. It consumes layout proofs and performs no database I/O.
* `_trusted_result_layout.py` owns the bounded result-table probes and resumable
  verification state. `ResultLayoutQueries` holds both pending evidence and
  completed proofs; `observe_run` advances the same finite queries as before.

The adapter owns one `ResultLayoutQueries` instance. Rebinding the broker's
executor updates both owners without discarding progress. Bootstrap clears all
layout evidence; a changed run GUID or result table discards only that run's
evidence. Queries may yield to higher-priority broker work, so the adapter still
merges results into the newest cached run after the final query boundary.
Layouts are never inferred from a declared shape alone. The query modules do
not own SQLite connections, change VFS policy, or extend transaction deadlines.

Basic runs are paged by
increasing `run_id`; selected-run layouts are paged through a captured
`layout_id` watermark. The basic page defers large description/parameter
scalars; per-run and layout values are byte-preflighted, guarded, cumulatively
budgeted, and explicitly marked unavailable when they cannot safely cross the
fixed public-detail envelope. Database-derived identifiers are quoted, and
related reads use `query_batch()` when they need one repeatable-read
transaction. Plans
validate the current known QCoDeS schema with zero-row `SELECT` statements
against the known tables (and result tables where needed); they do not issue
`PRAGMA table_info`, return cursors or QCoDeS objects, or read result-table rows
when counts, shapes, storage, or bounded setpoint summaries are sufficient.
For current QCoDeS append-only result tables, expensive reads capture the
integer-primary-key `id` watermark. That watermark counts physical result rows;
it is not automatically the number of scientific points because current QCoDeS
may store multiple dependent rows for one logical coordinate. Bounded direct
primary-key probes classify and validate dense/shared, interleaved, or
dependent-blocked regular layouts. If those probes are inconclusive, the
adapter can verify a captured prefix through resumable 4,096-row keyset pages,
retaining only constant-size counters, endpoints, direction/fingerprint state,
and the prior coordinate. There is no database-size or 100,000-row gate and no
whole-prefix `COUNT`/`DISTINCT`/`GROUP BY` or `dbstat` aggregate.

Only a structurally verified layout produces acquired logical
`read_setpoint_count`. A valid shape from the run description can produce a
planned `setpoint_count`, but it is a provisional expected extent whose product
can differ from the acquired logical count of an unfinished run. A full
rectangular captured prefix becomes an observed exact shape and observed
`setpoint_count` only after verification. Irregular or inconclusive acquired
values remain unknown; in particular, an incomplete dependent-blocked layout
without a complete rectangular candidate is not inferred from its plan. Fixed
ID-edge summaries still provide bounded first/last setpoint values, grouped
eight parameters at a time, and result storage remains explicitly estimated.

The Stage 4 application read broker is Qt-independent and belongs to one exact
accepted `DatabaseInstance`. It owns exactly one
`TrustedLiveReaderSupervisor` and persistent helper, serialises all operations,
and carries database/session generation, request identity, priority, deadline,
and cancellation state with each bounded request. Its queue is bounded,
coalesces deadline-compatible duplicates, promotes unfinished requests, applies
backpressure, compacts its scheduling heap to the live queued operations, and
publishes results only through request handles. It exposes no completion
callback facility or callback thread that could hold retirement open. The
deadline-aware control path expires queued requests while query dispatch is
blocked. Blocking open, query, wait, cancel, close, and join work stays on
broker/control threads, not the GUI thread. Initial helper startup is capped by
the active request's remaining deadline.

Broker priority is basic run-list bootstrap, lightweight commit discovery,
selected-run cheap then expensive detail, visible rows in viewport order, and
the remaining rows in stable table order. Promotion does not duplicate work,
and stable-order draining prevents starvation when interaction stops. A bounded
foreground burst advances the oldest background item even under recurring
refresh, while job-free adapter transaction boundaries allow newly selected
work to overtake a multi-transaction background operation without replaying it.
Trusted ordinary selection renders cached basic values and loading placeholders
immediately. The Stage 5 coordinator requests one immutable bounded detail view
with selected metadata and applies it only after database-instance, helper,
coordinator, source, selection-generation, run-ID, and GUID checks. The view is
ephemeral: it is not a fake or eager QCoDeS `DataSet`, private snapshot, or
derived-cache item.
Snapshot-fallback sessions instead stop at cached run-list basics and an
unavailable detail state: ordinary selection starts no selected-detail worker
and prepares no additional selected-detail snapshot. A trusted detail failure
also does not trigger an automatic fallback copy. This is a property of the
selection action, not the whole fallback session: fallback metadata and retained
preview paths can still create private snapshots. Fallback plot actions
(including Auto-plot) and fallback raw CSV actions can materialise a DataSet.

Both selection paths share generation invalidation, metadata capture, selected
ID updates, dataset release, and run-ID control synchronization. Their source
preflight and later mode-specific publication fences remain in each caller.

Trusted selection is owned by the derived-work bridge. Its detail publication
rechecks selection and source identity after each widget update. A per-attempt
token prevents an older reentrant callback from discarding a newer publication
or snapshot session; bounded restoration repairs rows, detail tabs, and preview
selection from the current accepted state.

Trusted Auto-plot owns a deduplicated queue independently of selected-detail
and refresh requests. It submits one bounded plot-metadata request at a time,
then uses the same asynchronous plot pipeline as manual plotting. Unavailable
descriptions are operation-local failures that leave the reader session usable.
Retries use bounded backoff; later run-metadata changes wake exhausted requests.
Disabling Auto-plot, committing a database switch, closing the database, and
shutdown cancel the queue. Source identity checks still guard publication.

Stage 5A's `trusted_work_scheduler.py` owns the exact derived priority key
`(run tier, work kind, stable run index)` without materialising one task per
run. The selected run receives metadata, thumbnail, and preview; visible runs
then drain metadata for all stable indices, thumbnails for all, and previews for
all; remaining runs follow the same three lanes. Stage 5B's
`trusted_work_coordinator.py` adds one controlled
worker and a one-result owner-thread marshalling boundary. It permits only one
claim in flight, performs cache and rendering work off the owner thread,
rejects stale exact claim identities before any callback or mutation, supports
append-only reconciliation and database/format/helper boundaries, and coalesces
changes to an active run until all work for its captured prefix can publish.
One absolute deadline covers cache lookup, broker work, rendering, validation,
cache write, and eviction. Recoverable broker pressure is retried with capped
backoff. Exact retry deadlines live in fixed three-slots-per-run arrays, so a
delayed claim neither hides an independent ready claim nor loses its own
deadline; one coalesced timer wakes the earliest retry. Cancelled and stale
outcomes are silent. Append reconciliation and reused-coordinator database
switches install the current selected and visible priority before the first new
claim is allowed to start.

The coordinator keeps continuation readiness/fairness in `_ContinuationState`
and retry attempts/deadlines in `_RetryState`. Reset and append paths share these
records without expanding compact per-run arrays into individual objects.

A progressive full-prefix verifier consumes at most one 4,096-row page per
owner turn and re-evaluates the exact base priority at every boundary.
Progressive metadata is a per-run dependency. Strictly higher-tier work and a
ready selected thumbnail or preview always win. Within the same tier, ordinary
work receives at most three claims (one sibling metadata/thumbnail/preview
sequence) before one paced continuation page is aged in. Eligible pages rotate,
and newly selected work pre-empts at the next boundary. Without a ready selected
image, bounded aging admits a remaining-run metadata claim within eight
foreground claims. A
dedicated 10 ms coalesced wakeup paces continuation progress without zero-delay
polling, and the schedule fully drains. An inconclusive page is parked as
metadata-only work: it produces no derived render, publication, or cache write,
and blocks only that run's image kinds. Once verification is conclusive, the
normal metadata claim renders, publishes, and attempts its cache write once;
the thumbnail and preview claims can then each do the same once. Thus the final
output is published exactly once rather than once per verification page.
Accepted state for a compatible append resumes at the previous physical cursor
instead of retaining or rereading the prefix; incompatible evidence fails
closed.

Stage 5C's `windows/_trusted_derived_qt.py` is the single Qt ownership
boundary. `MainWindow` owns one `TrustedDerivedQtBridge`, which in turn owns at
most one active coordinator. Worker and retry threads can request only one
coalesced queued wakeup. The bridge drains `coordinator.poll()`, validates the
exact database instance, service, generation, stable run identity, source
revision, helper incarnation, and work format, decodes bounded PNG bytes, and
mutates widgets only on the GUI thread. Selection and the actual sorted or
filtered viewport feed stable scheduler indices through one coalescing Qt
timer; neither scrolling nor selection rebuilds the coordinator. A committed
cheap run-list baseline is installed before coordinator binding, cache access,
derived extraction, rendering, or image decoding begins.

The bridge does not retain decoded images. `PreviewTab` is the sole retained
owner of full trusted previews and enforces independent 512-entry and 128 MiB
limits; its currently displayed pixmap may outlive an evicted cache entry only
as widget state. Run-list thumbnails are owned only by the fixed set of inline
preview cells, and databases above the inline-preview threshold skip thumbnail
decoding in Qt. Selecting an evicted preview uses the scheduler's exact
database/generation/run/kind replay operation, normally rehydrating from the
Stage 5B disk cache without clearing completed metadata or thumbnail state.
Changing selection can independently request ephemeral detail with selected
metadata.

The current selection's richer Raw key-value, Metadata, and Snapshot tabs use
one bounded ephemeral selected-detail read attached to selected metadata work.
It is fenced by database, helper, coordinator, source, selection generation,
run ID, and GUID, and is neither written to the derived cache nor retained for
another selection. Changing selection cancels or makes the old result stale.
Metadata and Raw presentation normalisation distinguishes structural omission
from local cell compaction. Their node, depth, container, aggregate-text,
upstream, and protocol omissions produce the global `[truncated]` marker. A
scalar that is present but too long for a cell instead receives a bounded path,
an explicit local marker, its exact UTF-8 byte count, and a non-error `[display]`
summary. Exception-like fields use a bounded head-and-tail cell representation.

Snapshot has a separate lazy model. The broker fetches at most the existing
4 MiB source limit through the pinned trusted reader, then a bounded iterative
scanner validates every token without constructing the complete Python object
tree. The Stage 5C bridge extracts the accepted source from the publication,
strips it from retained/Qt-facing detail, and keeps at most that one current
selection's source outside Qt. It is never written to the derived disk cache or
retained per run. Later page and value requests scan that in-memory source; they
do not open SQLite or weaken the invariant that main, WAL, and rollback-journal
handles are physically read-only and only SQLite's exact SHM coordination may
modify source-side state.

The initial Snapshot view and each expanded container contain only one page of
direct children. A continuing page has at most 127 data rows plus one
`Load more…` row, so the Qt parent has at most 128 newly materialised children;
a final page stays within the same 128-child ceiling. Every page independently caps displayed
text, including the control row, at 32 KiB. Containers receive children only
when expanded; the top-level QCoDeS `station` container is expanded by default
and requests only its first page. A shortened Snapshot scalar has its own exact
**View Full** request. A validated complete source is `available — loaded on demand`;
pagination is not truncation. `malformed`, `truncated`, or `unavailable` remains
reserved for genuine malformed input, source/detail size omission, excessive
depth or scalar size, changed-during-read, or comparable
acquisition/validation failure.

`windows/_snapshot_session.py` owns the retained raw Snapshot source, session
cancellation, and the dedicated single-worker executor for lazy pages and values.
It admits at most two submitted or queued requests in total. Its completion
callback only queues a result and requests a wakeup; the Qt bridge drains those
results, validates its current database/selection state, and updates widgets.
The session revalidates each result individually because publishing a previous
result can reenter the UI and change selection. Shutdown cancels the session
and closes the executor without waiting; liveness and diagnostics continue to
account for unfinished futures.

Every request and result is tied to the exact `DatabaseInstance`, bridge binding,
helper incarnation,
source revision, coordinator and selection generations, run ID and GUID,
Snapshot-session token, parent handle and continuation cursor, and deadline.
Those identities are checked before submission and again before owner-thread
publication. Selection changes, database switching or same-path replacement,
helper restart, source revision, timeout, and shutdown cancel or make results
stale. Only the GUI thread mutates widgets or creates images and dialogs.

One identity-deduplicating, 4 MiB registry retains exact strings referenced by
the Metadata and Raw views for the current accepted detail only. Qt tree items
hold bounded identifiers and paths, never the exact strings. One reusable
read-only dialog materialises a selected string on demand; clipboard mutation
occurs only through its Copy action. Selection, database, source revision,
coordinator generation, helper incarnation, same-path replacement, and shutdown
boundaries clear the registry, remove item identifiers, and discard any open
dialog before stale publication can be displayed. Raw expansion is restored by
bounded paths for the same run; Snapshot expansion follows its independently
bounded page protocol.
This path does not construct a QCoDeS `DataSet`, open a private snapshot, or
turn a trusted-session error into an automatic fallback copy.

`trusted_live_queries.py` and `trusted_live_service.py` expose the corresponding
fixed derived-source operation through the existing persistent broker and
helper. One repeatable-read batch captures the run GUID/table identity, current
schema, indexed physical `MAX(id)` watermark, and guarded run-description
facts. A structurally proved 2D layout uses at most 4,095 representative
primary-key rows spread across both complete logical axis extents, preserving
exact endpoints, small axes, mirror symmetry, orientation evidence, and each
dependent's physical row pattern. Sources without such a grid proof use the
bounded generic fallback of 15 primary-key windows of 256 rows plus the newest
256-row edge. The complete observation still has at most 33 columns (the `id`
plus 32 source columns), 4,096 retained rows, and 135,168 cells. There is no
OFFSET, complete result-table materialisation, or large-result
COUNT/DISTINCT/GROUP BY plan.

`trusted_derived_rendering.py` renderer version 3 converts that immutable
primitive observation to versioned metadata or deterministic PNG bytes without
Qt, Matplotlib, QCoDeS DataSets, pickle, cursors, or database handles. For 2D
data it renders declared dependency 0 vertically and dependency 1 horizontally,
independent of which axis changed fastest physically; reversed and serpentine
acquisition is restored to scientific orientation. Nearest representative
source-domain bins cover both full extents, missing and not-yet-measured cells
remain neutral, and finite cells use the embedded Viridis table. Dependents are
rendered independently, so a missing cell in one does not blank another;
thumbnail and preview sizes share the same scientific mapping. A singleton
finite 1D point is explicitly visible. The renderer caps dependents/images at
8, each dimension at 2,048 pixels, decoded image bytes at 16 MiB, encoded image
bytes at 8 MiB, and the retained payload at 16 MiB. Payload schema, graph, text,
binary, and image fields are validated before retention. Unsupported, empty,
malformed, non-finite/extreme numeric, or higher-dimensional sources become
bounded descriptions.

`_trusted_preview_raster.py` provides shared exact numeric normalization,
Viridis colors, pixel/line drawing, and 1D rendering for scalar and array
previews. Neither renderer imports the other for these primitives. Payload
validation and PNG encoding remain in `trusted_derived_rendering.py`.

`trusted_derived_cache.py` cache format version 3 stores only canonical,
type-tagged keyed entries under the absolute platform application-cache root
returned by `trusted_derived_cache_root()`. SHA-256 filenames and an internal
version, complete key, length, and payload checksum are verified before
decoding. The cache uses same-directory exclusive temporary files and atomic
replacement; faults are misses or uncached results. Defaults cap an entry at
16 MiB, the
cache at 256 MiB and 2,048 entries. An initial inventory above the 4,096-file
recovery window is not accepted as healthy: qPlot-owned remnants are purged
under the job deadline before insertion. Later insertions use an in-memory
index and heap instead of rescanning. POSIX roots and files are normalised to
0700 and 0600. A cache root inside, containing, or equal to the selected
database directory is disabled. Restart-unique service namespaces deliberately
prevent unproved cross-session hits.

`_trusted_cache_codec.py` owns canonical value/key encoding, deterministic
filenames, and bounded JSON preflight/decoding. It performs no file operations;
`trusted_derived_cache.py` retains cache locks, artifact ownership, index
transactions, publication, and cleanup.

`src/qplot/datahandling/readonly.py` centralises the retained snapshot path.
Use these helpers for explicitly deferred QCoDeS/dataset consumers and the
narrow snapshot fallback so qPlot does not initialise, upgrade, or write to a
loaded source. A quiescent rollback-format source uses direct `mode=ro` access
so SQLite retains its normal read locks. A checkpointed WAL-format source is
copied first and is opened with `immutable=1` only at that private path.

A WAL accepted by that retained snapshot policy, or any observed rollback
`-journal`, routes the snapshot consumer through a private copy. The main file
is copied before the accepted sidecars, and main-file identity, header, sidecar
identity, size, timestamps, and journal header/trailer state must remain stable
across bounded retries. An observed journal is copied even when it is a cold
PERSIST or zero-length TRUNCATE artifact. SQLite opens the private copy
read-write only long enough to perform any permitted WAL inspection or
rollback-journal recovery; the source is never opened in a mode that can
recover or change it. Ambiguous or continuously changing state fails closed,
and every private snapshot is removed after failure or when its owning
connection closes.

The Stage 2 trusted live reader uses SQLite's real WAL index and native locking
against the selected source without copying the database. Its native VFS keeps
the main database, WAL, and rollback journal physically read-only. SQLite may
mutate only the exact colocated `-shm` file as transient WAL coordination state,
so a live read can change that file's contents or metadata without writing
experimental data or checkpointing the database.

The reader proves source binding from retained proof handles and SQLite's actual
file handles, using device/inode identity on POSIX and volume/file identity on
Windows. It exposes only finite, materialised queries with bounded busy handling,
deadlines, cancellation, and verified transaction cleanup. Columns, rows,
reply-wide cells, scalars, and a batch-shared conservative wire budget are
checked while the live cursor advances and before each row is retained. A
verified operation-wide SQLite `SQLITE_LIMIT_LENGTH` first preserves the 4 MiB
absolute scalar ceiling. After statement metadata reveals `w` result columns,
the reader installs a stricter per-value ceiling before execution: the minimum
of that baseline, `floor(8 MiB / max(1, w))`, and the width-dependent term that
keeps a conservative logical Python-object/payload accounting envelope for the
ordinary APSW tuple/scalar row at or below 32 MiB after worst-case Unicode and
logical object overhead. This is not an allocator-reserved-byte, RSS, arena,
fragmentation, or SQLite VM/intermediate-memory bound. It closes the cursor and
restores and verifies the operation baseline after every statement, then
restores SQLite's original limit during final operation cleanup. A clean
`TrustedLiveResultLimitError` remains reusable only after both restorations,
rollback, and all other cleanup are proved. Limit lifecycle uncertainty retires
the reader and, in Stage 3, the exact helper incarnation; only unproved final
native resource release quarantines the direct-reader process from opening a
new session. These limits bound result materialisation and IPC, not arbitrary
internal SQLite virtual-machine memory use. The full derivation and access
policy are documented in
[Trusted live QCoDeS reader](trusted-live-reader.md).

Stage 3 places that reader behind
`qplot.datahandling.trusted_live_supervisor.TrustedLiveReaderSupervisor`. Each
supervisor owns one persistent helper process for one database and explicitly
uses multiprocessing `spawn` on every platform. The package-level helper target
constructs, queries, rolls back, and closes `TrustedLiveReader` on its main
thread. Its control thread may only signal generation-bound cancellation and
call the reader's cross-thread-safe `interrupt()` method.

Parent and child communicate through a bounded, explicitly versioned protocol
containing only validated primitive values. All application startup
configuration is carried in the same session-bound, generation-zero `startup`
frame; spawn bootstrap arguments are limited to the unavoidable pipe handles,
that bytes frame, and fixed private test plumbing. Every later request and reply
is bound to the helper incarnation and a monotonically increasing job
generation. Conservative aggregate wire budgets reject oversized text, blobs,
base64, and nested values before constructing amplified payloads, followed by
an exact final frame cap. A query-batch success is accepted only when it has one
result for every submitted statement. Strict generic JSON decoding rejects
duplicate keys, more than 4,500,000 aggregate collection items, and untagged
non-finite numbers, including exponent overflow. SQLite reals instead use an
explicit tagged canonical representation.

One persistent receiver thread per incarnation exclusively owns the raw reply
connection and publishes only complete bounded frames or terminal failures into
a one-slot inbox. This is necessary because `Connection.poll()` can report
a readable partial length header or body without making `recv_bytes()` safe to
call synchronously. Public startup, job, cancellation, shutdown, close, restart,
destructor, and `atexit` paths obtain reply data only from that inbox and
otherwise retain finite waits. Retirement closes endpoints, escalates through
terminate and kill when necessary, and boundedly joins both the helper process
and receiver. If either cannot be proved stopped, the incarnation is
quarantined and no replacement can start until later zero-time joins prove both
have exited.
Partial-frame timeouts therefore cannot reuse or replay the affected generation.

After polling, decoding, or process inspection outside the supervisor lock,
every reply path uses the same lock-held helper/job ownership check. Completed
or superseded jobs cannot publish into a newer owner; each path retains its
contextual error and existing cleanup behavior.

Cancellation is cooperative first and uses a monotonic, generation-scoped
control tombstone. An exact cancellation may arrive before its command or once
after that job completed without affecting a newer generation; stale,
duplicate, wrong-session, and out-of-order control frames fail closed. Each
child operation has its own finite reader deadline and each parent wait is
bounded. `close()` enters a closing state before releasing the supervisor lock,
so no new job or replacement spawn can race teardown while the captured active
job is cancelled and finished.

If cancellation grace expires, the supervisor performs that bounded retirement.
Bounded `atexit` cleanup retries retirement before multiprocessing shutdown;
daemonic process status is only a final interpreter-exit fallback. Crashes,
source replacement, protocol violations, and cleanup quarantine discard the
incarnation without silently replaying a query. Every replacement helper must
match the originally accepted main `DatabaseInstance`; after that check, the
public source identity is refreshed with the helper's current journal mode and
WAL/SHM identities.

Stage 4 attempts trusted startup after only path validation, identity capture,
and any necessary cloud-file hydration. A successful helper startup and basic
query are the direct-access proof; the legacy access probe is not run first.
Only a genuinely unavailable native backend or an explicitly unsupported
source or filesystem is eligible for snapshot fallback, and fallback still
requires the legacy probe to establish a safe snapshot view. Cancellation,
deadline or busy expiry, source replacement, SQL/query/result-limit failure,
invalid database or source I/O, helper exit, protocol failure, forced
termination, and cleanup quarantine are terminal for that trusted attempt and
are never silently retried, replayed, or converted to a snapshot.

Basic runs are published before any result table is inspected. Pagination
captures a bootstrap maximum-`run_id` watermark; the first incremental refresh
reconciles commits made while those pages were being read. The same persistent
helper supplies `PRAGMA data_version`. An unchanged value avoids redundant
metadata work; a changed value fetches only later run IDs and refreshes only
the selected run plus watched unfinished runs, with visible watched rows kept
ahead of the remaining watched rows. A new helper
incarnation establishes a new baseline and triggers explicit targeted
reconciliation instead of comparing unrelated `data_version` values. Sampling
the incarnation on both sides of the probe also catches replacement during the
`data_version` submission itself.

Loading a second database uses separate pending and active brokers. The active
database remains committed until the pending database finishes successfully.
Failure, cancellation, or a stale callback retires the pending broker; success
revalidates and promotes it atomically before retiring the old one. Retired
brokers remain owned until asynchronous shutdown proves their query/control
threads, helper process, receiver, pipes, and requests have all finished. A
100 ms zero-wait reaper runs only while retired brokers remain. Application
shutdown repeats cancellation/cleanup once and uses one 15-second monotonic
overall deadline. The final 250 ms diagnostic grace is inside, not after, that
bound. A lightweight launcher starts a bounded absolute startup interval before
setup and installs its termination-signal guards before process creation. The
`qplot` and `python -m qplot` command-line process serves directly as that
launcher. Public `qplot.run()` first starts a dedicated Python launcher, making
that process—not the API caller—the sole GUI parent, reaper, and containment
owner. On POSIX it creates the GUI in a new session and dedicated process group,
retaining the unreaped group leader as its identity anchor. On Windows it creates a retained
kill-on-close Job Object and uses the process-creation attribute list to assign
the suspended GUI to that job atomically, before resuming its initial thread.
It keeps the original process and job handles. Both mechanisms establish
whole-qPlot-tree kill authority before the GUI can import Qt or spawn a reader
helper. A QCoDeS writer or test sentinel started outside that group or job is
outside the boundary and is never a termination target.

Before the public launcher can create the GUI, it authenticates a private
loopback result channel to its API caller within one bounded absolute startup
interval. Only that launcher retains the descendant-side endpoint: the GUI and
reader helpers cannot inherit it. The launcher sends a bounded authenticated
outcome only after its existing supervision has proved the complete GUI tree
gone, then exits without closing the endpoint early. Consequently EOF proves
launcher death even when a caller-owned `waitpid(-1, WNOHANG)` thread collected
its status first; result delivery does not depend on obtaining that wait status.
Normal and forced statuses pass through unchanged. A POSIX signal is returned
to the API caller non-destructively as `-signal_number`; only command-line or
dedicated launcher processes may reproduce a signal or use a hard exit. Channel,
protocol, or launcher failure returns 70 with retained diagnostics and never
signals or hard-exits the API caller.

The private API channel is full-duplex with exactly one reader in each
direction. A caller-side result reader owns all launcher framing, so a main
thread `KeyboardInterrupt`, `SystemExit`, or other control-flow exception
cannot corrupt a partially received outcome. The caller sends one fixed,
authenticated cancellation record through a dedicated sole writer. That writer
has a serialized `NOT_STARTED`/`STARTING`/`RUNNING`/`EOF_FALLBACK`/`COMPLETE`
lifecycle, so concurrent cleanup and result-reader requests can create one
thread object and attempt its start only once. A failed create/start transition
commits one synchronous EOF owner. The owner identity, creation attempt, worker
object, and start attempt are durable before their respective transitions, so
an interruption after a commit is resumed by that same requester while peers
wait. A worker also handshakes through the lifecycle condition before socket
I/O, preventing a late uncertain start from racing the committed EOF fallback.
Neither lifecycle lock is held during bounded socket I/O; the writer records
progress before any interruptible post-send work and resolves to either the
complete frame or irreversible write-side EOF. The
caller retains the exact first exception while later interruptions are absorbed
through cancellation, outcome/EOF processing, launcher exit/reap observation,
channel closure, and diagnostic publication. Its temporary SIGINT guard keeps a
stable absorber reference, captures the caller's original handler exactly once,
and separately verifies installation and restoration around interrupted
`signal.signal` side effects. A launcher-side reader
commits that cancellation, or authenticated-channel EOF when the caller
disappears, into an immutable event. The existing supervision owner then makes
whole-group or whole-Job termination its next external action. It sends the
final outcome only after the tree is gone and keeps the endpoint open until
launcher death; only after authenticated outcome, EOF, and exit/reap ownership
are all observed does the API caller re-raise its original exception.
Malformed, duplicate, unauthenticated, or late records cannot disarm or extend
an existing GUI deadline. Before API-launcher `READY`, the caller closes the
listener and relies on the launcher's bounded bootstrap self-exit; POSIX never
targets a potentially stale numeric PID after a foreign reaper may have
collected it.

Only after containment and the child's authenticated `HELLO` does the launcher
send `READY`. Setup, spawn, containment, authentication, never-`HELLO`, and
other failures before `READY` retain exact diagnostics and terminate and reap
the contained tree within the startup bound. Once `READY` has been committed,
EOF or malformed traffic before `ARM` is observe-only: the launcher keeps
ownership and waits for the GUI's real status instead of killing it. After quit
confirmation the GUI sends one nonce-authenticated `ARM` carrying the already
fixed absolute deadline. The launcher irrevocably stores the first finite
authenticated deadline immediately after decoding it, before allocating or
encoding the acknowledgement. There is no `DISARM` transition. A failed
acknowledgement, EOF, and invalid or duplicate later traffic cannot cancel or
extend the deadline. Exact startup, protocol, termination, and reap diagnostics
remain separate from, and do not replace, the newest resource-liveness scan.

At the hard deadline the launcher first terminates the contained process tree;
it performs no logging, snapshot I/O, cleanup, or resource-liveness work before
that kill attempt. POSIX operations address only the dedicated process group,
retry group signals while the unreaped leader still anchors every positive
signal decision, and then reap its exact status. After that reap the launcher
uses signal-zero observation only until the killed group is absent. Windows operations use
only the retained job and process handles, retry failed termination or
observation, and do not close them until the direct process is signalled and the
job is empty. Ownership is not silently released while the tree may still be
live. Diagnostic publication remains independent of tree termination. If the
launcher receives an ordinary POSIX termination signal, it first cleans and
reaps the group and then terminates itself with that same signal so its real
shell-visible signal status is preserved.

The launcher remains armed after the Qt event loop returns and throughout
potentially blocking destruction of a freshly proven-quiescent main window, its
owned `QThreadPool` objects, and the application. A final non-quiescent scan or
exhausted deadline instead reaches the process-boundary wait before any such Qt
destructor. The GUI also keeps a same-deadline in-process `_exit` backstop, but
the independent launcher remains the guarantee when native Qt code holds the
Python GIL. Exceptional termination lets the OS release read-only
database/WAL/journal handles and permitted SHM coordination state.

`qplot.run(return_objects=True)` deliberately bypasses the dedicated launcher and runs in
the caller's process so it can return the `QApplication` and `MainWindow`. The
caller owns those objects and their cleanup; this mode does not promise the
launcher's whole-tree containment or process hard deadline.

Trusted live load, automatic default selection, scrolling, and metadata
completion do not start snapshot-backed detail, preview, or thumbnail workers.
The Stage 5C bridge makes the Stage 5B coordinator the sole trusted producer of
derived metadata, run-table thumbnails, and selected-run preview cards, and the
selected run's richer tabs arrive through a bounded ephemeral detail read.
Cache hits and freshly rendered payloads use the same transactional publication
path; unsupported runs receive bounded nonfatal unavailable cards. The derived
lanes use `(run tier, work kind, stable index)`: selected metadata (with
detail), thumbnail, and preview; then visible metadata/thumbnail/preview lanes;
then the equivalent remaining lanes. Explicit trusted plots use connection-free `TrustedPlotDataset` metadata and
broker-owned prefix capture or numeric aggregation. Trusted raw CSV uses the
same prefix capture, while fallback CSV and plot actions remain snapshot
consumers. All address the selected GUID and exact
`DatabaseInstance`; snapshot fallback sessions may keep their existing preview
behavior. Trusted selection never creates an automatic
fallback copy.

Database switching disarms publications before the old coordinator is made
obsolete, and old queued Qt events are harmless under the bridge's identity and
generation checks. Appends reconcile stable run slots and source/completion
changes are coalesced. Preview-size changes invalidate previews only. Helper
incarnation changes invalidate the complete old generation. Shutdown first
disarms wakeups and publications, then retires coordinators asynchronously into
the existing bounded Stage 4 shutdown scan; it adds no watchdog or GUI-thread
join.

Reselecting the exact active database is an idempotent refresh of that bridge.
It preserves the coordinator, its two fixed timers, decoded preview cache, and
trusted widget binding while reconciling any newly appended stable runs. The
snapshot fallback fast path remains separate and unchanged.

The WAL-provenance discussion below describes the retained snapshot path used
by fallback and explicitly deferred consumers, not the trusted reader's native
SQLite transaction view.

SQLite's WAL format does not provide main-file provenance. Its header records
the WAL format, page size, checkpoint sequence, salts, and checksums; frame
headers repeat the salts and cumulatively checksum their page data. Those
fields bind frames to that WAL header, not to a unique main database. The main
header has no reciprocal WAL identifier, and the ordinary latest QCoDeS schema
has no database-level lineage token. Consequently, path agreement, directory,
timestamps, inode or platform file identity, compatible pages, and SQLite's
willingness to open a copied pair are continuity or consistency observations,
not proof of lineage. An arbitrary first-observed ordinary WAL containing
frames cannot be paired reliably and raises `UnverifiableDatabaseWalError`. A
stably observed zero-byte WAL has no frames to associate and is omitted from
the private view.

Generated databases provide the missing provenance explicitly. Alongside the
unique generation token they store a bounded ring of 256-bit nonce events;
each event names its parent and the singleton state records the exact chain
head. On private copies qPlot requires the combined main-and-WAL head to be a
strict, contiguous descendant of the main-only head. A divergent clone can
carry the same token and a numerically higher epoch, but it cannot reproduce
the selected branch's random head, so it is rejected. The 65,536-event ring
bounds storage; exceeding the retained proof window fails closed with owner
checkpoint guidance.

qPlot also checksum-scans the valid committed prefix of the private WAL and
requires every committed transaction to contain the lineage-state root page.
This prevents a later instrumented commit from retroactively authenticating an
earlier uninstrumented transaction in the same WAL. Invalid or unsupported WAL
structure and any commit without that page fail closed; parsing and SQLite
recovery still occur only on private copies.

Generation installs deterministic lineage triggers on every initial table.
SQLite has no database-wide DML or DDL trigger, so a future QCoDeS writer must
explicitly call `enable_generation_provenance_for_writer()` on a quiescent
`AtomicConnection` before any new experiment or measurement writes. Its
outer-commit hook discovers new tables, installs their triggers, and records a
lineage event in the same creation transaction. Persisted row triggers cover
foreground and background result connections. The hook refuses a nonempty WAL
so it cannot retroactively authenticate frames written before coverage. It
acquires SQLite's writer lock before checking WAL quiescence, closing the race
with a concurrent writer. None of this migration or trigger work occurs in the
viewer.

An equal-epoch result-page WAL cannot be authenticated after the fact. Its WAL
frame contains neither the generation token nor a reciprocal main-file
identifier; an independent database can produce the same page transition.
Append-only checks, integrity checks, root-page ownership, and successful
SQLite replay are therefore not substituted for provenance. Such a state fails
closed with owner-checkpoint and writer-integration guidance. A main-only older
generated database remains readable, but its epoch-only live WAL cannot prove
branch ancestry and is rejected. On a quiescent rollback-journal owner
connection the writer hook can explicitly migrate that database, rotate its
token, seed a fresh chain root, and install current triggers. The viewer never
performs that migration.

Process-local replacement quarantine is negative safety evidence only: it can
prevent an unpaired WAL from being consumed after an observed main-file
replacement, but it cannot establish that an unknown WAL belongs to the
replacement. Ordinary QCoDeS input becomes readable after all owning
connections close cleanly and SQLite checkpoints the WAL, or after the owning
writer performs that checkpoint. qPlot never performs a source checkpoint
itself.

Deferred direct SQLite reads, QCoDeS `AtomicConnection` reads, and dataset
loading go through the snapshot policy. Trusted run-list, refresh, and metadata
work goes through the Stage 4 broker and fixed-query layer. Never open an input
database with SQLite or QCoDeS directly from viewer or widget code.

`src/qplot/datahandling/LoadFromDB.py` adapts QCoDeS database loading for
threaded refreshes.

`src/qplot/datahandling/qcodes_cache.py` is the compatibility boundary for
QCoDeS cache internals used by per-parameter refreshes. Prefer adding cache
private-attribute access there instead of spreading it through GUI modules.
QCoDeS upgrades should be checked against this module first: the rest of qPlot
should call helpers such as `cache_data`, `cache_rundescriber`, and
`set_parameter_complete` instead of reaching into `_data`, `_dataset`, or
`_complete` directly.

`src/qplot/_testdata_specifications.py` owns the synthetic-data CSV schema,
specification records, and parsing. Named declaration records carry source row
numbers through forward-reference resolution. The parser imports no QCoDeS or
GUI modules and probes SQL identifiers only in an in-memory SQLite database.

`src/qplot/testdata.py` retains the public specification imports,
exports the cumulative instruction collection packaged under
`src/qplot/resources/testdata`, and writes synthetic QCoDeS databases for
testing. It backs the
`qplot-generate-db` command and remains separate from qPlot's enforced
read-only database-loading path.

`_testdata_samples.py` calculates bounded synthetic chunks and seeded sinusoid
components using NumPy and specification records. `testdata.py` retains chunk
iteration, cancellation, QCoDeS measurement writes, and destination publication.

`src/qplot/tools/worker.py` defines the background loader used by plot windows.
It loads data, reshapes it for the plot type, applies selected operations, and
emits results back to the GUI thread. Plot workers use cooperative cancellation:
shutdown marks every registered worker as cancelled, interrupts its active
read-only SQLite connection, clears work that has not started, and keeps the Qt
event loop alive until running workers unwind. Built-in operations receive a
cancellation callback and long Python loops check it periodically. Existing
third-party operation callables remain compatible with the one-argument
``operation(data_dict)`` contract; a callable that does not cooperate cannot be
stopped during that individual Python call, so cancellation takes effect as soon
as it returns. Threads are never terminated forcibly, and cancelled results are
not applied to plot windows.

The worker's internal heatmap spatial summary names its matching-row count and
coordinate bounds/cardinalities, keeping the SQL result contract explicit.

`tools/plot_result.py` defines the successful `PlotLoadResult`, its source
identity, and optional cache update. The worker snapshots output mappings once
processing succeeds; arrays keep their existing buffers. GUI publication reads
this result while the worker retains cancellation and progress state. Existing
source fences and publication rollback still govern acceptance.

`datahandling/LoadFromDB.py` prepares shaped and unshaped cache merges through
separate helpers. A `_PreparedMerge` record keeps columns, insertion shape, and
any planned-overflow sample count together. Shaped preparation owns private
C-contiguous arrays; precision promotion and cancellation finish before the
worker publishes its result. Database row counts remain separate from acquired
sample counts.

`src/qplot/tools/general.py` and `plot_tools.py` contain small data helpers and
plot operation functions. `src/qplot/tools/operation_registry.py` maps those
operation functions to the plot-window surfaces and input controls that expose
them.

`datahandling/numeric_samples.py` shares precision-preserving dtype selection,
concatenation, and missing-value classification without importing QCoDeS.
`parameter_data.py` retains acquisition-record decoding and database integration.

## Configuration

`src/qplot/configuration/config.py` loads, validates, updates, and resets
`~/.qplot/config.json` using `config_schema.json`.
Validated user edits and resets share `_commit_config`, which restores the
previous in-memory state if persistence fails. Startup recovery deliberately
keeps usable defaults even when they cannot be saved.

`src/qplot/configuration/scripts.py` backs the `qplot-cfg` command-line helper.
Dispatch and help share an explicit command registry. Adding a public method
does not expose a new command, and help/version do not load user settings.

`windows/_signal_blocking.py` provides scoped suppression for synchronous
control updates. It restores every control's previous state in reverse order,
including nested scopes and failed updates. The config setter, preferences
reload, menu synchronization, and picker population share this helper;
database publication and shutdown retain their separate lifecycle gates.

`src/qplot/windows/_preferences.py` exposes the common config keys through the
main-window preferences dialog and emits a signal when applied settings need to
be synced into the open UI. One keyed binding registry drives value conversion,
loading, reading, and edit tracking, including explicit edits to rounded values.

Theme files live in `src/qplot/configuration/themes`. The shared stylesheet
builder and plot-item helpers live in `themes/_base.py`; light, dark, and PyQt
themes should provide palettes or small overrides instead of duplicating full
QSS blocks.

The user-facing key reference and contributor checklist for config changes live
in `docs/configuration.md`.

## Tests

Tests are grouped by area:

* `tests/datahandling` covers database metadata helpers.
* `tests/widgets` covers main-window widgets and preview behavior.
* `tests/windows` covers main-window and plot-window behavior.
* `tests/test_config.py` and `tests/test_tools.py` cover configuration and
  general helper behavior.

Shared pytest and Qt setup lives in `tests/conftest.py`.
Integration isolation is the default: Qt initialization, QCoDeS location
restoration, and snapshot-connection leak auditing apply to every unmarked test.
Only audited `pure` tests bypass that setup; their fixtures import neither Qt
nor QCoDeS. Subprocess policy tests exercise both sides of this boundary.
