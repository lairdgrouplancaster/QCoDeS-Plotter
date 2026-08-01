# Demo Data

qPlot does not keep generated QCoDeS databases in the repository. For demos,
screenshots, and manual live-refresh checks, use the local helper scripts from
the repository root.

## Generate a Test Database

In qPlot, use `File -> Generate Test Data -> Create Example CSV...` to choose
where to save a ready-to-use specification. qPlot creates the file, reveals it
in Finder on macOS, or opens its containing folder in the platform file manager.
Edit the CSV in a spreadsheet application, then use
`File -> Generate Test Data -> Generate Database from CSV...` to choose the CSV
and its output `.db` file. Generation runs in the background so qPlot remains
responsive.

The installed `qplot-generate-db` command provides the same workflow from a
terminal. Start by writing an example CSV:

```console
qplot-generate-db --write-example test-runs.csv
```

Edit the CSV in a spreadsheet application, then generate the database:

```console
qplot-generate-db test-runs.csv test-runs.db
```

Every nonblank CSV row creates one run, named `run_1`, `run_2`, and so on. A 1D
row sweeps `V_SD`; a 2D row sweeps both `V_SD` and `V_G`. Each run receives a
random sinusoid amplitude and phase. The measured parameter name, label, unit,
sweep ranges, and point counts are set in the CSV. Existing CSV or database
files are not replaced unless `--overwrite` is supplied.

Run `qplot-generate-db --help` for the complete command reference.

## Screenshots

The committed screenshots below are generated from a small synthetic database.

![qPlot main window with synthetic runs](assets/qplot-main-window.png)

![qPlot line plot](assets/qplot-line-plot.png)

![qPlot heatmap](assets/qplot-heatmap.png)

![qPlot color scale dialog](assets/qplot-color-scale-dialog.png)

## Regenerate Screenshots

Run:

```console
python scripts/capture_demo_screenshots.py
```

The script creates a temporary synthetic database under the system temp
directory by default, starts qPlot offscreen, and overwrites the PNG files in
`docs/assets`. Set `QPLOT_DEMO_WORKDIR` to choose a different working folder,
or `QPLOT_DEMO_ASSET_DIR` to write screenshots somewhere other than
`docs/assets`.

## Generate Synthetic Data

Run:

```console
python scripts/liveplot.py
```

The script creates or updates:

```text
tests/data/qplot-demo.db
```

That database is ignored by Git. Regenerate it when you need fresh example runs
for screenshots or manual testing.

## Manual Demo Flow

1. Start qPlot with `python scripts/manual_run.py`.
2. Load `tests/data/qplot-demo.db`.
3. Select a run with both line and heatmap parameters.
4. Capture any additional workflow-specific views that are not covered by
   `scripts/capture_demo_screenshots.py`.

Keep screenshots focused on the actual application state. Avoid capturing local
paths, personal configuration values, or unrelated desktop windows.

## Refresh Testing

For local performance and live-refresh checks, use:

```console
python scripts/time_stress.py
```

The script writes timing CSV files into the configured qPlot directory, usually
`~/.qplot`. Those files are local diagnostics, not source assets.
