# qPlot bug-hunt campaign

## Baseline and scope

- Intended baseline: `2ece4436608a98580a540ecaafcba2781db4043f`, head of
  `codex/mixed-plots-ui-preferences` / PR #83; local working tree was clean.
- Verified all **47 checks successful**, including **Required checks**, using
  GitHub's commit check-runs API before beginning investigation.
  [Baseline CI](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/38315664438).
  The commit has no additional legacy commit statuses.
- Dedicated branch: `codex/qplot-bug-hunt`.
- Preserve AGENTS.md database-access invariant, current QCoDeS format, current
  settings validation and safe saving. No legacy settings migrations.
- No merge or release. Findings require concrete evidence and regression tests.

## Coverage and ownership

| Investigator | Initial coverage | State |
| --- | --- | --- |
| Database subchat | Native VFS, read-only/live database access, cancellation, services and scheduling | Initial audit complete |
| Measurement subchat | Extraction, dimensional interpretation, rendering, numeric operations and transforms | Initial audit complete; final numeric follow-up |
| UI/lifecycle subchat | Windows, mixed plots, refresh, removal, shutdown, worker lifetime | Initial audit complete |
| Coordinator | Settings, entry points, diagnostics, synthetic generation, packaging and integration | Initial audit complete; integration running |

Parallel edits use explicit disjoint file ownership. Coordinator owns this log
and integration commits. Substantive fixes require review by a different subchat.

## Findings, reproductions and fixes

### BH-001 — Settings recovery loops forever on a dangling backup symlink

- Reproduction: malformed `config.json` plus a dangling `config.invalid.json`
  symlink. Candidate selection uses `exists`, but exclusive creation rejects the
  occupied entry, so recovery retries the same name forever. A bounded subprocess
  regression failed with `TimeoutExpired` after 10 seconds before the fix.
- Fix: use `lexists` for base and numbered backup candidates. Preserve the link
  and its absent target, and choose the next unused directory entry.
- Regression: `tests/test_bug_hunt_config.py` covers base and numbered dangling
  links, original-byte backup, valid recovered settings and unchanged links.
- Independent review: UI/lifecycle subchat approved the fix and regression tests.
- Validation: settings, diagnostics, generation, packaging split and distribution
  suites: **222 passed, 28 subtests passed**.

## Validation

Baseline cross-platform CI passed as recorded above. Campaign validation pending.
All Python commands use the project venv; Matplotlib cache is
`/private/tmp/qplot-matplotlib-cache`.

- Actual `python -m qplot` startup with the existing packaging observation hook:
  visible window, live Qt event loop, normal shutdown, exit **0**. Temporary
  settings/logs used. Initial sandbox attempt could not bind supervisor IPC;
  approved unprivileged run outside the sandbox passed.
- Supervisor suite initially hit sandbox `socket.bind` restrictions (70 failures,
  86 passes); rerun outside sandbox: **156 passed** (26.04 s), covering auxiliary
  database protection, release validation and Windows shutdown-job unit tests too.
  Initial failures were environment failures, not application findings.
- Existing 2,000,000-row heatmap benchmark passed: 500 x 500 aggregate grid
  (250,000 cells), all finite; aggregation 2.449 s, preview 1.682 s. Synthetic
  temporary database only. This is a local observation, not a portable limit.

### BH-002 — Final live run status lost after a transient refresh failure

- Reproduction: the header accepts the writer's final `data_version`, then a
  watched status times out; later refreshes see no change and never retry status.
  Six initial regression variants failed before repair. Review exposed the same
  problem for failed BASIC_PAGE and cancellation between requests.
- Fix: thread-safe service retry marker plus worker failure/cancellation re-arm;
  next header requests status reconciliation even without another writer commit.
  Successful refresh restores the unchanged-version fast path.
- Tests: `tests/test_bug_hunt_database_refresh.py` covers both cheap/expensive
  failure, failed version probe/queued deadline, page failure and cancellation.
  Service/worker/new regression batch: **89 passed**.
- Independent review: measurement subchat requested the cycle-failure repair and
  approved the revised implementation.

### BH-003 — Trusted previews reject or misrepresent valid numeric arrays

- Reproductions: Boolean NPY coordinate/signal arrays yielded unsupported images;
  adjacent uint64 coordinates at `2**63` collapsed; repeated cell values
  `[1e308, 1e-100, -1e308]` lost their mean residual; finite extreme coordinates
  overflowed line scaling and crossing-zero heatmap colors. Review also exposed
  1D integer conversion and mixed int/float normalization loss.
- Fix: retain integer coordinates through grouping/rendering, accept numeric NPY
  booleans, use existing accurate finite mean, normalize before pixel scaling,
  and subtract mixed large numeric values exactly. Renderer cache version v7.
  Follow-up keeps repeated integer-containing cell means exact until color
  normalization; covers both integer and half-integer means at `2**63` and `2**52`.
- Tests: `tests/test_bug_hunt_measurement_previews.py` compares decoded image
  pixels to equivalent well-conditioned measurements, including real QCoDeS
  roundtrips with unchanged protected database state. **74 focused tests passed**
  including **24 new regression cases** after the mean follow-ups.
- Independent review: UI subchat approved after extending integer coverage.
- Performance: 131,072-cell preview median baseline 0.4294 s, fixed 0.4246 s
  (four runs each); singleton mean shortcut avoids per-cell allocation overhead.

### BH-004 — Pending refresh spins the GUI event loop

- Reproduction: queued callback encounters a replacement worker still running;
  original code reposted on every event-loop turn (25 callbacks in 25 turns).
- Fix: retain pending request/force state for existing worker completion to
  schedule; do not repeatedly post zero-delay callbacks.
