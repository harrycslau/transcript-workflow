"""Step 6.2a temporary split titles + derived display title + section
duration (service, parser, web, display/sort parity, duration).

Proves the approved contract:

- new editor-created ranges get ``Segment N of YYYYMMDDHHMM`` (N = the
  1-based canonical section ordinal; timestamp = ``recorded_at`` else
  ``discovered_at`` in the CONFIGURED timezone, server-authoritative);
  existing exact ranges preserve title/provenance; editing a title makes
  it custom;
- the editor payload carries bounded exact flags; the parser/service
  validate cardinality/type and reject any True flag whose non-blank
  title does not EXACTLY equal the server-derived expected value
  (forgery) — a custom title can never be claimed temporary;
- the stored ``Section.title`` is NEVER mutated during summary
  generation; whenever an active DEFAULT-language section Summary exists
  its ``Summary.title`` is the Section's display/page/Library title —
  superseding BOTH a stored temporary title AND a manually entered
  custom title in presentation (display override only; the custom title
  stays layout metadata/provenance); without a default Summary the
  stored ``Section.title`` is used; non-default tabs never change the
  page identity; DB title ordering and the rendered Library title use
  the SAME derived SQL expression; the Section detail page renders the
  summary title ONCE (H1), never a second time inside the summary body,
  while Recording Detail keeps its embedded title paragraph;
- section duration is the approximate span (earliest usable start_ms to
  latest usable end_ms, /1000) over the canonical range, safe unknown
  when unavailable/nonpositive; recording items keep the recording
  duration; no N+1/unbounded reads; the duration renders in the normal
  card/table and on section detail.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from brainlib.config import ConfigError, LLMConfig, TagSpec, TagsConfig
from workflow.models import (
    Recording,
    Section,
    SegmentedVersion,
    Summary,
    SummaryState,
    TranscriptSegment,
)
from workflow.services.segmentation import (
    SegmentationError,
    canonical_layout_for_transcript,
    derive_temporary_section_title,
    parse_segmentation_payload,
    save_segmented_version,
    segmentation_fingerprint,
    validate_payload_for_transcript,
)

from factories import (
    final_summary_json,
    make_config,
    make_summary_version,
    make_transcribed_recording,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]

TZ = "Europe/Helsinki"


def _recording(count=6, *, recorded=None, sha="temp-title"):
    """A transcribed recording with ``count`` segments; ``recorded`` sets
    ``recorded_at`` (aware)."""
    rec, transcript, fixed = make_transcribed_recording(
        [f"segment {i}" for i in range(count)], sha=sha
    )
    if recorded is not None:
        rec.recorded_at = recorded
        rec.save(update_fields=["recorded_at"])
    return rec, transcript, fixed


def _split(rec, transcript, splits, titles, flags, *, tz=TZ):
    return save_segmented_version(
        rec.pk, transcript.pk, 0, transcript.segments.count(),
        splits, titles, flags, timezone_name=tz,
    )


def _sections(result):
    return list(
        Section.objects.filter(segmented_version_id=result.version_id).order_by("ordinal")
    )


def _fragment_client():
    return Client()


# ---------------------------------------------------------------------------
# Server-derived temporary title
# ---------------------------------------------------------------------------


class TestDerivedTitle:
    def test_recorded_at_preferred_over_discovered_at(self):
        recorded = datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ))
        rec, _t, _f = _recording(recorded=recorded, sha="tt-recorded")
        assert derive_temporary_section_title(rec, 1, TZ) == "Segment 1 of 202609120930"
        # discovered_at is later (default now) — recorded_at must win.
        assert "202609120930" in derive_temporary_section_title(rec, 2, TZ)

    def test_discovered_at_used_when_no_recorded_at(self):
        rec, _t, _f = _recording(recorded=None, sha="tt-discovered")
        rec.discovered_at = datetime(2026, 1, 5, 8, 0, tzinfo=ZoneInfo(TZ))
        rec.save(update_fields=["discovered_at"])
        assert derive_temporary_section_title(rec, 3, TZ) == "Segment 3 of 202601050800"

    def test_timezone_is_server_authoritative(self):
        recorded = datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo("UTC"))
        rec, _t, _f = _recording(recorded=recorded, sha="tt-tz")
        # Same instant rendered in different configured zones.
        assert derive_temporary_section_title(rec, 1, "Europe/Helsinki") == (
            "Segment 1 of 202609121230"
        )
        assert derive_temporary_section_title(rec, 1, "America/New_York") == (
            "Segment 1 of 202609120530"
        )

    def test_ordinal_must_be_positive_int(self):
        rec, _t, _f = _recording(sha="tt-ordinal")
        with pytest.raises(SegmentationError) as exc:
            derive_temporary_section_title(rec, 0, TZ)
        assert exc.value.code == "invalid_input"


# ---------------------------------------------------------------------------
# Service: flags / derived titles / forgery / preservation
# ---------------------------------------------------------------------------


class TestServiceFlags:
    def test_new_ranges_get_derived_titles_and_true_flags(self):
        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        result = _split(rec, transcript, [2], ["", ""], [True, True])
        sections = _sections(result)
        assert [(s.title, s.title_is_temporary) for s in sections] == [
            ("Segment 1 of 202609120930", True),
            ("Segment 2 of 202609120930", True),
        ]
        assert all(s.start_ms is None and s.end_ms is None for s in sections)

    def test_discovered_timestamp_used_in_derived_title(self):
        rec, transcript, _f = _recording(recorded=None, sha="tt-svc-disc")
        rec.discovered_at = datetime(2026, 3, 4, 5, 6, tzinfo=ZoneInfo(TZ))
        rec.save(update_fields=["discovered_at"])
        result = _split(rec, transcript, [2], ["", ""], [True, True])
        sections = _sections(result)
        assert sections[1].title == "Segment 2 of 202603040506"

    def test_non_blank_true_flag_must_equal_derived_title(self):
        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        expected_1 = derive_temporary_section_title(rec, 1, TZ)
        expected_2 = derive_temporary_section_title(rec, 2, TZ)
        # Exactly the derived values are accepted (blank is filled below).
        result = _split(rec, transcript, [2], [expected_1, expected_2], [True, True])
        sections = _sections(result)
        assert sections[0].title == expected_1 and sections[0].title_is_temporary is True
        assert sections[1].title == expected_2 and sections[1].title_is_temporary is True
        # A wrong-but-shaped value for ANY ordinal is a forgery.
        with pytest.raises(SegmentationError) as exc:
            _split(rec, transcript, [2], [expected_1, expected_1], [True, True])
        assert exc.value.code == "title_flag_forgery"

    def test_true_flag_forgery_rejected_atomically(self):
        rec, transcript, _f = _recording(sha="tt-forge")
        with pytest.raises(SegmentationError) as exc:
            _split(rec, transcript, [2], ["My forged title", ""], [True, True])
        assert exc.value.code == "title_flag_forgery"
        assert SegmentedVersion.objects.count() == 0

    def test_blank_custom_title_rejected(self):
        rec, transcript, _f = _recording(sha="tt-blank-custom")
        with pytest.raises(SegmentationError) as exc:
            _split(rec, transcript, [2], ["", "Custom"], [False, False])
        assert exc.value.code == "title_blank"
        assert SegmentedVersion.objects.count() == 0

    def test_editing_title_makes_it_custom(self):
        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        result = _split(rec, transcript, [2], ["Custom A", ""], [False, True])
        sections = _sections(result)
        assert sections[0].title == "Custom A" and sections[0].title_is_temporary is False
        assert sections[1].title == "Segment 2 of 202609120930"
        assert sections[1].title_is_temporary is True

    def test_flag_count_mismatch_rejected(self):
        rec, transcript, _f = _recording(sha="tt-count")
        with pytest.raises(SegmentationError) as exc:
            _split(rec, transcript, [2], ["A", "B"], [True])
        assert exc.value.code == "title_flag_count_mismatch"
        with pytest.raises(SegmentationError) as exc:
            _split(rec, transcript, [2], ["A", "B"], [True, True, True])
        assert exc.value.code == "title_flag_count_mismatch"

    def test_flag_type_rejected(self):
        rec, transcript, _f = _recording(sha="tt-type")
        with pytest.raises(SegmentationError) as exc:
            _split(rec, transcript, [2], ["A", "B"], [1, 0])
        assert exc.value.code == "invalid_input"

    def test_omitted_flags_default_to_custom(self):
        """Legacy callers (and pre-0013 payloads) without flags keep the
        custom-title semantics: every title stored verbatim, False flag."""
        rec, transcript, _f = _recording(sha="tt-legacy")
        result = save_segmented_version(rec.pk, transcript.pk, 0, 6, [2], ["A", "B"])
        sections = _sections(result)
        assert [(s.title, s.title_is_temporary) for s in sections] == [
            ("A", False), ("B", False),
        ]

    def test_existing_exact_range_preserves_title_and_provenance(self):
        """A re-save with the SAME payload (stored temporary titles + True
        flags) is a ZERO-DML no-op — existing exact ranges preserve
        title/provenance, never regenerated."""
        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        result = _split(rec, transcript, [2], ["", ""], [True, True])
        sections = _sections(result)
        titles = [s.title for s in sections]
        revisions_before = SegmentedVersion.objects.count()
        again = _split(rec, transcript, [2], titles, [True, True])
        assert again.created is False
        assert SegmentedVersion.objects.count() == revisions_before
        # Stored titles/flags unchanged.
        fresh = _sections(result)
        assert [s.title for s in fresh] == titles
        assert all(s.title_is_temporary for s in fresh)

    def test_flag_change_alone_creates_a_revision(self):
        """The flags are part of the immutable payload: changing only a
        flag (e.g. promoting a custom title to the exact derived name with
        True) is NOT a no-op."""
        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        expected = derive_temporary_section_title(rec, 1, TZ)
        result = _split(rec, transcript, [2], [expected, "Custom B"], [False, False])
        sections = _sections(result)
        assert sections[0].title_is_temporary is False
        # Re-save with the SAME titles but True flag on section 1.
        again = _split(rec, transcript, [2], [expected, "Custom B"], [True, False])
        assert again.created is True
        fresh = _sections(again)
        assert fresh[0].title_is_temporary is True
        # Prior revision preserved as history (immutable).
        assert SegmentedVersion.objects.filter(transcript=transcript).count() == 2
        assert sections[0].segmented_version_id != fresh[0].segmented_version_id

    def test_canonical_layout_exposes_flag_and_fingerprint_binds_it(self):
        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        result = _split(rec, transcript, [2], ["", ""], [True, True])
        version = SegmentedVersion.objects.get(pk=result.version_id)
        canonical = canonical_layout_for_transcript(version, transcript)
        assert [(s["ordinal"], s["title"], s["title_is_temporary"]) for s in canonical["sections"]] == [
            (1, "Segment 1 of 202609120930", True),
            (2, "Segment 2 of 202609120930", True),
        ]
        fp = segmentation_fingerprint(rec.pk, transcript, timezone_name=TZ)
        # A custom re-save (different flags) invalidates the fingerprint.
        _split(rec, transcript, [2], ["Custom A", "Custom B"], [False, False])
        assert segmentation_fingerprint(rec.pk, transcript, timezone_name=TZ) != fp

    def test_layout_immutable_after_supersede(self):
        rec, transcript, _f = _recording(sha="tt-immutable")
        result = _split(rec, transcript, [2], ["", ""], [True, True])
        sections = _sections(result)
        # Supersede with a crop-only full-range layout.
        save_segmented_version(rec.pk, transcript.pk, 0, transcript.segments.count())
        sections[0].refresh_from_db()
        assert sections[0].title_is_temporary is True
        assert sections[0].segmented_version.is_active is False


# ---------------------------------------------------------------------------
# Parser / read-only payload validation
# ---------------------------------------------------------------------------


class TestParser:
    def _qd(self, **data):
        from django.http import QueryDict

        qd = QueryDict(mutable=True)
        for key, values in data.items():
            if isinstance(values, (list, tuple)):
                qd.setlist(key, list(values))
            else:
                qd[key] = values
        return qd

    def test_parses_exact_01_flags(self):
        qd = self._qd(
            transcript_id="1", start="0", end_exclusive="6",
            fingerprint="a" * 64,
            split=["2"], title=["A", "B"],
            title_is_temporary=["0", "1"],
        )
        payload = parse_segmentation_payload(qd)
        assert payload["title_is_temporary"] == [False, True]

    def test_rejects_bad_flag_values_and_cardinality(self):
        fingerprint = "a" * 64
        for bad in ("2", "true", "on", "", "True", "0.0"):
            qd = self._qd(
                transcript_id="1", start="0", end_exclusive="6",
                fingerprint=fingerprint, split=["2"], title=["A", "B"],
                title_is_temporary=[bad, "0"],
            )
            with pytest.raises(SegmentationError) as exc:
                parse_segmentation_payload(qd)
            assert exc.value.code == "invalid_input", bad
        qd = self._qd(
            transcript_id="1", start="0", end_exclusive="6",
            fingerprint=fingerprint, split=["2"], title=["A", "B"],
            title_is_temporary=["1"],
        )
        with pytest.raises(SegmentationError) as exc:
            parse_segmentation_payload(qd)
        assert exc.value.code == "title_flag_count_mismatch"

    def test_absent_flags_normalize_to_all_custom(self):
        qd = self._qd(
            transcript_id="1", start="0", end_exclusive="6",
            fingerprint="a" * 64, split=["2"], title=["A", "B"],
        )
        payload = parse_segmentation_payload(qd)
        assert payload["title_is_temporary"] == [False, False]

    def test_oversized_and_unknown_fields_rejected(self):
        from workflow.services.segmentation import MAX_TOPIC_SECTIONS

        qd = self._qd(
            transcript_id="1", start="0", end_exclusive="6",
            fingerprint="a" * 64, split=["2"], title=["A", "B"],
            title_is_temporary=["0"] * (MAX_TOPIC_SECTIONS + 1),
        )
        with pytest.raises(SegmentationError) as exc:
            parse_segmentation_payload(qd)
        assert exc.value.code == "too_many_topics"
        qd = self._qd(
            transcript_id="1", start="0", end_exclusive="6",
            fingerprint="a" * 64, evil="x",
        )
        with pytest.raises(SegmentationError) as exc:
            parse_segmentation_payload(qd)
        assert exc.value.code == "invalid_input"

    def test_readonly_semantic_validation_rejects_forgery(self):
        rec, transcript, _f = _recording(
            recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)), sha="tt-validate"
        )
        qd = self._qd(
            transcript_id=str(transcript.pk), start="0", end_exclusive="6",
            fingerprint="a" * 64, split=["2"],
            title=["Forged", "B"], title_is_temporary=["1", "0"],
        )
        payload = parse_segmentation_payload(qd)
        with pytest.raises(SegmentationError) as exc:
            validate_payload_for_transcript(rec, transcript, payload, timezone_name=TZ)
        assert exc.value.code == "title_flag_forgery"
        # Read-only: nothing written.
        assert SegmentedVersion.objects.count() == 0

    def test_readonly_semantic_validation_accepts_blank_temporary(self):
        rec, transcript, _f = _recording(
            recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)), sha="tt-validate-ok"
        )
        qd = self._qd(
            transcript_id=str(transcript.pk), start="0", end_exclusive="6",
            fingerprint="a" * 64, split=["2"],
            title=["", "Custom B"], title_is_temporary=["1", "0"],
        )
        payload = parse_segmentation_payload(qd)
        validate_payload_for_transcript(rec, transcript, payload, timezone_name=TZ)


# ---------------------------------------------------------------------------
# Web: editor payload + two-step save with flags
# ---------------------------------------------------------------------------


class TestWebEditor:
    @pytest.fixture
    def client(self):
        return Client()

    def _editor_state(self, content):
        match = re.search(
            r'<script id="segmentation-editor-state" type="application/json">(.*?)</script>',
            content,
            re.DOTALL,
        )
        assert match, "editor state json_script not rendered"
        import json

        return json.loads(match.group(1))

    def _fingerprint(self, content):
        import html

        match = re.search(
            r'name="fingerprint" id="segmentation-fingerprint-input" value="([^"]*)"',
            content,
        )
        assert match
        return html.unescape(match.group(1))

    def test_editor_state_carries_flags(self, client):
        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        result = save_segmented_version(
            rec.pk, transcript.pk, 0, 6, [2], ["Custom A", "Segment 2 of 202609120930"],
            [False, True], timezone_name=TZ,
        )
        content = client.get(f"/recordings/{rec.pk}/transcript/").content.decode()
        state = self._editor_state(content)
        assert state["splits"] == [2]
        assert state["titles"] == ["Custom A", "Segment 2 of 202609120930"]
        assert state["title_is_temporary"] == [False, True]

    def test_first_post_renders_flags_and_derived_titles(self, client):
        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        fingerprint = segmentation_fingerprint(rec.pk, transcript, timezone_name=TZ)
        response = client.post(
            f"/recordings/{rec.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "0",
                "end_exclusive": "6",
                "split": ["2"],
                "title": ["", ""],
                "title_is_temporary": ["1", "1"],
                "fingerprint": fingerprint,
            },
        )
        assert response.status_code == 200
        content = response.content.decode()
        # The confirmation shows the SERVER-derived names (never blanks).
        assert "Segment 1 of 202609120930" in content
        assert "Segment 2 of 202609120930" in content
        # The hidden payload carries the exact flags back.
        assert 'name="title_is_temporary" value="1"' in content
        assert content.count('name="title_is_temporary"') == 2
        assert SegmentedVersion.objects.count() == 0  # no mutation

    def test_confirmed_save_creates_derived_sections(self, client):
        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        fingerprint = segmentation_fingerprint(rec.pk, transcript, timezone_name=TZ)
        response = client.post(
            f"/recordings/{rec.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "0",
                "end_exclusive": "6",
                "split": ["2"],
                "title": ["", ""],
                "title_is_temporary": ["1", "1"],
                "fingerprint": fingerprint,
                "confirmed": "1",
            },
        )
        assert response.status_code == 302
        version = SegmentedVersion.objects.get(transcript=transcript, is_active=True)
        sections = list(version.sections.order_by("ordinal"))
        assert [(s.title, s.title_is_temporary) for s in sections] == [
            ("Segment 1 of 202609120930", True),
            ("Segment 2 of 202609120930", True),
        ]

    def test_confirmed_save_rejects_forged_flag(self, client):
        rec, transcript, _f = _recording(sha="tt-web-forge")
        fingerprint = segmentation_fingerprint(rec.pk, transcript, timezone_name=TZ)
        response = client.post(
            f"/recordings/{rec.pk}/transcript/save/",
            {
                "transcript_id": str(transcript.pk),
                "start": "0",
                "end_exclusive": "6",
                "split": ["2"],
                "title": ["Not derived", "B"],
                "title_is_temporary": ["1", "0"],
                "fingerprint": fingerprint,
                "confirmed": "1",
            },
        )
        assert response.status_code == 400
        assert "auto-named" in response.content.decode().lower()
        assert SegmentedVersion.objects.count() == 0

    def test_confirmed_save_still_locked_and_schedules_no_sync(self, client):
        """The temporary-title save keeps the exact Step 6.1 contract:
        confirmed saves run under the global pipeline lock (busy => 409)
        and schedule NO recording search/embedding sync."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from workflow.services.pipeline_lock import pipeline_lock
        from workflow.views.helpers import get_config

        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        fingerprint = segmentation_fingerprint(rec.pk, transcript, timezone_name=TZ)
        data = {
            "transcript_id": str(transcript.pk),
            "start": "0",
            "end_exclusive": "6",
            "split": ["2"],
            "title": ["", ""],
            "title_is_temporary": ["1", "1"],
            "fingerprint": fingerprint,
            "confirmed": "1",
        }
        # Busy lock => friendly 409, nothing written.
        holder = pipeline_lock(get_config())
        holder.__enter__()
        try:
            response = client.post(f"/recordings/{rec.pk}/transcript/save/", data)
            assert response.status_code == 409
        finally:
            holder.__exit__(None, None, None)
        assert SegmentedVersion.objects.count() == 0

        # Uncontended confirmed save: no search/embedding document writes.
        with CaptureQueriesContext(connection) as ctx:
            response = client.post(f"/recordings/{rec.pk}/transcript/save/", data)
        assert response.status_code == 302
        assert not any("workflow_search_document" in q["sql"] for q in ctx.captured_queries)
        assert not any("workflow_embedding_document" in q["sql"] for q in ctx.captured_queries)
        version = SegmentedVersion.objects.get(transcript=transcript, is_active=True)
        sections = list(version.sections.order_by("ordinal"))
        assert sections[0].title_is_temporary is True


