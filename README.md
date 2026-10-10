# QCoDeS-Plotter

[![release](https://img.shields.io/github/v/release/lairdgrouplancaster/QCoDeS-Plotter?label=release)](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/releases/latest)
[![pre-release](https://img.shields.io/github/v/release/lairdgrouplancaster/QCoDeS-Plotter?include_prereleases&label=pre-release&sort=semver)](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/releases)

QCoDeS-Plotter, or qPlot, is a PyQt-based data viewer for QCoDeS databases. It
is designed for inspecting completed and running experiments, with live refresh,
line plots, heatmaps, 1D cut extraction, CSV export, and simple data operations.

## Requirements

qPlot supports standard CPython 3.11–3.14. Free-threaded Python and PyPy are
outside the supported matrix.

Runtime dependencies are declared in `pyproject.toml` and are installed
automatically when qPlot is installed.

Application development and Git installs use a separately installed native
platform wheel for trusted live QCoDeS access. A C compiler is required only
when building the native distribution from source.

Native wheels are prepared for Windows x64, macOS ARM64, macOS Intel, and Linux
x86_64 (`manylinux_2_28`, requiring glibc 2.28 or newer). Windows and macOS are
the supported desktop platforms. Linux receives headless application, reader
and installation validation; its interactive desktop GUI is not yet supported.
Linux may need system Qt libraries such as `libegl1`. Other architectures,
32-bit Windows and musl-based Linux have no native wheel. Their ordinary PyPI
installation fails with a missing compatible distribution instead of compiling.

For a supported local database, qPlot now attempts trusted live access first.
One application read broker uses one persistent helper to publish the basic run
list, detect later commits, fill run metadata progressively, and load the
selected run's plain detail view without copying the database. Main-database,
WAL, and rollback-journal handles remain read-only; SQLite may update only the
exact colocated `-shm` file as transient WAL coordination state.

Normal `qplot`, `python -m qplot`, and `qplot.run()` launches put the complete
qPlot process tree behind a shutdown boundary before Qt or reader helpers can
start. The command-line process is the launcher. A public `qplot.run()` call
first starts a dedicated Python launcher, so the calling process, its threads,
its QCoDeS writer, and unrelated children stay outside that boundary. POSIX
uses a dedicated GUI session/process group whose leader is retained until it is
reaped; Windows assigns the GUI atomically to a retained kill-on-close Job
Object and keeps the original handles. The API launcher reports only after the
GUI tree is gone, through a private authenticated channel whose EOF remains
usable even if a caller thread reaps the launcher first. `qplot.run()` returns
70 for forced shutdown and represents POSIX signal termination without
signalling its caller as `-signal_number`. If the caller receives
`KeyboardInterrupt`, `SystemExit`, or another control-flow exception while
waiting, it sends an irreversible authenticated cancellation and re-raises the
original exception only after the launcher has killed and reaped its GUI/helper
tree and exited. A dedicated cancellation writer retains partial-send progress;
one serialized lifecycle permits exactly one worker creation and start attempt,
or one committed write-side-EOF fallback if startup fails. Later interrupts are
absorbed until authenticated outcome, launcher EOF, and exit/reap observation
are all complete, so they cannot replace the first exception or release a live
tree. During that bounded interval qPlot transactionally installs a temporary
SIGINT absorber and always restores the exact caller handler, including when an
interrupt lands after either signal-handler side effect. Caller-channel EOF
triggers the same fail-closed cleanup. A
confirmed shutdown therefore has
one immutable absolute deadline even if native Qt teardown blocks. The special
`qplot.run(return_objects=True)` form instead runs in the caller's process and
returns caller-owned Qt objects; it intentionally does not acquire the
launcher's process-tree containment or hard-deadline guarantee.

Snapshot fallback is limited to an unavailable native backend or an explicitly
unsupported source or filesystem that the legacy access probe can verify
safely. Ordinary fallback selection is basic-only: it renders cached run-list
fields and an unavailable detail state without starting a selected-detail
reader or preparing an additional selected-detail snapshot. That claim applies
only to row selection: fallback metadata and retained preview paths can still
create private snapshots. Plot and CSV actions also acquire action-owned
snapshots. Automatic trusted live
previews and thumbnails are disabled until the Stage 5 scheduler and disk cache
are implemented. See
[Trusted live QCoDeS reader](docs/trusted-live-reader.md).

## Packages

The application distribution is `qplotter`; its Python imports and
console commands remain `qplot`, `qplot-cfg`, and `qplot-generate-db`.
The separately built `qplotter-native==1.0.0` supplies the protected
SQLite reader through `qplot_native`. The application's exact dependency pin
installs the compatible native wheel automatically. Both packages pin the same
APSW/SQLite runtime, preserving trusted live reading and database protections.

Contributors can edit the application without a C compiler:

```console
python -m pip install --only-binary=:all: -e ".[dev]"
```

See [Contributing](CONTRIBUTING.md) and [Distribution](docs/distribution.md)
for prepublication development using a local native wheel, explicit native
source builds, and release validation.

## Install

Install qPlot inside a supported Python virtual environment. This checkout
prepares the first split PyPI release, application `1.6.0b3` with native `1.0.0`;
the commands targeting these packages work after publication. Until then, use
validated local artifacts as described in [Distribution](docs/distribution.md).

### Prelude
#### Windows

Create a folder for qPlot, open it in Terminal, and execute:
```console
py -3.11 --version
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
```
#### macOS / Linux

Create a folder for qPlot, open Terminal there, and execute:
```console
python3 --version
python3 -m venv .venv-mac
source .venv-mac/bin/activate
python -m pip install -U pip
```
### Installing your chosen version

For ordinary installation after a stable PyPI release is available:

```console
python -m pip install --only-binary=:all: qplotter
```

For the first split beta, explicitly select the prerelease:
```console
python -m pip install --only-binary=:all: qplotter==1.6.0b3
```

Both commands install the full runtime dependency set as wheels, with no C
compiler. To follow subsequent betas, add `--pre`. Check the selected Python
interpreter and platform if pip reports no compatible wheel.

Historical releases retain their old packaging. For example, the earlier full
release remains available from its Git tag:
```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.5.0
```

### Replacing an older installation

Older releases used the distribution name `qplot`. A fresh virtual environment
is the simplest replacement. To reuse an environment, close qPlot, uninstall
the old distribution and any overlapping split installation **before**
installing the new packages:

```console
python -m pip uninstall -y qplot qcodes-plotter qcodes-plotter-native qplotter qplotter-native
python -m pip install --only-binary=:all: qplotter==1.6.0b3
python -m pip check
```

Do this even if the old installation was editable. Installing the new name
over `qplot` leaves two distributions owning the same `qplot` import package
and console commands; uninstalling the old one afterward can remove the new
installation's files. Settings and measurement databases are outside these
packages and are unaffected by pip uninstall.

### Troubleshooting

If the version check above reports Python 3.10 or older, install Python 3.11 or newer
first and use that launcher instead, for example `python3.12` on macOS.

Virtual environments are not portable between operating systems. If the
checkout is synced between systems, make sure VS Code is using the interpreter
created for the current system:

* Windows: `.\.venv\Scripts\python.exe`
* macOS: `./.venv-mac/bin/python`
* Linux development: `./.venv-linux/bin/python`

### Check the install

```console
qplot-cfg -version
python -c "import qplot; print(qplot.__file__)"
```

## Run

Start the app from an activated virtual environment:

```console
qplot
```

To open a database directly:

```console
qplot path/to/database.db
```

For file-manager `Open With` and double-click setup, see
[Opening Databases from the File Manager](docs/user-guide.md#opening-databases-from-the-file-manager).

You can also run:

```console
python -m qplot
```

or start it from Python:

```python
import qplot

qplot.run()
```

## Basic Use

1. Open qPlot.
2. Drag a QCoDeS `.db` file onto the database path field, or use
   `File -> Load Database...`.
3. Select a run in the run table.
4. Plot a measurement using the run-table context menu, or enter a run ID and
   measurement number at the top of the window. Snapshot fallback sessions may
   also offer a preview that can be double-clicked.

Plot windows may appear before their data has finished loading. Check the
status bar at the bottom of the plot window before assuming a load has failed.

For the installed `1.5.0` release, see its
[versioned user guide](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/blob/v1.5.0/docs/user-guide.md).

Likewise, use the
[1.5.0 troubleshooting guide](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/blob/v1.5.0/docs/troubleshooting.md)
for this release.

For release history, see [CHANGELOG.md](CHANGELOG.md).

## Configuration

On first run, qPlot creates:

```text
~/.qplot/config.json
```

Useful commands:

```console
qplot-cfg -info
qplot-cfg -version
qplot-cfg -dump
qplot-cfg -find user_preference.theme
qplot-cfg -set_value user_preference.theme dark
qplot-cfg -reset
```

For all config keys, defaults, validation rules, and contributor notes, see
[docs/configuration.md](docs/configuration.md).

## Development

For development setup, test commands, and contribution workflow, see
[CONTRIBUTING.md](CONTRIBUTING.md).

For a short map of the codebase, see [docs/architecture.md](docs/architecture.md).

For demo data and screenshot workflow notes, see [docs/demo-data.md](docs/demo-data.md).

For release and packaging notes, see [docs/distribution.md](docs/distribution.md).

Local development helper scripts are documented in
[scripts/README.md](scripts/README.md).
