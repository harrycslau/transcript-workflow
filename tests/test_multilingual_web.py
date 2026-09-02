"""Multilingual web acceptance tests.

Proves the approved web behaviour:
- recording-detail and summary pages offer Default / English /
  Traditional Chinese / Original plus existing concrete variants (e.g.
  Finnish) and render ONLY the selected variant's summary;
- per-state Generate / Retry / Regenerate POST actions with the
  language preserved through the confirmation interstitial;
- export/Copy links preserve the selected language;
- variant isolation (generating one variant never replaces another);
- unresolved-Original GET is strictly read-only (zero external effects,
  zero writes, only SELECT queries);
- unknown concrete languages are a friendly 404 (no 500, no silent
  default fallback, on pages AND exports);
- stale fingerprint and lock contention stay safe.
"""

from __future__ import annotations

import json

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from factories import make_summary_version, make_transcribed_recording
from test_summarize import final_summary_json, make_llm_config
from workflow.models import Recording, SummaryVariantState
from workflow.services.web_actions import state_fingerprint

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]

ZH_PAYLOAD = final_summary_json(
    title="會議記錄",
    overview="討論了評分計劃。",
    key_points=[{"text": "評分將於週一開始", "level": 1}],
    language="zh-HK",
)


@pytest.fixture
def client():
    return Client()


def _multilingual_recording(sha="mweb-1", *, with_zh=False):
    """A Finnish-source recording with an English (default) summary."""
    recording, transcript, section = make_transcribed_recording(
        ["hello world"], sha=sha
    )
    transcript.language_observed = "fi"
    transcript.save(update_fields=["language_observed"])
    en = make_summary_version(recording, transcript, section, output_language="en")
    SummaryVariantState.objects.create(
        transcript=transcript, section=section, output_language="en", status="current"
    )
    zh = None
    if with_zh:
        zh = make_summary_version(
            recording, transcript, section,
            title="中文標題：評分計劃",
            overview="這是中文摘要的概覽。",
            output_language="zh-Hant",
        )
        SummaryVariantState.objects.create(
            transcript=transcript, section=section,
            output_language="zh-Hant", status="current",
        )
    return recording, transcript, section, en, zh


def _detail(client, recording, language=None):
    url = f"/recordings/{recording.pk}/"
    if language:
        url += f"?language={language}"
    return client.get(url)


def _summary_page(client, recording, language=None):
    url = f"/recordings/{recording.pk}/summary/"
    if language:
        url += f"?language={language}"
    return client.get(url)


