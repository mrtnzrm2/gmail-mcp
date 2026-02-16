# Privacy Policy
Last updated: February 2026

This Privacy Policy describes how Gmail MCP ("the Service") collects, uses, and protects information when you use the service.

## 1. Service Overview

Gmail MCP is a self-hosted Gmail API connector that allows authorized users to:
- Search emails
- Retrieve message metadata and content
- Create drafts
- Send emails
- Summarize email threads

The service operates only after explicit user authentication via Google OAuth.

---

## 2. Information We Access

When you connect your Gmail account, the Service may access:

- Email metadata (From, To, Subject, Date, Labels)
- Message snippets
- Message body content (plain text or HTML)
- Gmail thread data
- OAuth tokens issued by Google

The Service does NOT access data outside the Gmail scopes explicitly granted by the user.

---

## 3. How Information Is Used

Data is used solely to:

- Execute user-requested Gmail operations
- Retrieve email content requested by the user
- Generate summaries (optionally via OpenAI API)
- Maintain account connection state

The Service does NOT:
- Sell data
- Share data with third parties
- Use email content for advertising
- Use email data to train AI models

---

## 4. Data Storage

The Service stores:

- Encrypted Gmail OAuth tokens
- Minimal account metadata (email address, account_id, default flag)
- Optional thread summary cache

Email content is not permanently stored unless explicitly cached for thread summarization performance.

---

## 5. OpenAI Usage (Optional)

If thread summarization is enabled:
- Selected thread content may be processed by OpenAI APIs.
- Only the minimum necessary content is sent.
- Data is processed according to OpenAI’s API data policies.
- No data is used for model training.

If summarization is disabled, no data is sent to OpenAI.

---

## 6. Data Retention

OAuth tokens remain stored until:
- The user revokes access
- The account is disconnected
- The token expires

Users may revoke access at any time.

---

## 7. Security

The Service implements:

- OAuth 2.0 authentication
- Encrypted token storage
- Bearer token authorization (PAT/JWT)
- HTTPS transport security
- Scope validation

---

## 8. User Control

Users can:

- Disconnect Gmail accounts
- Revoke OAuth tokens
- Delete personal access tokens
- Stop using the service at any time

---

## 9. Contact

For questions regarding this policy:

Repository: https://github.com/mrtnzrm2/gmail-mcp
Operator: Jorge Martinez Armas

---

## 10. Changes

This policy may be updated. Continued use of the Service constitutes acceptance of changes.