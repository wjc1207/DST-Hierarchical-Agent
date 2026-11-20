"""Offline tests for event-driven external agent backends (Claude, Codex, DSH CLI/HTTP)."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from mcp_agent.external_backends import ExternalAgentBackend, extract_json_object


def sample_companion_ctx():
    return {
        "interaction": {"mode": "instruction", "enabled": True, "revision": 1},
        "members": {
            "buddy1": {
                "name": "一号",
                "interaction": {"mode": "instruction", "enabled": True, "revision": 1, "instruction": "采两根草", "messages": []},
                "observation": {"capabilities": ["collect", "follow", "stay"], "world": {"day": 1, "phase": "day"}, "buddy": {"inventory": {}}},
            },
        },
        "request_ids": ["buddy1"],
    }


class ExternalBackendTests(unittest.TestCase):
    def test_extract_json_object_handles_fences_and_prose(self):
        self.assertEqual(extract_json_object('{"a": 1}'), {"a": 1})
        self.assertEqual(extract_json_object('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(extract_json_object('Sure, here is the plan:\n{"a": 3}\nDone.'), {"a": 3})
        with self.assertRaises(ValueError):
            extract_json_object("no json here")

    def test_claude_backend_persists_session_and_resumes_per_world(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = ExternalAgentBackend("claude", role="companion", command="claude", model="sonnet", memory_root=Path(tmp))
            cmds = []
            plan_json = json.dumps({"assignments": {"buddy1": {"reply": "我去采草。", "tasks": [{"type": "collect", "prefab": "cutgrass", "count": 2}]}}}, ensure_ascii=False)

            def fake_run(cmd, *, input_text=None, cancel=None, timeout=120, cwd=None):
                cmds.append((cmd, input_text))
                return 0, json.dumps({"session_id": "claude-sid-1", "result": f"```json\n{plan_json}\n```"}), ""

            ctx = sample_companion_ctx()
            with patch("mcp_agent.external_backends._run_cancellable", fake_run):
                res1 = backend.plan_companion(ctx, [], threading.Event(), world_id="world_1")
                res2 = backend.plan_companion(ctx, [], threading.Event(), world_id="world_1")

            self.assertEqual(res1["kind"], "team_plan")
            self.assertEqual(res2["kind"], "team_plan")
            self.assertNotIn("--resume", cmds[0][0])
            self.assertIn("--resume", cmds[1][0])
            self.assertIn("claude-sid-1", cmds[1][0])
            self.assertIn("--model", cmds[0][0])
            self.assertIn("采两根草", cmds[0][1])

    def test_codex_backend_parses_jsonl_and_resumes_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = ExternalAgentBackend("codex", role="companion", command="codex", memory_root=Path(tmp))
            cmds = []
            plan_json = json.dumps({"assignments": {"buddy1": {"reply": "收到。", "outcome": "reply"}}}, ensure_ascii=False)
            jsonl_out = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "codex-thread-9"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": plan_json}}),
            ])

            def fake_run(cmd, *, input_text=None, cancel=None, timeout=120, cwd=None):
                cmds.append((cmd, input_text))
                return 0, jsonl_out, ""

            ctx = sample_companion_ctx()
            with patch("mcp_agent.external_backends._run_cancellable", fake_run):
                res1 = backend.plan_companion(ctx, [], threading.Event(), world_id="world_2")
                res2 = backend.plan_companion(ctx, [], threading.Event(), world_id="world_2")

            self.assertEqual(res1["assignments"][0]["plan"]["outcome"], "reply")
            self.assertEqual(res2["assignments"][0]["plan"]["outcome"], "reply")
            self.assertEqual(cmds[0][0][1:3], ["exec", "--json"])
            self.assertEqual(cmds[1][0][1:5], ["exec", "resume", "--json", "codex-thread-9"])
            self.assertEqual(cmds[0][0][-1], "-")
            self.assertIn("采两根草", cmds[0][1])

    def test_dsh_cli_and_dsh_http_backends(self):
        with tempfile.TemporaryDirectory() as tmp:
            dsh_cli = ExternalAgentBackend("dsh", role="companion", command="dsh-tui", memory_root=Path(tmp))
            plan_json = json.dumps({"assignments": {"buddy1": {"reply": "我去采草。", "tasks": [{"type": "collect", "prefab": "cutgrass", "count": 1}]}}}, ensure_ascii=False)
            cmds = []

            def fake_run(cmd, *, input_text=None, cancel=None, timeout=120, cwd=None):
                cmds.append((cmd, input_text))
                return 0, json.dumps({"schemaVersion": 1, "sessionId": "dsh-s1", "text": plan_json}), ""

            ctx = sample_companion_ctx()
            with patch("mcp_agent.external_backends._run_cancellable", fake_run):
                res = dsh_cli.plan_companion(ctx, [], threading.Event(), world_id="world_3")
                dsh_cli.plan_companion(ctx, [], threading.Event(), world_id="world_3")
            self.assertEqual(res["kind"], "team_plan")
            self.assertEqual(cmds[0][0][1:4], ["run", "--output-format", "json"])
            self.assertIn("--session-id", cmds[1][0])
            self.assertIn("dsh-s1", cmds[1][0])
            self.assertIn("采两根草", cmds[0][1])

            # Test dsh-http backend
            dsh_http = ExternalAgentBackend("dsh-http", role="player", dsh_url="http://127.0.0.1:57787/mcp/dsh/api", memory_root=Path(tmp))
            seen_req = []

            class FakeResp:
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def read(self):
                    return json.dumps({
                        "session_id": "dsh-http-7",
                        "plan": {"kind": "agent_reply", "outcome": "reply", "reply": "收到。"},
                    }).encode("utf-8")

            def fake_urlopen(req, timeout=120):
                seen_req.append(json.loads(req.data.decode("utf-8")))
                return FakeResp()

            player_ctx = {"interaction": {"mode": "instruction", "enabled": True, "instruction": "hi", "messages": []}}
            with patch("urllib.request.urlopen", fake_urlopen):
                reply = dsh_http.plan_player(1, player_ctx, [], threading.Event(), world_id="world_3")
            self.assertEqual(reply["outcome"], "reply")
            self.assertEqual(seen_req[0]["world_id"], "world_3")
            self.assertEqual(dsh_http.sessions.get("world_3", "dsh-http", "player"), "dsh-http-7")


if __name__ == "__main__":
    unittest.main()
