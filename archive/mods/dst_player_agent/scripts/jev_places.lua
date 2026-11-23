-- Observed buildings and one explicit home, scoped to the local world/player.
local M={}
local function copy(t)
    if type(t)~="table" then return t end
    local r={}; for k,v in pairs(t) do r[k]=copy(v) end; return r
end
local function distance(a,b) return math.sqrt((a.x-b.x)^2+(a.z-b.z)^2) end
local HOME_POINT_RADIUS=2
local REBIND_RADIUS=.25
local RELOAD_GRACE=10

function M.home_point_matches(a,b)
    local function point(p)
        return type(p)=="table" and type(p.x)=="number" and type(p.z)=="number"
            and p.x==p.x and p.z==p.z and math.abs(p.x)<1000000 and math.abs(p.z)<1000000
    end
    return point(a) and point(b) and distance(a,b)<=HOME_POINT_RADIUS
end

function M.memory(world,owner)
    if not world._jev_places or world._jev_places.owner~=owner then
        world._jev_places={owner=owner,buildings={},home_revision=0}
    end
    world._jev_places.guid_to_id=world._jev_places.guid_to_id or {}
    return world._jev_places
end

function M.id_for_guid(memory,guid)
    if not memory then return "building:"..tostring(guid) end
    local id=(memory.guid_to_id or {})[guid]
    local row=id and memory.buildings[id]
    return row and row.guid==guid and row.status~="missing" and id or nil
end
function M.guid_for_id(memory,id)
    local row=memory and memory.buildings[id]
    return row and row.status=="observed" and row.guid or nil
end

