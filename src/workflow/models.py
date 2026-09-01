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
    SUMMARIZATION = "summarization", "Summarization"


class AttemptStage(models.TextChoices):
    ROUTING = "routing", "Routing"
    TRANSCRIPTION = "transcription", "Transcription"
    SUMMARIZATION = "summarization", "Summarization"


class AttemptOutcome(models.TextChoices):
    RUNNING = "running", "Running"
    SUCCESS = "success", "Success"
    TIMEOUT = "timeout", "Timeout"
    NONZERO_EXIT = "nonzero_exit", "Non-zero Exit"
    INVALID_OUTPUT = "invalid_output", "Invalid Output"
    INTERRUPTED = "interrupted", "Interrupted"
    UNREACHABLE = "unreachable", "Endpoint Unavailable"
    HTTP_ERROR = "http_error", "HTTP Error"
    RESPONSE_TOO_LARGE = "response_too_large", "Response Too Large"
    INPUT_TOO_LARGE = "input_too_large", "Input Too Large"


class SummaryState(models.TextChoices):
    """Orthogonal summarization status for a Recording.

    Never surfaces in ``processing_status``: a transcription success can
    never look pipeline-failed because summarization failed.

    - ``not_ready``: no active transcript (nothing to summarize yet).
    - ``missing``: an active transcript exists but has no current summary
      (never attempted, or a new transcript superseded the old one).
    - ``current``: the recording's current summary is up to date.
    - ``failed``: the last summarization attempt failed and there is no
      current summary. ``brain run`` never auto-retries this state.
    """

    NOT_READY = "not_ready", "No Active Transcript"
    MISSING = "missing", "Missing"
    CURRENT = "current", "Current"
    FAILED = "failed", "Failed"


class GenerationMode(models.TextChoices):
    AUTOMATIC = "automatic", "Automatic"
    MANUAL = "manual", "Manual"


class TagOrigin(models.TextChoices):
    SUGGESTED = "suggested", "Model Suggested"
    MANUAL = "manual", "Manual"
    CONFIRMED = "confirmed", "User Confirmed"  # reserved for Step 4


class TagDeactivatedBy(models.TextChoices):
    """Who deactivated a TagAssignment row (empty while the row is active).

    ``user`` is an explicit suppression: future re-summarization keeps
    recording the model's suggestions (SummaryTagSuggestion provenance)
    but never reactivates the effective assignment. ``model`` means the
    current summary version simply stopped suggesting the tag; a future
    suggestion may reactivate it.
    """

    NONE = "", "Not deactivated"
    MODEL = "model", "Model"
    USER = "user", "User"


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
    # Orthogonal summarization state (see SummaryState). Failed
    # summarization never changes processing_status.
    summary_status = models.CharField(
        max_length=16, choices=SummaryState.choices, default=SummaryState.NOT_READY
    )
    # Failed re-summarization while a current summary exists: the current
    # summary stays active; explicit retry/regeneration clears the marker.
    resummarization_failed = models.BooleanField(default=False)
    last_failed_attempt = models.ForeignKey(
        "workflow.ProcessingAttempt", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="failed_for_recordings",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-discovered_at"]

    def current_summary(self) -> Summary | None:
        """The recording's current summary: the active Summary belonging
        to the active Transcript's whole-recording Section.

        Derived, not constrained: when a new transcript becomes active,
        the old transcript's Summary stays active in its own scope
        (historically valid) but is no longer current for the recording.
        """
        return (
            Summary.objects.filter(
                transcript__recording=self,
                transcript__is_active=True,
                section__ordinal=0,
                is_active=True,
            )
            .select_related("transcript", "section")
            .first()
        )

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
    outcome = models.CharField(max_length=32, choices=AttemptOutcome.choices, default=AttemptOutcome.RUNNING)
    exit_code = models.IntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    # Bounded structured provenance (e.g. input normalization, multi-run
    # speakers fallback). Never contains transcript text, raw model
    # output, prompts, or secrets. cli_args_json keeps its historical
    # per-stage shape.
    context_json = models.JSONField(null=True, blank=True)

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


class Tag(models.Model):
    """A configured tag definition.

    Rows are created/updated/retired by synchronization with the YAML
    ``tags.allowed`` list; they are never deleted, so historical
    suggestions and assignments keep their FK even after a tag is
    removed from configuration (``is_configured=False`` = retired).
    """

    name = models.CharField(max_length=64, help_text="Display name, preserved from first synchronization")
    name_key = models.CharField(
        max_length=64, unique=True, db_index=True, help_text="NFC + strip + casefold identity"
    )
    description = models.TextField(blank=True, default="")
    is_configured = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"Tag({self.name}, configured={self.is_configured})"


class Summary(models.Model):
    """Versioned structured summary; one active per (transcript, section) scope.

    Step 3 always writes the whole-recording Summary (the transcript's
    ordinal-0 Section); Step 6 section-level summaries reuse the same
    scope semantics. ``recording`` is denormalized for ordinal/audit
    constraints; the "current summary of a recording" is derived (see
    ``Recording.current_summary``), not constrained.
    """

    id = models.CharField(primary_key=True, max_length=36, default=_uuid, editable=False)
    recording = models.ForeignKey(Recording, on_delete=models.PROTECT, related_name="summaries")
    transcript = models.ForeignKey(Transcript, on_delete=models.PROTECT, related_name="summaries")
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="summaries")
    attempt = models.OneToOneField(ProcessingAttempt, on_delete=models.PROTECT, related_name="summary")
    ordinal = models.PositiveIntegerField()
    is_active = models.BooleanField(default=False)
    activated_at = models.DateTimeField(null=True, blank=True)
    superseded_at = models.DateTimeField(null=True, blank=True)

    # Validated structured payload (canonical representation).
    title = models.CharField(max_length=200)
    overview = models.TextField()
    key_points = models.JSONField(default=list, blank=True)
    action_items = models.JSONField(default=list, blank=True)
    people = models.JSONField(default=list, blank=True)
    organizations = models.JSONField(default=list, blank=True)
    topics = models.JSONField(default=list, blank=True)
    language = models.CharField(max_length=32, blank=True, default="")

    # Provenance / audit. suggested_tags_raw records the model's raw
    # suggestion list including names rejected for not being configured.
    suggested_tags_raw = models.JSONField(default=dict, blank=True)
    model_id = models.CharField(max_length=128, blank=True, default="")
    llm_base_url = models.CharField(max_length=255, blank=True, default="")
    prompt_version = models.CharField(max_length=16, default="1")
    parser_version = models.CharField(max_length=16, default="1")
    config_fingerprint = models.CharField(max_length=64, blank=True, default="")
    chunk_count = models.PositiveIntegerField(default=1)
    input_characters = models.PositiveIntegerField(default=0)
    input_truncated = models.BooleanField(default=False)  # always False in Step 3
    limits_used = models.JSONField(default=dict, blank=True)
    generation_mode = models.CharField(max_length=16, choices=GenerationMode.choices)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["recording", "ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["recording", "ordinal"], name="uniq_summary_ordinal"),
            # Exactly one active Summary per (transcript, section) scope.
            # Summaries of older transcripts stay is_active=True forever
            # (historically valid, not current for the recording).
            models.UniqueConstraint(
                fields=["transcript", "section"],
                condition=Q(is_active=True),
                name="uniq_active_summary_in_scope",
            ),
        ]

    def __str__(self) -> str:
        return f"Summary(#{self.ordinal}, active={self.is_active}, {self.title[:40]!r})"


