# Test suite review

Reviewed the local checkout at `817217f` on 15 November 2026. The objective is
clearer, maintainable protection of current qPlot requirements. Reducing the
number of assertions is not itself an objective.

## Findings and changes

The baseline contains 370 test modules, 3,322 Python test function definitions
and 8,291 collected cases. The earlier text-only estimate of 3,325 definitions
included test definitions embedded in subprocess source strings. Test modules
contain 143,846 lines, including their fixtures and helpers.

| Finding | Decision |
| --- | --- |
| 67 modules use `bug_hunt` or `campaign` prefixes; two more are named after numbered development stages. | Replace historical file names with the behaviour or feature they cover. Rename 15 ambiguous or historical test functions and update CI selectors. |
| All 128 top-level test modules mix application features, infrastructure, numerics and GUI work. | Move them into named feature directories, retaining the existing datahandling, windows and widgets organisation. There are 130 file moves including the two numbered-stage files. |
| 209 test modules import 796 symbols from 68 other test modules. | Extract 203 shared definitions or constant assignments into dedicated support modules. Redirect consumers and keep test-local helpers local. No test module needs to import another test module. |
| Extracted assertions would lose pytest's useful value comparisons. | Register `tests.support` for assertion rewriting before collection, preserving detailed failure diagnostics. |
| Eight settings-recovery tests repeat closely related setup and embed unrelated numerical defaults in their assertions. | Express two current behaviours over six invalid settings inputs: successful recovery, and recovery when saving fails. Check the full default configuration, exact original backup bytes and persistence separately. The 12 cases retain the rejection categories and extend failure-path coverage. |
| A preview-shutdown regression can pass without reaching its simulated deleted Qt signal. | Assert that cancellation and the finished-signal attempt both occur, in order, as well as checking that the deleted signal does not escape as an exception. |
| Eleven baseline desktop checks fail because this Windows environment lacks symlink privileges or sandbox registry access. | Probe only the required capability before application code runs. Eight symlink-dependent cases now skip explicitly here; native registry operations run through a private HKCU test root. The three native registry checks also pass outside the sandbox as a standard user. |
| Seven live-metadata cases can mistake delayed Windows writer timestamps for reader writes. | Establish stable content and metadata at each explicit writer boundary before submitting reader work. Preserve the full post-read comparison and native operation audits. Representative failures reproduce with the original test sources. |
| Identical test bodies can cover different requirements. | Retain the two identical CI-gate assertion bodies: their parameter sets distinguish mandatory jobs from optional jobs and exercise different accepted statuses. |
| Numerical, database and cancellation tests often look similar while checking different failure mechanisms. | Retain the independent rational oracles, scalar/array and memory/buffered/streamed routes, numerical boundaries, cancellation phases and writer/reader lifecycle checks. |
| Large mixed test modules and implementation-sensitive assertions remain. | Keep their current protection during this refactor. Split by a demonstrable behaviour boundary when modifying the relevant feature; a file-size target alone is not sufficient justification for changing assertions. |

No application or native-reader code is changed. The settings simplification
does not introduce support for old settings formats. It derives invalid inputs
from the current settings schema and checks the documented reset behaviour.
Dedicated tests of particular default values remain separate from recovery.

## Requirements and evidence

This maps the principal requirements to representative tests; it is not an
assertion that executing a line proves every possible behaviour of that feature.