- Test: `tests/test_bug_hunt_ui_refresh.py`; regression plus plot/lifecycle suites:
  **111 passed, 19 subtests passed**.
- Independent review: database subchat verified completion/error/cancel behavior.

### BH-005 — Removing a mixed plot source crashes when a cut remains

- Reproduction: open a heatmap cut, add it as a trace, then remove the primary
  heatmap. Source promotion constructs a cut with ordinary plot arguments,
  raising TypeError (`float(dict)`).
- Fix: promote the bottom eligible full line/heatmap source while preserving cut
  overlays and order. If only cuts would remain, disable removal with an
  explanatory tooltip/status message; cut removal itself still works.
- Tests: `tests/test_bug_hunt_ui_cuts.py` covers disabled removal without changed
  ownership, full line/heatmap promotion, live cut updates, closed cut retention,
  and final source release. **3 passed**.
- Independent review: database subchat reviewed and reran the integration cases.

## Packaging and wider coverage

- Built application sdist/pure-Python wheel; `twine check` passed both.
- Built native universal macOS cp311-abi3 wheel; `lipo` confirmed ARM64 and x86_64.
  Payload inspection found only runtime package/binary and distribution metadata.
  Installed reader passes exact native/SQLite/APSW version and source-ID checks.
  Native source unchanged; build was an audit, not an installation or release.
- Database safety/read-scope/live service/protocol/readonly batch: **212 passed,
  1 Windows-only skip**. Supervisor/partial-frame/packaging/compiler-free batch:
  **106 passed, 2 Windows-only skips**.
- Broader UI mixed drops/promotions/cut swaps/preferences/commands/SVG/CSV batch:
  **126 passed**.
- First integrated full suite: **4,604 passed, 41 skipped, 38 warnings** in
  773.11 s, using `--no-cov -n 2 --dist=loadfile --timeout=90
  --timeout-method=thread`. This run predates the generator/display/auxiliary
  follow-ups and final preview mean changes; final integration validation pending.
- Additional axes/annotations/settings/export integration batch: **237 passed**.
- Ruff, whitespace checks and mypy (**88 source files**) passed.
- Native wheel, application wheel and application sdist all passed `twine check`.

## Independent review rounds

- Round 1: rotated domain ownership. Database subchat freshly reviewed UI,
  lifecycle, export safety, configuration and launcher cleanup: no new findings.
  UI subchat caught the integer-mean threshold gap in BH-003, now corrected and
  independently approved. Measurement's settings/diagnostics/packaging/generation
  review found BH-006; database's generator follow-up found BH-007. Round 1 is
  **not clean**.
- Round 2: rotated measurement review identified BH-008. Further review/repairs
  underway; this round is **not clean**. Clean-round count remains **0**.
- Round 3: UI subchat's extraction/cache/operations/rendering/performance sweep
  found no new issue. Database subchat found BH-009 in diagnostics; this round is
  **not clean**. Further complete clean rounds are still required.

## Additional confirmed findings under repair

### BH-006 — Synthetic sweeps persist nonfinite coordinates from finite bounds

- Public generator accepts `[-1e308, 1e308]` with three points but NumPy linspace
  overflows the endpoint difference, persisting NaN/infinity and reporting success.
- Narrow repair halves overflowing opposite-sign bounds before interpolation,
  restores scale and exact endpoints, and applies to V_SD and V_G.
- Twelve new public QCoDeS generator cases cover both directions, both axes and
  maximum finite float bounds. Combined generator/packaging/diagnostics/release
  batch: **205 passed, 2 Windows skips, 3 subtests passed**. Database subchat
  independently approved and reran all twelve cases.

### BH-007 — CLI CSV overwrite can truncate an existing measurement database

- Database subchat reproduced both public `write_example_csv(overwrite=True)`
  and `copy_instruction_collection(overwrite=True)` following a destination link
  to a freshly generated QCoDeS database and replacing its header with CSV bytes.
  Direct/hardlinked database destinations also need protection.
- UI exports have destination protection, but CLI CSV helpers bypassed it.
  Fixed with guarded atomic CSV publication, full-collection preflight, final
  entry-state recheck and no-clobber publication for initially absent targets.
- Twenty-six regressions cover actual QCoDeS main/WAL/journal/SHM aliases,
  SQLite content with CSV filenames, destination changes during staging and
  ordinary CSV overwrite. Safety plus finite-sweep/existing generation tests:
  **126 passed**. Independent review: UI subchat approved.
- File ownership split: database agent owns CSV helpers; measurement owns sweep
  construction in `testdata.py`. No native VFS/source-reading change needed.

### BH-008 — Full heatmap display collapses distinct large integer signals

- UI subchat's real QCoDeS/Qt reproduction retains eight distinct uint64 signal
  values near `2**63` in dataGrid, but float conversion makes every image cell
  identical. Exact cached values/exports alone do not prevent misleading display.
- Shared display-precision guard now hides misleading images/lines and explains
  the unsupported precision; representable data and exact exports stay usable.
- Equivalent line-plot painter conversion confirmed; shared guard plus explicit
  display error/recovery is being applied to line and heatmap renderer paths.
- Heatmap validation: **14 new regressions passed**, plus 42 existing cases.
  Uniform/nonuniform grids retain exact samples/CSV/cuts; subtract-row-mean
  restores rendering. Overlay data is retained while its renderers are hidden.

### BH-009 — Logging can append through a hardlink to headerless SHM

- Private reproduction: create SQLite main plus 32,768-byte SHM, hardlink SHM as
  `qplot.log`, emit a diagnostic record. SHM grows to 32,783 bytes. Filename and
  magic-byte checks cannot identify a headerless SHM alias.
