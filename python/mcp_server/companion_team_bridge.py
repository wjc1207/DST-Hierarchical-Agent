"""One authenticated owner connection, independent single-actor bridges."""
import copy
import hmac
import math
import secrets
import threading
import time

from common import autonomy
from mcp_server.companion_bridge import CompanionBridge
from mcp_server.game_bridge import BridgeError

IDS={"buddy1","buddy2"}
ASCII_LOWER=str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")


class CompanionTeamBridge:
    def __init__(self,token,*,clock=time.monotonic):
        self.token,self.clock=token,clock
        self.lock=threading.RLock();self.members={};self.state=None
        self.session=None;self.sequence=0;self.received=-math.inf
        self.server_id=secrets.token_hex(12)

    def _fresh(self):
        if self.state is None or self.clock()-self.received>2:raise BridgeError("Companion team disconnected/stale")

    def exchange(self,payload,measurements=None):
        with self.lock:
            if not isinstance(payload.get("token"),str) or not hmac.compare_digest(payload["token"],self.token):raise PermissionError("Unauthorized")
            session,sequence=payload.get("session_id"),payload.get("sequence")
            state = payload.get("state")
            if not isinstance(state, dict) or state.get("team") is not True:
                raise BridgeError("Companion team snapshot required; update DST Buddy Agent")
            members=state.get("members")
            if members==[]:members={}
            control=autonomy.settings({"autonomy":state.get("control")})
            if (not isinstance(session,str) or not 1<=len(session)<=80 or type(sequence) is not int or sequence<1
                    or not isinstance(members,dict) or not set(members)<=IDS or control is None
                    or control.get("mode") not in {"instruction","autonomous"}
                    or (control["mode"]=="autonomous" and not control["goal"])):
                raise BridgeError("Invalid companion team envelope")
            if session!=self.session and self.session is not None and self.clock()-self.received<3:raise BridgeError("Another local client owns this team")
            if session==self.session and sequence<=self.sequence:raise BridgeError("Out-of-order team exchange")
            target_ids=state["control"].get("target_ids",[])
            if (not isinstance(target_ids,list) or any(not isinstance(i,str) for i in target_ids)
                    or len(set(target_ids))!=len(target_ids) or not set(target_ids)<=IDS):raise BridgeError("Invalid team scope")
            kind=state["control"].get("request_kind","manual")
            if kind not in {"message","manual","autonomy"}:raise BridgeError("Invalid team intent")
            control.update(target_ids=target_ids,request_kind=kind)
            roster=state.get("roster",[])
            if not isinstance(roster,list) or len(roster)>2:raise BridgeError("Invalid roster")
            seen_ids,seen_names=set(),set()
            for row in roster:
                name=row.get("name") if isinstance(row,dict) else None
                if (not isinstance(row,dict) or row.get("id") not in IDS or row["id"] in seen_ids
                        or not isinstance(name,str) or not 1<=len(name)<=12 or name.translate(ASCII_LOWER) in seen_names
                        or any(c.isspace() or ord(c)<32 or ord(c)==127 or c in "@/,[]\"'" for c in name) or name.translate(ASCII_LOWER) in {"all","全体"}):
                    raise BridgeError("Invalid or duplicate companion name")
                seen_ids.add(row["id"]);seen_names.add(name.translate(ASCII_LOWER))
            if not set(members)<=seen_ids:raise BridgeError("Member absent from roster")
            results=payload.get("results",[])
            if not isinstance(results,list) or len(results)>64:raise BridgeError("Invalid team results")
            staged={};commands=[];feedback=[]
            for identity,entry in members.items():
                if not isinstance(entry,dict):raise BridgeError("Invalid member state")
                member_control=entry.get("control")
                if (isinstance(member_control,dict) and member_control.get("mode")=="autonomous"
                        and (control["mode"]!="autonomous" or member_control.get("goal")!=control["goal"])):
                    raise BridgeError("Autonomous member does not match team goal")
                observed=entry.get("companion") or {}
                if observed.get("buddy_id",identity)!=identity:raise BridgeError("Mismatched companion identity")
                candidate=CompanionBridge(self.token,clock=self.clock)
                old=self.members.get(identity) if session==self.session else None
                if old:
                    for key,value in old.__dict__.items():
                        if key!="lock":setattr(candidate,key,copy.deepcopy(value))
                response=candidate.exchange({"token":self.token,"session_id":session+"|"+identity,"sequence":sequence,
                    "state":entry,"results":[r for r in results if isinstance(r,dict) and r.get("buddy_id")==identity]})
                staged[identity]=candidate
                if response["command"]:commands.append({**response["command"],"buddy_id":identity})
                feedback.extend({**r,"buddy_id":identity} for r in response["feedback"])
            self.members=staged;self.session=session;self.sequence=sequence;self.received=self.clock()
            self.state={"team":True,"control":copy.deepcopy(control),"roster":copy.deepcopy(roster),
                        "members":{key:copy.deepcopy(value.state) for key,value in staged.items()}}
            return {"server_id":self.server_id,"session_id":session,"sequence":sequence,"commands":commands,"feedback":feedback}

    def observe(self):
        with self.lock:
            self._fresh()
            return {"session_id":self.session,"state":copy.deepcopy(self.state),"connection_age_ms":(self.clock()-self.received)*1000}

    def member_call(self,method,buddy_id=None,*args,**kwargs):
        with self.lock:
            self._fresh()
            if buddy_id is None:
                if len(self.members)!=1:raise BridgeError("Specify buddy_id when controlling a team")
                buddy_id=next(iter(self.members))
            if buddy_id not in self.members:raise BridgeError("Unknown/unbound companion")
            return getattr(self.members[buddy_id],method)(*args,**kwargs)
