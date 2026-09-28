# Troubleshooting

This page covers common setup and runtime problems. For normal usage, see
[user-guide.md](user-guide.md).

## Creating a Virtual Environment

Use Python 3.11 or newer.

Windows PowerShell:

```console
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS:

```console
python3.11 -m venv .venv-mac
source .venv-mac/bin/activate
```

Linux development:

```console
python3.11 -m venv .venv-linux
source .venv-linux/bin/activate
```

Your prompt should start with the virtual environment name, such as `(.venv)`
on Windows, `(.venv-mac)` on macOS, or `(.venv-linux)` on Linux. Windows and
macOS are the currently supported and GUI-tested desktop platforms. A source
install may work on Linux, but Linux is not currently part of the GUI test or
support matrix.

## Creating a Virtual Environment in VS Code

If you prefer VS Code to terminal setup:

1. Use `File -> Open Folder...` to open a working folder.
2. Open the Command Palette with `Ctrl+Shift+P`.
3. Run `Python: Create Environment`.
4. Choose `Venv`.
5. Choose a Python 3.11 or newer base interpreter.
6. Open a new VS Code terminal with `Terminal -> New Terminal`.

The terminal prompt should start with the virtual environment name described
above. Avoid choosing an interpreter inside `anaconda3`, `miniconda3`, or an
`envs` folder unless you intentionally manage this project with Conda.

## VS Code Reports a Broken Virtual Environment

If VS Code reports a broken `.venv` on macOS, it is usually a Windows virtual
environment synced into the checkout. Delete or recreate that environment on
the current operating system, or keep virtual environments outside the synced
folder.

## `git` Is Not Found During Install

The GitHub install command requires Git. Use the explicit release tag from the
README; for example, the current full release is:

```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.4.0
```

Install Git, then open a new terminal before running the install command again.
Do not substitute `@main` unless you intentionally want unreleased code.

## PowerShell Blocks Virtual Environment Activation

If PowerShell refuses to activate `.venv`, allow script execution for the
current shell process:

```console
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

This changes the policy only for the current PowerShell session.

## `qplot` Command Is Not Found

Check that the virtual environment is activated. You can also start qPlot with:

```console
python -m qplot
```

If that works, the package is installed but the console script is not on the
current `PATH`. Open a new activated terminal or reinstall qPlot inside the
intended environment.

## Python Imports the Wrong `qplot`

Print the import path:

```console
python -c "import qplot; print(qplot.__file__)"
```

If the path points outside the virtual environment or outside the checkout you
expect, activate the right environment and reinstall qPlot.

## The App Opens but No Database Is Loaded

Load a database with `File -> Load Database...`, use `File -> Load Recent
Database`, or drag a QCoDeS `.db` file onto the database path field.

qPlot tries the newest path in its recent-database list at startup. If that file
has been moved or deleted, qPlot tries QCoDeS' configured database and otherwise
continues with an empty main window.

## A Database Does Not Load

Check the following first:

* The selected file exists.
* The file extension is `.db`.
* The file is a QCoDeS SQLite database.
* The file is not stored in a folder where another process blocks access.

An ordinary running QCoDeS experiment on a supported same-host local filesystem
should load through trusted live access without waiting for its WAL to be
checkpointed. qPlot attempts that bounded open and its basic run query before
the legacy access probe. For persistent failures, use the `Database
Information` button if the file can be opened far enough for diagnostics.

Snapshot fallback is deliberately narrow. It is considered only when the exact
type of the initial trusted-open failure reports that the pinned native backend
is genuinely unavailable or the source or filesystem is explicitly
unsupported. The legacy probe must still prove a safe snapshot. Cancellation,
deadline or busy expiry, source replacement, invalid database, source I/O, SQL
or result-limit failure, helper or protocol failure, forced termination, and
uncertain cleanup never become fallback and are never silently replayed.

