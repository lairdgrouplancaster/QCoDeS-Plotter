# User Guide

This guide covers the main qPlot workflows after installation. For setup
problems, see [troubleshooting.md](troubleshooting.md).

## Opening a Database

1. Start qPlot with `qplot` or `python -m qplot`.
2. Drag a QCoDeS `.db` file onto the database path field, or select
   `File -> Load Database...`.
3. The main window shows database controls at the top, the run table in the
   middle, and selected-run details at the bottom.

You can also open a database directly from the command line:

```console
qplot path/to/database.db
```

### Trusted-first loading and progressive metadata

For a supported same-host local database, qPlot first opens the trusted live
reader. One application broker keeps one persistent helper for the exact
accepted database instance. It publishes the basic run list before querying any
result table, checks the same helper for later commits, and fills cheap and
expensive metadata progressively in the background. Derived work uses the base
priority `(run tier, work kind, stable index)`: the selected run gets metadata
(including ephemeral detail), thumbnail, then preview; visible rows get all
metadata, then all thumbnails, then all previews; remaining rows follow the same
three lanes. Selecting or scrolling promotes the affected pending work without
duplicating it. If the window is left undisturbed, every safely bounded run field
completes. Large descriptions and parameter metadata are deferred from the
first run-list page; an individual field that exceeds the fixed detail budget
is left unavailable rather than causing the trusted read to fail and retry.

Selecting a run performs no synchronous database or snapshot read. In a
trusted session, qPlot shows cached basic values and loading placeholders
immediately, then fills the selected Raw key-value, Metadata, and Snapshot tabs
from one bounded ephemeral trusted detail read. The result is applied only while
the exact database, helper, source, selection generation, run ID, and GUID still
match; changing selection cancels it or makes it harmlessly stale. It is not a
QCoDeS `DataSet`, a private snapshot, or a retained derived-cache item.
Long scalar keys and values remain compact in the tree and are marked locally.
A `[display]` row counts these present-but-shortened cells; it is not a data-loss
warning. Double-click a marked cell, press Enter on its row, or use **View
Complete Value...** to open the current run's bounded read-only value viewer.
The viewer has Select All and an explicit Copy button and does not touch the
clipboard merely by opening. Traceback summaries retain both their beginning
and final exception line. A global `[truncated]` row is reserved for actual
source or structural omissions in Metadata and Raw. Raw categories open one
level by default.

Snapshot browsing is independently lazy. `available — loaded on demand` means
the complete stored snapshot was validated and remains accessible through
bounded pages. The top-level QCoDeS `station` container opens automatically and
fetches only its first bounded page; deeper
containers remain lazy. Expand one to fetch its children, and activate `Load
more…` to reveal each later page without omissions or duplicates. No page
creates more than 128 children or 32 KiB of displayed text. A shortened
Snapshot scalar shows a preview and a local **View Full** action for its exact
contents. `Load more…` is pagination, not a truncation warning; Snapshot
`malformed`, `truncated`, or `unavailable` is reserved for an actual source,
validation, or consistency failure.

Snapshot-fallback sessions stop at cached run-list basics and an unavailable
detail state: row selection starts no selected-detail reader, prepares no
additional selected-detail snapshot, and creates no QCoDeS `DataSet`. A trusted
detail error likewise does not trigger an automatic fallback copy. This applies
only to ordinary selection: fallback metadata and retained preview paths can
still create private snapshots. Explicit plot and CSV actions still materialise
their action-owned snapshots.

The indexed result `id` watermark counts physical QCoDeS rows, which can exceed
scientific points when dependents are interleaved or stored in blocks. A valid
run-description shape can supply a planned `setpoint_count`, but it remains a
provisional expected extent; an unfinished run may have acquired fewer logical
points. qPlot shows acquired `read_setpoint_count`, and labels `setpoint_count`
as observed, only after it has structurally verified the layout. An observed
exact shape appears only after the captured rectangular prefix is verified.
Irregular or inconclusive acquired shape/count values remain unknown rather
than being guessed, and storage stays labelled as estimated.

