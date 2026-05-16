# Scripts

This directory contains local development helpers. They are not installed as
package entry points and should be run from the repository root using the
project development environment.

## `manual_run.py`

Starts qPlot for a manual GUI smoke check:

```console
python scripts/manual_run.py
```

Use this after `python -m ruff check .` and `python -m pytest` when a change
affects runtime behavior or the GUI. If startup fails, the script runs the SQL
repair helper before re-raising the original exception.

## `liveplot.py`

Generates synthetic QCoDeS data using mock instruments. It creates or updates:

```text
tests/data/qplot-demo.db
```

The database is ignored by Git. Run this only when you intentionally want fresh
synthetic data for manual live-plot testing.

## `time_stress.py`

Runs qPlot with timing instrumentation for 2D refresh checks. It appends timing
rows to CSV files in the configured qPlot directory, usually `~/.qplot`.

Use this only for local performance investigation. The generated CSV files are
not part of the project source.

## `capture_demo_screenshots.py`

Generates the PNG screenshots used by `docs/demo-data.md`:

```console
python scripts/capture_demo_screenshots.py
```

The script creates a small temporary QCoDeS database under the system temp
directory by default, starts qPlot offscreen, and writes the screenshots into
`docs/assets`. Set `QPLOT_DEMO_WORKDIR` to choose a different working folder.
