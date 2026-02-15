from fastapi import Request
from jose import JWTError, jwt

from .config import APP_JWT_SECRET, JWT_ALGORITHM


def _fallback_ip_key(request: Request) -> str:
    client_host = request.client.host if request.client else "unknown"
    return f"ip:{client_host}"


def rate_limit_key(request: Request) -> str:
    try:
        auth_header = request.headers.get("authorization", "")
        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return _fallback_ip_key(request)

        token = parts[1].strip()
        payload = jwt.decode(token, APP_JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("sub")
        if user_id:
            return f"user:{user_id}"
    except (JWTError, ValueError, TypeError):
        pass
    except Exception:
        pass
    return _fallback_ip_key(request)
