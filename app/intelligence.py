import re
from urllib.parse import urlparse

from .config import DEFAULT_SIGNATURE_NAME


INTELLIGENCE_VERSION = "v1"


def _contains_any(text: str, needles: list[str]) -> bool:
    lower = (text or "").lower()
    return any(n in lower for n in needles)


def _extract_entities(thread_summary: dict) -> list[dict]:
    entities: list[dict] = []
    seen: set[tuple[str, str]] = set()
    participants = thread_summary.get("participants", {}) or {}
    for group in ("from", "to", "cc"):
        for item in participants.get(group, []) or []:
            raw = str(item or "").strip()
            if not raw:
                continue
            email_match = re.search(r"<([^>]+)>", raw)
            if email_match:
                email = email_match.group(1).strip()
                name = raw.split("<", 1)[0].strip().strip('"') or email
                for row in (("person", name), ("email", email)):
                    if row not in seen:
                        seen.add(row)
                        entities.append({"type": row[0], "name": row[1]})
            else:
                entity_type = "email" if "@" in raw else "person"
                row = (entity_type, raw)
                if row not in seen:
                    seen.add(row)
                    entities.append({"type": entity_type, "name": raw})

    context = str(thread_summary.get("context_block", "") or "")
    for email in set(re.findall(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", context, flags=re.IGNORECASE)):
        row = ("email", email)
        if row not in seen:
            seen.add(row)
            entities.append({"type": "email", "name": email})
    for url in set(re.findall(r"https?://\S+", context, flags=re.IGNORECASE)):
        parsed = urlparse(url)
        host = parsed.netloc or url
        row = ("url", host)
        if row not in seen:
            seen.add(row)
            entities.append({"type": "url", "name": host})
    return entities[:20]


def _extract_deadlines(thread_summary: dict) -> list[dict]:
    context = str(thread_summary.get("context_block", "") or "")
    timeline = thread_summary.get("timeline") or []
    due_keywords = [
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
    concrete_patterns = [
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}\b",
    ]
    relative_patterns = [
        r"\bby\s+eod\b",
        r"\bend of day\b",
        r"\btomorrow\b",
        r"\btoday\b",
        r"\bthis week\b",
        r"\bnext week\b",
        r"\basap\b",
        r"\burgent\b",
        r"\bbefore\s+[^,\.;\n]{1,40}\b",
        r"\bby\s+[^,\.;\n]{1,40}\b",
    ]
    rows: list[dict] = []
    candidates: list[str] = []
    candidates.extend([x.strip() for x in context.splitlines() if x.strip()])
    candidates.extend([str((x or {}).get("summary", "")).strip() for x in timeline if str((x or {}).get("summary", "")).strip()])

    for line_s in candidates:
        lower = line_s.lower()
        if not any(k in lower for k in due_keywords):
            continue
        concrete_match = None
        for p in concrete_patterns:
            m = re.search(p, lower, flags=re.IGNORECASE)
            if m:
                concrete_match = line_s[m.start():m.end()]
                break
        if concrete_match:
            rows.append({"date": concrete_match, "text": line_s[:200], "confidence": 0.8})
            continue

        relative_match = None
        for p in relative_patterns:
            m = re.search(p, lower, flags=re.IGNORECASE)
            if m:
                relative_match = line_s[m.start():m.end()]
                break
        if relative_match:
            rows.append({"date": None, "text": relative_match[:120], "confidence": 0.5})
    return rows[:10]


def _needs_reply(thread_summary: dict, context: str, me_email: str | None, last_sender_email: str | None) -> bool:
    me = (me_email or "").strip().lower()
    sender = (last_sender_email or "").strip().lower()
    from_not_user = bool(sender) and bool(me) and sender != me
    if sender and not me:
        from_not_user = True
    request_verbs = ["please", "could you", "can you", "let me know", "send", "review", "approve"]
    has_open_questions = bool(thread_summary.get("open_questions"))
    if sender and me and sender == me and not has_open_questions:
        return False
    return from_not_user and ("?" in context or _contains_any(context, request_verbs) or has_open_questions)


def _sentiment(context: str) -> str:
    if _contains_any(context, ["unfortunately", "problem", "issue", "rejected", "error"]):
        return "negative"
    if _contains_any(context, ["thanks", "great", "approved", "fine", "good"]):
        return "positive"
    return "neutral"


def _request_type(context: str) -> str:
    if "?" in context:
        return "question"
    if _contains_any(context, ["approved", "ok", "fine", "go ahead"]):
        return "approval"
    if _contains_any(context, ["send", "review", "attach", "schedule", "confirm"]):
        return "task"
    if context.strip():
        return "info"
    return "other"


def _urgency(context: str) -> str:
    if _contains_any(context, ["asap", "urgent", "immediately", "deadline", "today", "tomorrow", "next week"]):
        return "high"
    if _contains_any(context, ["soon", "please review", "reminder", "follow up"]):
        return "medium"
    return "low"


def _normalize_subject(subject: str) -> str:
    s = (subject or "").strip()
    if not s:
        return "Re:"
    if re.match(r"^\s*re\s*:", s, flags=re.IGNORECASE):
        return s
    return f"Re: {s}"


def format_signature(user_full_name: str | None, user_email: str) -> str:
    if DEFAULT_SIGNATURE_NAME:
        return DEFAULT_SIGNATURE_NAME
    full = str(user_full_name or "").strip()
    if full:
        return full
    email = str(user_email or "").strip().lower()
    if email and "@" in email:
        local = email.split("@", 1)[0]
        local = re.sub(r"[._-]+", " ", local).strip()
        if local:
            return local.title()
    return "User"


def get_user_display_name(user) -> str:
    return format_signature(
        user_full_name=(getattr(user, "full_name", None) or getattr(user, "name", None)),
        user_email=str(getattr(user, "email", "") or ""),
    )


def get_reply_signature(user) -> str:
    return f"Best,\n{get_user_display_name(user)}"


def normalize_reply_body(body_text: str, signature: str, user_email: str | None = None) -> str:
    body = (body_text or "").strip()
    body = re.sub(
        r"(?is)\n\s*(best|best regards|regards|kind regards|thanks|thank you|sincerely),?\s*\n.*$",
        "",
        body,
    ).strip()
    if user_email:
        body = re.sub(rf"(?im)^\s*{re.escape(user_email.strip())}\s*$", "", body).strip()
    if signature.strip():
        body = f"{body}\n\n{signature.strip()}"
    return body.strip()


def normalize_suggested_reply(
    me_email: str | None,
    last_sender: dict | None,
    subject: str,
    body_text: str,
    user_name: str | None = None,
    user_email: str | None = None,
) -> dict:
    sender = last_sender or {}
    target_name = str(sender.get("name") or "").strip()
    target_email = str(sender.get("email") or "").strip().lower()
    me = (me_email or "").strip().lower()
    if target_email == me:
        target_name = ""
    greeting = f"Hi {target_name}," if target_name else "Hi,"
    body = (body_text or "").strip()
    if re.match(r"^\s*dear\s+\w+", body, flags=re.IGNORECASE):
        body = re.sub(r"^\s*dear\s+\w+\s*,?", greeting, body, count=1, flags=re.IGNORECASE)
    if not body.lower().startswith(("hi ", "hello ", "dear ")):
        body = f"{greeting}\n\n{body}"
    signature = f"Best,\n{(user_name or '').strip()}" if (user_name or "").strip() else ""
    body = normalize_reply_body(body, signature=signature, user_email=user_email)
    body = re.sub(r"(?i)authorization:\s*bearer\s+\S+", "", body)
    body = re.sub(r"(?i)access_token\s*[:=]\s*\S+", "", body)
    return {
        "subject": _normalize_subject(subject),
        "body_text": body[:1200],
    }


def _suggested_reply(
    subject: str,
    needs_reply: bool,
    request_type: str,
    me_email: str | None,
    last_sender: dict | None,
    user_name: str | None = None,
) -> dict | None:
    if not needs_reply:
        return None
    line = "Thanks for the update."
    if request_type == "question":
        line = "Thanks for the question."
    elif request_type == "approval":
        line = "Thanks for the confirmation."
    elif request_type == "task":
        line = "Thanks for the request."
    draft = (
        f"{line} I reviewed your message and I will follow up shortly. "
        "Please let me know if there is a preferred deadline. "
        "I will confirm once the requested next step is complete."
    )
    return normalize_suggested_reply(
        me_email,
        last_sender,
        subject,
        draft,
        user_name=user_name,
        user_email=me_email,
    )


def _set_action_owner_for_non_reply_actions(
    thread_summary: dict, needs_reply: bool, current_user_display: str | None
) -> None:
    if needs_reply:
        return
    action_verbs = ("send", "submit", "forward", "attach")
    items = thread_summary.get("action_items") or []
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("item") or "").lower()
        owner = str(item.get("owner") or "").strip().lower()
        if any(v in text for v in action_verbs) and owner in {"", "unknown", "none", "null"}:
            row = dict(item)
            row["owner"] = (current_user_display or "unknown").strip() or "unknown"
            normalized.append(row)
        else:
            normalized.append(item)
    thread_summary["action_items"] = normalized


def build_intelligence_deterministic(
    thread_summary: dict,
    current_user_display: str | None = None,
    me_email: str | None = None,
    last_sender: dict | None = None,
) -> dict:
    context = str(thread_summary.get("context_block", "") or "")
    subject = str(thread_summary.get("subject", "") or "")
    last_sender_email = str((last_sender or {}).get("email") or "")
    needs_reply = _needs_reply(thread_summary, context, me_email=me_email, last_sender_email=last_sender_email)
    urgency = _urgency(context)
    sentiment = _sentiment(context)
    request_type = _request_type(context)
    deadlines = _extract_deadlines(thread_summary)
    entities = _extract_entities(thread_summary)
    _set_action_owner_for_non_reply_actions(thread_summary, needs_reply, current_user_display)
    safety_flags: list[str] = []
    if _contains_any(context, ["contract", "visa", "lawyer"]):
        safety_flags.append("legal")
    if _contains_any(context, ["invoice", "payment", "bank", "wire"]):
        safety_flags.append("financial")
    if _contains_any(context, ["doctor", "hospital"]):
        safety_flags.append("medical")
    confidence = 0.6
    signal_count = 0
    for flag in [needs_reply, bool(deadlines), urgency != "low", request_type != "info"]:
        if flag:
            signal_count += 1
    if signal_count >= 3:
        confidence = 0.8
    elif signal_count <= 1:
        confidence = 0.4

    return {
        "version": INTELLIGENCE_VERSION,
        "urgency": urgency,
        "sentiment": sentiment,
        "request_type": request_type,
        "needs_reply": bool(needs_reply),
        "suggested_reply": _suggested_reply(
            subject,
            needs_reply,
            request_type,
            me_email=me_email,
            last_sender=last_sender,
            user_name=current_user_display,
        ),
        "entities": entities,
        "deadlines": deadlines,
        "confidence": float(confidence),
        "safety_flags": safety_flags,
    }
