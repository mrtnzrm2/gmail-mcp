"""
app.py — minimal multi-account Gmail OAuth + test endpoints (local dev)

What this provides
- OAuth flow at:
    GET  /oauth/start
    GET  /oauth/callback
- Token storage per Gmail account:
    tokens/<email>.json
- Test endpoints:
    GET  /accounts
    GET  /gmail/search
    GET  /gmail/get
    POST /gmail/send   (requires confirmed=true)

Local dev note (HTTP callback):
- In a terminal before running uvicorn:
    export OAUTHLIB_INSECURE_TRANSPORT=1
"""

import os
import json
import base64
import argparse
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------
# Configuration
# ---------------------------

APP_NAME = "gmail-mcp-local"
TOKENS_DIR = "tokens"
CLIENT_SECRETS_FILE = "client_secret.json"

# Keep scopes minimal
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# Local redirect URI must match exactly what you configured in Google Cloud OAuth client.
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8000/oauth/callback")

# Safety: block sending unless confirmed=true
REQUIRE_SEND_CONFIRMATION = True

# ---------------------------
# App
# ---------------------------

app = FastAPI(title=APP_NAME)

os.makedirs(TOKENS_DIR, exist_ok=True)


# ---------------------------
# Helpers
# ---------------------------

def _assert_client_secrets_present() -> None:
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise HTTPException(
            status_code=500,
            detail=f"Missing {CLIENT_SECRETS_FILE}. Place your downloaded OAuth JSON here.",
        )


def _token_path(email: str) -> str:
    # Gmail addresses can include '+'; safe for filenames on macOS/Linux.
    return os.path.join(TOKENS_DIR, f"{email}.json")


def _load_creds(email: str) -> Credentials:
    path = _token_path(email)
    if not os.path.exists(path):
        raise HTTPException(
            status_code=404,
            detail=f"No token file for {email}. Run OAuth first: GET /oauth/start",
        )
    try:
        return Credentials.from_authorized_user_file(path, SCOPES)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load credentials: {e}")


def _save_creds_json(email: str, creds: Credentials) -> None:
    path = _token_path(email)
    with open(path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())


def _has_refresh_token(creds_json: Dict[str, Any]) -> bool:
    # In stored JSON, refresh token is usually under "refresh_token"
    rt = creds_json.get("refresh_token")
    return bool(rt and isinstance(rt, str) and rt.strip())


def _gmail_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds)


def _safe_http_error(e: HttpError) -> str:
    try:
        status = getattr(e, "status_code", None) or getattr(getattr(e, "resp", None), "status", "unknown")
        reason = str(getattr(e, "reason", "") or "gmail api error").strip()
        return f"{status}: {reason[:160]}"
    except Exception:
        return "gmail api error"


# ---------------------------
# Basic health
# ---------------------------

@app.get("/")
def root():
    return {
        "status": "ok",
        "app": APP_NAME,
        "redirect_uri": REDIRECT_URI,
        "scopes": SCOPES,
        "tokens_dir": TOKENS_DIR,
    }


# ---------------------------
# OAuth flow
# ---------------------------

@app.get("/oauth/start")
def oauth_start():
    """
    Starts OAuth; redirect to Google consent screen.
    """
    _assert_client_secrets_present()

    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    authorization_url, _state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="false",
    )

    return RedirectResponse(authorization_url)


@app.get("/oauth/callback")
async def oauth_callback(request: Request):
    """
    Handles OAuth callback; exchanges code for tokens; stores creds under tokens/<email>.json.
    """
    _assert_client_secrets_present()

    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    try:
        flow.fetch_token(authorization_response=str(request.url))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}")

    creds = flow.credentials

    # Determine Gmail address via Gmail profile (reliable; no id_token needed)
    try:
        service = _gmail_service(creds)
        profile = service.users().getProfile(userId="me").execute()
        email = profile["emailAddress"]
    except HttpError as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch Gmail profile: {_safe_http_error(e)}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch Gmail profile: {e}")

    # Save credentials
    _save_creds_json(email, creds)

    # Check refresh token presence (critical)
    with open(_token_path(email), "r", encoding="utf-8") as f:
        stored = json.load(f)

    return {
        "message": f"OAuth successful for {email}",
        "token_file": _token_path(email),
        "has_refresh_token": _has_refresh_token(stored),
        "note": (
            "If has_refresh_token is false, revoke app access at https://myaccount.google.com/permissions "
            "and re-run /oauth/start in an incognito window."
        ),
    }


# ---------------------------
# Token/account utilities
# ---------------------------

@app.get("/accounts")
def list_accounts() -> List[str]:
    """
    Lists emails for which token files exist.
    """
    if not os.path.exists(TOKENS_DIR):
        return []
    emails = []
    for fn in os.listdir(TOKENS_DIR):
        if fn.endswith(".json"):
            emails.append(fn[:-5])
    return sorted(emails)


@app.get("/accounts/check")
def check_accounts() -> Dict[str, Any]:
    """
    Checks whether each stored token file includes a refresh_token.
    """
    results = {}
    for email in list_accounts():
        path = _token_path(email)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            results[email] = {
                "token_file": path,
                "has_refresh_token": _has_refresh_token(data),
                "expiry": data.get("expiry"),
                "scopes": data.get("scopes", []),
            }
        except Exception as e:
            results[email] = {"token_file": path, "error": str(e)}
    return results


# ---------------------------
# Gmail test endpoints
# ---------------------------

