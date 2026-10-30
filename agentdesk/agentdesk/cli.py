"""Command line interface. Works directly on the local database, so agents can
use it without the server running. Add --json to any command for machine output.

Identity: --as NAME, or the AGENTDESK_ACTOR environment variable, or your OS user.
Data folder: --home DIR, or AGENTDESK_HOME, default ~/.agentdesk
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, runner
from .store import (COMMENT_KINDS, LINK_TYPES, RESOLUTION_CODES, STATUSES, TYPES, DeskError, Store,
                    default_actor)


def _out(args, data, text=None):
    if args.json or text is None:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(text)


def _row(t):
    claim = f" ⚑{t['claimed_by']}" if t.get("claimed_by") else ""
    sla = " !SLA" if t["sla"]["breached"] and t["is_open"] else ""
    return (f"{t['key']:<10} {t['status']:<12} P{t['priority']} {t['type']:<8} "
            f"{(t['assignee'] or '-'):<12} {t['title'][:70]}{claim}{sla}")


def _show(t):
    lines = [f"{t['key']}  {t['title']}",
             f"  type={t['type']} status={t['status']} priority={t['priority_label']} project={t['project'] or '-'}",
             f"  requester={t['requester']} assignee={t['assignee'] or '-'} queue={t['queue'] or '-'}"
             f" claimed_by={t['claimed_by'] or '-'}",
             f"  created={t['created_at']} updated={t['updated_at']} sla_due={t['sla']['due_at']}"
             f"{' BREACHED' if t['sla']['breached'] else ''}{' (paused)' if t['sla']['paused'] else ''}"]
    if t["tags"]:
        lines.append(f"  tags={', '.join(t['tags'])}")
    if t.get("parent"):
        lines.append(f"  parent={t['parent']['key']} {t['parent']['title']}")
    if t.get("blocked_by"):
        lines.append(f"  blocked_by={', '.join(t['blocked_by'])}")
    if t["resolution_notes"]:
        lines.append(f"  resolution[{t['resolution_code']}]: {t['resolution_notes']}")
    if t["rating"]:
        lines.append(f"  feedback: {t['rating']}/5 {t['feedback'] or ''}")
    lines += ["", t["description"] or "(no description)"]
    if t.get("children"):
        lines += ["", "Subtasks:"] + [f"  {c['key']} [{c['status']}] {c['title']} ({c['assignee'] or '-'})"
                                      for c in t["children"]]
    if t.get("links"):
        lines += ["", "Links:"] + [f"  {l['rel']} {l['key']} [{l['status']}] {l['title']}" for l in t["links"]]
    if t.get("attachments"):
        lines += ["", "Attachments:"] + [f"  #{a['id']} {a['filename']} ({a['size']} B) {a['path']}"
                                         for a in t["attachments"]]
    if t.get("comments"):
        lines += ["", "Activity:"]
        for c in t["comments"]:
            lines.append(f"  --- {c['author']} [{c['kind']}] {c['created_at']}")
            lines += [f"  {ln}" for ln in c["body"].splitlines()]
    lines += ["", f"Allowed next statuses: {', '.join(t.get('allowed_transitions', [])) or 'none'}"]
    return "\n".join(lines)


def _text_arg(args, attr="body"):
    """Text from positional arg, -f FILE, or stdin ('-')."""
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8")
    val = getattr(args, attr, None)
    if val == "-":
        return sys.stdin.read()
    return val


def build_parser():
    p = argparse.ArgumentParser(prog="agentdesk", description="Local service desk for humans and AI agents.")
    p.add_argument("--home", help="data folder (default $AGENTDESK_HOME or ~/.agentdesk)")
    p.add_argument("--as", dest="actor", help="act as this actor (default $AGENTDESK_ACTOR or OS user)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the web GUI + API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--runner", action="store_true", help="also run all enabled agents in the background")

    s = sub.add_parser("mcp", help="run the MCP server on stdio (for Claude Code, Codex, ...)")
    s.add_argument("--actor", dest="mcp_actor", help="identity the connected AI acts as")

    s = sub.add_parser("run", help="dispatch tickets to agents (loop, or --once)")
    s.add_argument("agents", nargs="*", help="agent names (default: all enabled agents with a command)")
    s.add_argument("--once", action="store_true", help="one ticket per agent, then exit")
    s.add_argument("--ticket", help="run the (single) agent on this specific ticket")
    s.add_argument("--interval", type=int, default=15)
    s.add_argument("--timeout", type=int, default=1800, help="seconds per agent run")

    sub.add_parser("whoami", help="identity, unread notifications, assigned work")

    s = sub.add_parser("create", help="create a ticket")
    s.add_argument("title")
    s.add_argument("-d", "--description", default="")
    s.add_argument("-f", "--file", help="read description from file")
    s.add_argument("-t", "--type", default="task", choices=list(TYPES))
    s.add_argument("-p", "--priority", type=int, choices=range(1, 6))
    s.add_argument("--impact", type=int, default=2, choices=(1, 2, 3))
    s.add_argument("--urgency", type=int, default=2, choices=(1, 2, 3))
    s.add_argument("--project")
    s.add_argument("--assign", dest="assignee")
    s.add_argument("--queue", default="")
    s.add_argument("--parent")
    s.add_argument("--tags", default="")
    s.add_argument("--category", default="")
    s.add_argument("--requester")

    s = sub.add_parser("goal", help="shortcut: create a goal (optionally assigned to a planner agent)")
    s.add_argument("title")
    s.add_argument("-d", "--description", default="")
    s.add_argument("-f", "--file")
    s.add_argument("--project")
    s.add_argument("--assign", dest="assignee")
    s.add_argument("-p", "--priority", type=int, choices=range(1, 6))

    s = sub.add_parser("list", aliases=["ls"], help="list tickets")
    s.add_argument("-s", "--status", default="open", help="status, comma list, or open|active|done|all")
    s.add_argument("-a", "--assignee", help="name, 'me' or 'none'")
    s.add_argument("--project")
    s.add_argument("-t", "--type", choices=list(TYPES))
    s.add_argument("--parent")
    s.add_argument("-q", "--query", dest="q")
    s.add_argument("--tag")
    s.add_argument("--queue")
    s.add_argument("--order", default="priority", choices=["priority", "updated", "created", "due"])
    s.add_argument("-n", "--limit", type=int, default=100)

    s = sub.add_parser("show", help="show a ticket with its full activity")
    s.add_argument("ref")

    s = sub.add_parser("context", help="print the agent briefing for a ticket")
    s.add_argument("ref")

    s = sub.add_parser("update", help="change ticket fields")
    s.add_argument("ref")
    for f in ("title", "description", "project", "assignee", "queue", "parent", "category", "requester", "due_at"):
        s.add_argument(f"--{f.replace('_', '-')}", dest=f)
    s.add_argument("--type", choices=list(TYPES))
    s.add_argument("-p", "--priority", type=int, choices=range(1, 6))
    s.add_argument("--impact", type=int, choices=(1, 2, 3))
    s.add_argument("--urgency", type=int, choices=(1, 2, 3))
    s.add_argument("--tags")

    s = sub.add_parser("assign", help="assign a ticket")
    s.add_argument("ref")
    s.add_argument("assignee", help="actor name, or 'none'")

    s = sub.add_parser("status", help=f"change status: {', '.join(STATUSES)}")
    s.add_argument("ref")
    s.add_argument("status", choices=STATUSES)
    s.add_argument("-m", "--note", help="resolution note / question / reason")
    s.add_argument("--code", choices=RESOLUTION_CODES, help="resolution code")
    s.add_argument("--force", action="store_true")

    for name, st in (("resolve", "resolved"), ("close", "closed"), ("reopen", "open"), ("start", "in_progress")):
        s = sub.add_parser(name, help=f"shortcut for: status REF {st}")
        s.add_argument("ref")
        s.add_argument("-m", "--note")
        s.add_argument("--code", choices=RESOLUTION_CODES)
        s.set_defaults(target_status=st)

    s = sub.add_parser("comment", help="add a comment (use @name to mention)")
    s.add_argument("ref")
    s.add_argument("body", nargs="?", help="text, or '-' for stdin")
    s.add_argument("-f", "--file")
    s.add_argument("--internal", action="store_true", help="internal work note")
    s.add_argument("--kind", choices=COMMENT_KINDS)

    s = sub.add_parser("attach", help="attach files (screenshots, logs...)")
    s.add_argument("ref")
    s.add_argument("paths", nargs="+")

    s = sub.add_parser("link", help=f"relate tickets: {', '.join(LINK_TYPES)}")
    s.add_argument("src")
    s.add_argument("rel", choices=list(LINK_TYPES))
    s.add_argument("dst")

    s = sub.add_parser("claim", help="take an exclusive lease on a ticket")
    s.add_argument("ref")
    s.add_argument("--minutes", type=int, default=30)
    s = sub.add_parser("release", help="release your claim")
    s.add_argument("ref")
    s = sub.add_parser("next", help="claim and show the next ticket you should work")
    s.add_argument("--project")
    s.add_argument("--peek", action="store_true", help="don't claim")

    s = sub.add_parser("watch", help="follow a ticket's notifications")
    s.add_argument("ref")
    s.add_argument("--off", action="store_true")

    s = sub.add_parser("feedback", help="rate a resolved ticket 1-5")
    s.add_argument("ref")
    s.add_argument("rating", type=int, choices=range(1, 6))
    s.add_argument("text", nargs="?", default="")
    s.add_argument("--reopen", action="store_true")

    s = sub.add_parser("ask", help="ask another actor a question (new question ticket, or @mention on --ticket)")
    s.add_argument("to")
    s.add_argument("question")
    s.add_argument("--ticket")
    s.add_argument("--project")

    s = sub.add_parser("inbox", help="your notifications")
    s.add_argument("--all", action="store_true")
    s.add_argument("--read", action="store_true", help="mark all as read after listing")

    s = sub.add_parser("events", help="event stream (use --follow to tail)")
    s.add_argument("--since", type=int, default=0)
    s.add_argument("--follow", action="store_true")

    sub.add_parser("stats", help="dashboard numbers")

    s = sub.add_parser("project", help="manage projects")
    ps = s.add_subparsers(dest="action", required=True)
    x = ps.add_parser("add")
    x.add_argument("key")
    x.add_argument("name", nargs="?")
    x.add_argument("-d", "--description", default="")
    x.add_argument("-w", "--workspace", default="", help="local folder agents work in")
    ps.add_parser("list")

    for group, kind in (("agent", "agent"), ("actor", None)):
        s = sub.add_parser(group, help=f"manage {group}s" + (" (local AI models)" if kind else " (humans & agents)"))
        ps = s.add_subparsers(dest="action", required=True)
        x = ps.add_parser("add")
        x.add_argument("name")
        if not kind:
            x.add_argument("--kind", choices=["human", "agent"], default="human")
        x.add_argument("--preset", choices=list(runner.PRESETS), help="default command for a known CLI")
        x.add_argument("--command", help="command line; prompt goes to stdin unless {prompt}/{prompt_file} used")
        x.add_argument("--capabilities", default=None, help="what it is good at (shown to other agents)")
        x.add_argument("--description", default=None)
        x.add_argument("--queue", default=None, help="assignment group to pull unassigned work from")
        x.add_argument("--disable", action="store_true")
        x.set_defaults(fixed_kind=kind)
        ps.add_parser("list").set_defaults(fixed_kind=kind)

    s = sub.add_parser("kb", help="knowledge base / project documentation")
    ps = s.add_subparsers(dest="action", required=True)
    x = ps.add_parser("put")
    x.add_argument("slug")
    x.add_argument("title")
    x.add_argument("body", nargs="?", help="markdown, or '-' for stdin")
    x.add_argument("-f", "--file")
    x.add_argument("--project")
    x.add_argument("--tags")
    x = ps.add_parser("get")
    x.add_argument("slug")
    x = ps.add_parser("list")
    x.add_argument("-q", "--query")
    x.add_argument("--project")

    s = sub.add_parser("autoclose", help="close tickets resolved more than N days ago")
    s.add_argument("--days", type=int, default=7)
    return p


def _hoist_globals(argv: list[str]) -> list[str]:
    """Allow --as/--home/--json anywhere on the command line, not just before the subcommand."""
    front, rest, i = [], [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            rest += argv[i:]
            break
        if a in ("--as", "--home") and i + 1 < len(argv):
            front += [a, argv[i + 1]]
            i += 2
            continue
        if a == "--json" or a.startswith(("--as=", "--home=")):
            front.append(a)
        else:
            rest.append(a)
        i += 1
    return front + rest


def main(argv=None) -> int:
    args = build_parser().parse_args(_hoist_globals(list(sys.argv[1:] if argv is None else argv)))
    store = Store(args.home)
    me = args.actor or default_actor()
    try:
        return dispatch(store, me, args) or 0
    except DeskError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()


def dispatch(s: Store, me: str, args) -> int | None:
    c = args.cmd
    if c == "serve":
        from .server import serve
        serve(s, args.host, args.port, with_runner=args.runner)
    elif c == "mcp":
        from .mcp import serve_stdio
        serve_stdio(s, args.mcp_actor or me)
    elif c == "run":
        if args.once or args.ticket:
            names = args.agents or [a["name"] for a in s.list_actors(kind="agent") if a["enabled"] and a["command"]]
            results = []
            for n in names:
                runner.wake_parents(s)
                r = runner.run_once(s, n, ticket=args.ticket, timeout=args.timeout)
                results.append(r or {"agent": n, "idle": True})
            _out(args, results, "\n".join(
                f"{r['agent']}: idle" if r.get("idle") else
                f"{r['agent']}: {r['ticket']} -> {r['status']} (exit {r['exit_code']}, {r['seconds']}s)"
                for r in results) or "no agents configured")
        else:
            runner.run_loop(s, args.agents or None, interval=args.interval, timeout=args.timeout)
    elif c == "whoami":
        s.heartbeat(me)
        mine = s.list_tickets(status="open", assignee=me)
        inbox = s.inbox(me)
        _out(args, {"actor": me, "home": str(s.home), "unread": len(inbox), "assigned": mine},
             f"You are {me} (data: {s.home})\n{len(inbox)} unread notification(s)\n"
             f"Assigned open tickets:\n" + ("\n".join(_row(t) for t in mine) or "  none"))
    elif c in ("create", "goal"):
        desc = _text_arg(args, "description") if args.file else args.description
        kw = dict(description=desc, project=args.project, assignee=args.assignee, priority=args.priority)
        if c == "create":
            kw.update(type=args.type, impact=args.impact, urgency=args.urgency, queue=args.queue,
                      parent=args.parent, tags=args.tags, category=args.category, requester=args.requester)
        else:
            kw["type"] = "goal"
        t = s.create_ticket(args.title, me, **kw)
        _out(args, t, f"created {t['key']}: {t['title']}")
    elif c in ("list", "ls"):
        assignee = me if args.assignee == "me" else args.assignee
        rows = s.list_tickets(status=args.status, assignee=assignee, project=args.project, type=args.type,
                              parent=args.parent, q=args.q, tag=args.tag, queue=args.queue,
                              order=args.order, limit=args.limit)
        _out(args, rows, "\n".join(_row(t) for t in rows) or "no tickets")
    elif c == "show":
        t = s.get_ticket(args.ref, full=True)
        _out(args, t, _show(t))
    elif c == "context":
        ctx = runner.build_context(s, args.ref, me)
        _out(args, {"context": ctx}, ctx)
    elif c == "update":
        fields = {k: getattr(args, k) for k in ("title", "description", "project", "assignee", "queue", "parent",
                                                "category", "requester", "due_at", "type", "priority", "impact",
                                                "urgency", "tags") if getattr(args, k) is not None}
        if fields.get("assignee") == "none":
            fields["assignee"] = None
        t = s.update_ticket(args.ref, me, **fields)
        _out(args, t, f"updated {t['key']}")
    elif c == "assign":
        t = s.update_ticket(args.ref, me, assignee=None if args.assignee == "none" else args.assignee)
        _out(args, t, f"{t['key']} assigned to {t['assignee'] or 'nobody'}")
    elif c in ("status", "resolve", "close", "reopen", "start"):
        st = getattr(args, "target_status", None) or args.status
        t = s.set_status(args.ref, st, me, note=args.note, resolution_code=args.code,
                         force=getattr(args, "force", False) or c == "reopen")
        _out(args, t, f"{t['key']} is now {t['status']}")
    elif c == "comment":
        body = _text_arg(args)
        if not body:
            raise DeskError("comment text required (argument, -f FILE, or '-' for stdin)")
        kind = args.kind or ("work_note" if args.internal else "comment")
        cm = s.add_comment(args.ref, body, me, kind=kind)
        _out(args, cm, f"comment #{cm['id']} added to {args.ref}")
    elif c == "attach":
        res = []
        for path in args.paths:
            p = Path(path).expanduser()
            res.append(s.add_attachment(args.ref, p.name, p.read_bytes(), me))
        _out(args, res, "\n".join(f"attached #{a['id']} {a['filename']} ({a['size']} B)" for a in res))
    elif c == "link":
        s.link(args.src, args.rel, args.dst, me)
        _out(args, {"ok": True}, f"{args.src} {args.rel} {args.dst}")
    elif c == "claim":
        t = s.claim(args.ref, me, args.minutes)
        _out(args, t, f"{t['key']} claimed by {me} until {t['claim_expires']}")
    elif c == "release":
        t = s.release(args.ref, me)
        _out(args, t, f"{t['key']} released")
    elif c == "next":
        t = s.next_ticket(me, project=args.project, claim=not args.peek)
        if t:
            t = {**s.get_ticket(t["id"], full=True), "reason": t["reason"]}
        _out(args, t, (f"[{t['reason']}]\n" + _show(t)) if t else "nothing to do")
    elif c == "watch":
        w = s.watch(args.ref, me, on=not args.off)
        _out(args, w, f"watchers: {', '.join(w)}")
    elif c == "feedback":
        t = s.add_feedback(args.ref, args.rating, args.text, me, reopen=args.reopen)
        _out(args, t, f"feedback saved on {t['key']} ({t['status']})")
    elif c == "ask":
        r = s.ask(args.to, args.question, me, ref=args.ticket, project=args.project)
        _out(args, r, f"asked {args.to} " + (f"on {args.ticket}" if args.ticket else f"in {r['ticket']['key']}"))
    elif c == "inbox":
        rows = s.inbox(me, unread_only=not args.all)
        _out(args, rows, "\n".join(f"#{n['id']} {n['created_at']} [{n['kind']}] {n['message']}" for n in rows)
             or "inbox empty")
        if args.read:
            s.mark_read(me)
    elif c == "events":
        since = args.since
        while True:
            for e in s.events(since):
                since = e["id"]
                print(json.dumps(e, default=str) if args.json else
                      f"{e['id']} {e['created_at']} {e['actor'] or '-'} {e['kind']} {json.dumps(e['data'])}", flush=True)
            if not args.follow:
                break
            time.sleep(2)
    elif c == "stats":
        st = s.stats()
        _out(args, st, "\n".join(f"{k}: {v}" for k, v in st.items()))
    elif c == "project":
        if args.action == "add":
            ws = str(Path(args.workspace).expanduser().resolve()) if args.workspace else ""
            p = s.add_project(args.key, args.name, args.description, ws)
            _out(args, p, f"project {p['key']} saved")
        else:
            rows = s.list_projects()
            _out(args, rows, "\n".join(f"{p['key']:<8} {p['name']:<30} {p['workspace']}" for p in rows) or "no projects")
    elif c in ("agent", "actor"):
        if args.action == "add":
            kind = args.fixed_kind or args.kind
            command = args.command or (runner.PRESETS[args.preset] if args.preset else None)
            a = s.upsert_actor(args.name, kind, command=command, capabilities=args.capabilities,
                               description=args.description, queue=args.queue,
                               enabled=0 if args.disable else 1)
            _out(args, a, f"{a['kind']} {a['name']} saved" + (f" (command: {a['command']})" if a["command"] else ""))
        else:
            rows = s.list_actors(args.fixed_kind)
            _out(args, rows, "\n".join(
                f"{a['name']:<14} {a['kind']:<6} open={a['open_assigned']:<3} last_seen={a['last_seen']} "
                f"{a['command'] or ''}" for a in rows) or "none")
    elif c == "kb":
        if args.action == "put":
            body = _text_arg(args) or ""
            k = s.kb_put(args.slug, args.title, body, me, args.project.upper() if args.project else None, args.tags)
            _out(args, k, f"kb/{k['slug']} saved (v{k['version']})")
        elif args.action == "get":
            k = s.kb_get(args.slug)
            _out(args, k, f"# {k['title']}  (v{k['version']} by {k['author']})\n\n{k['body']}")
        else:
            rows = s.kb_list(args.query, args.project)
            _out(args, rows, "\n".join(f"{k['slug']:<30} {k['title']}" for k in rows) or "no articles")
    elif c == "autoclose":
        keys = s.auto_close(args.days, me)
        _out(args, keys, f"closed: {', '.join(keys) or 'none'}")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
