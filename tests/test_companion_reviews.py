"""Replay completed collection/deposit milestones and pause/reconnect diagnostics."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_companion import ImmediateWorker
from test_companion_storage import TransferPeer,observation
from mcp_agent.companion_agent import CompanionAgent,CycleDiagnostics
from mcp_agent.companion_planner import validate_plan
from mcp_agent.companion_tasks import CompanionTasks,validate_board


def board():
    return validate_board({"goal":"Collect three grass and deposit it","reply":"我把草存好。","tasks":[
        {"id":"gather","title":"Collect","target":{"type":"inventory","prefab":"cutgrass","count":3}},
        {"id":"deposit","title":"Deposit","depends_on":["gather"],
         "target":{"type":"deposit","container_guid":300001,"prefab":"cutgrass","count":3}}]},1)


def completed_runtime():
    observed=observation();runtime=CompanionTasks();runtime.adopt(board(),observed)
    _,op,args=runtime.next_action(observed);assert op=="deposit"
    observed["buddy"]["inventory"]={};observed["version"]+=1
    observed["transfer_receipts"]=[{"task_key":args["task_key"],"count":3}]
    assert runtime.next_action(observed)[1]=="completed"
    return runtime,observed,args


class ReviewTests(unittest.TestCase):
    def test_completed_deposit_review_does_not_reactivate_consumed_stock_or_reset_receipt(self):
        runtime,observed,args=completed_runtime()
        runtime.adopt(board(),observed)
        self.assertEqual(runtime.next_action(observed)[1],"completed")
        self.assertEqual(runtime.goals.records["deposit"]["transfer_key"],args["task_key"])
        self.assertTrue(runtime.goals.records["gather"]["done"])
        gather=next(t for t in runtime.view()["tasks"] if t["id"]=="gather")
        self.assertEqual(gather["progress"]["current"],0)

    def test_new_task_id_or_increased_stock_target_can_start_genuinely_new_work(self):
        for change in ("id","count"):
            runtime,observed,_=completed_runtime();fresh=board();fresh["tasks"]=fresh["tasks"][:1]
            if change=="id":fresh["tasks"][0]["id"]="new_collection"
            else:fresh["tasks"][0]["target"]["count"]=4
            runtime.adopt(fresh,observed)
            self.assertEqual(runtime.next_action(observed)[1],"collect")

    def test_completed_storage_history_is_valid_after_leaving_chest_but_new_work_is_not(self):
        runtime,observed,_=completed_runtime();observed["containers"]=[];observed["version"]+=1
        ctx={"observation":observed,"current_task_board":runtime.view(observed)}
        clean=validate_plan(board(),ctx)
        runtime.adopt(clean,observed)
        self.assertEqual(runtime.next_action(observed)[1],"completed")
        for change in ("id","count","guid","unfinished","incoming_status_only"):
            raw=board();context=copy.deepcopy(ctx)
            if change=="id":raw["tasks"][1]["id"]="another_deposit"
            elif change=="count":raw["tasks"][1]["target"]["count"]=4
            elif change=="guid":raw["tasks"][1]["target"]["container_guid"]=999
            elif change=="unfinished":
                next(t for t in context["current_task_board"]["tasks"] if t["id"]=="deposit")["status"]="ready"
            else:context.pop("current_task_board");raw["tasks"][1]["status"]="done"
            with self.assertRaisesRegex(ValueError,"observed chest"):validate_plan(raw,context)

    def test_validator_does_not_revive_omitted_history_and_fill_new_board_slots(self):
        runtime,observed,_=completed_runtime();observed["containers"]=[];observed["version"]+=1
        ctx={"observation":observed,"current_task_board":runtime.view(observed)}
        fresh={"goal":"Six new tasks","reply":"我继续下一步。","tasks":[
            {"id":f"new{i}","title":"Collect","target":{"type":"inventory","prefab":"cutgrass","count":i+1}}
            for i in range(6)]}
        self.assertEqual(len(validate_plan(fresh,ctx)["tasks"]),6)


class ReviewAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_completion_verification_retaining_old_board_cannot_trigger_a_second_collection_trip(self):
        peer=TransferPeer();contexts=[]
        def planner(context,*_):
            contexts.append(context)
            if len(contexts)<=2:return board()
            return {"kind":"agent_reply","outcome":"completed","reply":"我已存好三根草。","evidence":"存箱回执累计三根草。"}
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor",ImmediateWorker):
            agent=CompanionAgent(peer,planner,execute=True)
        try:
            for _ in range(24):await agent.cycle()
            ops=[args["op"] for name,args in peer.calls if name=="buddy_action"]
            self.assertEqual(ops,["deposit"]*3)
            self.assertEqual(len(contexts),3)
            self.assertEqual(peer.state["companion"]["buddy"]["inventory"]["cutgrass"],0)
            self.assertTrue(any(row["finish"] for row in peer.bridge.feedback.values()))
        finally:await agent.close()


class PauseDiagnosticsTests(unittest.TestCase):
    def test_two_minutes_of_paused_game_yields_bounded_logs_and_recovery_counts(self):
        now=[0.];events=[]
        reporter=CycleDiagnostics(SimpleNamespace(trace=lambda event,**fields:events.append({"event":event,**fields})),clock=lambda:now[0])
        with patch("builtins.print"):
            for i in range(600):
                now[0]=i*.2;reporter.error(RuntimeError("Companion team disconnected/stale"))
        self.assertEqual(len(events),4)
        now[0]=120;reporter.recovered()
        self.assertEqual(events[-1]["event"],"cycle_error_end")
        self.assertEqual(events[-1]["repeats"],600)
        self.assertEqual(events[-1]["duration_seconds"],120)
        self.assertGreater(events[-1]["suppressed"],100)
        reporter.recovered();self.assertEqual(len(events),5)

    def test_actual_execution_errors_remain_immediate_even_with_same_message(self):
        events=[];reporter=CycleDiagnostics(SimpleNamespace(trace=lambda event,**fields:events.append(event)),clock=lambda:0.)
        with patch("builtins.print"):
            reporter.error(ValueError("Invalid action"));reporter.error(ValueError("Invalid action"))
        self.assertEqual(events,["cycle_error","cycle_error"])


if __name__=="__main__":unittest.main()