class SummaryTagSuggestion(models.Model):
    """Append-only provenance: which tags a specific Summary version suggested."""

    summary = models.ForeignKey(Summary, on_delete=models.CASCADE, related_name="tag_suggestions")
    tag = models.ForeignKey(Tag, on_delete=models.PROTECT, related_name="summary_suggestions")
    suggested_by_model = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["summary", "tag__name"]
        constraints = [
            models.UniqueConstraint(fields=["summary", "tag"], name="uniq_summary_tag_suggestion"),
        ]

    def __str__(self) -> str:
        return f"SummaryTagSuggestion({self.tag.name} for summary #{self.summary_id})"


class TagAssignment(models.Model):
    """Effective tag on a recording, with origin and provenance.

    One row per (recording, tag); ``is_active`` marks the effective
    assignment. Model regeneration replaces only ``suggested`` rows;
    ``manual`` rows are never modified by re-summarization.
    """

    recording = models.ForeignKey(Recording, on_delete=models.PROTECT, related_name="tag_assignments")
    tag = models.ForeignKey(Tag, on_delete=models.PROTECT, related_name="assignments")
    origin = models.CharField(max_length=16, choices=TagOrigin.choices)
    source_summary = models.ForeignKey(
        Summary, on_delete=models.SET_NULL, null=True, blank=True, related_name="assignments"
    )
    is_active = models.BooleanField(default=True)
    # Deactivation actor (see TagDeactivatedBy). Enforced by the
    # chk_tagassignment_deactivation_state constraint: active rows must
    # carry "" and inactive rows must carry "user" or "model".
    deactivated_by = models.CharField(
        max_length=16, blank=True, default="", choices=TagDeactivatedBy.choices
    )
    created_at = models.DateTimeField(default=timezone.now)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["recording", "tag__name"]
        constraints = [
            models.UniqueConstraint(fields=["recording", "tag"], name="uniq_tag_assignment"),
            # Redundant beside uniq_tag_assignment (one row per pair);
            # kept for schema stability, never relied upon for race
            # handling (which uses transactions + the row uniqueness).
            models.UniqueConstraint(
                fields=["recording", "tag"],
                condition=Q(is_active=True),
                name="uniq_active_tag_assignment",
            ),
            models.CheckConstraint(
                check=Q(is_active=True, deactivated_by="")
                | Q(is_active=False, deactivated_by__in=["user", "model"]),
                name="chk_tagassignment_deactivation_state",
            ),
        ]

    def __str__(self) -> str:
        return f"TagAssignment({self.tag.name}, {self.origin}, active={self.is_active})"
