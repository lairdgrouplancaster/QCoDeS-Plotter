# Changelog

All notable user-facing changes are recorded here.

This project currently releases from GitHub source tags rather than PyPI. For
installation commands and release validation, see `docs/distribution.md`.

## Unreleased

### Added

- Add a complete trusted live-reader boundary that uses SQLite's
  real WAL index and native locking without copying the selected QCoDeS
  database. Main, WAL, and rollback-journal handles remain physically read-only;
  only SQLite's exact colocated SHM coordination file may be updated. Operations
  are bounded, cancellable, and identity-bound. The existing snapshot reader is
  retained for the narrow Stage 4 fallback and explicitly deferred consumers.
- Isolate the trusted live reader in a persistent, explicitly spawned helper
  process. Startup and job IPC is bounded and versioned, with conservative
  allocation preflight and exact query-batch result cardinality. Monotonic
  generation-bound cancellation, a closing gate, bounded exit cleanup with a
  daemon fallback, unreaped-process quarantine, and source-bound fresh-process
  recovery prevent stale or failed work from leaking into a later job. Stage 4
  consumes this non-Qt infrastructure without weakening its failure taxonomy.
- Add a Qt-independent application read broker that owns one persistent trusted
  helper for an accepted database, serialises bounded fixed-query work, and
  publishes the basic run list before progressively filling metadata. Selected
  runs are promoted ahead of visible rows and the remaining table, later commits
  are detected through the same helper, and cancellation, database switching,
  and shutdown stay off the GUI thread.
- Make database loading, run-list refresh, progressive run metadata, and the
  selected run's plain detail view use trusted live access first. The legacy
  access probe and snapshot fallback are used only when the native backend is
  genuinely unavailable or the source or filesystem is explicitly unsupported;
  trusted-session failures are never converted into fallback or silently
  replayed.
- Remove eager QCoDeS DataSet loading from ordinary selection in every access
  mode. Snapshot-fallback ordinary selection is basic-only and starts no
  selected-detail worker or additional selected-detail snapshot; rich fallback
  detail remains deferred. Fallback metadata and retained preview paths can
  still create private snapshots. DataSets remain exclusive to explicit plot
  and CSV actions.
- Add the Stage 5B trusted derived-work backend. A Qt-independent single-claim
  coordinator executes the Stage 5A selected/visible/remaining schedule,
  captures immutable result prefixes through the persistent trusted helper,
  renders bounded deterministic metadata and PNG payloads, and uses a verified,
  atomic, size-bounded application cache. Active appends are coalesced so a
  captured prefix can publish before its newer revision is scheduled.
- Keep Stage 5B disconnected from the Qt presentation layer. Trusted live
  loading, selection, scrolling, and metadata completion still show the Stage 4
  preview and thumbnail placeholders; explicit plot and CSV actions remain
  action-owned snapshot consumers. Stage 5C will decode and route the backend
  payloads to those widgets.
- Build the native boundary in explicit C11 mode with MSVC and exercise installed
  reader wheels separately on ARM64 macOS, Intel macOS, Linux, and unprivileged
  Windows CI hosts before cross-platform acceptance.

### Fixed

- Decode and normalise selected-run snapshots on the broker thread before Qt
  publication. The published flat view caps UTF-8 input, nesting, container
  items, nodes, and rendered text; malformed or truncated snapshots show bounded
  diagnostics without placing raw multi-megabyte JSON in cells or tooltips.
- Replace dynamic selected/visible run priorities instead of accumulating stale
  scores, restore omitted rows to stable table order, and linearise trusted
  request installation with concurrent promotion.
