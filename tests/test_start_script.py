import shutil
import subprocess
from pathlib import Path

import pytest

PS = shutil.which("powershell") or shutil.which("pwsh")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "start-local.ps1"
pytestmark = pytest.mark.skipif(PS is None, reason="PowerShell unavailable")


def run(*args):
    return subprocess.run([PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
                          capture_output=True, text=True, timeout=20)


def test_script_parses():
    quoted = str(SCRIPT).replace("'", "''")
    result = run("-Command", "$tokens=$null; $errors=$null; "
                 f"[System.Management.Automation.Language.Parser]::ParseFile('{quoted}', [ref]$tokens, [ref]$errors) | Out-Null; "
                 "$errors | ForEach-Object { Write-Output $_ }; if ($errors.Count) { exit 1 }")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("flags,reload", [([], False), (["-Dev"], True), (["-Host", "127.0.0.1"], False)])
def test_dry_run(flags, reload):
    result = run("-File", str(SCRIPT), *flags, "-DryRun")
    assert result.returncode == 0, result.stderr
    expected = "main:app --reload --host" if reload else "main:app --host"
    assert expected + " 127.0.0.1 --port 8000" in result.stdout
    assert "npm run dev -- --host 127.0.0.1 --port 5173" in result.stdout
    assert ("--reload" in result.stdout) is reload


@pytest.mark.parametrize("flags", [[], ["-Dev"]])
def test_backend_command_bounds_graceful_shutdown(flags):
    # Without this, uvicorn waits for running suite jobs forever and the shutdown cleanup
    # (kill_live_processes) never runs; one Ctrl+C must reach it after at most 3 s.
    result = run("-File", str(SCRIPT), *flags, "-DryRun")
    assert result.returncode == 0, result.stderr
    backend = next(line for line in result.stdout.splitlines() if line.startswith("backend:"))
    assert "--timeout-graceful-shutdown 3" in backend
    assert "--timeout-graceful-shutdown" not in result.stdout.split("frontend:")[1]


def test_bind_host_rejects_command_separator():
    assert run("-File", str(SCRIPT), "-BindHost", "a;b", "-DryRun").returncode != 0


def test_dry_run_custom_python_and_ports():
    result = run("-File", str(SCRIPT), "-Python", "C:\\Owner's Apps\\python.exe",
                 "-ApiPort", "9000", "-WebPort", "6000", "-DryRun")
    assert result.returncode == 0, result.stderr
    assert "& 'C:\\Owner''s Apps\\python.exe' -m uvicorn" in result.stdout
    assert "--port 9000" in result.stdout and "--port 6000" in result.stdout
