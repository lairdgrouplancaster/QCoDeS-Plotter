"""Native extension build configuration for qPlot."""

import sys

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


class TrustedVFSBuildExt(build_ext):
    """Select the C language mode required by the trusted VFS extension."""

    def build_extensions(self) -> None:
        if sys.platform == "win32" and self.compiler.compiler_type == "msvc":
            for extension in self.extensions:
                compile_args = list(extension.extra_compile_args or ())
                if "/std:c11" not in compile_args:
                    compile_args.append("/std:c11")
                extension.extra_compile_args = compile_args

        super().build_extensions()


setup(
    cmdclass={"build_ext": TrustedVFSBuildExt},
    ext_modules=[
        Extension(
            "qplot.datahandling._trusted_vfs_native",
            sources=["src/qplot/datahandling/_trusted_vfs_native.c"],
            include_dirs=["src/qplot/datahandling"],
            depends=["src/qplot/datahandling/_trusted_vfs_sqlite_abi.h"],
            define_macros=[("Py_LIMITED_API", "0x030B0000")],
            export_symbols=["sqlite3_qplot_trusted_vfs_init"],
            libraries=["advapi32"] if sys.platform == "win32" else [],
            py_limited_api=True,
        )
    ],
    options={"bdist_wheel": {"py_limited_api": "cp311"}},
)