- Remove the broker callback facility and callback thread, terminally complete
  active as well as queued requests after fatal loop failure, reap retired
  services periodically with zero-wait polls, and cap application shutdown with
  one 15-second monotonic deadline. Before Qt can import or create descendants,
  a Qt-free launcher contains the complete qPlot tree in a dedicated POSIX
  session/process group led by its retained unreaped child, or in a retained
  Windows kill-on-close Job Object assigned atomically at process creation.
  Signal guards and the bounded startup interval begin before spawn; failures
  before authenticated `READY`, including a child that never sends `HELLO`,
  tear down the contained tree. After `READY`, malformed traffic or EOF before
  `ARM` is observe-only. The launcher commits the first authenticated finite
  deadline before constructing its acknowledgement, has no `DISARM` transition,
  and remains armed across Qt event-loop return and quiescent owned-thread-pool
  destruction. Deadline or non-quiescent shutdown bypasses blocking Qt
  destruction; diagnostic persistence is independent, while last-resort
  termination retries whole-tree kill and retained-leader reap without releasing
  live containment. POSIX launcher signals retain their real signal status, and
  QCoDeS writers or sentinels started outside qPlot's group or job remain
  untouched. Public `qplot.run()` now starts a dedicated Python launcher so an
  acquisition caller and its `waitpid(-1)` threads cannot reap the GUI or be
  killed by forced qPlot shutdown. A private authenticated, bounded result
  channel reports only after the GUI tree is gone and remains independent of
  obtaining the launcher's wait status; forced shutdown returns 70 and POSIX
  signals return non-destructively as negative signal numbers. Protocol or
  launcher failure returns 70 without hard-exiting the caller.
  Caller-side `KeyboardInterrupt`, `SystemExit`, and other control-flow
  exceptions now commit an authenticated cancellation, wait through repeated
  interruption until the GUI/helper tree and dedicated launcher are gone, and
  then re-raise the exact first exception. Cancellation has one dedicated
  writer with a serialized one-time create/start lifecycle. Its owner, creation
  attempt, worker assignment, and start attempt remain durable across an
  interruption immediately after any commit; an uncertain committed start is
  never retried and resolves through proven worker entry or the unique EOF
  fallback. The writer persists partial-send progress and resolves to either the
  complete authenticated record or one committed irreversible write-side EOF.
  Concurrent cleanup and result-reader requests therefore share the same worker
  or fallback. The temporary repeated-
  SIGINT guard captures the caller's original handler once and transactionally
  verifies installation and restoration even when interruption follows the real
  `signal.signal` side effect. No later exception may escape during cancellation
  setup, result/EOF processing, launcher waiting, channel closure, or diagnostic
  publication. Caller-channel EOF also terminates the contained tree. Pre-`READY`
  POSIX cleanup no longer signals an
  unverified PID after a foreign reaper may have collected the launcher.
  `run(return_objects=True)` remains deliberately in-process and
  caller-owned, without the launcher's containment or hard-deadline guarantee.
- Replace 32 GiB filesystem test extensions with logical payload/stat proxies
  and small physical fixtures, retaining a bounded 64 MiB native no-copy
  integration case on every platform.
- Keep Stage 4 expensive metadata from holding a reader transaction across a
  large result-table or `dbstat` scan. Current QCoDeS result counts use the
  append-only integer-primary-key watermark; only twice-stable small sources
  may use bounded aggregate batches, while large/changing sources use planned
  shapes, fixed ID-window summaries, and explicitly estimated storage.
- Keep Stage 4 metadata plans intrinsically inside the helper's wire and public
  result budgets: defer large run descriptions from 1,000-row basic pages,
  preflight and guard per-run scalars, cap selected layouts and grouped edge
  summaries, and report oversized omitted fields explicitly. Refresh now
  samples helper incarnation on both sides of `data_version`, so an idle-helper
  replacement with the same numeric version still forces schema and watermark
  reconciliation.
- Keep every spawned-helper reply path deadline-bounded when a peer sends only
  part of a multiprocessing frame. One persistent receiver per incarnation now
  owns raw reply reads and feeds a one-slot inbox; timed-out startup, job, and
  shutdown frames retire the exact process and receiver without replay, while
  unreaped resources remain quarantined.
