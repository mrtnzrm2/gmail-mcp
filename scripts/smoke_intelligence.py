#!/usr/bin/env python3
import argparse
import json
import os
import sys

import requests


REQUIRED_KEYS = {
    "version",
    "urgency",
    "sentiment",
    "request_type",
    "needs_reply",
    "suggested_reply",
    "entities",
    "deadlines",
    "confidence",
    "safety_flags",
}


def _call(base_url: str, jwt_token: str, thread_id: str, force_refresh: bool = False):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "gmail.thread_summarize",
            "arguments": {
                "thread_id": thread_id,
                "max_messages": 10,
                "force_refresh": force_refresh,
            },
        },
    }
    headers = {"Authorization": f"Bearer {jwt_token}", "Content-Type": "application/json"}
    resp = requests.post(f"{base_url}/mcp", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", dest="thread_id")
    parser.add_argument("--jwt", dest="jwt")
    parser.add_argument("--base-url", dest="base_url")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    base_url = (args.base_url or os.getenv("BASE_URL", "http://127.0.0.1:8000")).rstrip("/")
    jwt_token = args.jwt or os.getenv("JWT_TOKEN")
    thread_id = args.thread_id or os.getenv("THREAD_ID")
    if not jwt_token or not thread_id:
        print("Set JWT_TOKEN and THREAD_ID (or pass --jwt and --thread-id)")
        return 2

    first = _call(base_url, jwt_token, thread_id, force_refresh=False)
    second = _call(base_url, jwt_token, thread_id, force_refresh=False)
    third = _call(base_url, jwt_token, thread_id, force_refresh=True)

    intel = ((first.get("result") or {}).get("intelligence") or {})
    missing = sorted(REQUIRED_KEYS - set(intel.keys()))
    if missing:
        print(f"Missing intelligence keys: {missing}")
        return 1
    deadlines = intel.get("deadlines") or []
    due_signals = [
        "by ",
        "before ",
        "deadline",
        "due",
        "eod",
        "end of day",
        "tomorrow",
        "today",
        "this week",
        "next week",
        "asap",
        "urgent",
    ]
    if deadlines:
        for row in deadlines:
            text = str((row or {}).get("text") or "").lower()
            if not any(sig in text for sig in due_signals):
                print(f"Invalid deadline without due signal: {row}")
                return 1

    entities = intel.get("entities") or []
    names = " ".join(str((e or {}).get("name") or "") for e in entities).lower()
    if "jan" not in names or "jorge" not in names:
        print("Expected entities to include Jan and Jorge")
        return 1

    output = {
        "intelligence": intel,
        "cache_first": (((first.get("result") or {}).get("_cache") or {}).get("hit")),
        "cache_second": (((second.get("result") or {}).get("_cache") or {}).get("hit")),
        "cache_third_force": (((third.get("result") or {}).get("_cache") or {}).get("hit")),
        "cache_third_reason": (((third.get("result") or {}).get("_cache") or {}).get("reason")),
    }
    print(json.dumps(output, indent=2))

    if output["cache_second"] is not True:
        return 1
    if output["cache_third_force"] is not False:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
