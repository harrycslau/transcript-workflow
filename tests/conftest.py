"""Test session setup.

This module runs at import time, BEFORE pytest-django initializes Django,
so that ``BRAIN_CONFIG`` points at an isolated, network-free test
configuration (unreachable loopback endpoint, nonexistent mw path).
No test requires MacWhisper, a running oMLX server, network access, or
real audio.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="brain-tests-"))
_CONFIG_PATH = _TEST_ROOT / "config.yaml"

_CONFIG_PATH.write_text(
    f"""\
storage:
  inbox: {_TEST_ROOT}/inbox
  database: {_TEST_ROOT}/database/brain.sqlite3
  transcripts: {_TEST_ROOT}/transcripts
  exports: {_TEST_ROOT}/exports
  logs: {_TEST_ROOT}/logs
  temp: {_TEST_ROOT}/temp

macwhisper:
  command: {_TEST_ROOT}/nonexistent/mw
  model: null
  language: auto
  speakers: true
  output_format: json

llm:
  provider: openai_compatible
  base_url: http://127.0.0.1:1/v1
  model: ""
  api_key_env: BRAIN_TEST_LLM_API_KEY
  temperature: 0.2
  timeout_seconds: 600

embedding:
  base_url: http://127.0.0.1:1/v1
  model: ""
  api_key_env: BRAIN_TEST_LLM_API_KEY

retention:
  enabled: false
  audio_days: 3
  delete_mode: permanent
  require_transcript: true
  require_summary: true

initial_tags:
  - name: Unknown
    description: Content that cannot yet be classified
""",
    encoding="utf-8",
)

os.environ["BRAIN_CONFIG"] = str(_CONFIG_PATH)
os.environ["DJANGO_SETTINGS_MODULE"] = "brain.settings"
os.environ.pop("BRAIN_TEST_LLM_API_KEY", None)


@pytest.hookimpl()
def pytest_configure(config):
    # pytest-django's early init runs before conftest import, so Django is
    # fully set up here, after this module has pointed DJANGO_SETTINGS_MODULE
    # and BRAIN_CONFIG at the isolated test configuration.
    import django

    django.setup()


@pytest.fixture
def config(tmp_path):
    from factories import make_config

    return make_config(tmp_path)