- Enforce trusted-query column, row, reply-wide cell, scalar, and shared batch
  wire limits while consuming the live SQLite cursor. A temporary verified
  SQLite length baseline preserves the 4 MiB absolute scalar ceiling, while a
  width-derived per-statement limit caps aggregate raw row payload at 8 MiB and
  a conservative logical Python-object/payload accounting envelope for the
  ordinary APSW tuple/scalar row at 32 MiB before it is yielded. Allocator
  rounding and reservation, RSS, fragmentation, arenas, and SQLite VM memory
  are explicitly outside that logical bound. Clean result-limit failures remain
  reusable only after rollback and both limit restorations are proved;
  installation, verification, or restoration uncertainty aborts the batch and
  retires the helper incarnation.
- Reject exponent-overflow and other untagged non-finite JSON numbers at the
  generic IPC boundary, with regressions for duplicate keys and aggregate
  collection limits while preserving tagged SQLite-real round trips.

## 1.6.0-b1 - 2026-08-18

### Added

- Print the visible plot area through the system print dialog, including
  page-formatted PDFs when it exposes a concrete PDF destination. PDF file
  output is staged and atomically published; Save Plot as PDF remains the
  plot-sized output path.
- Make every run-table column optional and persistent from the header menu,
  including Experiment, Sample, Name, Completed, and GUID, with horizontal
  scrolling for wider layouts.

### Changed

- Replace legacy settings upgrades with one strict configuration format for
  the new major version. Older or incomplete settings files are backed up and
  reset to current defaults, and the recent-database list is now the single
  source for restoring the last opened database.

### Fixed

- Bind generated-database WAL provenance to the exact checkpointed branch with
  a bounded parent-linked nonce chain. Provenance-aware QCoDeS writers cover
  later result tables, background writes, repeated checkpoints, and fresh qPlot
  processes, while equal-lineage WALs and higher-counter divergent clone WALs
  remain fail-closed. Every committed private-WAL transaction must carry its
  lineage-state page, so a later valid commit cannot bless earlier unknown
  frames, and writer enablement checks WAL quiescence while holding the SQLite
  writer lock.
- Refuse a first-observed nonempty ordinary SQLite WAL when its association
  with the selected QCoDeS main database cannot be proved. qPlot-generated
  databases remain readable with a live WAL only when its retained chain
  descends from the selected main; all inspection and recovery stays on private
  copies without checkpointing or changing input files.

## 1.5.1-b2 - 2026-08-13

### Changed

- Show percentage progress and clearer per-run status while generating test
  databases, including interrupted and failed runs.
- Preserve the selected run and its details while database refreshes are
  staged, updating the view only after the refreshed state is ready.
- Validate source distributions by running their full test suite in an
  isolated environment, and smoke-test installed wheels during package CI.

### Fixed

- Keep every input QCoDeS database strictly read-only, including when reading
  live WAL-backed data, so viewing cannot create, modify, publish, or remove
  SQLite `-wal`, `-shm`, or `-journal` sidecar files.
- Publish generated test databases atomically without exposing stale SQLite
  sidecars, and restore the original database and sidecars if replacement
  fails.
- Safely replace a generated database that is already loaded, including
  through a symlink, while preserving unrelated open datasets and refreshing
  plots against the replacement.
- Prevent preview CSV exports and overwrite confirmations from acting on a
  database that was replaced while the export was being prepared.
- Prevent database reads from racing with same-path test-database generation.
- Synchronize completion across plots sharing a live run, including runs that
  finish without appending another data row.
- Treat zero-row datasets as valid selections and avoid showing stale selected
  run details after refreshes.
- Resolve generated-database URI paths consistently and close temporary QCoDeS
  loader connections without taking ownership of shared connections.
- Save GUI configuration changes transactionally so a failed write does not
  leave partial settings behind.

## 1.5.1-b1 - 2026-08-04

### Fixed

- Restore live plot updates for unshaped runs. Valid database reads were
  incorrectly discarded when QCoDeS reset its in-memory write offset after
  appending data, leaving plot windows frozen while previews continued to
  update.

## 1.5.0 - 2026-08-03

### Fixed

- Cancel active plot loads and discard queued plot work during shutdown so
  cooperative database and processing work no longer keeps the command line
  occupied after qPlot closes.
