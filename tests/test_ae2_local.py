"""Unit tests for the fork-local AgentEnforcer2 Python stage adapter."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from subprocess import CompletedProcess
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER_PATH = REPO_ROOT / "scripts" / "ci" / "ae2_local.py"


@pytest.fixture(scope="module")
def ae2_local() -> ModuleType:
    """Load the standalone adapter without turning scripts/ into a package."""
    spec = importlib.util.spec_from_file_location("ae2_local", ADAPTER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_result_has_the_shared_stage_schema(ae2_local: ModuleType) -> None:
    result = ae2_local.make_result("lint", "ok", "ruff check passed.")

    assert result == {
        "name": "lint",
        "status": "ok",
        "note": "ruff check passed.",
        "duration_ms": 0,
        "details": {},
        "issues": [],
        "metrics": {},
    }


def test_parse_lane_output_uses_only_boolean_classifier_values(ae2_local: ModuleType) -> None:
    lanes = ae2_local.parse_lane_output(
        "python=true\nfrontend=false\nsummary=ignored\nmalformed=true \n"
    )

    assert lanes == {"python": True, "frontend": False}


def test_parse_diagnostics_projects_gitlab_json(ae2_local: ModuleType) -> None:
    diagnostics = ae2_local.parse_diagnostics(
        '[{"check_name":"invalid-assignment","description":"value has an incompatible type",'
        '"location":{"path":"tools/example.py","positions":{"begin":{"line":12}}}}]',
        "ty",
    )

    assert diagnostics == [
        {
            "language": "python",
            "tool": "ty",
            "rule": "invalid-assignment",
            "count": 1,
            "message": "tools/example.py:12: value has an incompatible type",
        }
    ]


def test_typecheck_uses_structured_stdout_when_stderr_has_a_warning(
    ae2_local: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    completed = CompletedProcess(
        args=["ty"],
        returncode=0,
        stdout=(
            '[{"check_name":"panic","description":"checker panic",'
            '"location":{"path":"tools/example.py","positions":{"begin":{"line":2}}}}]'
        ),
        stderr="WARN Some files were skipped.\n",
    )
    monkeypatch.setattr(ae2_local.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(ae2_local, "run_command", lambda *_: completed)

    result = ae2_local.stage_typecheck(tmp_path, tmp_path / "typecheck.log")

    assert result["status"] == "warn"
    assert result["issues"][0]["rule"] == "panic"
    assert result["issues"][0]["message"] == "tools/example.py:2: checker panic"


def test_changed_paths_compares_dev_branch_against_main(
    ae2_local: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    responses = iter((b"main\n", b"committed.py\0", b"unstaged.py\0", b"staged.py\0", b"untracked.py\0"))
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> CompletedProcess[bytes]:
        commands.append(command)
        return CompletedProcess(command, 0, stdout=next(responses), stderr=b"")

    monkeypatch.setattr(ae2_local.subprocess, "run", fake_run)

    paths, baseline = ae2_local.changed_paths(tmp_path)

    assert paths == ["committed.py", "staged.py", "unstaged.py", "untracked.py"]
    assert baseline == "main...HEAD plus working tree"
    assert ["git", "-C", str(tmp_path), "diff", "--name-only", "-z", "main...HEAD"] in commands


def test_compile_python_files_reports_syntax_error(ae2_local: ModuleType, tmp_path: Path) -> None:
    source_path = tmp_path / "broken.py"
    source_path.write_text("def broken(:\n", encoding="utf-8")

    issues = ae2_local.compile_python_files([source_path], tmp_path)

    assert len(issues) == 1
    assert issues[0]["tool"] == "compile"
    assert issues[0]["rule"] == "SyntaxError"
    assert issues[0]["message"].startswith("broken.py:")


def test_compile_python_files_keeps_valid_source_bytecode_free(ae2_local: ModuleType, tmp_path: Path) -> None:
    source_path = tmp_path / "valid.py"
    source_path.write_text("VALUE = 42\n", encoding="utf-8")

    issues = ae2_local.compile_python_files([source_path], tmp_path)

    assert issues == []
    assert not (tmp_path / "__pycache__").exists()
