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

When the classifier is unavailable or invalid, the result is
needs_review (no non-LLM auto-transcription fallback has been
evaluated).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from brainlib.config import AppConfig
from workflow.services import audiosamples
from workflow.services.audiosamples import SampleExtractionError

logger = logging.getLogger(__name__)

ROUTER_VERSION = "1"

ROUTE_CANTONESE = "cantonese"
ROUTE_MANDARIN = "mandarin"
ROUTE_EUROPEAN = "european"
ROUTE_UNCERTAIN = "uncertain"

# Stable reason codes surfaced in RoutingDecision.reason_code.
REASON_AUTO_CONFIDENT = "auto_confident"
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

    Mapping: HTTP/connectivity/timeout problems raise
    :class:`RoutingUnavailable`; malformed HTTP JSON, an invalid
    OpenAI-compatible envelope, or invalid classifier JSON/schema raise
    :class:`RoutingInvalid`. Response bodies, headers, API keys, and
    prompt contents never appear in raised messages.
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
    payload = {
        "model": config.llm.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 400,
    }
    url = f"{config.llm.base_url.rstrip('/')}/chat/completions"
    timeout = timeout or CLASSIFIER_TIMEOUT

    def parse_envelope(body) -> str:
        """Strict OpenAI-compatible envelope validation; returns content."""
        if not isinstance(body, dict):
            raise RoutingInvalid("response is not a JSON object")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RoutingInvalid("response has no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise RoutingInvalid("invalid choice")
        message = first.get("message")
        if not isinstance(message, dict):
            raise RoutingInvalid("choice has no message object")
        content = message.get("content")
        if not isinstance(content, str):
            raise RoutingInvalid("message content is not a string")
        return content

    def call() -> dict:
        client_kwargs = {"timeout": timeout}
        if transport is not None:
            client_kwargs["transport"] = transport
        with httpx.Client(**client_kwargs) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            try:
                body = response.json()
            except ValueError:
                raise RoutingInvalid("response is not valid JSON") from None
        return _parse_classifier_json(parse_envelope(body))

    last_error: Exception | None = None
    for _ in range(2):  # one retry on invalid output
        try:
            return call()
        except httpx.HTTPError as exc:
            raise RoutingUnavailable(f"endpoint error: {type(exc).__name__}") from exc
        except (KeyError, TypeError, IndexError, RoutingInvalid) as exc:
            last_error = exc
            continue
    raise RoutingInvalid(str(last_error) or "invalid classifier output")


def _parse_classifier_json(content: str) -> dict:
    text = content.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except ValueError:
        raise RoutingInvalid("not valid JSON") from None
    if not isinstance(data, dict):
        raise RoutingInvalid("not a JSON object")
    route = data.get("route")
    confidence = data.get("confidence")
    if route not in (ROUTE_CANTONESE, ROUTE_MANDARIN, ROUTE_EUROPEAN, ROUTE_UNCERTAIN):
        raise RoutingInvalid(f"invalid route value: {route!r}")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise RoutingInvalid("invalid confidence")
    if not 0.0 <= float(confidence) <= 1.0:
        raise RoutingInvalid("confidence out of range")
    return {
        "route": route,
        "confidence": float(confidence),
        "reason_code": str(data.get("reason_code", ""))[:64],
        "evidence": str(data.get("evidence", ""))[:300],
    }


class RoutingUnavailable(Exception):
    pass


class RoutingInvalid(Exception):
    pass


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
    except RoutingUnavailable:
        return RoutingOutcome(
            route=ROUTE_UNCERTAIN, profile_name=None, model_id=None, language_arg=None,
            method="automatic", confidence=None, reason_code=REASON_CLASSIFIER_UNAVAILABLE,
            evidence=evidence,
        )
    except RoutingInvalid:
        return RoutingOutcome(
            route=ROUTE_UNCERTAIN, profile_name=None, model_id=None, language_arg=None,
            method="automatic", confidence=None, reason_code=REASON_CLASSIFIER_INVALID,
            evidence=evidence,
        )

    evidence["classifier"] = classifier_result
    classifier_route = classifier_result["route"]
    confidence = classifier_result["confidence"]

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
