-- Local server facts only: terrain samples, safe short legs and hazards.
-- No world-wide entity scan, resource search policy or model calls here.
local M={}
local CELL=4
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
    local function clear_hazards(ax,az,x,z,dangers)
        local dx,dz=x-ax,z-az;local length=dx*dx+dz*dz
        for _,h in ipairs(dangers) do
            local t=length>0 and math.max(0,math.min(1,((h.x-ax)*dx+(h.z-az)*dz)/length)) or 0
            if (h.x-ax-t*dx)^2+(h.z-az-t*dz)^2<h.radius*h.radius then return false end
        end
        return true
    end
    local function segment(a,x,z,dangers)
        local ax,az=E.position(a)
        if not finite(x) or not finite(z) or (x-ax)^2+(z-az)^2>20^2 or not clear_hazards(ax,az,x,z,dangers) then return false end
        local steps=math.max(1,math.ceil(math.sqrt((x-ax)^2+(z-az)^2)/2))
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
                if (x-px)^2+(z-pz)^2<=20^2 then
                    local row={gx=gx+dx,gz=gz+dz,land=land(x,z)}
                    if G.TheWorld.Map.GetTileAtPoint then row.tile=G.TheWorld.Map:GetTileAtPoint(x,0,z) end
                    cache.cells[#cache.cells+1]=row
                end
            end end
            local ids={}
            for _,radius in ipairs({16,8}) do for n=0,7 do
                local x,z=grid(gx*CELL+math.cos(n*math.pi/4)*radius)*CELL,grid(gz*CELL+math.sin(n*math.pi/4)*radius)*CELL
                local id=key(grid(x),grid(z))
                if not ids[id] and (x-px)^2+(z-pz)^2>=4^2 and segment(a,x,z,{}) then
                    ids[id]=true;cache.points[#cache.points+1]={id=id,x=x,z=z}
                end
            end end
            a._jev_companion_terrain=cache
        end
        local dangers=hazards(a,entities);local points={}
        for _,p in ipairs(cache.points) do
            if (p.x-px)^2+(p.z-pz)^2<=20^2 and clear_hazards(px,pz,p.x,p.z,dangers) then points[#points+1]=p end
        end
        return {cell_size=CELL,radius=20,resource_radius=15,observed_at=now,cells=cache.cells,points=points,hazards=dangers}
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
