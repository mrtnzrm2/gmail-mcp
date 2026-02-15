import base64
import html
import json
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timedelta

import requests
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parseaddr

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .auth import get_current_user
from .config import (
    GMAIL_SCOPES,
    GOOGLE_GMAIL_CLIENT_ID,
    GOOGLE_GMAIL_CLIENT_SECRET,
    HYBRID_MODEL,
    HYBRID_SUMMARIZE_ENABLED,
    OPENAI_API_KEY,
)
from .crypto import decrypt_refresh_token
from .db import get_db
from .gmail_caps import (
    InsufficientScopeError,
    compute_capabilities,
    parse_scopes,
    require_capability,
)
from .models import AuditEvent, GmailAccount, ThreadSummaryCache, User
from .rate_limit import limiter
from .send_safety import apply_force_to_self
from .intelligence import (
    INTELLIGENCE_VERSION,
    build_intelligence_deterministic,
    get_reply_signature,
    get_user_display_name,
    normalize_reply_body,
    normalize_suggested_reply,
)
from .intelligence_hybrid import refine_intelligence_hybrid, refine_triage_hybrid
from .summarize_hybrid import HybridRefinementError, build_context_digest, refine_summary_with_llm

router = APIRouter(prefix="/gmail", tags=["gmail"])
logger = logging.getLogger(__name__)


def _parse_scopes(scopes_value: str) -> list[str]:
    return [s for s in scopes_value.split(" ") if s]


def slugify_code(s: str) -> str:
    text = (s or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text or "error"


def classify_google_httperror(err: HttpError) -> tuple[int, str | None, str | None]:
    http_status = int(getattr(err.resp, "status", None) or 500)
    reason = None
    message = None
    content = getattr(err, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    if content:
        try:
            payload = json.loads(content)
            details = payload.get("error", {}).get("errors") or []
            if details and isinstance(details[0], dict):
                reason = details[0].get("reason")
                message = details[0].get("message")
            if not message:
                message = payload.get("error", {}).get("message")
            if not reason:
                reason = payload.get("error", {}).get("status")
        except Exception:
            pass
    return http_status, (str(reason) if reason else None), (str(message) if message else None)


def normalize_audit_error(exc) -> tuple[int | None, str, str | None]:
    if isinstance(exc, HttpError):
        http_status, reason, message = classify_google_httperror(exc)
        reason = (reason or "").lower()
        msg = (message or "").lower()
        if http_status == 403 and (reason in {"insufficientpermissions", "forbidden"} or "insufficient" in msg):
            return 403, "insufficient_scope", "google_insufficient_permissions"
        if reason in {"invalidargument", "invalid_argument"}:
            code = "invalid_argument"
        elif reason in {"ratelimitexceeded", "userratelimitexceeded"}:
            code = "rate_limited"
        elif reason in {"autherror", "unauthorized", "invalidcredentials"}:
            code = "google_unauthorized"
        elif reason in {"notfound", "not_found"}:
            code = "google_not_found"
        else:
            code = "google_http_error"
        return int(http_status), "google_http_error", code

    if isinstance(exc, InsufficientScopeError):
        return exc.status_code, "insufficient_scope", exc.error_code

    if isinstance(exc, HTTPException):
        if hasattr(exc, "error_type") and hasattr(exc, "error_code"):
            return exc.status_code, getattr(exc, "error_type"), getattr(exc, "error_code")
        return exc.status_code, "http_exception", slugify_code(str(exc.detail))[:80]

    if isinstance(exc, RefreshError):
        text = str(exc).lower()
        if "invalid_grant" in text:
            return 401, "google_invalid_grant", "invalid_grant"
        for arg in getattr(exc, "args", []):
            if isinstance(arg, dict) and "invalid_grant" in json.dumps(arg).lower():
                return 401, "google_invalid_grant", "invalid_grant"
            if isinstance(arg, str) and "invalid_grant" in arg.lower():
                return 401, "google_invalid_grant", "invalid_grant"

    return 500, "internal_error", "internal_error"


def log_audit_event(
    db: Session,
    user_id,
    account_id,
    tool_name: str,
    status: str,
    error_code: str | None = None,
    http_status: int | None = None,
    error_type: str | None = None,
    resolved_via_default: bool = False,
    cache_hit: bool = False,
    hybrid_applied: bool = False,
    hybrid_model: str | None = None,
    hybrid_error_code: str | None = None,
    intelligence_applied: bool = False,
    intelligence_hybrid_applied: bool = False,
    intelligence_version: str | None = None,
    forced_to_self: bool = False,
    reply_mode: str | None = None,
    override_applied: bool = False,
    override_reason: str | None = None,
    original_to: str | None = None,
    final_to: str | None = None,
):
    event = AuditEvent(
        user_id=user_id,
        account_id=account_id,
        tool_name=tool_name,
        status=status,
        error_code=error_code,
        http_status=http_status,
        error_type=error_type,
        resolved_via_default=resolved_via_default,
        cache_hit=bool(cache_hit),
        hybrid_applied=bool(hybrid_applied),
        hybrid_model=hybrid_model,
        hybrid_error_code=hybrid_error_code,
        intelligence_applied=bool(intelligence_applied),
        intelligence_hybrid_applied=bool(intelligence_hybrid_applied),
        intelligence_version=intelligence_version,
        forced_to_self=bool(forced_to_self),
        reply_mode=reply_mode,
        override_applied=bool(override_applied),
        override_reason=override_reason,
        original_to=original_to,
        final_to=final_to,
    )
    db.add(event)
    db.commit()


def _run_with_audit(db: Session, user: User, account_id, tool_name: str, fn, audit_ctx: dict | None = None):
    ctx = audit_ctx or {}
    resolved_via_default = bool(ctx.get("resolved_via_default", False))
    resolved_account_id = ctx.get("account_id", account_id)
    hybrid_applied = ctx.get("hybrid_applied")
    hybrid_model = ctx.get("hybrid_model")
    hybrid_error_code = ctx.get("hybrid_error_code")
    cache_hit = bool(ctx.get("cache_hit", False))
    intelligence_applied = bool(ctx.get("intelligence_applied", False))
    intelligence_hybrid_applied = bool(ctx.get("intelligence_hybrid_applied", False))
    intelligence_version = ctx.get("intelligence_version")
    forced_to_self = bool(ctx.get("forced_to_self", False))
    reply_mode = ctx.get("reply_mode")
    override_applied = bool(ctx.get("override_applied", False))
    override_reason = ctx.get("override_reason")
    original_to = ctx.get("original_to")
    final_to = ctx.get("final_to")
    try:
        result = fn()
        resolved_via_default = bool(ctx.get("resolved_via_default", resolved_via_default))
        resolved_account_id = ctx.get("account_id", resolved_account_id)
        hybrid_applied = ctx.get("hybrid_applied", hybrid_applied)
        hybrid_model = ctx.get("hybrid_model", hybrid_model)
        hybrid_error_code = ctx.get("hybrid_error_code", hybrid_error_code)
        cache_hit = bool(ctx.get("cache_hit", cache_hit))
        intelligence_applied = bool(ctx.get("intelligence_applied", intelligence_applied))
        intelligence_hybrid_applied = bool(ctx.get("intelligence_hybrid_applied", intelligence_hybrid_applied))
        intelligence_version = ctx.get("intelligence_version", intelligence_version)
        forced_to_self = bool(ctx.get("forced_to_self", forced_to_self))
        reply_mode = ctx.get("reply_mode", reply_mode)
        override_applied = bool(ctx.get("override_applied", override_applied))
        override_reason = ctx.get("override_reason", override_reason)
        original_to = ctx.get("original_to", original_to)
        final_to = ctx.get("final_to", final_to)
        log_audit_event(
            db=db,
            user_id=user.id,
            account_id=resolved_account_id,
            tool_name=tool_name,
            status="success",
            error_code=None,
            http_status=200,
            error_type=None,
            resolved_via_default=resolved_via_default,
            cache_hit=cache_hit,
            hybrid_applied=hybrid_applied,
            hybrid_model=hybrid_model,
            hybrid_error_code=hybrid_error_code,
            intelligence_applied=intelligence_applied,
            intelligence_hybrid_applied=intelligence_hybrid_applied,
            intelligence_version=intelligence_version,
            forced_to_self=forced_to_self,
            reply_mode=reply_mode,
            override_applied=override_applied,
            override_reason=override_reason,
            original_to=original_to,
            final_to=final_to,
        )
        return result
    except HttpError as exc:
        resolved_via_default = bool(ctx.get("resolved_via_default", resolved_via_default))
        resolved_account_id = ctx.get("account_id", resolved_account_id)
        hybrid_applied = ctx.get("hybrid_applied", hybrid_applied)
        hybrid_model = ctx.get("hybrid_model", hybrid_model)
        hybrid_error_code = ctx.get("hybrid_error_code", hybrid_error_code)
        cache_hit = bool(ctx.get("cache_hit", cache_hit))
        intelligence_applied = bool(ctx.get("intelligence_applied", intelligence_applied))
        intelligence_hybrid_applied = bool(ctx.get("intelligence_hybrid_applied", intelligence_hybrid_applied))
        intelligence_version = ctx.get("intelligence_version", intelligence_version)
        forced_to_self = bool(ctx.get("forced_to_self", forced_to_self))
        reply_mode = ctx.get("reply_mode", reply_mode)
        override_applied = bool(ctx.get("override_applied", override_applied))
        override_reason = ctx.get("override_reason", override_reason)
        original_to = ctx.get("original_to", original_to)
        final_to = ctx.get("final_to", final_to)
        http_status, error_type, error_code = normalize_audit_error(exc)
        log_audit_event(
            db=db,
            user_id=user.id,
            account_id=resolved_account_id,
            tool_name=tool_name,
            status="error",
            error_code=error_code,
            http_status=http_status,
            error_type=error_type,
            resolved_via_default=resolved_via_default,
            cache_hit=cache_hit,
            hybrid_applied=hybrid_applied,
            hybrid_model=hybrid_model,
            hybrid_error_code=hybrid_error_code,
            intelligence_applied=intelligence_applied,
            intelligence_hybrid_applied=intelligence_hybrid_applied,
            intelligence_version=intelligence_version,
            forced_to_self=forced_to_self,
            reply_mode=reply_mode,
            override_applied=override_applied,
            override_reason=override_reason,
            original_to=original_to,
            final_to=final_to,
        )
        status_code, reason, message = classify_google_httperror(exc)
        reason_l = (reason or "").lower()
        message_l = (message or "").lower()
        if (
            tool_name in {"gmail.get", "gmail.get_body"}
            and status_code == 400
            and reason_l in {"invalidargument", "invalid_argument"}
            and "invalid id value" in message_l
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Message id is invalid for the selected mailbox. "
                    "Use a msg_id returned by gmail.search for the same account/default."
                ),
            )
        if (
            tool_name in {"gmail.get", "gmail.get_body"}
            and status_code == 404
        ):
            raise HTTPException(
                status_code=404,
                detail=(
                    "Message id was not found for the selected mailbox. "
                    "If you switched default accounts, use a msg_id from gmail.search for that account."
                ),
            )
        if http_status == 403 and error_type == "insufficient_scope":
            err = HTTPException(status_code=403, detail="Insufficient Gmail scope")
            setattr(err, "error_type", "insufficient_scope")
            setattr(err, "error_code", error_code or "google_insufficient_permissions")
            raise err
        raise HTTPException(status_code=http_status or 502, detail=error_code or "google_http_error")
    except Exception as exc:
        resolved_via_default = bool(ctx.get("resolved_via_default", resolved_via_default))
        resolved_account_id = ctx.get("account_id", resolved_account_id)
        hybrid_applied = ctx.get("hybrid_applied", hybrid_applied)
        hybrid_model = ctx.get("hybrid_model", hybrid_model)
        hybrid_error_code = ctx.get("hybrid_error_code", hybrid_error_code)
        cache_hit = bool(ctx.get("cache_hit", cache_hit))
        intelligence_applied = bool(ctx.get("intelligence_applied", intelligence_applied))
        intelligence_hybrid_applied = bool(ctx.get("intelligence_hybrid_applied", intelligence_hybrid_applied))
        intelligence_version = ctx.get("intelligence_version", intelligence_version)
        forced_to_self = bool(ctx.get("forced_to_self", forced_to_self))
        reply_mode = ctx.get("reply_mode", reply_mode)
        override_applied = bool(ctx.get("override_applied", override_applied))
        override_reason = ctx.get("override_reason", override_reason)
        original_to = ctx.get("original_to", original_to)
        final_to = ctx.get("final_to", final_to)
        http_status, error_type, error_code = normalize_audit_error(exc)
        log_audit_event(
            db=db,
            user_id=user.id,
            account_id=resolved_account_id,
            tool_name=tool_name,
            status="error",
            error_code=error_code,
            http_status=http_status,
            error_type=error_type,
            resolved_via_default=resolved_via_default,
            cache_hit=cache_hit,
            hybrid_applied=hybrid_applied,
            hybrid_model=hybrid_model,
            hybrid_error_code=hybrid_error_code,
            intelligence_applied=intelligence_applied,
            intelligence_hybrid_applied=intelligence_hybrid_applied,
            intelligence_version=intelligence_version,
            forced_to_self=forced_to_self,
            reply_mode=reply_mode,
            override_applied=override_applied,
            override_reason=override_reason,
            original_to=original_to,
            final_to=final_to,
        )
        raise


def _decode_b64url(data: str) -> str:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _decode_header(value):
    if not value:
        return ""
    decoded_parts = decode_header(value)
    result = ""
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result += part.decode(encoding or "utf-8", errors="replace")
        else:
            result += part
    return result


def _walk_parts(payload: dict) -> list[dict]:
    parts: list[dict] = []
    if payload.get("parts"):
        for part in payload["parts"]:
            parts.extend(_walk_parts(part))
    else:
        parts.append(payload)
    return parts


def _extract_bodies(payload: dict) -> dict:
    text_plain = None
    text_html = None
    for part in _walk_parts(payload):
        mime_type = part.get("mimeType")
        body_data = (part.get("body") or {}).get("data")
        if not body_data:
            continue
        decoded = _decode_b64url(body_data)
        if mime_type == "text/plain" and text_plain is None:
            text_plain = decoded
        elif mime_type == "text/html" and text_html is None:
            text_html = decoded
    return {"text_plain": text_plain, "text_html": text_html}


def _html_to_text(value: str) -> str:
    # Remove script/style blocks then strip tags.
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value or "")
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    return text


