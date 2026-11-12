# Scripts

This directory contains local development helpers. They are not installed as
package entry points and should be run from the repository root using the
project development environment.

## `render_icons.py`

Rebuilds the packaged PNG, Windows ICO, and macOS ICNS icons from the editable
masters in `src/qplot/assets`: `qplot` (program), `qplot-database`, `sqlite-wal`,
and `sqlite-shm`. Each has a main `.svg` and a `-small.svg` master:

```console
python scripts/render_icons.py
```

Use the project development environment, which supplies PyQt6 and Pillow.
On macOS, prefix the command with
`MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache` and use `.venv-mac/bin/python`
unless the environment is activated. The ICO includes a dedicated 16-pixel
version; larger sizes are rendered directly from the main vector artwork.
Commit the regenerated assets with the SVG changes. Desktop setup uses these
prebuilt files and does not run this script.

## `manual_run.py`

Legacy public-API launch helper:

```console
python scripts/manual_run.py
```

For a manual GUI smoke check, use `python -m qplot` instead: it propagates the
launcher’s exit status. `manual_run.py` discards the status returned by the public
API, so a failed child launch can still leave this script with exit status zero.
Its repair handler runs only when the API raises an `Exception`, not when it
returns a failure status. See [contributor checks](../CONTRIBUTING.md#manual-gui-check)
for the project-venv and macOS Matplotlib cache requirements.

## `liveplot.py`

Generates synthetic QCoDeS data using mock instruments. It creates a new database:

```text
tests/data/qplot-demo.db
```

The database is ignored by Git. The script refuses an existing database or any
WAL, SHM or rollback-journal sidecar. Use a fresh working directory for another
synthetic database for manual live-plot testing.
From the fresh working directory, invoke `liveplot.py` using its absolute path
inside the repository.

## `time_stress.py`

Runs qPlot with timing instrumentation for 2D refresh checks. Each plot creates
its own new CSV in the configured qPlot directory, usually `~/.qplot`, and prints
the path. Later rows use the same file, retained after the plot closes. Existing
files are preserved.

Use this only for local performance investigation. The generated CSV files are
not part of the project source.

## `benchmark_heatmap_aggregation.py`

Creates a temporary 2,000,000-row heatmap database and reports the direct SQL
spatial-aggregation load and spatial-mean preview generation times separately
from database creation:

```console
python scripts/benchmark_heatmap_aggregation.py
```

The benchmark asserts that every source row contributes, the plotted grid stays
within its configured cell bound, all plotted values are finite, and a bounded
preview image is produced. The temporary database is removed automatically.

## `capture_demo_screenshots.py`

Use the candidate application and matching native package with
`requirements/screenshots.txt`. The script captures real qPlot windows with
isolated settings, fixed synthetic data and bounded readiness checks. It stages
and validates all four images and records provenance before installing the set.
For release preparation, workflow bootstrap and final pre-tag verification, see
[documentation releases](../docs/documentation-releases.md).

## `screenshot_support.py`

`validate` checks committed PNGs and their documentation references without
requiring fresh screenshots for ordinary code PRs. `verify-release` additionally
checks the candidate's input fingerprint, application version, generation checks
and hashes of the reviewed set. Use `--require-clean --generation-id RUN/ATTEMPT`
on the final candidate before tagging. This command does not authorise tagging.

## Screenshot capture command

Generates the PNG screenshots used by `docs/demo-data.md`:

```console
python scripts/capture_demo_screenshots.py
```

The script creates a small temporary QCoDeS database under the system temp
directory by default, starts qPlot offscreen, and writes the screenshots into
`docs/assets`. Set `QPLOT_DEMO_WORKDIR` to choose a different working folder,
or `QPLOT_DEMO_ASSET_DIR` to write screenshots somewhere other than
`docs/assets`.
