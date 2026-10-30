"""Model Context Protocol server (stdio, JSON-RPC 2.0) exposing the desk as tools.

Register it with your local agents, for example:

    claude mcp add agentdesk -- python -m agentdesk mcp --actor claude
    # ~/.codex/config.toml
    [mcp_servers.agentdesk]
    command = "python"
    args = ["-m", "agentdesk", "mcp", "--actor", "codex"]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import __version__
from .runner import build_context
from .store import COMMENT_KINDS, LINK_TYPES, RESOLUTION_CODES, STATUSES, TYPES, DeskError, Store

PROTOCOL_VERSION = "2025-06-18"

_ref = {"type": "string", "description": "Ticket key or number, e.g. TASK-12 or 12"}


def _tool(name, description, props=None, required=None):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props or {}, "required": required or []}}


TOOLS = [
    _tool("whoami", "Your actor name, unread notifications count and assigned open tickets."),
    _tool("list_tickets", "List/search tickets. status accepts a status name, comma list, or open|active|done|all.",
          {"status": {"type": "string", "default": "open"}, "assignee": {"type": "string", "description": "actor name, 'me' or 'none'"},
           "project": {"type": "string"}, "type": {"type": "string", "enum": list(TYPES)},
           "parent": _ref, "q": {"type": "string", "description": "text search"}, "limit": {"type": "integer", "default": 50}}),
    _tool("get_ticket", "Full ticket: fields, comments, attachments (with local file paths), history, links, subtasks.",
          {"ref": _ref}, ["ref"]),
    _tool("get_context", "The ticket rendered as a work briefing (same prompt the runner gives agents).", {"ref": _ref}, ["ref"]),
    _tool("create_ticket", "Create a ticket. Use type=goal for objectives, parent=<goal> for subtasks.",
          {"title": {"type": "string"}, "description": {"type": "string"},
           "type": {"type": "string", "enum": list(TYPES), "default": "task"},
           "priority": {"type": "integer", "minimum": 1, "maximum": 5},
           "impact": {"type": "integer", "minimum": 1, "maximum": 3}, "urgency": {"type": "integer", "minimum": 1, "maximum": 3},
           "project": {"type": "string"}, "assignee": {"type": "string"}, "queue": {"type": "string"},
           "parent": _ref, "tags": {"type": "array", "items": {"type": "string"}}}, ["title"]),
    _tool("update_ticket", "Change fields of a ticket (not status).",
          {"ref": _ref, "title": {"type": "string"}, "description": {"type": "string"},
           "type": {"type": "string", "enum": list(TYPES)}, "priority": {"type": "integer"},
           "project": {"type": "string"}, "assignee": {"type": "string"}, "queue": {"type": "string"},
           "parent": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
           "category": {"type": "string"}}, ["ref"]),
    _tool("set_status", "Move a ticket through its lifecycle. resolved requires a note; waiting_info note = your question.",
          {"ref": _ref, "status": {"type": "string", "enum": STATUSES}, "note": {"type": "string"},
           "resolution_code": {"type": "string", "enum": RESOLUTION_CODES}}, ["ref", "status"]),
    _tool("add_comment", "Comment on a ticket. kind=work_note for internal notes. @name mentions notify that actor.",
          {"ref": _ref, "body": {"type": "string"},
           "kind": {"type": "string", "enum": [k for k in COMMENT_KINDS if k != "system"], "default": "comment"}},
          ["ref", "body"]),
    _tool("attach_file", "Attach a local file (screenshot, log, output) to a ticket.",
          {"ref": _ref, "path": {"type": "string"}}, ["ref", "path"]),
    _tool("link_tickets", "Relate two tickets, e.g. TASK-3 blocks TASK-4.",
          {"src": _ref, "rel": {"type": "string", "enum": list(LINK_TYPES)}, "dst": _ref}, ["src", "rel", "dst"]),
    _tool("claim_ticket", "Take an exclusive lease on a ticket so other agents leave it alone.",
          {"ref": _ref, "minutes": {"type": "integer", "default": 30}}, ["ref"]),
    _tool("release_ticket", "Release your claim.", {"ref": _ref}, ["ref"]),
    _tool("next_ticket", "Claim and return the most important ticket for you to work next (or null).",
          {"project": {"type": "string"}}),
    _tool("ask", "Ask another actor something: @mentions them on `ref` or opens a question ticket assigned to them.",
          {"to": {"type": "string"}, "question": {"type": "string"}, "ref": _ref, "project": {"type": "string"}},
          ["to", "question"]),
    _tool("inbox", "Your notifications (assignments, mentions, status changes).",
          {"unread_only": {"type": "boolean", "default": True}}),
    _tool("mark_read", "Mark notifications read (all, or for one ticket).", {"ref": _ref}),
    _tool("kb_search", "Search the knowledge base.", {"q": {"type": "string"}, "project": {"type": "string"}}),
    _tool("kb_get", "Read a knowledge base article.", {"slug": {"type": "string"}}, ["slug"]),
    _tool("kb_put", "Create or update a knowledge base article (project documentation, decisions, how-tos).",
          {"slug": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"},
           "project": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
          ["slug", "title", "body"]),
    _tool("list_actors", "Humans and agents on the desk, with capabilities."),
    _tool("list_projects", "Projects and their workspaces."),
    _tool("stats", "Desk dashboard numbers."),
]


class DeskTools:
    def __init__(self, store: Store, actor: str):
        self.s, self.actor = store, actor

    def call(self, name, args: dict):
        s, me = self.s, self.actor
        a = dict(args or {})
        if name == "whoami":
            return {"actor": me, "unread": len(s.inbox(me)),
                    "assigned_open": [_brief(t) for t in s.list_tickets(status="open", assignee=me)]}
        if name == "list_tickets":
            if a.get("assignee") == "me":
                a["assignee"] = me
            a.setdefault("status", "open")
            return [_brief(t) for t in s.list_tickets(**a)]
        if name == "get_ticket":
            return s.get_ticket(a["ref"], full=True)
        if name == "get_context":
            return build_context(s, a["ref"], me)
        if name == "create_ticket":
            return s.create_ticket(actor=me, **a)
        if name == "update_ticket":
            ref = a.pop("ref")
            return s.update_ticket(ref, me, **a)
        if name == "set_status":
            return s.set_status(a["ref"], a["status"], me, note=a.get("note"), resolution_code=a.get("resolution_code"))
        if name == "add_comment":
            return s.add_comment(a["ref"], a["body"], me, kind=a.get("kind", "comment"))
        if name == "attach_file":
            p = Path(a["path"]).expanduser()
            return s.add_attachment(a["ref"], p.name, p.read_bytes(), me)
        if name == "link_tickets":
            return s.link(a["src"], a["rel"], a["dst"], me)["links"]
        if name == "claim_ticket":
            return s.claim(a["ref"], me, a.get("minutes", 30))
        if name == "release_ticket":
            return s.release(a["ref"], me)
        if name == "next_ticket":
            return s.next_ticket(me, project=a.get("project"))
        if name == "ask":
            return s.ask(a["to"], a["question"], me, ref=a.get("ref"), project=a.get("project"))
        if name == "inbox":
            return s.inbox(me, unread_only=a.get("unread_only", True))
        if name == "mark_read":
            return {"marked": s.mark_read(me, ticket=a.get("ref"))}
        if name == "kb_search":
            return s.kb_list(a.get("q"), a.get("project"))
        if name == "kb_get":
            return s.kb_get(a["slug"])
        if name == "kb_put":
            return s.kb_put(a["slug"], a["title"], a["body"], me, a.get("project"), a.get("tags"))
        if name == "list_actors":
            return s.list_actors()
        if name == "list_projects":
            return s.list_projects()
        if name == "stats":
            return s.stats()
        raise DeskError(f"unknown tool {name}")


def _brief(t):
    return {k: t[k] for k in ("key", "type", "title", "status", "priority_label", "assignee", "project", "claimed_by")}


def handle(tools: DeskTools, msg: dict):
    """Process one JSON-RPC message; returns a response dict or None for notifications."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None  # notification (e.g. notifications/initialized)

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "agentdesk", "version": __version__},
                   "instructions": f"AgentDesk local ticket system. You act as '{tools.actor}'. "
                                   "Use next_ticket/whoami to find work, comment as you go, resolve with a note."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        try:
            result = tools.call(params.get("name"), params.get("arguments") or {})
            text = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except (DeskError, KeyError, TypeError, OSError) as e:
            return ok({"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve_stdio(store: Store, actor: str):
    tools = DeskTools(store, actor)
    try:
        store.get_actor(actor)
    except DeskError:
        store.upsert_actor(actor, "agent")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle(tools, msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, default=str) + "\n")
            sys.stdout.flush()