class TestVariantSwitching:
    def test_default_shows_default_language_summary(self, client):
        recording, _t, _s, en, _zh = _multilingual_recording()
        response = _detail(client, recording)
        assert response.status_code == 200
        assert en.title in response.content.decode()

    def test_explicit_english_selector_switches_correctly(self, client):
        recording, _t, _s, en, zh = _multilingual_recording(with_zh=True)
        response = _detail(client, recording, "en")
        content = response.content.decode()
        # English content shown — NOT the Chinese variant.
        assert en.title in content
        assert zh.title not in content

    def test_zh_hant_selector_shows_chinese_content(self, client):
        recording, _t, _s, en, zh = _multilingual_recording(with_zh=True)
        response = _detail(client, recording, "zh-Hant")
        content = response.content.decode()
        assert zh.title in content
        assert zh.overview in content
        assert en.title not in content

    def test_finnish_variant_appears_when_it_exists(self, client):
        recording, transcript, section, _en, _zh = _multilingual_recording()
        fi = make_summary_version(
            recording, transcript, section,
            title="Suomenkielinen otsikko", output_language="fi",
        )
        SummaryVariantState.objects.create(
            transcript=transcript, section=section, output_language="fi", status="current"
        )
        response = _detail(client, recording, "fi")
        assert response.status_code == 200
        content = response.content.decode()
        assert fi.title in content
        assert "Finnish" in content  # friendly label tab present
        # Summary page too.
        response = _summary_page(client, recording, "fi")
        assert fi.title in response.content.decode()

    def test_all_four_standard_selectors_offered(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        content = _detail(client, recording).content.decode()
        for label in ("Default", "English", "Traditional Chinese", "Original"):
            assert label in content
        # Summary page offers the same controls.
        content = _summary_page(client, recording).content.decode()
        for label in ("Default", "English", "Traditional Chinese", "Original"):
            assert label in content

    def test_unknown_concrete_language_is_404(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        assert _detail(client, recording, "sv").status_code == 404
        assert _summary_page(client, recording, "sv").status_code == 404

    def test_summary_history_shows_output_language(self, client):
        recording, transcript, section, _en, _zh = _multilingual_recording(with_zh=True)
        response = client.get(f"/recordings/{recording.pk}/history/")
        content = response.content.decode()
        assert "zh-Hant" in content
        assert "en" in content


class TestPerStateActions:
    def test_missing_variant_offers_generate(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        content = _detail(client, recording, "zh-Hant").content.decode()
        assert "No summary in this language yet" in content
        assert "Generate" in content
        assert 'value="zh-Hant"' in content  # language carried in the form

    def test_failed_variant_offers_retry(self, client):
        recording, transcript, section, _en, _zh = _multilingual_recording()
        SummaryVariantState.objects.create(
            transcript=transcript, section=section,
            output_language="zh-Hant", status="failed",
        )
        content = _detail(client, recording, "zh-Hant").content.decode()
        assert "failed" in content
        assert "Retry" in content
        assert 'value="zh-Hant"' in content

    def test_current_variant_offers_regenerate(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        content = _detail(client, recording, "en").content.decode()
        assert "Regenerate" in content
        assert 'value="en"' in content

    def test_unresolved_original_shows_informative_state(self, client):
        recording, transcript, _s, _en, _zh = _multilingual_recording()
        transcript.language_observed = ""
        transcript.save(update_fields=["language_observed"])
        response = _detail(client, recording, "original")
        assert response.status_code == 200
        content = response.content.decode()
        assert "original language" in content
        assert "Generate" in content

    def test_confirmation_preserves_language(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"mode": "first", "language": "zh-Hant", "fingerprint": state_fingerprint(recording)},
        )
        assert response.status_code == 200  # confirmation interstitial
        content = response.content.decode()
        assert 'name="language" value="zh-Hant"' in content
        assert "Traditional Chinese" in content or "zh-Hant" in content

    def test_confirmed_generation_preserves_variant_isolation(
        self, client, monkeypatch
    ):
        """Generating zh-Hant must not replace the English summary."""
        recording, transcript, section, en, _zh = _multilingual_recording()

        def fake_summarize(config, rec, *, target_language="default", **kw):
            make_summary_version(
                rec, transcript, section,
                title="中文標題：評分計劃", output_language="zh-Hant",
            )
            SummaryVariantState.objects.get_or_create(
                transcript=transcript, section=section,
                output_language="zh-Hant",
                defaults={"status": "current"},
            )
            return {"recording_id": rec.pk, "result": "summarized",
                    "output_language": target_language}

        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one", fake_summarize
        )
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "confirmed": "1", "mode": "first", "language": "zh-Hant",
                "fingerprint": state_fingerprint(recording),
            },
        )
        assert response.status_code == 302
        en.refresh_from_db()
        assert en.is_active is True
        active = SummaryVariantState.objects.get(
            transcript=transcript, section=section, output_language="en"
        )
        assert active.status == "current"
        # Both variants now exist side by side.
        assert SummaryVariantState.objects.filter(
            transcript=transcript, section=section
        ).count() == 2

    def test_redirect_preserves_language(self, client, monkeypatch):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one",
            lambda config, rec, **kw: {"recording_id": rec.pk, "result": "summarized",
                                       "output_language": kw.get("target_language")},
        )
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "confirmed": "1", "mode": "first", "language": "zh-Hant",
                "fingerprint": state_fingerprint(recording),
            },
        )
        assert response.status_code == 302
        assert "language=zh-Hant" in response["Location"]

    def test_unsupported_generation_language_rejected(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"confirmed": "1", "mode": "first", "language": "sv",
             "fingerprint": state_fingerprint(recording)},
        )
        assert response.status_code == 400
        assert "not a valid generation target" in response.content.decode()

    def test_stale_fingerprint_is_safe_noop(self, client, monkeypatch):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        fingerprint = state_fingerprint(recording)
        # State moves on after the form was rendered.
        Recording.objects.filter(pk=recording.pk).update(resummarization_failed=True)
        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("stale form must not execute")
            ),
        )
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"confirmed": "1", "mode": "first", "language": "zh-Hant",
             "fingerprint": fingerprint},
        )
        assert response.status_code == 302  # safe no-op redirect

    def test_lock_contention_renders_409(self, client):
        """Contention on the SAME lock the view uses (the app config's
        lock file, via views.helpers.get_config)."""
        from workflow.services.pipeline_lock import pipeline_lock
        from workflow.views.helpers import get_config

        recording, _t, _s, _en, _zh = _multilingual_recording()
        config = get_config()
        with pipeline_lock(config):
            response = client.post(
                f"/recordings/{recording.pk}/summarize/",
                {"confirmed": "1", "mode": "first", "language": "zh-Hant",
                 "fingerprint": state_fingerprint(recording)},
            )
        assert response.status_code == 409


