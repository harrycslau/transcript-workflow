"""Multilingual summary variants: output_language, SummaryVariantState, constraint swap.

Operation ordering (reverse-critical):

1. Add fields / create the new model (schema only).
2. Validate the pre-migration invariant (one active Summary per
   ``(transcript, section)`` under 0006). Valid 0006 data proceeds
   normally; corrupted legacy data FAILS the migration clearly and
   atomically — historical rows are never deactivated or rewritten to
   force-fit the new constraint. The constraint swap below is safe at
   this point precisely because the invariant guarantees no collisions
   even while every ``output_language`` is still empty.
3. Constraint swap + indexes (schema only; safe on validated data).
4. ``RunPython`` data backfill LAST. This matters for reversibility:
   reverse unapplies operations in reverse order, so the irreversible
   backfill is the FIRST operation hit on reverse — Django raises
   ``IrreversibleError`` before ANY schema or data mutation, never
   mid-way through a partially reversed schema.

Reverse migration is not supported: the data transformation involves
prose-based heuristic classification that cannot be reversed
deterministically. ``RunPython`` therefore carries no ``reverse_code``.

Language helpers below are a historical-model-compatible local copy of
``workflow.services.languages`` (single documented policy: BCP-47
canonical casing — primary lowercase, script Titlecase, region
uppercase; Chinese family zh/yue/cmn[*] → ``zh-Hant`` output) and of
``workflow.services.variant_state`` (exact-scope failure attribution
and regeneration recency by attempt ordinal). The copies must not
drift: any semantic change to those modules requires re-reviewing this
migration's helpers.
"""

from __future__ import annotations

import re

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
from django.db.models import Q


# ---------------------------------------------------------------------------
# Migration-local language helpers (mirror workflow.services.languages)
# ---------------------------------------------------------------------------

_CHINESE_FAMILY_PRIMARY = ("zh", "yue", "cmn")

_LANG_TAG_RE = re.compile(
    r"^(?P<language>[A-Za-z]{2,3})"
    r"(?:-(?P<script>[A-Za-z]{4}))?"
    r"(?:-(?P<region>[A-Za-z]{2}|[0-9]{3}))?$"
)


def _canonicalize_language(code):
    """Canonical casing (primary lower, script Title, region UPPER) or ""."""
    raw = (code or "").strip()
    if not raw:
        return ""
    match = _LANG_TAG_RE.match(raw)
    if match is None:
        return ""
    parts = [match.group("language").lower()]
    if match.group("script"):
        script = match.group("script")
        parts.append(script[0].upper() + script[1:].lower())
    if match.group("region"):
        parts.append(match.group("region").upper())
    return "-".join(parts)


def _is_chinese_family(language_code: str) -> bool:
    code = _canonicalize_language(language_code)
    if not code:
        return False
    primary = code.split("-", 1)[0]
    return primary in _CHINESE_FAMILY_PRIMARY


_ZH_HANT = "zh-Hant"


def _canonicalize_stored_language(lang):
    """Canonicalize a stored language code for output_language assignment.

    Chinese-family codes (zh, zh-*, yue, yue-*, cmn, cmn-*) → "zh-Hant".
    Valid non-Chinese BCP-47 codes → canonical casing.
    Malformed/empty → None.
    """
    canonical = _canonicalize_language(lang)
    if not canonical:
        return None
    if _is_chinese_family(canonical):
        return _ZH_HANT
    return canonical


def _prose_cjk_ratio(summary):
    """Collect bounded prose from a Summary and compute CJK character ratio."""
    parts = [summary.title or "", summary.overview or ""]
    for kp in summary.key_points or []:
        if isinstance(kp, dict):
            parts.append(kp.get("text", ""))
        elif isinstance(kp, str):
            parts.append(kp)
    for ai in summary.action_items or []:
        if isinstance(ai, dict):
            parts.append(ai.get("text", ""))
    prose = " ".join(parts)
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", prose))
    visible = sum(not c.isspace() for c in prose)
    return cjk, visible, cjk / max(1, visible)


# ---------------------------------------------------------------------------
# Migration-local failure attribution (mirrors variant_state semantics)
# ---------------------------------------------------------------------------

