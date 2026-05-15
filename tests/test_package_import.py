import subprocess
import sys


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
