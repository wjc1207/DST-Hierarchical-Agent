"""Temperature facts and refueling pauses, without guessing missing telemetry."""
from common.task_timing import number


def heat_risk(state):
    v=state.get("player",{}).get("vitals",{})
    return bool((state.get("thermal") or {}).get("active") or v.get("overheating") is True or
                (number(v.get("temperature")) and number(v.get("heat_warning_threshold"))
                 and v["temperature"]>=v["heat_warning_threshold"]))


def refuel_pause(target,state):
    if heat_risk(state):
        return "temperature_too_high"
    row=next((row for row in (state.get("refueling") or {}).get("targets",[])
              if row.get("building_id")==target["building_id"]),None)
    return row.get("ratio_reason","") if row and "at_least" in target else row.get("reason","") if row else ""
