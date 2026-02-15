import os

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - safe fallback if dotenv is unavailable
    def load_dotenv(*args, **kwargs):
        return False

load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip().lower()
    return v in {"1", "true", "t", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except Exception:
        return default

DATABASE_URL = os.getenv("DATABASE_URL", "")
APP_JWT_SECRET = os.getenv("APP_JWT_SECRET", "")
FERNET_KEY = os.getenv("FERNET_KEY", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

GOOGLE_LOGIN_CLIENT_ID = os.getenv("GOOGLE_LOGIN_CLIENT_ID", "")
GOOGLE_LOGIN_CLIENT_SECRET = os.getenv("GOOGLE_LOGIN_CLIENT_SECRET", "")

GOOGLE_GMAIL_CLIENT_ID = os.getenv("GOOGLE_GMAIL_CLIENT_ID", "")
GOOGLE_GMAIL_CLIENT_SECRET = os.getenv("GOOGLE_GMAIL_CLIENT_SECRET", "")

JWT_ALGORITHM = "HS256"
JWT_EXP_MINUTES = env_int("JWT_EXP_MINUTES", 60)
STATE_TTL_MINUTES = env_int("STATE_TTL_MINUTES", 10)

OIDC_SCOPES = ["openid", "email", "profile"]
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
]

LOGIN_REDIRECT_URI = f"{BASE_URL}/oauth/login/callback"
GMAIL_REDIRECT_URI = f"{BASE_URL}/oauth/gmail/callback"

DEV_CORS_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]

HYBRID_SUMMARIZE_ENABLED = env_bool("HYBRID_SUMMARIZE_ENABLED", False)
_hybrid_model = os.getenv("HYBRID_MODEL", "gpt-4.1-mini").strip()
HYBRID_MODEL = _hybrid_model or None
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
HYBRID_MAX_CONTEXT_CHARS = env_int("HYBRID_MAX_CONTEXT_CHARS", 2000)
DEV_DEBUG = env_bool("DEV_DEBUG", False)
DEV_DEBUG_ENDPOINTS_ENABLED = env_bool("DEV_DEBUG_ENDPOINTS_ENABLED", False)
DEBUG = env_bool("DEBUG", False)
DEV_FORCE_TO_SELF = env_bool("DEV_FORCE_TO_SELF", False)
DEV_FORCE_TO_SELF_ALLOWLIST = os.getenv("DEV_FORCE_TO_SELF_ALLOWLIST", "").strip()
DEV_FORCE_TO_SELF_REASON = os.getenv("DEV_FORCE_TO_SELF_REASON", "self_only").strip() or "self_only"
DEFAULT_SIGNATURE_NAME = os.getenv("DEFAULT_SIGNATURE_NAME", "").strip() or None

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")
if not APP_JWT_SECRET:
    raise RuntimeError("APP_JWT_SECRET is not set")
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY is not set")
if not BASE_URL:
    raise RuntimeError("BASE_URL is not set")
if not GOOGLE_LOGIN_CLIENT_ID or not GOOGLE_LOGIN_CLIENT_SECRET:
    raise RuntimeError("GOOGLE_LOGIN_CLIENT_ID/SECRET must be set")
if not GOOGLE_GMAIL_CLIENT_ID or not GOOGLE_GMAIL_CLIENT_SECRET:
    raise RuntimeError("GOOGLE_GMAIL_CLIENT_ID/SECRET must be set")
