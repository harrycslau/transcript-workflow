"""Summarization pipeline: eligibility, map/reduce oMLX calls, persistence.

Flow per recording:

1. Eligibility: an active transcript exists and (unless the user
   explicitly requested regeneration) no current summary.
2. A durable ``ProcessingAttempt`` (stage ``summarization``) is created
   BEFORE any oMLX work, carrying a safe request fingerprint.
3. Pre-flight: the transcript is deterministically chunked
   (:mod:`workflow.services.chunking`); exceeding ``max_total_characters``
   or ``max_chunk_count`` finishes the attempt with ``input_too_large``
   (input size, computed chunk count and limits recorded) — zero HTTP
   calls and no Summary row.
4. Map/reduce: every request payload is serialized in full and checked
   against ``max_input_characters`` (scaffolding and JSON escaping
   included) before any HTTP call; oversized reduce inputs are handled
   by deterministic hierarchical reduction, failing cleanly before HTTP
   when even a single intermediate cannot fit. One logical call = size
   gate + HTTP + envelope validation + JSON parse + schema validation;
   invalid output retries that whole logical call exactly once.
5. Persistence goes exclusively through :func:`persist_summary`, which
   enforces the section/transcript/recording relationship invariants
   and the one-active-summary-per-scope constraint in one transaction.

State semantics: failed summarization never touches
``processing_status``. Every completed failure durably records
``last_failed_attempt`` on the recording. Without a current summary the
recording enters ``summary_status=failed`` (``brain run`` never
auto-retries it — there is one automatic attempt per active transcript,
interrupted attempts included); a failed regeneration keeps the current
summary and sets ``resummarization_failed``. No API key, prompt text,
or transcript content is ever stored on attempts or in error messages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from django.db import transaction
from django.utils import timezone

from brainlib.config import AppConfig, ConfigError, tag_name_key
from workflow.models import (
    AttemptOutcome,
    AttemptStage,
    GenerationMode,
    ProcessingAttempt,
    ProcessingStatus,
    Recording,
    Summary,
    SummaryState,
    SummaryTagSuggestion,
    Tag,
    TagAssignment,
    TagDeactivatedBy,
    TagOrigin,
    Transcript,
    Section,
)
from workflow.services import chunking
from workflow.services import llm as llm_service
from workflow.services import tags as tags_service
from workflow.services.chunking import ChunkPlan, InputTooLarge, build_chunks, check_chunk_limits
from workflow.services.transcription import next_ordinal, sanitize_error

logger = logging.getLogger(__name__)

PARSER_VERSION = "1"

FINAL_SHAPE_DOC = (
    '{"title": string, "overview": string, "key_points": [string], '
    '"action_items": [{"text": string, "owner": string|null, "due_date": string|null}], '
    '"people": [string], "organizations": [string], "topics": [string], '
    '"suggested_tags": [string], "language": string}'
)
MAP_SHAPE_DOC = '{"overview": string, "key_points": [string]}'

# Bounded item counts / string lengths of the validated schema.
MAX_KEY_POINTS = 30
MAX_KEY_POINT_CHARS = 1000
MAX_ACTION_ITEMS = 30
MAX_ACTION_TEXT_CHARS = 1000
MAX_ACTION_OWNER_CHARS = 200
MAX_ACTION_DUE_CHARS = 32
MAX_NAME_ITEMS = 50
MAX_NAME_CHARS = 200
MAX_TITLE_CHARS = 200
MAX_OVERVIEW_CHARS = 8000
MAX_LANGUAGE_CHARS = 32
MAX_MAP_OVERVIEW_CHARS = 4000
MAX_MAP_KEY_POINTS = 15
MAX_MAP_KEY_POINT_CHARS = 500
MAX_SUGGESTED_TAGS = 50
MAX_TAG_NAME_CHARS = 64

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _tag_lines(tags: list[Tag]) -> str:
    if not tags:
        return "(no tags configured; return an empty suggested_tags list)"
    return "\n".join(f"- {tag.name}: {tag.description}" for tag in tags)


def _final_system_prompt(tags: list[Tag]) -> str:
    return (
        "You are a transcript summarizer. You receive the text of a recorded session "
        "and must respond with ONLY a JSON object — no prose, no Markdown fences.\n"
        "Rules:\n"
        "- Use the dominant language. For all Chinese—including Cantonese, Mandarin, or "
        "Simplified Chinese—ALWAYS use Traditional Chinese; retain needed English terms.\n"
        "- `overview`: about 50–80 Chinese characters, or equally concise; avoid repetition.\n"
        "- `key_points`: detailed reasoning, examples, decisions, and conclusions. Number only "
        "genuine hierarchy as `1.`, `1.1`, `1.1.1` (maximum three levels); never force hierarchy; "
        "leave unrelated/deeper details unnumbered.\n"
        "- `action_items`: only explicit future commitments, assignments, or requests—not advice, "
        "possibilities, discussion, or completed work. Owner/due date must be explicit, else null.\n"
        "- `people`: explicitly named identifiable people only; no pronouns, roles, or generic "
        "references. Organizations must be named; topics must be substantively discussed.\n"
        "- If uncertain use []; never infer, fill for completeness, or fabricate.\n"
        "- `suggested_tags`: choose zero or more from the ALLOWED TAGS list below, "
        "using the exact names. Use `Unknown` only when no other allowed tag fits.\n"
        "- `language`: the primary language of the transcript (for example zh-HK, en, fi).\n\n"
        f"ALLOWED TAGS:\n{_tag_lines(tags)}\n\n"
        f"Respond with ONLY a JSON object with exactly this shape:\n{FINAL_SHAPE_DOC}"
    )


def _map_system_prompt() -> str:
    return (
        "You are a transcript summarizer working on ONE chunk of a longer recording. "
        "Respond with ONLY a JSON object — no prose, no Markdown fences.\n"
        "Rules:\n"
        "- Use the dominant language; for all Chinese ALWAYS use Traditional Chinese.\n"
        "- Retain useful reasoning, examples, decisions, explicit future actions, named people/"
        "organizations, and substantive topics so final merge need not infer them.\n"
        "- Never fabricate or force chunk-level numbering.\n\n"
        f"Respond with ONLY a JSON object with exactly this shape:\n{MAP_SHAPE_DOC}"
    )


def _user_transcript_prompt(text: str) -> str:
    return f"<transcript>\n{text}\n</transcript>\n\nSummarize the transcript above. Respond with ONLY the JSON object."


def _map_user_prompt(text: str, index: int, total: int) -> str:
    return (
        f"<transcript_chunk part={index} of={total}>\n{text}\n</transcript_chunk>\n\n"
        "Summarize this chunk. Respond with ONLY the JSON object."
    )


def _reduce_user_prompt(intermediates: list[dict], *, final: bool) -> str:
    body = json.dumps(intermediates, ensure_ascii=False)
    if final:
        instruction = (
            "These are partial summaries of consecutive chunks of one transcript, in "
            "chronological order. Merge them into one final summary of the whole "
            "recording; add no new actions, people, organizations, or topics. Respond with ONLY a JSON "
            "object with exactly this shape:\n"
            f"{FINAL_SHAPE_DOC}"
        )
    else:
        instruction = (
            "These are partial summaries of consecutive chunks of one transcript, in "
            "chronological order. Merge them into one shorter partial summary, preserving "
            "explicit actions and named entities; deduplicate but add nothing. "
            "Respond with ONLY a JSON object with exactly this shape:\n"
            f"{MAP_SHAPE_DOC}"
        )
    return f"<partial_summaries>\n{body}\n</partial_summaries>\n\n{instruction}"


# ---------------------------------------------------------------------------
# Parsing and schema validation
# ---------------------------------------------------------------------------


def _parse_model_json(content: str):
    text = content.strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except ValueError:
        raise llm_service.LLMInvalid("malformed_model_json", "model output is not valid JSON") from None
    if not isinstance(data, dict):
        raise llm_service.LLMInvalid("malformed_model_json", "model output is not a JSON object")
    return data


def _require_str(value, field: str, *, max_length: int, allow_empty: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise llm_service.LLMInvalid("schema_validation", f"field '{field}' must be a string")
    cleaned = value.strip()
    if not cleaned and not allow_empty:
        raise llm_service.LLMInvalid("schema_validation", f"field '{field}' must be a non-empty string")
    if len(cleaned) > max_length:
        raise llm_service.LLMInvalid(
            "schema_validation",
            f"field '{field}' exceeds the maximum of {max_length} characters",
        )
    return cleaned


def _str_list(value, field: str, max_items: int, max_item_chars: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise llm_service.LLMInvalid("schema_validation", f"field '{field}' must be a list of strings")
    if len(value) > max_items:
        raise llm_service.LLMInvalid(
            "schema_validation", f"field '{field}' exceeds the maximum of {max_items} items"
        )
    return [
        _require_str(item, f"{field}[{index}]", max_length=max_item_chars)
        for index, item in enumerate(value)
    ]


def _validate_action_items(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise llm_service.LLMInvalid("schema_validation", "field 'action_items' must be a list")
    if len(value) > MAX_ACTION_ITEMS:
        raise llm_service.LLMInvalid(
            "schema_validation", f"field 'action_items' exceeds the maximum of {MAX_ACTION_ITEMS} items"
        )
    items: list[dict] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise llm_service.LLMInvalid(
                "schema_validation", f"field 'action_items[{index}]' must be an object"
            )
        text = _require_str(item.get("text"), f"action_items[{index}].text", max_length=MAX_ACTION_TEXT_CHARS)
        owner_raw = item.get("owner")
        owner = (
            None
            if owner_raw is None
            else _require_str(owner_raw, f"action_items[{index}].owner", max_length=MAX_ACTION_OWNER_CHARS)
        )
        due_raw = item.get("due_date")
        due = (
            None
            if due_raw is None
            else _require_str(due_raw, f"action_items[{index}].due_date", max_length=MAX_ACTION_DUE_CHARS)
        )
        items.append({"text": text, "owner": owner, "due_date": due})
    return items


def _validate_suggested_tags(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise llm_service.LLMInvalid("schema_validation", "field 'suggested_tags' must be a list of strings")
    if len(value) > MAX_SUGGESTED_TAGS:
        raise llm_service.LLMInvalid(
            "schema_validation", f"field 'suggested_tags' exceeds the maximum of {MAX_SUGGESTED_TAGS} items"
        )
    return [
        _require_str(item, f"suggested_tags[{index}]", max_length=MAX_TAG_NAME_CHARS)
        for index, item in enumerate(value)
    ]


def validate_map_payload(data: dict) -> dict:
    """Validate an intermediate (map-stage) summary. Raises LLMInvalid."""
    return {
        "overview": _require_str(data.get("overview"), "overview", max_length=MAX_MAP_OVERVIEW_CHARS),
        "key_points": _str_list(
            data.get("key_points"), "key_points", MAX_MAP_KEY_POINTS, MAX_MAP_KEY_POINT_CHARS
        ),
    }


def validate_final_payload(data: dict, allowed: dict[str, Tag]) -> dict:
    """Validate the final structured summary; returns the canonical payload.

    ``suggested_tags`` are matched case-insensitively against configured
    tags; unknown names are returned as ``rejected`` and never persisted
    as tags. A suggested ``Unknown`` is dropped whenever any real tag is
    also suggested.
    """
    title = _require_str(data.get("title"), "title", max_length=MAX_TITLE_CHARS)
    overview = _require_str(data.get("overview"), "overview", max_length=MAX_OVERVIEW_CHARS)
    key_points = _str_list(data.get("key_points"), "key_points", MAX_KEY_POINTS, MAX_KEY_POINT_CHARS)
    action_items = _validate_action_items(data.get("action_items"))
    people = _str_list(data.get("people"), "people", MAX_NAME_ITEMS, MAX_NAME_CHARS)
    organizations = _str_list(data.get("organizations"), "organizations", MAX_NAME_ITEMS, MAX_NAME_CHARS)
    topics = _str_list(data.get("topics"), "topics", MAX_NAME_ITEMS, MAX_NAME_CHARS)
    language = _require_str(
        data.get("language", ""), "language", max_length=MAX_LANGUAGE_CHARS, allow_empty=True
    )
    suggested_raw = _validate_suggested_tags(data.get("suggested_tags"))

    resolved: list[Tag] = []
    rejected: list[str] = []
    for name in suggested_raw:
        tag = allowed.get(tag_name_key(name))
        if tag is None:
            rejected.append(name)
        elif all(tag.pk != existing.pk for existing in resolved):
            resolved.append(tag)
    if len(resolved) > 1:
        non_unknown = [tag for tag in resolved if tag.name_key != "unknown"]
        if non_unknown:
            resolved = non_unknown

    return {
        "title": title,
        "overview": overview,
        "key_points": key_points,
        "action_items": action_items,
        "people": people,
        "organizations": organizations,
        "topics": topics,
        "language": language,
        "suggested": resolved,
        "rejected": rejected,
    }


# ---------------------------------------------------------------------------
# Bounded LLM calls (map, sub-reduce, final reduce)
# ---------------------------------------------------------------------------


def _call_llm(
    config: AppConfig,
    *,
    system: str,
    user: str,
    validate,
    transport=None,
    llm_call=None,
) -> dict:
    """One logical summarization call, retried once on invalid output.

    A logical call is: (1) the fully serialized request-size gate,
    (2) one HTTP call (or the injected ``llm_call`` behind the same
    gate), (3) envelope validation, (4) model-JSON parsing, (5) the
    applicable map/final schema validation. The complete logical call is
    retried exactly once when — and only when — the failure is an
    invalid-output failure (malformed HTTP JSON, invalid envelope,
    malformed model JSON, schema validation failure). Endpoint, timeout,
    HTTP-status, response-too-large and request-too-large failures are
    never retried. Both attempts use the same bounded request; the last
    specific error code is preserved when both attempts fail.
    """
    last: Exception | None = None
    for _ in range(2):
        try:
            payload = llm_service.build_chat_payload(
                config,
                system_prompt=system,
                user_prompt=user,
                temperature=config.summarization.temperature,
                max_tokens=config.summarization.max_output_tokens,
            )
            size = llm_service.request_payload_characters(payload)
            if size > config.summarization.max_input_characters:
                raise InputTooLarge(
                    "request_too_large",
                    f"serialized request is {size} characters, exceeding the per-request limit of "
                    f"{config.summarization.max_input_characters}",
                )
            if llm_call is not None:
                # Test injection runs behind the same size gate and the
                # same parse/validation retry semantics.
                content = llm_call(system=system, user=user)
            else:
                content = llm_service.chat_completion(
                    config,
                    system_prompt=system,
                    user_prompt=user,
                    temperature=config.summarization.temperature,
                    max_tokens=config.summarization.max_output_tokens,
                    transport=transport,
                )
            return validate(_parse_model_json(content))
        except llm_service.LLMInvalid as exc:
            last = exc
    raise last  # type: ignore[misc]


def _reduce_layer(
    intermediates: list[dict],
    config: AppConfig,
    allowed: dict[str, Tag],
    *,
    final: bool,
    transport=None,
    llm_call=None,
) -> dict:
    """Deterministic hierarchical reduction over actual serialized sizes.

    Tries one merged call; when the fully serialized request exceeds the
    per-request cap, splits deterministically in half and recurses with
    the map schema. If even a single intermediate cannot fit the cap,
    fails cleanly BEFORE any HTTP call.
    """
    if final:
        validate = lambda data: validate_final_payload(data, allowed)  # noqa: E731
    else:
        validate = validate_map_payload
    try:
        return _call_llm(
            config,
            system=_final_system_prompt(list(allowed.values())) if final else _map_system_prompt(),
            user=_reduce_user_prompt(intermediates, final=final),
            validate=validate,
            transport=transport,
            llm_call=llm_call,
        )
    except InputTooLarge:
        if len(intermediates) < 2:
            raise
        mid = len(intermediates) // 2
        left = _reduce_layer(
            intermediates[:mid], config, allowed, final=False, transport=transport, llm_call=llm_call
        )
        right = _reduce_layer(
            intermediates[mid:], config, allowed, final=False, transport=transport, llm_call=llm_call
        )
        return _reduce_layer(
            [left, right], config, allowed, final=final, transport=transport, llm_call=llm_call
        )


def _generate_summary(
    config: AppConfig,
    plan: ChunkPlan,
    tags: list[Tag],
    *,
    transport=None,
    llm_call=None,
) -> tuple[dict, int]:
    """Run the map/reduce flow; returns (canonical payload, chunk_count)."""
    allowed = {tag.name_key: tag for tag in tags}
    if len(plan.chunks) == 1:
        payload = _call_llm(
            config,
            system=_final_system_prompt(tags),
            user=_user_transcript_prompt(plan.chunks[0]),
            validate=lambda data: validate_final_payload(data, allowed),
            transport=transport,
            llm_call=llm_call,
        )
        return payload, 1

    intermediates: list[dict] = []
    total = len(plan.chunks)
    for index, chunk in enumerate(plan.chunks):
        intermediates.append(
            _call_llm(
                config,
                system=_map_system_prompt(),
                user=_map_user_prompt(chunk, index + 1, total),
                validate=validate_map_payload,
                transport=transport,
                llm_call=llm_call,
            )
        )
    final = _reduce_layer(intermediates, config, allowed, final=True, transport=transport, llm_call=llm_call)
    return final, total


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def config_fingerprint(config: AppConfig, tags: list[Tag]) -> str:
    """Safe fingerprint of the summarization-relevant configuration.

    Contains model/endpoint identity, prompt/parser versions, limits and
    configured tag identities — never secrets or prompt text.
    """
    payload = {
        "model": config.llm.model,
        "base_url": config.llm.base_url,
        "prompt_version": config.summarization.prompt_version,
        "parser_version": PARSER_VERSION,
        "temperature": config.summarization.temperature,
        "max_output_tokens": config.summarization.max_output_tokens,
        "max_input_characters": config.summarization.max_input_characters,
        "chunk_characters": config.summarization.chunk_characters,
        "chunk_overlap_characters": config.summarization.chunk_overlap_characters,
        "max_chunk_count": config.summarization.max_chunk_count,
        "max_total_characters": config.summarization.max_total_characters,
        "tags": sorted(tag.name_key for tag in tags),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _limits_used(config: AppConfig) -> dict:
    s = config.summarization
    return {
        "max_input_characters": s.max_input_characters,
        "max_chunk_count": s.max_chunk_count,
        "max_total_characters": s.max_total_characters,
    }


def _outcome_for(exc: llm_service.LLMError) -> str:
    if isinstance(exc, llm_service.LLMTimeout):
        return AttemptOutcome.TIMEOUT
    if isinstance(exc, llm_service.LLMHTTPError):
        return AttemptOutcome.HTTP_ERROR
    if isinstance(exc, llm_service.LLMUnavailable):
        return AttemptOutcome.UNREACHABLE
    if isinstance(exc, llm_service.LLMResponseTooLarge):
        return AttemptOutcome.RESPONSE_TOO_LARGE
    return AttemptOutcome.INVALID_OUTPUT


# ---------------------------------------------------------------------------
# Attempt state transitions
# ---------------------------------------------------------------------------


def _finish_attempt_failure(
    attempt: ProcessingAttempt,
    recording: Recording,
    outcome: str,
    error_code: str,
    message: str,
) -> None:
    """Finish a failed attempt; update orthogonal summary state only.

    Never touches ``processing_status``. Every completed failure — with
    or without a current summary — durably records the failed attempt on
    the recording (``last_failed_attempt``):

    - no current summary: ``summary_status=failed``,
      ``resummarization_failed=False``;
    - current summary (failed regeneration): it stays active,
      ``summary_status=current``, ``resummarization_failed=True``.

    Successful persistence clears all three fields (see
    ``persist_summary``); a new active transcript clears them too.
    """
    attempt.outcome = outcome
    attempt.error_code = error_code
    attempt.error_message = sanitize_error(message)
    attempt.finished_at = timezone.now()
    with transaction.atomic():
        rec = Recording.objects.select_for_update().get(pk=recording.pk)
        attempt.save()
        if rec.current_summary() is not None:
            rec.summary_status = SummaryState.CURRENT
            rec.resummarization_failed = True
        else:
            rec.summary_status = SummaryState.FAILED
            rec.resummarization_failed = False
        rec.last_failed_attempt = attempt
        rec.save()


def reconcile_recording_summary_state(recording: Recording, recovered_attempt: ProcessingAttempt | None = None) -> bool:
    """Idempotent reconciliation of summary state after recovery.

    Contract:

    - ``recovered_attempt`` (the recommended path) is the specific
      summarization attempt just converted to ``interrupted`` by
      recovery; because the pipeline lock excludes concurrent workflow
      mutations, it is the authoritative event being reconciled:
      - no current Summary: ``summary_status=failed``,
        ``resummarization_failed=False``,
        ``last_failed_attempt=<recovered attempt>``;
      - current Summary (interrupted regeneration): it stays active,
        ``summary_status=current``, ``resummarization_failed=True``,
        ``last_failed_attempt=<recovered attempt>``.

      In both states normal ``brain run`` must not retry; explicit
      retry is required.

    - Without ``recovered_attempt`` (defensive/direct use only) the
      function makes ONLY corrections that cannot violate the locked
      retry rule: it never upgrades an existing ``failed`` to
      ``missing`` and never touches failure markers. It sets
      ``missing`` only when the recording has ZERO summarization
      attempts (durable proof of "never attempted"); recordings whose
      only summarization attempts belong to older transcripts are left
      untouched (a new active transcript receives ``missing`` from the
      transcription persistence hook, which is the valid event).

    Returns True when the recording was saved. Repeated runs compare
    the expected state before saving, so they never create attempts,
    change ordinals, or rewrite an already-correct state.
    """
    if not recording.transcripts.filter(is_active=True).exists():
        if recording.summary_status != SummaryState.NOT_READY:
            recording.summary_status = SummaryState.NOT_READY
            recording.save(update_fields=["summary_status"])
        return False

    if recovered_attempt is not None:
        current = recording.current_summary()
        expected: dict
        if current is None:
            expected = dict(
                summary_status=SummaryState.FAILED,
                resummarization_failed=False,
                last_failed_attempt_id=recovered_attempt.pk,
            )
        else:
            expected = dict(
                summary_status=SummaryState.CURRENT,
                resummarization_failed=True,
                last_failed_attempt_id=recovered_attempt.pk,
            )
        changed = any(getattr(recording, field) != value for field, value in expected.items())
        if changed:
            for field, value in expected.items():
                setattr(recording, field, value)
            recording.save()
        return changed

    # Defensive path (no recovered attempt): conservative corrections only.
    current = recording.current_summary()
    if current is not None:
        if recording.summary_status != SummaryState.CURRENT:
            recording.summary_status = SummaryState.CURRENT
            recording.save(update_fields=["summary_status"])
            return True
        return False
    has_any_summarization_attempt = ProcessingAttempt.objects.filter(
        recording=recording, stage=AttemptStage.SUMMARIZATION
    ).exists()
    if not has_any_summarization_attempt and recording.summary_status != SummaryState.MISSING:
        recording.summary_status = SummaryState.MISSING
        recording.save(update_fields=["summary_status"])
        return True
    return False


# ---------------------------------------------------------------------------
# Persistence (the single validated atomic path for Summary creation)
# ---------------------------------------------------------------------------


class SummaryRelationError(Exception):
    """Summary objects relationship invariant violated; nothing is written."""


def persist_summary(
    *,
    recording: Recording,
    transcript: Transcript,
    section: Section,
    attempt: ProcessingAttempt,
    payload: dict,
    model_id: str,
    base_url: str,
    prompt_version: str,
    fingerprint: str,
    chunk_count: int,
    input_characters: int,
    limits_used: dict,
    generation_mode: str,
) -> Summary:
    """Create the Summary, activate it, materialize tag suggestions.

    The ONLY code path that creates ``Summary`` rows. Enforces:

    - ``section.transcript_id == transcript.pk`` and
      ``transcript.recording_id == recording.pk`` (nothing is written on
      mismatch);
    - exactly one active Summary per (transcript, section) scope: active
      summaries of THIS transcript are deactivated atomically before the
      new one activates. Summaries of older transcripts are untouched —
      they stay historically active in their own scopes.
    """
    if section.transcript_id != transcript.pk:
        raise SummaryRelationError("section does not belong to the summary's transcript")
    if transcript.recording_id != recording.pk:
        raise SummaryRelationError("transcript does not belong to the summary's recording")

    now = timezone.now()
    with transaction.atomic():
        rec = Recording.objects.select_for_update().get(pk=recording.pk)
        Summary.objects.filter(transcript=transcript, section=section, is_active=True).update(
            is_active=False, superseded_at=now
        )
        last = Summary.objects.filter(recording=rec).order_by("-ordinal").first()
        summary = Summary.objects.create(
            recording=rec,
            transcript=transcript,
            section=section,
            attempt=attempt,
            ordinal=(last.ordinal + 1) if last else 1,
            is_active=True,
            activated_at=now,
            title=payload["title"],
            overview=payload["overview"],
            key_points=payload["key_points"],
            action_items=payload["action_items"],
            people=payload["people"],
            organizations=payload["organizations"],
            topics=payload["topics"],
            language=payload["language"],
            suggested_tags_raw={
                "suggested": [tag.name for tag in payload["suggested"]],
                "rejected": payload["rejected"],
            },
            model_id=model_id,
            llm_base_url=base_url,
            prompt_version=prompt_version,
            parser_version=PARSER_VERSION,
            config_fingerprint=fingerprint,
            chunk_count=chunk_count,
            input_characters=input_characters,
            input_truncated=False,
            limits_used=limits_used,
            generation_mode=generation_mode,
        )
        _materialize_tags(rec, summary, payload["suggested"])
        rec.summary_status = SummaryState.CURRENT
        rec.resummarization_failed = False
        rec.last_failed_attempt = None
        rec.save()
        attempt.outcome = AttemptOutcome.SUCCESS
        attempt.error_code = ""
        attempt.error_message = ""
        attempt.finished_at = now
        attempt.save()
    return summary


def _materialize_tags(recording: Recording, summary: Summary, tags: list[Tag]) -> None:
    """Record per-version suggestions; materialize effective assignments.

    Only ``suggested``-origin assignments are refreshed: ones the new
    version no longer suggests are deactivated (``deactivated_by=
    "model"``), and new suggestions are activated with
    ``source_summary`` provenance. Manual (and confirmed) assignments
    are never modified — a tag the user assigned manually is only
    recorded as a suggestion for this summary version.

    User suppressions are honoured: an assignment the user explicitly
    removed/rejected (``deactivated_by="user"``) is never reactivated by
    a new suggestion; the suggestion itself is still recorded on the
    summary version for provenance and review.
    """
    now = timezone.now()
    new_keys = {tag.name_key for tag in tags}
    for assignment in TagAssignment.objects.filter(
        recording=recording, is_active=True, origin=TagOrigin.SUGGESTED
    ):
        if assignment.tag.name_key not in new_keys:
            assignment.is_active = False
            assignment.deactivated_at = now
            assignment.deactivated_by = TagDeactivatedBy.MODEL
            assignment.save()
    for tag in tags:
        SummaryTagSuggestion.objects.get_or_create(summary=summary, tag=tag)
        assignment = TagAssignment.objects.filter(recording=recording, tag=tag).first()
        if assignment is None:
            TagAssignment.objects.create(
                recording=recording,
                tag=tag,
                origin=TagOrigin.SUGGESTED,
                source_summary=summary,
                is_active=True,
            )
        elif not assignment.is_active:
            if assignment.deactivated_by == TagDeactivatedBy.USER:
                # Explicit user suppression survives re-summarization.
                continue
            assignment.is_active = True
            assignment.origin = TagOrigin.SUGGESTED
            assignment.source_summary = summary
            assignment.deactivated_at = None
            assignment.deactivated_by = TagDeactivatedBy.NONE
            assignment.save()
        elif assignment.origin == TagOrigin.SUGGESTED:
            assignment.source_summary = summary
            assignment.save()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _skip(recording: Recording, reason: str) -> dict:
    return {
        "recording_id": recording.pk,
        "result": "skipped",
        "reason": reason,
        "summary_status": recording.summary_status,
    }


def summarize_one(
    config: AppConfig,
    recording: Recording,
    *,
    regenerate: bool = False,
    generation_mode: str = GenerationMode.MANUAL,
    transport=None,
    llm_call=None,
) -> dict:
    """Summarize one recording under the caller's pipeline lock."""
    recording = Recording.objects.get(pk=recording.pk)
    if not config.summarization.enabled:
        return _skip(recording, "summarization_disabled")
    transcript = recording.transcripts.filter(is_active=True).first()
    if transcript is None:
        return _skip(recording, "no_active_transcript")
    current = recording.current_summary()
    if current is not None and not regenerate:
        return _skip(recording, "summary_current")
    if not config.llm.model.strip():
        raise ConfigError("no summarization model configured (llm.model is blank)")
    section = transcript.sections.filter(ordinal=0).first()
    if section is None:
        raise ConfigError("active transcript has no whole-recording section")

    # Synchronize configured tags inside the locked mutating path (never
    # on read-only commands); retired rows are kept, never deleted.
    tags_service.sync_tags(config)

    attempt = ProcessingAttempt.objects.create(
        recording=recording,
        stage=AttemptStage.SUMMARIZATION,
        ordinal=next_ordinal(recording, AttemptStage.SUMMARIZATION),
        model_id=config.llm.model,
        cli_args_json={
            "kind": "omlx_summarization",
            "base_url": config.llm.base_url,
            "model": config.llm.model,
            "prompt_version": config.summarization.prompt_version,
            "generation_mode": generation_mode,
            "regenerate": regenerate,
        },
    )

    s = config.summarization
    segments = list(transcript.segments.order_by("ordinal").values_list("text", flat=True))
    if not segments and transcript.text_normalized:
        segments = [transcript.text_normalized]
    if not segments:
        _finish_attempt_failure(
            attempt, recording, AttemptOutcome.INVALID_OUTPUT, "empty_transcript",
            "active transcript has no text to summarize",
        )
        return _failed(recording, "empty_transcript", kept_current=False)

    plan = build_chunks(
        segments,
        chunk_characters=s.chunk_characters,
        overlap_characters=s.chunk_overlap_characters,
    )
    attempt.cli_args_json = {
        **attempt.cli_args_json,
        "input_characters": plan.input_characters,
        "chunk_count": len(plan.chunks),
        "limits": _limits_used(config),
    }
    attempt.save(update_fields=["cli_args_json"])

    try:
        check_chunk_limits(
            plan, max_total_characters=s.max_total_characters, max_chunk_count=s.max_chunk_count
        )
        tags = list(Tag.objects.filter(is_configured=True).order_by("name"))
        fingerprint = config_fingerprint(config, tags)
        payload, chunk_count = _generate_summary(
            config, plan, tags, transport=transport, llm_call=llm_call
        )
    except InputTooLarge as exc:
        _finish_attempt_failure(attempt, recording, AttemptOutcome.INPUT_TOO_LARGE, "input_too_large", str(exc))
        return _failed(recording, "input_too_large", kept_current=recording.current_summary() is not None)
    except llm_service.LLMError as exc:
        # Never store raw exception messages (they could carry sensitive
        # content from injected or third-party exceptions): record only
        # the sanitized exception type plus, for HTTP errors, the status
        # code. Error codes are stable identifiers.
        message = type(exc).__name__
        if isinstance(exc, llm_service.LLMHTTPError):
            message = f"HTTP {exc.status_code}"
        _finish_attempt_failure(attempt, recording, _outcome_for(exc), exc.code, message)
        return _failed(recording, exc.code, kept_current=recording.current_summary() is not None)

    summary = persist_summary(
        recording=recording,
        transcript=transcript,
        section=section,
        attempt=attempt,
        payload=payload,
        model_id=config.llm.model,
        base_url=config.llm.base_url,
        prompt_version=s.prompt_version,
        fingerprint=fingerprint,
        chunk_count=chunk_count,
        input_characters=plan.input_characters,
        limits_used=_limits_used(config),
        generation_mode=generation_mode,
    )
    return {
        "recording_id": recording.pk,
        "result": "summarized",
        "summary_id": summary.pk,
        "regeneration": current is not None,
        "chunk_count": chunk_count,
        "input_characters": plan.input_characters,
        "tags": [tag.name for tag in payload["suggested"]],
    }


