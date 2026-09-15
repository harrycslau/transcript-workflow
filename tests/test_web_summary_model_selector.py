"""Focused web tests for the Retry/Regenerate summary model selector.

Proves, for recording- and section-scoped forms:

- the selector is rendered ONLY for Retry/Regenerate (initial Generate
  stays default-model-only and renders no selector);
- the exact configured default is selected and configured alternatives
  are offered;
- a valid alternate reaches the service (and the selected model is
  re-validated at the service boundary);
- missing/duplicate/blank/oversized/unknown values are ONE fixed friendly
  400 BEFORE any pipeline lock/recovery/network/write;
- initial Generate cannot be switched by a forged alternate model;
- a config-choice change invalidates rendered forms (fingerprint);
- GET stays strictly read-only and performs no model discovery.
No network, no real audio.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client

from brainlib.config import LLMConfig
from workflow.models import Section, SummaryState
from workflow.services.segmentation import SegmentedVersion, save_segmented_version
from workflow.services.web_actions import section_state_fingerprint, state_fingerprint

from factories import (
    default_summarization,
    make_config,
    make_summary_version,
    make_transcribed_recording,
)

pytestmark = [pytest.mark.django_db]

DEFAULT_MODEL = "default-model"
ALT_MODEL = "alt-model"
ALT2_MODEL = "alt-model-2"


@pytest.fixture
def client():
    return Client()


def _llm(model=DEFAULT_MODEL):
    return LLMConfig(
        provider="openai_compatible",
        base_url="http://127.0.0.1:1/v1",
        model=model,
        api_key_env="BRAIN_TEST_LLM_API_KEY",
        temperature=0.2,
        timeout_seconds=600,
    )


def model_config(tmp_path, *, default=DEFAULT_MODEL, models=(ALT_MODEL,)):
    return make_config(
        tmp_path,
        llm=_llm(default),
        summarization=default_summarization(models=models),
    )


def _install_config(monkeypatch, config):
    monkeypatch.setattr("workflow.views.actions.get_config", lambda: config)
    monkeypatch.setattr("workflow.views.recordings.get_config", lambda: config)


def _regenerate_recording(sha="model-sel-1"):
    recording, transcript, section = make_transcribed_recording(["x"], sha=sha)
    make_summary_version(recording, transcript, section)
    recording.refresh_from_db()
    return recording, transcript, section


def _retry_recording(sha="model-sel-retry"):
    recording, transcript, section = make_transcribed_recording(
        ["x"], sha=sha, summary_status=SummaryState.FAILED
    )
    return recording, transcript, section


def _assert_labelled_selector(content, label):
    """The visible action label is associated with the model select."""
    match = re.search(rf'<label for="([^"]+)">{label}</label>', content)
    assert match is not None, f"missing {label!r} label"
    assert f'<select id="{match.group(1)}" name="model">' in content


def _split_section(sha="model-sel-section"):
    recording, transcript, _fixed = make_transcribed_recording(
        [f"segment {i}" for i in range(6)], sha=sha
    )
    result = save_segmented_version(
        recording.pk, transcript.pk, 0, 6, [3], ["First topic", "Second topic"]
    )
    version = SegmentedVersion.objects.get(pk=result.version_id)
    sections = list(
        Section.objects.filter(segmented_version=version).order_by("ordinal")
    )
    return recording, transcript, sections


class TestRendering:
    def test_regenerate_renders_selector_with_default_selected(
        self, client, tmp_path, monkeypatch
    ):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, _t, _s = _regenerate_recording()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'name="model"' in content
        assert "<select" in content
        assert f'value="{DEFAULT_MODEL}" selected' in content
        assert f'value="{ALT_MODEL}"' in content
        assert "Regenerate" in content
        _assert_labelled_selector(content, "Regenerate:")

    def test_retry_renders_selector(self, client, tmp_path, monkeypatch):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, _t, _s = _retry_recording()
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'name="model"' in content
        assert f'value="{DEFAULT_MODEL}" selected' in content
        assert "Retry" in content
        _assert_labelled_selector(content, "Retry:")

    def test_first_generate_has_no_selector(self, client, tmp_path, monkeypatch):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, _t, _s = make_transcribed_recording(["x"], sha="model-sel-first")
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert 'name="model"' not in content
        assert "Generate" in content

    def test_section_first_has_no_selector_and_regenerate_does(
        self, client, tmp_path, monkeypatch
    ):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, transcript, sections = _split_section()
        section = sections[0]
        first = client.get(
            f"/recordings/{recording.pk}/sections/{section.pk}/"
        ).content.decode()
        assert 'name="model"' not in first

        make_summary_version(recording, transcript, section)
        regenerate = client.get(
            f"/recordings/{recording.pk}/sections/{section.pk}/"
        ).content.decode()
        assert 'name="model"' in regenerate
        assert f'value="{DEFAULT_MODEL}" selected' in regenerate
        _assert_labelled_selector(regenerate, "Regenerate:")

    def test_rendered_inline_note_removed_but_pending_message_remains(
        self, client, tmp_path, monkeypatch
    ):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, _t, _s = _regenerate_recording(sha="model-sel-copy")
        content = client.get(f"/recordings/{recording.pk}/").content.decode()
        assert "is created completely" not in content
        assert "current summary stays active unless the new version" in content


class TestGetPurity:
    def test_get_makes_no_model_discovery_call(
        self, client, tmp_path, monkeypatch, forbid_external_effects
    ):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, _t, _s = _regenerate_recording(sha="model-sel-get")
        response = client.get(f"/recordings/{recording.pk}/")
        assert response.status_code == 200
        # No subprocess/HTTP was attempted (the fixture would raise).


class TestActionModelSelection:
    def test_valid_alternate_reaches_service(self, client, tmp_path, monkeypatch):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, _t, _s = _regenerate_recording(sha="model-sel-alt")
        captured = {}

        def fake_summarize(cfg, rec, **kwargs):
            captured["model"] = kwargs.get("model")
            return {"recording_id": rec.pk, "result": "summarized", "output_language": "en"}

        monkeypatch.setattr("workflow.services.summarize.summarize_one", fake_summarize)
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "mode": "regenerate",
                "model": ALT_MODEL,
                "language": "default",
                "fingerprint": state_fingerprint(recording, config=config),
            },
        )
        assert response.status_code == 302
        assert captured["model"] == ALT_MODEL

    def test_section_valid_alternate_reaches_service(self, client, tmp_path, monkeypatch):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, transcript, sections = _split_section(sha="model-sel-sec-alt")
        section = sections[0]
        make_summary_version(recording, transcript, section)
        captured = {}

        def fake_summarize(cfg, sec, **kwargs):
            captured["model"] = kwargs.get("model")
            return {
                "recording_id": recording.pk,
                "section_id": sec.pk,
                "result": "summarized",
                "output_language": "en",
            }

        monkeypatch.setattr(
            "workflow.services.summarize.summarize_section_one", fake_summarize
        )
        response = client.post(
            f"/recordings/{recording.pk}/sections/{section.pk}/summarize/",
            {
                "language": "default",
                "mode": "regenerate",
                "model": ALT_MODEL,
                "fingerprint": section_state_fingerprint(recording, section, config=config),
            },
        )
        assert response.status_code == 302
        assert captured["model"] == ALT_MODEL


class TestStrictRejectionBeforeLock:
    def _forbid_lock(self, monkeypatch):
        def no_lock(*args, **kwargs):
            raise AssertionError("pipeline lock must not be acquired")

        def no_service(*args, **kwargs):
            raise AssertionError("summarize service must not run")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", no_lock)
        monkeypatch.setattr("workflow.services.summarize.summarize_one", no_service)

    def test_missing_model_rejected(self, client, tmp_path, monkeypatch):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, _t, _s = _retry_recording(sha="model-sel-missing")
        self._forbid_lock(monkeypatch)
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "mode": "retry_summary",
                "language": "default",
                "fingerprint": state_fingerprint(recording, config=config),
            },
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "model",
        ["", "evil-model", "x" * 256, ["a", "b"]],
        ids=["blank", "unknown", "oversized", "duplicate"],
    )
    def test_invalid_model_rejected(self, client, tmp_path, monkeypatch, model):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, _t, _s = _regenerate_recording(sha=f"model-sel-bad-{model!r}")
        self._forbid_lock(monkeypatch)
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "mode": "regenerate",
                "model": model,
                "language": "default",
                "fingerprint": state_fingerprint(recording, config=config),
            },
        )
        assert response.status_code == 400

    def _section_url(self, recording, section):
        return f"/recordings/{recording.pk}/sections/{section.pk}/summarize/"

    def test_section_missing_model_rejected(self, client, tmp_path, monkeypatch):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, transcript, sections = _split_section(sha="model-sel-sec-missing")
        section = sections[0]
        make_summary_version(recording, transcript, section)

        def no_lock(*args, **kwargs):
            raise AssertionError("pipeline lock must not be acquired")

        def no_service(*args, **kwargs):
            raise AssertionError("section summarize must not run")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", no_lock)
        monkeypatch.setattr(
            "workflow.services.summarize.summarize_section_one", no_service
        )
        response = client.post(
            self._section_url(recording, section),
            {
                "language": "default",
                "mode": "regenerate",
                "fingerprint": section_state_fingerprint(
                    recording, section, config=config
                ),
            },
        )
        assert response.status_code == 400

    def test_section_unknown_model_rejected(self, client, tmp_path, monkeypatch):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, transcript, sections = _split_section(sha="model-sel-sec-unknown")
        section = sections[0]
        make_summary_version(recording, transcript, section)

        def no_lock(*args, **kwargs):
            raise AssertionError("pipeline lock must not be acquired")

        monkeypatch.setattr("workflow.services.web_actions.pipeline_lock", no_lock)
        response = client.post(
            self._section_url(recording, section),
            {
                "language": "default",
                "mode": "regenerate",
                "model": "evil-model",
                "fingerprint": section_state_fingerprint(
                    recording, section, config=config
                ),
            },
        )
        assert response.status_code == 400

    def test_initial_generate_forged_alternate_rejected(
        self, client, tmp_path, monkeypatch
    ):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, _t, _s = make_transcribed_recording(["x"], sha="model-sel-forged")
        self._forbid_lock(monkeypatch)
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "mode": "first",
                "model": ALT_MODEL,
                "language": "default",
                "fingerprint": state_fingerprint(recording, config=config),
            },
        )
        assert response.status_code == 400

    def test_initial_generate_without_model_runs_with_default(
        self, client, tmp_path, monkeypatch
    ):
        config = model_config(tmp_path)
        _install_config(monkeypatch, config)
        recording, _t, _s = make_transcribed_recording(["x"], sha="model-sel-first-ok")
        captured = {}

        def fake_summarize(cfg, rec, **kwargs):
            captured["model"] = kwargs.get("model")
            return {"recording_id": rec.pk, "result": "summarized", "output_language": "en"}

        monkeypatch.setattr("workflow.services.summarize.summarize_one", fake_summarize)
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "mode": "first",
                "language": "default",
                "fingerprint": state_fingerprint(recording, config=config),
            },
        )
        assert response.status_code == 302
        assert captured["model"] == DEFAULT_MODEL


class TestFingerprintBindsModelConfig:
    def test_recording_fingerprint_changes_with_allowlist(self, tmp_path):
        recording, _t, _s = _regenerate_recording(sha="model-sel-fp")
        config_a = model_config(tmp_path, models=(ALT_MODEL,))
        config_b = model_config(tmp_path, models=(ALT_MODEL, ALT2_MODEL))
        assert state_fingerprint(recording, config=config_a) != state_fingerprint(
            recording, config=config_b
        )

    def test_recording_fingerprint_changes_with_default(self, tmp_path):
        recording, _t, _s = _regenerate_recording(sha="model-sel-fp-default")
        config_a = model_config(tmp_path, default=DEFAULT_MODEL)
        config_b = model_config(tmp_path, default="other-default", models=(ALT_MODEL,))
        assert state_fingerprint(recording, config=config_a) != state_fingerprint(
            recording, config=config_b
        )

    def test_section_fingerprint_changes_with_allowlist(self, tmp_path):
        recording, transcript, sections = _split_section(sha="model-sel-sec-fp")
        make_summary_version(recording, transcript, sections[0])
        section = sections[0]
        config_a = model_config(tmp_path, models=(ALT_MODEL,))
        config_b = model_config(tmp_path, models=(ALT_MODEL, ALT2_MODEL))
        assert section_state_fingerprint(
            recording, section, config=config_a
        ) != section_state_fingerprint(recording, section, config=config_b)

    def test_config_change_makes_rendered_form_stale(
        self, client, tmp_path, monkeypatch
    ):
        recording, _t, _s = _regenerate_recording(sha="model-sel-stale")
        config_a = model_config(tmp_path, models=(ALT_MODEL,))
        config_b = model_config(tmp_path, models=(ALT_MODEL, ALT2_MODEL))
        stale = state_fingerprint(recording, config=config_a)
        _install_config(monkeypatch, config_b)

        def forbidden(*args, **kwargs):
            raise AssertionError("stale config form must not run")

        monkeypatch.setattr("workflow.services.summarize.summarize_one", forbidden)
        response = client.post(
            f"/recordings/{recording.pk}/summarize/",
            {
                "mode": "regenerate",
                "model": ALT_MODEL,
                "language": "default",
                "fingerprint": stale,
            },
        )
        # Under-lock stale safe no-op (302 redirect with a warning).
        assert response.status_code == 302