- Shared auxiliary guard will reject multiply-linked existing files before
  diagnostic opens/appends/rotation or configuration writes/recovery. Fixed in
  `6364ad1`; five regressions cover initial/late aliases, rotation and settings.
  Selected suite: **114 passed, 28 subtests**. UI independently approved and reran
  all five cases. No native changes.

### BH-010 — Native averaging rounds individually valid integer traces wrongly

- Fresh reviewer reproduced three constant int64 traces with values
  `[2**52, 2**52 + 1, 2**52 + 1]`: average is displayed as `2**52`, but the correctly
  rounded mean is `2**52 + 1`. Each individual trace is safely representable.
- Existing exact-mean replay used a `2**53` threshold and missed cumulative
  rounding errors, including mixed fractional inputs below that threshold.
- Fixed with bounded int64 totals or Python integer totals and final division;
  mixed integer/fractional cells use exact replay. Float-only behavior unchanged.
  **67 focused tests passed, 2 domain skips**; 1,000 randomized exact-mean
  comparisons passed. Independent fresh UI review approved.
- Three million-point integer traces average in approximately 36 ms; mixed
  fractional replay costs approximately 1 s per 100,000 samples. Round 6 will
  assess its effect on actual display/downsampling workflows.

## Fresh review and validation checkpoint

- Integrated display guard committed/pushed as `fe1d82f`; nine line regressions
  independently passed, 63 native/numeric cases passed. Heatmap implementation
  independently approved with 14 passing regressions.
- Actual final-guard `python -m qplot` smoke: visible window/event loop observed,
  normal shutdown, exit 0. Ruff and mypy (88 source files) passed.
- Full suite at `fe1d82f` running in `/private/tmp/qplot-bug-hunt-final-tests.log`.
  BH-010 will require focused revalidation after this frozen-suite start.
- Fresh subchats replaced original authors for round 4. Safety review found no
  issue; measurement review found BH-010, so round 4 is **not clean**. Rotated
  round 5 begins after/alongside BH-010 repair. Clean-round count: **0**.

### BH-011 — Reduced heatmaps put tiny coordinates into the final bin

- Fresh safety reviewer reproduced actual SQLite and real QCoDeS scalar loading:
  x = `[0, .5, 1] * 1e-309`, y = `[0, 1]`, z rows `[1, 3, 5]` / `[101, 103, 105]`,
  four-cell limit. Result is `[[NaN, 3], [NaN, 103]]`; scale-equivalent unit
  coordinates correctly produce `[[1, 4], [101, 104]]`.
- Shared `spatial_axis_bins` computes an infinite reciprocal width, and SQLite
  clamps the bin expression to the last bin. Both trusted and snapshot SQL
  consumers are affected. Real QCoDeS reproduction verifies DB hash unchanged.
- Fixed with finite power-of-two-normalized coordinates and direct division by
  normalized bin width in both SQL paths. **187 focused tests passed**;
  independent fresh UI review approved with **61 passed, 2 domain skips** across
  both repairs and existing coordinate tests. Extreme finite/subnormal probes
  also passed. No native changes needed.
- Round 5 is **not clean**; its rotated safety audit found no additional issue.
  Round 6 rotates all three reviewers across the frozen current tree.
  Clean-round count remains **0**.

## Git and publication checkpoint

### Round 6 findings and integration

- BH-012: real streamed QCoDeS array heatmap with repeated cell values
  `int64(2**51), float64(2**51 + .5), float64(2**51 + .5)` returns `2**51`;
  correctly rounded mean is `2**51 + .5`. `FiniteBinMeans` promotes exact
  arithmetic only above an absolute-sum threshold and misses this mixed
  fractional case. Fresh UI reviewer owns consolidated repair and regressions;
  related trusted SQL aggregation also lost the fractional result across page
  boundaries. Per-bin fractional provenance now triggers exact replay alongside
  integer provenance. **47 new regressions passed**, including four actual
  trusted-service/native-reader cases with unchanged protected source files.
  Related suites: **193** and **74** passed. Fresh measurement independently
  approved; committed as `a5c3360`. Ordinary 200,000-bin mixed integral benchmark
  changed from 17.3 to 19.6 ms (local medians).
- BH-013: BH-010 introduced synchronous exact Fraction replay for ordinary
  mixed int/float traces. Real Qt toggle with three 300,000-point sources
  (integer ones, float twos, float threes) takes 3.17 s and blocks a queued
  timer. Default source data is not automatically downsampled. Fresh safety
  reviewer repaired this with integer numerators in shared binary units; the
  same GUI action takes 16.5 ms, fractional mixed data 139.8 ms. Focused cases
  and 4,750 randomized means match the rational oracle. Independent review and
  zoom synchronization follow-up also approved by fresh measurement reviewer.
- Round 6 safety/native/packaging review found no further actionable bug;
  24 focused checks passed. Round 6 is **not clean**, clean-round count **0**.
- BH-010 committed as `1edba35`, BH-011 as `7c5958e`, both pushed to draft PR.
- After BH-011, repeated 2,000,000-row benchmark retained all finite cells:
  500 x 500 grid; aggregation 2.506 s, preview 1.739 s. Ruff and mypy passed.
- Full suite started at `fe1d82f`: **26 failed, 4,648 passed, 41 skipped**,
  767.30 s. Nine sparse axis/style test hosts lack the new precision-overlay
  callback; their harnesses now stub this separately tested integration concern.
  Remaining numeric failures are being individually audited for intentional
  display rejection versus over-rejection. Two GUI CSV failures confirmed BH-014.

