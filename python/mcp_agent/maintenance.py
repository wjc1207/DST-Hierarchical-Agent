"""Observed game-time windows for persistent hunger/fire goals, independent of safety pauses."""
from common.task_timing import game_time, number, TERMINAL_TASKS
from common.refueling import maintenance_state as fire_maintenance_state


def is_maintenance(task):
    return task["target"]["type"] in {"hunger_ratio", "refuel"} and "duration" in task


def update_maintenance(view, state, records):
    observed = view.get("state_basis") == "observed"
    now = game_time(state)
    vitals = state.get("player", {}).get("vitals", {})
    hunger, maximum = vitals.get("hunger"), vitals.get("hunger_max")
    for task in view["tasks"]:
        if not is_maintenance(task) or task.get("timing_error"):
            continue
        record = records[task["id"]]
        fire = fire_maintenance_state(task["target"],state) if task["target"]["type"] == "refuel" else None
        known = fire["available"] if fire else number(hunger) and number(maximum) and maximum > 0
        satisfied = fire["satisfied"] if fire else known and hunger / maximum >= task["target"]["at_least"]
        desired = "maintaining" if (not fire["needs_fuel"] if fire else satisfied) else "in_progress"
        if fire:
            expiry_reason = "maintenance ended below fuel ratio" if fire["observed"] else "maintenance ended without current fire telemetry"
        else:
            expiry_reason = "maintenance ended below hunger target" if known else "maintenance ended without hunger telemetry"
        timer = record.get("maintenance")
        if timer and timer.get("status") in TERMINAL_TASKS:
            task.update(status=timer["status"], blocked_reason=timer.get("reason", ""))
        elif observed:
            eligible = task["status"] in {"ready", "in_progress", "maintaining"}
            can_start = eligible or (fire is not None and task.get("blocked_reason") in {
                "temperature_too_high", "search_budget_exhausted", "fuel_ratio_unavailable", "fuel_ratio_unverifiable"})
            if timer is None and can_start and now is not None and known:
                timer = {"started_at":now, "deadline":now+task["duration"], "last":now}
                record["maintenance"] = timer
            if timer:
                if now is None:
                    task.update(status="blocked", blocked_reason="game time unavailable")
                elif now < timer["last"]:
                    timer.update(status="failed", reason="game time moved backwards; resubmit with a new task ID")
                    task.update(status=timer["status"], blocked_reason=timer["reason"])
                elif now+1e-9 >= timer["deadline"]:
                    done = satisfied and not vitals.get("dead")
                    reason = ("" if done else "player dead at maintenance deadline" if vitals.get("dead") else
                              expiry_reason)
                    timer.update(status="done" if done else "expired",reason=reason)
                    task.update(status=timer["status"],blocked_reason=reason)
                    record["done"] = done
                elif eligible:
                    task.update(status=desired,blocked_reason="")
                if now is not None:
                    timer["last"] = max(timer["last"],now)
            elif eligible and now is None:
                task.update(status="blocked",blocked_reason="game time unavailable")
        if timer:
            elapsed = min(task["duration"],max(0,timer["last"]-timer["started_at"]))
            task["maintenance"] = {"phase":task["status"],"started_at":timer["started_at"],
                "deadline":timer["deadline"],"elapsed_days":round(elapsed,6),
                "remaining_days":round(max(0,task["duration"]-elapsed),6)}
