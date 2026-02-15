#!/usr/bin/env python3
import argparse
import json
import os
import sys

import requests


def mcp_call(base_url: str, jwt: str, name: str, arguments: dict):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    headers = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
    resp = requests.post(f"{base_url}/mcp", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fail(msg: str, payload=None) -> int:
    print(msg)
    if payload is not None:
        print(json.dumps(payload, indent=2))
    return 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--thread-id", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--jwt", default=os.getenv("JWT") or os.getenv("JWT_TOKEN"))
    p.add_argument("--base-url", default=os.getenv("BASE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--expect-force", choices=["true", "false"], required=True)
    p.add_argument("--expected-signer", default=os.getenv("EXPECTED_SIGNER", "Jorge Martinez Armas"))
    ns = p.parse_args()

    if not ns.jwt:
        return fail("Provide --jwt or JWT/JWT_TOKEN")

    base_url = ns.base_url.rstrip("/")
    expect_force = ns.expect_force == "true"
    expected_signer = (ns.expected_signer or "").strip()

    health_resp = requests.get(f"{base_url}/health", timeout=15)
    health_resp.raise_for_status()
    health = health_resp.json() if isinstance(health_resp.json(), dict) else {}
    server_force = bool(health.get("dev_force_to_self", False))
    if expect_force and not server_force:
        print("Server is not in force mode but --expect-force=true was requested.")
        print(json.dumps({"dev_force_to_self": server_force, "health": health}, indent=2))
        return 2
    if (not expect_force) and server_force:
        print("Warning: server dev_force_to_self=true but --expect-force=false was requested.")

    accounts_resp = requests.get(
        f"{base_url}/gmail/accounts",
        headers={"Authorization": f"Bearer {ns.jwt}"},
        timeout=30,
    )
    accounts_resp.raise_for_status()
    accounts = accounts_resp.json()
    acct = next((a for a in accounts if a.get("account_id") == ns.account_id), None)
    if not acct:
        return fail("account_id not found in /gmail/accounts")
    me_email = str(acct.get("gmail_email") or "").strip().lower()

    # 1) Create reply draft (should keep external recipient in preview metadata)
    create = mcp_call(
        base_url,
        ns.jwt,
        "gmail.thread_reply_draft",
        {
            "thread_id": ns.thread_id,
            "account_id": ns.account_id,
            "use_suggested_reply": True,
            "confirmed": True,
        },
    )
    create_result = create.get("result")
    if not isinstance(create_result, dict) or not create_result.get("draft_id"):
        return fail("Expected draft_id from gmail.thread_reply_draft confirmed=true", create)

    created_to = str(create_result.get("final_to") or create_result.get("to") or "").strip().lower()
    if not created_to:
        return fail("Expected recipient on created draft", create_result)
    preview = str(create_result.get("preview") or "")
    if expected_signer and expected_signer not in preview:
        return fail("Expected signer missing from preview", {"expected_signer": expected_signer, "preview": preview})
    if "Jmrtnza" in preview:
        return fail("Preview contains legacy local-part signer", {"preview": preview})
    for line in preview.splitlines():
        if "@" in line and line.strip().lower().endswith("@gmail.com"):
            return fail("Preview signature must not contain raw email line", {"preview": preview})

    # 2) Send that draft and verify force behavior metadata
    send = mcp_call(
        base_url,
        ns.jwt,
        "gmail.draft_send",
        {
            "account_id": ns.account_id,
            "draft_id": create_result["draft_id"],
            "confirmed": True,
        },
    )
    send_result = send.get("result")
    if not isinstance(send_result, dict) or not send_result.get("id"):
        return fail("Expected send result id", send)

    applied = bool(send_result.get("override_applied"))
    final_to = str(send_result.get("final_to") or "").strip().lower()
    original_to = str(send_result.get("original_to") or "").strip().lower()
    create_mode = str(create_result.get("reply_mode") or "")
    send_mode = str(send_result.get("reply_mode") or "")

    if expect_force and not applied:
        return fail("Expected override_applied=true with DEV_FORCE_TO_SELF=true", send_result)
    if not expect_force and applied:
        return fail("Expected override_applied=false with DEV_FORCE_TO_SELF=false", send_result)
    if not original_to or not final_to:
        return fail("Expected original_to and final_to always populated", send_result)
    if expect_force:
        if final_to != me_email:
            return fail("Expected final_to to be me_email in forced mode", {"me_email": me_email, "send_result": send_result})
        if original_to == final_to:
            return fail("Expected original_to preserved and different when forced", send_result)
    else:
        if original_to != final_to:
            return fail("Expected original_to == final_to in non-forced mode", send_result)
        if create_mode != send_mode:
            return fail("Expected stable reply_mode between create and send in non-forced mode", {"create_mode": create_mode, "send_mode": send_mode})

    print(
        json.dumps(
            {
                "create_result": {
                    "draft_id": create_result.get("draft_id"),
                    "to": create_result.get("to"),
                    "final_to": create_result.get("final_to"),
                    "reply_mode": create_result.get("reply_mode"),
                },
                "send_result": {
                    "id": send_result.get("id"),
                    "override_applied": send_result.get("override_applied"),
                    "override_reason": send_result.get("override_reason"),
                    "original_to": send_result.get("original_to"),
                    "final_to": send_result.get("final_to"),
                    "reply_mode": send_result.get("reply_mode"),
                },
                "expect_force": expect_force,
                "server_dev_force_to_self": server_force,
                "final_to": final_to,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
