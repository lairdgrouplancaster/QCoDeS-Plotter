# qPlot native reader

`qplotter-native` supplies `qplot_native._trusted_vfs_native` for qPlot.
Build it independently from this directory with `python -m build`.
The extension retains the CPython 3.11 stable ABI (`cp311-abi3`) and requires
the exact APSW 3.53.4.0 / SQLite 3.53.4 runtime and SQLite source ID checked by
the application and C implementation. Building this distribution requires a
C compiler; installing its platform wheel does not.

PyPI releases contain wheels only for Windows x64, macOS ARM64/Intel and Linux
x86_64 (repaired manylinux_2_28). Unsupported platforms get a missing compatible
distribution rather than a source-build fallback. Native source archives remain
available as CI artifacts. For explicit native development, install a platform
C compiler/SDK and run `python -m pip install --only-binary apsw ./native` from
the repository root, or build this directory with `python -m build native`.
Application contributors use the prebuilt wheel and `python -m pip install
--only-binary=:all: -e ".[dev]"` from the root without a compiler.

Keep its APSW dependency, the application pin and reader compatibility checks,
and the C SQLite ABI header coordinated when releasing a new native version.
