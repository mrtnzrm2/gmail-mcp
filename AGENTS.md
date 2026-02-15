# Gmail MCP Server (Multi-account) — Agent Guide

This repository is a minimal, security-conscious Gmail integration intended to be exposed as **tools** to an LLM agent via a **remote MCP server**.

Right now, the project is implemented as a **local FastAPI app** to validate OAuth + Gmail API behavior (multi-account tokens, search, fetch, send). After local validation, the same code is deployed to a cheap VPS with HTTPS and then wrapped as a proper **remote MCP server**.

## Goals

- Authorize **multiple Gmail accounts** via OAuth 2.0 and store credentials per-account.
- Provide tool-like operations:
  - list connected accounts
  - search messages
  - fetch message metadata
  - fetch message body (text/plain preferred)
  - send email (guarded by explicit confirmation)
- Keep cost minimal (≈ $5/month VPS when remote).
- Keep scopes minimal (**gmail.readonly** + **gmail.send** + **gmail.compose**) and avoid Google verification by using **Testing mode + Test Users**.
- Existing accounts connected before compose was added may need a scope upgrade via /oauth/gmail/start.

## Non-goals (MVP)

- Full Gmail UI parity (threads, attachments, drafts, labels management).
- Multi-user SaaS / public distribution.
- Background polling / webhooks.

---

## Current State (Local Development)

### Key files

- `app.py`: FastAPI app implementing OAuth and Gmail endpoints.
- `client_secret.json`: OAuth client JSON (DO NOT COMMIT).
- `tokens/`: per-account credential files (DO NOT COMMIT).

### Token storage

After OAuth succeeds, credentials are stored as:

- `tokens/<email>.json`

Each file must include a `refresh_token` for stable operation.

### Local OAuth transport

OAuth web flow requires HTTPS by default. For **localhost development only**, set:

```bash
export OAUTHLIB_INSECURE_TRANSPORT=1
```

Never use this in production.

---

## Local Setup

### 1) Install dependencies

Create a venv and install minimal requirements:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install fastapi uvicorn google-auth google-auth-oauthlib google-api-python-client python-multipart
```

### 2) Place OAuth client JSON

Put the downloaded Google OAuth client JSON at:

- `client_secret.json`

### 3) Run server

```bash
export OAUTHLIB_INSECURE_TRANSPORT=1
uvicorn app:app --host 127.0.0.1 --port 8000
```

### 4) Authorize each Gmail account

For each Gmail account you want to connect:

1. Open in an incognito window:
   - `http://localhost:8000/oauth/start`
2. Log into the desired Gmail account.
3. Approve consent.
4. Confirm a new token file exists in `tokens/`.

### 5) Verify refresh tokens

Open:

- `http://localhost:8000/accounts/check`

Ensure `has_refresh_token: true` for every account.

If missing:
- Revoke access at `https://myaccount.google.com/permissions`
- Delete `tokens/*.json`
- Repeat OAuth

---

## Local Test Endpoints

### List accounts

- `GET /accounts`

### Gmail OAuth start (scope upgrade / reconnect)

- `GET /oauth/gmail/start`
- Optional query param: `login_hint=<email>`
- Redirect parameters are always set to include:
  - `scope` including `gmail.readonly`, `gmail.send`, `gmail.compose`
  - `access_type=offline`
  - `prompt=select_account consent`

Example redirect check:

```bash
curl -s -D - -o /dev/null "http://localhost:8000/oauth/gmail/start?login_hint=you@example.com" | grep -i location
```

A successful `Location` URL should include encoded compose scope and encoded prompt, for example:
- `scope=...gmail.readonly...gmail.send...gmail.compose...`
- `prompt=select_account+consent`

### Dev debug endpoints (DEBUG only)

- `GET /health` returns server status.
- `GET /debug/last_error` returns the most recent captured exception for OAuth start diagnostics.
- `DEBUG` must be enabled in env for `/debug/last_error`; otherwise it returns 404.

OAuth start smoke command:

```bash
python3 scripts/smoke_oauth_gmail_start.py --base-url http://localhost:8000 --login-hint csg1c12@gmail.com --jwt "$JWT"
```

### Search

- `GET /gmail/search?account=<email>&q=<gmail_query>&max_results=5`

Example:

```
/gmail/search?account=you@gmail.com&q=newer_than:7d&max_results=5
```

### Get message metadata

- `GET /gmail/get?account=<email>&msg_id=<id>`

### Get message body

- `GET /gmail/get_body?account=<email>&msg_id=<id>`

Notes:
- Prefers `text/plain`.
- Falls back to `text/html`.

