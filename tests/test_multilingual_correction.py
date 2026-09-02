"""Language correction acceptance tests.

Tests set_transcript_language with exact failure evidence, cross-family
transitions, and proper state tuples.
"""
from __future__ import annotations

import pytest
from django.utils import timezone as tz
from workflow.models import (
    Summary, SummaryVariantState, Recording, Transcript, Section,
    ProcessingAttempt, AttemptStage, AttemptOutcome, SummaryState,
    RoutingDecision, RoutingMethod,
)
from workflow.services.summarize import (
    set_transcript_language, resolve_default_language, is_chinese_family,
    normalize_source_language, canonical_source_for_output,
)
from factories import make_transcribed_recording, make_summary_version


@pytest.mark.django_db
class TestSetTranscriptLanguage:
    """Atomic language correction with exact state transitions."""

    def test_en_to_yue_makes_zh_hant_default(self):
        """en → yue: default changes from en to zh-Hant."""
        rec, trans, sec = make_transcribed_recording(["hello"])
        make_summary_version(rec, trans, sec, output_language="en")
        result = set_transcript_language(rec, "yue")
        assert result["old_default_output"] == "en"
        assert result["new_default_output"] == "zh-Hant"
        assert result["new_source_language"] == "yue"
        rec.refresh_from_db()
        # New default is zh-Hant, but no zh-Hant summary exists → missing
        assert rec.summary_status == SummaryState.MISSING

    def test_zh_hk_to_fi_makes_en_default(self):
        """zh-HK → fi: default changes from zh-Hant to en."""
        rec, trans, sec = make_transcribed_recording(["hello"])
        # Set up as Chinese recording
        RoutingDecision.objects.create(
            recording=rec, ordinal=1, route_suggestion="cantonese",
            profile_name="cantonese", model_id="apple:zh-HK",
            method="manual", routing_verified=True, is_active=True,
        )
        trans.language_observed = "zh-HK"
        trans.save(update_fields=["language_observed"])
        make_summary_version(rec, trans, sec, output_language="zh-Hant")

        result = set_transcript_language(rec, "fi")
        assert result["old_default_output"] == "zh-Hant"
        assert result["new_default_output"] == "en"
        assert result["new_source_language"] == "fi"

    def test_matching_failure_sets_failed_state(self):
        """When a matching failure exists for the new default, state is failed."""
        rec, trans, sec = make_transcribed_recording(["hello"])
        # Create a failed attempt matching the new default scope
        attempt = ProcessingAttempt.objects.create(
            recording=rec, stage=AttemptStage.SUMMARIZATION, ordinal=1,
            outcome=AttemptOutcome.INVALID_OUTPUT, error_code="test",
            finished_at=tz.now(),
            context_json={"language": {"resolved": "en", "transcript_id": trans.pk, "section_id": sec.pk}},
        )
        result = set_transcript_language(rec, "fi")
        rec.refresh_from_db()
        assert rec.summary_status == SummaryState.FAILED
        assert rec.last_failed_attempt_id == attempt.pk

    def test_optional_failure_does_not_affect_new_default(self):
        """A failure belonging to an optional language doesn't mark new default as failed."""
        rec, trans, sec = make_transcribed_recording(["hello"])
        # Failed attempt for zh-Hant (optional)
        ProcessingAttempt.objects.create(
            recording=rec, stage=AttemptStage.SUMMARIZATION, ordinal=1,
            outcome=AttemptOutcome.INVALID_OUTPUT, error_code="test",
            context_json={"language": {"resolved": "zh-Hant", "transcript_id": trans.pk, "section_id": sec.pk}},
        )
        result = set_transcript_language(rec, "fi")
        rec.refresh_from_db()
        assert rec.summary_status == SummaryState.MISSING  # no matching failure for en

    def test_old_default_failure_not_reused(self):
        """A failure belonging only to the old default is not reused for new default."""
        from workflow.models import ProcessingAttempt, AttemptStage, AttemptOutcome
        rec, trans, sec = make_transcribed_recording(["hello"])
        make_summary_version(rec, trans, sec, output_language="en")
        # Failed attempt for en (old default)
        next_ord = ProcessingAttempt.objects.filter(
            recording=rec, stage=AttemptStage.SUMMARIZATION,
        ).count() + 1
        ProcessingAttempt.objects.create(
            recording=rec, stage=AttemptStage.SUMMARIZATION, ordinal=next_ord,
            outcome=AttemptOutcome.INVALID_OUTPUT, error_code="test",
            context_json={"language": {"resolved": "en", "transcript_id": trans.pk, "section_id": sec.pk}},
        )
        # Change to Chinese — new default is zh-Hant, old failure is for en
        RoutingDecision.objects.create(
            recording=rec, ordinal=1, route_suggestion="cantonese",
            profile_name="cantonese", model_id="apple:zh-HK",
            method="manual", routing_verified=True, is_active=True,
        )
        result = set_transcript_language(rec, "zh-HK")
        rec.refresh_from_db()
        # New default is zh-Hant, but no zh-Hant summary exists → missing
        # The old en failure is NOT reused for zh-Hant
        assert rec.summary_status == SummaryState.MISSING

    def test_no_matching_failure_results_in_missing(self):
        """When no matching failure exists, state is missing."""
        rec, trans, sec = make_transcribed_recording(["hello"])
        result = set_transcript_language(rec, "fi")
        rec.refresh_from_db()
        assert rec.summary_status == SummaryState.MISSING
        assert rec.resummarization_failed is False
        assert rec.last_failed_attempt_id is None

    def test_active_summary_current_after_correction(self):
        from factories import make_summary_version

        rec, trans, sec = make_transcribed_recording(["hello"])
        set_transcript_language(rec, "yue")
        make_summary_version(rec, trans, sec, output_language="zh-Hant")
        # A second correction re-derives the same default from DB truth.
        result = set_transcript_language(rec, "zh-HK")
        rec.refresh_from_db()
        assert result["new_default_output"] == "zh-Hant"
        assert rec.summary_status == SummaryState.CURRENT
        assert rec.resummarization_failed is False
        assert rec.last_failed_attempt_id is None

    def test_newer_matching_failed_regeneration_sets_regeneration_failed(self):
        """Active Summary plus a NEWER matching failed regeneration →
        current + regeneration_failed (+ recording resummarization_failed)."""
        from factories import make_summary_version

        from workflow.models import ProcessingAttempt, AttemptStage, AttemptOutcome

        rec, trans, sec = make_transcribed_recording(["hello"])
        summary = make_summary_version(rec, trans, sec, output_language="en")
        result = set_transcript_language(rec, "yue")  # default becomes zh-Hant
        # Generate zh-Hant summary, then a matching failed regeneration.
        make_summary_version(rec, trans, sec, output_language="zh-Hant")
        # Real generation writes provenance on the attempt; craft a
        # matching failed regeneration attempt NEWER than the summary's.
        last = ProcessingAttempt.objects.filter(
            recording=rec, stage=AttemptStage.SUMMARIZATION,
        ).order_by("-ordinal").first()
        failure = ProcessingAttempt.objects.create(
            recording=rec, stage=AttemptStage.SUMMARIZATION,
            ordinal=last.ordinal + 1,
            outcome=AttemptOutcome.UNREACHABLE, error_code="endpoint_unavailable",
            finished_at=tz.now(),
            context_json={
                "language": {
                    "requested": "zh-Hant", "resolved": "zh-Hant",
                    "is_default": True,
                    "transcript_id": trans.pk, "section_id": sec.pk,
                },
            },
        )
        result = set_transcript_language(rec, "yue")
        rec.refresh_from_db()
        vs = SummaryVariantState.objects.get(
            transcript=trans, section=sec, output_language="zh-Hant"
        )
        assert vs.status == SummaryVariantState.VariantStatus.CURRENT
        assert vs.regeneration_failed is True
        assert vs.last_failed_attempt_id == failure.pk
        assert rec.summary_status == SummaryState.CURRENT
        assert rec.resummarization_failed is True
        assert rec.last_failed_attempt_id == failure.pk

    def test_older_matching_failure_is_not_regeneration(self):
        """Recency is by attempt ordinal: a matching failure OLDER than
        the active Summary's attempt does not set regeneration_failed."""
        from factories import make_summary_version

        from workflow.models import ProcessingAttempt, AttemptStage, AttemptOutcome

        rec, trans, sec = make_transcribed_recording(["hello"])
        set_transcript_language(rec, "yue")
        # Matching failure FIRST (first-generation failure attempt), then
        # a successful summary with a newer attempt.
        ProcessingAttempt.objects.create(
            recording=rec, stage=AttemptStage.SUMMARIZATION,
            ordinal=ProcessingAttempt.objects.filter(
                recording=rec, stage=AttemptStage.SUMMARIZATION,
            ).count() + 1,
            outcome=AttemptOutcome.TIMEOUT, error_code="timeout",
            finished_at=tz.now(),
            context_json={
                "language": {
                    "requested": "default", "resolved": "zh-Hant",
                    "is_default": True,
                    "transcript_id": trans.pk, "section_id": sec.pk,
                },
            },
        )
        make_summary_version(rec, trans, sec, output_language="zh-Hant")
        set_transcript_language(rec, "zh-HK")
        rec.refresh_from_db()
        vs = SummaryVariantState.objects.get(
            transcript=trans, section=sec, output_language="zh-Hant"
        )
        assert vs.status == SummaryVariantState.VariantStatus.CURRENT
        assert vs.regeneration_failed is False
        assert rec.resummarization_failed is False

    def test_correction_stamps_user_provenance(self):
        rec, trans, sec = make_transcribed_recording(["hello"])
        set_transcript_language(rec, "ZH-hk")
        trans.refresh_from_db()
        # Canonical BCP-47 casing, never blind lowercasing.
        assert trans.language_observed == "zh-HK"
        assert trans.language_observed_verified_by == "user"
        assert trans.language_observed_verified_at is not None

    def test_no_self_locking_service_runs_inside_held_lock(self, tmp_path):
        """set_transcript_language must NOT acquire the pipeline lock
        itself: it succeeds while the caller already holds it."""
        from factories import make_config as _mc
        from workflow.services.pipeline_lock import pipeline_lock

        config = _mc(tmp_path)
        rec, trans, sec = make_transcribed_recording(["hello"])
        with pipeline_lock(config):
            result = set_transcript_language(rec, "fi")
        assert result["new_default_output"] == "en"

    def test_lock_contention_exit_3_without_mutation(
        self, tmp_path, monkeypatch, capsys
    ):
        """Genuine lock contention: the CLI exits 3 and nothing changes."""
        from factories import write_cli_config
        from workflow.services.pipeline_lock import pipeline_lock

        config = write_cli_config(tmp_path, monkeypatch)
        rec, trans, sec = make_transcribed_recording(["hello"])
        from brainlib import cli as brain_cli

        with pipeline_lock(config):
            exit_code = brain_cli.main(
                ["transcript-language", str(rec.pk), "--set", "fi", "--json"]
            )
        assert exit_code == 3
        assert "another pipeline process" in capsys.readouterr().err
        trans.refresh_from_db()
        assert trans.language_observed == ""  # unchanged
        assert trans.language_observed_verified_by == ""


