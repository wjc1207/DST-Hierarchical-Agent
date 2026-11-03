"""Game-time task properties. No wall clock, model calls or game inputs."""
from copy import deepcopy
import math

PHASES = {"day": 0, "dusk": 1, "night": 2}
TIME_FIELDS = ("start_at", "duration", "end_at")
TERMINAL_TASKS = {"done", "failed", "expired"}


def number(value):
    return type(value) in {int, float} and math.isfinite(value)


def moment(raw, day, *, end=False):
    if not isinstance(raw, dict) or set(raw) - {"day", "progress", "phase", "next"}:
        raise ValueError("Time must be {day, progress} or {day, phase}")
    if ("progress" in raw) == ("phase" in raw):
        raise ValueError("Time requires exactly one of progress or phase")
    if "next" in raw:
        if not end or raw.get("next") is not True or "day" in raw or "phase" not in raw:
            raise ValueError("next:true is only allowed for end_at {phase, next:true}")
        result = {"next": True}
    else:
        value = raw.get("day", day)
        if type(value) is not int or not 1 <= value <= 100000:
            raise ValueError("Time day must be a positive game day")
        result = {"day": value}
    if "progress" in raw:
        if not number(raw["progress"]) or not 0 <= raw["progress"] < 1:
            raise ValueError("Time progress must be in [0,1)")
        result["progress"] = raw["progress"]
    else:
        if raw["phase"] not in PHASES:
            raise ValueError("Time phase must be day, dusk or night")
        result["phase"] = raw["phase"]
    return result


def validate_timing(task, day):
    """Return fields and a task-local error, so one bad timer cannot drop a board."""
    raw = {k: deepcopy(task[k]) for k in TIME_FIELDS if k in task}
    try:
        stay = task["target"]["type"] == "stay_home"
        maintenance = task["target"]["type"] in {"hunger_ratio", "refuel"}
        if task["target"]["type"] == "refuel" and "duration" in raw and "at_least" not in task["target"]:
            raise ValueError("Refuel duration requires at_least; legacy count is one-shot only")
        if "duration" in raw and "end_at" in raw:
            raise ValueError("duration and end_at are mutually exclusive; specify only one")
        if not stay and "end_at" in raw:
            raise ValueError("end_at requires stay_home; hunger_ratio/refuel accept optional duration only")
        if not stay and not maintenance and "duration" in raw:
            raise ValueError("Ordinary tasks accept start_at only; duration requires stay_home, hunger_ratio or refuel")
        if stay and not ("duration" in raw or "end_at" in raw):
            raise ValueError("stay_home requires exactly one of duration or end_at")
        clean = {}
        if "start_at" in raw:
            clean["start_at"] = moment(raw["start_at"], day)
        if "duration" in raw:
            if not number(raw["duration"]) or not 0 < raw["duration"] <= 7:
                raise ValueError("duration must be game days in (0,7]")
            clean["duration"] = raw["duration"]
        if "end_at" in raw:
            clean["end_at"] = moment(raw["end_at"], day, end=True)
        start, end = clean.get("start_at"), clean.get("end_at")
        if start and end and not end.get("next"):
            if end["day"] < start["day"] or (end["day"] == start["day"] and (
                "progress" in start and "progress" in end and end["progress"] <= start["progress"]
                or "phase" in start and "phase" in end and PHASES[end["phase"]] <= PHASES[start["phase"]])):
                raise ValueError("end_at must be later than start_at")
        return clean, ""
    except (ValueError, TypeError, KeyError) as exc:
        return raw, str(exc)


def game_time(state):
    world = state.get("world", {})
    day, progress = world.get("day"), world.get("time")
    if type(day) is not int or day < 1 or not number(progress) or not 0 <= progress <= 1:
        return None
    return day - 1 + progress


def reached(at, state):
    world = state.get("world", {})
    day = world.get("day")
    if type(day) is not int:
        return None
    if day != at["day"]:
        return day > at["day"]
    if "progress" in at:
        progress = world.get("time")
        return progress >= at["progress"] if number(progress) else None
    phase = world.get("phase")
    return PHASES[phase] >= PHASES[at["phase"]] if phase in PHASES else None


def resolve_end(at, state):
    if not at.get("next"):
        return at
    world = state.get("world", {})
    phase, day = world.get("phase"), world.get("day")
    if phase not in PHASES or type(day) is not int:
        return None
    return {"day": day + int(PHASES[at["phase"]] <= PHASES[phase]), "phase": at["phase"]}
