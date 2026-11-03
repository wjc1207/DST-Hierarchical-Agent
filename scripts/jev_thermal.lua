-- Native temperature and observable fire facts; bounded, illuminated cooling steps.
local M={}
local function finite(n) return type(n)=="number" and n==n and math.abs(n)<1000000 end
local function safe(fn, fallback)
    local ok,value=pcall(fn);if ok and value~=nil then return value end;return fallback
end
function M.read_vitals(G,player,v)
    local component=player.components and player.components.temperature
    local classified=player.player_classified
    local available=component~=nil or (classified and finite(classified.currenttemperature))
    if not available then return end
    local value=safe(function() return player:GetTemperature() end,nil)
    if not finite(value) and component then value=safe(function() return component:GetCurrent() end,nil) end
    if not finite(value) then return end
    local threshold=component and component.overheattemp or (G.TUNING or {}).OVERHEAT_TEMP
    v.temperature=math.floor(value*10+0.5)/10
    if finite(threshold) then
        v.overheat_threshold=threshold
        v.heat_warning_threshold=threshold-5
        v.heat_recovery_threshold=threshold-10
    end
    local overheated
    if finite(threshold) then overheated=value>threshold end
    v.overheating=safe(function() return player:IsOverheating() end,overheated)
    v.freezing=safe(function() return player:IsFreezing() end,value<0)
    v.temperature_source="player_native"
end
function M.hot(v)
    return v and (v.overheating==true or (finite(v.temperature) and finite(v.heat_warning_threshold)
        and v.temperature>=v.heat_warning_threshold)) or false
end
function M.fuel_bounds(facts)
    if finite(facts.fuel_percent) and facts.fuel_percent>=0 and facts.fuel_percent<=1 then
        return facts.fuel_percent,facts.fuel_percent,true
    end
    local section=facts.fire_section
    if finite(section) and section==math.floor(section) and section>=0 and section<=4 then
        return math.max(0,(section-1)/4),section/4,false
    end
end
local function annotate_bounds(facts)
    local low,high,exact=M.fuel_bounds(facts)
    facts.fuel_min,facts.fuel_max,facts.fuel_estimated=low,high,low~=nil and not exact or nil
    facts.fuel_lower_inclusive=low~=nil and (exact or facts.fire_section~=1) or nil
    facts.fuel_upper_inclusive=low~=nil and (exact or facts.fire_section==0 or facts.fire_section==4) or nil
    return facts
end
function M.fire(G,entity)
    local lit=safe(function() return entity:HasTag("fire") end,false)
    local result={lit=lit,fire_data_source="unavailable"}
    local fueled=entity.components and entity.components.fueled
    if not lit and not fueled then result.fire_section=0;result.fire_data_source="observed_tag";return annotate_bounds(result) end
    if fueled then
        result.fire_section=safe(function() return fueled:GetCurrentSection() end,nil)
        result.fuel_percent=safe(function() return fueled:GetPercent() end,nil)
        result.fire_data_source="native_component"
    elseif G.TheSim and entity.Transform then
        local x,y,z=entity.Transform:GetWorldPosition()
        for _,fx in ipairs(safe(function() return G.TheSim:FindEntities(x,y,z,0.5,{"HASHEATER"}) end,{})) do
            if fx.prefab=="campfirefire" and fx.AnimState then
                for section=4,1,-1 do
                    if safe(function() return fx.AnimState:IsCurrentAnimation("level"..section) end,false) then
                        result.fire_section=math.max(result.fire_section or 0,section)
                        result.fire_data_source="observed_fx";break
                    end
                end
            end
        end
    end
    if not finite(result.fire_section) then result.fire_section=nil end
    if not finite(result.fuel_percent) then result.fuel_percent=nil end
    return annotate_bounds(result)
