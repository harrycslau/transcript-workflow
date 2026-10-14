"""Tests for the root ``Brain.app`` Finder launcher bundle.

The bundle's Finder entry point must be a native Mach-O applet (never a
text shell script), so macOS on Apple Silicon never asks for Rosetta. The
real launch behavior lives in the bundled zsh resource; these tests
exercise its branches in a temporary sandbox with substituted commands, so
no browser is opened and no server is started.
"""

from __future__ import annotations

import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLE = REPO_ROOT / "Brain.app"
INFO_PLIST = BUNDLE / "Contents" / "Info.plist"
LAUNCHER = BUNDLE / "Contents" / "Resources" / "Brain-launch.zsh"
BRAIN_URL = "http://127.0.0.1:8787/"

_ZSH = shutil.which("zsh") or ("/bin/zsh" if Path("/bin/zsh").exists() else None)

requires_zsh = pytest.mark.skipif(_ZSH is None, reason="zsh is not available")
requires_darwin = pytest.mark.skipif(
    sys.platform != "darwin", reason="the applet is a macOS Mach-O binary"
)


def _read_plist() -> dict:
    with INFO_PLIST.open("rb") as handle:
        return plistlib.load(handle)


def _executable() -> Path:
    return BUNDLE / "Contents" / "MacOS" / _read_plist()["CFBundleExecutable"]


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_plist_points_to_native_executable_and_custom_icon():
    plist = _read_plist()
    assert plist["CFBundleExecutable"] == "applet"
    assert plist["CFBundleIconFile"] == "Brain"
    assert _executable().is_file()
    icon = BUNDLE / "Contents" / "Resources" / f"{plist['CFBundleIconFile']}.icns"
    assert icon.is_file()
    # The old text entry point must be gone.
    assert not (BUNDLE / "Contents" / "MacOS" / "Brain").exists()
    # No key may force an Intel-only launch (which would require Rosetta).
    assert "LSMinimumSystemVersionByArchitecture" not in plist
    assert "LSArchitecturePriority" not in plist


@pytest.mark.skipif(
    shutil.which("file") is None or shutil.which("lipo") is None,
    reason="file/lipo are not available",
)
def test_entry_point_is_native_arm64_macho():
    executable = _executable()
    file_output = subprocess.run(
        ["file", str(executable)], capture_output=True, text=True, check=True
    ).stdout
    assert "Mach-O" in file_output
    assert "text" not in file_output.lower()
    lipo_output = subprocess.run(
        ["lipo", "-info", str(executable)], capture_output=True, text=True, check=True
    ).stdout
    assert "arm64" in lipo_output


@requires_zsh
def test_bundled_launcher_passes_zsh_syntax_check():
    result = subprocess.run(
        [_ZSH, "-n", str(LAUNCHER)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@requires_darwin
def test_applet_entry_point_invokes_bundled_launcher(tmp_path):
    app = tmp_path / "Brain.app"
    shutil.copytree(BUNDLE, app, symlinks=True)
    probe = app / "Contents" / "Resources" / "Brain-launch.zsh"
    # Mirror the production project-root derivation: the resource lives at
    # <root>/Brain.app/Contents/Resources/Brain-launch.zsh.
    probe.write_text(
        '#!/bin/zsh\n'
        'SCRIPT_DIR="${0:A:h}"\n'
        'ROOT="${SCRIPT_DIR:h:h:h}"\n'
        'echo "$ROOT" > "$ROOT/probe-root.txt"\n'
    )
    probe.chmod(0o755)
    result = subprocess.run(
        [str(app / "Contents" / "MacOS" / "applet")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    observed = Path((tmp_path / "probe-root.txt").read_text().strip())
    assert observed.resolve() == tmp_path.resolve()


def _build_sandbox(tmp_path: Path, *, timeout: int | None = None):
    """Copy the launcher into a fake project with substituted commands.

    The production script is used verbatim except for the absolute tool
    paths and (optionally) the startup timeout, so its real control flow is
    under test while ``open``/``curl``/``osascript``/``nohup``/``sleep``
    are harmless mocks.
    """

    root = tmp_path / "project"
    resources = root / "Brain.app" / "Contents" / "Resources"
    resources.mkdir(parents=True)
    mocks = tmp_path / "mocks"
    mocks.mkdir()

    _write_exec(
        mocks / "curl",
        f'#!/bin/sh\n[ -e "{tmp_path}/healthy" ] && exit 0\nexit 1\n',
    )
    _write_exec(mocks / "open", f'#!/bin/sh\necho "$@" >> "{tmp_path}/open_calls"\n')
    _write_exec(
        mocks / "osascript", f'#!/bin/sh\necho "$@" >> "{tmp_path}/dialogs"\n'
    )
    _write_exec(mocks / "nohup", '#!/bin/sh\nexec "$@"\n')
    _write_exec(mocks / "sleep", "#!/bin/sh\n/bin/sleep 0.05\n")

    text = LAUNCHER.read_text()
    for real, name in (
        ("/usr/bin/open", "open"),
        ("/usr/bin/curl", "curl"),
        ("/usr/bin/osascript", "osascript"),
        ("/usr/bin/nohup", "nohup"),
        ("/bin/sleep", "sleep"),
    ):
        text = text.replace(real, str(mocks / name))
    if timeout is not None:
        text = text.replace(
            "readonly STARTUP_TIMEOUT=45", f"readonly STARTUP_TIMEOUT={timeout}"
        )
    script = resources / "Brain-launch.zsh"
    script.write_text(text)
    script.chmod(0o755)
    return root, script


def _run(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_ZSH, str(script)], capture_output=True, text=True, timeout=60
    )


@requires_zsh
def test_already_running_opens_without_starting_server(tmp_path):
    root, script = _build_sandbox(tmp_path)
    (tmp_path / "healthy").write_text("")

    result = _run(script)

    assert result.returncode == 0
    assert (tmp_path / "open_calls").read_text().strip() == BRAIN_URL
    assert not (tmp_path / "dialogs").exists()
    assert not (root / "data").exists()


@requires_zsh
def test_missing_venv_brain_shows_guidance_and_exits_nonzero(tmp_path):
    _root, script = _build_sandbox(tmp_path)

    result = _run(script)

    assert result.returncode == 1
    assert "uv sync" in (tmp_path / "dialogs").read_text()
    assert not (tmp_path / "open_calls").exists()


@requires_zsh
def test_starts_server_and_opens_once_health_appears(tmp_path):
    root, script = _build_sandbox(tmp_path)
    brain = root / ".venv" / "bin" / "brain"
    brain.parent.mkdir(parents=True)
    _write_exec(
        brain,
        f'#!/bin/sh\necho "$@" >> "{tmp_path}/brain_calls"\n'
        f': > "{tmp_path}/healthy"\n/bin/sleep 2\n',
    )

    result = _run(script)

    assert result.returncode == 0
    assert (tmp_path / "open_calls").read_text().strip() == BRAIN_URL
    assert (tmp_path / "brain_calls").read_text().strip() == "serve"
    assert (root / "data" / "temp" / "brain-app.log").is_file()
    assert not (tmp_path / "dialogs").exists()


@requires_zsh
def test_server_never_healthy_reports_failure(tmp_path):
    root, script = _build_sandbox(tmp_path, timeout=1)
    brain = root / ".venv" / "bin" / "brain"
    brain.parent.mkdir(parents=True)
    _write_exec(brain, "#!/bin/sh\n/bin/sleep 5\n")

    result = _run(script)

    assert result.returncode == 1
    assert "did not respond" in (tmp_path / "dialogs").read_text()
    assert not (tmp_path / "open_calls").exists()
