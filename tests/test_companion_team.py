import asyncio
import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_companion import state,TOKEN
from test_shared_planning import Response
from mcp_server.companion_team_bridge import CompanionTeamBridge
from mcp_server.companion_server import create_server
from mcp_agent.companion_team_planner import CompanionTeamPlanner,validate_team_plan
from mcp_agent.companion_team_agent import CompanionTeamAgent


def team_state():
    members={}
    for i in (1,2):
        entry=state();identity=f"buddy{i}"
        observed=entry["companion"];observed.update(buddy_id=identity,buddy_name=f"{i}号",token=f"bound:{i}")
        observed["buddy"]["guid"]=20+i
        observed["nearby"]=[{"guid":99,"prefab":"grass","product":"cutgrass","distance":1,"x":1,"z":0},
                            {"guid":100,"prefab":"sapling","product":"twigs","distance":2,"x":2,"z":0}]
        members[identity]=entry
    control={**copy.deepcopy(members["buddy1"]["control"]),"instruction":"分别收集草和树枝","target_ids":["buddy1","buddy2"],"request_kind":"message"}
    return {"team":True,"control":control,"roster":[{"id":f"buddy{i}","name":f"{i}号","bound":True} for i in (1,2)],"members":members}


def local_plan(identity,count=1):
    product="cutgrass" if identity=="buddy1" else "twigs"
    return {"goal":"Gather assigned material","reply":"我去收集。","tasks":[{"id":"gather","title":"Gather", "target":{"type":"inventory","prefab":product,"count":count}}]}


def context(raw=None,requests=None):
    raw=raw or team_state()
    return {"interaction":raw["control"],"request_ids":requests or ["buddy1","buddy2"],"members":{
        identity:{"name":identity,"interaction":entry["control"],"observation":entry["companion"],"current_task_board":None}
        for identity,entry in raw["members"].items()}}


def assignments(ctx):
    result=[]
    for identity in ctx["request_ids"]:
        actor=ctx["members"][identity];board=actor.get("current_task_board")
        if board and all(t["status"]=="done" for t in board["tasks"]):
            plan={"kind":"agent_reply","outcome":"completed","reply":"我这部分完成了。","evidence":"当前观察和看板均确认完成。"}
        else:plan=local_plan(identity)
        result.append({"buddy_id":identity,"base_revision":actor["interaction"]["revision"],"plan":plan})
    return {"kind":"team_plan","outcome":"working","assignments":result}


class TeamBridgeTests(unittest.TestCase):
    def setUp(self):
        self.raw=team_state();self.bridge=CompanionTeamBridge(TOKEN,clock=lambda:0);self.seq=0;self.exchange()
    def exchange(self,results=None):
        self.seq+=1
        return self.bridge.exchange({"token":TOKEN,"session_id":"team","sequence":self.seq,"state":self.raw,"results":results or []})
    def action(self,identity,guid):
        obs=self.bridge.member_call("observe",identity)
        return self.bridge.member_call("action",identity,"collect",obs["observation_id"],{"target_guid":guid})
    def test_parallel_actions_have_independent_credentials_queues_and_results(self):
        a=self.action("buddy1",99);b=self.action("buddy2",100)
        response=self.exchange();self.assertEqual({c["buddy_id"] for c in response["commands"]},{"buddy1","buddy2"})
        with self.assertRaises(ValueError):self.bridge.member_call("result","buddy2",a["id"])
        obs=self.bridge.member_call("observe","buddy1")
        with self.assertRaises(ValueError):self.bridge.member_call("action","buddy2","stop",obs["observation_id"],{})
        self.exchange([{"buddy_id":"buddy1","id":a["id"],"revision":1,"status":"completed","reason":"inventory_increased"}])
        self.assertEqual(self.bridge.member_call("result","buddy1",a["id"])["status"],"completed")
        self.assertEqual(self.bridge.member_call("result","buddy2",b["id"])["status"],"queued")
    def test_targeted_new_revision_interrupts_only_its_actor(self):
        a=self.action("buddy1",99);b=self.action("buddy2",100)
        self.raw["control"].update(revision=2,target_ids=["buddy1"])
        self.raw["members"]["buddy1"]["control"]["revision"]=2
        self.raw["members"]["buddy1"]["companion"]["revision"]=2;self.exchange()
        self.assertEqual(self.bridge.member_call("result","buddy1",a["id"])["status"],"interrupted")
        self.assertEqual(self.bridge.member_call("result","buddy2",b["id"])["status"],"queued")
    def test_invalid_second_member_is_rejected_without_partially_replacing_first(self):
        before=self.bridge.observe();self.raw["members"]["buddy1"]["companion"]["buddy"]["inventory"]={"cutgrass":99}
        self.raw["members"]["buddy2"]["control"]["revision"]=True
        with self.assertRaises(ValueError):self.exchange()
        self.assertEqual(self.bridge.observe(),before)
    def test_duplicate_names_unknown_scope_and_stale_actor_feedback_are_rejected(self):
        self.raw["roster"][1]["name"]=self.raw["roster"][0]["name"]
        with self.assertRaises(ValueError):self.exchange()
        self.raw=team_state();self.raw["control"]["target_ids"]=[{}]
        with self.assertRaises(ValueError):self.exchange()
        with self.assertRaises(ValueError):self.bridge.member_call("reply","buddy1","旧消息",0,"old",False)
        with self.assertRaises(ValueError):self.bridge.member_call("observe","other_player_buddy")