### BH-014 — Guarded example CSV creation rejects its own replaced stage

- BH-007's CLI atomic writer replaces its target inode. GUI example CSV creation
  already owns an atomic export transaction, whose staging identity check
  correctly rejects that replacement. Ordinary creation therefore fails before
  publication with "The private export staging file changed unexpectedly."
- Split byte generation into `example_csv_contents`; CLI keeps its guarded
  publication, GUI writes these bytes to its existing owned stage using the
  collection-export writer. No weakening of destination/stage checks.
- Existing GUI creation/replacement and protected-destination regressions caught
  this. Rerun with generator safety and sparse-host fixes: **158 passed,
  33 Windows-only skips, 29 subtests passed**. Independent fresh safety reviewer
  approved; committed as `90cd101`.

### BH-015 — Native averages retain stale downsampled data after zoom

- Round 6 actual Qt reproduction: a 100,000-point sine trace with automatic
  mean downsampling and clipping yields 2,857 initial source/average points.
  Zoom to X=1000..1050 updates the source to 52 points, but its single-source
  average remains at the old 2,857 coarse points. Generated averages disable
  their own processing and miss display-data invalidation on range changes.
- Fresh safety reviewer owns a focused view-change synchronization repair and
  real native regression. Source display-change signals now queue one owned
  timer; source-array identity comparisons prevent feedback. Primary/secondary
  viewboxes, coalescing, removal and deletion covered. Combined native suites:
  **71 passed, 2 domain skips**; fresh measurement independently approved.

### Precision-guard integration follow-up (BH-008)

- Full-suite phase-map failure exposed over-rejection: exact coordinates
  opposite missing derivatives cannot draw a point, yet their conversion
  collisions rejected the remaining valid curve. Log-hidden values had the
  same issue. Check only paired finite, log-visible samples.
- Existing numeric tests that expected drawing/snapping of indistinguishable
  values are updated to verify explicit rejection, retained original samples,
  safe CSV output, and native centering recovery. Real paired/log cases have
  regressions that failed before repair. **108 integration tests** and **10
  focused tests** passed. Fresh UI independently approved and reran **36 tests**.

## Final integration and fresh review checkpoint

- All fixes through BH-015 and BH-008 integration follow-up are frozen and
  independently approved. Ruff, mypy (88 source files), whitespace checks pass.
- Actual `python -m qplot` startup after these changes: visible window, running
  event loop, normal shutdown, exit 0, temporary settings/logs.
- Rebuilt application source distribution and pure-Python wheel successfully
  in `/private/tmp/qplot-bug-hunt-final-dist`.
- Final integrated full suite running with verbose failure reporting in
  `/private/tmp/qplot-bug-hunt-integrated-tests.log`.
- Round 7 starts with three newly created reviewers (`final_safety`,
  `final_measurement`, `final_ui`) who did not author campaign fixes. Coverage
  spans safety/native/packaging, measurements/performance and UI/lifecycle.
  Clean-round count: **0**. Two complete consecutive clean rounds still required.

### BH-016 — Synthetic overwrite and settings startup block on named pipes

- Fresh round 7 safety reviewer reproduced `generate_database(...,
  existing_fifo_named_output_db, overwrite=True)`: generation finishes in
  0.02 s, then the destination probe blocks for more than eight seconds.
  `_require_rollback_journal_database` opens/reads before rejecting non-regular
  file mode. No concurrent namespace substitution is involved.
- Settings startup similarly blocks reading a FIFO `config.json`. Both paths now
  reject a non-regular source before opening; settings keeps its existing
  in-memory-default recovery and ordinary symlink-read policy.
- Four direct/symlink FIFO regressions use subprocess deadlines and verify
  bounded completion, unchanged FIFO and temporary/guard cleanup. Generator:
  **128 passed**; settings/auxiliary: **102 passed, 25 subtests**. Final UI reviewer
  independently approved both repairs and reran **94 tests, 25 subtests**.
- Round 7 is **not clean**, clean-round count **0**. Safety/native/packaging
  audit otherwise completed with **188 passed, 2 platform skips**; UI audit
  completed with **205 distinct tests passed** across appropriate environments.

### BH-017 — Array heatmap reduction overflows finite coordinate spans

- Actual QCoDeS array reproduction: x = `[-1, 0, 1] * 1e308`, y = `[0, 1]`,
  z = `[[1, 3, 5], [101, 103, 105]]`, four-cell reduction. Memory, buffered and
  streamed routes all fail with nonfinite centres/wrong bins; scale-equivalent
  unit data succeeds. Source hashes unchanged. Scalar SQL was fixed in BH-011.
- `_scaled_axis_indices` now normalizes coordinates by a binary scale before
  subtraction/binning and restores original centre units, preserving the
  existing array-edge convention. **210 focused tests passed**, including 24
  real-QCoDeS regressions across three routes, both axes and four scales.
- Final safety reviewer independently approved and reran **40 array/SQL tests**.
  Broader measurement audit: **435 passed**. Two-million-point ordinary binning
  retained identical centres/indices: median 9.32 ms before, 9.65 ms after.

### BH-018 — Exact numeric fallback rejects NumPy longdouble scalars

- Actual native plot reproduction: integer `[1, 1]` plus longdouble
  `[1e308, 1e-308]` forces the finite exact-average fallback and raises
  `TypeError` from `Fraction`. NumPy longdouble `.item()` retains a NumPy scalar
  even on this ARM platform where its mantissa is binary64-sized.
