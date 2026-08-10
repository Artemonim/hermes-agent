#!/usr/bin/env python3
"""Run the Python-specific stages of Hermes' fork-local AE2 pipeline."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import Any


MAX_REPORTED_ISSUES = 20


def make_result(
    stage: str,
    status: str,
    note: str,
    *,
    details: dict[str, Any] | None = None,
    issues: list[dict[str, Any]] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a result compatible with the AgentEnforcer2 report schema."""
    return {
        "name": stage,
        "status": status,
        "note": note,
        "duration_ms": 0,
        "details": details or {},
        "issues": issues or [],
        "metrics": metrics or {},
    }


def write_stage_log(
    log_path: Path,
    command: list[str],
    stdout: str,
    stderr: str,
    exit_code: int,
) -> None:
    """Persist complete command output outside the compact JSON report."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        (
            f"command: {' '.join(command)}",
            f"exit_code: {exit_code}",
            "",
            "[stdout]",
            stdout.rstrip(),
            "",
            "[stderr]",
            stderr.rstrip(),
        )
    )
    log_path.write_text(f"{body}\n", encoding="utf-8")


def run_command(command: list[str], root: Path, log_path: Path) -> subprocess.CompletedProcess[str]:
    """Run a fixed local command without a shell and store its full output."""
    completed = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
    )
    write_stage_log(log_path, command, completed.stdout, completed.stderr, completed.returncode)
    return completed


def parse_diagnostics(output: str, tool: str) -> list[dict[str, Any]]:
    """Return a bounded diagnostic projection while preserving full output in logs."""
    issues: list[dict[str, Any]] = []
    try:
        structured_output = json.loads(output)
    except json.JSONDecodeError:
        structured_output = None

    if isinstance(structured_output, list):
        for finding in structured_output:
            if not isinstance(finding, dict):
                continue
            location = finding.get("location")
            path = ""
            line = None
            if isinstance(location, dict):
                path = str(location.get("path") or "")
                positions = location.get("positions")
                if isinstance(positions, dict):
                    begin = positions.get("begin")
                    if isinstance(begin, dict):
                        line = begin.get("line")
            prefix = f"{path}:{line}: " if path and line else f"{path}: " if path else ""
            message = finding.get("description") or finding.get("message")
            issues.append(
                {
                    "language": "python",
                    "tool": tool,
                    "rule": str(finding.get("check_name") or "diagnostic"),
                    "count": 1,
                    "message": f"{prefix}{message or json.dumps(finding, ensure_ascii=False)}",
                }
            )
            if len(issues) >= MAX_REPORTED_ISSUES:
                break
        if issues:
            return issues

    for line in output.splitlines():
        message = line.strip()
        if not message:
            continue
        issues.append(
            {
                "language": "python",
                "tool": tool,
                "rule": "diagnostic",
                "count": 1,
                "message": message,
            }
        )
        if len(issues) >= MAX_REPORTED_ISSUES:
            break
    return issues


def stage_lint(root: Path, log_path: Path) -> dict[str, Any]:
    """Run the repository's already-blocking Ruff policy."""
    if importlib.util.find_spec("ruff") is None:
        return make_result(
            "lint",
            "fail",
            "Ruff is unavailable in the selected Python environment; run `uv sync --locked --extra dev`.",
        )

    command = [sys.executable, "-m", "ruff", "check", "."]
    completed = run_command(command, root, log_path)
    output = f"{completed.stdout}{completed.stderr}"
    if completed.returncode == 0:
        return make_result("lint", "ok", "ruff check passed.", details={"command": command})
    return make_result(
        "lint",
        "fail",
        "ruff check failed.",
        details={"command": command, "exit_code": completed.returncode},
        issues=parse_diagnostics(output, "ruff"),
    )


def stage_typecheck(root: Path, log_path: Path) -> dict[str, Any]:
    """Run Hermes' advisory Ty check without turning existing debt into a hard gate."""
    if importlib.util.find_spec("ty") is None:
        return make_result(
            "typecheck",
            "warn",
            "Ty is unavailable in the selected Python environment; run `uv sync --locked --extra dev`.",
        )

    command = [sys.executable, "-m", "ty", "check", "--output-format", "gitlab", "--exit-zero"]
    completed = run_command(command, root, log_path)
    output = f"{completed.stdout}{completed.stderr}".strip()
    issues = parse_diagnostics(completed.stdout, "ty")
    if not issues and completed.stderr:
        issues = parse_diagnostics(completed.stderr, "ty")
    if completed.returncode != 0:
        return make_result(
            "typecheck",
            "fail",
            "ty check could not run.",
            details={"command": command, "exit_code": completed.returncode},
            issues=issues,
        )
    if output:
        return make_result(
            "typecheck",
            "warn",
            "ty reported advisory diagnostics.",
            details={"command": command},
            issues=issues,
        )
    return make_result("typecheck", "ok", "ty check has no diagnostics.", details={"command": command})