class TestReadSelectorVersusGenerationSelector:
    """Concrete existing variants are READ selectors; the generation
    selector is derived (Finnish tab regenerates via `original`) or the
    tab is read-only. The four-selector generation allowlist is intact."""

    def test_finnish_tab_exposes_original_generation_selector(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        fi = _make_finnish_variant(recording)
        from workflow.services.variant_view import build_variant_view

        view = build_variant_view(recording, "fi")
        assert view.action_selector == "original"
        assert view.action_mode == "regenerate"
        option = next(o for o in view.options if o.selector == "fi")
        assert option.action_selector == "original"
        assert fi.title in _detail(client, recording, "fi").content.decode()

    def test_finnish_tab_action_post_is_accepted_not_400(self, client, monkeypatch):
        """The action exposed from a concrete Finnish tab POSTs the
        `original` GENERATION selector and is accepted."""
        recording, _t, _s, _en, _zh = _multilingual_recording()
        _make_finnish_variant(recording)
        captured = {}

        def fake_summarize(config, rec, *, target_language="default", **kw):
            captured["target_language"] = target_language
            return {"recording_id": rec.pk, "result": "summarized",
                    "output_language": "fi"}

        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one", fake_summarize
        )
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "confirmed": "1", "mode": "regenerate", "language": "original",
                "return_language": "fi", "return_view": "summary",
                "fingerprint": state_fingerprint(recording),
            },
        )
        assert response.status_code == 302  # accepted — not a 400 rejection
        assert captured["target_language"] == "original"
        # The user returns to the concrete Finnish read tab, on the page
        # the action originated from.
        assert "/summary/?language=fi" in response["Location"]

    def test_finnish_tab_confirmation_keeps_return_selector(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        _make_finnish_variant(recording)
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"mode": "regenerate", "language": "original", "return_language": "fi",
             "fingerprint": state_fingerprint(recording)},
        )
        assert response.status_code == 200  # confirmation interstitial
        content = response.content.decode()
        assert 'name="language" value="original"' in content
        assert 'name="return_language" value="fi"' in content

    def test_unrepresentable_concrete_tab_is_read_only(self, client):
        """A concrete variant that no approved generation selector
        produces (Swedish variant on a Finnish-source recording) stays
        readable/exportable but shows NO generation action."""
        recording, transcript, section, _en, _zh = _multilingual_recording()
        sv = make_summary_version(
            recording, transcript, section,
            title="Svensk titel", output_language="sv",
        )
        SummaryVariantState.objects.create(
            transcript=transcript, section=section, output_language="sv",
            status="current",
        )
        response = _detail(client, recording, "sv")
        assert response.status_code == 200
        content = response.content.decode()
        assert sv.title in content
        assert "action-summarize" not in content  # no action form at all
        # Still exportable.
        export = client.get(
            f"/recordings/{recording.pk}/summary/export/?format=markdown&language=sv"
        )
        assert export.status_code == 200
        assert "Svensk titel" in export.content.decode()

    def test_return_language_is_validated_before_redirect(self, client, monkeypatch):
        """An arbitrary return_language must not become query injection;
        unknown selectors fall back to the generation selector."""
        recording, _t, _s, _en, _zh = _multilingual_recording()
        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one",
            lambda config, rec, **kw: {"recording_id": rec.pk, "result": "summarized",
                                       "output_language": "en"},
        )
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "confirmed": "1", "mode": "first", "language": "en",
                "return_language": "drop%20table--",  # invalid selector
                "fingerprint": state_fingerprint(recording),
            },
        )
        assert response.status_code == 302
        assert "drop%20table" not in response["Location"]
        assert response["Location"].endswith("language=en")


