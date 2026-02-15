#!/usr/bin/env python3
import argparse
import json
import os
import sys
import re

import requests


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--thread-id", required=True)
    p.add_argument("--jwt")
    p.add_argument("--account-id")
    p.add_argument("--base-url")
    p.add_argument("--body-text")
    ns = p.parse_args()
    base_url = (ns.base_url or os.getenv("BASE_URL", "http://127.0.0.1:8000")).rstrip("/")
    jwt_token = ns.jwt or os.getenv("JWT_TOKEN")
    account_id = ns.account_id or os.getenv("ACCOUNT_ID")
    if not jwt_token:
        print("Provide --jwt or JWT_TOKEN")
        return 2
    if not account_id:
        print("Provide --account-id or ACCOUNT_ID")
        return 2
    triage_payload = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {
            "name": "gmail.triage",
            "arguments": {
                "thread_id": ns.thread_id,
                "max_messages": 10,
                "force_refresh": False,
            },
        },
    }
    triage_payload["params"]["arguments"]["account_id"] = account_id
    headers = {"Authorization": f"Bearer {jwt_token}", "Content-Type": "application/json"}
    triage_resp = requests.post(f"{base_url}/mcp", headers=headers, json=triage_payload, timeout=30)
    triage_resp.raise_for_status()
    triage_data = triage_resp.json().get("result") or {}
    suggested = triage_data.get("suggested_reply") or {}
    me_email = str((triage_data.get("signals") or {}).get("me_email") or "").strip().lower()
    me_display = me_email.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ").title() if me_email else "User"

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "gmail.thread_reply_draft",
            "arguments": {
                "thread_id": ns.thread_id,
                "use_suggested_reply": True,
                "confirmed": False,
            },
        },
    }
    payload["params"]["arguments"]["account_id"] = account_id
    if ns.body_text:
        payload["params"]["arguments"]["body_text"] = ns.body_text
    resp = requests.post(f"{base_url}/mcp", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        err = data["error"]
        if (
            isinstance(err, dict)
            and str(((err.get("data") or {}).get("error_code") or "")).lower() in {"google_not_found", "not_found"}
        ):
            print(json.dumps({"tool_call_arguments": payload["params"]["arguments"], "account_id": account_id, "error": err}, indent=2))
            return 1
        print(json.dumps(data["error"], indent=2))
        return 1
    result = data.get("result") or {}
    preview = str(result.get("preview") or "")
    no_email_sig = not re.search(rf"(?im)^\\s*{re.escape(me_email)}\\s*$", preview) if me_email else True
    no_duplicate_closing = len(re.findall(r"(?im)^\\s*(best|best regards|regards|kind regards),?\\s*$", preview)) <= 1
    greeting_present = preview.lstrip().lower().startswith(("hi", "hello", "dear"))
    checks = {
        "greeting_present": greeting_present,
        "no_email_signature_line": no_email_sig,
        "no_duplicate_closing": no_duplicate_closing,
        "to_matches_triage": (
            not triage_data.get("needs_reply")
            or str(result.get("to") or "").strip().lower() == str(suggested.get("to") or "").strip().lower()
        ),
        "preview_excerpt": preview,
    }
    print(json.dumps({"triage_suggested_reply": suggested, "draft_result": result, "checks": checks}, indent=2))
    if not all([checks["greeting_present"], checks["no_email_signature_line"], checks["no_duplicate_closing"], checks["to_matches_triage"]]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