- Interrupt active read-only SQLite plot queries where safe, add cancellation
  checkpoints throughout plot preparation and heatmap processing, and prevent
  cancelled or partial results from reaching closed plot windows.

### Changed

- Allow qPlot's built-in data operations to cooperate with cancellation while
  preserving the existing one-argument contract for third-party operations.

## 1.5.0-b5 - 2026-08-02

### Added

- Include ten cumulative CSV instruction files targeting databases from about
  10 MB to 30 GB, with CLI and File-menu actions for exporting the collection.
- Add **File > Close Database** and **Quit qPlot** commands. Quit uses the
  platform-standard shortcut, including Command-Q on macOS and Ctrl-Q on
  Windows and Linux.

### Changed

- Timestamp test-database generation output and report start, stop, elapsed
  time, and completion state for the database and each run.
- Write generated QCoDeS results in bounded array chunks instead of one point
  at a time, substantially reducing large test-database generation time while
  retaining cancellation between chunks.
- Cancel database work, close plot and preview connections, and wait for
  background workers before qPlot exits.

### Fixed

- Prevent shutdown tracebacks when an interrupted SQLite worker completes
  after its Qt signal object has already been deleted.
- Detect a database file replaced at the same path and discard cached metadata,
  plots, previews, and connections belonging to the old file.
- Clear stale run selection and details when loading a selected run fails.
- Validate generated result columns and stored row counts, and publish generated
  databases atomically without overwriting a concurrently created file.
- Reject merging 2D cuts whose fixed-axis values are incompatible.
- Keep preview scheduling bounded and prevent stale workers from overwriting
  newer preview state.

## 1.5.0-b4 - 2026-08-01

### Added

- Add `qplot-generate-db` for creating 1D and 2D QCoDeS test runs with randomly
  phased and scaled sinusoidal data from spreadsheet-friendly CSV
  specifications, including CLI and File-menu actions for writing a ready-to-use
  example CSV, revealing its folder, and generating the database in the
  background.

## 1.5.0-b3 - 2026-08-01

### Fixed

- Restore horizontal and vertical heatmap cut windows after axis-state
  initialization prevented their fixed-axis controls from being constructed.
- Reject measurements with more than two independent axes instead of silently
  averaging omitted dimensions, and show unsupported-dimensionality
  placeholders in run previews.
- Invalidate previews when dependency or grid-shape metadata changes, without
  regenerating them for storage-size-only updates.
- Coalesce repeated plot refresh requests and run main-window database polling
  outside the GUI thread.
- Close selected, exported, and cached dataset connections deterministically
  when their last owner releases them.
- Reject non-finite or reversed manual axis limits without raising from a Qt
  callback.
- Save configuration updates atomically so interrupted writes preserve the
  previous valid file.
- Keep 1D traces from different databases distinct when their run IDs and
  parameter names match.
- Keep multiple cuts from the same heatmap distinct and visibly numbered when
  they are added to a 1D plot.
- Refresh merged heatmap cuts as they move, clear them when their sweep axis is
  incompatible, update Add to Plot choices when that axis changes, and handle
  empty cut data without wedging its refresh worker.
- Keep ordinary merged live traces updating after their source window closes,
  while sharing and releasing hidden-source polling safely across target plots.
- Release line and cut workers after rendering failures, and roll back failed
  secondary-trace construction without leaking dataset ownership.
- Keep reordered and removed secondary traces safe during refresh, cleanup,
  legacy picker selection, and Trace Appearance updates.
- Preserve explicit database provenance while opening plots, and select the
  exact same-label trace during cross-database drops without closing a visible
  source window.
- Ignore empty or entirely non-finite heatmap data when autoscaling the
  colorbar.
- Give constant-valued heatmaps an accurate finite color scale, including at
  floating-point extremes.
- Keep colorbar interaction rounding finite, positive, and synchronized with
  changing levels, including constant and extreme-valued heatmaps.
- Stop polling completed runs whose database row has no completion timestamp.
- Detect live-plot completion even when no final data row is added, and ignore
  late worker callbacks after their last dataset owner closes.
