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

macOS/Linux:

```console
python3.11 -m venv .venv-mac
source .venv-mac/bin/activate
```

Your prompt should start with the virtual environment name, such as `(.venv)` on
Windows or `(.venv-mac)` on macOS/Linux.

## Creating a Virtual Environment in VS Code

If you prefer VS Code to terminal setup:

1. Use `File -> Open Folder...` to open a working folder.
2. Open the Command Palette with `Ctrl+Shift+P`.
3. Run `Python: Create Environment`.
4. Choose `Venv`.
5. Choose a Python 3.11 or newer base interpreter.
6. Open a new VS Code terminal with `Terminal -> New Terminal`.

The terminal prompt should start with the virtual environment name, such as
`(.venv)` on Windows or `(.venv-mac)` on macOS/Linux. Avoid choosing an
interpreter inside `anaconda3`, `miniconda3`, or an `envs` folder unless you
intentionally manage this project with Conda.

## `git` Is Not Found During Install

The GitHub install command requires Git:

```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@main
```

Install Git, then open a new terminal before running the install command again.

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

qPlot remembers the last database path. If that file has been moved or deleted,
startup continues with an empty main window.

## A Database Does Not Load

Check the following first:

* The selected file exists.
* The file extension is `.db`.
* The file is a QCoDeS SQLite database.
* The file is not stored in a folder where another process blocks access.

If the database is being written by a running experiment, try again after a
short wait. For persistent failures, use the `Database Information` button in
the main window if the file can be opened far enough for diagnostics.

## A OneDrive Database Waits for Sync

On macOS, OneDrive and other cloud providers can leave a `.db` file as an
online-only placeholder until an application reads it. When qPlot detects this
kind of cloud-backed path, or when the first database access check fails for a
cloud-backed path, it reads the file in the background to trigger the provider's
Files On-Demand download. The database loading strip and status bar show
progress such as `Waiting for OneDrive sync...`, and the load can be cancelled
without closing qPlot. If the provider does not make the file available within
the configured timeout, qPlot stops waiting and reports a database-load error.

If the message stays visible for a long time, check that OneDrive is running,
signed in, and allowed to download the file. You can also mark important
database folders as always available in Finder. The timeout can be changed with
`qplot-cfg -set_value runtime_settings.cloud_sync_timeout 180`.

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

* The main-window refresh interval checks for new runs in the database.
* Each plot window's refresh interval checks for new data in that run.

Set the relevant refresh interval above `0.0 s`, or refresh manually with `R`.
If a database is locked by another process, wait and refresh again.

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

If `config.json` is invalid JSON or fails validation, qPlot backs it up in
`~/.qplot` with a name such as `config.invalid.json` and creates a fresh config
from defaults.

## Diagnostic Logs

qPlot writes diagnostic messages and tracebacks to:

```text
~/.qplot/qplot.log
```

Check this file when a user-facing error dialog does not contain enough detail.
The log records startup, database loads, refresh failures, CSV export failures,
plot-opening failures, preview-generation failures, and background worker
errors.

## Theme or Preference Changes Do Not Look Right

Use `Options -> Restore Default Settings...` from the app, or run:

```console
qplot-cfg -reset
```

Then restart qPlot. Resetting defaults closes current plot windows and clears
the loaded database from the main window.

## Development Checks Fail Locally

Use the project virtual environment and run checks through Python:

```console
python -m ruff check .
python -m pytest
```

Do not run bare `pytest`; use `python -m pytest` from the active project
environment. More development setup details are in
[../CONTRIBUTING.md](../CONTRIBUTING.md).
