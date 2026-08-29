"""Bounded, non-executable disk cache for trusted Stage 5B payloads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import ntpath
import os
import re
import secrets
import sqlite3
import stat
import struct
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import cast

from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_derived_rendering import (
    TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES,
    DerivedPayload,
    TrustedDerivedRenderingError,
    validate_trusted_derived_payload,
)
from qplot.datahandling.trusted_work_scheduler import (
    TrustedCacheWorkKey,
    trusted_derived_cache_root,
)

TRUSTED_DERIVED_CACHE_FORMAT_VERSION = 1
TRUSTED_DERIVED_CACHE_MAX_ENTRY_BYTES = 16 * 1024 * 1024
TRUSTED_DERIVED_CACHE_MAX_TOTAL_BYTES = 256 * 1024 * 1024
TRUSTED_DERIVED_CACHE_MAX_ENTRIES = 2_048
TRUSTED_DERIVED_CACHE_MAX_CLEANUP_FILES = 4_096
TRUSTED_DERIVED_CACHE_MAX_HEADER_BYTES = 64 * 1024
TRUSTED_DERIVED_CACHE_MAX_TEMP_FILES = 16
TRUSTED_DERIVED_CACHE_MAX_JSON_DEPTH = 32
TRUSTED_DERIVED_CACHE_MAX_JSON_NODES = 131_072
TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS = 65_536
TRUSTED_DERIVED_CACHE_MAX_TEXT_BYTES = 1024 * 1024

_MAGIC = b"QPLTDCB1"
_PREFIX = struct.Struct(">8sIQ")
_ENTRY_PATTERN = re.compile(r"^[0-9a-f]{64}\.qdc$")
_TEMP_PATTERN = re.compile(r"^[0-9a-f]{64}\.[0-9a-f]{32}\.tmp$")
_INDEX_NAME = ".qplot-derived-cache-index.sqlite3"
_LOCK_NAME = ".qplot-derived-cache.lock"
_INDEX_SCHEMA_VERSION = "1"


class _CacheLockUnavailable(OSError):
    pass


class _CacheEpochChanged(RuntimeError):
    pass


class _CacheIndexCorrupt(ValueError):
    pass


class _CacheOwnershipError(ValueError):
    pass


@dataclass(frozen=True)
class _ArtifactProof:
    path: Path
    device: int
    inode: int
    temporary: bool
    canonical_key: bytes


def _rollback_index_best_effort(connection: object) -> None:
    try:
        cast(sqlite3.Connection, connection).rollback()
    except (sqlite3.Error, OSError):
        pass


def _close_index_best_effort(connection: object) -> None:
    try:
        cast(sqlite3.Connection, connection).close()
    except (sqlite3.Error, OSError):
        pass


@contextmanager
def _cross_process_cache_lock(
    path: Path,
    *,
    cancel_check: Callable[[], None],
):
    """Acquire one bounded unprivileged root lock on POSIX or Windows."""

    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    if os.name == "posix":
        os.chmod(path, 0o600)
    acquired = False
    deadline = time.monotonic() + 1.0
    try:
        if os.name == "posix":
            import fcntl

            while True:
                cancel_check()
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise _CacheLockUnavailable(
                            "The cache root lock is busy."
                        ) from None
                    time.sleep(0.005)
        elif os.name == "nt":
            import msvcrt

            locking = msvcrt.locking  # type: ignore[attr-defined]
            lock_nonblocking = msvcrt.LK_NBLCK  # type: ignore[attr-defined]
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            while True:
                cancel_check()
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    locking(descriptor, lock_nonblocking, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise _CacheLockUnavailable(
                            "The cache root lock is busy."
                        ) from None
                    time.sleep(0.005)
        else:
            raise _CacheLockUnavailable("No cache-root lock is available.")
        yield
    finally:
        if acquired:
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(  # type: ignore[attr-defined]
                    descriptor,
                    msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                    1,
                )
        os.close(descriptor)


def _valid_text(value: str, *, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) > maximum:
        raise ValueError("A cache text value is oversized.")
    return value


def _canonical_value(value: object) -> object:
    if value is None:
        return ["none"]
    if isinstance(value, bool):
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["str", _valid_text(value, maximum=TRUSTED_DERIVED_CACHE_MAX_TEXT_BYTES)]
    if isinstance(value, bytes):
        return ["bytes", base64.b64encode(value).decode("ascii")]
    if isinstance(value, tuple):
        return ["tuple", [_canonical_value(item) for item in value]]
    if isinstance(value, dict):
        if any(not isinstance(name, str) for name in value):
            raise TypeError("Derived payload dictionary keys must be text.")
        return [
            "dict",
            [
                [name, _canonical_value(value[name])]
                for name in sorted(cast(dict[str, object], value))
            ],
        ]
    raise TypeError(f"Unsupported canonical cache value {type(value).__name__}.")


def _restore_value(
    value: object,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> object:
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > TRUSTED_DERIVED_CACHE_MAX_JSON_NODES:
        raise ValueError("A cache value has too many nodes.")
    if depth > 16 or not isinstance(value, list) or not value:
        raise ValueError("A cache value has an invalid canonical structure.")
    tag = value[0]
    if tag == "none" and len(value) == 1:
        return None
    if tag == "bool" and len(value) == 2 and isinstance(value[1], bool):
        return value[1]
    if tag == "int" and len(value) == 2 and isinstance(value[1], str):
        _valid_text(value[1], maximum=128)
        return int(value[1])
    if tag == "float" and len(value) == 2 and isinstance(value[1], str):
        _valid_text(value[1], maximum=128)
        restored = float.fromhex(value[1])
        if not (restored == restored and abs(restored) != float("inf")):
            raise ValueError("A cache float is not finite.")
        return restored
    if tag == "str" and len(value) == 2 and isinstance(value[1], str):
        return _valid_text(value[1], maximum=TRUSTED_DERIVED_CACHE_MAX_TEXT_BYTES)
    if tag == "bytes" and len(value) == 2 and isinstance(value[1], str):
        maximum_encoded = 4 * ((TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES + 2) // 3)
        if len(value[1]) > maximum_encoded:
            raise ValueError("A cache byte scalar is oversized.")
        restored_bytes = base64.b64decode(value[1], validate=True)
        if len(restored_bytes) > TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES:
            raise ValueError("A cache byte scalar is oversized.")
        return restored_bytes
    if tag == "tuple" and len(value) == 2 and isinstance(value[1], list):
        if len(value[1]) > TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS:
            raise ValueError("A cache tuple has too many items.")
        return tuple(
            _restore_value(item, depth=depth + 1, budget=budget) for item in value[1]
        )
    if tag == "dict" and len(value) == 2 and isinstance(value[1], list):
        if len(value[1]) > TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS:
            raise ValueError("A cache dictionary has too many items.")
        restored_dict: dict[str, object] = {}
        for item in value[1]:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or item[0] in restored_dict
            ):
                raise ValueError("A cache dictionary item is invalid.")
            _valid_text(item[0], maximum=256)
            restored_dict[item[0]] = _restore_value(
                item[1], depth=depth + 1, budget=budget
            )
        return restored_dict
    raise ValueError("A cache value has an unknown canonical tag.")


def canonical_trusted_cache_key(key: TrustedCacheWorkKey) -> bytes:
    """Return a type-preserving deterministic cache-key encoding."""

    if not isinstance(key, TrustedCacheWorkKey):
        raise TypeError("key must be TrustedCacheWorkKey.")
    value = (
        "qplot-trusted-cache-key-v1",
        (
            key.database_instance.logical_path,
            key.database_instance.resolved_path,
            cast(tuple[object, ...], key.database_instance.identity),
        ),
        key.run_guid,
        int(key.kind),
        key.source_revision.fingerprint,
        key.renderer_version,
        key.rendering_options.canonical_values,
    )
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def trusted_cache_filename(key: TrustedCacheWorkKey) -> str:
    return f"{hashlib.sha256(canonical_trusted_cache_key(key)).hexdigest()}.qdc"


class _JsonPreflightParser:
    """Count JSON structure without materialising its container graph."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._length = len(text)
        self._position = 0
        self._nodes = 0

    def parse(self) -> None:
        self._skip_whitespace()
        self._parse_value(depth=0)
        self._skip_whitespace()
        if self._position != self._length:
            raise ValueError("A cache JSON document has trailing content.")

    def _skip_whitespace(self) -> None:
        while self._position < self._length and self._text[self._position] in " \t\r\n":
            self._position += 1

    def _count_node(self) -> None:
        self._nodes += 1
        if self._nodes > TRUSTED_DERIVED_CACHE_MAX_JSON_NODES:
            raise ValueError("A cache JSON document has too many nodes.")

    def _parse_value(self, *, depth: int, binary: bool = False) -> str | None:
        self._skip_whitespace()
        if self._position >= self._length:
            raise ValueError("A cache JSON document is incomplete.")
        self._count_node()
        character = self._text[self._position]
        if character == '"':
            return self._parse_string(binary=binary)
        if character == "[":
            self._parse_array(depth=depth + 1)
            return None
        if character == "{":
            self._parse_object(depth=depth + 1)
            return None
        start = self._position
        while (
            self._position < self._length
            and self._text[self._position] not in " \t\r\n,]}"
        ):
            self._position += 1
        if self._position == start:
            raise ValueError("A cache JSON scalar is invalid.")
        return None

    def _parse_array(self, *, depth: int) -> None:
        if depth > TRUSTED_DERIVED_CACHE_MAX_JSON_DEPTH:
            raise ValueError("A cache JSON document is too deeply nested.")
        self._position += 1
        self._skip_whitespace()
        count = 0
        first_value: str | None = None
        while self._position >= self._length or self._text[self._position] != "]":
            count += 1
            if count > TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS:
                raise ValueError("A cache JSON container has too many items.")
            value = self._parse_value(
                depth=depth,
                binary=count == 2 and first_value == "bytes",
            )
            if count == 1:
                first_value = value
            self._skip_whitespace()
            if self._position >= self._length:
                raise ValueError("A cache JSON document is incomplete.")
            if self._text[self._position] == "]":
                break
            if self._text[self._position] != ",":
                raise ValueError("A cache JSON array is invalid.")
            self._position += 1
            self._skip_whitespace()
        self._position += 1

    def _parse_object(self, *, depth: int) -> None:
        if depth > TRUSTED_DERIVED_CACHE_MAX_JSON_DEPTH:
            raise ValueError("A cache JSON document is too deeply nested.")
        self._position += 1
        self._skip_whitespace()
        count = 0
        while self._position >= self._length or self._text[self._position] != "}":
            count += 1
            if count > TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS:
                raise ValueError("A cache JSON container has too many items.")
            if self._position >= self._length or self._text[self._position] != '"':
                raise ValueError("A cache JSON object key is invalid.")
            self._parse_string(binary=False)
            self._skip_whitespace()
            if self._position >= self._length or self._text[self._position] != ":":
                raise ValueError("A cache JSON object is invalid.")
            self._position += 1
            self._parse_value(depth=depth)
            self._skip_whitespace()
            if self._position >= self._length:
                raise ValueError("A cache JSON document is incomplete.")
            if self._text[self._position] == "}":
                break
            if self._text[self._position] != ",":
                raise ValueError("A cache JSON object is invalid.")
            self._position += 1
            self._skip_whitespace()
        self._position += 1

    def _parse_string(self, *, binary: bool) -> str | None:
        start = self._position + 1
        self._position = start
        escaped = False
        byte_count = 0
        while self._position < self._length:
            character = self._text[self._position]
            if not escaped and character == '"':
                break
            if binary:
                if escaped or character == "\\" or not character.isascii():
                    raise ValueError("A cache byte scalar is invalid.")
                if not (character.isalnum() or character in "+/="):
                    raise ValueError("A cache byte scalar is invalid.")
                byte_count += 1
            else:
                byte_count += len(character.encode("utf-8"))
                if byte_count > TRUSTED_DERIVED_CACHE_MAX_TEXT_BYTES:
                    raise ValueError("A cache JSON string is oversized.")
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            self._position += 1
        if self._position >= self._length or escaped:
            raise ValueError("A cache JSON document is incomplete.")
        end = self._position
        self._position += 1
        if binary:
            maximum_encoded = 4 * ((TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES + 2) // 3)
            if byte_count > maximum_encoded or byte_count % 4:
                raise ValueError("A cache byte scalar is oversized.")
            padding = int(byte_count > 0 and self._text[end - 1] == "=")
            padding += int(byte_count > 1 and self._text[end - 2] == "=")
            if self._text.find("=", start, end - padding) != -1:
                raise ValueError("A cache byte scalar is invalid.")
            decoded_bytes = byte_count // 4 * 3 - padding
            if decoded_bytes > TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES:
                raise ValueError("A cache byte scalar is oversized.")
            return None
        if end - start == 5 and self._text.startswith("bytes", start):
            return "bytes"
        return None


def _preflight_json(data: bytes) -> None:
    """Bound JSON structure and canonical binary strings before ``json.loads``."""

    if len(data) > TRUSTED_DERIVED_CACHE_MAX_ENTRY_BYTES:
        raise ValueError("A cache JSON document is oversized.")
    _JsonPreflightParser(data.decode("utf-8")).parse()


class TrustedDerivedDiskCache:
    """Best-effort qPlot-owned cache; every failure degrades to an uncached result."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        enabled: bool = True,
        max_entry_bytes: int = TRUSTED_DERIVED_CACHE_MAX_ENTRY_BYTES,
        max_total_bytes: int = TRUSTED_DERIVED_CACHE_MAX_TOTAL_BYTES,
        max_entries: int = TRUSTED_DERIVED_CACHE_MAX_ENTRIES,
    ) -> None:
        selected_root = Path(
            trusted_derived_cache_root() if root is None else os.fspath(root)
        )
        if not selected_root.is_absolute():
            raise ValueError("The trusted derived cache root must be absolute.")
        if (
            type(max_entry_bytes) is not int
            or type(max_total_bytes) is not int
            or type(max_entries) is not int
            or not 1 <= max_entry_bytes <= TRUSTED_DERIVED_CACHE_MAX_ENTRY_BYTES
            or not max_entry_bytes
            <= max_total_bytes
            <= TRUSTED_DERIVED_CACHE_MAX_TOTAL_BYTES
            or not 1 <= max_entries <= TRUSTED_DERIVED_CACHE_MAX_ENTRIES
        ):
            raise ValueError("Trusted derived cache bounds are invalid.")
        self.root = selected_root
        self._requested_enabled = bool(enabled)
        self.enabled = bool(enabled)
        self.max_entry_bytes = max_entry_bytes
        self.max_total_bytes = max_total_bytes
        self.max_entries = max_entries
        self._state_lock = threading.RLock()
        self._state_changed = threading.Condition(self._state_lock)
        self._epoch = 0
        self._active_puts = 0
        self._protected_database_family: frozenset[Path] = frozenset()

    def configure_for_database(self, database_instance: DatabaseInstance) -> None:
        """Disable disk writes unless cache and database directories are disjoint."""

        if not isinstance(database_instance, DatabaseInstance):
            raise TypeError("database_instance must be a DatabaseInstance.")
        database_path = Path(database_instance.resolved_path)
        protected_family = frozenset(
            Path(f"{database_path}{suffix}")
            for suffix in ("", "-wal", "-journal", "-shm")
        )
        try:
            root = self.root.resolve(strict=False)
            database_directory = database_path.parent.resolve(strict=False)
            unsafe = (
                root == database_directory
                or root.is_relative_to(database_directory)
                or database_directory.is_relative_to(root)
            )
        except (OSError, RuntimeError, ValueError):
            unsafe = True
        with self._state_changed:
            self._epoch += 1
            self._protected_database_family = protected_family
            self.enabled = self._requested_enabled and not unsafe
            self._state_changed.notify_all()

    def get(
        self,
        key: TrustedCacheWorkKey,
        *,
        cancel_check: Callable[[], None] = lambda: None,
    ) -> DerivedPayload | None:
        """Read and verify one exact entry without creating or touching files."""

        if not self.enabled:
            return None
        try:
            cancel_check()
            canonical_key = canonical_trusted_cache_key(key)
            path = self.root / trusted_cache_filename(key)
            status = path.stat()
            if not path.is_file() or status.st_size > self.max_entry_bytes:
                return None
            data = path.read_bytes()
        except (InterruptedError, TimeoutError):
            raise
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ):
            return None
        try:
            if len(data) < _PREFIX.size:
                return None
            magic, header_length, payload_length = _PREFIX.unpack_from(data)
            if (
                magic != _MAGIC
                or header_length > TRUSTED_DERIVED_CACHE_MAX_HEADER_BYTES
                or payload_length > self.max_entry_bytes
                or _PREFIX.size + header_length + payload_length != len(data)
            ):
                return None
            cancel_check()
            header_start = _PREFIX.size
            payload_start = header_start + header_length
            _preflight_json(data[header_start:payload_start])
            header = json.loads(data[header_start:payload_start].decode("utf-8"))
            if not isinstance(header, dict):
                return None
            if header.get("format_version") != TRUSTED_DERIVED_CACHE_FORMAT_VERSION:
                return None
            expected_key = base64.b64encode(canonical_key).decode("ascii")
            if (
                header.get("key") != expected_key
                or header.get("payload_length") != payload_length
            ):
                return None
            payload_bytes = data[payload_start:]
            if (
                header.get("payload_sha256")
                != hashlib.sha256(payload_bytes).hexdigest()
            ):
                return None
            _preflight_json(payload_bytes)
            decoded = json.loads(payload_bytes.decode("utf-8"))
            restored = _restore_value(decoded)
            if not isinstance(restored, dict):
                return None
            payload = cast(DerivedPayload, restored)
            validate_trusted_derived_payload(payload)
            cancel_check()
            return payload
        except (InterruptedError, TimeoutError):
            raise
        except (
            binascii.Error,
            UnicodeError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
            json.JSONDecodeError,
            OSError,
            TrustedDerivedRenderingError,
        ):
            return None

    def put(
        self,
        key: TrustedCacheWorkKey,
        payload: DerivedPayload,
        *,
        cancel_check: Callable[[], None] = lambda: None,
    ) -> bool:
        """Atomically store one entry; never turn a cache fault into DB failure."""

        with self._state_changed:
            if not self.enabled:
                return False
            epoch = self._epoch
            self._active_puts += 1
        temporary: Path | None = None
        temporary_proof: _ArtifactProof | None = None
        destination: Path | None = None
        published: Path | None = None
        published_proof: _ArtifactProof | None = None
        successful = False
        index_started = False
        disable_after_cleanup = False

        def cache_cancel_check() -> None:
            cancel_check()
            with self._state_lock:
                if not self.enabled or epoch != self._epoch:
                    raise _CacheEpochChanged("The cache database selection changed.")

        try:
            cache_cancel_check()
            validate_trusted_derived_payload(payload)
            canonical_key = canonical_trusted_cache_key(key)
            payload_bytes = json.dumps(
                _canonical_value(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            header = json.dumps(
                {
                    "format_version": TRUSTED_DERIVED_CACHE_FORMAT_VERSION,
                    "key": base64.b64encode(canonical_key).decode("ascii"),
                    "payload_length": len(payload_bytes),
                    "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            total = _PREFIX.size + len(header) + len(payload_bytes)
            if (
                len(header) > TRUSTED_DERIVED_CACHE_MAX_HEADER_BYTES
                or total > self.max_entry_bytes
            ):
                return False
            self._ensure_root()
            filename = trusted_cache_filename(key)
            destination = self.root / filename
            temporary = self.root / f"{filename[:-4]}.{secrets.token_hex(16)}.tmp"
            cache_cancel_check()
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            created_status = os.fstat(descriptor)
            temporary_proof = _ArtifactProof(
                temporary,
                created_status.st_dev,
                created_status.st_ino,
                True,
                canonical_key,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_PREFIX.pack(_MAGIC, len(header), len(payload_bytes)))
                stream.write(header)
                stream.write(payload_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            cache_cancel_check()
            self._before_publish()
            cache_cancel_check()
            with _cross_process_cache_lock(
                self.root / _LOCK_NAME,
                cancel_check=cache_cancel_check,
            ):
                index_started = True
                connection = self._open_index()
                try:
                    if not self._ensure_global_inventory(
                        connection,
                        cancel_check=cache_cancel_check,
                        expected_epoch=epoch,
                    ):
                        return False
                    cache_cancel_check()
                    try:
                        existing_proof = self._prove_framed_artifact(
                            destination,
                            temporary=False,
                        )
                    except FileNotFoundError:
                        existing_proof = None
                    except (OSError, ValueError):
                        disable_after_cleanup = True
                        return False
                    if existing_proof is not None:
                        if existing_proof.canonical_key != canonical_key:
                            disable_after_cleanup = True
                            return False
                        self._reuse_existing_entry(
                            connection,
                            filename,
                            existing_proof,
                            expected_epoch=epoch,
                        )
                        cache_cancel_check()
                        successful = True
                        return True
                    if not self._reserve_and_evict(
                        connection,
                        filename,
                        total,
                        cancel_check=cache_cancel_check,
                        expected_epoch=epoch,
                    ):
                        disable_after_cleanup = True
                        return False
                    try:
                        published_proof = self._publish_no_clobber(
                            temporary_proof,
                            destination,
                            expected_epoch=epoch,
                        )
                    except FileExistsError:
                        try:
                            existing_proof = self._prove_framed_artifact(
                                destination,
                                temporary=False,
                            )
                        except (OSError, ValueError):
                            disable_after_cleanup = True
                            return False
                        if existing_proof.canonical_key != canonical_key:
                            disable_after_cleanup = True
                            return False
                        self._reuse_existing_entry(
                            connection,
                            filename,
                            existing_proof,
                            expected_epoch=epoch,
                        )
                        cache_cancel_check()
                        successful = True
                        return True
                    else:
                        published = destination
                        status = destination.stat()
                        connection.execute("BEGIN IMMEDIATE")
                        connection.execute(
                            "UPDATE entries SET modified = ?, size = ?, ready = 1 "
                            "WHERE name = ?",
                            (status.st_mtime_ns, status.st_size, filename),
                        )
                        connection.commit()
                        cache_cancel_check()
                except BaseException:
                    _rollback_index_best_effort(connection)
                    raise
                finally:
                    _close_index_best_effort(connection)
            successful = True
            return True
        except (InterruptedError, TimeoutError):
            raise
        except _CacheEpochChanged:
            return False
        except sqlite3.Error:
            disable_after_cleanup = True
            return False
        except (
            OSError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
            TrustedDerivedRenderingError,
        ):
            if index_started:
                disable_after_cleanup = True
            return False
        finally:
            if published is not None and published_proof is not None and not successful:
                try:
                    self._unlink_proven_artifact(
                        published_proof,
                        expected_epoch=epoch,
                        operation="failed-publication",
                    )
                except (OSError, ValueError, _CacheEpochChanged):
                    pass
            if temporary is not None and temporary_proof is not None:
                try:
                    with self._state_lock:
                        operation_invalidated = not self.enabled or self._epoch != epoch
                    self._unlink_proven_artifact(
                        temporary_proof,
                        expected_epoch=epoch,
                        operation=(
                            "invalidated-operation-cleanup"
                            if operation_invalidated
                            else "temporary-cleanup"
                        ),
                    )
                except (OSError, ValueError, _CacheEpochChanged):
                    pass
            with self._state_changed:
                if disable_after_cleanup:
                    self.enabled = False
                self._active_puts -= 1
                self._state_changed.notify_all()

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.root, 0o700)

    def _before_publish(self) -> None:
        """Test seam immediately before the epoch/publication lock."""

    def _before_artifact_proof(self, path: Path, operation: str) -> None:
        """Test seam after candidate selection and before ownership proof."""

    def _before_destructive_cache_file(self, path: Path, operation: str) -> None:
        """Test seam after ownership proof and before the atomic state guard."""

    @staticmethod
    def _is_protected_database_artifact(
        path: Path,
        protected_family: frozenset[Path],
    ) -> bool:
        if path in protected_family:
            return True
        try:
            resolved = path.resolve(strict=False)
            return any(
                resolved == protected.resolve(strict=False)
                for protected in protected_family
            )
        except (OSError, RuntimeError, ValueError):
            return True

    def _validated_artifact_path(
        self,
        stored_name: object,
        *,
        temporary: bool,
        index_derived: bool,
        protected_family: frozenset[Path] | None = None,
    ) -> Path:
        """Return one syntactically safe direct-child candidate."""

        def reject() -> None:
            error = "The cache inventory contains an unsafe deletion target."
            if index_derived:
                raise _CacheIndexCorrupt(error)
            raise ValueError(error)

        if type(stored_name) is not str:
            reject()
        name = cast(str, stored_name)
        drive, _tail = ntpath.splitdrive(name)
        if (
            not name
            or name in {".", ".."}
            or drive
            or name.startswith(("/", "\\"))
            or "/" in name
            or "\\" in name
            or Path(name).is_absolute()
            or Path(name).name != name
        ):
            reject()
        pattern = _TEMP_PATTERN if temporary else _ENTRY_PATTERN
        if pattern.fullmatch(name) is None:
            reject()
        candidate = self.root / name
        if candidate.parent != self.root:
            reject()
        try:
            root = self.root.resolve(strict=False)
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            reject()
        if protected_family is None:
            with self._state_lock:
                protected_family = self._protected_database_family
        if resolved.parent != root or self._is_protected_database_artifact(
            candidate,
            protected_family,
        ):
            reject()
        return candidate

    def _prove_framed_artifact(
        self,
        path: Path,
        *,
        temporary: bool,
    ) -> _ArtifactProof:
        """Prove a persistent artifact from bounded qPlot framing and integrity."""

        candidate = self._validated_artifact_path(
            path.name,
            temporary=temporary,
            index_derived=False,
        )
        if candidate != path:
            raise _CacheOwnershipError("The cache artifact path is not canonical.")
        status = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_size < _PREFIX.size
            or status.st_size > self.max_entry_bytes
        ):
            raise _CacheOwnershipError("The cache artifact is not a bounded file.")
        with path.open("rb") as stream:
            data = stream.read(self.max_entry_bytes + 1)
        final_status = path.stat(follow_symlinks=False)
        if (final_status.st_dev, final_status.st_ino) != (
            status.st_dev,
            status.st_ino,
        ) or len(data) != status.st_size:
            raise _CacheOwnershipError("The cache artifact identity changed.")
        magic, header_length, payload_length = _PREFIX.unpack_from(data)
        if (
            magic != _MAGIC
            or header_length > TRUSTED_DERIVED_CACHE_MAX_HEADER_BYTES
            or payload_length > self.max_entry_bytes
            or _PREFIX.size + header_length + payload_length != len(data)
        ):
            raise _CacheOwnershipError("The cache artifact framing is invalid.")
        header_start = _PREFIX.size
        payload_start = header_start + header_length
        header_bytes = data[header_start:payload_start]
        _preflight_json(header_bytes)
        header = json.loads(header_bytes.decode("utf-8"))
        if not isinstance(header, dict) or set(header) != {
            "format_version",
            "key",
            "payload_length",
            "payload_sha256",
        }:
            raise _CacheOwnershipError("The cache artifact header is invalid.")
        encoded_key = header.get("key")
        digest = header.get("payload_sha256")
        if (
            header.get("format_version") != TRUSTED_DERIVED_CACHE_FORMAT_VERSION
            or header.get("payload_length") != payload_length
            or not isinstance(encoded_key, str)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise _CacheOwnershipError("The cache artifact header is invalid.")
        canonical_key = base64.b64decode(encoded_key, validate=True)
        expected_stem = hashlib.sha256(canonical_key).hexdigest()
        actual_stem = path.name[:64]
        payload_bytes = data[payload_start:]
        if (
            actual_stem != expected_stem
            or digest != hashlib.sha256(payload_bytes).hexdigest()
        ):
            raise _CacheOwnershipError("The cache artifact integrity is invalid.")
        return _ArtifactProof(
            path,
            final_status.st_dev,
            final_status.st_ino,
            temporary,
            canonical_key,
        )

    def _recheck_proof_locked(
        self,
        proof: _ArtifactProof,
        *,
        expected_epoch: int,
    ) -> Path:
        if not self.enabled or self._epoch != expected_epoch:
            raise _CacheEpochChanged("The cache database selection changed.")
        path = self._validated_artifact_path(
            proof.path.name,
            temporary=proof.temporary,
            index_derived=False,
            protected_family=self._protected_database_family,
        )
        if path != proof.path:
            raise _CacheOwnershipError("The cache artifact path changed.")
        status = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(status.st_mode) or (status.st_dev, status.st_ino) != (
            proof.device,
            proof.inode,
        ):
            raise _CacheOwnershipError("The cache artifact identity changed.")
        return path

    def _unlink_proven_artifact(
        self,
        proof: _ArtifactProof,
        *,
        expected_epoch: int,
        operation: str,
    ) -> None:
        """Perform the only cache-artifact unlink under the configuration lock."""

        self._before_destructive_cache_file(proof.path, operation)
        with self._state_lock:
            path = self._recheck_proof_locked(proof, expected_epoch=expected_epoch)
            path.unlink()

    def _publish_no_clobber(
        self,
        temporary_proof: _ArtifactProof,
        destination: Path,
        *,
        expected_epoch: int,
    ) -> _ArtifactProof:
        """Atomically publish by hard link, never replacing an existing path."""

        with self._state_lock:
            temporary = self._recheck_proof_locked(
                temporary_proof,
                expected_epoch=expected_epoch,
            )
            destination = self._validated_artifact_path(
                destination.name,
                temporary=False,
                index_derived=False,
                protected_family=self._protected_database_family,
            )
            os.link(temporary, destination)
            return _ArtifactProof(
                destination,
                temporary_proof.device,
                temporary_proof.inode,
                False,
                temporary_proof.canonical_key,
            )

    def _reuse_existing_entry(
        self,
        connection: sqlite3.Connection,
        filename: str,
        proof: _ArtifactProof,
        *,
        expected_epoch: int,
    ) -> None:
        with self._state_lock:
            path = self._recheck_proof_locked(proof, expected_epoch=expected_epoch)
        status = path.stat(follow_symlinks=False)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR REPLACE INTO entries(name, modified, size, ready) "
            "VALUES(?, ?, ?, 1)",
            (filename, status.st_mtime_ns, status.st_size),
        )
        connection.commit()

    def _open_index(self) -> sqlite3.Connection:
        index_path = self.root / _INDEX_NAME
        connection = sqlite3.connect(index_path, timeout=0.0, isolation_level=None)
        try:
            if os.name == "posix":
                os.chmod(index_path, 0o600)
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cache_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS entries ("
                "name TEXT PRIMARY KEY, modified INTEGER NOT NULL, "
                "size INTEGER NOT NULL, ready INTEGER NOT NULL) WITHOUT ROWID"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS entries_oldest ON entries(modified, name)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO cache_meta(key, value) VALUES('schema', ?)",
                (_INDEX_SCHEMA_VERSION,),
            )
            schema = connection.execute(
                "SELECT value FROM cache_meta WHERE key = 'schema'"
            ).fetchone()
            if schema != (_INDEX_SCHEMA_VERSION,):
                raise ValueError("The cache inventory schema is incompatible.")
            return connection
        except BaseException:
            _close_index_best_effort(connection)
            raise

    def _ensure_global_inventory(
        self,
        connection: sqlite3.Connection,
        *,
        cancel_check: Callable[[], None],
        expected_epoch: int,
    ) -> bool:
        complete = connection.execute(
            "SELECT value FROM cache_meta WHERE key = 'inventory_complete'"
        ).fetchone()
        if complete == ("1",):
            return self._recover_pending(connection, cancel_check=cancel_check)

        scanned: list[os.DirEntry[str]] = []
        with os.scandir(self.root) as iterator:
            for index, entry in enumerate(
                islice(iterator, TRUSTED_DERIVED_CACHE_MAX_CLEANUP_FILES)
            ):
                if index % 128 == 0:
                    cancel_check()
                scanned.append(entry)
        if len(scanned) >= TRUSTED_DERIVED_CACHE_MAX_CLEANUP_FILES:
            deletion_targets: list[_ArtifactProof] = []
            for entry in scanned:
                if not (
                    _ENTRY_PATTERN.fullmatch(entry.name)
                    or _TEMP_PATTERN.fullmatch(entry.name)
                ):
                    continue
                cancel_check()
                if not entry.is_file(follow_symlinks=False):
                    continue
                is_temporary = _TEMP_PATTERN.fullmatch(entry.name) is not None
                path = self._validated_artifact_path(
                    entry.name,
                    temporary=is_temporary,
                    index_derived=True,
                )
                self._before_artifact_proof(path, "inventory-overflow")
                deletion_targets.append(
                    self._prove_framed_artifact(path, temporary=is_temporary)
                )
            for proof in deletion_targets:
                cancel_check()
                self._unlink_proven_artifact(
                    proof,
                    expected_epoch=expected_epoch,
                    operation="inventory-overflow",
                )
            return False

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DELETE FROM entries")
            stale_temporaries: list[tuple[int, _ArtifactProof]] = []
            for entry in scanned:
                cancel_check()
                try:
                    status = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if _ENTRY_PATTERN.fullmatch(entry.name):
                    path = self._validated_artifact_path(
                        entry.name,
                        temporary=False,
                        index_derived=True,
                    )
                    self._before_artifact_proof(path, "inventory-entry")
                    self._prove_framed_artifact(path, temporary=False)
                    connection.execute(
                        "INSERT OR REPLACE INTO entries "
                        "(name, modified, size, ready) VALUES(?, ?, ?, 1)",
                        (entry.name, status.st_mtime_ns, status.st_size),
                    )
                elif _TEMP_PATTERN.fullmatch(entry.name):
                    path = self._validated_artifact_path(
                        entry.name,
                        temporary=True,
                        index_derived=True,
                    )
                    self._before_artifact_proof(path, "stale-temporary")
                    stale_temporaries.append(
                        (
                            status.st_mtime_ns,
                            self._prove_framed_artifact(path, temporary=True),
                        )
                    )
            stale_temporaries.sort(key=lambda item: (item[0], item[1].path.name))
            for _modified, proof in stale_temporaries[
                :-TRUSTED_DERIVED_CACHE_MAX_TEMP_FILES
            ]:
                cancel_check()
                self._unlink_proven_artifact(
                    proof,
                    expected_epoch=expected_epoch,
                    operation="stale-temporary",
                )
            connection.execute(
                "INSERT OR REPLACE INTO cache_meta(key, value) "
                "VALUES('inventory_complete', '1')"
            )
            connection.commit()
        except BaseException:
            _rollback_index_best_effort(connection)
            raise
        within_limits = self._enforce_global_limits(
            connection,
            exclude=None,
            cancel_check=cancel_check,
            expected_epoch=expected_epoch,
        )
        if not within_limits:
            with self._state_lock:
                self.enabled = False
        return within_limits

    def _recover_pending(
        self,
        connection: sqlite3.Connection,
        *,
        cancel_check: Callable[[], None],
    ) -> bool:
        pending = connection.execute(
            "SELECT name FROM entries WHERE ready = 0 ORDER BY modified, name LIMIT ?",
            (TRUSTED_DERIVED_CACHE_MAX_CLEANUP_FILES,),
        ).fetchall()
        validated_pending = []
        for (name,) in pending:
            path = self._validated_artifact_path(
                name,
                temporary=False,
                index_derived=True,
            )
            try:
                proof = self._prove_framed_artifact(path, temporary=False)
            except FileNotFoundError:
                proof = None
            validated_pending.append((name, path, proof))
        connection.execute("BEGIN IMMEDIATE")
        try:
            for name, path, proof in validated_pending:
                cancel_check()
                try:
                    status = path.stat()
                except FileNotFoundError:
                    connection.execute("DELETE FROM entries WHERE name = ?", (name,))
                except OSError:
                    connection.rollback()
                    with self._state_lock:
                        self.enabled = False
                    return False
                else:
                    if proof is None:
                        raise _CacheOwnershipError(
                            "A pending cache artifact lacks ownership proof."
                        )
                    connection.execute(
                        "UPDATE entries SET modified = ?, size = ?, ready = 1 "
                        "WHERE name = ?",
                        (status.st_mtime_ns, status.st_size, name),
                    )
            connection.commit()
        except BaseException:
            _rollback_index_best_effort(connection)
            raise
        return len(pending) < TRUSTED_DERIVED_CACHE_MAX_CLEANUP_FILES

    def _reserve_and_evict(
        self,
        connection: sqlite3.Connection,
        name: str,
        size: int,
        *,
        cancel_check: Callable[[], None],
        expected_epoch: int,
    ) -> bool:
        connection.execute("BEGIN IMMEDIATE")
        try:
            prior = connection.execute(
                "SELECT size FROM entries WHERE name = ?", (name,)
            ).fetchone()
            reserved = max(size, prior[0] if prior is not None else 0)
            connection.execute(
                "INSERT OR REPLACE INTO entries(name, modified, size, ready) "
                "VALUES(?, ?, ?, 0)",
                (name, time.time_ns(), reserved),
            )
            if not self._enforce_global_limits(
                connection,
                exclude=name,
                cancel_check=cancel_check,
                expected_epoch=expected_epoch,
                in_transaction=True,
            ):
                connection.rollback()
                return False
            connection.commit()
            return True
        except BaseException:
            _rollback_index_best_effort(connection)
            raise

    def _enforce_global_limits(
        self,
        connection: sqlite3.Connection,
        *,
        exclude: str | None,
        cancel_check: Callable[[], None],
        expected_epoch: int,
        in_transaction: bool = False,
    ) -> bool:
        if not in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        try:
            count, total = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM entries"
            ).fetchone()
            inventory = connection.execute(
                "SELECT name, size FROM entries ORDER BY modified, name LIMIT ?",
                (TRUSTED_DERIVED_CACHE_MAX_CLEANUP_FILES,),
            ).fetchall()
            if (
                type(count) is not int
                or type(total) is not int
                or count < 0
                or total < 0
                or count != len(inventory)
            ):
                raise _CacheIndexCorrupt("The cache inventory bounds are invalid.")
            validated_inventory: list[tuple[str, int, Path, _ArtifactProof | None]] = []
            for name, entry_size in inventory:
                cancel_check()
                if type(entry_size) is not int or entry_size < 0:
                    raise _CacheIndexCorrupt("A cache inventory size is invalid.")
                path = self._validated_artifact_path(
                    name,
                    temporary=False,
                    index_derived=True,
                )
                self._before_artifact_proof(path, "eviction")
                try:
                    proof = self._prove_framed_artifact(path, temporary=False)
                except FileNotFoundError:
                    proof = None
                validated_inventory.append((cast(str, name), entry_size, path, proof))
            victims: list[tuple[str, Path, _ArtifactProof | None]] = []
            projected_count = count
            projected_total = total
            for name, entry_size, path, proof in validated_inventory:
                if (
                    projected_count <= self.max_entries
                    and projected_total <= self.max_total_bytes
                ):
                    break
                if exclude is not None and name == exclude:
                    continue
                victims.append((name, path, proof))
                projected_count -= 1
                projected_total -= entry_size
            if (
                projected_count > self.max_entries
                or projected_total > self.max_total_bytes
            ):
                if not in_transaction:
                    connection.rollback()
                return False
            for name, _path, proof in victims:
                cancel_check()
                if proof is not None:
                    self._unlink_proven_artifact(
                        proof,
                        expected_epoch=expected_epoch,
                        operation="eviction",
                    )
                connection.execute("DELETE FROM entries WHERE name = ?", (name,))
            if not in_transaction:
                connection.commit()
            return True
        except BaseException:
            if not in_transaction:
                _rollback_index_best_effort(connection)
            raise
