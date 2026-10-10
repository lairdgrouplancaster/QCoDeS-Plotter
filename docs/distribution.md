# Distribution

This repository builds two distributions: the pure-Python application
`qplotter` at the root and `qplotter-native` in `native/`.
Application imports remain `qplot`, and native imports use `qplot_native`.
These are the final distribution names. On 2026-10-10 both canonical project JSON
endpoints returned HTTP 404: [application](https://pypi.org/pypi/qplotter/json)
and [native](https://pypi.org/pypi/qplotter-native/json). No existing PyPI
project conflict was found. A 404 does not prove name eligibility or ownership:
pending publishers and account/project permissions are private. Confirm them in
the release owner's authenticated PyPI account before release. No PyPI account
connector is available in this workspace. The public GitHub environments API
also reported no environments; create both protected publishing environments
described below before triggering publication.

The authoritative package version is `project.version` in `pyproject.toml`.
At runtime, `qplot.__version__` reads the installed `qplotter` metadata through
`importlib.metadata`.

## Ordinary installation

After a stable split release is published, install inside a supported Python
virtual environment with:

```console
python -m pip install --only-binary=:all: qplotter
```

The first prepared application release is the beta `1.6.0b2`, with exact native
dependency `1.0.0`. Once published, install that prerelease explicitly:

```console
python -m pip install --only-binary=:all: qplotter==1.6.0b2
```

The application installs its native dependency and all runtime dependencies
automatically as wheels. Native PyPI releases contain **only wheels**. Without
a compatible native wheel, even pip's default installation cannot fall back to
compiling native source. `--only-binary=:all:` also prevents source builds of
runtime dependencies. Application sdists remain on PyPI; native source builds
are an explicit developer workflow from this repository or its CI native sdist.

Before PyPI publication, install the exact application and matching native
wheel paths from this revision's validated artifacts together. For example,
on Windows x64 (replace the application version when appropriate):

```console
python -m pip install --only-binary=:all: dist/qplotter_native-1.0.0-cp311-abi3-win_amd64.whl dist/qplotter-1.6.0b2-py3-none-any.whl
```

Use your platform's native filename on macOS/Linux. Never mix artifacts from
different revisions when validating a paired release.

Application installs expose the `qplot`, `qplot-cfg`, and `qplot-generate-db` entry
points.

## Editable application development

After the pinned native version is published:

```console
python -m pip install --only-binary=:all: -e ".[dev]"
python -m qplot
```

Before publication, supply the exact local native wheel as an additional pip
argument. See [CONTRIBUTING](../CONTRIBUTING.md#development-environment). Python
edits are picked up when qPlot next starts, without reinstalling the application
or rebuilding native code. All runtime and development dependencies are wheels.

## Replacing the old distribution

Historical tags installed a distribution named `qplot`. That distribution and
`qplotter` own the same Python package and commands, so they must never
coexist. Prefer a fresh venv. When reusing a venv, close qPlot and uninstall both
application names before installing the replacement:

```console
python -m pip uninstall -y qplot qcodes-plotter qcodes-plotter-native qplotter qplotter-native
python -m pip install --only-binary=:all: qplotter==1.6.0b2
python -m pip check
python -c "from importlib.metadata import packages_distributions; p = packages_distributions(); assert set(p['qplot']) == {'qplotter'}; assert set(p['qplot_native']) == {'qplotter-native'}"
```

If both names were previously installed, removing the old one can delete shared
files; reinstalling the new packages **after both removals** repairs ownership.
For old editable installs, uninstall first and remove the legacy generated
metadata from the reused checkout as described in
[Contributing](../CONTRIBUTING.md), before installing the new editable app.
Settings and measurement
databases are outside pip's package files. Historical Git tags, such as `v1.5.0`
and `v1.6.0b1`, retain their original packaging.

## Supported platforms

| Platform | Native publication wheel | Application validation |
| --- | --- | --- |
| Windows x64 | `cp311-abi3-win_amd64` | CPython 3.11–3.14; unprivileged reader/GUI checks |
| macOS ARM64 | `cp311-abi3-macosx_*_arm64` | CPython 3.11–3.14 |
| macOS Intel | `cp311-abi3-macosx_*_x86_64` | CPython 3.11–3.14 |
| Linux x86_64 | repaired `cp311-abi3-manylinux_2_28_x86_64` | CPython 3.11–3.14; headless GUI/reader checks |

Windows and macOS are the supported desktop platforms. Linux native wheels
require glibc 2.28 or newer; runtime wheels may impose further OS requirements,
and system Qt libraries such as `libegl1` may be needed. Interactive Linux GUI
support is not claimed. ARM Linux, Windows ARM64/32-bit, musl Linux, PyPy and
free-threaded CPython are outside this matrix. The exact release must pass its
hosted jobs before these configured checks count as acceptance.

## Native development

Use the project venv and an explicitly installed platform C toolchain/SDK.
From the root:

```console
python -m pip install --only-binary apsw ./native
python -m pip install --only-binary=:all: -e ".[dev]"
python -m build native --outdir dist
python -m pytest --no-cov tests/datahandling/test_trusted_live.py tests/datahandling/test_readonly.py
```

Native source archives are built/validated in CI and retained in its
`qplotter-native-linux-x86_64` artifact, but never uploaded to PyPI. Building an
explicit archive path with `python -m pip install --only-binary apsw <native-sdist-path>`
is also a developer-only source build. See
[CONTRIBUTING](../CONTRIBUTING.md#native-development-explicit-compiler-workflow)
for rebuild instructions. Retain the stable ABI, physical read-only main/WAL
handles, exact-SHM protections and coordinated native/APSW/SQLite pins.

## Package Validation

Build local release artifacts from a clean source tree with:

```console
python scripts/validate_distribution.py --check-clean
python -m build native --outdir dist
python -m build
```

Validate the contents and testability of the built artifacts, then validate
their metadata with:

```console
python scripts/validate_distribution.py dist
python -m twine check dist/*
```

For a platform job that builds both wheels without sdists, use
`python scripts/validate_distribution.py --wheel-only dist`; it performs the
same wheel-content and installed-helper smoke checks without requiring sdists.
Both local wheels remain explicit pip arguments throughout installation, so
neither qPlot distribution is fetched from PyPI.

CI also uses `--install-only --with-dev-tools --audit-path
dist/wheel-installation-audit.py dist` to install the pair into the selected
test interpreter. It first force-installs both explicit wheels with
`--no-index --no-deps`, then resolves dependencies with both wheel paths still
explicit. Missing or duplicate artifacts fail before installation. An isolated
import audit checks module origins before loading the extension and compares
installed package locations, versions, and every runtime file's SHA-256 with
the supplied wheels. The full and compatibility pytest jobs
disable the development `pythonpath=src` setting and run the same audit inside
each pytest process through `QPLOT_CI_WHEEL_AUDIT`. A checkout import, changed
extension, or older installed release fails validation.

Compiler-free acceptance uses separate fresh virtual environments for each
installation path:

```console
python scripts/validate_compiler_free_install.py --mode wheel dist
python scripts/validate_compiler_free_install.py --mode editable dist
```

The ordinary path installs both explicit wheels and the complete runtime
dependency set with `--only-binary=:all: --no-cache-dir`. The editable path
extracts this run's application sdist outside the checkout and installs it with
`--editable <source>[dev]`, the explicit prebuilt native wheel, and the same
binary-only dependency policy. Pip's install reports must show wheels for every
dependency, and `pip check` must succeed. No native sdist is installed. Download
and build caches are disabled; neither environment inherits installed packages.

C and C++ compiler selection and PATH commands deliberately fail. A venv `.pth`
audit hook also rejects absolute compiler paths, including in pip's isolated
build subprocesses. Negative controls prove these guards work. Pip's optional
`rustc --version` user-agent probes are rejected too; those failed probes are
the sole permitted additional compiler attempts. Any build invocation fails
acceptance.

Both paths launch the actual installed `qplot` command offscreen and observe
its visible MainWindow inside the real Qt event loop before requesting normal
shutdown. They then run the retained installed-package, trusted-WAL-reader,
concurrent-writer/checkpoint, database-protection and shutdown smoke checks from
outside the repository. For editable acceptance, an existing application
Python module is changed in the extracted source and a fresh interpreter must
observe the changed function without another install/build command. The native
wheel hashes, file identity and modification time must remain unchanged across
the edit, startup and reader checks. The temporary source is discarded afterward.

The source distribution deliberately contains all source tests and fixtures,
shared `tests/conftest.py`, project metadata, documentation, developer scripts,
application source, schemas, and CSV resources. The separate native sdist
contains its build metadata, package, C source, and SQLite ABI header. Virtual
environments, build output, caches, coverage output, bytecode, `.DS_Store`, and
compiled `.so`, `.pyd`, `.dll`, and `.dylib` files are excluded. The artifact
validator independently rejects those compiled files in an sdist, so native
wheels must build the extension from source.

The native wheel contains the `cp311-abi3` extension
`qplot_native._trusted_vfs_native` and is platform-specific. The application
wheel is `py3-none-any`. The application requires exactly native version 1.0.0;
both distributions pin APSW 3.53.4.0. Keep these dependencies, the validator's
pins, and the Python/native SQLite 3.53.4 and source-ID checks coordinated.
The reader also rejects an incompatible native distribution version, SQLite
version, or VFS name before loading the extension. The C implementation and
its database protections are unchanged. The setuptools native build selects
C11 mode for MSVC and preserves `Py_LIMITED_API=0x030B0000`, `py_limited_api=True`,
and the `cp311` wheel setting.

The Stage 3 supervisor, protocol, and helper target are ordinary installed
Python modules under `qplot.datahandling`. Setuptools package discovery includes
them in every wheel, while the source-distribution policy's `graft src` includes
them in every sdist. The artifact validator compares both artifacts with the
source inventory, so omitting any helper module fails validation. The helper is
not a console entry point; the supervisor starts its package-level target with
the multiprocessing `spawn` context.

The Stage 4 fixed-query adapter and application read broker are ordinary
installed Python modules as well. The same exact source-inventory comparison
requires them in every wheel, while `graft src` and `graft tests` require all
Stage 4 modules, tests, and fixtures in the sdist. Mypy lists new application
modules explicitly. The Qt-free `_shutdown_supervisor` module is packaged by
the same inventory and runs behind the existing `qplot` entry point: the
entry-point process establishes complete qPlot-tree containment before the GUI
imports Qt or starts helpers. POSIX uses a dedicated child session/process group
anchored by its retained unreaped leader. The installed
`_windows_shutdown_job` module instead uses retained handles and atomically
assigns the suspended GUI to a kill-on-close Job Object as part of process
creation. Stage 4 changes no console entry-point declaration or version
metadata.
Its trusted-first run list, refresh, progressive metadata, and selected plain
view share one broker-owned supervisor. Snapshot fallback remains a narrow
initial-open outcome; ordinary fallback selection is basic-only and starts no
selected-detail worker or additional selected-detail snapshot. Fallback
metadata and retained preview paths can still create private snapshots.
Explicit plot/CSV snapshots remain deferred DataSet consumers. Existing
fallback previews remain separately permitted. The Stage 5A scheduler and
Stage 5B coordinator, derived-query, rendering, and cache modules are normal
installed Python package files and are covered by the same wheel/sdist inventory
comparison and explicit mypy list. The packaged Stage 5C
`TrustedDerivedQtBridge` connects that backend to installed Qt code. Trusted sessions
use that one owner-thread bridge and one bounded coordinator for progressive
metadata, thumbnails, and previews; the competing legacy producers remain
disabled. The bridge module and Stage 5C source regressions participate in the
same exact wheel/sdist inventory and configured mypy checks.

CI builds the application wheel and sdist once on Python 3.11. A separate native
build matrix produces one `cp311-abi3` wheel each for Windows x64, macOS ARM64,
macOS Intel, and Linux x86_64. Linux uses cibuildwheel's `manylinux_2_28` image
and auditwheel repair rather than publishing an ordinary Ubuntu
`linux_x86_64` wheel. cibuildwheel also audits the stable ABI. The Linux build
produces the native sdist. These jobs upload immutable artifacts; every consumer
downloads the application and matching native artifact from the same workflow
run, without choosing another run, branch, or release. For an application-only
release the native matrix instead downloads the exact pinned PyPI version for
each platform, verifies its JSON listing and downloaded SHA-256, and passes
those files through the same validation/upload/consumer pipeline. No native
wheel is rebuilt or republished on that path. The native sdist is still built
for explicit source-build validation, which may use a compiler in CI.

The retained Linux package job compares both sdists and wheels with the source
inventory, runs the extracted sdists' complete test suite in an isolated venv,
and exercises the installed wheel pair in another venv. Linux coverage still
runs the complete suite. The Windows and ARM64 macOS full suites retain both
partitions on Python 3.11 and 3.14; their middle versions use the existing
compatibility subset. Intel macOS runs that subset on all four versions, with
additional Linux compatibility checks on 3.11 and 3.13. The compatibility
subset retains the database protections, concurrent-writer tests and focused
Stage 4 coverage. All four platforms run the complete installed-package smoke
on each advertised CPython version, 3.11, 3.12, 3.13 and 3.14, reusing the same
platform's stable-ABI wheel across versions. Free-threaded CPython and PyPy are
not part of the advertised CPython support matrix.

The validator writes a real `if __name__ == "__main__"`-guarded smoke script
into a temporary directory outside the repository and runs it with isolated
Python. The retained direct Stage 3 exercise keeps a temporary WAL writer open
and uses `TrustedLiveReaderSupervisor` to read committed WAL-only data, has that
same persistent helper observe a later writer commit, checks that mutating SQL
is rejected, rejects a nine-column oversized live SQLite row with the distinct
result-limit error before it can cross IPC, and proves that the same helper
remains usable after clean rollback and both length-limit restorations. It also
injects uncertain per-statement limit restoration, proves that exact helper is
retired, and requires a later explicit query to use a fresh incarnation. The
smoke confirms writer checkpoint progress and verifies around each reader-only
phase that the main database, WAL, and rollback journal were not changed; SHM
coordination changes are allowed. Running a real guarded script makes the
package-level helper target and Windows `spawn` startup part of the test rather
than relying on source-tree imports or a `python -c` main module.

The same outside-repository script separately exercises the installed Stage 4
application adapter against a writer-held, current-schema QCoDeS-shaped WAL
fixture. It reads a basic run page and cheap metadata, observes a later commit
through the same broker-owned helper, keeps accepted source A alive until source
B has opened and read its basic page, then proves A retires while B remains
usable. It also permits writer checkpoint progress and audits protected
artifacts through the application boundary. The source test suite additionally
proves GUI publication ordering and atomic pending/active database switching.
This extends rather than replaces the direct supervisor smoke.

The installed-wheel Stage 5B smoke additionally uses the application broker and
actual coordinator against a writer-held current-schema QCoDeS WAL database. It
requires self-contained bounded prefix metadata/PNG publication before any
cheap/expensive detail enrichment and after an append, cache files only under a
selected application-cache directory disjoint from the database directory,
writer commit and checkpoint progress between short transactions, protected
main/WAL/journal artifacts unchanged during reader-only intervals, and no
coordinator worker or helper left after shutdown.

The installed-wheel Stage 5C smoke drives the packaged Qt bridge offscreen. It
checks cheap-baseline-first binding, queued GUI-owner polling, equivalent cache
hit/miss publication without duplicate cache writes, suppression of the legacy
detail and preview workers, preview decoding and size invalidation, and bounded
bridge/coordinator shutdown. It also reselects the active binding and verifies
that the same coordinator, two timers, and cached preview remain operational.
Source-tree acceptance additionally proves bounded decoded-preview ownership,
byte-based eviction, absence of hidden thumbnails for large lists, exact
preview-only cache replay, selected/visible/remaining progression, and stale
database/helper-generation rejection. A real writer-held QCoDeS WAL database
exercises append, completion, new-run reconciliation, helper restart, active
database switching and reselection, later writer commits, PASSIVE/TRUNCATE
checkpoints, cache separation, and protected-artifact audits. Hosted Linux,
ARM64 macOS, Intel macOS, and unprivileged Windows checks remain required for
the exact final revision.

The installed-wheel checks also invoke the installed shutdown launcher rather
than substituting a source-tree module. They exercise both the unchanged CLI
launcher and the actual public `qplot.run()` dedicated-launcher boundary. They
require a normal GUI child status of 17 to pass through unchanged even when a
foreign POSIX `waitpid(-1)` thread collects the API launcher, verify
non-destructive signal and protocol-EOF mappings, force a deadline while the GUI
holds the GIL, and run a real stuck `TrustedLiveReaderSupervisor` helper inside
the API boundary. They also deliver a first caller control-flow exception and a
second exception after the temporary SIGINT guard's real installation side
effect while that helper is stuck. A separate installed concurrency probe races
two requesters through worker lookup, creation, assignment, and start and
requires one worker, one start, one sending thread, and one exact cancellation
frame. The smoke then kills a disposable API caller after launcher `READY`.
Authenticated cancellation and caller-channel EOF must remove the launcher,
GUI, and helper while preserving the exact first caller exception and its
`SystemExit.code`. The acquisition
caller and its active writer must survive,
commit afterward, and retain its unrelated sentinel. Launcher completion
must mean that the GUI and its complete contained helper tree have disappeared;
printing that `app.exec()` returned is not process-termination evidence. The
same smoke keeps an external sentinel and WAL writer outside the group or Job
Object and proves they survive containment cleanup, continue making writer
progress, and leave the protected main database, WAL, and rollback journal
unchanged under the reader policy (with only exact SHM coordination changes
permitted). A separate delegation check calls the actual installed `qplot`
entry point.

The Windows test suites and installed-wheel smoke run under a disposable local
standard account because the trusted reader rejects the hosted runner's
elevated token; CI separately verifies that elevated context is refused.

The 32 MiB pre-yield result-row figure exercised by these checks is a logical
Python-object/payload accounting envelope for standard APSW conversion, not an
allocator-reserved-byte or process-RSS limit; the separate raw text/blob-payload
bound is 8 MiB.

Every artifact receives a `twine check` before upload. CI itself does not publish
to PyPI; the separate release workflow calls it before publishing. Cross-platform acceptance applies
only after the Linux, ARM64 macOS, Intel macOS, and unprivileged Windows jobs
have all passed for the exact source revision. Workflow configuration alone is
not acceptance; hosted results for a newly changed revision remain pending
until all four exact-revision jobs finish successfully.
Local regression tests and workflow lint checks establish configuration
correctness; they do not establish hosted platform acceptance. Hosted results
for changes to this workflow must be reported separately, with the tested
revision and platform/Python jobs, after the workflow actually runs.

## Release automation

`.github/workflows/release.yml` runs on two tag forms, each requiring the exact
application version from `pyproject.toml`:

| Tag | Native handling | PyPI uploads |
| --- | --- | --- |
| `native-and-app/v1.6.0b2` | Build/validate native `1.0.0` at this revision | Native wheels first, then application wheel/sdist |
| `v1.6.0b3` (example later version) | Fetch/validate the application's exact published native pin | Application wheel/sdist only |

The workflow calls the entire CI workflow at the tagged revision. Failure of
any build, static check, source-distribution suite, database-protection suite,
concurrent-writer test, installed smoke or compiler-free installation job
prevents staging and publishing. Both ordinary and editable acceptance run on
every platform/Python matrix entry, including standard-user Windows execution.

After success, `scripts/release.py stage` selects only the single application
wheel/sdist and exactly four repaired/stable-ABI native wheels. The validated
native sdist is excluded from the upload directory. A SHA-256 receipt binds
each upload set to the repository, tagged commit and workflow run; publishing
jobs recheck the exact file set and bytes. They download immutable artifacts
from that run and do no builds. OIDC permission exists only in the two separate
publishing jobs in this non-reusable workflow. The PyPA action uploads with
Trusted Publishing and produces attestations, with no stored upload token.

For a paired release, native publication must succeed first. The next job
checks PyPI's complete native file listing, rejects sdists/yanked files, and
redownloads **every platform wheel**, comparing bytes with the validated
hashes. A fresh Linux compiler-blocked environment then installs the application
with the actual published native wheel resolved by pip from PyPI's simple
index (binary-only, uncached, pinned version and hash checked), starts qPlot and exercises trusted
live reading and the retained safety/writer smokes outside the checkout.
Only success unlocks application publication; a final job verifies the
published application files against the validated hashes. Application-only
releases pass the same native verification gate without a native upload.

After application publication and byte verification, `public-installations`
runs on all four platforms and every advertised CPython version (3.11–3.14).
Each entry creates two fresh environments outside the original checkout.
The ordinary path runs `python -m pip install --only-binary=:all:
qplotter==1.6.0b2` against the public PyPI index with all runtime dependencies.
The editable path fetches the exact tested commit into a fresh checkout and
installs `.[dev]` with `qplotter-native==1.0.0` resolved from public PyPI.
Neither path installs a local release wheel; the validated artifacts are only
comparison receipts for the pip report and installed-file hashes. All runtime
and development dependencies must resolve to public wheels. Both paths disable
caches, reject compiler invocation, check version reporting and console commands,
start the real qPlot GUI, and exercise trusted live reading and database/writer
protections. Editable acceptance additionally observes a Python edit without
reinstalling and checks that the native binary's bytes, identity and timestamp
remain unchanged. Windows uses the standard-user wrapper for both paths.
Publication is irreversible; failures in these post-publication checks must
be reported and fixed in a new release, rather than described as acceptance.

### One-time account configuration

These settings must be created in the account/repository UIs before a release;
they have not been created by this change. The connected GitHub app confirmed
the repository is `lairdgrouplancaster/QCoDeS-Plotter` (public, default branch
`main`). Its available tools do not configure PyPI publishers or GitHub
environments. Follow the
[official publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)
and [pending-publisher instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

1. Sign into your release-owner PyPI account with verified email and 2FA. At
   [PyPI account publishing](https://pypi.org/manage/account/publishing/), add
   **two pending GitHub publishers**, with these exact fields:

   | Field | Native publisher | Application publisher |
   | --- | --- | --- |
   | PyPI project name | `qplotter-native` | `qplotter` |
   | GitHub owner | `lairdgrouplancaster` | `lairdgrouplancaster` |
   | Repository | `QCoDeS-Plotter` | `QCoDeS-Plotter` |
   | Workflow filename | `release.yml` | `release.yml` |
   | Environment | `pypi-native` | `pypi-application` |

   Enter just `release.yml`, not its directory path or display name. Pending
   publishers create each project on first successful upload. Once a project
   exists, its Publishing settings should show the corresponding active
   publisher. If the name has meanwhile been taken, stop and resolve ownership
   or rename/coordinately update the packages before release.

2. In [repository environments](https://github.com/lairdgrouplancaster/QCoDeS-Plotter/settings/environments),
   create `pypi-native` and `pypi-application`. Add a release maintainer as a
   **required reviewer** for both. Restrict deployment branches/tags using
   selected **tag** rules: `native-and-app/v*` for native, and both
   `native-and-app/v*` and `v*` for application. Do not add branch rules.
   Restrict admin bypass; prevent self-review if a second maintainer will
   approve. No environment or repository PyPI token secrets are needed.

3. Protect `main` with the existing `Required checks` CI status. Restrict tag
   creation/update/deletion for `v*` and `native-and-app/v*` to release
   maintainers using repository rulesets. Merge/review this workflow and the
   release version before tagging; never move a published tag. If migrating
   from token publishing, revoke/remove the old upload tokens/secrets.

### First release commands

Complete the account setup and merge the reviewed packaging/release changes
to `main`. With application `1.6.0b2` and native `1.0.0`, use the configured
Git remote/credentials:

```console
git fetch origin
git switch main
git pull --ff-only origin main
python scripts/validate_distribution.py --check-clean
git tag -a native-and-app/v1.6.0b2 -m "qPlot 1.6.0b2 with native reader 1.0.0"
git push origin refs/tags/native-and-app/v1.6.0b2
```

Use the activated project venv for Python; on macOS set
`MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache` for Python commands that may
import Matplotlib. The tag push is the publication trigger. Review its hosted
CI results, then approve the `pypi-native` environment. Confirm native
verification succeeds before approving `pypi-application`. The final PyPI
verification and all `public-installations` jobs must succeed before announcing the release. No GitHub Release
object is needed to trigger publication; use the connected GitHub app for any
subsequent release/PR operations if supported, never `gh`.

### Later Python-only releases

Update the application version (for example to `1.6.0b3`) and changelog, keep
the exact native/APSW pins when still compatible, and merge the changes. Do
not change native sources/ABI under an existing native version. Then:

```console
git fetch origin
git switch main
git pull --ff-only origin main
python scripts/validate_distribution.py --check-clean
git tag -a v1.6.0b3 -m "qPlot 1.6.0b3"
git push origin refs/tags/v1.6.0b3
```

The native release must already contain all four non-yanked compatible wheels.
CI installs those exact PyPI files and validates the new application against
them. Only `pypi-application` needs approval. When native/SQLite compatibility
changes, bump the native version and every coordinated application/validator/
reader pin, then use a new `native-and-app/v<application-version>` tag.

Uploads deliberately do not use `skip-existing`. If a job fails after native
publication, rerun the failed jobs in GitHub Actions; successful upload jobs
must not be rerun. Do not delete/reuse a PyPI version or tag. If a native upload
was only partial, stop and resolve that incomplete release before proceeding;
the application remains blocked. Once the complete native release is verified,
an application-only tag for the still-unpublished application version can also
recover a failed paired release after a new validation run.

### Local and hosted results

Local tests and actionlint validate release configuration and failure gates.
Local macOS acceptance establishes that machine/Python's reader and startup
behavior. No release tag or PyPI upload has been made as part of preparation;
hosted release/installation results and account configuration remain pending.
Report actual successful hosted jobs with their tagged revision and matrix
separately from these configured checks.

## Release checklist

Before creating a tagged release:

1. Update the version in `pyproject.toml`. For prereleases, use the PEP 440
   package form, such as `1.6.0b1`, with a matching Git tag prefixed by `v`,
   such as `v1.6.0b3` for an application-only release or
   `native-and-app/v1.6.0b2` for a paired release.
2. Move relevant entries from `CHANGELOG.md`'s Unreleased section into the new
   release section.
3. Run `python -m ruff check .`.
4. Run `python -m mypy`.
5. Run `python -m pytest`.
6. Run `python scripts/validate_distribution.py --check-clean`.
7. Run `python -m build native --outdir dist` and `python -m build`.
8. Run `python scripts/validate_distribution.py dist`.
9. Run `python -m twine check dist/*`.
10. Confirm the validator ran the extracted sdist tests, the installed direct
    trusted-WAL-helper smoke, the Stage 4 application-adapter smoke, and the
    installed Stage 5B live-WAL backend and Stage 5C Qt-bridge smokes.
11. Confirm unprivileged Windows x64, ARM64 macOS, Intel macOS, and Linux
    installed-wheel jobs passed on Python 3.11–3.14 for the exact source, and
    use the repaired manylinux artifact for Linux publication.
12. Run the manual GUI check from `CONTRIBUTING.md`.
13. Confirm README install and compatibility notes still match the release.
14. Confirm publisher/environment settings, push the appropriate tag using the
    commands above, review both validation and PyPI verification, then announce
    user-facing changes. GitHub release creation is optional and uses the
    connected GitHub app when available.

## Future Options

Standalone desktop installers may help non-Python users, but they should be
treated as a separate distribution target. The installer needs explicit testing
for QCoDeS database access, Qt platform plugins, themes, configuration files,
and the `qplot-cfg` helper.
