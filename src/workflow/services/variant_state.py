"""Centralized SummaryVariantState reconciliation (single writer).

Every runtime path that changes a summary variant's lifecycle state —
successful persistence, generation failure, interruption recovery, and
manual source-language correction — delegates to
:func:`reconcile_variant_state`. No other module may assign
``SummaryVariantState`` fields or the Recording-level default summary
tuple directly. (Migration 0007 keeps a historical-model-compatible
local equivalent whose semantics mirror this module; it is documented
in the migration file.)

The reconciler derives truth from the database inside one transaction:

1. locks the Recording row;
2. verifies the transcript belongs to the recording and the section
   belongs to the transcript (inconsistent identity aborts with
   :class:`VariantScopeError` and NO partial writes);
3. queries the exact active ``Summary`` for
   (transcript, section, output_language);
4. queries and validates the newest exact matching finished failed
   attempt: same recording (by query), same transcript, same section,
   same resolved output language, stage ``summarization``, a finished
   failure outcome, with durable provenance in ``context_json``;
5. derives the complete variant tuple:
   - ``current`` requires an active Summary for the exact scope;
   - ``failed`` requires no active Summary plus an exact matching
     finished failed attempt;
   - ``missing`` requires no active Summary and no matching failure;
   - ``regeneration_failed`` requires ``status=current`` AND a matching
     failed attempt whose ``ordinal`` is NEWER than the active
     Summary's attempt (ordinal, not wall-clock: ``finished_at`` is
     used only for display/secondary ordering).
6. updates the Recording-level default tuple ONLY when all of:
   - the transcript is currently active;
   - the section is that transcript's ordinal-0 section;
   - the output language equals the transcript's currently derived
     default output language.
   An optional or historical variant NEVER overwrites Recording-level
   default fields.

The supplied output-language identity is validated at the boundary
(``languages.is_valid_output_identity``): it must already be canonical,
Chinese-family output must be exactly ``zh-Hant`` (never ``yue``,
``cmn``, ``zh-HK``, …), and the migration-only ``und`` marker is not a
runtime output identity. Invalid identities raise
:class:`VariantScopeError` with no writes of any kind.
"""

from __future__ import annotations

from django.db import transaction

from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    ProcessingAttempt,
    Recording,
    Section,
    Summary,
    SummaryState,
    SummaryVariantState,
    Transcript,
)
from workflow.services.languages import is_valid_output_identity
from workflow.services.langresolve import resolve_default_language

# Outcomes that count as a "finished failed" summarization attempt for
# attribution. ``interrupted`` is included: recovery closes an
# interrupted attempt as a finished failure of the exact provenanced
# scope (and only there).
FAILURE_OUTCOMES = frozenset(
    {
        AttemptOutcome.TIMEOUT,
        AttemptOutcome.NONZERO_EXIT,
        AttemptOutcome.INVALID_OUTPUT,
        AttemptOutcome.UNREACHABLE,
        AttemptOutcome.HTTP_ERROR,
        AttemptOutcome.RESPONSE_TOO_LARGE,
        AttemptOutcome.INPUT_TOO_LARGE,
        AttemptOutcome.INTERRUPTED,
    }
)


class VariantScopeError(Exception):
    """Variant identity is inconsistent; nothing was written."""


def has_scope_provenance_ids(attempt: ProcessingAttempt) -> bool:
    """True when the attempt carries transcript/section scope ids (the
    structural part of provenance, regardless of resolved validity)."""
    ctx = attempt.context_json or {}
    lang = ctx.get("language", {})
    if not isinstance(lang, dict):
        return False
    return bool(
        str(lang.get("transcript_id", "")).strip()
        and str(lang.get("section_id", "")).strip()
    )


def has_complete_scope_provenance(attempt: ProcessingAttempt) -> bool:
    """True when the attempt durably proves its exact summary scope.

    Complete provenance means a ``language`` object in ``context_json``
    with non-empty ``transcript_id``, ``section_id`` and ``resolved``,
    where ``resolved`` is a canonical output-language identity (see
    :func:`languages.is_valid_output_identity`). Provenance-less
    (legacy) attempts and attempts with malformed or noncanonical
    resolved values can never be attributed to a scope by inference.
    """
    ctx = attempt.context_json or {}
    lang = ctx.get("language", {})
    if not isinstance(lang, dict):
        return False
    resolved = str(lang.get("resolved", "")).strip()
    if not (
        str(lang.get("transcript_id", "")).strip()
        and str(lang.get("section_id", "")).strip()
        and resolved
    ):
        return False
    return is_valid_output_identity(resolved)


def is_detection_attempt(attempt: ProcessingAttempt) -> bool:
    """True for source-language-detection attempts.

    Detection attempts (explicit Original with unknown source) are NOT
    summary-variant generation events. Recovery must never attribute
    them to a variant or to the Recording-level default state.
    """
    ctx = attempt.context_json or {}
    return ctx.get("language_detection") is True


