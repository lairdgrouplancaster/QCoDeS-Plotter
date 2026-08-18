"""Safe filename approval and publication for user-requested exports."""

from __future__ import annotations

import ntpath
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from PyQt6 import QtWidgets as qtw

from qplot.datahandling.file_identity import (
    SQLITE_SIDECAR_SUFFIXES,
    DatabaseFileIdentity,
    canonical_database_path,
    database_file_identity,
    logical_database_path,
)

_DATABASE_ARTIFACT_SUFFIXES = ("", *SQLITE_SIDECAR_SUFFIXES)


class UnsafeExportDestinationError(RuntimeError):
    """Raised when an export cannot safely use its selected destination."""


@dataclass(frozen=True, slots=True)
class ProtectedDatabaseFiles:
    """Filesystem names and instances owned by current or retained DB views."""

    logical_paths: frozenset[str] = frozenset()
    resolved_paths: frozenset[str] = frozenset()
    identities: frozenset[DatabaseFileIdentity] = frozenset()

    def merged(self, other: ProtectedDatabaseFiles) -> ProtectedDatabaseFiles:
        """Return the union of two protection observations."""
        return ProtectedDatabaseFiles(
            logical_paths=self.logical_paths | other.logical_paths,
            resolved_paths=self.resolved_paths | other.resolved_paths,
            identities=self.identities | other.identities,
        )


@dataclass(frozen=True, slots=True)
class ExportDestinationTransaction:
    """One exact export target approved against a stable filesystem view."""

    filename: str
    parent_path: str
    parent_identity: tuple[int, ...]
    target_signature: tuple[object, ...] | None
    replacement_confirmed: bool
    protected_database_files: ProtectedDatabaseFiles
    owner: object | None = field(default=None, compare=False, repr=False)
    existing_target_validator: Callable[[str], Any] | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    @property
    def target_existed(self) -> bool:
        """Return whether approval captured an existing target."""
        return self.target_signature is not None

    @property
    def protected_database_identities(self) -> frozenset[DatabaseFileIdentity]:
        """Compatibility view of approval-time protected file identities."""
        return self.protected_database_files.identities

    def __fspath__(self) -> str:
        return self.filename

    def __str__(self) -> str:
        return self.filename

    def validate(self, *, parent_fd: int | None = None) -> None:
        """Revalidate the exact approval without changing filesystem state."""
        _ensure_parent_is_unchanged(self, parent_fd=parent_fd)

        protected_files = self.protected_database_files.merged(
            collect_protected_database_files(self.owner)
        )
        current_signature = _target_signature(
            self.filename,
            parent_fd=parent_fd,
        )
        _ensure_target_is_not_protected(self.filename, protected_files)
        if current_signature != self.target_signature:
            raise UnsafeExportDestinationError(
                "The selected file changed while printing or exporting; "
                "it was not replaced."
            )
        if self.target_existed and not self.replacement_confirmed:
            raise UnsafeExportDestinationError(
                "Replacement of the existing export file was not confirmed."
            )
        _run_existing_target_validator(self, current_signature)


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
    existing_target_validator: Callable[[str], Any] | None = None,
) -> ExportDestinationTransaction | None:
    """Choose, normalize, and approve the exact path that may be replaced."""
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
    return approve_export_path(
        parent,
        filename,
        replace_title=replace_title,
        file_description=file_description,
        existing_target_validator=existing_target_validator,
    )


