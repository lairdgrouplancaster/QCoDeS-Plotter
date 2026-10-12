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

1. Integrate independently reviewed BH-010/BH-011 and push for CI.
2. Diagnose failures in full integration run at `fe1d82f`, then revalidate fixes.
3. Complete fresh round 6 and subsequent complete rounds until two consecutive
   rounds find no actionable bugs. Current clean-round count: **0**.
4. Finish final automated tests, actual qPlot startup, distribution checks and
   draft PR/CI status. No native source changed; rebuilt native artifact already
   passed architecture/ABI/package checks earlier in this campaign.