Large regular prefixes are verified through resumable 4,096-row metadata-only
pages. Only one page runs per UI owner turn, and priority is re-evaluated at
every boundary. Progressive metadata is a dependency only for its own run.
Strictly higher-tier work and ready selected images always win; within the same
tier, ordinary work receives at most three claims (one sibling
metadata/thumbnail/preview sequence) before one paced continuation page is aged
in. Pages rotate between eligible runs, and selecting a new run pre-empts at the
next boundary. When no selected image is ready, qPlot admits remaining-run
metadata within eight foreground claims.
One 10 ms coalesced continuation wakeup avoids zero-delay polling while the
schedule still fully drains. Inconclusive pages do not render, publish, or write
a cache entry. Once conclusive, the normal metadata, thumbnail, and preview
results are each produced only once, not once per page. Verification keeps
constant-size state and can resume from the accepted physical cursor when newly
appended rows retain a compatible layout. Incompatible or insufficient evidence
remains unknown rather than being guessed.

Trusted live access uses SQLite's real colocated WAL index, so committed rows
that exist only in an active WAL are visible without copying or checkpointing
the database. Main-database, WAL, and rollback-journal handles remain physically
read-only. SQLite may create or update only the exact colocated `-shm` file as
transient WAL coordination state; its contents, size, timestamps, or permission
mode may therefore change. Transactions are short so the owning QCoDeS writer
can continue committing and checkpointing.

After path validation, identity capture, and any required cloud hydration,
qPlot attempts the trusted open and basic query before the legacy access probe.
Snapshot fallback is considered only when the exact type of that initial-open
failure says the pinned native backend is genuinely unavailable or the source
or filesystem is explicitly unsupported. The old probe must then verify that a
safe snapshot can be taken. Cancellation, timeout, busy state, source
replacement, invalid database, source I/O, rejected or failed SQL, result-limit
failure, helper crash, protocol failure, forced termination, and uncertain
cleanup are reported directly and never trigger a silent retry or fallback.

If trusted access is unavailable for an ordinary active WAL and the snapshot
probe cannot prove the WAL belongs to the selected main file, qPlot reports an
actionable error. It never hides WAL-only commits by showing the older main-file
state.

### Snapshot fallback and deferred actions

The retained snapshot path is used for the narrow fallback above, explicit plot
or CSV actions that still need a QCoDeS `DataSet`, and the deferred Database
Information diagnostic. A quiescent
rollback-format source can use SQLite's normal read-only locking. A checkpointed
WAL-format source is copied to a private snapshot before immutable read-only
mode is used. An observed rollback `-journal` is copied with the main database
and any permitted WAL, and SQLite may recover it only on that private copy.
Source identities and journal state must remain stable throughout capture.

When a WAL must be copied, qPlot accepts it only when the snapshot path can
prove that it descends from the selected main file. An ordinary SQLite WAL has
frame salts and checksums but no unique identity for its main database, so
matching names, directories, timestamps, or file identities are not proof. A
stably observed zero-byte WAL contains no frames and is treated as having no
pending WAL data. The source database and sidecars are left untouched, and all
private files are removed when their owning connection closes.

Automatic default selection, scrolling, and metadata completion do not launch
legacy preview or thumbnail snapshot workers during a trusted live session.
Snapshot fallback sessions may retain their current preview behavior. The
trusted Stage 5C scheduler instead produces bounded derived thumbnails and
previews through the persistent helper and version-3 derived cache. Explicit
plot and CSV actions remain available through their action-owned snapshots and
the selected GUID and exact database instance.

Generated test databases carry provenance for snapshot consumers that ordinary
SQLite files lack: a unique generation token and a bounded chain of random,
parent-linked lineage events. On a private copy qPlot accepts the WAL only when
its chain is a strict descendant of the exact main-file chain head; a higher
counter from a divergent clone is not enough. The source `-shm` is not opened by
the snapshot path.

The private WAL checksum scan also requires every committed transaction to
carry the lineage-state page. A later valid provenance commit therefore cannot
bless earlier uninstrumented frames. If the valid WAL prefix is malformed or a
transaction has no lineage event, qPlot fails closed with checkpoint guidance.

SQLite has no persistent database-wide write trigger. If a QCoDeS script will
add new measurements to a generated database and those writes must remain
available to snapshot consumers before checkpointing, enable qPlot provenance
on the writer's QCoDeS connection before creating the `Measurement`:

```python
from qcodes.dataset import load_or_create_experiment
from qcodes.dataset.sqlite.database import connect
from qplot.testdata import enable_generation_provenance_for_writer

connection = connect(database_path)
enable_generation_provenance_for_writer(connection)
experiment = load_or_create_experiment("later runs", "sample", conn=connection)
measurement = Measurement(exp=experiment)
```

