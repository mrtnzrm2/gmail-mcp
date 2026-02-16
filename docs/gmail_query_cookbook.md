# Gmail Query Cookbook (`/gmail/search`)

## Quick start

- `q` is passed directly to Gmail API `users.messages.list(q=...)` with no server-side query rewriting.
- `/gmail/search` returns only message IDs: `[{"id": "...", "threadId": "..."}]`.
- To read message metadata/body, hydrate each result with `/gmail/get` and optionally `/gmail/get_body`.
- `max_results` is clamped server-side to `1..50`.
- Default `max_results` is `10`.
- No pagination token is exposed by this API route.

Reference:
- `app/gmail_tools.py:920`
- `app/gmail_tools.py:937`
- `app/gmail_tools.py:942`
- `app/gmail_tools.py:950`
- `app/gmail_tools.py:2144`

## Common recipes

### 1) Last N inbox messages

- Intent: Recent inbox activity.
- Query:

```text
in:inbox newer_than:30d
```

- Notes:
  - Use `max_results` to limit returned IDs.
  - Server does not add sorting; order is Gmail API response order.

### 2) Unread in recent window

- Intent: Work through fresh unread mail.
- Query:

```text
in:inbox is:unread newer_than:30d
```

- Notes:
  - If too broad, add sender or subject constraints.

### 3) From a sender

- Intent: Messages from a specific contact.
- Query:

```text
in:inbox from:email@example.com newer_than:365d
```

- Notes:
  - Replace `email@example.com` with exact sender email.

### 4) Subject exact phrase

- Intent: Match exact title phrase.
- Query:

```text
in:inbox subject:"Exact title here" newer_than:365d
```

- Notes:
  - Quotes keep phrase matching tight.

### 5) Subject keyword

- Intent: Match subject by keyword.
- Query:

```text
in:inbox subject:(keyword) newer_than:365d
```

- Notes:
  - Good for ticket codes or project tags.

### 6) Multiple keywords

- Intent: Find messages that mention several terms.
- Query:

```text
in:inbox (foo bar) newer_than:365d
```

- Notes:
  - Parentheses keep grouped terms readable.

### 7) Time windows

- Intent: Control search period.
- Query options:

```text
newer_than:7d
older_than:30d
after:2025/01/01 before:2025/02/01
```

- Notes:
  - `after:`/`before:` are useful for fixed investigation windows.

### 8) Attachments / size

- Intent: Find files and large attachments.
- Query:

```text
has:attachment filename:pdf larger:5M
```

- Notes:
  - Combine with inbox/date filters to keep results small.

### 9) Labels and categories

- Intent: Filter by Gmail system labels/categories.
- Query examples:

```text
label:IMPORTANT
category:updates
category:promotions
```

- Notes:
  - Combine with `newer_than:` and `in:inbox` for practical slices.

### 10) Exclusions

- Intent: Remove known noise.
- Query:

```text
in:inbox newer_than:30d -from:noreply@example.com -subject:(digest) -category:promotions
```

- Notes:
  - Useful for operational inboxes.

### 11) Exact recipient filters

- Intent: Find mail sent to/cc specific addresses.
- Query:

```text
to:me@example.com
cc:team@example.com
```

- Notes:
  - Can be combined with subject/date constraints.

## Troubleshooting

### “Too many results”

- Narrow with `newer_than:` or `after:/before:`.
- Add `from:` and/or `subject:` constraints.
- Keep `max_results` small (for example `3` or `10`) and iterate.

### “Didn’t find it”

- Remove aggressive time constraints (drop `newer_than:` first).
- Try a broader query and then narrow down.
- Verify default mailbox is correct (`/gmail/accounts/default`) or pass explicit `account_id`.

### “Need newest first”

- Do not assume `/gmail/search` is newest-first.
- Hydrate each ID via `/gmail/get` and sort client-side by the returned `date` header.

Reference:
- `app/gmail_tools.py:942`
- `app/gmail_tools.py:950`
- `app/gmail_tools.py:989`

## Copy/paste API examples

Assume `BASE_URL=http://127.0.0.1:8000` and `TOKEN=<PAT_OR_JWT>`.

### Search

```bash
curl -sS -G "$BASE_URL/gmail/search" \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'q=in:inbox newer_than:30d subject:(invoice)' \
  --data-urlencode 'max_results=3'
```

Expected shape:

```json
[
  {"id": "18f...", "threadId": "18f..."},
  {"id": "18e...", "threadId": "18e..."}
]
```

### Hydrate a message (`/gmail/get`)

```bash
curl -sS -G "$BASE_URL/gmail/get" \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'msg_id=18f...'
```

Expected shape:

```json
{
  "id": "18f...",
  "threadId": "18f...",
  "snippet": "Short snippet...",
  "labels": ["INBOX", "UNREAD"],
  "from": "Sender <sender@example.com>",
  "to": "Me <me@example.com>",
  "subject": "Subject line",
  "date": "Tue, 20 Jan 2026 09:14:22 -0800"
}
```

### Hydrate message body (`/gmail/get_body`)

```bash
curl -sS -G "$BASE_URL/gmail/get_body" \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'msg_id=18f...'
```

Expected shape:

```json
{
  "text_plain": "Plain body text...",
  "text_html": "<div>HTML body...</div>"
}
```

## Thread Resolution Pattern (Important)

When resolving a thread by subject:

1. Search:
   in:inbox subject:"Exact Title" newer_than:365d

2. Hydrate each id with `/gmail/get`.

3. Group by `threadId`.

4. Choose the threadId whose:
   - subject best matches
   - AND latest Date header is most recent.

Never assume one result equals one thread.