### Send email (guarded)

- `POST /gmail/send?account=<email>&to=<to>&subject=<subject>&body=<body>&confirmed=true`

If `confirmed=false`, the server returns a preview and does not send.

---

## Security Hygiene

### Treat these as secrets

- `client_secret.json`
- all `tokens/*.json` (contains refresh tokens)

If any secret is pasted or exposed, rotate:

1. Create a new OAuth client (or add a new client secret) in Google Auth Platform.
2. Revoke app access at `https://myaccount.google.com/permissions`.
3. Delete token files and re-authorize.

### Recommended local `.gitignore`

```
client_secret.json
tokens/
*.log
.env
```

---

## Roadmap: Convert to Remote MCP Server

Once local endpoints are stable:

1. **VPS** (~$5/month) + optional domain.
2. **HTTPS** via Nginx + Let’s Encrypt.
3. Update Google OAuth Redirect URI to:
   - `https://<domain>/oauth/callback`
4. Remove `OAUTHLIB_INSECURE_TRANSPORT`.
5. Add basic auth controls (at minimum):
   - restrict access to known IPs OR require API key.
6. Implement the MCP protocol layer:
   - expose tools: `accounts_list`, `gmail_search`, `gmail_get`, `gmail_get_body`, `gmail_send`

OpenAI references for MCP and remote servers:
- OpenAI MCP guide. citeturn0search2
- Tools/connectors and remote MCP servers. citeturn0search0
- MCP for Codex (CLI/IDE). citeturn0search6

---

## Troubleshooting

### `InvalidGrantError: invalid_grant`

- You refreshed/reused the callback URL (auth codes are single-use)
- Redirect URI mismatch (`localhost` vs `127.0.0.1`, port mismatch, trailing slash)

Fix:
- Restart server, use incognito window, re-run `/oauth/start`.

### Missing `refresh_token`

- Existing grant reused by Google

Fix:
- Revoke app access at `https://myaccount.google.com/permissions`
- Delete tokens
- Re-auth with `access_type=offline` and `prompt=consent`

### Body decode issues

Gmail returns bodies as base64url within message parts for `format=full`; raw message is returned for `format=raw`. See Gmail API reference for messages. citeturn0search3turn0search7

---

## Reconnect Flow Checklist

1. Connect account once and verify it appears as active in `GET /gmail/accounts`.
2. Revoke that account and verify it disappears and Gmail tools return 404.
3. Reconnect the same Gmail address and verify it becomes active again (`revoked_at` cleared) and tools work.
4. Reconnect when Google returns no `refresh_token`: this should succeed only if the account already existed.

## Invalid Grant Auto-Revoke Checklist

1. Simulate invalid grant:
   - Revoke app access in Google account permissions, or
   - Replace stored `refresh_token_enc` with junk in DB.
2. Call `GET /gmail/search` for that account.
3. Verify response is `404 {"detail":"Gmail account not found"}`.
4. Verify `gmail_accounts.revoked_at` is set for that row.
5. Verify `GET /gmail/accounts` no longer lists that account.

## Draft-Only Send Workflow

1. Create a draft:
   - `POST /gmail/draft_create?to=<email>&subject=<s>&body_text=<body>`
2. Send the draft:
   - `POST /gmail/draft_send?draft_id=<id>&confirmed=true`
3. Calling `POST /gmail/send` with `confirmed=false` must return guidance to use draft workflow.

## Thread Summarization Checklist

1. Call:
   - `GET /gmail/thread_summarize?thread_id=<thread_id>&max_messages=10`
2. Verify response fields:
   - `participants`, `timeline`, `action_items`, `open_questions`.
3. Verify no message body or token values are written to audit error codes.
4. MCP curl regression:
   - `curl -X POST http://127.0.0.1:8000/mcp -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"gmail.thread_summarize","arguments":{"thread_id":"<THREAD_ID>","max_messages":10}}}'`

## Hybrid Summarization (Optional) Checklist

1. Enable in `.env`:
   - `HYBRID_SUMMARIZE_ENABLED=true`
   - `OPENAI_API_KEY=<your_key>`
   - `HYBRID_MODEL=gpt-4.1-mini`
   - `HYBRID_MAX_CONTEXT_CHARS=2000`
2. Restart `uvicorn` after any `.env` change (`--reload` does not reliably reload env vars), then call via MCP:
   - `curl -X POST http://127.0.0.1:8000/mcp -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"gmail.thread_summarize","arguments":{"thread_id":"<THREAD_ID>","max_messages":10}}}'`
