import json

import pytest
from packaging.tags import Tag
from packaging.version import Version

from qplot.updates import (
    CHECK_INTERVAL_SECONDS,
    MAX_RESPONSE_BYTES,
    check_due,
    latest_release,
)


def release_file(version, **fields):
    return {"filename": f"qplotter-{version}-py3-none-any.whl", "yanked": False, **fields}


def release_list(*files):
    return json.dumps({"name": "qplotter", "files": list(files)}).encode()


def select(data, **options):
    return latest_release(data, python_version="3.12.2",
                          supported_tags=[Tag("py3", "none", "any")], **options)


def test_latest_release_uses_pep440_and_excludes_unusable_distributions():
    data = release_list(
        release_file("1.9"), release_file("1.10"), release_file("2.0rc1"),
        release_file("20.0", yanked=True), release_file("21.0", yanked=""),
        release_file("30.0", **{"requires-python": ">=3.14"}),
        release_file("31.0", **{"requires-python": "not a specifier"}),
        release_file("32.0", filename="different_project-32.0-py3-none-any.whl"),
        release_file("33.0", filename="qplotter-33.0-cp314-cp314-win_amd64.whl"),
        release_file("34.0", filename="qplotter-34.0.tar.gz"),
        release_file("35.0", **{"requires-python": 0}),
    )
    assert select(data) == Version("1.10")
    assert select(data, include_prereleases=True) == Version("2.0rc1")


def test_stable_release_replaces_its_prerelease_and_local_versions_are_ignored():
    data = release_list(release_file("2.0rc1"), release_file("2.0"), release_file("3.0+local"))
    assert select(data) == Version("2.0")
    assert select(data, include_prereleases=True) == Version("2.0")
    assert select(release_list(release_file("3.0b1"))) is None
    assert select(release_list()) is None


@pytest.mark.parametrize("data", [b"not json", b"[]", b'{"name":"another","files":[]}',
                                 b'{"name":"qplotter","files":{}}', b"x" * (MAX_RESPONSE_BYTES + 1)])
def test_invalid_or_excessive_update_response_is_rejected(data):
    with pytest.raises(ValueError):
        select(data)


def test_daily_check_handles_first_launch_and_clock_adjustments():
    assert check_due(0, 100)
    assert not check_due(100, 101)
    assert not check_due(100, 100 + CHECK_INTERVAL_SECONDS - 1)
    assert check_due(100, 100 + CHECK_INTERVAL_SECONDS)
    assert check_due(1000, 100)
