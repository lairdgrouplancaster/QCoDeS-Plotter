# Changelog

All notable user-facing changes are recorded here.

This project currently releases from GitHub source tags rather than PyPI. For
installation commands and release validation, see `docs/distribution.md`.

## Unreleased

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
