"""Tests for deterministic Markdown/plain-text summary renderers."""

from __future__ import annotations

import pytest

from workflow.services.rendering import key_point_lines, render_markdown, render_text, summary_to_dict

pytestmark = pytest.mark.django_db


def make_summary(**overrides):
    from factories import make_transcribed_recording

    from workflow.models import ProcessingAttempt, Summary

    recording, transcript, section = make_transcribed_recording(["segment one", "segment two"])
    attempt = ProcessingAttempt.objects.create(
        recording=recording, stage="transcription", ordinal=99, outcome="success",
        finished_at=transcript.activated_at,
    )
    fields = dict(
        recording=recording,
        transcript=transcript,
        section=section,
        attempt=attempt,
        ordinal=1,
        is_active=True,
        activated_at=transcript.activated_at,
        title="研究會議記錄 Research meeting",
        overview="討論咗研究計劃。Mixed English terminology is preserved.",
        key_points=["Point A", "Point 中文 B"],
        action_items=[{"text": "Write draft", "owner": "Alice", "due_date": "Friday"}],
        people=["Alice", "Bob"],
        organizations=[" university "],
        topics=["grading"],
        language="zh-HK",
        suggested_tags_raw={"suggested": ["Academic", "Read2Learn"], "rejected": []},
        generation_mode="automatic",
    )
    fields.update(overrides)
    return Summary.objects.create(**fields)


class TestMarkdown:
    def test_full_render_is_stable_and_golden(self):
        summary = make_summary()
        expected = (
            "# 研究會議記錄 Research meeting\n"
            "\n"
            "## Overview\n"
            "\n"
            "討論咗研究計劃。Mixed English terminology is preserved.\n"
            "\n"
            "## Key points\n"
            "- Point A\n"
            "- Point 中文 B\n"
            "\n"
            "## Action items\n"
            "- Write draft (owner: Alice; due: Friday)\n"
            "\n"
            "## People\n"
            "- Alice\n"
            "- Bob\n"
            "\n"
            "## Organizations\n"
            "-  university \n"
            "\n"
            "## Topics\n"
            "- grading\n"
            "\n"
            "## Tags\n"
            "\n"
            "Academic, Read2Learn\n"
        )
        assert render_markdown(summary) == expected
        assert render_markdown(summary) == render_markdown(summary)  # deterministic

    def test_empty_optional_sections_omitted(self):
        summary = make_summary(
            key_points=[], action_items=[], people=[], organizations=[], topics=[],
            suggested_tags_raw={"suggested": [], "rejected": []},
        )
        text = render_markdown(summary)
        assert "## Key points" not in text
        assert "## People" not in text
        assert "## Organizations" not in text
        assert "## Topics" not in text
        assert "## Tags" not in text
        # Action items: meaningful placeholder instead of an empty section.
        assert "- No action items identified." in text

    def test_action_item_without_owner_or_due(self):
        summary = make_summary(action_items=[{"text": "Call back", "owner": None, "due_date": None}])
        assert "- Call back" in render_markdown(summary)

    def test_copy_friendly_no_json(self):
        text = render_markdown(make_summary())
        assert '"suggested_tags"' not in text
        assert "{" not in text

    def test_structured_points_are_numbered_deterministically(self):
        summary = make_summary(
            key_points=[
                {"text": "Main", "level": 1},
                {"text": "Child", "level": 2},
                {"text": "Grandchild", "level": 3},
                {"text": "Extra detail", "level": 0},
                {"text": "Second main", "level": 1},
            ]
        )
        text = render_markdown(summary)
        assert "1. Main\n1.1 Child\n1.1.1 Grandchild\n- Extra detail\n2. Second main" in text


def test_key_point_lines_support_historical_strings():
    assert key_point_lines(["Old point"]) == ["- Old point"]


class TestPlainText:
    def test_full_render_is_stable(self):
        summary = make_summary()
        text = render_text(summary)
        assert text.startswith("Title: 研究會議記錄 Research meeting\n")
        assert "Key points:\n- Point A\n- Point 中文 B\n" in text
        assert "Action items:\n- Write draft (owner: Alice; due: Friday)\n" in text
        assert "People:\n- Alice\n- Bob\n" in text
        assert "Tags: Academic, Read2Learn\n" in text
        assert render_text(summary) == render_text(summary)

    def test_empty_optional_sections(self):
        summary = make_summary(
            key_points=[], action_items=[], people=[], organizations=[], topics=[],
            suggested_tags_raw={"suggested": [], "rejected": []},
        )
        text = render_text(summary)
        assert "Key points:" not in text
        assert "People:" not in text
        assert "Tags:" not in text
        assert "No action items identified." in text


class TestStructuredDict:
    def test_summary_to_dict_shape(self):
        payload = summary_to_dict(make_summary())
        assert payload["title"] == "研究會議記錄 Research meeting"
        assert payload["suggested_tags"] == ["Academic", "Read2Learn"]
        assert payload["input_truncated"] is False
        assert payload["is_active"] is True
        assert "attempt_id" in payload
