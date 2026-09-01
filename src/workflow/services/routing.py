"""Sample-based language routing.

Pipeline per recording:
  1. extract beginning/middle/end samples (data/temp only, cleaned up)
  2. transcribe each sample with the candidate profiles (--no-speakers)
  3. compute deterministic heuristic evidence
  4. ask the oMLX LLM to classify the labelled candidate transcripts
  5. fuse evidence; auto-route high-confidence results, otherwise
     needs_review

Honest limitation: automatic Cantonese-vs-Mandarin routing cannot be
claimed reliable until evaluated on real labelled recordings. Script
(Simplified/Traditional) ratios are weak supporting evidence only - the
apple:zh-HK / apple:zh-CN models may produce Traditional/Simplified
output because of their locale regardless of the spoken dialect, so
script choice can describe the model rather than the source. The
deciding zh evidence is colloquial vocabulary markers plus the
classifier; near-ties always go to needs_review (zh_ambiguous).

When the classifier is unavailable or invalid, a conservative
deterministic heuristic gate (``heuristic_auto_route`` config) may
auto-route when ALL independent conditions hold: family verdict
chinese, zh verdict matches the target without ambiguity, minimum CJK
ratio, minimum marker score for the target, dominance of the target
score over opposing marker scores, a low absolute ceiling on opposing
scores, and sufficient non-silent sample coverage. Scores are
uncalibrated evidence, never probabilities. Everything weaker,
ambiguous, contradictory, or incomplete stays needs_review.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from brainlib.config import AppConfig, HeuristicAutoRouteConfig
from workflow.services import audiosamples
from workflow.services.audiosamples import SampleExtractionError

logger = logging.getLogger(__name__)

ROUTER_VERSION = "2"
HEURISTIC_GATE_VERSION = "1"

ROUTE_CANTONESE = "cantonese"
ROUTE_MANDARIN = "mandarin"
ROUTE_EUROPEAN = "european"
ROUTE_UNCERTAIN = "uncertain"

# Stable reason codes surfaced in RoutingDecision.reason_code.
REASON_AUTO_CONFIDENT = "auto_confident"
REASON_AUTO_CONFIDENT_HEURISTIC_INVALID = "auto_confident_heuristic_classifier_invalid"
REASON_AUTO_CONFIDENT_HEURISTIC_UNAVAILABLE = "auto_confident_heuristic_classifier_unavailable"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_ZH_AMBIGUOUS = "zh_ambiguous"
REASON_CANDIDATES_DISAGREE = "candidates_disagree"
REASON_CLASSIFIER_UNAVAILABLE = "classifier_unavailable"
REASON_CLASSIFIER_INVALID = "classifier_invalid"
REASON_SAMPLING_FAILED = "sampling_failed"
REASON_ROUTING_DISABLED = "routing_disabled"
REASON_CONTRADICTORY = "contradictory_evidence"
REASON_TOO_SHORT = "too_short"
REASON_SILENT = "silent_audio"

# Classifier request state machine (finite; no loops):
#   1. structured request (response_format json_schema)
#      -> capability rejection (HTTP 400/422 explicitly naming
#         response_format/json_schema as unsupported/unknown/unexpected)
#         allows exactly one plain initial request
#      -> HTTP-successful but schema-invalid content allows exactly one
#         repair request
#   2. plain initial request (only via capability rejection)
#   3. repair request (only after schema-invalid structured response)
MAX_CLASSIFIER_CALLS = 3
CLASSIFIER_CALL_STRUCTURED = "structured"
CLASSIFIER_CALL_PLAIN = "plain"
CLASSIFIER_CALL_REPAIR = "repair"

# Capability-rejection classification: ALL three must hold (HTTP
# 400/422; explicit response_format/json_schema mention; explicit
# unsupported/unknown/unexpected-parameter semantics). Generic
# "unexpected"/"unknown parameter" text alone is insufficient.
_CAPABILITY_STATUS_CODES = (400, 422)
_CAPABILITY_PARAM_PATTERNS = ("response_format", "responseformat", "json_schema")
_CAPABILITY_SEMANTIC_PATTERNS = (
    "unsupported",
    "unknown parameter",
    "unexpected parameter",
    "unrecognized parameter",
    "not supported",
    "invalid parameter",
)

# Bounded tolerance for model-output wrappers: the leading think block
# may not exceed this many characters.
MAX_THINK_BLOCK_CHARS = 10000

# Cantonese colloquial vocabulary/grammar markers (both scripts where relevant).
CANTONESE_MARKERS = ["係", "唔", "咗", "喺", "嘅", "冇", "佢", "嗰", "嚟", "乜", "點", "畀", "攞", "啲", "噉"]
# Mandarin-specific colloquial markers (both script variants).
MANDARIN_MARKERS = ["吗", "嗎", "们", "們", "什么", "什麼", "怎么", "怎麼", "哪儿", "哪兒", "干嘛", "幹嘛", "咱"]

# Weak supporting evidence only: representative simplified-only vs
# traditional-only characters. NEVER decisive on its own.
_SIMPLIFIED_ONLY = "们这说对吗么没车东学给语读见谓语让证"
_TRADITIONAL_ONLY = "們這說對嗎麼沒車東學給語讀見謂語讓證"

CLASSIFIER_TIMEOUT = 120


@dataclass
class RoutingOutcome:
    route: str  # cantonese|mandarin|european|uncertain
    profile_name: str | None
    model_id: str | None
    language_arg: str | None
    method: str  # automatic|manual
    confidence: float | None
    reason_code: str
    evidence: dict = field(default_factory=dict)
    ready_to_transcribe: bool = False


def cjk_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    cjk = sum(1 for c in chars if "\u4e00" <= c <= "\u9fff")
    return cjk / len(chars)


def script_ratio(text: str) -> float:
    """Fraction of traditional-only vs simplified-only marker characters.

    WEAK evidence: zh-HK/zh-CN models may output their locale's script
    regardless of the spoken dialect. Never used as the deciding signal.
    """
    traditional = sum(text.count(c) for c in _TRADITIONAL_ONLY)
    simplified = sum(text.count(c) for c in _SIMPLIFIED_ONLY)
    total = traditional + simplified
    if total == 0:
        return 0.5
    return traditional / total


def marker_score(text: str, markers: list[str]) -> float:
    """Marker hits per 100 characters (both scripts counted)."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    hits = sum(text.count(m) for m in markers)
    return hits * 100.0 / len(chars)