def choose_export_path_with_suffixes(
    parent: qtw.QWidget,
    *,
    caption: str,
    suggested_path: str,
    name_filter: str,
    allowed_suffixes: Iterable[str],
    default_suffix: str,
    replace_title: str,
    file_description: str,
) -> ExportDestinationTransaction | None:
    """Choose an export whose format may use one of several suffixes."""
    suffixes = tuple(
        suffix.casefold() if suffix.startswith(".") else f".{suffix.casefold()}"
        for suffix in allowed_suffixes
    )
    if not suffixes:
        raise ValueError("At least one export suffix must be allowed.")
    default_suffix = (
        default_suffix.casefold()
        if default_suffix.startswith(".")
        else f".{default_suffix.casefold()}"
    )
    if default_suffix not in suffixes:
        raise ValueError("The default export suffix must also be allowed.")

    dialog_options = (
        qtw.QFileDialog.Option.DontConfirmOverwrite
        | qtw.QFileDialog.Option.DontUseNativeDialog
    )
    filename, selected_filter = qtw.QFileDialog.getSaveFileName(
        parent,
        caption,
        suggested_path,
        name_filter,
        options=dialog_options,
    )
    if not filename:
        return None

    selected_suffix = os.path.splitext(filename)[1].casefold()
    if selected_suffix not in suffixes:
        selected_match = re.search(r"\*\.([A-Za-z0-9]+)\b", selected_filter)
        if selected_match is not None:
            filtered_suffix = f".{selected_match.group(1).casefold()}"
            if filtered_suffix in suffixes:
                selected_suffix = filtered_suffix
        if selected_suffix not in suffixes:
            selected_suffix = default_suffix
        filename = f"{filename}{selected_suffix}"

    return approve_export_path(
        parent,
        os.path.abspath(filename),
        replace_title=replace_title,
        file_description=file_description,
    )


def approve_export_path(
    parent: qtw.QWidget,
    filename: str,
    *,
    replace_title: str,
    file_description: str,
    existing_target_validator: Callable[[str], Any] | None = None,
) -> ExportDestinationTransaction | None:
    """Capture one selected path and explicitly confirm its exact target."""
    destination = _capture_export_destination(
        parent,
        os.path.abspath(filename),
        existing_target_validator=existing_target_validator,
    )
    if not destination.target_existed:
        return destination

    reply = qtw.QMessageBox.question(
        parent,
        replace_title,
        f"{destination.filename} already exists.\n\n"
        f"Replace the existing {file_description}?",
        qtw.QMessageBox.StandardButton.Yes | qtw.QMessageBox.StandardButton.No,
        qtw.QMessageBox.StandardButton.No,
    )
    if reply != qtw.QMessageBox.StandardButton.Yes:
        return None
    return replace(destination, replacement_confirmed=True)


def prepare_export_destination(
    owner: object | None,
    filename: str,
    *,
    replacement_confirmed: bool = False,
    required_suffix: str | None = None,
    existing_target_validator: Callable[[str], Any] | None = None,
) -> ExportDestinationTransaction:
    """Approve a caller-supplied path without supplying implicit consent."""
    if required_suffix is not None:
        filename = normalize_export_path(filename, required_suffix)
    else:
        filename = os.path.abspath(filename)
    destination = _capture_export_destination(
        owner,
        filename,
        existing_target_validator=existing_target_validator,
    )
    if destination.target_existed and not replacement_confirmed:
        raise UnsafeExportDestinationError(
            "Replacement of the existing export file was not confirmed."
        )
    return replace(
        destination,
        replacement_confirmed=bool(
            replacement_confirmed and destination.target_existed
        ),
    )


def write_export_atomically(
    destination: ExportDestinationTransaction | str | os.PathLike[str],
    writer: Callable[[str], Any],
    *,
    before_publish: Callable[[], Any] | None = None,
) -> bool:
    """Stage beside an approved target and publish without widening consent.

    Raw paths remain accepted only for a target that is absent at approval.
    Replacing an existing target requires an ``ExportDestinationTransaction``
    whose confirmation flag came from the user-facing approval step.
    """
    if not isinstance(destination, ExportDestinationTransaction):
        destination = prepare_export_destination(None, os.fspath(destination))

    parent_fd = _open_parent_directory(destination)
    stage_fd: int | None = None
    stage_path = ""
    stage_name = ""
    stage_identity: tuple[int, ...] | None = None
    try:
        # This is deliberately the last operation before staging begins.
        destination.validate(parent_fd=parent_fd)
        suffix = os.path.splitext(destination.filename)[1] or ".tmp"
        stage_fd, stage_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(destination.filename)}.",
            suffix=suffix,
            dir=destination.parent_path,
        )
        stage_name = os.path.basename(stage_path)
        stage_identity = _stage_identity(os.fstat(stage_fd))
        _verify_owned_stage(
            destination,
            stage_path,
            stage_name,
            stage_identity,
            parent_fd=parent_fd,
        )

        if writer(stage_path) is False:
            return False

        _verify_owned_stage(
            destination,
            stage_path,
            stage_name,
            stage_identity,
            parent_fd=parent_fd,
        )
        os.fsync(stage_fd)
        os.close(stage_fd)
        stage_fd = None

        if before_publish is not None:
            before_publish()

        # A callback can replace the private pathname just as it can replace
        # the public target. Revalidate the owned stage after every callback,
        # then leave destination validation immediately adjacent to publish.
        _verify_owned_stage(
            destination,
            stage_path,
            stage_name,
            stage_identity,
            parent_fd=parent_fd,
        )
        if destination.target_existed:
            _replace_approved_target(
                destination,
                stage_path,
                stage_name,
                stage_identity,
                parent_fd=parent_fd,
            )
            stage_path = ""
        else:
            _publish_new_target(
                destination,
                stage_path,
                stage_name,
                stage_identity,
                parent_fd=parent_fd,
            )
            stage_path = ""

        _sync_parent_directory(parent_fd)
        return True
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        if stage_path:
            _unlink_owned_stage(
                destination,
                stage_path,
                stage_name,
                stage_identity,
                parent_fd=parent_fd,
            )
        if parent_fd is not None:
            os.close(parent_fd)