- A shared exact scalar conversion uses NumPy floating `as_integer_ratio`,
  preserving wider mantissas on platforms that provide them. Statistics,
  bounded means and directly related native/operation paths use it. Real
  QCoDeS NPY round-trip regressions cover platform dtype differences. Five new
  tests passed; focused batch **158 passed, 3 skips**. Final UI reviewer
  independently approved and reran all five regressions (plus 81 related tests).
- Native TypeError is reproduced locally on ARM; wider NPY mantissa preservation
  is conditional on platforms that provide it. This does not promise exact
  rounding for ordinary float-only operations.
- BH-016 committed as `8c13368`; BH-017 as `d41c798`. Round 7 remains not clean.
- Round 8 starts with rotated complete coverage: final safety reviews measurement,
  final measurement reviews UI/lifecycle, final UI reviews safety/packaging.
  Current clean-round count **0**.

### BH-019 — Snapshot preflight blocks on a named-pipe database sidecar

- Fresh round 8 safety reviewer used an actual QCoDeS DELETE-mode database and
  exact colocated `source.db-journal` FIFO. `sqlite_read_only_connection` with
  a 0.2 s deadline blocks more than five seconds in the sidecar prefix read.
  Main database hash and FIFO are unchanged. Trusted native preflight already
  rejects nonregular artifacts; the snapshot observation path did not.
- Repair centralizes regular-file rejection in snapshot file signatures before
  prefix reads/copies, covering sidecars consistently without changing allowed
  source writes or native VFS behavior. Final safety independently approved and
  reran 16 regressions. Readonly batch: **93 passed**; packaging/IPC/auxiliary:
  **114 passed, 2 skips**; native checks: **12 passed**.
- Consolidated input audit found a further BH-016 case: generator CSV input
  FIFOs hang the synchronous GUI validation path. CSV now checks regular-file
  mode before opening, preserving regular symlink reads. **112 generator/UI
  tests passed**; independent final safety reviewer approved and reran all five
  generator FIFO/symlink cases. DB/config/CSV public input entrypoints audited;
  destination/diagnostic/cache guards already reject special files appropriately.
- Actual qPlot launcher/UI smoke after these repairs passed (visible window,
  active event loop, normal exit 0). Round 8 is **not clean**, count remains **0**.

### BH-020 — Rectilinearity detection accepts an overflowing coordinate mismatch

- Real shaped QCoDeS arrays: X rows `[-1, 0, 1]` and `[-1, 0, .9]`, Y=0/1,
  signals `[1, 2, 3]` / `[4, 5, 6]`. Unit data correctly yields four X columns
  and missing holes. At X scale `1e308`, raw origin subtraction overflows;
  `isclose(inf, inf)` accepts a false match and moves signal 6 from `9e307`
  to `1e308` in a three-column grid.
- Fixed with binary-normalized comparison, retaining existing tolerance and
  cancellation behavior. **166 focused tests passed**, including 19 new
  scale/axis/direction/tolerance cases. Final measurement independently approved
  with **90 tests passed**. Ordinary two-million-coordinate performance unchanged.
- Round 8 measurement checks otherwise passed **432 tests, 3 skips**;
  UI/lifecycle completed with **409 tests passed**.

### BH-021 — Extreme heatmap display reports success with an infinite transform

- Actual UI follow-up to BH-017: correct reduced 2x2 data and finite centres
  `±5e307` reach Qt, but image scale becomes infinite, view stays at its default
  range, renderers remain marked visible, and "Loaded" replaces any warning.
  There is no crash, but the application implies that an unusable plot succeeded.
- A focused primary/overlay display-range guard is being added by final
  measurement, retaining correct raw grid/axes and CSV behavior. Explicit
  unsupported display status must survive normal completion callbacks. Semantic
  bin geometry should remain usable; no unrelated axis rebasing redesign.
- Author regressions: 21 actual UI cases passed; broader plotting batch:
  196 passed and 10 subtests. Independent review reproduced a follow-up:
  the 450 ms viewport reload timer can start another worker after range
  rejection and hide the explanation. Timer cancellation/scheduling behavior
  now stops the pending timer and skips automatic reload while rejected.
  The regression waits 650 ms and asserts unchanged worker, grid and warning
  before explicit safe recovery. Final UI independently approved the final
  patch and reran all **21 cases** (29.47 s), including line-host overlays,
  secondary axes, resize/zoom, explicit recovery and layer removal.

### Integrated suite result

- Full suite begun at `19acccd`: **4,773 passed, 44 skipped, 119 subtests passed**,
  860.01 s. This resolves all previous 26 integration failures. Subsequent
  BH-016–BH-019 repairs have the focused validation recorded above; BH-020 and
  final review rounds remain pending before the campaign can finish.

### BH-022 — Extreme line coordinates silently produce an invalid drawing

- Real QCoDeS UI probe with X or Y `[-1, 0, 1] * 1e-309` / `1e-320`
  paints and resizes a 500 px window to 1,500 px. The curve scene transform
  contains infinite scale and NaN translation while retaining three displayed
  samples, no unsupported-range explanation and no callback exception.
- With scale `1e308`, autorange retains the default `[0, 1]` on the affected
  axis, again without explanation. Probe: `/private/tmp/qplot_probe_line_ranges.log`.
- Final safety owns the focused line guard and regressions; final UI will
  independently review. Shared precision-helper changes are coordinated with
  BH-021's author. Raw samples/export and recovery through processing must remain
  available. The focused guard now preserves original/mapped data, checks settled
  drawing transforms, disconnects on removal/reparenting and releases obsolete
  bounds caches. Final UI independently approved **18 new regression cases**;
  an earlier new-tests/averaging batch passed **55 tests** and the author's
  broader native/precision/merge batch passed **197 tests**. Actual module
  startup passed (visible window, active event loop, clean exit 0).