The writer hook installs provenance triggers for each newly created result
table before the same QCoDeS transaction commits. Later foreground and
background result writes then extend the durable ancestry chain across
checkpoints. Call the hook on a quiescent `AtomicConnection`, before any new
experiment or measurement writes. It refuses a nonempty WAL so it cannot
retroactively bless unknown frames. Checkpoint such a WAL with `TRUNCATE` on
the owning writer first. Enablement holds SQLite's writer lock while checking
that quiescent state, so a concurrent writer cannot slip frames between the
check and the first lineage event. This is an explicit writer operation; the
qPlot viewer never calls it.

The retained chain covers 65,536 lineage events between checkpoints. If a very
large uncheckpointed write overwrites that proof window, qPlot fails closed and
asks the owner to checkpoint rather than inferring ancestry from filenames,
timestamps, or successful SQLite replay.

Process-local replacement history remains a separate safety mechanism. It can
identify that a WAL is unpaired with a main file that replaced one qPlot had
already observed, so qPlot can quarantine that WAL. This is negative evidence
only: quarantine never turns an otherwise unknown WAL into a trusted one.

If a busy writer changes the main file or sidecars throughout every snapshot
attempt, or their transaction state cannot be proved safe, qPlot reports that
the database is busy or temporarily unavailable and leaves the source
untouched. It does not fall back to an immutable view that could expose stale
or uncommitted data. Finish the transaction or refresh after the writer pauses.

### Opening Databases from the File Manager

qPlot can be added to a file manager's `Open With` menu by pointing the file
manager at a small launcher that runs qPlot with the selected database path.
Because `.db` is a generic SQLite extension used by many applications, prefer
adding qPlot to `Open With` before making it the default app for every `.db`
file.

#### macOS

1. Open Automator.
2. Create a new `Application`.
3. Add the `Run Shell Script` action.
4. Set `Pass input` to `as arguments`.
5. Use this script, replacing `PYTHON` with the Python executable from the
   qPlot virtual environment:

```bash
PYTHON="/path/to/QCoDeS-Plotter/.venv-mac/bin/python"

for database in "$@"; do
  "$PYTHON" -m qplot "$database" &
done
```

6. Save the Automator application as `qPlot.app`.
7. In Finder, right-click a QCoDeS `.db` file and choose
   `Open With -> Other...`.
8. Select `qPlot.app`. To make qPlot the default for `.db` files, tick
   `Always Open With` before opening.

For a development checkout using the repository virtual environment, `PYTHON`
will usually look like:

```bash
PYTHON="/Users/you/path/to/QCoDeS-Plotter/.venv-mac/bin/python"
```

#### Windows

For a development checkout using the repository virtual environment, create a
launcher file such as `qplot-open-db.cmd`:

```batch
@echo off
set "PYTHON=C:\path\to\QCoDeS-Plotter\.venv\Scripts\pythonw.exe"
start "" "%PYTHON%" -m qplot "%~1"
```

Then right-click a QCoDeS `.db` file and choose
`Open with -> Choose another app`. If Windows does not show the launcher in
the app picker, set a per-user `.db` association from Command Prompt after
replacing the Python path:

```batch
reg add HKCU\Software\Classes\qplot.db\shell\open\command /ve /d "\"C:\path\to\QCoDeS-Plotter\.venv\Scripts\pythonw.exe\" -m qplot \"%1\"" /f
reg add HKCU\Software\Classes\.db /ve /d qplot.db /f
```

This makes double-clicking `.db` files open them with qPlot for the current
Windows user. Use it only if that is the default behavior you want for `.db`
files on that account.

On first launch, qPlot shows a small empty-database prompt with direct load and
quick-start actions. It disappears once a database is loaded or a load is in
progress.

The run table gives a compact view of each run, including measurements,
setpoints, start time, completion state, duration, and estimated size. The
details pane fills the selected run's overview, parameters, bounded setpoint
summary, snapshot fields, and raw metadata progressively. During a trusted live
session its preview area receives the bounded derived preview after that
selected run's metadata and thumbnail complete; it does not start an automatic
snapshot worker. The
renderer covers both complete 2D extents with representative samples, keeps
missing or not-yet-measured cells neutral, restores reversed/serpentine axes,
uses dependency 0 vertically and dependency 1 horizontally, and gives
thumbnails and previews the same scientific mapping. Dependents render
independently, and a finite singleton 1D point remains visible. In snapshot
fallback, ordinary selection shows cached run-list basics rather than starting
an additional selected-detail snapshot. Fallback metadata and the separately
retained legacy-preview path can still create private snapshots.

