-- Session-local memory of OBSERVED entities only. No hidden entity/map queries.
local M = {}
local function distance(a,b) return math.sqrt((a.x-b.x)^2+(a.z-b.z)^2) end
function M.observe(memory, position, nearby, region_at, now, radius, complete)
    memory.sources=memory.sources or {}
    local seen={}
    for _,e in ipairs(nearby) do
        seen[e.guid]=true
        local useful=e.pickable~=nil
        for _,tag in ipairs(e.tags or {}) do
            useful=useful or tag=="_inventoryitem" or tag=="CHOP_workable" or tag=="MINE_workable"
        end
        if useful and e.guid and e.dx and e.dz then
            local x,z=position.x+e.dx,position.z+e.dz
            memory.sources[e.guid]={guid=e.guid,prefab=e.prefab,x=x,z=z,node_id=region_at(x,z),
                pickable=e.pickable,tags=e.tags,work_required=e.work_required,
                available=e.pickable~=false,last_seen=now}
        end
    end
    local ordered={}
    for id,e in pairs(memory.sources) do
        -- A capped scan cannot prove that an absent GUID has disappeared.
        if complete and not seen[id] and distance(position,e)<radius-1 then
            memory.sources[id]=nil
        else ordered[#ordered+1]=e end
    end
    table.sort(ordered,function(a,b) return a.last_seen>b.last_seen or a.last_seen==b.last_seen and a.guid<b.guid end)
    for i=513,#ordered do memory.sources[ordered[i].guid]=nil end
    return seen
end
function M.candidates(memory,position,node,seen)
    local result={}
    for _,e in pairs(memory.sources or {}) do
        -- Visible work targets beyond six units need approach before native work.
        if e.available and e.node_id==node and (not seen[e.guid] or e.work_required and distance(position,e)>5.5) then
            result[#result+1]={guid=e.guid,prefab=e.prefab,pickable=e.pickable,tags=e.tags,
                work_required=e.work_required,dx=e.x-position.x,dz=e.z-position.z,
                distance=distance(position,e),x=e.x,z=e.z}
        end
    end
    table.sort(result,function(a,b) return a.distance<b.distance or a.distance==b.distance and a.guid<b.guid end)
    -- Keep resource types diverse, so trees cannot crowd out a remembered gold source.
    local bounded,types={},{}
    for _,e in ipairs(result) do if not types[e.prefab] then
        types[e.prefab]=true; bounded[#bounded+1]=e
        if #bounded==24 then break end
    end end
    return bounded
end
return M