def nonsense_ratio(text: str) -> float:
    """Heuristic degenerate-output measure: repeated 1-2 char token share."""
    tokens = [t for t in re.split(r"\s+", text) if t]
    if not tokens:
        return 1.0
    degenerate = sum(1 for t in tokens if len(t) <= 2 and not any("\u4e00" <= c <= "\u9fff" for c in t))
    return degenerate / len(tokens)


def _zh_family_evidence(zh_hk_text: str, zh_cn_text: str) -> dict:
    hk_cantonese = marker_score(zh_hk_text, CANTONESE_MARKERS)
    hk_mandarin = marker_score(zh_hk_text, MANDARIN_MARKERS)
    cn_cantonese = marker_score(zh_cn_text, CANTONESE_MARKERS)
    cn_mandarin = marker_score(zh_cn_text, MANDARIN_MARKERS)
    return {
        "zh_hk_cantonese_score": round(hk_cantonese, 2),
        "zh_hk_mandarin_score": round(hk_mandarin, 2),
        "zh_cn_cantonese_score": round(cn_cantonese, 2),
        "zh_cn_mandarin_score": round(cn_mandarin, 2),
        "zh_hk_script_traditional_ratio": round(script_ratio(zh_hk_text), 3),
        "zh_cn_script_traditional_ratio": round(script_ratio(zh_cn_text), 3),
    }


def _zh_verdict(evidence: dict) -> tuple[str, bool]:
    """Decide cantonese vs mandarin from zh candidate evidence.

    Returns (route, is_ambiguous). Cantonese evidence = cantonese
    markers in the zh-HK candidate relative to Mandarin markers;
    Mandarin evidence likewise for the zh-CN candidate. Script ratio is
    only a small tiebreaker, never decisive.
    """
    hk_score = evidence["zh_hk_cantonese_score"] + 0.1 * evidence["zh_hk_script_traditional_ratio"]
    cn_score = evidence["zh_cn_mandarin_score"] + 0.1 * (1.0 - evidence["zh_cn_script_traditional_ratio"])
    if hk_score <= 0 and cn_score <= 0:
        return ROUTE_UNCERTAIN, True
    if hk_score >= 2.0 * max(cn_score, 0.001):
        return ROUTE_CANTONESE, False
    if cn_score >= 2.0 * max(hk_score, 0.001):
        return ROUTE_MANDARIN, False
    return ROUTE_UNCERTAIN, True


