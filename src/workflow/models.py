"""Database models for the Brain ingestion/transcription pipeline.

Design notes:
- ``Recording`` is content identity (SHA-256); ``AudioSource`` is each
  observed local file path. One recording can have several sources.
- ``processing_status`` (pipeline) and ``audio_status`` (file presence)
  are orthogonal: a transcribed recording whose WAV was deleted stays
  ``transcribed`` with ``audio_status=missing``.
- ``RoutingDecision`` and ``ProcessingAttempt`` are append-only history.
  Confirming a routing decision updates the active decision in place
  (``verified_at``/``verified_by``); changing profile appends.
- ``Transcript`` is versioned (one per successful transcription
  attempt); a partial unique constraint keeps at most one active
  transcript per recording.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone


class ProcessingStatus(models.TextChoices):
    DISCOVERED = "discovered", "Discovered"
    HASHING = "hashing", "Hashing"
    ROUTING = "routing", "Routing"
    NEEDS_REVIEW = "needs_review", "Needs Review"
    READY_TO_TRANSCRIBE = "ready_to_transcribe", "Ready to Transcribe"
    TRANSCRIBING = "transcribing", "Transcribing"
    TRANSCRIBED = "transcribed", "Transcribed"
    FAILED = "failed", "Failed"


class AudioStatus(models.TextChoices):
    PRESENT = "present", "Present"
    MISSING = "missing", "Missing"
    # Reserved for Step 6 (not implemented): deleted_by_retention


class FailureStage(models.TextChoices):
    INGEST = "ingest", "Ingest"
    ROUTING = "routing", "Routing"
    TRANSCRIPTION = "transcription", "Transcription"


class AttemptStage(models.TextChoices):
    ROUTING = "routing", "Routing"
    TRANSCRIPTION = "transcription", "Transcription"


class AttemptOutcome(models.TextChoices):
    RUNNING = "running", "Running"
    SUCCESS = "success", "Success"
    TIMEOUT = "timeout", "Timeout"
    NONZERO_EXIT = "nonzero_exit", "Non-zero Exit"
    INVALID_OUTPUT = "invalid_output", "Invalid Output"
    INTERRUPTED = "interrupted", "Interrupted"


class DiscoveryState(models.TextChoices):
    """Persisted pre-hash discovery state for AudioSource rows.

    Rows in OBSERVING/HASHING with a null recording are recovered by the
    next ``brain ingest``: OBSERVING rows re-enter the stability wait,
    HASHING rows are re-hashed (their hash attempt was interrupted), and
    FAILED rows are reset to OBSERVING so hashing is retried.
    """

    OBSERVING = "observing", "Observing stability"
    HASHING = "hashing", "Hashing"
    HASHED = "hashed", "Hashed"
    FAILED = "failed", "Hash failed"


class RoutingMethod(models.TextChoices):
    AUTOMATIC = "automatic", "Automatic"
    MANUAL = "manual", "Manual"


def _uuid() -> str:
    return str(uuid.uuid4())


class Recording(models.Model):
    """Content identity of an audio recording (SHA-256 based)."""

    id = models.CharField(primary_key=True, max_length=36, default=_uuid, editable=False)
    sha256 = models.CharField(max_length=64, unique=True, db_index=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    recorded_at = models.DateTimeField(null=True, blank=True)
    discovered_at = models.DateTimeField(default=timezone.now)
    processing_status = models.CharField(
        max_length=32, choices=ProcessingStatus.choices, default=ProcessingStatus.DISCOVERED
    )
    audio_status = models.CharField(max_length=16, choices=AudioStatus.choices, default=AudioStatus.PRESENT)
    failure_stage = models.CharField(max_length=16, choices=FailureStage.choices, blank=True, default="")
    # Explicit, queryable retranscription-failure marker: set when a
    # retranscription fails while a usable active transcript exists (the
    # recording stays `transcribed`); cleared on successful retry.
    retranscription_failed = models.BooleanField(default=False)
    last_failed_attempt = models.ForeignKey(
        "workflow.ProcessingAttempt", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="failed_for_recordings",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-discovered_at"]

    def __str__(self) -> str:
        return f"Recording({self.sha256[:12]}…, {self.processing_status})"


class AudioSource(models.Model):
    """A local file path that (provably, after hashing) holds this recording."""

    recording = models.ForeignKey(
        Recording, on_delete=models.PROTECT, null=True, blank=True, related_name="sources"
    )
    path = models.TextField(help_text="Absolute, symlink-resolved path as first observed")
    # Case-insensitive identity for macOS filesystems; unique across sources.
    path_identity = models.TextField(unique=True, db_index=True)
    original_filename = models.CharField(max_length=255)
    file_size = models.BigIntegerField(null=True, blank=True)
    file_mtime = models.FloatField(null=True, blank=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    stable_since = models.DateTimeField(null=True, blank=True)
    discovery_state = models.CharField(
        max_length=16, choices=DiscoveryState.choices, default=DiscoveryState.OBSERVING
    )
    presence = models.CharField(max_length=16, choices=AudioStatus.choices, default=AudioStatus.PRESENT)
    missing_at = models.DateTimeField(null=True, blank=True)
    is_canonical = models.BooleanField(default=False)
    discovery_note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["first_seen_at"]

    def __str__(self) -> str:
        return f"AudioSource({self.original_filename}, {self.discovery_state})"


class RoutingDecision(models.Model):
    """Append-only routing history; ``is_active`` marks the effective decision."""

    recording = models.ForeignKey(Recording, on_delete=models.PROTECT, related_name="routing_decisions")
    ordinal = models.PositiveIntegerField()
    route_suggestion = models.CharField(max_length=32)  # cantonese|mandarin|european|uncertain
    profile_name = models.CharField(max_length=64)
    model_id = models.CharField(max_length=128)
    language_arg = models.CharField(max_length=32, null=True, blank=True)
    method = models.CharField(max_length=16, choices=RoutingMethod.choices)
    confidence = models.FloatField(null=True, blank=True)  # router score, not a calibrated probability
    reason_code = models.CharField(max_length=64, blank=True, default="")
    evidence = models.JSONField(default=dict, blank=True)  # bounded metrics/excerpts; never full transcripts
    routing_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["recording", "ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["recording", "ordinal"], name="uniq_routing_ordinal"),
            models.UniqueConstraint(
                fields=["recording"], condition=Q(is_active=True), name="uniq_active_routing_decision"
            ),
        ]

    def __str__(self) -> str:
        return f"RoutingDecision({self.profile_name}, {self.method}, active={self.is_active})"


class ProcessingAttempt(models.Model):
    """Immutable record of one routing or transcription attempt."""

    recording = models.ForeignKey(Recording, on_delete=models.PROTECT, related_name="attempts")
    stage = models.CharField(max_length=16, choices=AttemptStage.choices)
    ordinal = models.PositiveIntegerField()
    model_id = models.CharField(max_length=128, blank=True, default="")
    language_arg = models.CharField(max_length=32, null=True, blank=True)
    cli_args_json = models.JSONField(default=list, blank=True)
    mw_version = models.CharField(max_length=64, blank=True, default="")
    router_version = models.CharField(max_length=32, blank=True, default="")
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=AttemptOutcome.choices, default=AttemptOutcome.RUNNING)
    exit_code = models.IntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["recording", "stage", "ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["recording", "stage", "ordinal"], name="uniq_attempt_ordinal"),
            # At most one unfinished attempt per recording/stage: a second
            # safety layer alongside the pipeline file lock.
            models.UniqueConstraint(
                fields=["recording", "stage"],
                condition=Q(finished_at__isnull=True),
                name="uniq_unfinished_attempt",
            ),
        ]

    def __str__(self) -> str:
        return f"ProcessingAttempt({self.stage}#{self.ordinal}, {self.outcome})"


class Transcript(models.Model):
    """Versioned transcript output; exactly one active per recording."""

    recording = models.ForeignKey(Recording, on_delete=models.PROTECT, related_name="transcripts")
    attempt = models.OneToOneField(ProcessingAttempt, on_delete=models.PROTECT, related_name="transcript")
    is_active = models.BooleanField(default=False)
    activated_at = models.DateTimeField(null=True, blank=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    text_normalized = models.TextField(blank=True, default="")
    mw_json = models.JSONField(null=True, blank=True)
    parser_version = models.CharField(max_length=16, default="1")
    language_observed = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["recording"], condition=Q(is_active=True), name="uniq_active_transcript"
            ),
        ]

    def __str__(self) -> str:
        return f"Transcript({self.recording_id}, active={self.is_active})"


class TranscriptSegment(models.Model):
    transcript = models.ForeignKey(Transcript, on_delete=models.CASCADE, related_name="segments")
    ordinal = models.PositiveIntegerField()
    start_ms = models.BigIntegerField(null=True, blank=True)
    end_ms = models.BigIntegerField(null=True, blank=True)
    speaker = models.CharField(max_length=64, blank=True, default="")
    text = models.TextField()

    class Meta:
        ordering = ["transcript", "ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["transcript", "ordinal"], name="uniq_segment_ordinal"),
        ]

    def __str__(self) -> str:
        return f"TranscriptSegment({self.ordinal})"


class Section(models.Model):
    """Logical part of a transcript. Step 2 creates one whole-recording
    Section per successful transcript; topic splitting arrives in Step 6."""

    transcript = models.ForeignKey(Transcript, on_delete=models.CASCADE, related_name="sections")
    ordinal = models.PositiveIntegerField(default=0)
    title = models.CharField(max_length=255, blank=True, default="")
    start_ms = models.BigIntegerField(null=True, blank=True)
    end_ms = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["transcript", "ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["transcript", "ordinal"], name="uniq_section_ordinal"),
        ]

    def __str__(self) -> str:
        return f"Section({self.title!r})"