| Requirement | Representative evidence and review decision |
| --- | --- |
| Preserve existing main databases, WALs and rollback journals; permit only the specified native SHM operations. | `datahandling/test_readonly.py`, `test_trusted_live.py`, `test_trusted_read_scope.py`, `test_trusted_wal_contention.py` and the separate-process artefact audits. Retain both native operation audits and protected-file comparisons. |
| Bound and cancel reader work, releasing checkpoints. | `datahandling/test_live_run_discovery.py`, `test_trusted_live_supervisor.py`, `test_trusted_live_service.py` and worker cancellation cases. Retain lifecycle and failure-path distinctions. |
| Read current QCoDeS measurements faithfully. | `datahandling/test_qcodes_measurement_layouts.py`, `test_multidimensional_measurements.py`, array metadata and inferred-parameter cases, plus `workers` tests. Preserve independent stored-sample expectations. |
| Preserve numerical meaning across aggregation, transforms, display and export. | `workers/test_bounded_heatmap_means.py`, `tools/test_sample_statistics_accuracy.py`, native transform/FFT cases and plot CSV precision cases in `windows`. Retain independent exact-arithmetic expectations and zero-sensitive tolerances. |
| Publish current live state and reject stale work. | Trusted-work scheduler/coordinator tests, `windows/test_paused_plot_manual_refresh.py`, source promotion, pending-drop and live WAL UI cases. Retain ordering and ownership scenarios. |
| Export only to permitted destinations and preserve sources. | Database and CSV export cases in `datahandling`; export paths, staging, tasks and selection cases in `windows`. Preserve cancellation, failure cleanup, aliases and destination-conflict checks. |
| Validate current settings, recover incompatibilities and save safely. | `configuration/test_config.py`, `test_settings_recovery.py`, auxiliary database protection and diagnostics cases. Consolidate repeated recovery scenarios while retaining atomic-write fault injection. |
| Generate synthetic data only when explicitly requested and only at authorised destinations. | `generation` specifications, backend isolation, instruction output protection and special destination cases. Preserve separate generator and viewer responsibilities. |
| Start, stop and release resources predictably. | `lifecycle` supervisor/protocol tests and the GUI lifecycle tests in `windows`. Keep true process checks as well as fake-worker failure injection. |
| Preserve desktop integration and update behaviour. | `desktop` and `updates`, with GUI integration cases in `windows`. Platform-specific results remain subject to their actual platform runs. |
| Ship the intended artefacts and enforce release/documentation checks. | `infrastructure` tests. Preserve the existing release workflow and publishing behaviour; only test selectors and timing-profile paths change. |

Unsupported database rejection tests remain relevant to fail-closed viewing.
References to old writer-provenance encodings or the snapshot fallback do not
constitute a settings-migration requirement and are not removed merely because
their names contain “legacy”.

## Baseline failures

The original full Windows run took 50 minutes and reported 7,906 passed,
374 skipped and 11 failed, plus 142 passed subtests. Eight failures were
`WinError 1314` while tests emulating macOS tried to create actual symlinks.
Three were `WinError 5` at native registry access. None of these failures
established an obsolete product expectation or an application regression.

The desktop fixtures check capabilities before entering application code.
They do not catch application failures and convert them into skips. Native
registry checks use the actual Windows API, with `HKEY_CURRENT_USER` redirected
to a newly created private test key, and assert that cleanup removes that key.
The association round trip now seeds a known default viewer inside this private
root instead of adding a test Open With entry to the developer's real `.db`
association. Existing fake-registry failure-injection tests remain in place.

The revised run also exposed seven failures in metadata recovery and live schema
refresh audits. Two representative cases reproduced against the original test
sources: their content hashes and identities were unchanged, but Windows
published a pending `st_ctime_ns` update when the first audit descriptor closed.
They had passed in the full baseline run, demonstrating a timing-dependent test
defect rather than a reason to update the expected reader behaviour.

These cases now use the existing `_stable_artifact_state` helper only after
explicit writer changes and before the reader operation under examination.
Post-read comparisons still require exact hashes, identity and metadata for the
main database, WAL and rollback journal. No timestamp is ignored, and a failed
reader comparison never causes the baseline to be replaced.

## Coverage interpretation

The baseline measured 49,689 of 57,340 Python statements (86.66%) and 14,942 of
19,860 branches (75.24%). The combined figure printed by coverage.py is 83.72%;
it should not be confused with statement coverage alone.

Combining the revised run and final focused checks measures **49,703 statements
(86.68%)** and **14,949 branches (75.27%)**, or 83.75% combined. Source denominators
are unchanged. This union includes repeated executions, so the small increase
is not evidence of a material improvement in coverage. Fourteen previously
executed lines were not hit in these runs, while 28 additional lines were hit.
The lost lines are in timing-dependent acknowledgement polling, cancellation
exception paths and late GUI callbacks; their tests remain present. The local
`coverage-differences.json` records the exact line and branch differences.

