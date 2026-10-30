import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentdesk import cli, mcp, runner  # noqa: E402
from agentdesk.server import make_server  # noqa: E402
from agentdesk.store import DeskError, Store  # noqa: E402

MOCK = f'"{sys.executable}" "{ROOT / "examples" / "mock_agent.py"}"'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(self.tmp.name)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()


class TestLifecycle(Base):
    def test_create_defaults_and_priority_matrix(self):
        t = self.s.create_ticket("Printer on fire", "alice", type="incident", impact=1, urgency=1)
        self.assertEqual(t["key"], "INC-1")
        self.assertEqual(t["status"], "new")
        self.assertEqual(t["priority"], 1)
        self.assertEqual(t["requester"], "alice")
        self.assertFalse(t["sla"]["breached"])

    def test_transitions_enforced(self):
        t = self.s.create_ticket("x", "alice")
        with self.assertRaises(DeskError):
            self.s.set_status(t["key"], "closed", "bob")
        with self.assertRaises(DeskError):  # resolution note required
            self.s.set_status(t["key"], "resolved", "bob")
        self.s.set_status(t["key"], "resolved", "bob", note="fixed it", resolution_code="fixed")
        t = self.s.set_status(t["key"], "closed", "bob")
        self.assertEqual(t["status"], "closed")
        with self.assertRaises(DeskError):
            self.s.add_comment(t["key"], "late", "alice")
        t = self.s.set_status(t["key"], "open", "alice", force=True)
        self.assertEqual(t["reopen_count"], 1)
        self.assertIsNone(t["resolution_notes"])

    def test_waiting_info_pauses_sla_and_requester_reply_reopens(self):
        t = self.s.create_ticket("need help", "alice", assignee="bob")
        t = self.s.set_status(t["key"], "waiting_info", "bob", note="which version?")
        self.assertTrue(t["sla"]["paused"])
        self.s.add_comment(t["key"], "a reply from someone else", "carol")
        self.assertEqual(self.s.get_ticket(t["key"])["status"], "waiting_info")
        self.s.add_comment(t["key"], "v2.1", "alice")
        t = self.s.get_ticket(t["key"])
        self.assertEqual(t["status"], "open")
        self.assertFalse(t["sla"]["paused"])

    def test_mentions_notify_and_work_notes(self):
        self.s.upsert_actor("codex", "agent")
        t = self.s.create_ticket("x", "alice")
        self.s.add_comment(t["key"], "@codex please look, @nobody", "claude", kind="work_note")
        inbox = self.s.inbox("codex")
        self.assertEqual(inbox[0]["kind"], "mention")
        self.assertIn("codex", self.s.get_ticket(t["key"], full=True)["watchers"])
        self.assertEqual(self.s.mark_read("codex"), 1)

    def test_parent_cannot_resolve_with_open_children(self):
        g = self.s.create_ticket("goal", "alice", type="goal", assignee="planner")
        c = self.s.create_ticket("sub", "planner", parent=g["key"])
        with self.assertRaises(DeskError):
            self.s.set_status(g["key"], "resolved", "planner", note="done")
        self.s.set_status(c["key"], "resolved", "planner", note="done")
        self.assertTrue(any(n["kind"] == "children_done" for n in self.s.inbox("planner")))
        self.s.set_status(g["key"], "resolved", "planner", note="done")

    def test_claims_are_exclusive(self):
        t = self.s.create_ticket("x", "alice")
        t = self.s.claim(t["key"], "claude")
        self.assertEqual(t["status"], "in_progress")
        self.assertEqual(t["assignee"], "claude")
        with self.assertRaises(DeskError):
            self.s.claim(t["key"], "codex")
        self.s.release(t["key"], "claude")
        self.s.claim(t["key"], "codex")

    def test_next_ticket_order_and_dependencies(self):
        self.s.upsert_actor("codex", "agent", queue="backend")
        low = self.s.create_ticket("low", "alice", assignee="codex", priority=4)
        high = self.s.create_ticket("high", "alice", assignee="codex", priority=1)
        blocker = self.s.create_ticket("blocker", "alice", assignee="bob", priority=3)
        self.s.link(blocker["key"], "blocks", high["key"], "alice")
        self.assertEqual(self.s.next_ticket("codex", claim=False)["key"], low["key"])
        self.s.set_status(blocker["key"], "resolved", "bob", note="ok")
        self.assertEqual(self.s.next_ticket("codex", claim=False)["key"], high["key"])
        q = self.s.create_ticket("queued", "alice", queue="backend", priority=1)
        self.s.set_status(high["key"], "on_hold", "alice")
        self.s.set_status(low["key"], "on_hold", "alice")
        nxt = self.s.next_ticket("codex")
        self.assertEqual((nxt["key"], nxt["reason"]), (q["key"], "queue"))
        self.assertEqual(nxt["claimed_by"], "codex")

    def test_attachments_and_feedback(self):
        t = self.s.create_ticket("x", "alice")
        a = self.s.add_attachment(t["key"], "../../evil name.png", b"\x89PNG", "alice")
        self.assertEqual(a["filename"], "evil name.png")
        self.assertTrue(Path(a["path"]).is_file())
        self.assertTrue(Path(a["path"]).resolve().is_relative_to(Path(self.tmp.name).resolve()))
        self.s.set_status(t["key"], "resolved", "bob", note="done")
        t = self.s.add_feedback(t["key"], 2, "still broken", "alice", reopen=True)
        self.assertEqual((t["rating"], t["status"]), (2, "open"))

    def test_kb_versions_and_search(self):
        self.s.kb_put("Deploy Guide", "Deploy", "run make", "claude")
        k = self.s.kb_put("deploy-guide", "Deploy", "run make deploy", "codex")
        self.assertEqual((k["slug"], k["version"], k["author"]), ("deploy-guide", 2, "codex"))
        self.assertEqual(len(self.s.kb_list("make deploy")), 1)

    def test_ask_creates_question_ticket(self):
        self.s.upsert_actor("codex", "agent")
        r = self.s.ask("codex", "What test framework do we use?", "claude")
        self.assertEqual(r["ticket"]["type"], "question")
        self.assertEqual(r["ticket"]["assignee"], "codex")


