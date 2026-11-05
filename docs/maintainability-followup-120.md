# Maintainability follow-up to PR #120

Reviewed `main` at `75b51ae1bd7d7aff707957ccd866d74bdde479eb` on
5 November 2026, after fetching the configured remote. This is the merge of
[PR #120](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/120).
The review considered its final description and discussion before selecting
these ten additional improvements.

| # | Finding on the reviewed revision | Improvement | Behavior to preserve |
| --- | --- | --- | --- |
| 1 | Trace, heatmap, and mixed appearance dialogs repeat selection normalization, destination adjustment, and row movement. | Share a pure row-order calculation in `_plot_appearance.py`. | Stable selected-row order, destination clamping, and each dialog's existing refresh/no-op policy. |
| 2 | `_plot_drop_axes.py` represents axis identity and candidate ranking through positional tuples. | Give identities and candidates named immutable records. | Coordinate-versus-measurement matching, stable ranking ties, native transform checks, and exact warnings. |
| 3 | Colorbar filters repeat labels, widget attributes, settings keys, and source-group mappings across dialog construction, synchronization, and catalogue selection. | Share one registry of immutable filter specifications. | Existing named controls and slots, display order, subtype filtering, and settings rollback. |
| 4 | Array previews import private numeric/raster helpers from `trusted_derived_rendering.py`, which imports the array renderer in return. | Move the shared raster operations and error types into `_trusted_preview_raster.py`. | Renderer version, encoded image bytes, numeric precision, sample limits, and cancellation points. |
| 5 | `trusted_derived_cache.py` mixes canonical encoding and bounded JSON parsing with filesystem locks, index transactions, and artifact ownership. | Isolate the pure codec in `_trusted_cache_codec.py`. | Canonical key bytes, filenames, persisted format, validation limits/errors, and all filesystem ownership rules. |
| 6 | The plot worker returns a seven-field positional heatmap summary, then indexes or unpacks it at selection sites. | Name the summary fields for row count and per-axis bounds/cardinality. | SQL, count-versus-bound ordering, numeric aggregation, and cancellation. |
| 7 | `desktop.py` embeds Windows registry preflight, ownership checks, writes, and rollback alongside launcher provisioning. | Move the association transaction into `_desktop_associations.py`. | Setup entry points, native registry value types, exact rollback order, and retained ownership when rollback fails. |
| 8 | `testdata.py` interleaves synthetic sample arithmetic with QCoDeS measurement setup and writes. | Isolate bounded sample synthesis in `_testdata_samples.py`. | Seeded RNG consumption, sweep order, endpoint precision, chunk size, cancellation, and publication permissions. |
| 9 | `_desktop_sidecars.py` validates persisted records but passes raw dictionaries paired with decoded tuples through mutation and rollback. | Use immutable named internal records after validation. | JSON schema, validation order, previous native values, later-owner checks, and rollback semantics. |
| 10 | QCoDeS-style CSV names, measurement CSV names, and plot exports repeat the same character sanitizer. | Share `suggested_export_filename` in the existing `_export_names.py` module. | Unicode alphanumeric characters, spelling, method entry points, and separate destination approval. |

## Intentional decisions retained

This follow-up does not mechanically address the automated findings discussed
in PR #120. `BaseException` cleanup at shutdown and ownership boundaries,
Protocol ellipses, Qt sorting hooks, test assertion sentinels, the snapshot
equality fallback, and protocol constants used by other modules retain their
existing purpose. The protocol version and wire format do not change.

The database-access invariant in [Agents.md](../Agents.md) continues to apply.
Viewer handles and transaction lifetimes are unchanged. Only the pinned VFS
may perform the already permitted SHM coordination; no new database writes,
checkpoints, schema changes, or sidecar operations are introduced. Synthetic
sample calculation remains separate from the explicit generation/publication
workflow. Settings formats and compatibility policy are unchanged.

## Validation

All Python checks used `.venv-mac/bin/python` and
`MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache`.

- Focused export/window checks: 351 passed and 21 subtests passed.
- Focused data/cache/numeric checks: 499 passed, 19 platform skips.
- Focused appearance/drop/filter checks: 279 passed, two intentional skips,
  and 48 subtests passed, including 13 shared row-order edge cases.
- Focused desktop/generator checks: 218 passed, four Windows-only skips.
  The native macOS applet test required execution outside the sandbox;
  its initial sandbox-only compilation failure passed on that rerun.
- Ruff, mypy across 134 source files, and `git diff --check` passed.
- Application wheel and sdist builds and Twine metadata checks passed. Both
  artifacts contain all four new modules with exact source bytes; the sdist
  also includes the new regression tests and findings table.
- Real `.venv-mac/bin/python -m qplot` offscreen startup observed a visible main
  window and running event loop, then requested normal shutdown after two
  seconds. Isolated temporary settings were used; exit status was zero.
- Differential comparisons with the reviewed `main` matched 576 colorbar
  filter combinations, 3,888 drop-axis plans, 4,092 row moves, and 696 generated
  chunks (array bytes, shapes, and dtypes), plus RNG state across 64 runs.
- All 15 extracted raster/codec functions and classes, all 11 retained cache
  functions/classes (including their I/O methods), and the three extracted
  association definitions are AST-identical to the reviewed source.
- Independent review found no additional regressions in the runtime, data,
  or export changes. Native Windows API execution remains a CI check.