Low reported coverage for `_trusted_live_helper.py` (7.8% combined),
`_desktop_bootstrap.py` (22.6%) and `_shutdown_supervisor.py` (42.8%) needs careful
interpretation. Tests exercise isolated child processes, but the original
pytest-cov run did not collect their Python execution. Native C branches are
also outside Python coverage. These are measurement gaps alongside any genuinely
unexercised branches, not evidence that the processes have no tests.

`database_export.py` (51.6% combined), `_windows_shutdown_job.py` (56.6%) and
platform-specific startup paths are useful priorities for a subsequent branch
review. First distinguish code exercised in child processes or on other platforms
from missing failure-path assertions. The follow-up below adds focused process
measurement while retaining isolated startup, unprivileged execution and the
existing reader deadlines.

## Verification method

The original full suite ran under the project Windows venv as an unprivileged
user, collecting branch coverage and JUnit durations. The reorganisation was
staged separately so the baseline could not import partly moved files.
The environment uses Python 3.14.8, QCoDeS 0.63.0, qplotter-native 1.0.1,
pytest 9.2.0 and pytest-cov 7.1.0.

Before simplifying settings recovery, the full collection was compared as a
multiset after mapping paths: all 8,291 case identities were preserved, with no
lost or accidentally duplicated cases. Python syntax trees were compared as
well: 3,302 of the 3,322 test functions were unchanged; the remaining 20 had
import destinations,
repository-relative paths or the deliberately patched helper-module reference
updated. No product expectations change in this structural pass.

After the settings consolidation the collection is 8,295 cases. Revised
application tests ran in the staged copy with the same coverage settings.
Infrastructure, desktop and preview tests have run again in the final checkout;
these focused runs include the capability and assertion repairs. Their results
supersede the staged versions of those tests. The three private native registry
cases additionally passed outside the sandbox without elevation.
The metadata audit files and screenshot smoke test were rerun from the final
checkout. The screenshot failure in the staged run was caused by a missing
`scripts/capture_demo_screenshots.py` in that temporary copy; it passes from the
actual checkout without a test or application change.
Existing recorded file costs follow renamed files; this is not a claim to have
measured a new cross-platform timing profile.

Detailed local inventories, the move map, collection lists, logs, JUnit reports
and coverage JSON are retained under `.recovery/test-review/` (ignored by Git).
The contributor-facing policy is in [Testing qPlot](testing.md).

## Validation results

All **8,295 unique collected cases** are accounted for by the full run and final
focused runs: **7,913 passed, 382 skipped, no unresolved failures**. This combines
the latest result for each case; it is not a claim of one uninterrupted green
full-suite execution after the final repairs.

| Run | Passed | Skipped | Failed |
| --- | ---: | ---: | ---: |
| Original complete suite (50 minutes) | 7,906 | 374 | 11 |
| Revised application suite, excluding infrastructure (49 minutes) | 7,557 | 269 | 19 |
| Final infrastructure, desktop and preview files | 479 | 124 | 0 |
| Private native registry checks outside the sandbox | 3 | 0 | 0 |
| Final metadata audit files and screenshot smoke test | 12 | 0 | 0 |
| Final shared-helper, real plot/reader and isolation checks | 94 | 0 | 0 |

The final runs supersede all 19 staged failures: 11 desktop prerequisites,
seven audit-baseline timing defects and the incomplete temporary screenshot
setup. Three sandbox registry skips are superseded by native passing results.
Most remaining skips concern Ubuntu/Bash infrastructure, unavailable extended
floating precision, POSIX-only filesystem behaviour and macOS APIs. Eight new
skips identify this host's missing directory-symlink capability explicitly.

The final collection contains no duplicate case IDs. Ruff, patch whitespace
checks, all four committed documentation images and a strict MkDocs build pass.
Application source, native-reader source and the release workflow are unchanged.

## Follow-up: process measurement and behavioural contracts

The substantive changes from the first pass were reviewed separately from the
moves. Settings recovery still checks independent current defaults and exact
backup bytes. The preview test now proves it reaches both cancellation and the
deleted-signal path. Registry tests use disposable keys, and database audit
baselines settle only at explicit writer boundaries before the operation under
test. These checks preserve the current requirements.

The follow-up makes four further changes:

* Add an opt-in coverage profile and a focused CI step for normally exiting
  children. The standard platform tests keep their original launch conditions
  and deadlines. No signal or `os._exit` patch is enabled. A portable regression
  checks ordinary subprocesses, isolated `python -I` launches and spawned
  workers, both serially and through pytest-xdist. It also verifies that ordinary
  runs do not start coverage in those children.
