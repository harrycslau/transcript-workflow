"""Tests for tag suggestions, assignments, provenance and history preservation."""

from __future__ import annotations

import pytest

from brainlib.config import LLMConfig, TagSpec, TagsConfig
from workflow.models import (
    Summary,
    Tag,
    TagAssignment,
    TagDeactivatedBy,
    TagOrigin,
    SummaryState,
)
from workflow.services import summarize as summarize_service
from workflow.services.tags import (
    TagOperationError,
    add_manual_tag,
    apply_tag_selection,
    create_custom_tag_and_assign,
    sync_tags,
)

from factories import (
    final_summary_json,
    make_config,
    make_summary_version,
    make_tag,
    make_tag_assignment,
    make_transcribed_recording,
)

pytestmark = pytest.mark.django_db


def tags_config(*names) -> TagsConfig:
    return TagsConfig(allowed=tuple(TagSpec(name=n, description=f"{n} description") for n in names))


def llm_config(tmp_path):
    return LLMConfig(
        provider="openai_compatible", base_url="http://127.0.0.1:1/v1", model="test-model",
        api_key_env="BRAIN_TEST_LLM_API_KEY", temperature=0.2, timeout_seconds=600,
    )


def make_llm(*responses):
    calls = []
    queue = list(responses)

    def llm_call(*, system, user):
        calls.append((system, user))
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    llm_call.calls = calls
    return llm_call


