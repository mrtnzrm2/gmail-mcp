# Gmail MCP Server

## Manual Test Checklist

1. Start server:
   - `uvicorn app.main:app --reload --port 8000`
   - If you change `.env`, restart `uvicorn` (reload does not reliably reload environment variables).
2. Enable hybrid summary in `.env` and restart:
   - `HYBRID_SUMMARIZE_ENABLED=true`
2. Open `http://localhost:8000/oauth/login/start` and complete login.
3. Open `http://localhost:8000/app`:
   - Verify it loads only when authenticated.
   - Verify it shows your signed-in email.
   - Verify it lists only your non-revoked Gmail accounts.
4. Click `Connect Gmail Account` link on `/app` and complete consent.
5. Return to `http://localhost:8000/app` and verify the newly connected account appears.
6. Revoke an account:
   - `POST /gmail/accounts/{account_id}/revoke`
   - Verify revoked account disappears from `GET /gmail/accounts` and `gmail.accounts_list` MCP tool.
7. Verify revoked account access is blocked consistently:
   - `GET /gmail/search?account_id={account_id}&q=newer_than:7d`
   - `GET /gmail/get?account_id={account_id}&msg_id=...`
   - `GET /gmail/get_body?account_id={account_id}&msg_id=...`
   - `POST /gmail/send?...&account_id={account_id}`
   - Expected everywhere: `404 {"detail":"Gmail account not found"}`
8. Multi-user isolation test:
   - Login as user1, capture `account_id`.
   - Login as user2 and attempt revoke/search/get/get_body/send using user1 `account_id`.
   - Expected everywhere: `404 {"detail":"Gmail account not found"}`.
9. Default account behavior:
   - Set default with `POST /gmail/accounts/{account_id}/default`.
   - Verify `GET /gmail/accounts` shows exactly one `is_default=true`.
   - Verify `/gmail/search?q=in:inbox` works without `account_id`.
   - Revoke default account and verify calls without `account_id` return:
     `400 {"detail":"No default Gmail account set"}`.
10. Invalid `msg_id` against current default mailbox:
   - Call `GET /gmail/get?msg_id=<id_from_other_mailbox_or_invalid>`.
   - Verify friendly error text for mismatched IDs:
     - `400`: `Message id is invalid for the selected mailbox...`
     - or `404`: `Message id was not found for the selected mailbox...`
11. Audit default-resolution metadata:
   - Call a tool without `account_id` (for example `GET /gmail/get?msg_id=...`).
   - Verify latest row in `audit_events` has `resolved_via_default=true`.
