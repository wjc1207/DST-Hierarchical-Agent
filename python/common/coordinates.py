"""Observed world coordinates, shared by planning and the MCP command boundary."""
import math
from common.task_timing import number


def validate_point(raw):
    if not isinstance(raw, dict) or any(not number(raw.get(k)) or abs(raw[k]) > 100000 for k in ('x','z')):
        raise ValueError('Destination requires finite world x/z coordinates within +/-100000')
    radius = raw.get('arrival_radius', 2)
    if not number(radius) or not .5 <= radius <= 4:
        raise ValueError('arrival_radius must be 0.5-4 world units')
    return {'x':raw['x'],'z':raw['z'],'arrival_radius':radius}


def observed_point(raw, world_map):
    point = validate_point(raw)
    size = world_map.get('cell_size') if isinstance(world_map,dict) else None
    if not number(size) or size <= 0:
        raise ValueError('Observed coordinate map unavailable; update Mod and refresh state')
    gx,gz = (math.floor(point[k]/size+.5) for k in ('x','z'))
    cells = world_map.get('cells')
    row = cells.get(f'{gx}:{gz}') if isinstance(cells,dict) else None
    if not isinstance(row,list) or len(row) < 6 or row[2] != 1:
        raise ValueError('Destination is not on observed passable land; explore or select another point')
    return point


def arrived(target, state):
    try:
        point = observed_point(target,state.get('world_map'))
    except ValueError as exc:
        return False,str(exc)
    position = state.get('player',{}).get('position') or {}
    if any(not number(position.get(k)) for k in ('x','z')):
        return False,'player position unavailable'
    return math.hypot(position['x']-point['x'],position['z']-point['z']) <= point['arrival_radius'],''


def move_action(target, state):
    point = observed_point(target,state.get('world_map'))
    if state.get('bridge',{}).get('coordinate_navigation') is not True:
        raise ValueError('Coordinate navigation requires updated Lua Mod and re-entering the world')
    return {'kind':'explore','mode':'coordinate','coordinate_target':point,'planner_only':True,
            'description':f"Travel to world ({point['x']}, {point['z']})"}
