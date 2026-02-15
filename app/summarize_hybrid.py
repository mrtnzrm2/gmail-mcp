import json
import logging
import re
from typing import Any

from .config import (
    HYBRID_MAX_CONTEXT_CHARS,
    HYBRID_MODEL,
    HYBRID_SUMMARIZE_ENABLED,
    OPENAI_API_KEY,
)

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", flags=re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+", flags=re.IGNORECASE)
SIG_RE = re.compile(r"(?im)^\s*(--+|sent from\b|best regards\b|thanks\b|cheers\b).*$")
MAX_FIELD_CHARS = 500


class HybridRefinementError(Exception):
    def __init__(
        self,
        error_type: str,
        error_code: str,
        error_message: str | None = None,
        http_status: int | None = None,
    ):
        super().__init__(f"{error_type}:{error_code}")
        self.error_type = error_type
        self.error_code = error_code
        self.error_message = (error_message or "")[:300] or None
        self.http_status = http_status


def _clip(value: str, n: int) -> str:
    if len(value) <= n:
        return value
    return value[:n].rstrip() + "..."


def _trim(value: Any, max_len: int = MAX_FIELD_CHARS) -> str:
    return _clip(str(value or "").strip(), max_len)


def _redact(value: str) -> str:
    text = EMAIL_RE.sub("<email>", value or "")
    text = URL_RE.sub("<url>", text)
    lines: list[str] = []
    for line in text.splitlines():
        if SIG_RE.match(line):
            break
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_context_digest(deterministic_summary: dict[str, Any]) -> str:
    raw_context = str(deterministic_summary.get("context_block", "") or "")
    timeline = deterministic_summary.get("timeline") or []
    timeline_lines: list[str] = []
    for item in timeline[:12]:
        date = str(item.get("date", "") or "")
        frm = str(item.get("from", "") or "")
        summary = _clip(str(item.get("summary", "") or ""), 200)
        line = f"{date} | {frm} | {summary}".strip(" |")
        if line:
            timeline_lines.append(line)
    base = raw_context or "\n".join(timeline_lines)
    redacted = _redact(base)
    return _clip(redacted, max(200, HYBRID_MAX_CONTEXT_CHARS))


def _is_hybrid_available() -> bool:
    return HYBRID_SUMMARIZE_ENABLED and bool(OPENAI_API_KEY) and bool(HYBRID_MODEL)


