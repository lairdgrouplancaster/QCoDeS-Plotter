# Workflow performance and stability

Use the existing plot-launch benchmark to measure the actual Qt application,
including bounded trusted reads, rendering, refreshes and shutdown. The optional
refresh and selection loops also exercise repeated opening and closing. These
are diagnostic measurements, not timing assertions in ordinary CI.

## Reproduction

Use the project venv, with the `dev` extra installed (including `psutil`). Create
a new synthetic workload; the generator refuses to replace an existing file:

```powershell
python scripts/create_benchmark_database.py workload.db
python scripts/benchmark_plot_launch.py workload.db 1 --repeats 12 --refreshes 2 --selection-rounds 2 --output line.json
python scripts/benchmark_plot_launch.py workload.db 2 --repeats 12 --refreshes 2 --selection-rounds 2 --output heatmap.json
```

The default workload has 30 completed QCoDeS runs: a 20,000-point line, a
2,250,000-point heatmap and 28 short runs for selection changes. Its numerical
data uses a fixed random seed. QCoDeS supplies fresh identities and timestamps;
use the same generated database for comparisons between application versions.
The benchmark opens it through the trusted reader and never checkpoints it.
It verifies exact main-database, WAL and rollback-journal hashes and file
observations after shutdown. SQLite may update the permitted SHM file.

Use the appropriate venv path when it is not activated. On macOS prefix Python
commands with `MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache`. For a repeatable
headless comparison set `QT_QPA_PLATFORM=offscreen`; record this choice because
it does not measure the desktop compositor or physical display. Run comparisons
sequentially without competing test suites. Keep the benchmark script identical
when changing the application source through `PYTHONPATH`.

Each JSON report records the application source fingerprint, package versions,
platform, source hashes, database-open time, display and refresh times, event-loop
timer delay, process RSS, helper RSS, threads, handles/file descriptors, retained
plot objects and active workers. Report files are created exclusively and must
use a new `.json` path. Resource sampling follows close, a bounded settling
interval, deferred deletion and garbage collection. It does not forcibly delete
plot windows to make the observations pass.

Treat initial cycles as warm-up. RSS includes allocator and library caches, so
a rise followed by a plateau does not establish a leak. Continuing growth in
plot objects, handles, workers or memory deserves investigation. The event-loop
probe measures lateness of a 10 ms Qt timer; it includes synchronous work on the
GUI thread and operating-system scheduling. It is not input-to-display latency.
The selected-run loop rapidly visits up to ten runs between plot cycles.

## Assessment

Measured on 17 November 2026 with Windows 11, Python 3.14.8, PyQt6 6.12.0,
QCoDeS 0.63.0 and the offscreen Qt platform. Both versions used the same 80.5 MB
synthetic source, the same benchmark script, 12 open/close cycles, two forced
refreshes and two rapid-selection rounds per cycle. No test suite ran alongside
the recorded comparisons. The baseline is the application merged in PR #153
(`3a97348`); full source fingerprints and environment details are in the reports.

| Observation | Baseline | Candidate |
| --- | ---: | ---: |
| Closed line plots still retained after 12 cycles | 12 | 0 |
| Line-process RSS, first to final cycle | 162.4 to 225.6 MiB | 157.9 to 167.4 MiB |
| Line-process RSS range over cycles 7-12 | 198.5-225.6 MiB | 165.3-168.5 MiB |
| Closed heatmaps still retained after 12 cycles | 12 | 0 |
| Heatmap-process RSS, first to final cycle | 247.8 to 375.1 MiB | 247.2 to 252.3 MiB |
| Heatmap-process RSS range over cycles 7-12 | 319.2-375.1 MiB | 247.8-252.3 MiB |
| Warm line display / refresh median | 575 / 203 ms | 502 / 198 ms |
| Warm heatmap display / refresh median | 390 / 97 ms | 379 / 93 ms |

Warm timings exclude the first two cycles. This is one sequential comparison
per workload, not a statistical claim of a speed-up. The clear result is the
removal of continuing plot retention: close previously hid the window but left
its native Qt objects and signal closures alive. Deferred retirement now waits
for the final consumer and refresh callback, then releases the native window.
The plot refresh timer is also parented to its window. Real-window regressions
reproduce the missing retirement on the baseline and protect retained overlays
and closure during publication or completion feedback, including nested event
loops that must not dispose of a window while its callback is still running.

The candidate's database-open times were 520 ms for lines and 545 ms for heatmaps.
The 10 ms event-loop probe had aggregate 95th-percentile lateness of 85 ms for lines
and 41 ms for heatmaps. Its largest startup/load delays were about 1.1 s and
1.0 s. Within the repeated cycles the maxima were 203 ms and 438 ms; the latter
occurred in the first heatmap cycle, with subsequent heatmap maxima below
216 ms. These observations leave room for future startup/responsiveness work
if it matters in ordinary use, but do not justify another general refactor.

No plot or snapshot-preview workers remained active at the sampled close
boundaries. The trusted-reader helper remained alive as expected while the main
database view was open; its sampled RSS stayed between 40-45 MiB for lines and
61-64 MiB for heatmaps. All four runs completed normal shutdown and preserved
protected source-file hashes and observations.

Raw reports are committed under `docs/assets/benchmarks/2026-11-17/` as
`baseline-line.json`, `baseline-heatmap.json`, `final-line.json` and
`final-heatmap.json`. They are repository evidence, excluded from the published
documentation site with the other JSON artefacts. The candidate application
fingerprint is `991f68789c0a4aa4ba7e21b89d7a513aa1d6bd294feaddca3248596f03988230`.
Full platform tests remain separate correctness evidence; a local performance
run cannot establish behaviour on another operating system. After the preview
recovery and measured retention fixes, pause broad restructuring and make further
changes in response to specific failures, user-visible delays or new features.