def git_files(root: Path, pattern: str) -> list[Path]:
    """Return tracked plus non-ignored files matched by one Git pathspec."""
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", pattern],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git ls-files failed: {stderr or completed.returncode}")
    return [root / item for item in completed.stdout.decode("utf-8", errors="surrogateescape").split("\0") if item]


def compile_python_files(files: list[Path], root: Path) -> list[dict[str, Any]]:
    """Compile Python source in memory so the stage does not create bytecode artifacts."""
    issues: list[dict[str, Any]] = []
    for path in files:
        try:
            with tokenize.open(path) as source_file:
                source = source_file.read()
            compile(source, str(path), "exec", dont_inherit=True)
        except (OSError, SyntaxError, UnicodeError) as error:
            try:
                display_path = path.relative_to(root).as_posix()
            except ValueError:
                display_path = str(path)
            line = getattr(error, "lineno", None)
            location = f"{display_path}:{line}" if line else display_path
            issues.append(
                {
                    "language": "python",
                    "tool": "compile",
                    "rule": type(error).__name__,
                    "count": 1,
                    "message": f"{location}: {error}",
                }
            )
            if len(issues) >= MAX_REPORTED_ISSUES:
                break
    return issues


def stage_compile(root: Path, log_path: Path) -> dict[str, Any]:
    """Syntax-check tracked and non-ignored Python files without direct pytest usage."""
    try:
        files = git_files(root, "*.py")
    except RuntimeError as error:
        return make_result("compile", "fail", str(error))

    issues = compile_python_files(files, root)
    log_output = "\n".join(issue["message"] for issue in issues) or "All Python files compiled successfully."
    write_stage_log(log_path, [sys.executable, "compile"], log_output, "", 1 if issues else 0)
    if issues:
        return make_result(
            "compile",
            "fail",
            f"Python syntax check found {len(issues)} issue(s).",
            details={"checked_files": len(files)},
            issues=issues,
        )
    return make_result(
        "compile",
        "ok",
        f"Python syntax check passed for {len(files)} file(s).",
        details={"checked_files": len(files)},
    )


def changed_paths(root: Path) -> tuple[list[str], str]:
    """Collect branch, working-tree, staged, and untracked paths for the fast profile."""
    baseline = "working tree only (main is unavailable)"
    commands: list[tuple[str, list[str]]] = []
    main_ref = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "main"],
        capture_output=True,
        check=False,
    )
    if main_ref.returncode == 0:
        baseline = "main...HEAD plus working tree"
        commands.append(
            (
                "main...HEAD",
                ["git", "-C", str(root), "diff", "--name-only", "-z", "main...HEAD"],
            )
        )
    commands.extend(
        (
            ("working tree", ["git", "-C", str(root), "diff", "--name-only", "-z"]),
            ("staging area", ["git", "-C", str(root), "diff", "--cached", "--name-only", "-z"]),
            ("untracked files", ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"]),
        )
    )
    paths: set[str] = set()
    for command_name, command in commands:
        completed = subprocess.run(command, capture_output=True, check=False)
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"{command_name} changed-file lookup failed: {stderr or completed.returncode}")
        paths.update(item for item in completed.stdout.decode("utf-8", errors="surrogateescape").split("\0") if item)
    return sorted(paths), baseline


def parse_lane_output(output: str) -> dict[str, bool]:
    """Extract boolean lane decisions emitted by the existing CI classifier."""
    lanes: dict[str, bool] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and value in {"true", "false"}:
            lanes[key] = value == "true"
    return lanes


def stage_changed(root: Path, log_path: Path) -> dict[str, Any]:
    """Reuse Hermes' current path classifier for the fast-profile lane selection."""
    try:
        paths, baseline = changed_paths(root)
    except RuntimeError as error:
        return make_result("changed", "fail", str(error))

    classifier = root / "scripts/ci/classify_changes.py"
    if not classifier.is_file():
        return make_result("changed", "fail", "Existing CI change classifier is missing.")
    completed = subprocess.run(
        [sys.executable, str(classifier)],
        cwd=root,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        input="\n".join(paths),
        text=True,
    )
    write_stage_log(log_path, [sys.executable, str(classifier)], completed.stdout, completed.stderr, completed.returncode)
    if completed.returncode != 0:
        return make_result("changed", "fail", "Existing CI change classifier failed.")

    test_files = [path for path in paths if path.startswith("tests/") and path.endswith(".py")]
    return make_result(
        "changed",
        "ok",
        f"Detected {len(paths)} working-tree change(s).",
        details={
            "baseline": baseline,
            "changed_files": paths,
            "lanes": parse_lane_output(completed.stdout),
            "test_files": test_files,
        },
    )


def parse_arguments() -> argparse.Namespace:
    """Parse the narrow, fixed interface used by build.ps1."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("changed", "lint", "typecheck", "compile"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--log-path", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    """Execute one stage and write exactly one compact JSON result to stdout."""
    args = parse_arguments()
    root = args.root.resolve()
    log_path = args.log_path.resolve()
    handlers = {
        "changed": stage_changed,
        "lint": stage_lint,
        "typecheck": stage_typecheck,
        "compile": stage_compile,
    }
    result = handlers[args.stage](root, log_path)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