class TestSuggestionsAndProvenance:
    def _config(self, tmp_path):
        return make_config(tmp_path, llm=llm_config(tmp_path), tags=tags_config("Family", "Academic", "Unknown"))

    def test_multiple_suggested_tags_with_provenance(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = make_llm(final_summary_json(suggested_tags=["Academic", "Family"]))
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["result"] == "summarized"
        assert sorted(result["tags"]) == ["Academic", "Family"]
        summary = recording.current_summary()
        suggestions = list(summary.tag_suggestions.order_by("tag__name"))
        assert [s.tag.name for s in suggestions] == ["Academic", "Family"]
        assert all(s.suggested_by_model for s in suggestions)
        # Effective assignments point back at the summary version.
        for assignment in TagAssignment.objects.filter(recording=recording, is_active=True):
            assert assignment.origin == TagOrigin.SUGGESTED
            assert assignment.source_summary_id == summary.pk

    def test_case_insensitive_matching_persists_display_name(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = make_llm(final_summary_json(suggested_tags=["aCaDeMic"]))
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["tags"] == ["Academic"]
        tag = Tag.objects.get(name_key="academic")
        assert TagAssignment.objects.get(recording=recording, tag=tag).is_active

    def test_unconfigured_suggestion_not_persisted_but_recorded(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = make_llm(final_summary_json(suggested_tags=["Academic", "Nonexistent"]))
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["tags"] == ["Academic"]
        raw = recording.current_summary().suggested_tags_raw
        assert raw["rejected"] == ["Nonexistent"]
        assert not Tag.objects.filter(name="Nonexistent").exists()

    def test_unknown_conflict_end_to_end(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = make_llm(final_summary_json(suggested_tags=["Academic", "Unknown"]))
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["tags"] == ["Academic"]
        assert not TagAssignment.objects.filter(recording=recording, tag__name_key="unknown").exists()

    def test_unknown_alone_persisted(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = make_llm(final_summary_json(suggested_tags=["Unknown"]))
        result = summarize_service.summarize_one(config, recording, llm_call=llm)
        assert result["tags"] == ["Unknown"]
        assert TagAssignment.objects.filter(recording=recording, tag__name_key="unknown", is_active=True).exists()

    def test_no_duplicate_effective_assignments(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        llm = make_llm(final_summary_json(suggested_tags=["Academic", "ACADEMIC", "academic"]))
        summarize_service.summarize_one(config, recording, llm_call=llm)
        tag = Tag.objects.get(name_key="academic")
        assert TagAssignment.objects.filter(recording=recording, tag=tag).count() == 1


class TestRegenerationAndHistory:
    def _config(self, tmp_path):
        return make_config(tmp_path, llm=llm_config(tmp_path), tags=tags_config("Family", "Academic", "Unknown"))

    def test_manual_assignment_not_erased_by_regeneration(self, tmp_path):
        config = self._config(tmp_path)
        sync_tags(config)
        recording, _, _ = make_transcribed_recording(["hello world"])
        family = Tag.objects.get(name_key="family")
        # The user manually assigned Family before any summarization.
        manual = TagAssignment.objects.create(
            recording=recording, tag=family, origin=TagOrigin.MANUAL, is_active=True
        )
        summarize_service.summarize_one(
            config, recording, llm_call=make_llm(final_summary_json(suggested_tags=["Academic"]))
        )
        manual.refresh_from_db()
        assert manual.is_active is True
        assert manual.origin == TagOrigin.MANUAL
        assert manual.source_summary is None
        # Regeneration suggesting Family still leaves the manual row intact.
        summarize_service.summarize_one(
            config, recording, regenerate=True,
            llm_call=make_llm(final_summary_json(title="V2", suggested_tags=["Academic", "Family"])),
        )
        manual.refresh_from_db()
        assert manual.is_active is True
        assert manual.origin == TagOrigin.MANUAL
        assignments = TagAssignment.objects.filter(recording=recording, is_active=True)
        assert sorted(a.tag.name for a in assignments) == ["Academic", "Family"]

    def test_suggested_assignments_replaced_on_regeneration(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(
            config, recording, llm_call=make_llm(final_summary_json(suggested_tags=["Academic"]))
        )
        summarize_service.summarize_one(
            config, recording, regenerate=True,
            llm_call=make_llm(final_summary_json(title="V2", suggested_tags=["Family"])),
        )
        active = TagAssignment.objects.filter(recording=recording, is_active=True)
        assert [a.tag.name for a in active] == ["Family"]
        academic = Tag.objects.get(name_key="academic")
        old = TagAssignment.objects.get(recording=recording, tag=academic)
        assert old.is_active is False
        assert old.deactivated_at is not None
        # Provenance history: both summary versions recorded their suggestions.
        assert Summary.objects.count() == 2
        versions = {s.ordinal: sorted(s.tag_suggestions.values_list("tag__name", flat=True)) for s in Summary.objects.all()}
        assert versions[1] == ["Academic"]
        assert versions[2] == ["Family"]

    def test_retired_tag_keeps_historical_assignment(self, tmp_path):
        config = self._config(tmp_path)
        recording, _, _ = make_transcribed_recording(["hello world"])
        summarize_service.summarize_one(
            config, recording, llm_call=make_llm(final_summary_json(suggested_tags=["Academic"]))
        )
        # Remove Academic from configuration; sync retires it.
        retired_config = make_config(
            tmp_path, llm=llm_config(tmp_path), tags=tags_config("Family", "Unknown")
        )
        counts = sync_tags(retired_config)
        assert counts["retired"] == 1
        academic = Tag.objects.get(name_key="academic")
        assert academic.is_configured is False
        assignment = TagAssignment.objects.get(recording=recording, tag=academic)
        assert assignment.is_active is True  # history preserved
        # Retired tags are not offered to the model afterwards.
        # The prompt's allowed list excludes retired tags.
        from workflow.services.summarize import _final_system_prompt

        system_prompt = _final_system_prompt(list(Tag.objects.filter(is_configured=True)))
        assert "Academic" not in system_prompt
        assert "Family" in system_prompt

    def test_suggestion_for_manually_tagged_recording_is_provenance_only(self, tmp_path):
        config = self._config(tmp_path)
        sync_tags(config)
        recording, _, _ = make_transcribed_recording(["hello world"])
        family = Tag.objects.get(name_key="family")
        TagAssignment.objects.create(recording=recording, tag=family, origin=TagOrigin.MANUAL, is_active=True)
        summarize_service.summarize_one(
            config, recording, llm_call=make_llm(final_summary_json(suggested_tags=["Family", "Academic"]))
        )
        family_assignment = TagAssignment.objects.get(recording=recording, tag=family)
        assert family_assignment.origin == TagOrigin.MANUAL
        assert family_assignment.source_summary is None


class TestDefinitionOriginAndCustomTags:
    """Tag definition provenance (config/custom), YAML promotion, and the
    bounded create_custom_tag_and_assign service.

    Covers (production integration):
    - sync creates config-owned tags and retires only absent CONFIG tags,
      never custom tags;
    - a custom tag whose normalized name later appears in YAML is
      PROMOTED to config-owned on the SAME row (pk/assignments/history
      preserved) with the configured name/description applied;
    - create_custom_tag_and_assign validates (exact str, nonblank,
      control/newline rejection, name and normalized-key length bounds,
      collisions including retired rows), creates a global reusable
      custom Tag + manual assignment in one transaction, schedules
      exactly one search sync inside the transaction, and never leaks
      IntegrityError on duplicates.
    """

    def test_custom_tag_created_with_provenance_and_manual_assignment(self, tmp_path):
        recording, _t, _s = make_transcribed_recording(["x"], sha="cust-1")
        result = create_custom_tag_and_assign(recording, "  Work  ")
        tag = Tag.objects.get(name_key="work")
        assert result["tag"].pk == tag.pk
        assert result["created_tag"] is True
        assert tag.name == "Work"  # stripped
        assert tag.definition_origin == Tag.DefinitionOrigin.CUSTOM
        assert tag.is_configured is True
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.MANUAL
        assert assignment.source_summary is None

    def test_custom_tag_is_global_reusable_across_recordings(self, tmp_path):
        rec_a, _t, _s = make_transcribed_recording(["a"], sha="glob-a")
        rec_b, _t2, _s2 = make_transcribed_recording(["b"], sha="glob-b")
        first = create_custom_tag_and_assign(rec_a, "SharedTag")
        # A second CREATE with the same normalized name is a collision
        # (the definition already exists globally) — friendly error.
        with pytest.raises(TagOperationError) as excinfo:
            create_custom_tag_and_assign(rec_b, "sharedtag")
        assert excinfo.value.code == "duplicate_tag"
        # The existing GLOBAL tag is reusable on another recording via
        # the ordinary add-existing semantics — one row, two assignments.
        add_manual_tag(rec_b, first["tag"])
        assert Tag.objects.filter(name_key="sharedtag").count() == 1
        assert TagAssignment.objects.filter(recording=rec_a, tag=first["tag"]).exists()
        assert TagAssignment.objects.filter(recording=rec_b, tag=first["tag"]).exists()

    def test_unicode_casefold_normalization_and_collision(self, tmp_path):
        recording, _t, _s = make_transcribed_recording(["x"], sha="uni-1")
        result = create_custom_tag_and_assign(recording, "Älykäs")
        assert result["tag"].name_key == "älykäs"
        with pytest.raises(TagOperationError) as excinfo:
            create_custom_tag_and_assign(recording, "ÄLYKÄS")
        assert excinfo.value.code == "duplicate_tag"
        assert "already exists" in excinfo.value.message
        assert Tag.objects.filter(name_key="älykäs").count() == 1

    @pytest.mark.parametrize(
        "raw,code",
        [
            ("", "invalid_tag_name"),
            ("   ", "invalid_tag_name"),
            (12345, "invalid_tag_name"),
            ("line\nbreak", "invalid_tag_name"),
            ("tab\there", "invalid_tag_name"),
            ("control\x00char", "invalid_tag_name"),
            ("x" * 65, "invalid_tag_name"),
            ("ß" * 33, "invalid_tag_name"),  # casefold expands: 33 -> 66 > 64
        ],
    )
    def test_invalid_and_overlength_names_rejected_with_stable_errors(self, tmp_path, raw, code):
        recording, _t, _s = make_transcribed_recording(["x"], sha="inv-1")
        with pytest.raises(TagOperationError) as excinfo:
            create_custom_tag_and_assign(recording, raw)
        assert excinfo.value.code == code
        assert Tag.objects.count() == 0
        assert TagAssignment.objects.count() == 0

    def test_collision_with_retired_row_rejected(self, tmp_path):
        recording, _t, _s = make_transcribed_recording(["x"], sha="ret-1")
        Tag.objects.create(
            name="OldTopic", name_key="oldtopic", is_configured=False,
            definition_origin=Tag.DefinitionOrigin.CONFIG,
        )
        with pytest.raises(TagOperationError) as excinfo:
            create_custom_tag_and_assign(recording, "OldTopic")
        assert excinfo.value.code == "duplicate_tag"
        assert Tag.objects.filter(name_key="oldtopic").count() == 1
        assert TagAssignment.objects.count() == 0

    def test_str_subclass_rejected_as_not_exact_str(self, tmp_path):
        class _StrSubclass(str):
            pass

        recording, _t, _s = make_transcribed_recording(["x"], sha="sub-1")
        with pytest.raises(TagOperationError) as excinfo:
            create_custom_tag_and_assign(recording, _StrSubclass("Work"))
        assert excinfo.value.code == "invalid_tag_name"
        assert Tag.objects.count() == 0
        assert TagAssignment.objects.count() == 0

    def test_concurrent_race_after_prevalidation_raises_duplicate_not_leak(
        self, tmp_path, monkeypatch
    ):
        """The insert-time IntegrityError (another writer created the
        same normalized key between the pre-validation and the insert)
        is converted to the SAME stable duplicate_tag error — one winner
        row, no assignment, no leaked IntegrityError."""
        from workflow.models import Tag as TagModel

        recording, _t, _s = make_transcribed_recording(["x"], sha="race-1")
        winner = TagModel.objects.create(
            name="Winner", name_key="race", is_configured=True,
            definition_origin=TagModel.DefinitionOrigin.CUSTOM,
        )
        # Simulate the race window: the pre-validation collision check is
        # blind to the existing row (as it would be under true
        # concurrency), so the service reaches the insert.
        real_filter = TagModel.objects.filter
        monkeypatch.setattr(
            TagModel.objects, "filter",
            lambda *args, **kwargs: real_filter(*args, **kwargs).none(),
        )
        with pytest.raises(TagOperationError) as excinfo:
            create_custom_tag_and_assign(recording, "RACE")
        assert excinfo.value.code == "duplicate_tag"
        assert "already exists" in excinfo.value.message
        # Exactly one definition survives; the lost request assigned nothing.
        # (The manager filter is still blinded, so assert via the real one.)
        assert real_filter(name_key="race").count() == 1
        assert winner.definition_origin == TagModel.DefinitionOrigin.CUSTOM
        assert TagAssignment.objects.count() == 0

    def test_duplicate_create_rejected_no_duplicate_no_leak(self, tmp_path):
        recording, _t, _s = make_transcribed_recording(["x"], sha="dup-1")
        first = create_custom_tag_and_assign(recording, "Work")
        # Sequential duplicate submission: rejected with a stable friendly
        # error — never a duplicate row, never a leaked IntegrityError.
        with pytest.raises(TagOperationError) as excinfo:
            create_custom_tag_and_assign(recording, "work")
        assert excinfo.value.code == "duplicate_tag"
        assert "already exists" in excinfo.value.message
        assert Tag.objects.filter(name_key="work").count() == 1
        assert TagAssignment.objects.filter(recording=recording).count() == 1
        first["assignment"].refresh_from_db()
        assert first["assignment"].is_active is True
        assert first["assignment"].origin == TagOrigin.MANUAL

    def test_custom_tag_survives_sync_when_absent_from_config(self, tmp_path):
        config = make_config(tmp_path, tags=tags_config("Family"))
        recording, _t, _s = make_transcribed_recording(["x"], sha="surv-1")
        result = create_custom_tag_and_assign(recording, "Personal")
        counts = sync_tags(config)
        assert counts["retired"] == 0
        result["tag"].refresh_from_db()
        assert result["tag"].is_configured is True  # never retired
        assert result["tag"].definition_origin == Tag.DefinitionOrigin.CUSTOM

    def test_yaml_promotion_preserves_pk_assignments_and_history(self, tmp_path):
        recording, transcript, section = make_transcribed_recording(["x"], sha="prom-1")
        result = create_custom_tag_and_assign(recording, "Side Project")
        original_pk = result["tag"].pk
        assignment = result["assignment"]
        make_summary_version(recording, transcript, section)
        from workflow.models import SummaryTagSuggestion

        SummaryTagSuggestion.objects.create(
            summary=recording.current_summary(), tag=result["tag"]
        )
        # YAML now declares the same normalized name with a description.
        config = make_config(
            tmp_path,
            tags=TagsConfig(
                allowed=(TagSpec(name="Side Project", description="Adopted by config"),)
            ),
        )
        counts = sync_tags(config)
        assert counts["promoted"] == 1
        tag = Tag.objects.get(pk=original_pk)  # SAME row
        assert tag.definition_origin == Tag.DefinitionOrigin.CONFIG
        assert tag.is_configured is True
        assert tag.description == "Adopted by config"
        assert tag.name == "Side Project"
        assignment.refresh_from_db()
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.MANUAL
        assert SummaryTagSuggestion.objects.filter(
            summary=recording.current_summary(), tag=tag
        ).count() == 1
        assert Tag.objects.count() == 1  # no replacement row

    def test_promotion_applies_configured_respelling(self, tmp_path):
        recording, _t, _s = make_transcribed_recording(["x"], sha="prom-2")
        result = create_custom_tag_and_assign(recording, "work")
        config = make_config(
            tmp_path,
            tags=TagsConfig(allowed=(TagSpec(name="Work", description="d"),)),
        )
        sync_tags(config)
        tag = Tag.objects.get(pk=result["tag"].pk)
        assert tag.name == "Work"  # configured display name applied
        assert tag.definition_origin == Tag.DefinitionOrigin.CONFIG

    def test_sync_never_retires_custom_tags_when_config_changes(self, tmp_path):
        recording, _t, _s = make_transcribed_recording(["x"], sha="never-1")
        create_custom_tag_and_assign(recording, "KeepMe")
        config = make_config(tmp_path, tags=tags_config("Family"))
        sync_tags(config)  # config lacks KeepMe entirely
        tag = Tag.objects.get(name_key="keepme")
        assert tag.is_configured is True
        assert tag.definition_origin == Tag.DefinitionOrigin.CUSTOM

    def test_create_schedules_exactly_one_search_sync(
        self, django_capture_on_commit_callbacks
    ):
        from workflow.models import SearchDocument
        from workflow.services import search_index as si

        recording, _t, _s = make_transcribed_recording(["x"], sha="sync-1")
        si.rebuild_index()
        meta = SearchDocument.objects.get(document_key=f"recording:{recording.pk}")
        assert meta.aux_text == ""
        with django_capture_on_commit_callbacks(execute=True) as captured:
            create_custom_tag_and_assign(recording, "Work")
        assert len(captured) == 1  # exactly one recording sync per commit
        assert SearchDocument.objects.get(
            document_key=f"recording:{recording.pk}"
        ).aux_text == "Work"
        assert si.build_status_report()["healthy"] is True

    def test_rejected_create_rolls_back_everything_no_sync(
        self, django_capture_on_commit_callbacks
    ):
        recording, _t, _s = make_transcribed_recording(["x"], sha="rb-1")
        Tag.objects.create(
            name="Exists", name_key="exists", is_configured=True,
            definition_origin=Tag.DefinitionOrigin.CONFIG,
        )
        with django_capture_on_commit_callbacks(execute=True) as captured:
            with pytest.raises(TagOperationError):
                create_custom_tag_and_assign(recording, "Exists")
        assert captured == []  # rolled-back transaction schedules nothing
        assert Tag.objects.count() == 1
        assert TagAssignment.objects.count() == 0


class TestApplyTagSelection:
    """Bulk atomic tag selection (the + Add tag modal Done path).

    Covers: unchanged Done preserves suggested/confirmed/manual with zero
    DML and zero callback; add+remove in ONE commit; reactivation clears
    suppression; active-unselected applies the exact user-removal
    suppression; retired explicit-field validation; invalid/missing/
    duplicate/bool/bounded IDs; optional custom creation atomic with the
    selection; collision rolls EVERYTHING back; exactly one recording
    search sync when membership changed; the retry wrapper structure.
    """

    def _apply(self, recording, available=(), retired=(), *, new_tag_name=""):
        return apply_tag_selection(
            recording, list(available), list(retired), new_tag_name=new_tag_name
        )

    def test_unchanged_done_preserves_suggested_zero_dml_zero_callback(
        self, django_capture_on_commit_callbacks
    ):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        recording, transcript, section = make_transcribed_recording(["x"], sha="bulk-1")
        summary = make_summary_version(recording, transcript, section)
        suggested = make_tag("Sug")
        make_tag_assignment(recording, suggested, origin="suggested", source_summary=summary)
        manual = make_tag("Man")
        make_tag_assignment(recording, manual, origin="manual")
        confirmed = make_tag("Conf")
        make_tag_assignment(recording, confirmed, origin="confirmed", source_summary=summary)

        with django_capture_on_commit_callbacks(execute=True) as captured:
            with CaptureQueriesContext(connection) as ctx:
                result = self._apply(
                    recording, available=[suggested.pk, manual.pk, confirmed.pk]
                )
        assert result["changed"] is False
        assert result["created_tag"] is False
        assert result["counts"] == {"created": 0, "reactivated": 0, "removed": 0, "unchanged": 3}
        assert captured == []  # unchanged Done: zero callback
        # Zero DML: only SELECTs (plus the atomic SAVEPOINT bookkeeping).
        dml = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        assert dml == []
        # Row-level preservation: suggested stays suggested with its
        # provenance, confirmed stays confirmed, manual stays manual.
        sug = TagAssignment.objects.get(recording=recording, tag=suggested)
        assert sug.origin == TagOrigin.SUGGESTED
        assert sug.is_active is True
        assert sug.source_summary_id == summary.pk
        conf = TagAssignment.objects.get(recording=recording, tag=confirmed)
        assert conf.origin == TagOrigin.CONFIRMED
        assert conf.source_summary_id == summary.pk
        assert TagAssignment.objects.get(recording=recording, tag=manual).origin == TagOrigin.MANUAL

    def test_add_and_remove_in_one_commit(self, django_capture_on_commit_callbacks):
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-2")
        drop = make_tag("Drop")
        make_tag_assignment(recording, drop, origin="manual")
        add = make_tag("Add")
        with django_capture_on_commit_callbacks(execute=True) as captured:
            result = self._apply(recording, available=[add.pk])
        assert result["changed"] is True
        assert result["counts"]["created"] == 1
        assert result["counts"]["removed"] == 1
        assert len(captured) == 1  # exactly one recording sync
        dropped = TagAssignment.objects.get(recording=recording, tag=drop)
        assert dropped.is_active is False
        assert dropped.deactivated_by == TagDeactivatedBy.USER
        added = TagAssignment.objects.get(recording=recording, tag=add)
        assert added.is_active is True
        assert added.origin == TagOrigin.MANUAL

    def test_reactivation_clears_suppression_and_source_summary(self, django_capture_on_commit_callbacks):
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-3")
        tag = make_tag("Revive")
        make_tag_assignment(recording, tag, origin="suggested", active=False)  # suppressed
        with django_capture_on_commit_callbacks(execute=True) as captured:
            result = self._apply(recording, available=[tag.pk])
        assert result["counts"]["reactivated"] == 1
        assert len(captured) == 1
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.MANUAL
        assert assignment.deactivated_by == TagDeactivatedBy.NONE
        assert assignment.deactivated_at is None
        assert assignment.source_summary is None

    def test_active_unselected_applies_user_removal_suppression(self):
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-4")
        tag = make_tag("DropMe")
        make_tag_assignment(recording, tag, origin="confirmed")
        result = self._apply(recording, available=[])
        assert result["counts"]["removed"] == 1
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.is_active is False
        assert assignment.deactivated_by == TagDeactivatedBy.USER
        assert assignment.deactivated_at is not None

    def test_confirmed_selected_preserved(self):
        recording, transcript, section = make_transcribed_recording(["x"], sha="bulk-5")
        summary = make_summary_version(recording, transcript, section)
        tag = make_tag("KeepConf")
        make_tag_assignment(recording, tag, origin="confirmed", source_summary=summary)
        result = self._apply(recording, available=[tag.pk])
        assert result["changed"] is False
        assignment = TagAssignment.objects.get(recording=recording, tag=tag)
        assert assignment.origin == TagOrigin.CONFIRMED
        assert assignment.source_summary_id == summary.pk

    def test_retired_ids_are_explicit_opt_in(self, django_capture_on_commit_callbacks):
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-6")
        retired = Tag.objects.create(
            name="OldTopic", name_key="oldtopic", is_configured=False,
            definition_origin=Tag.DefinitionOrigin.CONFIG,
        )
        with django_capture_on_commit_callbacks(execute=True) as captured:
            result = self._apply(recording, retired=[retired.pk])
        assert result["counts"]["created"] == 1
        assert len(captured) == 1
        assignment = TagAssignment.objects.get(recording=recording, tag=retired)
        assert assignment.is_active is True
        assert assignment.origin == TagOrigin.MANUAL

    def test_category_fields_are_authoritative(self, django_capture_on_commit_callbacks):
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-7")
        available = make_tag("Avail")
        retired = Tag.objects.create(
            name="Old", name_key="old", is_configured=False,
            definition_origin=Tag.DefinitionOrigin.CONFIG,
        )
        # An available ID in the retired slot is rejected.
        with pytest.raises(TagOperationError) as excinfo:
            self._apply(recording, retired=[available.pk])
        assert excinfo.value.code == "invalid_tag_selection"
        # A retired ID in the available slot is rejected.
        with pytest.raises(TagOperationError) as excinfo:
            self._apply(recording, available=[retired.pk])
        assert excinfo.value.code == "invalid_tag_selection"
        with django_capture_on_commit_callbacks(execute=True) as captured:
            pass  # nothing was written by the rejected calls
        assert TagAssignment.objects.count() == 0
        assert captured == []

    @pytest.mark.parametrize(
        "bad_available",
        [
            [True],                 # bool is not an exact integer ID
            [1.5],                  # non-integer
            ["abc"],                # non-digit string
            [0],                    # non-positive
            [-1],                   # negative
            ["1", 1],               # duplicate after coercion
            [999999],               # nonexistent tag
        ],
    )
    def test_invalid_ids_rejected(self, bad_available, django_capture_on_commit_callbacks):
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-8")
        make_tag("Real")
        with pytest.raises(TagOperationError) as excinfo:
            self._apply(recording, available=bad_available)
        assert excinfo.value.code == "invalid_tag_selection"
        with django_capture_on_commit_callbacks(execute=True) as captured:
            pass
        assert TagAssignment.objects.count() == 0
        assert captured == []

    def test_submitted_id_count_is_bounded(self, django_capture_on_commit_callbacks):
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-9")
        for i in range(3):
            make_tag(f"Bound{i}")
        # limit = min(hard, count + 1) = 4; five submitted IDs must be
        # rejected BEFORE any category/existence work.
        with pytest.raises(TagOperationError) as excinfo:
            self._apply(recording, available=[1, 2, 3, 4, 5])
        assert excinfo.value.code == "tag_selection_too_large"
        with django_capture_on_commit_callbacks(execute=True) as captured:
            pass
        assert TagAssignment.objects.count() == 0
        assert captured == []

    def test_custom_create_atomic_with_selection(self, django_capture_on_commit_callbacks):
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-10")
        keep = make_tag("Keep")
        make_tag_assignment(recording, keep, origin="manual")
        drop = make_tag("Drop")
        make_tag_assignment(recording, drop, origin="suggested")
        with django_capture_on_commit_callbacks(execute=True) as captured:
            result = self._apply(
                recording, available=[keep.pk], new_tag_name="  Work  "
            )
        assert result["changed"] is True
        assert result["created_tag"] is True
        assert result["tag"].name == "Work"
        assert result["tag"].definition_origin == Tag.DefinitionOrigin.CUSTOM
        assert result["counts"]["created"] == 1  # the new custom tag only
        assert result["counts"]["removed"] == 1
        assert len(captured) == 1  # ONE sync for the whole commit
        work = Tag.objects.get(name_key="work")
        assert TagAssignment.objects.get(recording=recording, tag=work).origin == TagOrigin.MANUAL
        assert TagAssignment.objects.get(recording=recording, tag=drop).is_active is False

    def test_collision_rolls_back_all_selection_changes(self, django_capture_on_commit_callbacks):
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-11")
        keep = make_tag("Keep")
        make_tag_assignment(recording, keep, origin="manual")
        Tag.objects.create(
            name="Exists", name_key="exists", is_configured=True,
            definition_origin=Tag.DefinitionOrigin.CONFIG,
        )
        with django_capture_on_commit_callbacks(execute=True) as captured:
            with pytest.raises(TagOperationError) as excinfo:
                # keep would be REMOVED and the custom tag would collide:
                # the whole transaction must roll back.
                self._apply(recording, available=[], new_tag_name="Exists")
        assert excinfo.value.code == "duplicate_tag"
        assert captured == []  # nothing committed -> no callback
        assert Tag.objects.filter(name_key="exists").count() == 1
        assignment = TagAssignment.objects.get(recording=recording, tag=keep)
        assert assignment.is_active is True  # removal rolled back
        assert assignment.origin == TagOrigin.MANUAL

    def test_unchanged_after_converged_second_done_zero_callback(self, django_capture_on_commit_callbacks):
        """A second Done over an already-converged selection is zero DML
        and zero callback (the sync converges on the first commit)."""
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-12")
        tag = make_tag("Once")
        first = self._apply(recording, available=[tag.pk])
        assert first["changed"] is True
        with django_capture_on_commit_callbacks(execute=True) as captured:
            second = self._apply(recording, available=[tag.pk])
        assert second["changed"] is False
        assert second["counts"]["unchanged"] == 1
        assert captured == []
        assert TagAssignment.objects.filter(recording=recording, tag=tag).count() == 1

    @pytest.mark.parametrize("raw_ids", [None, "123", {1, 2}, 42, b"12", 1.5])
    def test_non_list_tuple_id_container_rejected(self, raw_ids, django_capture_on_commit_callbacks):
        """Only list/tuple containers are accepted; a bare str, None, a
        set, an int, bytes, etc. are rejected with a stable value-free
        error BEFORE any element is consumed."""
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-shape")
        make_tag("Real")
        with pytest.raises(TagOperationError) as excinfo:
            apply_tag_selection(recording, raw_ids, [], new_tag_name="")
        assert excinfo.value.code == "invalid_tag_selection"
        assert "Real" not in excinfo.value.message  # value-free
        with django_capture_on_commit_callbacks(execute=True) as captured:
            pass
        assert TagAssignment.objects.count() == 0
        assert captured == []

    def test_generator_rejected_without_consumption(self):
        """A generator (unbounded iterable) is rejected by the container
        check BEFORE iteration, so a hostile direct caller can never force
        unbounded accumulation."""
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-gen")
        make_tag("Real")
        consumed: list[int] = []

        def hostile():
            for i in range(10**6):
                consumed.append(i)
                yield i

        with pytest.raises(TagOperationError) as excinfo:
            apply_tag_selection(recording, hostile(), [], new_tag_name="")
        assert excinfo.value.code == "invalid_tag_selection"
        assert consumed == []  # never iterated

    def test_combined_available_plus_retired_cap_enforced(self, django_capture_on_commit_callbacks):
        """Each submitted list is individually under the limit but the
        COMBINED available+retired set exceeds it -> rejected with the
        stable too-large error before any category/existence work."""
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-comb")
        for i in range(4):
            make_tag(f"Avail{i}")
        for i in range(2):
            Tag.objects.create(
                name=f"Ret{i}", name_key=f"ret{i}", is_configured=False,
                definition_origin=Tag.DefinitionOrigin.CONFIG,
            )
        # 6 tags -> limit 7; 4 available + 4 retired = 8 > 7.
        with pytest.raises(TagOperationError) as excinfo:
            apply_tag_selection(
                recording, [1, 2, 3, 4], [101, 102, 103, 104], new_tag_name=""
            )
        assert excinfo.value.code == "tag_selection_too_large"
        with django_capture_on_commit_callbacks(execute=True) as captured:
            pass
        assert TagAssignment.objects.count() == 0
        assert captured == []

    def test_race_collision_mapped_to_duplicate_tag_transaction_stays_intact(
        self, monkeypatch, django_capture_on_commit_callbacks
    ):
        """A concurrent creator between the pre-validation and the insert
        hits the DB unique constraint inside the bulk transaction: the
        INNER atomic savepoint rolls back cleanly, the stable
        duplicate_tag error is raised (never TransactionManagementError /
        raw IntegrityError), the winner row survives, and the except-block
        re-query executes REAL SQL — proving the outer transaction stays
        usable. Nothing commits."""
        from django.db import connection

        from workflow.models import Tag as TagModel

        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-race")
        winner = TagModel.objects.create(
            name="Winner", name_key="race", is_configured=True,
            definition_origin=TagModel.DefinitionOrigin.CUSTOM,
        )
        real_filter = TagModel.objects.filter
        state = {"n": 0}

        def flaky_filter(*args, **kwargs):
            # Simulate the race window: the FIRST Tag.objects.filter call
            # is the pre-validation collision check and is blind (as under
            # true concurrency); every later call — including the
            # except-block re-query — executes real SQL, so a broken
            # transaction would surface as TransactionManagementError.
            state["n"] += 1
            qs = real_filter(*args, **kwargs)
            if state["n"] == 1:
                return qs.none()
            return qs

        monkeypatch.setattr(TagModel.objects, "filter", flaky_filter)
        with django_capture_on_commit_callbacks(execute=True) as captured:
            with pytest.raises(TagOperationError) as excinfo:
                apply_tag_selection(recording, [], [], new_tag_name="RACE")
        assert excinfo.value.code == "duplicate_tag"
        assert "Winner" in excinfo.value.message  # the real row was re-read
        assert captured == []  # rolled-back transaction schedules nothing
        # The transaction is usable again after the inner savepoint rollback.
        assert connection.needs_rollback is False
        assert real_filter(name_key="race").count() == 1  # exactly one winner
        assert TagAssignment.objects.count() == 0

    def test_custom_tag_requires_room_within_the_same_limit(
        self, monkeypatch, django_capture_on_commit_callbacks
    ):
        """A selection that already fills the combined cap cannot add a new
        custom tag: the request is rejected with the stable
        tag_selection_too_large error BEFORE any write — no tag, no
        assignment, no callback — so the documented hard cap is never
        violated by the appended definition."""
        from workflow.services import tags as tags_service

        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-room")
        first = make_tag("RoomA")
        second = make_tag("RoomB")
        # Hard cap 2 -> limit = min(2, 2 tags + 1) = 2; selecting BOTH
        # available tags fills the combined cap exactly.
        monkeypatch.setattr(tags_service, "_TAG_SELECTION_HARD_LIMIT", 2)
        with pytest.raises(TagOperationError) as excinfo:
            apply_tag_selection(
                recording, [first.pk, second.pk], [], new_tag_name="NewCustom"
            )
        assert excinfo.value.code == "tag_selection_too_large"
        with django_capture_on_commit_callbacks(execute=True) as captured:
            pass
        # Zero writes and zero callbacks.
        assert Tag.objects.filter(name_key="newcustom").count() == 0
        assert Tag.objects.count() == 2  # only the pre-existing definitions
        assert TagAssignment.objects.count() == 0
        assert captured == []
        # With one fewer selected ID there IS room: the custom tag is
        # created and both members become active in one commit.
        with django_capture_on_commit_callbacks(execute=True) as captured:
            result = apply_tag_selection(
                recording, [first.pk], [], new_tag_name="NewCustom"
            )
        assert result["created_tag"] is True
        assert result["counts"]["created"] == 2  # RoomB + the new custom tag
        assert len(captured) == 1
        assert TagAssignment.objects.filter(recording=recording).count() == 2

    @pytest.mark.parametrize("bad_name", [None, True, False, 123, b"Work", ["Work"]])
    def test_non_str_new_tag_name_rejected(self, bad_name, django_capture_on_commit_callbacks):
        """Only an exact str is accepted for new_tag_name; None/bool/any
        non-str raises a stable invalid_tag_name instead of being silently
        ignored or leaking."""
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-ntn")
        make_tag("Real")
        with pytest.raises(TagOperationError) as excinfo:
            apply_tag_selection(recording, [], [], new_tag_name=bad_name)
        assert excinfo.value.code == "invalid_tag_name"
        assert "Real" not in excinfo.value.message  # value-free
        with django_capture_on_commit_callbacks(execute=True) as captured:
            pass
        assert Tag.objects.count() == 1  # only the pre-existing tag
        assert TagAssignment.objects.count() == 0
        assert captured == []

    def test_blank_or_whitespace_new_tag_name_means_no_custom_tag(self, django_capture_on_commit_callbacks):
        """Only an exact empty/whitespace str means 'no custom tag'; the
        selection still applies normally and no definition is created."""
        recording, _t, _s = make_transcribed_recording(["x"], sha="bulk-blank")
        tag = make_tag("Pick")
        for i, blank in enumerate(("", "   ")):
            with django_capture_on_commit_callbacks(execute=True) as captured:
                result = apply_tag_selection(
                    recording, [tag.pk], [], new_tag_name=blank
                )
            assert result["created_tag"] is False
            assert result["tag"] is None
            if i == 0:
                assert result["changed"] is True
                assert len(captured) == 1  # the selection add still syncs
            else:
                assert result["changed"] is False  # already converged
                assert captured == []  # no callback for the unchanged Done
        assert Tag.objects.count() == 1  # no custom tag was created
        assert TagAssignment.objects.get(recording=recording, tag=tag).is_active is True
