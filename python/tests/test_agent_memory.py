"""Tests for Claude-style AgentMemoryStore, ContextHarness compaction, and planner integration."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from mcp_agent import companion_planner, daily_planner
from mcp_agent.agent_memory import AgentMemoryStore, capabilities_fingerprint
from mcp_agent.context_harness import ContextHarness, flush_memories_from_payload


class AgentMemoryStoreTests(unittest.TestCase):
    def test_write_read_search_delete_and_index_maintenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentMemoryStore(root=Path(tmp), world_id="world_alpha")
            res = store.memory_write(
                scope="global",
                name="Night Torch Timing",
                description="Craft a torch before dusk ends instead of starting distant mining.",
                content="When dusk has under 60s left, prioritize a torch before mining.",
                kind="lesson",
            )
            self.assertTrue(res["saved"])
            self.assertEqual(res["path"], "global/night_torch_timing.md")

            wres = store.memory_write(
                scope="world",
                name="camp_chests",
                description="Chest 1 holds building materials; icebox holds meat.",
                content="Store logs and boards in chest 1; keep meat in the icebox.",
                kind="convention",
            )
            self.assertEqual(wres["path"], "world/camp_chests.md")

            # MEMORY.md indexes are updated automatically
            prompt = store.index_prompt()
            self.assertIn("night_torch_timing", prompt)
            self.assertIn("camp_chests", prompt)

            # memory_read retrieves full content
            entry = store.memory_read("global/night_torch_timing.md")
            self.assertEqual(entry["kind"], "lesson")
            self.assertIn("prioritize a torch", entry["content"])

            # memory_search matches across scopes
            found = store.memory_search("icebox meat", scope="all")
            self.assertEqual(len(found["matches"]), 1)
            self.assertEqual(found["matches"][0]["path"], "world/camp_chests.md")

            # memory_delete removes file and updates MEMORY.md
            deleted = store.memory_delete("global/night_torch_timing.md")
            self.assertTrue(deleted["deleted"])
            self.assertNotIn("night_torch_timing", store.index_prompt())
            with self.assertRaises(ValueError):
                store.memory_read("global/night_torch_timing.md")

    def test_world_isolation_and_path_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_a = AgentMemoryStore(root=root, world_id="save_A")
            store_b = AgentMemoryStore(root=root, world_id="save_B")

            store_a.memory_write("world", "secret_camp", "Camp A note", "Alpha camp is north.", "convention")
            store_a.memory_write("global", "shared_tip", "Global tip", "Always check fuel.", "lesson")

            # World B sees global memory, NOT World A's memory
            self.assertEqual(store_b.memory_search("Alpha", scope="all")["total"], 0)
            self.assertEqual(store_b.memory_search("fuel", scope="global")["total"], 1)
            with self.assertRaises(ValueError):
                store_b.memory_read("world/secret_camp.md")

            # Path traversal and MEMORY.md direct access are rejected
            for bad in ("../global/shared_tip.md", "world/../global/shared_tip.md", "/etc/passwd", "world/MEMORY.md", "global/a/b.md"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    store_b.memory_read(bad)

            # Unbound store rejects world writes
            unbound = AgentMemoryStore(root=root, world_id=None)
            with self.assertRaises(ValueError):
                unbound.memory_write("world", "x", "y", "z")


class ContextHarnessTests(unittest.TestCase):
    def test_append_only_persistence_and_world_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = ContextHarness(role="player", root=root, world_id="save_1")
            ctx.append("turn1_user", "turn1_asst", prompt_tokens=1000)
            ctx.append("turn2_user", "turn2_asst", prompt_tokens=2000)
            self.assertEqual(len(ctx.messages_for_prompt()), 2)

            # Reloading the same world restores exact append-only turns
            reloaded = ContextHarness(role="player", root=root, world_id="save_1")
            self.assertEqual(reloaded.messages_for_prompt(), [("turn1_user", "turn1_asst"), ("turn2_user", "turn2_asst")])
            self.assertEqual(reloaded.last_prompt_tokens, 2000)

            # Switching worlds isolates contexts cleanly
            reloaded.bind_world("save_2")
            self.assertEqual(reloaded.messages_for_prompt(), [])
            reloaded.bind_world("save_1")
            self.assertEqual(len(reloaded.messages_for_prompt()), 2)

    def test_threshold_triggered_compaction_and_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ContextHarness(
                role="companion",
                root=Path(tmp),
                world_id="save_compact",
                max_context_tokens=1000,
                compact_ratio=0.75,
                keep_recent=2,
            )
            ctx.append("u1", "a1", prompt_tokens=300)
            ctx.append("u2", "a2", prompt_tokens=500)
            ctx.append("u3", "a3", prompt_tokens=740)
            self.assertFalse(ctx.should_compact())

            # Real prompt_tokens crosses 75% threshold (750)
            ctx.append("u4", "a4", prompt_tokens=780)
            self.assertTrue(ctx.should_compact())

            called = []
            def fake_summarizer(prev, older):
                called.append((prev, older))
                return "Summary of turns 1-2: built campfire and gathered grass."

            self.assertTrue(ctx.compact(summarizer=fake_summarizer))
            self.assertEqual(len(called), 1)
            self.assertEqual(len(called[0][1]), 2)  # u1, u2 compacted; u3, u4 kept verbatim
            msgs = ctx.messages_for_prompt()
            self.assertEqual(len(msgs), 3)
            self.assertIn("Summary of turns 1-2", msgs[0][1])
            self.assertEqual(msgs[1], ("u3", "a3"))
            self.assertEqual(msgs[2], ("u4", "a4"))
            self.assertEqual(ctx.compaction_count, 1)
            self.assertIsNone(ctx.last_prompt_tokens)

            # Persisted summary survives reload
            reloaded = ContextHarness(role="companion", root=Path(tmp), world_id="save_compact", keep_recent=2)
            self.assertEqual(reloaded.messages_for_prompt(), msgs)

            # Fallback summary works when summarizer raises
            reloaded.append("u5", "a5", prompt_tokens=900)
            def broken(_prev, _older):
                raise RuntimeError("offline")
            self.assertTrue(reloaded.compact(summarizer=broken))
            self.assertIn("Prior decision: a3", reloaded.compacted_summary)


class PlannerMemoryIntegrationTests(unittest.TestCase):
    def test_player_planner_registers_memory_tools_and_preserves_append_only_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentMemoryStore(root=Path(tmp), world_id="world_p")
            store.memory_write("global", "torch_first", "Make torch before dark", "Craft a torch early.", "lesson")
            captured = {}

            def fake_request(api_url, api_key, body, validate, *, functions=None, on_usage=None, **kwargs):
                captured["body"] = body
                captured["functions"] = functions
                # Simulate model actively writing a new world memory during planning
                functions["memory_write"](
                    scope="world", name="base_loc", description="Camp near portal", content="Camp is east of portal.", kind="convention"
                )
                if on_usage:
                    on_usage({"prompt_tokens": 1234})
                return {"kind": "agent_reply", "outcome": "reply", "reply": "好的。"}

            usage_seen = []
            ctx = {"interaction": {"mode": "instruction", "enabled": True, "instruction": "hi", "messages": []}}
            history = [(f"u{i}", f"a{i}") for i in range(5)]  # > old 3-turn limit
            with patch("mcp_agent.daily_planner.request_json_plan", fake_request):
                daily_planner.request_plan(
                    "https://offline.invalid", "fake", "fake", 1, ctx, history,
                    memory_store=store, on_usage=usage_seen.append, preserve_history=True,
                )

            self.assertIn("torch_first", captured["body"]["messages"][0]["content"])
            # All 5 prior turns preserved (system + 5*2 + current user = 12)
            self.assertEqual(len(captured["body"]["messages"]), 12)
            tool_names = {t["function"]["name"] for t in captured["body"]["tools"]}
            self.assertTrue({"memory_search", "memory_read", "memory_write", "memory_delete"} <= tool_names)
            self.assertEqual(usage_seen, [{"prompt_tokens": 1234}])
            self.assertEqual(store.memory_read("world/base_loc.md")["name"], "base_loc")

    def test_companion_planner_registers_memory_tools_without_buddy_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentMemoryStore(root=Path(tmp), world_id="world_c")
            store.memory_write("global", "split_work", "Split gathering", "Assign grass and twigs to different buddies.", "playbook")
            captured = {}

            def fake_request(api_url, api_key, body, validate, *, functions=None, on_usage=None, **kwargs):
                captured["body"] = body
                captured["functions"] = functions
                if on_usage:
                    on_usage({"prompt_tokens": 2222})
                return {"kind": "team_plan", "assignments": [], "outcome": "working"}

            usage_seen = []
            planner = companion_planner.CompanionPlanner(
                "https://offline.invalid", "fake", "fake",
                memory_store=store, on_usage=usage_seen.append, preserve_history=True,
            )
            ctx = {
                "interaction": {"mode": "instruction", "enabled": True, "revision": 1},
                "members": {
                    "buddy1": {
                        "name": "一号",
                        "interaction": {"mode": "instruction", "enabled": True, "revision": 1},
                        "observation": {"capabilities": ["collect"], "world": {"day": 1, "phase": "day"}},
                    },
                },
                "request_ids": ["buddy1"],
            }
            history = [(f"u{i}", f"a{i}") for i in range(4)]
            with patch("mcp_agent.companion_planner.request_json_plan", fake_request):
                planner(ctx, history, threading.Event())

            self.assertIn("split_work", captured["body"]["messages"][0]["content"])
            self.assertEqual(len(captured["body"]["messages"]), 10)  # system + 4*2 + user
            mem_tools = [t for t in captured["body"]["tools"] if t["function"]["name"].startswith("memory_")]
            self.assertEqual(len(mem_tools), 4)
            for t in mem_tools:
                self.assertNotIn("buddy_id", t["function"]["parameters"].get("required", []))
            self.assertEqual(usage_seen, [{"prompt_tokens": 2222}])
            sys_prompt = captured["body"]["messages"][0]["content"]
            self.assertLess(sys_prompt.index("split_work"), sys_prompt.index("Actor profile:"))

    def test_pre_compaction_memory_flush_writes_durable_entries_and_blocks_inferred_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentMemoryStore(root=Path(tmp), world_id="world_flush")
            saved = flush_memories_from_payload(store, [
                {
                    "scope": "world",
                    "name": "camp_rules",
                    "description": "Firepit is home; keep berries in icebox.",
                    "kind": "convention",
                    "source": "inferred",
                    "content": "Home anchor is the main firepit. Store perishable berries in the icebox.",
                },
                {
                    "scope": "global",
                    "name": "always_reply_cn",
                    "description": "Owner wants short Chinese replies.",
                    "kind": "preference",
                    "source": "user",
                    "content": "Reply in concise Chinese.",
                },
            ])
            self.assertEqual(saved, ["world/camp_rules.md", "global/always_reply_cn.md"])

            # Inferred global entries are blocked during compaction flush (prevents Consolidation Amplification)
            blocked = flush_memories_from_payload(store, [
                {
                    "scope": "global",
                    "name": "inferred_ban",
                    "description": "Inferred global rule from failures.",
                    "kind": "lesson",
                    "source": "inferred",
                    "content": "Do not try chopping at night.",
                },
            ])
            self.assertEqual(blocked, [])
            self.assertIn("camp_rules", store.index_prompt())
            self.assertIn("always_reply_cn", store.index_prompt())

    def test_anti_poisoning_runtime_limit_rejection_and_stale_capabilities_tagging(self):
        with tempfile.TemporaryDirectory() as tmp:
            v1 = capabilities_fingerprint(["inventory", "deposit"])
            v2 = capabilities_fingerprint(["inventory", "deposit", "equip", "unequip"])
            store = AgentMemoryStore(root=Path(tmp), world_id="world_guard", capabilities_hash=v1)

            # Inferred runtime/capability-limit notes are rejected at write time
            with self.assertRaisesRegex(ValueError, "Do not store runtime capability"):
                store.memory_write(
                    scope="global",
                    name="no_equip_action",
                    description="equip target unsupported in target_types",
                    content="Multiple equip attempts failed with focused_task_unavailable.",
                    kind="lesson",
                    source="inferred",
                )

            # Valid gameplay lesson & explicit user preference succeed
            store.memory_write(
                scope="global", name="drop_search", description="Dont chase distant drops",
                content="Skip distant loose drops before nightfall.", kind="playbook", source="inferred",
            )
            store.memory_write(
                scope="world", name="night_torch", description="Always equip torch at night",
                content="Player requires equipping torch before night.", kind="preference", source="user",
            )

            # On capability upgrade (v1 -> v2), inferred lesson is flagged stale; user preference stays fresh
            store.set_capabilities_hash(v2)
            self.assertTrue(store.memory_read("global/drop_search.md")["stale"])
            self.assertFalse(store.memory_read("world/night_torch.md")["stale"])
            prompt = store.index_prompt()
            self.assertIn("[stale-capabilities: verify live or delete]", prompt)
            self.assertIn('<agent_memory_notes priority="low"', prompt)


if __name__ == "__main__":
    unittest.main()
