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
