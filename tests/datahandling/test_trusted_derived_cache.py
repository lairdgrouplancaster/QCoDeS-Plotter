from __future__ import annotations

import base64
import os
import sqlite3
import stat
import threading
import time
from multiprocessing import get_context
from pathlib import Path

import pytest

import qplot.datahandling.trusted_derived_cache as cache_module
from qplot.datahandling.file_identity import DatabaseInstance
from qplot.datahandling.trusted_derived_cache import (
    TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS,
    TRUSTED_DERIVED_CACHE_MAX_TEXT_BYTES,
    TrustedDerivedDiskCache,
    _preflight_json,
    canonical_trusted_cache_key,
    trusted_cache_filename,
)
from qplot.datahandling.trusted_derived_rendering import (
    TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES,
)
from qplot.datahandling.trusted_live_queries import TrustedSourceRevision
from qplot.datahandling.trusted_work_scheduler import (
    RenderingOptions,
    TrustedCacheWorkKey,
    TrustedWorkKind,
    trusted_derived_cache_root,
)

_DEFAULT_OPTIONS = RenderingOptions()


def _key(
    value: int = 1,
    *,
    options: RenderingOptions = _DEFAULT_OPTIONS,
) -> TrustedCacheWorkKey:
    instance = DatabaseInstance("/data/test.db", "/data/test.db", (7, 11))
    return TrustedCacheWorkKey(
        instance,
        "guid-1",
        TrustedWorkKind.PREVIEW,
        TrustedSourceRevision(f"revision-{value}".encode()),
        "renderer-v1",
        options,
    )


def _payload(value: str = "ok"):
    return {
        "format": "qplot-trusted-derived-payload-v1",
        "kind": "preview",
        "status": "ok",
        "description": value,
        "source": (("revision", value.encode()),),
        "images": (),
    }


def _image_payload(
    byte_count: int,
    *,
    width: int = 1,
    height: int = 1,
):
    encoded = b"\x89PNG\r\n\x1a\n" + bytes(byte_count - 8)
    payload = _payload("image")
    payload["images"] = (
        (
            ("encoding", "png"),
            ("width", width),
            ("height", height),
            ("dependent", "signal"),
            ("dimensions", 2),
            ("sampled_points", 1),
            ("bytes", encoded),
        ),
    )
    return payload


def _qplot_cache_artifacts(directory: Path) -> tuple[Path, ...]:
    fixed_names = {
        ".qplot-derived-cache-index.sqlite3",
        ".qplot-derived-cache-index.sqlite3-journal",
        ".qplot-derived-cache-index.sqlite3-shm",
        ".qplot-derived-cache-index.sqlite3-wal",
        ".qplot-derived-cache.lock",
    }
    return tuple(
        path
        for path in directory.rglob("*")
        if path.name in fixed_names or path.suffix == ".qdc" or path.suffix == ".tmp"
    )