def collect_protected_database_files(
    owner: object | None,
) -> ProtectedDatabaseFiles:
    """Collect current and retained DB artifacts without opening SQLite."""
    identities: set[DatabaseFileIdentity] = set()
    database_paths: set[str] = set()

    def add_identity(candidate: object) -> None:
        if isinstance(candidate, tuple) and len(candidate) in {2, 3}:
            try:
                hash(candidate)
            except TypeError:
                return
            identities.add(candidate)  # type: ignore[arg-type]

    def add_identities(candidates: object) -> None:
        add_identity(candidates)
        if isinstance(candidates, (frozenset, set, list)):
            for candidate in candidates:
                add_identity(candidate)

    def add_path(candidate: object) -> None:
        if candidate:
            try:
                database_paths.add(os.path.abspath(os.fspath(candidate)))
            except (TypeError, ValueError):
                pass

    def add_source(candidate: object) -> None:
        if candidate is None:
            return
        if isinstance(candidate, (str, os.PathLike)):
            add_path(candidate)
            return
        add_identity(candidate)
        for attribute in ("database_identity", "identity"):
            try:
                add_identity(getattr(candidate, attribute, None))
            except RuntimeError:
                pass
        for attribute in (
            "sidecar_identities",
            "sqlite_sidecar_identities",
            "protected_database_identities",
        ):
            try:
                add_identities(getattr(candidate, attribute, None))
            except RuntimeError:
                pass
        for attribute in (
            "database_path",
            "logical_path",
            "path_to_db",
            "resolved_database_path",
            "resolved_path",
        ):
            try:
                add_path(getattr(candidate, attribute, None))
            except RuntimeError:
                pass
        try:
            cache = getattr(candidate, "cache", None)
            cached_dataset = getattr(cache, "_dataset", None)
            add_path(getattr(cached_dataset, "path_to_db", None))
        except RuntimeError:
            pass

    application = qtw.QApplication.instance()
    owners: list[object] = []
    if owner is not None:
        owners.append(owner)
    if application is not None:
        owners.extend(qtw.QApplication.topLevelWidgets())
        owners.extend(qtw.QApplication.allWidgets())

    seen_owners: set[int] = set()
    for current_owner in owners:
        if id(current_owner) in seen_owners:
            continue
        seen_owners.add(id(current_owner))
        _collect_owner_database_sources(current_owner, add_source, add_identity)

    logical_paths: set[str] = set()
    resolved_paths: set[str] = set()
    for database_path in database_paths:
        logical_base = logical_database_path(database_path)
        resolved_base = canonical_database_path(logical_base)
        for base_path in {logical_base, resolved_base}:
            for suffix in _DATABASE_ARTIFACT_SUFFIXES:
                artifact_path = f"{base_path}{suffix}"
                logical_paths.add(logical_database_path(artifact_path))
                resolved_paths.add(canonical_database_path(artifact_path))
                identity = database_file_identity(artifact_path)
                if identity is not None:
                    identities.add(identity)

    return ProtectedDatabaseFiles(
        logical_paths=frozenset(logical_paths),
        resolved_paths=frozenset(resolved_paths),
        identities=frozenset(identities),
    )