# ---------------------------------------------------------------------------
# Temporary provenance READ validation: corrupt True-flag rows fail closed
# ---------------------------------------------------------------------------


class TestTemporaryProvenanceReadValidation:
    @pytest.fixture
    def client(self):
        return Client()

    def _editor_state(self, content):
        match = re.search(
            r'<script id="segmentation-editor-state" type="application/json">(.*?)</script>',
            content,
            re.DOTALL,
        )
        assert match, "editor state json_script not rendered"
        import json

        return json.loads(match.group(1))

    def _corrupt_temp_row(self, sha="tt-corrupt"):
        """A valid temporary layout whose first section's stored title is
        replaced with an ARBITRARY custom title while
        ``title_is_temporary`` stays True — corrupt stored state."""
        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        result = save_segmented_version(
            rec.pk, transcript.pk, 0, 6, [2], ["", ""], [True, True],
            timezone_name=TZ,
        )
        version = SegmentedVersion.objects.get(pk=result.version_id)
        section = version.sections.get(ordinal=1)
        section.title = "Arbitrary custom title"
        section.save(update_fields=["title"])
        return rec, transcript, version, section

    def test_corrupt_temporary_row_fails_closed_in_canonical_reads(self):
        rec, transcript, version, section = self._corrupt_temp_row()
        with pytest.raises(SegmentationError) as exc:
            canonical_layout_for_transcript(version, transcript)
        assert exc.value.code == "layout_invalid"

    def test_corrupt_temporary_row_section_detail_404(self, client):
        rec, transcript, version, section = self._corrupt_temp_row("tt-corrupt-404")
        response = client.get(f"/recordings/{rec.pk}/sections/{section.pk}/")
        assert response.status_code == 404

    def test_corrupt_temporary_row_library_parent_fallback(self, client):
        """The Library SQL validity predicate rejects the corrupt layout:
        neither corrupt section appears and the PARENT recording falls
        back to a normal recording-backed item."""
        rec, transcript, version, section = self._corrupt_temp_row("tt-corrupt-lib")
        from workflow.query import (
            apply_item_sort,
            hydrate_library_items,
            library_item_count,
            library_item_queryset,
        )

        qs = library_item_queryset(__import__("workflow.query", fromlist=["ListFilters"]).ListFilters(), TZ)
        qs = apply_item_sort(qs, "newest")
        rows = list(qs)
        assert len(rows) == 1
        assert rows[0]["item_kind"] == "recording"
        assert rows[0]["recording_id"] == rec.pk
        assert rows[0]["section_id"] is None
        assert library_item_count(__import__("workflow.query", fromlist=["ListFilters"]).ListFilters(), TZ) == 1
        # The corrupt section is not linkable from the Library.
        content = client.get("/recordings/").content.decode()
        assert f"/sections/{section.pk}/" not in content

    def test_unchanged_noop_usable_after_effective_timestamp_drift(self):
        """The no-op comparison uses the RAW submitted payload: an
        unchanged active temporary layout stays a ZERO-DML no-op even
        after the recording's effective timestamp changed (reads and the
        no-op never compare stored titles to the current timestamp)."""
        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        result = save_segmented_version(
            rec.pk, transcript.pk, 0, 6, [2], ["", ""], [True, True],
            timezone_name=TZ,
        )
        sections = _sections(result)
        stored_titles = [s.title for s in sections]
        assert all(s.title_is_temporary for s in sections)
        revisions = SegmentedVersion.objects.count()

        # Drift: recorded_at changes (what the CURRENT server-derived
        # title would now be differs from the stored creation-time title).
        rec.recorded_at = datetime(2026, 1, 1, 0, 0, tzinfo=ZoneInfo(TZ))
        rec.save(update_fields=["recorded_at"])
        current_derived = derive_temporary_section_title(rec, 1, TZ)
        assert current_derived != stored_titles[0]

        # Re-submitting the UNCHANGED stored payload is a usable no-op.
        again = save_segmented_version(
            rec.pk, transcript.pk, 0, 6, [2], list(stored_titles), [True, True],
            timezone_name=TZ,
        )
        assert again.created is False
        assert SegmentedVersion.objects.count() == revisions
        # The read-side shape validation still accepts the stored titles
        # (creation-time metadata; never compared to the current time).
        version = SegmentedVersion.objects.get(pk=result.version_id)
        canonical = canonical_layout_for_transcript(version, transcript)
        assert canonical["titles"] == tuple(stored_titles)

    def test_editor_json_carries_server_temporary_titles(self, client):
        """The bounded server-generated temporary-title list is rendered
        in the editor JSON (index = ordinal - 1), so the editor can
        visibly prefill new split-created sections without deriving a
        timestamp from the browser clock."""
        rec, transcript, _f = _recording(recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        content = client.get(f"/recordings/{rec.pk}/transcript/").content.decode()
        state = self._editor_state(content)
        assert "temporary_titles" in state
        assert state["temporary_titles"][0] == "Segment 1 of 202609120930"
        assert state["temporary_titles"][1] == "Segment 2 of 202609120930"
        assert len(state["temporary_titles"]) == 200  # bounded at MAX_TOPIC_SECTIONS
        assert all(isinstance(t, str) for t in state["temporary_titles"])


# ---------------------------------------------------------------------------
# Display: derived user-facing title (Library + section detail + tabs)
# ---------------------------------------------------------------------------


class TestDisplayTitle:
    @pytest.fixture
    def client(self):
        return Client()

    def _zh_split(self, sha, recorded=None):
        """A recorded transcription with a temporary split layout; returns
        (rec, transcript, sections)."""
        rec, transcript, _f = _recording(count=5, recorded=recorded, sha=sha)
        transcript.language_observed = "zh-HK"
        transcript.save(update_fields=["language_observed"])
        result = save_segmented_version(
            rec.pk, transcript.pk, 0, 5, [2], ["", ""], [True, True],
            timezone_name=TZ,
        )
        return rec, transcript, _sections(result)

    def test_library_card_uses_default_summary_title_for_temporary(self, client):
        rec, transcript, sections = self._zh_split("disp-lib-summary")
        make_summary_version(
            rec, transcript, sections[0], title="Generated section title",
            output_language="zh-Hant",
        )
        content = client.get("/recordings/").content.decode()
        assert "Generated section title" in content
        # The stored temporary name is NOT displayed for that section.
        stored = derive_temporary_section_title(rec, 1, TZ)
        assert stored not in content

    def test_library_table_uses_default_summary_title_for_temporary(self, client):
        rec, transcript, sections = self._zh_split("disp-table-summary")
        make_summary_version(
            rec, transcript, sections[0], title="Table generated title",
            output_language="zh-Hant",
        )
        content = client.get("/recordings/?view=table").content.decode()
        assert "Table generated title" in content

    def test_library_stored_title_when_no_default_summary(self, client):
        rec, transcript, sections = self._zh_split("disp-no-summary")
        stored = derive_temporary_section_title(rec, 1, TZ)
        content = client.get("/recordings/").content.decode()
        assert stored in content

    def test_default_summary_title_wins_over_custom_title(self, client):
        """The active DEFAULT-language Summary's title is the user-facing
        Library title even when the stored Section title is a manually
        entered CUSTOM title (display override only — the stored custom
        title stays layout metadata/provenance and is never mutated)."""
        rec, transcript, _f = _recording(count=5, sha="disp-custom")
        transcript.language_observed = "zh-HK"
        transcript.save(update_fields=["language_observed"])
        result = save_segmented_version(
            rec.pk, transcript.pk, 0, 5, [2], ["My custom name", ""],
            [False, True], timezone_name=TZ,
        )
        sections = _sections(result)
        make_summary_version(
            rec, transcript, sections[0], title="Generated default title",
            output_language="zh-Hant",
        )
        content = client.get("/recordings/").content.decode()
        assert "Generated default title" in content
        # The stored custom title is NOT rendered for that section (it is
        # only layout metadata now) and the Summary title never wins over
        # the stored title in the DB.
        assert "My custom name" not in content
        sections[0].refresh_from_db()
        assert sections[0].title == "My custom name"

    def test_non_default_variant_never_wins(self, client):
        """Only the active DEFAULT-language Summary's title may supersede a
        temporary title — an optional variant (even lower ordinal) never
        changes the display title."""
        rec, transcript, sections = self._zh_split("disp-nondefault")
        make_summary_version(
            rec, transcript, sections[0], title="Optional variant title",
            output_language="en",
        )
        stored = derive_temporary_section_title(rec, 1, TZ)
        content = client.get("/recordings/").content.decode()
        assert stored in content
        assert "Optional variant title" not in content

    def test_title_sort_uses_same_derived_expression(self, client):
        """DB Title A–Z ordering and the rendered Library title use the
        SAME derived SQL expression: a Section sorts by its default-
        Summary title — whether the stored title is TEMPORARY or a
        manually entered CUSTOM one — never by the stored name."""
        rec_a, t_a, secs_a = self._zh_split("disp-sort-a")
        make_summary_version(rec_a, t_a, secs_a[0], title="Zulu topic", output_language="zh-Hant")
        # rec_b carries a CUSTOM stored title ("Zealot custom") that the
        # default Summary title still supersedes for display AND sorting.
        rec_b, transcript_b, _f = _recording(count=5, sha="disp-sort-b")
        transcript_b.language_observed = "zh-HK"
        transcript_b.save(update_fields=["language_observed"])
        result_b = save_segmented_version(
            rec_b.pk, transcript_b.pk, 0, 5, [2], ["Zealot custom", ""],
            [False, True], timezone_name=TZ,
        )
        secs_b = _sections(result_b)
        make_summary_version(
            rec_b, transcript_b, secs_b[0], title="Alpha topic",
            output_language="zh-Hant",
        )
        cards = client.get("/recordings/?sort=title_az").context["cards"]
        # The FIRST section of each recording carries the derived Summary
        # title; the second stays stored (no summary). Compare the
        # summarized section-1 cards' ORDER (the derived expression drives
        # the DB ordering).
        first_by_rec = {
            card.recording_id: card for card in cards
            if card.is_section and card.section.ordinal == 1
        }
        assert first_by_rec[rec_a.pk].title == "Zulu topic"
        assert first_by_rec[rec_b.pk].title == "Alpha topic"
        ordered = [c for c in cards if c.is_section]
        assert ordered.index(first_by_rec[rec_b.pk]) < ordered.index(first_by_rec[rec_a.pk])
        # Rendered order matches the same derived titles.
        content = client.get("/recordings/?sort=title_az").content.decode()
        assert content.index("Alpha topic") < content.index("Zulu topic")
        # The custom stored title is superseded everywhere on the page.
        assert "Zealot custom" not in content

    def test_section_detail_h1_uses_default_summary_title(self, client):
        rec, transcript, sections = self._zh_split("disp-detail-h1")
        make_summary_version(
            rec, transcript, sections[0], title="Detail generated title",
            output_language="zh-Hant",
        )
        content = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/"
        ).content.decode()
        assert "<h1 class=\"detail-title\">Detail generated title</h1>" in content
        assert f"<title>Detail generated title — Brain</title>" in content

    def test_section_detail_h1_stable_across_non_default_tabs(self, client):
        rec, transcript, sections = self._zh_split("disp-detail-tabs")
        make_summary_version(
            rec, transcript, sections[0], title="Stable page identity",
            output_language="zh-Hant",
        )
        make_summary_version(
            rec, transcript, sections[0], title="Optional variant title",
            output_language="en",
        )
        default = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/"
        ).content.decode()
        english = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/?language=en"
        ).content.decode()
        assert "<h1 class=\"detail-title\">Stable page identity</h1>" in default
        # Switching to the optional EN tab NEVER changes the page identity.
        assert "<h1 class=\"detail-title\">Stable page identity</h1>" in english
        assert "<h1 class=\"detail-title\">Optional variant title</h1>" not in english

    def test_section_detail_custom_title_wins_and_stored_when_no_summary(self, client):
        rec, transcript, _f = _recording(count=5, sha="disp-detail-custom")
        transcript.language_observed = "zh-HK"
        transcript.save(update_fields=["language_observed"])
        result = save_segmented_version(
            rec.pk, transcript.pk, 0, 5, [2], ["My custom", ""], [False, True],
            timezone_name=TZ,
        )
        sections = _sections(result)
        make_summary_version(
            rec, transcript, sections[0], title="Generated title", output_language="zh-Hant",
        )
        content = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/"
        ).content.decode()
        # The H1 is the DEFAULT Summary title even over a stored CUSTOM
        # title (a display override only — the stored title is untouched).
        assert "<h1 class=\"detail-title\">Generated title</h1>" in content
        assert "<h1 class=\"detail-title\">My custom</h1>" not in content
        sections[0].refresh_from_db()
        assert sections[0].title == "My custom"
        # The stored custom title still shows as provenance metadata.
        assert "My custom" in content
        # No summary at all: the stored temporary title is the H1.
        rec2, transcript2, secs2 = self._zh_split("disp-detail-stored")
        content2 = client.get(
            f"/recordings/{rec2.pk}/sections/{secs2[1].pk}/"
        ).content.decode()
        stored2 = derive_temporary_section_title(rec2, 2, TZ)
        assert f"<h1 class=\"detail-title\">{stored2}</h1>" in content2

    def test_section_detail_renders_summary_title_once(self, client):
        """The Section page has ONE user-facing title: the generated
        default Summary title renders as the H1 only — never a second
        time inside the summary body (the embedded title paragraph is
        suppressed), while the h3 heading hierarchy is retained."""
        rec, transcript, sections = self._zh_split("disp-detail-once")
        make_summary_version(
            rec, transcript, sections[0], title="Single page title",
            overview="Single overview text.", output_language="zh-Hant",
        )
        content = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/"
        ).content.decode()
        # The title appears in the document <title> and the H1 — exactly
        # one rendered heading, and NEVER inside the summary body as a
        # duplicate embedded title paragraph.
        assert '<h1 class="detail-title">Single page title</h1>' in content
        assert content.count("Single page title") == 2  # <title> + H1
        assert content.count('class="summary-title"') == 0
        # The h3 heading hierarchy survives the suppression.
        assert "<h3>Overview</h3>" in content
        assert "Single overview text." in content

    def test_recording_detail_keeps_embedded_summary_title_paragraph(self, client):
        """Recording Detail keeps its existing embedded title paragraph:
        the ``suppress_title`` option is a Section-detail-only include
        option and does not change the Recording page."""
        rec, transcript, fixed = _recording(count=5, sha="disp-rec-detail")
        make_summary_version(
            rec, transcript, fixed, title="Recording summary title",
            overview="Recording overview.", output_language="en",
        )
        content = client.get(f"/recordings/{rec.pk}/").content.decode()
        assert "Recording summary title" in content
        assert content.count('class="summary-title">Recording summary title</p>') == 1

    def test_section_title_never_mutated_during_summary_generation(self, tmp_path, monkeypatch):
        """Running the real section-summarization path NEVER writes
        Section.title: the stored temporary/custom name is untouched by
        summary generation (the derived display title is read-only)."""
        from workflow.services.segmentation import save_segmented_version as sv

        config = make_config(tmp_path, llm=LLMConfig(
            provider="openai_compatible", base_url="http://127.0.0.1:1/v1",
            model="test-model", api_key_env="BRAIN_TEST_LLM_API_KEY",
            temperature=0.2, timeout_seconds=600,
        ), tags=TagsConfig(allowed=(
            TagSpec(name="Family", description="F"), TagSpec(name="Academic", description="A"),
        )))
        rec, transcript, _f = _recording(count=5, recorded=datetime(2026, 9, 12, 9, 30, tzinfo=ZoneInfo(TZ)))
        result = sv(rec.pk, transcript.pk, 0, 5, [2], ["", ""], [True, True], timezone_name=TZ)
        sections = _sections(result)
        stored_title = sections[0].title

        class ScriptedLLM:
            def __call__(self, *, system, user):
                return final_summary_json(title="Generated by model")

        from workflow.services.summarize import summarize_section_one

        result = summarize_section_one(config, sections[0], llm_call=ScriptedLLM())
        assert result["result"] == "summarized"
        sections[0].refresh_from_db()
        assert sections[0].title == stored_title
        assert sections[0].title_is_temporary is True
        # The generated Summary carries its own title (the display source).
        summary = Summary.objects.get(section=sections[0], is_active=True)
        assert summary.title == "Generated by model"