def _family_verdict(parakeet_text: str, zh_text: str) -> str:
    """european vs chinese-family based on candidate output shapes."""
    parakeet_cjk = cjk_ratio(parakeet_text)
    zh_cjk = cjk_ratio(zh_text)
    if zh_cjk >= 0.25 and parakeet_cjk < 0.25:
        return "chinese"
    if parakeet_cjk < 0.25 and nonsense_ratio(parakeet_text) < 0.5:
        return "european"
    if zh_cjk >= 0.25:
        return "chinese"
    return "uncertain"


def _excerpt(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text[:limit]


def heuristic_gate_fingerprint(cfg: HeuristicAutoRouteConfig) -> str:
    """Stable bounded fingerprint of the resolved gate settings."""
    canonical = json.dumps(
        {
            "enabled": cfg.enabled,
            "min_non_silent_windows": cfg.min_non_silent_windows,
            "min_cjk_ratio": cfg.min_cjk_ratio,
            "cantonese_enabled": cfg.cantonese_enabled,
            "cantonese_min_score": cfg.cantonese_min_score,
            "mandarin_enabled": cfg.mandarin_enabled,
            "mandarin_min_score": cfg.mandarin_min_score,
            "dominance_ratio": cfg.dominance_ratio,
            "max_opposing_score": cfg.max_opposing_score,
            "gate_version": HEURISTIC_GATE_VERSION,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def evaluate_heuristic_gate(evidence: dict, cfg: HeuristicAutoRouteConfig) -> tuple[dict, str | None]:
    """Conservative deterministic auto-route gate (classifier failure only).

    Returns ``(bounded gate evidence, route | None)``. The route is
    non-None only when ALL independent checks hold for EXACTLY ONE
    enabled Chinese family: chinese family verdict, unambiguous zh
    verdict for the target, minimum CJK ratio, minimum target marker
    score, dominance of the target score over opposing marker scores
    (ratio check), a separate low absolute ceiling on opposing scores,
    and sufficient non-silent window coverage. Scores are uncalibrated
    evidence, never probabilities. Cantonese and Mandarin use
    independent thresholds and kill switches; European has no gate.
    """
    windows = evidence.get("windows", []) or []
    non_silent = sum(1 for window in windows if not window.get("silent"))
    window_count = int(evidence.get("window_count") or 0)
    zh_route = evidence.get("zh_verdict")
    cjk = float(evidence.get("zh_cjk_ratio") or 0.0)
    opposing_eps = 0.01  # keeps the dominance ratio meaningful vs a 0.0 opponent

    detail: dict = {
        "gate_version": HEURISTIC_GATE_VERSION,
        "config_fingerprint": heuristic_gate_fingerprint(cfg),
        "enabled": cfg.enabled,
        "family_ok": evidence.get("family_verdict") == "chinese",
        "zh_not_ambiguous": evidence.get("zh_ambiguous") is False,
        "coverage_ok": window_count >= 2 and non_silent >= cfg.min_non_silent_windows,
        "min_cjk_ok": cjk >= cfg.min_cjk_ratio,
        "window_count": window_count,
        "non_silent_windows": non_silent,
        "zh_cjk_ratio": round(cjk, 3),
    }

    if not cfg.enabled:
        return detail, None

    def family_candidate(route: str, verdict_ok: bool, min_score: float, target_key: str, opposing_keys: list[str]) -> dict:
        target = float(evidence.get(target_key) or 0.0)
        opposing = max(float(evidence.get(key) or 0.0) for key in opposing_keys)
        return {
            "route": route,
            "verdict_ok": verdict_ok,
            "min_score_ok": target >= min_score,
            "dominance_ok": target >= cfg.dominance_ratio * max(opposing, opposing_eps),
            "opposing_ok": opposing <= cfg.max_opposing_score,
            "target_score": round(target, 2),
            "opposing_score": round(opposing, 2),
        }

    candidates = []
    if cfg.cantonese_enabled:
        candidates.append(
            family_candidate(
                ROUTE_CANTONESE,
                zh_route == ROUTE_CANTONESE,
                cfg.cantonese_min_score,
                "zh_hk_cantonese_score",
                ["zh_hk_mandarin_score", "zh_cn_mandarin_score"],
            )
        )
    if cfg.mandarin_enabled:
        candidates.append(
            family_candidate(
                ROUTE_MANDARIN,
                zh_route == ROUTE_MANDARIN,
                cfg.mandarin_min_score,
                "zh_cn_mandarin_score",
                ["zh_hk_cantonese_score", "zh_cn_cantonese_score"],
            )
        )
    detail["candidates"] = candidates

    if not (detail["family_ok"] and detail["zh_not_ambiguous"] and detail["coverage_ok"] and detail["min_cjk_ok"]):
        return detail, None
    passing = [
        candidate
        for candidate in candidates
        if candidate["verdict_ok"] and candidate["min_score_ok"] and candidate["dominance_ok"] and candidate["opposing_ok"]
    ]
    detail["candidates_passing"] = len(passing)
    if len(passing) != 1:
        return detail, None
    detail["route"] = passing[0]["route"]
    return detail, passing[0]["route"]


_CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {
            "type": "string",
            "enum": [ROUTE_CANTONESE, ROUTE_MANDARIN, ROUTE_EUROPEAN, ROUTE_UNCERTAIN],
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason_code": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": ["route", "confidence"],
    "additionalProperties": False,
}


def _capability_rejection(status: int, body_text: str) -> bool:
    """True only for an explicit response_format/json_schema capability rejection.

    Requires ALL three: HTTP 400/422; explicit mention of
    response_format/json_schema; explicit unsupported/unknown/unexpected
    -parameter semantics. Generic "unexpected"/"unknown parameter" text
    alone is insufficient. The body text is never stored or logged.
    """
    if status not in _CAPABILITY_STATUS_CODES:
        return False
    text = (body_text or "")[:4000].lower()
    has_param = any(pattern in text for pattern in _CAPABILITY_PARAM_PATTERNS)
    has_semantic = any(pattern in text for pattern in _CAPABILITY_SEMANTIC_PATTERNS)
    return has_param and has_semantic


class ClassifierResult(dict):
    """Successful classifier outcome: the validated classification mapping
    plus bounded locally generated ``diagnostics``.

    Being a dict subclass keeps mapping access (``result["route"]``),
    JSON serialization, and injected-classifier-callable compatibility.
    Diagnostics are generated by Brain only — never accepted from model
    output — and stay bounded to stable codes, counts, and booleans.
    """

    def __init__(self, classification: dict, diagnostics: dict) -> None:
        super().__init__(classification)
        self.diagnostics = diagnostics


class RoutingUnavailable(Exception):
    def __init__(self, message: str, diagnostics: dict | None = None) -> None:
        super().__init__(message)
        # Bounded, stable diagnostic metadata only (never a response body).
        self.diagnostics = diagnostics or {}


class RoutingInvalid(Exception):
    def __init__(self, message: str, diagnostics: dict | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def classify_with_omlx(
    config: AppConfig,
    candidates: dict[str, str],
    timeout: float | None = None,
    transport=None,
) -> dict:
    """Ask the configured oMLX endpoint to classify labelled candidates.

    ``candidates`` maps labels (zh_hk, zh_cn, european) to bounded
    excerpts; the prompt contains three distinct labelled blocks.
    Returns the validated dict {route, confidence, reason_code, evidence}.

    Finite request state machine (max three calls, no loops):
    1. structured request (``response_format`` json_schema). An HTTP
       400/422 that explicitly and safely classifies as an unsupported
       response_format/json_schema capability permits exactly ONE plain
       initial request. Any HTTP-successful but schema-invalid content
       permits exactly ONE repair request (stable validation category
       only — the raw previous output is never echoed back).
    2. plain initial request (only via capability rejection).
    3. repair request (only after schema-invalid structured response).

    HTTP/connectivity/timeout problems raise :class:`RoutingUnavailable`;
    malformed or invalid output raises :class:`RoutingInvalid`. Both carry
    bounded ``diagnostics`` (call count, capability, validation category)
    so routing evidence can retain them; response bodies, headers, API
    keys, prompts, and model output never appear in raised messages or
    diagnostics.
    """
    if not config.llm.model.strip():
        raise RoutingUnavailable("no classifier model configured (llm.model is blank)")
    api_key = config.api_key_for(config.llm.api_key_env)
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def block(label: str) -> str:
        return f"<{label}>{candidates.get(label, '')}</{label}>"

    prompt = (
        "You are a language router for audio transcripts. Three candidate "
        "transcripts of the same audio follow, produced by different models.\n"
        + block("zh_hk")
        + "\n"
        + block("zh_cn")
        + "\n"
        + block("european")
        + "\nDecide the spoken language: cantonese, mandarin, european "
        "(Finnish/English/other Latin-language speech, including mixed with English), "
        "or uncertain. Respond with ONLY a JSON object:\n"
        '{"route": "cantonese"|"mandarin"|"european"|"uncertain", '
        '"confidence": 0.0-1.0, "reason_code": "short_snake_case", '
        '"evidence": "at most 300 characters"}'
    )
    url = f"{config.llm.base_url.rstrip('/')}/chat/completions"
    timeout = timeout or CLASSIFIER_TIMEOUT

    structured_payload = {
        "model": config.llm.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 400,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "language_route", "strict": True, "schema": _CLASSIFIER_SCHEMA},
        },
    }
    plain_payload = {key: value for key, value in structured_payload.items() if key != "response_format"}

    diag: dict = {
        "classifier_calls": 0,
        "structured_output": "used",
        "classifier_validation": "",
        "calls": [],
        "repair_used": False,
    }

    def http_post(payload: dict) -> httpx.Response:
        client_kwargs = {"timeout": timeout}
        if transport is not None:
            client_kwargs["transport"] = transport
        with httpx.Client(**client_kwargs) as client:
            return client.post(url, json=payload, headers=headers)

    def http_error(exc: httpx.HTTPError) -> RoutingUnavailable:
        return RoutingUnavailable(f"endpoint error: {type(exc).__name__}", dict(diag))

    def envelope_content(response: httpx.Response) -> str:
        """Strict OpenAI-compatible envelope validation; returns content."""
        try:
            body = response.json()
        except ValueError:
            raise RoutingInvalid("invalid_envelope") from None
        if not isinstance(body, dict):
            raise RoutingInvalid("invalid_envelope")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RoutingInvalid("invalid_envelope")
        first = choices[0]
        if not isinstance(first, dict):
            raise RoutingInvalid("invalid_envelope")
        message = first.get("message")
        if not isinstance(message, dict):
            raise RoutingInvalid("invalid_envelope")
        content = message.get("content")
        if not isinstance(content, str):
            raise RoutingInvalid("invalid_envelope")
        return content

    def validate_content(content: str) -> dict:
        try:
            return _parse_classifier_json(content)
        except RoutingInvalid as exc:
            diag["classifier_validation"] = str(exc)[:64]
            raise

    def request_and_validate(payload: dict, kind: str) -> dict:
        """One plain/repair call; failures are terminal (no further requests)."""
        diag["classifier_calls"] += 1
        diag["calls"].append(kind)
        try:
            response = http_post(payload)
        except httpx.HTTPError as exc:
            raise http_error(exc) from exc
        if not 200 <= response.status_code < 300:
            raise RoutingUnavailable(f"endpoint http status {response.status_code}", dict(diag))
        try:
            content = envelope_content(response)
        except RoutingInvalid as exc:
            diag["classifier_validation"] = str(exc)[:64]
            raise RoutingInvalid(str(exc)[:64], dict(diag)) from None
        try:
            return validate_content(content)
        except RoutingInvalid as exc:
            raise RoutingInvalid(str(exc)[:64], dict(diag)) from None

    def repair_request(validation: str) -> dict:
        diag["repair_used"] = True
        repair_payload = {
            **plain_payload,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\nYour previous reply failed validation "
                        f"({validation}). Respond with ONLY the JSON object."
                    ),
                }
            ],
        }
        return request_and_validate(repair_payload, CLASSIFIER_CALL_REPAIR)

    # State 1: structured request.
    diag["classifier_calls"] += 1
    diag["calls"].append(CLASSIFIER_CALL_STRUCTURED)
    try:
        response = http_post(structured_payload)
    except httpx.HTTPError as exc:
        raise http_error(exc) from exc
    if 200 <= response.status_code < 300:
        try:
            content = envelope_content(response)
        except RoutingInvalid as exc:
            diag["classifier_validation"] = str(exc)[:64]
            repaired = repair_request(str(exc)[:64])
            return ClassifierResult(repaired, dict(diag))
        try:
            result = validate_content(content)
        except RoutingInvalid as exc:
            repaired = repair_request(str(exc)[:64])
            return ClassifierResult(repaired, dict(diag))
        return ClassifierResult(result, dict(diag))
    if _capability_rejection(response.status_code, response.text):
        # State 2: exactly one plain initial request; no further repair.
        diag["structured_output"] = "rejected_unsupported"
        result = request_and_validate(plain_payload, CLASSIFIER_CALL_PLAIN)
        return ClassifierResult(result, dict(diag))
    diag["structured_output"] = "rejected_error"
    raise RoutingUnavailable(f"endpoint http status {response.status_code}", dict(diag))