If qPlot reports that a WAL cannot be verified, trusted access was unavailable
or an explicit plot/CSV action reached the retained snapshot path. SQLite's WAL
salts and checksums validate the WAL's own frames; they do not identify the main
database to which those frames belong. A matching filename or successful SQLite
open is not enough, so qPlot refuses the snapshot rather than showing unrelated
data or an older main-file-only view.

For that snapshot-only failure, close every QCoDeS, SQLite, Python, and notebook
connection that owns the database cleanly, then retry the action. SQLite
normally checkpoints the WAL when its final connection closes. If it remains,
use the owning application or writer to checkpoint it. Do not remove a live WAL
manually.

During trusted live access, main-database, WAL, and rollback-journal handles are
physically read-only. SQLite may create or update only the exact colocated
`-shm` file as transient WAL coordination state, so its contents, size,
timestamps, or permission mode can change. qPlot never checkpoints the source
or writes experimental data. Network and other unsupported filesystems can take
snapshot fallback only if the legacy probe verifies them safely; otherwise the
load fails with actionable guidance. See
[Trusted live QCoDeS reader](trusted-live-reader.md).

WALs for current qPlot-generated test databases can remain readable by snapshot
consumers because they contain a unique generation token and a parent-linked
random lineage chain. On private copies qPlot proves that the WAL head strictly
descends from the exact main head and that every committed WAL transaction
carries the lineage-state page. A different token, equal head, missing event,
divergent clone, malformed WAL, or exhausted proof window is rejected by the
same fail-closed policy. A writer that changes an otherwise trusted main or WAL
throughout every snapshot attempt can also make the database temporarily
unavailable; wait for a pause and retry.

QCoDeS creates a new SQLite result table for each later measurement. If those
runs must be readable by snapshot fallback or a deferred snapshot consumer
before the WAL is checkpointed, call
`qplot.testdata.enable_generation_provenance_for_writer(connection)` on the
owner application's quiescent writable QCoDeS `AtomicConnection`. This installs
coverage for the new table in its creation transaction, including when results
will later be written in the background. The hook refuses a nonempty WAL so it
cannot bless earlier uninstrumented frames. Use the owning writer to checkpoint
that WAL with `TRUNCATE`, then enable the hook before the next measurement. The
hook takes SQLite's writer lock before checking WAL quiescence, so concurrent
writes cannot race that check. qPlot cannot safely repair or upgrade provenance
while viewing the input.

Older epoch-only generated databases remain readable by the snapshot path
without a live WAL. Their live WALs fail closed on that path because an epoch
cannot prove branch ancestry. To migrate,
first make the owner database quiescent in rollback-journal mode, then call the
writer hook; it rotates the token and seeds the current lineage format. If more
than 65,536 lineage events accumulate after the main's last checkpoint, the
retained proof window is exhausted and the owner must checkpoint before qPlot
can safely accept another live WAL.

For a rollback-format database, qPlot uses normal SQLite read-only locking while
the database is quiescent. If a `-journal` is present, qPlot captures the main
file and journal into a private snapshot and allows SQLite recovery only there.
Cold PERSIST and zero-length TRUNCATE journals are valid and should load
normally. If the main file or journal changes during every capture attempt, or
their transaction state is ambiguous, qPlot reports that the database is busy
or temporarily unavailable. Finish or pause the writing transaction and
refresh; qPlot will not fall back to a source mode that could expose
uncommitted pages or modify the database and its sidecars.

## A OneDrive Database Waits for Sync

On macOS, OneDrive and other cloud providers can leave a `.db` file as an
online-only placeholder until an application reads it. During initial path
handling, qPlot hydrates a detected cloud placeholder before attempting the
trusted open; it does not run the legacy snapshot access probe first. The
database loading strip and status bar show progress such as `Waiting for
OneDrive sync...`, and the load can be cancelled without closing qPlot. If the
provider does not make the file available within the configured timeout, qPlot
stops waiting and reports a database-load error.

If the message stays visible for a long time, check that OneDrive is running,
signed in, and allowed to download the file. You can also mark important
database folders as always available in Finder. The timeout can be changed with
`qplot-cfg -set_value runtime_settings.cloud_sync_timeout 180`.