@pytest.mark.django_db
class TestLanguageCanonicalization:
    """Canonical language code handling."""

    def test_yue_is_chinese_family(self):
        assert is_chinese_family("yue")
        assert is_chinese_family("yue-HK")
        assert is_chinese_family("YUE")

    def test_cmn_is_chinese_family(self):
        assert is_chinese_family("cmn")
        assert is_chinese_family("cmn-CN")

    def test_zh_variants_are_chinese_family(self):
        assert is_chinese_family("zh")
        assert is_chinese_family("zh-HK")
        assert is_chinese_family("zh-CN")

    def test_non_chinese_not_chinese_family(self):
        assert not is_chinese_family("en")
        assert not is_chinese_family("fi")
        assert not is_chinese_family("sv")

    def test_chinese_maps_to_zh_hant_for_output(self):
        assert canonical_source_for_output("yue") == "zh-Hant"
        assert canonical_source_for_output("cmn") == "zh-Hant"
        assert canonical_source_for_output("zh-HK") == "zh-Hant"

    def test_non_chinese_preserved_for_output(self):
        assert canonical_source_for_output("en") == "en"
        assert canonical_source_for_output("fi") == "fi"

    def test_normalize_rejects_malformed(self):
        assert normalize_source_language("") == ""
        assert normalize_source_language("xyz!") == ""
        assert normalize_source_language("toolong123456789") == ""

    def test_normalize_canonicalizes_casing(self):
        assert normalize_source_language("EN") == "en"
        assert normalize_source_language("Fi") == "fi"