def _stable_artifact_snapshot(path: Path) -> tuple[bytes, int, int, int, int, int]:
    contents = path.read_bytes()
    status = path.stat()
    return (
        contents,
        status.st_mode,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _seed_cache_index(root: Path, row_name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / ".qplot-derived-cache-index.sqlite3")
    try:
        connection.execute(
            "CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) "
            "WITHOUT ROWID"
        )
        connection.execute(
            "CREATE TABLE entries (name TEXT PRIMARY KEY, modified INTEGER NOT NULL, "
            "size INTEGER NOT NULL, ready INTEGER NOT NULL) WITHOUT ROWID"
        )
        connection.execute("CREATE INDEX entries_oldest ON entries(modified, name)")
        connection.execute("INSERT INTO cache_meta VALUES('schema', '1')")
        connection.execute("INSERT INTO cache_meta VALUES('inventory_complete', '1')")
        connection.execute(
            "INSERT INTO entries VALUES(?, 0, 4096, 1)",
            (row_name,),
        )
        connection.commit()
    finally:
        connection.close()


def _database_family_snapshot(database_path: Path) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    for suffix in ("", "-wal", "-journal", "-shm"):
        path = Path(f"{database_path}{suffix}")
        snapshot[suffix] = _stable_artifact_snapshot(path) if path.exists() else None
    return snapshot


def _create_queryable_sqlite_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE protected(value TEXT NOT NULL)")
        connection.execute("INSERT INTO protected VALUES('still-queryable')")
        connection.commit()
    finally:
        connection.close()


def _assert_queryable_sqlite_database(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT value FROM protected").fetchall() == [
            ("still-queryable",)
        ]
    finally:
        connection.close()


def _write_cache_entries_in_process(
    root: str,
    start: int,
    ready: object,
    release: object,
) -> None:
    cache = TrustedDerivedDiskCache(
        Path(root),
        max_entry_bytes=4_096,
        max_total_bytes=32_768,
        max_entries=8,
    )
    ready.put(True)  # type: ignore[attr-defined]
    release.wait(10.0)  # type: ignore[attr-defined]
    for offset in range(20):
        value = start + offset
        cache.put(_key(value), _payload(str(value)))


def test_cache_round_trip_exact_key_separation_and_atomic_replacement(
    tmp_path: Path,
) -> None:
    cache = TrustedDerivedDiskCache(tmp_path / "cache")
    first = _key(1)
    second = _key(2)

    assert cache.get(first) is None
    assert cache.put(first, _payload("first"))
    assert cache.get(first) == _payload("first")
    assert cache.get(second) is None
    assert cache.put(first, _payload("replacement"))
    assert cache.get(first) == _payload("first")
    assert not tuple(cache.root.glob("*.tmp"))


def test_valid_existing_same_key_entry_is_reused_without_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    key = _key(193)
    first = TrustedDerivedDiskCache(root)
    assert first.put(key, _payload("original"))
    entry = root / trusted_cache_filename(key)
    before = _stable_artifact_snapshot(entry)

    second = TrustedDerivedDiskCache(root)
    assert second.put(key, _payload("different-render-is-not-published"))

    assert _stable_artifact_snapshot(entry) == before
    assert second.get(key) == _payload("original")
    assert not tuple(root.glob("*.tmp"))


def test_publication_never_clobbers_existing_sqlite_database_destination(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    key = _key(194)
    destination = root / trusted_cache_filename(key)
    _create_queryable_sqlite_database(destination)
    before = _database_family_snapshot(destination)
    selected = tmp_path / "selected" / "live.db"
    cache = TrustedDerivedDiskCache(root)
    cache.configure_for_database(
        DatabaseInstance(str(selected), str(selected), (7, 194))
    )

    assert not cache.put(key, _payload("must-not-clobber"))

    assert _database_family_snapshot(destination) == before
    _assert_queryable_sqlite_database(destination)
    assert not cache.enabled


@pytest.mark.parametrize(
    "byte_count",
    [
        TRUSTED_DERIVED_CACHE_MAX_TEXT_BYTES - 1,
        TRUSTED_DERIVED_CACHE_MAX_TEXT_BYTES + 1,
        1_100_008,
    ],
)
def test_valid_png_payloads_on_both_sides_of_text_limit_round_trip(
    tmp_path: Path,
    byte_count: int,
) -> None:
    cache = TrustedDerivedDiskCache(tmp_path / "cache")
    payload = _image_payload(byte_count)

    assert cache.put(_key(byte_count), payload)
    assert cache.get(_key(byte_count)) == payload


def test_maximum_encoded_and_decoded_image_boundary_round_trips(
    tmp_path: Path,
) -> None:
    cache = TrustedDerivedDiskCache(tmp_path / "cache")
    payload = _image_payload(
        TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES,
        width=2_048,
        height=2_048,
    )

    assert cache.put(_key(91), payload)
    assert cache.get(_key(91)) == payload


def test_binary_preflight_rejects_encoded_and_decoded_overflow_boundaries() -> None:
    maximum = TRUSTED_DERIVED_MAX_ENCODED_IMAGE_BYTES
    maximum_base64 = base64.b64encode(bytes(maximum))
    _preflight_json(b'["bytes","' + maximum_base64 + b'"]')

    decoded_overflow = base64.b64encode(bytes(maximum + 1))
    with pytest.raises(ValueError, match="byte scalar is oversized"):
        _preflight_json(b'["bytes","' + decoded_overflow + b'"]')
    with pytest.raises(ValueError, match="byte scalar is oversized"):
        _preflight_json(b'["bytes","' + maximum_base64 + b'A"]')


def test_oversized_ordinary_text_is_not_treated_as_binary() -> None:
    oversized = b"x" * (TRUSTED_DERIVED_CACHE_MAX_TEXT_BYTES + 1)

    with pytest.raises(ValueError, match="string is oversized"):
        _preflight_json(b'["str","' + oversized + b'"]')
    with pytest.raises(ValueError, match="string is oversized"):
        _preflight_json(b'{"description":"' + oversized + b'"}')


def test_key_encoding_preserves_bool_integer_float_and_string_types() -> None:
    keys = [
        _key(options=RenderingOptions.from_mapping({"value": value}))
        for value in (True, 1, 1.0, "1")
    ]

    assert len({canonical_trusted_cache_key(key) for key in keys}) == 4
    assert len({trusted_cache_filename(key) for key in keys}) == 4


@pytest.mark.parametrize("mutation", ["truncate", "corrupt", "version"])
def test_corrupt_truncated_and_incompatible_entries_are_misses(
    tmp_path: Path,
    mutation: str,
) -> None:
    cache = TrustedDerivedDiskCache(tmp_path / "cache")
    key = _key()
    assert cache.put(key, _payload())
    path = cache.root / trusted_cache_filename(key)
    data = path.read_bytes()
    if mutation == "truncate":
        path.write_bytes(data[:10])
    elif mutation == "corrupt":
        path.write_bytes(data[:-1] + bytes((data[-1] ^ 1,)))
    else:
        changed = bytearray(data)
        marker = b'"format_version":1'
        offset = changed.index(marker) + len(marker) - 1
        changed[offset] = ord("2")
        path.write_bytes(changed)

    assert cache.get(key) is None


def test_disabled_cache_miss_and_put_create_no_filesystem_entries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-not-exist"
    cache = TrustedDerivedDiskCache(root, enabled=False)

    assert cache.get(_key()) is None
    assert not cache.put(_key(), _payload())
    assert not root.exists()


def test_available_cache_miss_does_not_create_its_root(tmp_path: Path) -> None:
    root = tmp_path / "missing-cache"
    cache = TrustedDerivedDiskCache(root)

    assert cache.get(_key()) is None
    assert not root.exists()


def test_permission_and_disk_full_failures_degrade_to_uncached_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = TrustedDerivedDiskCache(tmp_path / "cache")
    key = _key()

    def disk_full(_source: object, _destination: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "link", disk_full)
    assert not cache.put(key, _payload())
    assert cache.get(key) is None
    assert not tuple(cache.root.glob("*.tmp"))

    real_open = os.open

    def permission_denied(path: object, *args: object, **kwargs: object):
        if str(path).endswith(".tmp"):
            raise PermissionError("read-only cache")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", permission_denied)
    assert not cache.put(key, _payload())


def test_corrupt_sqlite_index_disables_cache_and_degrades_to_false(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    (root / ".qplot-derived-cache-index.sqlite3").write_bytes(b"not sqlite")
    cache = TrustedDerivedDiskCache(root)

    assert not cache.put(_key(), _payload())
    assert not cache.enabled
    assert not tuple(root.glob("*.qdc"))


@pytest.mark.parametrize(
    "phase",
    ["open", "reservation", "replace", "ready", "commit", "rollback"],
)
def test_index_failures_at_every_publication_phase_are_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    cache = TrustedDerivedDiskCache(tmp_path / "cache")
    original_open_index = cache._open_index
    destructive_operations: list[str] = []
    monkeypatch.setattr(
        cache,
        "_before_destructive_cache_file",
        lambda _path, operation: destructive_operations.append(operation),
    )

    class FaultConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def execute(self, statement: str, *args: object):
            if phase == "ready" and statement.startswith("UPDATE entries SET modified"):
                raise sqlite3.OperationalError("ready update failed")
            if phase == "rollback" and statement.startswith(
                "SELECT value FROM cache_meta"
            ):
                raise sqlite3.OperationalError("transaction failed")
            return self.connection.execute(statement, *args)

        def commit(self) -> None:
            if phase == "commit":
                raise sqlite3.OperationalError("commit failed")
            self.connection.commit()

        def rollback(self) -> None:
            if phase == "rollback":
                raise sqlite3.DatabaseError("rollback failed")
            self.connection.rollback()

        def close(self) -> None:
            self.connection.close()

    if phase == "open":
        monkeypatch.setattr(
            cache,
            "_open_index",
            lambda: (_ for _ in ()).throw(sqlite3.DatabaseError("open failed")),
        )
    elif phase == "reservation":
        monkeypatch.setattr(
            cache,
            "_reserve_and_evict",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                sqlite3.OperationalError("reservation failed")
            ),
        )
    elif phase == "replace":
        monkeypatch.setattr(
            os,
            "link",
            lambda *_args: (_ for _ in ()).throw(OSError("replacement failed")),
        )
    else:
        monkeypatch.setattr(
            cache,
            "_open_index",
            lambda: FaultConnection(original_open_index()),
        )

    assert not cache.put(_key(), _payload())
    assert not tuple(cache.root.glob("*.qdc"))
    assert not cache.enabled
    if phase == "ready":
        assert "failed-publication" in destructive_operations


def test_cache_cancellation_is_checked_at_normal_phase_boundaries(
    tmp_path: Path,
) -> None:
    cache = TrustedDerivedDiskCache(tmp_path / "cache")
    key = _key()
    checks = 0

    def cancel_during_put() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise InterruptedError("cancelled cache put")

    with pytest.raises(InterruptedError, match="cache put"):
        cache.put(key, _payload(), cancel_check=cancel_during_put)
    assert not tuple(cache.root.glob("*.tmp"))


def test_eviction_only_removes_owned_cache_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cache"
    cache = TrustedDerivedDiskCache(
        root,
        max_entry_bytes=4_096,
        max_total_bytes=8_192,
        max_entries=1,
    )
    unrelated = root / "keep-me.txt"
    root.mkdir()
    unrelated.write_text("user data")
    operations: list[str] = []
    monkeypatch.setattr(
        cache,
        "_before_destructive_cache_file",
        lambda _path, operation: operations.append(operation),
    )

    assert cache.put(_key(1), _payload("one"))
    assert cache.put(_key(2), _payload("two"))

    assert len(tuple(root.glob("*.qdc"))) == 1
    assert unrelated.read_text() == "user data"
    assert "eviction" in operations


def test_unproved_temporary_lookalike_is_retained_and_disables_cache(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    corrupt = root / f"{'a' * 64}.{'b' * 32}.tmp"
    corrupt.write_bytes(b"not a qPlot cache frame")
    before = _stable_artifact_snapshot(corrupt)
    cache = TrustedDerivedDiskCache(root)

    assert not cache.put(_key(197), _payload("uncached"))

    assert not cache.enabled
    assert _stable_artifact_snapshot(corrupt) == before


def test_stale_temporary_cleanup_uses_central_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    source_cache = TrustedDerivedDiskCache(source_root)
    key = _key(198)
    assert source_cache.put(key, _payload("framed"))
    source = source_root / trusted_cache_filename(key)
    root = tmp_path / "cache"
    root.mkdir()
    stem = source.stem
    for token in ("1" * 32, "2" * 32):
        (root / f"{stem}.{token}.tmp").write_bytes(source.read_bytes())
    monkeypatch.setattr(cache_module, "TRUSTED_DERIVED_CACHE_MAX_TEMP_FILES", 1)
    cache = TrustedDerivedDiskCache(root)
    operations: list[str] = []
    monkeypatch.setattr(
        cache,
        "_before_destructive_cache_file",
        lambda _path, operation: operations.append(operation),
    )

    assert cache.put(_key(199), _payload("new"))

    assert "stale-temporary" in operations


def test_inventory_overflow_cleanup_uses_central_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    for value in range(4):
        source_root = tmp_path / f"source-{value}"
        source_cache = TrustedDerivedDiskCache(source_root)
        key = _key(210 + value)
        assert source_cache.put(key, _payload(str(value)))
        source = source_root / trusted_cache_filename(key)
        (root / source.name).write_bytes(source.read_bytes())
    monkeypatch.setattr(cache_module, "TRUSTED_DERIVED_CACHE_MAX_CLEANUP_FILES", 4)
    cache = TrustedDerivedDiskCache(root)
    operations: list[str] = []
    monkeypatch.setattr(
        cache,
        "_before_destructive_cache_file",
        lambda _path, operation: operations.append(operation),
    )

    assert not cache.put(_key(214), _payload("overflow"))

    assert "inventory-overflow" in operations
    assert "temporary-cleanup" in operations


def test_eviction_rechecks_selection_atomically_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cache"
    target = root / f"{'9' * 64}.qdc"
    _create_queryable_sqlite_database(target)
    _seed_cache_index(root, target.name)
    before = _database_family_snapshot(target)
    first_database = tmp_path / "first" / "live.db"
    cache = TrustedDerivedDiskCache(
        root,
        max_entry_bytes=4_096,
        max_total_bytes=8_192,
        max_entries=1,
    )
    cache.configure_for_database(
        DatabaseInstance(str(first_database), str(first_database), (7, 195))
    )
    validation_entered = threading.Event()
    release = threading.Event()

    def pause_after_candidate_validation(path: Path, operation: str) -> None:
        if path == target and operation == "eviction":
            validation_entered.set()
            assert release.wait(2.0)

    monkeypatch.setattr(
        cache, "_before_artifact_proof", pause_after_candidate_validation
    )
    outcome: list[bool] = []
    writer = threading.Thread(
        target=lambda: outcome.append(cache.put(_key(195), _payload("new")))
    )
    writer.start()
    assert validation_entered.wait(2.0)

    cache.configure_for_database(DatabaseInstance(str(target), str(target), (7, 196)))
    exclusion_epoch = cache._epoch
    assert not cache.enabled
    release.set()
    writer.join(2.0)

    assert not writer.is_alive()
    assert cache._epoch == exclusion_epoch
    assert outcome == [False]
    assert _database_family_snapshot(target) == before
    _assert_queryable_sqlite_database(target)


@pytest.mark.parametrize(
    "row_kind",
    [
        "parent",
        "absolute",
        "windows-drive",
        "windows-unc",
        "nested",
        "database-main",
        "database-wal",
        "database-journal",
        "database-shm",
        "cache-index",
        "cache-lock",
        "normalized-child",
    ],
)
def test_corrupt_index_deletion_target_disables_cache_without_deleting_anything(
    tmp_path: Path,
    row_kind: str,
) -> None:
    root = tmp_path / "cache"
    database_directory = tmp_path / "database"
    database_directory.mkdir()
    database_path = database_directory / "live.db"
    database_family = tuple(
        Path(f"{database_path}{suffix}") for suffix in ("", "-wal", "-journal", "-shm")
    )
    for index, path in enumerate(database_family):
        path.write_bytes(f"selected-family-{index}".encode())

    escaped = tmp_path / "protected.db"
    escaped.write_bytes(b"outside-cache-root")
    nested = root / "subdir" / f"{'1' * 64}.qdc"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"nested-protected")
    normalized = root / f"{'2' * 64}.qdc"
    normalized.write_bytes(b"normalized-protected")
    row_names = {
        "parent": "../protected.db",
        "absolute": str(database_path),
        "windows-drive": r"C:\protected\file.qdc",
        "windows-unc": r"\\server\share\file.qdc",
        "nested": f"subdir/{'1' * 64}.qdc",
        "database-main": database_path.name,
        "database-wal": f"{database_path.name}-wal",
        "database-journal": f"{database_path.name}-journal",
        "database-shm": f"{database_path.name}-shm",
        "cache-index": ".qplot-derived-cache-index.sqlite3",
        "cache-lock": ".qplot-derived-cache.lock",
        "normalized-child": f"subdir/../{'2' * 64}.qdc",
    }
    _seed_cache_index(root, row_names[row_kind])
    protected = database_family + (escaped, nested, normalized)
    snapshots = {path: _stable_artifact_snapshot(path) for path in protected}
    cache = TrustedDerivedDiskCache(
        root,
        max_entry_bytes=4_096,
        max_total_bytes=8_192,
        max_entries=1,
    )
    cache.configure_for_database(
        DatabaseInstance(str(database_path), str(database_path), (7, 193))
    )

    assert not cache.put(_key(), _payload())
    assert not cache.enabled
    assert {path: _stable_artifact_snapshot(path) for path in protected} == snapshots
    assert not tuple(root.glob("*.tmp"))


def test_posix_cache_root_and_entries_ignore_ordinary_umask(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX modes are not available")
    cache = TrustedDerivedDiskCache(tmp_path / "cache")
    prior = os.umask(0o022)
    try:
        assert cache.put(_key(), _payload())
    finally:
        os.umask(prior)

    entry = cache.root / trusted_cache_filename(_key())
    assert stat.S_IMODE(cache.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600


def test_cache_inside_database_directory_is_disabled(tmp_path: Path) -> None:
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    database_path = database_dir / "live.db"
    instance = DatabaseInstance(str(database_path), str(database_path), (7, 91))
    cache = TrustedDerivedDiskCache(database_dir / "qplot-cache")
    cache.configure_for_database(instance)

    assert not cache.enabled
    assert not cache.put(_key(), _payload())
    assert not tuple(database_dir.rglob("*.qdc"))


@pytest.mark.parametrize(
    "database_name",
    [
        ".qplot-derived-cache-index.sqlite3",
        ".qplot-derived-cache.lock",
        f"{'a' * 64}.qdc",
        f"{'b' * 64}.{'c' * 32}.tmp",
    ],
)
def test_unsafe_configuration_never_removes_selected_database_family_or_lookalikes(
    tmp_path: Path,
    database_name: str,
) -> None:
    first_directory = tmp_path / "first"
    database_directory = tmp_path / "selected"
    first_directory.mkdir()
    database_directory.mkdir()
    database_path = database_directory / database_name
    protected = tuple(
        Path(f"{database_path}{suffix}") for suffix in ("", "-wal", "-journal", "-shm")
    )
    for index, path in enumerate(protected):
        path.write_bytes(f"protected-{index}-{database_name}".encode())

    fixed_lookalikes = (
        database_directory / ".qplot-derived-cache-index.sqlite3",
        database_directory / ".qplot-derived-cache-index.sqlite3-journal",
        database_directory / ".qplot-derived-cache-index.sqlite3-wal",
        database_directory / ".qplot-derived-cache-index.sqlite3-shm",
        database_directory / ".qplot-derived-cache.lock",
        database_directory / f"{'d' * 64}.qdc",
        database_directory / f"{'e' * 64}.{'f' * 32}.tmp",
    )
    for index, path in enumerate(fixed_lookalikes):
        if not path.exists():
            path.write_bytes(f"pre-existing-lookalike-{index}".encode())

    cache = TrustedDerivedDiskCache(database_directory)
    cache.configure_for_database(
        DatabaseInstance(
            str(first_directory / "live.db"),
            str(first_directory / "live.db"),
            (7, 190),
        )
    )
    snapshots = {
        path: _stable_artifact_snapshot(path)
        for path in set(protected + fixed_lookalikes)
    }

    cache.configure_for_database(
        DatabaseInstance(str(database_path), str(database_path), (7, 191))
    )
    cache.configure_for_database(
        DatabaseInstance(str(database_path), str(database_path), (7, 192))
    )

    assert not cache.enabled
    assert not cache.put(_key(), _payload())
    assert {path: _stable_artifact_snapshot(path) for path in snapshots} == snapshots


def test_default_cache_uses_platform_application_cache_root(tmp_path: Path) -> None:
    cache = TrustedDerivedDiskCache()
    expected = Path(trusted_derived_cache_root())
    database_path = tmp_path / "database" / "live.db"
    database_path.parent.mkdir()
    cache.configure_for_database(
        DatabaseInstance(str(database_path), str(database_path), (7, 92))
    )

    assert cache.root == expected
    assert cache.enabled
    assert cache.root != database_path.parent
    assert database_path.parent not in cache.root.parents


def test_more_than_cleanup_window_cannot_escape_global_caps(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    for index in range(4_100):
        (root / f"{index:064x}.qdc").write_bytes(b"remnant")
    cache = TrustedDerivedDiskCache(
        root,
        max_entry_bytes=4_096,
        max_total_bytes=4_096,
        max_entries=1,
    )

    assert not cache.put(_key(), _payload())
    assert not cache.enabled
    assert len(tuple(root.glob("*.qdc"))) == 4_100


def test_overflow_recovery_never_inspects_or_unlinks_more_than_one_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    for index in range(4_100):
        (root / f"{index:064x}.qdc").write_bytes(b"remnant")
    inspected = 0
    directory_entries = 0
    unlinked = 0
    real_stat = Path.stat
    real_unlink = Path.unlink
    real_scandir = os.scandir

    class CountedScandir:
        def __init__(self, path: object) -> None:
            self._iterator = real_scandir(path)
            self._counted = Path(path) == root

        def __enter__(self):
            self._iterator.__enter__()
            return self

        def __exit__(self, *args: object):
            return self._iterator.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal directory_entries
            entry = next(self._iterator)
            if self._counted:
                directory_entries += 1
            return entry

    def counted_stat(path: Path, *args: object, **kwargs: object):
        nonlocal inspected
        if path.parent == root:
            inspected += 1
        return real_stat(path, *args, **kwargs)

    def counted_unlink(path: Path, *args: object, **kwargs: object):
        nonlocal unlinked
        if path.parent == root:
            unlinked += 1
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counted_stat)
    monkeypatch.setattr(Path, "unlink", counted_unlink)
    monkeypatch.setattr(os, "scandir", CountedScandir)
    cache = TrustedDerivedDiskCache(root)

    assert not cache.put(_key(), _payload())
    assert directory_entries <= 4_096
    assert inspected <= 4_096
    assert unlinked <= 4_096


def test_failed_overflow_deletion_disables_writes_and_never_claims_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    for index in range(4_100):
        (root / f"{index:064x}.qdc").write_bytes(b"remnant")
    real_unlink = Path.unlink

    def denied(path: Path, *args: object, **kwargs: object):
        if path.parent == root and path.suffix == ".qdc":
            raise PermissionError("cache cleanup denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    cache = TrustedDerivedDiskCache(root)

    assert not cache.put(_key(), _payload())
    assert not cache.enabled
    assert len(tuple(root.glob("*.qdc"))) == 4_100


def test_two_cache_instances_share_global_entry_and_byte_limits(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    first = TrustedDerivedDiskCache(
        root, max_entry_bytes=4_096, max_total_bytes=4_096, max_entries=1
    )
    second = TrustedDerivedDiskCache(
        root, max_entry_bytes=4_096, max_total_bytes=4_096, max_entries=1
    )

    assert first.put(_key(1), _payload("one"))
    assert second.put(_key(2), _payload("two"))
    assert first.put(_key(3), _payload("three"))

    entries = tuple(root.glob("*.qdc"))
    assert len(entries) <= 1
    assert sum(path.stat().st_size for path in entries) <= 4_096


def test_database_switch_blocks_old_cache_publication_inside_new_database_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_directory = tmp_path / "first"
    new_directory = tmp_path / "new"
    first_directory.mkdir()
    new_directory.mkdir()
    root = new_directory / "cache"
    cache = TrustedDerivedDiskCache(root)
    cache.configure_for_database(
        DatabaseInstance(
            str(first_directory / "live.db"),
            str(first_directory / "live.db"),
            (7, 101),
        )
    )
    entered = threading.Event()
    release = threading.Event()
    destructive_operations: list[str] = []

    def blocked_before_publish() -> None:
        entered.set()
        release.wait(2.0)

    monkeypatch.setattr(cache, "_before_publish", blocked_before_publish)
    monkeypatch.setattr(
        cache,
        "_before_destructive_cache_file",
        lambda _path, operation: destructive_operations.append(operation),
    )
    outcome: list[bool] = []
    worker = threading.Thread(
        target=lambda: outcome.append(cache.put(_key(), _payload()))
    )
    worker.start()
    assert entered.wait(2.0)
    configuration = threading.Thread(
        target=lambda: cache.configure_for_database(
            DatabaseInstance(
                str(new_directory / "live.db"),
                str(new_directory / "live.db"),
                (7, 102),
            )
        )
    )
    configuration.start()
    deadline = time.monotonic() + 2.0
    while cache._epoch == 1 and time.monotonic() < deadline:
        time.sleep(0.002)
    assert cache._epoch == 2
    release.set()
    worker.join(2.0)
    configuration.join(2.0)

    assert not configuration.is_alive()
    assert outcome == [False]
    assert not tuple(root.glob("*.qdc"))
    assert len(tuple(root.glob("*.tmp"))) == 1
    assert destructive_operations == ["invalidated-operation-cleanup"]


def test_database_switch_preserves_completed_preexisting_cache_artifacts(
    tmp_path: Path,
) -> None:
    first_directory = tmp_path / "first"
    new_directory = tmp_path / "new"
    first_directory.mkdir()
    new_directory.mkdir()
    root = new_directory / "cache"
    cache = TrustedDerivedDiskCache(root)
    cache.configure_for_database(
        DatabaseInstance(
            str(first_directory / "live.db"),
            str(first_directory / "live.db"),
            (7, 106),
        )
    )
    assert cache.put(_key(), _payload())
    artifacts = _qplot_cache_artifacts(new_directory)
    assert artifacts
    snapshots = {path: _stable_artifact_snapshot(path) for path in artifacts}

    cache.configure_for_database(
        DatabaseInstance(
            str(new_directory / "live.db"),
            str(new_directory / "live.db"),
            (7, 107),
        )
    )

    assert not cache.enabled
    assert {path: _stable_artifact_snapshot(path) for path in artifacts} == snapshots
    assert not cache.put(_key(2), _payload("after exclusion"))


def test_database_switch_commits_exclusion_while_writer_holds_publication_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_directory = tmp_path / "first"
    new_directory = tmp_path / "new"
    first_directory.mkdir()
    new_directory.mkdir()
    cache = TrustedDerivedDiskCache(new_directory / "cache")
    cache.configure_for_database(
        DatabaseInstance(
            str(first_directory / "live.db"),
            str(first_directory / "live.db"),
            (7, 111),
        )
    )
    entered = threading.Event()
    release = threading.Event()
    original_inventory = cache._ensure_global_inventory

    def blocked_inventory(*args: object, **kwargs: object) -> bool:
        entered.set()
        assert release.wait(2.0)
        return original_inventory(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cache, "_ensure_global_inventory", blocked_inventory)
    put_outcome: list[bool] = []
    writer = threading.Thread(
        target=lambda: put_outcome.append(cache.put(_key(), _payload()))
    )
    writer.start()
    assert entered.wait(2.0)

    configured = threading.Event()
    configuration = threading.Thread(
        target=lambda: (
            cache.configure_for_database(
                DatabaseInstance(
                    str(new_directory / "live.db"),
                    str(new_directory / "live.db"),
                    (7, 112),
                )
            ),
            configured.set(),
        )
    )
    configuration.start()
    assert configured.wait(0.5)
    assert cache._epoch == 2
    assert not cache.enabled
    assert writer.is_alive()
    release.set()
    writer.join(2.0)
    configuration.join(2.0)

    assert not writer.is_alive()
    assert not configuration.is_alive()
    assert put_outcome == [False]
    assert not cache.enabled
    assert not (cache.root / trusted_cache_filename(_key())).exists()
    assert len(tuple(cache.root.glob("*.tmp"))) == 1


def test_two_processes_share_global_entry_and_byte_limits(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    context = get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    processes = [
        context.Process(
            target=_write_cache_entries_in_process,
            args=(str(root), start, ready, release),
        )
        for start in (1, 101)
    ]
    for process in processes:
        process.start()
    for _process in processes:
        assert ready.get(timeout=10.0)
    release.set()
    for process in processes:
        process.join(20.0)
        assert process.exitcode == 0

    entries = tuple(root.glob("*.qdc"))
    assert len(entries) <= 8
    assert sum(path.stat().st_size for path in entries) <= 32_768


@pytest.mark.parametrize("operation", ["get", "put"])
@pytest.mark.parametrize("error_type", [InterruptedError, TimeoutError])
def test_cache_propagates_cancellation_and_deadline_exceptions(
    tmp_path: Path,
    operation: str,
    error_type: type[BaseException],
) -> None:
    cache = TrustedDerivedDiskCache(tmp_path / "cache")

    def cancelled() -> None:
        raise error_type("cache phase stopped")

    with pytest.raises(error_type, match="cache phase stopped"):
        if operation == "get":
            cache.get(_key(), cancel_check=cancelled)
        else:
            cache.put(_key(), _payload(), cancel_check=cancelled)


def test_json_preflight_counts_whitespace_separated_nodes_and_bounds_text() -> None:
    child = b"[" + b",\n\t".join([b"true"] * 65_536) + b"]"
    excessive_nodes = b"[" + child + b",\n\t" + child + b"]"
    with pytest.raises(ValueError, match="too many nodes"):
        _preflight_json(excessive_nodes)
    oversized_text = b'"' + b"x" * (1024 * 1024 + 1) + b'"'
    with pytest.raises(ValueError, match="string is oversized"):
        _preflight_json(oversized_text)


def test_json_preflight_enforces_exact_array_item_boundary_with_whitespace() -> None:
    maximum = TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS
    _preflight_json(b"[\n" + b", \n\t".join([b"null"] * maximum) + b"\n]")

    with pytest.raises(ValueError, match="too many items"):
        _preflight_json(b"[\n" + b", \n\t".join([b"null"] * (maximum + 1)) + b"\n]")


def test_json_preflight_enforces_exact_object_item_boundary_with_whitespace() -> None:
    maximum = TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS
    item = b'"repeated" : null'
    _preflight_json(b"{\n" + b", \n\t".join([item] * maximum) + b"\n}")

    with pytest.raises(ValueError, match="too many items"):
        _preflight_json(b"{\n" + b", \n\t".join([item] * (maximum + 1)) + b"\n}")


def test_json_preflight_tracks_nested_container_and_aggregate_limits_separately() -> (
    None
):
    half = TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS // 2
    child = b"[" + b",\n".join([b"null"] * half) + b"]"
    _preflight_json(b"[" + child + b",\n" + child + b"]")

    excessive_child = (
        b"["
        + b",\n".join([b"null"] * (TRUSTED_DERIVED_CACHE_MAX_CONTAINER_ITEMS + 1))
        + b"]"
    )
    with pytest.raises(ValueError, match="too many items"):
        _preflight_json(b"[[],\n" + excessive_child + b"]")


def test_steady_state_put_does_not_rescan_cache_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cache"
    cache = TrustedDerivedDiskCache(root)
    calls = 0
    original = Path.iterdir

    def counted(path: Path):
        nonlocal calls
        if path == root:
            calls += 1
        return original(path)

    monkeypatch.setattr(Path, "iterdir", counted)
    for index in range(40):
        assert cache.put(_key(index + 1), _payload(str(index)))

    assert calls <= 2


def test_surrogate_cache_key_degrades_to_miss(tmp_path: Path) -> None:
    instance = DatabaseInstance("/data/\ud800.db", "/data/\ud800.db", (7, 11))
    key = TrustedCacheWorkKey(
        instance,
        "guid-1",
        TrustedWorkKind.PREVIEW,
        TrustedSourceRevision(b"revision"),
        "renderer-v1",
    )
    cache = TrustedDerivedDiskCache(tmp_path / "cache")

    assert cache.get(key) is None
    assert not cache.put(key, _payload())
    assert not cache.root.exists()


def test_json_preflight_rejects_depth_and_node_growth_before_decode() -> None:
    with pytest.raises(ValueError, match="deeply nested"):
        _preflight_json(b"[" * 33 + b"]" * 33)
    child = b"[" + b",".join([b'"x"'] * 65_536) + b"]"
    with pytest.raises(ValueError, match="too many nodes"):
        _preflight_json(b"[" + child + b"," + child + b"]")


def test_invalid_payload_schema_degrades_to_uncached_result(tmp_path: Path) -> None:
    payload = _payload()
    payload["status"] = "invented"
    cache = TrustedDerivedDiskCache(tmp_path / "cache")

    assert not cache.put(_key(), payload)
    assert cache.get(_key()) is None
    assert not cache.root.exists()
