"""Tests for Step 3 CLI commands: summarize, summaries, summary, tags."""

from __future__ import annotations

import json

import pytest

from brainlib import cli
from workflow.models import Summary, SummaryState, Tag

from factories import (
    final_summary_json,
    make_transcribed_recording,
    map_summary_json,
    omlx_envelope,
    write_cli_config,
)

pytestmark = pytest.mark.django_db


def omlx_transport(responses_by_call):
    """MockTransport serving a scripted sequence of envelopes."""
    import httpx

    queue = list(responses_by_call)

    def handler(request: httpx.Request) -> httpx.Response:
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return httpx.Response(200, content=json.dumps(omlx_envelope(response)).encode())

    return httpx.MockTransport(handler)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from brainlib.config import LLMConfig

    write_cli_config(
        tmp_path, monkeypatch,
        llm=LLMConfig(
            provider="openai_compatible", base_url="http://127.0.0.1:1/v1", model="test-model",
            api_key_env="BRAIN_TEST_LLM_API_KEY", temperature=0.2, timeout_seconds=600,
        ),
    )
    return tmp_path


class TestSummarizeCommand:
    def test_summarize_batch_json(self, cli_env, capsys, monkeypatch):
        recording, _, _ = make_transcribed_recording(["hello world"])
        from workflow.services import llm as llm_service

        real_client = llm_service.chat_completion
        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion",
            lambda config, **kwargs: real_client(
                config, **{**kwargs, "transport": omlx_transport([final_summary_json()])}
            ),
        )
        assert cli.main(["summarize", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["results"][0]["result"] == "summarized"
        assert recording.current_summary() is not None

    def test_summarize_single_explicit(self, cli_env, capsys, monkeypatch):
        recording, _, _ = make_transcribed_recording(["hello world"])
        from workflow.services import llm as llm_service

        real_client = llm_service.chat_completion
        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion",
            lambda config, **kwargs: real_client(
                config, **{**kwargs, "transport": omlx_transport([final_summary_json()])}
            ),
        )
        assert cli.main(["summarize", str(recording.pk), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "summarized"

    def test_summarize_regenerate_requires_id(self, cli_env, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["summarize", "--regenerate"])
        assert excinfo.value.code == 2

    def test_summarize_unknown_recording_exits_one(self, cli_env, capsys):
        assert cli.main(["summarize", "nope", "--json"]) == 1
        assert "recording not found" in capsys.readouterr().err

    def test_run_includes_summarization_stage(self, cli_env, capsys, monkeypatch):
        recording, _, _ = make_transcribed_recording(["hello world"])
        from workflow.services import llm as llm_service

        real_client = llm_service.chat_completion
        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion",
            lambda config, **kwargs: real_client(
                config, **{**kwargs, "transport": omlx_transport([final_summary_json()])}
            ),
        )
        assert cli.main(["run", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert "summarization" in payload
        assert payload["summarization"]["results"][0]["result"] == "summarized"
        # Idempotent: a second run does not regenerate.
        assert cli.main(["run", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["summarization"]["results"] == []
        assert Summary.objects.count() == 1


class TestSummaryOutput:
    def _summarized(self, cli_env, capsys, monkeypatch, **overrides):
        recording, _, _ = make_transcribed_recording(["hello world"])
        from workflow.services import llm as llm_service

        real_client = llm_service.chat_completion
        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion",
            lambda config, **kwargs: real_client(
                config,
                **{**kwargs, "transport": omlx_transport([final_summary_json(**overrides)])},
            ),
        )
        assert cli.main(["summarize", str(recording.pk)]) == 0
        capsys.readouterr()
        monkeypatch.undo()
        return recording

    def test_markdown_default_is_copy_friendly(self, cli_env, capsys, monkeypatch):
        recording = self._summarized(cli_env, capsys, monkeypatch)
        assert cli.main(["summary", str(recording.pk)]) == 0
        out = capsys.readouterr().out
        assert out.startswith("# Meeting about grading")
        assert "## Overview" in out
        assert "## Key points" in out
        assert "- Prepare rubric" in out
        assert "## Tags" in out
        assert "{" not in out and "}" not in out

    def test_text_format(self, cli_env, capsys, monkeypatch):
        recording = self._summarized(cli_env, capsys, monkeypatch)
        assert cli.main(["summary", str(recording.pk), "--format", "text"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("Title: Meeting about grading")
        assert "Action items:" in out

    def test_json_format(self, cli_env, capsys, monkeypatch):
        recording = self._summarized(cli_env, capsys, monkeypatch)
        assert cli.main(["summary", str(recording.pk), "--format", "json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["title"] == "Meeting about grading"
        assert payload["input_truncated"] is False

    def test_no_current_summary_exits_one(self, cli_env, capsys):
        recording, _, _ = make_transcribed_recording(["hello world"])
        assert cli.main(["summary", str(recording.pk)]) == 1
        assert "no current summary" in capsys.readouterr().err

    def test_summaries_lists_versions(self, cli_env, capsys, monkeypatch):
        from workflow.services import llm as llm_service
        from workflow.services.summarize import summarize_one

        from brainlib.config import LLMConfig, TagSpec, TagsConfig
        from factories import make_config

        recording, _, _ = make_transcribed_recording(["hello world"])
        config = make_config(
            cli_env,
            llm=LLMConfig(
                provider="openai_compatible", base_url="http://x/v1", model="m",
                api_key_env="BRAIN_TEST_LLM_API_KEY", temperature=0.2, timeout_seconds=600,
            ),
            tags=TagsConfig(allowed=(TagSpec(name="Academic", description="d"),)),
        )
        summarize_one(config, recording, llm_call=lambda **k: final_summary_json(title="V1"))
        summarize_one(
            config, recording, regenerate=True,
            llm_call=lambda **k: final_summary_json(title="V2"),
        )
        assert cli.main(["summaries", str(recording.pk), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert [v["title"] for v in payload["summaries"]] == ["V1", "V2"]
        assert [v["is_active"] for v in payload["summaries"]] == [False, True]
        assert [v["is_current"] for v in payload["summaries"]] == [False, True]


class TestTagsCommand:
    def test_tags_read_only_makes_no_writes(self, cli_env, capsys):
        # The session config's allowed tags are NOT synced by read-only tags.
        before = Tag.objects.count()
        assert cli.main(["tags", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["configured"] == 0
        assert Tag.objects.count() == before

    def test_tags_sync_creates_rows(self, cli_env, capsys):
        assert cli.main(["tags", "--sync", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["sync"]["created"] == len(payload["tags"])
        assert payload["configured"] == len(payload["tags"])

    def test_tags_reports_retired_and_assignments(self, cli_env, capsys, monkeypatch):
        from workflow.services.tags import sync_tags

        sync_tags(write_cli_config(cli_env, monkeypatch))
        recording, _, _ = make_transcribed_recording(["hello world"])
        from brainlib.config import LLMConfig, TagSpec, TagsConfig
        from factories import make_config
        from workflow.services.summarize import summarize_one

        config = make_config(
            cli_env,
            llm=LLMConfig(
                provider="openai_compatible", base_url="http://x/v1", model="m",
                api_key_env="BRAIN_TEST_LLM_API_KEY", temperature=0.2, timeout_seconds=600,
            ),
            tags=TagsConfig(allowed=(TagSpec(name="Academic", description="d"),)),
        )
        summarize_one(config, recording, llm_call=lambda **k: final_summary_json())
        # Remove Academic from config; sync retires it.
        from brainlib.config import TagSpec as TS

        write_cli_config(
            cli_env, monkeypatch,
            tags=TagsConfig(allowed=(TS(name="Family", description="f"), TS(name="Unknown", description="u"))),
        )
        assert cli.main(["tags", "--sync", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        academic = next(t for t in payload["tags"] if t["name"] == "Academic")
        assert academic["is_configured"] is False
        assert academic["active_assignments"] == 1
        assert payload["retired"] >= 1


class TestStatusReviewEnrichment:
    def test_status_summary_counts(self, cli_env, capsys):
        recording, _, _ = make_transcribed_recording(["hello world"])
        recording2, _, _ = make_transcribed_recording(["other"])
        recording2.summary_status = SummaryState.FAILED
        recording2.save(update_fields=["summary_status"])
        recording.summary_status = SummaryState.CURRENT
        recording.resummarization_failed = True
        recording.save(update_fields=["summary_status", "resummarization_failed"])
        assert cli.main(["status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["awaiting_summary"] == 0
        assert payload["summary"]["summary_failed"] == 1
        assert payload["summary"]["failed_resummarization"] == 1
        assert payload["summary"]["summarized"] == 0

    def test_review_lists_summary_sections(self, cli_env, capsys):
        recording, _, _ = make_transcribed_recording(["hello world"])
        assert cli.main(["review", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert {"recording_id": recording.pk, "kind": "awaiting_summary"} in payload["awaiting_summary"]


class TestFreshProcessBehaviour:
    def test_summarize_missing_config_concise_error(self, monkeypatch, capsys):
        monkeypatch.setenv("BRAIN_CONFIG", "/nonexistent/brain.yaml")
        assert cli.main(["summarize", "--json"]) == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "Configuration file not found" in err
        assert "Traceback" not in err

    def test_summary_missing_config_concise_error(self, monkeypatch, capsys):
        monkeypatch.setenv("BRAIN_CONFIG", "/nonexistent/brain.yaml")
        assert cli.main(["summary", "some-id"]) == 1
        err = capsys.readouterr().err
        assert "Configuration file not found" in err
        assert "Traceback" not in err


class TestReviewErrorCodes:
    def test_review_reports_actual_error_code_and_attempt(self, cli_env, capsys, monkeypatch):
        import httpx

        from workflow.services import llm as llm_service

        recording, _, _ = make_transcribed_recording(["hello world"])
        real_client = llm_service.chat_completion
        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion",
            lambda config, **kwargs: real_client(
                config,
                **{**kwargs, "transport": httpx.MockTransport(lambda request: (_ for _ in ()).throw(
                    httpx.ConnectError("refused")))},
            ),
        )
        assert cli.main(["summarize", str(recording.pk), "--json"]) == 0
        capsys.readouterr()
        assert cli.main(["review", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        entry = next(e for e in payload["failed_summary"] if e["recording_id"] == recording.pk)
        assert entry["error_code"] == "endpoint_unavailable"
        assert entry["error_code"] != "unknown"

    def test_second_run_does_not_retry_failed_summary(self, cli_env, capsys, monkeypatch):
        import httpx

        from workflow.models import Summary
        from workflow.services import llm as llm_service

        recording, _, _ = make_transcribed_recording(["hello world"])
        real_client = llm_service.chat_completion
        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion",
            lambda config, **kwargs: real_client(
                config,
                **{**kwargs, "transport": httpx.MockTransport(lambda request: (_ for _ in ()).throw(
                    httpx.ConnectError("refused")))},
            ),
        )
        assert cli.main(["run", "--json"]) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["summarization"]["results"][0]["result"] == "failed"
        assert cli.main(["run", "--json"]) == 0
        second = json.loads(capsys.readouterr().out)
        assert second["summarization"]["results"] == []
        assert Summary.objects.count() == 0  # no new attempt rows either


class TestCLIErrorHygiene:
    """Failure output surfaces (CLI JSON, human output, stderr, logs)
    must never contain sensitive content; stable codes must be present
    so the assertions cannot pass vacuously."""

    KEY = "sk-SUPERSECRET-KEY-7f3a9"
    TRANSCRIPT = "TRANSCRIPT-SENTINEL-私人內容-1a2b"
    BODY = "RESPONSE-BODY-SENTINEL-42xy"

    def _recording(self):
        recording, _, _ = make_transcribed_recording([self.TRANSCRIPT])
        return recording

    def _failing_transport(self, status=500, body=b""):
        import httpx

        return httpx.MockTransport(lambda request: httpx.Response(status, content=body))

    def _run_cli(self, cli_env, capsys, caplog, monkeypatch, recording, **kwargs):
        from workflow.services import llm as llm_service

        real_client = llm_service.chat_completion
        transport = self._failing_transport(**kwargs)
        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion",
            lambda config, **kw: real_client(config, **{**kw, "transport": transport}),
        )
        with caplog.at_level("DEBUG"):
            code = cli.main(["summarize", str(recording.pk), "--json"])
        captured = capsys.readouterr()
        return code, captured.out, captured.err, caplog.text

    def test_http_failure_sentinels_absent_from_cli_and_logs(self, cli_env, capsys, caplog, monkeypatch):
        import json as jsonlib

        recording = self._recording()
        body = jsonlib.dumps({"error": f"leak {self.BODY} {self.KEY}"}).encode()
        code, out, err, log_text = self._run_cli(cli_env, capsys, caplog, monkeypatch, recording, body=body)
        assert code == 0
        payload = json.loads(out)
        assert payload["error_code"] == "http_error"
        surface = out + err + log_text
        assert surface and "http_error" in surface  # effective, not vacuous
        for sentinel in (self.BODY, self.KEY, self.TRANSCRIPT, "leak"):
            assert sentinel not in surface

    def test_unreachable_sentinels_absent_from_cli_and_logs(self, cli_env, capsys, caplog, monkeypatch):
        import httpx

        from workflow.services import llm as llm_service

        recording = self._recording()
        real_client = llm_service.chat_completion

        def handler(request):
            raise httpx.ConnectError(f"refused urltoken=SECRETVALUE123 key={self.KEY}")

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion",
            lambda config, **kw: real_client(config, **{**kw, "transport": transport}),
        )
        with caplog.at_level("DEBUG"):
            code = cli.main(["summarize", str(recording.pk), "--json"])
        captured = capsys.readouterr()
        surface = captured.out + captured.err + caplog.text
        assert "endpoint_unavailable" in surface
        for sentinel in ("SECRETVALUE123", self.KEY, self.TRANSCRIPT, "refused"):
            assert sentinel not in surface

    def test_status_and_review_surfaces_stay_sanitized(self, cli_env, capsys, caplog, monkeypatch):
        import httpx

        from workflow.services import llm as llm_service

        recording = self._recording()
        real_client = llm_service.chat_completion
        transport = httpx.MockTransport(
            lambda request: httpx.Response(500, content=json.dumps({"e": self.BODY}).encode())
        )
        monkeypatch.setattr(
            "workflow.services.summarize.llm_service.chat_completion",
            lambda config, **kw: real_client(config, **{**kw, "transport": transport}),
        )
        with caplog.at_level("DEBUG"):
            assert cli.main(["summarize", str(recording.pk), "--json"]) == 0
        capsys.readouterr()
        assert cli.main(["status", "--json"]) == 0
        status_out = capsys.readouterr().out
        assert cli.main(["review", "--json"]) == 0
        review_out = capsys.readouterr().out
        surface = status_out + review_out
        # The stable code and the recording id ARE surfaced (effective check).
        assert "http_error" in surface and recording.pk in surface
        for sentinel in (self.BODY, self.KEY, self.TRANSCRIPT):
            assert sentinel not in surface
