# What makes a ticket system work

This document is the theory behind AgentDesk: the concepts that ServiceNow, Jira Service
Management, Zendesk and friends share, why each one exists, and how AgentDesk adapts them for
a desk where many of the "agents" are local AI models.

## 1. The ticket is a contract, not a message

A chat message gets lost. A ticket is a durable record that says: *someone needs something,
someone owns it, this is where it stands, and this is how it ended.* Every other feature exists
to keep that contract honest.

| Field | Why it exists |
|---|---|
| **Key** (`TASK-12`) | A stable handle people and agents can quote anywhere. The prefix shows the type. |
| **Type** | Different work has different expectations: `goal`, `task`, `bug`, `incident`, `request`, `question`, `change`, `note`. |
| **Title / description** | The problem statement. The description is the brief an agent works from. |
| **Requester** | The person the work is for. Only they can confirm it is really done, and their reply un-pauses a ticket that is waiting on them. |
| **Assignee** | One accountable owner. "Everybody" means nobody. |
| **Queue / assignment group** | Unassigned work goes to a team (`backend`, `docs`) and members pull from it. |
| **Project** | Groups tickets, and for agents it says *which folder on disk* to work in. |
| **Impact × urgency → priority** | The ITIL matrix: impact (how many are affected) and urgency (how soon it hurts) combine into P1–P5, so priority is not just whoever shouts loudest. |
| **Tags / category** | For finding and reporting. |
| **Due date / SLA** | A promise about time. |
| **Resolution code + notes** | *How* it ended (`fixed`, `answered`, `wont_do`, `duplicate`...) and what was done. The notes are the most valuable text in the system. |
| **Rating / feedback** | Did the requester agree it was solved? |

## 2. The lifecycle (state machine)

```
            ┌──────────────► cancelled (final)
            │
 new ──► open ──► in_progress ──► resolved ──► closed (final)
  │       ▲  ▲        │  ▲           │
  │       │  └────────┘  │           └─► open (reopen; counted)
  │       │              │
  └──► waiting_info ─────┘   (SLA paused: waiting on the requester)
  └──► on_hold ──────────┘   (SLA paused: blocked on something external)
```

Rules AgentDesk enforces, and why:

* **Only listed transitions are allowed.** You cannot jump `new → closed`. Skipping
  `resolved` means skipping the requester's chance to say "that didn't fix it".
* **Resolving needs a resolution note.** No silent closures, so the next person (or agent) can
  learn from it.
* **A parent can't be resolved while subtasks are open.** Goals finish only when their work finishes.
* **`waiting_info` / `on_hold` pause the SLA clock.** You're not penalized while waiting on
  someone else.
* **When the requester replies to a `waiting_info` ticket, it goes back to `open`.** This is the
  classic service-desk loop, and it is how a human answers an agent's question without touching
  any controls.
* **Reopening is counted** (`reopen_count`). Lots of reopens means the "fixes" aren't fixes.
* **Closed and cancelled tickets are read-only.** History stays trustworthy. Follow-ups get a new
  ticket with a link.
* **Resolved → closed automatically after N days** (default 7) if nobody objects.

## 3. Conversation: three kinds of text

| Kind | Audience | Use |
|---|---|---|
| **Comment** | Everyone, including the requester | Replies, questions, status updates |
| **Work note** (internal) | The people doing the work | Investigation notes, dead ends, internal debate |
| **Agent output** | Everyone | The raw transcript of an AI run, kept apart so it doesn't drown the human conversation |
| **Feedback** | Everyone | The requester's rating and comments |
| **System** | Everyone | Automatic events ("requester replied", "all subtasks done") |

On top of that there is the **audit history**: every field change records who changed it, from
what, to what, and when. The timeline in the GUI mixes all of these, with filters.

**@mentions** turn a comment into a message for someone: that person (or agent) gets a
notification and is added as a watcher. For agents a mention also puts the ticket at the top of
their work queue. That is how agents talk to each other.

## 4. Attachments

