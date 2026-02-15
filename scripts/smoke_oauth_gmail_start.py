#!/usr/bin/env python3
import argparse

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke /oauth/gmail/start redirect")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--login-hint", default="")
    parser.add_argument("--jwt", default="")
    args = parser.parse_args()

    url = f"{args.base_url.rstrip('/')}/oauth/gmail/start"
    params = {}
    if args.login_hint:
        params["login_hint"] = args.login_hint

    headers = {}
    if args.jwt:
        headers["Authorization"] = f"Bearer {args.jwt}"

    resp = requests.get(url, params=params, headers=headers, allow_redirects=False, timeout=30)
    print(f"status_code={resp.status_code}")
    print(f"location={resp.headers.get('Location', '')}")

    if resp.status_code == 500:
        dbg = requests.get(f"{args.base_url.rstrip('/')}/debug/last_error", timeout=10)
        print(f"debug_last_error_status={dbg.status_code}")
        print(f"debug_last_error={dbg.text}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
