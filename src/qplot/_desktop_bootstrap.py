"""Standalone launcher payload, copied at setup so broken installs show errors.

Keep this module standard-library-only until cache and diagnostic paths exist.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path


def show_error(message: str) -> None:
    message = f"{message}\n\nReinstall qPlot if necessary, then remove the old launcher and recreate it:\npython -m qplot.desktop remove\npython -m qplot.desktop setup\nUse --destination for a custom launcher location."
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "qPlot could not start", 16)
    elif sys.platform == "darwin":
        subprocess.run(
            ["/usr/bin/osascript", "-e", "on run argv",
             "-e", 'display alert "qPlot could not start" message (item 1 of argv)',
             "-e", "end run", message], check=False,
        )
    else:
        print(message, file=sys.stderr)


def main() -> int:
    log_name = None
    try:
        record = json.loads(Path(__file__).with_name("installation.json").read_text(encoding="utf-8"))
        if os.path.normcase(sys.prefix) != os.path.normcase(record["prefix"]):
            raise RuntimeError("The recorded Python environment has moved or changed.")
        # Child supervisors must also discover this installation independently
        # of shell import overrides.
        os.environ.pop("PYTHONPATH", None)
        os.environ.pop("PYTHONHOME", None)
        # These are qPlot-owned cache/log directories, never measurement paths.
        root = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "qPlot"
                if sys.platform == "win32" else Path.home() / "Library/Caches/qPlot")
        cache = root / "matplotlib"
        cache.mkdir(parents=True, exist_ok=True)
        os.environ["MPLCONFIGDIR"] = str(cache)
        logs = root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        # One log per invocation also captures supervisor/GUI child diagnostics.
        import tempfile

        descriptor, log_name = tempfile.mkstemp(prefix="launch-", suffix=".log", dir=logs)
        with os.fdopen(descriptor, "w", encoding="utf-8", buffering=1) as log:
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            sys.stdout = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
            sys.stderr = os.fdopen(os.dup(2), "w", encoding="utf-8", buffering=1)
            try:
                import qplot

                if str(Path(qplot.__file__).absolute()) != record["package"]:
                    raise RuntimeError("The qPlot installation has moved or changed.")
                from qplot._auxiliary_paths import ensure_safe_auxiliary_path

                # Matplotlib may replace an invalid cache file. Refuse an
                # existing database or sidecar even at this auxiliary location.
                for entry in cache.rglob("*"):
                    if entry.is_symlink():
                        raise RuntimeError("The Matplotlib cache contains a symlink.")
                    if entry.is_file():
                        ensure_safe_auxiliary_path(entry)
                from qplot.__main__ import run

                result = int(run())
                if result:
                    show_error(f"qPlot exited with status {result}. See {log_name}")
                    return 79  # Native error already displayed.
                return result
            except Exception:
                traceback.print_exc()
                raise
    except Exception as error:
        detail = f"\nSee {log_name}" if log_name else ""
        show_error(f"Python could not launch the installed qPlot: {error}{detail}")
        return 79  # Native error already displayed.


if __name__ == "__main__":
    raise SystemExit(main())