def _failed(recording: Recording, error_code: str, *, kept_current: bool) -> dict:
    return {
        "recording_id": recording.pk,
        "result": "failed",
        "error_code": error_code,
        "kept_current_summary": kept_current,
    }


def summarize_pending(config: AppConfig) -> dict:
    """Automatic stage of ``brain run``: summarize never-attempted recordings.

    Only ``summary_status=missing`` recordings are processed — never
    ``failed`` ones (no automatic retry loop) and never regenerations.
    """
    if not config.summarization.enabled:
        return {"skipped_reason": "summarization_disabled", "results": []}
    if not config.llm.model.strip():
        return {"skipped_reason": "llm_model_not_configured", "results": []}
    from workflow.services.tags import sync_tags

    sync = sync_tags(config)
    results: list[dict] = []
    eligible = Recording.objects.filter(
        processing_status=ProcessingStatus.TRANSCRIBED, summary_status=SummaryState.MISSING
    )
    for recording in eligible:
        results.append(summarize_one(config, recording, generation_mode=GenerationMode.AUTOMATIC))
    return {"tag_sync": sync, "results": results}


__all__ = [
    "PARSER_VERSION",
    "InputTooLarge",
    "SummaryRelationError",
    "summarize_one",
    "summarize_pending",
    "persist_summary",
    "validate_final_payload",
    "validate_map_payload",
    "config_fingerprint",
    "reconcile_recording_summary_state",
]