_FAILURE_OUTCOMES = (
    "timeout",
    "nonzero_exit",
    "invalid_output",
    "unreachable",
    "http_error",
    "response_too_large",
    "input_too_large",
    "interrupted",
)


def _validate_invariant(apps, schema_editor):
    """Fail the migration clearly on corrupted legacy data.

    The valid 0006 schema enforces one active Summary per
    (transcript, section); such data can never collide after
    output_language is assigned. If corrupted data violates the
    invariant, this raises and the whole migration aborts atomically —
    no winner is selected and no historical rows are deactivated.
    """
    alias = schema_editor.connection.alias
    Summary = apps.get_model("workflow", "Summary")
    seen = set()
    for summary in Summary.objects.using(alias).filter(is_active=True).iterator():
        key = (summary.transcript_id, summary.section_id)
        if key in seen:
            raise RuntimeError(
                "Migration 0007: corrupted legacy data — multiple active Summaries "
                "for transcript %s / section %s. The pre-migration invariant is "
                "violated; the migration aborted without any mutation. Repair the "
                "data manually before migrating." % key
            )
        seen.add(key)


def _backfill_output_language(apps, schema_editor):
    alias = schema_editor.connection.alias
    Summary = apps.get_model("workflow", "Summary")

    for summary in Summary.objects.using(alias).all().iterator():
        if summary.output_language:
            continue  # already set
        cjk_count, visible_count, ratio = _prose_cjk_ratio(summary)
        if cjk_count >= 10 and ratio >= 0.10:
            # CJK-heavy prose is Chinese; all Chinese output is Traditional Chinese.
            summary.output_language = _ZH_HANT
        else:
            # Non-Chinese prose: canonicalize stored language.
            canonical = _canonicalize_stored_language(summary.language)
            if canonical and canonical != _ZH_HANT:
                # Non-Chinese canonical (e.g. "en", "fi") — use it
                summary.output_language = canonical
            else:
                # Chinese-labeled but non-CJK prose, or malformed: ambiguous
                summary.output_language = "und"
        summary.save(update_fields=["output_language"])


def _backfill(apps, schema_editor):
    _backfill_output_language(apps, schema_editor)
    _backfill_variant_states(apps, schema_editor)
    _reconcile_recording_state(apps, schema_editor)


def _default_output_language_for_recording(recording, apps, alias):
    """Derive the default output language (mirrors langresolve policy).

    Priority: user-corrected source > confirmed routing > unverified
    automatic Chinese routing > transcript source language > fallback en.
    """
    RoutingDecision = apps.get_model("workflow", "RoutingDecision")
    Transcript = apps.get_model("workflow", "Transcript")

    transcript = Transcript.objects.using(alias).filter(
        recording=recording, is_active=True,
    ).first()

    # 1. User-corrected source first
    if (
        transcript
        and transcript.language_observed
        and transcript.language_observed_verified_by == "user"
    ):
        if _is_chinese_family(transcript.language_observed):
            return _ZH_HANT
        return "en"

    decision = RoutingDecision.objects.using(alias).filter(
        recording=recording, is_active=True,
    ).first()
    # 2. Confirmed routing
    if (
        decision
        and decision.routing_verified
        and decision.route_suggestion in ("cantonese", "mandarin")
    ):
        return _ZH_HANT
    # 3. Unverified automatic Chinese routing
    if (
        decision
        and decision.method == "automatic"
        and decision.route_suggestion in ("cantonese", "mandarin")
    ):
        return _ZH_HANT

    # 4. Transcript source language
    if transcript and transcript.language_observed:
        if _is_chinese_family(transcript.language_observed):
            return _ZH_HANT
        return "en"

    # 5. Fallback
    return "en"


def _matching_failure(recording, transcript, section, output_language,
                      ProcessingAttempt, alias, *, newer_than_ordinal=None):
    """Newest finished failed attempt exactly matching the scope.

    Attribution requires durable provenance: exact transcript, exact
    section, resolved output language. Ambiguous legacy failures match
    nothing (conservative → ``missing``).
    """
    for attempt in (
        ProcessingAttempt.objects.using(alias).filter(
            recording=recording,
            stage="summarization",
            outcome__in=_FAILURE_OUTCOMES,
            finished_at__isnull=False,
        ).order_by("-ordinal", "-pk")
    ):
        if newer_than_ordinal is not None and attempt.ordinal <= newer_than_ordinal:
            break
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