def _normalize_text(value: str) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    # Common mojibake repairs.
    text = text.replace("â€™", "'").replace("â€œ", '"').replace("â€\x9d", '"').replace("â€“", "-")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_headers(payload: dict) -> dict:
    headers = {}
    for item in payload.get("headers", []):
        name = item.get("name")
        if name in {"From", "To", "Cc", "Subject", "Date", "In-Reply-To", "References", "X-Gmail-MCP-Reply-Mode"}:
            headers[name] = item.get("value")
    return headers


def _extract_due_hint(text: str) -> str | None:
    patterns = [
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}\b",
        r"\btomorrow\b",
        r"\bnext week\b",
    ]
    lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, lower, flags=re.IGNORECASE)
        if match:
            return text[match.start():match.end()]
    return None


def _participant_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    raw = (value or "").strip()
    if not raw:
        return tokens
    tokens.add(raw.lower())
    match = re.search(r"<([^>]+)>", raw)
    if match:
        tokens.add(match.group(1).strip().lower())
    elif "@" in raw and " " not in raw:
        tokens.add(raw.lower())
    return tokens


def _normalize_action_item_owners(summary: dict, current_user_email: str | None = None) -> None:
    participants = summary.get("participants", {}) or {}
    known_display_by_token: dict[str, str] = {}
    user_display = None
    for group in ("from", "to", "cc"):
        for item in participants.get(group, []) or []:
            display = str(item or "").strip()
            if not display:
                continue
            tokens = _participant_tokens(display)
            for token in tokens:
                known_display_by_token[token] = display
            if current_user_email and current_user_email.lower() in tokens:
                user_display = display

    normalized_items: list[dict] = []
    for item in summary.get("action_items", []) or []:
        if not isinstance(item, dict):
            continue
        owner = str(item.get("owner") or "").strip()
        owner_tokens = _participant_tokens(owner)
        mapped_owner = "unknown"
        for token in owner_tokens:
            if token in known_display_by_token:
                mapped_owner = known_display_by_token[token]
                break
        if mapped_owner == "unknown" and user_display:
            mapped_owner = user_display
        normalized = dict(item)
        normalized["owner"] = mapped_owner
        normalized_items.append(normalized)
    summary["action_items"] = normalized_items


def _cache_model_name() -> str:
    base = HYBRID_MODEL or ""
    return f"{base}|intel:{INTELLIGENCE_VERSION}"


def _cache_lookup(
    db: Session, user_id, account_id, thread_id: str, max_messages: int, hybrid_enabled: bool, model: str
) -> ThreadSummaryCache | None:
    return db.scalar(
        select(ThreadSummaryCache).where(
            ThreadSummaryCache.user_id == user_id,
            ThreadSummaryCache.account_id == account_id,
            ThreadSummaryCache.thread_id == thread_id,
            ThreadSummaryCache.max_messages == max_messages,
            ThreadSummaryCache.hybrid_enabled.is_(hybrid_enabled),
            ThreadSummaryCache.model == model,
        )
    )


