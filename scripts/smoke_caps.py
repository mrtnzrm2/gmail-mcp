#!/usr/bin/env python3
import json
import os
import sys

import requests


def _mcp_call(base_url: str, jwt: str, name: str, arguments: dict):
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


def main() -> int:
    base_url = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    jwt = os.getenv("JWT") or os.getenv("JWT_TOKEN")
    if not jwt:
        print("Set JWT (or JWT_TOKEN)")
        return 2

    headers = {"Authorization": f"Bearer {jwt}"}
    accounts_resp = requests.get(f"{base_url}/gmail/accounts", headers=headers, timeout=30)
    accounts_resp.raise_for_status()
    accounts = accounts_resp.json()
    if not accounts:
        print("No connected accounts")
        return 2
    default = next((a for a in accounts if a.get("is_default")), accounts[0])
    caps = default.get("capabilities", {})
    account_id = default.get("account_id")
    print(json.dumps({"default_account": default.get("gmail_email"), "capabilities": caps}, indent=2))

    if not caps.get("can_draft", False):
        draft = _mcp_call(
            base_url,
            jwt,
            "gmail.draft_create",
            {
                "account_id": account_id,
                "to": default.get("gmail_email"),
                "subject": "caps smoke",
                "body_text": "caps smoke",
            },
        )
        err = draft.get("error", {})
        data = err.get("data", {}) if isinstance(err, dict) else {}
        ok = (
            data.get("http_status") == 403
            and data.get("error_type") == "insufficient_scope"
            and str(data.get("error_code", "")) == "missing_gmail_compose"
        )
        print(json.dumps({"draft_create_error": err, "draft_scope_gate_ok": ok}, indent=2))
        if not ok:
            return 1

    if caps.get("can_send", False):
        send = _mcp_call(
            base_url,
            jwt,
            "gmail.send",
            {
                "account_id": account_id,
                "to": default.get("gmail_email"),
                "subject": "caps smoke",
                "body_text": "caps smoke",
                "confirmed": False,
            },
        )
        err = send.get("error", {})
        gate_ok = "Use gmail.draft_create then gmail.draft_send with confirmed=true." in str(err.get("message", ""))
        print(json.dumps({"send_confirmation_gate": err, "send_gate_ok": gate_ok}, indent=2))
        if not gate_ok:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