class TeamPlannerTests(unittest.TestCase):
    def test_actual_shared_request_loop_accepts_team_envelope_and_repairs_without_losing_scope(self):
        ctx=context();bad=assignments(ctx);bad["assignments"][1]["buddy_id"]="buddy1"
        planner=CompanionTeamPlanner("https://offline.invalid","fake","fake")
        with patch("mcp_agent.planning_core.urllib.request.urlopen",side_effect=[Response(bad),Response(assignments(ctx))]) as send:
            result=planner(ctx,[],threading.Event())
        self.assertEqual(len(result["assignments"]),2)
        self.assertEqual([json.loads(c.args[0].data)["reasoning_effort"] for c in send.call_args_list],["medium","none"])
        repair=json.loads(send.call_args_list[1].args[0].data)["messages"][-1]["content"]
        self.assertIn("ONE team_plan",repair)
        self.assertIn("request_ids",repair)

    def test_completion_diagnostics_identify_actor_and_invalid_field_and_retry_keeps_team_schema(self):
        ctx=context(requests=["buddy1"]);ctx["members"]["buddy1"]["current_task_board"]={"tasks":[{"status":"done"}]}
        valid=assignments(ctx);valid["assignments"][0]["plan"]={"kind":"agent_reply","outcome":"completed","reply":"完成。","evidence":"背包已有足量草。"}
        bad=copy.deepcopy(valid);bad["assignments"][0]["plan"]["evidence"]={"cutgrass":1}
        with self.assertRaisesRegex(ValueError,r"assignments\[buddy1\].plan: agent_reply.evidence"):
            validate_team_plan(bad,ctx)
        planner=CompanionTeamPlanner("https://offline.invalid","fake","fake")
        with patch("mcp_agent.planning_core.urllib.request.urlopen",side_effect=[Response(bad),Response(valid)]) as send:
            result=planner(ctx,[],threading.Event())
        self.assertEqual(result["assignments"][0]["plan"]["outcome"],"completed")
        repair=json.loads(send.call_args_list[1].args[0].data)["messages"][-1]["content"]
        self.assertIn("agent_reply.evidence",repair)
        self.assertIn("ONE team_plan",repair)

    def test_top_level_team_speech_is_rejected_and_each_board_needs_its_own_reply(self):
        ctx=context()
        for reply in (None,"我来统筹。","\n"):
            raw=assignments(ctx);raw["reply"]=reply
            with self.assertRaisesRegex(ValueError,"team_plan.reply is unsupported"):
                validate_team_plan(raw,ctx)
        raw=assignments(ctx);raw["assignments"][1]["plan"].pop("reply")
        with self.assertRaisesRegex(ValueError,r"assignments\[buddy2\].plan.reply"):
            validate_team_plan(raw,ctx)

    def test_shared_request_repairs_team_broadcast_into_individual_companion_speech(self):
        ctx=context();bad=assignments(ctx);bad["reply"]="队伍开始工作。";valid=assignments(ctx)
        valid["assignments"][0]["plan"]["reply"]="我去采草。"
        valid["assignments"][1]["plan"]["reply"]="我去找树枝。"
        planner=CompanionTeamPlanner("https://offline.invalid","fake","fake")
        with patch("mcp_agent.planning_core.urllib.request.urlopen",side_effect=[Response(bad),Response(valid)]) as send:
            clean=planner(ctx,[],threading.Event())
        self.assertNotIn("reply",clean)
        self.assertEqual([a["plan"]["reply"] for a in clean["assignments"]],["我去采草。","我去找树枝。"])
        repair=json.loads(send.call_args_list[1].args[0].data)["messages"][-1]["content"]
        self.assertIn("assignments[].plan.reply",repair)

    def test_one_response_assigns_two_local_boards_using_the_same_task_contract(self):
        ctx=context();clean=validate_team_plan(assignments(ctx),ctx)
        self.assertEqual(len(clean["assignments"]),2)
        for item in clean["assignments"]:self.assertEqual(item["plan"]["execution_policy"],"priority_goals")
    def test_scope_revision_and_duplicate_assignments_are_strict(self):
        ctx=context(requests=["buddy1"]);raw=assignments(ctx)
        raw["assignments"][0]["buddy_id"]="buddy2"
        with self.assertRaises(ValueError):validate_team_plan(raw,ctx)
        raw=assignments(ctx);raw["assignments"][0]["base_revision"]=99
        with self.assertRaises(ValueError):validate_team_plan(raw,ctx)
        raw=assignments(context());raw["assignments"][1]=copy.deepcopy(raw["assignments"][0])
        with self.assertRaises(ValueError):validate_team_plan(raw,context())
    def test_one_actor_finishing_cannot_claim_the_other_active_actor_is_complete(self):
        ctx=context(requests=["buddy1"])
        ctx["members"]["buddy1"]["current_task_board"]={"tasks":[{"status":"done"}]}
        raw=assignments(ctx);raw["outcome"]="completed"
        clean=validate_team_plan(raw,ctx)
        self.assertEqual(clean["outcome"],"working")
        self.assertEqual(clean["assignments"][0]["plan"]["outcome"],"completed")
        raw["outcome"]="working";self.assertEqual(validate_team_plan(raw,ctx)["outcome"],"working")
    def test_team_completion_still_rejects_unfinished_requested_assignments(self):
        ctx=context();raw=assignments(ctx);raw["outcome"]="completed"
        with self.assertRaisesRegex(ValueError,"every requested"):validate_team_plan(raw,ctx)
    def test_shared_request_uses_one_medium_model_call_and_queries_are_actor_scoped(self):
        ctx=context();seen=[]
        def request(url,key,body,validator,*,functions,**kwargs):
            seen.append(body)
            self.assertEqual(body["reasoning_effort"],"medium")
            self.assertGreaterEqual(body["max_tokens"],8192)
            for tool in body["tools"]:self.assertIn("buddy_id",tool["function"]["parameters"]["required"])
            with self.assertRaises(ValueError):functions["query_places"](buddy_id="stranger")
            return validator(assignments(ctx),False)
        planner=CompanionTeamPlanner("https://offline.invalid","fake","fake")
        with patch("mcp_agent.companion_team_planner.request_json_plan",request):
            result=planner(ctx,[],threading.Event())
        self.assertEqual(len(seen),1);self.assertEqual(len(result["assignments"]),2)