# ---------------------------------------------------------------------------
# Section duration
# ---------------------------------------------------------------------------


class TestSectionDuration:
    @pytest.fixture
    def client(self):
        return Client()

    def _recording(self, count=6, sha="dur-web"):
        rec, transcript, _fixed = make_transcribed_recording(
            [f"segment {i}" for i in range(count)], sha=sha
        )
        # Factories set start_ms=i*1000, end_ms=(i+1)*1000.
        return rec, transcript

    def _split(self, rec, transcript, splits, titles, flags=None):
        flags = flags if flags is not None else [False] * len(titles)
        result = save_segmented_version(
            rec.pk, transcript.pk, 0, transcript.segments.count(),
            splits, titles, flags, timezone_name=TZ,
        )
        return _sections(result)

    def test_span_from_earliest_usable_start_to_latest_usable_end(self):
        rec, transcript = self._recording(sha="dur-span")
        sections = self._split(rec, transcript, [2], ["A", "B"])
        from workflow.query import section_duration_seconds

        # Section 1 = segments [0,2): start 0s, end 2s => 2.0s.
        assert section_duration_seconds(sections[0]) == 2.0
        # Section 2 = segments [2,6): start 2s, end 6s => 4.0s.
        assert section_duration_seconds(sections[1]) == 4.0

    def test_null_timestamps_give_unknown(self):
        rec, transcript = self._recording(sha="dur-null")
        sections = self._split(rec, transcript, [2], ["A", "B"])
        from workflow.query import section_duration_seconds

        TranscriptSegment.objects.filter(transcript=transcript).update(start_ms=None, end_ms=None)
        assert section_duration_seconds(sections[0]) is None

    def test_partial_null_endpoints_span_independently(self):
        """Earliest NON-NULL start_ms and latest NON-NULL end_ms are
        selected INDEPENDENTLY within the canonical range: a first
        segment carrying only a start and a last segment carrying only an
        end still yield a span."""
        rec, transcript = self._recording(sha="dur-partial")
        sections = self._split(rec, transcript, [2], ["A", "B"])
        from workflow.query import section_duration_seconds

        # Section 1 = segments [0,2): segment 0 start-only (0ms), segment
        # 1 end-only (end 2000ms) => span = 2.0s.
        TranscriptSegment.objects.filter(transcript=transcript, ordinal=0).update(end_ms=None)
        TranscriptSegment.objects.filter(transcript=transcript, ordinal=1).update(start_ms=None)
        assert section_duration_seconds(sections[0]) == 2.0

    def test_missing_either_endpoint_gives_unknown(self):
        rec, transcript = self._recording(sha="dur-partial-missing")
        sections = self._split(rec, transcript, [2], ["A", "B"])
        from workflow.query import section_duration_seconds

        # No usable START anywhere in the range => unknown.
        TranscriptSegment.objects.filter(transcript=transcript).update(start_ms=None)
        assert section_duration_seconds(sections[0]) is None
        # No usable END anywhere in the range => unknown.
        TranscriptSegment.objects.filter(transcript=transcript).update(
            start_ms=1000, end_ms=None
        )
        assert section_duration_seconds(sections[0]) is None

    def test_nonpositive_span_is_unknown(self):
        rec, transcript = self._recording(sha="dur-nonpos")
        sections = self._split(rec, transcript, [2], ["A", "B"])
        from workflow.query import section_duration_seconds

        # Every usable segment carries a REVERSED span so the max end_ms is
        # not greater than the min start_ms => nonpositive => unknown.
        TranscriptSegment.objects.filter(transcript=transcript).update(
            start_ms=2000, end_ms=1000
        )
        assert section_duration_seconds(sections[0]) is None

    def test_recording_items_keep_recording_duration(self, client):
        rec, transcript = self._recording(sha="dur-rec")
        rec.duration_seconds = 123.5
        rec.save(update_fields=["duration_seconds"])
        cards = client.get("/recordings/").context["cards"]
        recording_cards = [c for c in cards if not c.is_section]
        assert any(c.duration_seconds == 123.5 for c in recording_cards)

    def test_library_card_and_table_render_section_duration(self, client):
        rec, transcript = self._recording(sha="dur-render")
        sections = self._split(rec, transcript, [2], ["A", "B"])
        content = client.get("/recordings/").content.decode()
        # Section 1 span = 2s => "2s"; section 2 span = 4s => "4s".
        assert ">2s<" in content
        assert ">4s<" in content
        content_table = client.get("/recordings/?view=table").content.decode()
        assert ">2s<" in content_table
        assert ">4s<" in content_table

    def test_unknown_duration_renders_safely(self, client):
        rec, transcript = self._recording(sha="dur-unknown")
        sections = self._split(rec, transcript, [2], ["A", "B"])
        TranscriptSegment.objects.filter(transcript=transcript).update(start_ms=None, end_ms=None)
        content = client.get("/recordings/").content.decode()
        assert "unknown" in content

    def test_section_detail_shows_duration(self, client):
        rec, transcript = self._recording(sha="dur-detail")
        sections = self._split(rec, transcript, [2], ["A", "B"])
        content = client.get(
            f"/recordings/{rec.pk}/sections/{sections[1].pk}/"
        ).content.decode()
        assert ">4s<" in content

    def test_section_detail_renders_unknown_explicitly(self, client):
        """An unavailable/nonpositive section duration renders the literal
        'unknown' meta item — the field is never omitted."""
        rec, transcript = self._recording(sha="dur-detail-unknown")
        sections = self._split(rec, transcript, [2], ["A", "B"])
        TranscriptSegment.objects.filter(transcript=transcript).update(start_ms=None, end_ms=None)
        content = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/"
        ).content.decode()
        assert "unknown" in content

    def test_library_query_count_constant_with_duration(self, client):
        def run(row_count):
            for index in range(row_count):
                rec, transcript = self._recording(sha=f"dur-q-{row_count}-{index}")
                self._split(rec, transcript, [2], ["A", "B"])
            with CaptureQueriesContext(connection) as ctx:
                client.get("/recordings/")
            return len(ctx.captured_queries)

        small = run(4)
        large = run(25)
        assert small == large, f"query count grew: {small} -> {large}"

    def test_get_remains_select_only_with_duration_and_titles(self, client):
        rec, transcript = self._recording(sha="dur-select")
        sections = self._split(rec, transcript, [2], ["", ""], [True, True])
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(f"/recordings/{rec.pk}/sections/{sections[0].pk}/")
        assert response.status_code == 200
        non_select = [
            q for q in ctx.captured_queries
            if not q["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert non_select == []