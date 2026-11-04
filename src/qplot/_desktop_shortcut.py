"""Create and remove only qPlot-owned shortcuts on the user's desktop."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from qplot._auxiliary_paths import ensure_safe_auxiliary_path


def desktop_directory() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Desktop"
    if sys.platform != "win32":
        raise ValueError("Desktop shortcuts support macOS and Windows only.")
    import ctypes
    import uuid

    # Resolve the user's actual Desktop, including OneDrive/folder redirection.
    # https://learn.microsoft.com/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath
    folder_id = ctypes.create_string_buffer(uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641").bytes_le)
    get_folder = ctypes.windll.shell32.SHGetKnownFolderPath
    get_folder.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                          ctypes.POINTER(ctypes.c_void_p)]
    get_folder.restype = ctypes.c_long
    free = ctypes.windll.ole32.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    pointer = ctypes.c_void_p()
    try:
        result = get_folder(folder_id, 0, None, ctypes.byref(pointer))
        if result < 0 or not pointer.value:
            raise OSError(f"Could not locate the Desktop (HRESULT 0x{result & 0xffffffff:08x})")
        return Path(ctypes.wstring_at(pointer.value))
    finally:
        free(pointer)


def create_desktop_shortcut(destination: Path) -> dict | None:
    """Return an ownership record after exclusive creation; never overwrite."""
    path = desktop_directory() / ("qPlot.app" if sys.platform == "darwin" else "qPlot.lnk")
    if path == destination:
        return None  # A custom macOS destination is already the Desktop app.
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"{path} already exists. Move it or use --no-desktop-shortcut.")
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        path.symlink_to(destination, target_is_directory=True)
        return {"path": str(path), "target": str(destination), "kind": "symlink"}
    payload = (destination / "qPlot.lnk").read_bytes()
    with path.open("xb") as stream:
        try:
            stream.write(payload)
        except BaseException:
            stream.close()
            path.unlink()  # Only the new, exclusively created file is ours.
            raise
    return {"path": str(path), "kind": "file", "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest()}


def remove_desktop_shortcut(record: dict | None) -> None:
    """Leave moved, edited or replaced shortcuts and their targets untouched."""
    if record is None:
        return
    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
        raise ValueError("Invalid desktop shortcut record.")
    path = Path(record["path"])
    if not path.is_absolute():
        raise ValueError("Invalid desktop shortcut path.")
    if record.get("kind") == "symlink" and path.name == "qPlot.app":
        if path.is_symlink() and os.readlink(path) == record.get("target"):
            path.unlink()
    elif record.get("kind") == "file" and path.name == "qPlot.lnk":
        if path.is_symlink() or not path.is_file():
            return
        if path.stat().st_size != record.get("size"):
            return
        ensure_safe_auxiliary_path(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() == record.get("sha256"):
            path.unlink()
    else:
        raise ValueError("Invalid desktop shortcut kind.")