Right-click the run-table header and open **Columns** to show or hide any
column, including Experiment, Sample, Name, Completed, and GUID. Column choices
and manually adjusted widths persist between sessions. When the enabled
columns need more room, use the horizontal scroll bar below the table.

## Main Window

The main window is the database and run-selection hub.

Common actions:

* `File -> Load Database...` loads a QCoDeS database.
* `File -> Load Recent Database` reopens a recently used database.
* `File -> Refresh` checks the current database for new runs.
* `File -> Open Database Folder` opens the folder containing the loaded
  database.
* `File -> Close Database` closes its plot windows, cancels background database
  work, and releases the current database.
* `File -> Generate Test Data -> Create Example CSV...` writes a spreadsheet
  template, reveals it in Finder on macOS, or opens its containing folder in
  the platform file manager.
* `File -> Generate Test Data -> Export CSV Collection...` copies ten installed,
  cumulative instruction files for generating databases from approximately
  10 MB to 30 GB, then opens their folder.
* `File -> Generate Test Data -> Generate Database from CSV...` creates a
  QCoDeS test database from an edited specification without blocking the qPlot
  interface.
* `Options -> Preferences...` edits common theme, plot mouse mode, default
  load location, preview, refresh, confirmation, and runtime settings. On
  macOS this appears as `qPlot -> Preferences...`.
  Use `Restore Defaults` in that dialog to reset the shown preferences without
  leaving the dialog.
* `Options -> Reset All Settings...` resets all qPlot settings to their
  defaults.
* `Help -> Quick Start` shows the core workflow inside qPlot.
* `Help -> Keyboard Shortcuts` shows the shortcut reference inside qPlot.
* `Help -> Copy Diagnostic Log Path` copies the log file location for support
  or troubleshooting.
* `File -> Quit qPlot` closes the database and exits. Its shortcut is `Cmd+Q`
  on macOS and `Ctrl+Q` on Windows and Linux.
* The refresh interval controls how often qPlot checks for new runs. Set it to
  `0.0 s` to disable automatic checks.
* `Auto-plot` retains its existing behavior for snapshot fallback sessions.
  Trusted live Stage 4 does not materialise an action-owned plot dataset
  automatically; use an explicit plot action instead.

Where a legacy preview is available, its double-click and context-menu actions
can still request a plot or export. Trusted live sessions do not generate those
previews automatically in Stage 4; use the run-table plot action or the run and
measurement controls instead.

## Plotting a Measurement

There are several ways to open plots:

* Double-click a preview image when one is available in a snapshot fallback
  session.
* Right-click a run in the run table and choose a plot action.
* Enter a run ID and measurement number at the top of the main window, then
  press the plot button.
* Enter `*` as the measurement to open all plottable measurements for the
  selected run.

Parameters with one independent variable open as line plots, and parameters
with two independent variables open as heatmaps. Measurements with three or
more independent variables are not projected or averaged implicitly: qPlot
shows an `nD` unsupported placeholder in the run table and leaves the data
available for CSV export. Create an explicit 1D/2D slice before plotting it.

Line plots require real-valued measurements and coordinates, including samples
stored in QCoDeS array parameters. Complex values produce a `Plot Error` naming
the unsupported parameter. Raw measurement CSV exports preserve complex values.

Plot and CSV requests are the deliberate Stage 4 boundary: the action addresses
the selected GUID and exact database instance, then acquires an action-owned
snapshot and materialises the QCoDeS dataset. Merely loading, selecting, or
scrolling a trusted live database does not do this work or copy the database.

Plot windows may appear before their data has finished loading. Check the plot
window status bar; unless qPlot stops responding or shows an error, wait for the
load to complete.

## Plot Windows

Each plot window has plot controls, a status bar, optional toolbars, and dock
panels. Toolbars and dock panels can be shown or hidden from `View -> Toolbars`,
by right-clicking a toolbar or panel, or with keyboard shortcuts.

Common plot controls:

