"""Known test leaks are removed without touching repository content."""

import importlib.util
import sqlite3
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[1] / "scripts/run_tests_parallel.py"
    spec = importlib.util.spec_from_file_location("artifact_cleanup_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sentinel(root, suffix="c"):
    return root / (
        "C\uf03aUsersExampleTemphermes-pytest-tmproot-example"
        "pytest-of-examplepytest-0test_approved_command_genuine_0"
        f"cmd_started_{suffix}"
    )


def test_cleanup_preserves_tracked_and_unrecognized_files(runner, tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    sentinel = _sentinel(tmp_path)
    sentinel.touch()
    tracked = _sentinel(tmp_path, "d")
    tracked.touch()
    # * An index-only entry avoids read-only Git objects leaking on Windows.
    blob = subprocess.run(
        ["git", "hash-object", "--stdin"], input=b"", capture_output=True, check=True,
    ).stdout.decode("ascii").strip()
    subprocess.run(
        ["git", "-C", str(tmp_path), "update-index", "--add", "--cacheinfo",
         f"100644,{blob},{tracked.name}"], check=True,
    )
    database_dir = tmp_path / "MagicMock/mock._session_db.db_path"
    database_dir.mkdir(parents=True)
    database = database_dir / "123456"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
    connection.close()
    lock = database.with_suffix(".quarantine.lock")
    lock.touch()
    unrelated = [
        tmp_path / "cmd_started_c", database_dir / "notes.txt",
        database_dir / "987654", tmp_path / "debug.log",
        database_dir / "654321.fts_rebuild.lock",
        tmp_path / _sentinel(tmp_path).name.replace("example", "nonempty"),
    ]
    for path in unrelated:
        path.write_text("keep", encoding="utf-8")

    runner._cleanup_test_artifacts(tmp_path)

    assert not any(path.exists() for path in (sentinel, database, lock))
    assert tracked.exists()
    assert all(path.read_text(encoding="utf-8") == "keep" for path in unrelated)


@pytest.mark.parametrize("outcome", [0, 1, KeyboardInterrupt, "nested"])
def test_run_scope_cleans_after_success_failure_or_interrupt(
    runner, tmp_path, monkeypatch, outcome,
):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    if outcome == "nested":
        monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path))
    else:
        monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    sentinel = _sentinel(tmp_path)

    def run_tests():
        with runner._test_artifact_cleanup(tmp_path):
            sentinel.touch()
            if outcome is KeyboardInterrupt:
                raise KeyboardInterrupt
            return outcome

    if outcome is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            run_tests()
    else:
        assert run_tests() == outcome
    assert sentinel.exists() == (outcome == "nested")
