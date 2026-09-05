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
from dataclasses import dataclass

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
from workflow.services import languages
from workflow.services import llm as llm_service
from workflow.services import tags as tags_service
from workflow.services import variant_state as variant_state_service
from workflow.services.chunking import ChunkPlan, InputTooLarge, build_chunks, check_chunk_limits
from workflow.services.langresolve import (  # noqa: F401 (re-exported)
    resolve_default_language,
    resolve_output_language,
)
from workflow.services.search_sync import schedule_recording_sync
from workflow.services.transcription import next_ordinal, sanitize_error

logger = logging.getLogger(__name__)

PARSER_VERSION = "1"
# Code-owned prompt implementation version. Stored on Summary for
# provenance. Replaces config-driven prompt_version for new summaries.
PROMPT_IMPLEMENTATION_VERSION = "2"

FINAL_SHAPE_DOC = (
    '{"title": string, "overview": string, '
    '"key_points": [{"text": string, "level": 0|1|2|3}], '
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
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


# ---------------------------------------------------------------------------
# Language resolution
#
# The canonical language policy lives in workflow.services.languages and
# output-language resolution in workflow.services.langresolve; both are
# re-exported here for backwards compatibility. There is no second
# normalization policy anywhere in the runtime.
# ---------------------------------------------------------------------------

is_chinese_family = languages.is_chinese_family  # noqa: F401 (re-export)
normalize_source_language = languages.canonicalize_language  # noqa: F401 (re-export)
canonical_source_for_output = languages.output_language_for_source  # noqa: F401 (re-export)


def _detect_source_language(
    config: AppConfig, transcript: Transcript, *, transport=None, llm_call=None
) -> tuple[str | None, llm_service.LLMError | None]:
    """Bounded local source-language detection via the oMLX endpoint.

    Returns (canonical_language_code_or_None, error_or_None).
    Invalid output may retry once (max 2 calls). Endpoint/HTTP/timeout
    failures do not retry. No raw prompts, responses, or transcript
    text are persisted.
    """
    segments = list(transcript.segments.order_by("ordinal").values_list("text", flat=True))
    if not segments and transcript.text_normalized:
        segments = [transcript.text_normalized]
    if not segments:
        return None, None
    # Bound input to first 4000 characters
    text = "\n".join(segments)[:4000]

    system = (
        "You are a language detector. Given a transcript, return ONLY a "
        'JSON object: {"language": "<BCP-47 code>"}. Detect the dominant '
        "spoken language. Use standard codes like en, fi, zh-HK, zh-CN, "
        "yue, sv, etc."
    )
    user = f"<transcript>\n{text}\n</transcript>\n\nDetect the language. Respond with ONLY the JSON object."

    def _validate(data):
        lang = data.get("language", "")
        if not isinstance(lang, str) or not lang.strip():
            raise llm_service.LLMInvalid("schema_validation", "field 'language' must be a non-empty string")
        lang = lang.strip()
        if len(lang) > MAX_LANGUAGE_CHARS:
            raise llm_service.LLMInvalid("schema_validation", "language code too long")
        # Canonicalize casing first, then validate
        canonical = normalize_source_language(lang)
        if not canonical:
            raise llm_service.LLMInvalid("schema_validation", f"invalid language code: {lang}")
        return canonical

    try:
        result = _call_llm(
            config,
            system=system,
            user=user,
            validate=_validate,
            transport=transport,
            llm_call=llm_call,
        )
        return result, None
    except llm_service.LLMInvalid:
        # Invalid output: _call_llm already retried once; return failure
        return None, llm_service.LLMInvalid("source_language_unknown", "invalid detector output after retry")
    except llm_service.LLMError as exc:
        # Endpoint/HTTP/timeout failures: no retry, preserve category
        return None, exc
    except InputTooLarge as exc:
        # Request-too-large is its own stable category — never
        # conflated with invalid output. No retry.
        raise


def _language_instruction(output_language: str, source_language: str) -> str:
    """Return the language-specific instruction block for prompts."""
    if output_language == "en":
        return (
            "- Write ALL summary prose (title, overview, key_points text, "
            "action_items text) in English. Preserve necessary proper nouns "
            "and technical terms."
        )
    if output_language == "zh-Hant":
        return (
            "- Write ALL summary prose in Traditional Chinese (繁體中文). "
            "All Chinese characters MUST be Traditional Chinese, never "
            "Simplified. Retain needed English terms and proper nouns."
        )
    # Concrete language code (e.g. "fi")
    return (
        f"- Write ALL summary prose in {output_language}. "
        "Do not translate into another language. Retain proper nouns."
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _tag_lines(tags: list[Tag]) -> str:
    if not tags:
        return "(no tags configured; return an empty suggested_tags list)"
    return "\n".join(f"- {tag.name}: {tag.description}" for tag in tags)


def _final_system_prompt(
    tags: list[Tag], output_language: str = "", source_language: str = ""
) -> str:
    lang_instruction = _language_instruction(output_language, source_language)
    return (
        "You are a transcript summarizer. You receive the text of a recorded session "
        "and must respond with ONLY a JSON object — no prose, no Markdown fences.\n"
        "Rules:\n"
        f"{lang_instruction}\n"
        "- `overview`: concise—about 50–80 Chinese characters for Chinese, or a similarly short "
        "one-to-three sentences in another language; avoid repetition.\n"
        "- `key_points`: detailed reasoning, examples, decisions, and conclusions. Set `level` "
        "to 1/2/3 only for genuine hierarchy (maximum three levels); the app generates `1.`, "
        "`1.1`, `1.1.1`. Use level "
        "0 for unrelated items or detail beyond level 3. Never write numbering inside `text`, "
        "force hierarchy, or jump from level 1 directly to level 3.\n"
        "- `action_items`: only explicit future commitments, assignments, or requests—not advice, "
        "possibilities, discussion, or completed work. Owner/due date must be explicit, else null.\n"
        "- `people`: explicitly named identifiable people only; no pronouns, roles, or generic "
        "references. Organizations must be named; topics must be substantively discussed.\n"
        "- If uncertain use []; never infer, fill for completeness, or fabricate.\n"
        "- `suggested_tags`: choose zero or more from the ALLOWED TAGS list below, "
        "using the exact names. Use `Unknown` only when no other allowed tag fits.\n"
        "- `language`: the primary language of the transcript (for example zh-HK, en, fi). "
        "This is the transcript's dominant language, NOT the summary output language.\n\n"
        f"ALLOWED TAGS:\n{_tag_lines(tags)}\n\n"
        f"Respond with ONLY a JSON object with exactly this shape:\n{FINAL_SHAPE_DOC}"
    )


def _map_system_prompt(output_language: str = "", source_language: str = "") -> str:
    lang_instruction = _language_instruction(output_language, source_language)
    return (
        "You are a transcript summarizer working on ONE chunk of a longer recording. "
        "Respond with ONLY a JSON object — no prose, no Markdown fences.\n"
        "Rules:\n"
        f"{lang_instruction}\n"
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


def _key_point_list(value) -> list[dict]:
    """Validate structured key points; numbering is rendered deterministically."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise llm_service.LLMInvalid("schema_validation", "field 'key_points' must be a list")
    if len(value) > MAX_KEY_POINTS:
        raise llm_service.LLMInvalid(
            "schema_validation", f"field 'key_points' exceeds the maximum of {MAX_KEY_POINTS} items"
        )
    points: list[dict] = []
    previous_numbered_level = 0
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise llm_service.LLMInvalid(
                "schema_validation", f"field 'key_points[{index}]' must be an object"
            )
        text = _require_str(
            item.get("text"), f"key_points[{index}].text", max_length=MAX_KEY_POINT_CHARS
        )
        level = item.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or level not in (0, 1, 2, 3):
            raise llm_service.LLMInvalid(
                "schema_validation", f"field 'key_points[{index}].level' must be 0, 1, 2, or 3"
            )
        if level > 0:
            if level > previous_numbered_level + 1:
                raise llm_service.LLMInvalid(
                    "schema_validation", f"field 'key_points[{index}].level' skips a hierarchy level"
                )
            previous_numbered_level = level
        points.append({"text": text, "level": level})
    return points


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


def _payload_prose(payload: dict) -> str:
    """Collect bounded validated prose fields for a script-consistency check."""
    values = [str(payload.get("title", "")), str(payload.get("overview", ""))]
    for point in payload.get("key_points", []):
        values.append(str(point.get("text", "")) if isinstance(point, dict) else str(point))
    for item in payload.get("action_items", []):
        if isinstance(item, dict):
            values.append(str(item.get("text", "")))
    return "\n".join(values)


def _validate_language_consistency(
    payload: dict, *, output_language: str
) -> dict:
    """Validate that summary prose matches the expected output language.

    Uses script-level checks only. Conservative: rejects high-confidence
    contradictions. Cannot distinguish Latin-script languages (Finnish vs
    English) — relies on the LLM following the prompt.
    """
    if output_language == "original" or not output_language:
        return payload  # no validation for original or empty

    prose = _payload_prose(payload)
    cjk_count = len(_CJK_RE.findall(prose))
    visible_count = sum(not c.isspace() for c in prose)

    if output_language == "en":
        # Reject clearly Chinese prose
        if cjk_count >= 10 and cjk_count / max(1, visible_count) >= 0.10:
            raise llm_service.LLMInvalid(
                "language_mismatch",
                "expected English output but detected predominantly Chinese characters",
            )
    elif output_language == "zh-Hant":
        # Reject clearly non-Chinese prose (need enough text to be meaningful)
        if visible_count > 50 and cjk_count / max(1, visible_count) < 0.05:
            raise llm_service.LLMInvalid(
                "language_mismatch",
                "expected Chinese output but detected no Chinese characters",
            )
    # For other Latin-script languages: no script-level check is possible.
    # We rely on the LLM following the prompt instruction.

    return payload


def validate_final_payload(
    data: dict, allowed: dict[str, Tag], *, source_language: str = ""
) -> dict:
    """Validate the final structured summary; returns the canonical payload.

    ``suggested_tags`` are matched case-insensitively against configured
    tags; unknown names are returned as ``rejected`` and never persisted
    as tags. A suggested ``Unknown`` is dropped whenever any real tag is
    also suggested.

    Source-language provenance (one deterministic rule):

    - If ``source_language`` is a valid canonical code (the Transcript's
      already-resolved source), it is AUTHORITATIVE: it is stored as
      ``payload["language"]`` and the model's own ``language`` value —
      empty or contradictory — is ignored. A user-verified Transcript
      language is never displaced by the summary model.
    - If the Transcript source is genuinely unknown, the model's value
      is canonicalized: empty stays empty (the documented
      unknown-source case); any non-empty value that is not a valid
      BCP-47 tag raises ``LLMInvalid`` with a stable schema-validation
      code so the established invalid-output retry policy applies.

    Raw mixed-case or malformed model values never reach persistence.
    """
    title = _require_str(data.get("title"), "title", max_length=MAX_TITLE_CHARS)
    overview = _require_str(data.get("overview"), "overview", max_length=MAX_OVERVIEW_CHARS)
    key_points = _key_point_list(data.get("key_points"))
    action_items = _validate_action_items(data.get("action_items"))
    people = _str_list(data.get("people"), "people", MAX_NAME_ITEMS, MAX_NAME_CHARS)
    organizations = _str_list(data.get("organizations"), "organizations", MAX_NAME_ITEMS, MAX_NAME_CHARS)
    topics = _str_list(data.get("topics"), "topics", MAX_NAME_ITEMS, MAX_NAME_CHARS)
    raw_language = _require_str(
        data.get("language", ""), "language", max_length=MAX_LANGUAGE_CHARS, allow_empty=True
    )
    known_source = languages.canonicalize_language(source_language)
    if known_source:
        # The Transcript source was resolved before generation; it is
        # the single authoritative provenance for this Summary.
        canonical_language = known_source
    else:
        canonical_language = languages.canonicalize_language(raw_language)
        if raw_language and not canonical_language:
            raise llm_service.LLMInvalid(
                "schema_validation",
                "field 'language' is not a valid BCP-47 language tag",
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
        "language": canonical_language,
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
    output_language: str,
    source_language: str,
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
        validate = lambda data: _validate_language_consistency(  # noqa: E731
            validate_final_payload(data, allowed, source_language=source_language),
            output_language=output_language,
        )
    else:
        validate = lambda data: _validate_language_consistency(  # noqa: E731
            validate_map_payload(data), output_language=output_language
        )
    try:
        return _call_llm(
            config,
            system=_final_system_prompt(list(allowed.values()), output_language, source_language) if final else _map_system_prompt(output_language, source_language),
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
            intermediates[:mid], config, allowed, final=False,
            output_language=output_language, source_language=source_language,
            transport=transport, llm_call=llm_call,
        )
        right = _reduce_layer(
            intermediates[mid:], config, allowed, final=False,
            output_language=output_language, source_language=source_language,
            transport=transport, llm_call=llm_call,
        )
        return _reduce_layer(
            [left, right], config, allowed, final=final,
            output_language=output_language, source_language=source_language,
            transport=transport, llm_call=llm_call,
        )


def _generate_summary(
    config: AppConfig,
    plan: ChunkPlan,
    tags: list[Tag],
    *,
    output_language: str,
    source_language: str,
    transport=None,
    llm_call=None,
) -> tuple[dict, int]:
    """Run the map/reduce flow; returns (canonical payload, chunk_count)."""
    allowed = {tag.name_key: tag for tag in tags}
    if len(plan.chunks) == 1:
        payload = _call_llm(
            config,
            system=_final_system_prompt(tags, output_language, source_language),
            user=_user_transcript_prompt(plan.chunks[0]),
            validate=lambda data: _validate_language_consistency(
                validate_final_payload(data, allowed, source_language=source_language),
                output_language=output_language,
            ),
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
                system=_map_system_prompt(output_language, source_language),
                user=_map_user_prompt(chunk, index + 1, total),
                validate=lambda data: _validate_language_consistency(
                    validate_map_payload(data), output_language=output_language
                ),
                transport=transport,
                llm_call=llm_call,
            )
        )
    final = _reduce_layer(
        intermediates, config, allowed, final=True,
        output_language=output_language, source_language=source_language,
        transport=transport, llm_call=llm_call,
    )
    return final, total


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def config_fingerprint(config: AppConfig, tags: list[Tag], output_language: str = "") -> str:
    """Safe fingerprint of the summarization-relevant configuration.

    Contains model/endpoint identity, prompt/parser versions, limits,
    configured tag identities, and output_language — never secrets or
    prompt text.
    """
    payload = {
        "model": config.llm.model,
        "base_url": config.llm.base_url,
        "prompt_version": PROMPT_IMPLEMENTATION_VERSION,
        "parser_version": PARSER_VERSION,
        "temperature": config.summarization.temperature,
        "max_output_tokens": config.summarization.max_output_tokens,
        "max_input_characters": config.summarization.max_input_characters,
        "chunk_characters": config.summarization.chunk_characters,
        "chunk_overlap_characters": config.summarization.chunk_overlap_characters,
        "max_chunk_count": config.summarization.max_chunk_count,
        "max_total_characters": config.summarization.max_total_characters,
        "tags": sorted(tag.name_key for tag in tags),
        "output_language": output_language,
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
    *,
    output_language: str,
    transcript: Transcript,
    section: Section,
) -> None:
    """Finish a failed attempt; reconcile variant state via the
    centralized reconciler.

    Never touches ``processing_status``. The attempt is finished
    durably, then :func:`variant_state.reconcile_variant_state` derives
    the variant tuple and — only when this exact scope is the
    currently derived default of the active transcript's ordinal-0
    section — the Recording-level default tuple from the database.
    Optional variants never touch Recording-level summary fields.
    """
    attempt.outcome = outcome
    attempt.error_code = error_code
    attempt.error_message = sanitize_error(message)
    attempt.finished_at = timezone.now()
    with transaction.atomic():
        attempt.save()
        variant_state_service.reconcile_variant_state(
            recording=recording,
            transcript=transcript,
            section=section,
            output_language=output_language,
            triggering_attempt=attempt,
        )


def reconcile_recording_summary_state(recording: Recording, recovered_attempt: ProcessingAttempt | None = None) -> bool:
    """Idempotent reconciliation of summary state after recovery.

    Contract:

    - ``recovered_attempt`` is REQUIRED for any state change: it is the
      specific summarization attempt just converted to ``interrupted``
      by recovery. Reconciliation is EXACT-SCOPE ONLY — the attempt's
      durable provenance (``context_json["language"]`` with
      ``transcript_id``, ``section_id``, ``resolved``) decides which
      variant state is updated; the Recording-level default tuple is
      touched only when that exact scope is still the currently derived
      default of the ACTIVE transcript's ordinal-0 section.

      Conservative rules (no guessing, ever):

      - source-language-detection attempts (``language_detection``) are
        not variant events: nothing is updated;
      - attempts WITHOUT complete scope provenance (legacy, or a
        malformed/noncanonical ``resolved`` identity) never update any
        state — a stable ``legacy_attempt_scope_unknown`` diagnostic is
        logged; the attempt stays ``interrupted``;
      - forged provenance (transcript belonging to another recording,
        section belonging to another transcript) is rejected without
        any write.

      In the proven, exact-scope case: no current Summary for that
      variant → ``failed``; current Summary with a newer matching
      failure → ``current`` + ``regeneration_failed``. Normal
      ``brain run`` must not retry either state; explicit retry only.

    - Without ``recovered_attempt`` the function is a documented NO-OP.
      There is no production caller for an attempt-less defensive
      write: the reconciler is the single runtime writer of summary
      lifecycle state after an active transcript exists, and the new
      transcript's initial ``missing`` default state is set by the
      transcription-activation hook (the one documented initialization
      exception). Keeping this entry point write-free means no code
      path can "helpfully" guess state outside the reconciler.

    Returns True when the recording row changed (only possible with a
    recovered attempt).
    """
    if recovered_attempt is None:
        # Documented no-op: state is derived exclusively from recovery
        # events through the reconciler (see docstring).
        return False

    if variant_state_service.is_detection_attempt(recovered_attempt):
        # Detection failures are not summary-variant events.
        return False
    if not variant_state_service.has_complete_scope_provenance(recovered_attempt):
        logger.warning(
            "legacy_attempt_scope_unknown: interrupted summarization attempt %s "
            "carries no complete exact-scope provenance (legacy data or invalid "
            "output-language identity); summary state left unchanged "
            "(conservative recovery)",
            recovered_attempt.pk,
        )
    lang = (recovered_attempt.context_json or {}).get("language", {})
    attempt_transcript = Transcript.objects.filter(
        pk=str(lang.get("transcript_id", ""))
    ).first()
    if attempt_transcript is None:
        logger.warning(
            "legacy_attempt_scope_unknown: attempt %s provenance transcript "
            "does not exist; state left unchanged",
            recovered_attempt.pk,
        )
        return False
    if attempt_transcript.recording_id != recovered_attempt.recording_id:
        # Forged/mismatched transcript identity: never write.
        logger.warning(
            "Recovery: attempt %s provenance transcript %s does not belong to "
            "recording %s; state left unchanged",
            recovered_attempt.pk, attempt_transcript.pk, recovered_attempt.recording_id,
        )
        return False
    attempt_section = Section.objects.filter(pk=str(lang.get("section_id", ""))).first()
    if attempt_section is None or attempt_section.transcript_id != attempt_transcript.pk:
        logger.warning(
            "Recovery: attempt %s provenance section does not belong to provenance "
            "transcript %s; state left unchanged",
            recovered_attempt.pk, attempt_transcript.pk,
        )
        return False

    output_language = str(lang.get("resolved", ""))
    before = (
        recording.summary_status,
        recording.resummarization_failed,
        recording.last_failed_attempt_id,
    )
    variant_state_service.reconcile_variant_state(
        recording=recording,
        transcript=attempt_transcript,
        section=attempt_section,
        output_language=output_language,
        triggering_attempt=recovered_attempt,
    )
    recording.refresh_from_db()
    after = (
        recording.summary_status,
        recording.resummarization_failed,
        recording.last_failed_attempt_id,
    )
    return before != after


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
    output_language: str,
    is_default: bool,
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
    - exactly one active Summary per (transcript, section, output_language):
      active summaries of THIS transcript with the same output_language
      are deactivated atomically before the new one activates. Summaries
      of older transcripts or different output_languages are untouched.
    """
    if section.transcript_id != transcript.pk:
        raise SummaryRelationError("section does not belong to the summary's transcript")
    if transcript.recording_id != recording.pk:
        raise SummaryRelationError("transcript does not belong to the summary's recording")

    now = timezone.now()
    with transaction.atomic():
        rec = Recording.objects.select_for_update().get(pk=recording.pk)
        # Deactivate only same output_language active summaries
        Summary.objects.filter(
            transcript=transcript, section=section,
            output_language=output_language, is_active=True,
        ).update(is_active=False, superseded_at=now)
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
            output_language=output_language,
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
        # Materialize tags only for the default language variant
        if is_default:
            _materialize_tags(rec, summary, payload["suggested"])

        # Derive the variant state (and, when this variant is the
        # currently derived default, the Recording tuple) from the
        # database via the centralized reconciler.
        variant_state_service.reconcile_variant_state(
            recording=recording,
            transcript=transcript,
            section=section,
            output_language=output_language,
            triggering_attempt=attempt,
        )

        attempt.outcome = AttemptOutcome.SUCCESS
        attempt.error_code = ""
        attempt.error_message = ""
        attempt.finished_at = now
        attempt.save()
        # Step 5A.3: the new variant (+ materialized default-variant tags
        # and possible title/default-language changes) syncs after commit.
        schedule_recording_sync([rec.pk])
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
    target_language: str = "default",
    regenerate: bool = False,
    generation_mode: str = GenerationMode.MANUAL,
    transport=None,
    llm_call=None,
) -> dict:
    """Summarize one recording under the caller's pipeline lock.

    ``target_language``: one of the generation selectors ``default``,
    ``original``, ``en``, ``zh-Hant`` — nothing else may create a
    variant. Resolved to a concrete ``output_language`` before
    generation.
    """
    if target_language not in languages.GENERATION_SELECTORS:
        raise ConfigError(
            f"unsupported generation target: {target_language!r} "
            f"(allowed: {', '.join(languages.GENERATION_SELECTORS)})"
        )
    recording = Recording.objects.get(pk=recording.pk)
    if not config.summarization.enabled:
        return _skip(recording, "summarization_disabled")
    transcript = recording.transcripts.filter(is_active=True).first()
    if transcript is None:
        return _skip(recording, "no_active_transcript")
    if not config.llm.model.strip():
        raise ConfigError("no summarization model configured (llm.model is blank)")
    section = transcript.sections.filter(ordinal=0).first()
    if section is None:
        raise ConfigError("active transcript has no whole-recording section")

    # Resolve target_language to output_language
    output_language = resolve_output_language(transcript, target_language)

    # Handle "original" when source language is unknown
    if target_language == "original" and not output_language:
        detection = _detect_source_language_with_attempt(
            config, recording, transcript, section,
            transport=transport, llm_call=llm_call,
        )
        if not detection.language:
            # Surface the durable attempt's actual stable category
            # (endpoint_unavailable, timeout, http_error,
            # request_too_large, response_too_large,
            # source_language_unknown, ...). No variant state is created
            # until a concrete output language exists.
            return _failed(
                recording, detection.error_code or "source_language_unknown",
                kept_current=False,
            )
        detected = detection.language
        # Persist detection on transcript
        now = timezone.now()
        transcript.language_observed = detected
        transcript.language_observed_verified_by = "llm_detection"
        transcript.language_observed_verified_at = now
        transcript.save(update_fields=[
            "language_observed", "language_observed_verified_by",
            "language_observed_verified_at",
        ])
        # Step 5A.3: the detected source language changes the derived
        # default output language (metadata title chain). Sync now — even
        # if the later generation fails, this committed fact must reach
        # the index.
        schedule_recording_sync([recording.pk])
        # Re-resolve now that source is known
        output_language = resolve_output_language(transcript, target_language)
        if not output_language:
            return _failed(recording, "source_language_unknown", kept_current=False)

    # Determine if this is the default variant
    default_language = resolve_default_language(transcript)
    is_default = output_language == default_language

    # Check if variant already exists (unless regenerating)
    existing_active = Summary.objects.filter(
        transcript=transcript, section=section,
        output_language=output_language, is_active=True,
    ).first()
    if existing_active is not None and not regenerate:
        return _skip(recording, "variant_current")

    # Synchronize configured tags inside the locked mutating path
    tags_service.sync_tags(config)

    # Build language provenance for context_json
    language_provenance = {
        "requested": target_language,
        "resolved": output_language,
        "source": transcript.language_observed or "",
        "is_default": is_default,
        "source_method": transcript.language_observed_verified_by or "",
        "transcript_id": transcript.pk,
        "section_id": section.pk,
    }

    attempt = ProcessingAttempt.objects.create(
        recording=recording,
        stage=AttemptStage.SUMMARIZATION,
        ordinal=next_ordinal(recording, AttemptStage.SUMMARIZATION),
        model_id=config.llm.model,
        cli_args_json={
            "kind": "omlx_summarization",
            "base_url": config.llm.base_url,
            "model": config.llm.model,
            "prompt_version": PROMPT_IMPLEMENTATION_VERSION,
            "generation_mode": generation_mode,
            "regenerate": regenerate,
        },
        context_json={"language": language_provenance},
    )

    s = config.summarization
    segments = list(transcript.segments.order_by("ordinal").values_list("text", flat=True))
    if not segments and transcript.text_normalized:
        segments = [transcript.text_normalized]
    if not segments:
        _finish_attempt_failure(
            attempt, recording, AttemptOutcome.INVALID_OUTPUT, "empty_transcript",
            "active transcript has no text to summarize",
            output_language=output_language,
            transcript=transcript, section=section,
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

    source_language = transcript.language_observed or ""

    # Check if target variant has an active summary (for kept_current reporting)
    target_has_active = Summary.objects.filter(
        transcript=transcript, section=section,
        output_language=output_language, is_active=True,
    ).exists()

    try:
        check_chunk_limits(
            plan, max_total_characters=s.max_total_characters, max_chunk_count=s.max_chunk_count
        )
        tags = list(Tag.objects.filter(is_configured=True).order_by("name"))
        fingerprint = config_fingerprint(config, tags, output_language=output_language)
        payload, chunk_count = _generate_summary(
            config, plan, tags,
            output_language=output_language,
            source_language=source_language,
            transport=transport, llm_call=llm_call,
        )
    except InputTooLarge as exc:
        _finish_attempt_failure(
            attempt, recording, AttemptOutcome.INPUT_TOO_LARGE, "input_too_large",
            str(exc), output_language=output_language,
            transcript=transcript, section=section,
        )
        return _failed(recording, "input_too_large", kept_current=target_has_active)
    except llm_service.LLMError as exc:
        message = type(exc).__name__
        if isinstance(exc, llm_service.LLMHTTPError):
            message = f"HTTP {exc.status_code}"
        _finish_attempt_failure(
            attempt, recording, _outcome_for(exc), exc.code, message,
            output_language=output_language,
            transcript=transcript, section=section,
        )
        return _failed(recording, exc.code, kept_current=target_has_active)

    summary = persist_summary(
        recording=recording,
        transcript=transcript,
        section=section,
        attempt=attempt,
        payload=payload,
        output_language=output_language,
        is_default=is_default,
        model_id=config.llm.model,
        base_url=config.llm.base_url,
        prompt_version=PROMPT_IMPLEMENTATION_VERSION,
        fingerprint=fingerprint,
        chunk_count=chunk_count,
        input_characters=plan.input_characters,
        limits_used=_limits_used(config),
        generation_mode=generation_mode,
    )

    # Persist the detected source language using the canonical policy —
    # never raw model casing. Malformed values are never persisted.
    if not transcript.language_observed and payload.get("language"):
        detected_lang = languages.canonicalize_language(payload["language"])
        if detected_lang:
            transcript.language_observed = detected_lang
            transcript.language_observed_verified_by = "llm_detection"
            transcript.language_observed_verified_at = timezone.now()
            transcript.save(update_fields=[
                "language_observed", "language_observed_verified_by",
                "language_observed_verified_at",
            ])
            # Step 5A.3: this late committed write also changes the
            # derived default; its own (cheap, idempotent) sync covers it.
            schedule_recording_sync([recording.pk])

    return {
        "recording_id": recording.pk,
        "result": "summarized",
        "summary_id": summary.pk,
        "regeneration": existing_active is not None,
        "output_language": output_language,
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


# ---------------------------------------------------------------------------
# Language correction service
#
# Language family normalization lives in workflow.services.languages
# (single documented policy) and is re-exported at the top of this
# module for backwards compatibility.
# ---------------------------------------------------------------------------


def set_transcript_language(
    recording: Recording,
    language_code: str,
    *,
    transport=None,
    llm_call=None,
) -> dict:
    """Atomically set the source language on the active transcript.

    Must be called under the caller's pipeline lock (the service does
    NOT acquire the lock itself — no nested/self locking). Updates
    transcript provenance, derives old/new default output languages,
    and reconciles the new default's variant state and the Recording
    tuple through the centralized reconciler, using exact matching
    evidence from the database:

    - no Summary / no matching failure → ``missing``;
    - active Summary / no newer failure → ``current``;
    - no Summary / matching failure → ``failed``;
    - active Summary / newer matching failed regeneration →
      ``current`` + ``regeneration_failed`` (recency by attempt
      ordinal);
    - failures belonging to the old default, an optional language or a
      historical transcript are never reused (exact provenance match).

    Existing summaries are preserved when their language becomes
    optional. Returns a result dict with old/new source and default.
    """
    code = languages.canonicalize_language(language_code)
    if not code:
        raise ConfigError(f"invalid or unsupported language code: {language_code!r}")

    now = timezone.now()
    with transaction.atomic():
        rec = Recording.objects.select_for_update().get(pk=recording.pk)
        transcript = rec.transcripts.filter(is_active=True).first()
        if transcript is None:
            raise ConfigError(f"no active transcript for recording {rec.pk}")

        section = transcript.sections.filter(ordinal=0).first()
        if section is None:
            raise ConfigError(f"active transcript has no ordinal-0 section")

        old_source = transcript.language_observed or ""
        old_default = resolve_default_language(transcript)

        # Update transcript source language with user provenance
        transcript.language_observed = code
        transcript.language_observed_verified_by = "user"
        transcript.language_observed_verified_at = now
        transcript.save(update_fields=[
            "language_observed", "language_observed_verified_by",
            "language_observed_verified_at",
        ])

        new_default = resolve_default_language(transcript)

        # Reconcile the new default's variant state and the
        # Recording-level tuple from the database (exact evidence).
        variant_state_service.reconcile_variant_state(
            recording=rec,
            transcript=transcript,
            section=section,
            output_language=new_default,
        )
        rec.refresh_from_db()

        # Step 5A.3: the corrected source language changes the derived
        # default output language (metadata title chain) for this
        # recording's index documents.
        schedule_recording_sync([rec.pk])

        return {
            "recording_id": rec.pk,
            "transcript_id": transcript.pk,
            "old_source_language": old_source or "(not detected)",
            "new_source_language": code,
            "old_default_output": old_default,
            "new_default_output": new_default,
            "default_summary_status": rec.summary_status,
        }


@dataclass
class SourceLanguageDetectionResult:
    """Outcome of an explicit-Original source-language detection.

    ``language`` is the canonical source language on success (None on
    failure). ``error_code`` is the stable failure category on failure
    (None on success) — the SAME code stored on the durable attempt and
    surfaced by the CLI/Web layers; the attempt remains the source of
    truth.
    """

    language: str | None
    attempt: ProcessingAttempt
    error_code: str | None


def _detect_source_language_with_attempt(
    config: AppConfig,
    recording: Recording,
    transcript: Transcript,
    section: Section,
    *,
    transport=None,
    llm_call=None,
) -> SourceLanguageDetectionResult:
    """Detect source language, creating a durable attempt in every case.

    Returns a :class:`SourceLanguageDetectionResult`. On failure, the
    finished ProcessingAttempt carries the actual failure category as a
    stable error code (``request_too_large`` is its own category —
    never conflated with invalid output). The attempt's
    ``context_json`` durably proves the exact scope (``transcript_id``,
    ``section_id``) and the detection nature (``language_detection``,
    single ``requested: "original"`` entry); no resolved language is
    faked, and no transcript excerpt, prompt, response, or secret is
    stored.
    """
    attempt = ProcessingAttempt.objects.create(
        recording=recording,
        stage=AttemptStage.SUMMARIZATION,
        ordinal=next_ordinal(recording, AttemptStage.SUMMARIZATION),
        model_id=config.llm.model,
        cli_args_json={
            "kind": "source_language_detection",
            "base_url": config.llm.base_url,
            "model": config.llm.model,
        },
        context_json={
            "language_detection": True,
            "requested": "original",
            "transcript_id": transcript.pk,
            "section_id": section.pk,
        },
    )

    try:
        detected, error = _detect_source_language(
            config, transcript, transport=transport, llm_call=llm_call,
        )
    except InputTooLarge:
        attempt.outcome = AttemptOutcome.INPUT_TOO_LARGE
        attempt.error_code = "request_too_large"
        attempt.error_message = sanitize_error("detector request too large")
        attempt.finished_at = timezone.now()
        attempt.save()
        return SourceLanguageDetectionResult(None, attempt, attempt.error_code)

    if detected:
        attempt.outcome = AttemptOutcome.SUCCESS
        attempt.error_code = ""
        attempt.error_message = ""
        attempt.finished_at = timezone.now()
        attempt.save()
        return SourceLanguageDetectionResult(detected, attempt, None)

    # Detection failed — create durable evidence with the actual error
    # category. Messages are type-level only (sanitized); no raw detail.
    if error is not None:
        attempt.outcome = _outcome_for(error)
        attempt.error_code = error.code
        attempt.error_message = sanitize_error(type(error).__name__)
    else:
        attempt.outcome = AttemptOutcome.INVALID_OUTPUT
        attempt.error_code = "source_language_unknown"
        attempt.error_message = sanitize_error("source language detection failed")
    attempt.finished_at = timezone.now()
    attempt.save()
    return SourceLanguageDetectionResult(None, attempt, attempt.error_code)


__all__ = [
    "PARSER_VERSION",
    "PROMPT_IMPLEMENTATION_VERSION",
    "InputTooLarge",
    "SummaryRelationError",
    "summarize_one",
    "summarize_pending",
    "persist_summary",
    "validate_final_payload",
    "validate_map_payload",
    "config_fingerprint",
    "reconcile_recording_summary_state",
    "resolve_default_language",
    "resolve_output_language",
    "is_chinese_family",
    "normalize_source_language",
    "canonical_source_for_output",
    "set_transcript_language",
]
