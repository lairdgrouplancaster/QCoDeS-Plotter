# qPlot bug-hunt campaign

## Resumed 2026-10-25 — up to five rounds, 34–38

The user authorized up to five further rounds. Start at round 34 and stop after
two complete fresh rounds find no new actionable bugs, or after round 38.
Round 33 found two bugs, so the consecutive clean-round count starts at zero.
The campaign currently contains **48 application/diagnostic repairs** and
**seven test-only CI corrections**. Continue preparing bugfix 1.6.3 on
`codex/qplot-bug-hunt-2026-10-23` and update
[draft PR #91](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/91).
Do not merge, tag or release. Preserve the database-access invariant, support
current QCoDeS and do not introduce legacy settings migrations.

### Verified resumption baseline

- Clean local revision and freshly fetched upstream:
  `f3f8dea197ead97eb77c65c970b2fcf3c0141ecd`. The PR is still open, draft and
  unmerged with that exact head. Main remains
  `4a0baf68be2b005e25ba802d2fc166a4da38d418`.
- Fresh GitHub API verification finds **47/47 completed successful checks**,
  including `Required checks`. The response contains the full API total,
  47 unique IDs and matching head SHAs throughout. Evidence:
  `/private/tmp/qplot-r34-baseline-check-runs.json`;
  [baseline CI](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/40043107821).
- Three fresh subchats investigate database safety/live reading/native access,
  measurement correctness, and UI/lifecycle. The coordinator covers generation,
  settings, entry points, diagnostics, packaging and performance. Rotate coverage
  in later rounds, use real current-QCoDeS acquisitions and independent oracles,
  and require concrete evidence before counting a finding. Coordinate exact
  file ownership before edits; another subchat reviews every substantive fix.

### Round 34 — in progress

- All three lanes begin with read-only investigation and owned scratch fixtures.
  Local process-supervision tests need approved IPC permissions; sandbox-denied
  setup errors are not application findings. Use the project venv and fixed
  Matplotlib cache throughout. Do not reuse the known broken old Intel scratch
  runtime without validating or replacing it.
- Next: complete fresh coverage, reproduce and repair confirmed bugs, obtain
  independent reviews and targeted regression results, then record this round
  before advancing. Reserve final integration for the full required checks,
  qPlot startup, applicable native/artifact validation and exact-head hosted CI.

#### C19-050 — Screenshot output overwrites existing measurement files

- The documentation screenshot helper passes a fixed PNG pathname directly to
  `QPixmap.save`. A real Qt capture to a pre-existing hard link changed an owned
  current-QCoDeS database from **77,824 SQLite bytes to 3,324 PNG bytes**. No
  concurrent namespace substitution is involved. Direct SQLite content under
  the PNG name and a pre-existing symlink reproduce the same problem.
  Evidence: `/private/tmp/qplot-r34-screenshot-probe-receipt.json`.
- Repair: reuse `ensure_safe_auxiliary_path` immediately before the Qt save.
  Existing database/journal signatures, hard links, symlinks and special files
  are refused; ordinary PNG creation and replacement remain supported. The
  coordinator owns `scripts/capture_demo_screenshots.py` and
  `tests/test_demo_database_safety.py`. Three real-QCoDeS/Qt regressions fail
  before the fix while the normal PNG control passes.
- Different safety subchat independently **approved** the frozen code/tests:
  **28 focused tests pass**, plus nine distinct actual-Qt refusals involving a
  separate real-QCoDeS writer's main, WAL, SHM and genuinely held rollback journal.
  **50 protected-file comparisons** and two ordinary PNG saves pass. Writer,
  Qt objects and all owned fixtures retire. Receipt:
  `/private/tmp/qplot-r34-c050-safety-review-receipt.json`.
- Coordinator related settings/generation/diagnostics/packaging validation:
  **227 passed, two Windows-specific skips and 25 subtests passed** in 9.31 s.
  Changed-file Ruff and whitespace checks pass. The real screenshot script's
  complete entry point also passes: all four expected screenshots are valid
  PNGs, its reader cleanup audit passes, and all disposable settings, databases
  and outputs are removed. Receipts/logs:
  `/private/tmp/qplot-r34-root-focused.log` and
  `/private/tmp/qplot-r34-screenshot-smoke-receipt.json`.

#### Round-34 coverage recorded so far

- **Safety/live/native:** no additional finding. **43 tests pass**. Fresh
  13-run acquisition verifies **3,151,936 exact values**, eight-entry cache
  eviction with retained consumers, coalesced cancellation, queued deadline,
  partial BLOB capture, schema addition, checkpoints and service reopen.
  **27 protected-file comparisons, 16 physically read-only descriptors and
  three writer checkpoints** pass. Cancellation takes 0.134 ms locally; maximum
  checkpoint duration is 24.98 ms. Both services, 16 spools, three native proof
  handles and owned fixtures retire; prohibited native counters remain zero.
  ARM macOS execution does not exercise the 512 MiB cache threshold or replace
  other-platform CI. Receipt: `/private/tmp/qplot-r34-safety-receipt.json`.
- **Measurement:** no additional finding. **183 tests pass**, plus a fresh
  live three-output acquisition through four unequal stages, using opposing
  heatmap dependency orders and an independent line. Independent Fraction,
  interpolation and direct DFT oracles verify **84 raw/reset grid cells, 350
  derivative values, 114 cut values, 50 FFT oracles, 76 CSV cells and 144
  coordinates**. **39 protected-file comparisons**, five retired plot objects,
  one retired service and complete fixture cleanup pass without callback errors.
  Receipt: `/private/tmp/qplot-r34-measurement-receipt.json`.
- **UI/lifecycle:** **159 focused tests pass**. A fresh shared-source live
  composition passes both host types: **204 exact values, 18 protected-file
  comparisons, four real Qt drops, eight Refresh clicks and two promotions**.
  All 20 QObjects and both services retire. Receipt:
  `/private/tmp/qplot-r34-ui-live-receipt.json`.
- **C19-051 confirmed; repair in progress:** opening a cut's Custom Color
  picker, then replacing an owned source through an external test writer while
  the real 100 ms refresh timer runs, automatically closes the plots but leaves
  the unparented modal picker visible. Accepting it changes the already-closed
  cut's color. There is no programmatic Close All call in this reproduction.
  Evidence: `/private/tmp/qplot-r34-ui-color-replacement-receipt.json`. UI author
  owns the color widget and its regressions; measurement subchat independently
  reviews the repair. Round 34 is not yet complete. C19-050 brings confirmed
  repairs to **49**, with C19-051 still pending; test-only CI corrections remain
  seven and the clean-round count remains zero.

## Resumed — one additional round, 33

The user authorized one additional round after rounds 28–32. Stop after round
33 and its repairs, independent review and integration; do not begin round 34.
Continue on `codex/qplot-bug-hunt-2026-10-23`, preparing bugfix 1.6.3 in
[draft PR #91](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/91).
Do not merge, tag or release. Preserve the database-access invariant and
current QCoDeS support; do not add legacy settings migrations.

### Round-33 verified baseline and scope

- Clean starting revision: `29e96a1576a4a97621934bd5aadf4e46d4f1566c`, matching
  the branch upstream and draft PR head. Fresh GitHub API verification finds
  **47/47 completed successful checks**, including `Required checks`. Every
  returned SHA matches, all check IDs are unique, and the complete response
  count equals the API total. The PR remains open, draft and unmerged.
  [Successful baseline CI](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/40011099582).
  Evidence: `/private/tmp/qplot-r33-baseline-check-runs.json`.
- Three fresh subchats own initial read-only investigation of database
  safety/live reading, measurement correctness, and UI/lifecycle. The
  coordinator covers packaging, entry points, generation, diagnostics,
  performance interactions and integration. Assign exact file ownership before
  repairs and require a different subchat to independently review each
  substantive fix. Use current-QCoDeS fixtures, concrete reproductions and
  meaningful regression tests; do not count unsupported or previously repaired
  behavior as a new finding.
- The starting campaign total was 46 confirmed application/diagnostic
  repairs plus six historical test-only CI corrections. Round 32 found two
  bugs, so the consecutive clean-round count starts at zero.

### Round-33 confirmed fixes

- **C19-048: queued plots survive accepted Close All.** A real current-QCoDeS
  WAL database and real Qt confirmation loop show one visible plot plus an
  earlier pending metadata request before Yes. Close All returns with zero
  windows, then the pending request creates a visible plot. The same sequence
  occurs through the separate Auto Plot queue. No correctly preserves both
  requests. This differs from C19-047: those plots were already published during
  the dialog, whereas these requests remain pending when accepted close returns.
  Evidence: `/private/tmp/qplot-r33-ui-pending-close-receipt.json` and
  `/private/tmp/qplot-r33-ui-auto-pending-close-receipt.json`. UI author owns
  `main.py`, `_plot_actions.py`, `_auto_plot.py` and its new regression module.
  The repair invalidates/cancels manual launches accepted before Close All and
  discards queued Auto Plot work while retaining completed-GUID history and
  future acquisition support. The same cancellation applies when no plot window
  exists yet. The real regression module fails **11 cases before repair**, with
  three passing controls. Author validation: **223 tests plus two subtests**;
  the final guarded regression module passes all **14 cases**. A separate safety
  subchat independently **approved** final source/test hashes: **62 unique
  focused tests pass**, followed by a final 14-case rerun. Four distinct real
  nested-modal compositions exercise eight queued launches, including manual
  and automatic subscribers sharing one broker operation, Yes/No/empty close,
  completion races, same-GUID explicit reopening, new automatic acquisition and
  replay prevention. **78 displayed values, 24 protected-file comparisons and
  three writer checkpoints** pass; four services and 16 QObjects retire without
  callback errors. Receipt:
  `/private/tmp/qplot-r33-safety-c048-review-receipt.json`.
- **C19-049: plot-launch profiling waits for absent capture steps.**
  The real `benchmark_plot_launch.py --preceding-run 2 --repeats 2` workflow
  waited 137.5 seconds before a controlled interrupt ran its normal cleanup.
  A small first capture or a retained completed-run prefix can finish without
  yielding, while the benchmark waits for a capture step. Its default wait is
  3,660 seconds. Explicit aggregate/large-summary stage requests
  similarly wait when a small valid run completes without entering that stage.
  A different measurement subchat independently confirmed the source cause.
  Evidence: `/private/tmp/qplot-r33-root-probe.log`. The coordinator owns
  the benchmark script and `tests/test_campaign_profiling_scripts.py`; the
  measurement subchat independently reviewed the frozen repair. The original
  interrupted performance composition is not counted as a successful run.
  The repair accepts completed captures for the default `any` stage and raises
  an explicit diagnostic when a completed capture never entered a requested
  named stage. Two representative regressions time out before the fix; all
  **15 profiling tests pass** afterward, including three launch actions and both
  unavailable-stage controls. A different measurement subchat independently
  **approved** the frozen code/tests: another **15 tests pass**, and distinct
  two-output/ragged-array acquisitions complete double-click and Run-button
  three-repeat benchmarks plus both named-stage failure cases. All **26 QObjects
  and four services** retire before application deletion, with protected files
  unchanged. Receipt: `/private/tmp/qplot-r33-measurement-c049-review-receipt.json`.

### Round-33 completed investigation coverage

- **Safety/live/native:** no additional finding. **28 focused tests pass**;
  a fresh two-run acquisition verifies **5,206,096 exact values**, independently
  nullable outputs and ragged uint64/int64 BLOBs, completion/metadata changes
  during suspended prefix capture, two helpers and service reopen, retained
  prefixes/cache isolation and coalesced cancellation. **27 protected-file
  comparisons, 16 physically read-only descriptors and two writer checkpoints**
  pass; cancellation takes 0.032 ms locally. Native forbidden counters remain
  zero. All three services, six spools, three native proofs and the owned writer
  and fixture retire. Receipt: `/private/tmp/qplot-r33-safety-receipt.json`.
- **Measurement:** no additional finding. **167 focused tests pass**; fresh
  public-control compositions pass on **ARM and Intel**, each using 12 actual
  current-QCoDeS acquisitions. Clipping, missing-cell fill, row/column centering,
  nonuniform differentiation, CSV, Clear/reset and native cut centering/FFT
  match independent Fraction/interpolation/trigonometric DFT oracles. Each
  architecture checks **168 raw samples, 180 derivative cells, 180 CSV values,
  180 reset cells, 60 centered values and 72 numeric oracles**, plus **108
  protected-file comparisons**. All 24 QObjects, 12 services and fixtures per
  architecture retire without callback errors. Receipt:
  `/private/tmp/qplot-r33-measurement-receipt.json`.
- **Coordinator/generation/settings/diagnostics/performance/packaging:**
  **96 focused tests pass**. A fresh generated current-QCoDeS database contains
  **131,714 rows** with descending line and map axes crossing the 65,536-row
  page boundary; **197,891 exact coordinates and 32 independent scalar sine
  oracles** match. Current-schema CLI updates persist, invalid updates preserve
  prior bytes, database-as-settings recovery preserves the database, diagnostic
  rotation works, and unselected sibling files remain untouched. With C19-049
  repaired, two-repeat GUID/double-click/Run-button benchmarks with one worker
  and a preceding map complete in 9.8–12.0 seconds locally, including actual Qt
  painting. All seven protected-file comparisons pass and the fixture is
  removed. Receipt: `/private/tmp/qplot-r33-root-receipt.json`. Native sources,
  pinned native/APSW dependencies and build configuration remain unchanged;
  no native rebuild is needed for these Python changes.
- **UI/lifecycle:** the confirmed C19-048 is the only additional finding.
  Broader pending-drop, mixed/source-promotion, owned-dialog, preferences and
  CSV-selection-race coverage passes **145 tests**. Four author real-modal
  manual/automatic Yes/No compositions check **50 exact values and 12 protected
  file states**, retiring 12 plots, four services and four fixture roots with
  no callback errors. Receipt: `/private/tmp/qplot-r33-ui-receipt.json`.
- **Investigation is complete:** round 33 fixes **two confirmed bugs**, bringing
  the campaign to **48 application/diagnostic repairs** plus seven test-only
  CI corrections, including the integration correction below. The consecutive
  clean-round count remains zero.
  Do not begin round 34 without further authorization.
- **Local integration complete:** both substantive fixes have independent approval;
  the complete automated suite passes on frozen application code. An initial
  sandboxed attempt was interrupted after local process-supervision IPC raised
  `PermissionError` before those tests could exercise their intended behavior
  (540 passed, 70 permission-related failures, exit 2). The complete run passed
  with approved local IPC/process permissions; the interrupted attempt is not
  counted as final validation. Its log is
  `/private/tmp/qplot-r33-full-sandbox-interrupted.log`. Actual project-venv
  `python -m qplot` startup already passes with a visible window, real Qt event
  loop, exit 0, isolated settings/logs and complete cleanup; its receipt binds
  all three changed application files:
  `/private/tmp/qplot-r33-ui-startup-receipt.json`.

### Round-33 artifact and installation validation

- Frozen reviewed source revision: `735d7a2263373884a8491eddee1869c2128f7272`.
  Source/test hashes match both independent approvals; receipt:
  `/private/tmp/qplot-r33-integration-source.json`.
- Built the app wheel/sdist in a clean detached worktree, with exact clean,
  unreleased PR #91 provenance. Every tracked archive member matches that
  revision; the sdist includes all **254 test modules plus conftest**. All four
  app/native artifacts pass Twine. Artifacts:
  `/private/tmp/qplot-bh20261023-735d7a2-dist`; manifest:
  `/private/tmp/qplot-bh20261023-735d7a2-artifact-manifest.json`.
- Native sources remain identical to the verified baseline. The native wheel
  and sdist retain their audited hashes; two missing old scratch-cache copies
  were restored from the prior round's surviving artifacts only after exact
  hash verification. No native source or dependency change was needed.
- Fresh **ARM and Intel compiler-free wheel installations both pass**, including
  exact installed-file checks, actual qPlot startup, current QCoDeS reading,
  native protected-file/cleanup/checkpoint behavior and dependency checks.
  Dependencies install entirely from wheels; no compiler build occurs. All
  temporary environments, the Intel host-Git metadata helper and the detached
  build worktree are removed. Receipts:
  `/private/tmp/qplot-r33-wheel-install-arm64-receipt.json` and
  `/private/tmp/qplot-r33-artifact-intel-acceptance-receipt.json`.
- A separate Intel pytest attempt could not collect because the old shared
  scratch runtime lacked Python package files and fell back to an ARM-only
  dependency. It ran no tests and is not counted as a pass. The successful
  fresh Intel wheel installation above does not use that scratch runtime;
  earlier completed ARM/Intel measurement compositions retain their own
  explicit architecture checks and receipts.

### Round-33 final local validation and publication checkpoint

- Complete suite: **5,663 passed, 128 skipped, 61 warnings**, all **5,791 items**
  accounted for, exit 0 in **932.93 seconds**. Command:
  `QT_QPA_PLATFORM=offscreen MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/python -m pytest --no-cov -n 2 --dist=loadfile --no-loadscope-reorder`.
  Log: `/private/tmp/qplot-r33-full.log`. Skips cover platform-specific cases;
  extended-precision public workflows were independently exercised on Intel.
- Whole-tree Ruff, mypy (**88 source files**), dependency and whitespace checks
  pass. Application/script/native trees match frozen reviewed revision
  `735d7a2263373884a8491eddee1869c2128f7272`. The later integration correction
  below changes one test and this record. Startup and fresh ARM/Intel wheel
  acceptance above use identical application source; their sdist contains the
  original test. Final hosted CI validates the corrected test and artifacts.
  No merge, tag or release is authorized.
- Round 33 is complete with **two repairs**. The branch now contains **ten
  confirmed fixes across rounds 28–33** for bugfix 1.6.3; native remains 1.0.1.
  No additional investigation round is authorized.
- Publication continuation: commit/push this validation record, verify all CI
  checks for the resulting **exact final head**, and update draft PR #91. Record
  the final hosted-CI observation in the linked PR, avoiding a further log-only
  commit that would change the revision just validated. The complete API
  receipt uses `/private/tmp/qplot-r33-ci-<shortsha>.json`. If interrupted, resume
  only this integration checkpoint; do not start round 34 or merge/release.

### Round-33 test-only CI integration correction

- Hosted CI for `737d99116a300fceae95782f02a1d29ea6e507a1` exposed one obsolete
  assertion in `test_closed_target_discards_pending_drop`: Windows Python 3.11,
  Windows 3.14 and Linux observed two cancellations of the same request instead
  of exactly one. Close All now cancels immediately; its already-queued stale
  callback can also cancel before retiring. `TrustedLiveReadService.cancel`
  immediately returns false for a completed request, so the second invocation
  cannot repeat the underlying cancellation or affect another subscriber.
  This is one test-only correction, not a third application finding.
  [Affected CI run](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/40039236029).
- The test now waits for the specific deferred timer to stop or be deleted,
  requires cancellation of the intended request and its completion, and retains
  every unchanged no-publication, dataset-ownership and resource-use assertion.
  This removes the scheduling-sensitive exact invocation count while ensuring
  the stale callback actually retires before the final checks. Application
  source is unchanged. All **32 affected lifecycle tests pass** locally in
  **26.96 seconds**; whole-tree Ruff and whitespace checks pass. Log:
  `/private/tmp/qplot-r33-ci-cancel-fix.log`.
- The safety subchat independently **approved** this correction and reran all
  **18 pending-drop cases**, passing in **15.29 seconds** with separate fixtures.
  Its reviewed test hash and cleanup evidence are recorded in
  `/private/tmp/qplot-r33-safety-ci-test-correction-receipt.json`.

## Resumed 2026-10-23 — up to five rounds, 28–32

The user authorized up to five additional rounds with substantial effort.
Start a fresh clean-round count at zero; stop after two complete fresh rounds
find no new actionable bugs, or after round 32. Maintain the database-access
invariant, current QCoDeS support and no legacy settings migrations. Every
substantive fix requires concrete reproduction, regression tests and independent
review by a different subchat. Do not merge or release.

### Verified resumption baseline

- Previous PR #90 was merged externally on 2026-10-22; its remote branch was
  deleted. The former local head `d3af0b2` has **47/47 successful checks**.
- Fetched current `origin/main` / tag `v1.6.2`:
  `4a0baf68be2b005e25ba802d2fc166a4da38d418`. The checkout was clean.
  Verified **111 exact-SHA checks across both API pages: 110 successful and
  one skipped `publish-native` job**. Both `Required checks` and
  `validate / Required checks` succeeded. All check IDs are unique, every
  returned head SHA matches and the combined length matches the API total.
  [Main CI](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/39772723874),
  [release validation](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/39774775931).
  Evidence: `/private/tmp/qplot-r28-main-baseline-all-checks.json`.
- New dedicated branch: `codex/qplot-bug-hunt-2026-10-23`, based on that verified
  main revision. [Draft PR #91](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/91)
  was opened at `9f80242`; the merged PR is history.
- Since round 27, main gained full-dataset statistics from thumbnail menus,
  persistent CSV filename preferences, revised default plot tiling and related
  validation. These changes are part of the baseline, not new campaign fixes.
- Project venv: CPython 3.12.2, QCoDeS 0.61.0, NumPy 2.5.3, PyQt6 6.11.0,
  pyqtgraph 0.14.0, APSW 3.53.4.0 and native 1.0.1. Use the fixed Matplotlib
  cache and offscreen Qt for automated startup checks.

### Round 28 — complete: two confirmed bugs fixed

Three fresh subchats investigate safety/live/native reading, measurement
correctness and UI/lifecycle. The coordinator owns settings, entry points,
generation, diagnostics, packaging, integration and this log. Investigation
begins read-only; exact source/test ownership is assigned before any edit.
Initial emphasis is new statistics acquisition/precision/lifecycle and CSV
filename preference/export-path interactions, then other application seams.

#### C19-040 — Inferred dataset statistics use basis columns as axes

- Confirmed through real current-QCoDeS scalar and ragged-array recordings:
  `converted(x)` also carries an inferred `basis` column. Statistics inferred
  its axes from every non-output column, reporting X=basis and Y=x instead of
  X=x and Y=converted. A text/complex basis made statistics fail. For a two-axis
  measurement, adding a basis column reversed the displayed default axes.
- Fix: take the ordered setpoint names from the requested parameter's actual
  `depends_on_` metadata and pass them explicitly to the statistics summarizer.
  Inference inputs remain available in recorded data but never define axes.
- Twelve real QCoDeS regressions cover line/heatmap, scalar/ragged-array and
  numeric/text/complex bases. All fail on the previous implementation and pass
  after the repair. Author related suite: **91 passed**. Logs:
  `/private/tmp/qplot-r28-measurement-before-final.log` and
  `/private/tmp/qplot-r28-measurement-after.log`.
- Independent UI reviewer **approved** the frozen repair: **16 tests passed**
  plus a distinct two-output acquisition with opposite dependency ordering and
  numeric, text and complex bases together. The original helper reverses both
  outputs' X/Y ranges; corrected real row-menu dialogs match seven-line literal
  oracles and retain copied text after Close Database. Protected files and
  cleanup pass, with no callback errors. Receipt:
  `/private/tmp/qplot-r28-ui-independent-review-receipt.json`.

#### C19-041 — Unsupported statistics values silently become real, infinite or zero

- Reproduced through the actual thumbnail context-menu action: complex measured
  values lose their imaginary parts; pure-imaginary measurements report four
  zero summaries. On Intel, current-QCoDeS extended array values such as `1e400`
  or `1e-400` produce infinity/NaN or zero despite finite nonzero recordings.
  Distinct extended samples whose narrow values coincide can report zero spread.
- Fix: validate actual measurements and declared setpoints before conversion,
  explicitly refusing complex data, finite samples outside the display range,
  nonfinite summaries and an unrepresentable nonzero spread. Keep original
  arrays for exact centering; complex ancillary inference inputs remain valid.
- Fifteen meaningful regressions fail before the repair on Intel. The related
  author suite passes **46 tests / 7 expected platform skips on ARM**, **53 on
  Intel**, including four positive precision/extreme-value controls. Logs:
  `/private/tmp/qplot-r28-measurement-c041-after-{arm,intel}.log`.
- Different safety subchat independently **approved** source and real menu
  behavior: **7 ARM / 10 Intel compositions**, **28 passed / 7 expected skips
  on ARM**, **35 passed on Intel**. Warning-free refusals, ragged and map axes,
  complex ancillary bases, no-finite handling and valid recovery all pass.
  **57 protected-file comparisons**, 17 retired snapshots and all helper/dialog
  cleanup checks pass. Receipts:
  `/private/tmp/qplot-r28-safety-numeric-review-{arm64,x86_64}-receipt.json`.

#### Round-28 completed coverage

- Safety lane found no additional bug: **40 focused tests passed**. Fresh live
  current-QCoDeS array composition verified **2,100,086 exact values**, 18
  protected-file comparisons, five writer-owned checkpoints and eight physically
  read-only main/WAL descriptors across two helpers. Coalesced cancellation,
  active-WAL statistics refusal/checkpoint recovery and copied-run local versus
  captured identifiers passed. All helpers, snapshots, prefixes and writers
  retired. Receipt: `/private/tmp/qplot-r28-safety-receipt.json`.
- UI lane found no additional bug: **126 focused tests and 17 subtests passed**.
  Three real menu actions, eleven captured-dialog clipboard checks, switching,
  Close Database and two nested-menu source transitions pass without callback
  errors. Four dialogs and background resources retire; a million int64 samples
  generate statistics in 0.265 s locally. Receipt:
  `/private/tmp/qplot-r28-ui-receipt.json`.
- Coordinator settings/preferences/package checks: **108 passed**. Fresh real
  QCoDeS/Qt export composition: **1 passed**, six CSV/TSV exports preserving 60
  exact values, three persisted filename-format changes, one rejected atomic
  settings write and one cancelled export. Correct suggestions remain available
  through the reused exporter; source artifacts are unchanged and owned UI and
  reader resources close. Receipt:
  `/private/tmp/qplot-r28-coordinator-receipt.json`.
- Fresh measurement composition on each architecture covers eight real inferred
  array runs and 24 incremental snapshots: repeated coordinates, ragged records,
  planned-shape overruns, missing text bases, signed/unsigned/float16/extended and
  nonfinite/extreme samples. Each passes **128 exact sample checks**, **92
  independent Fraction/100-digit Decimal summary oracles**, 24 protected-file
  comparisons and reader cleanup. Receipts:
  `/private/tmp/qplot-r28-measurement-oracles-{arm64,x86_64}.json`.
- Actual project-venv `python -m qplot` startup on the frozen repairs passes:
  visible window, running Qt event loop, isolated settings/logs and clean exit 0.
  Receipt: `/private/tmp/qplot-r28-ui-startup-receipt.json`.
- Target is the next bugfix **1.6.3**, retaining native **1.0.1**. Native source
  did not change and does not need rebuilding for these Python repairs.
- Campaign total: **40 confirmed application/diagnostic repairs**, plus six
  earlier test-only CI corrections. Round 28 is not clean; clean-round count
  remains **0**. Next: fresh round 29, then further review until two consecutive
  complete clean rounds or the authorized round-32 limit; finish full validation
  and update the new draft PR without merging or releasing.

### Round 29 — complete: two confirmed bugs fixed

Three fresh subchats review native/snapshot failure recovery, measurement
composition and live-cut/dialog lifecycle. The coordinator examines generator,
settings, diagnostics, entry points and packaging. Exact edit ownership is
assigned before changes; no overlapping author files.

- **C19-042 confirmed:** current-QCoDeS scalar and array measurements containing
  `[2 * smallest_subnormal, -smallest_subnormal]` show `Average: 0.000` even
  though the exact Fraction mean is nonzero and the displayed spread is positive.
  Nine real menu compositions reproduce both signs and mixed integer/float
  records, with protected sources and cleanup. Fix: check exact cancellation
  only when the computed mean is zero, reusing the existing exact accumulator
  without changing `finite_mean` or rendering semantics. Author focused suites:
  **67 passed / 8 expected skips on ARM**, **75 passed on Intel**. Evidence:
  `/private/tmp/qplot-r29-measurement-underflow-receipt.json`.
  Different safety subchat independently **approved**: **49 passed / 8 skips
  on ARM**, **57 passed on Intel**, plus 21 distinct real menu acquisitions
  involving large cancellation, mixed uint64/int64/float residuals, ragged exact
  zeros and ordinary/large-offset controls. All 63 protected-file comparisons,
  21 retired snapshots, exact-helper oracles and cancellation checks pass.
  Million-record zero-mean menus take 0.550/0.235 s for float/int on ARM and
  1.081/0.397 s on Intel. Receipts:
  `/private/tmp/qplot-r29-safety-c042-review-{arm64,x86_64}-receipt.json`.
- **C19-043 confirmed:** change a newly opened heatmap cut's enabled color
  selector before its queued first data publication. The Qt callback calls
  `update_sweep()` without `dataGrid`, raising an uncaught `AttributeError`.
  The actual 3×4 current-QCoDeS heatmap reproduction preserves source files.
  Recoloring during a pending axis swap also moves/hides the old marker using
  requested axes against committed samples. Fix: recolor the existing trace and
  marker without rereading data; retain chosen color before first publication.
  Notify sample consumers only when no axis transaction is pending.
  Author related suite: **183 tests and 10 subtests passed**, including seven
  new lifecycle regressions. Evidence: `/private/tmp/qplot-r29-ui-color-probe.log`.
  Independent measurement review caught an initial repair gap: a merged overlay
  cleared during pending recolor and stayed empty after cancellation. Two
  strengthened real-overlay regressions fail before the notification guard and
  pass afterward; final relevant subset **22 passed**. Different reviewer
  approved the corrected source with two horizontal/vertical cuts, nine recolors,
  17 marker checks, seven merged-consumer checks, 59 exact quadratic sample
  checks and 18 protected-file comparisons. Both rollback and committed-axis
  changes preserve the intended data/appearance policy. No callback errors and
  complete cleanup. Receipt:
  `/private/tmp/qplot-r29-measurement-c043-review-receipt.json`.
- Safety investigation is complete with no additional finding: **34 tests
  passed**; 19 native refusals and 20 successful recoveries, cancellation and
  deadline expiry, **1,620,126 exact values**, 36 protected-file comparisons,
  five writer-owned checkpoints and eight physically read-only descriptors.
  Private spool/copy ENOSPC, cancellation after connection open and deadline
  after snapshot attachment all retire resources correctly; reopening recovers.
  No prohibited VFS operations or proof leaks. Native runtime pins verify APSW
  3.53.4.0, SQLite 3.53.4 and native 1.0.1. Receipt:
  `/private/tmp/qplot-r29-safety-receipt.json`.
- Fresh numeric composition on **each** architecture covers eight actual
  QCoDeS acquisitions and 64 workers, with 288 exact raw-sample checks, 288 CSV
  round trips, 192 grid, 192 centering and 384 derivative oracles. All eight
  protected-file comparisons and reader cleanup checks pass. Receipts:
  `/private/tmp/qplot-r29-measurement-composition-{arm64,x86_64}-receipt.json`.
- Final UI composition passes 87 exact plotted/exported values, 14 protected
  comparisons, six color actions, three pending swaps, six live refreshes,
  two CSV/TSV exports and six dialog-reuse cycles. Tiling wrap/reset, source
  switching, rejected ranges/exports and public Close Database recover normally.
  Nine owned QObjects, five dialogs and both services retire without callback
  errors. Final corrected-source actual module startup also passes with private
  settings, visible window/event loop and exit 0. Receipts:
  `/private/tmp/qplot-r29-ui-receipt.json` and
  `/private/tmp/qplot-r29-ui-startup-receipt.json`.
- Coordinator focused generator/auxiliary/diagnostic/distribution suite:
  **122 passed**. Fresh generator-to-reader-to-persistence composition passes:
  literal `#`, `%`, `?`, spaces and Unicode filename, 78 exact setpoint checks,
  four closed readers, nine protected-file comparisons, rejected unconfirmed
  overwrite, cancellation during generation, safe settings/log refusal at the
  measurement path, recovery of settings/log rotation and explicit selected
  synthetic-output replacement. All fixtures retired. Receipt:
  `/private/tmp/qplot-r29-coordinator-receipt.json`.
- Whole-tree Ruff, mypy (**88 files**) and project-venv dependency check pass.
  The project editable metadata now reports **1.6.3**. Initial draft-head CI at
  exact `9f802428ee25103ec5182c68a34bc5cbe1c94c19`: 46 checks, 10 successful,
  20 running, 16 queued, no failure; not yet a complete green result.
- Two actual `python -m qplot` launches load the exact generated database with
  literal filename punctuation/Unicode, once as a plain positional argument and
  once following Qt style options and `--`. Both show the window, enter the Qt
  event loop, load/select the run and exit 0 with isolated settings, unchanged
  protected files and removed fixtures. Receipt:
  `/private/tmp/qplot-r29-startup-path-receipt.json`.
- Round 29 is not clean; consecutive clean-round count remains **0**. Campaign
  total is **42 confirmed application/diagnostic repairs**, plus six earlier
  test-only CI corrections. Next: fresh round 30, then 31/32 as needed. Final
  full-suite, exact-artifact/install and hosted CI validation remains due.

### Round 30 — complete: two confirmed bugs fixed

Three new subchats began from clean, committed `2602ea7`, pushed to draft PR #91.
The coordinator owns packaging/native-install acceptance and profiling. The
clean-round count remains zero; remaining authorized rounds are 30–32.

- **C19-044 confirmed:** while a heatmap cut's axis change is pending, drag its
  still-enabled source marker and cancel. The cut restores committed samples,
  but the marker remains on a different row and the merged overlay remains
  empty. Actual current-QCoDeS/Qt reproduction, protected files and cleanup
  verified. Fix: restore the committed marker position/axes and avoid sample
  publication while a cut-axis transaction is pending. Eight real regressions
  fail before, covering horizontal/vertical cuts, commit/cancel and both outer
  bounds. Author related suites: **221 passed and 10 subtests**. Different
  measurement subchat independently **approved**: **16 focused tests passed**,
  eight requests, 26 marker actions (edge, keyboard and group), cancellation,
  supersession and injected failures; **846 exact samples**, 54 CSV values and
  24 protected comparisons. All QObjects/readers/fixtures retire. Evidence:
  `/private/tmp/qplot-r30-ui-marker-receipt.json`.
  Independent receipt: `/private/tmp/qplot-r30-c044-independent-receipt.json`.
- **C19-045 confirmed:** select `[2*tiny, -tiny]` in a heatmap containing other
  normal-scale cells, where `tiny` is the smallest binary64 subnormal. The
  enabled marquee Stats dialog displays a zero mean despite a nonzero exact
  Fraction oracle. Selecting `[0, tiny]` also displays zero spread for distinct
  samples. The older selection formatter bypasses full-dataset summary guards.
  Fix: compute summaries once and return a copyable explanation with selection
  count/ranges when finite means/spread cannot be represented. Preserve exact
  cancellation and constant-zero results. Twelve real line/heatmap and mixed
  record regressions fail before; eight exact-zero controls remain valid.
  Author related suite: **94 passed / 8 expected ARM skips**. Reproduction:
  `/private/tmp/qplot-r30-marquee-probe.log`.
  Different safety subchat independently **approved**: **48 focused tests
  passed**, plus **on each of ARM and Intel** 19 actual line/heatmap acquisitions,
  ten correctly refused selections and nine valid controls, 38 Fraction/Decimal
  oracles, 57 protected comparisons and 57 Copy checks. Captured dialog text
  survives selection changes; valid selections recover. All 38 dialogs, 19 plots and background
  readers retire without callback errors or runtime warnings; no snapshot
  directories were created. Million-sample exact-zero
  summaries take 0.484 s float / 0.207 s integer locally. Receipt:
  `/private/tmp/qplot-r30-safety-c045-review-{arm64,x86_64}-receipt.json`.
- Independent packaging audit: **116 passed / 2 expected Windows skips**.
  App/docs/release metadata consistently target **1.6.3**, native/APSW pins stay
  **1.0.1 / 3.53.4.0**. Native tree
  `6c9809d7830d69ae5fbe78e96417f5886f28009b` matches the verified baseline;
  all eight tracked native paths match the cached sdist, both cached artifact
  hashes match 14 earlier exact-source receipts and the wheel contains arm64
  and x86_64 slices. Runtime SQLite/source-ID checks pass. No native rebuild is
  needed; final fresh environments will exercise the exact cached native wheel.
- Fresh diagnostic profiling of 140,000 generated current-QCoDeS samples passes
  three repeated paged scans in both optimized and full-path-validation native
  modes, with identical result checksums, no prohibited VFS operations and
  unchanged protected files. Literal path punctuation/Unicode and cleanup pass.
  Receipt: `/private/tmp/qplot-r30-profiler-receipt.json`.
- Safety lane complete with no additional bug: **117 focused tests passed**.
  A fresh three-run composition covers 129 queued cancellations plus a coalesced
  cancellation, 131 priority changes, an expired queued deadline, writer append
  and checkpoint during paused BLOB capture, immutable old/fresh prefixes,
  asynchronous close and reopened namespaces. **1,114,208 exact values**, 12
  protected comparisons, four writer checkpoints and eight physically read-only
  descriptors pass. Both services, seven spools and writer retire. Cancellation
  and `close_async` return in 0.015 and 0.020 ms locally. Receipt:
  `/private/tmp/qplot-r30-safety-receipt.json`.
- Fresh non-statistics composition on each architecture passes six real ragged,
  planned and serpentine acquisitions: clipping, exact centering, nonuniform FFT
  and raw CSV. Per architecture: 60 raw values, 30 exact clipped cells, 36
  Fraction-interpolation/trigonometric-DFT oracles, 60 CSV round trips and 18
  protected comparisons; all six readers and fixtures retire. Receipts:
  `/private/tmp/qplot-r30-transform-composition-{arm64,x86_64}-receipt.json`.
- Fresh UI composition exercises auto-plot dormant retries, unchanged-metadata
  non-replay and real completion-metadata rearming, merged export and queue
  disable/re-enable/Close Database. **27 controls** before first publication,
  64 exact values, two exports, five actual Refresh clicks and ten protected
  comparisons pass. Ten QObjects and the service retire without callback errors.
  Actual project-venv qPlot **1.6.3** startup passes on both frozen fixes, with
  source hashes, private settings/logs, visible window/event loop and exit 0.
  Receipts: `/private/tmp/qplot-r30-ui-receipt.json` and
  `/private/tmp/qplot-r30-ui-startup-receipt.json`.
- Whole-tree Ruff, mypy (**88 files**) and diff checks pass. Campaign total:
  **44 confirmed application/diagnostic repairs**, plus six historical test-only
  CI corrections. Round 30 is not clean; clean-round count remains **0**.
  Next: fresh rounds **31 and 32**, the two remaining authorized rounds. Final
  full-suite, stamped exact-artifact/compiler-free installs and CI acceptance
  remain due. Do not merge, tag or release.

### Round 31 — complete: no new actionable bugs

Three fresh subchats independently reviewed committed `1a7c7d7`. The coordinator
validated stamped distribution artifacts and fresh compiler-free installations.
Application, tests and native source did not change during this round.

- Safety: **56 focused tests passed**. A new two-helper composition covers
  partial array capture, clean and abrupt writer restarts, helper termination,
  sibling service/prefix survival and reopened namespaces. **3,960,990 exact
  values**, 407 literal NPY 3.0 bytes, 27 protected-file comparisons, 20 physically
  read-only descriptors and four writer-owned checkpoints pass. The failed
  helper retires in 3.46 ms locally; the longest checkpoint takes 6.07 ms.
  All four services, the direct native reader, three writers and 13 spools
  retire. Native prohibited-operation counters remain zero; proof handles
  balance. Receipt: `/private/tmp/qplot-r31-safety-receipt.json`.
- Measurement: **234 focused tests passed**. On **each of ARM and Intel**, eight
  current-QCoDeS acquisitions and 40 workers cover empty/ragged/planned arrays,
  endian/Fortran storage, large signed/unsigned integers, unsupported complex
  data and operation-limit recovery. Per architecture: 432 exact raw values,
  432 raw CSV cells, 336 grid checks, 288 directional-fill oracles, 24 exact bin
  means, 864 derived CSV cells, six preview comparisons and 42 Fraction/Decimal
  statistics oracles pass. Fifteen protected comparisons, six readers, eight
  prefix directories and service/fixture retirement pass; both processes exit 0.
  Temporary-directory finalizer notifications were checked against the actual
  paths: all were absent, with no runtime warnings. A suspected operated-grid
  narrowing path is unreachable because real workers enforce the full-resolution
  operation limit first. Receipts:
  `/private/tmp/qplot-r31-measurement-composition-{arm64,x86_64}-receipt.json`.
- UI: **98 focused tests passed**. A distinct cross-database mixed workflow
  covers retained hidden sources, provenance, Top/Right reassignment,
  visibility/opacity/width changes, preferences with appearance open, injected
  promotion failure/recovery, two source promotions and SVG/CSV export.
  **72 exact values**, 24 protected comparisons, 11 destroyed QObjects and two
  closed services pass, with no callback errors or remaining fixtures/helpers.
  The print-dialog lead was withdrawn: stubbing `exec()` bypasses the installed
  SIP `sipTransferBack` ownership transition, exactly the historical withdrawn
  BH-027 reproduction. No speculative print change was made. Receipt:
  `/private/tmp/qplot-r31-ui-receipt.json`.
- Coordinator built app **1.6.3** wheel/sdist with clean-source provenance for
  exact `1a7c7d7b5771e46455ca13590b23c48e751c035f`, unreleased/draft PR #91.
  Verified every tracked archive member against that commit: 133 wheel members
  (127 runtime files), 418 sdist members, all 251 test modules and conftest.
  Reused native **1.0.1** artifacts only after unchanged-source/hash verification;
  Twine passes all four artifacts. Manifest:
  `/private/tmp/qplot-bh20261023-1a7c7d7-artifact-manifest.json`.
- Fresh compiler-free **ARM and Intel wheel and editable** acceptance
  pass: wheel-only dependencies, actual installed qPlot startup, native live-reader
  proof, installed-file hashes and environment cleanup. Intel's temporary harness
  runs only Git metadata queries as ARM because this host's CommandLineTools
  `libxcrun` lacks an Intel slice; parent and installed Python are independently
  verified x86_64. No application workaround or compiler build is used.
  Logs: `/private/tmp/qplot-r31-{wheel,editable}-install-{arm64,x86_64}.log`.
- Round 31 is the **first consecutive complete clean round**. Campaign total
  remains **44 confirmed application/diagnostic repairs**, plus six historical
  test-only CI corrections. Fresh **round 32 is in progress and is the final
  authorized round**. Finish final-source full-suite/artifact/CI validation and
  update draft PR #91 without merging or releasing.

### Round 32 — complete: two confirmed bugs fixed

Three fresh subchats reviewed native rollback/WAL transitions and derived
shutdown, operation-chain correctness, and modal-dialog/live-plot lifecycle.
Every repair had a different subchat's independent review before commit.

- **C19-046 fixed:** on Intel, enter an exactly
  representable extended-precision clipping bound through the normal Limit
  Maximum/Minimum control, then apply a derivative. The clipping helper narrows
  the configured Decimal bound through binary64 first, so both the plotted
  derivative and its CSV export are wrong. Two real current-QCoDeS/public-control
  reproductions fail without warnings; source protections and cleanup pass.
  Reproduction: `/private/tmp/qplot-r32-measurement-clip-before.log`.
  Fix: convert configured decimal text directly to the homogeneous extended
  sample dtype, preserving direct float-call semantics and the existing exact
  integer/object path. The final real shaped-QCoDeS regression fixture has
  **8 failures before** (six public line/row/column operation chains and two
  Decimal controls). Related suites: **45 passed / 34 expected ARM skips**,
  **79 passed on Intel**. Drawing, CSV and Clear/reset use exact oracles.
  Author receipt: `/private/tmp/qplot-r32-measurement-c046-author-receipt.json`.
  Different safety reviewer independently **approved**: **35 passed / 12
  expected skips on ARM**, **47 passed on Intel**. Distinct public min/max then
  derivative compositions pass **12 ARM / 24 Intel acquisitions**, 280/560 exact
  displayed/processed cells, 140/280 raw-reset cells, 140/280 TSV cells and 36/72
  protected comparisons. Intel's old function gives the wrong result for all
  four signed-bound combinations; corrected results match Fraction oracles.
  Float/Decimal/NaN/Inf controls pass. All 36/72 QObjects and 12/24 services
  retire without callback/runtime warnings. Receipts:
  `/private/tmp/qplot-r32-safety-c046-review-{arm64,x86_64}-receipt.json`.
- **C19-047 fixed:** while the real Close Database
  confirmation is open, a live QCoDeS writer adds a run and normal Auto-plot
  publishes it. The close routine's pre-dialog window snapshot omits that plot;
  accepting the dialog releases its dataset handle while leaving it visible,
  and subsequent refresh fails. Reproduction uses the actual modal event loop,
  not a mocked dialog. Fix: re-snapshot current plot windows after accepting
  the confirmation, before closing windows and releasing their readers.
  Evidence: `/private/tmp/qplot-r32-ui-close-confirmation.log`.
  Red regressions: **2 failed,
  2 passed**; author related suite **175 passed and 2 subtests**. A different
  measurement subchat passes **36 focused tests** plus four distinct real modal
  cases with queued two-output heatmaps and an existing parent/cut. **256 exact
  samples**, 48 CSV values, eight deferred publications and 48 protected-file
  comparisons pass; 16 QObjects and four services retire. An extra pending
  request immediately before accepted Close Database is cancelled, with no
  post-close publication after service retirement and event-loop pumping.
  Independent **approval** receipt:
  `/private/tmp/qplot-r32-measurement-c047-independent-receipt.json`.
- Fresh safety lane finds no additional defect: **23 focused tests passed**;
  50,154 exact values, 48 protected comparisons, 12 physically read-only
  descriptors, five native sessions with clean audits and six writer-owned
  checkpoints. DELETE→WAL→DELETE, genuine held-journal refusal, last-writer
  close/reopen and immutable-prefix reuse after derived/service cancellation
  pass. A stopped helper retires in 0.154 s locally; three services, two spools,
  writer and fixture retire. A probe that skipped normal bootstrap/basic-page
  acceptance was corrected, not counted as an application bug. Receipt:
  `/private/tmp/qplot-r32-safety-receipt.json`.
- Fresh UI composition covers retained Color Scale/Appearance editors, heatmap
  and cut, real Preferences dialogs, settings-save failure/rollback/retry,
  two settings applies, cancelled Quit and accepted Quit during pending refresh.
  **80 exact samples**, 18 protected comparisons, three Preferences sessions
  and two real quit confirmations pass. All dialogs/plots/readers retire with
  no callback errors. Actual project-venv `python -m qplot` startup passes with
  version **1.6.3**, visible window/event loop and exit 0. Both final source
  hashes are recorded before launch and unchanged afterward; private settings
  retire. Receipts: `/private/tmp/qplot-r32-ui-receipt.json` and
  `/private/tmp/qplot-r32-ui-startup-receipt.json`.
- Whole-tree Ruff, mypy (**88 source files**), dependency and whitespace checks
  pass. Application fixes are committed as `952f56b` (C19-047) and `e0895df`
  (C19-046). The native sources remain unchanged; no native rebuild is needed.
- The integration run against `1a7c7d7` was deliberately interrupted before
  changing application source: **2,419 passed, 34 skipped**, no test failures,
  pytest exit 2 from requested SIGINT. This is **not** a complete-suite pass.
  Its log is `/private/tmp/qplot-r32-interrupted-full-1a7c7d7.log`. The complete
  final-source suite and rebuilt app-artifact acceptance are next; round-31
  installs remain evidence for their exact recorded revision.
- All **five authorized rounds, 28–32, are complete**, with **eight confirmed
  repairs** (2, 2, 2, 0, 2). Campaign total: **46 confirmed application/diagnostic
  repairs**, plus six historical test-only CI corrections. Round 32 resets the
  consecutive clean-round count to **0**; the two-clean-round target was not
  reached within this authorization. Do not start round 33 without a new request.
- Integration checkpoint: preserve frozen code/tests, finish full-suite and
  exact-artifact/compiler-free installation/CI acceptance, record the results,
  commit/push and update draft PR #91 for bugfix **1.6.3**. No merge, tag or release.

### Final integration validation — local acceptance complete

- Frozen validation revision: **`f1f0efa990967411f8710ba17aa12e12d02267e6`**,
  committed and pushed to draft PR #91. The code/tests are frozen; subsequent
  validation-record edits affect only this campaign log.
- Rebuilt app **1.6.3** wheel and sdist from that clean commit, with unreleased
  PR #91 provenance. Verified every tracked member against Git: 133 wheel
  members / 127 runtime files; 420 sdist members including all 253 test modules
  and conftest. App/native Twine checks pass. Artifact manifest:
  `/private/tmp/qplot-bh20261023-f1f0efa-artifact-manifest.json`.
- All **four compiler-free installs pass**, using these exact final app/native
  artifacts: ARM and Intel, wheel and editable. Each checks installed file
  hashes, dependency consistency, entry points, actual qPlot startup and native
  trusted-live behavior. Editable source edits take effect without reinstall;
  native files remain unchanged. Intel interpreter architectures are verified.
  All owned environments and temporary host-Git helpers are removed. Receipt:
  `/private/tmp/qplot-r32-install-acceptance-receipt.json`; logs:
  `/private/tmp/qplot-r32-{wheel,editable}-install-{arm64,x86_64}.log`.
- Native source tree still matches the verified baseline exactly. The cached
  native **1.0.1** universal wheel/sdist hashes match the audited sources; no
  local native rebuild was necessary. Hosted CI additionally rebuilt all four
  native platform wheels successfully for the frozen revision.
- Complete final-source suite: **5,644 passed, 128 skipped, 61 warnings** in
  **949.67 seconds**, exit **0**, accounting for all **5,772 collected items**.
  Run with offscreen Qt, the fixed Matplotlib cache and project-venv
  `python -m pytest --no-cov -n 2 --dist=loadfile --no-loadscope-reorder`.
  Extended-precision cases skipped on ARM are covered by the Intel author and
  independent-review runs above. Log: `/private/tmp/qplot-r32-final-full.log`.
- Hosted CI for the frozen source has no reported failure at this checkpoint;
  native/application builds and 30 checks have succeeded, with remaining matrix
  jobs running or queued. The final documentation-only head must receive its own
  required-check acceptance; do not infer a final green result from these partial
  observations. Exact-SHA receipts are retained under
  `/private/tmp/qplot-r32-ci-<shortsha>.json`, with the final observation recorded
  in the [draft PR](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/91).
- Investigation and local acceptance are complete at the authorized five-round
  limit. Remaining integration task: push this validation record, verify hosted
  CI for the exact final head, and update draft PR #91 with its result. This log
  is the only file changed after frozen-source validation. No further round,
  merge, tag or release is authorized here.

## Campaign starting 2026-10-19 — round 27 completed 2026-10-21

**The one additional authorized round, 27, is complete: no new actionable bugs.**
Three fresh subchats and coordinator integration exercised live cancellation and
reader recovery, transform/export recovery, mixed-plot ownership, generation,
settings and startup. Only this campaign record changed. The campaign remains
at **38 confirmed application/diagnostic repairs**, plus **six test-only CI
corrections**. Do not start round 28 without a new user request.

Starting local, fetched and remote-tracking revision was
`4ab5862beb3db93bac06688e5bcb5815aff6a510`. Its exact-SHA CI initially had
25 successful, 12 running and 9 queued checks; a later observation had
**31 successful, 10 running and 5 queued**, with no reported failures.
All 46 returned checks matched the SHA and API total. Hosted acceptance,
including round 26's Windows SHM-observation correction, remains pending.

The last full frozen-source suite at `fce02c3` passed **5,545 tests, 110 skipped**;
round 26's changed helper and all its consumers passed **116 tests, 2 skipped**.
Round 27 focused suites passed **8 safety, 23 measurement, 58 UI and 58
coordinator tests**, plus fresh end-to-end compositions and actual qPlot
startups. Application/native source remains unchanged from `5c9d7e6`; test and
script source remains unchanged from `4ab5862`. Bugfix target remains **1.6.2**,
native **1.0.1**; draft PR #90, no merge, tag or release.

### Verified baseline

- Intended revision: `d766a0f45edf266215e8366b8ff8d5ccf65b9a0e`, current
  `main` / `origin/main` after successful `git fetch origin`; clean working tree.
- Verified **49/49 check runs completed successfully for that exact SHA**,
  including Required checks, all platform/Python jobs, native builds,
  compiler-free installations, static analysis, coverage and CodeQL.
  [Baseline CI](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/38631829472).
  There are no additional legacy commit statuses. The GitHub app's workflow
  lookup only covers PR events, so push checks were verified using the public
  commit check-runs API, including returned count and each `head_sha`.
- Dedicated branch: `codex/qplot-bug-hunt-2026-10-19`. No merge or release.
- Initial reviewed batch pushed at `25a46cc51c36f6bf579546b446b96d25aa6441b4`;
  [draft PR #90](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/90)
  opened for cross-platform CI. Investigation resumes with round 22.
- Local Python: project `.venv-mac`, CPython 3.12.2, QCoDeS 0.61.0,
  APSW 3.53.4.0. Installed native package was stale (1.0.0; source pins 1.0.1);
  rebuilt/installed current native source before runtime validation.
- Follow `Agents.md` and user-supplied AGENTS.md; preserve all database access
  restrictions, support current QCoDeS, and add no legacy settings migrations.

### Coverage, ownership and execution plan

| Owner | Initial owned source | Investigation |
| --- | --- | --- |
| Safety subchat | `src/qplot/datahandling/`, `native/src/` | Physical read-only handles, SHM exception, snapshots, live services, cancellation |
| Measurement subchat | `src/qplot/tools/` | Extraction/transform correctness, numerics, measurement preservation |
| UI subchat | `src/qplot/windows/` | Windows, workflows, lifecycle, refresh, export, shutdown |
| Coordinator | Remaining source, scripts, packaging, this log | Configuration, entry points, diagnostics, generation, build/install, integration |

- Shared checkout uses disjoint file ownership. Request ownership before editing
  another area; tests get distinct campaign-specific filenames. Only coordinator
  edits this log or commits. All substantive fixes get a different subchat review.
- Expand to all application areas, native packaging, performance and subsystem
  interactions. Require concrete reproductions and meaningful regression tests.
- Round 13 ended at the user's request. On 2026-10-20 the user authorized
  **up to five further rounds (14–18)**, followed by an update to draft PR #90.
  Stop sooner if two consecutive complete fresh rounds find no actionable bugs.
  The next release remains the bugfix **1.6.2**; no merge, tag or release.
- Clean-round count at resumption: **0** (round 13 found C19-031).
  Historical results below are not counted toward the new reviews.
- Previous extension finished with **two complete clean rounds, 16 and 17**.
  On 2026-10-21 the user authorized **up to four more rounds (18–21)** and a
  draft PR update afterward. Begin a fresh clean-round count at zero; stop
  sooner if two consecutive complete new rounds are clean. Bugfix target
  remains 1.6.2; no merge, tag or release.

### Resumption baseline — rounds 22–25, 2026-10-21

- Clean local branch and fetched upstream both match
  `f7c02fe543326bf8ff814d3388e4f88917fc6341`.
- At resumption, exact-SHA CI was **pending: 17 successful, 17 running, 12 queued**,
  no failures. All 46 returned checks match that SHA and the API total. This
  is not a green hosted baseline; continue independent investigation while
  monitoring the pending checks. Last completely green earlier baseline was
  `64aff93` (47/47). [Current CI](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/39562904041).
  Evidence: `/private/tmp/qplot-r22-baseline-checks.json`.
- Completion checked during final integration: **45/47 successful**. macOS
  Python 3.14 full shard 1/2 failed, making Required checks fail too. All returned
  check SHAs/counts match `f7c02fe`; this baseline is not green. The benchmark
  subprocess reports SIGBUS during explicit QApplication destruction. See the
  final CI follow-up below; evidence
  `/private/tmp/qplot-r22-baseline-checks-latest.json`.
- Current application source is unchanged from tested `3cb0e01`: 5,528 passed,
  110 skipped, plus final assertion checks; startup, static analysis, native
  artifact checks and compiler-free installations pass. Native remains 1.0.1.
- Three fresh subchats cover safety/live reading, measurement and UI/lifecycle;
  coordinator owns remaining areas, packaging, performance and integration.
  All investigators begin read-only and request exact file ownership before
  repairs. Substantive fixes require another subchat's independent review.

### Previous resumption baseline — rounds 18–21, 2026-10-21

- Clean local and fetched remote bug-hunt branch both point to
  `64aff93ded7ad2187a36e3ac097910623cf25abc`.
- Verified **47/47 check runs completed successfully for that exact SHA**,
  checking every returned `head_sha` and the API total. This includes the
  previously failing platform jobs and Required checks.
  [Verified CI](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/39512764225).
  Evidence: `/private/tmp/qplot-bh20261021-baseline-checks.json`.
- Final prior local validation: 5,509 passed/110 skipped, startup/static checks,
  exact-commit artifact checks and compiler-free wheel/editable installations.
  Three fresh subchats again own safety, measurement and UI investigation;
  coordinator covers remaining areas, packaging, performance and integration.

### Resumption baseline — 2026-10-20

- Clean local and fetched remote bug-hunt branch both point to
  `e5fc7ebe088a3e566de53c0f7002ffa9ec2e13c5`.
- Verified all **47 check runs** against that exact SHA and the API total:
  **43 successful, three test jobs failed, Required checks failed**.
  The previous local validation passed; the resumed CI baseline is not green.
  [Resumption CI](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/39323753600).
  Evidence: `/private/tmp/qplot-bh20261020-baseline-checks.json`.
- macOS Python 3.11/3.14 and Linux installed-sdist failures involve live
  inferred-parameter refreshes encountering a rollback journal/service closure.
  macOS 3.14 also reports a line-plot benchmark failure. Round 14 starts by
  identifying their causes, alongside fresh measurement and application review.
- Three independent subchats cover safety/live reading, measurement correctness,
  and UI/lifecycle. Coordinator covers packaging, settings, diagnostics,
  performance and integration; file ownership is assigned before repairs.

### Findings and validation

- Rebuilt and installed current native 1.0.1 from source (universal2 cp311-abi3).
  Refreshed stale application editable metadata to 1.6.1. No native source edit.
- Initial coordinator settings/diagnostics/auxiliary safety/generation/version
  checks: **200 passed, 28 subtests passed**. Ruff and mypy (88 files) pass.
- Actual `.venv-mac/bin/python -m qplot` with the existing packaging startup
  observer: window visible, Qt event loop running, normal exit **0**. Temporary
  settings/logs only. Sandbox blocked local IPC; approved unprivileged run
  outside the sandbox passed. This is an environment constraint, not a bug.
- Initial safety checks: **149 passed, 1 Windows-only skip** for trusted reader,
  read scopes and snapshots; **131 passed** for broker/supervisor/partial frames
  and concurrent WAL writes. No protected-file mutation found.
- Initial package tests: **132 passed, 5 skipped**, one stale-metadata pin
  mismatch. Editable reinstall alone did not repair pytest's precedence for an
  old ignored `src/qplotter.egg-info` (1.6.0b3/native pin 1.0.0). Removed only
  that generated metadata directory as permitted by CONTRIBUTING; all **7 pin
  and package-split tests passed** afterward. This was local environment residue.
- Existing 2,000,000-row synthetic heatmap benchmark passed: 500 x 500 grid,
  all finite, aggregation 2.394 s and preview 1.522 s. Temporary synthetic
  database only; timings are local observations, not portable requirements.
- Launcher/Windows containment suite outside IPC sandbox: **121 passed**.
- Initial UI batch: **335 passed, 3 skipped**; four sandbox IPC failures match
  the coordinator's successful external launcher validation. Real nested-dialog
  Close All/cancel probes passed (four cases); rejected hypotheses left no edits.
- Measurement second pass covers dimensional/dependency inference, current
  QCoDeS cache/ragged append and dtype behavior, trusted numeric/array capture,
  previews, metadata and bounded summaries. Additional 600 deterministic
  irregular derivative cases agree with independent Fraction stencils within
  1e-12. No new bug there; intentional finite-observation filtering retained.
- Obsolete integration batch 1 was gracefully interrupted after **226 passed**
  because C19-004/005 landed after its start. A coverage-instrumented restart
  was also stopped to avoid repeating expensive per-row instrumentation.
  The subsequent complete local suite finished: **5,073 passed, 44 skipped**
  in 1,645.53 s, log `/private/tmp/qplot-bh20261019-full.log`. It began after
  the initial five repairs; later source changes mean this is a transitional
  integration result, not final exact-source validation. PR CI retains coverage.

#### C19-001 — Generator CLI leaks CSV decoding/parser exceptions

- Real malformed UTF input and a CSV field beyond the parser limit escape from
  `main()` as `UnicodeDecodeError` / `_csv.Error` instead of its normal diagnostic
  and exit code 2. Both regression cases failed before the repair.
- Fix: catch these input-error families in the existing CLI error handler.
  GUI already handles validation failures. No generation/output mutation occurs.
- Tests: `tests/test_campaign_generator_input.py`; malformed files stay intact,
  output/temp files are absent, stderr is diagnostic and exit code is 2.
- Safety subchat independently approved scope, error handling and protection.
  Generator and regression suite: **90 passed**.

#### C19-002 — Differentiation rejects finite overflowing coordinate gaps

- Measurement subchat reproduced `x=[-1e308,1e308], y=x` failing with
  "axis coordinates must be finite" instead of derivative 1. Three-point
  irregular case also fails. The inputs are finite; adjacent subtraction is not.
- Fix validates original coordinates and computes overflowing-gap quotients
  from exact differences. Existing native-transform behavior needs no change.
- Real-QCoDeS tests cover lines/both heatmap axes, order reversal, tiny finite
  slopes, mixed/integer signals, missing values, invalid axes and cancellation,
  with source hash/mtime invariance. Focused suites: **113 passed**.
- Safety subchat independently approved; **79 related tests passed**.

#### C19-003 — Accepted cancellation during cleanup still publishes results

- A real native-backed reader is paused after result-limit restoration. A
  separate thread receives `interrupt()==True`, but the query returned normally.
  The regression failed before repair (`TrustedLiveCancelledError` not raised).
- `_finish_operation` now honors the latched cancellation reason before result
  publication. Regression checks rejection, subsequent reader reuse, no stale
  interruption, and unchanged source bytes/mtime.
- Test: `tests/test_campaign_safety_cancellation.py`. Independent UI subchat
  approved: mutex retirement makes the latched reason stable and generation
  local; no added native I/O or transaction lifetime. Regression passed.

#### C19-004 — Source promotion changes operations' scientific meaning

- Real-QCoDeS reproduction: add another line, apply Limit Maximum=1 in that
  source, swap host axes, then remove the primary dataset. Displayed overlay
  `(x=[0,1,1], y=[0,1,2])` becomes `(x=[0,1,2], y=[0,1,1])` after promotion;
  clipping was reapplied to the coordinate. Both line/heatmap hosts reproduce.
- Repair maps axis-specific heatmap operations when transposing. A transposed
  processed line cannot be represented by existing Y-only operation controls;
  preflight refuses that removal with guidance to swap axes back, preserving
  the current plot/data. Unprocessed transposed lines remain promotable.
- Tests: `tests/test_campaign_ui_promotion.py`; **16 passed**, covering both
  host kinds, refusal atomicity/swap-back recovery, unprocessed promotion,
  physical derivative labels, ordered mean/clip pipeline, repeated refresh and
  exported CSV samples. Broader mixed/drop checks: **87 passed, 2 expected skips**.
- Measurement subchat independently approved and reran final **16 cases**
  after **62** earlier promotion/mixed checks. UI runtime startup/clean Qt
  shutdown also passed. No unrelated operation-model redesign.

#### C19-005 — A genuine delayed cancellation retires a healthy helper

- Real supervisor public API reproduction: `cancel(first)==True`; first and
  second reads finish before the child control thread receives that cancel.
  The genuine delayed frame then causes the third read to fail with
  `TrustedLiveProtocolError`. Only child scheduling is delayed; no forged frame.
- Minimal fix uses completed and cancelled generation high watermarks to
  accept delayed cancellations for this helper's completed jobs. Contiguous
  command generations prove membership; initialize cancellation lower bound
  from the first generation. Memory stays bounded; duplicates, older-incarnation
  and future frames remain errors. Supervisor's lock and synchronous bounded
  sends guarantee increasing cancel-frame order; send failures retire helper.
- Safety regression file covers generations 1/17 plus prior-incarnation and
  future-frame rejection. The two positive cases failed before repair;
  all five campaign safety cases pass. Supervisor/protocol: **77 passed**.
- Measurement subchat independently approved with **7 passing** new/existing
  cancellation cases outside the IPC sandbox. No native source change.

### Fresh independent review rounds

- **Round 1 found three new actionable issues** with three new subchats using fresh contexts: safety and
  native/live interactions; measurement and numeric/export correctness;
  UI/lifecycle/settings/entry-point interactions. Coordinator covers remaining
  packaging/scripts/performance/integration. C19-006/007/008 below mean this
  round cannot count as clean. All eight fixes are independently approved.
- Round 1 measurement covered 488 focused tests and 1,152 independent oracle
  cases: exact derivative stencils, mixed-bin means, explicit DFTs, and real
  QCoDeS ragged/repeated/missing/mixed-dtype grids. Safety added 15 direct-reader
  and 5 broker cycles exercising commit visibility, cancellation/deadlines,
  reuse and writer checkpoints, plus 262 related tests. No further findings.
- **Round 2 found actionable issues** with three new contexts covering safety/native/auxiliary,
  measurement, and UI/lifecycle/interactions; coordinator covers remaining
  entrypoints, settings, generation, scripts, performance and packaging.
- Second reviewed batch pushed at `5fcbd9a`; actual qPlot startup again passed
  (visible window, running event loop, exit 0). CSV integration: 88 passed,
  1 platform skip. Configured mypy checks 88 files successfully; Ruff passes.
- Coordinator round 2 settings/generation/package/CLI validation: 291 passed,
  2 Windows-only skips, 25 subtests. Reviewed CLI argument flow, settings
  recovery/atomic saving, generation publication and compiler-free/install
  validation scripts. No new coordinator-area bug found in this round.
- The locally rebuilt universal2 native wheel also passed 85 tests under Intel
  emulation, with 1 Windows-only skip. Verified the imported native binary came
  from that extracted artifact, not an index wheel. No native source change.
- Coordinator built both application/native sdists and wheels from clean
  `25a46cc`. Inventory/metadata/twine checks pass: application sdist 378 files
  including all 214 test modules, wheel 124 runtime files; native sdist 11 files,
  wheel 2 runtime files. All nonbinary runtime bytes match that exact commit.
  Native universal2 binary contains ARM64/x86_64 slices. Artifacts:
  `/private/tmp/qplot-bh20261019-dist`. Later repairs require application rebuild.

#### C19-006 — Promoted heatmap cuts attach to another window

- Real QCoDeS/Qt reproduction: open B separately, drop B into A, remove A's
  primary to promote B, then open a horizontal cut in promoted B. Its marker
  appears in the original B window; promoted B has none. Both A host kinds fail.
- Sweeper creation chose the first matching dataset/parameter window instead
  of the emitting window. Fix forwards the emitting heatmap explicitly; direct
  callers retain the existing lookup behavior.
- Four real QCoDeS/Qt regressions failed before repair and now pass: horizontal
  and vertical cuts from promoted line/heatmap hosts, marker/slider ownership,
  bidirectional updates and closing isolation. File:
  `tests/test_campaign_ui_cut_ownership.py`.
- Author validation: 195 tests plus 2 subtests; broader 116 tests. Independent
  measurement reviewer approved with 36 tests. Actual qPlot startup passed.

#### C19-007 — Logging can append through a detached database handle

- Fresh safety review found that logging validates its pathname but writes
  through a retained stream. Rename an empty log file, create a current QCoDeS
  database at that renamed inode, then emit: the missing log pathname passes
  validation while the retained descriptor targets the database.
- Actual disposable QCoDeS database hash, size and mtime changed before repair.
  The handler now checks pathname/descriptor identity and retires invalid
  streams by closing the raw file first, preventing pending buffered output
  from flushing into a repurposed database during emit/flush/rollover/close.
- `tests/test_campaign_diagnostics.py`: 12 regressions, including missing and
  replaced log paths, rotation and buffered writes. Windows skips only open
  file rename cases which its filesystem disallows; same-path cases still run.
- Independent measurement reviewer approved; 36 tests and 3 subtests passed.
  Integrated diagnostics/cuts checks: 23 passed, 3 subtests; startup passed.
  Only disposable synthetic databases were used in these reproductions.

#### C19-008 — Plot CSV narrows extended-precision array samples

- Actual QCoDeS array read/worker/plot/export on Intel reproduced three distinct
  `longdouble` readings at `1 + [0,1,2]*eps` exported as three `1.0` values.
  ARM macOS cannot expose this loss because its longdouble equals binary64.
  Used Rosetta with the project universal Python and isolated temporary Intel
  dependencies; project environment was not replaced.
- Fix formats longdouble samples directly with NumPy's unique scientific
  formatter. Ordinary float16/32/64 retain the binary64 round-trip behavior;
  integers and publication safety are unchanged.
- `tests/test_campaign_measurement_longdouble_csv.py`: all 5 tests pass under
  Intel, including actual QCoDeS source invariance; ARM runs 4 with 1 expected
  platform skip. Independent reviewer approved after 1,016 extended-value
  round-trips plus ordinary floats and actual CSV on both axes, including
  values outside binary64, subnormals, signed zero, infinities and NaN.
- Reviewer corrected the additional test oracle: ordinary floats round-trip
  through binary64; parsing their shortest decimals directly into longdouble
  is a different representation and was not the promised contract.

#### C19-009 — Cut overlay stays blank after returning to a compatible axis

- Real QCoDeS heatmap, open horizontal cut, add through the host trace combo,
  switch cut to vertical and back. Cut window contains valid samples, but the
  retained overlay remains empty until another slider movement or refresh.
- Cut publication emits fresh samples and then invalidates compatibility,
  clearing the samples just published. Minimal repair reverses those signals.
- Dedicated real-control regression: `tests/test_campaign_ui_cut_axis_refresh.py`.
  Author's related suite: 214 passed, 39 subtests. Measurement reviewer approved
  C19-009/012 after 29 cut/cursor/promotion tests. Actual qPlot startup passed.

#### C19-010 — Extended array precision lost in drawing and statistics

- Actual Intel QCoDeS/Qt reproduction: `[1,1+eps80,1+2*eps80]` has three
  recorded values but renders a flat QPainterPath without the existing precision
  warning, and reports standard deviation zero. Subtract Mean restores visible
  differences, making the established warning/recovery workflow applicable.
- Independently reachable statistics case remains distinguishable in the plot:
  `[1+0.4*eps64,1+0.6*eps64]` reports deviation five times the exact Fraction
  oracle because each sample is rounded before centering.
- Repair extends existing precision guards and exact statistics paths to
  extended floating dtypes. Intel real-QCoDeS/Qt regressions: 11 passed; ARM
  broader measurement/cache/geometry/operations batch: 192 passed.
- UI reviewer independently approved after 200 exact-reference cases covering
  Fraction means, Decimal population deviations and display distinctions.
  Review caught an introduced overflow for means beyond binary64: fixed to
  preserve the previous signed-infinity result, with actual FFT control and
  callback-exception regression. Sources and raw CSV remain intact.
- Values wholly outside binary64 display range remain unplottable with Stats
  unavailable. Representable extended variations now get the existing
  subtraction/recovery warning instead of a silently flattened plot.

#### C19-011 — QCoDeS split-storage loading bypasses the read boundary

- Real public QCoDeS 0.61 `sqlite_per_dataset_db` measurement with 10 points:
  writer child exits leaving committed raw-data WAL. qPlot's read-only ID loader
  delegates DataSet construction to QCoDeS, which opens the recorded separate
  raw database with O_RDWR. Reading/closing/GC changes that database's hash and
  deletes its WAL and SHM. Reproduction uses disposable synthetic files only.
- The ordinary main-database connection remains protected, but the new QCoDeS
  backend opens another connection internally before qPlot regains control.
  Merely passing QCoDeS `read_only=True` would still bypass qPlot's pinned VFS.
- Safety repair: explicitly reject this unsupported multi-file backend
  before construction/plot loading, with a useful diagnostic. Full safe
  multi-file live viewing is a separate feature; standard main-DB runs remain
  supported. README documents this limitation.
- Three actual mixed-database regressions pass: ID/GUID refusal before raw
  connection creation; unchanged main/raw bytes, mtimes and sidecar existence
  through GC; trusted metadata/list accessible; expensive/derived/plot actions
  rejected; ordinary run counts and plot capture still work on the same broker.
- UI subchat independently approved the final guards and all construction
  routes. Author validation: 186 related loader/plot/query cases, 133 other
  safety cases, fresh WAL/cancellation probes and actual qPlot startup pass.

#### C19-012 — Promoted cut mapping assigns signal values to a coordinate

- Actual QCoDeS heatmap/cut/full-source workflow: promote another primary, then
  switch the retained cut to its other axis. Instead of becoming incompatible,
  the cut overlay maps measured values onto X and sweep coordinates onto Y.
- Cut `axis_options['y']` identifies its fixed setpoint, while plotted Y is the
  measured signal. Promotion's axis mapping incorrectly treats them as equal.
- Repair uses actual cut plot axis identities. Dedicated tests cover promoted
  and unpromoted cases, surviving line/heatmap hosts, axis return, sliders,
  refresh and style/sample invariance: 6 passed; broader mixed/drop/overlay
  batch 101 passed. Measurement reviewer independently approved with C19-009.

#### C19-013 — Unshaped extended-float heatmap aggregation crashes

- Fresh real QCoDeS array-grid probes on Intel reproduce a pandas
  `TypeError: No matching signature found` in `data2matrix`'s grouped mean /
  pivot path for unshaped longdouble heatmaps. Forced SQL heatmap route works.
- Repair routes extended floats through existing exact-cell aggregation and
  represents singleton/duplicate extended cells consistently as Decimal, avoiding
  unsupported NumPy longdouble/Decimal ordering in mixed grids. Narrow floating
  fast paths stay unchanged. Source samples are preserved.
- Final 19 regressions pass on ARM and Intel, including real Qt plotting, CSV,
  row-mean processing and cancellation. Another 32 deterministic real-QCoDeS
  grids agree with an independent exact-group oracle; related 199 tests pass.
- Fresh round 3 UI subchat independently approved and reran all 19 Intel cases.
  Ruff, mypy and actual qPlot startup pass.
- Fresh round 3 safety/UI reviews have started. Its measurement review and
  complete-round result must use the completed C19-013 repair.

#### Round 3 progress and rejected hypotheses

- Three fresh contexts now cover safety/native/entrypoints, UI/configuration,
  and measurement/generator/performance/packaging, with coordinator integration.
- C19-014 was reserved for a marquee-overflow hypothesis, then **withdrawn**:
  reproducing coordinates exceed PyQtGraph's visible linear range and qPlot's
  existing guards reject them. The probe drove unreachable offscreen positions;
  no normal workflow bug was established. Temporary test removed, no code edit.
- UI/configuration/CLI/entrypoint validation so far: 103 tests and 25 subtests;
  lifecycle 138 tests plus 4 sandbox-IPC cases passing when rerun unprivileged
  outside the sandbox. Complete UI round: 264 tests including the 19-case
  independent C19-013 review; no new UI bug established.
- Fresh native/live probe: 8 direct-reader cycles, 40 prohibited-query refusals,
  8 deadlines, 8 event cancellations, 8 query-error recoveries; 4 broker
  lifecycles with 48 coalesced/cancelled subscribers and exact 10,009+ row
  captures. All helpers/threads/endpoints reaped, 12 writer checkpoints,
  protected main/WAL/journal bytes and mtimes unchanged.

#### C19-015 — Synthetic generation inherits external QCoDeS side effects

- Actual QCoDeS configuration can redirect generator results into separate
  per-dataset databases outside its selected output; the resulting main is
  unsupported for measurement viewing. Automatic CSV export is more serious:
  an existing disposable QCoDeS main at the unselected export pathname was
  replaced by CSV bytes (32,768 SQLite bytes became 67 CSV bytes).
- The public Measurement API has no per-run backend/automatic-export override.
  Narrow preflight repair: reject non-main storage and enabled
  automatic exports before any generator directory/temp-file creation, without
  changing global QCoDeS settings used by a concurrent writer. Nonempty default
  subscribers are also refused; an actual callback-factory marker reproduction
  confirms those run during generation.
- Independent UI review found a further real path: inherited `Station.default`
  snapshots read invalid parameter caches, invoking acquisition callbacks.
  Generation now supplies a private empty `Station(default=False)` through the
  public Measurement API; global station identity and caches stay unchanged.
- Final 11 new cases plus broader generator/GUI suite: 163 passed. Independent
  UI reviewer approved after 30 tests. Existing unselected QCoDeS data remains
  unchanged; rejected settings create no destination directory/raw files/marker;
  standard output remains readable. `docs/demo-data.md` documents requirements.
- An additional `DataSaver.default_callback` hypothesis was disproved: current
  QCoDeS raises before invoking it and cleans temporary files, including with
  the final station repair. No speculative callback guard was added.
- Round 3 cannot count as clean. Native packaging/launcher/shutdown review
  completed with no new safety finding beyond C19-015.

#### C19-016 — Mixed-sign mean subtraction erases extended residuals

- Actual Intel QCoDeS/Qt native Subtract Mean and shaped-heatmap Row/Column
  Mean controls turn the centered zero sample of `[1+eps80,-1,0]` into zero;
  the exact result is `-3.614007241618348e-20`. The mixed-sign fallback narrowed
  each source sample before summing, losing the recorded cancellation residual.
- Repair retains exact original samples until final binary residual rounding,
  preserving the source dtype's precision, exponent range and subnormals.
  Ordinary floating paths and cancellation remain covered.
- Independent UI review caught halfway tie errors in an initial Decimal
  conversion. Final direct binary conversion is approved: 35 Intel tests,
  including 150 halfway cases, plus an independent 1,684-residual nearest-
  neighbor Fraction oracle across binary exponents -16,445 through 16,000.

#### C19-017/018 — Profiling helpers fail on current QCoDeS run types

- Actual subprocess reproduction: the plot-launch benchmark paints a valid
  three-point line then accesses nonexistent `plot1d.dataGrid`, raises
  AttributeError and exits with a Qt cleanup segmentation fault.
- The numeric reader profiler accepts an array run, aggregates array BLOBs,
  then raises `TypeError: bytes is not JSON serializable` while checksumming.
- Plot benchmark now reports line sample counts alongside heatmap grids and
  uses the application's bounded shutdown before deleting Qt objects. Numeric
  profiler selects declared scalar numeric parameters and explains array-only
  rejection through its CLI parser.
- Six actual subprocess regressions pass: repeated line/heatmap paint,
  invalid-run shutdown, scalar/mixed profiling and array-only refusal, all with
  unchanged protected database hashes, sizes and mtimes. Independent UI
  subchat reviewed both fixes and reran all six cases successfully. Additional
  reviewer WAL fixtures were refused by existing preconditions before the
  workload; those attempts are not counted as WAL validation.

#### Round 3 completion and round 4 integration

- Round 3 is complete and not clean: C19-015 through C19-018 were confirmed,
  repaired and independently approved. C19-014 remains withdrawn.
- Measurement/generator/package review: 692 tests passed, 2 platform skips;
  bounded-capture/CSV/native-transform integration: 336 passed. Independent
  Fraction grid oracle passed 140 real QCoDeS loads. Two-million-row benchmark
  again passed (500 x 500 grid, aggregation 2.328 s, preview 1.577 s locally).
- Fresh round 4 reviewers cover safety/native, measurement/performance and
  UI/lifecycle; coordinator owns integration and final artifacts.
- Integrated Ruff and configured mypy pass (88 files). Actual project-venv
  `python -m qplot` startup passed with visible window, running event loop and
  normal exit; temporary observer/settings at
  `/private/tmp/qplot-bh20261019-integrated-startup-6x1o99q_`.
- The next complete full suite finished with two xdist workers: 5,178 passed,
  52 skipped in 869.02 s, log `/private/tmp/qplot-bh20261019-final-full.log`.
  It began at the profiling-fix source; later C19-019/020 changes make this
  transitional validation. Native source remains unchanged.

#### C19-019 — Clipping mixed extended-float records fails

- Fresh round 4 actual QCoDeS 0.61/Qt reproduction on Intel: one signal record
  contains `longdouble([1,2])`, another `int64([3,4])`. Limit Maximum = 2.5
  should give `[1,2,2.5,2.5]`; the operation instead fails comparing
  `numpy.longdouble` with `Decimal`. Limit Minimum fails analogously. Both
  regression cases fail before repair; protected source remains unchanged.
- Narrow `pass_filter` repair compares extended object samples by exact ratios
  and, only when clipping actually changes data, normalizes retained extended
  samples to exact Decimal values so downstream rendering/CSV can order them.
  Original source cells and no-op results stay untouched. Cancellation is
  polled through the fallback loops; ordinary vectorized paths are retained.
- Author final Intel regression: 15 passed; ARM related batch: 59 passed,
  2 extended-only skips; related Intel batch: 44 passed. Actual qPlot startup
  passed. Independent safety reviewer verified 4,508 exact Fraction comparisons
  across extreme exponents, subnormals, thresholds and nonfinite values;
  60 ordinary-dtype cases unchanged, two 2-million-sample cancellation probes
  returned in 8 and 14 ms. Final independent Intel UI/render/CSV and related
  regression batch: 51 passed. Reviewer approved; round 4 is not clean.
- Artifacts at `b8fb173` passed inventory, exact committed runtime-byte checks
  (124 application runtime files), and twine. Application sdist contains 389
  files including all 225 test modules. Compiler-free wheel acceptance passed:
  installed runtime matches wheels, actual startup/live reader work, no compiler
  builds attempted. These artifacts and the 5,178-test full suite predate
  C19-019/020 and count as transitional validation.

#### C19-020 — Promoting a source resumes a paused plot

- Fresh real-QCoDeS UI reproduction: set Refresh interval to 0, then remove
  the primary dataset through Appearance. The promoted plot resumes at the
  configured 1-second default. Promotion passes the explicit zero correctly;
  `plotWidget.initRefresh` accepts only positive values and replaces zero.
- Single-condition repair in `_plotWin.py` preserves zero alongside positive
  intervals; prior absent/negative/NaN fallback remains unchanged. New 10-case
  `test_campaign_ui_paused_promotion.py` covers every host/promoted kind, real
  live timers, manual refresh and resume/re-pause. Six pause cases failed before
  repair, positive controls already passed. Author related suite: 35 passed.
- Independent safety subchat approved after 19 promotion/refresh tests. Actual
  qPlot startup and Ruff pass. Committed as `ab5c599`.

#### Round 4 completion and round 5 review

- Round 4 found C19-019/020 and is not clean. Fresh safety review itself found
  no further issue: 291 tests passed, one Windows-only skip; six reader cycles,
  30 mutation refusals, three 240,001-sample array records, four mid-capture
  cancellations/close operations. Protected hashes/identity/mtime unchanged,
  13 writer TRUNCATE checkpoints succeeded, all helpers/threads/endpoints reaped.
- Measurement review passed 144 real QCoDeS grid routes on 36 new random
  fixtures against independent grouped-Fraction oracles, plus 140 focused
  existing tests. UI review added 28 transform/ownership/lifecycle probes.
- Fresh round 5 UI, measurement and safety subchats review the repaired source.
  Clean-round count remains zero. Integrated Ruff/configured mypy pass.

#### C19-021 — Extended nonuniform FFT raises from interpolation

- Fresh Intel real-QCoDeS/Qt cases: `x=[0,1,3]`, `y=[1,2,4]`, with either X
  or Y stored as longdouble. Clicking FFT raises an uncaught `np.interp`
  TypeError refusing the float128-to-float64 safe cast. Both cases fail before
  repair; protected database and normal lifecycle checks pass.
- Expected spectrum follows uniform resampling `[1,2.5,4]`: frequencies
  `[0,2/9]`, magnitudes `[2.5,sqrt(3)/2]`. Measurement subchat owns narrow
  coordinate/interpolation changes in `_native_transforms.py` and real-control
  regressions. Independent safety review follows. Round 5 is not clean.
- Initial repair passed 12 Intel cases. Independent review caught a repair
  regression: rebasing `[-1e308,0,1e308]` makes a later binary64 scale infinite
  and rejects a previously supported FFT. Author is preserving the original
  scaling path for wide spans and adding forward/reversed uniform/nonuniform
  regressions. Revised fix is independently approved: 92 Intel tests passed
  (all 35 new cases plus related suites), and 240 deterministic Fraction
  resampling cases passed with exact expected binary64 DC and maximum relative
  AC error 3.31e-16. Raw recorded samples stay unchanged.

#### C19-022 — Delayed drop attaches a source after its target closes

- Drop an unloaded run preview onto a plot, then close the target before the
  existing trusted-metadata poll runs. The callback later attaches a hidden
  retained source and live dataset reference to the closed target.
- Deterministic actual QCoDeS/Qt reproduction fails all four host/source
  line/heatmap combinations without timing monkeypatches. Protected source
  files remain unchanged. UI subchat owns the target-lifetime check in
  `_plot_actions.py` and `test_campaign_ui_pending_drop.py`; different safety
  subchat will review before commit.
- Separate failed-before resource probe confirmed all four leaks: no visible
  plot windows remain, but the closed source has one merged-trace user, its
  dataset-holder reference count is one, and it still accepts refresh work.
- Narrow fix rejects closed targets before dispatch and cancels their delayed
  metadata requests through existing stale-request cleanup. Eighteen new
  regressions cover close/Close All, completed-before-poll requests, independent
  targets sharing a source, and target replacement by promotion. Author total:
  75 passed; real qPlot startup and Ruff pass. Independent safety reviewer
  approved after all 18 new cases plus 6 additional checks, including cancelled
  Close All retaining its still-valid drop and ordinary unloaded previews.
  Other-target requests remain isolated; completed metadata owns no SQLite
  connection. Committed separately from the numeric repairs.

#### C19-023 — Operations derivative narrows extended coordinates

- Actual Intel QCoDeS/Qt data `x=y=longdouble(1)+[.4,.6]*eps64` remains
  visibly distinct. Operations dy/dx returns `[0.2001953125,0.2001953125]`
  instead of `[1,1]`; native derivative correctly returns 1. The operation
  narrows dense floating coordinates to float64 before subtraction.
- Measurement subchat owns the narrow coordinate-dtype repair in
  `plot_tools.differentiate` and dedicated actual-control regressions, separate
  from FFT changes. Independent safety subchat will review. Protected source
  bytes/mtimes held in the failed-before reproduction.
- Final narrow repair widens narrow coordinates to binary64 while retaining
  extended coordinates before subtraction. All 32 new cases pass; author Intel
  batch: 167 passed, independent Intel batch: 147 passed. Independent 360-case
  Fraction stencil oracle has maximum relative error 1.05e-16, inputs unchanged.
- Unrepresentable extended heatmap coordinates are intentionally rejected by
  existing validation, so that initially blocked fixture was removed as bug
  evidence. Actual heatmap checks use accepted coordinates, with separate exact
  both-axis stencils. Both obsolete test processes/helpers were reaped.
- Independent safety reviewer approved. Integrated Ruff/configured mypy and
  actual project-venv qPlot startup pass with visible window, event loop, exit 0.

#### Round 5 completion

- Round 5 found C19-021/022/023; all are now fixed and independently approved.
  All 22 confirmed campaign findings are repaired. C19-014 remains withdrawn.
- Fresh safety/native review found no additional issue: 40 WAL commit/rollback/
  checkpoint cycles, 10 deadlines, 10 cross-thread interrupts, 10 result-limit
  failures, 40 private snapshots, 12 concurrent helper wait/cancel-or-close
  cycles, and 8 broker close_async cycles. Every protected-file check passed;
  writer checkpoints succeeded and all resources released (max helper shutdown
  0.126 s). Native audit counters for forbidden source operations stayed zero.
- A real crash fixture retained a 185,864-byte hot rollback journal and 10,012
  QCoDeS rows. Trusted open refused it; private SQLite/QCoDeS snapshots recovered
  the exact count/sum while original main/journal stayed unchanged.
- Fresh round 6 will review the frozen repaired source. Clean-round count is
  still zero; two consecutive complete clean rounds remain necessary.

#### C19-024 — Manual refresh leaves paused secondary sources stale

- Real QCoDeS acquisition/UI reproduction with main/host/hidden-source refresh
  intervals zero: initial primary `[1,2]`, secondary `[11,12]`; writer appends
  `[3,4]` and `[13,14]`. Clicking host Refresh updates primary to four samples
  but secondary remains at two; heatmap equivalent also fails. The source
  worker does not run. Protected file checks hold between intentional writes.
- Toolbar and R action call primary-only `refreshWindow(force=True)`;
  secondary synchronizers copy existing source data and retain zero intervals.
  UI subchat owns a narrow explicit manual dispatcher in `_plot_refresh.py`
  and `_plotWin.py`, leaving automatic and operation refresh semantics intact.
  Direct source workers are deduplicated and pause settings preserved. Source
  overlays are not traversed recursively; mutually overlaid windows cannot cycle.
- All 21 new regressions pass, covering line/heatmap combinations, hidden/visible
  sources, button/R action, repeated acquisition, shared sources/cuts, cycles,
  pending refresh coalescing, immediate closure and operation-Apply scope.
  Author related checks: 79 passed. Independent safety reviewer approved the
  final source with all 21 cases plus five ownership/pause/replacement checks.
  Protected-file assertions, Ruff/configured mypy and actual qPlot startup pass.
- Round 6 is not clean. Its candidate full
  suite was gracefully interrupted once this new required repair was confirmed;
  1,806 passed and 33 skipped in 205.93 s before interruption, no failures.
  Final full-suite validation is deferred until the repaired source stabilizes.
- Artifacts at `ae32de4` pass inventory, metadata/twine and exact committed
  runtime-byte checks: 124 application runtime files, 394 sdist files including
  all 230 test modules. Native source/binary remains unchanged.

#### Round 6 completion

- C19-024 is the sole actionable finding; all 23 confirmed findings are now
  repaired and independently approved. Clean-round count remains zero.
- Safety/native/package/process review: 68 focused tests passed. Four fresh
  sources with different page sizes and URI-special paths passed 44 protected
  file checks, 24 acquisition/schema changes, 12 each of private snapshots,
  deadlines, cancellations and result-limit failures, and four helper lifecycles.
  Both native slices, pinned versions and SQLite linkage boundary remain valid.
- Measurement/generation/package review: 178 focused tests passed. ARM and Intel
  each passed 132 fresh real-QCoDeS grid/cache/SQL comparisons and 400 exact
  Fraction derivative oracles. Intel passed 240 exact statistics comparisons;
  worst standard-deviation relative error 2.20e-16. Integer mean plus standard
  deviation for 300,000 samples took 0.138 s.
- Eight actual Qt composed workflows passed on each architecture: clipping,
  derivative, native mean/FFT, CSV, forced refresh and restored samples. Protected
  source hashes, mtimes and sidecar states held. One-ULP floating bin differences
  and probe-created overwrite dialogs were investigated and rejected as bugs.
- UI review additionally covered promotion/drop callbacks, cut mapping, source
  ownership, CSV collection, preferences/settings, shutdown and CLI routing.

#### C19-025 — Differentiation rejects valid mixed Boolean array records

- Current QCoDeS 0.61 `DataSet.add_results` accepts Boolean NPY array records
  (the higher-level DataSaver rejects them). Real shaped and unshaped runs
  containing Boolean, integer and floating records display `[0,1,2,3,4,5]`.
  Applying Operations dy/dx raises an isfinite TypeError wrapped in
  OperationExecutionError instead of returning six ones. Both actual Qt
  reproductions fail before repair; source hashes/mtimes and shutdown hold.
- The exact mixed numeric predicates explicitly exclude Boolean object cells;
  their fallback sends object arrays to NumPy isfinite. Measurement subchat
  owns `plot_tools.py` and `test_campaign_boolean_derivative.py` for the narrow
  repair. Final repair accepts Boolean cells in the exact mixed-numeric path;
  pure dtype handling and original measurements remain unchanged.
- All 36 new cases pass, including six failed-before real-UI cases, CSV output,
  restore, both heatmap axes, exact integer/extended coordinates, missing values,
  nonnumeric rejection and cancellation. Author ARM related batch: 277 passed;
  Intel: 153 passed. One implementation-coupled cancellation observer expected
  the old exact type tuple; updated its tuple only, all three controls pass.
- Independent boundary reviewer approved all three files. Final Intel rerun:
  39 passed. Independent ARM and Intel Fraction/Lagrange oracles each passed
  1,452 checks. Four 200k/2M-sample cancellation probes completed within 23 ms,
  inputs unchanged. Ruff, configured mypy (88 files), whitespace and actual
  project-venv qPlot startup (visible window/event loop/exit 0) pass.
- Round 7 is not clean. Coordinator integration has passed 390 rejected atomic
  settings transactions, six corrupt-settings recoveries, and an independent
  analytic comparison of all 250,000 spatial cells from 2,000,000 source rows
  (maximum absolute error 2.27e-13). Aggregation 2.439 s, preview 1.670 s;
  original database hash/mtime unchanged. Package/entry/profiling: 88 passed.

#### Round 7 completion

- C19-025 is the sole confirmed finding; all 24 findings are now fixed and
  independently approved. Clean-round count remains zero.
- Fresh boundary review: 292 focused tests passed. Three URI-special-path
  sources exercised first-WAL appearance, 12 acquisition rounds, 72 mutation
  refusals, 12 each of deadlines, interrupts, result limits and snapshots,
  six helper and six broker shutdowns with 42 selected-run subscribers.
  All 30 protected-file checks held, 15 writer checkpoints succeeded, forbidden
  native audit counters remained zero, and handles/threads/endpoints were reaped.
- Fresh UI review: 14 actual-QCoDeS/Qt compositions and 182 focused checks passed,
  with 33 expected platform skips. Covered both plot hosts, settings, swaps,
  transforms, SVG/PDF/clipboard, promotion, retained cuts, editor closure,
  close/reopen, cancelled reload/retry and paused acquisition. Source protection
  held. Four sandbox-blocked IPC checks passed on unprivileged external rerun.
- Fresh measurement acquisition probe passed 96 SQL/cache comparisons across
  scalar/array expansion, incremental mixed records, planned-shape overflow,
  duplicate coordinates and axis reversal. No additional actionable finding.

#### Frozen-source integration at `bc1f4fd`

- Rebuilt application artifacts: 396 sdist files including all 232 test modules;
  wheel has 130 files and all 124 runtime files match committed source bytes.
  Native wheel/sdist inventory and pinned metadata pass; native source remains
  unchanged, using this campaign's previously rebuilt universal2 binary.
- All four artifacts pass twine. Exact source and SHA-256 receipt:
  `/private/tmp/qplot-bh20261019-bc1f4fd-artifact-manifest.json`.
- Fresh compiler-free wheel and editable acceptance both passed: exact installed
  wheel bytes/native identity, console version, actual GUI startup, live Python
  edit for editable mode, installed workflows and operational trusted live
  reader. No compiler build attempted. Logs:
  `/private/tmp/qplot-bh20261019-bc1f4fd-wheel-install.log` and
  `/private/tmp/qplot-bh20261019-bc1f4fd-editable-install.log`.
- Fresh round 8 was complete and clean. The candidate final full suite is being
  gracefully interrupted after round 9 confirmed C19-026; no complete result
  is claimed. Final Ruff/mypy (88 files), dependency check and project-venv
  startup passed: `/private/tmp/qplot-bh20261019-final-startup-fy7w3ik2`.

#### Round 8 — first complete clean round

- Three fresh independent subchats and coordinator integration review found
  no new actionable bug; no source or test edits. Clean-round count is one.
- Safety: 86 focused checks; two real WAL sources, eight newly created scalar/
  array runs, six simultaneous helpers plus eight restarts, eight snapshots
  and 36 protected-file checks. Eight cancellations and eight deadlines released
  intentionally blocked checkpoints (maximum combined release 0.404 s), ten
  writer checkpoints succeeded, and generator hardlink/sidecar/cancel guards
  passed. Native forbidden-operation/proof-handle counters ended at zero; all
  helpers/threads/endpoints were reaped. Pins, slices and linkage remain valid.
- Measurement: 182 focused checks. On each of ARM and Intel, 12 actual runs
  passed 144 grid-route comparisons, 48 protected-file checks, 240 exact
  derivatives, 480 native derivative/mean comparisons, 240 statistics oracles
  and 1,926 CSV cells. Intel additionally passed 120 independent interpolation/
  DFT comparisons (max normalized error 3.93e-16) and 240 fill/row/column means.
- UI: 10 fresh composed workflows plus 208 focused checks passed, with 33
  expected Windows-specific skips. Covered distinct databases sharing GUIDs,
  repeated promotions with independent old/new cuts, CSV, editor/pending-drop
  closure during reload, reopen, real WAL auto-plot and replay suppression.
  All protected-file checks and cleanup passed; sandbox IPC cases passed on
  unprivileged external rerun.

#### C19-026 — Extended numeric arrays lose trusted previews

- Actual Intel current-QCoDeS runs storing ordinary `[0,1,2]` values as
  longdouble in each possible 1D/2D coordinate/signal column return unsupported
  preview status; identical float64 controls return ok. Five actual runs prove
  the failure, without extreme magnitudes or unrepresentable drawing coordinates.
  Protected main/WAL/journal hashes/mtimes hold and resources close normally.
- Array scalar `.item()` retains np.longdouble, which `_finite_number` rejects
  because it accepts only Python int/float. Measurement reviewer owns the two
  trusted rendering modules and `test_campaign_extended_float_previews.py` for
  a bounded precision-preserving repair; separate safety reviewer will approve.
- Round 9 is not clean; the streak resets to zero. Candidate full suite stopped
  for the newly required source repair: 2,268 passed, 34 skipped, no failures,
  in 255.61 s. Repeat fresh complete rounds afterward.
- Final repair preserves wider finite ratios internally and uses the normal
  float path for exact binary64 round trips. Independent review identified
  possible memory amplification for full-size extended-subnormal arrays;
  a 64 MiB retained-ratio budget and exhaustion/cancellation regressions now
  pass. Renderer version v8 invalidates stale unsupported previews.
- Independent safety reviewer approved the final repair. All 32 new regressions
  pass; author Intel 105 passed, ARM 86 passed/19 expected extended-only skips,
  plus the initially excluded IPC converter check passed separately outside the
  sandbox. Actual project-venv startup passed (visible/event loop/exit 0).
- Independent Intel batch: 130 passed. Seventy-two exact affine/duplicate-mean
  oracles passed across 1D/2D, mixed integer/extended coordinates and exponents
  -16440 through +16000, with unchanged source arrays and primitive-only output
  payloads. Maximum subnormal fixture stops safely at the storage budget in
  0.394 s with 56,864,768-byte peak RSS increase; cancellation takes 0.024 s.
  Ruff, configured mypy (88 source files) and whitespace checks pass.
- Both subchats were interrupted by a usage limit. The user explicitly asked
  to continue; author and reviewer resumed with existing file ownership and
  checkpoints. No unfinished review was counted as approved or clean.

#### Round 9 completion and CI follow-up

- C19-026 is this round's sole application finding. All 25 confirmed findings
  are repaired and independently approved. Round 9 is not clean; streak zero.
- Fresh safety review: 38 focused tests and two real QCoDeS sources with 12
  acquisition/SAVEPOINT/schema transactions, six helpers, 48 broker requests,
  12 each of deadlines/interrupts/snapshots, 72 mutation refusals, 12 writer
  checkpoints, 40 protected-file comparisons, 10 export and eight auxiliary
  refusals. Native forbidden counters zero, proof handles balanced, all reaped.
- Fresh measurement probe on both architectures: eight real runs, 176 raw/
  cache/SQL grid comparisons, 368 raw-export samples, 32 row/column means,
  32 protected-file checks, 150 Fraction stencils, 300 native derivative/mean
  comparisons, 150 statistics oracles and 1,036 CSV cells per architecture.
- Fresh UI review: six real Qt compositions passed, including queued swaps,
  native operations, preferences, cached export, promotion during loading,
  actual incompatible-drop modal cancellation/closure, redraw/removal and
  clipboard. Source protection and cleanup held; no additional finding.
- CI at `bc1f4fd`: 40 checks succeeded; six jobs plus Required checks failed.
  Python 3.11 uses QCoDeS 0.58, whose absent split-storage configuration breaks
  new test setup before application invocation. Windows manual-refresh test
  captures missing `_heatmap_geometry` callbacks on an incomplete `_LayerHost`
  test fixture. One Intel shutdown assertion overshot its 0.2 s allowance by
  0.029 s. All three causes are now corrected in tests and independently
  reviewed, with no application change or relaxed application deadline.
- QCoDeS capability setup: generator guards remain tested on both versions by
  setting the intended configuration dictionary directly. Only real storage
  fixtures skip when package defaults prove the backend absent. Author and
  independent UI reviewer each passed all 14 checks on 0.61, and 11 with three
  feature-unavailable skips on isolated 0.58. Main venv remains on 0.61.
- Windows callback contamination: a real paint reproduced the incomplete unit
  host's missing geometry protocol before repair. The fixture now implements
  its honest absent primary geometry, registers owned secondary layers and
  disconnects/destroys/drains them before another test. Strict exception capture
  remains. Author combined batch 58 passed; independent ordered layer/manual-
  refresh batch 44 passed, including a new actual-paint/cleanup regression.
- Shutdown timing oracle: first successful native group/Job termination is now
  timestamped in the launcher; parent pipe draining/reaping/scheduling is not
  mistaken for kill time. The original -0.04/+0.20 s deadline bound remains,
  and GUI/launcher disappearance is checked. Real delayed-parent control showed
  kill at deadline+0.00085 s while the old assertion observed +0.421 s and
  failed. Author and independent reviewer each passed five Intel process cases.
  Independent deliberately late kill at +0.257 s still fails the same bound.
- Rebuilt application wheel at `179c99b` matches all 124 committed runtime
  files. Compiler-free wheel installation, GUI startup and trusted live reader
  pass: `/private/tmp/qplot-bh20261019-179c99b-wheel-install.log`.

#### Candidate artifacts and fresh round 10

- Pushed all three independently approved CI test repairs at `a7036eb` and
  updated draft PR #90. Exact-revision CI run 39312252542 is in progress;
  no completed passing result is claimed yet.
- Current application sdist contains 397 files and all 233 test modules;
  all 394 tracked archive files match committed bytes. The wheel has 130
  files and all 124 runtime files match both current Git source and the
  compiler-free accepted `179c99b` wheel. Both native artifacts remain the
  previously rebuilt, validated 1.0.1 artifacts. All four pass twine.
- Receipts: `/private/tmp/qplot-bh20261019-a7036eb-artifact-manifest.json`
  and `/private/tmp/qplot-bh20261019-a7036eb-sdist-source-proof.json`.
- Compiler-free editable acceptance passed, including actual GUI startup,
  native identity, live reader and edit-without-reinstall checks. No compiler
  builds attempted: `/private/tmp/qplot-bh20261019-a7036eb-editable-install.log`.
- Fresh safety review is complete and clean: 65 focused checks, four actual
  runs across two URI-special sources, ten acquisition/metadata/SAVEPOINT and
  WAL-truncation cycles, 80 mutation refusals, ten each deadlines/cancellations/
  result limits, 20 private snapshots, six helpers and six brokers with 72
  selected-run requests, 24 protected-file comparisons and 14 native audits.
  All helper/thread/endpoint resources reaped; forbidden counters zero.
- Fresh UI review completed with 119 focused checks and six actual Qt/QCoDeS
  compositions: all line/heatmap combinations, mutual overlays, retained-source
  closure, paused acquisition, preferences, swaps/native operations, eight CSV
  exports, clipboard, promotion, both cut orientations, cancelled reload and
  close/reopen. Protected-file comparisons and full resource cleanup passed.
- Fresh measurement probes on each of ARM and Intel: eight real runs, 208
  cache/SQL grid routes, 496 raw samples, eight previews, 40 protected checks,
  160 derivative oracles, 320 native-transform comparisons, 320 statistics
  checks, 160 row means and 876 CSV cells. Author found C19-027; independent
  repair review subsequently confirmed C19-028.
- Coordinator revisited package inventories/provenance, entry points, release
  verification boundaries and the repaired profiling scripts. No additional
  actionable issue; no release or publication action performed.

#### C19-027 — Inferred dependent channels fail to load

- Four actual public QCoDeS DataSaver/Qt reproductions cover numeric/array
  records and line/heatmap plots. Register `converted` with the same setpoints
  as `signal` and `basis=('signal',)`. Opening the run automatically creates
  both plots: signal is correct, converted stays blank and reports
  `KeyError: 'converted'`. Raw export independently reads its exact values.
- QCoDeS cache preparation deliberately creates top-level trees only; qPlot
  offers every parameter with setpoints but indexes a missing inferred tree.
  Source hashes/mtimes and normal cleanup hold in all four reproductions.
- Measurement subchat owns the narrow viewer-cache repair and regression file;
  a different safety subchat independently reviews concurrency, correctness
  and database protection. No global QCoDeS changes or source writes required.
- Eight regression GUI cases failed before repair, covering numeric/array,
  line/heatmap and shaped/unshaped data with two inferred siblings. The repair
  creates only a private empty tree snapshot; existing successful publication
  commits it. Cancelled initial reads leave shared cache and statuses untouched.
- Author validation: 64 focused tests passed, including all 18 new regressions;
  isolated QCoDeS 0.58 also passes all 18. Actual project-venv startup passes
  (visible window/event loop/exit 0). Ruff, configured mypy (88 files) and
  whitespace checks pass.
- Independent safety reviewer approved the final cache-only repair: 43 focused
  checks passed. Ten real runs across snapshot/trusted routes passed 20 load
  orders, 104 worker loads, 168 raw-SQL column comparisons, 640 concurrent
  private initial snapshots, 20 cancelled reads without cache mutation, 20 stale
  publication refusals and 20 protected-file checks. Baseline KeyError was
  independently reproduced in every case. No new SQLite/filesystem operations.

#### C19-028 — Missing basis cells break inferred array plots and raw export

- Independent reviewer confirmed with public QCoDeS DataSaver and actual Qt:
  array `signal` and `other` depend on `x`, while `converted` has the same
  setpoints and `basis=('signal', 'other')`. QCoDeS stores two rows, each with
  one basis channel NULL and converted values `[101,102]` present in both.
- Raw export already fails before C19-027 with `AttributeError` on `None.size`
  in QCoDeS array expansion. After the cache repair, plotting reaches and fails
  at that same decoder step. Both basis plots themselves work. Expected raw
  converted samples are exactly `[101,102,101,102]`, matching stored records.
- Both baseline and repaired-cache GUI reproductions keep protected-file
  hashes/mtimes unchanged and shut down cleanly. Reproduction:
  `/private/tmp/qplot-c28-minimal.py`.
- Measurement author owns the separate narrow `parameter_data.py` repair and
  regression file; safety reviewer independently checks missing-cell meaning,
  precision, row alignment, cancellation and database protection.
- Fourteen GUI/mixed-storage cases failed before repair. Present fields retain
  QCoDeS expansion semantics; absent ancillary fields return in original order
  and output shape, including empty acquisitions. Missing text remains None
  (blank CSV), numeric/array cells remain NaN and complex cells retain complex
  missingness. Scalar broadcast views avoid allocating transient full arrays.
- Functional validation: 138 ARM checks, all 33 new cases on Intel and all 33
  on isolated QCoDeS 0.58 pass. Actual project-venv startup, Ruff/scoped mypy and
  whitespace pass. Independent ARM/Intel oracles each passed four actual runs,
  848 exact stored-SQL value comparisons, 20 cancellations and eight protected
  file checks. Source values, row duplication and NULL-output exclusion hold.
- Before approval, independent review confirmed expanded-cache amplification:
  16 actual array bases x 8,192 samples produce a 3,188,736-byte prefix estimate
  but 18,874,368 retained bytes. A safely reduced 8,388,608-byte worker limit
  still accepts that cache; measured peak is 22,173,716 bytes. Reproduction:
  `/private/tmp/qplot-c28-budget.py`. No giant allocation attempted.
- Author additionally owns `LoadFromDB.py` and `tools/worker.py` to propagate
  the existing decoding budget and reject projected expanded storage before
  column allocation, including incremental and planned extents. Measurements
  are not trimmed. Final guard accounts for numeric float64, extended types,
  object/text storage, empty acquired dtype promotion, existing cache extent
  and planned padding, with cancellable projection before column construction.
- Independent reviewer approved the final application source and all 39 new
  regressions. Final trusted probe refuses at 8 MiB before column allocation
  with unchanged cache/status, and succeeds at 24 MiB with the exact 18 MiB
  cache. Incremental, planned/overflow, object-text, empty-dtype, cancellation
  and retry controls pass. Final ARM/Intel SQL oracles remain exact; all
  owned helpers and processes reaped.
- Author focused validation: 171 passed (170 plus one final durable cached-
  text/cancellation case). Root Ruff, configured mypy (88 files) and whitespace
  checks pass. Full-decoder median timings for three scalar columns:
  100k rows 0.2085 s without guard / 0.2900 s with it; 200k 0.4356 / 0.6331 s.
  Array decoding is unchanged within timing noise (0.0147 / 0.0134 s).
- Final isolated QCoDeS 0.58 run passes all 39 cases. Actual project-venv
  module startup has visible window, active event loop and normal exit 0:
  `/private/tmp/qplot-c28-final-startup-DIHLfb/startup.json`. No owned processes
  remain, and source protection/cleanup assertions pass.
- Round 10 is not clean; clean streak zero.

#### Additional CI fixture follow-up

- At `a7036eb`, Windows 3.13 compatibility reports one test failure after
  640 passes: its shutdown receipt parser cannot find `TREE_TERMINATED` at
  the start of a line. The actual receipt contains valid termination times,
  appended to the child's last partial stdout message after forced exit.
  No late application termination is shown by this failure.
- UI subchat owns a narrow test-harness correction to frame the post-reap
  receipt on its own line, with a deterministic partial-child-output regression.
  Existing deadline and process-disappearance assertions must remain intact.
  No application change proposed. Evidence:
  `/private/tmp/qplot-ci-118265310237-failure.txt`.
- Deterministic real-process regression failed before repair, despite actual
  termination at deadline+0.000325 s. Final repair adds a leading newline to
  the launcher's post-reap receipt; strict parsing and the original deadline,
  exit-status and both process-disappearance checks remain unchanged.
- Author validation: all 17 bounded-shutdown tests pass on ARM, six actual
  process cases pass on Intel. Independent safety reviewer approved and passed
  five selected real-process cases, including both delayed-parent controls
  and the unterminated-child-output regression. All resources reaped.
- Windows 3.14 full partition independently hit the same partial-line defect
  after 2,599 passing tests and 119 skips; no distinct cause. Reviewed fixture
  repair committed at `4fea3dd` and pushed with C19-027 for renewed draft CI.
  Final source CI remains required after C19-028; old passing jobs are not
  claimed as final validation.

#### Frozen-source candidate `7326849`

- All 27 application repairs and four CI test follow-ups are independently
  reviewed, committed and pushed to draft PR #90. Three fresh subchats have
  begun round 11; current clean-round count remains zero until it completes.
- Rebuilt application wheel/sdist: 130 wheel files including 124 runtime files,
  399 sdist files including all 235 test modules. All tracked archive and runtime
  bytes match the exact committed revision. Native artifacts retain the verified
  rebuilt 1.0.1 binary/source; no native source changes. All four pass twine.
  Receipt: `/private/tmp/qplot-bh20261019-7326849-artifact-manifest.json`.
- Fresh compiler-free wheel and editable install acceptance both pass against
  these artifacts, including exact installed bytes/native identity, actual GUI
  startup, trusted reader and editable live-edit checks. No compiler builds
  attempted. Logs: `/private/tmp/qplot-bh20261019-7326849-wheel-install.log`
  and `/private/tmp/qplot-bh20261019-7326849-editable-install.log`.
  Final full suite and exact-head CI are still pending.

#### Round 11 — scalar expansion finding

- Fresh safety remit is clean: 64 focused checks, four actual QCoDeS runs,
  14 acquisition/metadata/SAVEPOINT cycles, 24 exact array BLOB boundaries,
  80 mutation refusals, eight each deadlines/cancellations/scalar cap refusals,
  16 snapshots, eight helpers, four brokers with 48 selected-run subscribers,
  four private prefixes, 12 writer checkpoints and 18 protected-file comparisons.
  Ten native audits have zero forbidden operations and balanced proof handles;
  auxiliary/export/live-generator refusals and all resource cleanup pass.
- Fresh UI review is clean: 130 focused checks and six actual WAL/inferred-
  array compositions across line/heatmap, shaped/unshaped and new auto-plot
  acquisition. Retained sources, cancellation/retry, clipping/native mean,
  swaps, promotion/cuts, preferences/statistics, clipboard, eight CSV exports,
  four reopens and two cancelled reloads pass. All 21 protected-file comparisons
  and owned-resource cleanup pass.
- Fresh measurement format probe passed 24 actual runs, 48 grids/416 cells,
  24 raw-tree comparisons and 96 incremental cache steps. Package inventories
  and both installation modes pass. C19-029 is the new actionable finding.

#### C19-029 — Scalar array expansion allocates before the budget check

- Actual trusted worker reproduction uses two valid public QCoDeS DataSet rows
  with 8,192-element x/converted arrays, one 1,024-character text basis and
  missing sibling basis cells. Trusted prefix estimate is 333,440 bytes.
- With the existing cap reduced to 8 MiB for a bounded test, QCoDeS expands
  the text into a 33,554,432-byte column before the materialization guard runs.
  Traced peak reaches 34,287,311 bytes before the correct budget refusal.
  No final column assembler runs, and shared cache identities/status remain
  unchanged. Protected source and all service cleanup checks pass.
- Reproduction: `/private/tmp/qplot-r11-trusted-expansion.py`. No giant
  allocation was attempted. This confirms the earlier scalar expansion must
  also be preflighted, not merely final column/cache construction.
- Repair projects QCoDeS scalar/singleton expansion using allocation-free
  views, then applies the existing cumulative/cache/planned budget before any
  expansion. Missing/empty records and every acquired sample retain their
  existing semantics. No cap changes or additional database operations.
- Different safety subchat independently approved the final source and tests:
  85 related tests and all 20 final durable regression cases pass. ARM and Intel
  each pass 1,200 exact QCoDeS shape/dtype/value comparisons and 9,600 matching
  cumulative/planned budget decisions. Review caught and resolved a singleton
  rank regression before commit; tests cover ranks zero through three on both
  input and output sides, plus cancellation before expansion and exact retry.
- Independent trusted-worker reproduction now refuses before every expansion
  call; traced peak falls from 34,287,366 to 741,190 bytes. Shared cache state,
  source hashes/mtime and service cleanup remain intact.
- Separate UI integration passes actual modal refusal, three timer retries
  without repeated dialogs, pause/cancellation, full 16,384-sample retry at
  96 MiB, close during reload and reopen. Eight worker starts, no callback
  errors, protected files unchanged and all resources released.
  Log: `/private/tmp/qplot-c29-ui-integration.log`.
- Coordinator Ruff, configured mypy (88 files), whitespace and actual project
  module startup pass: visible window, running event loop, normal exit zero.
  Startup receipt: `/private/tmp/qplot-c29-final-startup-xratvw2y/startup.json`.
- Author focused validation: 102 passed, including the 20 durable cases. The
  trusted-worker regression fails against the pre-repair decoder loaded only
  in memory: two expansions precede refusal. Both incremental cases also fail
  before repair. Author startup separately passes with normal Qt teardown.
- Final runtime-neutral local tuple/list typing correction was separately
  reviewed; all 20 cases rerun successfully, targeted decoder mypy and Ruff
  pass. Final approved decoder SHA-256:
  `b6ce21246929fe8c8a1055bb774b334d710a8ecc46d96457aed94324c370970d`.
- Round 11 is not clean. Clean-round streak remains zero.

#### Round 12 — singleton-array preview finding

- Three fresh independent subchats review frozen application commit `f899ecf`:
  safety/native/live, measurement/operations/preview, and actual UI lifecycle.
  Coordinator covers configuration, entry points, scripts and packaging.
- Fresh safety composition passes three public-QCoDeS runs and nine acquisition/
  checkpoint cycles: 54 newly opened source/proof handles physically read-only,
  24 native audits with no forbidden operation and balanced final proofs,
  45 mutation refusals, nine deadlines, 18 cancellations, 36 detail reads,
  54 exact raw prefix rows, nine disk-cache reconfiguration cycles, 36 auxiliary
  refusals, 15 snapshots and nine unmarked-live-WAL refusals. Thirty protected
  file comparisons and all three helper/thread/prefix cleanups pass.
- Safety focused checks: 43 passed across operation cleanup, subscriber/cache
  ownership, deadlines, helper failures, reconciliation and stale completions.
  Fresh source audit confirms pinned native ABI, read-only main/WAL opens,
  rejection of journal recovery/write/truncate/sync/delete, and exact SHM scope.
- Fresh UI remit is clean: seven composed real-QCoDeS/Qt scenarios and 134
  focused tests plus three subtests pass. Actual drag/drop, preferences,
  promotion with exporter open, cuts, auto-plot, cancelled database switch,
  retained old-source refresh, inline operation recovery, raw CSV cancellation,
  metadata Copy/Close and shortcuts work. Close during workers/modeless editors
  reaches shutdown-ready with no handles/windows/workers or callback errors;
  protected files remain unchanged. Probe: `/private/tmp/test_qplot_round12_ui.py`.
- Coordinator's fresh CLI/settings composition passes: eight persisted current
  settings roundtrips, nine atomic invalid-value refusals and three malformed
  settings recoveries; generated three tiny runs at a Unicode/URI-special path,
  refused unconfirmed replacement, ran all three numeric profilers and actual
  plot launch via GUID, double-click and Run actions. Expected sample counts,
  generator's 5-by-3 sweep order, painting and seven protected-file comparisons
  pass. Actual plot command runtimes were 1.94–2.20 seconds locally.
  Receipt: `/private/tmp/qplot-r12-coordinator-frty0ymx/receipt.json`.
  Initial probe used the wrong generator axis order; no application issue.

#### C19-030 — Singleton/empty array records disagree across viewing routes

- Public QCoDeS DataSaver records an array-typed x
  setpoint supplied as scalar 2.0 alongside signal `[0., 1., 2.]`. QCoDeS and
  qPlot raw decoding correctly expand x to `[2., 2., 2.]`; trusted preview
  instead reports "Array measurement and setpoint sizes differ." Its strict
  BLOB-size equality rejects valid singleton broadcasting. Protected source
  files remain unchanged. Measurement subchat authored the repair; separate
  safety subchat independently reviewed semantics, bounds and cancellation.
- Same semantic issue also affects the full heatmap preflight: public DataSet
  records x `[0,1,2]`, slow `[0]`, signal `[2]`; QCoDeS correctly expands three
  aligned samples, but `_array_heatmap_records` hard-codes output array length
  and refuses.
- Independent review confirms the empty-record boundary is also publicly
  reachable: DataSaver writes array-typed x supplied as scalar 2.0 and empty
  signal, then scalar x 3.0 with signal `[4,5,6]`. Existing unshaped worker
  correctly skips the empty acquisition; declared-shape worker and raw CSV
  both refuse its zero/one sample-size mismatch. C19-030 repair scope therefore
  includes exact `flatten_record_columns` and `_csv_scalar_columns` paths.
  No invented samples or omission of nonempty measurements is acceptable.
- Separate actual-Qt C19-030 integration passes six WAL scenarios across
  singleton ranks zero/one/three and planned/unplanned grids: thumbnails,
  details previews, thumbnail double-click, full plots, writer append, both
  refresh controls and close/reopen. No callback errors; protected files and
  every retained service cleanup pass. These checks precede the additional
  empty-record repair. Probe: `/private/tmp/test_qplot_c19030_ui.py`.
- Independent cache check passes: retained v8 unsupported payload misses under
  v9, and both exact keys coexist without touching the measurement source.
- Fix broadcasts singleton fields by indexing existing samples or existing
  bounded chunks. Empty selected outputs contribute zero samples; incompatible
  nonempty records still refuse. Shaped-cache flattening also handles dense
  zero/one shape mismatches, preserving acquired-row versus sample offsets.
  CSV uses explicit output identity, and empty slices retain dtype promotion.
  Renderer version v9 invalidates old negative previews. No cap increases,
  added source I/O, settings migrations or unrelated refactoring.
- Final independent review approved: 99 tests pass, including all 68 new
  regressions, the 20 C19-029 budget cases and existing array-preview cases.
  Separate oracle matrix passes 28 actual QCoDeS previews and 28 bounded
  workers across singleton positions/ranks, C/Fortran layout and numeric
  outputs. Four additional real empty-record workflows pass all routes.
- Incremental regression proves empty-only acquisition advances read count to
  one but leaves written samples zero; cancellation leaves cache unchanged,
  valid append reaches read two/write three, and stale empty-prefix publication
  is rejected. Selected-output authority, exact uint64, cumulative caps,
  cancellation/retry and protected source/cleanup checks pass.
- Empty-record actual-Qt extension passes both shaped/unshaped cases: normal
  Plot/refresh and raw CSV preserve exactly x `[3,3,3]`, signal `[4,5,6]`,
  including after close/reopen. Thumbnails/details and cleanup pass.
  Probe: `/private/tmp/test_qplot_c19030_empty_ui.py`.
- Coordinator Ruff, configured mypy (88 files), whitespace and actual project
  startup pass: visible window, event loop running, exit zero. Receipt:
  `/private/tmp/qplot-c30-final-startup-it4ogmwx/startup.json`.
- Author's final ARM batch: 247 passed across the 68 new cases, inferred/missing
  cells, expansion budgets, variable records, integer caches, full/bounded
  heatmaps and previews. Failed-before evidence includes four singleton viewing
  routes, two empty raw/shaped routes, and actual incremental read-one/write-one
  corruption followed by four coordinates against three measurements. Earlier
  Intel numeric/preview batch: 192 passed, one deselected; final Intel follow-up
  after empty alignment passes 16 selected cases (97 outside that scope).
- Measurement remit completed with no further finding. Fresh independent
  numerical oracles pass 720 comparisons on each ARM/Intel architecture:
  Fraction derivatives/secants, native/statistical/axis means, bounded fill
  and independently interpolated DFTs. Unsupported oversized operation paths
  were verified to refuse before the hypothesized narrowing, so no speculative
  repair was made. All sources/services/processes cleaned up.
- Round 12 is not clean; repair and independent review are complete. Begin
  two new complete fresh rounds; current clean-round streak remains zero.

#### Round 13 — final review at user's request

- All 29 application repairs are committed and pushed at `769e9b1`; fresh
  safety, measurement and UI subchats have begun review with separate contexts.
  Coordinator covers remaining entry points, settings, scripts and packaging.
  Count remains zero until a complete round has no actionable findings.
- Fresh coordinator entry-point composition passes two real public `qplot.run()`
  launches from one embedding caller: each GUI becomes visible, runs its event
  loop and returns zero; caller PID/process group and continuation are preserved.
  Separate explicit `return_objects=True` launch returns owned Qt objects after
  quiescent shutdown; caller deletes both safely and continues. Private settings
  and logs only. Receipts: `/private/tmp/qplot-r13-entrypoints-v_8myciy/api.json`
  and `/private/tmp/qplot-r13-caller-owned-h413gqm4/receipt.json`.
- Coordinator actual 100,000-row/three-column decoder check preserves all
  300,000 values and protected files. Decoding took 0.211–0.219 s without the
  budget projection and 0.455–0.473 s with it, retaining 2.4 MB of array data.
  Receipt: `/private/tmp/qplot-r13-decoder-e43l86tz/receipt.json`.
- Safety remit clean: 84 focused tests pass; fresh public-QCoDeS compositions
  verify 36 physical read-only handles, nine native audits, 45 mutation refusals,
  21 writer checkpoints, 180 exact prefix rows, repeatable concurrent reads,
  source replacement rejection, snapshots, deadlines/cancellation and cache/
  auxiliary boundaries. Forty-two protected-file comparisons pass; all owned
  helpers, threads, writers and temporary resources cleaned up.
- UI remit clean: 191 focused tests and ten fresh real-Qt compositions pass,
  covering overlay/promotion/cut ownership, operations/export, database switches,
  paused retained-source refresh, numeric run entry, details, live Auto-plot,
  completion and reopen. No callbacks/errors or source mutation; all resources
  reaped. No further broad review after the user's credit-limit instruction.

#### C19-031 — Empty unshaped acquisitions corrupt incremental alignment

- Actual public-DataSaver/Qt reproduction fails in two cases: opening an
  unshaped empty line reports a setpoint/measurement mismatch; opening an empty
  heatmap then appending a singleton output reports incompatible broadcast
  shapes and remains blank. Scalar inputs to array-typed coordinates retain
  one coordinate despite zero acquired measurements, before unshaped cache
  merging. Protected source files remain unchanged.
- Repair is eight added lines in `LoadFromDB.py`: before cache merge, validate
  and flatten newly decoded zero-output trees so singleton setpoints contribute
  zero acquired samples. Read status still advances by SQL rows; empty dtype
  contributions remain intact. No worker, transaction, native or source-I/O
  changes. Nine meaningful regressions cover initial/middle empties, subsequent
  singleton/larger appends, planned/unplanned line/heatmap, exact uint64,
  cancellation/retry and stale publication. Two fail against original source.
- Author's nine new cases and all 68 C19-030 cases pass. Different safety
  subchat independently approved with 13 focused passing checks. Separate UI
  subchat passes two actual-WAL flows through Plot, refresh, exact plotted CSV,
  close/reopen, preserving `[4,6,7]` and matching coordinates. Protected source
  and all resource-cleanup checks pass. No further investigation remains.

#### Release preparation — 1.6.2

- User initially requested the next minor release, then explicitly corrected
  this to the next bugfix release (1.6.2). Application
  version, changelog, README and distribution instructions now prepare 1.6.2;
  separate UI subchat independently approved these four release files.
- Native source and dependency remain 1.0.1. Public PyPI metadata verifies all
  four native platform wheels exist and are not yanked; 1.6.2 uses the
  application-only release workflow after review/merge and explicit tagging.
  No merge, tag, publishing or release creation was performed.
- Project editable metadata refreshed to 1.6.2. Ruff, configured mypy (88
  files), dependency consistency and whitespace checks pass. Actual project
  startup passes with a visible 1.6.2 window, running Qt event loop and exit
  zero. Both author and independent release reviewer are finished.

#### Final validation

- Final application/test revision: `3b909f4b9a4868fab712fe89e0a21ac52bcaa1c8`.
  Complete local suite: **5,485 passed, 101 skipped**, 56 warnings, 915.18 s.
  Command uses project venv, fixed Matplotlib cache, offscreen Qt and
  `-m pytest --no-cov -n 2 --dist=loadfile --no-loadscope-reorder`.
  Log: `/private/tmp/qplot-bh20261019-3b909f4-full.log`. Hosted CI retains
  coverage and platform-specific checks. The final log-only commit does not
  alter this tested application, test, packaging or release source.
- Ruff, configured mypy (88 files), `pip check` and whitespace checks pass.
  Actual project module startup confirms visible version 1.6.2, running Qt
  event loop and exit zero. Receipt:
  `/private/tmp/qplot-162-final-startup-wgxf_70d/startup.json`.
- Rebuilt 1.6.2 application wheel/sdist pass inventory and exact committed-byte
  comparisons: 130 wheel files/124 runtime files, 402 sdist files including all
  238 source test modules. Verified unchanged native 1.0.1 artifacts contain
  seven wheel files/two runtime files and 11 sdist files; no native source
  change required another rebuild. All four artifacts pass twine checks.
  Manifest: `/private/tmp/qplot-bh20261019-3b909f4-artifact-manifest.json`.
- Fresh compiler-free wheel and editable installations both pass: installed
  bytes/native identity, GUI startup, trusted live reader, console commands,
  and editable edits without reinstalling. No compiler builds attempted.
  Logs: `/private/tmp/qplot-bh20261019-3b909f4-wheel-install.log` and
  `/private/tmp/qplot-bh20261019-3b909f4-editable-install.log`.
- Candidate hosted CI observation: 31 successful checks, 11 running and four
  queued, no failures; all returned checks verified against exact `3b909f4`.
  Final branch-head CI remains pending after the log update. Passing local
  checks are not represented as completed hosted platform acceptance.

### Round-13 checkpoint (superseded by the authorized resumption above)

All **30 confirmed application fixes** and four test-only CI follow-ups are
committed and independently approved; C19-014 was withdrawn. Investigation ends
after round 13 at the user's explicit credit-limit instruction. The original
two-clean-round condition was superseded and was not achieved (streak zero).
Final local whole-suite, static, startup, artifact and installation validation
pass. Application 1.6.2 bugfix release is prepared on
`codex/qplot-bug-hunt-2026-10-19` in [draft PR #90](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/90).
No uncommitted repair or further investigation round remains. Hosted CI and
maintainer review are the remaining merge/release gates; no merge, tag or
publication was performed. Resume only if requested or to address a concrete
subsequent CI/review failure.

### Further rounds authorized 2026-10-20

#### Round 14 — complete; two confirmed repairs and one CI fixture correction

- Fresh coordinator review covers configuration/console settings, synthetic
  generation, cancellation/publication, metadata, package boundaries and
  auxiliary-file protection. **74 focused tests passed**.
- Independent probe exercises every scalar settings category through the
  console command: **122 successful persistence round-trips, 144 invalid
  updates rejected atomically**. Eight actual QCoDeS synthetic runs cover
  line/heatmap, reversed sweeps, subnormal and overflowing finite spans.
  Five cancellation points leave the existing selected output byte-identical
  with unchanged inode/size/mtime and no generator temporary artifacts.
  Receipt: `/private/tmp/qplot-r14-coordinator-1dxi0a28/receipt.json`.
- Safety subchat identified and repaired a CI fixture error: the inferred live-refresh test
  writes in DELETE mode while asynchronous reader jobs are active. A held-writer
  reproduction confirms the intentional `journal/journal_appeared` refusal;
  WAL allows the same bounded reads and exposes the subsequent commit without
  qPlot modifying protected files. Live cases now use WAL and obtain ordered
  plot metadata through the already accepted broker; static DELETE coverage is
  retained. Measurement subchat independently approved; **18 tests passed**.
  Committed as `564be1b`.
- Fresh safety composition passes four held/uncommitted append cycles, four
  deadlines and four cancellations (40–51 ms), eight complete writer RESTART
  checkpoints, 20 mutation refusals and 17 protected-file comparisons. Native
  audits show no forbidden operations; all readers/writers were closed/reaped.
  Receipt: `/private/tmp/qplot-round14-safety-compose.json`.
- Measurement subchat confirmed and repaired C19-032 below. Round 14 is not
  clean; the consecutive clean-round count remains **zero**.
- Four fresh UI compositions pass: shared-dataset overlays, closing one host,
  retained-source promotion while paused, manual refresh, CSV export, heatmap
  cuts and complete cleanup. Probe: `/private/tmp/test_qplot_round14_composition.py`.
  Actual project module startup passes with visible window, running event loop
  and exit zero. No protected database changes.

#### C19-032 — Mixed extended-float records silently lose finite heatmap samples

- Actual current-QCoDeS mixed longdouble/integer array records turn a finite
  `1e310` heatmap row into NaNs on extended-range platforms. Pure-longdouble
  controls retain it, and subtracting its row mean yields representable exact
  residuals. Two real worker regressions failed before the repair.
- `numeric_isfinite` previously narrowed object cells to float64. Keep its
  vectorized fast path, but recheck original cells whose converted values are
  infinite; exact conversion overflow also uses original scalar identities.
  No recorded data or database access changes. Unsupported nonnumeric/complex
  objects remain refused, and the bounded float64-mean route still rejects
  unrepresentable results rather than inventing samples.
- **18 new Intel tests pass**, including planned/unplanned scans, positive and
  negative extended values, real GUI row-mean recovery, scalar/empty shapes,
  exact-number classes and invalid values. Related validation: **69 passed,
  33 expected skips** on ARM; **151 passed** on Intel. Ruff and configured mypy
  (88 files) pass.
- Safety subchat independently approved, ran all 18 Intel regressions and an
  88-case classification oracle on each architecture. Review caught an initial
  slow NaN retry loop; final missing-data masks remain vectorized (250,000 NaNs:
  2.45 ms versus 2.42 ms before on ARM; 4.48 versus 4.42 ms on Intel). Ordinary
  250,000-cell mixed masks remain about 3 ms. These are local observations.
- Evidence: `/private/tmp/qplot-r14-finiteness-before.log`,
  `/private/tmp/qplot-r14-finiteness-fixed.log`,
  `tests/test_campaign_mixed_extended_finiteness.py`.

#### C19-033 — Plot benchmark retains native Qt objects through interpreter teardown

- macOS 3.14 CI reported SIGBUS after successful benchmark JSON output. Sixteen
  local repetitions do not reproduce that signal; its cause remains unproven.
  A separate deterministic observation shows completed plots and QApplication
  are still alive when `main()` returns, after MainWindow itself was deleted.
- Repair explicitly destroys the script-owned QApplication after the existing
  bounded shutdown proves background work quiescent and deletes MainWindow,
  matching production entry-point lifetime ordering. This retires closed plots
  while their Python wrappers and interpreter remain alive.
- Three real subprocess regressions retain actual Qt wrappers and fail before
  repair for line, heatmap and invalid-run paths. All **nine profiling tests
  pass** afterward; protected hashes/size/mtime are unchanged.
- Safety subchat independently approved and ran six focused lifecycle cases.
  Committed as `bf1c61a`. Hosted macOS acceptance still needs fresh CI evidence;
  this repair is not represented as a proven explanation of the earlier SIGBUS.

#### Round 15 — complete; one confirmed CSV precision repair

- Fresh safety, measurement and UI subchats review the integrated `add63bb`
  revision. No native source changes; the pinned native reader remains 1.0.1.
- Coordinator rebuilt application wheel/sdist and verified every tracked
  archive/runtime byte against `add63bb5df777ecf2a88490070bf025e4ca47aa3`.
  Inventories: 130 wheel files/124 runtime files; 403 sdist files including all
  239 test modules. Unchanged native wheel/sdist inventories and all four twine
  checks pass. Fresh compiler-free wheel and editable installations both pass
  GUI startup, trusted reading, installed-byte checks and editable behavior.
  Manifest: `/private/tmp/qplot-bh20261019-add63bb-artifact-manifest.json`.
  Logs: `/private/tmp/qplot-bh20261020-add63bb-{wheel,editable}-install.log`.
- Ruff, configured mypy (88 files), dependency consistency and whitespace pass.
  Integration suite at `add63bb` was gracefully stopped after **1,551 passed**
  when C19-034 was confirmed; it is partial, superseded validation, not a final
  full-suite result. Log: `/private/tmp/qplot-bh20261020-add63bb-full.log`.
- Four fresh UI compositions pass: pending and successful database switching
  while mixed plots retain sources, preferences, export/appearance disposal,
  Auto-plot disable/re-enable during selection/refresh, paused manual refresh
  and closed-run replay suppression. Protected database checks and complete
  QObject/worker/service cleanup pass. No additional UI finding.
  Probe: `/private/tmp/test_qplot_round15_ui.py`.
- Safety review is clean: **45 focused tests passed** plus 288 subscriber
  requests, six acquisition cycles, six each deadline/cancellation/result-limit
  refusals, 30 mutation refusals, seven private snapshots and 15 writer
  checkpoints. First WAL arrival works through persistent readers and broker;
  stale instances refuse and replacement reopening succeeds. All 21 protected
  file comparisons and native audits pass; all owned resources are reaped.
  A 2,400-node snapshot traverses 19 bounded pages without loss or duplication;
  343 full-value handles retain exact contents and refuse foreign-source reuse.
  Receipts: `/private/tmp/qplot-round15-safety-receipt.json` and
  `/private/tmp/qplot-round15-boundary-receipt.json`.
- Fresh measurement oracle covers six public-QCoDeS runs with planned/unplanned
  and empty/ragged/descending/duplicate data: 18 raw-column comparisons,
  18 grids through cache/SQL/swapped axes, six preview images and 72 composed
  Fill/Clip/Mean/Derivative cells agree with independent Fraction expectations.
  Protected sources remain unchanged and all temporary resources are closed.
- Measurement subchat confirmed and repaired C19-034 below; round 15 is not
  clean. Consecutive clean-round count remains **zero**.

#### C19-034 — Narrow floating CSV exports change recorded numeric values

- Actual heatmap Export Plot and raw Run CSV actions serialize float16/float32
  values using their short dtype-specific text, but CSV carries no dtype.
  Stored `float16(0.1)` is `0.0999755859375`, exported as `0.1`; the smallest
  float32 subnormal changes from `1.401298464324817e-45` to `1e-45` (about 29%).
  Six actual-dialog/raw-path regressions fail before repair.
- Heatmap CSV now uses the existing precise numeric formatter already used by
  lines. Raw CSV widens only temporary float16/32 pandas Series to float64,
  exactly preserving their values. Recorded/cache dtype and contents remain
  unchanged; integer/object/extended-float export behavior is retained.
- Twelve new cases pass on ARM and Intel, covering planned/unplanned scans,
  CSV/TSV, both real export actions, adjacent values, tiny/subnormal values,
  signed zero and float64/longdouble controls. Ruff, configured mypy (88 files)
  and whitespace checks pass.
- UI subchat independently approved: **24 passed, one expected skip** on ARM
  and **eight independent cases passed** on Intel. Random bit-pattern probes
  include 257 samples per float dtype, NaN/Inf/signed zero/maxima/subnormals,
  exact extended precision and equal/ragged mixtures with uint64 above 2**63.
  Tests verify exact parsed values, source dtype/bytes and protected files.
  Probe: `/private/tmp/test_qplot_c034_independent.py`.
- Author's final related export/complex-refusal batch: **116 passed, one
  expected extended-only skip** on ARM. Actual project module startup after
  integration confirms visible 1.6.2 window, real event loop and exit zero:
  `/private/tmp/qplot-r15-final-startup-8tfchaj_/startup.json`.
  Repair committed as `14b73632f1dffb69c8659f9cad7b8e5bd101ffe2` and pushed.

#### Round 16 — complete and clean (streak 1)

- Fresh independent safety/measurement/UI subchats review `14b7363` without
  sharing the previous reviewers' investigation context; all changes require
  concrete evidence and a different reviewer.
- Coordinator public-entry-point composition passes two actual launches against
  a new Unicode/special-character database path: explicit Path argument and
  command-line parsing with Qt options. Both display the five exact sweep
  coordinates, preserve the host process/group, finish normal shutdown and
  leave protected database inode/hash/size/mtime unchanged.
  Receipt: `/private/tmp/qplot-r16-entrypoints-cxyqwhjj/receipt.json`.
- UI review is clean: **36 checks passed**, including two fresh actual Qt
  compositions with two hosts sharing a processed source, source close during
  refresh, editor/export disposal, reopen, old overlay/CSV preservation, cuts,
  consumer remove/re-add, promotion and paused refresh. Protected artifacts and
  complete QObject/worker/service cleanup pass; no source edits.
  Probe: `/private/tmp/test_qplot_round16_ui.py`.
- Safety review is clean: **10 focused tests passed** plus three concurrent
  WAL/array-prefix compositions (shared cancellation with append/foreground
  metadata, sole cancellation/retry, close during private BLOB staging). Nine
  protected-file comparisons and seven writer checkpoints pass; four new
  descriptors are physically read-only, four mutation refusals/audits pass,
  and every owned helper/service/thread/endpoint/spool is released.
  Receipt: `/private/tmp/qplot-round16-safety-receipt.json`.
- Measurement review is clean on ARM and Intel: six fresh actual QCoDeS runs
  per architecture cover incremental empty/ragged/partial/overrun acquisitions,
  mixed integer/float/complex/Boolean records, caches, previews and exports.
  Per architecture: 72 incremental-column comparisons, 18 grids, six previews,
  36 Fraction-oracle operation cells and 234 CSV cells pass. Targeted tests:
  **52 passed, nine expected skips** on ARM; **30 passed** on Intel. Protected
  files unchanged; all temporary sources/connections cleaned.
  Receipt: `/private/tmp/qplot-r16-measurement-receipt.json`.
- Rebuilt `14b7363` application artifacts match exact committed bytes and pass
  inventories/twine: 130 wheel files/124 runtime files, 404 sdist files/all
  240 test modules. Native artifacts remain unchanged and verified. Full local
  suite is running; fresh compiler-free wheel and editable installations both
  pass for this candidate, including actual GUI/native-reader startup.
  Manifest: `/private/tmp/qplot-bh20261019-14b7363-artifact-manifest.json`.
- All four coverage areas found no new actionable bug. This is the first
  complete clean round of the resumed campaign; one more clean round is needed.

#### Round 17 — complete and clean (streak 2; investigation finished)

- Fresh independent reviewers cover safety, measurement and UI/lifecycle;
  coordinator completes package, startup, static and integration validation.
  All four areas are clean; the stopping condition is met without round 18.
- Coordinator's fresh ordinary-workflow performance composition passes six
  real paints across GUID/Run/double-click launches: 65,537 line samples and a
  257×65 heatmap, each repeated twice. First displays take 1.2–2.9 s locally,
  subsequent displays 0.46–0.84 s while integration tests are also running;
  these are observations, not portable performance promises. Two bounded-read
  configurations each repeat twice across the 65,536-row boundary with equal
  checksums, and every source fingerprint remains unchanged. All processes exit
  normally with no Qt teardown crash.
  Receipt: `/private/tmp/qplot-r17-performance-gtmjsf2o/receipt.json`.
- Safety review is clean: **10 focused tests passed**, plus real WAL metadata
  and snapshot updates at constant row count, cache eviction/restart and
  invalidated pending publication during database selection. Four revisions,
  eight long snapshot values, three foreign-handle refusals, four mutation
  refusals, four writer checkpoints and 13 protected-file comparisons pass.
  Both native audits show zero forbidden operations and no remaining proof
  handles. All owned connections/thread/disposable files are cleaned up.
  Receipt: `/private/tmp/qplot-round17-safety-receipt.json`.
- Measurement review is clean on ARM and Intel. Per architecture: four real
  QCoDeS runs, 32 full/cache/SQL/restored grids, 1,440 ordered-operation cells,
  4,320 CSV cells, 120 geometry checks, 192 exact statistics checks and four
  previews pass. Covers duplicate/descending/irregular/nonfinite coordinates,
  large integers, float16, transposition and Fill/Mean/Derivative order.
  **26 focused derivative/statistics/FFT-cursor tests passed**. Protected files
  unchanged and all owned databases/connections cleaned up.
  Receipt: `/private/tmp/qplot-r17-measurement-receipt.json`.
- UI review is clean: **two fresh actual Qt compositions passed** for line and
  heatmap hosts, real WAL growth, orthogonal cuts, coalesced refresh, exact
  overlay values, axis swaps/marker movement, clipping, real CSV chooser
  cancel/save and Close All No/Yes during pending reads/cut-axis work. All
  source checks pass and every owned QObject/reader/worker/service is released.
  Early automated-dialog responder stalls were temporary probe issues,
  corrected without application changes; stopped probes/helpers were reaped.
  Probe: `/private/tmp/test_qplot_round17_ui.py`.

### Resumed campaign final validation — completed 2026-10-21

- Final application/test revision is
  `14b73632f1dffb69c8659f9cad7b8e5bd101ffe2`. Rounds 16 and 17 made no source
  edits. Full local suite: **5,509 passed, 110 skipped**, 61 warnings in
  **925.04 s**. Project venv, fixed Matplotlib cache, offscreen Qt and
  `-m pytest --no-cov -n 2 --dist=loadfile --no-loadscope-reorder`.
  Log: `/private/tmp/qplot-bh20261020-14b7363-full.log`. The final log-only commit
  changes none of this tested runtime/test source.
- Ruff, configured mypy (88 files), dependency consistency and whitespace
  checks pass. Actual project startup displays 1.6.2, runs Qt's event loop and
  exits zero. Receipt: `/private/tmp/qplot-r15-final-startup-8tfchaj_/startup.json`.
- Application/native inventories, exact committed-byte comparisons and four
  twine checks pass. Final application artifacts contain 130 wheel files/124
  runtime files and 404 sdist files/all 240 source test modules. Unchanged
  native 1.0.1 artifacts remain verified; no native source edit requires another
  rebuild. Manifest: `/private/tmp/qplot-bh20261019-14b7363-artifact-manifest.json`.
- Fresh compiler-free wheel and editable installations both pass actual GUI
  startup, trusted live reading, installed-byte identity and editable updates.
  No compiler builds attempted. Logs:
  `/private/tmp/qplot-bh20261020-14b7363-wheel-install.log` and
  `/private/tmp/qplot-bh20261020-14b7363-editable-install.log`.
- All **33 confirmed application/diagnostic fixes** are committed and have a
  different subchat's independent approval (C19-014 remains withdrawn).
  Five test-only CI follow-ups include the resumed WAL fixture correction.
- Final hosted candidate observation: **30 successful checks, 10 running,
  six queued**, no failures; all 46 returned checks match the exact `14b7363`
  SHA and API total. This is pending CI, not completed platform acceptance.
  Evidence: `/private/tmp/qplot-bh20261020-14b7363-checks.json`.

### Previous extension checkpoint — superseded by the 2026-10-21 resumption

Four further rounds (14–17) are complete. Rounds 16 and 17 are independently
clean, satisfying the stopping condition within the user's five-round limit.
The resumed work adds C19-032/033/034 and the live-WAL CI fixture correction;
all substantive repairs have regressions and separate approval. No uncommitted
application repair or additional investigation remains. All agent-owned
processes, workers, connections and disposable probe resources were closed;
evidence receipts remain under `/private/tmp`.

Prepared bugfix release: **qplotter 1.6.2**, retaining **qplotter-native 1.0.1**.
Branch: `codex/qplot-bug-hunt-2026-10-19`;
[draft PR #90](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/90).
The database-access invariant is preserved and no settings migrations were
added. Remaining tasks are hosted CI completion and maintainer review/release
gates. The final documentation push may queue new checks; do not infer their
completion from local tests or the earlier candidate status. No merge, tag or
release was performed. Resume investigation only if requested or for concrete
subsequent CI/review failures.

### Additional review rounds — resumed 2026-10-21

#### Round 18 — C19-035 repaired and independently approved

- Three fresh subchats cover safety/live acquisition, measurement correctness
  and actual Qt workflows. Coordinator covers settings/diagnostics, entry points,
  packaging and integration. Baseline is the exact green `64aff93` revision
  recorded above; new clean-round streak remains zero.
- Measurement: **12 fresh real-QCoDeS compositions pass on each of ARM and
  Intel**. Per architecture: 80 worker loads, 60 raw-column comparisons, 1,040
  independent direct-DFT cells, 738 exact secants, 32 grids/288 exact bin means,
  2,880 CSV cells, eight previews and 56 protected-file comparisons. Includes
  incremental multidimensional arrays, Fortran/strided/big-endian storage,
  waveform controls and viewport reduction. No actionable finding.
  Receipt: `/private/tmp/qplot-r18-measurement-receipt.json`.
- UI: **26 checks pass**, including two fresh actual-Qt compositions with
  overlapping run IDs across databases, typed/table launch, rapid selection,
  preferences Apply/Cancel/Restore, retained-source operation refresh, SVG/PDF
  export and normal shutdown. Owned windows/workers close and protected files
  remain unchanged. Receipt: `/private/tmp/qplot-round18-ui-receipt.json`.
- Coordinator: **three actual module startups pass** with ordinary settings,
  hardlinked config/log paths and symlinked config/log paths pointing at a real
  disposable QCoDeS database. All show version 1.6.2, enter Qt's event loop,
  report settings recovery where appropriate and exit zero. Protected file
  contents and identities remain unchanged; no unsafe recovery backup appears.
  Receipt: `/private/tmp/qplot-r18-auxiliary-elyxfpn6/receipt.json`.
  Configuration, diagnostics and packaging checks: **50 passed, two
  Windows-only skips**, log `/private/tmp/qplot-r18-coordinator-tests.log`.
- Safety found C19-035: a persistent reader can validate stale columns after
  QCoDeS adds metadata. The same first-query failure is independently reproduced
  directly against SQLite and through an actual QCoDeS acquisition. Author owns
  only `trusted_live.py` and the dedicated regression file; another subchat
  reviews the repair. Full integration and new clean rounds follow the fix.

**C19-035 — refresh the persistent reader's schema before describing results.**

- Reproduction: open a live broker against an empty current-QCoDeS database,
  create a background-acquired run and call `dataset.add_metadata`. Refresh
  sees a changed `data_version`, but the selected run omits the newly created
  metadata column. Direct native reads isolate two forms: the first zero-row
  `SELECT * FROM runs WHERE 0` returns stale column names after the schema
  change; the first nonempty `SELECT * FROM runs` raises a metadata mismatch.
  All **three new regressions fail before the repair**.
- Cause: `BEGIN` is deferred and APSW `query_info` prepares without stepping.
  SQLite refreshes its cached schema on execution, after qPlot has already
  validated columns and calculated their result limits. Empty results then
  retain the stale validated description; nonempty results fail the consistency
  guard. The broker can retain those stale column names for later metadata reads.
- Fix: step a fixed `SELECT 1 FROM main.sqlite_schema LIMIT 1` immediately
  inside the existing transaction before describing user queries. This pins
  the snapshot and refreshes the schema without materializing schema contents.
  Existing native protections, authorizer, length/work/deadline controls,
  cancellation and rollback cover the read. No native source changes or
  database writes; the application change is nine lines including explanation.
- Author validation: **95 passed, one Windows-only skip** across native reader,
  result limits, cancellation, the three regressions and fresh compositions.
  Three background QCoDeS runs/six acquired blocks pass 18 protected-file
  comparisons, ten writer checkpoints, four physically read-only main/WAL
  descriptor checks, four mutation refusals and native audit/resource cleanup.
  Three real rollback-lock contention cycles pass shared cancellation, queued
  expiry, backpressure and recovery on the same helper.
  Final broker/service follow-up: **61 passed**. Owned test databases, helpers,
  workers, connections and endpoints are cleaned up.
  Receipt: `/private/tmp/qplot-round18-safety-receipt.json`.
- Separate UI subchat independently approves: **eight checks pass**, including
  actual schema commits during query validation (stable current snapshot/new
  schema on next transaction), cancellation/deadline immediately after the
  schema step, rollback/length restoration/reuse and exact result-budget checks.
  Source invariance, writer checkpoints and zero forbidden native operations
  pass. Receipt: `/private/tmp/qplot-round18-schema-review-receipt.json`.
- Coordinator post-fix actual startup: all three ordinary/hardlink/symlink
  scenarios pass again; receipt
  `/private/tmp/qplot-r18-auxiliary-z8i6mlvd/receipt.json`. Ruff, configured mypy
  (88 files), dependency consistency and whitespace checks pass. Full local
  integration is running against the final repair and all three new tests:
  `/private/tmp/qplot-bh20261021-c035-full.log`. No legacy settings migrations.

#### Round 19 — C19-036 repaired and independently approved

- Three fresh independent subchats examine the repaired application. Safety
  and UI find no additional actionable issue. Measurement confirms C19-036
  below, so the new clean-round streak remains zero.
- Safety: seven actual QCoDeS schema additions; 15 rejected, expired or
  cancelled reads followed by recovery on the same reader; oversized metadata
  presentation; 2/3/4-point captures and completed-cache reuse. **15 protected
  file comparisons**, four writer checkpoints, zero forbidden native operations
  or proof leaks, and full helper/thread/endpoint/spool cleanup pass. Focused
  follow-up: **10 passed**. Receipt:
  `/private/tmp/qplot-round19-safety-receipt.json`.
- UI: one fresh actual-Qt composition passes with **three background runs,
  five flushed blocks and 11 protected-file comparisons** on one persistent
  helper. New metadata before/after points, exact line/map values, selection
  and tab changes, Auto-plot disable/re-enable, completion status, closed-plot
  replay suppression and normal close during refresh all pass. Helper resources
  retire and plot QObjects are deleted. Receipt:
  `/private/tmp/qplot-round19-ui-receipt.json`.
- Coordinator: committed C19-035 revision `f89bbd211763f2bbce30758ec2810fa1e16ab70a`
  is pushed. Application/native artifact inventories, exact committed-byte
  comparisons and four twine checks pass: application wheel 130 files/124
  runtime files; sdist 405 files including all **241 source test modules**.
  Unchanged native 1.0.1 artifacts remain verified. Manifest:
  `/private/tmp/qplot-bh20261019-f89bbd2-artifact-manifest.json`.
- Fresh compiler-free wheel and editable installations pass dependency checks,
  actual GUI startup/live reading, installed-byte identity and editable update
  without reinstalling. No compiler builds occur. Logs:
  `/private/tmp/qplot-bh20261021-f89bbd2-wheel-install.log` and
  `/private/tmp/qplot-bh20261021-f89bbd2-editable-install.log`.
- Superseded C19-035 full run was gracefully interrupted after **2,618 passed,
  34 skipped**, no failures (399.27 s), when C19-036 was confirmed. This is
  partial validation, not final acceptance. The next full run will cover both
  repairs together; log `/private/tmp/qplot-bh20261021-c035-full.log`.

**C19-036 — finite clipping bounds underflow or overflow narrow floating arrays.**

- Reproduction through actual registered controls and real current-QCoDeS
  arrays: float16 `[1, 2, 3]` with Limit Minimum `70000` becomes all infinity,
  leaving no line or CSV samples. Limit Maximum `1e-8` instead becomes all zero.
  Float32 limits `1e40` and `1e-46` show the same overflow/underflow. NumPy casts
  scalar clipping bounds back to the narrow input dtype. Original measurement
  values are unchanged, but processed/exported results are wrong.
  Evidence: `/private/tmp/qplot-r19-clipping-probe.log`.
- Author owns only `plot_tools.py` and the new narrow-float clipping regression
  file. A different subchat reviews the proposed narrow-float promotion before
  clipping, including unchanged integer/object/extended-precision paths.
- Fix: widen only dense floating arrays narrower than binary64 before `np.clip`.
  All original float16/float32 samples are exactly representable after widening,
  and the existing finite scalar bound is retained. Four production lines;
  integer/object and float64/longdouble processing stays unchanged.
- All **16 new regressions fail before** the fix: actual line/heatmap controls,
  processed and raw CSV, clipping followed by derivatives, signed bounds and
  special values. Author validation: **71 passed, two ARM-only extended-float
  skips**; final no-op/signed-zero assertions: **four passed**. Originals and raw
  exports remain unchanged. Logs: `/private/tmp/qplot-r19-clipping-before.log`
  and `/private/tmp/qplot-r19-clipping-after.log`.
- Different safety subchat independently approves: **73 passed/two skipped on
  ARM; 13 passed on Intel**. Independent scalar oracles compare **1,253,376
  values per architecture**, including every float16 bit pattern, seeded
  float32 values, signed bounds and foreign-endian arrays. Actual controls,
  export/restore and existing integer/object/true extended-float regressions
  pass; no repository edits by reviewer. Receipt:
  `/private/tmp/qplot-c036-independent-review-receipt.json`.
- Coordinator post-fix startup passes all three ordinary/protected-alias cases,
  with visible 1.6.2 window, active event loop and exit zero. Receipt:
  `/private/tmp/qplot-r18-auxiliary-i_4lkww0/receipt.json`. Ruff, configured mypy
  (88 files) and whitespace checks pass. Full integration restarted against
  both application fixes: `/private/tmp/qplot-bh20261021-c036-full.log`.

#### Round 20 — complete and clean (streak 1)

- Safety: fresh actual-QCoDeS capture/snapshot/close/reopen composition passes
  **6,400,048 exact array values, 524 lazy snapshot pages, 15 protected-file
  comparisons, eight physically read-only main/WAL descriptors and five writer
  checkpoints**. Close during active BLOB capture returns in 0.029 ms and
  cleanup completes in 89 ms; no partial publication. Retained data remains
  usable, reopened sessions reject stale ownership, completed cache reuse and
  new-session capture are exact. All helper/thread/endpoint groups and private
  spools retire. No actionable finding or source edit.
  Receipt: `/private/tmp/qplot-round20-safety-receipt.json`.
- Measurement: four planned/unplanned float32 and mixed signed/unsigned integer
  variants pass **40 worker loads, 32 cache prefixes, 784 raw-cell checks,
  192 grid cells, 96 Fraction-oracle centered cells, eight exact reduced means,
  576 CSV/TSV cells, 16 previews and 16 protected-file comparisons**. Covers
  empty/singleton/ragged records, missing basis fields, unequal inferred outputs,
  independent load order and bounded-to-full retry. All sources remain intact;
  helpers/readers and fixtures are cleaned. No actionable finding.
  Receipt: `/private/tmp/qplot-r20-measurement-receipt.json`.
- UI: one fresh actual-Qt composition passes **89 exact plotted values and
  28 CSV cells**, including shared processed sources across two hosts, retained
  refresh, line-to-map promotion, independent Clear/reapply, two differentiated
  cuts/axis changes, export and queued shutdown. Three protected-file snapshots
  are unchanged; six plot QObjects and all helper/thread/endpoint resources
  retire. No source edit or actionable finding. Receipt:
  `/private/tmp/qplot-round20-ui-receipt.json`.
- Coordinator performance: a real 65,537-point QCoDeS WAL database passes
  **360 four-query batches/1,440 statements**, exact result comparisons, six
  protected-file comparisons and safe native audits/handle cleanup. Median
  batch times are 1.19/1.26 ms with the schema preflight versus 1.14 ms in a
  static-source reference probe omitting only that preflight. These are local
  observations under concurrent tests, not portable timing requirements.
  Initial probe-only multiprocessing-guard and WAL-fixture assumptions were
  corrected; no application issue. Receipt:
  `/private/tmp/qplot-round20-read-performance-receipt.json`.
- Both repairs are committed and pushed at
  `3cb0e010879036a7c1be0ad71bd90e2861622bfa`. Rebuilt application artifacts and
  unchanged native 1.0.1 artifacts pass exact committed-byte/inventory checks
  and four twine checks. The source archive contains all **242 test modules**.
  Fresh wheel and editable compiler-free installations again pass real GUI
  startup/live reading, byte identity, dependency consistency and editable
  updates. Logs: `/private/tmp/qplot-bh20261021-3cb0e01-build.log`,
  `/private/tmp/qplot-bh20261021-3cb0e01-wheel-install.log` and
  `/private/tmp/qplot-bh20261021-3cb0e01-editable-install.log`.

#### Round 21 — complete and clean (streak 2; investigation finished)

- Safety: fresh real-QCoDeS journal/SHM refusal-and-recovery composition passes
  **39 protected-file comparisons, seven expected refusals and three healthy
  native reads after refusal**. Missing/empty regular SHM recovers safely;
  symlink/hardlink/directory SHM layouts remain unchanged on refusal. A real
  appearing rollback journal retires the old service; explicit reopening keeps
  retained captures valid and reads resumed acquisition/completion exactly.
  All six services, proofs, helper/thread/endpoint groups, captures and writer
  retire. The initial execution approval concern was resolved by explicitly
  assigning/asserting the disposable fixture path before opening QCoDeS; no
  user database was accessed and nothing remains blocked. No source edit or
  actionable finding. Receipt: `/private/tmp/qplot-round21-safety-receipt.json`.
- Measurement: four actual-QCoDeS planned/unplanned and ascending/descending
  cases pass **1,280 heatmap cells against rational derivative oracles, 108
  axis-coordinate checks, 216 cut samples, 3,600 plot CSV values, 240 raw CSV
  values, four previews and four protected-file comparisons**. Covers nonuniform
  axes, swapped assignments, ordered derivatives and slider/marker ownership.
  Probe-only preview-order/unique-output-path assumptions were corrected; final
  execution and cleanup pass. No source edit or actionable finding. Receipt:
  `/private/tmp/qplot-round21-measurement-receipt.json`.

- UI: fresh actual-Qt composition passes **67 exact values and three protected
  file comparisons**. Real incompatible-axis Cancel/OK and Preferences
  Apply/Cancel/OK change concurrency/heatmap budgets during queued refresh;
  full-to-one-bin-to-full values stay exact. Closing open editors and pending
  refresh deletes nine owned QObjects and retires all helper/thread/endpoint
  resources. No source edits or actionable issue. Receipt:
  `/private/tmp/qplot-round21-ui-receipt.json`.
- Coordinator: **11 actual installed CLI invocations** pass with Unicode/
  whitespace/hash-containing paths. Current settings update/find/reset work;
  invalid numeric input leaves settings unchanged. Synthetic generation creates
  two complete runs with 7/21 points; ordinary read leaves main/WAL/journal
  unchanged. Existing-output refusal, explicit selected-output replacement and
  refusal with an existing WAL sentinel preserve every unselected file and
  settings. All disposable fixtures cleaned. Receipt:
  `/private/tmp/qplot-round21-cli-receipt.json`.

All four authorized rounds (18–21) are complete. Rounds 20 and 21 are two
consecutive complete clean rounds. C19-035 and C19-036 are repaired, committed
and independently approved. No more investigation rounds are needed or planned.

### Final validation — 2026-10-21, four-round extension

- Final application/test revision:
  `3cb0e010879036a7c1be0ad71bd90e2861622bfa`. No application or test edits in
  rounds 20/21. Complete local suite: **5,528 passed, 110 skipped, 61 warnings
  in 926.84 s**, exit zero. Project venv, fixed Matplotlib cache, offscreen Qt:
  `-m pytest --no-cov -n 2 --dist=loadfile --no-loadscope-reorder`.
  Log: `/private/tmp/qplot-bh20261021-c036-full.log`. Four extra test-only
  no-op/signed-zero assertions added after this run's collection also pass in
  the author's focused rerun. All final application code is covered by the
  complete run; the final documentation commit changes no tested source.
- Final Ruff, configured mypy (88 files), dependency consistency and whitespace
  checks pass. Actual project startup after both fixes passes three settings/
  protected-alias variants: visible version 1.6.2 window, Qt event loop and exit
  zero. Startup receipts and logs retained; owned disposable measurement files
  and aliases subsequently removed.
- Exact committed application/native artifact checks and four twine checks
  pass; the sdist includes all **242 test modules**. Fresh compiler-free wheel
  and editable installations pass actual GUI/native-reader startup, installed
  byte identity, dependencies and editable changes without reinstalling.
  Manifest: `/private/tmp/qplot-bh20261019-3cb0e01-artifact-manifest.json`.
  Native source/pin remains 1.0.1, already rebuilt and verified; no new native
  modification requires another rebuild.
- Current campaign totals: **35 confirmed application/diagnostic fixes** and
  five test-only CI corrections. C19-014 remains withdrawn. Every substantive
  repair has concrete evidence, meaningful regressions and another subchat's
  independent approval. Database invariants remain intact; no legacy settings
  migrations were added.
- Hosted CI observation for exact `3cb0e01`: **28 successful, ten running,
  eight queued**, no failures; all 46 returned check runs match the SHA and API
  count. This remains pending platform acceptance. Evidence:
  `/private/tmp/qplot-bh20261021-3cb0e01-checks.json`.
  [Candidate CI](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/39561239943).
  The starting revision `64aff93` was independently verified **47/47 green**.

### Previous extension checkpoint — rounds 18–21

The four-round request is complete. Rounds 20/21 satisfy the two-consecutive-
clean-round condition. All subchats have finished; owned processes, readers,
Qt objects, helper endpoints and disposable fixtures are retired. Reproduction
scripts, receipts and validation logs remain under `/private/tmp`.

Branch: `codex/qplot-bug-hunt-2026-10-19`;
[draft PR #90](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/90).
Prepared release: **qplotter 1.6.2**, retaining **qplotter-native 1.0.1**.
The final documentation commit records local acceptance; the branch push and
draft PR update publish this checkpoint. That push may queue fresh CI; do not
infer completion from the earlier candidate or local results. Subsequent work
is hosted CI completion and maintainer review/release gates, or investigation
of a concrete later CI/review failure. No merge, tag or release was performed.

### Additional review rounds — rounds 22–25

#### Round 22 — C19-037 repaired and independently approved

- Three fresh subchats investigated safety/live scheduling, sparse measurement
  operations and actual Qt export/source workflows. Coordinator reviewed
  diagnostics, startup, metadata/repair helpers and packaging integration.
  Current clean-round count is zero because C19-037 was confirmed.
- Safety: fresh current-QCoDeS composition passes **1,048,624 exact array
  values**, three subscriber cancellations, queued expiry and two priority
  preemptions. Five writer checkpoints succeed, including between capture
  transactions; nine protected-file/acquisition-timestamp comparisons and four
  physically read-only main/WAL descriptors pass. Retained captures exclude
  later appends; replacement work survives cancellation of previous subscribers.
  Helper/thread/endpoint/spool cleanup passes. Focused broker follow-up:
  **12 passed**. Receipt: `/private/tmp/qplot-round22-safety-receipt.json`.
- Measurement: four planned/unplanned uint64/float32 sparse-map compositions
  pass 20 actual-control fill/clip/mean refreshes: **1,008 exact cells, 504
  Fraction-oracle centered cells, 1,512 processed CSV/TSV values, 252 exact raw
  CSV values, 252 restored cells, 504 worker comparisons and 12 protected-file
  comparisons**. Opposite fill order, edge gaps, snake acquisition and fractional
  clipping preserve data. Bounded display `float(Fraction mean)` rounding and
  refusal of operations during forced detail reload match explicit existing
  contracts; these hypotheses were rejected. Focused suite: **31 passed**.
  Receipt: `/private/tmp/qplot-round22-measurement-receipt.json`.
- UI: one actual-Qt composition passes **30 plotted values, 60 CSV/TSV cells,
  eight file dialogs, two overwrite confirmations and four protected-file
  comparisons**. Covers same-name databases/overlapping IDs, retained sources,
  overlays, browser close, window restoration, format switching, Cancel/No/Yes,
  PNG/SVG clipboard and literal source annotations. Owned QObjects, both reader
  services and fixtures retire. Receipt:
  `/private/tmp/qplot-round22-ui-receipt.json`.

**C19-037 — cyclic diagnostic log paths crash startup on Python 3.11/3.12.**

- Reproduction: actual project `-m qplot` with an isolated ordinary log exits
  zero and displays a window; a self-loop or two-link log cycle exits **1 before
  any window appears**. `Path.resolve()` raises `RuntimeError` on supported
  Python 3.11/3.12, but duplicate-handler detection catches only `OSError`, so
  the failure escapes before nonfatal log setup. Resolving an existing handler's
  newly cyclic filename also blocks a healthy replacement destination.
  Evidence: `/private/tmp/qplot-r22-log-startup-before.log` and
  `/private/tmp/qplot-r22-log-startup-js5mw9d5/receipt.json`.
- Fix: catch `RuntimeError` alongside `OSError` at both diagnostic path
  resolution sites. Actual opening remains governed by the unchanged auxiliary
  safety guard; unreadable/nonregular paths still fail closed. No SQLite,
  native, settings-schema or database-access change.
- Four meaningful regressions cover self/pair/parent cycles and recovery from
  an old cyclic handler; all **four fail before** the repair. Focused logging/
  auxiliary protection validation: **45 passed, three subtests passed**.
  Logs: `/private/tmp/qplot-c037-before.log`, `/private/tmp/qplot-c037-after.log`.
  Actual ordinary/self-loop/two-link startups all show the window/event loop
  and exit zero afterward, preserving the symlinks. Receipt:
  `/private/tmp/qplot-r22-log-startup-y5xpopzn/receipt.json`.
- Different safety subchat independently approves: four regressions, two actual
  module startups (including a cyclic parent directory), and two retained-handler
  recovery compositions pass. A detached log inode repurposed as an explicitly
  disposable QCoDeS database remains byte/identity/timestamp identical despite
  buffered diagnostics and a later cycle/protected alias; unsafe streams are
  discarded and safe logging recovers. All resources cleaned. Receipt:
  `/private/tmp/qplot-c037-independent-review-receipt.json`.
- Ruff, configured mypy (88 files) and whitespace checks pass. Complete local
  integration was superseded by round 23's two new fixes (partial result below):
  `/private/tmp/qplot-r22-c037-full.log`.
  Baseline CI remains pending at its later exact-SHA observation: **40 successful,
  six running**, no failures; evidence
  `/private/tmp/qplot-r22-baseline-checks-latest.json`.

#### Round 23 — C19-038/039 repaired and independently approved

- Three new subchats cover live metadata/result limits, independent measurement
  caches and line controls. Measurement is clean; safety/UI each found a
  reproducible issue. New clean-round streak remains zero.
- Measurement: one live WAL run, two shared-cache output plots and four
  alternating acquisition stages cover empty records, multidimensional/ragged
  changes, big-endian float32/float64/float16 and signed/unsigned arrays. Actual
  derivative/mean/FFT controls match independent Fraction/interpolation oracles:
  **84 original coordinates/values, 34 slopes, 42 centered values, 108 FFT
  coordinates/magnitudes and 84 CSV cells**. Five protected-file comparisons,
  restoration and full Qt/helper/writer cleanup pass. Focused follow-up:
  **24 passed**. Receipt: `/private/tmp/qplot-round23-measurement-receipt.json`.
- Coordinator: exact committed C19-037 artifacts (`472698f`) pass inventories,
  byte comparisons and four twine checks; source archive contains all **243 test
  modules**. Fresh compiler-free wheel/editable installations pass actual GUI,
  native reading, dependency/byte consistency and editable updates without
  reinstalling. Native source remains unchanged. Manifest:
  `/private/tmp/qplot-bh20261019-472698f-artifact-manifest.json`; logs
  `/private/tmp/qplot-r23-472698f-wheel-install.log` and
  `/private/tmp/qplot-r23-472698f-editable-install.log`.
- Fresh actual-module startup/Help composition passes ordinary, unavailable and
  stalled optional Git metadata. All show 1.6.2 and open/close nine total About/
  Quick Start/Shortcuts dialogs; diagnostic-path clipboard and ownership cleanup
  pass. Stalled Git reads respect their five-second per-lookup budget; startup
  plus About finishes in 12.95 s and leaves no child processes. Missing Git
  finishes in 3.01 s; ordinary in 2.47 s. No actionable finding. Receipt:
  `/private/tmp/qplot-round23-metadata-startup-receipt.json`.
- Superseded C19-037 full suite was gracefully interrupted after **2,605 passed,
  34 skipped**, no failures (368.97 s), after the two new fixes were confirmed.
  This is partial validation, not final acceptance. Log:
  `/private/tmp/qplot-r22-c037-full.log`. Final integration will cover all repairs.

**C19-038 — clipped trace axis reassignment raises uncaught Qt callback errors.**

- Actual line workflow: enable Clip to View, then reassign axes through Trace
  Appearance. Temporary detachment makes PyQtGraph report a GraphicsLayoutWidget
  instead of a ViewBox. Native clip-to-view mapping calls its nonexistent
  `autoRangeEnabled()`, causing 12 uncaught callback AttributeErrors across the
  composition. This is a callback failure, not a demonstrated process crash;
  qPlot's exception logger keeps the reproduction running.
  Evidence: `/private/tmp/qplot-round23-ui-r3.log`.
- Author owns `_native_transforms.py` and a dedicated reparenting regression
  file. Four real-QCoDeS regressions fail before the narrow view-range guard
  and pass afterward; detach defers rendering and reattach rebuilds display.
  Fresh workflow after the fix passes 12 snap samples, 44 selected values,
  1,170 unchanged raw values, eight invalid-limit recoveries, nine refreshes
  and five protected-file comparisons, with all owned resources retired.
  Receipt: `/private/tmp/qplot-round23-ui-receipt.json`.
- All **109 focused author tests pass**. Different measurement subchat
  independently approves: four regressions and an additional real-QCoDeS
  phase/mean/automatic-downsampling composition pass, including eight axis
  moves, four refreshes, 224 restored coordinates/values and zero Qt callback
  errors. Restoring only the original callback makes that independent
  composition fail with the same AttributeError. Protected files and cleanup
  pass. Evidence: `/private/tmp/qplot-c038-after.log` and
  `/private/tmp/qplot-c038-independent-receipt.json`.

**C19-039 — recovered live metadata retains false omission/truncation warnings.**

- Current-QCoDeS metadata is initially too large for bounded presentation, then
  replaced with small valid values. The same helper recovers all six values and
  a valid snapshot but retains historical unavailable-field names; Raw remains
  `truncated` and incorrectly says complete source data was omitted. The adapter
  only unions old omission flags. Evidence:
  `/private/tmp/qplot-round23-safety-receipt.json` and its probe/log.
- Author owns `trusted_live_queries.py` and a dedicated metadata-recovery
  regression file. Clearing must be tied to newly observed fields and retain
  omissions for fields still unavailable or not re-read. Measurement subchat
  independently reviews both C19-038 and C19-039; coordinator owns integration.
- Independent review caught an initial repair losing a newer omission during
  nested cheap refresh between selected-detail column groups. The revised
  patch follows the existing newest-cache merge authority and reconciles only
  observations still used. Eight regressions cover real live recovery plus
  newer omission/recovery at both column-group and layout yield boundaries.
  No new cache, SQL budget or native access policy is introduced.
- Author validation: **83 passed**. Fresh composition passes **327,710 exact
  array values**, 12 protected-file comparisons, four writer checkpoints and
  four physically read-only descriptors; scalar-limit refusal is recoverable
  with the same helper, and all spools/threads/endpoints retire. Recovered Raw
  is available with no omission flags; malformed snapshots retain their real
  warning until corrected. Before/final receipts:
  `/private/tmp/qplot-round23-safety-before-receipt.json` and
  `/private/tmp/qplot-round23-safety-receipt.json`.
- Different measurement subchat independently approves after **13 checks**:
  eight regressions, the initially failing inter-group case, a new real-QCoDeS
  composition and three unchanged payload/layout-budget controls. Includes
  oversized-to-empty metadata, oversized-to-malformed-to-valid snapshots,
  partial recovery, 12 protected-file comparisons, four writer checkpoints,
  same-helper reuse and full cleanup. Evidence:
  `/private/tmp/qplot-c039-independent-receipt.json`.
- Final full integration started on the frozen C19-038/039 source/tests:
  `/private/tmp/qplot-r23-c039-full.log`. Ruff, configured mypy (88 source files)
  and dependency consistency pass. Native source remains unchanged.
- Integrated actual `-m qplot` startup checks pass ordinary/self-loop/two-link
  log destinations: all show a visible window and event loop, preserve links
  and exit normally. Receipt:
  `/private/tmp/qplot-r22-log-startup-o12qdggw/receipt.json`.

#### Round 24 — complete and clean (streak 1)

- Three fresh subchats independently reviewed the frozen, approved repairs at
  `5c9d7e6`. No new actionable finding or source edit.
- Safety: two actual current-QCoDeS reader helpers on one live WAL database
  pass **43,176 exact values**, eight rollback-visibility checks, four accepted
  cancellations (0.57–1.01 ms), and four deadlines that correctly retire/reap
  the affected helper (112–186 ms total). Independent browser helper remains
  usable; refresh/capture takes 50–102 ms. Thirteen writer TRUNCATE checkpoints,
  39 protected-file/activity comparisons and 24 physically read-only descriptor
  observations pass. Native proofs, prefixes, helpers, threads and endpoints
  retire. Focused suite: **19 passed**. Receipt:
  `/private/tmp/qplot-round24-safety-receipt.json`.
- Measurement: descending snake/nonuniform grids with duplicate uint64 exact
  means and shaped float32 records match independent rational/Lagrange oracles:
  **120 derivatives, 80 centered values, 108 cut coordinates/values, 24 statistics
  samples, 100 raw values, 120 restored cells and 240 CSV/TSV cells**. Four axis
  swaps, four refreshes and four protected-file comparisons pass. Six plot
  QObjects and all reader/writer resources retire, with zero Qt callback errors.
  Intentional precision refusal and captured Stats-copy behavior were verified,
  not patched. Focused suite: **25 passed**. Receipt:
  `/private/tmp/qplot-round24-measurement-receipt.json`.
- UI: fresh three-layer nonuniform heatmaps pass **165 grid values, 32 cut
  values, 15 CSV cells, 24 manual color-level bindings**, four colormap changes,
  eight source refreshes and six protected-file comparisons. Includes all four
  axis pairs, reorder/opacity/visibility, manual-to-union-auto color ranges,
  browser close and retained-source removal with Appearance/Color dialogs open.
  Eight owned QObjects and all services retire; zero callback errors. Focused
  suite: **22 passed**. Receipt: `/private/tmp/qplot-round24-ui-receipt.json`.
- Coordinator: a fresh generator CLI to actual Qt plot-launch composition uses
  Unicode paths, descending 120,001-sample lines and a 151×401 heatmap. GUID,
  double-click and Run actions each paint two launches with one worker thread;
  protected file identity/timestamps/hashes remain unchanged. Cold display
  takes 3.82–5.30 s and repeated display 1.04–1.33 s; these are local observations,
  not portable performance limits. All windows/background work retire. Receipt:
  `/private/tmp/qplot-round24-generator-launch-receipt.json`.
- Exact `5c9d7e6` application artifacts pass all committed-byte comparisons and
  inventories: 130 wheel files/124 runtime files, 409 sdist files including all
  245 test modules and conftest. All four twine checks pass. Native 1.0.1 source
  is unchanged, so its already rebuilt and verified artifacts are reused.
  Fresh compiler-free wheel and editable installations pass GUI/native reads,
  dependency/installed-byte consistency and live editable updates without
  reinstalling. Manifest:
  `/private/tmp/qplot-bh20261019-5c9d7e6-artifact-manifest.json`.

#### Round 25 — complete and clean (streak 2; investigation finished)

- Three new subchats independently reviewed final application `5c9d7e6`.
  No new actionable finding or source edit. Rounds **24 and 25** satisfy the
  two-consecutive-clean-round condition and the requested four-round cap.
- Safety: fresh live WAL reading across settings/export/diagnostic collisions
  passes **8,088 exact values**, 81 protected-destination refusals, nine protected
  log-rotation cases, three safe settings/export recoveries and three rejected
  late-alias publications. Includes direct paths, hardlinks and symlinks, 57
  protected-file/activity comparisons, four writer checkpoints and four physically
  read-only descriptors. Same helper works through all acquisition stages;
  native handles, stages, prefixes, threads and endpoints retire. Native/SHM
  guards and package pins reviewed. Focused suite: **11 passed**. Receipt:
  `/private/tmp/qplot-round25-safety-receipt.json`.
- Measurement: actual three-output live QCoDeS acquisition with unequal array
  chunks and append, merged averages, eleven native-control states per stage,
  removal and restoration agrees with independent Fraction/trigonometric DFT
  oracles: **1,408 displayed values, 240 averages, 288 raw/restored values and
  192 CSV cells**. Three protected-file comparisons and all Qt/helper/writer
  cleanup pass. Complex rejection/raw export/recovery and averaging follow-up:
  **14 passed**. Receipt: `/private/tmp/qplot-round25-measurement-receipt.json`.
- UI: current-running Auto-plot activation, append/completion, new-run plotting
  during selection changes, closed-plot deduplication, sorted/multiple/typed-ID
  selections and browser close/reopen with retained plots pass **150 exact
  values, 24 selection checks, three typed IDs, six refreshes, four no-replay
  checks and seven protected-file comparisons**. Four plot QObjects and both
  services retire. Focused suite: **98 passed, three subtests passed**. Receipt:
  `/private/tmp/qplot-round25-ui-receipt.json`.
- Coordinator: six actual module startups/restarts recover invalid JSON,
  boolean-as-number and nonfinite current settings. Existing backup names point
  by hardlink/symlink to a disposable real QCoDeS database; recovery selects the
  next absent backup, preserves original bytes and all database identity/hash/
  timestamps, saves valid current settings and restarts without new backups.
  All windows/event loops/normal exits pass. Receipt:
  `/private/tmp/qplot-round25-settings-startup-receipt.json`.

Next: finish the already-running full suite, record exact baseline/final CI
observations, commit the final checkpoint and update draft PR #90. Investigation
is complete; retain bugfix 1.6.2/native 1.0.1 without merging or releasing.

#### Final CI follow-up — benchmark destruction on macOS Python 3.14

- The last pending starting-revision job completed with **one failed, 2,757
  passed, 61 skipped, 26 subtests passed**. The actual line-benchmark subprocess
  exits with SIGBUS at `sip.delete(app)`; its native stack includes
  `cleanup_qobject`, `sip_api_visit_wrappers` and `release_QApplication`.
  [Failed job](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/39562904041/job/119099264551).
- This is a concrete validation follow-up to C19-033, not another investigation
  round. Earlier retained-object cleanup was independently verified but never
  claimed to prove the earlier platform SIGBUS resolved. UI subchat owns the
  benchmark and its regression module; measurement subchat independently
  reviews. Avoid claiming a crash fix without evidence of its mechanism.
  Exact stack: `/private/tmp/qplot-f7c02fe-macos314-benchmark-traceback.log`.
- Three stronger subprocess cases demonstrate live plot owners at entry to
  QApplication destruction in the original benchmark (line, heatmap and
  post-display paint failure). An initial generic top-level-widget cleanup
  candidate was rejected because it can destroy Qt menus/frames out of order;
  independent review reproduced that candidate's SIGSEGV. The candidate was
  never committed. The revision restricts cleanup to qPlot plot owners after
  bounded shutdown and parents the paint observer to MainWindow.
- The first complete integration run finished **5,540 passed, 110 skipped and
  four failed**, all four in benchmark subprocess tests that encountered the
  rejected transient cleanup candidate. Stack traces point to that candidate's
  widget deletion; no application-runtime test failed. Log:
  `/private/tmp/qplot-r23-c039-full.log` (966.78 s). A final frozen-source run
  will replace this transitional result after independent review.
- Final owner-specific patch is independently approved. All **10 profiling
  tests pass** for both author and reviewer. Two additional real-QCoDeS
  subprocesses inject second-render failure for line/heatmap plots: the baseline
  retains both plot owners at QApplication destruction; the fix retires them
  and MainWindow first, preserves the original RuntimeError, leaves no active
  background work or Qt callbacks, and completes QApplication destruction.
  Both source comparisons pass. Receipt:
  `/private/tmp/qplot-c033-independent-final-receipt.json`.
- Approval establishes the cleanup-order improvement, **not a proven resolution
  of the hosted Python 3.14 SIGBUS**. That platform remains a required fresh-CI
  acceptance gate. No production application or native source changed in this
  follow-up. Full frozen-source integration is running at
  `/private/tmp/qplot-r25-final-full.log`; Ruff and mypy (88 files) pass.

### Final validation — rounds 22–25, completed 2026-10-21

- Final application/test/script source: `fce02c3`. Full frozen-source suite:
  **5,545 passed, 110 skipped, 61 warnings in 875.25 s**. All benchmark tests
  now pass in the complete run; this supersedes the transient-candidate result.
  Command: project-venv `-m pytest --no-cov -n 2 --dist=loadfile
  --no-loadscope-reorder` with offscreen Qt and the required fixed Matplotlib
  cache. Log: `/private/tmp/qplot-r25-final-full.log`.
- Ruff, configured mypy (**88 files**), dependency consistency and whitespace
  checks pass. Final actual module startup passes ordinary/self-loop/two-link
  log destinations with visible windows, running event loops, preserved links
  and normal exits. Receipt:
  `/private/tmp/qplot-r22-log-startup-avui1bsn/receipt.json`.
- Final runtime bytes under `src/`, native source and dependency declarations
  are unchanged from the exact `5c9d7e6` artifacts that passed compiler-free
  wheel/editable installation, GUI/native-reader and editable-update checks.
  The benchmark follow-up changes only the development script and its tests.
  Final documentation archives are rebuilt against the final checkpoint SHA
  before pushing, with exact tracked-byte/inventory and all four twine checks.
- All substantive fixes have independent approval. Three fresh subchats per
  investigation round covered safety, measurement and UI; coordinator covered
  entry points, settings, diagnostics, generation, performance and packaging.
  Database-access restrictions, current QCoDeS support and settings policy
  remain unchanged. No native rebuild was needed after the initially verified
  native 1.0.1 rebuild because no native source changed.

### Final checkpoint and continuation — four-round cap reached

Investigation, repair, independent review and local validation are complete.
Commits `472698f`, `5c9d7e6` and `fce02c3` contain this extension's repairs and
evidence; the final documentation commit records this checkpoint. Keep draft
PR #90 targeting bugfix 1.6.2. No merge, tag or release has been performed.

The starting revision `f7c02fe` finished **45/47 green**, with the macOS 3.14
benchmark failure and consequent Required-check failure recorded above. The
final push queues new exact-head CI; do not infer success from local tests or
another SHA. Remaining work is hosted CI completion (especially macOS 3.14
benchmark destruction), maintainer review and the normal release gates. Any
concrete CI failure needs diagnosis and validation before acceptance. Do not
start another broad investigation round without a new user request.

### Additional round 26 — complete, 2026-10-21

- Clean local branch, fetched branch and remote tracking ref all match
  `9745b4dc7cb1f6874c99c01e43ebc28fcbf3bd6f`.
- Verified all **47 exact-SHA check runs** and their total: **45 successful**,
  Windows Python 3.14 full shard 2/2 failed, and Required checks consequently
  failed. Both macOS Python 3.14 shards now pass, including the previously
  failing benchmark validation. This starting baseline is not fully green.
  Evidence: `/private/tmp/qplot-r26-baseline-checks.json`.
  [Baseline CI](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/39570306021).
- The failing test helper reads/hash-checks the live `-shm` file even though its
  invariance assertion ignores SHM contents. Windows rejects that read while
  SQLite holds byte-range locks. A writer append has already changed the WAL,
  so the interrupted baseline assignment then reports an unrelated stale-WAL
  comparison during cleanup. This is a test-helper failure; no application
  write is established by the traceback.
  Exact traceback: `/private/tmp/qplot-r26-windows-ci-failure.log`.
- Three fresh subchats cover safety/live/native behavior, measurement and UI.
  Safety owns the shared test observation helper and dedicated regressions;
  measurement independently reviews that correction. Coordinator owns the log,
  packaging, entry-point integration and publishing. No production source
  changed in this round.

#### Confirmed test-only CI correction — locked SHM observation

- Reproduction: the real Windows Python 3.14 failure in
  `test_manual_refresh_updates_paused_sources[button-visible-heatmap_source-heatmap_host]`
  shows `PermissionError` opening SHM after a fixture writer append. A local
  denial-injection regression reproduces the interrupted baseline assignment.
  The pre-fix regression run had **3 failed, 8 passed**, also exposing missing
  checks for deletion or replacement of an existing SHM.
- Repair in `tests/windows/test_heatmap_layer_integration.py`: retain the exact
  main/WAL/journal size, timestamp and SHA-256 observations; use `lstat` only
  for SHM. Require regular type and preserved device/inode when it already
  existed. Creation and in-place SQLite changes remain allowed. Unexpected
  metadata/read errors still propagate. No application database code changed.
- Eleven regressions in `tests/test_campaign_wal_artifact_observation.py` cover
  locked SHM, post-writer baselines, protected content/read failures, permitted
  SHM creation/growth and forbidden SHM removal/replacement/symlinks. They use
  disposable byte fixtures, never a user's measurement database.
- Author validation including every shared-helper consumer: **116 passed,
  2 expected skips**. Evidence:
  `/private/tmp/qplot-round26-artifacts-before.log` and
  `/private/tmp/qplot-round26-artifacts-after.log`.
- Independent measurement subchat **approved** the frozen patch and reran its
  regressions. Additional probes verified exact protected reads without opening
  SHM, propagation of SHM metadata errors, protected-file disappearance,
  nonregular SHM rejection and allowed in-place metadata normalization.
  Receipt: `/private/tmp/qplot-round26-artifacts-independent-receipt.json`.
  This is the sixth test-only CI correction, not another application finding.

#### Fresh application coverage and validation

- **Safety/live/native:** empty current-QCoDeS database to three runs with
  Unicode parameter names, six int64 array appends above `2**62`, expanding
  schema/metadata and retained prefixes across **11 writer TRUNCATE resets**.
  Verified **5,112,042 exact array values**, 20 watermark/page checks, 18
  metadata/snapshot checks, 33 protected-file/activity comparisons, eight
  physically read-only main/WAL descriptors and seven refused mutating queries.
  Refresh/capture stayed within **0.143–0.303 s**. Additional native SHM/reset,
  cancellation and busy checks: **9 passed**. Receipt:
  `/private/tmp/qplot-round26-safety-receipt.json`.
- **Measurement:** real sparse/ragged integer and narrow-float acquisitions
  through ordered Fill Below/Right, clipping, centering, cuts, raw/processed
  CSV/TSV, reset and refresh. Independent fill/Fraction oracles verified 60
  recorded cells, 60 filled cells, 60 centered cells, 72 cut values, 360 export
  cells, 39 raw values and 120 restored cells; six protected-file comparisons
  passed. Focused tests: **31 passed**. Receipt:
  `/private/tmp/qplot-round26-measurement-receipt.json`.
- **UI/lifecycle:** two actual QCoDeS WAL databases with overlapping run IDs,
  lazy snapshot pages/full-value dialogs, run-selection changes, refresh during
  paging, close/reopen and stale queued requests. Verified 20 pages containing
  1,555 exact ordered unique entries, 25,392 copied snapshot characters and
  4,341 metadata characters, eight selections, two stale-request rejections and
  14 protected-file comparisons; no Qt callback errors. Fresh composition:
  **1 passed**; focused suite: **47 passed, 6 subtests passed**. Receipt:
  `/private/tmp/qplot-round26-ui-receipt.json`.
- **Coordinator startup/integration:** actual `python -m qplot -style Fusion`
  and public `qplot.run(database_path=...)` against a private live current-QCoDeS
  WAL writer and a Unicode path with spaces. Verified five then ten exact rows,
  QApplication identity, visible window/event loop, trusted reader, clipboard
  database path, explicit API path precedence over inherited command-line
  arguments, lazy PyQt import and return to the API caller. Both exit normally;
  protected main/WAL/journal identity, size, timestamps and hashes are unchanged;
  helpers retire and both writer checkpoints succeed. Receipt:
  `/private/tmp/qplot-round26-entrypoints-receipt.json`.
- All private writers, plot/dialog objects, reader helpers, worker threads,
  endpoints, retained captures and fixture directories were retired. Ruff,
  configured mypy (**88 files**) and whitespace checks pass. Python commands use
  the project venv, fixed Matplotlib cache and offscreen Qt where appropriate.
- Application/native source and dependencies remain unchanged from the verified
  `5c9d7e6` artifacts and compiler-free installations. No native rebuild is
  needed for this test-only change. The full **5,545 passed / 110 skipped** run
  at `fce02c3` remains the last full-suite result; the relevant changed tests and
  consumers above were rerun, rather than claiming a new full-suite result.
- Publication validation rebuilds the application archives from the clean final
  commit, compares tracked bytes and inventories, and checks all four
  application/native artifacts with Twine. Evidence is recorded in
  `/private/tmp/qplot-r26-final-build.log` and
  `/private/tmp/qplot-bh20261019-<final-short-sha>-artifact-manifest.json`.

#### Round-26 checkpoint and continuation

This request is complete: **zero new application bugs, one confirmed CI
test-helper correction**. The correction is independently approved and all
relevant local checks pass. Commit these three changed files, push the dedicated
branch and update draft PR #90; preserve bugfix target 1.6.2/native 1.0.1.
The final push requests new exact-head CI, especially Windows Python 3.14
manual-refresh validation. Do not equate local simulation with that platform's
acceptance. Remaining gates are final-head hosted CI, maintainer review and
normal release checks. No merge, tag, release or round 27 is authorized by this
one-round request.

### Additional round 27 — complete, 2026-10-21

- Clean starting local, fetched and remote-tracking revision:
  `4ab5862beb3db93bac06688e5bcb5815aff6a510`. Exact-SHA checks initially:
  **25 successful, 12 running, 9 queued**, no failures. All 46 check SHAs and
  the API total matched. A later observation found **31 successful, 10 running,
  5 queued**, still no failures. This is not a fully green baseline. Evidence:
  `/private/tmp/qplot-r27-baseline-checks.json` and
  `/private/tmp/qplot-r27-baseline-checks-latest.json`.
  [Starting CI](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/39603700089).
- Three fresh subchats cover safety/live/native, measurement and UI/lifecycle;
  coordinator covers generation, settings, entry points, packaging, integration
  and this record. Read-only investigation first, with exact file ownership and
  independent review required before any substantive repair. Exactly one new
  round is authorized; do not start round 28.
- **Safety/live/native — clean:** fresh current-QCoDeS incomplete acquisition
  through coalesced capture cancellation, concurrent appends, partial-spool
  cleanup, idle-helper termination/restart, stale cancellation fencing, metadata
  and snapshot paging, completed-cache reuse and retained captures after close.
  Verified **240,320 exact values**, 14 source-page reads, two coalesced pairs,
  three cancelled subscribers, five metadata checks, four snapshot pages,
  **21 protected-file comparisons, six writer TRUNCATE checkpoints and eight
  physically read-only main/WAL descriptors** across two helper incarnations.
  Cancel returned in 0.004–0.019 ms; cancelled spool retired in 1.37 ms. Focused
  checks: **8 passed, 72 deselected**. All helpers, threads, endpoints, private
  prefixes and fixture writers retired. Receipt:
  `/private/tmp/qplot-round27-safety-receipt.json`.
- **Measurement — clean:** two live current-QCoDeS WAL runs acquire valid then
  reversing/repeated coordinates with derivative/FFT controls active. Verified
  atomic rejection, axis-swap recovery, independent Lagrange-gradient and
  interpolated trigonometric DFT oracles, raw/processed exports and reset:
  **58 stored values, 28 DFT coordinates/magnitudes, eight gradients, 16 retained
  values across four rejected refreshes, 26 raw CSV values, 52 plot CSV/TSV
  values and 65 restored values**. Four axis swaps, ten protected-file
  identity/hash/mtime/ctime comparisons and two writer checkpoints passed.
  Both documented active-WAL raw-export refusals preserve existing destinations;
  export succeeds after the disposable writer explicitly checkpoints. qPlot
  never checkpoints the source. Focused checks: **23 passed, 46 deselected**.
  Four plot QObjects, both windows/readers/writers and private fixtures retired;
  no uncaught callbacks. Receipt:
  `/private/tmp/qplot-round27-measurement-receipt.json`.
- **UI/lifecycle — clean:** reciprocal line/heatmap hosts on one live QCoDeS
  WAL acquisition, append, captured statistics/CSV/SVG/image dialogs, Window
  menu restoration, cross-dimension primary-source promotion, normal database
  switching with retained readers, closing the opposite host, further live
  append/refresh and the public Close Database action. Fresh composition:
  **1 passed**; focused refresh/promotion/window/dialog/export checks:
  **58 passed, 175 deselected**. Verified **144 plotted values, 40 CSV values**, four each
  statistics/SVG/image copies (640 × 480 pixels), four Window actions, four
  manual refreshes and seven protected-file identity/hash/timestamp comparisons.
  Three plot and five tracked dialog QObjects plus both services retired;
  no Qt callback errors. Receipt:
  `/private/tmp/qplot-round27-ui-receipt.json`.
- **Coordinator generation/settings/startup — clean:** nine actual console
  operations plus two actual module startups compose private settings commands,
  invalid-value rejection, current-QCoDeS synthetic generation, refusal to
  overwrite without consent and explicit replacement. The selected filename
  contains Unicode, spaces and `#`; the exact selected output is published and
  the historically truncated alias remains absent. Independently checked 38
  setpoint values for reversed line/heatmap sweeps, five protected-file states,
  fresh generation token/GUID/inode after replacement, persisted themes/recent
  path and visible event-loop startup with **5+12 then 9 exact result rows**.
  Both helpers and all reader/process/fixture resources retired; no temporary
  generation artifacts remained. Receipt:
  `/private/tmp/qplot-round27-generation-startup-receipt.json`.
- Coordinator focused generator backend/input/safety, diagnostics and package
  boundary checks: **58 passed**. Ruff and configured mypy (**88 files**) pass.
  No production, native, dependency or test source has changed in this round.
- **Rejected hypothesis:** a retained-plot Refresh after calling low-level
  `close_database()` directly raised a fallback refusal/KeyError. The probe
  bypassed normal lifecycle ordering: File → Close Database calls
  `close_current_database()`, closes plots first and aborts if closure is
  cancelled. Quit and Reset Settings also close plots before source teardown.
  Safety independently confirmed this contract. Normal database switching
  retains plot-owned brokers. No speculative patch or regression was added for
  the unsupported low-level sequence; UI validation uses the actual supported
  switching workflow instead.

#### Round-27 validation and checkpoint

All four investigation streams are complete and clean. No new regression test
or production repair is justified. Database-access restrictions, current QCoDeS
support and settings policy remain unchanged. Relevant focused suites, fresh
compositions and actual qPlot startup pass; Ruff, configured mypy and whitespace
checks pass. The last full-suite result remains **5,545 passed / 110 skipped**
at `fce02c3`; all tests changed afterward were validated in round 26. No source
change here requires another full run, native rebuild or compiler-free install.

The publication checkpoint rebuilds application archives from the clean final
documentation commit and verifies exact tracked bytes, inventories and all four
application/native Twine checks. Evidence:
`/private/tmp/qplot-r27-final-build.log` and
`/private/tmp/qplot-bh20261019-<final-short-sha>-artifact-manifest.json`.
Push the record on the existing dedicated branch and update draft PR #90 with
the completed round and honest CI status. Hosted CI completion, maintainer
review and normal bugfix-release gates remain; no merge, tag, release or round
28 has been performed or authorized by this one-round request.

## Previous campaign archive

### Final campaign result

- **27 confirmed findings repaired and independently reviewed**: BH-001–026
  and BH-028. BH-027 was withdrawn after a mocked reproduction was disproved;
  its speculative change and tests were removed.
- **Two consecutive complete clean review rounds: 15 and 16**, with rotated
  safety, measurement and UI reviewers plus coordinator integration review.
- Final application revision: `f6a9d3f372a3c7d1e635dd8362c0580eb5649a60`.
  Full suite: **4,963 passed, 44 skipped, 119 subtests passed** in 782.96 s.
  Log: `/private/tmp/qplot-bug-hunt-bh028-full.log`.
- Ruff, mypy (**88 source files**), whitespace checks and actual qPlot startup
  pass. The window becomes visible, Qt's event loop runs, and shutdown exits 0.
- Rebuilt application wheel/sdist pass inventory and twine checks: 370 sdist
  files including 208 test modules plus conftest; 129 wheel files including
  123 runtime files, all byte-identical to the final source. Artifacts:
  `/private/tmp/qplot-bug-hunt-f6a9d3f-dist`.
- Native sources are unchanged. The earlier rebuilt universal2 cp311-abi3 wheel
  passed ARM64/x86_64 architecture, package and pinned runtime checks.
- Branch: `codex/qplot-bug-hunt`. [Draft PR #84](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/84)
  holds the changes and exact-head cross-platform CI results. No merge/release.
- Database-access invariant preserved; current QCoDeS format supported; no
  settings migrations added. Findings, evidence and review history follow.

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
| Database subchat | Native VFS, read-only/live database access, cancellation, services and scheduling | Complete; two final clean rounds |
| Measurement subchat | Extraction, dimensional interpretation, rendering, numeric operations and transforms | Complete; two final clean rounds |
| UI/lifecycle subchat | Windows, mixed plots, refresh, removal, shutdown, worker lifetime | Complete; two final clean rounds |
| Coordinator | Settings, entry points, diagnostics, synthetic generation, packaging and integration | Complete; final validation passed |

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

Baseline cross-platform CI passed as recorded above. Detailed campaign and final
validation results are recorded below.
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

### Integration validation at `42355f9`

- Full suite: **4,931 passed, 44 skipped, 119 subtests passed**, 805.25 s.
  The corrected finalizer test passes without changing its deadline or cleanup
  assertions. Log: `/private/tmp/qplot-bug-hunt-final-clean-full.log`.
  This run started before BH-028 edits and does not claim validation of that fix.
- Ruff, mypy (**88 files**), actual qPlot startup/window/event-loop/normal exit,
  rebuilt sdist/wheel inventory and `twine check` all pass. All **123 runtime
  wheel files byte-match the source**. Sdist includes 204 test modules plus
  conftest. Artifacts: `/private/tmp/qplot-bug-hunt-42355f9-dist`.

### Fresh round 14 — not clean

- UI/lifecycle: **81 passed**, including fresh actual-QCoDeS line and heatmap
  compositions through mixed drops, appearance, resize/axis swap, refresh,
  SVG export, source promotion and resource release.
- Safety/native/packaging: **144 passed, two Windows-only skips**, plus fresh
  live-QCoDeS settings/logging/export source-protection probes. Protected main,
  WAL and journal identities, metadata and contents remained unchanged.
- Measurement review found BH-028 below. Consecutive clean count resets to
  **0**; fresh complete rounds resume after repair and independent review.

### BH-028 — Long extraction and fill loops miss cancellation checks

- Real current-QCoDeS int64 array heatmap at the normal 2,000,000-point limit,
  with repeated coordinates, uses `general.data2matrix`'s exact duplicate mean.
  Cancellation requested 28.6 ms into Fraction aggregation returned only at
  1.455 s: **1.427 s after cancellation**, with no intermediate cancellation check.
  The worker correctly avoids publication and source mutation, but responds late.
- Reproduction: `/private/tmp/qplot_round14_duplicate_cancel_probe.py`;
  output: `/private/tmp/qplot-round14-duplicate-cancel.log`.
- The same-class audit confirmed normalization of two current-QCoDeS records
  (1,000,000 int64 samples followed by 1,000,000 uint64 samples) spends 1.166 s
  classifying the object array, despite cancellation requested at 35.5 ms.
  Probe: `/private/tmp/qplot_round14_cancel_sweep.py`. Both paths preserve source
  data and avoid publication, but delay cancellation.
- Adjacent real-value classification has the same mixed-record defect:
  cancellation at 35.4 ms returns at 1.106 s. An actual registered Fill Right
  operation on a fully populated default-limit row returns 1.016 s after cancel;
  its finite-sample loop polls only at row boundaries. Source data remains intact.
  Logs: `/private/tmp/qplot-round14-cancel-classify.log` and
  `/private/tmp/qplot-round14-fill-cancel.log`.
- Duplicate aggregation now polls every 1,024 samples, preserving exact means
  and Decimal CSV cells. **62 author tests and 41 independent checks pass**;
  actual two-million-sample cancellation response improves to **18–22 ms**.
- The registered `dz/dx` operation on the same mixed-record two-million-point
  heatmap also delayed cancellation (26.4 ms request, 1.345 s return) in repeated
  object/integer classification. Probe: `/private/tmp/qplot_round14_derivative_probe.py`.
- Worker normalization and complex-value classification now poll every 1,024
  samples. Independent review: **101 cases passed**; actual cancellation responses
  are **0.45 ms and 0.43 ms**, with no publication or source mutation.
- Object-coordinate conversion also spent 1.578 s sorting before its next
  cancellation check. Exact per-sample conversion now proves the same existing
  lossless-coordinate policy without that sort; dense numeric paths are unchanged.
- All repairs are frozen and independently approved. Author tests: **75** for
  aggregation/fill and **123** for worker/derivative paths. Independent batches:
  **41** aggregation, **101** classification, **17** fill, **34** precision, and
  **94** derivative/integration cases passed (batch counts may overlap).
- Independent actual default-limit cancellation responses: **18.1 ms** duplicate
  mean, **0.45 ms** normalization, **0.43 ms** classification, **0.47 ms** fill,
  **0.50 ms** coordinate conversion and **0.12 ms** derivative. No partial result
  was published; source measurements remained unchanged. Nine adversarial
  coordinate comparisons confirmed identical baseline/fixed acceptance and values.
- Coordinator Ruff, mypy, whitespace and actual qPlot startup/window/event-loop/
  normal-exit checks pass. No native source changed. There are now **27 confirmed
  repaired findings** (BH-001–026 and BH-028); BH-027 remains withdrawn.

### Fresh round 15 — complete and clean

- Safety/native/packaging: **20 focused checks passed**, including three fresh
  real-QCoDeS cancel/read/writer-commit/checkpoint/reuse cycles; protected files
  unchanged, cancellation responses 0.265–0.422 ms. Fresh source audit covered
  physical read-only access, exact SHM handling, IPC/transactions/snapshots,
  settings/logging/export protection, generator publication and package pins.
- Measurement: **220 passed, one platform skip**, plus 18 exact irregular-
  derivative oracles and 12 new-seed QCoDeS grid comparisons across extraction
  routes. Source hashes unchanged; caches, numeric operations, statistics,
  previews, averages, rendering and CSV semantics reviewed afresh.
- UI/lifecycle: **19 passed**, including actual cancellation→export→retry→close
  for duplicate means and Fill Right. Previous grids and byte-identical CSV
  survived cancellation; no unexpected Qt callbacks. Mixed promotion, retained
  ownership, cuts, pending previews, SVG protection and preferences also checked.
- Coordinator integration/package review found no new issue. Rebuilt f6a9d3f
  sdist/wheel inventories and twine checks pass; all 123 wheel runtime files
  byte-match source. Ruff/mypy/startup pass. Full integration and CI are running.
- Consecutive complete clean-round count: **1**. Round 16 has rotated ownership
  again and is underway on the same application source.

### Fresh round 16 — complete and clean

- UI/lifecycle: **16 fresh checks passed**, including both real-QCoDeS mixed
  plot hosts through settings, averaging/centering, resize, PDF/clipboard,
  refresh, unchanged-sample comparison, layer removal and shutdown. Main-window
  cancellation/reentry, live CSV freshness, cuts and retained editors also pass.
- Measurement: **32 passed**, including six fresh signed/unsigned/mixed QCoDeS
  ragged-duplicate cases against independent Fraction mean, centering, derivative
  and standard-deviation oracles. Rounded SQL overview semantics and subsequent
  full-resolution operation reload were checked alongside source invariance.
- Safety/native/packaging: **27 checks passed**, including a fresh live-QCoDeS
  materialized-batch cancellation (0.220 ms), no partial publication, rejected
  mutation, unchanged protected hashes/identities, successful writer commit and
  TRUNCATE checkpoint, and zero native handles after close. Full source audit
  includes snapshots, IPC/cleanup, SHM, auxiliary writes, generation and packaging.
- Coordinator integration/package review has no new actionable finding.
  **Rounds 15 and 16 are consecutive complete clean rounds** on application
  revision `f6a9d3f`. The requested independent-review stopping condition is met.
  Final full-suite validation subsequently passed as recorded at the top.
  Cross-platform required checks and the final report are attached to draft PR #84.

### Final CI test-portability correction

- Linux coverage on `af7eec6` passed 4,958 tests but failed the CSV-input FIFO
  symlink metadata assertion. The job showed only symlink access time changed;
  all other metadata and the FIFO target were unchanged. Read-only resolution
  can update Linux symlink atime; this is not an application write or regression.
  [Failure evidence](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/actions/runs/38381171331/job/115242879883).
- The test now compares identity, type, permissions, link count, ownership, size,
  and nanosecond mtime/ctime, plus explicit symlink target/type. Only access time
  is excluded. The rejection and ten-second subprocess limit are unchanged.
- Independent safety review approved; **25 related FIFO cases passed**. Adjacent
  generator-destination validation rejects symlinks before following them and
  remains unchanged; snapshot/config tests already handle allowed access times.
- No application source changed. Two clean application-review rounds remain
  valid. Final exact-head CI includes this portable oracle correction.

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

- Investigation, confirmed repairs, independent reviews and local validation
  are complete. Rounds 15 and 16 satisfy the requested stopping condition.
- The only publishing follow-up is exact-head CI verification on draft PR #84;
  its live check results and final PR description record that outcome. If a CI
  failure appears, inspect that job's evidence and repair a confirmed issue
  before treating validation as complete.
- Keep the PR draft for user review. No merge or release is authorized. No
  unresolved manual test or known confirmed application defect remains.

Subchat usage limits interrupted BH-021 review. The user reported usage reset;
all three final reviewers have been restarted with their existing file ownership.
No unfinished fix or fresh review round was marked approved during interruption.

CI follow-up: cancellation instrumentation now wraps the real `sample_fraction`
helper and still requires interruption at exactly 1,024 converted samples.
All six cancellation/residual regressions pass; final UI independently approved
and reran both cancellation cases. No application cancellation change was needed.
