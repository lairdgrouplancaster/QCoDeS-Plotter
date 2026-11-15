"""Coverage must observe child-only work without changing ordinary test runs."""

import json
import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import coverage
import pytest

pytestmark = pytest.mark.pure


@pytest.mark.parametrize("enabled", [False, True], ids=["ordinary", "measured"])
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "xdist"])
def test_child_coverage_is_opt_in_and_survives_changed_working_directory(
    tmp_path, enabled, workers,
):
    repository = Path(__file__).resolve().parents[2]
    config_path = repository / ".github/coverage-subprocess.toml"
    if not config_path.is_file():
        pytest.skip("Hosted coverage profile is not included in sdists")
    # Keep the actual project's measurement options, replacing only paths for
    # this tiny probe. The parent must not execute the child function itself.
    settings = tomllib.loads(config_path.read_text(encoding="utf-8"))["tool"]["coverage"]
    settings["run"]["source"] = [str(tmp_path)]
    probe_config = tmp_path / "coverage.toml"
    probe_config.write_text(
        "[tool.coverage.run]\n" + "\n".join(
            f"{key} = {json.dumps(value)}" for key, value in settings["run"].items()
        ) + "\n",
        encoding="utf-8",
    )
    worker_source = textwrap.dedent('''\
        import json
        import os
        import sys
        from pathlib import Path

        def child(mode, target):
            if mode == "spawn":
                result = "spawn branch"
            elif mode == "isolated":
                result = "isolated branch"
            else:
                result = "subprocess branch"
            measured = "coverage" in sys.modules and sys.modules["coverage"].Coverage.current() is not None
            Path(target).write_text(json.dumps({"result": result, "measured": measured, "pid": os.getpid()}))

        if __name__ == "__main__":
            child(sys.argv[1], sys.argv[2])
    ''')
    worker = tmp_path / "coverage_worker.py"
    worker.write_text(worker_source, encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_children.py").write_text(textwrap.dedent('''\
        import multiprocessing
        import os
        import subprocess
        import sys
        from pathlib import Path
        from coverage_worker import child

        def test_children():
            root = Path(__file__).parent
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            os.chdir(elsewhere)
            for mode, flags in (("ordinary", []), ("isolated", ["-I"])):
                subprocess.run([sys.executable, *flags, str(root / "coverage_worker.py"),
                                mode, str(root / (mode + ".json"))], check=True, timeout=15)
            process = multiprocessing.get_context("spawn").Process(
                target=child, args=("spawn", str(root / "spawn.json")))
            try:
                process.start()
                process.join(15)
                assert process.exitcode == 0
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                process.close()
    '''), encoding="utf-8")
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("COVERAGE_", "COV_CORE_", "PYTEST_"))
    }
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["COVERAGE_FILE"] = str(tmp_path / ".coverage")
    command = [sys.executable, "-m", "pytest", "-q", "-p", "pytest_cov.plugin",
               "--cov", f"--cov-config={probe_config}", "--cov-report=", "-p", "no:cacheprovider"]
    if not enabled:
        command.append("--no-cov")
    if workers:
        command.extend(["-p", "xdist.plugin", "-n", str(workers)])
    result = subprocess.run(command, cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    children = [json.loads((tmp_path / (name + ".json")).read_text())
                for name in ("ordinary", "isolated", "spawn")]
    assert [row["result"] for row in children] == [
        "subprocess branch", "isolated branch", "spawn branch",
    ]
    assert all(row["measured"] is enabled for row in children)
    assert all(row["pid"] != os.getpid() for row in children)
    if enabled:
        data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
        data.read()
        child_only_lines = {number for number, line in enumerate(worker_source.splitlines(), 1)
                            if "result = " in line}
        assert len(child_only_lines) == 3
        assert child_only_lines <= set(data.lines(str(worker)) or [])
        assert data.has_arcs()
        assert not list((tmp_path / "elsewhere").glob(".coverage*"))
    else:
        assert not list(tmp_path.rglob(".coverage*"))