- Broad measurement integration found eight existing FFT fixture assumptions
  expecting extreme raw data to be drawable before enabling FFT. Those fixtures
  are being updated to assert original-data preservation and explicit initial
  rejection, retaining the actual transformed-results assertions. Three also
  exposed status precedence: the range explanation masked the active FFT error.
  The follow-up retains the range overlay and specific FFT status explanation.
  Source and fixture adaptations are frozen for full integration. Final UI
  independently approved the follow-up and **47 FFT cases**; spectrum assertions
  remain intact. Ruff and configured mypy (88 files) pass.

### Fresh round 9

- Measurement/data interpretation/performance: final measurement; UI/lifecycle:
  final UI; safety/native/packaging: final safety after FFT fixture integration.
  Coordinator owns full integration, startup, distributions and CI. All areas
  must finish against the combined fixes before the round can count as clean.
- Independent measurement probes: 12 randomized real-QCoDeS ragged mixed-integer
  heatmaps across initial/cache/buffered/streamed routes and 500
  clipping/centering/nonuniform-derivative oracle pipelines passed. No new
  actionable finding yet. Current consecutive clean-round count remains **0**.
- UI/lifecycle portion complete with no new actionable bug: **193 tests passed**
  covering mixed/source promotion, cuts, trusted-live interactions, delayed
  refresh/cancellation, bounded shutdown, dialog ownership and free-cursor axes.

### BH-023 — Ordinary mixed-sign float averaging blocks the GUI

- Fresh round 9 actual `NativePlotItem` probe: enabling averaging for 12
  ordinary float traces with 300,000 points each (`1., -1.1, 1.2, -1.3, ...,
  -2.1`) takes **14.605 s synchronously**, preventing Qt event processing.
  Three all-positive curves take 5 ms versus 1.271 s for mixed signs. Clearing
  the plot also repeatedly recomputes shrinking averages.
- The mixed-sign pure-float path replays every source separately for every
  displayed cell on each update. BH-013's existing binary-integer total path
  currently applies only to integer/mixed provenance. Final measurement owns a
  narrow reuse of that path for mixed signs; final UI independently reviews.
  Correctness/fallback/weighted replacement and before/after latency probes are
  required. Round 9 is **not clean**; consecutive clean-round count remains **0**.
- Initial candidate reduces the 12-trace probe to 0.826 s (about 18x faster).
  Review then identified missing-data boundaries: a later NaN must invalidate
  promoted totals correctly, and one missing cell must not force per-cell
  replay for all good cells. Both are repaired. Independent missing-data timing:
  **14.248 s → 1.188 s**; **49 tests passed, 3 expected skips**, plus 40
  exact-result/nonfinite oracle probes. Raw samples and fallback behavior remain.
- Explicit `NativePlotItem.clear()` separately took 3.061 s recomputing every
  shrinking group. Bulk removal now suppresses those intermediate averages,
  cancels queued work and retains the averaging preference/single-removal
  behavior. Measured clear time: **0.006 s**. This is not a measured window-close
  improvement. Final UI independently approved the follow-up with **57 tests
  passed**, including 14 new performance/lifecycle cases.
- Round 9 safety/native/packaging completed without another finding:
  **435 passed, 28 subtests, one Windows-only skip**, against `e05e43b`.

### Post-display integration follow-up

- Full suite begun with the display fixes at `e05e43b` exposed axis auto-scale,
  derivative and additional extreme FFT failures. Final safety owns diagnosis;
  final UI independently reviews. A confirmed runtime interaction is that
  hiding an unrenderable curve removed its bounds from existing axis validation.
  The repair retains processed bounds independently of drawing visibility.
  All failures must be resolved before fresh round 10 and final validation.
- Run result: **22 failed, 4,861 passed, 44 skipped, 119 subtests**, 821.50 s.
  Log: `/private/tmp/qplot-bug-hunt-post-display-full.log`. Additional failures
  exercise centering/subnormal residuals; original-data and processed numerical
  assertions must remain meaningful when a Qt display is explicitly rejected.
  This run began at the frozen display-fix tree committed as `e05e43b` and
  predates BH-023; the averaging fix has **137 passed, one expected skip** in its
  author batch plus independent validation above. Final full rerun is required.
- All 22 failures resolved and independently approved. Mapped bounds now support
  rejection and ordinary Reset/Auto recovery after unsafe zoom, independently of
  drawing visibility. Original 32 global-auto cases pass unchanged; combined
  final line/global/filtered-auto coverage: **97 distinct cases passed**.
  Broader author integration: **119 passed**; independent existing numeric/axis
  integration: **72 passed**. Exact numerical/source assertions are retained.
- Linux CI found one additional test-only issue: symlink resolution advances
  `st_atime` while all identity/content/modification metadata stay unchanged.
  Settings FIFO regression now ignores access time but checks nanosecond
  modification/change times and the exact link target.

### Fresh round 10

- Rotated review started on combined fixes: final UI covers measurements and
  performance; final safety covers UI/lifecycle; final measurement covers
  database safety/native packaging/settings/generation/diagnostics/export.
  Coordinator runs final full integration, actual startup and package checks.
  All three domains completed without new actionable bugs on `1798979`:
  **first complete clean round**.
- UI/lifecycle: **360 passed, 35 expected platform/type skips**, plus fresh
  line/heatmap mixed-workflow probes (resize, axes, Auto, refresh, CSV, close),
  no callback exceptions or protected database changes.
- Safety/native/packaging/auxiliary publication: **1,020 passed, 34 platform
  skips, 28 subtests**; no source or native changes. The existing settings helper
  named `_migrate_config` is unchanged from baseline; this campaign adds no
  settings migrations or compatibility formats.