class TeamPeer:
    def __init__(self):
        self.raw=team_state();self.bridge=CompanionTeamBridge(TOKEN);self.seq=0;self.calls=[];self.running={};self.seen=set();self.exchange()
    def exchange(self,results=None):
        self.seq+=1;self.raw["control"]["enabled"]=any(e["control"]["enabled"] for e in self.raw["members"].values())
        response=self.bridge.exchange({"token":TOKEN,"session_id":"team","sequence":self.seq,"state":self.raw,"results":results or []})
        changed=False
        for message in response["feedback"]:
            if message["id"] in self.seen:continue
            self.seen.add(message["id"])
            if message.get("finish"):
                entry=self.raw["members"][message["buddy_id"]];entry["control"]["enabled"]=False;entry["control"]["revision"]+=1
                entry["companion"]["revision"]=entry["control"]["revision"];entry["companion"]["mode"]="follow";changed=True
        if changed:self.exchange()
        return response
    def complete(self,identity):
        record=self.running.pop(identity);entry=self.raw["members"][identity];guid=record["args"]["target_guid"]
        observed=entry["companion"];row=next(r for r in observed["nearby"] if r["guid"]==guid)
        inv=observed["buddy"]["inventory"];inv[row["product"]]=inv.get(row["product"],0)+1
        for e in self.raw["members"].values():e["companion"]["nearby"]=[r for r in e["companion"]["nearby"] if r["guid"]!=guid]
        observed["action"]=None;observed["version"]+=1
        self.exchange([{"buddy_id":identity,"id":record["id"],"revision":record["revision"],"status":"completed","reason":"inventory_increased"}])
    def message(self,ids,text):
        c=self.raw["control"];c["revision"]+=1;c.update(target_ids=ids,request_kind="message")
        c["messages"].append({"id":c["revision"],"text":text,"target_ids":ids})
        for identity in ids:
            entry=self.raw["members"][identity];entry["control"]["revision"]+=1;entry["control"]["enabled"]=True
            entry["companion"]["revision"]=entry["control"]["revision"];entry["companion"]["version"]+=1;entry["companion"]["action"]=None
            self.running.pop(identity,None)
        self.exchange()
    async def call_tool(self,name,args):
        self.calls.append((name,copy.deepcopy(args)));self.exchange()
        try:
            identity=args.get("buddy_id")
            if name=="buddy_observe":value=self.bridge.observe() if identity is None else self.bridge.member_call("observe",identity)
            elif name=="buddy_action":
                value=self.bridge.member_call("action",identity,args["op"],args["observation_id"],args.get("args"))
                if args["op"]=="collect":
                    self.running[identity]=value;self.raw["members"][identity]["companion"]["action"]={"status":"running","op":"collect"}
                    self.exchange([{"buddy_id":identity,"id":value["id"],"revision":value["revision"],"status":"running"}])
                else:self.exchange([{"buddy_id":identity,"id":value["id"],"revision":value["revision"],"status":"completed"}])
            elif name=="buddy_result":value=self.bridge.member_call("result",identity,args["action_id"])
            elif name=="buddy_reply":value=self.bridge.member_call("reply",identity,args["text"],args["revision"],args["message_id"],args.get("finish",False));self.exchange()
            else:raise ValueError(name)
            return SimpleNamespace(is_error=False,structured_content=value)
        except ValueError as exc:return SimpleNamespace(is_error=True,content=[SimpleNamespace(text=str(exc))])


