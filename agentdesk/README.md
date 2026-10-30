# AgentDesk

A ticket system in the style of ServiceNow / ServiceDesk that runs entirely on your machine. It is built
so that **you and your local AI models (Claude Code, Codex, Gemini CLI, ollama...) work from the same
queue**. They can pick up work, ask each other and you questions, attach files, split goals into
subtasks and document the project.

* **Pure Python standard library.** Nothing to install, Python 3.10+, data stored in SQLite.
* **Four ways in, one set of rules:** a browser GUI, a CLI, an MCP server and a JSON HTTP API.
* **Agent runner** hands tickets to locally installed model CLIs, with a full briefing.

See [docs/DESIGN.md](docs/DESIGN.md) for the theory: lifecycle, SLA, comments vs. work notes,
claims, links, notifications, and how it is adapted for AI agents.

## Quick start

```bash
cd agentdesk
python -m agentdesk serve            # open http://127.0.0.1:8765
```

Optional: `pip install -e .` gives you an `agentdesk` command. The examples below use
`python -m agentdesk`. Data lives in `~/.agentdesk` (override with `AGENTDESK_HOME` or `--home`).

### Try it in a minute, without a real model

```bash
python -m agentdesk project add WEB "Website" -w ~/code/website
python -m agentdesk agent add mock --command "python examples/mock_agent.py" --capabilities planning
python -m agentdesk goal "Launch pricing page" --project WEB --assign mock
python -m agentdesk run --once mock     # plans: creates 2 subtasks, goal goes on_hold
python -m agentdesk run --once mock     # works a subtask
python -m agentdesk list -s all
```

## Set up real local agents

```bash
python -m agentdesk agent add claude --preset claude --capabilities "planning, architecture, Python"
python -m agentdesk agent add codex  --preset codex  --capabilities "coding, tests, refactors"
python -m agentdesk agent add llama  --command "ollama run llama3.1" --capabilities "writing, summaries"
python -m agentdesk serve --runner      # GUI + every enabled agent working its queue
```

* The `command` gets the briefing on **stdin**. Use `{prompt}` (passed as an argument) or
  `{prompt_file}` (a path to a temp file) for tools that want it another way.
* Runs happen in the project's **workspace folder** with `AGENTDESK_ACTOR`, `AGENTDESK_HOME` and
  `AGENTDESK_TICKET` set, so the CLI inside the agent already knows who it is.
* Presets are starting points. Check them against your installed versions, especially the
  permission flags. For Claude Code, the preset allows the `agentdesk` CLI plus file edits.

### Or connect agents over MCP

```bash
claude mcp add agentdesk -- python -m agentdesk mcp --actor claude
```

```toml
# ~/.codex/config.toml
[mcp_servers.agentdesk]
command = "python"
args = ["-m", "agentdesk", "mcp", "--actor", "codex"]
```

For `python -m` to work there, run from this folder, `pip install -e .`, or set `PYTHONPATH`.
MCP tools: `whoami`, `next_ticket`, `list_tickets`, `get_ticket`, `get_context`, `create_ticket`,
`update_ticket`, `set_status`, `add_comment`, `attach_file`, `link_tickets`, `claim_ticket`,
`release_ticket`, `ask`, `inbox`, `mark_read`, `kb_search`, `kb_get`, `kb_put`, `list_actors`,
`list_projects`, `stats`.

## How the pieces fit

```
 Browser GUI ──HTTP──┐
 curl / scripts ─────┤   server.py ─┐
                     │              │
 Claude/Codex ──MCP──┼─ mcp.py ─────┼──► store.py ──► SQLite (WAL) + attachments/
 any shell agent ────┼─ cli.py ─────┤      (all the rules)
                     │              │
 runner.py ──────────┘  builds briefings, runs model CLIs, applies results
```

| File | What it does |
|---|---|
| `agentdesk/store.py` | Data model and every rule: state machine, priority matrix, SLA, comments, mentions, attachments, links, claims, notifications, events, knowledge base, stats |
| `agentdesk/server.py` | REST API + static GUI, `127.0.0.1` only by default |
| `agentdesk/static/index.html` | Single-page GUI: dashboard, ticket list, kanban board, goals, inbox, ticket detail (timeline, composer with paste-to-attach screenshots, status actions, feedback), knowledge base, agents, projects |
| `agentdesk/cli.py` | `agentdesk ...` commands, all with `--json` |
| `agentdesk/mcp.py` | stdio MCP server |
| `agentdesk/runner.py` | Agent dispatch loop + briefing builder + ` ```agentdesk ` directive parser |

## The goal workflow

1. You create a **goal** in the GUI (or `agentdesk goal "..."`) and assign it to a planner agent.
2. The planner reads the briefing, writes a plan, creates **subtasks** for the agents best suited
   to them, and links dependencies (`TASK-3 blocks TASK-4`). The goal parks in `on_hold`.
3. Each agent's runner picks up its subtasks, skipping blocked ones, and claims each one so no
   other agent touches it. It comments, attaches outputs, @mentions colleagues and resolves with notes.
4. If an agent needs you, the ticket goes to **waiting_info** with a question. It shows on your
   dashboard under "Needs your answer". Your reply sends it back to the agent.
5. When every subtask is done, the goal returns to the planner. The planner reviews, writes a
   knowledge base summary and resolves. You rate it, or reopen it if it isn't right.

## CLI cheat sheet

```bash
agentdesk whoami
agentdesk create "Fix login 500" -t bug --impact 1 --urgency 1 --project WEB --assign codex
agentdesk list -s open -a me              # -s new|open|in_progress|waiting_info|...|done|all
agentdesk show BUG-7
agentdesk next                            # claim the most important ticket for you
agentdesk comment BUG-7 "@claude can you review?"
agentdesk comment BUG-7 --internal -f notes.md
agentdesk attach BUG-7 screenshot.png log.txt
agentdesk status BUG-7 waiting_info -m "Which browser?"
agentdesk resolve BUG-7 -m "Null check added in auth.py" --code fixed
agentdesk feedback BUG-7 5 "works"
agentdesk link TASK-3 blocks TASK-4
agentdesk ask codex "Which test runner do we use?"
agentdesk kb put deploy "How to deploy" -f DEPLOY.md --project WEB
agentdesk inbox ; agentdesk events --follow ; agentdesk stats
```

Global flags (`--as NAME`, `--json`, `--home DIR`) can go anywhere on the line.

## Security notes

The server has **no authentication**. Identity is just the `X-Actor` header, which is fine on your
own machine. It binds to `127.0.0.1`, so don't expose it with `--host 0.0.0.0` on a network you
don't trust. Agents run with whatever permissions their command line gives them, in the project
workspace, so be deliberate with flags like `--full-auto` and `--yolo`.

## Tests

```bash
python -m unittest discover -s tests -v
```
