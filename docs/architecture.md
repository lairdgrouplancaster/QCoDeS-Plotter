# Architecture

This document is a working map of the current codebase. It is intentionally
short: update it when module responsibilities move.

## Entry Points

`src/qplot/__main__.py` defines the `qplot` command and `qplot.run()`. It creates
the `QApplication`, opens `MainWindow`, and starts the Qt event loop.

`src/qplot/__init__.py` exposes the public package imports used by scripts and
interactive users.

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

`src/qplot/windows/_plot_actions.py` contains the plot-facing main window
actions. It handles:

* opening 1D and 2D plot windows
* exporting measurement data
* preview plot/export actions
* adding compatible preview traces to existing 1D plots
* tracking datasets currently used by plot windows

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

`src/qplot/windows/_commands.py` is the shared command and shortcut registry.
Use it when adding menu actions, context-menu actions, keyboard shortcuts, or
shortcut-help rows so labels, status tips, tooltips, and in-app shortcut help
stay in sync.

## Plot Windows

`src/qplot/windows/_plotWin.py` is the shared base for plot windows. It owns
common plotting behavior such as refresh timers, axis selection controls,
context menus, plot-area resizing, and operation panels.

`src/qplot/windows/_plot_refresh.py` contains shared worker-backed plot refresh
orchestration: deciding whether to read from the database or cached data,
starting workers, applying worker results back to plot-window state, and
surfacing worker failures.

`src/qplot/windows/_dataset_handle.py` defines the small `DatasetHandle`
structure used by the main window and plot windows to track an open dataset,
its active plot-window user count, and any delayed-release timer.

`src/qplot/windows/_plot_export.py` contains shared plot output behavior:
native printing, pyqtgraph export-dialog setup, PDF rendering, clipboard image
copies, high-DPI copies, and SVG clipboard output.

`src/qplot/windows/_plot_feedback.py` contains shared plot-window status,
state-overlay, error-dialog, and shortcut helpers. Keep common plot-window user
feedback there instead of adding more status or message-box methods to
`_plotWin.py`.

`src/qplot/windows/_plot_axis_scaling.py` contains the shared plot-axis scaling
mixin and the custom axis item used for power-of-ten unit labels. It owns the
X/Y axis scaling dialogs opened by double-clicking plot axes.

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

`src/qplot/windows/plot2d.py` extends the shared plot window for heatmaps. It
owns heatmap rendering, hover pixel display, and marquee color scaling.

`src/qplot/windows/_plot2d_layers.py` owns secondary heatmap layers added by
preview drag-and-drop, including per-layer renderers and opacity controls,
hidden source-window refresh ownership, shared-colorbar membership, and layer
cleanup.

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
thumbnails. It also handles preview selection, drag payloads, and background
preview generation.

`src/qplot/windows/_widgets/operations.py` defines the operation panel widgets
that collect user-selected data operations before refresh processing.

`src/qplot/windows/_widgets/dropbox.py` and `toolbar.py` contain smaller
reusable UI controls used inside plot windows.

## Data Loading

`src/qplot/datahandling/readSQL.py` reads run metadata directly from the current
QCoDeS SQLite database. It also computes summary fields used by the run table,
including status, point counts, and storage size estimates.

`src/qplot/datahandling/database.py` contains database-file access helpers,
cloud-storage hydration, background main-window load workers, and database
diagnostic report generation.

`src/qplot/datahandling/readonly.py` centralises enforced read-only database
access. Use these helpers for QCoDeS and direct SQLite connections so qPlot does
not initialise, upgrade, or write to loaded QCoDeS databases. A quiescent
rollback-format source uses direct `mode=ro` access so SQLite retains its normal
read locks. A checkpointed WAL-format source is copied first and is opened with
`immutable=1` only at that private path.

An accepted WAL or any observed rollback `-journal` routes access through a
private snapshot. The main file is copied before the accepted sidecars, and
main-file identity, header, sidecar identity, size, timestamps, and journal
header/trailer state must remain stable across bounded retries. An observed
journal is copied even when it is a cold PERSIST or zero-length TRUNCATE
artifact. SQLite opens the private copy read-write only long enough to perform
any permitted WAL inspection or rollback-journal recovery; the source is never
opened in a mode that can recover or change it. Ambiguous or continuously
changing state fails closed, and every private snapshot is removed after
failure or when its owning connection closes.

The non-default Stage 2 trusted live reader uses SQLite's real WAL index and
native locking against the selected source without copying the database. Its
native VFS keeps the main database, WAL, and rollback journal physically
read-only. SQLite may mutate only the exact colocated `-shm` file as transient
WAL coordination state, so a live read can change that file's contents or
metadata without writing experimental data or checkpointing the database.

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

Application loading, preview, plotting, and refresh continue to use the snapshot
path above; UI scheduling and integration remain later stages. The WAL-provenance
discussion below describes that snapshot path, not the trusted reader's native
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

Direct SQLite reads, QCoDeS `AtomicConnection` reads, dataset loading, refresh
workers, metadata inspection, and the subprocess access probe all go through
this policy. Never open an input database with SQLite or QCoDeS directly from
viewer code.

`src/qplot/datahandling/LoadFromDB.py` adapts QCoDeS database loading for
threaded refreshes.

`src/qplot/datahandling/qcodes_cache.py` is the compatibility boundary for
QCoDeS cache internals used by per-parameter refreshes. Prefer adding cache
private-attribute access there instead of spreading it through GUI modules.
QCoDeS upgrades should be checked against this module first: the rest of qPlot
should call helpers such as `cache_data`, `cache_rundescriber`, and
`set_parameter_complete` instead of reaching into `_data`, `_dataset`, or
`_complete` directly.

`src/qplot/testdata.py` validates spreadsheet-friendly CSV specifications,
exports the cumulative instruction collection packaged under
`src/qplot/resources/testdata`, and writes synthetic QCoDeS databases for
testing. It backs the
`qplot-generate-db` command and remains separate from qPlot's enforced
read-only database-loading path.

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

`src/qplot/tools/general.py` and `plot_tools.py` contain small data helpers and
plot operation functions. `src/qplot/tools/operation_registry.py` maps those
operation functions to the plot-window surfaces and input controls that expose
them.

## Configuration

`src/qplot/configuration/config.py` loads, validates, updates, and resets
`~/.qplot/config.json` using `config_schema.json`.

`src/qplot/configuration/scripts.py` backs the `qplot-cfg` command-line helper.

`src/qplot/windows/_preferences.py` exposes the common config keys through the
main-window preferences dialog and emits a signal when applied settings need to
be synced into the open UI.

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