- Measurement/performance: **547 passed**, 24 independent real-QCoDeS grid
  comparisons across three loading routes. A 300k-sample FFT took 0.112 s,
  derivative 0.010 s, and an extreme mixed-mean probe cancelled at its requested
  fifth callback. No new actionable finding.

### Fresh round 11 and final validation

- Rotated review started on the same `1798979` source: final safety covers
  measurements/performance; final measurement covers UI/lifecycle; final UI
  covers safety/native/packaging/auxiliary publication. Consecutive clean count:
  **1**, pending completion of every round-11 area.
- Full suite running against the source tree committed as `1798979`:
  `/private/tmp/qplot-bug-hunt-final-integrated.log`. Ruff and mypy (88 files)
  pass; actual `python -m qplot` startup reports visible window, live event loop
  and exit 0. Application sdist/wheel rebuilt: 363 source archive files,
  all 201 test modules included; 129 wheel files / 123 runtime files. Inventory
  and `twine check` pass. Native code remains unchanged; earlier rebuilt
  universal artifact passed architecture and pinned ABI checks.
- UI/lifecycle completed clean: **486 passed, 2 expected skips, 2 subtests**,
  including fresh active-refresh drop/remove/close probes for both host types.
  Safety/native/packaging completed clean: **217 passed, 3 subtests**, including
  actual native SHM/read-only/cancellation and synthetic publication checks.
  Measurement review found BH-024 below, so round 11 is **not clean** and the
  consecutive clean-round count resets to **0**.

### BH-024 — Long integer row centering delays cancellation

- Real current-QCoDeS array dataset at the default 2,000,000-point full-resolution
  limit enters registered Subtract Row Mean with shape `(1, 2_000_000)` and int64
  samples. `worker.cancel()` requested at 37.8 ms is not observed until 3.727 s,
  after the entire exact calculation. No partial result publishes and source
  hashes remain unchanged; this is operation cancellation latency, not an
  unbounded database transaction or incorrect result.
- Exact conversion, summation and residual loops only check cancellation at row
  boundaries. Final safety owns periodic checks within those phases and bounded
  consumption/real-worker regressions. Final measurement independently approved:
  **55 tests passed**, including 17 new regressions and 11 additional exact-result,
  NaN/Inf/source-immutability probes. Real default-limit cancellation improved
  **3.727 s → 0.0381 s** (request at 0.0378 s), without partial publication.
  Probe: `/private/tmp/qplot_round11_cancel_probe.py` and corresponding `.log`.

- Full `1798979` integration result: **1 failed, 4,898 passed, 44 skipped,
  119 subtests**, 830.59 s. The sole failure is subnormal line-mean recovery:
  processed values are representable signed zeros but the range guard can leave
  drawing rejected after processing/refresh. Isolated diagnosis: constant-data
  Auto reuses the previous subnormal-width range, rejects that range and disables
  Auto. Final UI owns a narrow representable constant-data recovery fix;
  final measurement independently reviews. Final full validation and
  two subsequent complete clean rounds remain required.

### BH-025 — Constant-data Auto retains an unusable previous span

- The final integration failure exposed an Auto-range defect: centering
  `[tiny, tiny, 2*tiny, 2*tiny]` correctly produces signed zeros, but Auto
  inherits the prior subnormal span, rejects it and leaves supported data hidden.
  The original numerical test is unchanged; this is an axis-recovery repair.
- A seven-line guard chooses a representable default span only for constant-data
  Auto when the inherited viewport scale overflows. Manual ranges and AutoPan
  remain unchanged. Five new real-QCoDeS regressions cover X/Y, tiny/min-subnormal
  ranges, explicit Auto, mean processing and settled refresh callbacks.
- Final measurement independently approved **126 passed** checks, including
  two extra secondary-axis/AutoPan probes. Author focused validation:
  **123 passed**. Ruff, mypy, whitespace checks and actual qPlot startup pass.

### Fresh round 12

- Rotated safety review completed clean on `ecd2a9b`: **110 focused tests** and
  six fresh live-QCoDeS cycles verified protected artifacts, cancellation,
  checkpoint recovery and continued reading. Measurement review passed **314
  focused tests**; UI review passed **199**. Their confirmed findings below make
  round 12 **not clean**. Consecutive clean-round count remains **0**.

### BH-026 — Eager marquee statistics block the GUI on integer traces

- Actual current-QCoDeS 1,000,000-point int64 line, select all and call the normal
  marquee context-menu path: **4.705 s before the menu exists**, compared with
  1.068 s for the float control. Integer mean and standard deviation convert each
  sample through Fraction; direct measurements attribute 1.298 s and 1.899 s.
- Exact bounded integer sums and common binary denominators remove per-cell
  Fraction allocation while preserving integer endpoints and mixed residuals.
  Same-process million-point menu timing improved from **3.330 to 0.319 s**
  for int64, **3.031 to 0.790 s** for mixed integer/float, and **2.377 to
  0.596 s** for mixed signed/unsigned records. Source samples remain unchanged.
- Ten new real-QCoDeS, exactness and bounded-cancellation regressions pass.
  Author validation: **89 passed**; independent final-safety review: **64
  distinct cases passed**, including fresh exact rational oracles. Ruff, mypy,
  whitespace checks and actual qPlot startup pass. No native source changed.
  Probe: `/private/tmp/qplot_round12_stats_probe.py`.

### BH-027 — Withdrawn: mocked print-dialog lifetime false positive

- The initial reproduction replaced `QPrintDialog.exec` with a result stub.
  That left parent-owned dialogs alive, but bypassed real PyQt ownership behavior.
