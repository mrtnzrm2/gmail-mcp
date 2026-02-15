#!/usr/bin/env python3
import argparse
import json
import os
import sys
import re
from email.utils import parseaddr

import requests


def _call(base_url: str, jwt_token: str, args_payload: dict):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "gmail.triage", "arguments": args_payload},
    }
    headers = {"Authorization": f"Bearer {jwt_token}", "Content-Type": "application/json"}
    resp = requests.post(f"{base_url}/mcp", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--thread-id")
    p.add_argument("--msg-id")
    p.add_argument("--jwt")
    p.add_argument("--account-id")
    p.add_argument("--base-url")
    ns = p.parse_args()
    base_url = (ns.base_url or os.getenv("BASE_URL", "http://127.0.0.1:8000")).rstrip("/")
    jwt_token = ns.jwt or os.getenv("JWT_TOKEN")
    thread_id = ns.thread_id or os.getenv("THREAD_ID")
    msg_id = ns.msg_id or os.getenv("MSG_ID")
    if not jwt_token or (not thread_id and not msg_id):
        print("Provide --jwt and either --thread-id or --msg-id")
        return 2
    args_payload = {"force_refresh": False, "max_messages": 10}
    if ns.account_id or os.getenv("ACCOUNT_ID"):
        args_payload["account_id"] = ns.account_id or os.getenv("ACCOUNT_ID")
    if thread_id:
        args_payload["thread_id"] = thread_id
    if msg_id:
        args_payload["msg_id"] = msg_id

    first = _call(base_url, jwt_token, args_payload)
    second = _call(base_url, jwt_token, args_payload)
    args_payload["force_refresh"] = True
    third = _call(base_url, jwt_token, args_payload)
    triage = first.get("result") or {}
    suggested = triage.get("suggested_reply") or {}
    signals = triage.get("signals") or {}
    me_email = str(signals.get("me_email") or "").strip().lower()
    last_sender_signal = str(signals.get("last_sender") or "").strip().lower()
    last_external_email = str(signals.get("last_external_sender_email") or "").strip().lower()
    me_display = me_email.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ").title() if me_email else "User"
    body = str(suggested.get("body_text") or "")
    expected_sig = f"Best,\\n{me_display}"
    has_expected_sig = body.rstrip().endswith(expected_sig)
    no_email_sig = not re.search(rf"(?im)^\\s*{re.escape(me_email)}\\s*$", body) if me_email else True
    no_duplicate_closing = len(re.findall(r"(?im)^\\s*(best|best regards|regards|kind regards),?\\s*$", body)) <= 1
    last_sender_raw = str(signals.get("last_sender") or "")
    _, last_sender_email = parseaddr(last_sender_raw)
    last_sender_email = last_sender_email.strip().lower()
    to_matches_sender = (not triage.get("needs_reply")) or (
        bool(last_sender_email) and str(suggested.get("to") or "").strip().lower() == last_sender_email
    )
    me_email_present = bool(me_email)
    external_sender_not_me = True
    if last_external_email:
        external_sender_not_me = last_external_email != me_email and last_sender_signal != me_email
    excerpt = "\\n".join(body.splitlines()[-6:])
    print(
        json.dumps(
            {
                "first": triage,
                "second_cache": ((second.get("result") or {}).get("_cache") or {}).get("hit"),
                "third_cache": ((third.get("result") or {}).get("_cache") or {}).get("hit"),
                "third_reason": ((third.get("result") or {}).get("_cache") or {}).get("reason"),
                "signature_ok": has_expected_sig,
                "no_email_signature_line": no_email_sig,
                "no_duplicate_closing": no_duplicate_closing,
                "to_matches_last_sender": to_matches_sender,
                "me_email_present": me_email_present,
                "external_sender_not_me": external_sender_not_me,
                "excerpt_tail": excerpt,
            },
            indent=2,
        )
    )
    if not me_email_present or not external_sender_not_me:
        return 1
    if triage.get("needs_reply") and not (has_expected_sig and no_email_sig and no_duplicate_closing and to_matches_sender):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
