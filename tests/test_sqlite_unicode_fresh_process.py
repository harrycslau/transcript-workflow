"""Fresh-process reproduction for Unicode Title sorting (Step 5A.1).

Reproduces the original lifecycle bug: in a brand-new Django process the
very FIRST database operation is the Title-sort path. The old design kept
a process-global ``_registered`` flag set only by ``connection_created``,
so the first Title sort in a fresh process raised
``RuntimeError: The 'unicode_fold' SQLite collation is not registered``.

The subprocess uses an ISOLATED temporary configuration/database — the
user's real database and config are never opened or modified.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FRESH_SCRIPT = r"""
import django, os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "brain.settings")
django.setup()

from django.db import connections
from django.utils import timezone
from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    Section,
    Summary,
    SummaryState,
    Transcript,
)
from workflow.query import ListFilters, apply_filters, recording_list_queryset

# This process has opened NO database connection yet: connections are lazy.
assert connections["default"].connection is None, "expected a fresh process"

# FIRST database operation: construct AND execute the Title-sort path on
# an empty (migrated) database. The old code raised a RuntimeError here
# because no connection had been opened, so connection_created had not
# fired and the global flag was still False.
qs = apply_filters(
    recording_list_queryset(), ListFilters(sort="title_az"), "Europe/Helsinki"
)
assert list(qs) == []


def seed(title: str) -> None:
    rec = Recording.objects.create(
        sha256=title,
        duration_seconds=60.0,
        processing_status=ProcessingStatus.TRANSCRIBED,
        summary_status=SummaryState.CURRENT,
    )
    attempt = ProcessingAttempt.objects.create(
        recording=rec,
        stage=AttemptStage.TRANSCRIPTION,
        ordinal=1,
        outcome=AttemptOutcome.SUCCESS,
        finished_at=timezone.now(),
    )
    transcript = Transcript.objects.create(recording=rec, attempt=attempt)
    transcript.is_active = True
    transcript.activated_at = timezone.now()
    transcript.save()
    section = Section.objects.create(transcript=transcript, ordinal=0, title="Full")
    summary_attempt = ProcessingAttempt.objects.create(
        recording=rec,
        stage=AttemptStage.SUMMARIZATION,
        ordinal=1,
        outcome=AttemptOutcome.SUCCESS,
        finished_at=timezone.now(),
    )
    Summary.objects.create(
        recording=rec,
        transcript=transcript,
        section=section,
        attempt=summary_attempt,
        ordinal=1,
        is_active=True,
        title=title,
        overview="Overview",
        key_points=[],
        action_items=[],
        people=[],
        organizations=[],
        topics=[],
        language="en",
        output_language="en",
        suggested_tags_raw={"suggested": [], "rejected": []},
        model_id="m",
        prompt_version="1",
        parser_version="1",
        chunk_count=1,
        input_characters=1,
        generation_mode="automatic",
    )


for title in ("Åland", "äiti", "Örebro"):
    seed(title)

rows = list(
    apply_filters(
        recording_list_queryset(), ListFilters(sort="title_az"), "Europe/Helsinki"
    )
)
got = [row.display_title for row in rows]
assert got == ["äiti", "Åland", "Örebro"], got
print("OK fresh-process title-sort")
"""


def _write_isolated_config(tmp_path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""\
storage:
  inbox: {tmp_path}/inbox
  database: {tmp_path}/database/brain.sqlite3
  transcripts: {tmp_path}/transcripts
  exports: {tmp_path}/exports
  logs: {tmp_path}/logs
  temp: {tmp_path}/temp

macwhisper:
  command: {tmp_path}/nonexistent/mw
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
    return cfg


def test_fresh_process_title_sort_is_first_database_operation(tmp_path):
    cfg = _write_isolated_config(tmp_path)
    env = dict(os.environ)
    env["BRAIN_CONFIG"] = str(cfg)
    env["DJANGO_SETTINGS_MODULE"] = "brain.settings"
    env.pop("BRAIN_TEST_LLM_API_KEY", None)

    # Build the isolated schema in a SEPARATE process so the fresh
    # process below genuinely starts with no open connection.
    migrated = subprocess.run(
        [sys.executable, "-m", "django", "migrate", "--noinput"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr

    fresh = subprocess.run(
        [sys.executable, "-c", FRESH_SCRIPT],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert "OK fresh-process title-sort" in fresh.stdout
    assert "RuntimeError" not in fresh.stderr