def _collect_owner_database_sources(
    owner: object,
    add_source: Callable[[object], None],
    add_identity: Callable[[object], None],
) -> None:
    """Inspect the bounded set of qPlot state holders used by its windows."""
    add_source(owner)
    for attribute in (
        "_dataset_key",
        "_selected_dataset_key",
        "_loaded_database_instance",
        "_database_refresh_instance",
        "_database_load_worker",
        "_database_refresh_worker",
        "_database_detail_worker",
        "_database_expensive_detail_worker",
        "_test_database_generation_worker",
        "worker",
    ):
        try:
            add_source(getattr(owner, attribute, None))
        except RuntimeError:
            pass

    for holder_name in ("_dataset_holder", "dataset_holder"):
        try:
            holder = getattr(owner, holder_name, None)
            items = getattr(holder, "items", None)
            if callable(items):
                for dataset_key, dataset_handle in items():
                    add_source(dataset_key)
                    add_source(dataset_handle)
        except (RuntimeError, TypeError):
            pass

    try:
        load_state = getattr(owner, "_database_load_state", None)
    except RuntimeError:
        load_state = None
    if isinstance(load_state, Mapping):
        add_source(load_state.get("load_instance"))
        add_identity(load_state.get("load_identity"))
        add_source(load_state.get("abspath"))

    try:
        replacement_state = getattr(owner, "_test_database_replacement_state", None)
    except RuntimeError:
        replacement_state = None
    add_source(replacement_state)
    add_source(getattr(replacement_state, "original_instance", None))

    for container_name in ("_plot_workers", "_workers"):
        try:
            container = getattr(owner, container_name, None)
            if isinstance(container, Mapping):
                sources: Iterable[object] = container.values()
            elif isinstance(container, (set, frozenset, list, tuple)):
                sources = container
            else:
                continue
            for source in tuple(sources):
                add_source(source)
        except RuntimeError:
            pass

    try:
        file_textbox = getattr(owner, "fileTextbox", None)
        displayed_path = getattr(file_textbox, "text", None)
        if callable(displayed_path):
            add_source(displayed_path())
    except RuntimeError:
        pass


def _capture_export_destination(
    owner: object | None,
    filename: str,
    *,
    existing_target_validator: Callable[[str], Any] | None,
) -> ExportDestinationTransaction:
    filename = os.path.abspath(filename)
    _ensure_windows_export_path_is_unambiguous(filename)
    parent_path = os.path.dirname(filename)
    try:
        parent_stat = os.stat(parent_path)
    except OSError as err:
        raise UnsafeExportDestinationError(
            "The selected export folder is not available."
        ) from err
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise UnsafeExportDestinationError(
            "The selected export parent is not a folder."
        )
    if not _directory_identity_is_stable(parent_stat):
        raise UnsafeExportDestinationError(
            "The selected export folder has no stable filesystem identity."
        )

    target_signature = _target_signature(filename)
    protected_files = collect_protected_database_files(owner)
    _ensure_target_is_not_protected(filename, protected_files)
    destination = ExportDestinationTransaction(
        filename=filename,
        parent_path=parent_path,
        parent_identity=_directory_identity(parent_stat),
        target_signature=target_signature,
        replacement_confirmed=False,
        protected_database_files=protected_files,
        owner=owner,
        existing_target_validator=existing_target_validator,
    )
    _run_existing_target_validator(destination, target_signature)
    return destination


def _ensure_target_is_not_protected(
    filename: str,
    protected_files: ProtectedDatabaseFiles,
) -> None:
    logical_path = logical_database_path(filename)
    resolved_path = canonical_database_path(filename)
    protected_paths = (
        protected_files.logical_paths | protected_files.resolved_paths
    )
    protected_aliases = {
        _filesystem_alias_key(protected_path)
        for protected_path in protected_paths
    }
    if (
        logical_path in protected_paths
        or resolved_path in protected_paths
        or _filesystem_alias_key(logical_path) in protected_aliases
        or _filesystem_alias_key(resolved_path) in protected_aliases
    ):
        raise UnsafeExportDestinationError(
            "The selected export path refers to an input database or one of "
            "its SQLite sidecars."
        )

    target_identity = database_file_identity(filename)
    if (
        target_identity is not None
        and target_identity in protected_files.identities
    ):
        raise UnsafeExportDestinationError(
            "The selected export path refers to an input database file."
        )


