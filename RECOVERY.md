# Stage 5C recovery

Recovered on 2026-09-23 from the committed Stage 5C baseline (`a606d4e`)
and the original local session patch records. The previous temporary worktree
under `/private/tmp` no longer existed.

This checkout lives permanently inside the original project at
`.recovery/stage5c`, on branch `recovery/stage5c`. The original Stage 4
checkout and its branch have been preserved. The project virtual environment
has an editable installation pointing to this recovery checkout.

From the original project directory, launch with:

```sh
MPLCONFIGDIR=/private/tmp/qplot-matplotlib-cache .venv-mac/bin/qplot
```

With that virtual environment activated, `qplot` uses this checkout too.

The recovered changes include the derived-work scheduling and scientific
rendering repairs, selected metadata full-value actions, bounded lazy Snapshot
browsing, default expansion of the station node, and square thumbnails.
The native reader is built by the editable installation; compiled extensions
are not committed.

To reinstall from the original project directory:

```sh
.venv-mac/bin/python -m pip install --no-deps --no-build-isolation -e .recovery/stage5c
```

Validation includes the restored automated regressions and a Qt display check
against `qplot_test_db_01_10mb.db`: ten square thumbnails and a preview for run 7,
with unchanged contents and timestamps for the protected database family.