* Mouse wheel over the plot: zoom.
* Left-click drag: pan in the default mouse mode.
* Right-click: open the plot context menu.
* `Alt`/`Option` + left-click drag: draw a marquee selection.
* Drag marquee handles to resize the selection.
* Right-click inside a marquee selection for zoom and statistics actions.
* Press `Esc` or double-click the plot to clear a marquee selection.
* Double-click an X or Y axis to open its scaling dialog.
* Use `Log Scale` in an axis-scaling tab to switch a line-plot axis between
  linear and base-10 logarithmic scaling. Non-positive values are omitted.
* Right-drag on the plot, or scroll over an axis, to fast scale an axis.
* The bottom toolbar shows cursor coordinates and array indices.
* The left panel controls assigned axes and plot-specific options.
* The right `Operations` panel applies data operations during refresh, after
  data is loaded from the database.

### Line Plots

Line plots support multiple compatible traces in one window. When a snapshot
fallback session supplies a preview thumbnail, drag it from the run table onto
an existing line plot to add a trace. You can also use the left panel.

Compatible plots are matched by independent variable name. The source plot
window for an added trace can be closed after the trace is added. Live updates
continue at the same refresh rate.

When multiple traces use different Y axes:

* Zooming or dragging in the central plot controls both axes.
* Interacting with a side axis controls that axis only.
* Secondary traces attached to the right axis cannot be rotated.

### Heatmaps

Heatmaps support multiple compatible maps in one window. When a snapshot
fallback session supplies a heatmap preview thumbnail, drag it from the run
table onto an existing heatmap to add it as a layer. Layers must use the same
two independent variables with matching axis units and the same currently
displayed dependent-value unit; their coordinate ranges and grid sizes may
differ. If the two axes are in the opposite order, qPlot transposes the added
layer automatically. An operation that temporarily makes units incompatible
hides that layer until its units match again.

The left panel lists the heatmap layers. Added layers are translucent so that
overlapping maps remain visible; use each layer's opacity control or remove
button to adjust the composition. All layers share one colormap and color
range, which is autoscaled across their combined finite values. A hidden source
plot remains live while its layer is present, just as it does for an added line
trace. Large hidden heatmap sources also follow the visible window's viewport
when qPlot reloads zoomed data.

Cursor readout, marquee statistics, color zoom, and 1D cut extraction continue
to use the original heatmap in the window. Heatmaps also add the following
color-scale and cut controls.

Color-scale controls:

* Right-click the plot and choose `Autoscale Color`, or press `C`.
* Double-click the color scale bar to open the color scaling dialog.
* Drag one color-scale handle to adjust a limit.
* Drag between handles to slide the range.
* Drag outside the handles to widen or narrow the range.

1D cut extraction:

* Right-click the heatmap and select `Horizontal Cut` or
  `Vertical Cut`.
* A cut window opens, with a cursor shown on the heatmap.
* Move the cut position with the cut window slider or by dragging the cursor on
  the heatmap.
* Hold Shift while dragging a cut cursor to move all cuts with the same
  orientation together.
* Switch the cut and fixed parameters with the `x axis` and `fixed parameter`
  dropdowns.
* Cut plots are live-data compatible.
* Cut plots can be added to compatible 1D plots when their X axis matches the
  1D plot's independent variable.

### Data Operations

Plot windows can apply operations during refresh from the `Operations` panel.
The available operations depend on the plot type.

Common operations:

* Limit maximum values.
* Limit minimum values.

Line-plot operations:

* Differentiate `dy/dx`.

Heatmap operations:

* Subtract row mean.
* Subtract column mean.
* Differentiate `dz/dx`.
* Differentiate `dz/dy`.
* Fill below.
* Fill right.

Cut-plot operations:

* Subtract cut mean.
* Subtract fixed mean.
* Differentiate cut.
* Differentiate fixed.

Select operations in the panel, drag active operations to control order, then
choose `Apply/Refresh`.

Line plots and 1D cuts also expose native controls in the plot context menu
under `Plot Options -> Transforms`. FFT and `dy/dx` can each be used alone or
with mean subtraction. Outside phase map, enabling FFT clears `dy/dx`, and
enabling `dy/dx` clears FFT. Phase map allows both controls to remain checked;
it plots the original Y samples against their derivative, overriding the FFT
and mean-subtraction mappings. Disabling phase map with both FFT and `dy/dx`
checked clears FFT and keeps `dy/dx`. If only one is checked, that transform is
retained. These controls update all traces in the window, including traces on
secondary axes, and the axis labels follow the displayed quantities.

