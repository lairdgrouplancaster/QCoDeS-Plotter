# Stage 5C recovery

Recovered on 2026-09-23 from the committed Stage 5C baseline (`a606d4e`)
and the original local session patch records. The previous temporary worktree
under `/private/tmp` no longer existed.

The recovered work was originally kept inside the project at
`.recovery/stage5c`, on branch `recovery/stage5c`, with the Mac editable
installation pointing to that separate checkout. Those changes were absent
from PR #77. They are now integrated into the main source tree on
`integrate-stage5c`, together with the subsequent release fixes.

For development, install from the repository root so `qplot` runs the source
being edited. On Windows, activate the project environment and run:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
qplot
```

On macOS, from the repository root:

```sh
source .venv-mac/bin/activate
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache python -m pip install -e ".[dev]"
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache qplot
```

Check the loaded source with `python -c "import qplot; print(qplot.__file__)"`.
It should point into this repository's `src/qplot`, not `.recovery/stage5c`.

The recovered changes include the derived-work scheduling and scientific
rendering repairs, selected metadata full-value actions, bounded lazy Snapshot
browsing, default expansion of the station node, and square thumbnails.
The native reader is built by the editable installation; compiled extensions
are not committed.

The original recovery validation included restored automated regressions and a Qt display check
against `qplot_test_db_01_10mb.db`: ten square thumbnails and a preview for run 7,
with unchanged contents and timestamps for the protected database family.
