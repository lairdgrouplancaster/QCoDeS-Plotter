# QCoDeS-Plotter

[![release](https://img.shields.io/github/v/release/lairdgrouplancaster/QCoDeS-Plotter?label=release)](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/releases/latest)
[![beta](https://img.shields.io/github/v/release/lairdgrouplancaster/QCoDeS-Plotter?include_prereleases&label=beta&color=orange)](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/releases)

QCoDeS-Plotter, or qPlot, is a PyQt-based data viewer for QCoDeS databases. It
is designed for inspecting completed and running experiments, with live refresh,
line plots, heatmaps, 1D cut extraction, CSV export, and simple data operations.

## Requirements

QCoDeS-Plotter requires Python 3.11 or newer.

Runtime dependencies are declared in `pyproject.toml` and are installed
automatically when qPlot is installed.

Windows and macOS are the currently supported and GUI-tested desktop
platforms. A source installation may work on Linux, but Linux is not currently
part of the GUI test or support matrix.

## Install

Install qPlot inside a Python 3.11 or newer virtual environment:
The commands below install the latest full release, `1.4.0`. The current beta
is `1.5.0-b3`; it is documented below but is not used for the default install.

Windows:

```console
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.4.0
```

macOS:

```console
python3 --version
python3 -m venv .venv-mac
source .venv-mac/bin/activate
python -m pip install -U pip
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.4.0
```

To test the current beta instead, install its explicit tag in the same virtual
environment:

```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.5.0-b3
```

If the version check reports Python 3.10 or older, install Python 3.11 or newer
first and use that launcher instead, for example `python3.12` on macOS.

Virtual environments are not portable between operating systems. If the
checkout is synced between systems, make sure VS Code is using the interpreter
created for the current system:

* Windows: `.\.venv\Scripts\python.exe`
* macOS: `./.venv-mac/bin/python`
* Linux development: `./.venv-linux/bin/python`

Check the install:

```console
qplot-cfg -info
python -c "import qplot; print(qplot.__file__)"
```

For editable development installs, see [CONTRIBUTING.md](CONTRIBUTING.md).

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
4. Plot a measurement by double-clicking its preview, using the run-table
   context menu, or entering a run ID and measurement number at the top of the
   window.

Plot windows may appear before their data has finished loading. Check the
status bar at the bottom of the plot window before assuming a load has failed.

For the installed `1.4.0` release, see its
[versioned user guide](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/blob/v1.4.0/docs/user-guide.md).
For the current beta, see [docs/user-guide.md](docs/user-guide.md).

Likewise, use the
[1.4.0 troubleshooting guide](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/blob/v1.4.0/docs/troubleshooting.md)
for the full release or [docs/troubleshooting.md](docs/troubleshooting.md) for
the current beta.

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
