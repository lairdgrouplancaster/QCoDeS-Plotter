# qPlot native reader

`qcodes-plotter-native` supplies `qplot_native._trusted_vfs_native` for qPlot.
Build it independently from this directory with `python -m build`.
The extension retains the CPython 3.11 stable ABI (`cp311-abi3`) and requires
the exact APSW 3.53.4.0 / SQLite 3.53.4 runtime and SQLite source ID checked by
the application and C implementation. Building this distribution requires a
C compiler; installing its platform wheel does not.

Keep its APSW dependency, the application pin and reader compatibility checks,
and the C SQLite ABI header coordinated when releasing a new native version.
