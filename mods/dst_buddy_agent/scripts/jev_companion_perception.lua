-- Local server facts only: terrain samples, safe short legs and hazards.
-- No world-wide entity scan, resource search policy or model calls here.
local M={}
local CELL=4
local MIN_LEG_SQ=3^2
local MAX_LEG_SQ=20^2
local ARRIVAL_RADIUS=.5
local function key(gx,gz) return tostring(gx)..":"..tostring(gz) end
local function grid(value) return math.floor(value/CELL+.5) end
local function finite(v) return type(v)=="number" and v==v and math.abs(v)<100000 end
function M.new(G,E)
    local P={}
    local function supported()
        return G.TheWorld and G.TheWorld.Pathfinder and G.TheWorld.Pathfinder.IsClear~=nil
    end
    local function land(x,z)
        local map=G.TheWorld.Map
        return map:IsPassableAtPoint(x,0,z) and map:IsAboveGroundAtPoint(x,0,z)
    end
    local function hazards(a,entities)
        local result={}
        for _,e in ipairs(entities) do
            if e:IsValid() and e~=a then
                local c=e.components or {};local kind
                if e:HasTag("fire") or c.burnable and c.burnable:IsBurning() then kind="fire"
                elseif not e:HasTag("player") and (e:HasTag("hostile") or e:HasTag("monster") or c.combat and c.combat.target==a) then kind="hostile" end
                if kind then
                    local x,z=E.position(e)
                    result[#result+1]={guid=e.GUID,kind=kind,x=x,z=z,radius=kind=="fire" and 4 or 8}
                    if #result>=24 then break end
                end
            end
        end
        return result
    end
    local function clear_hazards(ax,az,x,z,dangers,escaping)
        local dx,dz=x-ax,z-az;local length=dx*dx+dz*dz
        for _,h in ipairs(dangers) do
            local hx,hz=ax-h.x,az-h.z
            local radius_sq=h.radius*h.radius
            local outward=hx*dx+hz*dz
            if hx*hx+hz*hz<radius_sq then
                -- Leave enough room to exit even with the native arrival tolerance.
                local exit_radius=h.radius+ARRIVAL_RADIUS
                if outward<0 or (not escaping and (x-h.x)^2+(z-h.z)^2<exit_radius^2) then return false end
            else
                local t=length>0 and math.max(0,math.min(1,-outward/length)) or 0
                if (hx+t*dx)^2+(hz+t*dz)^2<radius_sq then return false end
            end
        end
        return true
    end
    local function segment(a,x,z,dangers,escaping)
        local ax,az=E.position(a)
        if not finite(x) or not finite(z) then return false end
        local distance_sq=(x-ax)^2+(z-az)^2
        if distance_sq<MIN_LEG_SQ or distance_sq>MAX_LEG_SQ or not clear_hazards(ax,az,x,z,dangers,escaping) then return false end
        local steps=math.ceil(math.sqrt(distance_sq)/2)
        for i=0,steps do if not land(ax+(x-ax)*i/steps,az+(z-az)*i/steps) then return false end end
        return G.TheWorld.Pathfinder:IsClear(ax,0,az,x,0,z,a.components.locomotor.pathcaps)==true
    end
    function P.observe(a,entities)
        if not supported() then return end
        local px,pz=E.position(a);local gx,gz=grid(px),grid(pz);local now=E.now()
        local cache=a._jev_companion_terrain
        if not cache or cache.gx~=gx or cache.gz~=gz or now-cache.at>=2 then
            cache={gx=gx,gz=gz,at=now,cells={},points={}}
            for dx=-4,4 do for dz=-4,4 do
                local x,z=(gx+dx)*CELL,(gz+dz)*CELL
                if (x-px)^2+(z-pz)^2<=MAX_LEG_SQ then
                    local row={gx=gx+dx,gz=gz+dz,land=land(x,z)}
                    if G.TheWorld.Map.GetTileAtPoint then row.tile=G.TheWorld.Map:GetTileAtPoint(x,0,z) end
                    cache.cells[#cache.cells+1]=row
                end
            end end
            local ids={}
            for _,radius in ipairs({16,8,4}) do for n=0,7 do
                local x,z=grid(gx*CELL+math.cos(n*math.pi/4)*radius)*CELL,grid(gz*CELL+math.sin(n*math.pi/4)*radius)*CELL
                local id=key(grid(x),grid(z))
                if not ids[id] and segment(a,x,z,{}) then
                    ids[id]=true;cache.points[#cache.points+1]={id=id,x=x,z=z}
                end
            end end
            a._jev_companion_terrain=cache
        end
        local dangers=hazards(a,entities);local points={};local walkable=0
        for _,p in ipairs(cache.points) do
            local distance_sq=(p.x-px)^2+(p.z-pz)^2
            if distance_sq>=MIN_LEG_SQ and distance_sq<=MAX_LEG_SQ then
                walkable=walkable+1
                if clear_hazards(px,pz,p.x,p.z,dangers) then points[#points+1]=p end
            end
        end
        return {cell_size=CELL,radius=20,resource_radius=15,observed_at=now,cells=cache.cells,points=points,
            hazards=dangers,walkable_points=walkable}
    end
    function P.escape_point(a,threats,entities)
        local data=P.observe(a,entities)
        if not data then return end
        local included={}
        for _,h in ipairs(data.hazards) do included[h.guid]=true end
        for _,h in ipairs(threats) do
            if not included[h.guid] then data.hazards[#data.hazards+1]=h end
        end
        local function clearance(x,z)
            local distance_sq=math.huge
            for _,h in ipairs(threats) do distance_sq=math.min(distance_sq,(x-h.x)^2+(z-h.z)^2) end
            return distance_sq
        end
        local x,z=E.position(a)
        local best,score=nil,clearance(x,z)
        for _,point in ipairs(a._jev_companion_terrain.points) do
            local distance=clearance(point.x,point.z)
            if distance>score and segment(a,point.x,point.z,data.hazards,true) then
                best,score=point,distance
            end
        end
        return best
    end
    function P.arrived(a,x,z)
        local ax,az=E.position(a)
        return (ax-x)^2+(az-z)^2<=ARRIVAL_RADIUS^2
    end
    function P.point(a,args)
        if type(args.point_id)~="string" or not finite(args.x) or not finite(args.z) then return end
        local x,z=E.position(a);local entities=G.TheSim:FindEntities(x,0,z,20,nil,{"INLIMBO","FX","DECOR"})
        local data=P.observe(a,entities)
        if data then for _,p in ipairs(data.points) do
            if p.id==args.point_id and math.abs(p.x-args.x)<.01 and math.abs(p.z-args.z)<.01
                and segment(a,p.x,p.z,data.hazards) then return p end
        end end
    end
    return P
end
return M
