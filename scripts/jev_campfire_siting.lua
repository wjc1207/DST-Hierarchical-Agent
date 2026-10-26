-- Local placement policy, not world navigation. Klei's campfire spreads up to
-- four units at maximum fuel (prefabs/campfire.lua PROPAGATE_RANGES).
local M = { MAX_PROPAGATE_RANGE = 4, SAFETY_MARGIN = 1, PLAYER_CLEARANCE = 3,
    SEARCH_RADIUS = 6, RADIAL_STEP = 0.5, ANGLE_STEP = 22.5 }
local function finite(x) return type(x)=="number" and x==x and math.abs(x)<1000000 end

function M.clearance(tuning)
    local factor = tuning and tuning.SPRING_FIRE_RANGE_MOD or 1
    if not finite(factor) or factor<1 then factor=1 end
    return M.MAX_PROPAGATE_RANGE*factor+M.SAFETY_MARGIN
end

function M.combustible(entity)
    if not entity or not entity:IsValid() or entity:HasTag("INLIMBO") then return false end
    if entity:HasTag("fire") or entity:HasTag("smolder") then return true end
    if entity:HasTag("burnt") then return false end
    -- canlight is replicated by burnable, so clients need no server components.
    if entity:HasTag("canlight") or entity:HasTag("player") then return true end
    local burnable = entity.components and entity.components.burnable
    return burnable~=nil and burnable.canlight~=false
end

function M.select(origin,hazards,clearance,can_build,safe_path)
    if not origin or not finite(origin.x) or not finite(origin.z) then return nil end
    for radius=M.PLAYER_CLEARANCE,M.SEARCH_RADIUS,M.RADIAL_STEP do
        local best,best_gap
        for angle=0,360-M.ANGLE_STEP,M.ANGLE_STEP do
            local radians=angle*math.pi/180
            local point={x=origin.x+math.cos(radians)*radius,z=origin.z+math.sin(radians)*radius}
            local gap=math.huge
            for _,hazard in ipairs(hazards) do
                if not finite(hazard.x) or not finite(hazard.z) then return nil end
                gap=math.min(gap,math.sqrt((point.x-hazard.x)^2+(point.z-hazard.z)^2))
            end
            if gap>=clearance and safe_path(point) and can_build(point)
                and (not best or gap>best_gap) then
                best,best_gap=point,gap
            end
        end
        if best then return best,best_gap end
    end
    return nil
end
return M