def _cache_upsert(
    db: Session,
    user_id,
    account_id,
    thread_id: str,
    max_messages: int,
    hybrid_enabled: bool,
    model: str,
    thread_last_date: str,
    thread_last_msg_id: str | None,
    thread_last_internal_ms: int | None,
    summary_json: dict,
) -> None:
    row = db.scalar(
        select(ThreadSummaryCache).where(
            ThreadSummaryCache.user_id == user_id,
            ThreadSummaryCache.account_id == account_id,
            ThreadSummaryCache.thread_id == thread_id,
            ThreadSummaryCache.max_messages == max_messages,
            ThreadSummaryCache.hybrid_enabled.is_(hybrid_enabled),
            ThreadSummaryCache.model == model,
        )
    )
    now = datetime.utcnow()
    if row:
        row.summary_json = summary_json
        row.thread_last_date = thread_last_date
        row.thread_last_msg_id = thread_last_msg_id
        row.thread_last_internal_ms = thread_last_internal_ms
        row.created_at = now
        db.add(row)
    else:
        db.add(
            ThreadSummaryCache(
                user_id=user_id,
                account_id=account_id,
                thread_id=thread_id,
                max_messages=max_messages,
                hybrid_enabled=hybrid_enabled,
                model=model,
                summary_json=summary_json,
                thread_last_date=thread_last_date,
                thread_last_msg_id=thread_last_msg_id,
                thread_last_internal_ms=thread_last_internal_ms,
                created_at=now,
            )
        )
    db.commit()


def _header_value(headers: list[dict], name: str) -> str:
    for h in headers or []:
        if str(h.get("name") or "").lower() == name.lower():
            return str(h.get("value") or "")
    return ""


def _parse_person(raw: str) -> dict:
    name, email = parseaddr(raw or "")
    display_name = _decode_header(name).strip() if name else ""
    email_value = (email or "").strip().lower()
    if not display_name and email_value:
        display_name = email_value
    return {"name": display_name, "email": email_value, "raw": (raw or "").strip()}


def _latest_thread_message(thread: dict) -> dict | None:
    messages = thread.get("messages") or []
    if not messages:
        return None
    return max(messages, key=lambda m: int(m.get("internalDate", "0") or "0"))


def _internal_ms(msg: dict | None) -> int:
    if not msg:
        return 0
    try:
        return int(msg.get("internalDate", "0") or "0")
    except Exception:
        return 0


def _thread_sender_context(thread: dict, me_email: str) -> dict:
    messages = sorted(thread.get("messages") or [], key=lambda m: _internal_ms(m))
    me = (me_email or "").strip().lower()
    last_msg = messages[-1] if messages else None
    last_self_msg = None
    last_external_msg = None
    last_external_sender = {"name": "", "email": "", "raw": ""}

    for msg in reversed(messages):
        headers = (msg.get("payload") or {}).get("headers") or []
        sender = _parse_person(_header_value(headers, "From"))
        sender_email = str(sender.get("email") or "").strip().lower()
        if not sender_email:
            continue
        if sender_email == me and last_self_msg is None:
            last_self_msg = msg
            continue
        if sender_email != me and last_external_msg is None:
            last_external_msg = msg
            last_external_sender = sender
        if last_self_msg is not None and last_external_msg is not None:
            break

    latest_from_me = False
    if last_msg is not None:
        last_headers = (last_msg.get("payload") or {}).get("headers") or []
        latest_sender = _parse_person(_header_value(last_headers, "From"))
        latest_from_me = str(latest_sender.get("email") or "").strip().lower() == me

    external_newer_than_self = False
    if last_external_msg is not None:
        external_newer_than_self = _internal_ms(last_external_msg) > _internal_ms(last_self_msg)

    return {
        "last_msg": last_msg,
        "last_self_msg": last_self_msg,
        "last_external_msg": last_external_msg,
        "last_external_sender": last_external_sender,
        "last_external_sender_email": str(last_external_sender.get("email") or "").strip().lower(),
        "last_external_sender_display": str(last_external_sender.get("name") or "").strip(),
        "latest_from_me": latest_from_me,
        "external_newer_than_self": external_newer_than_self,
    }


def _build_reply_raw_message(
    to: str,
    subject: str,
    body_text: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    reply_mode: str | None = None,
) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if reply_mode:
        msg["X-Gmail-MCP-Reply-Mode"] = reply_mode
    msg.set_content(body_text)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


def _build_triage_baseline(
    summary: dict,
    me_email: str,
    last_sender: dict,
    last_message_date: str,
    unread: bool,
    list_unsubscribe: bool,
    latest_from_me: bool,
    external_newer_than_self: bool,
    user_display_name: str | None = None,
) -> dict:
    context = str(summary.get("context_block", "") or "").lower()
    subject = str(summary.get("subject", "") or "")
    has_question = bool(summary.get("open_questions")) or "?" in context
    contains_deadline = bool((summary.get("intelligence") or {}).get("deadlines"))
    request_phrases = ["could you", "please", "can you", "deadline", "by "]
    sender_email = str(last_sender.get("email") or "").lower()
    needs_reply = False
    if latest_from_me:
        needs_reply = False
    elif sender_email and external_newer_than_self:
        needs_reply = True
    elif sender_email and sender_email != me_email.lower():
        needs_reply = bool(unread or has_question or any(p in context for p in request_phrases))
    urgency = "low"
    if any(x in context for x in ["urgent", "asap", "today", "tomorrow"]) or contains_deadline:
        urgency = "high"
    elif any(x in context for x in ["soon", "this week"]):
        urgency = "medium"

    category = "fyi"
    if list_unsubscribe or re.search(r"(newsletter|unsubscribe|digest)", context):
        category = "newsletter"
    elif re.search(r"(lottery|crypto giveaway|claim now|winner)", context):
        category = "spam"
    elif sender_email and sender_email == me_email.lower():
        category = "awaiting_reply"
        needs_reply = False
    elif needs_reply:
        category = "action_required"

    next_action = "No action needed."
    if category == "action_required":
        next_action = "Draft and send a concise reply with the requested information."
    elif category == "awaiting_reply":
        next_action = "Wait for a response and follow up if needed."
    elif category == "newsletter":
        next_action = "Skim and archive if not relevant."
    elif category == "spam":
        next_action = "Mark as spam and avoid engaging."
    elif category == "fyi":
        next_action = "Read and archive or label for reference."

    confidence = 0.6
    if needs_reply and (has_question or contains_deadline):
        confidence = 0.8
    elif category in {"spam", "newsletter"}:
        confidence = 0.75

    suggested_reply = None
    if needs_reply:
        intel_reply = (summary.get("intelligence") or {}).get("suggested_reply")
        if isinstance(intel_reply, dict):
            sr = normalize_suggested_reply(
                me_email=me_email,
                last_sender=last_sender,
                subject=intel_reply.get("subject") or subject,
                body_text=intel_reply.get("body_text") or "",
                user_name=user_display_name,
                user_email=me_email,
            )
            suggested_reply = {"to": sender_email, **sr}
        else:
            sr = normalize_suggested_reply(
                me_email=me_email,
                last_sender=last_sender,
                subject=subject,
                body_text="Thanks for your message. I will review and get back to you shortly.",
                user_name=user_display_name,
                user_email=me_email,
            )
            suggested_reply = {"to": sender_email, **sr}

    return {
        "thread_id": str(summary.get("thread_id") or ""),
        "subject": subject,
        "needs_reply": bool(needs_reply),
        "urgency": urgency,
        "category": category,
        "next_action": next_action,
        "suggested_reply": suggested_reply,
        "confidence": float(max(0.0, min(1.0, confidence))),
        "signals": {
            "last_sender": last_sender.get("raw") or sender_email,
            "last_external_sender_email": sender_email,
            "me_email": me_email,
            "last_message_date": last_message_date,
            "unread": bool(unread),
            "has_question": bool(has_question),
            "contains_deadline": bool(contains_deadline),
        },
    }


def require_gmail_account(db: Session, current_user: User, account_id: str) -> GmailAccount:
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Gmail account not found")

    acct = db.scalar(
        select(GmailAccount).where(
            GmailAccount.id == account_uuid,
            GmailAccount.user_id == current_user.id,
            GmailAccount.revoked_at.is_(None),
        )
    )
    if not acct:
        raise HTTPException(status_code=404, detail="Gmail account not found")
    return acct


def resolve_account(db: Session, user: User, account_id: str | None) -> tuple[GmailAccount, bool]:
    if account_id is not None:
        return require_gmail_account(db, user, account_id), False

    acct = db.scalar(
        select(GmailAccount).where(
            GmailAccount.user_id == user.id,
            GmailAccount.revoked_at.is_(None),
            GmailAccount.is_default.is_(True),
        )
    )
    if not acct:
        raise HTTPException(status_code=400, detail="No default Gmail account set")
    return acct, True