Screenshots, logs, generated files. Each one is stored on disk under
`~/.agentdesk/attachments/<ticket>/` with its size, MIME type, SHA-256 and uploader, and can be
tied to the comment it was posted with. You can paste a screenshot straight into the GUI. Agents
are given the *local file path* in their briefing so they can open it.

## 5. Relationships

* **Parent / child (subtasks):** a goal breaks down into tasks. Progress rolls up, and the parent's
  owner is notified when every child is done.
* **Links:** `blocks / blocked_by`, `relates_to`, `duplicates / duplicated_by`.
  A ticket that is *blocked by* an unfinished ticket is not handed to an agent, so dependencies
  are respected without anyone watching.

## 6. Ownership under concurrency: claims

Several AIs working from the same queue would trip over each other. A **claim** is an expiring
lease (default 30 minutes): while it is held, no other agent gets the ticket from `next`. It is
taken atomically (`BEGIN IMMEDIATE` in SQLite, so separate agent processes are safe) and released
when the ticket is resolved, put on hold, set waiting, or when the run ends. If an agent crashes,
the lease just runs out.

## 7. Notifications, watchers and the event stream

* **Watchers:** the requester, the assignee, anyone mentioned, and anyone who clicks Watch.
* **Notifications:** assigned, mentioned, commented, status changed, subtask added, all subtasks
  done, feedback received. Each actor has an inbox with unread/read state.
* **Events:** an append-only stream of everything that happened (`/api/events?since=N`,
  `agentdesk events --follow`). The GUI polls it for live updates, and other tools can use it to react.

## 8. SLA

Each priority has a resolution target (P1 4h, P2 24h, P3 72h, P4 1 week, P5 2 weeks). The
effective due time is pushed back by however long the ticket sat in a paused state. A
ticket is **breached** when it is still open past the effective due time, or was resolved after it.
The dashboard counts breaches.

## 9. Knowledge base

Resolved tickets answer one question once. Knowledge base articles answer it for good: project
documentation, decisions, runbooks. Articles are versioned, can be scoped to a project, and are
listed in agent briefings, so agents build up and reuse shared project memory.

## 10. Reporting

Counts by status, open work by priority, assignee and type, SLA breaches, how many tickets are
waiting on the requester, and average satisfaction. These show where work is stuck.

---

# Adapting it for AI agents

The service-desk model fits AI collaboration well: it gives you identity, ownership,
written handoffs, a place for questions, and an audit trail. AgentDesk adds:

1. **Agents are actors.** They have a name (for @mentions), capabilities (shown to other agents when
   they choose who to delegate to), an optional queue, and a **command** that runs the model.
2. **The briefing.** `build_context()` turns a ticket into a prompt containing the fields,
   description, project and workspace, parent goal, subtasks, links, attachment paths, the
   conversation so far, relevant knowledge base articles, the other agents, and instructions for reporting back.
3. **Two ways to report back:**
   * Tool-using agents (Claude Code, Codex, Gemini CLI...) use the CLI or MCP tools directly.
   * Plain-text models (ollama, llama.cpp) end their reply with an ` ```agentdesk ` JSON block
     (status, resolution, subtasks, comments, knowledge base articles). The runner applies it.
4. **The runner** loops over each enabled agent: `next_ticket` (mentions, then assigned, then queue,
   skipping blocked or claimed tickets) → claim → run the command in the project's workspace
   → post the output → apply directives → set the final status → release.
   Safety valves:
   * A failing command (non-zero exit) puts the ticket in `waiting_info` for a human.
   * A run with no status lands in `waiting_info`, which stops endless loops.
   * `continue` is capped at `max_runs` per ticket.
   * A goal with open subtasks parks in `on_hold` and wakes when they finish.
5. **Goals.** A human writes the outcome they want as a `goal` and assigns it to a planner agent.
   The planner breaks it into subtasks for the best-suited agents and links dependencies. The subtasks
   run, possibly in parallel across agents. When the last one finishes, the goal goes back
   to the planner to review, write a knowledge base summary and resolve. The human is the requester
   all the way through, so they get questions as `waiting_info` and answer by replying.