class TestRunner(Base):
    def test_goal_breakdown_end_to_end(self):
        self.s.upsert_actor("mock", "agent", command=MOCK)
        g = self.s.create_ticket("Ship website", "alice", type="goal", assignee="mock")
        r = runner.run_once(self.s, "mock")
        self.assertEqual(r["ticket"], g["key"])
        g = self.s.get_ticket(g["key"], full=True)
        self.assertEqual(len(g["children"]), 2)
        self.assertEqual(g["status"], "on_hold")
        self.assertTrue(any(c["kind"] == "agent_output" for c in g["comments"]))
        for _ in range(2):
            self.assertEqual(runner.run_once(self.s, "mock")["status"], "resolved")
        runner.wake_parents(self.s)
        self.assertEqual(self.s.get_ticket(g["key"])["status"], "open")
        self.assertEqual(runner.run_once(self.s, "mock")["status"], "resolved")
        self.assertIsNone(runner.run_once(self.s, "mock"))
        self.assertEqual(len(self.s.kb_list()), 1)

    def test_failing_agent_goes_to_waiting_info(self):
        self.s.upsert_actor("bad", "agent", command=f'"{sys.executable}" -c "import sys; sys.exit(3)"')
        t = self.s.create_ticket("x", "alice", assignee="bad")
        r = runner.run_once(self.s, "bad")
        self.assertEqual((r["exit_code"], r["status"]), (3, "waiting_info"))
        self.assertIsNone(self.s.get_ticket(t["key"])["claimed_by"])

    def test_context_contains_instructions(self):
        t = self.s.create_ticket("Fix login", "alice", description="It 500s")
        ctx = runner.build_context(self.s, t["key"], "claude")
        self.assertIn("Fix login", ctx)
        self.assertIn("```agentdesk", ctx)

    def test_parse_directives(self):
        self.assertEqual(runner.parse_directives("blah\nSTATUS: resolved\n")["status"], "resolved")
        d = runner.parse_directives('x\n```agentdesk\n{"status":"waiting_info","resolution":"?"}\n```')
        self.assertEqual(d["status"], "waiting_info")


class TestInterfaces(Base):
    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["--home", self.tmp.name, "--json", *args])
        return code, buf.getvalue()

    def test_cli_roundtrip(self):
        code, out = self.run_cli("create", "From CLI", "-t", "bug", "--as", "claude")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["key"], "BUG-1")
        self.run_cli("comment", "BUG-1", "hello", "--internal", "--as", "claude")
        code, out = self.run_cli("show", "BUG-1")
        self.assertEqual(json.loads(out)["comments"][0]["kind"], "work_note")
        code, out = self.run_cli("status", "BUG-1", "closed")
        self.assertEqual(code, 1)

    def test_mcp_tools(self):
        tools = mcp.DeskTools(self.s, "claude")
        init = mcp.handle(tools, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        listed = mcp.handle(tools, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIn("next_ticket", [t["name"] for t in listed["result"]["tools"]])
        res = mcp.handle(tools, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "create_ticket", "arguments": {"title": "via mcp", "type": "goal"}}})
        self.assertEqual(json.loads(res["result"]["content"][0]["text"])["key"], "GOAL-1")
        bad = mcp.handle(tools, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                 "params": {"name": "set_status", "arguments": {"ref": "GOAL-1", "status": "closed"}}})
        self.assertTrue(bad["result"]["isError"])
        self.assertIsNone(mcp.handle(tools, {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_http_api(self):
        httpd = make_server(self.s, "127.0.0.1", 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def call(path, body=None, method=None, raw=None):
                data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                             headers={"X-Actor": "alice", "Content-Type": "application/json"})
                with urllib.request.urlopen(req) as r:
                    return r.read()
            t = json.loads(call("/api/tickets", {"title": "web", "type": "request"}))
            self.assertEqual(t["requester"], "alice")
            call(f"/api/tickets/{t['key']}/attachments?filename=shot.png", raw=b"\x89PNG data")
            full = json.loads(call(f"/api/tickets/{t['key']}"))
            self.assertEqual(full["attachments"][0]["filename"], "shot.png")
            self.assertEqual(call(f"/api/attachments/{full['attachments'][0]['id']}"), b"\x89PNG data")
            json.loads(call(f"/api/tickets/{t['key']}", {"priority": 1}, method="PATCH"))
            self.assertIn(b"AgentDesk", call("/"))
            with self.assertRaises(urllib.error.HTTPError) as e:
                call(f"/api/tickets/{t['key']}/status", {"status": "closed"})
            self.assertEqual(e.exception.code, 400)
            self.assertGreater(json.loads(call("/api/events?since=0"))["last"], 0)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
