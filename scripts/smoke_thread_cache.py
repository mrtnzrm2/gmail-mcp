#!/usr/bin/env python3
import json
import os
import sys

import requests


def main() -> int:
    base_url = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    jwt_token = os.getenv("JWT_TOKEN")
    thread_id = os.getenv("THREAD_ID")
    account_id = os.getenv("ACCOUNT_ID")
    if not jwt_token or not thread_id:
        print("Set JWT_TOKEN and THREAD_ID environment variables.")
        return 2

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "gmail.thread_summarize",
            "arguments": {"thread_id": thread_id, "max_messages": 10},
        },
    }
    if account_id:
        payload["params"]["arguments"]["account_id"] = account_id

    headers = {"Authorization": f"Bearer {jwt_token}", "Content-Type": "application/json"}
    first = requests.post(f"{base_url}/mcp", headers=headers, json=payload, timeout=30)
    second = requests.post(f"{base_url}/mcp", headers=headers, json=payload, timeout=30)
    force_payload = json.loads(json.dumps(payload))
    force_payload["id"] = 2
    force_payload["params"]["arguments"]["force_refresh"] = True
    third = requests.post(f"{base_url}/mcp", headers=headers, json=force_payload, timeout=30)
    first.raise_for_status()
    second.raise_for_status()
    third.raise_for_status()
    first_json = first.json()
    second_json = second.json()
    third_json = third.json()

    first_hit = (((first_json.get("result") or {}).get("_cache") or {}).get("hit"))
    second_hit = (((second_json.get("result") or {}).get("_cache") or {}).get("hit"))
    third_hit = (((third_json.get("result") or {}).get("_cache") or {}).get("hit"))
    third_reason = (((third_json.get("result") or {}).get("_cache") or {}).get("reason"))

    print(
        json.dumps(
            {
                "first_cache_hit": first_hit,
                "second_cache_hit": second_hit,
                "third_cache_hit_force_refresh": third_hit,
                "third_reason": third_reason,
            },
            indent=2,
        )
    )
    if second_hit is not True:
        return 1
    if third_hit is not False or third_reason != "force_refresh":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