## Export

The main window can export measurement data as CSV:

* Select a run and use the CSV button.
* In a snapshot fallback session, right-click a preview and choose the export
  action.

Plot windows can export plot images and data through `File -> Export Plot...` or
`Ctrl+E`, using pyqtgraph's export dialog. Use `File -> Save Plot as PDF...` or
the plot context menu to save a plot-sized PDF of the rendered plot area. Use
`Edit -> Copy Plot Image`, `Ctrl+C`, or the plot context menu to copy it to the
clipboard without the surrounding window menus or toolbars. The copy resolution
is set in `Options -> Preferences...`: screen resolution preserves the current
display pixels, while 300 dpi renders a higher-resolution clipboard image at the
same logical plot size, and vector SVG copies editable SVG data for applications
that accept SVG from the clipboard.

Use `File -> Print Plot...`, `Ctrl+P`, or `Cmd+P` to open the system print
dialog. Printing includes only the visible plot area, scales it to the printable
page without stretching or cropping, and excludes the plot-window controls.
When the system dialog exposes a concrete PDF destination, `Print Plot...` can
also produce a page-formatted PDF. qPlot stages that file beside the selected
destination and publishes it atomically only after rendering succeeds. Use
`Save Plot as PDF...` instead when you want a plot-sized PDF without the printer
page layout.

## Live Data

qPlot can display running QCoDeS experiments. The main-window refresh interval
checks for newly added runs. Each plot window has its own refresh timer for
loading new data from the database.

Use `File -> Refresh` or `R` to refresh manually. Set a refresh interval to
`0.0 s` when you want manual refresh only.

## Keyboard Shortcuts

General shortcuts:

| Shortcut | Action |
| --- | --- |
| `F1` | Show quick start help |
| `Ctrl+L` | Load a database |
| `R` | Refresh the current window |
| `Ctrl+W` / `Cmd+W` | Close the current qPlot window |
| `Ctrl+Q` / `Cmd+Q` | Quit qPlot |
| `Ctrl+,` | Open preferences |
| `Ctrl+M` / `Alt+Space, N` | Minimize the current window |
| `Alt+Space, X` / `Alt+Space, R` | Maximize or restore the current window on Windows |
| `Ctrl+Cmd+F` / `F11` | Enter or leave full screen |
| `Ctrl+C` / `Cmd+C` | Copy selected cells or rows in the run details pane |
| `Ctrl+Shift+C` / `Cmd+Shift+C` | Copy the current cell or value in the run details pane |
| `Shift+F10` | Open the focused widget's context menu |
| `Ctrl+Shift+D` | Open the current database folder |
| `Ctrl+Shift+M` | Bring the main window to front, or behind the graph windows |
| `Ctrl+Return` | Plot the requested run and measurement |
| `Ctrl+Shift+Return` | Plot all measurements in the selected run |
| `Ctrl+1` to `Ctrl+9` | Plot measurements 1 to 9 in the selected run |
| `Ctrl+Shift+W` | Close all plot windows |

Plot-window shortcuts:

| Shortcut | Action |
| --- | --- |
| `Ctrl+0` | Return all plot axes to autoscale mode |
| `Ctrl+C` / `Cmd+C` | Copy the plot image to the clipboard using the selected copy format/resolution |
| `Ctrl+E` | Export the plot |
| `Ctrl+P` / `Cmd+P` | Print the visible plot area |
| `Ctrl+Alt+R` | Show or hide the refresh toolbar |
| `Ctrl+Alt+C` | Show or hide the coordinate toolbar |
| `Ctrl+Alt+A` | Show or hide the axis control panel |
| `Ctrl+Alt+O` | Show or hide the operations dock |
| `S` | Snap the 1D coordinate readout to the nearest trace point |

Heatmap shortcuts:

| Shortcut | Action |
| --- | --- |
| `C` | Autoscale the colour range |
| `H` | Open a horizontal cut |
| `V` | Open a vertical cut |
| Arrow keys | Move the selected cut cursor by one pixel |

Dynamic context-menu entries are numbered or underlined. Once a menu is open,
press the shown number or letter to trigger that entry.