def _extract_response_json(resp: Any) -> dict[str, Any]:
    # Preferred parsed output path.
    parsed = getattr(resp, "output_parsed", None)
    if isinstance(parsed, dict):
        return parsed

    output_text = getattr(resp, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return json.loads(output_text)

    # Robust fallback: inspect response output blocks for JSON-like content.
    for output_item in getattr(resp, "output", []) or []:
        for content_item in getattr(output_item, "content", []) or []:
            text = getattr(content_item, "text", None)
            if isinstance(text, str) and text.strip():
                return json.loads(text)
            content_json = getattr(content_item, "json", None)
            if isinstance(content_json, dict):
                return content_json
            arguments = getattr(content_item, "arguments", None)
            if isinstance(arguments, str) and arguments.strip():
                return json.loads(arguments)

    if isinstance(resp, dict):
        if isinstance(resp.get("output_text"), str) and resp["output_text"].strip():
            return json.loads(resp["output_text"])
        if isinstance(resp.get("output_parsed"), dict):
            return resp["output_parsed"]

    raise HybridRefinementError("schema_error", "invalid_json")


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = _trim(value)
        return [text] if text else []
    if isinstance(value, list):
        items: list[str] = []
        for x in value:
            if isinstance(x, str):
                s = _trim(x)
                if s:
                    items.append(s)
        return items
    return []


def _normalize_action_items(value: Any) -> list[dict]:
    if value is None:
        return []
    items: list[dict] = []
    if isinstance(value, str):
        text = _trim(value)
        return [{"owner": "unknown", "item": text, "due": None}] if text else []
    if not isinstance(value, list):
        return []
    for row in value:
        if isinstance(row, str):
            text = _trim(row)
            if text:
                items.append({"owner": "unknown", "item": text, "due": None})
            continue
        if not isinstance(row, dict):
            continue
        item = _trim(row.get("item"))
        if not item:
            continue
        owner = _trim(row.get("owner") or "unknown")
        due_raw = row.get("due")
        due = None if due_raw in (None, "") else _trim(due_raw)
        items.append({"owner": owner or "unknown", "item": item, "due": due})
    return items


def _normalize_timeline_overrides(value: Any) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    rows: list[dict] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        idx = row.get("index")
        text = _trim(row.get("summary"))
        if isinstance(idx, int) and text:
            rows.append({"index": idx, "summary": text})
    return rows


def normalize_refinement_payload_for_test(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    normalized = {
        "action_items": _normalize_action_items(payload.get("action_items")),
        "open_questions": _coerce_string_list(payload.get("open_questions")),
        "decisions": _coerce_string_list(payload.get("decisions")),
        "risk_flags": _coerce_string_list(payload.get("risk_flags")),
        "timeline_overrides": _normalize_timeline_overrides(payload.get("timeline_overrides")),
    }
    return normalized


def _validate_refinement(normalized: dict[str, Any]) -> None:
    required = {"action_items", "open_questions", "decisions", "risk_flags", "timeline_overrides"}
    if set(normalized.keys()) != required:
        raise HybridRefinementError("schema_error", "schema_validation_failed")
    if not all(isinstance(normalized[k], list) for k in required):
        raise HybridRefinementError("schema_error", "schema_validation_failed")
    for row in normalized["action_items"]:
        if not isinstance(row, dict):
            raise HybridRefinementError("schema_error", "schema_validation_failed")
        if not isinstance(row.get("item"), str) or not row["item"]:
            raise HybridRefinementError("schema_error", "schema_validation_failed")
        owner = row.get("owner")
        if owner is not None and (not isinstance(owner, str) or not owner):
            raise HybridRefinementError("schema_error", "schema_validation_failed")
        due = row.get("due")
        if due is not None and not isinstance(due, str):
            raise HybridRefinementError("schema_error", "schema_validation_failed")
    for key in ("open_questions", "decisions", "risk_flags"):
        if not all(isinstance(x, str) and x for x in normalized[key]):
            raise HybridRefinementError("schema_error", "schema_validation_failed")
    for row in normalized["timeline_overrides"]:
        if not isinstance(row, dict):
            raise HybridRefinementError("schema_error", "schema_validation_failed")
        if not isinstance(row.get("index"), int) or not isinstance(row.get("summary"), str):
            raise HybridRefinementError("schema_error", "schema_validation_failed")


def _log_invalid_shape(payload: Any, exc: Exception) -> None:
    keys: list[str] = []
    types: dict[str, str] = {}
    if isinstance(payload, dict):
        keys = list(payload.keys())[:20]
        types = {k: type(v).__name__ for k, v in payload.items()}
    logger.warning(
        "hybrid_refinement_shape_error exc=%s keys=%s types=%s",
        exc.__class__.__name__,
        keys,
        types,
    )


def _log_openai_error(exc: Exception) -> None:
    msg = (str(exc) or "")[:300]
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    response = getattr(exc, "response", None)
    response_status = None
    response_text = None
    if response is not None:
        response_status = getattr(response, "status_code", None)
        raw_text = getattr(response, "text", None)
        if isinstance(raw_text, str):
            response_text = raw_text[:300]
    logger.warning(
        "hybrid_openai_call_failed exc=%s status=%s response_status=%s msg=%s response_text=%s",
        exc.__class__.__name__,
        status,
        response_status,
        msg,
        response_text,
    )


def _extract_openai_diag(exc: Exception) -> tuple[int | None, str | None]:
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None) if response is not None else None
    status_int = None
    for candidate in (status, response_status):
        if isinstance(candidate, int):
            status_int = candidate
            break
        try:
            if candidate is not None:
                status_int = int(candidate)
                break
        except Exception:
            pass
    msg = (str(exc) or "")[:300] or None
    return status_int, msg


def _as_input_text(s: str) -> dict[str, str]:
    # Smoke test:
    # 1) curl -s http://127.0.0.1:8000/health
    # 2) call gmail.thread_summarize and confirm result._hybrid.applied == true
    return {"type": "input_text", "text": s}


def refine_summary_with_llm(deterministic_summary: dict[str, Any], context_digest: str) -> dict[str, Any]:
    if not _is_hybrid_available():
        return {}

    try:
        from openai import APIError, AuthenticationError, OpenAI, RateLimitError
    except Exception as exc:
        logger.warning("hybrid_openai_import_failed exc=%s msg=%s", exc.__class__.__name__, (str(exc) or "")[:300])
        _, msg = _extract_openai_diag(exc)
        raise HybridRefinementError("llm_error", "openai_call_failed", error_message=msg, http_status=None)

    client = OpenAI(api_key=OPENAI_API_KEY)
    schema = {
        "type": "object",
        "properties": {
            "action_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item": {"type": "string"},
                        "owner": {"type": ["string", "null"]},
                        "due": {"type": ["string", "null"]},
                    },
                    "required": ["owner", "item", "due"],
                    "additionalProperties": False,
                },
            },
            "open_questions": {"type": "array", "items": {"type": "string"}},
            "decisions": {"type": "array", "items": {"type": "string"}},
            "risk_flags": {"type": "array", "items": {"type": "string"}},
            "timeline_overrides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "summary": {"type": "string"},
                    },
                    "required": ["index", "summary"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["action_items", "open_questions", "decisions", "risk_flags", "timeline_overrides"],
        "additionalProperties": False,
    }
    system_prompt = (
        "Refine deterministic email-thread insights. "
        "Return JSON only. No markdown. Keep content concise and non-sensitive."
    )
    user_prompt = (
        f"Subject: {deterministic_summary.get('subject', '')}\n"
        f"Participants: {json.dumps(deterministic_summary.get('participants', {}), ensure_ascii=False)}\n"
        f"Timeline: {json.dumps(deterministic_summary.get('timeline', []), ensure_ascii=False)}\n"
        f"Context digest:\n{context_digest}\n"
    )

    try:
        resp = client.responses.create(
            model=HYBRID_MODEL,
            input=[
                {"role": "system", "content": [_as_input_text(system_prompt)]},
                {"role": "user", "content": [_as_input_text(user_prompt)]},
            ],
            temperature=0.2,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "thread_summary_refinement",
                    "strict": True,
                    "schema": schema,
                }
            },
        )
    except TypeError:
        # SDK fallback path: still parse + normalize + validate post-response.
        try:
            resp = client.responses.create(
                model=HYBRID_MODEL,
                input=[
                    {"role": "system", "content": [_as_input_text(system_prompt)]},
                    {"role": "user", "content": [_as_input_text(user_prompt)]},
                ],
                temperature=0.2,
            )
        except AuthenticationError as exc:
            _log_openai_error(exc)
            status, msg = _extract_openai_diag(exc)
            raise HybridRefinementError("llm_error", "openai_auth_failed", error_message=msg, http_status=status)
        except RateLimitError as exc:
            _log_openai_error(exc)
            status, msg = _extract_openai_diag(exc)
            raise HybridRefinementError("llm_error", "openai_rate_limited", error_message=msg, http_status=status)
        except APIError as exc:
            _log_openai_error(exc)
            status, msg = _extract_openai_diag(exc)
            raise HybridRefinementError("llm_error", "openai_api_error", error_message=msg, http_status=status)
        except Exception as exc:
            _log_openai_error(exc)
            status, msg = _extract_openai_diag(exc)
            raise HybridRefinementError("llm_error", "openai_call_failed", error_message=msg, http_status=status)
    except AuthenticationError as exc:
        _log_openai_error(exc)
        status, msg = _extract_openai_diag(exc)
        raise HybridRefinementError("llm_error", "openai_auth_failed", error_message=msg, http_status=status)
    except RateLimitError as exc:
        _log_openai_error(exc)
        status, msg = _extract_openai_diag(exc)
        raise HybridRefinementError("llm_error", "openai_rate_limited", error_message=msg, http_status=status)
    except APIError as exc:
        _log_openai_error(exc)
        status, msg = _extract_openai_diag(exc)
        raise HybridRefinementError("llm_error", "openai_api_error", error_message=msg, http_status=status)
    except Exception as exc:
        _log_openai_error(exc)
        status, msg = _extract_openai_diag(exc)
        raise HybridRefinementError("llm_error", "openai_call_failed", error_message=msg, http_status=status)

    # Access output_text explicitly to enforce Responses API output handling path.
    _ = getattr(resp, "output_text", None)

    payload = {}
    try:
        payload = _extract_response_json(resp)
    except json.JSONDecodeError as exc:
        logger.warning("hybrid_invalid_json exc=%s", exc.__class__.__name__)
        raise HybridRefinementError("schema_error", "invalid_json")
    except HybridRefinementError:
        raise
    except Exception as exc:
        logger.warning("hybrid_response_parse_failed exc=%s", exc.__class__.__name__)
        raise HybridRefinementError("schema_error", "invalid_json")

    try:
        normalized = normalize_refinement_payload_for_test(payload)
        _validate_refinement(normalized)
        return normalized
    except HybridRefinementError as exc:
        _log_invalid_shape(payload, exc)
        raise
    except Exception as exc:
        _log_invalid_shape(payload, exc)
        raise HybridRefinementError("schema_error", "schema_validation_failed")