def _matching_failure(
    recording: Recording,
    transcript: Transcript,
    section: Section,
    output_language: str,
    *,
    newer_than_ordinal: int | None = None,
) -> ProcessingAttempt | None:
    """Newest finished failed attempt exactly matching the scope.

    A failure is attributable only when durable provenance proves the
    exact recording (by query), transcript, section, and resolved
    output language. Ambiguous legacy attempts match nothing. Ordinal
    is the primary ordering; ``finished_at``/``pk`` are deterministic
    secondary ordering.
    """
    for attempt in ProcessingAttempt.objects.filter(
        recording=recording,
        stage=AttemptStage.SUMMARIZATION,
        outcome__in=FAILURE_OUTCOMES,
        finished_at__isnull=False,
    ).order_by("-ordinal", "-pk"):
        if newer_than_ordinal is not None and attempt.ordinal <= newer_than_ordinal:
            break  # ordered by -ordinal; nothing newer remains
        lang = (attempt.context_json or {}).get("language", {})
        if not isinstance(lang, dict):
            continue
        if (
            str(lang.get("transcript_id", "")) == str(transcript.pk)
            and str(lang.get("section_id", "")) == str(section.pk)
            and lang.get("resolved", "") == output_language
        ):
            return attempt
    return None


def reconcile_variant_state(
    *,
    recording: Recording,
    transcript: Transcript,
    section: Section,
    output_language: str,
    triggering_attempt: ProcessingAttempt | None = None,
) -> SummaryVariantState:
    """Derive and persist the complete variant state from the database.

    ``triggering_attempt`` (informational) documents which event asked
    for reconciliation; the reconciler never trusts it for state
    derivation — the active Summary and matching failure are queried
    from the database. Callers must hold the pipeline lock (they
    already do: every mutating path is locked).
    """
    if not output_language:
        raise VariantScopeError("output_language is required")
    if not is_valid_output_identity(output_language):
        # Malformed, noncanonical, source-style Chinese (yue/zh-HK/...),
        # or the migration-only "und" marker: never create or mutate a
        # variant row under such an identity.
        raise VariantScopeError(
            f"invalid output-language identity: {output_language!r}"
        )
    with transaction.atomic():
        rec = Recording.objects.select_for_update().get(pk=recording.pk)
        # Identity checks — inconsistent identity aborts without writes.
        if transcript.recording_id != rec.pk:
            raise VariantScopeError("transcript does not belong to the recording")
        if section.transcript_id != transcript.pk:
            raise VariantScopeError("section does not belong to the transcript")

        summary = (
            Summary.objects.filter(
                transcript=transcript,
                section=section,
                output_language=output_language,
                is_active=True,
            )
            .select_related("attempt")
            .first()
        )

        if summary is not None:
            if summary.attempt_id and summary.attempt is not None:
                matching = _matching_failure(
                    rec,
                    transcript,
                    section,
                    output_language,
                    newer_than_ordinal=summary.attempt.ordinal,
                )
            else:
                # Without an attempt ordinal on the active Summary the
                # regeneration-recency rule cannot be verified; be
                # conservative and never claim regeneration_failed.
                matching = None
            status = SummaryVariantState.VariantStatus.CURRENT
            regen_failed = matching is not None
        else:
            matching = _matching_failure(rec, transcript, section, output_language)
            status = (
                SummaryVariantState.VariantStatus.FAILED
                if matching is not None
                else SummaryVariantState.VariantStatus.MISSING
            )
            regen_failed = False

        vs, _ = SummaryVariantState.objects.get_or_create(
            transcript=transcript,
            section=section,
            output_language=output_language,
        )
        vs = SummaryVariantState.objects.select_for_update().get(pk=vs.pk)
        vs.status = status
        vs.regeneration_failed = regen_failed
        vs.last_failed_attempt = matching
        vs.failed_at = matching.finished_at if matching is not None else None
        vs.activated_at = summary.activated_at if summary is not None else None
        vs.save()

        # Recording-level default reconciliation: only when this exact
        # variant IS the currently derived default of the ACTIVE
        # transcript's ordinal-0 section.
        if (
            transcript.is_active
            and section.ordinal == 0
            and output_language == resolve_default_language(transcript)
        ):
            if status == SummaryVariantState.VariantStatus.CURRENT:
                expected = {
                    "summary_status": SummaryState.CURRENT,
                    "resummarization_failed": regen_failed,
                    "last_failed_attempt": matching if regen_failed else None,
                }
            elif status == SummaryVariantState.VariantStatus.FAILED:
                expected = {
                    "summary_status": SummaryState.FAILED,
                    "resummarization_failed": False,
                    "last_failed_attempt": matching,
                }
            else:
                expected = {
                    "summary_status": SummaryState.MISSING,
                    "resummarization_failed": False,
                    "last_failed_attempt": None,
                }
            changed = any(
                getattr(rec, field) != value for field, value in expected.items()
            )
            if changed:
                for field, value in expected.items():
                    setattr(rec, field, value)
                rec.save(update_fields=list(expected.keys()))
        return vs
