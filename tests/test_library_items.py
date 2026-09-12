"""Step 6.2 Library item projection: DB-side UNION + bounded hydration.

Proves the approved product decisions on the CURRENT schema:

- any Recording with NO active topic Sections (unprocessed/no transcript,
  unsplit active transcript, crop-only active layout with zero Sections)
  yields exactly ONE recording-backed item;
- an active transcript/layout with N canonical topic Sections (valid
  N >= 2) yields exactly those N section-backed items and REPLACES its
  recording-backed item in the normal Library overview;
- historical (superseded) layouts are absent; retranscription naturally
  returns to one recording-backed item;
- exact DB-side count/pagination across mixed data (never a Python
  expansion); deterministic sorts (date grouping uses the parent
  Recording effective date; Title sorts use the item display title);
- filters: any/all tag semantics exact per item (recording scope vs that
  Section's assignments), summary/status/date/has_summary per item;
- bounded hydration: no N+1 (batched parent Recording/Section/tag/
  summary queries, constant as rows grow).
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import Client

from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    Section,
    SegmentedVersion,
    SummaryState,
    SummaryVariantState,
    TagAssignment,
    Transcript,
    TranscriptSegment,
)
from workflow.query import (
    ListFilters,
    apply_item_sort,
    hydrate_library_items,
    library_item_count,
    library_item_queryset,
)
from workflow.services.segmentation import save_segmented_version

from factories import (
    make_summary_version,
    make_tag,
    make_tag_assignment,
    make_transcribed_recording,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


def _split(recording, transcript, splits, titles, start=0, end=None):
    """Save an active topic layout; return the topic Sections by ordinal."""
    if end is None:
        end = transcript.segments.count()
    result = save_segmented_version(
        recording.pk, transcript.pk, start, end, list(splits), list(titles)
    )
    return list(
        Section.objects.filter(segmented_version_id=result.version_id).order_by("ordinal")
    )


def _items(filters=None, timezone_name="Europe/Helsinki"):
    """Project + sort + hydrate into card adapters (the view contract)."""
    filters = filters or ListFilters()
    qs = library_item_queryset(filters, timezone_name)
    qs = apply_item_sort(qs, filters.sort)
    return hydrate_library_items(qs)


def _recording_item(cards, recording) -> bool:
    return any(
        not c.is_section and c.recording_id == recording.pk for c in cards
    )


class TestOneRecordingItem:
    def test_unprocessed_recording_is_one_recording_item(self):
        rec = Recording.objects.create(
            sha256="lib-unproc-1", processing_status=ProcessingStatus.DISCOVERED
        )
        cards = _items()
        assert _recording_item(cards, rec)
        assert sum(1 for c in cards if not c.is_section and c.recording_id == rec.pk) == 1

    def test_unsplit_active_transcript_is_one_recording_item(self):
        rec, _t, _s = make_transcribed_recording(["a", "b"], sha="lib-unsplit-1")
        cards = _items()
        assert _recording_item(cards, rec)
        assert sum(1 for c in cards if c.recording_id == rec.pk) == 1

    def test_crop_only_layout_zero_sections_is_one_recording_item(self):
        rec, transcript, _s = make_transcribed_recording(
            ["a", "b", "c", "d"], sha="lib-crop-1"
        )
        _split(rec, transcript, [], [], start=1, end=3)
        cards = _items()
        assert _recording_item(cards, rec)
        assert sum(1 for c in cards if c.recording_id == rec.pk) == 1


class TestSectionItemsReplaceRecording:
    def test_split_n_yields_n_sections_and_no_parent(self):
        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d", "e"], sha="lib-split-1"
        )
        sections = _split(rec, transcript, [2], ["Topic A", "Topic B"])
        assert len(sections) == 2
        cards = _items()
        section_cards = [c for c in cards if c.recording_id == rec.pk]
        assert len(section_cards) == 2
        assert all(c.is_section for c in section_cards)
        assert {c.section_id for c in section_cards} == {s.pk for s in sections}
        assert not _recording_item(cards, rec)

    def test_section_item_identity_title_and_range(self):
        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d", "e"], sha="lib-split-2"
        )
        sections = _split(rec, transcript, [2], ["First part", "Second part"])
        cards = _items()
        by_section = {c.section_id: c for c in cards if c.is_section}
        first = by_section[sections[0].pk]
        second = by_section[sections[1].pk]
        assert first.title == "First part"
        assert second.title == "Second part"
        assert first.range_label == "segments 0–1"
        assert second.range_label == "segments 2–4"
        assert first.parent_title  # parent recording title context
        assert first.recording_id == rec.pk

    def test_old_layouts_absent_and_clear_crop_restores_recording(self):
        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d"], sha="lib-old-1"
        )
        old_sections = _split(rec, transcript, [2], ["Old A", "Old B"])
        # Save a crop-only full-range layout (zero topics): supersedes.
        _split(rec, transcript, [], [])
        cards = _items()
        # The old section items are gone and the recording item is back.
        assert not any(c.section_id in {s.pk for s in old_sections} for c in cards)
        assert _recording_item(cards, rec)
        assert sum(1 for c in cards if c.recording_id == rec.pk) == 1

    def test_retranscription_returns_to_one_recording_item(self):
        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c"], sha="lib-retx-1"
        )
        _split(rec, transcript, [1], ["Old A", "Old B"])
        # Retranscription: a NEW active transcript with no segmented version.
        attempt = ProcessingAttempt.objects.create(
            recording=rec,
            stage=AttemptStage.TRANSCRIPTION,
            ordinal=2,
            outcome=AttemptOutcome.SUCCESS,
        )
        transcript.is_active = False
        transcript.save(update_fields=["is_active"])
        new_transcript = Transcript.objects.create(
            recording=rec, attempt=attempt, text_normalized="new"
        )
        TranscriptSegment.objects.bulk_create(
            [
                TranscriptSegment(
                    transcript=new_transcript, ordinal=0, text="new segment"
                )
            ]
        )
        Section.objects.create(transcript=new_transcript, ordinal=0, title="Full")
        new_transcript.is_active = True
        new_transcript.save(update_fields=["is_active"])
        cards = _items()
        # One recording item; the old layout's sections never surface.
        assert sum(1 for c in cards if c.recording_id == rec.pk) == 1
        assert not _recording_item(cards, rec) is False or any(
            not c.is_section and c.recording_id == rec.pk for c in cards
        )
        assert all(
            not c.is_section or c.section.transcript_id != transcript.pk
            for c in cards
            if c.recording_id == rec.pk
        )

    def test_mixed_count_pagination(self):
        # 20 unsplit + 2 split (2 sections each) => 24 items.
        for index in range(20):
            rec, _t, _s = make_transcribed_recording(
                [f"m{index}"], sha=f"lib-mix-rec-{index}"
            )
        for index in range(2):
            rec, transcript, _fixed = make_transcribed_recording(
                ["a", "b", "c", "d"], sha=f"lib-mix-split-{index}"
            )
            _split(rec, transcript, [2], [f"S{index} A", f"S{index} B"])
        filters = ListFilters()
        count = library_item_count(filters, "Europe/Helsinki")
        assert count == 24
        qs = apply_item_sort(
            library_item_queryset(filters, "Europe/Helsinki"), "newest"
        )
        assert qs.count() == count  # the DB union row count matches
        rows = list(qs)
        assert len(rows) == 24
        kinds = {row["item_kind"] for row in rows}
        assert kinds == {"recording", "section"}


class TestSorts:
    def _mixed(self):
        a, t_a, _s_a = make_transcribed_recording(["a"], sha="lib-sort-a")
        b, t_b, _s_b = make_transcribed_recording(["b"], sha="lib-sort-b")
        c, t_c, _s_c = make_transcribed_recording(
            ["x", "y", "z", "w"], sha="lib-sort-c"
        )
        sections = _split(c, t_c, [2], ["Zebra topic", "Alpha topic"])
        return a, b, c, sections

    def test_newest_oldest_use_parent_effective_date(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        older, _t, _s = make_transcribed_recording(["a"], sha="lib-date-older")
        older.recorded_at = datetime(2026, 1, 1, 10, 0, tzinfo=ZoneInfo("Europe/Helsinki"))
        older.save(update_fields=["recorded_at"])
        newer, transcript, _fixed = make_transcribed_recording(
            ["x", "y"], sha="lib-date-newer"
        )
        newer.recorded_at = datetime(2026, 1, 5, 10, 0, tzinfo=ZoneInfo("Europe/Helsinki"))
        newer.save(update_fields=["recorded_at"])
        sections = _split(newer, transcript, [1], ["Sec A", "Sec B"])
        cards = _items(ListFilters(sort="newest"))
        assert [c.recording_id for c in cards] == [newer.pk, newer.pk, older.pk]
        cards_old = _items(ListFilters(sort="oldest"))
        assert [c.recording_id for c in cards_old] == [older.pk, newer.pk, newer.pk]

    def test_title_sort_uses_item_display_title(self):
        a, b, c, sections = self._mixed()
        # Section titles "Zebra topic" (first) / "Alpha topic" (second).
        cards = _items(ListFilters(sort="title_az"))
        titles = [c.title for c in cards]
        assert titles == sorted(titles, key=lambda t: t.casefold())
        assert titles[0] == "Alpha topic"
        assert "Zebra topic" in titles
        za = _items(ListFilters(sort="title_za"))
        assert [c.title for c in za] == list(reversed(titles))

    def test_title_tiebreak_deterministic(self):
        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c"], sha="lib-tie-1"
        )
        _split(rec, transcript, [1], ["Same", "Same"])
        first = _items(ListFilters(sort="title_az"))
        second = _items(ListFilters(sort="title_az"))
        assert [c.section_id for c in first] == [c.section_id for c in second]


class TestFilters:
    def _setup(self):
        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d"], sha="lib-filt-1"
        )
        sections = _split(rec, transcript, [2], ["Topic A", "Topic B"])
        other, _t, _s = make_transcribed_recording(["z"], sha="lib-filt-2")
        return rec, transcript, sections, other

    def test_tag_any_matches_section_assignment_only(self):
        rec, transcript, sections, other = self._setup()
        tag = make_tag("Family")
        TagAssignment.objects.create(
            recording=rec, section=sections[0], tag=tag, origin="manual", is_active=True
        )
        cards = _items(ListFilters(tags=[tag.name_key], tag_match="any"))
        # Only the tagged Section item matches; its sibling and the other
        # recording do not.
        assert [c.section_id for c in cards] == [sections[0].pk]
        assert not _recording_item(cards, other)

    def test_tag_all_requires_every_tag_per_item(self):
        rec, transcript, sections, other = self._setup()
        family = make_tag("Family")
        academic = make_tag("Academic")
        TagAssignment.objects.create(
            recording=rec, section=sections[0], tag=family, origin="manual", is_active=True
        )
        TagAssignment.objects.create(
            recording=rec, section=sections[0], tag=academic, origin="manual", is_active=True
        )
        TagAssignment.objects.create(
            recording=rec, section=sections[1], tag=family, origin="manual", is_active=True
        )
        cards = _items(ListFilters(tags=[family.name_key, academic.name_key]))
        assert [c.section_id for c in cards] == [sections[0].pk]

    def test_recording_tag_filter_unchanged(self):
        rec, _t, _s = make_transcribed_recording(["a"], sha="lib-filt-rec")
        tag = make_tag("Work")
        make_tag_assignment(rec, tag, origin="manual")
        cards = _items(ListFilters(tags=[tag.name_key]))
        assert [c.recording_id for c in cards] == [rec.pk]

    def test_summary_status_filter_section_default_variant(self):
        rec, transcript, sections, other = self._setup()
        SummaryVariantState.objects.create(
            transcript=transcript, section=sections[0],
            output_language="en", status="current",
        )
        cards = _items(ListFilters(summary=SummaryState.CURRENT))
        assert [c.section_id for c in cards] == [sections[0].pk]
        # Missing (no variant-state row at all) matches the sibling.
        cards_missing = _items(ListFilters(summary=SummaryState.MISSING))
        assert {c.section_id for c in cards_missing if c.is_section} == {sections[1].pk}
        # The untranscribed/summary-less other recording is also missing.
        other_ids = [c.recording_id for c in cards_missing if not c.is_section]
        assert other.pk in other_ids

    def test_status_filter_inherits_parent(self):
        rec, transcript, sections, other = self._setup()
        cards = _items(ListFilters(status=ProcessingStatus.TRANSCRIBED))
        assert {c.recording_id for c in cards} == {rec.pk, other.pk}
        assert len([c for c in cards if c.is_section]) == 2

    def test_audio_filter_inherits_parent(self):
        rec, transcript, sections, other = self._setup()
        Recording.objects.filter(pk=other.pk).update(audio_status="missing")
        cards = _items(ListFilters(audio="missing"))
        assert [c.recording_id for c in cards] == [other.pk]
        assert all(not c.is_section for c in cards)

    def test_date_filter_uses_parent_effective_date(self):
        from datetime import date, datetime
        from zoneinfo import ZoneInfo

        rec, transcript, sections, _other = self._setup()
        rec.recorded_at = datetime(2026, 2, 10, 12, 0, tzinfo=ZoneInfo("Europe/Helsinki"))
        rec.save(update_fields=["recorded_at"])
        other, _t, _s = make_transcribed_recording(["y"], sha="lib-filt-date")
        other.recorded_at = datetime(2026, 3, 1, 12, 0, tzinfo=ZoneInfo("Europe/Helsinki"))
        other.save(update_fields=["recorded_at"])
        cards = _items(ListFilters(date=date(2026, 2, 10)))
        assert {c.recording_id for c in cards} == {rec.pk}
        assert len(cards) == 2  # the two section items of the split recording

    def test_has_summary_section_default_variant(self):
        rec, transcript, sections, _other = self._setup()
        make_summary_version(rec, transcript, sections[0], output_language="en")
        SummaryVariantState.objects.create(
            transcript=transcript, section=sections[0],
            output_language="en", status="current",
        )
        cards = _items(ListFilters(has_summary=True))
        assert [c.section_id for c in cards] == [sections[0].pk]
        cards_without = _items(ListFilters(has_summary=False))
        assert {c.section_id for c in cards_without if c.is_section} == {sections[1].pk}

    def test_section_available_languages(self):
        rec, transcript, sections, _other = self._setup()
        make_summary_version(rec, transcript, sections[0], output_language="en")
        make_summary_version(rec, transcript, sections[0], output_language="zh-Hant")
        SummaryVariantState.objects.create(
            transcript=transcript, section=sections[0],
            output_language="en", status="current",
        )
        cards = _items()
        card = next(c for c in cards if c.section_id == sections[0].pk)
        assert card.available_languages == ["en", "zh-Hant"]

    def test_invalid_filters_never_error(self):
        _items(ListFilters(status="banana"))
        _items(ListFilters(summary="banana"))


class TestHydrationBound:
    def _seed(self, prefix, split_count, plain_count):
        for index in range(plain_count):
            rec, _t, _s = make_transcribed_recording(
                [f"{prefix}p{index}"], sha=f"{prefix}-plain-{index}"
            )
        for index in range(split_count):
            rec, transcript, _fixed = make_transcribed_recording(
                ["a", "b", "c", "d"], sha=f"{prefix}-split-{index}"
            )
            _split(rec, transcript, [2], [f"T{index} A", f"T{index} B"])
            make_tag(f"{prefix}-tag-{index}")

    def test_query_count_constant_as_rows_grow(self):
        # A FIXED number of split recordings (bounded layout-validation
        # cost) plus a growing number of PLAIN recordings: the projection
        # must not add queries per recording.
        self._seed("fixed-splits", 3, 5)

        def run(plain):
            self._seed(f"vary-{plain}", 0, plain)
            with CaptureQueriesContext(connection) as ctx:
                _items()
            return len(ctx.captured_queries)

        small = run(5)
        large = run(40)
        assert small == large, f"query count grew: {small} -> {large}"

    def test_hydration_is_batched_not_per_section(self):
        # MANY split recordings => a full page of section items: tags,
        # summaries and variant states are hydrated with a few batched
        # queries, never one per section.
        self._seed("hyd", 13, 0)  # 13 split recordings => 26 section items
        with CaptureQueriesContext(connection) as ctx:
            _items()
        tag_queries = [
            q["sql"]
            for q in ctx.captured_queries
            if "workflow_tagassignment" in q["sql"]
        ]
        state_queries = [
            q["sql"]
            for q in ctx.captured_queries
            if "workflow_summaryvariantstate" in q["sql"]
        ]
        # 26 section items all hydrated by ONE batched IN query each.
        assert len(tag_queries) <= 2
        assert len(state_queries) <= 2

    def test_page_context_uses_item_count(self):
        for index in range(30):
            rec, _t, _s = make_transcribed_recording([f"z{index}"], sha=f"pg-{index}")
        client = Client()
        response = client.get("/recordings/")
        assert response.status_code == 200
        assert response.context["page"].paginator.count == 30
        assert len(response.context["cards"]) == 25


class TestLibraryProjectionLazyState:
    """Fix: the active-topic-layout state is a LAZY parameterized
    database-side subquery — the number of active layouts/sections never
    materializes in Python, never grows SQL parameters, and never changes
    the projection query count. Malformed layouts still fail closed."""

    def _seed_splits(self, prefix, count):
        for index in range(count):
            rec, transcript, _fixed = make_transcribed_recording(
                ["a", "b", "c", "d"], sha=f"{prefix}-split-{index:04d}"
            )
            _split(rec, transcript, [2], [f"T{index} A", f"T{index} B"])

    def _projection(self):
        """Execute ONLY the DB-side projection + count (no hydration):
        returns (captured queries, materialized row count, item count)."""
        filters = ListFilters()
        qs = apply_item_sort(
            library_item_queryset(filters, "Europe/Helsinki"), "newest"
        )
        with CaptureQueriesContext(connection) as ctx:
            rows = list(qs)
            count = qs.count()
        return ctx.captured_queries, len(rows), count

    def _projection_sql(self):
        qs = apply_item_sort(
            library_item_queryset(ListFilters(), "Europe/Helsinki"), "newest"
        )
        return str(qs.query)

    def test_fixed_query_count_sql_and_parameters_at_scale(self):
        self._seed_splits("lazy-small", 5)
        small_q, small_rows, small_count = self._projection()
        small_sql = self._projection_sql()
        self._seed_splits("lazy-large", 220)  # 225 splits => 450 items
        large_q, large_rows, large_count = self._projection()
        large_sql = self._projection_sql()

        # Exact DB-side count semantics at scale (never a Python union
        # expansion): 5 splits -> 10 items, 225 splits -> 450 items.
        assert small_rows == small_count == 10
        assert large_rows == large_count == 450

        # FIXED query behavior: the projection layer runs the SAME number
        # of queries regardless of how many active layouts exist.
        assert len(large_q) == len(small_q)

        # FIXED SQL/parameter behavior: the compiled projection SQL is
        # BYTE-IDENTICAL across scales — the validated-layout state is a
        # constant parameterized subquery (two fixed parameters per
        # branch), never a growing IN (...) id list and never a Python
        # expansion.
        assert small_sql == large_sql
        assert "workflow_segmentedversion" in large_sql
        assert "workflow_section" in large_sql
        assert "IN (1, 2" not in large_sql

    def test_malformed_lone_section_layout_fails_closed(self):
        # A corrupt active layout (lone topic Section) never hides its
        # recording and never surfaces Section items.
        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d"], sha="prepass-malformed"
        )
        version = SegmentedVersion.objects.create(
            transcript=transcript, revision=1, start_segment_ordinal=0,
            end_segment_ordinal_exclusive=4, is_active=True,
            activated_at=transcript.activated_at or transcript.created_at,
        )
        Section.objects.create(
            transcript=transcript, segmented_version=version, ordinal=1,
            title="Only topic", start_segment_ordinal=0,
            end_segment_ordinal_exclusive=4,
        )
        cards = _items()
        # The recording stays as ONE recording-backed item; the corrupt
        # section is never projected.
        assert _recording_item(cards, rec)
        assert not any(c.is_section for c in cards)
        assert sum(1 for c in cards if c.recording_id == rec.pk) == 1

    def test_cross_parent_layout_fails_closed(self):
        # A cross-parent Section row corrupts the layout: fail closed —
        # the parent recording stays visible, no Section items.
        rec_a, t_a, _fixed_a = make_transcribed_recording(
            ["a", "b", "c", "d"], sha="prepass-cross-a"
        )
        rec_b, t_b, _fixed_b = make_transcribed_recording(
            ["x", "y", "z"], sha="prepass-cross-b"
        )
        sections = _split(rec_a, t_a, [2], ["A", "B"])
        Section.objects.create(
            transcript=t_b, segmented_version=sections[0].segmented_version,
            ordinal=3, title="Cross", start_segment_ordinal=0,
            end_segment_ordinal_exclusive=3,
        )
        cards = _items()
        assert _recording_item(cards, rec_a)
        assert not any(c.is_section for c in cards)

    def test_oversized_section_count_layout_fails_closed(self):
        # A version carrying more Sections than the canonical cap is
        # corrupt: the SQL predicate excludes it (no unbounded fetch) and
        # the recording stays visible as one recording item.
        from workflow.services.segmentation import MAX_TOPIC_SECTIONS

        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d"], sha="prepass-oversized"
        )
        version = SegmentedVersion.objects.create(
            transcript=transcript, revision=1, start_segment_ordinal=0,
            end_segment_ordinal_exclusive=4, is_active=True,
            activated_at=transcript.activated_at or transcript.created_at,
        )
        Section.objects.bulk_create(
            [
                Section(
                    transcript=transcript, segmented_version=version,
                    ordinal=i, title=f"T{i}", start_segment_ordinal=0,
                    end_segment_ordinal_exclusive=4,
                )
                for i in range(1, MAX_TOPIC_SECTIONS + 2)
            ]
        )
        cards = _items()
        assert _recording_item(cards, rec)
        assert not any(c.is_section for c in cards)

    def test_control_char_title_layout_fails_closed(self):
        # A title carrying a control character (tab) makes the layout
        # corrupt: fail closed (no items, recording stays visible).
        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d"], sha="prepass-ctrl"
        )
        version = SegmentedVersion.objects.create(
            transcript=transcript, revision=1, start_segment_ordinal=0,
            end_segment_ordinal_exclusive=4, is_active=True,
            activated_at=transcript.activated_at or transcript.created_at,
        )
        Section.objects.create(
            transcript=transcript, segmented_version=version, ordinal=1,
            title="Good A", start_segment_ordinal=0,
            end_segment_ordinal_exclusive=2,
        )
        Section.objects.create(
            transcript=transcript, segmented_version=version, ordinal=2,
            title="Bad\tTitle", start_segment_ordinal=2,
            end_segment_ordinal_exclusive=4,
        )
        cards = _items()
        assert _recording_item(cards, rec)
        assert not any(c.is_section for c in cards)

    def test_blank_title_layout_fails_closed(self):
        # A whitespace-only title makes the layout corrupt: fail closed.
        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d"], sha="prepass-blank"
        )
        version = SegmentedVersion.objects.create(
            transcript=transcript, revision=1, start_segment_ordinal=0,
            end_segment_ordinal_exclusive=4, is_active=True,
            activated_at=transcript.activated_at or transcript.created_at,
        )
        Section.objects.create(
            transcript=transcript, segmented_version=version, ordinal=1,
            title="Good A", start_segment_ordinal=0,
            end_segment_ordinal_exclusive=2,
        )
        Section.objects.create(
            transcript=transcript, segmented_version=version, ordinal=2,
            title="   ", start_segment_ordinal=2,
            end_segment_ordinal_exclusive=4,
        )
        cards = _items()
        assert _recording_item(cards, rec)
        assert not any(c.is_section for c in cards)

    def test_unicode_whitespace_title_layout_fails_closed(self):
        """Fix: an all-Unicode-whitespace title (ideographic space U+3000)
        is blank per Python 3.12 ``str.strip()`` — SQLite's one-argument
        TRIM would NOT catch it, so the SQL predicate must use the full
        Python whitespace set. Fails closed: no items, recording visible."""
        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d"], sha="prepass-uniws"
        )
        version = SegmentedVersion.objects.create(
            transcript=transcript, revision=1, start_segment_ordinal=0,
            end_segment_ordinal_exclusive=4, is_active=True,
            activated_at=transcript.activated_at or transcript.created_at,
        )
        Section.objects.create(
            transcript=transcript, segmented_version=version, ordinal=1,
            title="Good A", start_segment_ordinal=0,
            end_segment_ordinal_exclusive=2,
        )
        Section.objects.create(
            transcript=transcript, segmented_version=version, ordinal=2,
            title="\u3000", start_segment_ordinal=2,
            end_segment_ordinal_exclusive=4,
        )
        cards = _items()
        assert _recording_item(cards, rec)
        assert not any(c.is_section for c in cards)

    def test_nonblank_unicode_title_stays_valid(self):
        """Nonblank exact Unicode titles are preserved (not rejected)."""
        rec, transcript, _fixed = make_transcribed_recording(
            ["a", "b", "c", "d"], sha="prepass-uniok"
        )
        sections = _split(rec, transcript, [2], ["\u4f1a\u8bae", "Etude"])
        cards = _items()
        assert not _recording_item(cards, rec)
        assert {c.section_id for c in cards} == {s.pk for s in sections}