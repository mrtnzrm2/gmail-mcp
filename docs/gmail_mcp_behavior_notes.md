
## Design Principles

1. Search returns IDs only (never metadata).
2. All human-facing output must hydrate IDs via `/gmail/get`.
3. Default mailbox resolution happens server-side if `account_id` omitted.
4. Send operations require explicit confirmation.
5. Thread-level reasoning must use `/gmail/thread_summarize`.

# Gmail MCP Behavior Notes

## Overview

This service provides:

- Google login + Gmail OAuth account connection.
- Multi-account Gmail support per authenticated user.
- Default mailbox resolution when `account_id` is omitted on Gmail operations.

Auth model:

- Most operational endpoints require `Authorization: Bearer <token>`.
- Token may be app JWT or PAT, validated by `get_current_user`.
- OpenAPI is normalized to bearer security scheme `BearerAuth`.

Reference:
- `app/main.py:72`
- `app/main.py:109`
- `app/main.py:112`
- `app/auth.py:97`
- `app/auth.py:119`
- `app/pat.py:23`

## Endpoint behavior notes (gotchas)

### `/gmail/accounts` and `/gmail/accounts/default`

- `GET /gmail/accounts` returns connected non-revoked accounts with:
  - `account_id`, `gmail_email`, `is_default`, `scopes`, `capabilities`.
- `GET /gmail/accounts/default` returns current default account.
- If no default exists, `/gmail/accounts/default` returns `404` with `"No default Gmail account set"`.

Reference:
- `app/gmail_tools.py:901`
- `app/gmail_tools.py:2078`
- `app/gmail_tools.py:2087`
- `app/gmail_tools.py:2131`
- `app/gmail_tools.py:2139`

### Default resolution when `account_id` is omitted

- Gmail operations call `resolve_account(...)`.
- If `account_id` is provided, that account is used.
- If omitted, server uses account where `is_default = true`.
- If omitted and no default exists, Gmail operations fail with `400` and `"No default Gmail account set"`.

Reference:
- `app/gmail_tools.py:797`
- `app/gmail_tools.py:801`
- `app/gmail_tools.py:809`

### Switching default account

Supported by:

- `POST /gmail/accounts/{account_id}/default`
- `POST /gmail/accounts/default/by_email` with JSON body `{ "gmail_email": "..." }`

Behavior:

- Server clears `is_default` on all user accounts, then marks selected account default.
- By-email endpoint returns a message plus selected account info.

Reference:
- `app/gmail_tools.py:2019`
- `app/gmail_tools.py:2023`
- `app/gmail_tools.py:2050`
- `app/gmail_tools.py:2109`
- `app/gmail_tools.py:2120`

## Search behavior (`/gmail/search`)

- Returns only IDs:
  - `[{ "id": "...", "threadId": "..." }]`
- Query is passed through directly as Gmail `q`.
- Server does not sort results itself.
- `max_results` defaults to `10` and is clamped to `1..50`.
- No pagination support in this API surface (`nextPageToken` is not returned/accepted).

Reference:
- `app/gmail_tools.py:920`
- `app/gmail_tools.py:937`
- `app/gmail_tools.py:942`
- `app/gmail_tools.py:950`
- `app/gmail_tools.py:2144`

## Message retrieval

### `/gmail/get`

- Calls Gmail `messages.get(format="metadata")`.
- Requests headers: `From`, `To`, `Subject`, `Date`.
- Returns:
  - `id`, `threadId`, `snippet`, `labels`, `from`, `to`, `subject`, `date`.
- Does not return MIME parts, decoded body text, or attachment payloads.

Reference:
- `app/gmail_tools.py:955`
- `app/gmail_tools.py:977`
- `app/gmail_tools.py:978`
- `app/gmail_tools.py:989`

### `/gmail/get_body`

- Calls Gmail `messages.get(format="full")`.
- Recursively walks MIME parts and returns first `text/plain` and first `text/html` decoded.
- If no `text/plain`, falls back to top-level payload body data decode.
- Returns only:
  - `text_plain`, `text_html`
- Attachments are not separately downloaded by this endpoint.
- HTML is returned raw (not sanitized) for this endpoint.

Reference:
- `app/gmail_tools.py:397`
- `app/gmail_tools.py:401`
- `app/gmail_tools.py:410`
- `app/gmail_tools.py:1019`
- `app/gmail_tools.py:1027`

## Thread summarize

- Route: `GET /gmail/thread_summarize`.
- Required: `thread_id`.
- Optional:
  - `max_messages` default `10`, clamped `1..50`
  - `account_id` optional
  - `force_refresh` default `false`
