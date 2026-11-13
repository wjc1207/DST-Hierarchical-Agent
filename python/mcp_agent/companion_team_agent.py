"""One planning queue; reuse the tested actor host for concurrent execution."""
import asyncio
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor,TimeoutError as FutureTimeout
from datetime import datetime,timezone
from types import SimpleNamespace

from mcp_agent.companion_agent import CompanionAgent
from mcp_agent.companion_tasks import capabilities
from mcp_agent.companion_team_planner import validate_team_plan
from mcp_agent.planning_core import recent_exchanges
from mcp_server.transport_metrics import TransportRecorder


class Ticket:
    def __init__(self,identity,context,cancel):
        self.identity,self.context,self.cancel=identity,context,cancel
        self.at=time.monotonic();self.event=threading.Event();self.result=None;self.error=None
    def finish(self,result=None,error=None):
        self.result,self.error=result,error;self.event.set()


class PlanningProxy:
    """Local actor requests are tickets, not independent model calls."""
    def __init__(self,host,identity):self.host,self.identity=host,identity
    def __call__(self,context,history,cancel):
        ticket=Ticket(self.identity,context,cancel)
        with self.host.lock:self.host.pending[self.identity]=ticket
        while not ticket.event.wait(.1):
            if cancel.is_set() or self.host.closed:raise RuntimeError("Planning superseded by newer player intent")
        if ticket.error:raise ticket.error
        return ticket.result


class ActorClient:
    def __init__(self,host,identity):self.host,self.identity=host,identity
    async def call_tool(self,name,args):
        args={**args,"buddy_id":self.identity}
        claim=None
        if name=="buddy_action" and args.get("op") in {"collect","work"}:
            claim=(args.get("args") or {}).get("target_guid")
            if not self.host.claim(self.identity,claim):raise RuntimeError("Resource is being worked by a teammate")
        try:result=await self.host.client.call_tool(name,args)
        except Exception:
            if claim is not None:self.host.release(self.identity)
            raise
        if claim is not None:
            if result.is_error:self.host.release(self.identity)
            else:self.host.claims[claim]["action_id"]=result.structured_content["id"]
        if name=="buddy_result" and not result.is_error and result.structured_content.get("status") in {"completed","failed","interrupted"}:
            self.host.release(self.identity,action_id=args["action_id"])
        if name=="buddy_observe" and not result.is_error:
            packet=copy.deepcopy(result.structured_content);observed=packet["state"].get("companion") or {}
            busy={guid for guid,row in self.host.claims.items() if row["owner"]!=self.identity and row["until"]>time.monotonic()}
            if busy:
                products={r.get("product") for r in observed.get("nearby",[]) if r.get("guid") in busy}
                for row in observed.get("work_targets",[]):
                    if row.get("guid") in busy:products.update(row.get("products",[]))
                observed["_team_claimed_products"]=list(products)
                observed["nearby"]=[row for row in observed.get("nearby",[]) if row.get("guid") not in busy]
                observed["work_targets"]=[row for row in observed.get("work_targets",[]) if row.get("guid") not in busy]
                observed["resources_complete"]=False
            return SimpleNamespace(is_error=False,structured_content=packet)
        return result


