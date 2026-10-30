"""A fake AI agent for trying AgentDesk without a real model.

    python -m agentdesk agent add mock --command "python examples/mock_agent.py"

It reads the briefing on stdin. Goals are split into two subtasks; everything
else is "resolved" with a short summary, using the ```agentdesk block protocol
that plain-text models use.
"""

import json
import os
import re
import sys

prompt = sys.stdin.read()
me = os.environ.get("AGENTDESK_ACTOR", "mock")
m = re.search(r"^## Ticket (\S+): (.*)$", prompt, re.M)
key, title = (m.group(1), m.group(2)) if m else ("?", "?")

print(f"I am {me}. I read the briefing for {key} ({len(prompt)} chars).")
if key.startswith("GOAL") and "### Subtasks" not in prompt:
    print("This is a goal; here is my plan: research first, then implement.")
    block = {"comments": [f"Plan for {key}: 1) research 2) implement."],
             "subtasks": [{"title": f"Research: {title}", "assignee": me, "description": "Collect requirements."},
                          {"title": f"Implement: {title}", "assignee": me, "description": "Build it."}]}
elif key.startswith("GOAL"):
    block = {"status": "resolved", "resolution": "All subtasks finished; goal achieved.", "code": "done",
             "kb": [{"slug": f"{key.lower()}-summary", "title": f"Summary of {title}", "body": "What we did."}]}
else:
    block = {"status": "resolved", "resolution": f"Completed '{title}'.", "code": "done"}
print("```agentdesk\n" + json.dumps(block) + "\n```")
