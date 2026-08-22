import os
import subprocess
import sys
from pathlib import Path

from PyQt6 import QtGui

EXPECTED_SCREENSHOTS = {
    "qplot-main-window.png": (900, 500),
    "qplot-line-plot.png": (700, 450),
    "qplot-heatmap.png": (700, 450),
    "qplot-color-scale-dialog.png": (450, 450),
}


def _sampled_image_colours(image):
    step_x = max(1, image.width() // 40)
    step_y = max(1, image.height() // 40)
    colours = []

    for y in range(0, image.height(), step_y):
        for x in range(0, image.width(), step_x):
            colour = image.pixelColor(x, y)
            colours.append((colour.red(), colour.green(), colour.blue()))

    return colours


def _assert_screenshot_has_content(path, minimum_size):
    image = QtGui.QImage(str(path))
    assert not image.isNull(), f"{path.name} could not be loaded"
    assert image.width() >= minimum_size[0]
    assert image.height() >= minimum_size[1]

    colours = _sampled_image_colours(image)
    unique_colours = set(colours)
    luminance_values = [
        0.2126 * red + 0.7152 * green + 0.0722 * blue
        for red, green, blue in colours
    ]

    assert len(unique_colours) > 3, f"{path.name} appears blank"
    assert max(luminance_values) - min(luminance_values) > 20, (
        f"{path.name} has too little contrast"
    )


def test_demo_screenshot_workflow_generates_nonblank_images(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    asset_dir = tmp_path / "assets"
    work_dir = tmp_path / "work"
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    env = os.environ.copy()
    env["QPLOT_DEMO_ASSET_DIR"] = str(asset_dir)
    env["QPLOT_DEMO_WORKDIR"] = str(work_dir)
    env["QPLOT_DEMO_VERIFY_CLEANUP"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["MPLCONFIGDIR"] = str(work_dir / "matplotlib")
    env["PYTHONPATH"] = str(repo_root / "src")
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    env["TMPDIR"] = str(temp_dir)

    result = subprocess.run(
        [sys.executable, "scripts/capture_demo_screenshots.py"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "PermissionError" not in result.stderr
    assert not list(temp_dir.glob("qplot-readonly-*"))
    for filename, minimum_size in EXPECTED_SCREENSHOTS.items():
        path = asset_dir / filename
        assert path.exists(), f"{filename} was not generated"
        assert path.stat().st_size > 1000
        _assert_screenshot_has_content(path, minimum_size)
