"""Tests for the flock-based pipeline lock."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from workflow.services.pipeline_lock import EXIT_BUSY, PipelineBusy, pipeline_lock

from factories import make_config

pytestmark = pytest.mark.django_db


def test_lock_acquires_and_releases(tmp_path):
    config = make_config(tmp_path)
    with pipeline_lock(config):
        lock_file = Path(config.storage.temp) / "locks" / "pipeline.lock"
        assert lock_file.exists()
    # After release, acquiring again must succeed.
    with pipeline_lock(config):
        pass


def test_second_process_is_rejected(tmp_path):
    config = make_config(tmp_path)
    with pipeline_lock(config):
        with pytest.raises(PipelineBusy) as excinfo:
            with pipeline_lock(config):
                pass
    assert "another pipeline process is active" in str(excinfo.value)


def test_lock_contention_exit_code(tmp_path):
    """A second process hits contention and receives PipelineBusy."""
    config = make_config(tmp_path)
    script = (
        "import sys, time;\n"
        "sys.path.insert(0, 'src'); sys.path.insert(0, 'tests');\n"
        "from pathlib import Path;\n"
        "from factories import make_config;\n"
        "from workflow.services.pipeline_lock import pipeline_lock;\n"
        f"cfg = make_config(Path({str(tmp_path)!r}));\n"
        "with pipeline_lock(cfg):\n"
        "    time.sleep(5)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", script])
    import time

    time.sleep(1.0)
    try:
        with pytest.raises(PipelineBusy) as excinfo:
            with pipeline_lock(config):
                pass
        assert excinfo.value.holder_pid
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_crash_releases_lock(tmp_path):
    """No stale-lock handling is needed: killing the holder releases it."""
    config = make_config(tmp_path)
    script = (
        "import sys, os;\n"
        "sys.path.insert(0, 'src'); sys.path.insert(0, 'tests');\n"
        "from pathlib import Path;\n"
        "from factories import make_config;\n"
        "from workflow.services.pipeline_lock import pipeline_lock;\n"
        f"cfg = make_config(Path({str(tmp_path)!r}));\n"
        "with pipeline_lock(cfg):\n"
        "    os._exit(9)\n"  # crash while holding the lock
    )
    subprocess.run([sys.executable, "-c", script], check=False)
    with pipeline_lock(config):
        pass  # must succeed despite the crashed holder
