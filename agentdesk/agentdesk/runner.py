"""Agent runner: hands tickets to locally installed AI models and records results.

Each agent is an actor with kind='agent' and a `command`, e.g.

    claude -p --allowedTools "Bash(agentdesk:*)" Read Edit Write
    codex exec --full-auto -
    ollama run llama3.1

The runner picks the agent's next ticket (store.next_ticket), builds a prompt
that contains the full ticket context plus instructions for talking back to
the desk, runs the command with the prompt on stdin (or substituted for
{prompt} / {prompt_file}), and posts the output to the ticket.

Two ways for an agent to act on the desk:
  1. Tool-capable agents (Claude Code, Codex...) call the `agentdesk` CLI or
     the MCP server directly while they work.
  2. Plain text models (ollama, llama.cpp...) end their answer with a fenced
     ```agentdesk JSON block; the runner applies it (status, subtasks,
     comments, knowledge base articles, questions to other agents).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .store import DONE_STATUSES, DeskError, Store, TYPES, RESOLUTION_CODES

PRESETS = {
    "claude": 'claude -p --allowedTools "Bash(agentdesk:*)" "Bash(python -m agentdesk:*)" Read Edit Write Glob Grep',
    "codex": "codex exec --full-auto -",
    "gemini": "gemini --yolo",
    "ollama": "ollama run llama3.1",
    "aider": "aider --yes --message-file {prompt_file}",
}

MAX_COMMENT_CHARS = 20000
DIRECTIVE_RE = re.compile(r"```agentdesk\s*\n(.*?)```", re.S)
STATUS_LINE_RE = re.compile(r"^\s*STATUS:\s*(\w+)\s*$", re.M | re.I)


def cli_command() -> str:
    """How an agent subprocess should invoke the desk CLI."""
    exe = shutil.which("agentdesk")
    return "agentdesk" if exe else f"{shlex.quote(sys.executable)} -m agentdesk"


def build_context(store: Store, ref, actor: str, max_comments=30) -> str:
    t = store.get_ticket(ref, full=True)
    cli = cli_command()
    lines = [
        f"# You are `{actor}`, an AI agent working tickets in AgentDesk (a local service desk).",
        "",
        f"## Ticket {t['key']}: {t['title']}",
        f"- Type: {t['type']} | Status: {t['status']} | Priority: {t['priority_label']}",
        f"- Project: {t['project'] or '-'} | Requester: {t['requester']} | Assignee: {t['assignee'] or '-'}",
        f"- SLA due: {t['sla']['due_at']}{' (BREACHED)' if t['sla']['breached'] else ''}",
    ]
    if t["tags"]:
        lines.append(f"- Tags: {', '.join(t['tags'])}")
    if t.get("reason"):
        lines.append(f"- Why you got this: {t['reason']}")
    lines += ["", "### Description", t["description"] or "(none)"]
    if t["project"]:
        p = store.get_project(t["project"])
        lines += ["", f"### Project {p['key']} — {p['name']}", p["description"] or "",
                  f"Workspace folder: {p['workspace'] or '(not set)'}"]
    if t["parent"]:
        parent = store.get_ticket(t["parent"]["id"])
        lines += ["", f"### Parent {parent['key']} ({parent['status']}): {parent['title']}",
                  (parent["description"] or "")[:3000]]
    if t["children"]:
        lines += ["", "### Subtasks"] + [
            f"- {c['key']} [{c['status']}] {c['title']} (assignee: {c['assignee'] or '-'})" for c in t["children"]]
    if t["links"]:
        lines += ["", "### Linked tickets"] + [f"- {l['rel']} {l['key']} [{l['status']}] {l['title']}" for l in t["links"]]
    if t["attachments"]:
        lines += ["", "### Attachments (local files you can open)"] + [
            f"- {a['filename']} ({a['mime']}, {a['size']} bytes): {a['path']}" for a in t["attachments"]]
    comments = [c for c in t["comments"] if c["kind"] != "system"][-max_comments:]
    if comments:
        lines += ["", "### Conversation (oldest first)"]
        for c in comments:
            body = c["body"] if len(c["body"]) < 4000 else c["body"][:4000] + "\n…(truncated)"
            lines.append(f"**{c['author']}** [{c['kind']}] {c['created_at']}:\n{body}\n")
    kb = store.kb_list(project=t["project"])[:10] if t["project"] else []
    if kb:
        lines += ["", "### Knowledge base articles for this project"] + [f"- {k['slug']}: {k['title']}" for k in kb]
    agents = [a for a in store.list_actors() if a["name"] != actor]
    if agents:
        lines += ["", "### Other actors you can @mention or assign work to"] + [
            f"- {a['name']} ({a['kind']}){': ' + a['capabilities'] if a['capabilities'] else ''}" for a in agents]
    lines += ["", GUIDE.format(cli=cli, key=t["key"], types="|".join(TYPES), codes="|".join(RESOLUTION_CODES))]
    if t["type"] == "goal":
        lines += ["", GOAL_GUIDE.format(cli=cli, key=t["key"])]
    return "\n".join(lines)


GUIDE = """## How to work this ticket
Do the work the ticket asks for, then report back. You can update the desk in either way:

