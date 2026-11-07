-- Immutable building-relative area; no current-position anchor or hidden search.
local M={}
local function finite(n) return type(n)=="number" and n==n and math.abs(n)<1000000 end
function M.inside(point,area)
    return type(point)=="table" and finite(point.x) and finite(point.z)
        and (point.x-area.x)^2+(point.z-area.z)^2<=area.radius^2
end
function M.target(raw,s)
    if type(raw)~="table" or type(raw.anchor_id)~="string" or not raw.anchor_id:match("^building:%d+$")
        or not finite(raw.radius) or raw.radius<4 or raw.radius>20 then return nil end
    for _,b in ipairs((s.places or {}).buildings or {}) do
        if b.id==raw.anchor_id and b.status~="missing" and finite(b.x) and finite(b.z) then
            return {anchor_id=b.id,radius=raw.radius,x=b.x,z=b.z}
        end
    end
end
function M.resolve(raw,s)
    local area=M.target(raw,s)
    if area and finite(raw.x) and finite(raw.z) and math.abs(area.x-raw.x)<0.0001 and math.abs(area.z-raw.z)<0.0001 then
        return area
    end
end
function M.live(G,player,area,point)
    if not area then return true end
    if not player or not player:IsValid() then return false end
    local memory=G.TheWorld and G.TheWorld._jev_places
    if memory and memory.owner~=player.GUID then return false end
    local guid=memory and require("jev_places").guid_for_id(memory,area.anchor_id)
        or not memory and tonumber(area.anchor_id:match("^building:(%d+)$"))
    local anchor=G.Ents and G.Ents[guid]
    if not anchor or not anchor:IsValid() or not anchor.Transform then return false end
    local x,_,z=anchor.Transform:GetWorldPosition()
    if math.abs(x-area.x)>=0.0001 or math.abs(z-area.z)>=0.0001 then return false end
    local px,_,pz=player.Transform:GetWorldPosition()
    return M.inside({x=px,z=pz},area) and (not point or M.inside(point,area))
end
return M
