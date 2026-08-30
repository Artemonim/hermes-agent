"""Regression: Windows Desktop update retry must re-probe --keep-stash.

The 2026-08-30 Desktop update probed ``--keep-stash`` against a ``dev``
checkout, switched to ``main`` (argparse no longer knew the flag), then
retried with the original argv. argparse exited 2, which the hand-off treats
as the "close all Hermes windows" sentinel, so the update died and relaunched
the previous Desktop against a backend that had jumped to upstream ``main``.

Linux CI cannot run the PowerShell hand-off; the executable proof is the
script's ``-SelfTestKeepStashRetry`` fixture (``windows_only``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_PS1 = REPO_ROOT / "scripts" / "desktop-update" / "windows.ps1"


@pytest.mark.windows_only
def test_keep_stash_retry_reprobes_after_failed_first_attempt(tmp_path):
    """First update exits 1 with --keep-stash; retry must drop the flag."""
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        pytest.skip(f"Windows PowerShell not found at {powershell}")

    env = {
        **os.environ,
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "HERMES_SELFTEST_PYTHON": sys.executable,
    }
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WINDOWS_PS1),
            "-SelfTestKeepStashRetry",
            "-NoUi",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert "KEEP-STASH RETRY SELF-TEST: PASS" in result.stdout, (
        "The Windows update hand-off reused --keep-stash on retry after the "
        "first attempt mutated the argparse surface. That is argparse exit 2 "
        "on a successful tree switch — the 2026-08-30 Desktop update failure.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"-SelfTestKeepStashRetry exited {result.returncode}.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


@pytest.mark.windows_only
def test_handoff_prefers_config_yaml_updates_branch_over_desktop_default(tmp_path):
    """Stale Desktop passes -Branch main; config.yaml updates.branch must win."""
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        pytest.skip(f"Windows PowerShell not found at {powershell}")

    env = {
        **os.environ,
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
    }
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WINDOWS_PS1),
            "-SelfTestYamlBranch",
            "-NoUi",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert "YAML BRANCH SELF-TEST: PASS" in result.stdout, (
        "The Windows update hand-off still trusts Desktop's hardcoded "
        "-Branch main when config.yaml names another updates.branch. "
        "That is the 2026-08-30 switch onto upstream main.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"-SelfTestYamlBranch exited {result.returncode}.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
