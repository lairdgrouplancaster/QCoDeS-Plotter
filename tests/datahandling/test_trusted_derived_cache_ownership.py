"""Intercept cache writes before real QCoDeS measurement files can be touched."""

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest
from qcodes.dataset import (
    Measurement,
    initialise_or_create_database_at,
    load_or_create_experiment,
)

from qplot.datahandling.file_identity import database_instance
from qplot.datahandling.trusted_derived_cache import TrustedDerivedDiskCache
from qplot.datahandling.trusted_live_queries import TrustedSourceRevision
from qplot.datahandling.trusted_work_scheduler import (
    RenderingOptions,
    TrustedCacheWorkKey,
    TrustedWorkKind,
)


def _measurement(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    initialise_or_create_database_at(str(path), journal_mode="DELETE")
    experiment = load_or_create_experiment("cache ownership", "audit")
    measurement = Measurement(exp=experiment)
    measurement.register_custom_parameter("x")
    measurement.register_custom_parameter("signal", setpoints=("x",))
    try:
        with measurement.run(write_in_background=False) as saver:
            saver.add_result(("x", 1), ("signal", 2))
        return saver.dataset.guid
    finally:
        saver.dataset.conn.close()
        experiment.conn.close()


def _state(path):
    status = path.stat()
    return (
        hashlib.sha256(path.read_bytes()).digest(),
        status.st_size,
        status.st_mtime_ns,
    )


def _cache_inputs(tmp_path):
    source = tmp_path / "measurements" / "source.db"
    guid = _measurement(source)
    instance = database_instance(source)
    key = TrustedCacheWorkKey(
        instance,
        guid,
        TrustedWorkKind.PREVIEW,
        TrustedSourceRevision(b"audit"),
        "audit",
        RenderingOptions(),
    )
    payload = {
        "format": "qplot-trusted-derived-payload-v1",
        "kind": "preview",
        "status": "ok",
        "description": "audit",
        "source": (("revision", b"audit"),),
        "images": (),
    }
    root = tmp_path / "cache"
    cache = TrustedDerivedDiskCache(root)
    cache.configure_for_database(instance)
    return source, key, payload, cache


@pytest.mark.parametrize(
    "target,seed_index",
    [
        ("lock", False),
        ("journal", False),
        ("wal", False),
        ("shm", False),
        ("journal", True),
        ("wal", True),
        ("shm", True),
    ],
)
def test_cache_refuses_foreign_measurement_ownership(
    tmp_path,
    monkeypatch,
    target,
    seed_index,
):
    source, key, payload, cache = _cache_inputs(tmp_path)
    root = cache.root
    index = root / ".qplot-derived-cache-index.sqlite3"
    if seed_index:
        assert cache.put(key, payload)
    foreign = root / (
        ".qplot-derived-cache.lock" if target == "lock" else f"{index.name}-{target}"
    )
    _measurement(foreign)
    before = _state(foreign)
    source_before = _state(source)
    attempts = []
    original_open = os.open
    original_connect = sqlite3.connect

    def guard_open(path, flags, *args, **kwargs):
        if (
            Path(path) == foreign
            and flags & (os.O_RDWR | os.O_WRONLY)
            and not flags & os.O_EXCL
        ):
            attempts.append("writable open of foreign measurement")
            raise OSError("intercepted forbidden write access")
        return original_open(path, flags, *args, **kwargs)

    def guard_connect(path, *args, **kwargs):
        if target != "lock" and str(path) == str(index):
            attempts.append("mutable SQLite open with foreign sidecar")
            raise OSError("intercepted forbidden SQLite sidecar access")
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guard_open)
    monkeypatch.setattr(sqlite3, "connect", guard_connect)
    assert not cache.put(key, payload)
    assert not cache.enabled
    assert _state(foreign) == before
    assert _state(source) == source_before
    assert index.exists() == seed_index
    assert not tuple(root.glob("*.tmp"))
    assert not attempts


@pytest.mark.parametrize("contents", [b"", b"\0"])
def test_existing_owned_lock_remains_usable(tmp_path, contents):
    source, key, payload, cache = _cache_inputs(tmp_path)
    cache.root.mkdir()
    lock = cache.root / ".qplot-derived-cache.lock"
    lock.write_bytes(contents)
    before = _state(source)

    assert cache.put(key, payload)
    assert cache.get(key) == payload
    reopened = TrustedDerivedDiskCache(cache.root)
    reopened.configure_for_database(database_instance(source))
    assert reopened.put(key, payload)
    assert reopened.get(key) == payload
    assert _state(source) == before