A) With the CLI (if you can run shell commands) — your identity is already set via AGENTDESK_ACTOR:
   {cli} show {key}                                  # full ticket
   {cli} comment {key} "text"                        # public reply (use @name to ping another agent/human)
   {cli} comment {key} --internal "text"             # internal work note
   {cli} attach {key} path/to/file.png               # attach screenshots, logs, outputs
   {cli} create "title" --type task --parent {key} --assign <agent> -d "details"
   {cli} status {key} waiting_info -m "question for the requester"
   {cli} status {key} resolved -m "what you did" --code done
   {cli} kb put <slug> "Title" -f notes.md           # document reusable knowledge
   {cli} ask <agent> "question"                      # open a question for another agent

B) Or end your reply with ONE fenced block the runner will apply for you:
```agentdesk
{{"status": "resolved|waiting_info|on_hold|continue",
  "resolution": "summary of what was done (required for resolved)",
  "code": "{codes}",
  "comments": ["optional extra comments, may @mention others"],
  "subtasks": [{{"title": "...", "description": "...", "assignee": "agent-name", "type": "{types}"}}],
  "kb": [{{"slug": "short-name", "title": "...", "body": "markdown"}}]}}
```
Use "waiting_info" when you need the human to answer something (put the question in "resolution").
Use "continue" only if you made progress and need another turn. Everything you print is saved on the ticket."""

GOAL_GUIDE = """## This is a GOAL
Analyze it, then break it into concrete subtasks (each with a clear definition of done), assign each
to the most suitable agent above (or leave unassigned), and link dependencies with
`{cli} link TASK-A blocks TASK-B`. Record the plan as a comment on {key}. Leave the goal in
"continue" status while subtasks are open; it is resolved once every subtask is done."""


def parse_directives(output: str) -> dict:
    d: dict = {}
    m = DIRECTIVE_RE.findall(output or "")
    if m:
        try:
            d = json.loads(m[-1])
        except json.JSONDecodeError as e:
            d = {"_error": f"could not parse agentdesk block: {e}"}
    if "status" not in d:
        s = STATUS_LINE_RE.findall(output or "")
        if s:
            d["status"] = s[-1].lower()
    return d


def render_command(template: str, prompt: str) -> tuple[list[str], str | None, str | None]:
    """Returns (argv, stdin_text, temp_file_to_delete)."""
    tmp = None
    if "{prompt_file}" in template:
        fd, tmp = tempfile.mkstemp(prefix="agentdesk-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        template = template.replace("{prompt_file}", shlex.quote(tmp))
    if "{prompt}" in template:
        argv = [a.replace("{prompt}", prompt) for a in shlex.split(template)]
        return argv, None, tmp
    return shlex.split(template), (None if tmp else prompt), tmp


def run_once(store: Store, agent: str, ticket=None, timeout=1800, max_runs=6, project=None) -> dict | None:
    """Run one unit of work for `agent`. Returns a result dict, or None if idle."""
    a = store.get_actor(agent)
    if not a["command"]:
        raise DeskError(f"agent {agent} has no command configured (agentdesk agent add {agent} --preset claude)")
    store.heartbeat(agent)
    t = store.claim(ticket, agent, minutes=timeout // 60 + 5) if ticket else \
        store.next_ticket(agent, project=project, minutes=timeout // 60 + 5)
    if not t:
        return None
    key = t["key"]
    store.mark_read(agent, ticket=key)
    store.record_agent_run(key)
    prompt = build_context(store, key, agent)
    cwd = None
    if t["project"]:
        ws = store.get_project(t["project"])["workspace"]
        if ws and Path(ws).expanduser().is_dir():
            cwd = str(Path(ws).expanduser())
    env = dict(os.environ, AGENTDESK_ACTOR=agent, AGENTDESK_HOME=str(store.home), AGENTDESK_TICKET=key)
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    store.add_comment(key, f"Agent run started (`{a['command']}`) in {cwd or os.getcwd()}", agent, kind="work_note")
    argv, stdin, tmp = render_command(a["command"], prompt)
    started = time.time()
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout,
                              cwd=cwd, env=env, encoding="utf-8", errors="replace")
        output, code = proc.stdout or "", proc.returncode
        if proc.stderr and code != 0:
            output += "\n\n--- stderr ---\n" + proc.stderr[-4000:]
    except FileNotFoundError:
        output, code = f"Command not found: {argv[0]}", 127
    except subprocess.TimeoutExpired as e:
        output, code = f"Timed out after {timeout}s.\n{(e.stdout or '')[-4000:] if isinstance(e.stdout, str) else ''}", 124
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
    elapsed = round(time.time() - started, 1)
    return _record(store, agent, key, output, code, elapsed, max_runs)


def _record(store: Store, agent, key, output, code, elapsed, max_runs) -> dict:
    text = output.strip() or "(no output)"
    if len(text) > MAX_COMMENT_CHARS:
        store.add_attachment(key, f"{agent}-output-{int(time.time())}.txt", output.encode(), agent)
        text = text[:MAX_COMMENT_CHARS] + "\n\n…(truncated; full output attached)"
    store.add_comment(key, text, agent, kind="agent_output")
    directives = parse_directives(output)
    applied = []
    if directives.get("_error"):
        store.add_comment(key, directives["_error"], "system", kind="work_note")
    for c in directives.get("comments") or []:
        store.add_comment(key, str(c), agent)
        applied.append("comment")
    for s in directives.get("subtasks") or []:
        try:
            st = store.create_ticket(s["title"], agent, type=s.get("type", "task") if s.get("type") in TYPES else "task",
                                     description=s.get("description", ""), parent=key,
                                     assignee=s.get("assignee") or None)
            applied.append(f"created {st['key']}")
        except (DeskError, KeyError) as e:
            store.add_comment(key, f"Could not create subtask {s!r}: {e}", "system", kind="work_note")
    for k in directives.get("kb") or []:
        try:
            store.kb_put(k["slug"], k.get("title", k["slug"]), k.get("body", ""), agent,
                         project=store.get_ticket(key)["project"])
            applied.append(f"kb {k['slug']}")
        except (DeskError, KeyError) as e:
            store.add_comment(key, f"Could not save kb article: {e}", "system", kind="work_note")

    t = store.get_ticket(key)
    status = (directives.get("status") or "").lower()
    note = directives.get("resolution")
    try:
        if code != 0:
            store.set_status(key, "waiting_info", agent,
                             note=f"Agent `{agent}` failed (exit {code}). A human should look at the output above.")
        elif t["status"] in DONE_STATUSES or t["status"] in ("waiting_info", "on_hold"):
            pass  # agent already moved the ticket itself via CLI/MCP
        elif status in ("resolved", "waiting_info", "on_hold", "cancelled"):
            if status == "resolved" and t["child_count"] and any(
                    c["status"] not in DONE_STATUSES for c in store.get_ticket(key, full=True)["children"]):
                store.set_status(key, "on_hold", agent, note="Waiting for subtasks to complete.")
            else:
                store.set_status(key, status, agent, note=note or "See agent output above.",
                                 resolution_code=directives.get("code") if directives.get("code") in RESOLUTION_CODES else None)
        elif status == "continue" and t["agent_runs"] < max_runs:
            store.set_status(key, "open", agent)
        elif t["child_count"] and any(c["status"] not in DONE_STATUSES
                                      for c in store.get_ticket(key, full=True)["children"]):
            # e.g. a goal that was broken down: wait for the subtasks, the parent is notified when they finish
            store.set_status(key, "on_hold", agent, note="Waiting for subtasks to complete.")
        else:
            store.set_status(key, "waiting_info", agent,
                             note="Agent run finished without a final status. Please review the output and reply or resolve.")
    except DeskError as e:
        store.add_comment(key, f"Runner could not update status: {e}", "system", kind="work_note")
    finally:
        try:
            store.release(key, agent, force=True)
        except DeskError:
            pass
    return {"ticket": key, "agent": agent, "exit_code": code, "seconds": elapsed,
            "status": store.get_ticket(key)["status"], "applied": applied}


def wake_parents(store: Store):
    """Goals parked on_hold for subtasks go back to open once all subtasks are done."""
    for t in store.list_tickets(status="on_hold", limit=1000):
        if t["child_count"]:
            kids = store.get_ticket(t["id"], full=True)["children"]
            if all(k["status"] in DONE_STATUSES for k in kids):
                store.set_status(t["id"], "open", "system", note="All subtasks are done; back to the assignee for review.")


def run_loop(store: Store, agents: list[str] | None = None, interval=15, timeout=1800,
             stop: threading.Event | None = None, log=print):
    """Run every enabled agent in its own thread, polling for work."""
    stop = stop or threading.Event()
    names = agents or [a["name"] for a in store.list_actors(kind="agent") if a["enabled"] and a["command"]]
    if not names:
        log("No agents with a command configured. Add one: agentdesk agent add claude --preset claude")
        return

    def worker(name):
        own = Store(store.home)  # one connection per thread/agent
        log(f"[runner] {name} started")
        while not stop.is_set():
            try:
                wake_parents(own)
                res = run_once(own, name, timeout=timeout)
                if res:
                    log(f"[runner] {name}: {res['ticket']} -> {res['status']} ({res['seconds']}s) {res['applied']}")
                    continue
            except Exception as e:  # keep the loop alive
                log(f"[runner] {name}: error: {e}")
            stop.wait(interval)

    threads = [threading.Thread(target=worker, args=(n,), daemon=True, name=f"agent-{n}") for n in names]
    for th in threads:
        th.start()
    try:
        while any(th.is_alive() for th in threads) and not stop.is_set():
            stop.wait(1)
    except KeyboardInterrupt:
        stop.set()
    for th in threads:
        th.join(timeout=2)