def _auto_revoke_if_invalid_grant(db: Session, account: GmailAccount, exc: Exception) -> bool:
    tokens = ("invalid_grant", "token has been expired or revoked")
    text_parts: list[str] = [str(exc)]
    status = None

    if isinstance(exc, RefreshError):
        for arg in getattr(exc, "args", []):
            if isinstance(arg, dict):
                try:
                    text_parts.append(json.dumps(arg))
                except Exception:
                    pass
            else:
                text_parts.append(str(arg))
    elif isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        content = getattr(exc, "content", b"")
        if isinstance(content, bytes):
            try:
                content = content.decode("utf-8", errors="replace")
            except Exception:
                content = str(content)
        text_parts.append(str(content))

    text = " ".join(text_parts).lower()
    if not any(token in text for token in tokens):
        return False

    # Keep transient upstream failures out of auto-revoke behavior.
    if status == 429 or (status is not None and status >= 500):
        return False

    account.revoked_at = datetime.utcnow()
    db.add(account)
    db.commit()
    return True


def _build_service(db: Session, account: GmailAccount):
    try:
        refresh_token = decrypt_refresh_token(account.refresh_token_enc)
    except ValueError:
        raise HTTPException(status_code=401, detail="Stored credentials are invalid")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_GMAIL_CLIENT_ID,
        client_secret=GOOGLE_GMAIL_CLIENT_SECRET,
        scopes=_parse_scopes(account.scopes) or GMAIL_SCOPES,
    )

    try:
        creds.refresh(GoogleAuthRequest())
    except RefreshError as exc:
        if _auto_revoke_if_invalid_grant(db, account, exc):
            raise HTTPException(status_code=404, detail="Gmail account not found")
        raise HTTPException(
            status_code=503,
            detail="Gmail service temporarily unavailable",
        )

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _build_raw_message(to: str, subject: str, body_text: str, reply_mode: str | None = None) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if reply_mode:
        msg["X-Gmail-MCP-Reply-Mode"] = reply_mode
    msg.set_content(body_text)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


def _apply_send_override(me_email: str, intended_to: str, reply_mode: str) -> dict:
    original_to = (intended_to or "").strip().lower()
    final_to, applied, reason = apply_force_to_self(original_to, (me_email or "").strip().lower())
    return {
        "original_to": original_to,
        "final_to": final_to,
        "override_applied": bool(applied),
        "override_reason": reason,
        "reply_mode": "self_test" if applied else reply_mode,
    }


def gmail_accounts_list_tool(db: Session, user: User) -> list[dict]:
    rows = db.scalars(
        select(GmailAccount).where(
            GmailAccount.user_id == user.id,
            GmailAccount.revoked_at.is_(None),
        )
    ).all()
    return [
        {
            "account_id": str(r.id),
            "gmail_email": r.gmail_email,
            "is_default": bool(r.is_default),
            "scopes": sorted(parse_scopes(r.scopes)),
            "capabilities": compute_capabilities(parse_scopes(r.scopes)),
        }
        for r in rows
    ]


def gmail_search_tool(
    db: Session, user: User, account_id: str | None, query: str, max_results: int = 10
) -> list[dict]:
    account_uuid = None
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        pass

    audit_ctx: dict = {"account_id": account_uuid, "resolved_via_default": False}

    def _run():
        account, resolved_via_default = resolve_account(db, user, account_id)
        audit_ctx["account_id"] = account.id
        audit_ctx["resolved_via_default"] = resolved_via_default
        require_capability(account, "can_search", tool_name="gmail.search")
        service = _build_service(db, account)
        max_rows = max(1, min(max_results, 50))
        try:
            response = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_rows)
                .execute()
            )
        except HttpError as exc:
            if _auto_revoke_if_invalid_grant(db, account, exc):
                raise HTTPException(status_code=404, detail="Gmail account not found")
            raise
        messages = response.get("messages", [])
        return [{"id": m.get("id"), "threadId": m.get("threadId")} for m in messages]

    return _run_with_audit(db, user, account_uuid, "gmail.search", _run, audit_ctx=audit_ctx)


def gmail_get_tool(db: Session, user: User, account_id: str | None, msg_id: str) -> dict:
    account_uuid = None
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        pass

    audit_ctx: dict = {"account_id": account_uuid, "resolved_via_default": False}

    def _run():
        account, resolved_via_default = resolve_account(db, user, account_id)
        audit_ctx["account_id"] = account.id
        audit_ctx["resolved_via_default"] = resolved_via_default
        require_capability(account, "can_get", tool_name="gmail.get")
        service = _build_service(db, account)
        try:
            response = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                )
                .execute()
            )
        except HttpError as exc:
            if _auto_revoke_if_invalid_grant(db, account, exc):
                raise HTTPException(status_code=404, detail="Gmail account not found")
            raise

        headers = _extract_headers(response.get("payload", {}))

        return {
            "id": response.get("id"),
            "threadId": response.get("threadId"),
            "snippet": response.get("snippet"),
            "labels": response.get("labelIds", []),
            "from": _decode_header(headers.get("From")),
            "to": _decode_header(headers.get("To")),
            "subject": _decode_header(headers.get("Subject")),
            "date": headers.get("Date"),
        }

    return _run_with_audit(db, user, account_uuid, "gmail.get", _run, audit_ctx=audit_ctx)


def gmail_get_body_tool(db: Session, user: User, account_id: str | None, msg_id: str) -> dict:
    account_uuid = None
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        pass

    audit_ctx: dict = {"account_id": account_uuid, "resolved_via_default": False}

    def _run():
        account, resolved_via_default = resolve_account(db, user, account_id)
        audit_ctx["account_id"] = account.id
        audit_ctx["resolved_via_default"] = resolved_via_default
        require_capability(account, "can_get", tool_name="gmail.get_body")
        service = _build_service(db, account)
        try:
            response = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        except HttpError as exc:
            if _auto_revoke_if_invalid_grant(db, account, exc):
                raise HTTPException(status_code=404, detail="Gmail account not found")
            raise

        payload = response.get("payload", {})
        bodies = _extract_bodies(payload)
        if not bodies["text_plain"] and (payload.get("body") or {}).get("data"):
            bodies["text_plain"] = _decode_b64url(payload["body"]["data"])
        return bodies

    return _run_with_audit(db, user, account_uuid, "gmail.get_body", _run, audit_ctx=audit_ctx)


def gmail_send_tool(
    db: Session,
    user: User,
    account_id: str | None,
    to: str,
    subject: str,
    body_text: str,
    confirmed: bool = False,
) -> dict:
    account_uuid = None
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        pass

    audit_ctx: dict = {
        "account_id": account_uuid,
        "resolved_via_default": False,
        "reply_mode": "compose",
        "override_applied": False,
        "override_reason": None,
        "original_to": None,
        "final_to": None,
        "forced_to_self": False,
    }

    def _run():
        account, resolved_via_default = resolve_account(db, user, account_id)
        audit_ctx["account_id"] = account.id
        audit_ctx["resolved_via_default"] = resolved_via_default
        require_capability(account, "can_send", tool_name="gmail.send")
        if not confirmed:
            err = HTTPException(
                status_code=400,
                detail={
                    "message": "Set confirmed=true to send.",
                    "remediation": "Set confirmed=true to send.",
                },
            )
            setattr(err, "error_type", "confirmation_required")
            setattr(err, "error_code", "send_requires_confirmed_true")
            raise err
        me_email = str(account.gmail_email or user.email or "").strip().lower()
        safety = _apply_send_override(me_email=me_email, intended_to=to, reply_mode="compose")
        to_effective = safety["final_to"]
        audit_ctx["override_applied"] = safety["override_applied"]
        audit_ctx["override_reason"] = safety["override_reason"]
        audit_ctx["original_to"] = safety["original_to"]
        audit_ctx["final_to"] = safety["final_to"]
        audit_ctx["forced_to_self"] = safety["override_applied"]
        audit_ctx["reply_mode"] = safety["reply_mode"]
        service = _build_service(db, account)
        raw = _build_raw_message(to_effective, subject, body_text, reply_mode=safety["reply_mode"])
        try:
            sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except HttpError as exc:
            if _auto_revoke_if_invalid_grant(db, account, exc):
                raise HTTPException(status_code=404, detail="Gmail account not found")
            raise
        out = {
            "id": sent.get("id"),
            "threadId": sent.get("threadId"),
            "labelIds": sent.get("labelIds", []),
            "reply_mode": safety["reply_mode"],
            "override_applied": safety["override_applied"],
            "override_reason": safety["override_reason"],
            "original_to": safety["original_to"],
            "final_to": safety["final_to"],
        }
        if safety["override_applied"]:
            out["_dev"] = {"forced_to_self": True}
        return out

    return _run_with_audit(db, user, account_uuid, "gmail.send", _run, audit_ctx=audit_ctx)