def _decode_single_object(text: str) -> dict:
    """Decode exactly one JSON object; stable rejection categories."""
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        raise RoutingInvalid("no_json_object")
    decoder = json.JSONDecoder()
    try:
        data, end = decoder.raw_decode(stripped)
    except ValueError:
        raise RoutingInvalid("invalid_json") from None
    remainder = stripped[end:].strip()
    if remainder:
        if remainder.startswith("{"):
            raise RoutingInvalid("multiple_objects")
        raise RoutingInvalid("trailing_commentary")
    if not isinstance(data, dict):
        raise RoutingInvalid("not_object")
    return data


def _parse_classifier_json(content: str) -> dict:
    """Restricted tolerant extraction + strict schema validation.

    Accepts exactly:
    - a pure JSON object;
    - content wholly enclosed in one JSON fence;
    - one bounded, closed leading ``<think>...</think>`` block followed
      immediately by one JSON object (no fence).

    Rejects arbitrary leading/trailing commentary, unclosed or oversized
    think blocks, and multiple JSON objects. Error messages are stable
    category codes; no model output is ever included in them.
    """
    text = (content or "").strip()
    think = re.match(r"^<think>(.*?)</think>\s*(.*)$", text, re.DOTALL)
    if think is not None:
        inner, rest = think.group(1), think.group(2)
        if len(inner) > MAX_THINK_BLOCK_CHARS:
            raise RoutingInvalid("think_block_too_large")
        if rest.strip().startswith("```"):
            raise RoutingInvalid("no_json_object")
        data = _decode_single_object(rest)
    else:
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        data = _decode_single_object(text)

    route = data.get("route")
    confidence = data.get("confidence")
    if route not in (ROUTE_CANTONESE, ROUTE_MANDARIN, ROUTE_EUROPEAN, ROUTE_UNCERTAIN):
        raise RoutingInvalid("invalid_route")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise RoutingInvalid("invalid_confidence")
    if not 0.0 <= float(confidence) <= 1.0:
        raise RoutingInvalid("confidence_out_of_range")
    return {
        "route": route,
        "confidence": float(confidence),
        "reason_code": str(data.get("reason_code", ""))[:64],
        "evidence": str(data.get("evidence", ""))[:300],
    }


