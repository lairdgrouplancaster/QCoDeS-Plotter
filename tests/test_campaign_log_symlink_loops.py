"""Unusable diagnostic paths must not prevent startup or logging recovery."""

import logging
import os

import pytest

from qplot import diagnostics


@pytest.fixture(autouse=True)
def isolated_logging():
    diagnostics._reset_logging_for_tests()
    yield
    diagnostics._reset_logging_for_tests()


def _symlink(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Symlink creation unavailable: {error}")


def _file_state(path):
    state = path.lstat()
    return tuple(getattr(state, field) for field in (
        "st_dev", "st_ino", "st_mode", "st_nlink", "st_size",
        "st_mtime_ns", "st_ctime_ns",
    ))


def _link_state(paths):
    return [(os.readlink(path), _file_state(path)) for path in paths]


@pytest.mark.parametrize("layout", ["self", "pair", "parent"])
def test_cyclic_log_path_is_nonfatal_and_preserved(tmp_path, layout):
    target = tmp_path / "qplot.log"
    if layout == "parent":
        parent = tmp_path / "logs"
        _symlink(parent, parent.name, directory=True)
        target = parent / "qplot.log"
        links = [parent]
    elif layout == "pair":
        other = tmp_path / "other.log"
        _symlink(target, other.name)
        _symlink(other, target.name)
        links = [target, other]
    else:
        _symlink(target, target.name)
        links = [target]
    before = _link_state(links)

    logger = diagnostics.configure_logging(target)
    diagnostics.log_event("Startup can continue without a writable log")

    assert any(isinstance(handler, logging.NullHandler) for handler in logger.handlers)
    diagnostics._reset_logging_for_tests()
    assert _link_state(links) == before


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot rename an open log.")
def test_cyclic_old_log_does_not_block_new_logging_destination(tmp_path):
    previous = tmp_path / "previous.log"
    detached = tmp_path / "detached.log"
    current = tmp_path / "current.log"
    logger = diagnostics.configure_logging(previous)
    logger.info("Original diagnostic")
    previous.rename(detached)
    _symlink(previous, previous.name)
    previous_state = _link_state([previous])
    detached_state = detached.read_bytes(), _file_state(detached)

    assert diagnostics.configure_logging(current) is logger
    logger.info("Recovered diagnostic")
    diagnostics._reset_logging_for_tests()

    assert "Recovered diagnostic" in current.read_text()
    assert _link_state([previous]) == previous_state
    assert (detached.read_bytes(), _file_state(detached)) == detached_state