- Server fetches thread internally using Gmail `threads.get(format="full")`.
- Summary pipeline:
  - deterministic extraction (participants/timeline/actions/questions/decisions/risk/context), then
  - optional LLM refinement if hybrid summarize is enabled and OpenAI key is configured.

Reference:
- `app/gmail_tools.py:1278`
- `app/gmail_tools.py:1305`
- `app/gmail_tools.py:1307`
- `app/gmail_tools.py:1472`
- `app/gmail_tools.py:1499`
- `app/gmail_tools.py:1506`

## Draft/send model and confirmations

### Confirmation requirement

- `POST /gmail/send` requires `confirmed=true` or returns `400` with remediation details.
- `POST /gmail/draft_send` requires `confirmed=true` (required query parameter) or returns `400` if false.

Reference:
- `app/gmail_tools.py:1034`
- `app/gmail_tools.py:1065`
- `app/gmail_tools.py:1168`
- `app/gmail_tools.py:1193`
- `app/gmail_tools.py:2210`

### Threading/reply support in direct HTTP routes

- `POST /gmail/send` inputs: `to`, `subject`, `body_text` (+ optional `account_id`, `confirmed`).
- `POST /gmail/draft_create` inputs: `to`, `subject`, `body_text` (+ optional `account_id`).
- These direct HTTP routes do not accept explicit `thread_id`, `In-Reply-To`, or `References` parameters.

Reference:
- `app/gmail_tools.py:2181`
- `app/gmail_tools.py:2196`

### Threaded reply endpoint availability

- Threaded draft reply exists as MCP tool `gmail.thread_reply_draft` via `POST /mcp` (`tools/call`).
- It is not exposed as a direct `/gmail/...` HTTP route.

Reference:
- `app/mcp.py:173`
- `app/mcp.py:204`
- `app/mcp.py:313`

## Error model cheat sheet

Common HTTP statuses on direct REST endpoints:

- `401`:
  - Missing/invalid bearer token.
  - Stored Gmail credential decryption failure (`"Stored credentials are invalid"`).
- `403`:
  - Insufficient Gmail scope/capability.
- `400`:
  - No default account set when `account_id` omitted.
  - Confirmation-required send attempts.
  - Invalid `msg_id` shape for selected mailbox on get/get_body.
- `404`:
  - Missing account/message/token (context-dependent).
  - No default account for `/gmail/accounts/default`.
- `503`:
  - Gmail refresh/transient upstream failures.
- `422`:
  - Validation errors (missing required query/body fields, bad types).

Response shape notes:

- Typical FastAPI HTTP error: `{ "detail": ... }`.
- Validation error: `{ "detail": [ ... ] }`.
- `/mcp` wraps tool errors in JSON-RPC envelope:
  - `{ "jsonrpc": "2.0", "id": ..., "error": { "code": -32000, "message": "...", "data": {...} } }`

Reference:
- `app/auth.py:105`
- `app/gmail_tools.py:809`
- `app/gmail_tools.py:855`
- `app/gmail_tools.py:870`
- `app/gmail_tools.py:872`
- `app/gmail_tools.py:298`
- `app/gmail_tools.py:309`
- `app/gmail_tools.py:316`
- `app/gmail_tools.py:321`
- `app/gmail_tools.py:2053`
- `app/gmail_tools.py:2087`
- `app/mcp.py:329`
- `app/mcp.py:341`

## Minimal operator check

Use this quick sanity sequence after deploy/auth setup:

```bash
BASE_URL=http://127.0.0.1:8000
TOKEN=<PAT_OR_JWT>

curl -sS "$BASE_URL/health"

curl -sS -H "Authorization: Bearer $TOKEN" "$BASE_URL/gmail/accounts"

curl -sS -H "Authorization: Bearer $TOKEN" "$BASE_URL/gmail/accounts/default"
# If 404, set one default (choose one method):
# curl -sS -X POST -H "Authorization: Bearer $TOKEN" "$BASE_URL/gmail/accounts/<account_id>/default"
# curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
#   -d '{"gmail_email":"me@example.com"}' "$BASE_URL/gmail/accounts/default/by_email"

curl -sS -G -H "Authorization: Bearer $TOKEN" "$BASE_URL/gmail/search" \
  --data-urlencode 'q=in:inbox newer_than:30d' \
  --data-urlencode 'max_results=3'

# Hydrate one msg_id from search
curl -sS -G -H "Authorization: Bearer $TOKEN" "$BASE_URL/gmail/get" \
  --data-urlencode 'msg_id=<msg_id_from_search>'
```