3. Verify fallback behavior:
   - If key is missing, hybrid is disabled, or LLM call fails, endpoint still returns deterministic summary shape.
4. If `_hybrid.error_code=openai_call_failed`, check server logs for:
   - `hybrid_openai_call_failed msg=...`
   - OpenAI API usage requires a valid `OPENAI_API_KEY` and is billed via OpenAI API (separate from ChatGPT Plus).
5. Cache smoke check:
   - `JWT_TOKEN=<jwt> THREAD_ID=<thread_id> python3 scripts/smoke_thread_cache.py`
   - Expected: `first_cache_hit` false (or null), `second_cache_hit` true.
6. Conversation intelligence smoke:
   - `JWT_TOKEN=<jwt> THREAD_ID=<thread_id> python3 scripts/smoke_intelligence.py`
   - Expected: `intelligence` contains required keys and second call is cache hit.
7. Triage tool smoke:
   - `python3 scripts/smoke_triage.py --thread-id <thread_id> --jwt <jwt>`
   - Expected: triage payload includes `needs_reply`, `urgency`, `category`, `signals`, `_hybrid`, `_cache`.
8. Reply draft wrapper smoke:
   - `python3 scripts/smoke_reply_draft.py --thread-id <thread_id> --jwt <jwt>`
   - Expected: returns `draft_id`, `thread_id`, `to`, `subject`, `preview`; does not send mail.
9. Reply draft safety smoke:
   - `python3 scripts/smoke_reply_draft_safety.py --thread-id <thread_id> --account-id <account_id> --jwt <jwt> --expect-force true`
   - `python3 scripts/smoke_reply_draft_safety.py --thread-id <thread_id> --account-id <account_id> --jwt <jwt> --expect-force false`
   - To hard-stop accidental external recipients in dev:
     - `export DEV_FORCE_TO_SELF=true`
     - optional allowlist: `export DEV_FORCE_TO_SELF_ALLOWLIST=trusted@example.com`
     - optional reason: `export DEV_FORCE_TO_SELF_REASON=self_only`
     - run server: `DEV_FORCE_TO_SELF=true uvicorn app.main:app --reload --port 8000`
   - Draft creation keeps chosen recipient metadata; force-to-self is applied at send time (`gmail.send` / `gmail.draft_send`).
   - Send responses include safety fields:
     - `original_to`, `final_to`, `override_applied`, `override_reason`, `reply_mode`.
     - `original_to` and `final_to` are always present on send responses.
   - Signature naming rules:
     - use `DEFAULT_SIGNATURE_NAME` if set
     - else use `users.full_name` from Google login identity
     - never sign with raw email or duplicate `Best,` lines
10. MCP triage curl:
   - `curl -X POST http://127.0.0.1:8000/mcp -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":21,"method":"tools/call","params":{"name":"gmail.triage","arguments":{"thread_id":"<THREAD_ID>","max_messages":10,"force_refresh":false}}}'`
11. MCP reply draft curl:
   - `curl -X POST http://127.0.0.1:8000/mcp -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":22,"method":"tools/call","params":{"name":"gmail.thread_reply_draft","arguments":{"thread_id":"<THREAD_ID>","use_suggested_reply":true,"confirmed":false}}}'`
12. Capability map rules:
   - `can_search/can_get`: readonly or modify
   - `can_send`: send only
   - `can_draft/can_thread_reply_draft`: compose or modify
   - `can_label_modify/can_mark_read`: modify only
13. Capability output example (`/gmail/accounts` or `gmail.accounts_list`):
   - `{"account_id":"...","gmail_email":"...","scopes":["https://www.googleapis.com/auth/gmail.readonly"],"capabilities":{"can_search":true,"can_get":true,"can_send":false,"can_draft":false,...}}`
14. Missing capability remediation:
   - reconnect account via `/oauth/gmail/start` and grant `gmail.compose` for draft features.
   - expected missing-compose 403 detail:
     - `{"error":"insufficient_scope","message":"This Gmail account is connected without gmail.compose, so draft creation is not allowed.","remediation":"Reconnect the account by visiting /oauth/gmail/start and granting gmail.compose.","required_scopes":["https://www.googleapis.com/auth/gmail.compose"]}`

## Rate Limit / Auth Audit Checklist

1. Hit `POST /mcp` above 60 requests per minute and verify HTTP 429.
2. Hit `/gmail/search` above 30 requests per minute and verify HTTP 429.
3. Trigger an authenticated 401/403 scenario and verify audit rows for:
   - `tool_name=http.request`, `error_type=auth_error`.
