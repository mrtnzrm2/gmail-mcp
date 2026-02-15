import os
import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from . import models
from .auth import get_current_user, router as auth_router
from .config import (
    BASE_URL,
    DEBUG,
    DEV_DEBUG,
    DEV_DEBUG_ENDPOINTS_ENABLED,
    DEV_CORS_ORIGINS,
    HYBRID_MODEL,
    HYBRID_MAX_CONTEXT_CHARS,
    HYBRID_SUMMARIZE_ENABLED,
    OPENAI_API_KEY,
)
from .dev_debug import get_last_error
from .db import Base, SessionLocal, engine
from .gmail_oauth import router as gmail_oauth_router
from .gmail_tools import router as gmail_router
from .mcp import router as mcp_router
from .models import AuditEvent, User
from .rate_limit import limiter
from .safety import dev_force_to_self_enabled
from .ui import router as ui_router

app = FastAPI(title="Gmail MCP Server", version="0.1.0")
logger = logging.getLogger(__name__)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=DEV_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(gmail_oauth_router)
app.include_router(gmail_router)
app.include_router(mcp_router)
app.include_router(ui_router)


def _is_local_dev() -> bool:
    env = os.getenv("ENV", "").lower()
    if env in {"dev", "development", "local"}:
        return True
    return BASE_URL.startswith("http://localhost") or BASE_URL.startswith("http://127.0.0.1")


@app.on_event("startup")
def startup() -> None:
    if _is_local_dev():
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

    # Ensure required tables exist for local/dev deployment.
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS users
                ADD COLUMN IF NOT EXISTS full_name text NULL
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS resolved_via_default boolean NOT NULL DEFAULT false
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS hybrid_applied boolean NULL
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ALTER COLUMN hybrid_applied SET DEFAULT false
                """
            )
        )
        conn.execute(
            text(
                """
                UPDATE audit_events SET hybrid_applied=false
                WHERE hybrid_applied IS NULL
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ALTER COLUMN hybrid_applied SET NOT NULL
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS cache_hit boolean NOT NULL DEFAULT false
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS hybrid_model text NULL
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS hybrid_error_code text NULL
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS intelligence_applied boolean NOT NULL DEFAULT false
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS intelligence_hybrid_applied boolean NOT NULL DEFAULT false
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS intelligence_version text NULL
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS forced_to_self boolean NOT NULL DEFAULT false
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS reply_mode text NULL
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS override_applied boolean NOT NULL DEFAULT false
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS override_reason text NULL
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS original_to text NULL
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS audit_events
                ADD COLUMN IF NOT EXISTS final_to text NULL
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS thread_summaries_cache
                ADD COLUMN IF NOT EXISTS thread_last_msg_id text NULL
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE IF EXISTS thread_summaries_cache
                ADD COLUMN IF NOT EXISTS thread_last_internal_ms bigint NULL
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_thread_summaries_cache_user_account_thread
                ON thread_summaries_cache (user_id, account_id, thread_id)
                """
            )
        )
    logger.info("Rate limiter key function: rate_limit_key")


@app.middleware("http")
async def audit_auth_failures(request: Request, call_next):
    response = await call_next(request)
    if response.status_code not in (401, 403):
        return response

    user_id = getattr(request.state, "auth_user_id", None)
    if not user_id:
        return response

    db = SessionLocal()
    try:
        code = "unauthorized" if response.status_code == 401 else "forbidden"
        db.add(
            AuditEvent(
                user_id=user_id,
                account_id=None,
                tool_name="http.request",
                status="error",
                http_status=response.status_code,
                error_type="auth_error",
                error_code=code,
                resolved_via_default=False,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
    return response


@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/health")
def health():
    env_raw = os.getenv("ENV", "").strip().lower()
    env_name = "dev" if env_raw in {"dev", "development", "local"} else "prod"
    return {
        "status": "ok",
        "hybrid_enabled": bool(HYBRID_SUMMARIZE_ENABLED),
        "openai_key_present": bool(OPENAI_API_KEY),
        "model": HYBRID_MODEL or None,
        "dev_force_to_self": dev_force_to_self_enabled(),
        "env": env_name,
    }


@app.get("/debug/last_error")
def debug_last_error():
    if not DEBUG:
        raise HTTPException(status_code=404, detail="Not found")
    return get_last_error() or {"detail": "none"}


@app.get("/debug/config")
def debug_config():
    if not DEV_DEBUG:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "hybrid_enabled": bool(HYBRID_SUMMARIZE_ENABLED),
        "model": HYBRID_MODEL,
        "max_context_chars": HYBRID_MAX_CONTEXT_CHARS,
        "openai_key_present": bool(OPENAI_API_KEY),
    }


@app.get("/debug/hybrid")
def debug_hybrid():
    if not DEV_DEBUG:
        raise HTTPException(status_code=404, detail="Not found")
    sdk_version = None
    try:
        import openai  # type: ignore

        sdk_version = getattr(openai, "__version__", None)
    except Exception:
        sdk_version = None
    return {
        "enabled": bool(HYBRID_SUMMARIZE_ENABLED),
        "model": HYBRID_MODEL or None,
        "openai_key_present": bool(OPENAI_API_KEY),
        "sdk_version": sdk_version,
    }


@app.get("/debug/token")
def debug_token(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if not DEV_DEBUG_ENDPOINTS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Missing access token")

    return {
        "token": token,
        "user_id": str(current_user.id),
        "email": current_user.email,
    }