class TestSummaryPageActions:
    """The standalone summary page mirrors the detail page's per-variant
    Generate/Retry/Regenerate actions for every state."""

    def test_current_variant_offers_regenerate(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording(with_zh=True)
        content = _summary_page(client, recording, "zh-Hant").content.decode()
        assert "Regenerate" in content
        assert 'value="zh-Hant"' in content
        assert 'value="regenerate"' in content

    def test_missing_variant_offers_generate(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        content = _summary_page(client, recording, "zh-Hant").content.decode()
        assert "No summary in this language yet" in content
        assert "Generate" in content
        assert 'value="zh-Hant"' in content
        assert 'value="first"' in content

    def test_failed_variant_offers_retry(self, client):
        recording, transcript, section, _en, _zh = _multilingual_recording()
        SummaryVariantState.objects.create(
            transcript=transcript, section=section,
            output_language="zh-Hant", status="failed",
        )
        content = _summary_page(client, recording, "zh-Hant").content.decode()
        assert "failed" in content
        assert "Retry" in content
        assert 'value="retry_summary"' in content

    def test_regeneration_failed_retains_summary_and_offers_regenerate(self, client):
        recording, transcript, section, _en, _zh = _multilingual_recording(with_zh=True)
        vs = SummaryVariantState.objects.get(
            transcript=transcript, section=section, output_language="zh-Hant"
        )
        vs.regeneration_failed = True
        vs.save(update_fields=["regeneration_failed"])
        response = _summary_page(client, recording, "zh-Hant")
        content = response.content.decode()
        # Current summary retained and clearly flagged.
        assert "regeneration failed" in content
        assert "中文標題：評分計劃" in content
        assert "Regenerate" in content
        assert 'value="regenerate"' in content

    def test_unresolved_original_offers_generate(self, client):
        recording, transcript, _s, _en, _zh = _multilingual_recording()
        transcript.language_observed = ""
        transcript.save(update_fields=["language_observed"])
        content = _summary_page(client, recording, "original").content.decode()
        assert "original language" in content
        assert "Generate" in content
        assert 'value="original"' in content

    def test_read_only_tab_offers_no_action_on_summary_page(self, client):
        recording, transcript, section, _en, _zh = _multilingual_recording()
        make_summary_version(
            recording, transcript, section, title="Svensk titel", output_language="sv"
        )
        SummaryVariantState.objects.create(
            transcript=transcript, section=section, output_language="sv",
            status="current",
        )
        content = _summary_page(client, recording, "sv").content.decode()
        assert "Svensk titel" in content
        assert "action-summarize" not in content

    def test_summary_page_confirmed_action_executes_and_preserves_tab(
        self, client, monkeypatch
    ):
        """A confirmed POST from the summary page runs with the hidden
        generation selector and returns to the selected read tab."""
        recording, _t, _s, _en, _zh = _multilingual_recording()
        captured = {}

        def fake_summarize(config, rec, *, target_language="default", **kw):
            captured["target_language"] = target_language
            return {"recording_id": rec.pk, "result": "summarized",
                    "output_language": "zh-Hant"}

        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one", fake_summarize
        )
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "confirmed": "1", "mode": "first", "language": "zh-Hant",
                "return_language": "zh-Hant",
                "fingerprint": state_fingerprint(recording),
            },
        )
        assert response.status_code == 302
        assert captured["target_language"] == "zh-Hant"
        assert "language=zh-Hant" in response["Location"]


