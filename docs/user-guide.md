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
The viewer offers **Find next**, **Wrap lines**, **Select all** and **Copy entire
value**. The copy shortcut in the text area copies the selected text. Traceback summaries retain both their beginning
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
still create private snapshots. Fallback plot and raw CSV actions use that
read-only path; trusted plots and raw CSV exports use the broker.

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

The retained snapshot path is used for the narrow fallback above, fallback plot
actions, fallback raw CSV actions, and the deferred Database Information diagnostic. A quiescent
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
Snapshot fallback sessions may retain their current preview behaviour. The
trusted Stage 5C scheduler instead produces bounded derived thumbnails and
previews through the persistent helper and version-3 derived cache. Explicit
trusted plots capture or aggregate the selected run through the broker. Raw CSV
exports capture that run's committed prefix through the same broker. Both
address the selected GUID and exact database instance.

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

On macOS and Windows, open **Settings → Desktop integration…** or the same
action in **About qPlot**. Choose the launcher location, optional desktop
shortcut and file associations, then select **Install launcher**. File
registration adds an Open With option and icons for the database and its WAL
and SHM sidecars. Your default opener stays in place. For an existing qPlot
launcher the dialog offers **Update launcher** and **Remove integration**.
**Browse…** lets you choose an installation folder. Progress and error
details appear when an operation runs.

After your first plot opens, the main window offers **Set up…** and **Dismiss**
in its status bar. Both choices dismiss future invitations. qPlot also skips
the invitation when it detects an existing launcher at the default location.

**Recommended if you mainly work with QCoDeS databases:** create the desktop
launcher and register the database, WAL and SHM icons with the command below.
Run it from the same Python virtual environment used for installation:

```console
python -m qplot.desktop setup --file-associations
```

The flag also adds qPlot to **Open With** for `.db` files. File registration
remains opt-in because `.db`, `.db-wal` and `.db-shm` are shared with other
programs, whose files may receive these icons too. For just the program icon,
launcher and desktop shortcut, omit the flag: `python -m qplot.desktop setup`.

No administrator rights are needed. On macOS this creates
`~/Applications/qPlot.app`; on Windows it creates a **qPlot** shortcut in your
Start menu. Setup also adds a **qPlot** shortcut to your desktop, using Windows'
configured Desktop location even when it is redirected to OneDrive. Use
`--no-desktop-shortcut` to omit it. Existing desktop files and shortcuts are
never overwritten; if the name is already in use, move that item or use the
option to omit the shortcut.
On Windows, setup creates a small `qPlot.exe` launcher with the qPlot icon
embedded, so **Opens with** in file properties displays the application icon.
Both use qPlot's rounded-square red Coulomb-peak icon, which also appears on the
running application's windows where supported by the desktop. On macOS the
running application's Dock label is **qPlot**, including when started from a
terminal. Desktop launches run inside the installed `qPlot.app`, sharing the
shortcut's Dock identity. The first graphical terminal launch prepares a small signed `qPlot.app` host
in `~/Library/Caches/qPlot/launchers`; it uses the same Python environment and
does not modify the installed Python application. Double-click the
app or shortcut to start qPlot. You can pin it to
the Dock or Start. Everyday
launches need no terminal, environment activation, repository working directory,
or manual `MPLCONFIGDIR` setting.

If you already created a launcher, recreate it to add the file icons or update
older icons:

```console
python -m qplot.desktop remove
python -m qplot.desktop setup --file-associations
```

For a custom launcher location, pass the same `--destination` to both commands.
File associations are per user and do
not make qPlot the default: `.db` is also used by other applications.
The `.db` icon is a red folded page with a white Coulomb peak, making the main
database stand out among its companion files. It shares the program icon's
peak and palette, with the colours reversed. The `.db-wal` and `.db-shm`
companions use white pages with grey log-line and connected-block symbols and
WAL and SHM badges, simplified at small sizes. A red first log line and red
upper-left block outline link them to the other icons; both accents remain at
small sizes.
Neither companion is registered for opening
in qPlot. These are SQLite companion extensions, so the icons also apply to
matching files produced outside QCoDeS.

The icon shown on `.db` files depends on the operating system's selected file
association. macOS also declares the companion descriptions **SQLite write-ahead
log** and **SQLite shared-memory index**. Windows preserves existing file-type
descriptions and opener choices, and changes only the companion icons. Finder
or Explorer may need to refresh before displaying new icons.
If Finder shows the correct icon in **Get Info** but a generic icon in the
folder, press **Command–J** in that folder and turn off **Show icon preview**.
Finder's previews can cover the assigned file-type icons.

On macOS, right-click a QCoDeS database in Finder and choose
**Open With → Other…**, then select `~/Applications/qPlot.app`. On Windows,
right-click it in Explorer and choose **Open with → Choose another app →
qPlot QCoDeS viewer** (you may need **More apps**). The exact picker wording
varies by Windows version. Choose to open just this file unless you deliberately
want to change the default for all `.db` files. Selecting multiple files on
macOS opens a separate supervised qPlot instance for each. Paths containing
spaces and Unicode are supported.

Setup records the Python environment that ran the command. The macOS launcher
copies the interpreter executable for its Dock identity, but still depends on
that environment and its installed libraries; it is not a standalone qPlot
bundle. Keep the environment installed at its original path.
Upgrading qPlot in the same environment normally needs no launcher changes.
If Python or qPlot is removed or moved, a launch error explains how to recreate
the launcher. After recreating a moved environment, run setup from its Python.
For an editable development installation, keep the source checkout in place too.
On macOS, an environment inside Documents, OneDrive, or another protected folder
may require file-access permission for the generated app. If opening qPlot.app
shows **qPlot could not start**, open **System Settings → Privacy & Security →
Files and Folders**, expand **qPlot**, and turn on **OneDrive** (or the folder
containing the installation). Dismiss the error and open qPlot.app again.
Granting this permission needs no reinstallation or launcher recreation;
Terminal and qPlot can have different permissions for the same folder.

