import json
import logging
from typing import Any

from .config import HYBRID_MODEL, OPENAI_API_KEY
from .intelligence import INTELLIGENCE_VERSION, normalize_suggested_reply
from .summarize_hybrid import HybridRefinementError, build_context_digest

logger = logging.getLogger(__name__)

_URGENCY = {"low", "medium", "high"}
_SENTIMENT = {"negative", "neutral", "positive"}
_REQUEST_TYPE = {"question", "approval", "task", "info", "other"}
_DUE_SIGNALS = [
    "by ",
    "before ",
    "deadline",
    "due",
    "eod",
    "end of day",
    "tomorrow",
    "today",
    "this week",
    "next week",
    "asap",
    "urgent",
]


def _as_input_text(s: str) -> dict[str, str]:
    return {"type": "input_text", "text": s}


def _clip_text(s: str, limit: int = 2000) -> str:
    text = (s or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _normalize_intelligence(
    payload: dict,
    deterministic_intel: dict,
    me_email: str | None = None,
    last_sender: dict | None = None,
    user_name: str | None = None,
) -> dict:
    normalized = dict(deterministic_intel)
    if not isinstance(payload, dict):
        raise HybridRefinementError("schema_error", "schema_validation_failed")

    version = payload.get("version")
    if isinstance(version, str) and version.strip():
        normalized["version"] = version.strip()
    else:
        normalized["version"] = INTELLIGENCE_VERSION

    urgency = str(payload.get("urgency", normalized["urgency"])).lower()
    normalized["urgency"] = urgency if urgency in _URGENCY else normalized["urgency"]

    sentiment = str(payload.get("sentiment", normalized["sentiment"])).lower()
    normalized["sentiment"] = sentiment if sentiment in _SENTIMENT else normalized["sentiment"]

    request_type = str(payload.get("request_type", normalized["request_type"])).lower()
    normalized["request_type"] = request_type if request_type in _REQUEST_TYPE else normalized["request_type"]

    needs_reply = payload.get("needs_reply", normalized["needs_reply"])
    normalized["needs_reply"] = bool(needs_reply)

    reply = payload.get("suggested_reply")
    if normalized["needs_reply"] and isinstance(reply, dict):
        sub = str(reply.get("subject") or "").strip()
        body = str(reply.get("body_text") or "").strip()
        if sub or body:
            normalized["suggested_reply"] = normalize_suggested_reply(
                me_email=me_email,
                last_sender=last_sender,
                subject=sub,
                body_text=body,
                user_name=user_name,
                user_email=me_email,
            )
        else:
            normalized["suggested_reply"] = None
    elif normalized["needs_reply"]:
        normalized["suggested_reply"] = normalized.get("suggested_reply")
    else:
        normalized["suggested_reply"] = None

    entities = []
    for row in payload.get("entities", []) or []:
        if not isinstance(row, dict):
            continue
        e_type = str(row.get("type") or "").strip()[:80]
        name = str(row.get("name") or "").strip()[:200]
        if e_type and name:
            entities.append({"type": e_type, "name": name})
    if entities:
        normalized["entities"] = entities[:30]

    deadlines = []
    for row in payload.get("deadlines", []) or []:
        if not isinstance(row, dict):
            continue
        date = row.get("date")
        if date is not None:
            date = str(date)[:120]
        text = str(row.get("text") or "").strip()[:300]
        if not any(sig in text.lower() for sig in _DUE_SIGNALS):
            continue
        conf = row.get("confidence")
        try:
            conf_f = float(conf)
        except Exception:
            conf_f = 0.5
        conf_f = max(0.0, min(1.0, conf_f))
        if text:
            deadlines.append({"date": date, "text": text, "confidence": conf_f})
    if deadlines:
        normalized["deadlines"] = deadlines[:20]

    conf = payload.get("confidence", normalized["confidence"])
    try:
        conf_f = float(conf)
    except Exception:
        conf_f = float(normalized["confidence"])
    normalized["confidence"] = max(0.0, min(1.0, conf_f))

    flags = []
    for item in payload.get("safety_flags", []) or []:
        if isinstance(item, str) and item.strip():
            flags.append(item.strip()[:80])
    normalized["safety_flags"] = flags[:20]

    return normalized


def refine_intelligence_hybrid(
    deterministic_intel: dict,
    thread_summary: dict,
    me_email: str | None = None,
    last_sender: dict | None = None,
    user_name: str | None = None,
) -> dict:
    if not OPENAI_API_KEY or not HYBRID_MODEL:
        return deterministic_intel

    try:
        from openai import APIError, AuthenticationError, OpenAI, RateLimitError
    except Exception as exc:
        logger.warning("intelligence_hybrid_import_failed exc=%s", exc.__class__.__name__)
        raise HybridRefinementError("llm_error", "openai_call_failed", error_message=(str(exc) or "")[:300])

    digest = build_context_digest(thread_summary)
    user_payload = {
        "deterministic_intelligence": deterministic_intel,
        "context_digest": _clip_text(digest, 2000),
        "me_email": me_email,
        "last_sender": last_sender,
    }
    schema = {
        "type": "object",
        "properties": {
            "version": {"type": "string"},
            "urgency": {"type": "string"},
            "sentiment": {"type": "string"},
            "request_type": {"type": "string"},
            "needs_reply": {"type": "boolean"},
            "suggested_reply": {
                "type": ["object", "null"],
                "properties": {
                    "subject": {"type": "string"},
                    "body_text": {"type": "string"},
                },
                "required": ["subject", "body_text"],
                "additionalProperties": False,
            },
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["type", "name"],
                    "additionalProperties": False,
                },
            },
            "deadlines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": ["string", "null"]},
                        "text": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["date", "text", "confidence"],
                    "additionalProperties": False,
                },
            },
            "confidence": {"type": "number"},
            "safety_flags": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "version",
            "urgency",
            "sentiment",
            "request_type",
            "needs_reply",
            "suggested_reply",
            "entities",
            "deadlines",
            "confidence",
            "safety_flags",
        ],
        "additionalProperties": False,
    }

    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        resp = client.responses.create(
            model=HYBRID_MODEL,
            input=[
                {"role": "system", "content": [_as_input_text("Refine intelligence JSON only. Keep schema exact.")]},
                {"role": "user", "content": [_as_input_text(json.dumps(user_payload, ensure_ascii=False))]},
            ],
            temperature=0.2,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "intelligence_refinement",
                    "strict": True,
                    "schema": schema,
                }
            },
        )
    except AuthenticationError as exc:
        raise HybridRefinementError("llm_error", "openai_auth_failed", error_message=(str(exc) or "")[:300])
    except RateLimitError as exc:
        raise HybridRefinementError("llm_error", "openai_rate_limited", error_message=(str(exc) or "")[:300])
    except APIError as exc:
        raise HybridRefinementError("llm_error", "openai_api_error", error_message=(str(exc) or "")[:300])
    except Exception as exc:
        raise HybridRefinementError("llm_error", "openai_call_failed", error_message=(str(exc) or "")[:300])

    raw = getattr(resp, "output_text", None)
    if not isinstance(raw, str) or not raw.strip():
        raise HybridRefinementError("schema_error", "invalid_json")
    try:
        payload = json.loads(raw)
    except Exception:
        raise HybridRefinementError("schema_error", "invalid_json")

    return _normalize_intelligence(
        payload,
        deterministic_intel,
        me_email=me_email,
        last_sender=last_sender,
        user_name=user_name,
    )