@app.get("/gmail/search")
def gmail_search(account: str, q: str, max_results: int = 5) -> List[Dict[str, str]]:
    """
    Example q: newer_than:7d, from:someone@example.com, subject:"Hello"
    """
    creds = _load_creds(account)
    try:
        service = _gmail_service(creds)
        resp = service.users().messages().list(
            userId="me",
            q=q,
            maxResults=max_results,
        ).execute()
        return resp.get("messages", []) or []
    except HttpError as e:
        raise HTTPException(status_code=400, detail=f"Gmail search failed: {_safe_http_error(e)}")
    except Exception:
        raise HTTPException(status_code=500, detail="Gmail search failed")


@app.get("/gmail/get")
def gmail_get(account: str, msg_id: str) -> Dict[str, Any]:
    """
    Returns metadata + snippet for a message.
    """
    creds = _load_creds(account)
    try:
        service = _gmail_service(creds)
        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()

        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        return {
            "id": msg.get("id"),
            "threadId": msg.get("threadId"),
            "snippet": msg.get("snippet"),
            "from": headers.get("from"),
            "to": headers.get("to"),
            "subject": headers.get("subject"),
            "date": headers.get("date"),
            "labelIds": msg.get("labelIds", []),
        }
    except HttpError as e:
        raise HTTPException(status_code=400, detail=f"Gmail get failed: {_safe_http_error(e)}")
    except Exception:
        raise HTTPException(status_code=500, detail="Gmail get failed")

def _b64url_decode(data: str) -> str:
    # Gmail uses base64url without padding sometimes
    data = data.replace("-", "+").replace("_", "/")
    pad = "=" * (-len(data) % 4)
    return base64.b64decode(data + pad).decode("utf-8", errors="replace")

def _extract_text_from_payload(payload: dict) -> dict:
    """
    Returns {"text_plain": str|None, "text_html": str|None}
    Walks the MIME tree and collects the first text/plain and text/html bodies found.
    """
    text_plain = None
    text_html = None

    def walk(part: dict):
        nonlocal text_plain, text_html
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")

        if data and mime == "text/plain" and text_plain is None:
            text_plain = _b64url_decode(data)
        elif data and mime == "text/html" and text_html is None:
            text_html = _b64url_decode(data)

        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return {"text_plain": text_plain, "text_html": text_html}

@app.get("/gmail/get_body")
def gmail_get_body(account: str, msg_id: str):
    """
    Fetch full body (prefers text/plain, falls back to text/html).
    """
    creds = _load_creds(account)
    try:
        service = _gmail_service(creds)
        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="full",
        ).execute()

        payload = msg.get("payload", {}) or {}
        extracted = _extract_text_from_payload(payload)

        # If it's a simple single-part message, body.data might exist at top level
        if extracted["text_plain"] is None and extracted["text_html"] is None:
            top_data = (payload.get("body", {}) or {}).get("data")
            if top_data:
                decoded = _b64url_decode(top_data)
                if payload.get("mimeType") == "text/html":
                    extracted["text_html"] = decoded
                else:
                    extracted["text_plain"] = decoded

        return {
            "id": msg.get("id"),
            "threadId": msg.get("threadId"),
            "text_plain": extracted["text_plain"],
            "text_html": extracted["text_html"],
        }
    except HttpError as e:
        raise HTTPException(status_code=400, detail=f"Gmail get_body failed: {_safe_http_error(e)}")
    except Exception:
        raise HTTPException(status_code=500, detail="Gmail get_body failed")

@app.post("/gmail/send")
def gmail_send(
    account: str,
    to: str,
    subject: str,
    body: str,
    confirmed: bool = False,
) -> Dict[str, Any]:
    """
    Sends an email. Requires confirmed=true by default for safety.
    """
    if REQUIRE_SEND_CONFIRMATION and not confirmed:
        return {
            "error": "Not sent. Set confirmed=true to send.",
            "required": {"confirmed": True},
            "preview": {"from": account, "to": to, "subject": subject, "body": body[:200]},
        }

    creds = _load_creds(account)
    try:
        service = _gmail_service(creds)

        message = EmailMessage()
        message["To"] = to
        message["From"] = account
        message["Subject"] = subject
        message.set_content(body)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()

        return {"status": "sent", "id": sent.get("id"), "threadId": sent.get("threadId")}
    except HttpError as e:
        raise HTTPException(status_code=400, detail=f"Gmail send failed: {_safe_http_error(e)}")
    except Exception:
        raise HTTPException(status_code=500, detail="Gmail send failed")


def _run_smoke() -> int:
    accounts = list_accounts()
    print(f"accounts: {accounts}")
    if not accounts:
        print("smoke: no accounts found in tokens/")
        return 1

    account = accounts[0]
    print(f"smoke: using account={account}")

    try:
        results = gmail_search(account=account, q="newer_than:1d", max_results=5)
    except HTTPException as e:
        print(f"smoke: search failed ({e.detail})")
        return 2

    print(f"smoke: search count={len(results)}")
    if not results:
        print("smoke: no recent messages")
        return 0

    msg_id = results[0].get("id")
    if not msg_id:
        print("smoke: first search result missing id")
        return 3

    try:
        meta = gmail_get(account=account, msg_id=msg_id)
    except HTTPException as e:
        print(f"smoke: get failed ({e.detail})")
        return 4

    print(
        "smoke: first message",
        {
            "id": meta.get("id"),
            "threadId": meta.get("threadId"),
            "subject": meta.get("subject"),
            "date": meta.get("date"),
            "from": meta.get("from"),
        },
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gmail MCP local app utilities")
    parser.add_argument("--smoke", action="store_true", help="Run a local smoke test against stored accounts")
    args = parser.parse_args()
    if args.smoke:
        raise SystemExit(_run_smoke())
