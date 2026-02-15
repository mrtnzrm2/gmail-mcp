from html import escape

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import get_current_user
from .db import get_db
from .models import GmailAccount, User

router = APIRouter(tags=["ui"])


@router.get("/app", response_class=HTMLResponse)
def app_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    accounts = db.scalars(
        select(GmailAccount).where(
            GmailAccount.user_id == current_user.id,
            GmailAccount.revoked_at.is_(None),
        )
    ).all()

    items = "".join(
        f"<li><code>{escape(str(account.id))}</code> - {escape(account.gmail_email)}</li>"
        for account in accounts
    )
    if not items:
        items = "<li>No connected Gmail accounts yet.</li>"

    html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Gmail MCP Dashboard</title>
      </head>
      <body>
        <h1>Gmail MCP Dashboard</h1>
        <p>Signed in as: <strong>{escape(current_user.email)}</strong></p>
        <p><a href="/oauth/gmail/start">Connect Gmail Account</a></p>
        <h2>Your Gmail Accounts</h2>
        <ul>{items}</ul>
      </body>
    </html>
    """
    return HTMLResponse(content=html)