def _backfill_variant_states(apps, schema_editor):
    alias = schema_editor.connection.alias
    Summary = apps.get_model("workflow", "Summary")
    SummaryVariantState = apps.get_model("workflow", "SummaryVariantState")
    Transcript = apps.get_model("workflow", "Transcript")
    Recording = apps.get_model("workflow", "Recording")
    ProcessingAttempt = apps.get_model("workflow", "ProcessingAttempt")

    for recording in Recording.objects.using(alias).all().iterator():
        active_transcript = Transcript.objects.using(alias).filter(
            recording=recording, is_active=True,
        ).first()
        if not active_transcript:
            continue
        section = active_transcript.sections.using(alias).filter(ordinal=0).first()
        if not section:
            continue

        # Active summaries of the active transcript's ordinal-0 section
        # (invariant guarantees at most one per output_language).
        seen_languages = set()
        for summary in Summary.objects.using(alias).filter(
            transcript=active_transcript, section=section, is_active=True,
        ):
            ol = summary.output_language or "und"
            matching = _matching_failure(
                recording, active_transcript, section, ol, ProcessingAttempt, alias,
                newer_than_ordinal=(summary.attempt.ordinal if summary.attempt_id else None),
            )
            SummaryVariantState.objects.using(alias).update_or_create(
                transcript=active_transcript,
                section=section,
                output_language=ol,
                defaults={
                    "status": "current",
                    "regeneration_failed": matching is not None,
                    "last_failed_attempt": matching,
                    "failed_at": matching.finished_at if matching else None,
                    "activated_at": summary.activated_at,
                },
            )
            seen_languages.add(ol)

        default_lang = _default_output_language_for_recording(recording, apps, alias)
        if default_lang not in seen_languages:
            # Proven matching default failure → failed; anything else
            # (including ambiguous legacy failures) → missing.
            matching_failure = _matching_failure(
                recording, active_transcript, section, default_lang, ProcessingAttempt, alias,
            )
            vs, _ = SummaryVariantState.objects.using(alias).update_or_create(
                transcript=active_transcript,
                section=section,
                output_language=default_lang,
                defaults={
                    "status": "failed" if matching_failure else "missing",
                    "regeneration_failed": False,
                    "last_failed_attempt": matching_failure,
                    "failed_at": matching_failure.finished_at if matching_failure else None,
                },
            )

        # Historical transcripts: variant states for their active summaries.
        for transcript in Transcript.objects.using(alias).filter(
            recording=recording,
        ).exclude(pk=active_transcript.pk):
            for summary in Summary.objects.using(alias).filter(
                transcript=transcript, is_active=True,
            ):
                sec = summary.section
                ol = summary.output_language or "und"
                matching = _matching_failure(
                    recording, transcript, sec, ol, ProcessingAttempt, alias,
                    newer_than_ordinal=(summary.attempt.ordinal if summary.attempt_id else None),
                )
                SummaryVariantState.objects.using(alias).update_or_create(
                    transcript=transcript,
                    section=sec,
                    output_language=ol,
                    defaults={
                        "status": "current",
                        "regeneration_failed": matching is not None,
                        "last_failed_attempt": matching,
                        "failed_at": matching.finished_at if matching else None,
                        "activated_at": summary.activated_at,
                    },
                )