def gmail_draft_create_tool(
    db: Session,
    user: User,
    account_id: str | None,
    to: str,
    subject: str,
    body_text: str,
) -> dict:
    account_uuid = None
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        pass

    audit_ctx: dict = {
        "account_id": account_uuid,
        "resolved_via_default": False,
        "reply_mode": "compose",
        "override_applied": False,
        "override_reason": None,
        "original_to": None,
        "final_to": None,
        "forced_to_self": False,
    }

    def _run():
        account, resolved_via_default = resolve_account(db, user, account_id)
        audit_ctx["account_id"] = account.id
        audit_ctx["resolved_via_default"] = resolved_via_default
        require_capability(account, "can_draft", tool_name="gmail.draft_create")
        service = _build_service(db, account)
        to_effective = (to or "").strip().lower()
        audit_ctx["reply_mode"] = "compose"
        audit_ctx["override_applied"] = False
        audit_ctx["override_reason"] = None
        audit_ctx["original_to"] = to_effective
        audit_ctx["final_to"] = to_effective
        raw = _build_raw_message(to_effective, subject, body_text, reply_mode="compose")
        try:
            draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        except HttpError as exc:
            if _auto_revoke_if_invalid_grant(db, account, exc):
                raise HTTPException(status_code=404, detail="Gmail account not found")
            raise
        out = {
            "draft_id": draft.get("id"),
            "preview": {"to": to_effective, "subject": subject, "body_text": body_text},
            "reply_mode": "compose",
            "override_applied": False,
            "override_reason": None,
            "original_to": to_effective,
            "final_to": to_effective,
        }
        return out

    return _run_with_audit(db, user, account_uuid, "gmail.draft_create", _run, audit_ctx=audit_ctx)


def gmail_draft_send_tool(
    db: Session,
    user: User,
    account_id: str | None,
    draft_id: str,
    confirmed: bool,
) -> dict:
    account_uuid = None
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        pass

    audit_ctx: dict = {
        "account_id": account_uuid,
        "resolved_via_default": False,
        "reply_mode": "compose",
        "override_applied": False,
        "override_reason": None,
        "original_to": None,
        "final_to": None,
        "forced_to_self": False,
    }

    def _run():
        if not confirmed:
            err = HTTPException(
                status_code=400,
                detail={
                    "message": "Set confirmed=true to send.",
                    "remediation": "Set confirmed=true to send.",
                },
            )
            setattr(err, "error_type", "confirmation_required")
            setattr(err, "error_code", "send_requires_confirmed_true")
            raise err

        account, resolved_via_default = resolve_account(db, user, account_id)
        audit_ctx["account_id"] = account.id
        audit_ctx["resolved_via_default"] = resolved_via_default
        require_capability(account, "can_send", tool_name="gmail.draft_send")
        require_capability(account, "can_draft", tool_name="gmail.draft_send")
        service = _build_service(db, account)
        me_email = str(account.gmail_email or user.email or "").strip().lower()
        try:
            draft = service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
        except HttpError as exc:
            if _auto_revoke_if_invalid_grant(db, account, exc):
                raise HTTPException(status_code=404, detail="Gmail account not found")
            raise
        message = draft.get("message") or {}
        payload = message.get("payload") or {}
        headers = _extract_headers(payload)
        to_header = headers.get("To") or ""
        _, parsed_to = parseaddr(to_header)
        original_to = (parsed_to or "").strip().lower()
        subject = headers.get("Subject") or ""
        bodies = _extract_bodies(payload)
        body_text = bodies.get("text_plain") or ""
        if not body_text:
            body_text = _strip_html(bodies.get("text_html") or "")
        if not body_text and (payload.get("body") or {}).get("data"):
            body_text = _decode_b64url(payload["body"]["data"])
        stored_reply_mode = str(headers.get("X-Gmail-MCP-Reply-Mode") or "compose").strip().lower() or "compose"
        safety = _apply_send_override(me_email=me_email, intended_to=original_to, reply_mode=stored_reply_mode)
        final_to = safety["final_to"]
        applied = bool(safety["override_applied"])
        audit_ctx["override_applied"] = safety["override_applied"]
        audit_ctx["override_reason"] = safety["override_reason"]
        audit_ctx["original_to"] = safety["original_to"]
        audit_ctx["final_to"] = safety["final_to"]
        audit_ctx["forced_to_self"] = safety["override_applied"]
        audit_ctx["reply_mode"] = safety["reply_mode"]

        if applied:
            raw = _build_raw_message(final_to, subject, body_text, reply_mode=safety["reply_mode"])
            body = {"message": {"raw": raw}}
            if message.get("threadId"):
                body["message"]["threadId"] = message.get("threadId")
            try:
                new_draft = service.users().drafts().create(userId="me", body=body).execute()
                sent = service.users().drafts().send(userId="me", body={"id": new_draft.get("id")}).execute()
            except HttpError as exc:
                if _auto_revoke_if_invalid_grant(db, account, exc):
                    raise HTTPException(status_code=404, detail="Gmail account not found")
                raise
        else:
            try:
                sent = service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
            except HttpError as exc:
                if _auto_revoke_if_invalid_grant(db, account, exc):
                    raise HTTPException(status_code=404, detail="Gmail account not found")
                raise
        out = {
            "id": sent.get("id"),
            "threadId": sent.get("threadId"),
            "labelIds": sent.get("labelIds", []),
            "reply_mode": safety["reply_mode"],
            "override_applied": safety["override_applied"],
            "override_reason": safety["override_reason"],
            "original_to": safety["original_to"],
            "final_to": safety["final_to"],
        }
        if applied:
            out["_dev"] = {"forced_to_self": True}
        return out

    return _run_with_audit(db, user, account_uuid, "gmail.draft_send", _run, audit_ctx=audit_ctx)