* Split the 4,901-line shutdown supervisor test file into supervision, public
  API and cancellation modules. All 58 test function syntax trees and all 102
  collected cases are preserved in the split. Shared process harnesses live in
  `tests/support/lifecycle/shutdown_supervisor.py`. The original scheduling cost
  is distributed by case count; these estimates are not new platform timings.
* Replace a 1 ms export deadline test that could finish before a child started
  with deterministic cancellation/deadline cases. Each observes a real blocked
  child before aborting, then verifies that the child is reaped, private scratch
  files are removed, the stage stays empty and source bytes are unchanged. Add
  live and snapshot cases for a selected run that no longer exists.
* Check bootstrap JSON fields, credentials, Unicode paths, arguments and round
  trips independently of JSON key order or whitespace. Exact authenticated
  binary frame tests and invalid-input rejection tests remain intact.

A controlled comparison uses the same ten existing tests with and without the
process profile. Both runs pass. The counts below are measured statements in
that focused sample, not whole-suite percentages:

| Application module | Parent measurement | With children | Total statements |
| --- | ---: | ---: | ---: |
| `database_export.py` | 77 | 135 | 157 |
| `_shutdown_supervisor.py` | 232 | 525 | 1,865 |
| `_trusted_live_helper.py` | 46 | 203 | 425 |

This demonstrates previously unmeasured execution. It does not imply that every
child failure path is tested. Forced termination can prevent a child from saving
coverage; native C execution remains outside this measurement. The CI merge
regression verifies that adding child data retains the parent's branches.

Collection after this follow-up is **8,303 cases**, with no duplicates. Relative to the first
pass, the old deadline case is replaced by two cases; two selected-run cases and
five coverage-infrastructure cases are added. Local follow-up validation:

| Check | Passed | Skipped |
| --- | ---: | ---: |
| Split lifecycle suite | 173 | 21 |
| Export, database-copy UI and final bootstrap contracts | 52 | 0 |
| CI planning, scheduling, artefacts, coverage and gates | 103 | 104 |
| Packaging, fixture isolation and bounded GUI shutdown | 126 | 0 |
| Same focused process cases, parent measurement | 10 | 0 |
| Same focused process cases, child measurement | 10 | 0 |

These focused runs overlap. A second complete local Windows run was not performed.
Full hosted validation was explicitly requested on the published candidate:
ordinary PR CI selects routine desktop validation. No release workflow or
application/native source is changed. Follow-up logs, collection comparisons and
coverage JSON are retained under `.recovery/test-review-next/` locally.

The hosted Python 3.12/3.13 Windows runs exposed portability errors in the
independent statistics and native-average oracles: NumPy's finite limit can be
a `longdouble`, and `longdouble.item()` remains a NumPy scalar, which older
Python `Fraction` constructors reject. Both oracles now construct rationals from
exact integer ratios. They do not narrow extended values to Python floats or
change numerical tolerances. Named longdouble cases make these inputs explicit,
and the other cases in those parameter sets now have descriptive IDs.

The statistics and marquee-action checks pass locally (19 cases). A broader
diagnostic runs 53 numerical test modules with older `Fraction` scalar acceptance
enforced and NumPy's Windows limit cache initialised through `longdouble`:
**1,235 passed, 94 platform skips**. This focused diagnostic uses the local
Python 3.14 interpreter; the hosted matrix supplies actual Python 3.12/3.13
validation. Collection increases to **8,305 cases**. See
[PR #141](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/pull/141) for
validation runs against the latest candidate.

## Practical limits

The inventory and collection comparison cover the entire suite. The semantic
review concentrates on current requirements, shared support, high-risk database
and numerical checks, and overlapping recovery cases; it is not a claim of a
new independent proof of every assertion in 143,846 lines.

Windows results do not exercise macOS APIs, POSIX permissions/FIFOs, or extended
floating precision unavailable on Windows. Python branch coverage does not
measure the native reader's C branches. Use the existing platform CI matrix for
those checks. A missing branch is a review lead, not automatic justification for
another test, and a passing suite cannot settle an ambiguous product requirement.
