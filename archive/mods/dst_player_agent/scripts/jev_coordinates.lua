-- Observed-only coordinate validation and bounded map export. No hidden map scan.
local M={}
local function finite(v) return type(v)=='number' and v==v and math.abs(v)<=100000 end
function M.point(raw)
    if type(raw)~='table' or not finite(raw.x) or not finite(raw.z) then return nil,'invalid_coordinate' end
    local radius=raw.arrival_radius or 2
    if not finite(radius) or radius<0.5 or radius>4 then return nil,'invalid_arrival_radius' end
    return {x=raw.x,z=raw.z,arrival_radius=radius}
end
function M.validate(raw,memory,size)
    local p,err=M.point(raw); if not p then return nil,err end
    local cell=memory.cells[tostring(math.floor(p.x/size+0.5))..':'..tostring(math.floor(p.z/size+0.5))]
    if not cell or not cell.passable then return nil,'coordinate_not_observed_land' end
    p.node_id=cell.region
    return p
end
function M.segment(memory,size,ax,az,bx,bz)
    local steps=math.max(1,math.ceil(math.sqrt((bx-ax)^2+(bz-az)^2)))
    for i=0,steps do
        local t=i/steps
        local key=tostring(math.floor((ax+(bx-ax)*t)/size+0.5))..':'..tostring(math.floor((az+(bz-az)*t)/size+0.5))
        if not memory.cells[key] or not memory.cells[key].passable then return false end
    end
    return true
end
-- Sixteen real road probes per cell, cached and refreshed under one shared per-tick budget.
M.ROAD_SUBDIVISIONS=4
M.ROAD_SAMPLE_BUDGET=8
function M.sample_road(cell,size,probe,now,budget)
    if cell._road_passable==cell.passable and cell._road_sample_at
        and now>=cell._road_sample_at and now-cell._road_sample_at<30 then return cell._road_mask end
    if not cell.passable then
        cell._road_passable=false;cell._road_mask=0;cell._road_sample_at=now;return 0
    end
    if not probe or not budget or budget.remaining<=0 then return cell._road_mask end
    budget.remaining=budget.remaining-1
    local mask=0;local step=size/M.ROAD_SUBDIVISIONS
    for iz=0,M.ROAD_SUBDIVISIONS-1 do for ix=0,M.ROAD_SUBDIVISIONS-1 do
        local x=cell.x-size/2+(ix+.5)*step
        local z=cell.z-size/2+(iz+.5)*step
        local value=probe(x,z)
        if type(value)~='boolean' then return cell._road_mask end
        if value then mask=mask+2^(iz*M.ROAD_SUBDIVISIONS+ix) end
    end end
    cell._road_mask=mask;cell._road_sample_at=now;cell._road_passable=true
    return mask
end
function M.observe(memory,cell,road,road_mask)
    memory.map_rows=memory.map_rows or {};memory.map_order=memory.map_order or {};memory.map_first=memory.map_first or 1
    local key=tostring(cell.gx)..':'..tostring(cell.gz)
    local row={cell.gx,cell.gz,cell.passable and 1 or 0,cell.biome or 'unknown',cell.region or '',road and 1 or 0,road_mask}
    local old=memory.map_rows[key];local changed=not old
    if old then for i=1,7 do if old[i]~=row[i] then changed=true end end end
    if not changed then return end
    if not old then memory.map_order[#memory.map_order+1]=key end
    memory.map_rows[key]=row;memory.map_revision=(memory.map_revision or 0)+1
    while #memory.map_order-memory.map_first+1>4096 do
        memory.map_rows[memory.map_order[memory.map_first]]=nil;memory.map_first=memory.map_first+1
        memory.map_truncated=true
    end
    -- Keep the FIFO bookkeeping bounded as well as the exported cells.
    if memory.map_first>4096 then
        local order={};for i=memory.map_first,#memory.map_order do order[#order+1]=memory.map_order[i] end
        memory.map_order=order;memory.map_first=1
    end
end
function M.snapshot(memory,size)
    if not memory.map_snapshot or memory.map_snapshot.version~=(memory.map_revision or 0) then
        local rows={};for k,v in pairs(memory.map_rows or {}) do rows[k]=v end
        memory.map_snapshot={version=memory.map_revision or 0,cell_size=size,cells=rows,truncated=memory.map_truncated or false}
    end
    local result={};for k,v in pairs(memory.map_snapshot) do result[k]=v end
    result.road_subdivisions=M.ROAD_SUBDIVISIONS
    local sources={};for _,e in pairs(memory.sources or {}) do if e.available then sources[#sources+1]=e end end
    table.sort(sources,function(a,b) return a.last_seen>b.last_seen or a.last_seen==b.last_seen and a.guid<b.guid end)
    result.sources={}
    for i=1,math.min(128,#sources) do local e=sources[i]
        result.sources[i]={x=e.x,z=e.z,prefab=e.prefab,last_seen=e.last_seen}
    end
    return result
end
function M.action(target,route)
    if route.status~='ready' or not route.action then return nil,route.reason or 'coordinate_route_unavailable' end
    local a={};for k,v in pairs(route.action) do a[k]=v end
    a.kind='explore';a.planner_only=true;a.coordinate_target={x=target.x,z=target.z,arrival_radius=target.arrival_radius}
    return a
end
return M
