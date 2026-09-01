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
  file_stable_seconds: 1
  cli_timeout_seconds: 600

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

summarization:
  enabled: true
  prompt_version: "1"
  max_input_characters: 120000
  chunk_characters: 24000
  chunk_overlap_characters: 1000
  max_chunk_count: 8
  max_total_characters: 960000
  temperature: 0.2
  max_output_tokens: 3000

tags:
  allowed:
    - name: Unknown
      description: Content that cannot yet be classified

web:
  recordings_per_page: 25
  transcript_segments_per_page: 200
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


@pytest.fixture
def forbid_external_effects(monkeypatch):
    """Any subprocess or HTTP call from a web test fails the test.

    GET-purity guard: page/export rendering must never launch MacWhisper
    or query the network. Tests that exercise actions mock at the
    service level instead.
    """

    def _no_subprocess(*args, **kwargs):
        raise AssertionError("subprocess must not be called during web requests")

    def _no_http(*args, **kwargs):
        raise AssertionError("HTTP requests must not be made during web requests")

    monkeypatch.setattr("subprocess.run", _no_subprocess)
    monkeypatch.setattr("subprocess.Popen", _no_subprocess)
    monkeypatch.setattr("httpx.get", _no_http)
    monkeypatch.setattr("httpx.post", _no_http)
    monkeypatch.setattr("httpx.Client", _no_http)