class TestReturnView:
    """Actions return to the page they originated from via a
    server-owned allowlist token (detail | summary) — never an
    arbitrary client-supplied URL."""

    def test_summary_page_form_carries_return_view(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording(with_zh=True)
        content = _summary_page(client, recording, "zh-Hant").content.decode()
        assert 'name="return_view" value="summary"' in content

    def test_detail_page_form_carries_detail_token(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording(with_zh=True)
        content = _detail(client, recording, "zh-Hant").content.decode()
        assert 'name="return_view" value="detail"' in content

    def test_confirmed_summary_action_returns_to_summary_page(
        self, client, monkeypatch
    ):
        recording, _t, _s, _en, _zh = _multilingual_recording(with_zh=True)
        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one",
            lambda config, rec, **kw: {"recording_id": rec.pk,
                                       "result": "summarized",
                                       "output_language": "zh-Hant"},
        )
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "confirmed": "1", "mode": "regenerate", "language": "zh-Hant",
                "return_language": "zh-Hant", "return_view": "summary",
                "fingerprint": state_fingerprint(recording),
            },
        )
        assert response.status_code == 302
        assert "/summary/?language=zh-Hant" in response["Location"]

    def test_confirmed_detail_action_returns_to_detail_page(
        self, client, monkeypatch
    ):
        recording, _t, _s, _en, _zh = _multilingual_recording(with_zh=True)
        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one",
            lambda config, rec, **kw: {"recording_id": rec.pk,
                                       "result": "summarized",
                                       "output_language": "zh-Hant"},
        )
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "confirmed": "1", "mode": "regenerate", "language": "zh-Hant",
                "return_language": "zh-Hant", "return_view": "detail",
                "fingerprint": state_fingerprint(recording),
            },
        )
        assert response.status_code == 302
        location = response["Location"]
        assert "/summary/" not in location
        assert "language=zh-Hant" in location

    def test_confirmation_interstitial_preserves_return_view(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording(with_zh=True)
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"mode": "regenerate", "language": "zh-Hant", "return_view": "summary",
             "return_language": "zh-Hant", "fingerprint": state_fingerprint(recording)},
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert 'name="return_view" value="summary"' in content
        assert 'name="language" value="zh-Hant"' in content

    def test_invalid_return_view_falls_back_to_detail(self, client, monkeypatch):
        recording, _t, _s, _en, _zh = _multilingual_recording(with_zh=True)
        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one",
            lambda config, rec, **kw: {"recording_id": rec.pk,
                                       "result": "summarized",
                                       "output_language": "zh-Hant"},
        )
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "confirmed": "1", "mode": "regenerate", "language": "zh-Hant",
                "return_view": "javascript:alert(1)",
                "fingerprint": state_fingerprint(recording),
            },
        )
        assert response.status_code == 302
        location = response["Location"]
        assert location.startswith("/recordings/")
        assert "javascript" not in location
        assert "/summary/" not in location

    def test_forged_return_view_value_is_ignored(self, client, monkeypatch):
        recording, _t, _s, _en, _zh = _multilingual_recording(with_zh=True)
        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one",
            lambda config, rec, **kw: {"recording_id": rec.pk,
                                       "result": "summarized",
                                       "output_language": "zh-Hant"},
        )
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "confirmed": "1", "mode": "regenerate", "language": "zh-Hant",
                "return_view": "http://evil.example/steal",
                "fingerprint": state_fingerprint(recording),
            },
        )
        assert response.status_code == 302
        assert "evil.example" not in response["Location"]


