import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
from jose import JWTError, jwt
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .config import (
    APP_JWT_SECRET,
    BASE_URL,
    JWT_ALGORITHM,
    JWT_EXP_MINUTES,
    STATE_TTL_MINUTES,
    GOOGLE_LOGIN_CLIENT_ID,
    GOOGLE_LOGIN_CLIENT_SECRET,
)
from .db import get_db
from .models import OAuthState, User
from .rate_limit import limiter

router = APIRouter(tags=["auth"])
LOGIN_SCOPES = ["openid", "email", "profile"]


def _extract_full_name(info: dict) -> str | None:
    name = str(info.get("name") or "").strip()
    if name:
        return name
    given = str(info.get("given_name") or "").strip()
    family = str(info.get("family_name") or "").strip()
    joined = " ".join(x for x in [given, family] if x).strip()
    return joined or None


def _login_client_config() -> dict:
    return {
        "web": {
            "client_id": GOOGLE_LOGIN_CLIENT_ID,
            "client_secret": GOOGLE_LOGIN_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _build_login_flow() -> Flow:
    redirect_uri = f"{BASE_URL}/oauth/login/callback"
    return Flow.from_client_config(
        _login_client_config(),
        scopes=LOGIN_SCOPES,
        redirect_uri=redirect_uri,
    )


def _create_access_token(user_id: uuid.UUID, email: str) -> str:
    now = datetime.utcnow()
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=JWT_EXP_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, APP_JWT_SECRET, algorithm=JWT_ALGORITHM)


def _set_access_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=JWT_EXP_MINUTES * 60,
        path="/",
    )


def _parse_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> User:
    token = _parse_bearer_token(authorization) or request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing access token")

    try:
        payload = jwt.decode(token, APP_JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        uid = uuid.UUID(user_id)
    except (JWTError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    user = db.scalar(select(User).where(User.id == uid))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    request.state.auth_user_id = user.id
    return user


@router.get("/oauth/login/start")
@limiter.limit("30/hour")
def oauth_login_start(request: Request, db: Session = Depends(get_db)):
    redirect_uri = f"{BASE_URL}/oauth/login/callback"
    state = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=STATE_TTL_MINUTES)
    db.add(OAuthState(state=state, user_id=None, purpose="login", expires_at=expires_at))
    db.commit()

    flow = Flow.from_client_config(
        _login_client_config(),
        scopes=LOGIN_SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, _ = flow.authorization_url(
        state=state,
        prompt="consent",
        include_granted_scopes="false",
    )
    return RedirectResponse(auth_url)


@router.get("/oauth/login/callback")
@limiter.limit("30/hour")
def oauth_login_callback(request: Request, db: Session = Depends(get_db)):
    returned_scope = request.query_params.get("scope", "")
    if "https://www.googleapis.com/auth/gmail." in returned_scope:
        raise HTTPException(
            status_code=400,
            detail="Login callback received Gmail scope. Use /oauth/gmail/start for Gmail consent.",
        )

    state = request.query_params.get("state")
    if not state:
        raise HTTPException(status_code=400, detail="Missing OAuth state")

    state_obj = db.scalar(
        select(OAuthState).where(OAuthState.state == state, OAuthState.purpose == "login")
    )
    now = datetime.utcnow()
    if not state_obj or state_obj.expires_at < now:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    db.execute(delete(OAuthState).where(OAuthState.state == state))
    db.commit()

    redirect_uri = f"{BASE_URL}/oauth/login/callback"
    flow = Flow.from_client_config(
        _login_client_config(),
        scopes=LOGIN_SCOPES,
        redirect_uri=redirect_uri,
    )
    try:
        flow.fetch_token(authorization_response=str(request.url))
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Google token exchange failed: {e.__class__.__name__}: {e}",
        )

    credentials = flow.credentials
    if not credentials.id_token:
        raise HTTPException(status_code=400, detail="Google did not return an id_token")

    try:
        info = id_token.verify_oauth2_token(
            credentials.id_token,
            google_requests.Request(),
            GOOGLE_LOGIN_CLIENT_ID,
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to verify Google id_token")

    email = info.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Google account email is missing")
    full_name = _extract_full_name(info)

    stmt = (
        insert(User)
        .values(email=email, full_name=full_name)
        .on_conflict_do_update(index_elements=[User.email], set_={"email": email, "full_name": full_name})
        .returning(User.id, User.email, User.full_name)
    )
    user_row = db.execute(stmt).one()
    db.commit()

    token = _create_access_token(user_row.id, user_row.email)
    response = JSONResponse(
        {
            "message": "Login successful",
            "user_id": str(user_row.id),
            "email": user_row.email,
        }
    )
    _set_access_cookie(response, token, secure=(request.url.scheme == "https"))
    return response


@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {"id": str(current_user.id), "email": current_user.email}
