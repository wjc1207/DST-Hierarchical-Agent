"""Observed game-time residence lifecycle; uses the existing return-home action."""
from common.task_timing import game_time, reached, resolve_end, TERMINAL_TASKS
from common.residence import inside
from common.thermal import heat_risk


def update_residences(view, state, records):
    if view.get("state_basis") != "observed":
        return None
    now = game_time(state)
    home = (state.get("places") or {}).get("home") or {}
    threatened = heat_risk(state) or any(e.get("activeThreat") for e in state.get("nearby", []))
    active = []
    for task in view["tasks"]:
        if task["target"]["type"] != "stay_home" or task.get("timing_error"):
            continue
        record = records[task["id"]]
        hold = record.setdefault("residence", {"elapsed":0.,"arrived":False,"inside_prev":False,"last":None})
        if record["done"]:
            task["residence"] = {"phase":"done","elapsed_days":round(hold["elapsed"],6),"end_at":hold.get("end")}
            task["progress"] = {"current":round(hold["elapsed"],6) if "duration" in task else 1,
                                "required":task.get("duration",1)}
            continue
        end = hold.get("end") or task.get("end_at")
        if end and end.get("next"):
            end = None
        if now is None:
            if task["status"] != "scheduled":
                task.update(status="blocked",blocked_reason="game time unavailable")
            hold.update(last=None,inside_prev=False)
            continue
        if hold["last"] is not None and now < hold["last"]:
            hold["error"] = "game time moved backwards; resubmit with a new task ID"
        if hold.get("error"):
            task.update(status="failed",blocked_reason=hold["error"])
            continue
        valid = home.get("id") == task["target"]["home_id"]
        area = {"home_id":home.get("id"),"x":home.get("x"),"z":home.get("z"),"radius":task["target"]["radius"]}
        within = valid and inside(state.get("player",{}).get("position"),area)
        eligible = task["status"] in {"ready","in_progress"} or task.get("blocked_reason") == "search_budget_exhausted"
        was_arrived = hold["arrived"]
        if eligible and "end_at" in task and not hold.get("end"):
            end = resolve_end(task["end_at"],state)
            if end:
                hold["end"] = end
        if eligible and valid and home.get("arrived") and not threatened:
            hold["arrived"] = True
        if eligible and hold["arrived"] and within and not threatened and hold["inside_prev"] and hold["last"] is not None:
            hold["elapsed"] += max(0,now-hold["last"])
        hold.update(last=now,inside_prev=bool(eligible and hold["arrived"] and within and not threatened))
        ended = end and reached(end,state) is True
        duration_done = "duration" in task and hold["elapsed"]+1e-9 >= task["duration"]
        if ended or duration_done:
            if (was_arrived if ended else hold["arrived"]) and within and not threatened:
                task.update(status="done",blocked_reason="")
                record["done"] = True
            else:
                task.update(status="expired",blocked_reason="residence ended before safe residence was completed")
        elif eligible and valid:
            task["status"] = "in_progress"
            active.append((task,hold,area,within))
        phase = (task["status"] if task["status"] in TERMINAL_TASKS | {"scheduled","blocked"}
                 else "holding" if hold["arrived"] and within else "returning")
        task["residence"] = {"phase":phase,"elapsed_days":round(hold["elapsed"],6),"end_at":hold.get("end")}
        task["progress"] = {"current":1 if task["status"] == "done" and "end_at" in task else round(hold["elapsed"],6),
                            "required":task.get("duration",1)}
    if not active:
        return None
    # All valid tasks bind the one current home. Enforce the intersection of
    # their radii; overlapping timers can finish independently.
    task,hold,area,within = min(active,key=lambda row:row[2]["radius"])
    return {**area,"task_ids":[t["id"] for t,_,_,_ in active],
            "holding":all(h["arrived"] and w for _,h,_,w in active)}