def _make_finnish_variant(recording):
    transcript = recording.transcripts.filter(is_active=True).first()
    section = transcript.sections.get(ordinal=0)
    summary = make_summary_version(
        recording, transcript, section,
        title="Suomenkielinen otsikko", output_language="fi",
    )
    SummaryVariantState.objects.create(
        transcript=transcript, section=section, output_language="fi", status="current",
    )
    return summary


class TestFingerprintBindsLanguageResolution:
    """A source-language correction can change the resolved default/
    Original generation target WITHOUT creating a ProcessingAttempt or
    changing the action mode. The confirmation fingerprint must bind
    the language-resolution inputs, not just the mode."""

    def _fi_recording(self, sha="fp-1"):
        recording, transcript, section = make_transcribed_recording(
            ["hello world"], sha=sha
        )
        transcript.language_observed = "fi"
        transcript.save(update_fields=["language_observed"])
        return recording, transcript, section

    def _set_source(self, transcript, code):
        transcript.language_observed = code
        transcript.save(update_fields=["language_observed"])

    def _post_confirmed(self, client, recording, fingerprint):
        return client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"confirmed": "1", "mode": "first", "language": "original",
             "fingerprint": fingerprint},
            follow=True,
        )

    def test_fi_to_sv_missing_same_mode_rejects_old_fingerprint(
        self, client, monkeypatch
    ):
        """Both variants missing, both modes `first`, no new attempt:
        the stale confirmation must still be rejected with the
        state-changed behaviour and ZERO summarization calls."""
        recording, transcript, _section = self._fi_recording()
        # Both concrete variants pre-exist as MISSING so the current
        # variant-language set is unchanged by the correction.
        SummaryVariantState.objects.create(
            transcript=transcript, section=transcript.sections.get(ordinal=0),
            output_language="fi", status="missing",
        )
        SummaryVariantState.objects.create(
            transcript=transcript, section=transcript.sections.get(ordinal=0),
            output_language="sv", status="missing",
        )
        old_fingerprint = state_fingerprint(recording)

        def forbidden(*args, **kwargs):
            raise AssertionError("stale confirmation must not summarize")

        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one", forbidden
        )
        self._set_source(transcript, "sv")

        response = self._post_confirmed(client, recording, old_fingerprint)

        assert response.status_code == 200  # followed redirect
        assert "changed since the form was opened" in response.content.decode()

    def test_fi_to_sv_current_same_mode_rejects_old_fingerprint(
        self, client, monkeypatch
    ):
        """Both variants current, both modes `regenerate`, variant-language
        set unchanged: the stale confirmation is still rejected."""
        recording, transcript, section = self._fi_recording()
        for language in ("en", "fi", "sv"):
            make_summary_version(recording, transcript, section, output_language=language)
            SummaryVariantState.objects.create(
                transcript=transcript, section=section,
                output_language=language, status="current",
            )
        old_fingerprint = state_fingerprint(recording)

        def forbidden(*args, **kwargs):
            raise AssertionError("stale confirmation must not summarize")

        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one", forbidden
        )
        self._set_source(transcript, "sv")

        response = self._post_confirmed(client, recording, old_fingerprint)

        assert "changed since the form was opened" in response.content.decode()

    def test_fresh_fingerprint_after_correction_is_accepted(self, client, monkeypatch):
        """Positive control: a confirmation rendered AFTER the correction
        runs and targets the newly resolved language."""
        recording, transcript, _section = self._fi_recording()
        captured = {}

        def fake_summarize(config, rec, *, target_language="default", **kw):
            captured["target_language"] = target_language
            return {"recording_id": rec.pk, "result": "summarized",
                    "output_language": "sv"}

        monkeypatch.setattr(
            "workflow.services.summarize.summarize_one", fake_summarize
        )
        self._set_source(transcript, "sv")
        fresh = state_fingerprint(recording)

        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {"confirmed": "1", "mode": "first", "language": "original",
             "fingerprint": fresh},
        )
        assert response.status_code == 302
        assert captured["target_language"] == "original"

    def test_unrelated_get_does_not_change_fingerprint(self, client):
        recording, _t, _s = self._fi_recording()
        before = state_fingerprint(recording)
        assert _detail(client, recording).status_code == 200
        assert _summary_page(client, recording).status_code == 200
        assert state_fingerprint(recording) == before

    def test_fingerprint_computation_is_read_only(self, recording_fixture=None):
        """Fingerprinting performs SELECT queries only — no writes, no
        external calls (the module-level forbid_external_effects guard
        is active)."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        recording, _t, _s = self._fi_recording()
        with CaptureQueriesContext(connection) as ctx:
            state_fingerprint(recording)
        non_select = [
            q for q in ctx.captured_queries
            if not q["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert non_select == []

    def test_unresolved_original_is_explicitly_fingerprinted(self):
        recording, transcript, _s = self._fi_recording()
        self._set_source(transcript, "")
        fingerprint = json.loads(state_fingerprint(recording))
        assert fingerprint["language_state"]["original_output"] == ""
        assert fingerprint["language_state"]["default_output"] == "en"


class TestGetPurityAndExports:
    def test_unresolved_original_get_is_read_only(self, client):
        """Zero network/subprocess (forbid_external_effects), zero writes:
        only SELECT queries, no state changes."""
        recording, transcript, _s, _en, _zh = _multilingual_recording()
        transcript.language_observed = ""
        transcript.save(update_fields=["language_observed"])

        with CaptureQueriesContext(connection) as ctx:
            response = _detail(client, recording, "original")
        assert response.status_code == 200
        non_select = [
            q for q in ctx.captured_queries
            if not q["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert non_select == []
        recording.refresh_from_db()
        transcript.refresh_from_db()
        assert transcript.language_observed == ""  # no detection on GET
        assert not SummaryVariantState.objects.filter(
            output_language="zh-Hant"
        ).exists()

    def test_export_links_preserve_language(self, client):
        recording, _t, _s, en, zh = _multilingual_recording(with_zh=True)
        for language, expected in (("en", en.title), ("zh-Hant", zh.title)):
            response = client.get(
                f"/recordings/{recording.pk}/summary/export/?format=markdown&language={language}"
            )
            assert response.status_code == 200
            assert expected.encode() in response.content

    def test_export_missing_variant_is_404_not_fallback(self, client):
        """No summary for the requested variant → 404, never a silent
        fallback to the default-language summary."""
        recording, _t, _s, _en, _zh = _multilingual_recording()
        response = client.get(
            f"/recordings/{recording.pk}/summary/export/?format=markdown&language=zh-Hant"
        )
        assert response.status_code == 404

    def test_export_unknown_language_is_404(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        response = client.get(
            f"/recordings/{recording.pk}/summary/export/?format=markdown&language=sv"
        )
        assert response.status_code == 404

    def test_export_unresolved_original_is_404(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording()
        response = client.get(
            f"/recordings/{recording.pk}/summary/export/?format=markdown&language=original"
        )
        assert response.status_code == 404

    def test_copy_link_href_preserves_selected_language(self, client):
        recording, _t, _s, _en, _zh = _multilingual_recording(with_zh=True)
        content = _detail(client, recording, "zh-Hant").content.decode()
        assert "language=zh-Hant" in content
        assert "Copy Markdown" in content