function M.observe(memory,position,entities,region_at,now,radius,complete)
    local seen,current,valid,bindings={},{},{},{}
    memory.guid_to_id={}
    for id,r in pairs(memory.buildings) do memory.guid_to_id[r.guid]=id end
    for _,e in ipairs(entities) do
        if e.guid and type(e.prefab)=="string" and type(e.x)=="number" and type(e.z)=="number"
            and distance(position,e)<=radius and not current[e.guid] then
            current[e.guid]=true;valid[#valid+1]=e
        end
    end
    table.sort(valid,function(a,b) return a.guid<b.guid end)
    -- Exact live GUIDs take precedence. Rebinding is a unique one-to-one match
    -- to an absent, previously out-of-view site, never nearest-of-many guessing.
    local candidates,uses={},{}
    for _,e in ipairs(valid) do
        local direct=memory.guid_to_id[e.guid]
        if direct then bindings[e.guid]=direct
        else
            local choices={}
            for id,r in pairs(memory.buildings) do
                if r.reload_allowed and r.status~="missing" and not current[r.guid]
                    and r.prefab==e.prefab and distance(r,e)<=REBIND_RADIUS then
                    choices[#choices+1]=id;uses[id]=(uses[id] or 0)+1
                end
            end
            candidates[e.guid]=choices
        end
    end
    for _,e in ipairs(valid) do
        local id=bindings[e.guid];local choices=candidates[e.guid]
        if not id and choices and #choices==1 and uses[choices[1]]==1 then id=choices[1] end
        id=id or "building:"..tostring(e.guid)
        -- GUIDs are process-local and normally unique, but logical IDs must never collide.
        while seen[id] or (memory.buildings[id] and not bindings[e.guid]
            and (not choices or #choices~=1 or uses[choices[1]]~=1)) do
            memory.next_site=(memory.next_site or 1000000000)+1;id="building:"..memory.next_site
        end
        local old=memory.buildings[id]
        local revision=old and old.binding_revision or 0
        if old and old.guid~=e.guid then
            revision=revision+1
            print("[JEV_DST_PLACE]rebound id="..id.." old_guid="..old.guid.." new_guid="..e.guid.." prefab="..e.prefab.." distance="..distance(old,e))
        end
        seen[id]=true
        memory.buildings[id]={id=id,guid=e.guid,prefab=e.prefab,x=e.x,z=e.z,
            node_id=region_at(e.x,e.z),name=e.name or e.prefab,
            semantic_prefab=e.semantic_prefab,radius=e.radius or 0,
            first_seen=old and old.first_seen or now,last_seen=now,status="observed",
            binding_revision=revision,capabilities=e.capabilities or {}}
    end
    local rows={}
    for id,record in pairs(memory.buildings) do
        if not seen[id] then
            if complete and distance(position,record)<radius-1 then
                record.missing_since=record.missing_since or now
                record.missing_scans=(record.missing_scans or 0)+1
                if record.missing_scans>=3 and now-record.missing_since>=(record.reload_allowed and RELOAD_GRACE or 1) then
                    if record.status~="missing" then
                        print("[JEV_DST_PLACE]missing id="..id.." guid="..record.guid.." reason=confirmed_scan_absence scans="..record.missing_scans)
                    end
                    record.status="missing";record.missing_reason="confirmed_scan_absence"
                elseif record.status~="missing" then record.status="unconfirmed" end
            else
                if distance(position,record)>=radius-1 then record.reload_allowed=true end
                record.missing_since=nil;record.missing_scans=nil
                if record.status~="missing" then record.status="unconfirmed" end
            end
        end
        rows[#rows+1]=record
    end
    table.sort(rows,function(a,b) return a.last_seen>b.last_seen or a.last_seen==b.last_seen and a.id<b.id end)
    -- Bounded observation memory. The home point is independent of evicted or
    -- destroyed structures and is never deleted merely because they disappear.
    for i=257,#rows do memory.buildings[rows[i].id]=nil end
    memory.guid_to_id={}
    for id,r in pairs(memory.buildings) do memory.guid_to_id[r.guid]=id end
end

function M.snapshot(memory,position,node,route,safe,now)
    local result={scope="world_player_session",identity="stable_site_current_guid",buildings={},anchors={},home=copy(memory.home)}
    for _,record in pairs(memory.buildings) do
        local row=copy(record)
        if row.status~="missing" then
            row.route=route(row,false)
            row.arrived=row.status=="observed" and row.route.status=="arrived"
            local ready=row.arrived and safe(position)
            -- Building identity survives movement and planning latency. The
            -- safe home standing point is selected only after actual arrival.
            result.anchors[#result.anchors+1]={id=row.id,building_id=row.id,
                x=ready and position.x or row.x,z=ready and position.z or row.z,
                node_id=ready and node or row.node_id,ready=ready,arrived=row.arrived}
        else row.arrived=false end
        result.buildings[#result.buildings+1]=row
    end
    table.sort(result.buildings,function(a,b) return a.id<b.id end)
    table.sort(result.anchors,function(a,b) return a.id<b.id end)
    if result.home then
        result.home.route=route(result.home,true)
        result.home.arrived=result.home.route.status=="arrived"
    end
    memory.candidates=copy(result.anchors)
    return result
end

function M.actions(places)
    local result={}
    if not places then return result end
    local function travel(id,target,field)
        local route=target.route or {}
        if route.status~="ready" or not route.action then return end
        local a=copy(route.action)
        a.kind="explore"; a.planner_only=true; a[field]=target.id
        if field=="building_id" and (target.prefab=="campfire" or target.prefab=="firepit") then
            a.refuel_approach=true
        elseif field=="building_id" and (target.prefab=="cookpot" or target.prefab=="treasurechest") then
            a.camp_approach=true
        end
        a.description="Travel to remembered "..(field=="home_id" and "home" or target.prefab)
        result[id]=a
    end
    for _,building in ipairs(places.buildings or {}) do
        if building.status~="missing" and not building.arrived then
            travel("visit_"..building.id,building,"building_id")
        end
    end
    for _,anchor in ipairs(places.anchors or {}) do
        if anchor.ready and anchor.building_id==anchor.id then
            result["set_home_"..anchor.id]={kind="set_home",anchor_id=anchor.id,
                x=anchor.x,z=anchor.z,planner_only=true,description="Set home beside building "..anchor.id}
        end
    end
    if places.home and not places.home.arrived then travel("return_home",places.home,"home_id") end
    return result
end

function M.set_home(memory,action,position,node,safe,now)
    for _,anchor in ipairs(memory.candidates or {}) do
        local building=memory.buildings[anchor.building_id]
        if anchor.ready and anchor.building_id==anchor.id and building and building.status=="observed"
            and anchor.id==action.anchor_id and M.home_point_matches(anchor,action)
            and anchor.node_id==node and M.home_point_matches(anchor,position) and safe(anchor) then
            memory.home_revision=memory.home_revision+1
            memory.home={id="home:"..memory.home_revision,anchor_id=anchor.id,
                x=anchor.x,z=anchor.z,node_id=node,building_id=anchor.building_id,set_at=now}
            return true
        end
    end
    return false
end

function M.destination(places,action)
    if not places then return nil end
    if action.home_id then
        return places.home and places.home.id==action.home_id and places.home or nil
    end
    for _,row in ipairs(places.buildings or {}) do
        if row.id==action.building_id then return row end
    end
end
return M
