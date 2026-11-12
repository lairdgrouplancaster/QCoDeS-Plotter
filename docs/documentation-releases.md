# Documentation and release screenshots

Documentation builds use the four approved PNG files committed in
`docs/assets/`. Read the Docs installs only `requirements/docs.txt`; it does
not install qPlot, Qt, QCoDeS, Pillow or the native reader and never generates
screenshots. MkDocs copies the PNGs into the website. Its `exclude_docs` list
keeps internal code reviews, branding proposals, provenance JSON, databases,
logs, Python scripts and archives out of the site. Repository-only links are
resolved by `scripts/docs_hooks.py` at the documentation's Git revision. The
build hook also verifies that the site contains the exact approved PNG bytes.

## Every release preparation cycle

1. Prepare the application version, changelog and candidate source on a release
   preparation branch. Pushes and PR operations require the maintainer's
   authorisation. Keep the native source and its version consistent with the
   application's exact native dependency pin.
2. Run **Prepare release documentation** on that branch, entering the intended
   application version. The workflow checks out the dispatch's exact commit,
   builds the native package from that candidate, installs its application in
   an isolated venv and captures the real application offscreen. It uses
   Python 3.12 on Ubuntu 24.04 and the rendering dependencies in
   `requirements/screenshots.txt`. It has read-only repository permissions,
   no publishing credentials and performs no repository writes.
3. Require the entire run to succeed: application/data/render/shutdown checks,
   PNG validation, source freshness and a strict documentation build. Download
   the `qplot-release-screenshots-<run>-<attempt>` artefact. Include **all five
   files**, the four PNGs and `qplot-screenshots.json`, under `docs/assets/` in
   the release preparation PR, even if the PNG bytes did not change. A new
   generation record is required for every preparation cycle.
4. Visually review all four images at full size in the PR. Check readable text,
   complete controls and axes, the expected line and heatmap, the colour scale,
   and absence of personal paths or warnings. Automated checks cannot prove
   that every label is readable or control is aesthetically correct. Review
   the generation record and its workflow run as well; it is evidence, not a
   cryptographic attestation or a substitute for review.
5. Require **Documentation and image checks** on the PR, alongside the existing
   **Required checks**. Ordinary PR checks validate committed PNGs, references
   and the website without requiring screenshot freshness for every code edit.
   Release approval additionally requires the successful preparation run and
   image review. Merge the approved images and record with the intended source.
