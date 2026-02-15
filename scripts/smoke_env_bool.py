#!/usr/bin/env python3
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ensure config import does not fail in minimal environments.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://user:pass@localhost:5432/db")
os.environ.setdefault("APP_JWT_SECRET", "dev-secret")
os.environ.setdefault("FERNET_KEY", "dev-fernet-key")
os.environ.setdefault("GOOGLE_LOGIN_CLIENT_ID", "dev-login-client-id")
os.environ.setdefault("GOOGLE_LOGIN_CLIENT_SECRET", "dev-login-client-secret")
os.environ.setdefault("GOOGLE_GMAIL_CLIENT_ID", "dev-gmail-client-id")
os.environ.setdefault("GOOGLE_GMAIL_CLIENT_SECRET", "dev-gmail-client-secret")

from app.config import env_bool  # noqa: E402


def check(name: str, value, expected: bool):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    actual = env_bool(name, default=False)
    assert actual is expected, f"{name}={value!r}: expected {expected}, got {actual}"


def main():
    check("X_BOOL_TEST", "true", True)
    check("X_BOOL_TEST", "TRUE", True)
    check("X_BOOL_TEST", "1", True)
    check("X_BOOL_TEST", "yes", True)
    check("X_BOOL_TEST", "on", True)
    check("X_BOOL_TEST", "false", False)
    check("X_BOOL_TEST", "0", False)
    check("X_BOOL_TEST", "no", False)
    check("X_BOOL_TEST", "", False)
    check("X_BOOL_TEST", None, False)
    print("env_bool smoke test passed")


if __name__ == "__main__":
    main()
