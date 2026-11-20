-- Bounded history of buildings actually observed by this actor. No world scan.
local M={}
local function book(a)
    if not a._jev_companion_places then a._jev_companion_places={rows={},order={}} end
    return a._jev_companion_places
end
function M.share(a,other,source_id)
    local memory=book(a)
    for _,id in ipairs(book(other).order) do
        local source=book(other).rows[id];local current=memory.rows[id]
        if not current or source.last_seen_at>current.last_seen_at then
            local row={};for key,value in pairs(source) do row[key]=value end
            row.shared_from=source.shared_from or source_id
            memory.rows[id]=row
            if not current then memory.order[#memory.order+1]=id end
            if #memory.order>64 then memory.rows[table.remove(memory.order,1)]=nil end
        end
    end
end
function M.new(G,E)
    local P={}
    function P.snapshot(a,structures)
        local memory=book(a);local visible={}
        for _,building in ipairs(structures) do
            local id="building:"..tostring(building.guid)
            local row=memory.rows[id]
            if not row then
                row={id=id,guid=building.guid,prefab=building.prefab}
                memory.rows[id]=row;memory.order[#memory.order+1]=id
                if #memory.order>64 then memory.rows[table.remove(memory.order,1)]=nil end
            else
                for i,key in ipairs(memory.order) do if key==id then table.remove(memory.order,i);break end end
                memory.order[#memory.order+1]=id
            end
            row.x,row.z=building.x,building.z;row.last_seen_at=E.now();row.invalid=nil;row.shared_from=nil
            row.name=(((G.STRINGS or {}).NAMES or {})[string.upper(building.prefab)]) or building.prefab
            visible[id]=true
        end
        local rows,anchors={},{};local x,z=E.position(a)
        for _,id in ipairs(memory.order) do
            local known=memory.rows[id]
            local distance=math.sqrt((known.x-x)^2+(known.z-z)^2)
            local arrived=visible[id]==true and not known.invalid and distance<=3
            local status=known.invalid or (visible[id] and "observed" or "unconfirmed")
            rows[#rows+1]={id=id,guid=known.guid,prefab=known.prefab,name=known.name,x=known.x,z=known.z,
                last_seen_at=known.last_seen_at,shared_from=known.shared_from,distance=math.floor(distance*10+.5)/10,
                status=status,arrived=arrived}
            if status~="missing" and status~="unavailable" then
                anchors[#anchors+1]={id=id,prefab=known.prefab,name=known.name,x=known.x,z=known.z,ready=arrived}
            end
        end
        local home
        if memory.home then
            local anchor=memory.rows[memory.home.anchor_id]
            if anchor and anchor.invalid~="missing" then
                memory.home.x,memory.home.z=anchor.x,anchor.z
                local distance=math.sqrt((anchor.x-x)^2+(anchor.z-z)^2)
                home={id=memory.home.id,anchor_id=memory.home.anchor_id,prefab=anchor.prefab,
                    x=anchor.x,z=anchor.z,distance=math.floor(distance*10+.5)/10,
                    arrived=visible[memory.home.anchor_id]==true and not anchor.invalid and distance<=3}
            else
                memory.home=nil
            end
        end
        return {available=true,scope="companion_observed_buildings",identity="server_guid",
            buildings=rows,anchors=anchors,home=home}
    end
    function P.set_home(a,anchor_id)
        if type(anchor_id)~="string" or #anchor_id>64 then return false,"invalid_anchor_id" end
        local e,reason=P.target(a,anchor_id)
        if not e then return false,reason end
        if E.distance(a,e)>3 then return false,"home_anchor_too_far" end
        local memory=book(a)
        local x,z=E.position(e)
        memory.home_revision=(memory.home_revision or 0)+1
        memory.home={id="home:"..tostring(memory.home_revision),anchor_id=anchor_id,prefab=e.prefab,x=x,z=z}
        return true,memory.home.id
    end
    function P.target(a,id)
        local known=type(id)=="string" and book(a).rows[id]
        if not known then return nil,"building_not_remembered" end
        if known.invalid then return nil,"building_"..known.invalid end
        -- Look up only this previously seen entity, never discover unseen buildings.
        local e=G.Ents[known.guid]
        if not e or not e:IsValid() or e.prefab~=known.prefab then known.invalid="missing";return nil,"building_missing" end
        if e:HasTag("burnt") or e:HasTag("INLIMBO") then
            known.invalid="unavailable";return nil,"building_unavailable"
        end
        if e.components.burnable and e.components.burnable:IsBurning() and e.prefab~="campfire" and e.prefab~="firepit" then return nil,"building_burning" end
        return e
    end
    function P.start(a,q)
        if type(q.args.building_id)~="string" or #q.args.building_id>64 then return false,"invalid_building_id" end
        local e,reason=P.target(a,q.args.building_id)
        if not e then return false,reason end
        local x,z=E.position(e)
        if not E.valid_point(a,x,z,math.huge) then return false,"building_destination_not_land" end
        local ax,az=E.position(a)
        q.travel={x=ax,z=az,progress_at=E.now(),deadline=E.now()+300,last_repath=-math.huge}
        return true
    end
    function P.progress(a,q)
        local e,reason=P.target(a,q.args.building_id)
        if not e then return "failed",reason end
        if E.distance(a,e)<=3 then
            local builder=a.components.builder
            if builder and builder.EvaluateTechTrees then builder:EvaluateTechTrees() end
            return "completed","building_arrived",{building_id=q.args.building_id,arrived=true,distance=E.distance(a,e)}
        end
        local now=E.now();local t=q.travel;local x,z=E.position(a)
        if (x-t.x)^2+(z-t.z)^2>=.5^2 then t.x,t.z,t.progress_at=x,z,now end
        if now>=t.deadline then return "failed","building_travel_timeout" end
        if now-t.progress_at>=20 then return "failed","building_travel_stalled" end
        if not t.next_walk or now>=t.next_walk then
            t.next_walk=now+1
            local motion=E.motion and E.motion(a) or {}
            local recover=now-t.progress_at>=(motion.path_pending and 15 or 5) and now-t.last_repath>=5
            if recover then t.last_repath=now end
            if E.follow(a,e,recover)==false then return "failed","building_path_rejected" end
        end
    end
    return P
end
return M
