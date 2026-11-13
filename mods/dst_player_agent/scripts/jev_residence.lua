-- Bounds ordinary actions during residence. Safety commands omit this policy.
local M={}
local function finite(v) return type(v)=="number" and v==v and math.abs(v)<1000000 end
function M.inside(p,a,margin)
    return type(p)=="table" and finite(p.x) and finite(p.z) and finite(a.x) and finite(a.z)
        and a.radius>=(margin or 0) and (p.x-a.x)^2+(p.z-a.z)^2<=(a.radius-(margin or 0))^2
end
function M.valid(a,s)
    local home=s.places and s.places.home
    return type(a)=="table" and home and a.home_id==home.id and finite(a.radius) and a.radius>=6 and a.radius<=30
        and finite(a.x) and finite(a.z) and finite(home.x) and finite(home.z)
        and math.abs(a.x-home.x)<=0.0001 and math.abs(a.z-home.z)<=0.0001
end
function M.allowed(action,s,area,walk)
    local p=s.player.position
    if not M.inside(p,area,0.5) then return false end
    local kind=action.kind
    if walk then return M.inside(action,area,2) end
    if kind=="open_container" or kind=="store_item" or kind=="take_item" or kind=="start_meal" or kind=="harvest_meal" then
        for _,b in ipairs((s.places or {}).buildings or {}) do
            if b.id==action.building_id and b.guid==action.guid then
                return M.inside(b,area,2) and M.inside(p,area,5)
            end
        end
        return false
    end
    if kind=="explore" and (action.refuel_approach==true or action.camp_approach==true) then
        for _,b in ipairs((s.places or {}).buildings or {}) do
            if b.id==action.building_id and b.status~="missing"
                and ((action.refuel_approach and (b.prefab=="campfire" or b.prefab=="firepit"))
                    or (action.camp_approach and (b.prefab=="cookpot" or b.prefab=="treasurechest"))) then
                local route=b.route or {};local length=route.distance
                return route.status=="ready" and action.mode=="place" and finite(length) and length>=0
                    and M.inside(b,area,2) and M.inside(action,area,2) and M.inside(p,area,length+2)
            end
        end
        return false
    end
    if kind=="explore" or kind=="flee_from_nearest_hostile" then return false end
    if kind=="place" then return M.inside(action,area,2) end
    if kind=="build_campfire" or kind=="build_sciencemachine" then return M.inside(p,area,6) end
    if kind=="collect" or kind=="chop_nearest_tree" or kind=="mine_nearest_rock" or kind=="add_fuel" then
        for _,e in ipairs(s.nearby or {}) do if e.guid==action.guid then
            local point={x=e.x or (finite(e.dx) and p.x+e.dx or nil),z=e.z or (finite(e.dz) and p.z+e.dz or nil)}
            if kind=="add_fuel" then
                return finite(e.distance) and e.distance>=0 and e.distance<=3
                    and M.inside(point,area,2) and M.inside(p,area,e.distance+2)
            end
            local margin=(kind~="collect" or e.prefab=="berrybush_juicy") and 6 or 2
            local length=(e.collect_path or {}).distance
            return finite(length) and length>=0 and M.inside(point,area,margin) and M.inside(p,area,length+2)
        end end
        return false
    end
    return kind=="wait" or kind=="craft" or kind=="craft_torch" or kind=="craft_axe" or kind=="craft_pickaxe"
        or kind=="eat_safe_food" or kind=="cook_food" or kind=="equip_torch" or kind=="unequip_torch" or kind=="set_home"
end
return M
