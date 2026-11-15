"""Shared fixtures and assertions for database generation."""

from pathlib import Path

from qplot import testdata as testdata_module

__all__ = [
    'complete_artifact_state',
    'small_specification',
]


def small_specification():
    return testdata_module.RunSpecification(
        (
            testdata_module.MeasurementSpecification(
                "current",
                "Current",
                "nA",
                (
                    testdata_module.AxisSpecification(
                        "V_SD", "Source-drain voltage", "V", -1.0, 1.0, 5
                    ),
                ),
            ),
        )
    )


def complete_artifact_state(database_path):
    # Reading may update atime (including this audit's own read_bytes call).
    # Preserve content, identity, permissions and modification/change checks;
    # access time is not evidence of a database write.
    artifacts = {}
    for suffix in ("", *testdata_module._SQLITE_SIDECAR_SUFFIXES):
        artifact_path = Path(f"{database_path}{suffix}")
        try:
            contents = artifact_path.read_bytes()
            status = artifact_path.stat()
        except FileNotFoundError:
            artifacts[suffix] = None
            continue
        artifacts[suffix] = (
            status.st_dev,
            status.st_ino,
            status.st_mode,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
            contents,
        )
    return artifacts