6. Before creating any tag, check out the final candidate and run the pre-tag
   verification below. A changed fingerprint requires regeneration and another
   review. Do not tag a commit that omits the reviewed set. Retain the existing
   packaging, full-test, manual GUI, protected-tag and PyPI approval checks in
   [the release checklist](distribution.md#release-checklist).

The workflow creates neither a branch nor a PR. A maintainer includes its
artefact in the release preparation PR using normal authorised Git operations
and the connected GitHub app for PR operations, never `gh`. No automatic commit
to `main`, tag, merge or publication occurs. The existing `release.yml` remains
unchanged. Repository protection and the release maintainer enforce the
pre-tag review gate; this separate workflow cannot prevent an authorised tag
push that bypasses those instructions.

### Initial workflow bootstrap

GitHub must first discover `workflow_dispatch` on the default branch. Review
and merge this infrastructure change before attempting the first hosted
dispatch. Then select the release preparation branch in **Run workflow**.
Bootstrap infrastructure review does not itself prepare a release. The first
release still needs a new successful preparation run, its five files in a
reviewed PR, and final pre-tag verification. Configure the new documentation
status as a required PR check through repository settings if desired; the
workflow does not change those settings.

## Local generation and pre-tag verification

Use the project venv. For example, on macOS/Linux, first install the candidate
native package and application with the capture tools:

The examples use the required macOS Matplotlib cache path. On Linux, use a
consistent writable `MPLCONFIGDIR`, such as `/tmp/qplot-matplotlib-cache`;
the hosted Linux workflow uses the runner's temporary directory.

```console
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/python -m pip install ./native -e . -r requirements/screenshots.txt
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/python scripts/capture_demo_screenshots.py --expected-version 1.7.0
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/python scripts/screenshot_support.py validate
```

Generation uses a private configuration, fixed synthetic values/dates/GUIDs,
UTC, British locale, light theme, Fusion style, DejaVu Sans from the pinned
Matplotlib package, 96 DPI and scale 1. The existing four filenames are retained.
It waits for current selection and synchronised plot publication, verifies the
displayed data and invokes synchronous widget rendering. Bounded polling is
used only to observe readiness, never to guess how long rendering takes.
The worker has a total 90-second timeout. All four images are staged; only
successful generation, validation, database protection and application shutdown
allow replacement of the approved set. Normal write failures restore previous
files. Replacing several files cannot be atomic across power loss; record
hashes detect an interrupted installation. POSIX worker process groups contain
descendants on timeout; Windows uses process-tree termination for a live worker.

After review and merge, on a clean final candidate, run:

```console
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/python scripts/screenshot_support.py verify-release --require-clean --generation-id RUN_ID/ATTEMPT
```

Use the generation ID recorded in the approved PR (a local generation uses
`local/<UUID>`). Verification checks the application version, input fingerprint,
all four PNG hashes/dimensions, references and successful application checks.
It does **not** require the generation SHA to equal the final commit SHA:
committing generated assets and merging legitimately change the commit.
It checks the current checkout; the maintainer must verify that this is the
exact commit they intend to tag and that the ID belongs to this preparation
cycle. It does not create a tag or verify remote reviews. A failed check must
be resolved before tagging; do not work around it by editing the record.

### Fingerprint and provenance

`qplot-screenshot-inputs-v1` hashes a sorted JSON mapping of repo-relative file
names to SHA-256 content hashes. Its inventory uses Git's tracked plus
non-ignored untracked files, omitting deleted files and ignoring build/cache
output. UTF-8 text without NUL bytes is normalised from CRLF to LF before
hashing, so Git's checkout line-ending conversion does not invalidate identical
source. Binary input hashes and the reviewed output PNG hashes use exact bytes.
It covers **all** `src/qplot/` application sources/resources, `native/`
sources, `pyproject.toml`, `requirements/screenshots.txt`, both screenshot
Python scripts and `.github/workflows/release-preparation.yml`. See
`INPUT_PATHS` in `scripts/screenshot_support.py` for the exact scope. This
conservative scope makes application, native, settings, resource, dependency
specification and capture environment changes require regeneration. Generated
screenshots and `qplot-screenshots.json` are excluded. Documentation prose,
website dependencies and unrelated tests can change without invalidating it.

The record includes the source commit and dirty-state flag, intended application
version, workflow run/attempt/repository, unique generation ID, fingerprint,
image hashes/dimensions, application check results, Python/OS/Qt versions, font
and native binary hashes, and the complete resolved Python package inventory.
Direct rendering/data dependencies are pinned; transitive packages and hosted
runner updates may still vary and are recorded. If a relevant resolved
dependency or rendering environment is changed deliberately, regenerate and
review rather than copying a previous record. The fingerprint checks repository
inputs and dependency specifications; pre-tag verification does not require
the maintainer's computer to reproduce the capture machine's environment.
Independent captures are not compared pixel by pixel. Image hashes identify
the exact reviewed files, not a universal rendering baseline. Visual review
remains mandatory across platforms and font/rendering updates.

The existing source-distribution GUI smoke test uses `--smoke-test` to exercise
the installed application when Git metadata is unavailable. That mode requires
a separate output directory and writes a schema-0 test record which release
verification rejects. It cannot update the documentation assets and must never
be used for release preparation.

During implementation on macOS with the pinned rendering libraries, two
independent captures produced pixel-identical versions of all four images.
An earlier comparison exposed unfinished main-window thumbnails; capture now
waits for both thumbnails and detail publication before rendering that window.
Cross-platform equivalence has not been established. Qt's offscreen plugin
emits harmless capability/font-alias warnings on this platform; these are not
application errors and do not replace the mandatory visual review.

## MkDocs and Read the Docs versions

Build locally with documentation-only dependencies:

```console
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/python -m pip install -r requirements/docs.txt
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/python -m mkdocs build --strict
```

Import/connect the GitHub repository in Read the Docs, use `.readthedocs.yaml`,
and enable its webhook. Keep `latest` on `main` for development documentation.
Activate release versions from their Git tags in the Read the Docs dashboard:
both `v<application-version>` (application-only) and
`native-and-app/v<application-version>` (paired native/application) identify the
corresponding source and approved screenshots. The paired tag's version is the
**application** version, not the native package version. Read the Docs may
normalise a slash-containing tag into a URL slug; select/check the original Git
identifier in the version settings rather than constructing its URL by hand.
Use the version selector/dashboard URL to link it. Configure version activation
rules for both tag forms or activate each explicitly, and check which stable
version is selected when using paired tags. Do not rewrite tags to suit a slug.

Release tags must include this configuration to build with it; historical tags
are not retrofitted or moved. Development and activated release versions build
independently from their own committed images. A tag webhook may publish
documentation before PyPI validation/publication finishes or even if it fails:
Read the Docs publication is independent of the PyPI workflow. Maintainers
must check PyPI success separately before announcing availability and may keep
a documentation version hidden until it is ready. No remote settings are
changed by this implementation.

Configuration references: [MkDocs exclusions](https://www.mkdocs.org/user-guide/configuration/#exclude_docs),
[Read the Docs configuration](https://docs.readthedocs.com/platform/stable/config-file/v2.html)
and [version management](https://docs.readthedocs.com/platform/stable/versions.html).