def gmail_thread_summarize_tool(
    db: Session,
    user: User,
    thread_id: str,
    max_messages: int = 10,
    account_id: str | None = None,
    force_refresh: bool = False,
) -> dict:
    account_uuid = None
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        pass

    audit_ctx: dict = {"account_id": account_uuid, "resolved_via_default": False}

    def _run():
        account, resolved_via_default = resolve_account(db, user, account_id)
        audit_ctx["account_id"] = account.id
        audit_ctx["resolved_via_default"] = resolved_via_default
        require_capability(account, "can_get", tool_name="gmail.thread_summarize")
        audit_ctx["hybrid_model"] = HYBRID_MODEL or None
        audit_ctx["intelligence_applied"] = True
        audit_ctx["intelligence_hybrid_applied"] = False
        audit_ctx["intelligence_version"] = INTELLIGENCE_VERSION
        audit_ctx["cache_hit"] = False
        service = _build_service(db, account)
        max_items = max(1, min(max_messages, 50))
        try:
            thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        except HttpError as exc:
            if _auto_revoke_if_invalid_grant(db, account, exc):
                raise HTTPException(status_code=404, detail="Gmail account not found")
            raise

        messages = thread.get("messages", [])
        sorted_msgs = sorted(messages, key=lambda m: int(m.get("internalDate", "0")))[:max_items]
        me_email = str(account.gmail_email or user.email or "").strip().lower()
        latest_msg = _latest_thread_message(thread) or (sorted_msgs[-1] if sorted_msgs else None)
        last_sender = {"name": "", "email": "", "raw": ""}
        if latest_msg:
            latest_headers = (latest_msg.get("payload") or {}).get("headers") or []
            last_sender = _parse_person(_header_value(latest_headers, "From"))
        if sorted_msgs:
            last_headers = _extract_headers(sorted_msgs[-1].get("payload", {}))
            thread_last_date = str(last_headers.get("Date") or sorted_msgs[-1].get("internalDate") or "")
            last_msg_id = str(sorted_msgs[-1].get("id") or "")
            try:
                last_internal_ms = int(sorted_msgs[-1].get("internalDate", "0"))
            except Exception:
                last_internal_ms = None
        else:
            thread_last_date = ""
            last_msg_id = None
            last_internal_ms = None

        cache_model = _cache_model_name()
        cache_row = _cache_lookup(
            db=db,
            user_id=user.id,
            account_id=account.id,
            thread_id=thread_id,
            max_messages=max_items,
            hybrid_enabled=bool(HYBRID_SUMMARIZE_ENABLED),
            model=cache_model,
        )
        cache_reason = "miss"
        if force_refresh:
            cache_reason = "force_refresh"
        elif cache_row is None:
            cache_reason = "miss"
        else:
            ttl_ok = cache_row.created_at >= datetime.utcnow() - timedelta(hours=24)
            msg_id_match = bool(last_msg_id and cache_row.thread_last_msg_id and cache_row.thread_last_msg_id == last_msg_id)
            internal_ms_match = bool(
                last_internal_ms is not None
                and cache_row.thread_last_internal_ms is not None
                and cache_row.thread_last_internal_ms == last_internal_ms
            )
            if ttl_ok and (msg_id_match or internal_ms_match):
                cached = dict(cache_row.summary_json)
                if "intelligence" not in cached:
                    cached["intelligence"] = build_intelligence_deterministic(
                        cached,
                        current_user_display=get_user_display_name(user),
                        me_email=me_email,
                        last_sender=last_sender,
                    )
                _normalize_action_item_owners(cached, current_user_email=user.email)
                cached["_cache"] = {"hit": True, "ttl_ok": True}
                audit_ctx["cache_hit"] = True
                hybrid_cached = cached.get("_hybrid", {}) if isinstance(cached.get("_hybrid"), dict) else {}
                audit_ctx["hybrid_applied"] = bool(hybrid_cached.get("applied", False))
                audit_ctx["hybrid_error_code"] = hybrid_cached.get("error_code")
                intelligence_cached = cached.get("intelligence", {}) if isinstance(cached.get("intelligence"), dict) else {}
                audit_ctx["intelligence_hybrid_applied"] = bool(
                    ((hybrid_cached.get("intelligence_hybrid_applied") is True) or intelligence_cached.get("_hybrid_applied") is True)
                )
                return cached
            cache_reason = "stale" if not ttl_ok else "changed"
        audit_ctx["cache_hit"] = False

        participants_from: set[str] = set()
        participants_to: set[str] = set()
        participants_cc: set[str] = set()
        timeline: list[dict] = []
        action_items: list[dict] = []
        open_questions: list[str] = []
        decisions: list[str] = []
        risk_flags: set[str] = set()
        context_lines: list[str] = []

        request_verbs = ("can", "could", "would", "should")
        decision_markers = ("we decided", "confirmed", "approved", "it is fine", "go ahead")
        action_starts = ("todo", "action:", "please", "could you", "i will", "we need to")
        risk_terms = {
            "bank": "bank mentioned",
            "wire": "wire mentioned",
            "password": "password mentioned",
            "invoice": "invoice mentioned",
            "contract": "contract mentioned",
            "visa": "visa mentioned",
            "passport": "passport mentioned",
            "money": "money mentioned",
        }

        for msg in sorted_msgs:
            payload = msg.get("payload", {})
            headers = _extract_headers(payload)
            frm = _decode_header(headers.get("From"))
            to = _decode_header(headers.get("To"))
            cc = _decode_header(headers.get("Cc"))
            subject = _decode_header(headers.get("Subject"))
            date = headers.get("Date") or ""
            bodies = _extract_bodies(payload)
            body_plain = bodies.get("text_plain") or ""
            body_html = bodies.get("text_html") or ""
            if not body_plain and body_html:
                body_plain = _html_to_text(body_html)
            if not body_plain and (payload.get("body") or {}).get("data"):
                body_plain = _decode_b64url(payload["body"]["data"])
            body = _normalize_text(body_plain)

            for raw_addr, bucket in (
                (frm, participants_from),
                (to, participants_to),
                (cc, participants_cc),
            ):
                if raw_addr:
                    for part in raw_addr.split(","):
                        p = part.strip()
                        if p:
                            bucket.add(p)

            summary = f"{subject}. {' '.join(body.split())[:200]}".strip(". ")
            timeline.append({"date": date, "from": frm, "summary": summary[:220]})
            if summary:
                context_lines.append(f"{date} | {frm} | {summary[:200]}")

            for line in body.splitlines():
                line_s = line.strip()
                if not line_s:
                    continue
                lower = line_s.lower()
                if lower.startswith(action_starts):
                    due = _extract_due_hint(line_s)
                    owner = "unknown"
                    if lower.startswith("i will"):
                        owner = frm or "unknown"
                    action_items.append({"owner": owner, "item": line_s[:300], "due": due})
                if any(marker in lower for marker in decision_markers):
                    decisions.append(line_s[:300])
                for key, label in risk_terms.items():
                    if key in lower:
                        risk_flags.add(label)

            # Sentence-level question extraction with request verbs.
            for sentence in re.split(r"(?<=[\.\?\!])\s+", body):
                s = sentence.strip()
                if s.endswith("?"):
                    sl = s.lower()
                    if any(f"{v} " in sl or f" {v} " in sl for v in request_verbs):
                        open_questions.append(s[:300])

        thread_subject = ""
        for msg in sorted_msgs:
            sub = _decode_header(_extract_headers(msg.get("payload", {})).get("Subject"))
            if sub:
                thread_subject = sub
                break

        context_block = "\n".join(context_lines)
        context_block = _normalize_text(context_block)[:2200]

        summary = {
            "thread_id": thread_id,
            "subject": thread_subject,
            "participants": {
                "from": sorted(participants_from),
                "to": sorted(participants_to),
                "cc": sorted(participants_cc),
            },
            "timeline": timeline[:6],
            "action_items": action_items[:12],
            "open_questions": open_questions[:12],
            "decisions": decisions[:12],
            "risk_flags": sorted(risk_flags),
            "context_block": context_block,
        }
        hybrid = {
            "enabled": bool(HYBRID_SUMMARIZE_ENABLED),
            "attempted": False,
            "applied": False,
            "model": HYBRID_MODEL or None,
            "error_type": None,
            "error_code": None,
            "error_message": None,
            "http_status": None,
            "intelligence_hybrid_applied": False,
        }

        if hybrid["enabled"] and not OPENAI_API_KEY:
            hybrid["error_type"] = "config"
            hybrid["error_code"] = "missing_openai_api_key"
        elif hybrid["enabled"]:
            try:
                context_digest = build_context_digest(summary)
                hybrid["attempted"] = True
                refined = refine_summary_with_llm(summary, context_digest)
                if not isinstance(refined, dict):
                    raise HybridRefinementError("schema_error", "schema_validation_failed")

                action_items = refined.get("action_items", [])
                open_questions = refined.get("open_questions", [])
                decisions = refined.get("decisions", [])
                risk_flags = refined.get("risk_flags", [])
                timeline_overrides = refined.get("timeline_overrides", [])

                if not any(len(v) > 0 for v in (action_items, open_questions, decisions, risk_flags)):
                    hybrid["error_type"] = "schema_error"
                    hybrid["error_code"] = "empty_refinement"
                else:
                    summary["action_items"] = action_items[:12]
                    summary["open_questions"] = open_questions[:12]
                    summary["decisions"] = decisions[:12]
                    summary["risk_flags"] = risk_flags[:20]
                    for override in timeline_overrides[:12]:
                        if not isinstance(override, dict):
                            continue
                        idx = override.get("index")
                        text = str(override.get("summary", "") or "").strip()
                        if isinstance(idx, int) and 0 <= idx < len(summary["timeline"]) and text:
                            summary["timeline"][idx]["summary"] = text[:220]
                    hybrid["applied"] = True

                if hybrid["applied"]:
                    log_audit_event(
                        db=db,
                        user_id=user.id,
                        account_id=account.id,
                        tool_name="gmail.thread_summarize.llm",
                        status="success",
                        http_status=200,
                        error_type=None,
                        error_code=None,
                        resolved_via_default=resolved_via_default,
                    )
                else:
                    log_audit_event(
                        db=db,
                        user_id=user.id,
                        account_id=account.id,
                        tool_name="gmail.thread_summarize.llm",
                        status="error",
                        http_status=500,
                        error_type=hybrid["error_type"] or "llm_error",
                        error_code=hybrid["error_code"] or "openai_call_failed",
                        resolved_via_default=resolved_via_default,
                    )
            except HybridRefinementError as exc:
                msg = str(exc).strip().splitlines()[0][:120]
                logger.warning("hybrid_refinement_failed type=%s msg=%s", exc.__class__.__name__, msg)
                hybrid["applied"] = False
                hybrid["error_type"] = exc.error_type
                hybrid["error_code"] = exc.error_code
                hybrid["error_message"] = exc.error_message
                hybrid["http_status"] = exc.http_status
                log_audit_event(
                    db=db,
                    user_id=user.id,
                    account_id=account.id,
                    tool_name="gmail.thread_summarize.llm",
                    status="error",
                    http_status=500,
                    error_type=hybrid["error_type"],
                    error_code=hybrid["error_code"],
                    resolved_via_default=resolved_via_default,
                )
            except Exception as exc:
                msg = str(exc).strip().splitlines()[0][:120]
                logger.warning("hybrid_refinement_failed type=%s msg=%s", exc.__class__.__name__, msg)
                hybrid["applied"] = False
                hybrid["error_type"] = "llm_error"
                hybrid["error_code"] = "openai_call_failed"
                hybrid["error_message"] = (str(exc) or "")[:300] or None
                hybrid["http_status"] = None
                log_audit_event(
                    db=db,
                    user_id=user.id,
                    account_id=account.id,
                    tool_name="gmail.thread_summarize.llm",
                    status="error",
                    http_status=500,
                    error_type=hybrid["error_type"],
                    error_code=hybrid["error_code"],
                    resolved_via_default=resolved_via_default,
                )
        intelligence = build_intelligence_deterministic(
            summary,
            current_user_display=get_user_display_name(user),
            me_email=me_email,
            last_sender=last_sender,
        )
        if hybrid["enabled"] and OPENAI_API_KEY:
            try:
                intelligence = refine_intelligence_hybrid(
                    intelligence,
                    summary,
                    me_email=me_email,
                    last_sender=last_sender,
                    user_name=get_user_display_name(user),
                )
                hybrid["intelligence_hybrid_applied"] = True
                audit_ctx["intelligence_hybrid_applied"] = True
            except HybridRefinementError as exc:
                logger.warning(
                    "intelligence_hybrid_refinement_failed type=%s code=%s",
                    exc.__class__.__name__,
                    exc.error_code,
                )
                if not hybrid.get("error_code"):
                    hybrid["error_type"] = exc.error_type
                    hybrid["error_code"] = exc.error_code
                    hybrid["error_message"] = exc.error_message
                    hybrid["http_status"] = exc.http_status

        _normalize_action_item_owners(summary, current_user_email=user.email)
        summary["_hybrid"] = hybrid
        summary["_cache"] = {"hit": False, "reason": cache_reason}
        summary["intelligence"] = intelligence
        audit_ctx["cache_hit"] = False
        audit_ctx["hybrid_applied"] = bool(hybrid.get("applied", False))
        audit_ctx["hybrid_error_code"] = hybrid.get("error_code")
        _cache_upsert(
            db=db,
            user_id=user.id,
            account_id=account.id,
            thread_id=thread_id,
            max_messages=max_items,
            hybrid_enabled=bool(HYBRID_SUMMARIZE_ENABLED),
            model=cache_model,
            thread_last_date=thread_last_date,
            thread_last_msg_id=last_msg_id,
            thread_last_internal_ms=last_internal_ms,
            summary_json=summary,
        )
        return summary

    return _run_with_audit(db, user, account_uuid, "gmail.thread_summarize", _run, audit_ctx=audit_ctx)