def _reconcile_recording_state(apps, schema_editor):
    alias = schema_editor.connection.alias
    """Reconcile Recording.summary_status, resummarization_failed, last_failed_attempt.

    Always writes the complete tuple for every recording, even if
    summary_status already appears correct — this corrects stale
    ``resummarization_failed`` / ``last_failed_attempt`` values.
    Regeneration-failure recency follows attempt ordinal (mirrors the
    runtime reconciler), not wall-clock.
    """
    Recording = apps.get_model("workflow", "Recording")
    SummaryVariantState = apps.get_model("workflow", "SummaryVariantState")
    Transcript = apps.get_model("workflow", "Transcript")

    for recording in Recording.objects.using(alias).all().iterator():
        active_transcript = Transcript.objects.using(alias).filter(
            recording=recording, is_active=True,
        ).first()
        if not active_transcript:
            expected = {
                "summary_status": "not_ready",
                "resummarization_failed": False,
                "last_failed_attempt": None,
            }
            _apply_recording_state(recording, expected)
            continue

        section = active_transcript.sections.using(alias).filter(ordinal=0).first()
        if not section:
            continue

        default_lang = _default_output_language_for_recording(recording, apps, alias)
        vs = SummaryVariantState.objects.using(alias).filter(
            transcript=active_transcript,
            section=section,
            output_language=default_lang,
        ).first()

        if vs is None or vs.status == "missing":
            expected = {
                "summary_status": "missing",
                "resummarization_failed": False,
                "last_failed_attempt": None,
            }
        elif vs.status == "current":
            expected = {
                "summary_status": "current",
                "resummarization_failed": bool(vs.regeneration_failed),
                "last_failed_attempt": vs.last_failed_attempt if vs.regeneration_failed else None,
            }
        else:  # failed
            expected = {
                "summary_status": "failed",
                "resummarization_failed": False,
                "last_failed_attempt": vs.last_failed_attempt,
            }

        _apply_recording_state(recording, expected)


def _apply_recording_state(recording, expected):
    """Write the complete state tuple (only changed fields hit the DB)."""
    changed_fields = [
        field for field, value in expected.items()
        if getattr(recording, field) != value
    ]
    if changed_fields:
        for field, value in expected.items():
            setattr(recording, field, value)
        recording.save(using=recording._state.db or "default", update_fields=changed_fields)


class Migration(migrations.Migration):

    dependencies = [
        ("workflow", "0006_attempt_context_json"),
    ]

    operations = [
        # Step 0: Validate the pre-migration invariant BEFORE any schema
        # change (see the comment below the docstring of
        # _validate_invariant). Runs on the untouched 0006 schema.
        migrations.RunPython(_validate_invariant),
        # Step 1: Add fields without constraints
        migrations.AddField(
            model_name="summary",
            name="output_language",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="transcript",
            name="language_observed_verified_by",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="transcript",
            name="language_observed_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Step 2: Create SummaryVariantState model (without constraints first)
        migrations.CreateModel(
            name="SummaryVariantState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("output_language", models.CharField(max_length=32)),
                ("status", models.CharField(choices=[("missing", "Missing"), ("current", "Current"), ("failed", "Failed")], default="missing", max_length=16)),
                ("regeneration_failed", models.BooleanField(default=False)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("failed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("transcript", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="variant_states", to="workflow.transcript")),
                ("section", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="variant_states", to="workflow.section")),
                ("last_failed_attempt", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="failed_variant_states", to="workflow.processingattempt")),
            ],
            options={
                "ordering": ["transcript", "section", "output_language"],
            },
        ),
        # Step 3: Constraint swap + indexes (safe on validated data)
        migrations.RemoveConstraint(
            model_name="summary",
            name="uniq_active_summary_in_scope",
        ),
        migrations.AddConstraint(
            model_name="summary",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True)),
                fields=("transcript", "section", "output_language"),
                name="uniq_active_summary_in_output_language",
            ),
        ),
        migrations.AddConstraint(
            model_name="summaryvariantstate",
            constraint=models.UniqueConstraint(
                fields=("transcript", "section", "output_language"),
                name="uniq_variant_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="summaryvariantstate",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(regeneration_failed=True, status="current")
                    | models.Q(regeneration_failed=False)
                ),
                name="chk_variant_state_regeneration_failed",
            ),
        ),
        migrations.AddIndex(
            model_name="summary",
            index=models.Index(fields=["output_language"], name="summary_output_language_idx"),
        ),
        migrations.AddIndex(
            model_name="summaryvariantstate",
            index=models.Index(fields=["transcript", "output_language"], name="variant_transcript_lang_idx"),
        ),
        # Step 4: Data backfill LAST — reverse unapplies in reverse
        # order, so this irreversible RunPython is the FIRST operation
        # hit on reverse: Django raises IrreversibleError before any
        # schema or data mutation. No reverse_code.
        migrations.RunPython(_backfill),
    ]
