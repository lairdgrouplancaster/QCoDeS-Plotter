import sqlite3
import subprocess
import sys
from importlib.metadata import distribution, version
from importlib.resources import files

import qplot


def test_package_version_comes_from_installed_metadata():
    assert qplot.__version__ == version("qplot")


def test_console_scripts_are_declared():
    scripts = {
        entry_point.name: entry_point.value
        for entry_point in distribution("qplot").entry_points
        if entry_point.group == "console_scripts"
    }

    assert scripts["qplot"] == "qplot.__main__:run"
    assert scripts["qplot-cfg"] == "qplot.configuration.scripts:scripts"
    assert scripts["qplot-generate-db"] == "qplot.testdata:main"


def test_config_schema_is_packaged():
    assert files("qplot.configuration").joinpath("config_schema.json").is_file()


def test_import_qplot_does_not_import_window_modules():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, qplot; "
                "print('qplot.windows' in sys.modules); "
                "print(callable(qplot.run))"
            ),
        ],
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout.splitlines() == ["False", "True"]


def test_database_probe_keeps_heavy_dependencies_lazy(tmp_path):
    database_path = tmp_path / "probe.db"
    sqlite3.connect(database_path).close()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from qplot.datahandling.readonly import "
                "probe_read_only_database\n"
                "probe_read_only_database(sys.argv[1])\n"
                "heavy_modules = (\n"
                "    'qcodes', 'numpy', 'PyQt6', 'pyqtgraph', 'jsonschema',\n"
                "    'qplot.configuration.config',\n"
                "    'qplot.datahandling.LoadFromDB',\n"
                "    'qplot.datahandling.readSQL',\n"
                ")\n"
                "print([name for name in heavy_modules if name in sys.modules])"
            ),
            str(database_path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )

    assert result.stdout.strip() == "[]"


def test_lazy_package_exports_preserve_public_api():
    from qplot.configuration.config import config as direct_config
    from qplot.datahandling import LoadFromDB, readSQL

    assert qplot.config is direct_config
    datahandling = qplot.datahandling
    for name in datahandling.__all__:
        defining_module = (
            LoadFromDB if name.startswith("load_param_data") else readSQL
        )
        assert getattr(datahandling, name) is getattr(defining_module, name)