- Fresh round 13 independently challenged this evidence. The installed PyQt
  `QtPrintSupport/qprintdialog.sip` calls `sipTransferBack(sipSelf)` before the
  native modal loop, returning ownership to Python so the dialog is destroyed
  when its wrapper is released. Actual generic modal-dialog cycles confirmed
  automatic destruction; native print execution is unsupported offscreen.
- The author and independent reviewer withdrew the finding and approval. The
  speculative cleanup and its mocked-only regressions are reverted. There are
  **26 confirmed repaired defects**, not 27; this identifier is retained solely
  to document the evidence correction. No manual printer test is required to
  justify an unrelated change.

### Final integration checkpoint at `28ae787`

- All 26 confirmed fixes are committed and independently approved; BH-027 is
  withdrawn as recorded above. Dedicated
  branch pushed; draft PR #84 remains open and unmerged.
- Full run started at `ecd2a9b`: **4,920 passed, 44 skipped, 119 subtests passed**;
  one partial-frame helper-finalizer timing failure (6.77 s against 5 s bound)
  is under investigation. This run does not claim full coverage of the subsequent
  print/statistics commits. Log: `/private/tmp/qplot-bug-hunt-ecd2a9b-full.log`.
- Final-code Ruff and mypy (**88 files**) pass. Actual `python -m qplot`
  starts, shows its window, runs the Qt event loop and exits normally (**0**).
- Rebuilt application sdist and wheel pass source/runtime inventory validation
  and `twine check`: **367 sdist files, 205 test modules plus conftest; 129 wheel
  files, 123 runtime files**. Artifacts: `/private/tmp/qplot-bug-hunt-28ae787-dist`.
- Fresh independent round 13 is underway with rotated ownership: measurement
  subchat covers safety/native/packaging, safety subchat covers UI/lifecycle,
  and UI subchat covers measurement/results/performance.

### Fresh round 13 — complete and clean

- Safety/native/packaging: **307 passed, 33 platform skips, 28 subtests**;
  fresh review and real-WAL, snapshot, publication, configuration, diagnostics,
  generator and export checks found no new actionable defect.
- Measurement: **274 passed, one platform skip**, plus 22 fresh exact-rational
  summary oracles and 15 real-QCoDeS duplicate/missing-cell grid comparisons
  across memory, buffered SQL and streamed SQL. A 300,000-sample integer summary
  took 74 ms; derivative took 56 ms; requested cancellation was observed.
- UI/lifecycle: **40 passed and 10 subtests**, including actual modal execution,
  retained-source cleanup, pending previews/refresh, mixed promotion/cuts,
  preferences, exports and shutdown. This review withdrew BH-027 as above.
- Coordinator reviewed launcher/configuration/diagnostic integration and rebuilt
  distribution evidence. No new actionable application bug. Consecutive clean
  count: **1**. Fresh rotated round 14 is underway.

### Integration-test timing correction

- The one full-suite failure charged unconditional whole-process garbage
  collection to an already-finished supervisor finalizer. Instrumentation found
  the real finalizer took **1.17 ms** and all four terminal conditions were true
  before unrelated GC. Delaying that unrelated collection reproduced the failure.
- Check completion before requesting GC. The **five-second limit and every
  weakref, job/error, receiver, thread, connection, child-process, protected
  artifact and writer-recovery assertion remain unchanged**. No application fix.
- Independent UI reviewer approved; all **eight partial-frame cases pass**.
  The delayed-GC probe now passes with finalization **1.07 ms** and no unrelated
  GC inside the measured interval. Full integration follows on this correction.

### Numerical scope note

- Float-only summation can differ from an exact rational oracle by one floating
  point unit. Without an accuracy contract or material workflow consequence,
  ordinary rounding alone is not classified as a bug. This campaign preserves
  integer/mixed provenance and repairs confirmed cancellation, overflow and
  visible representation errors; it does not promise universally correctly
  rounded floating-point operations.

- Focused commits: `af1d031` (settings), `3264ebc` (live retry), `98677c2` (UI).
- `d0062d3` preserves trusted preview numeric meaning; `e57126b` protects generator
  CSV destinations and finite synthetic coordinates. All pushed.
- Dedicated branch pushed successfully through configured Git credentials.
- [Draft PR #84](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/84)
  opened for cross-platform CI at `1534de7`; further confirmed repairs pending.
- `git fetch origin` failed on an existing unrelated local ref
  `refs/heads/fix/bug7-source-mtime` pointing at missing object
  `ad9bcd91383451b350d33dfd6a1df1fc62936a48`. The ref was left untouched;
  normal branch push succeeds. No merge or release performed.

## Next tasks and continuation checkpoint

1. All confirmed findings BH-001 through BH-026 are fixed and independently
   reviewed; BH-027 was withdrawn after fresh evidence review.
2. Run final full integration with the approved finalizer test correction.
3. Finish round 14. Round 13 is complete and clean; clean-round count: **1**.
   Any new actionable application defect resets that count.
4. Finish final startup/distribution/static validation and draft PR/CI status.
   No native source changed; the rebuilt native artifact already passed
   architecture/ABI/package checks earlier in this campaign.

Subchat usage limits interrupted BH-021 review. The user reported usage reset;
all three final reviewers have been restarted with their existing file ownership.
No unfinished fix or fresh review round was marked approved during interruption.

CI follow-up: cancellation instrumentation now wraps the real `sample_fraction`
helper and still requires interruption at exactly 1,024 converted samples.
All six cancellation/residual regressions pass; final UI independently approved
and reran both cancellation cases. No application cancellation change was needed.