class CompanionTeamAgent:
    def __init__(self,client,planner,*,execute=False,log_path=None):
        self.client,self.planner,self.execute=client,planner,execute
        self.log_path=log_path;self.events=TransportRecorder(log_path)
        self.agents={};self.pending={};self.lock=threading.RLock();self.closed=False
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix="team-deepseek")
        self.future=None;self.cancel=None;self.inflight={};self.context=None;self.basis=None
        self.state=None;self.history=[];self.claims={};self.member_bases={};self.dispatched_revision=None
        if hasattr(planner,"set_error_handler"):
            planner.set_error_handler(lambda stage,detail:self.trace("planning_detail",stage=stage,detail=detail))
        if hasattr(planner,"set_query_handler"):
            loop=asyncio.get_running_loop()
            def query(args,context,cancel):
                args=dict(args);identity=args.pop("buddy_id");actor=context["members"][identity]
                future=asyncio.run_coroutine_threadsafe(self.rpc("buddy_recipes",buddy_id=identity,**args,
                    revision=actor["interaction"]["revision"],binding_token=actor["observation"]["token"]),loop)
                try:
                    for _ in range(100):
                        if cancel.is_set():raise RuntimeError("Planning superseded by newer player intent")
                        try:return future.result(timeout=.1)
                        except FutureTimeout:continue
                    raise RuntimeError("Companion recipe query timed out")
                finally:
                    if not future.done():future.cancel()
            planner.set_query_handler(query)

    def trace(self,event,**fields):
        self.events.write({"timestamp_utc":datetime.now(timezone.utc).isoformat(),"event":event,
                           "team_revision":self.basis[1] if self.basis else None,**fields})
    async def rpc(self,name,**args):
        result=await self.client.call_tool(name,args)
        if result.is_error:raise RuntimeError("; ".join(getattr(c,"text","") for c in result.content)[:300])
        return result.structured_content
    def release(self,identity,action_id=None):
        self.claims={g:r for g,r in self.claims.items() if not(r["owner"]==identity and (action_id is None or r.get("action_id")==action_id))}
    def claim(self,identity,guid):
        now=time.monotonic();row=self.claims.get(guid)
        if row and row["owner"]!=identity and row["until"]>now:return False
        self.claims[guid]={"owner":identity,"until":now+12};return True
    def abort_plan(self):
        if self.cancel:self.cancel.set()
        if self.future:self.future.cancel()
        with self.lock:
            for identity,ticket in self.inflight.items():
                if not ticket.cancel.is_set():self.pending.setdefault(identity,ticket)
        self.future=None;self.inflight={}

    def update(self,packet):
        state=packet["state"]
        roster=tuple(sorted((identity,(entry.get("companion") or {}).get("token")) for identity,entry in state["members"].items()))
        basis=(packet["session_id"],state["control"]["revision"],roster)
        if self.basis!=basis:
            if self.basis and self.basis[0]!=basis[0]:
                self.history=[]
                self.dispatched_revision=None
            self.abort_plan();self.basis=basis
        self.state=state
        for identity,entry in state["members"].items():
            actor_basis=((entry.get("companion") or {}).get("token"),entry["control"]["revision"])
            if self.member_bases.get(identity)!=actor_basis:self.release(identity)
            self.member_bases[identity]=actor_basis
        for row in self.claims.values():
            actor=self.agents.get(row["owner"])
            if actor and actor.active and actor.active[0]==row.get("action_id") and state["members"].get(row["owner"],{}).get("ready"):
                row["until"]=time.monotonic()+12
        self.claims={g:r for g,r in self.claims.items() if r["until"]>time.monotonic() and r["owner"] in state["members"]}

    def build_context(self,requests):
        members={};names={r["id"]:r["name"] for r in self.state["roster"]}
        for identity,entry in self.state["members"].items():
            observed=copy.deepcopy(entry.get("companion") or {})
            runtime=self.agents.get(identity)
            members[identity]={"name":names[identity],"interaction":copy.deepcopy(entry["control"]),"observation":observed,
                "current_task_board":runtime.board() if runtime else None,"supported_capabilities":capabilities(observed)}
            if identity in requests:
                members[identity]["replan_reason"]=requests[identity].context.get("replan_reason")
            if runtime and runtime.active:
                members[identity]["active_task_id"]=runtime.active[1]
        control=copy.deepcopy(self.state["control"])
        return {"interaction":control,"long_term_goal":control.get("goal") if control["mode"]=="autonomous" else None,
                "members":members,"request_ids":sorted(requests),
                "replan_reasons":{i:t.context.get("replan_reason") for i,t in requests.items()},
                "resource_claims":[{"guid":g,"buddy_id":r["owner"]} for g,r in self.claims.items()]}

    async def flush(self):
        if self.future and self.future.done():
            future,self.future=self.future,None;requests,self.inflight=self.inflight,{}
            try:
                result=future.result();catalogs=result.get("_catalogs")
                clean=validate_team_plan(result,self.context,catalogs)
                # All assignments are checked before any actor is released to execute.
                for item in clean["assignments"]:
                    identity=item["buddy_id"];ticket=requests[identity]
                    current=self.state["members"].get(identity) or {}
                    control=current.get("control",{});requested=ticket.context["interaction"]
                    if (ticket.cancel.is_set() or control.get("revision")!=requested["revision"]
                            or control.get("dialogue_revision")!=requested.get("dialogue_revision")
                            or (current.get("companion") or {}).get("token")!=ticket.context["observation"].get("token")):
                        raise RuntimeError("Planning superseded by newer player intent")
                for item in clean["assignments"]:requests[item["buddy_id"]].finish(item["plan"])
                self.history=recent_exchanges([*self.history,(json.dumps(self.context,ensure_ascii=False),result.get("_assistant_message") or json.dumps(clean,ensure_ascii=False))])
                self.trace("team_plan_adopted",buddy_ids=list(requests),outcome=clean["outcome"])
            except Exception as exc:
                for ticket in requests.values():ticket.finish(error=exc)
            return
        if self.future or not self.execute or self.planner is None:return
        with self.lock:
            requests={}
            for identity,ticket in list(self.pending.items()):
                entry=self.state["members"].get(identity) or {}
                if ticket.cancel.is_set() or not entry.get("control",{}).get("enabled"):
                    self.pending.pop(identity,None);ticket.finish(error=RuntimeError("Planning cancelled"));continue
                current=entry["control"];requested=ticket.context["interaction"]
                if ((entry.get("companion") or {}).get("token")!=ticket.context["observation"].get("token")
                        or current["revision"]!=requested["revision"]
                        or current.get("dialogue_revision")!=requested.get("dialogue_revision")):
                    self.pending.pop(identity,None);ticket.finish(error=RuntimeError("Planning superseded"));continue
                if entry.get("ready"):requests[identity]=ticket
            if not requests or time.monotonic()-min(t.at for t in requests.values())<.1:return
            c=self.state["control"]
            if self.dispatched_revision!=c["revision"] and c.get("request_kind") in {"message","autonomy"}:
                expected={i for i in c["target_ids"] if self.state["members"].get(i,{}).get("control",{}).get("enabled")}
                if not expected<=set(requests):return
            for identity in requests:self.pending.pop(identity,None)
            self.inflight=requests
        self.context=self.build_context(requests);self.cancel=threading.Event()
        self.future=self.executor.submit(self.planner,copy.deepcopy(self.context),list(self.history),self.cancel)
        self.dispatched_revision=self.state["control"]["revision"]
        self.trace("team_plan_requested",buddy_ids=list(requests),reasons=self.context["replan_reasons"])

    async def cycle(self):
        packet=await self.rpc("buddy_observe")
        if packet["state"].get("team") is not True:
            raise ValueError("Companion team snapshot required; update DST Buddy Agent")
        self.update(packet)
        for identity in list(self.agents):
            if identity not in self.state["members"]:
                agent=self.agents.pop(identity);await agent.close();self.release(identity)
        for identity in self.state["members"]:
            if identity not in self.agents:
                self.agents[identity]=CompanionAgent(ActorClient(self,identity),PlanningProxy(self,identity) if self.planner else None,execute=self.execute,
                                                    log_path=self.log_path,buddy_id=identity,external_board=self.planner is None)
        results=await asyncio.gather(*(agent.cycle() for agent in self.agents.values()),return_exceptions=True)
        for identity,result in zip(self.agents,results):
            if isinstance(result,Exception):self.trace("member_cycle_error",buddy_id=identity,kind=type(result).__name__,detail=str(result)[:250])
        self.update(await self.rpc("buddy_observe"))
        await self.flush()

    async def close(self):
        self.closed=True;self.abort_plan()
        with self.lock:
            for ticket in self.pending.values():ticket.finish(error=RuntimeError("Team host stopped"))
            self.pending.clear()
        await asyncio.gather(*(a.close() for a in self.agents.values()),return_exceptions=True)
        self.executor.shutdown(wait=False,cancel_futures=True)