def _filesystem_alias_key(filename: str) -> str:
    """Return a conservative key for case/Unicode-equivalent path aliases.

    ``normcase`` alone does not model case-insensitive macOS volumes, and
    APFS/HFS resolve canonically equivalent NFC/NFD names to the same entry.
    Treating those spellings as aliases on every platform may reject a rare
    legitimate export on a case-sensitive filesystem, but it cannot let an
    export create an input database's currently absent SQLite sidecar.
    """
    return unicodedata.normalize("NFC", filename).casefold()


def _directory_identity(file_stat: os.stat_result) -> tuple[int, ...]:
    """Return fields stable while entries inside a directory are modified."""
    birthtime_ns = int(getattr(file_stat, "st_birthtime_ns", 0) or 0)
    if not birthtime_ns:
        birthtime = getattr(file_stat, "st_birthtime", None)
        if birthtime is not None:
            birthtime_ns = round(float(birthtime) * 1_000_000_000)
    return (
        int(file_stat.st_dev),
        int(file_stat.st_ino),
        int(file_stat.st_mode),
        birthtime_ns,
    )


def _directory_identity_is_stable(file_stat: os.stat_result) -> bool:
    """Return whether a directory observation can detect replacement."""
    return bool(
        int(getattr(file_stat, "st_ino", 0) or 0)
        or int(getattr(file_stat, "st_birthtime_ns", 0) or 0)
        or getattr(file_stat, "st_birthtime", None)
    )


