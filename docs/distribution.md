# Distribution

This repository builds two distributions: the pure-Python application
`qcodes-plotter` at the root and `qcodes-plotter-native` in `native/`.
Application imports remain `qplot`, and native imports use `qplot_native`.
The distribution names are provisional.

The authoritative package version is `project.version` in `pyproject.toml`.
At runtime, `qplot.__version__` reads the installed `qcodes-plotter` metadata through
`importlib.metadata`.

## Current Install Path

Recommended user install for the latest full release:

```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.5.0
```

Recommended development install:

```console
python -m pip install --only-binary qcodes-plotter-native --find-links dist -e ".[dev]"
```

For this checkout, first obtain the compatible native platform wheel from the
same release and place it in `dist/`. Install both release wheels together with
`python -m pip install dist/*.whl`, or use the editable command above.
No C compiler is needed to edit the application. Native contributors may instead
run `python -m pip install ./native` using a C compiler. Historical release
commands above use the packaging that existed at those tags.

Application installs expose the `qplot`, `qplot-cfg`, and `qplot-generate-db` entry
points.

## Current Beta

The published beta is `1.6.0b1`. This checkout prepares the next beta,
`1.6.0b2`, using the PEP 440 normal form. After release validation, its GitHub
release tag should be `v1.6.0b2`; that tag has not been published yet.

Beta test install:

```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.6.0b1
```

## Current Release

The current release is `1.5.0`. The package metadata uses the PEP 440 form
`1.5.0`; the GitHub release tag should be `v1.5.0`.

Release install:

```console
python -m pip install git+https://github.com/lairdgrouplancaster/QCoDeS-Plotter.git@v1.5.0
```

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
run, without choosing another run, branch, or release.

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

Every artifact receives a `twine check` before upload. CI does not publish to
PyPI or attach artifacts to GitHub releases. Cross-platform acceptance applies
only after the Linux, ARM64 macOS, Intel macOS, and unprivileged Windows jobs
have all passed for the exact source revision. Workflow configuration alone is
not acceptance; hosted results for a newly changed revision remain pending
until all four exact-revision jobs finish successfully.
Local regression tests and workflow lint checks establish configuration
correctness; they do not establish hosted platform acceptance. Hosted results
for changes to this workflow must be reported separately, with the tested
revision and platform/Python jobs, after the workflow actually runs.

## Release Checklist

Before creating a tagged release:

1. Update the version in `pyproject.toml`. For prereleases, use the PEP 440
   package form, such as `1.6.0b1`, with a matching Git tag prefixed by `v`,
   such as `v1.6.0b1`.
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
14. Create a GitHub release from the tag and include user-facing changes.

## Future Options

PyPI publishing would make user installs simpler, but should wait until the
project has a clear release owner and versioning process. When that happens,
extend the package job into a protected tag-only publish workflow.

Standalone desktop installers may help non-Python users, but they should be
treated as a separate distribution target. The installer needs explicit testing
for QCoDeS database access, Qt platform plugins, themes, configuration files,
and the `qplot-cfg` helper.
