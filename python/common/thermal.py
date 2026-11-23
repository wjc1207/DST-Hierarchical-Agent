"""Temperature facts, 4-season thermal comfort checks, and refueling pauses."""
from common.task_timing import number

HEATROCK_WARM_TEMP = 45.0
HEATROCK_COOL_TEMP = 20.0


def seasonal_gear_equip(observed, need):
    """Return GUID of the best unequipped usable seasonal/rain gear in observed equipment."""
    if "equip" not in (observed.get("capabilities") or []):
        return None
    gear = [r for r in observed.get("equipment", []) if r.get("usable")]
    if need in {"winter", "summer"}:
        slots = ("head", "body") if need == "winter" else ("head", "body", "hands")
        for slot in slots:
            cand = [r for r in gear if r.get("slot") == slot and (r.get("insulation") or 0) > 0 and r.get("insulation_type") == need]
            if cand:
                best = max(cand, key=lambda r: (r.get("insulation") or 0, 1 if r.get("equipped") else 0))
                if not best.get("equipped"):
                    return best["guid"]
    elif need == "rain":
        for slot in ("head", "body", "hands"):
            cand = [r for r in gear if r.get("slot") == slot and (r.get("waterproof") or 0) > 0]
            if cand:
                best = max(cand, key=lambda r: (r.get("waterproof") or 0, 1 if r.get("equipped") else 0))
                if not best.get("equipped"):
                    return best["guid"]
    return None


def thermal_comfort_status(target, observed):
    """Evaluate temperature, thermal stone charge, and rain/moisture comfort from observed state."""
    observed = observed or {}
    buddy = observed.get("buddy") or {}
    world = observed.get("world") or {}
    temp = buddy.get("temperature")
    moisture = buddy.get("moisture")
    known = number(temp)
    temp_ok = bool(known and target["min_temp"] <= temp <= target["max_temp"])
    stones = (observed.get("thermal") or {}).get("stones") or []
    charge = target.get("charge_heatrock")
    if charge == "warm":
        stone_ok = any(number(s.get("temperature")) and s["temperature"] >= HEATROCK_WARM_TEMP for s in stones)
    elif charge == "cool":
        stone_ok = any(number(s.get("temperature")) and s["temperature"] <= HEATROCK_COOL_TEMP for s in stones)
    else:
        stone_ok = True
    max_m = target.get("max_moisture")
    wet_issue = bool(max_m is not None and number(moisture) and moisture > max_m)
    israining = bool(world.get("israining"))
    rain_equip = seasonal_gear_equip(observed, "rain") is not None
    if max_m is not None:
        rain_gear_issue = bool((israining or wet_issue) and rain_equip)
    else:
        rain_gear_issue = bool(israining and number(moisture) and moisture >= 20 and rain_equip)
    moisture_ok = (not wet_issue) and (not rain_gear_issue)
    return {
        "known": bool(known),
        "temp_ok": temp_ok,
        "stone_ok": stone_ok,
        "moisture_ok": moisture_ok,
        "wet_issue": wet_issue,
        "rain_gear_issue": rain_gear_issue,
        "satisfied": bool(known and temp_ok and stone_ok and moisture_ok),
    }


def heat_risk(state):
    v=state.get("player",{}).get("vitals",{})
    return bool((state.get("thermal") or {}).get("active") or v.get("overheating") is True or
                (number(v.get("temperature")) and number(v.get("heat_warning_threshold"))
                 and v["temperature"]>=v["heat_warning_threshold"]))


def refuel_pause(target,state):
    row=next((row for row in (state.get("refueling") or {}).get("targets",[])
              if row.get("building_id")==target["building_id"]),None)
    building=next((b for b in (state.get("places") or {}).get("buildings",[])
                   if b.get("id")==target["building_id"]),None)
    is_cold=bool((row and row.get("is_cold")) or ((building or {}).get("prefab") in {"coldfire","coldfirepit"}))
    if not is_cold and heat_risk(state):
        return "temperature_too_high"
    return row.get("ratio_reason","") if row and "at_least" in target else row.get("reason","") if row else ""
