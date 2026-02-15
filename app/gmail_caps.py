from fastapi import HTTPException

S_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
S_SEND = "https://www.googleapis.com/auth/gmail.send"
S_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
S_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
S_METADATA = "https://www.googleapis.com/auth/gmail.metadata"


def parse_scopes(scopes_value: str | None) -> set[str]:
    text = (scopes_value or "").strip()
    if not text:
        return set()
    return {s for s in text.split(" ") if s}


def compute_capabilities(scopes: set[str]) -> dict[str, bool]:
    read_any = bool(scopes.intersection({S_READONLY, S_MODIFY, S_METADATA}))
    send_any = S_SEND in scopes
    draft_any = bool(scopes.intersection({S_COMPOSE, S_MODIFY}))
    modify = S_MODIFY in scopes
    return {
        "can_search": read_any,
        "can_get": read_any,
        "can_send": send_any,
        "can_draft": draft_any,
        "can_thread_reply_draft": draft_any,
        "can_label_modify": modify,
        "can_mark_read": modify,
    }


class InsufficientScopeError(HTTPException):
    def __init__(self, cap_key: str, detail=None, error_code: str | None = None):
        super().__init__(status_code=403, detail=detail or "Insufficient Gmail scope")
        self.error_type = "insufficient_scope"
        self.error_code = error_code or f"missing_{cap_key}"


def require_capability(account, cap_key: str, *, tool_name: str):
    caps = compute_capabilities(parse_scopes(getattr(account, "scopes", "")))
    if not caps.get(cap_key, False):
        if cap_key in {"can_draft", "can_thread_reply_draft"}:
            raise InsufficientScopeError(
                cap_key,
                detail={
                    "error": "insufficient_scope",
                    "message": "This Gmail account is connected without gmail.compose, so draft creation is not allowed.",
                    "remediation": "Reconnect the account by visiting /oauth/gmail/start and granting gmail.compose.",
                    "required_scopes": [S_COMPOSE],
                },
                error_code="missing_gmail_compose",
            )
        raise InsufficientScopeError(cap_key)
