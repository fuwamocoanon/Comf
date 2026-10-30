"""Data layer for AgentDesk.

Everything that makes the ticket system behave like a ticket system lives here:
the state machine, priority matrix, SLA clock, comments vs. work notes,
attachments, links/dependencies, watchers, notifications (@mentions),
claims (so several AI agents never work the same ticket at once), the audit
history and the event stream that the GUI and agents poll.

The GUI (server.py), the CLI (cli.py), the MCP server (mcp.py) and the agent
runner (runner.py) are thin layers over this module, so every interface
enforces the same rules.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

STATUSES = [
    "new",           # logged, nobody has looked at it yet
    "open",          # triaged / ready to be worked
    "in_progress",   # somebody (human or agent) is actively working it
    "waiting_info",  # blocked on the requester answering a question (SLA paused)
    "on_hold",       # parked / blocked on something external (SLA paused)
    "resolved",      # work done, waiting for requester confirmation/feedback
    "closed",        # final, read-only
    "cancelled",     # final, will not be done
]
OPEN_STATUSES = ["new", "open", "in_progress", "waiting_info", "on_hold"]
WORKABLE_STATUSES = ["new", "open", "in_progress"]
DONE_STATUSES = ["resolved", "closed", "cancelled"]
SLA_PAUSED_STATUSES = {"waiting_info", "on_hold"}

TRANSITIONS = {
    "new": ["open", "in_progress", "waiting_info", "on_hold", "resolved", "cancelled"],
    "open": ["in_progress", "waiting_info", "on_hold", "resolved", "cancelled"],
    "in_progress": ["open", "waiting_info", "on_hold", "resolved", "cancelled"],
    "waiting_info": ["open", "in_progress", "resolved", "cancelled"],
    "on_hold": ["open", "in_progress", "cancelled"],
    "resolved": ["closed", "open", "in_progress"],  # -> open/in_progress == reopen
    "closed": [],                                    # reopen only with force
    "cancelled": [],
}

TYPES = {
    "goal": "GOAL",       # a user objective; agents break it down into tasks
    "task": "TASK",
    "bug": "BUG",
    "incident": "INC",
    "request": "REQ",
    "question": "Q",      # also used for agent-to-agent questions
    "change": "CHG",
    "note": "NOTE",       # documentation / findings with no work attached
}

PRIORITY_LABELS = {1: "P1 Critical", 2: "P2 High", 3: "P3 Moderate", 4: "P4 Low", 5: "P5 Planning"}
# Resolution targets in hours, per priority.
SLA_HOURS = {1: 4, 2: 24, 3: 72, 4: 168, 5: 336}

RESOLUTION_CODES = ["done", "fixed", "answered", "workaround", "wont_do",
                    "duplicate", "cannot_reproduce", "obsolete"]
COMMENT_KINDS = ["comment", "work_note", "feedback", "agent_output", "system"]
ACTOR_KINDS = ["human", "agent", "system"]

LINK_TYPES = {
    "blocks": "blocked_by",
    "blocked_by": "blocks",
    "relates_to": "relates_to",
    "duplicates": "duplicated_by",
    "duplicated_by": "duplicates",
}

MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MENTION_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9_][A-Za-z0-9_.-]*)")
TICKET_FIELDS = {"title", "description", "type", "impact", "urgency", "priority", "project",
                 "category", "tags", "assignee", "queue", "parent", "due_at", "requester"}


class DeskError(Exception):
    """A rule was violated (bad transition, missing field, claim conflict...)."""


class NotFound(DeskError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="seconds")


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def default_home() -> Path:
    return Path(os.environ.get("AGENTDESK_HOME") or Path.home() / ".agentdesk")


def default_actor() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return os.environ.get("AGENTDESK_ACTOR") or user


def priority_from(impact: int, urgency: int) -> int:
    """ITIL impact x urgency matrix (1=high .. 3=low) -> priority 1..5."""
    return max(1, min(5, int(impact) + int(urgency) - 1))


SCHEMA = """
CREATE TABLE IF NOT EXISTS actors (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'human',
    display_name TEXT DEFAULT '',
    description TEXT DEFAULT '',
    command TEXT DEFAULT '',          -- for agents: how the runner invokes the model
    capabilities TEXT DEFAULT '',     -- free text: what this agent is good at
    queue TEXT DEFAULT '',            -- assignment group it pulls unassigned work from
    enabled INTEGER DEFAULT 1,
    created_at TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    workspace TEXT DEFAULT '',        -- local folder agents work in for this project
    archived INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE,
    type TEXT NOT NULL DEFAULT 'task',
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    impact INTEGER DEFAULT 2,
    urgency INTEGER DEFAULT 2,
    priority INTEGER DEFAULT 3,
    project TEXT REFERENCES projects(key),
    category TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    requester TEXT,
    assignee TEXT,
    queue TEXT DEFAULT '',
    parent_id INTEGER REFERENCES tickets(id),
    claimed_by TEXT,
    claim_expires TEXT,
    due_at TEXT,
    sla_paused_seconds REAL DEFAULT 0,
    sla_paused_at TEXT,
    resolution_code TEXT,
    resolution_notes TEXT,
    rating INTEGER,
    feedback TEXT,
    reopen_count INTEGER DEFAULT 0,
    agent_runs INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    first_response_at TEXT,
    resolved_at TEXT,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS ix_tickets_assignee ON tickets(assignee);
CREATE INDEX IF NOT EXISTS ix_tickets_parent ON tickets(parent_id);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    author TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'comment',
    body TEXT NOT NULL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_comments_ticket ON comments(ticket_id);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    comment_id INTEGER REFERENCES comments(id),
    filename TEXT NOT NULL,
    mime TEXT,
    size INTEGER,
    sha256 TEXT,
    path TEXT NOT NULL,
    uploaded_by TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id INTEGER NOT NULL REFERENCES tickets(id),
    rel TEXT NOT NULL,
    dst_id INTEGER NOT NULL REFERENCES tickets(id),
    created_by TEXT,
    created_at TEXT,
    UNIQUE (src_id, rel, dst_id)
);
CREATE TABLE IF NOT EXISTS watchers (
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    actor TEXT NOT NULL,
    PRIMARY KEY (ticket_id, actor)
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    actor TEXT,
    field TEXT,
    old TEXT,
    new TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    ticket_id INTEGER REFERENCES tickets(id),
    kind TEXT NOT NULL,
    message TEXT,
    source_actor TEXT,
    created_at TEXT,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_notifications_actor ON notifications(actor, read_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER,
    actor TEXT,
    kind TEXT,
    data TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS kb (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    project TEXT,
    tags TEXT DEFAULT '[]',
    author TEXT,
    version INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);
"""


class Store:
    def __init__(self, home: str | Path | None = None):
        self.home = Path(home) if home else default_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = self.home / "desk.db"
        self.files_dir = self.home / "attachments"
        self.files_dir.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                    isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        with self._lock:
            self.conn.executescript(SCHEMA)
        with self.tx() as c:
            self._ensure_actor(c, "system", "system")

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ infra

    @contextmanager
    def tx(self):
        """Write transaction. Re-entrant within a thread; BEGIN IMMEDIATE takes
        the write lock up front so concurrent agent processes serialize cleanly."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self.conn
                finally:
                    self._depth -= 1
                return
            self.conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0

    def _q(self, sql, args=()):
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def _q1(self, sql, args=()):
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def _event(self, c, ticket_id, actor, event, **data):
        c.execute("INSERT INTO events(ticket_id, actor, kind, data, created_at) VALUES (?,?,?,?,?)",
                  (ticket_id, actor, event, json.dumps(data, default=str), iso()))

    def _history(self, c, ticket_id, actor, field, old, new):
        c.execute("INSERT INTO history(ticket_id, actor, field, old, new, created_at) VALUES (?,?,?,?,?,?)",
                  (ticket_id, actor, field, None if old is None else str(old),
                   None if new is None else str(new), iso()))

    def _notify(self, c, actors, ticket_id, kind, message, source):
        for a in {a for a in actors if a and a != source and a != "system"}:
            c.execute("INSERT INTO notifications(actor, ticket_id, kind, message, source_actor, created_at)"
                      " VALUES (?,?,?,?,?,?)", (a, ticket_id, kind, message, source, iso()))

    def _ensure_actor(self, c, name, kind=None):
        if not name:
            raise DeskError("actor name required")
        row = c.execute("SELECT name FROM actors WHERE name=?", (name,)).fetchone()
        if not row:
            c.execute("INSERT INTO actors(name, kind, display_name, created_at, last_seen) VALUES (?,?,?,?,?)",
                      (name, kind or "human", name, iso(), iso()))
        else:
            c.execute("UPDATE actors SET last_seen=? WHERE name=?", (iso(), name))
        return name

    def _tid(self, ref) -> int:
        """Accepts 12, '12', '#12', 'TASK-12', 'goal-12'."""
        if isinstance(ref, int):
            tid = ref
        else:
            m = re.search(r"(\d+)\s*$", str(ref or ""))
            if not m:
                raise NotFound(f"not a ticket reference: {ref!r}")
            tid = int(m.group(1))
        if not self._q1("SELECT 1 FROM tickets WHERE id=?", (tid,)):
            raise NotFound(f"ticket {ref} not found")
        return tid

    def _row(self, tid):
        return self._q1("SELECT * FROM tickets WHERE id=?", (tid,))

    # ----------------------------------------------------------------- actors

    def upsert_actor(self, name, kind=None, **fields) -> dict:
        allowed = {"display_name", "description", "command", "capabilities", "queue", "enabled"}
        if kind and kind not in ACTOR_KINDS:
            raise DeskError(f"kind must be one of {ACTOR_KINDS}")
        with self.tx() as c:
            self._ensure_actor(c, name, kind)
            if kind:
                c.execute("UPDATE actors SET kind=? WHERE name=?", (kind, name))
            for k, v in fields.items():
                if k in allowed and v is not None:
                    c.execute(f"UPDATE actors SET {k}=? WHERE name=?", (v, name))
            self._event(c, None, name, "actor.updated", name=name)
        return self.get_actor(name)

    def get_actor(self, name) -> dict:
        row = self._q1("SELECT * FROM actors WHERE name=?", (name,))
        if not row:
            raise NotFound(f"actor {name} not found")
        return dict(row)

    def list_actors(self, kind=None) -> list[dict]:
        sql, args = "SELECT * FROM actors WHERE name != 'system'", []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        rows = [dict(r) for r in self._q(sql + " ORDER BY kind, name", args)]
        for r in rows:
            r["open_assigned"] = self._q1(
                f"SELECT COUNT(*) FROM tickets WHERE assignee=? AND status IN ({_ph(OPEN_STATUSES)})",
                [r["name"], *OPEN_STATUSES])[0]
        return rows

    def heartbeat(self, name):
        with self.tx() as c:
            self._ensure_actor(c, name)

    # --------------------------------------------------------------- projects

    def add_project(self, key, name=None, description="", workspace="") -> dict:
        key = key.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,15}", key):
            raise DeskError("project key must be 1-16 chars: letters, digits, underscore, starting with a letter")
        with self.tx() as c:
            if c.execute("SELECT 1 FROM projects WHERE key=?", (key,)).fetchone():
                c.execute("UPDATE projects SET name=COALESCE(?, name), description=?, workspace=? WHERE key=?",
                          (name, description, workspace, key))
            else:
                c.execute("INSERT INTO projects(key, name, description, workspace, created_at) VALUES (?,?,?,?,?)",
                          (key, name or key, description, workspace, iso()))
            self._event(c, None, None, "project.updated", key=key)
        return self.get_project(key)

    def get_project(self, key) -> dict:
        row = self._q1("SELECT * FROM projects WHERE key=?", (str(key).upper(),))
        if not row:
            raise NotFound(f"project {key} not found")
        return dict(row)

    def list_projects(self) -> list[dict]:
        rows = [dict(r) for r in self._q("SELECT * FROM projects WHERE archived=0 ORDER BY key")]
        for r in rows:
            counts = self._q("SELECT status, COUNT(*) n FROM tickets WHERE project=? GROUP BY status", (r["key"],))
            r["counts"] = {x["status"]: x["n"] for x in counts}
        return rows

    # ---------------------------------------------------------------- tickets

    def create_ticket(self, title, actor=None, type="task", description="", impact=2, urgency=2,
                      priority=None, project=None, assignee=None, queue="", parent=None, tags=None,
                      category="", due_at=None, requester=None) -> dict:
        actor = actor or default_actor()
        title = (title or "").strip()
        if not title:
            raise DeskError("title is required")
        if type not in TYPES:
            raise DeskError(f"type must be one of {list(TYPES)}")
        impact, urgency = _clamp(impact, 1, 3), _clamp(urgency, 1, 3)
        priority = _clamp(priority, 1, 5) if priority else priority_from(impact, urgency)
        if project:
            project = self.get_project(project)["key"]
        parent_id = self._tid(parent) if parent else None
        if parent_id and not project:
            project = self._row(parent_id)["project"]
        requester = requester or actor
        now = utcnow()
        due = due_at or iso(now + timedelta(hours=SLA_HOURS[priority]))
        with self.tx() as c:
            self._ensure_actor(c, actor)
            self._ensure_actor(c, requester)
            if assignee:
                self._ensure_actor(c, assignee)
            cur = c.execute(
                "INSERT INTO tickets(type, title, description, status, impact, urgency, priority, project,"
                " category, tags, requester, assignee, queue, parent_id, due_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (type, title, description or "", "new", impact, urgency, priority, project, category or "",
                 json.dumps(_tags(tags)), requester, assignee, queue or "", parent_id, due, iso(now), iso(now)))
            tid = cur.lastrowid
            key = f"{TYPES[type]}-{tid}"
            c.execute("UPDATE tickets SET key=? WHERE id=?", (key, tid))
            self._history(c, tid, actor, "created", None, key)
            for w in (requester, assignee):
                if w:
                    c.execute("INSERT OR IGNORE INTO watchers VALUES (?,?)", (tid, w))
            if assignee:
                self._notify(c, [assignee], tid, "assigned", f"{key} assigned to you: {title}", actor)
            if parent_id:
                prow = c.execute("SELECT key, assignee FROM tickets WHERE id=?", (parent_id,)).fetchone()
                self._history(c, parent_id, actor, "child_added", None, key)
                self._notify(c, [prow["assignee"]], parent_id, "child",
                             f"Subtask {key} added to {prow['key']}: {title}", actor)
            self._event(c, tid, actor, "ticket.created", key=key, title=title)
        return self.get_ticket(tid)

    def get_ticket(self, ref, full=False) -> dict:
        tid = self._tid(ref)
        t = self._ticket_dict(self._row(tid))
        if not full:
            return t
        t["comments"] = [dict(r) for r in self._q(
            "SELECT * FROM comments WHERE ticket_id=? ORDER BY id", (tid,))]
        t["attachments"] = self.list_attachments(tid)
        t["history"] = [dict(r) for r in self._q("SELECT * FROM history WHERE ticket_id=? ORDER BY id", (tid,))]
        t["watchers"] = [r[0] for r in self._q("SELECT actor FROM watchers WHERE ticket_id=? ORDER BY actor", (tid,))]
        t["children"] = [self._summary(r) for r in self._q(
            "SELECT * FROM tickets WHERE parent_id=? ORDER BY id", (tid,))]
        t["parent"] = self._summary(self._row(t["parent_id"])) if t["parent_id"] else None
        t["links"] = self._links(tid)
        t["allowed_transitions"] = TRANSITIONS[t["status"]]
        return t

    def _summary(self, row) -> dict:
        return {k: row[k] for k in ("id", "key", "type", "title", "status", "priority", "assignee")}

    def _links(self, tid) -> list[dict]:
        out = []
        for r in self._q("SELECT l.*, t.key, t.title, t.status FROM links l JOIN tickets t ON t.id=l.dst_id"
                         " WHERE l.src_id=?", (tid,)):
            out.append({"id": r["id"], "rel": r["rel"], "ticket_id": r["dst_id"], "key": r["key"],
                        "title": r["title"], "status": r["status"]})
        for r in self._q("SELECT l.*, t.key, t.title, t.status FROM links l JOIN tickets t ON t.id=l.src_id"
                         " WHERE l.dst_id=?", (tid,)):
            out.append({"id": r["id"], "rel": LINK_TYPES[r["rel"]], "ticket_id": r["src_id"], "key": r["key"],
                        "title": r["title"], "status": r["status"]})
        return out

    def _blockers(self, tid) -> list[str]:
        """Keys of unfinished tickets this ticket is blocked by."""
        return [l["key"] for l in self._links(tid)
                if l["rel"] == "blocked_by" and l["status"] not in DONE_STATUSES]

    def _ticket_dict(self, row) -> dict:
        t = dict(row)
        t["tags"] = json.loads(t["tags"] or "[]")
        t["priority_label"] = PRIORITY_LABELS.get(t["priority"], str(t["priority"]))
        t["is_open"] = t["status"] in OPEN_STATUSES
        t["sla"] = self._sla(t)
        now = utcnow()
        claim_exp = parse_dt(t["claim_expires"])
        if t["claimed_by"] and claim_exp and claim_exp < now:
            t["claimed_by"] = None  # stale claim
        t["child_count"] = self._q1("SELECT COUNT(*) FROM tickets WHERE parent_id=?", (t["id"],))[0]
        t["blocked_by"] = self._blockers(t["id"])
        return t

    def _sla(self, t) -> dict:
        due = parse_dt(t["due_at"])
        if not due:
            return {"due_at": None, "breached": False, "paused": False, "remaining_seconds": None}
        paused = float(t["sla_paused_seconds"] or 0)
        if t["sla_paused_at"]:
            paused += (utcnow() - parse_dt(t["sla_paused_at"])).total_seconds()
        effective = due + timedelta(seconds=paused)
        end = parse_dt(t["resolved_at"]) or utcnow()
        remaining = (effective - end).total_seconds()
        return {"due_at": iso(effective), "breached": remaining < 0,
                "paused": bool(t["sla_paused_at"]), "remaining_seconds": int(remaining)}

    def list_tickets(self, status=None, assignee=None, requester=None, project=None, type=None,
                     parent=None, q=None, tag=None, queue=None, claimed_by=None, limit=200,
                     order="priority") -> list[dict]:
        sql, args = ["SELECT * FROM tickets WHERE 1=1"], []
        statuses = _status_filter(status)
        if statuses:
            sql.append(f"AND status IN ({_ph(statuses)})")
            args += statuses
        for col, val in (("assignee", assignee), ("requester", requester), ("type", type),
                         ("queue", queue), ("claimed_by", claimed_by)):
            if val == "none":
                sql.append(f"AND ({col} IS NULL OR {col}='')")
            elif val:
                sql.append(f"AND {col}=?")
                args.append(val)
        if project:
            sql.append("AND project=?")
            args.append(str(project).upper())
        if parent:
            sql.append("AND parent_id=?")
            args.append(self._tid(parent))
        if tag:
            sql.append("AND tags LIKE ?")
            args.append(f'%"{tag}"%')
        if q:
            like = f"%{q}%"
            sql.append("AND (title LIKE ? OR description LIKE ? OR key LIKE ? OR resolution_notes LIKE ?"
                       " OR id IN (SELECT ticket_id FROM comments WHERE body LIKE ?))")
            args += [like] * 5
        orders = {"priority": "priority ASC, due_at ASC, id ASC", "updated": "updated_at DESC",
                  "created": "id DESC", "due": "due_at ASC"}
        sql.append(f"ORDER BY {orders.get(order, orders['priority'])} LIMIT ?")
        args.append(int(limit))
        return [self._ticket_dict(r) for r in self._q(" ".join(sql), args)]

    def update_ticket(self, ref, actor=None, **fields) -> dict:
        actor = actor or default_actor()
        tid = self._tid(ref)
        unknown = set(fields) - TICKET_FIELDS
        if unknown:
            raise DeskError(f"unknown/readonly fields: {sorted(unknown)} (use set_status for status)")
        row = self._row(tid)
        if row["status"] in ("closed", "cancelled"):
            raise DeskError(f"{row['key']} is {row['status']} and read-only")
        changes = {}
        if "type" in fields and fields["type"] not in TYPES:
            raise DeskError(f"type must be one of {list(TYPES)}")
        if "project" in fields and fields["project"]:
            fields["project"] = self.get_project(fields["project"])["key"]
        if "parent" in fields:
            p = fields.pop("parent")
            pid = self._tid(p) if p else None
            if pid == tid:
                raise DeskError("a ticket cannot be its own parent")
            fields["parent_id"] = pid
        if "tags" in fields:
            fields["tags"] = json.dumps(_tags(fields["tags"]))
        for k in ("impact", "urgency"):
            if k in fields:
                fields[k] = _clamp(fields[k], 1, 3)
        if ("impact" in fields or "urgency" in fields) and "priority" not in fields:
            fields["priority"] = priority_from(fields.get("impact", row["impact"]),
                                               fields.get("urgency", row["urgency"]))
        if "priority" in fields:
            fields["priority"] = _clamp(fields["priority"], 1, 5)
        for k, v in fields.items():
            if v == "":
                v = None if k in ("assignee", "parent_id", "project", "due_at") else v
            if row[k] != v:
                changes[k] = (row[k], v)
        if not changes:
            return self.get_ticket(tid)
        with self.tx() as c:
            self._ensure_actor(c, actor)
            for k, (old, new) in changes.items():
                c.execute(f"UPDATE tickets SET {k}=? WHERE id=?", (new, tid))
                self._history(c, tid, actor, k, old, new)
            c.execute("UPDATE tickets SET updated_at=? WHERE id=?", (iso(), tid))
            if "assignee" in changes and changes["assignee"][1]:
                new = changes["assignee"][1]
                self._ensure_actor(c, new)
                c.execute("INSERT OR IGNORE INTO watchers VALUES (?,?)", (tid, new))
                self._notify(c, [new], tid, "assigned", f"{row['key']} assigned to you: {row['title']}", actor)
                if row["claimed_by"] and row["claimed_by"] != new:
                    c.execute("UPDATE tickets SET claimed_by=NULL, claim_expires=NULL WHERE id=?", (tid,))
            self._notify(c, self._watchers(c, tid), tid, "updated",
                         f"{row['key']} updated by {actor}: {', '.join(changes)}", actor)
            self._event(c, tid, actor, "ticket.updated", key=row["key"], fields=list(changes))
        return self.get_ticket(tid)

    def _watchers(self, c, tid):
        return [r[0] for r in c.execute("SELECT actor FROM watchers WHERE ticket_id=?", (tid,))]

    def set_status(self, ref, status, actor=None, note=None, resolution_code=None, force=False) -> dict:
        actor = actor or default_actor()
        tid = self._tid(ref)
        row = self._row(tid)
        old = row["status"]
        if status not in STATUSES:
            raise DeskError(f"status must be one of {STATUSES}")
        if status == old:
            if note:
                self.add_comment(tid, note, actor)
            return self.get_ticket(tid)
        if status not in TRANSITIONS[old] and not force:
            raise DeskError(f"{row['key']}: cannot go from {old} to {status}; allowed: {TRANSITIONS[old] or 'none (final)'}")
        if status == "resolved" and not (note or row["resolution_notes"]):
            raise DeskError("resolving requires a resolution note explaining what was done")
        if resolution_code and resolution_code not in RESOLUTION_CODES:
            raise DeskError(f"resolution_code must be one of {RESOLUTION_CODES}")
        if status == "resolved":
            blockers = [k["key"] for k in self.get_ticket(tid, full=True)["children"]
                        if k["status"] not in DONE_STATUSES]
            if blockers and not force:
                raise DeskError(f"{row['key']} still has open subtasks: {', '.join(blockers)} (use force to override)")
        now = iso()
        with self.tx() as c:
            self._ensure_actor(c, actor)
            sets = {"status": status, "updated_at": now}
            # SLA clock pauses while waiting on someone else
            if status in SLA_PAUSED_STATUSES and not row["sla_paused_at"]:
                sets["sla_paused_at"] = now
            if status not in SLA_PAUSED_STATUSES and row["sla_paused_at"]:
                sets["sla_paused_seconds"] = (row["sla_paused_seconds"] or 0) + (
                    utcnow() - parse_dt(row["sla_paused_at"])).total_seconds()
                sets["sla_paused_at"] = None
            if status == "in_progress" and not row["first_response_at"]:
                sets["first_response_at"] = now
            if status == "resolved":
                sets.update(resolved_at=now, resolution_code=resolution_code or row["resolution_code"] or "done",
                            resolution_notes=note or row["resolution_notes"])
            if status == "cancelled":
                sets.update(resolution_code=resolution_code or "wont_do",
                            resolution_notes=note or row["resolution_notes"], closed_at=now)
            if status == "closed":
                sets["closed_at"] = now
            if old in DONE_STATUSES and status in OPEN_STATUSES:  # reopen
                sets.update(reopen_count=(row["reopen_count"] or 0) + 1, resolved_at=None, closed_at=None,
                            resolution_code=None, resolution_notes=None)
            if status in DONE_STATUSES or status in SLA_PAUSED_STATUSES:
                sets.update(claimed_by=None, claim_expires=None)
            for k, v in sets.items():
                c.execute(f"UPDATE tickets SET {k}=? WHERE id=?", (v, tid))
            self._history(c, tid, actor, "status", old, status)
            if note:
                kind = "comment"
                prefix = {"waiting_info": "Question / info needed", "resolved": "Resolution",
                          "on_hold": "On hold", "cancelled": "Cancelled"}.get(status)
                self._insert_comment(c, tid, actor, f"**{prefix}:** {note}" if prefix else note, kind, row)
            targets = self._watchers(c, tid) + [row["requester"]]
            self._notify(c, targets, tid, "status", f"{row['key']} {old} -> {status} by {actor}", actor)
            self._event(c, tid, actor, "ticket.status", key=row["key"], old=old, new=status)
            if status in DONE_STATUSES and row["parent_id"]:
                self._check_parent(c, row["parent_id"], actor)
        return self.get_ticket(tid)

    def _check_parent(self, c, parent_id, actor):
        open_children = c.execute(
            f"SELECT COUNT(*) FROM tickets WHERE parent_id=? AND status NOT IN ({_ph(DONE_STATUSES)})",
            [parent_id, *DONE_STATUSES]).fetchone()[0]
        if open_children == 0:
            p = c.execute("SELECT * FROM tickets WHERE id=?", (parent_id,)).fetchone()
            if p["status"] in OPEN_STATUSES:
                msg = f"All subtasks of {p['key']} are done. Review and resolve the parent."
                self._insert_comment(c, parent_id, "system", msg, "system", p)
                self._notify(c, [p["assignee"], p["requester"]], parent_id, "children_done", msg, "system")

    # ------------------------------------------------------- claims / dispatch

    def claim(self, ref, actor=None, minutes=30) -> dict:
        """Take an exclusive, expiring lease on a ticket so no other agent works it."""
        actor = actor or default_actor()
        tid = self._tid(ref)
        now = utcnow()
        with self.tx() as c:
            self._ensure_actor(c, actor)
            row = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
            if row["status"] in DONE_STATUSES:
                raise DeskError(f"{row['key']} is {row['status']}")
            exp = parse_dt(row["claim_expires"])
            if row["claimed_by"] and row["claimed_by"] != actor and exp and exp > now:
                raise DeskError(f"{row['key']} is claimed by {row['claimed_by']} until {row['claim_expires']}")
            c.execute("UPDATE tickets SET claimed_by=?, claim_expires=?, updated_at=? WHERE id=?",
                      (actor, iso(now + timedelta(minutes=minutes)), iso(now), tid))
            if not row["assignee"]:
                c.execute("UPDATE tickets SET assignee=? WHERE id=?", (actor, tid))
                c.execute("INSERT OR IGNORE INTO watchers VALUES (?,?)", (tid, actor))
                self._history(c, tid, actor, "assignee", None, actor)
            if row["claimed_by"] != actor:
                self._history(c, tid, actor, "claimed_by", row["claimed_by"], actor)
            self._event(c, tid, actor, "ticket.claimed", key=row["key"])
            if row["status"] in ("new", "open"):
                self.set_status(tid, "in_progress", actor)
        return self.get_ticket(tid)

    def release(self, ref, actor=None, force=False) -> dict:
        actor = actor or default_actor()
        tid = self._tid(ref)
        row = self._row(tid)
        if row["claimed_by"] and row["claimed_by"] != actor and not force:
            raise DeskError(f"{row['key']} is claimed by {row['claimed_by']}, not {actor}")
        with self.tx() as c:
            c.execute("UPDATE tickets SET claimed_by=NULL, claim_expires=NULL WHERE id=?", (tid,))
            if row["claimed_by"]:
                self._history(c, tid, actor, "claimed_by", row["claimed_by"], None)
            self._event(c, tid, actor, "ticket.released", key=row["key"])
        return self.get_ticket(tid)

    def next_ticket(self, actor=None, project=None, types=None, claim=True, minutes=30) -> dict | None:
        """Pick the most important ticket this actor should work on next.

        Order: tickets where the actor was @mentioned (unread) > tickets assigned
        to the actor > unassigned tickets in the actor's queue. Tickets blocked by
        unfinished dependencies or claimed by someone else are skipped."""
        actor = actor or default_actor()
        a = self._q1("SELECT * FROM actors WHERE name=?", (actor,))
        queue = a["queue"] if a else ""
        types = [types] if isinstance(types, str) else types
        now = utcnow()
        candidates: list[tuple[str, sqlite3.Row]] = []
        seen = set()

        def add(reason, rows):
            for r in rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    candidates.append((reason, r))

        add("mentioned", self._q(
            "SELECT t.* FROM tickets t JOIN notifications n ON n.ticket_id=t.id"
            " WHERE n.actor=? AND n.read_at IS NULL AND n.kind IN ('mention','question')"
            f" AND t.status NOT IN ('closed','cancelled') ORDER BY t.priority, t.id", (actor,)))
        add("assigned", self._q(
            f"SELECT * FROM tickets WHERE assignee=? AND status IN ({_ph(WORKABLE_STATUSES)})"
            " ORDER BY priority, due_at, id", [actor, *WORKABLE_STATUSES]))
        if queue:
            add("queue", self._q(
                "SELECT * FROM tickets WHERE (assignee IS NULL OR assignee='') AND queue=?"
                " AND status IN ('new','open') ORDER BY priority, due_at, id", (queue,)))
        for reason, r in candidates:
            if project and r["project"] != str(project).upper():
                continue
            if types and r["type"] not in types:
                continue
            exp = parse_dt(r["claim_expires"])
            if r["claimed_by"] and r["claimed_by"] != actor and exp and exp > now:
                continue
            if reason != "mentioned" and self._blockers(r["id"]):
                continue
            if claim:
                try:
                    t = self.claim(r["id"], actor, minutes) if r["status"] not in DONE_STATUSES else self.get_ticket(r["id"])
                except DeskError:
                    continue
            else:
                t = self.get_ticket(r["id"])
            t["reason"] = reason
            return t
        return None

    # -------------------------------------------------------------- comments

    def _insert_comment(self, c, tid, actor, body, kind, row):
        cur = c.execute("INSERT INTO comments(ticket_id, author, kind, body, created_at) VALUES (?,?,?,?,?)",
                        (tid, actor, kind, body, iso()))
        c.execute("UPDATE tickets SET updated_at=? WHERE id=?", (iso(), tid))
        cid = cur.lastrowid
        known = {r[0] for r in c.execute("SELECT name FROM actors")}
        mentioned = [m for m in MENTION_RE.findall(body) if m in known and m != actor]
        if mentioned:
            self._notify(c, mentioned, tid, "mention",
                         f"{actor} mentioned you on {row['key']}: {_snip(body)}", actor)
            for m in mentioned:
                c.execute("INSERT OR IGNORE INTO watchers VALUES (?,?)", (tid, m))
        self._event(c, tid, actor, "comment.added", key=row["key"], comment_id=cid, kind=kind)
        return cid, mentioned

    def add_comment(self, ref, body, actor=None, kind="comment") -> dict:
        """kind: comment (visible to requester), work_note (internal), feedback, agent_output."""
        actor = actor or default_actor()
        if kind not in COMMENT_KINDS:
            raise DeskError(f"kind must be one of {COMMENT_KINDS}")
        body = (body or "").strip()
        if not body:
            raise DeskError("comment body is empty")
        tid = self._tid(ref)
        row = self._row(tid)
        if row["status"] in ("closed", "cancelled"):
            raise DeskError(f"{row['key']} is {row['status']}; reopen it or create a follow-up ticket")
        with self.tx() as c:
            self._ensure_actor(c, actor)
            cid, mentioned = self._insert_comment(c, tid, actor, body, kind, row)
            if actor != row["requester"] and not row["first_response_at"] and kind in ("comment", "work_note", "agent_output"):
                c.execute("UPDATE tickets SET first_response_at=? WHERE id=?", (iso(), tid))
            others = [w for w in self._watchers(c, tid) if w not in mentioned]
            self._notify(c, others, tid, "comment", f"{actor} commented on {row['key']}: {_snip(body)}", actor)
            # Classic service-desk behaviour: the requester answering moves the ticket back to work.
            if row["status"] == "waiting_info" and actor == row["requester"] and kind == "comment":
                self.set_status(tid, "open", actor)
                self._insert_comment(c, tid, "system", "Requester replied; ticket returned to open.", "system", row)
        return dict(self._q1("SELECT * FROM comments WHERE id=?", (cid,)))

    def add_feedback(self, ref, rating, text="", actor=None, reopen=False) -> dict:
        actor = actor or default_actor()
        rating = _clamp(rating, 1, 5)
        tid = self._tid(ref)
        row = self._row(tid)
        with self.tx() as c:
            self._ensure_actor(c, actor)
            c.execute("UPDATE tickets SET rating=?, feedback=?, updated_at=? WHERE id=?", (rating, text, iso(), tid))
            self._history(c, tid, actor, "rating", row["rating"], rating)
            self._insert_comment(c, tid, actor, f"**Feedback {'★' * rating}{'☆' * (5 - rating)}** {text}".strip(),
                                 "feedback", row)
            self._notify(c, [row["assignee"]], tid, "feedback", f"{actor} rated {row['key']} {rating}/5", actor)
            if reopen and row["status"] in ("resolved", "closed"):
                self.set_status(tid, "open", actor, force=True)
        return self.get_ticket(tid)

    def ask(self, to, question, actor=None, ref=None, project=None) -> dict:
        """Agent-to-agent (or human-to-agent) communication. On an existing ticket it
        posts an @mention; otherwise it opens a question ticket assigned to `to`."""
        actor = actor or default_actor()
        self.get_actor(to)
        if ref:
            return {"comment": self.add_comment(ref, f"@{to} {question}", actor)}
        title = _snip(question.splitlines()[0], 80)
        return {"ticket": self.create_ticket(title, actor, type="question", description=question,
                                             assignee=to, project=project)}

    # ----------------------------------------------------------- attachments

    def add_attachment(self, ref, filename, data: bytes, actor=None, comment_id=None, mime=None) -> dict:
        actor = actor or default_actor()
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise DeskError(f"attachment too large (max {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB)")
        tid = self._tid(ref)
        row = self._row(tid)
        name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(filename or "file").name).strip() or "file"
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        digest = hashlib.sha256(data).hexdigest()
        folder = self.files_dir / str(tid)
        folder.mkdir(parents=True, exist_ok=True)
        with self.tx() as c:
            self._ensure_actor(c, actor)
            cur = c.execute("INSERT INTO attachments(ticket_id, comment_id, filename, mime, size, sha256, path,"
                            " uploaded_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (tid, comment_id, name, mime, len(data), digest, "", actor, iso()))
            aid = cur.lastrowid
            path = folder / f"{aid}_{name}"
            path.write_bytes(data)
            c.execute("UPDATE attachments SET path=? WHERE id=?", (str(path), aid))
            c.execute("UPDATE tickets SET updated_at=? WHERE id=?", (iso(), tid))
            self._history(c, tid, actor, "attachment", None, name)
            self._event(c, tid, actor, "attachment.added", key=row["key"], attachment_id=aid, filename=name)
        return self.get_attachment(aid)

    def get_attachment(self, aid) -> dict:
        row = self._q1("SELECT * FROM attachments WHERE id=?", (int(aid),))
        if not row:
            raise NotFound(f"attachment {aid} not found")
        return dict(row)

    def list_attachments(self, ref) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM attachments WHERE ticket_id=? ORDER BY id", (self._tid(ref),))]

    # ---------------------------------------------------------- links/watch

    def link(self, src, rel, dst, actor=None) -> dict:
        actor = actor or default_actor()
        if rel not in LINK_TYPES:
            raise DeskError(f"rel must be one of {list(LINK_TYPES)}")
        a, b = self._tid(src), self._tid(dst)
        if a == b:
            raise DeskError("cannot link a ticket to itself")
        if rel in ("blocked_by", "duplicated_by"):  # store canonically
            a, b, rel = b, a, LINK_TYPES[rel]
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO links(src_id, rel, dst_id, created_by, created_at) VALUES (?,?,?,?,?)",
                      (a, rel, b, actor, iso()))
            ka, kb = self._row(a)["key"], self._row(b)["key"]
            self._history(c, a, actor, "link", None, f"{rel} {kb}")
            self._history(c, b, actor, "link", None, f"{LINK_TYPES[rel]} {ka}")
            self._event(c, a, actor, "ticket.linked", src=ka, rel=rel, dst=kb)
        return self.get_ticket(src, full=True)

    def unlink(self, link_id, actor=None):
        with self.tx() as c:
            c.execute("DELETE FROM links WHERE id=?", (int(link_id),))
            self._event(c, None, actor, "ticket.unlinked", link_id=link_id)

    def watch(self, ref, actor=None, on=True) -> list[str]:
        actor = actor or default_actor()
        tid = self._tid(ref)
        with self.tx() as c:
            self._ensure_actor(c, actor)
            if on:
                c.execute("INSERT OR IGNORE INTO watchers VALUES (?,?)", (tid, actor))
            else:
                c.execute("DELETE FROM watchers WHERE ticket_id=? AND actor=?", (tid, actor))
            return self._watchers(c, tid)

    # --------------------------------------------------------- notifications

    def inbox(self, actor=None, unread_only=True, limit=100) -> list[dict]:
        actor = actor or default_actor()
        sql = ("SELECT n.*, t.key, t.title, t.status FROM notifications n LEFT JOIN tickets t ON t.id=n.ticket_id"
               " WHERE n.actor=?")
        if unread_only:
            sql += " AND n.read_at IS NULL"
        return [dict(r) for r in self._q(sql + " ORDER BY n.id DESC LIMIT ?", (actor, int(limit)))]

    def mark_read(self, actor=None, ids=None, ticket=None) -> int:
        actor = actor or default_actor()
        sql, args = "UPDATE notifications SET read_at=? WHERE actor=? AND read_at IS NULL", [iso(), actor]
        if ids:
            sql += f" AND id IN ({_ph(ids)})"
            args += [int(i) for i in ids]
        if ticket:
            sql += " AND ticket_id=?"
            args.append(self._tid(ticket))
        with self.tx() as c:
            return c.execute(sql, args).rowcount

    def events(self, since=0, limit=200, ticket=None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE id>?", [int(since)]
        if ticket:
            sql += " AND ticket_id=?"
            args.append(self._tid(ticket))
        rows = [dict(r) for r in self._q(sql + " ORDER BY id LIMIT ?", args + [int(limit)])]
        for r in rows:
            r["data"] = json.loads(r["data"] or "{}")
        return rows

    def last_event_id(self) -> int:
        return self._q1("SELECT COALESCE(MAX(id), 0) FROM events")[0]

    # -------------------------------------------------------- knowledge base

    def kb_put(self, slug, title, body, actor=None, project=None, tags=None) -> dict:
        actor = actor or default_actor()
        slug = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")
        if not slug:
            raise DeskError("slug required")
        with self.tx() as c:
            self._ensure_actor(c, actor)
            if c.execute("SELECT 1 FROM kb WHERE slug=?", (slug,)).fetchone():
                c.execute("UPDATE kb SET title=?, body=?, project=?, tags=?, author=?, version=version+1, updated_at=?"
                          " WHERE slug=?", (title, body, project, json.dumps(_tags(tags)), actor, iso(), slug))
            else:
                c.execute("INSERT INTO kb VALUES (?,?,?,?,?,?,1,?,?)",
                          (slug, title, body, project, json.dumps(_tags(tags)), actor, iso(), iso()))
            self._event(c, None, actor, "kb.updated", slug=slug, title=title)
        return self.kb_get(slug)

    def kb_get(self, slug) -> dict:
        row = self._q1("SELECT * FROM kb WHERE slug=?", (slug,))
        if not row:
            raise NotFound(f"kb article {slug} not found")
        d = dict(row)
        d["tags"] = json.loads(d["tags"] or "[]")
        return d

    def kb_list(self, q=None, project=None) -> list[dict]:
        sql, args = "SELECT slug, title, project, tags, author, version, updated_at FROM kb WHERE 1=1", []
        if q:
            sql += " AND (title LIKE ? OR body LIKE ? OR tags LIKE ?)"
            args += [f"%{q}%"] * 3
        if project:
            sql += " AND project=?"
            args.append(str(project).upper())
        rows = [dict(r) for r in self._q(sql + " ORDER BY updated_at DESC", args)]
        for r in rows:
            r["tags"] = json.loads(r["tags"] or "[]")
        return rows

    # ------------------------------------------------------------ reporting

    def stats(self) -> dict:
        by_status = {r[0]: r[1] for r in self._q("SELECT status, COUNT(*) FROM tickets GROUP BY status")}
        open_rows = self.list_tickets(status="open", limit=100000)
        by_priority, by_assignee, by_type = {}, {}, {}
        for t in open_rows:
            by_priority[t["priority_label"]] = by_priority.get(t["priority_label"], 0) + 1
            by_assignee[t["assignee"] or "(unassigned)"] = by_assignee.get(t["assignee"] or "(unassigned)", 0) + 1
            by_type[t["type"]] = by_type.get(t["type"], 0) + 1
        rated = self._q1("SELECT AVG(rating), COUNT(rating) FROM tickets WHERE rating IS NOT NULL")
        return {
            "by_status": by_status,
            "open_total": len(open_rows),
            "open_by_priority": by_priority,
            "open_by_assignee": by_assignee,
            "open_by_type": by_type,
            "sla_breached": sum(1 for t in open_rows if t["sla"]["breached"]),
            "waiting_info": by_status.get("waiting_info", 0),
            "avg_rating": round(rated[0], 2) if rated[0] else None,
            "rated_count": rated[1],
        }

    def auto_close(self, days=7, actor="system") -> list[str]:
        """Resolved tickets with no reopen after `days` are closed automatically."""
        cutoff = iso(utcnow() - timedelta(days=days))
        keys = []
        for r in self._q("SELECT id, key FROM tickets WHERE status='resolved' AND resolved_at < ?", (cutoff,)):
            self.set_status(r["id"], "closed", actor, note=f"Auto-closed after {days} days in resolved.")
            keys.append(r["key"])
        return keys

    def record_agent_run(self, ref):
        with self.tx() as c:
            c.execute("UPDATE tickets SET agent_runs=agent_runs+1 WHERE id=?", (self._tid(ref),))

    def meta(self) -> dict:
        return {"statuses": STATUSES, "open_statuses": OPEN_STATUSES, "transitions": TRANSITIONS,
                "types": TYPES, "priorities": PRIORITY_LABELS, "sla_hours": SLA_HOURS,
                "resolution_codes": RESOLUTION_CODES, "comment_kinds": COMMENT_KINDS,
                "link_types": list(LINK_TYPES), "actor_kinds": ACTOR_KINDS}


# ----------------------------------------------------------------- helpers

def _ph(seq) -> str:
    return ",".join("?" * len(seq))


def _clamp(v, lo, hi) -> int:
    return max(lo, min(hi, int(v)))


def _tags(tags) -> list[str]:
    if not tags:
        return []
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    return sorted({str(t).strip().lower() for t in tags if str(t).strip()})


def _snip(s, n=120) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _status_filter(status) -> list[str]:
    if not status or status == "all":
        return []
    if isinstance(status, str):
        status = [s for s in status.split(",") if s]
    out = []
    for s in status:
        out += {"open": OPEN_STATUSES, "active": WORKABLE_STATUSES, "done": DONE_STATUSES}.get(s, [s])
    bad = set(out) - set(STATUSES)
    if bad:
        raise DeskError(f"unknown status {sorted(bad)}; use {STATUSES} or open/active/done/all")
    return out