def _target_signature(
    filename: str,
    *,
    parent_fd: int | None = None,
) -> tuple[object, ...] | None:
    """Capture an existing ordinary target without following its final link."""
    try:
        if parent_fd is None:
            file_stat = os.lstat(filename)
        else:
            file_stat = os.stat(
                os.path.basename(filename),
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
    except FileNotFoundError:
        return None
    except OSError as err:
        raise UnsafeExportDestinationError(
            "The selected export file could not be inspected safely."
        ) from err

    if stat.S_ISLNK(file_stat.st_mode):
        raise UnsafeExportDestinationError(
            "Symbolic-link export destinations are not supported."
        )
    if not stat.S_ISREG(file_stat.st_mode):
        raise UnsafeExportDestinationError(
            "The selected export destination is not a regular file."
        )
    if int(file_stat.st_nlink) != 1:
        raise UnsafeExportDestinationError(
            "Hard-linked export destinations are not supported."
        )

    inode = int(getattr(file_stat, "st_ino", 0) or 0)
    stable_identity: DatabaseFileIdentity | None
    if inode:
        stable_identity = (int(file_stat.st_dev), inode)
    else:
        stable_identity = database_file_identity(filename)
    if stable_identity is None:
        raise UnsafeExportDestinationError(
            "The selected export file has no stable filesystem identity."
        )

    return (
        int(file_stat.st_dev),
        inode,
        int(file_stat.st_mode),
        int(file_stat.st_nlink),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(file_stat.st_ctime_ns),
        stable_identity,
    )


def _run_existing_target_validator(
    destination: ExportDestinationTransaction,
    current_signature: tuple[object, ...] | None,
) -> None:
    validator = destination.existing_target_validator
    if current_signature is None or validator is None:
        return
    try:
        result = validator(destination.filename)
    except UnsafeExportDestinationError:
        raise
    except Exception as err:
        raise UnsafeExportDestinationError(
            "The existing export file could not be validated safely."
        ) from err
    if result is False:
        raise UnsafeExportDestinationError(
            "The existing export file did not match the selected format."
        )
    if _target_signature(destination.filename) != current_signature:
        raise UnsafeExportDestinationError(
            "The selected export file changed while it was being inspected."
        )


def _ensure_parent_is_unchanged(
    destination: ExportDestinationTransaction,
    *,
    parent_fd: int | None,
) -> None:
    try:
        current_parent = os.stat(destination.parent_path)
    except OSError as err:
        raise UnsafeExportDestinationError(
            "The selected folder changed while printing or exporting."
        ) from err
    if (
        not stat.S_ISDIR(current_parent.st_mode)
        or not _directory_identity_is_stable(current_parent)
        or _directory_identity(current_parent) != destination.parent_identity
    ):
        raise UnsafeExportDestinationError(
            "The selected folder changed while printing or exporting."
        )
    if parent_fd is not None:
        try:
            opened_parent = os.fstat(parent_fd)
        except OSError as err:
            raise UnsafeExportDestinationError(
                "The selected export folder is no longer available."
            ) from err
        if _directory_identity(opened_parent) != destination.parent_identity:
            raise UnsafeExportDestinationError(
                "The selected folder changed while printing or exporting."
            )


def _ensure_windows_export_path_is_unambiguous(filename: str) -> None:
    """Reject Win32 spellings that alias or attach streams to another file."""
    if os.name != "nt":
        return

    _drive, tail = ntpath.splitdrive(filename)
    components = tuple(
        component
        for component in re.split(r"[\\/]", tail)
        if component
    )
    reserved_names = {"con", "prn", "aux", "nul", "conin$", "conout$"}
    reserved_names.update(f"com{number}" for number in range(1, 10))
    reserved_names.update(f"lpt{number}" for number in range(1, 10))

    for component in components:
        if component.endswith((" ", ".")) or ":" in component:
            raise UnsafeExportDestinationError(
                "Ambiguous Windows export paths and alternate data streams "
                "are not supported."
            )
        device_name = component.split(".", 1)[0].casefold()
        if device_name in reserved_names:
            raise UnsafeExportDestinationError(
                "Windows device-name export destinations are not supported."
            )


def _open_parent_directory(
    destination: ExportDestinationTransaction,
) -> int | None:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        parent_fd = os.open(destination.parent_path, flags)
    except OSError as err:
        if os.name == "nt":
            return None
        raise UnsafeExportDestinationError(
            "The selected export folder could not be anchored safely."
        ) from err
    try:
        _ensure_parent_is_unchanged(destination, parent_fd=parent_fd)
    except Exception:
        os.close(parent_fd)
        raise
    return parent_fd


def _stage_identity(file_stat: os.stat_result) -> tuple[int, ...] | None:
    inode = int(getattr(file_stat, "st_ino", 0) or 0)
    birthtime_ns = int(getattr(file_stat, "st_birthtime_ns", 0) or 0)
    if not birthtime_ns:
        birthtime = getattr(file_stat, "st_birthtime", None)
        if birthtime is not None:
            birthtime_ns = round(float(birthtime) * 1_000_000_000)
    if not inode and not birthtime_ns:
        return None
    return (
        int(file_stat.st_dev),
        inode,
        stat.S_IFMT(int(file_stat.st_mode)),
        birthtime_ns,
    )


def _stage_path_stat(
    stage_path: str,
    stage_name: str,
    *,
    parent_fd: int | None,
) -> os.stat_result | None:
    try:
        if parent_fd is None:
            return os.lstat(stage_path)
        return os.stat(
            stage_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError:
        return None


def _stage_database_identity(
    stage_path: str,
    stage_stat: os.stat_result,
) -> DatabaseFileIdentity | None:
    inode = int(getattr(stage_stat, "st_ino", 0) or 0)
    if inode:
        return (int(stage_stat.st_dev), inode)
    return database_file_identity(stage_path)


def _stage_refers_to_protected_database(
    destination: ExportDestinationTransaction,
    stage_path: str,
    stage_stat: os.stat_result,
) -> bool:
    protected_files = destination.protected_database_files.merged(
        collect_protected_database_files(destination.owner)
    )
    stage_database_identity = _stage_database_identity(stage_path, stage_stat)
    return (
        stage_database_identity is None
        or stage_database_identity in protected_files.identities
    )


def _verify_owned_stage(
    destination: ExportDestinationTransaction,
    stage_path: str,
    stage_name: str,
    stage_identity: tuple[int, ...] | None,
    *,
    parent_fd: int | None,
) -> None:
    if stage_identity is None:
        raise UnsafeExportDestinationError(
            "The private export staging file has no stable identity."
        )
    _ensure_parent_is_unchanged(destination, parent_fd=parent_fd)
    stage_stat = _stage_path_stat(
        stage_path,
        stage_name,
        parent_fd=parent_fd,
    )
    if stage_stat is None or _stage_identity(stage_stat) != stage_identity:
        raise UnsafeExportDestinationError(
            "The private export staging file changed unexpectedly."
        )
    if not stat.S_ISREG(stage_stat.st_mode) or int(stage_stat.st_nlink) != 1:
        raise UnsafeExportDestinationError(
            "The private export staging target is not an unlinked regular file."
        )
    if _stage_refers_to_protected_database(
        destination,
        stage_path,
        stage_stat,
    ):
        raise UnsafeExportDestinationError(
            "The private export staging file refers to an input database."
        )


def _replace_approved_target(
    destination: ExportDestinationTransaction,
    stage_path: str,
    stage_name: str,
    stage_identity: tuple[int, ...] | None,
    *,
    parent_fd: int | None,
) -> None:
    # Keep the target/protection check in the publication helper so no qPlot
    # callback or format-specific code can sit between it and the rename.
    destination.validate(parent_fd=parent_fd)
    _verify_owned_stage(
        destination,
        stage_path,
        stage_name,
        stage_identity,
        parent_fd=parent_fd,
    )
    if not destination.replacement_confirmed:
        raise UnsafeExportDestinationError(
            "Replacement of the existing export file was not confirmed."
        )
    if parent_fd is None:
        os.replace(stage_path, destination.filename)
    else:
        os.replace(
            stage_name,
            os.path.basename(destination.filename),
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )


def _publish_new_target(
    destination: ExportDestinationTransaction,
    stage_path: str,
    stage_name: str,
    stage_identity: tuple[int, ...] | None,
    *,
    parent_fd: int | None,
) -> None:
    """Publish with atomic no-clobber semantics for an absent approval."""
    destination.validate(parent_fd=parent_fd)
    _verify_owned_stage(
        destination,
        stage_path,
        stage_name,
        stage_identity,
        parent_fd=parent_fd,
    )
    try:
        if os.name == "nt":
            # Win32 rename is atomic and refuses an existing destination. It
            # also supports FAT/exFAT volumes where hard links are unavailable.
            os.rename(stage_path, destination.filename)
        elif parent_fd is None:
            os.link(stage_path, destination.filename, follow_symlinks=False)
        else:
            os.link(
                stage_name,
                os.path.basename(destination.filename),
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
    except FileExistsError as err:
        raise UnsafeExportDestinationError(
            "The selected export file appeared before publication; it was "
            "not replaced."
        ) from err
    except OSError as err:
        raise UnsafeExportDestinationError(
            "The new export could not be published with no-clobber semantics."
        ) from err

    _unlink_owned_stage(
        destination,
        stage_path,
        stage_name,
        stage_identity,
        parent_fd=parent_fd,
    )


def _unlink_owned_stage(
    destination: ExportDestinationTransaction,
    stage_path: str,
    stage_name: str,
    stage_identity: tuple[int, ...] | None,
    *,
    parent_fd: int | None,
) -> None:
    """Unlink only the exact private entry created by this transaction."""
    if stage_identity is None:
        return
    current_stat = _stage_path_stat(
        stage_path,
        stage_name,
        parent_fd=parent_fd,
    )
    if current_stat is None or _stage_identity(current_stat) != stage_identity:
        return
    if _stage_refers_to_protected_database(
        destination,
        stage_path,
        current_stat,
    ):
        return
    try:
        if parent_fd is None:
            current_parent = os.stat(destination.parent_path)
            if _directory_identity(current_parent) != destination.parent_identity:
                return
            os.unlink(stage_path)
        else:
            os.unlink(stage_name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError:
        # Leaving an untrusted or unverifiable private entry is safer than
        # unlinking a pathname that may now refer to another file instance.
        pass


def _sync_parent_directory(parent_fd: int | None) -> None:
    if parent_fd is None:
        return
    try:
        os.fsync(parent_fd)
    except OSError:
        pass