def gmail_triage_tool(
    db: Session,
    user: User,
    thread_id: str | None = None,
    msg_id: str | None = None,
    account_id: str | None = None,
    max_messages: int = 10,
    force_refresh: bool = False,
) -> dict:
    account_uuid = None
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        pass
    audit_ctx: dict = {"account_id": account_uuid, "resolved_via_default": False, "cache_hit": False}

    def _run():
        if not thread_id and not msg_id:
            raise HTTPException(status_code=400, detail="thread_id or msg_id is required")

        account, resolved_via_default = resolve_account(db, user, account_id)
        audit_ctx["account_id"] = account.id
        audit_ctx["resolved_via_default"] = resolved_via_default
        require_capability(account, "can_get", tool_name="gmail.triage")
        audit_ctx["hybrid_model"] = HYBRID_MODEL or None

        service = _build_service(db, account)
        resolved_thread_id = thread_id
        if msg_id:
            try:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="metadata", metadataHeaders=["From", "Date"])
                    .execute()
                )
            except HttpError as exc:
                if _auto_revoke_if_invalid_grant(db, account, exc):
                    raise HTTPException(status_code=404, detail="Gmail account not found")
                raise
            resolved_thread_id = msg.get("threadId")
        if not resolved_thread_id:
            raise HTTPException(status_code=400, detail="Unable to resolve thread")

        summary = gmail_thread_summarize_tool(
            db=db,
            user=user,
            thread_id=str(resolved_thread_id),
            max_messages=max_messages,
            account_id=account_id,
            force_refresh=force_refresh,
        )
        cache_meta = summary.get("_cache") if isinstance(summary.get("_cache"), dict) else {"hit": False}
        audit_ctx["cache_hit"] = bool(cache_meta.get("hit", False))

        try:
            thread = service.users().threads().get(userId="me", id=str(resolved_thread_id), format="full").execute()
        except HttpError as exc:
            if _auto_revoke_if_invalid_grant(db, account, exc):
                raise HTTPException(status_code=404, detail="Gmail account not found")
            raise
        latest = _latest_thread_message(thread) or {}
        headers = (latest.get("payload") or {}).get("headers") or []
        display_name = get_user_display_name(user)
        me_email = str(account.gmail_email or user.email or "").strip().lower()
        sender_ctx = _thread_sender_context(thread, me_email)
        last_sender = sender_ctx["last_external_sender"]
        sender_email = sender_ctx["last_external_sender_email"]
        date_msg = sender_ctx["last_external_msg"] or sender_ctx["last_msg"] or {}
        date_headers = (date_msg.get("payload") or {}).get("headers") or []
        last_message_date = _header_value(date_headers, "Date") or str(date_msg.get("internalDate") or "")
        unread = "UNREAD" in (latest.get("labelIds") or [])
        list_unsubscribe = bool(_header_value(headers, "List-Unsubscribe"))
        baseline = _build_triage_baseline(
            summary=summary,
            me_email=me_email,
            last_sender=last_sender,
            last_message_date=last_message_date,
            unread=unread,
            list_unsubscribe=list_unsubscribe,
            latest_from_me=bool(sender_ctx["latest_from_me"]),
            external_newer_than_self=bool(sender_ctx["external_newer_than_self"]),
            user_display_name=display_name,
        )
        hybrid = {
            "enabled": bool(HYBRID_SUMMARIZE_ENABLED),
            "attempted": False,
            "applied": False,
            "model": HYBRID_MODEL or None,
            "error_type": None,
            "error_code": None,
            "error_message": None,
            "http_status": None,
        }
        triage = dict(baseline)
        if hybrid["enabled"] and OPENAI_API_KEY:
            try:
                hybrid["attempted"] = True
                triage = refine_triage_hybrid(
                    baseline=baseline,
                    context_summary=summary,
                    me_email=me_email,
                    last_sender=last_sender,
                    user_name=display_name,
                )
                hybrid["applied"] = True
            except HybridRefinementError as exc:
                hybrid["error_type"] = exc.error_type
                hybrid["error_code"] = exc.error_code
                hybrid["error_message"] = exc.error_message
                hybrid["http_status"] = exc.http_status
        if triage.get("needs_reply"):
            sr = triage.get("suggested_reply")
            if isinstance(sr, dict):
                if not sr.get("to") and sender_email:
                    sr["to"] = sender_email
                sr = normalize_suggested_reply(
                    me_email=me_email,
                    last_sender=last_sender,
                    subject=str(sr.get("subject") or summary.get("subject") or ""),
                    body_text=str(sr.get("body_text") or ""),
                    user_name=display_name,
                    user_email=me_email,
                )
                triage["suggested_reply"] = {"to": (sender_email or str((triage.get("suggested_reply") or {}).get("to") or "")).lower(), **sr}

        triage["_hybrid"] = hybrid
        triage["_cache"] = cache_meta
        audit_ctx["hybrid_applied"] = bool(hybrid.get("applied", False))
        audit_ctx["hybrid_error_code"] = hybrid.get("error_code")
        return triage

    return _run_with_audit(db, user, account_uuid, "gmail.triage", _run, audit_ctx=audit_ctx)


