"""Safe filename selection and publication for user-requested exports."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from typing import Any

from PyQt6 import QtWidgets as qtw


def normalize_export_path(filename: str, required_suffix: str) -> str:
    """Return an absolute export path with ``required_suffix`` exactly once."""
    if not required_suffix.startswith("."):
        required_suffix = f".{required_suffix}"
    if not filename.casefold().endswith(required_suffix.casefold()):
        filename = f"{filename}{required_suffix}"
    return os.path.abspath(filename)


def choose_export_path(
    parent: qtw.QWidget,
    *,
    caption: str,
    suggested_path: str,
    name_filter: str,
    required_suffix: str,
    replace_title: str,
    file_description: str,
) -> str | None:
    """Choose, normalize, and approve the exact path that will be replaced."""
    dialog_options = (
        qtw.QFileDialog.Option.DontConfirmOverwrite
        | qtw.QFileDialog.Option.DontUseNativeDialog
    )
    filename = qtw.QFileDialog.getSaveFileName(
        parent,
        caption,
        suggested_path,
        name_filter,
        options=dialog_options,
    )[0]
    if not filename:
        return None

    filename = normalize_export_path(filename, required_suffix)
    if not os.path.exists(filename):
        return filename

    reply = qtw.QMessageBox.question(
        parent,
        replace_title,
        f"{filename} already exists.\n\nReplace the existing {file_description}?",
        qtw.QMessageBox.StandardButton.Yes | qtw.QMessageBox.StandardButton.No,
        qtw.QMessageBox.StandardButton.No,
    )
    if reply != qtw.QMessageBox.StandardButton.Yes:
        return None
    return filename


def write_export_atomically(
    filename: str,
    writer: Callable[[str], Any],
    *,
    before_publish: Callable[[], Any] | None = None,
) -> bool:
    """Stage an export beside its target and atomically publish it on success."""
    destination = os.path.abspath(filename)
    directory = os.path.dirname(destination)
    suffix = os.path.splitext(destination)[1] or ".tmp"
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(destination)}.",
        suffix=suffix,
        dir=directory,
    )
    os.close(descriptor)

    try:
        if writer(temporary_path) is False:
            return False

        with open(temporary_path, "rb") as temporary:
            os.fsync(temporary.fileno())

        if before_publish is not None:
            before_publish()
        os.replace(temporary_path, destination)
        temporary_path = ""
        return True
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
