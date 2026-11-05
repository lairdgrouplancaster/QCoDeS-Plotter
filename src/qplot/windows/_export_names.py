"""Resolve export filenames before approving their filesystem destinations."""

import os
import re
from dataclasses import dataclass


def suggested_export_filename(filename: str) -> str:
    """Replace path-hostile characters in a suggestion, retaining Unicode names.

    This is display/naming policy only. Destination approval and atomic
    publication still belong to ``_export_paths``.
    """
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in filename)


def normalize_export_path(filename: str, required_suffix: str) -> str:
    """Return an absolute export path with ``required_suffix`` exactly once."""
    if not required_suffix.startswith("."):
        required_suffix = f".{required_suffix}"
    if not filename.casefold().endswith(required_suffix.casefold()):
        filename = f"{filename}{required_suffix}"
    return os.path.abspath(filename)


@dataclass(frozen=True, slots=True)
class ExportSuffixPolicy:
    """Validated formats and filename rules for one export dialog."""

    suffixes: tuple[str, ...]
    default_suffix: str

    def __post_init__(self) -> None:
        suffixes = tuple(
            suffix.casefold() if suffix.startswith(".") else f".{suffix.casefold()}"
            for suffix in self.suffixes
        )
        if not suffixes:
            raise ValueError("At least one export suffix must be allowed.")
        default_suffix = (
            self.default_suffix.casefold()
            if self.default_suffix.startswith(".")
            else f".{self.default_suffix.casefold()}"
        )
        if default_suffix not in suffixes:
            raise ValueError("The default export suffix must also be allowed.")
        object.__setattr__(self, "suffixes", suffixes)
        object.__setattr__(self, "default_suffix", default_suffix)

    def resolve(
        self,
        filename: str,
        selected_filter: str,
        *,
        match_selected_filter: bool = False,
    ) -> str:
        """Preserve explicit formats, or enforce a recognized selected filter.

        An unsupported extension is retained as part of the name before the
        chosen extension is appended. Only the first extension in a selected
        filter determines its format, matching the dialog's existing rules.
        """
        selected_suffix = os.path.splitext(filename)[1].casefold()
        selected_match = re.search(r"\*\.([A-Za-z0-9]+)\b", selected_filter)
        filtered_suffix = (
            f".{selected_match.group(1).casefold()}"
            if selected_match is not None else None
        )
        if match_selected_filter and filtered_suffix in self.suffixes:
            if selected_suffix in self.suffixes and selected_suffix != filtered_suffix:
                filename = f"{os.path.splitext(filename)[0]}{filtered_suffix}"
                selected_suffix = filtered_suffix
            elif selected_suffix not in self.suffixes:
                selected_suffix = filtered_suffix
                filename = f"{filename}{selected_suffix}"
        if selected_suffix not in self.suffixes:
            if filtered_suffix in self.suffixes:
                selected_suffix = filtered_suffix
            if selected_suffix not in self.suffixes:
                selected_suffix = self.default_suffix
            filename = f"{filename}{selected_suffix}"
        return os.path.abspath(filename)