def gmail_thread_reply_draft_tool(
    db: Session,
    user: User,
    thread_id: str,
    account_id: str | None = None,
    to: str | None = None,
    subject: str | None = None,
    body_text: str | None = None,
    use_suggested_reply: bool = True,
    confirmed: bool = False,
) -> dict:
    account_uuid = None
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        pass
    audit_ctx: dict = {
        "account_id": account_uuid,
        "resolved_via_default": False,
        "reply_mode": "reply",
        "override_applied": False,
        "override_reason": None,
        "original_to": None,
        "final_to": None,
        "forced_to_self": False,
    }

    def _run():
        account, resolved_via_default = resolve_account(db, user, account_id)
        audit_ctx["account_id"] = account.id
        audit_ctx["resolved_via_default"] = resolved_via_default
        require_capability(account, "can_thread_reply_draft", tool_name="gmail.thread_reply_draft")
        service = _build_service(db, account)
        try:
            thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        except HttpError as exc:
            if _auto_revoke_if_invalid_grant(db, account, exc):
                raise HTTPException(status_code=404, detail="Gmail account not found")
            raise
        latest = _latest_thread_message(thread)
        if not latest:
            raise HTTPException(status_code=404, detail="google_not_found")
        headers = (latest.get("payload") or {}).get("headers") or []
        me_email = str(account.gmail_email or user.email or "").strip().lower()
        sender_ctx = _thread_sender_context(thread, me_email)
        last_sender = sender_ctx["last_external_sender"]
        to_email = sender_ctx["last_external_sender_email"]
        if not to_email:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_type": "recipient_resolution_failed",
                    "error_code": "no_external_sender_found",
                    "message": "Unable to determine reply recipient from this thread.",
                    "remediation": "Provide explicit to=... or choose a different thread/message",
                },
            )

        summary = gmail_thread_summarize_tool(
            db=db,
            user=user,
            thread_id=thread_id,
            account_id=account_id,
            max_messages=10,
            force_refresh=False,
        )
        subject_base = str(summary.get("subject") or _header_value(headers, "Subject") or "")
        display_name = get_user_display_name(user)
        signature = get_reply_signature(user)
        reply_mode = "reply"
        override_applied = False
        override_reason = None
        incoming_to = str(to or "").strip().lower()
        incoming_subject = str(subject or "").strip()
        incoming_body = str(body_text or "").strip()

        if use_suggested_reply:
            # When use_suggested_reply is enabled, ignore manual to/subject/body_text inputs.
            if incoming_to or incoming_subject or incoming_body:
                override_reason = "ignored_external"
            triage = gmail_triage_tool(
                db=db,
                user=user,
                thread_id=thread_id,
                account_id=account_id,
                max_messages=10,
                force_refresh=False,
            )
            triage_reply = triage.get("suggested_reply")
            if isinstance(triage_reply, dict):
                to_email = str(triage_reply.get("to") or to_email).strip().lower()
                normalized_reply = {
                    "subject": str(triage_reply.get("subject") or subject_base),
                    "body_text": normalize_reply_body(
                        str(triage_reply.get("body_text") or ""),
                        signature=signature,
                        user_email=me_email,
                    ),
                }
            else:
                external_name = str(sender_ctx.get("last_external_sender_display") or "").strip()
                greeting = f"Hi {external_name}," if external_name else "Hi,"
                stub = (
                    f"{greeting}\n\n"
                    "Thanks for your message. I’ll review and follow up shortly."
                )
                normalized_reply = {
                    "subject": str(normalize_suggested_reply(
                        me_email=me_email,
                        last_sender=last_sender,
                        subject=subject_base,
                        body_text=stub,
                        user_name=display_name,
                        user_email=me_email,
                    ).get("subject")),
                    "body_text": normalize_reply_body(stub, signature=signature, user_email=me_email),
                }
        else:
            manual_to = incoming_to
            if manual_to:
                to_email = manual_to
                override_applied = True
                if manual_to == me_email:
                    reply_mode = "self_test"
                    override_reason = "self_only"
                else:
                    override_reason = "manual_override"
            manual_subject = incoming_subject or subject_base
            draft_body = incoming_body or "Thanks for your message. I will follow up shortly."
            normalized_reply = normalize_suggested_reply(
                me_email=me_email,
                last_sender=last_sender,
                subject=manual_subject,
                body_text=draft_body,
                user_name=display_name,
                user_email=me_email,
            )
        audit_ctx["reply_mode"] = reply_mode
        audit_ctx["override_applied"] = override_applied
        audit_ctx["override_reason"] = override_reason
        audit_ctx["original_to"] = to_email
        audit_ctx["final_to"] = to_email
        preview = normalized_reply["body_text"][:200]
        response = {
            "to": to_email,
            "subject": normalized_reply["subject"],
            "preview": preview,
            "reply_mode": reply_mode,
            "override_applied": override_applied,
            "override_reason": override_reason,
            "original_to": to_email,
            "final_to": to_email,
        }

        if not confirmed:
            return response

        in_reply_to = _header_value(headers, "Message-Id")
        references = _header_value(headers, "References")
        if in_reply_to and references:
            references = f"{references} {in_reply_to}".strip()
        elif in_reply_to and not references:
            references = in_reply_to

        raw = _build_reply_raw_message(
            to=to_email,
            subject=normalized_reply["subject"],
            body_text=normalized_reply["body_text"],
            in_reply_to=in_reply_to or None,
            references=references or None,
            reply_mode=reply_mode,
        )
        try:
            draft = (
                service.users()
                .drafts()
                .create(userId="me", body={"message": {"raw": raw, "threadId": thread_id}})
                .execute()
            )
        except HttpError as exc:
            if _auto_revoke_if_invalid_grant(db, account, exc):
                raise HTTPException(status_code=404, detail="Gmail account not found")
            raise
        out = {
            "draft_id": draft.get("id"),
            "thread_id": thread_id,
            "to": to_email,
            "subject": normalized_reply["subject"],
            "preview": preview,
            "reply_mode": reply_mode,
            "override_applied": override_applied,
            "override_reason": override_reason,
            "original_to": to_email,
            "final_to": to_email,
        }
        return out

    return _run_with_audit(db, user, account_uuid, "gmail.thread_reply_draft", _run, audit_ctx=audit_ctx)


def gmail_account_revoke_tool(db: Session, user: User, account_id: str) -> dict:
    account_uuid = None
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        pass

    def _run():
        account = require_gmail_account(db, user, account_id)

        # Best-effort upstream token revocation; local revoke remains authoritative.
        try:
            refresh_token = decrypt_refresh_token(account.refresh_token_enc)
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                data={"token": refresh_token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=5,
            )
        except Exception as exc:
            logger.warning(
                "gmail_revoke_upstream_failed account_id=%s error=%s",
                account.id,
                exc.__class__.__name__,
            )

        account.revoked_at = datetime.utcnow()
        db.add(account)
        db.commit()

        return {"account_id": str(account.id), "revoked": True}

    return _run_with_audit(db, user, account_uuid, "gmail.account_revoke", _run)


def gmail_set_default_tool(db: Session, user: User, account_id: str) -> dict:
    account_uuid = None
    try:
        account_uuid = uuid.UUID(account_id)
    except (ValueError, TypeError):
        pass

    def _run():
        account = require_gmail_account(db, user, account_id)
        db.execute(
            update(GmailAccount)
            .where(GmailAccount.user_id == user.id)
            .values(is_default=False)
        )
        account.is_default = True
        db.add(account)
        db.commit()
        db.refresh(account)
        return {
            "account_id": str(account.id),
            "gmail_email": account.gmail_email,
            "is_default": True,
        }

    return _run_with_audit(db, user, account_uuid, "gmail.set_default", _run)


@router.post("/accounts/{account_id}/revoke")
@limiter.limit("10/minute")
def gmail_account_revoke(
    request: Request,
    account_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return gmail_account_revoke_tool(db, current_user, account_id)


@router.post("/accounts/{account_id}/default")
@limiter.limit("10/minute")
def gmail_set_default(
    request: Request,
    account_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return gmail_set_default_tool(db, current_user, account_id)


@router.get("/accounts")
def gmail_accounts(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return gmail_accounts_list_tool(db, current_user)


@router.get("/search")
@limiter.limit("60/minute")
def gmail_search(
    request: Request,
    account_id: str | None = None,
    query: str = Query(alias="q"),
    max_results: int = 10,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return gmail_search_tool(db, current_user, account_id, query, max_results=max_results)


@router.get("/get")
@limiter.limit("120/minute")
def gmail_get(
    request: Request,
    msg_id: str,
    account_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return gmail_get_tool(db, current_user, account_id, msg_id)


@router.get("/get_body")
@limiter.limit("120/minute")
def gmail_get_body(
    request: Request,
    msg_id: str,
    account_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return gmail_get_body_tool(db, current_user, account_id, msg_id)


@router.post("/send")
@limiter.limit("20/minute")
def gmail_send(
    request: Request,
    to: str,
    subject: str,
    body_text: str,
    account_id: str | None = None,
    confirmed: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return gmail_send_tool(db, current_user, account_id, to, subject, body_text, confirmed=confirmed)


@router.post("/draft_create")
@limiter.limit("10/minute")
def gmail_draft_create(
    request: Request,
    to: str,
    subject: str,
    body_text: str,
    account_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return gmail_draft_create_tool(db, current_user, account_id, to, subject, body_text)


@router.post("/draft_send")
@limiter.limit("20/minute")
def gmail_draft_send(
    request: Request,
    draft_id: str,
    confirmed: bool,
    account_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return gmail_draft_send_tool(db, current_user, account_id, draft_id, confirmed)


@router.get("/thread_summarize")
@limiter.limit("30/minute")
def gmail_thread_summarize(
    request: Request,
    thread_id: str,
    max_messages: int = 10,
    account_id: str | None = None,
    force_refresh: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return gmail_thread_summarize_tool(
        db=db,
        user=current_user,
        thread_id=thread_id,
        max_messages=max_messages,
        account_id=account_id,
        force_refresh=force_refresh,
    )