def _classifier_failed_outcome(
    config: AppConfig,
    evidence: dict,
    category: str,
    exc: Exception,
) -> RoutingOutcome:
    """Classifier unavailable/invalid: conservative heuristic gate, else review.

    The gate is the ONLY deterministic auto-route path, and only for the
    Chinese family with overwhelming, internally consistent evidence.
    Bounded classifier diagnostics and the complete gate detail are
    preserved in evidence for later evaluation; the failure category is
    always kept visible in the reason code or evidence.
    """
    diagnostics = dict(getattr(exc, "diagnostics", None) or {})
    gate_detail, gated_route = evaluate_heuristic_gate(
        evidence, config.macwhisper.routing.heuristic_auto_route
    )
    merged = dict(evidence)
    merged["classifier_failure"] = category
    if diagnostics:
        merged["classifier_diagnostics"] = diagnostics
    merged["heuristic_gate"] = gate_detail

    if gated_route is not None:
        profile = config.macwhisper.routing.profiles.get(gated_route)
        if profile is not None and not profile.manual_only:
            reason = (
                REASON_AUTO_CONFIDENT_HEURISTIC_UNAVAILABLE
                if category == "unavailable"
                else REASON_AUTO_CONFIDENT_HEURISTIC_INVALID
            )
            return RoutingOutcome(
                route=gated_route,
                profile_name=profile.name,
                model_id=profile.model,
                language_arg=profile.language,
                method="automatic",
                confidence=None,
                reason_code=reason,
                evidence=merged,
                # Represents gate confidence; _apply_outcome applies the
                # routing.auto_transcribe policy (ready vs needs_review).
                ready_to_transcribe=True,
            )

    reason = REASON_CLASSIFIER_UNAVAILABLE if category == "unavailable" else REASON_CLASSIFIER_INVALID
    return RoutingOutcome(
        route=ROUTE_UNCERTAIN, profile_name=None, model_id=None, language_arg=None,
        method="automatic", confidence=None, reason_code=reason,
        evidence=merged,
    )