- Restart live polling after transient result-count failures, and prevent stale
  closed-window callbacks from releasing the current worker's waiter.
- Keep right-axis values synchronized with the traces that actually use them,
  and make the mutually exclusive dot and marker controls behave consistently.
- Prevent stale preview workers from clearing current-database worker state.
- Return a scalar completion timestamp, or `None`, from `has_finished`.
- Preserve the inherited Qt `layout()`, `width()`, `height()`, `x()`, and `y()`
  methods on qPlot windows.

## 1.5.0-b2 - 2026-08-01

### Highlights

- Database changes, plot opening, and background plot refreshes now preserve
  the last successfully committed state when newer work fails or becomes stale.
- The run table now distinguishes successful, interrupted, failed, and still
  unfinished measurements.
- Existing QCoDeS and last-used databases now populate their run data correctly
  when qPlot starts.

### Changed

- Database switching now keeps the committed database visible and active until
  the requested database has loaded successfully.
- Dataset identity now includes database provenance as well as GUID, allowing
  copied databases that contain matching GUIDs to coexist safely.

### Fixed

- Prevent cancellation, failure, and stale database-load callbacks from
  replacing the committed database.
- Retry transient cloud-provider timeout and cancellation errors while a
  database placeholder is being downloaded, within the existing overall load
  timeout.
- Prevent plot-opening failures from closing selected, cached, or
  already-published dataset connections; transient connections are still
  cleaned up when construction fails.
- Prevent superseded plot workers from replacing newer plot data or showing
  stale successes, failures, status messages, or error dialogs.
- Show completed keyboard interruptions as interrupted and completed
  measurement exceptions as failed, while retaining distinct successful and
  unfinished run states.
- Load run data for an explicit startup database, a valid last-used database,
  or an existing QCoDeS database through the normal startup loading path.

### Known Limitations

- Derivative operations currently retain the original displayed labels and
  units.
- Operation validation and failure handling can skip invalid operations or
  leave a partially applied operation pipeline.
- Large-dataset performance has not yet been comprehensively benchmarked.
- Restore Defaults in Preferences applies immediately, so Cancel does not
  restore the previous preferences.
- `qplot-cfg` does not correctly support every empty-string or empty-list
  update.
- Closed plots may occasionally remain in Add to Plot choices until the list is
  refreshed.

## 1.5.0-b1 - 2026-07-31


### Highlights

- This beta introduced spatial heatmap aggregation and geometry improvements,
  together with targeted database, plot-opening, operation, and trace fixes.
- As a result, thumbnails and previews now work properly.
- Handling large datasets remains slow and I have not benchmarked this against Ben Wordsworth's original version.
- I have also not tested this version thoroughly, which is why it's still a beta.

### Added

- Add a shared heatmap geometry model for uniform, nonuniform, descending, and
  singleton setpoint axes, with exact cell bounds used consistently by
  rendering, hovering, marquee selection, and sweep controls.
- Add a benchmark helper for spatial heatmap aggregation.

### Changed

- Large heatmaps now use bounded spatial mean aggregation in SQL instead of
  periodic database-row sampling. Every matching source row contributes, so
  the plotted heatmap no longer depends on row insertion order, while axis
  centres retain the corrected source heatmap extent.
- Large preview images and run-list thumbnails now originate from spatial bin
  means instead of periodic row-ID samples, preventing scan-pattern aliasing.
- Use run IDs instead of timestamps as the new-run refresh cursor, so runs with
  equal or missing timestamps cannot hide later runs.
- Require pyqtgraph 0.14 or newer for the heatmap rendering model.
- Let mypy use the active Python version during developer validation.

### Fixed

- Zoom to All, the on-plot auto-range button, and manual zooming out now
  restore and reload the complete source extent after a large heatmap has
  loaded a zoomed visible range.
- Render nonuniform heatmaps against their exact cell edges, keep descending
  axes aligned with their data, and give singleton axes a visible extent.
- Keep heatmap hover values, marquee selection, axis swapping, and extracted
  sweeps aligned with the recorded setpoints as geometry changes.
- Preserve the active database when another database load fails, is cancelled,
  or finishes after a newer load has started.
