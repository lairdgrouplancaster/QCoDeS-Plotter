"""Observations for opt-in workflow benchmarks; no timing assertions in CI."""

import hashlib
import importlib.metadata
import os
import platform
import statistics
from pathlib import Path


def protected_source_state(database):
    """Hash protected files outside timed sections; never open them writable."""
    result = {}
    for suffix in ("", "-wal", "-journal"):
        path = Path(str(database) + suffix)
        if not path.exists():
            result[suffix] = None
            continue
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        info = path.stat()
        result[suffix] = {
            "sha256": digest, "bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
            "device": info.st_dev, "inode": info.st_ino,
        }
    return result


def environment():
    import qplot

    source_root = Path(qplot.__file__).parent
    fingerprint = hashlib.sha256()
    for path in sorted(source_root.rglob("*.py")):
        fingerprint.update(path.relative_to(source_root).as_posix().encode())
        fingerprint.update(path.read_text(encoding="utf-8-sig").encode())
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", "native"),
        "application_sha256": fingerprint.hexdigest(),
        "packages": {name: importlib.metadata.version(name) for name in (
            "qcodes", "numpy", "PyQt6", "pyqtgraph", "qplotter-native", "psutil",
        )},
    }


def memory_sample():
    """Current RSS, not process-lifetime peak; keep helper memory separate."""
    import psutil

    process = psutil.Process()
    children = []
    for child in process.children(recursive=True):
        try:
            children.append({"pid": child.pid, "rss_bytes": child.memory_info().rss})
        except psutil.NoSuchProcess:
            pass
    return {"rss_bytes": process.memory_info().rss, "children": children,
            "threads": process.num_threads(),
            "handles_or_fds": process.num_handles() if os.name == "nt" else process.num_fds()}


def timing_summary(values):
    if not values:
        return {"count": 0, "median_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)
    # Nearest-rank percentile also has a clear meaning for small samples.
    rank = max(0, (95 * len(ordered) + 99) // 100 - 1)
    return {"count": len(values), "median_ms": statistics.median(values),
            "p95_ms": ordered[rank], "max_ms": ordered[-1]}