def refine_triage_hybrid(
    baseline: dict,
    context_summary: dict,
    me_email: str | None = None,
    last_sender: dict | None = None,
    user_name: str | None = None,
) -> dict:
    if not OPENAI_API_KEY or not HYBRID_MODEL:
        return baseline

    try:
        from openai import APIError, AuthenticationError, OpenAI, RateLimitError
    except Exception as exc:
        raise HybridRefinementError("llm_error", "openai_call_failed", error_message=(str(exc) or "")[:300])

    digest = build_context_digest(context_summary)
    schema = {
        "type": "object",
        "properties": {
            "needs_reply": {"type": "boolean"},
            "urgency": {"type": "string"},
            "category": {"type": "string"},
            "next_action": {"type": "string"},
            "suggested_reply": {
                "type": ["object", "null"],
                "properties": {
                    "subject": {"type": "string"},
                    "body_text": {"type": "string"},
                },
                "required": ["subject", "body_text"],
                "additionalProperties": False,
            },
            "confidence": {"type": "number"},
        },
        "required": ["needs_reply", "urgency", "category", "next_action", "suggested_reply", "confidence"],
        "additionalProperties": False,
    }
    payload = {
        "baseline": baseline,
        "me_email": me_email,
        "last_sender": last_sender,
        "context_digest": _clip_text(digest, 1800),
    }
    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        resp = client.responses.create(
            model=HYBRID_MODEL,
            input=[
                {"role": "system", "content": [_as_input_text("Refine triage JSON only. Keep schema exact.")]},
                {"role": "user", "content": [_as_input_text(json.dumps(payload, ensure_ascii=False))]},
            ],
            temperature=0.2,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "triage_refinement",
                    "strict": True,
                    "schema": schema,
                }
            },
        )
    except AuthenticationError as exc:
        raise HybridRefinementError("llm_error", "openai_auth_failed", error_message=(str(exc) or "")[:300])
    except RateLimitError as exc:
        raise HybridRefinementError("llm_error", "openai_rate_limited", error_message=(str(exc) or "")[:300])
    except APIError as exc:
        raise HybridRefinementError("llm_error", "openai_api_error", error_message=(str(exc) or "")[:300])
    except Exception as exc:
        raise HybridRefinementError("llm_error", "openai_call_failed", error_message=(str(exc) or "")[:300])

    raw = getattr(resp, "output_text", None)
    if not isinstance(raw, str) or not raw.strip():
        raise HybridRefinementError("schema_error", "invalid_json")
    try:
        refined = json.loads(raw)
    except Exception:
        raise HybridRefinementError("schema_error", "invalid_json")
    if not isinstance(refined, dict):
        raise HybridRefinementError("schema_error", "schema_validation_failed")

    out = dict(baseline)
    out["needs_reply"] = bool(refined.get("needs_reply", out.get("needs_reply", False)))
    urgency = str(refined.get("urgency", out.get("urgency", "low"))).lower()
    out["urgency"] = urgency if urgency in {"low", "medium", "high"} else out.get("urgency", "low")
    category = str(refined.get("category", out.get("category", "other"))).lower()
    if category in {"action_required", "awaiting_reply", "fyi", "newsletter", "spam", "other"}:
        out["category"] = category
    next_action = str(refined.get("next_action", out.get("next_action", ""))).strip()
    if next_action:
        out["next_action"] = next_action[:240]
    try:
        conf = float(refined.get("confidence", out.get("confidence", 0.6)))
    except Exception:
        conf = float(out.get("confidence", 0.6))
    out["confidence"] = max(0.0, min(1.0, conf))
    reply = refined.get("suggested_reply")
    if out["needs_reply"] and isinstance(reply, dict):
        out["suggested_reply"] = normalize_suggested_reply(
            me_email=me_email,
            last_sender=last_sender,
            subject=str(reply.get("subject") or out.get("subject") or ""),
            body_text=str(reply.get("body_text") or ""),
            user_name=user_name,
            user_email=me_email,
        )
    elif not out["needs_reply"]:
        out["suggested_reply"] = None
    return out