def route_recording(
    config: AppConfig,
    recording,
    source_path: Path,
    attempt_dir: Path,
    runner=None,
    classifier=None,
) -> RoutingOutcome:
    """Route one recording. Caller provides an attempt dir under
    data/temp and is responsible for cleanup (finally)."""
    mac = config.macwhisper
    routing_cfg = mac.routing
    profiles = routing_cfg.profiles

    # Disabled routing produces a review outcome BEFORE any expensive
    # work: no sample extraction, no MacWhisper subprocesses.
    if not routing_cfg.enabled:
        return RoutingOutcome(
            route=ROUTE_UNCERTAIN, profile_name=None, model_id=None, language_arg=None,
            method="automatic", confidence=None, reason_code=REASON_ROUTING_DISABLED,
            evidence={"routing": "disabled"},
        )

    candidates = [
        profiles["cantonese"],
        profiles["mandarin"],
        profiles["european"],
    ]

    try:
        bundle = audiosamples.extract_samples(Path(source_path), attempt_dir)
    except SampleExtractionError as exc:
        reason = REASON_TOO_SHORT if exc.reason_code == "too_short" else REASON_SAMPLING_FAILED
        return RoutingOutcome(
            route=ROUTE_UNCERTAIN, profile_name=None, model_id=None, language_arg=None,
            method="automatic", confidence=None, reason_code=reason,
            evidence={"sampling": exc.reason_code},
        )

    window_metrics = {
        "window_count": len(bundle.windows),
        "windows": [
            {"start_seconds": round(start_s, 2), "end_seconds": round(end_s, 2), "silent": silent}
            for (start_s, end_s), silent in zip(bundle.windows, bundle.window_silence)
        ],
    }

    if bundle.is_silent:
        return RoutingOutcome(
            route=ROUTE_UNCERTAIN, profile_name=None, model_id=None, language_arg=None,
            method="automatic", confidence=None, reason_code=REASON_SILENT,
            evidence={"duration_seconds": round(bundle.duration_seconds, 2), **window_metrics},
        )

    from workflow.services.transcription import run_mw_transcription

    # All candidate models transcribe the composite WAV, which contains
    # every extracted window in chronological order.
    composite = bundle.composite_path or (bundle.sample_paths[0] if bundle.sample_paths else None)
    if composite is None:
        return RoutingOutcome(
            route=ROUTE_UNCERTAIN, profile_name=None, model_id=None, language_arg=None,
            method="automatic", confidence=None, reason_code=REASON_SAMPLING_FAILED,
            evidence={"sampling": "no_windows", **window_metrics},
        )

    candidate_texts: dict[str, str] = {}
    candidate_ok: dict[str, bool] = {}
    for profile in candidates:
        text = run_mw_transcription(
            config=config,
            audio_path=composite,
            model_id=profile.model,
            language_arg=profile.language,
            speakers=False,
            runner=runner,
            timeout_seconds=600,
        )
        if text is None:
            candidate_ok[profile.name] = False
            candidate_texts[profile.name] = ""
        else:
            candidate_ok[profile.name] = True
            candidate_texts[profile.name] = text

    if not all(candidate_ok.values()):
        return RoutingOutcome(
            route=ROUTE_UNCERTAIN, profile_name=None, model_id=None, language_arg=None,
            method="automatic", confidence=None, reason_code=REASON_CANDIDATES_DISAGREE,
            evidence={"candidate_success": candidate_ok, **window_metrics},
        )

    evidence = _zh_family_evidence(candidate_texts["cantonese"], candidate_texts["mandarin"])
    evidence.update(
        {
            "parakeet_cjk_ratio": round(cjk_ratio(candidate_texts["european"]), 3),
            "zh_cjk_ratio": round(cjk_ratio(candidate_texts["cantonese"]), 3),
            "parakeet_nonsense_ratio": round(nonsense_ratio(candidate_texts["european"]), 3),
            "duration_seconds": round(bundle.duration_seconds, 2),
            "router_version": ROUTER_VERSION,
            **window_metrics,
        }
    )
    zh_route, zh_ambiguous = _zh_verdict(evidence)
    family = _family_verdict(candidate_texts["european"], candidate_texts["cantonese"])
    evidence["family_verdict"] = family
    evidence["zh_verdict"] = zh_route
    evidence["zh_ambiguous"] = zh_ambiguous

    # Bounded excerpts only; full candidate transcripts are never stored.
    evidence["excerpt_cantonese"] = _excerpt(candidate_texts["cantonese"])
    evidence["excerpt_mandarin"] = _excerpt(candidate_texts["mandarin"])
    evidence["excerpt_european"] = _excerpt(candidate_texts["european"])

    # Classifier candidates: distinct bounded labelled blocks.
    candidates_for_classifier = {
        "zh_hk": _excerpt(candidate_texts["cantonese"], 600),
        "zh_cn": _excerpt(candidate_texts["mandarin"], 600),
        "european": _excerpt(candidate_texts["european"], 600),
    }
    try:
        if classifier is None:
            classifier_result = classify_with_omlx(config, candidates_for_classifier)
        else:
            classifier_result = classifier(config, candidates_for_classifier)
    except RoutingUnavailable as exc:
        return _classifier_failed_outcome(config, evidence, "unavailable", exc)
    except RoutingInvalid as exc:
        return _classifier_failed_outcome(config, evidence, "invalid", exc)

    if isinstance(classifier_result, ClassifierResult):
        classification = dict(classifier_result)
        classifier_diagnostics = classifier_result.diagnostics
    else:
        # Compatibility adapter: injected classifier callables (used by
        # tests and future callers) return the validated classification
        # mapping directly. Bounded local diagnostics are synthesized so
        # evidence shape stays uniform; they never come from model output.
        classification = classifier_result
        classifier_diagnostics = {
            "classifier_calls": 1,
            "structured_output": "injected",
            "classifier_validation": "",
            "calls": ["injected"],
            "repair_used": False,
        }

    evidence["classifier"] = classification
    evidence["classifier_diagnostics"] = classifier_diagnostics
    classifier_route = classification["route"]
    confidence = classification["confidence"]

    # Family contradiction check.
    if family != "uncertain" and classifier_route != ROUTE_UNCERTAIN:
        classifier_family = "chinese" if classifier_route in (ROUTE_CANTONESE, ROUTE_MANDARIN) else "european"
        if classifier_family != family:
            return RoutingOutcome(
                route=ROUTE_UNCERTAIN, profile_name=None, model_id=None, language_arg=None,
                method="automatic", confidence=None, reason_code=REASON_CONTRADICTORY,
                evidence=evidence,
            )

    if classifier_route in (ROUTE_CANTONESE, ROUTE_MANDARIN):
        if zh_ambiguous:
            return RoutingOutcome(
                route=classifier_route, profile_name=None, model_id=None, language_arg=None,
                method="automatic", confidence=confidence, reason_code=REASON_ZH_AMBIGUOUS,
                evidence=evidence,
            )
        if zh_route != ROUTE_UNCERTAIN and zh_route != classifier_route:
            return RoutingOutcome(
                route=classifier_route, profile_name=None, model_id=None, language_arg=None,
                method="automatic", confidence=confidence, reason_code=REASON_ZH_AMBIGUOUS,
                evidence=evidence,
            )
        profile = profiles[classifier_route]
        auto = confidence >= routing_cfg.confidence_threshold
        return RoutingOutcome(
            route=classifier_route,
            profile_name=profile.name if auto else None,
            model_id=profile.model if auto else None,
            language_arg=profile.language if auto else None,
            method="automatic",
            confidence=confidence,
            reason_code=REASON_AUTO_CONFIDENT if auto else REASON_LOW_CONFIDENCE,
            evidence=evidence,
            ready_to_transcribe=auto,
        )

    if classifier_route == ROUTE_EUROPEAN:
        # European speech ALWAYS uses the `european` profile — never a
        # Chinese model via default_profile.
        profile = profiles["european"]
        auto = confidence >= routing_cfg.confidence_threshold
        return RoutingOutcome(
            route=ROUTE_EUROPEAN,
            profile_name=profile.name if auto else None,
            model_id=profile.model if auto else None,
            language_arg=profile.language if auto else None,
            method="automatic",
            confidence=confidence,
            reason_code=REASON_AUTO_CONFIDENT if auto else REASON_LOW_CONFIDENCE,
            evidence=evidence,
            ready_to_transcribe=auto,
        )

    return RoutingOutcome(
        route=ROUTE_UNCERTAIN, profile_name=None, model_id=None, language_arg=None,
        method="automatic", confidence=confidence, reason_code=REASON_LOW_CONFIDENCE,
        evidence=evidence,
    )