If the needed permission is unavailable, create a fresh installation in an
accessible local folder outside OneDrive, Documents, and Desktop, then recreate
the launcher from that environment. Do not move an existing virtual environment.
Recreating a launcher pointing at the same denied environment does not fix
access permissions. See [desktop startup troubleshooting](troubleshooting.md#qplotapp-could-not-start-on-macos).
On macOS, errors occurring before Python starts are saved
to a unique temporary `qplot-startup.*` file; the error dialog shows its path
and the last part of the diagnostic.

To remove the launcher and its opt-in Windows registration:

```console
python -m qplot.desktop remove
```

Removal leaves Python, qPlot, settings, caches, logs, and databases alone. It
refuses to delete a launcher folder containing a database or its sidecars.
On Windows it restores previous companion icons if qPlot's overrides are still
in place, preserving any icon changes made by another application since setup.
Removal also deletes the desktop shortcut created by setup, provided it still
matches the recorded link. Replaced or edited shortcuts are preserved.
Delete any additional copied shortcuts and unpin Dock/Start items yourself. If Python is
already gone, delete the macOS app in Finder or the Windows launcher folder
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\qPlot` in Explorer.
To clean a remaining Windows Open With entry, recreate an installed environment
and launcher at that same location with `--file-associations`, then run `remove`.
Any default file choice you made yourself can be changed in the OS settings.

Use `--destination "/your/path/qPlot.app"` on macOS or
`--destination "C:\your\path\qPlot"` on Windows to choose a different location;
pass the same option for removal. Existing destinations are never overwritten.
The desktop shortcuts refer to the original launcher location. On macOS the
desktop shortcut is a symbolic link to the app. Recreate the launcher and its
shortcut when changing the installation location.

Desktop launches set Matplotlib's cache to `~/Library/Caches/qPlot/matplotlib`
on macOS or `%LOCALAPPDATA%\qPlot\matplotlib` on Windows, overriding an inherited
`MPLCONFIGDIR`. Launch diagnostics are stored in the adjacent `logs` folder,
with a separate `launch-*.log` per invocation. Old logs may be deleted when qPlot
is closed. The same supervised entry point and protected database readers are
used as for command-line launches; setup and removal never open databases.
Linux desktop setup is not currently supported.

On first launch, qPlot shows a small empty-database prompt with direct load and
quick-start actions. It disappears once a database is loaded or a load is in
progress.

The run table gives a compact view of each run, including measurements,
setpoints, start time, completion state, duration, and estimated size.
Regular scalar measurements with three or more dimensions also report
setpoint counts. For runs containing different measurement grids, the table
shows the largest measurement's count. The info pane also shows per-axis
counts under **Sweep parameters** in the **Steps** column. The
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
* `File -> Load Recent Database` reopens a recently used database. If qPlot cannot save the list, newly opened databases still appear here for this session. Choose **Details** in the notice to see the settings path and error. Opening a database again retries the save.
* `File -> Refresh` checks the current database for new runs and refreshes all
  live plot windows once.
* `File -> Open Database Folder` opens the folder containing the loaded
  database.
* `File -> Close Database` closes its plot windows, cancels background database
  work, and releases the current database.
* `File -> Generate Test Data from Template -> Open Bundled CSV Folder` opens the installed
  examples in Finder or File Explorer. Use `Export CSV Collection...` to make
  editable copies elsewhere.
* `File -> Generate Test Data from Template -> Create Example CSV...` writes a spreadsheet
  template, reveals it in Finder on macOS, or opens its containing folder in
  the platform file manager.
* `File -> Generate Test Data from Template -> Export CSV Collection...` copies ten installed,
  cumulative instruction files for generating databases from approximately
  10 MB to 30 GB, then opens their folder.
* **File → Generate Test Data from Template → Generate database…** shows the
  workload for a bundled template or your own specification. Choose an output
  file and select **Generate database**. You can cancel in the progress window
  and open or reveal the completed database. The CSV dialogs remember the last folder used for CSV selection,
  example creation, or collection export across application launches.
* `Settings -> Preferences...` edits common theme, default
  load location, preview, refresh, confirmation, and runtime settings. On
  macOS this appears as `qPlot -> Preferences...`.
  On the Plots page, `Cursor precision (significant figures)` controls the x, y, and z
  coordinate readouts, including snapped trace values (1–17; default 6).
  **Reset this page** stages defaults for the selected page. Choose **Apply**
  or **OK** to save edits, or **Retry** after a failed save.
* `Settings -> Advanced -> Reset All Settings...` restores all qPlot settings,
  closes the current database and plot windows, and cancels their unfinished exports.
* **Help → Quick start** shows how to load a database, select a run and open a plot.
* **Help → Keyboard and mouse controls** lists the current window’s controls
  first. Press `Ctrl+F` (`Cmd+F` on macOS) to search by action or gesture.
  Choose **This window** to limit the results.
* `Help -> About qPlot` shows the version. **Technical details** contains
  copyable build, commit and installation information. `Committed` is the Git committer date and time shown in UTC;
  it describes the recorded commit, including when there are local changes.
  This information works offline in source checkouts and stamped packages.
  The date is omitted when unavailable; it is not a merge or publication date.
  The selectable `Python environment` and `qPlot location` paths identify the
  environment running qPlot and the loaded package folder. Each has an
  `Open folder` link to open it in your file manager. For editable installations,
  the package can be in a source checkout outside the environment.
* **Help → Check for updates…** checks PyPI for a newer qPlot release and shows
  release notes and upgrade instructions. Stable releases are selected by
  default; withdrawn releases and incompatible application wheels are ignored.
  `Help -> About qPlot` also offers **Check for updates**, with the result shown
  below the installed version. Both dialogs offer **Retry** on failure.
  `Preferences -> Application -> Updates` controls checks on launch (enabled by default) and can
  include pre-release versions. Launch checks run in the background, at most
  once every 24 hours, and notify you only when a newer release is available.
  Both checks have a five-second timeout. For source checkouts, update the
  checkout using your usual Git workflow; for installed releases, the dialog
  provides a command targeting the Python environment running qPlot. Use
  `Copy upgrade command` to copy it exactly, then close qPlot before running it.
* `Help -> Copy Path to Diagnostic Log` copies the log file location for support
  or troubleshooting.
* `Help -> Show Diagnostic Log in Finder` reveals the log on macOS. On Windows,
  `Show Diagnostic Log in File Explorer` selects the log in its folder.
* `File -> Quit qPlot` closes the database and exits. Its shortcut is `Cmd+Q`
  on macOS and `Ctrl+Q` on Windows and Linux.
* **Refresh every** controls the interval for the run table and all live plots.
  Set it to `0.0 s` to disable automatic refresh everywhere.
* `Auto-plot` opens every newly detected run, including runs already complete,
  in both trusted live and snapshot fallback sessions. Enabling it also opens
  the newest incomplete run already listed. Trusted live requests use the
  asynchronous reader and survive selection changes and subsequent refreshes.
  An empty plot waits for its first data. Missing descriptions are retried with
  backoff (eight attempts); after that, a run metadata change wakes the request,
  or you can open it manually. Disabling Auto-plot, changing databases, or
  quitting cancels pending automatic requests.

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
with two independent variables open as heatmaps. Three independent variables
open a 3D slice viewer; double-click a `3D` placeholder or use its Plot action.
Measurements with four or more independent variables retain placeholders with
CSV export and statistics, including ranges for all setpoint axes. No dimension
is projected or averaged implicitly. Mixed runs plot each supported quantity
according to its own dependencies.

Complex measurements, including QCoDeS array samples, use the persistent
preferences in **Preferences → Plots**. **1D complex plots** can show
magnitude, phase, Re or Im against the setpoint, or **Im vs Re** (Re horizontal,
Im vertical). **Higher-dimensional complex plots** select magnitude, phase, Re
or Im for heatmaps and 3D slices, retaining their usual setpoint axes. The
1D default is **Im vs Re** and the higher-dimensional default is **Magnitude**.
Phase is in degrees. Applying a preference
refreshes open plots, run-table thumbnails and the Preview pane. A small ℂ
badge identifies complex measurements; its tooltip names the displayed
component. Frozen snapshots retain their captured display.

The lower-left readout retains the original complex value and recorded
setpoints, including in Im vs Re plots. Complex readouts select a recorded
sample even when trace snapping is turned off. Raw measurement CSV exports
preserve both components. Setpoint axes must remain real-valued.
If a thumbnail is unavailable, double-click its `1D` or `2D` tile to open the
plot; an unavailable preview does not disable plotting.

Trusted plot actions request a connection-free run description, then capture
that run’s result prefix or aggregate a large numeric heatmap through the broker.
Private prefix staging contains the selected run, not a whole-database snapshot.
Trusted plotting needs neither a writer checkpoint nor generation provenance.
Trusted raw CSV exports also capture only the selected run. Fallback CSV and
plot actions retain the read-only snapshot path. All actions remain bound to the selected GUID and exact database
instance. See [explicit plotting](trusted-live-reader.md#explicit-plotting) for
capture limits and progress reporting.

Plot windows may appear before their data has finished loading. Check the plot
window status message; unless qPlot stops responding or shows an error, wait for the
load to complete.

## Plot Windows

Each plot window has plot controls, optional toolbars, and dock
panels. Toolbars and dock panels can be shown or hidden from `View -> Panels and Toolbars`,
by right-clicking a toolbar or panel, or with keyboard shortcuts.

Above the plot, qPlot identifies the displayed sources, for example
`Databasename: 9 (Signal); 10-12 (Current); 6-8, 11, 12 (Voltage)`.
Run IDs are grouped by parameter label, with ranges for three or more
consecutive IDs. Each database has its own line. Hidden or fully transparent
traces and heatmaps are omitted. This annotation is included in plot images,
SVGs, PDFs, and printing.
The annotation uses a shaded strip with a bold database name. In
`Preferences -> Plots`, `Show database and run details above plots` controls its visibility
in all plots and exports. It is enabled by default. Hiding it also removes
the space reserved for the strip.

Windows are titled **Plot 1**, **Plot 2**, and so on. These numbers remain stable
when other windows close or a plot's primary source changes.

Status appears at the right of the menu bar on Windows, and alongside the
native window title on macOS, including for completed runs. It shows the latest status message until another
message or loading progress replaces it. During loading it shows a progress
bar for the current stage, elapsed time, and the stage number. Hover over
progress for the stage name and detailed counts, or over a shortened status
message to read it in full. Empty-data and error explanations also appear in
the plot area.

Common plot controls:

* Mouse wheel over the plot: zoom.
* Left-drag: select a persistent rectangular region. Hover over the plot canvas
  for a reminder of selection, panning and zoom controls.
* Middle-drag or Space+left-drag: pan. Hold Shift to constrain movement to one axis.
* Right-click: open the plot context menu.
* `Alt`/`Option` + left-click drag: draw a marquee selection.
* Drag marquee handles to resize the selection.
* Drag inside a selection to move it; drag outside to replace it.
* Hold Shift while moving a selection to lock movement horizontally or vertically,
  according to the initial drag direction. Release Shift for free movement.
* Right-click inside a marquee selection for zoom and statistics actions.
  Statistics use the primary trace or heatmap identified in the report. A
  heatmap report gives the grid dimensions and the number of finite cells
  that contributed. The contributing sample count appears beneath the measured
  quantity, units and run, above the average, standard deviation and extrema.
  **Selection and source** contains the coordinate bounds and source details. **Copy full report**
  copies all results and source details. To copy individual rows, select them
  and use **Copy Selection**.
  For the full recorded measurement, choose statistics from its thumbnail.
  You can continue working or cancel while qPlot calculates.
* Press `Esc` or double-click the plot to clear a marquee selection.
* Double-click an axis to open **Plot settings → Axis scales** on that axis, or use
  **View → Axis Scaling…**. Switch axes with the **Bottom**, **Left**, **Top**
  and **Right** tabs at the top of the axis settings panel. The heading below
  the tabs shows the plotted quantity and units for the selected side.
  Hover over a shortened quantity name to read it in full. Unused sides are disabled.
* **Range** shows **Minimum**, **Maximum**, **Centre** and **Span** together.
  Centre is the arithmetic midpoint and Span is the full width. Focus a field
  to see its exact value. Logarithmic axes also use data units and require
  positive endpoints.
* **Automatic** follows the data. **Autoscale from visible data** fits a
  coordinate axis to samples inside the other axis's current limits. Editing
  an unlocked field selects **Manual**. Press **Enter** or leave the field
  to apply the value. If you select **Keep changes** with an invalid axis or
  colour limit, the dialog returns to its tab, selects the value and explains what to correct.
  **Esc** cancels the pending edit.
* In Manual, the two most recently edited fields determine the range. On the
  first edit, Minimum and Maximum are paired, as are Centre and Span. Click
  the padlock beside a field's label to hold that value while editing another or fitting to data.
  Clicking a second padlock transfers the lock. In Automatic, editing the held
  field refits the range and keeps tracking live data. In Manual, it preserves
  the most recently edited other field.
* **Set range from data** updates the range once, keeps any held value and leaves Manual selected.
  Holding Minimum or Maximum fits the opposite endpoint. Holding Centre gives
  equal reach on each side, enough to cover the data. Holding Span centres that
  width on the data midpoint. If a fit is impossible, the previous limits stay in use.
* To take just one value from the data, click the **Use data** button at the
  right edge of its number box, or right-click the field and choose **Use
  data minimum**, **Use data maximum**, **Use data centre** or **Use data span**.
  For example, with Centre held, **Use data span** copies the measured width
  and keeps it centred on your chosen value.
* Under **Axis behaviour**, **Log scale** switches a line-plot axis to
  base-10 logarithmic scaling. Non-positive values are omitted. **Link range
  to** lists other plot axes by plot title, axis position and quantity.
  Select **None** to control the range independently.
* Scroll over an axis to scale that axis. Right-drag on the plot has no scaling action.
* The bottom toolbar shows cursor coordinates and array indices.
* **Swap X/Y** on the Datasets page exchanges Bottom with Left and Top with Right.
  Each quantity carries its zoom, manual or automatic range settings, log scale
  and axis direction. A range link to another plot pauses while its two axes
  have different orientations. **Link range to** shows the paused link and
  reconnects it when the orientations match again. Choose **None** to remove it.
* Open **View → Plot settings…** to preview edits on the current plot.
  Choose **Datasets** for its measurements and drawing styles, **Axis scales** for its
  coordinate ranges, or **Colour scale** when the plot includes a heatmap.
  Drag the thin divider between the dataset list and the selected-dataset controls
  to resize the panes. Narrow windows stack these panes vertically, and the controls
  scroll when needed. **Whole plot** contains the shared colour-map and axis-swap controls,
  available independently of dataset selection. **Drawing** has compact **Line**
  and **Markers** rows for colour, width or size, and style or shape. Colour
  selectors show swatches; their tooltips and dropdowns give the hex codes.
  Visibility and opacity appear first, followed by Drawing and Trace axes.
  Line-style and marker-shape selectors show previews, with names in their
  tooltips and dropdowns. The dialog sizes the two panes to fit the dataset
  names and drawing controls.
  Choose **Keep changes** to keep edits across the pages, or **Cancel** to undo the session and close.
  `Esc` and the window's close button also cancel. Cancellation undoes dataset
  additions and removals, and restores styles, axis assignments, drawing order, the colour map
  and its range, edited axes, range locks and autoscaling choices.
  Multiple selections show **Mixed** where their values differ. Visibility, opacity
  and axis assignments apply to all selected datasets. Line and marker styles apply
  to selected traces. The colour scale and axis swap apply to the whole plot.
  Drag rows to change the drawing order, or select rows in the table and press
  `Alt+Up` or `Alt+Down`. The first row is drawn on top.
  Clearing the selection disables dataset-specific controls until
  you select a dataset again; refreshing does not select a dataset for you.
  To hide or show datasets, select their rows and change **Visible** in
  **Selected datasets**. Hidden rows keep their struck-through names and dimmed previews.
  For heatmaps, click the colour-map name and gradient in **Whole plot** to
  open the colour-map chooser directly.
* **Colour scale** shows the quantity, its **Range**, and the current map name
  and gradient. Select **Choose colour map…** to browse. The chooser opens at
  the selected map. Under **Available colour maps**, search by name or filter
  by **Source** and **Category**. If no maps match, **Clear filters** restores
  the full list and keeps the map shown under **Preview**.
  Selecting a row previews that map on the plot. **OK** keeps the preview in
  Plot settings. **Cancel** restores the map you started browsing with. The
  **Keep changes** and **Cancel** buttons in Plot settings apply to the whole session.
  Select the current map name to copy it, or use **Show in list** to reveal its
  row; filters are cleared if they hide it. Click the **Name** or **Type** heading
  to sort, and click again to reverse the order. Category tooltips explain the
  different kinds of colour map.
  **Visible area only** uses samples inside the current horizontal and vertical
  limits. **Central percentage** selects the proportion of those eligible
  colour values to include. For example, 98% uses the 1st and 99th percentiles.
* **Preferences → Plots → Plot display** groups the settings for open plots,
  including axis styling, cursor precision and the warning when adding data
  with incompatible axes. **New plot defaults** groups window placement,
  complex representations, trace appearance and the heatmap colour map.
  **Trace appearance**
  sets the initial colour, line width and line style in one row, using the same
  previews as **Plot settings → Datasets**.
  Choose a preset colour or **Custom**. These defaults apply to new
  plots and to traces added to existing plots. **Heatmap colour map** sets
  the default for new heatmaps. Existing traces and heatmaps keep their appearance.
  To change the colour map on the current plot, use **Plot settings → Colour scale**.
* Preferences are saved with
  `Apply` or `OK`; `Cancel` discards changes that have not been applied.
  The Preferences dialog fits the available screen and can be resized. Pages scroll
  vertically when needed, with the action buttons kept outside the scrolling
  area. Labels reflow in narrow windows, and arrows reveal tab headings when
  they do not all fit.
* If an edit would give an invalid range, the editor explains the problem
  and the plot keeps its last valid range.
* Cut windows have a `Cut controls` panel for the cut direction and position.
* The right `Data operations` panel applies data operations during refresh, after
  data is loaded from the database.

### Line Plots

Line plots support multiple compatible traces in one window. When a snapshot
fallback session supplies a preview thumbnail, drag it from the run table onto
an existing line plot to add a trace. In **View → Plot settings… → Datasets**,
you can also choose a measurement from the dropdown and select **Add**.

Compatible plots are matched by independent variable name. The source plot
window for an added trace can be closed after the trace is added. Live updates
continue at the same refresh rate.

When multiple traces use different Y axes:

* Zooming or dragging in the central plot controls both axes.
* Interacting with a side axis controls that axis only.
* Secondary traces attached to the right axis cannot be rotated.

### Heatmaps

When a heatmap is displayed at reduced resolution, its warning button opens a
modeless report showing the source and displayed grid dimensions (columns ×
rows), where known. The summary distinguishes spatial averaging, aggregation
and sampling. Expand **Details** for source-row counts, sampling and
interpolation information. **Adjust heatmap limits…** opens **Preferences → Performance**
at **Heatmap resolution**. It focuses **Maximum heatmap grid cells** when that
budget constrained the displayed grid, or **Full-resolution heatmap limit** when
the point threshold caused the reduction. If the cause is unclear, keyboard
focus stays on the **Performance** tab. The report updates while you use the plot.

`Preferences -> Performance -> Maximum heatmap grid cells` sets the total
cell budget for aggregated heatmaps (250,000 by default). A grid that fits is
kept at its available resolution; larger grids reduce both dimensions
proportionally. If one dimension reaches one bin, the other can use the
remaining budget. For example, 2,000 by 100 fits unchanged, while 1,000 by
1,000 becomes 500 by 500. Window resizing does not change this budget.

There is no separate per-axis preference. The legacy
`runtime_settings.max_heatmap_grid_side` entry remains readable in existing
settings files but is no longer used by the application. `Full-resolution
heatmap limit` remains a separate control for full-resolution loading and
operations. Streaming coordinate discovery has its own bounded memory: after
262,144 distinct values on an axis it uses spatial bins and a lower-bound
coordinate count, so extreme shapes can use less than the requested budget.
Aggregated grids have an internal limit of two million cells; Preferences
allows budgets up to that limit. Older settings requesting larger grids remain
readable, but a plot that would exceed this allocation limit reports an error.

Heatmaps support multiple compatible maps in one window. When a snapshot
fallback session supplies a heatmap preview thumbnail, drag it from the run
table onto an existing heatmap to add it as a layer. Layers must use the same
two independent variables with matching axis units and the same currently
displayed dependent-value unit; their coordinate ranges and grid sizes may
differ. If the two axes are in the opposite order, qPlot transposes the added
layer automatically. An operation that temporarily makes units incompatible
hides that layer until its units match again.

The **Datasets** page in **Plot settings** lists the heatmap layers. Added layers
are translucent so overlapping maps remain visible. Select a layer to adjust
its opacity or choose **Remove**. All layers share one colour map. In Automatic,
the colour range uses the combined finite values of the visible layers. A hidden source
plot remains live while its layer is present, just as it does for an added line
trace. Large hidden heatmap sources also follow the visible window's viewport
when qPlot reloads zoomed data.

Cursor readout, marquee statistics, colour zoom, and 1D cut extraction continue
to use the original heatmap in the window. Heatmaps also add the following
colour-scale and cut controls.

Colour-scale controls:

* Right-click the plot and choose `Autoscale Colour`, or press `C`.
* Double-click the colour scale bar to open **Plot settings** on the **Colour scale** page, or use **View → Colour Scale…**.
  Its **Range** controls work the same way as the coordinate axes.
* To keep positive and negative limits symmetric about zero, lock **Centre**
  and set its value to zero. In Automatic, the colour limits expand or shrink
  equally on both sides as the heatmap changes.
* Move the pointer over the colour strip to reveal both handles. Drag one handle to adjust a limit.
* Drag between handles to slide the range.
* Drag outside the handles to widen or narrow the range.

1D cut extraction:

* Right-click the heatmap and select `Horizontal Cut` or
  `Vertical Cut`.
* A cut window opens, with a cursor shown on the heatmap.
* Close the cut window to remove its probe. In heatmaps and 3D slices, double-click a probe
  line to close its cut, or Shift+double-click a probe line to close all cuts on
  that heatmap. Double-clicking elsewhere on the heatmap opens **Plot settings → Datasets**.
  Probe removal allows five extra screen pixels around the cursor, independent
  of zoom, and favours the nearest probe when hit areas overlap.
* Move the cut position with the cut window slider or by dragging the cursor on
  the heatmap.
* With the heatmap canvas focused, press `Ctrl+Tab` or `Ctrl+Shift+Tab` to select
  the next or previous visible cut in creation order, wrapping at either end.
  The selected cursor is highlighted; use the arrow keys to move it by one cell.
  This also works for horizontal, vertical and depth probes in 3D slices.
  Ordinary `Tab` and `Shift+Tab` continue to move between controls.
* Hold Shift while dragging a cut cursor to move all cuts with the same
  orientation together.
* Switch the cut and fixed parameters with the `x axis` and `fixed parameter`
  dropdowns.
* Cut plots are live-data compatible.
* Cut plots can be added to compatible 1D plots when their X axis matches the
  1D plot's independent variable.

### 3D Slice Viewer

A plot window can combine 1D traces, 2D heatmaps and one 3D measurement,
added in any order by dragging previews or using **Plot settings → Datasets → Add**. Adding a 3D
measurement brings up its slice controls; removing it removes those controls.
Slice selection, plane selection and slice operations affect only that 3D
measurement. Other datasets retain their coordinates and data. Each window
has its own slice selection, even when displaying the same volume.
Heatmaps share the window’s colour scale, including the displayed 3D slice.
Multiple 3D measurements in one window are not yet supported.

The bordered panel groups axis, colour and navigation controls in two rows.
X and Y selectors are stacked on the left, with colour controls in the middle.
On the right, the slice index sits above
the depth coordinate, beside the slider and **Follow latest** controls.
Drag its handle to move both rows together; the panel docks at the top or bottom
of the window.
Select any two independent parameters as X and Y. The remaining parameter is
Depth. The default uses the last declared dependency as X, the preceding one
as Y, and the first as Depth. The navigator shows a zero-based index and its
actual coordinate and unit. Hover over the slider or Slice control to see the
valid index range. Recorded depth coordinates
are ordered numerically, independently of acquisition order.

The compact cursor readout shows `[X index, Y index, depth index]` followed by
`(X, Y, depth, value)`. Hover over it for parameter names, units and the ordering.
Copy Coordinates includes all four values.
An em dash (`—`) marks an unavailable or nonfinite cursor value.

* Use the slider, Slice spinbox arrows, or Page Up/Page Down to step. Home/End
  select the first/last slice. Scroll over the slider to step; scrolling over
  the heatmap continues to zoom. Navigation does not wrap.
* Enter an index or a coordinate; coordinate entry selects the nearest recorded
  plane and shows the selected value. X/Y zoom and marquee bounds persist.
* Choose **Whole volume**, **Current slice**, or **Manual** colour limits.
  Whole-volume limits include slice operations and are calculated in the
  background, with an explicitly provisional range and scan progress. Live
  limits expand without shrinking. Press C to recalculate; dragging the
  colourbar or using marquee colour zoom locks the limits. In **Plot settings →
  Colour scale**, **Use data from** selects **Current slice** or **Whole volume**.
  **Current slice** enables **Visible area only** and **Central percentage**.
  **Whole volume** uses the full extent of the data. Switching back to the slice
  restores its filters. Hold **Centre** at zero for matching positive and negative limits.
* **Slice operations** run independently on each full plane, regardless of
  zoom or marquee bounds. Swapping X/Y preserves the physical operation.
  Changing the depth parameter restores that plane's own operation settings,
  initially empty. Derivative labels and units follow the named parameter.
* H/V cuts follow the current slice unless **Pin to this slice** is checked.
  D opens a depth cut at the selected X/Y position. Drag its point marker to
  move the position, or move the depth marker in the cut window to select a
  slice. Missing coordinates produce gaps, without snapping to another point
  independently in each plane.
* Cuts use the processed slice values by default. **Raw data** bypasses the
  slice pipeline; the cut's own operations run after extraction. Switching
  planes preserves cuts' physical meanings and pins H/V cuts from the old
  plane. Cuts retain their live source if its viewing window is closed.
* The marquee menu offers **Mean within selection versus depth**. This averages
  finite cells in the same physical rectangle in each plane. **Coverage…** lists
  the run, cut region, depth unit and finite versus selected cells for each
  depth. Ordinary two-column mean-cut CSV exports also include the finite counts.
* **Freeze Slice as 2D Plot** creates an independent processed snapshot with
  ordinary appearance, operations, H/V cuts and exports.

Refresh preserves the selected physical depth, even when new coordinates
change its index. **Follow latest** instead selects the most recently acquired
depth, which need not be the largest coordinate. Manual navigation disables
following. Incomplete slices show how many cells are missing; complete slices
omit this indicator. The navigator also reports repeated samples and planned
slice counts when known. Identical coordinate triples use a finite mean, with
repeat counts shown; missing cells remain missing unless an explicit fill
operation is applied.

Scalar and compatible numeric array storage are supported, including nonuniform
spacing, descending/snake sweeps and incomplete grids. In a complex volume,
each plane uses the chosen complex projection, even when all its readings are
real. At a repeated coordinate the displayed value is the projection of the
complex mean. The cursor shows the most recent finite reading at that point.
If a coordinate cannot be displayed without rounding, qPlot reports an error.
Each plane's cell count and acquired sample count must fit
`max_full_heatmap_points`; oversized planes report the limit instead of silently
downsampling. The viewer builds a private disk index, caches a few planes and
prefetches neighbours, without allocating a dense 3D cube. An initial capture
and index of the acquired run is required; live refresh rebuilds that index.

Slice/cut CSV exports include the
source GUID/revision, fixed coordinates, units and applied operations. The
normal run CSV export continues to provide all recorded measurement samples.
The first version uses one volume per viewer; use frozen slices for existing
2D comparison workflows.

### Data Operations

Plot windows can apply operations during refresh from the `Data operations` panel.
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

Use `File -> Export All Runs as Database…` to create a standalone `.db` for sharing.
Choose a new filename. If it is occupied or SQLite sidecars are present, use
**Choose another name…** to return to the same folder. The copy includes
committed data in the WAL and opens
without the original sidecar files. qPlot does not checkpoint or modify the
source database or WAL.
Databases whose measurements are stored in separate raw-data files cannot be
exported as a standalone database by this command.

Use `File -> Export Selected Runs as Database…` to export only the selected
runs. This includes every measurement, snapshot and metadata field in those
runs, with their experiment records. Run IDs and GUIDs are preserved. Links to
unselected runs remain as provenance references; those runs are not included.
The export compacts a private copy, so temporary space for the whole database
is needed. Database exports offer only the entire database or selected runs;
measurement selection applies only to CSV.

Acquisition can continue during export. The result is a consistent database
snapshot taken during the export, which may contain an unfinished run. Data
still buffered in QCoDeS is not included. Continued writes can cause SQLite to
restart copying pages; pause acquisition and retry if export cannot finish.
The copy preserves run IDs, GUIDs, metadata and measurement values, but is not
promised to be byte-for-byte identical to the original file.

The main-window **Export CSV…** button exports raw measurement values, including their
setpoints, without plot operations. Select or enter a Run ID and choose a
measurement number, or `*` for all measurements. Preview context menus can also
export one measurement using **Export This Measurement as CSV…**. The button
always uses the neighbouring Run ID and Measurement controls, regardless of
the table selection. Independent measurements are placed side by side, with
parameter names prefixed when exporting several measurements; shorter columns
have blank trailing cells. Array records are expanded into their individual
samples without rounding stored integer or extended-precision values.

To export several runs, use Shift-click or Ctrl-click (Cmd-click on macOS) to
select rows, then use the selected rows' context menu or
`File -> Export Selected Runs as CSV…`. Choose all measurements or a named
measurement shared by the selected runs under **Data to export**, then choose
an output folder. If there are no shared measurement names, **Data to export**
shows **All measurements in each run** as plain text. The review shows the
number of selected runs and their run numbers in a single-line field that you
can select and copy. After folder selection, the filename review lists one CSV
per run. Double-click a name to edit it, choose **Skip** to leave it out, or
choose **Replace** for an existing file. **Change folder…** keeps edited filenames
and skipped rows, and checks for conflicts in the new destination. Choose **Replace**
again for any existing file you want to overwrite there. The footer counts files that are ready,
need attention or are skipped. **Next issue** opens the next filename that needs
attention. **Use unique names** adds numbered suffixes to resolve conflicts,
preserving explicit replacement choices where possible. Select **Export** when
the filenames are ready. Each run captures the
committed data available when its export starts. Later live appends are excluded
from that run's captured export. Trusted live exports use the run-prefix reader
and do not need a writer checkpoint or a whole-database copy.

You can also select individual measurement thumbnails in the run table or the
Preview pane. Ctrl-click (Cmd-click on macOS) toggles a thumbnail; Shift-click
selects a range in the current display order. Right-click a selected thumbnail
and choose **Export Selected Measurements as CSV…**, then choose a folder. This
opens the same filename review with one raw CSV per selected measurement, including its setpoints. Selecting
two measurements from the same run produces two files. Selection is local to
the run table or Preview pane, and remains highlighted as previews finish
loading. Drag any highlighted thumbnail onto a plot to add the whole selection
in display order. The drag image shows the number of measurements being copied.
Dragging an unselected thumbnail carries just that measurement. The usual
axis compatibility choices and one-volume-per-window limit still apply.

Database copies and CSV exports run in the background. The **Exports** window lists
output filenames, groups selected-run and selected-measurement batches, and
shows how many files have finished in each batch. Select a pending file to cancel
it with **Cancel selected**, or use **Cancel all** for all unfinished exports.
Choose **Hide** to keep working, and reopen the window from `File -> View Export Progress…`. A failed or cancelled
export leaves its destination unchanged; files already completed remain saved.
Successful exports show **Export complete** with a tick. When all exports have
finished, **Hide** becomes **Done**; for a single successful export, the completion
message also shows the output path, which you can select and copy. Select a
completed export and use **Open file** to open it in its default application,
or **Show in folder** to locate it in your file manager. For a failed export, select
the row and choose **Details…**, or double-click the row, to view and copy
the error details.
Switching or closing the database cancels pending database copies and raw exports.
Closing a plot cancels that plot’s pending exports. When closing plots, the database
or qPlot would cancel unfinished exports, a confirmation shows how many are affected.
This warning also appears when routine close confirmations are turned off. Completed
files are kept.

In a plot's export dialog, select **Data**, then choose **Data to export**.
Individual traces and heatmaps are identified by run number and measurement name.
**All traces** combines the plot's measurement traces into columns; heatmaps are exported one
at a time because their grids may have different coordinates. Hidden measurements
are included when selected. Generated average curves are omitted from this
measurement selection. The initial choice is all measurement traces for a line
plot, or the primary grid for a heatmap plot.
This is a single-choice list: choose one measurement or **All traces**.
**Columns** is shown only when exporting multiple traces.

Check **Values** for the selected measurements:

* **After Operations-panel processing** includes applied Operations-panel processing,
  before native FFT, differentiation, phase-map, or mean-subtraction controls.
* **After operations and plot transforms** also includes those native controls. Both line
  modes export all loaded samples from the selected traces in physical
  coordinates; zoom, rendering downsampling, and logarithmic axis display do not
  change their values. Transformed export does not support error bars.
* **Current heatmap grid** exports the selected heatmap's currently loaded grid,
  including applied operations and spatial averaging. It retains the loaded
  extent and resolution and excludes other measurements. Secondary heatmaps use
  their own grid coordinates and measurement names.

Plot CSV values are captured from the plot after the destination is chosen;
later refreshes do not change the queued export. No plot CSV mode reads fresh
database data. Use the main-window CSV action for raw measurements.

Plot windows can export plot images and data through `File -> Export Plot...`
or `Ctrl+E` (`Cmd+E` on macOS). On the **Figure** tab, choose **PNG image**,
**SVG**, or **Plot-sized PDF**. Each format retains its settings when you switch
formats or tabs while this dialog is open.
Images and SVG export the complete plot, including colour scales and source
annotations. Image dimensions start at the displayed plot size. The fixed aspect
ratio appears below **Width** and **Height**. Editing either dimension adjusts
the other to preserve that ratio. **Background** offers **White**, **Transparent**
and **Choose colour…**. For a custom background, **Opacity** ranges from
0 (transparent) to 255 (opaque). Cancelling the
colour picker keeps the previous background. The selected swatch shows a
checkerboard for transparency or the hex code for a custom colour.
**Invert brightness** is shown beside **Smooth edges**. On the **Data** tab,
choose **CSV (comma-separated)** or **TSV (tab-separated)**. That choice sets
both the delimiter and the filename extension. The selection summary identifies
included measurements, including hidden ones. **Save as…** leaves the dialog open for further exports.
**Copy image** or **Copy SVG** copies the complete plot using the dimensions,
background and options selected in this dialog. CSV has no clipboard action.

Use `File -> Save Plot as PDF...` or
the plot context menu to save a plot-sized PDF of the rendered plot area. Use
`Edit -> Copy Plot Image`, `Ctrl+C`, or the plot context menu to copy it to the
clipboard without the surrounding window menus or toolbars. The copy resolution
is set in **Preferences → Files & export → Copied plot image quality**. Screen
resolution preserves the current display pixels, while 300 dpi renders a higher-resolution clipboard image at the
same logical plot size, and vector SVG copies editable SVG data for applications
that accept SVG from the clipboard.

Use `Window -> Resize Plot Canvas -> Custom...` to change the displayed plot
area in pixels. Width and Height start at the current canvas dimensions.
**Lock aspect ratio** shows the locked width-to-height ratio. Clear it to edit the dimensions
independently. Locking it again preserves the newly entered ratio, rounded to
whole pixels. **Resize** applies the dimensions; **Cancel** or `Esc` discards
the edits. Toolbars and window borders are excluded from the dimensions.

Use `File -> Print Plot...`, `Ctrl+P`, or `Cmd+P` to open the system print
dialog. Printing includes only the visible plot area, scales it to the printable
page without stretching or cropping, and excludes the plot-window controls.
When the system dialog exposes a concrete PDF destination, `Print Plot...` can
also produce a page-formatted PDF. qPlot stages that file beside the selected
destination and publishes it atomically only after rendering succeeds. Use
`Save Plot as PDF...` instead when you want a plot-sized PDF without the printer
page layout.

## Live Data

qPlot can display running QCoDeS experiments. **Refresh every** in the main
window sets one interval for checking the run table and updating all live plots.
New plots and cuts inherit this interval; frozen snapshots remain unchanged.

The main window's refresh button, `File -> Refresh`, or `R` refreshes the run
table and all live plots once. In a plot window, `R` refreshes just that plot's
sources. Set the main interval to `0.0 s` for manual refresh only.

## Keyboard and Mouse Controls

**Help → Keyboard and mouse controls** shows the bindings for your platform.
For qPlot shortcuts shown below with `Ctrl`, use `Cmd` on macOS unless a
Mac-specific combination is listed.

General shortcuts:

| Shortcut | Action |
| --- | --- |
| `F1` | Show quick start help |
| `Ctrl+L` | Load a database |
| `F5` | Refresh the active window |
| `R` | Refresh with the plot canvas or run table focused |
| `Enter` in the run table | Plot all measurements in the selected run |
| `Enter` on a preview | Open that measurement as a plot |
| `Space` on a preview | Toggle its selection |
| `Ctrl+W` / `Cmd+W` | Close the current qPlot window |
| `Ctrl+Q` / `Cmd+Q` | Quit qPlot |
| `Ctrl+,` | Open preferences |
| `Cmd+M` on macOS / `Alt+Space, N` on Windows | Minimise the current window |
| `Alt+Space, X` / `Alt+Space, R` | Maximise or restore the current window on Windows |
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
| `Ctrl+Alt+C` | Show or hide the coordinates toolbar |
| `Ctrl+Alt+A` | Show or hide Cut controls in a cut window |
| `Ctrl+Alt+O` | Show or hide the data operations panel |
| `S` | Snap the 1D coordinate readout to the nearest trace point |

Heatmap shortcuts:

| Shortcut | Action |
| --- | --- |
| `C` | Autoscale the colour range |
| `H` | Open a horizontal cut |
| `V` | Open a vertical cut |
| `Ctrl+Tab` / `Ctrl+Shift+Tab` | Select the next / previous visible cut on the canvas, in creation order |
| Arrow keys | Move the selected cut cursor by one cell; new cuts are selected automatically |

Dynamic context-menu entries are numbered or underlined. Once a menu is open,
press the shown number or letter to trigger that entry.