async def spin(host,predicate,limit=150):
    for _ in range(limit):
        await host.cycle()
        if predicate():return
        await asyncio.sleep(.01)
    raise AssertionError("Team host did not reach the expected state")


class TeamHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_autonomous_member_advances_milestones_and_waits_when_blocked(self):
        peer = TeamPeer()
        goal = "维持营地补给"
        del peer.raw["members"]["buddy2"]
        peer.raw["roster"] = peer.raw["roster"][:1]
        peer.raw["control"].update(mode="autonomous", goal=goal, target_ids=["buddy1"], request_kind="autonomy")
        peer.raw["members"]["buddy1"]["control"].update(mode="autonomous", goal=goal, instruction=None, messages=[])
        peer.exchange()
        contexts = []

        def planner(ctx, *_):
            contexts.append(ctx)
            result = assignments(ctx)
            if len(contexts) <= 2:
                result["assignments"][0]["plan"] = local_plan(f"buddy{len(contexts)}")
            else:
                result["assignments"][0]["plan"] = {
                    "kind": "agent_reply", "outcome": "blocked", "reply": "物资暂时够用，我会稍后检查。"}
            return result

        host = CompanionTeamAgent(peer, planner, execute=True)
        try:
            await spin(host, lambda: "buddy1" in peer.running)
            peer.complete("buddy1")
            await spin(host, lambda: "buddy1" in peer.running)
            self.assertEqual(contexts[1]["replan_reasons"], {"buddy1": "autonomous_milestone_complete"})
            self.assertEqual(contexts[1]["long_term_goal"], goal)
            peer.complete("buddy1")
            await spin(host, lambda: host.agents["buddy1"].retry_at > 0)
            for _ in range(5):
                await host.cycle()
            self.assertEqual(len(contexts), 3)
            self.assertTrue(peer.raw["members"]["buddy1"]["control"]["enabled"])
            self.assertTrue(all(not args["finish"] for name, args in peer.calls if name == "buddy_reply"))

            new_goal = "维持木头储备"
            peer.raw["control"].update(goal=new_goal, revision=2)
            member = peer.raw["members"]["buddy1"]
            member["control"].update(goal=new_goal, revision=2)
            member["companion"].update(revision=2)
            peer.exchange()
            await spin(host, lambda: len(contexts) == 4)
            self.assertEqual(contexts[3]["long_term_goal"], new_goal)
            self.assertIsNone(contexts[3]["members"]["buddy1"]["current_task_board"])
        finally:
            await host.close()

    async def test_shared_plan_queues_each_actors_own_speech_without_a_team_broadcast(self):
        peer=TeamPeer()
        def planner(ctx,*_):
            raw=assignments(ctx)
            for item in raw["assignments"]:item["plan"]["reply"]="我去采草。" if item["buddy_id"]=="buddy1" else "我去找树枝。"
            return raw
        host=CompanionTeamAgent(peer,planner,execute=True)
        try:
            await spin(host,lambda:len(peer.running)==2)
            self.assertTrue(all(a.tasks.plan and not a.failures for a in host.agents.values()))
            replies=[args for name,args in peer.calls if name=="buddy_reply"]
            self.assertEqual({a["buddy_id"]:a["text"] for a in replies},{"buddy1":"我去采草。","buddy2":"我去找树枝。"})
            self.assertEqual(len(replies),2)
            self.assertFalse(any(name=="buddy_team_reply" for name,_ in peer.calls))
            self.assertEqual({r["buddy_id"] for r in peer.exchange()["feedback"]},{"buddy1","buddy2"})
        finally:await host.close()

    async def test_superseded_central_plan_is_ignored_and_only_one_model_request_runs_at_a_time(self):
        peer=TeamPeer();calls=[];release=threading.Event();active=[0];maximum=[0]
        def planner(ctx,history,cancel):
            active[0]+=1;maximum[0]=max(maximum[0],active[0]);calls.append(ctx)
            try:
                if len(calls)==1:release.wait(3)
                raw=assignments(ctx)
                for item in raw["assignments"]:
                    if item["buddy_id"]=="buddy1" and item["base_revision"]==2:item["plan"]=local_plan("buddy1",count=2)
                return raw
            finally:active[0]-=1
        host=CompanionTeamAgent(peer,planner,execute=True)
        try:
            await spin(host,lambda:len(calls)==1)
            peer.message(["buddy1"],"改为两根草")
            for _ in range(4):await host.cycle();await asyncio.sleep(.02)
            self.assertFalse(peer.running)
            release.set();await spin(host,lambda:len(peer.running)==2)
            self.assertEqual(maximum[0],1)
            self.assertEqual(host.agents["buddy1"].tasks.plan["tasks"][0]["target"]["count"],2)
            self.assertEqual(len(host.history),1)
        finally:release.set();await host.close()

    async def test_one_plan_runs_two_actions_concurrently_and_first_finish_does_not_stop_the_other(self):
        peer=TeamPeer();contexts=[]
        def planner(ctx,*_):contexts.append(ctx);return assignments(ctx)
        host=CompanionTeamAgent(peer,planner,execute=True)
        try:
            await spin(host,lambda:len(peer.running)==2)
            self.assertEqual(len(contexts),1)
            self.assertEqual({i:r["args"]["target_guid"] for i,r in peer.running.items()},{"buddy1":99,"buddy2":100})
            peer.complete("buddy1")
            await spin(host,lambda:not peer.raw["members"]["buddy1"]["control"]["enabled"])
            self.assertIn("buddy2",peer.running)
            self.assertEqual(contexts[-1]["request_ids"],["buddy1"])
            peer.complete("buddy2")
            await spin(host,lambda:not peer.raw["members"]["buddy2"]["control"]["enabled"])
        finally:await host.close()
    async def test_named_replan_does_not_cancel_another_running_action(self):
        peer=TeamPeer();contexts=[]
        def planner(ctx,*_):contexts.append(ctx);return assignments(ctx)
        host=CompanionTeamAgent(peer,planner,execute=True)
        try:
            await spin(host,lambda:len(peer.running)==2);other=peer.running["buddy2"]["id"]
            before=len(peer.calls)
            peer.message(["buddy1"],"一号继续采草")
            await spin(host,lambda:len(contexts)>=2 and "buddy1" in peer.running)
            self.assertEqual(contexts[-1]["request_ids"],["buddy1"])
            self.assertEqual(peer.running["buddy2"]["id"],other)
            self.assertEqual([args["buddy_id"] for name,args in peer.calls[before:] if name=="buddy_reply"],["buddy1"])
        finally:await host.close()
    async def test_resource_claim_prevents_both_actors_chasing_the_same_entity(self):
        peer=TeamPeer()
        def planner(ctx,*_):
            raw=assignments(ctx)
            for item in raw["assignments"]:item["plan"]=local_plan("buddy1")
            return raw
        host=CompanionTeamAgent(peer,planner,execute=True)
        try:
            await spin(host,lambda:len(peer.running)==1)
            for _ in range(5):await host.cycle()
            self.assertEqual(len(peer.running),1)
            self.assertEqual(len(host.claims),1)
        finally:await host.close()
    async def test_an_invalid_second_assignment_is_not_partially_applied(self):
        peer=TeamPeer();contexts=[]
        def planner(ctx,*_):
            contexts.append(ctx);raw=assignments(ctx);raw["assignments"][1]["base_revision"]=99;return raw
        host=CompanionTeamAgent(peer,planner,execute=True)
        try:
            await spin(host,lambda:bool(contexts) and all(a.failures for a in host.agents.values()))
            self.assertFalse(peer.running)
            self.assertFalse(any(a.tasks.plan for a in host.agents.values()))
        finally:await host.close()


class TeamMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_recipe_query_for_second_actor_works_while_first_actor_has_an_action(self):
        from mcp import Client
        raw=team_state();raw["members"]["buddy2"]["companion"]["capabilities"].append("recipes")
        raw["members"]["buddy2"]["companion"]["recipes"]={}
        bridge=CompanionTeamBridge(TOKEN);seq=[0]
        def exchange(results=None):
            seq[0]+=1;return bridge.exchange({"token":TOKEN,"session_id":"sdk-team","sequence":seq[0],"state":raw,"results":results or []})
        exchange();observed=bridge.member_call("observe","buddy1")
        work=bridge.member_call("action","buddy1","collect",observed["observation_id"],{"target_guid":99})
        stopped=asyncio.Event()
        async def pump():
            while not stopped.is_set():
                response=exchange();results=[]
                for command in response["commands"]:
                    result={"buddy_id":command["buddy_id"],"id":command["id"],"revision":command["revision"],"status":"running"}
                    if command["op"]=="recipes":result.update(status="completed",data={"recipes":[],"total":0,"offset":0})
                    results.append(result)
                if results:exchange(results)
                await asyncio.sleep(.01)
        task=asyncio.create_task(pump())
        try:
            async with Client(create_server(bridge),read_timeout_seconds=3) as client:
                result=await client.call_tool("buddy_recipes",{"buddy_id":"buddy2","revision":1,"binding_token":"bound:2","query":"axe"})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["total"],0)
            self.assertEqual(bridge.member_call("result","buddy1",work["id"])["status"],"running")
        finally:stopped.set();await task


if __name__=="__main__":unittest.main()