## Trusted Run Details or Previews Stay on Loading

Trusted metadata, dimensions, thumbnails, and selected-run previews populate
progressively after the cheap run list appears. Work is prioritised for the
selected row, then rows visible in the actual table viewport, then the rest of
the database. Large databases can therefore retain loading placeholders for
off-screen runs while foreground work is already usable; if left idle, all
eligible runs are eventually populated. Scrolling to or selecting a run
promotes its pending work without starting a second worker.

Decoded full previews use a bounded in-memory cache. Revisiting an evicted run
may briefly show loading while qPlot requests only that preview again, normally
from the derived disk cache; it does not repeat metadata or thumbnail work.
Run-list thumbnails are not decoded for databases above the inline-preview
threshold. Reselecting the already active database preserves the trusted
preview binding and existing cached previews.

Unsupported, malformed, empty, or individually failing runs show a bounded
unavailable state and do not stop other runs. Check `~/.qplot/qplot.log` if a
supported run remains unavailable. Cache permission or corruption problems are
treated as misses and rendering continues without disk caching. The derived
cache lives in qPlot's application-cache directory and must never be moved into
or above the database directory.

Trusted live sessions do not use the legacy snapshot-backed detail, thumbnail,
or preview workers. Explicit plot and CSV actions still open their own deferred
action-owned dataset, and snapshot fallback retains its documented narrower
behavior. If the accepted database or helper incarnation changes, obsolete
derived results are discarded and current work is regenerated.

## Plot Windows Look Empty

Plot windows can open before data loading has finished. Check the status bar at
the bottom of the plot window. If it says data is loading or processing, wait
for the load to complete.

If the window stays empty:

* Refresh manually with `R`.
* Check that the run contains plottable dependent parameters.
* Check whether the selected measurement has only finite data.
* Close and reopen the plot if the underlying run was still being initialized.

## Live Data Does Not Update

Main-window refresh and plot-window refresh are separate.

* The main-window broker checks `data_version` through its persistent helper. An
  unchanged value avoids redundant metadata work; a change discovers later run
  IDs and refreshes the selected run plus watched unfinished runs, with visible
  watched rows first.
* Each plot window remains a separate action-owned dataset consumer and checks
  for new data in that run.

Set the relevant refresh interval above `0.0 s`, or refresh manually with `R`.
Overlapping main-window timer requests are coalesced. If qPlot reports that the
accepted database or one of its sidecars was replaced, reload it so a fresh
helper incarnation can establish a new identity and `data_version` baseline.

## Configuration Problems

qPlot stores settings in:

```text
~/.qplot/config.json
```

Print the current config:

```console
qplot-cfg -dump
```

Reset all settings to defaults:

```console
qplot-cfg -reset
```

If `config.json` is invalid JSON, incomplete, or from an unsupported settings
format, qPlot backs it up in `~/.qplot` with a name such as
`config.invalid.json` and creates a fresh config from defaults. This can happen
after a major-version upgrade.

## Diagnostic Logs

qPlot writes diagnostic messages and tracebacks to:

```text
~/.qplot/qplot.log
```

Check this file when a user-facing error dialog does not contain enough detail.
The log records startup, database loads, refresh failures, CSV export failures,
plot-opening failures, preview-generation failures, and background worker
errors.

Inside qPlot, use `Help -> Copy Diagnostic Log Path` to copy this path to the
clipboard.

## Theme or Preference Changes Do Not Look Right

Use `Options -> Reset All Settings...` from the app, or run:

```console
qplot-cfg -reset
```

Then restart qPlot. Resetting defaults closes current plot windows and clears
the loaded database from the main window.

## Development Checks Fail Locally

Use the project virtual environment and run checks through Python:

```console
python -m ruff check .
python -m mypy
python -m pytest
```

For release or packaging failures, rebuild and validate the package artifacts:

```console
python -m build
python -m twine check dist/*
```

Do not run bare `pytest`; use `python -m pytest` from the active project
environment. More development setup details are in
[../CONTRIBUTING.md](../CONTRIBUTING.md).
