from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .auth import get_current_user
from .db import get_db
from .gmail_tools import (
    gmail_account_revoke_tool,
    gmail_draft_create_tool,
    gmail_draft_send_tool,
    gmail_set_default_tool,
    gmail_accounts_list_tool,
    gmail_get_body_tool,
    gmail_get_tool,
    gmail_search_tool,
    gmail_send_tool,
    gmail_thread_reply_draft_tool,
    gmail_thread_summarize_tool,
    gmail_triage_tool,
)
from .models import User
from .rate_limit import limiter

router = APIRouter(tags=["mcp"])


TOOLS = [
    {
        "name": "gmail.accounts_list",
        "description": "List Gmail accounts connected for the authenticated user.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "gmail.search",
        "description": "Search Gmail messages for a connected account. If account_id omitted, uses your default connected Gmail account.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gmail.get",
        "description": "Get message metadata and snippet by message ID. If account_id omitted, uses your default connected Gmail account.",
        "input_schema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}, "msg_id": {"type": "string"}},
            "required": ["msg_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gmail.get_body",
        "description": "Get decoded text/plain and text/html body by message ID. If account_id omitted, uses your default connected Gmail account.",
        "input_schema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}, "msg_id": {"type": "string"}},
            "required": ["msg_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gmail.send",
        "description": "Send via draft workflow. If account_id omitted, uses your default connected Gmail account. Requires confirmed=true.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body_text": {"type": "string"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["to", "subject", "body_text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gmail.account_revoke",
        "description": "Revoke a connected Gmail account for the authenticated user.",
        "input_schema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
            "required": ["account_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gmail.account_set_default",
        "description": "Set the default Gmail account for the authenticated user.",
        "input_schema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
            "required": ["account_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gmail.draft_create",
        "description": "Create a Gmail draft for the authenticated user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body_text": {"type": "string"},
            },
            "required": ["to", "subject", "body_text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gmail.draft_send",
        "description": "Send an existing Gmail draft. Requires confirmed=true.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "draft_id": {"type": "string"},
                "confirmed": {"type": "boolean"},
            },
            "required": ["draft_id", "confirmed"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gmail.thread_summarize",
        "description": "Summarize a thread into participants, timeline, action items, open questions, and conversation intelligence.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "max_messages": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                "force_refresh": {"type": "boolean", "default": False},
            },
            "required": ["thread_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gmail.triage",
        "description": "Triage a thread/message and return reply urgency, category, next action, and optional reply suggestion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "msg_id": {"type": "string"},
                "max_messages": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                "force_refresh": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "gmail.thread_reply_draft",
        "description": "Preview or create a Gmail draft reply in-thread. If use_suggested_reply=true, manual to/subject/body_text are ignored. Draft creation requires confirmed=true.",
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body_text": {"type": "string"},
                "use_suggested_reply": {"type": "boolean", "default": True},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["thread_id"],
            "additionalProperties": False,
        },
    },
]


def _jsonrpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id, code: int, message: str, data=None):
    payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if data is not None:
        payload["error"]["data"] = data
    return payload


@router.post("/mcp")
@limiter.limit("120/minute")
def mcp_endpoint(
    request: Request,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    request_id = body.get("id")
    if body.get("jsonrpc") != "2.0":
        return _jsonrpc_error(request_id, -32600, "Invalid Request")

    method = body.get("method")
    params = body.get("params") or {}

    if method == "tools/list":
        return _jsonrpc_result(request_id, {"tools": TOOLS})

    if method != "tools/call":
        return _jsonrpc_error(request_id, -32601, "Method not found")

    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not name:
        return _jsonrpc_error(request_id, -32602, "Missing tool name")

    try:
        if name == "gmail.accounts_list":
            result = gmail_accounts_list_tool(db, current_user)
        elif name == "gmail.search":
            result = gmail_search_tool(
                db=db,
                user=current_user,
                account_id=arguments.get("account_id"),
                query=arguments["query"],
                max_results=int(arguments.get("max_results", 10)),
            )
        elif name == "gmail.get":
            result = gmail_get_tool(
                db=db,
                user=current_user,
                account_id=arguments.get("account_id"),
                msg_id=arguments["msg_id"],
            )
        elif name == "gmail.get_body":
            result = gmail_get_body_tool(
                db=db,
                user=current_user,
                account_id=arguments.get("account_id"),
                msg_id=arguments["msg_id"],
            )
        elif name == "gmail.send":
            result = gmail_send_tool(
                db=db,
                user=current_user,
                account_id=arguments.get("account_id"),
                to=arguments["to"],
                subject=arguments["subject"],
                body_text=arguments["body_text"],
                confirmed=bool(arguments.get("confirmed", False)),
            )
        elif name == "gmail.account_revoke":
            result = gmail_account_revoke_tool(
                db=db,
                user=current_user,
                account_id=arguments["account_id"],
            )
        elif name == "gmail.account_set_default":
            result = gmail_set_default_tool(
                db=db,
                user=current_user,
                account_id=arguments["account_id"],
            )
        elif name == "gmail.draft_create":
            result = gmail_draft_create_tool(
                db=db,
                user=current_user,
                account_id=arguments.get("account_id"),
                to=arguments["to"],
                subject=arguments["subject"],
                body_text=arguments["body_text"],
            )
        elif name == "gmail.draft_send":
            result = gmail_draft_send_tool(
                db=db,
                user=current_user,
                account_id=arguments.get("account_id"),
                draft_id=arguments["draft_id"],
                confirmed=bool(arguments["confirmed"]),
            )
        elif name == "gmail.thread_summarize":
            result = gmail_thread_summarize_tool(
                db=db,
                user=current_user,
                account_id=arguments.get("account_id"),
                thread_id=arguments["thread_id"],
                max_messages=int(arguments.get("max_messages", 10)),
                force_refresh=bool(arguments.get("force_refresh", False)),
            )
        elif name == "gmail.triage":
            result = gmail_triage_tool(
                db=db,
                user=current_user,
                account_id=arguments.get("account_id"),
                thread_id=arguments.get("thread_id"),
                msg_id=arguments.get("msg_id"),
                max_messages=int(arguments.get("max_messages", 10)),
                force_refresh=bool(arguments.get("force_refresh", False)),
            )
        elif name == "gmail.thread_reply_draft":
            result = gmail_thread_reply_draft_tool(
                db=db,
                user=current_user,
                account_id=arguments.get("account_id"),
                thread_id=arguments["thread_id"],
                to=arguments.get("to"),
                subject=arguments.get("subject"),
                body_text=arguments.get("body_text"),
                use_suggested_reply=bool(arguments.get("use_suggested_reply", True)),
                confirmed=bool(arguments.get("confirmed", False)),
            )
        else:
            return _jsonrpc_error(request_id, -32601, f"Unknown tool: {name}")
    except KeyError as exc:
        return _jsonrpc_error(request_id, -32602, f"Missing argument: {exc.args[0]}")
    except HTTPException as exc:
        data = {"http_status": exc.status_code}
        message = str(exc.detail)
        if hasattr(exc, "error_type"):
            data["error_type"] = getattr(exc, "error_type")
            message = str(getattr(exc, "error_type"))
        if hasattr(exc, "error_code"):
            data["error_code"] = getattr(exc, "error_code")
        if isinstance(exc.detail, dict):
            data["detail"] = exc.detail
            if "message" in exc.detail and isinstance(exc.detail["message"], str):
                message = exc.detail["message"] if not hasattr(exc, "error_type") else str(getattr(exc, "error_type"))
        return _jsonrpc_error(request_id, -32000, message, data)
    except Exception:
        return _jsonrpc_error(request_id, -32000, "Tool execution failed")

    return _jsonrpc_result(request_id, result)