end
function M.fuel_reason(G,player,entity,facts,ratio)
    local v={};M.read_vitals(G,player,v)
    if M.hot(v) or player._jev_cooling_active then return "temperature_too_high" end
    local now=G.GetTime()
    if ((player._jev_refuel_after or {})[entity.GUID] or 0)>now then return "fuel_settling" end
    facts=facts or M.fire(G,entity)
    if ratio then
        local low,_,exact=M.fuel_bounds(facts)
        if low==nil then return "fuel_ratio_unavailable" end
        if low>=1 or (not exact and low>=.75) then return "fuel_ratio_unverifiable" end
    elseif facts.lit and (facts.fire_section==nil or facts.fire_section>=2) then return "fire_already_sufficient" end
end
local function portable(player,G)
    local inv=player.replica and player.replica.inventory
    local item=inv and safe(function() return inv:GetEquippedItem(G.EQUIPSLOTS.HANDS) end,nil)
    return item and item.prefab=="torch" and item:IsValid()
end
function M.route_safe(G,player,point,segment)
    if not point or not finite(point.x) or not finite(point.z) then return false end
    local x,_,z=player.Transform:GetWorldPosition()
    local length=math.sqrt((point.x-x)^2+(point.z-z)^2)
    if length>4.1 or not segment(x,z,point.x,point.z) then return false end
    local sources=player._jev_cooling_sources or {}
    local function nearest(px,pz)
        local value=math.huge
        for _,source in ipairs(sources) do value=math.min(value,(px-source.x)^2+(pz-source.z)^2) end
        return value
    end
    local initial=nearest(x,z)
    for step=1,math.ceil(length*2) do
        local f=step/math.max(1,math.ceil(length*2))
        if nearest(x+(point.x-x)*f,z+(point.z-z)*f)<initial-0.05 then return false end
    end
    if G.TheWorld and G.TheWorld.state.isnight and not portable(player,G) then
        local cutoff=math.max(0.15,(G.TUNING or {}).DARK_CUTOFF or 0.1)
        for step=0,math.ceil(length*2) do
            local f=step/math.max(1,math.ceil(length*2))
            if safe(function() return G.TheSim:GetLightAtPoint(x+(point.x-x)*f,0,z+(point.z-z)*f) end,0)<=cutoff then
                return false
            end
        end
    end
    return true
end
function M.failed(player,action,now)
    local failed=player._jev_cooling_failed or {};player._jev_cooling_failed=failed
    failed[#failed+1]={x=action.x,z=action.z,until_time=now+5}
    while #failed>16 do table.remove(failed,1) end
end
function M.sample(G,player,v,nearby,segment)
    local active=player._jev_cooling_active==true
    if M.hot(v) then active=true
    elseif finite(v.temperature) and finite(v.heat_recovery_threshold) and v.temperature<=v.heat_recovery_threshold then active=false
    elseif not finite(v.temperature) and v.overheating~=true then active=false end
    player._jev_cooling_active=active
    local result={available=finite(v.temperature),active=active,status=active and "cooling" or "safe"}
    if not active then return result end
    local px,_,pz=player.Transform:GetWorldPosition()
    local sources={}
    for _,e in ipairs(nearby or {}) do
        if (e.prefab=="campfire" or e.prefab=="firepit") and e.lit==true then
            sources[#sources+1]={x=px+e.dx,z=pz+e.dz}
        end
    end
    player._jev_cooling_sources=sources
    local function distance(x,z)
        local nearest=math.huge
        for _,f in ipairs(sources) do nearest=math.min(nearest,(x-f.x)^2+(z-f.z)^2) end
        return nearest
    end
    local base=distance(px,pz);local score
    if #sources>0 then for step=1,8 do
        local angle=(step-1)*math.pi/4
        for _,length in ipairs({2,4}) do
            local point={x=px+math.cos(angle)*length,z=pz+math.sin(angle)*length}
            local value=distance(point.x,point.z);local failed=false
            for _,f in ipairs(player._jev_cooling_failed or {}) do
                if f.until_time>G.GetTime() and (point.x-f.x)^2+(point.z-f.z)^2<1 then failed=true end
            end
            if not failed and value>base+1 and (not score or value>score)
                and M.route_safe(G,player,point,segment) then result.route=point;score=value end
        end
    end end
    result.reason=result.route and "move_away_from_heat" or "no_safe_lit_cooling_route"
    return result
end
return M
