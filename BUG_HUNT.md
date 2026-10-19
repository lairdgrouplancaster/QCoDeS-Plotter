# qPlot bug-hunt campaign

## Campaign starting 2026-10-19 — active

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
  opened for cross-platform CI. Campaign remains active.
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
- Finish with integrated checks/startup and fresh rotated independent review
  rounds until **two consecutive complete rounds find no actionable bugs**.
- Current clean-round count: **0**. Historical results below are not counted.

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
- Final candidate full suite is running with two xdist workers, log
  `/private/tmp/qplot-bh20261019-final-full.log`. Source must stay unchanged for
  this run to count as final validation. Native source remains unchanged.

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
  builds attempted. Full suite is still running; after C19-019 these count as
  transitional validation.

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

### Latest continuation checkpoint

Initial coverage and rounds 1/2/3 complete. First 17 confirmed findings are fixed
and independently approved, pushed at `b8fb173`; C19-014 was withdrawn.
Round 4 found C19-019 and C19-020; both repaired and independently approved.
All 19 confirmed repairs are complete. Round 5 underway. Repeat
fresh/rotated reviews until two consecutive complete rounds are clean.
Transitional full suite: 5,073 passed, 44 skipped. Final candidate suite is
running; integrated startup/static checks pass. Final application rebuild and
exact-head CI remain required. No fresh complete clean round yet.
Draft PR #90 only; no merge/release.

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
