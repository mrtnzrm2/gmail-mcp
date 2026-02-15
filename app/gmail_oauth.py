import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .auth import get_current_user
from .config import (
    DEBUG,
    GMAIL_REDIRECT_URI,
    GMAIL_SCOPES,
    GOOGLE_GMAIL_CLIENT_ID,
    GOOGLE_GMAIL_CLIENT_SECRET,
    STATE_TTL_MINUTES,
)
from .crypto import encrypt_refresh_token
from .dev_debug import set_last_error
from .db import get_db
from .models import GmailAccount, OAuthState, User
from .rate_limit import limiter

router = APIRouter(tags=["gmail_oauth"])
logger = logging.getLogger(__name__)


def _gmail_client_config() -> dict:
    return {
        "web": {
            "client_id": GOOGLE_GMAIL_CLIENT_ID,
            "client_secret": GOOGLE_GMAIL_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _build_gmail_flow() -> Flow:
    return Flow.from_client_config(
        _gmail_client_config(),
        scopes=GMAIL_SCOPES,
        redirect_uri=GMAIL_REDIRECT_URI,
    )


def _upsert_gmail_account_for_user(
    db: Session,
    user_id,
    gmail_email: str,
    scopes: str,
    refresh_token: str | None,
) -> GmailAccount:
    existing = db.scalar(
        select(GmailAccount).where(
            GmailAccount.user_id == user_id,
            GmailAccount.gmail_email == gmail_email,
        )
    )

    if existing:
        if refresh_token:
            existing.refresh_token_enc = encrypt_refresh_token(refresh_token)
        existing.scopes = scopes
        existing.revoked_at = None
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing

    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="No refresh token received. Revoke app access and re-authorize with consent.",
        )

    account = GmailAccount(
        user_id=user_id,
        gmail_email=gmail_email,
        is_default=False,
        refresh_token_enc=encrypt_refresh_token(refresh_token),
        scopes=scopes,
        revoked_at=None,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _ensure_default_if_missing(db: Session, user_id, account: GmailAccount) -> None:
    has_active_default = db.scalar(
        select(GmailAccount.id).where(
            GmailAccount.user_id == user_id,
            GmailAccount.revoked_at.is_(None),
            GmailAccount.is_default.is_(True),
        )
    )
    if has_active_default:
        return

    # Keep one default account per user.
    db.execute(
        update(GmailAccount)
        .where(GmailAccount.user_id == user_id)
        .values(is_default=False)
    )
    account.is_default = True
    db.add(account)
    db.commit()


@router.get("/oauth/gmail/start")
@limiter.limit("30/hour")
def oauth_gmail_start(
    request: Request,
    login_hint: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="Missing access token")

        state = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(minutes=STATE_TTL_MINUTES)
        db.add(
            OAuthState(
                state=state,
                user_id=current_user.id,
                purpose="gmail_connect",
                expires_at=expires_at,
            )
        )
        db.commit()

        flow = _build_gmail_flow()
        redirect_params = {
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "select_account consent",
            "state": state,
        }
        if login_hint:
            redirect_params["login_hint"] = login_hint
        auth_url, _ = flow.authorization_url(**redirect_params)
        logger.info("oauth_gmail_start_redirect url=%s login_hint=%s", auth_url, login_hint)
        return RedirectResponse(auth_url, status_code=302)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("oauth_gmail_start_failed")
        set_last_error(exc, where="/oauth/gmail/start")
        if DEBUG:
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "oauth_gmail_start_failed",
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
        raise HTTPException(status_code=500, detail="Internal Server Error")


@router.get("/oauth/gmail/callback")
@limiter.limit("30/hour")
def oauth_gmail_callback(request: Request, db: Session = Depends(get_db)):
    state = request.query_params.get("state")
    if not state:
        raise HTTPException(status_code=400, detail="Missing OAuth state")

    state_obj = db.scalar(
        select(OAuthState).where(OAuthState.state == state, OAuthState.purpose == "gmail_connect")
    )
    now = datetime.utcnow()
    if not state_obj or state_obj.expires_at < now or not state_obj.user_id:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    db.execute(delete(OAuthState).where(OAuthState.state == state))
    db.commit()

    flow = _build_gmail_flow()
    try:
        flow.fetch_token(authorization_response=str(request.url))
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Google token exchange failed: {e.__class__.__name__}: {e}",
        )

    creds = flow.credentials

    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = service.users().getProfile(userId="me").execute()
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to fetch Gmail profile")

    gmail_email = profile.get("emailAddress")
    if not gmail_email:
        raise HTTPException(status_code=400, detail="Google did not return Gmail email")

    scopes = " ".join(sorted(creds.scopes or GMAIL_SCOPES))
    account_row = _upsert_gmail_account_for_user(
        db=db,
        user_id=state_obj.user_id,
        gmail_email=gmail_email,
        scopes=scopes,
        refresh_token=creds.refresh_token,
    )
    _ensure_default_if_missing(db, state_obj.user_id, account_row)

    return JSONResponse(
        {
            "message": "Gmail account connected",
            "account_id": str(account_row.id),
            "gmail_email": account_row.gmail_email,
        }
    )
