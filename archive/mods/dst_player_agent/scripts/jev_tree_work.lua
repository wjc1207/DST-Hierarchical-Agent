-- Growth stages are visible through replicated animations on remote clients.
-- Klei evergreens/deciduoustrees use short/normal/tall for 5/10/15 chops.
local M = { MAX_WORK=15 }
local ranks={tall=3,normal=2,short=1,unknown=0,old=-1,burnt=-2,monster=-3}
local work={short=5,normal=10,tall=15,old=1,burnt=1,monster=18}
local function safe(fn)
    local ok,value=pcall(fn); return ok and value or nil
end
function M.read(entity,tuning)
    local stage
    if entity:HasTag("burnt") then stage="burnt"
    elseif entity:HasTag("plantcreature") then stage="monster" end
    local growable=entity.components and entity.components.growable
    if not stage and growable then
        local data=safe(function() return growable:GetCurrentStageData() end)
        stage=data and data.name
        if not ranks[stage] then stage=({"short","normal","tall","old"})[growable.stage] end
    end
    local anim=entity.AnimState
    if not stage and anim and anim.IsCurrentAnimation then
        for _,size in ipairs({"tall","normal","short","old"}) do
            for _,prefix in ipairs({"idle_","sway1_loop_","sway2_loop_","chop_",
                                     "drop_leaves_","grow_leaves_","burning_loop_"}) do
                if safe(function() return anim:IsCurrentAnimation(prefix..size) end) then stage=size; break end
            end
            if stage then break end
        end
        if not stage then
            for animation,size in pairs({grow_old_to_short="short",grow_short_to_normal="normal",
                                         grow_normal_to_tall="tall",grow_tall_to_old="old"}) do
                if safe(function() return anim:IsCurrentAnimation(animation) end) then stage=size; break end
            end
        end
    end
    if not ranks[stage] then stage="unknown" end
    local family=entity.prefab=="deciduoustree" and "DECIDUOUS" or "EVERGREEN"
    local suffix=({short="SMALL",normal="NORMAL",tall="TALL",monster="MONSTER"})[stage]
    local required=suffix and tuning and tuning[family.."_CHOPS_"..suffix] or work[stage] or M.MAX_WORK
    local workable=entity.components and entity.components.workable
    local left=workable and safe(function() return workable:GetWorkLeft() end)
    if type(left)=="number" and left>0 and left<math.huge then required=math.ceil(left) end
    return {tree_stage=stage,work_required=required}
end
function M.prefer(candidate,current)
    if not current then return true end
    local a,b=ranks[candidate.tree_stage] or 0,ranks[current.tree_stage] or 0
    if a~=b then return a>b end
    if candidate.distance~=current.distance then return candidate.distance<current.distance end
    return (candidate.guid or 0)<(current.guid or 0)
end
return M
