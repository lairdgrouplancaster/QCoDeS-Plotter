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
| Database subchat | Native VFS, read-only/live database access, cancellation, services and scheduling | In progress |
| Measurement subchat | Extraction, dimensional interpretation, rendering, numeric operations and transforms | In progress |
| UI/lifecycle subchat | Windows, mixed plots, refresh, removal, shutdown, worker lifetime | In progress |
| Coordinator | Settings, entry points, diagnostics, synthetic generation, packaging and integration | In progress |

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

## In-progress confirmed repairs

- Live metadata refresh: accepting the writer's final `data_version` before a
  watched status failure can permanently suppress retries. Initial six failing
  cases repaired; independent measurement review found an additional BASIC_PAGE
  failure/cancellation gap, now being covered before approval.
- Trusted previews: Boolean NPY rejection, distinct uint64 coordinates merging,
  duplicate-cell cancellation residual loss, and finite-extreme overflow. Focused
  numeric/image and real QCoDeS roundtrip regressions in progress; cache renderer
  version updated. Independent review pending.
- Plot refresh: zero-delay pending callbacks repeatedly repost while a replacement
  worker runs. One-callback regression and 111 related tests passed; database
  subchat approved completion-path scheduling and cancellation behavior.
- Mixed source removal: a surviving heatmap cut is not a constructible full plot
  source; current action raises TypeError. Guard/eligible source selection and
  integration regressions in progress.

## Next tasks and continuation checkpoint

1. Finish initial three investigations and coordinator review, reproduce findings,
   and integrate focused repairs with regression tests.
2. Cross-review substantive fixes; cover remaining application areas, native
   packaging, performance, and subsystem interactions.
3. Run relevant automated checks and qPlot startup; rebuild native code if changed.
4. Commit/push focused changes and create/update a draft PR for cross-platform CI.
5. Perform fresh independent complete review rounds until two consecutive rounds
   find no new actionable bugs. Current clean-round count: **0**.