- Roll back dataset ownership cleanly when plot construction fails, including
  closing transient read-only connections without disturbing shared datasets.
- Fill Below and Fill Right now handle bounded, leading, trailing, and
  over-limit gaps consistently.
- Reject invalid integer operation input without raising from the UI callback,
  while preserving valid floating-point scientific notation.
- Keep 1D trace colors, axes, and appearance settings synchronized across the
  trace controls, appearance dialog, and theme changes.

## 1.5.0-a1 - 2026-05-21

### Highlights

- Large heatmaps now make downsampling visible in the plot window.
- The default README install path remains on the latest full release, 1.4.0.

### Added

- Add a heatmap resolution indicator showing the plotted grid size and source
  grid size when they differ.
- Add a single icon-only warning button at the bottom right of downsampled
  heatmaps. Clicking it opens a dialog explaining what downsampling was applied.

### Changed

- Use the selected heatmap setpoint grid, rather than the total run point count,
  when deciding whether a heatmap exceeds the full-resolution limit.
- Remove the onscreen heatmap color-rescale button while keeping the keyboard
  shortcut for color autoscaling.

## 1.4.0 - 2026-05-19

### Highlights

- Autoplot works more reliably.
- Copy function has been added from plot windows.
- Main GUI reports run status properly.

### Changed

- Promote the 1.4.0 beta line to the stable 1.4.0 release.

## 1.4.0b3 - 2026-05-18

### Highlights

- Autoplot works more reliably.
- Copy function has been added from plot windows.
- Main GUI reports run status properly.

### Added

- Add configurable plot image clipboard output with screen-resolution,
  300 dpi, and vector SVG options.
- Add a Preferences option for the default plot image copy format/resolution.
- Document the plot image copy setting in the user guide and configuration
  reference.
- Add tests for plot image copy resolution, SVG clipboard data, and preference
  persistence.

### Changed

- Update plot-window copy shortcut help text to reflect the selected copy
  format/resolution.

## 1.3.2 - 2026-05-16

### Added

- Add in-app help, keyboard shortcut reference, and diagnostic log path copying.
- Add application diagnostics, startup version logging, and `qplot-cfg -version`.
- Add Preferences, including restore defaults and auto-plot controls.
- Add live database load progress feedback and improved cloud-sync status
  reporting.
- Add demo-data notes, screenshot capture tooling, and expanded user
  documentation.
- Add local package build validation in CI and release documentation.
- Add macOS CI coverage alongside Windows.

### Changed

- Move database and cloud-load logic out of the main window into dedicated
  datahandling modules.
- Split plotting, colorbar, run-control, preference, and window-control code
  into smaller modules.
- Improve autoplot behavior and empty-database handling.
- Improve horizontal and vertical cut controls for heatmaps.
- Make `import qplot` lazy for GUI modules so lightweight commands do not import
  the full GUI stack.
- Improve README installation and usage guidance.

### Fixed

- Fix completion status handling for interrupted runs.
- Fix several CI, lint, and packaging issues found during release preparation.
- Fix the "Limit Maximum" operation label typo.

## 1.3.1 - 2026-05-14

### Added

- Add package metadata, project URLs, and release badge updates for the GitHub
  repository.
- Add contributor setup guidance, architecture notes, configuration reference,
  and release hygiene documentation.
- Add development checks for Ruff, mypy, pytest, build, and twine metadata
  validation.
- Add Edward Laird as a package author.

### Changed

- Consolidate and clarify README setup, installation, and usage instructions.
- Tighten generated-file ignores and repository hygiene around local build/test
  artifacts.
- Improve test coverage around configuration, reset behavior, and GUI display.

### Fixed

- Fix package license metadata problems that blocked CI.
- Fix duplicate and inconsistent README notes around plot-window loading.

## Earlier Snapshots

- `Laird-version` - 2026-05-14: interface, plotting, colorbar, marquee, status
  bar, shortcut, and README improvements.
- `Wordsworth-version` - 2025-09-23: earlier project snapshot before the current
  beta release process.
