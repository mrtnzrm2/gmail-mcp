#!/usr/bin/env python3
import argparse
import os
from urllib.parse import parse_qs, urlparse

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-check /oauth/gmail/start redirect")
    parser.add_argument("--base-url", default=os.getenv("BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--jwt", default=os.getenv("JWT") or os.getenv("JWT_TOKEN"))
    parser.add_argument("--login-hint", default=os.getenv("LOGIN_HINT"))
    args = parser.parse_args()

    if not args.jwt:
        print("Set --jwt or JWT/JWT_TOKEN env var")
        return 2

    params = {}
    if args.login_hint:
        params["login_hint"] = args.login_hint

    resp = requests.get(
        f"{args.base_url.rstrip('/')}/oauth/gmail/start",
        headers={"Authorization": f"Bearer {args.jwt}"},
        params=params,
        allow_redirects=False,
        timeout=30,
    )

    location = resp.headers.get("Location")
    print(f"status={resp.status_code}")
    print(f"location={location or ''}")
    if resp.status_code not in {302, 307} or not location:
        return 1

    qs = parse_qs(urlparse(location).query)
    scope = qs.get("scope", [""])[0]
    prompt = qs.get("prompt", [""])[0]
    access_type = qs.get("access_type", [""])[0]
    print(f"scope={scope}")
    print(f"prompt={prompt}")
    print(f"access_type={access_type}")

    checks = [
        "https://www.googleapis.com/auth/gmail.readonly" in scope,
        "https://www.googleapis.com/auth/gmail.send" in scope,
        "https://www.googleapis.com/auth/gmail.compose" in scope,
        prompt == "select_account consent",
        access_type == "offline",
    ]
    ok = all(checks)
    print(f"ok={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
