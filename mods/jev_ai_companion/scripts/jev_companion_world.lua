local M={}
local Actions=require("jev_companion_actions")
local Perception=require("jev_companion_perception")
local Storage=require("jev_companion_storage")
local Places=require("jev_companion_places")
local Presentation=require("jev_companion_presentation")
local SAFE_PICK={grass=true,sapling=true,berrybush=true,berrybush2=true,berrybush_juicy=true,carrot_planted=true,reeds=true}
local function round(v) return math.floor(v*100+.5)/100 end
function M.new(G,reply,validate_text,encoded_size,trace)
    local E={now=G.GetTime,reply=reply,validate_text=validate_text,encoded_size=encoded_size,trace=trace}
    function E.position(a) local x,_,z=a.Transform:GetWorldPosition();return x,z end
    function E.distance(a,b) return math.sqrt(a:GetDistanceSqToInst(b)) end
    function E.dead(a) return a.components.health:IsDead() end
    function E.stop(a) a:ClearBufferedAction();a.components.locomotor:Clear();a.components.locomotor:Stop() end
    function E.set_name(a,name) return Presentation.set_name(G,a,name) end
    function E.activate(a)
        if a.entity and a.entity.SetCanSleep then
            a.entity:SetCanSleep(false);a._jev_companion_keepawake=true
        end
        -- Vanilla GIVETOPLAYER requires the receiver's own inventory to be open.
        -- Wilson already has the native non-destructive player trader component.
        local inv=a.components.inventory
        if inv and inv.Open and not inv:IsOpenedBy(a) then
            a._jev_companion_opened_inventory=true;inv:Open()
        end
    end
    function E.deactivate(a)
        if a:IsValid() and a._jev_companion_keepawake and a.entity and a.entity.SetCanSleep then a.entity:SetCanSleep(true) end
        a._jev_companion_keepawake=nil
        if a:IsValid() and a._jev_companion_opened_inventory and a.components.inventory.Close then a.components.inventory:Close() end
        a._jev_companion_opened_inventory=nil
    end
    function E.motion(a)
        local loco=a.components.locomotor;local path=loco.path or {}
        local asleep;if a.IsAsleep then asleep=a:IsAsleep() end
        return {asleep=asleep,
            state=a.sg and a.sg.currentstate and a.sg.currentstate.name or nil,
            moving=loco.WantsToMoveForward and loco:WantsToMoveForward() or false,
            updating=loco.isupdating==true,path_pending=path.handle~=nil,path_steps=path.steps and #path.steps or 0}
    end
    function E.move(a,x,z) a.components.locomotor:GoToPoint(G.Vector3(x,0,z),nil,true);return true end
    function E.follow(a,owner,recover)
        E.activate(a)
        if a.IsAsleep and a:IsAsleep() then return true,"waking" end
        if recover then E.stop(a) end -- Clear failed searches and the native last-destination cache.
        a.components.locomotor:GoToEntity(owner,nil,true)
        return true
    end
    function E.arrived(a,x,z) local ax,az=E.position(a);return (ax-x)^2+(az-z)^2<=2.5^2 end
    function E.valid_point(a,x,z,radius)
        local ax,az=E.position(a)
        return (ax-x)^2+(az-z)^2<=(radius or 20)^2 and G.TheWorld.Map:IsPassableAtPoint(x,0,z) and G.TheWorld.Map:IsAboveGroundAtPoint(x,0,z)
    end
    function E.unmap(a)
        local icon=a._jev_companion_mapicon
        if icon and icon:IsValid() then icon:Remove() end
        a._jev_companion_mapicon=nil;a._jev_companion_mapplayer=nil
    end
    function E.map_bind(player,a)
        local icon=a._jev_companion_mapicon
        if icon and icon:IsValid() and a._jev_companion_mapplayer==player then return true end
        E.unmap(a)
        icon=G.SpawnPrefab and G.SpawnPrefab("globalmapiconnamed")
        if not icon then return false,"map_icon_unavailable" end
        if not icon.TrackEntity or not icon.Network or not icon.Network.SetClassifiedTarget or not icon.AddComponent then
            icon:Remove();return false,"map_icon_not_supported"
        end
        -- Match vanilla private map markers, including listen-server restrictions.
        -- The marker remains networked beyond the actor's client visibility range.
        icon:AddTag("globalmapicon_player")
        icon.MiniMapEntity:SetPriority(11)
        if icon._target_displayname then
            icon._target_displayname:set("伙伴："..(a._jev_companion_name or (a.GetDisplayName and a:GetDisplayName() or "Wilson")))
        end
        icon:TrackEntity(a,"player_"..player.userid,"wilson.png")
        icon.Network:SetClassifiedTarget(player)
        -- Use the private marker as the revealing source. Its transform already
        -- follows the companion, and removing it also stops the native timer.
        -- Do not alter any maprevealer component installed on the actor by other mods.
        icon:AddComponent("maprevealer")
        icon.components.maprevealer.revealperiod=1
        icon.components.maprevealer:SetPrivateOwner(player)
        a._jev_companion_mapicon=icon;a._jev_companion_mapplayer=player
        return true
    end
    function E.find_buddy(player)
        local x,z=E.position(player);local chosen,distance
        for _,a in ipairs(G.TheSim:FindEntities(x,0,z,20,{"player"},{"INLIMBO","playerghost"})) do
            local c=a.components or {}
            if a~=player and a.prefab=="wilson" and (not a.userid or a.userid=="") and not a.brain
                and not a._jev_companion_owner and not a._jev_probe_owner
                and (not a._jev_companion_created_for or a._jev_companion_created_for==player.userid)
                and a.sg and c.locomotor and c.inventory and c.health and not E.dead(a) then
                local d=E.distance(a,player);if not distance or d<distance then chosen,distance=a,d end
            end
        end
        return chosen
    end
    function E.spawn_buddy(player)
        if not player.components.health or player.components.health:IsDead() or player:HasTag("playerghost") then
            return nil,"owner_not_alive"
        end
        local px,pz=E.position(player)
        local sx,sz
        for _,radius in ipairs({2,3,4}) do
            for n=0,7 do
                local x,z=px+math.cos(n*math.pi/4)*radius,pz+math.sin(n*math.pi/4)*radius
                local clear=true
                for step=0,8 do
                    local tx,tz=px+(x-px)*step/8,pz+(z-pz)*step/8
                    if not G.TheWorld.Map:IsPassableAtPoint(tx,0,tz) or not G.TheWorld.Map:IsAboveGroundAtPoint(tx,0,tz) then clear=false;break end
                end
                if clear then
                    for _,e in ipairs(G.TheSim:FindEntities(x,0,z,4,nil,{"INLIMBO","FX","DECOR"})) do
                        local bx,bz=E.position(e)
                        local physics=e.GetPhysicsRadius and e:GetPhysicsRadius(0) or (e:HasTag("player") and .5 or 0)
                        if e:HasTag("fire") or ((e.components or {}).burnable and e.components.burnable:IsBurning())
                            or physics>0 and (bx-x)^2+(bz-z)^2<(physics+.6)^2 then clear=false;break end
                    end
                end
                if clear then sx,sz=x,z;break end
            end
            if sx then break end
        end
        if not sx then return nil,"no_safe_spawn_land" end
        local a=G.SpawnPrefab("wilson")
        if not a then return nil,"spawn_failed" end
        local c=a.components or {}
        if a.userid and a.userid~="" or not a.sg or not c.locomotor or not c.inventory or not c.health then
            if a.Remove then a:Remove() end
            return nil,"invalid_spawned_companion"
        end
        a.Transform:SetPosition(sx,0,sz)
        return a
    end
    local function inventory(a)
        local rows,counts={},{}
        for _,item in ipairs(a.components.inventory:FindItems(function() return true end)) do
            local stack=item.components.stackable;local count=stack and stack:StackSize() or 1
            counts[item.prefab]=(counts[item.prefab] or 0)+count
            if #rows<32 then rows[#rows+1]={prefab=item.prefab,count=count,guid=item.GUID} end
        end
        return rows,counts
    end
    function E.count(a,prefab) local _,counts=inventory(a);return counts[prefab] or 0 end
    local function target(a,e)
        if e==a or not e:IsValid() or e:HasTag("INLIMBO") or e:HasTag("heavy") or e:HasTag("fire") or E.distance(a,e)>15 then return end
        local c=e.components or {}
        if c.burnable and c.burnable:IsBurning() then return end
        local kind,product
        if c.inventoryitem and c.inventoryitem.canbepickedup and not c.inventoryitem:IsHeld() then kind,product="pickup",e.prefab
        elseif SAFE_PICK[e.prefab] and c.pickable and c.pickable:CanBePicked() and c.pickable.caninteractwith and not c.pickable:IsStuck() then kind,product="pick",c.pickable.product end
        if not kind or type(product)~="string" then return end
        local x,z=E.position(e)
        return {guid=e.GUID,prefab=e.prefab,product=product,kind=kind,distance=round(E.distance(a,e)),x=round(x),z=round(z)}
    end
    function E.collect_target(a,guid) local e=G.Ents[guid];return e and target(a,e) end
    function E.collect(a,row)
        local action=row.kind=="pickup" and G.ACTIONS.PICKUP or G.ACTIONS.PICK
        a.components.locomotor:PushAction(G.BufferedAction(a,G.Ents[row.guid],action),true);return true
    end
    local native=Actions.new(G,E)
    local perception=Perception.new(G,E)
    local storage=Storage.new(G,E)
    local places=Places.new(G,E)
    E.visit_start=places.start;E.visit_progress=places.progress
    function E.begin(a,q,r) return (Storage.ops[q.op] and storage.start or native.start)(a,q,r) end
    function E.progress(a,q,r) return (Storage.ops[q.op] and storage.progress or native.progress)(a,q,r) end
    E.query=native.query
    E.search_point=perception.point
    function E.search_found(a,products)
        local wanted={};for _,name in ipairs(products) do wanted[name]=true end
        local x,z=E.position(a)
        for _,e in ipairs(G.TheSim:FindEntities(x,0,z,15,nil,{"INLIMBO","FX","DECOR"})) do
            local row=target(a,e)
            if row and wanted[row.product] then return true end
            local work=native.work_target(a,e.GUID)
            if work then for _,name in ipairs(work.products) do if wanted[name] then return true end end end
        end
        return false
    end
    function E.say(a,text,r)
        if not a.components.talker then return end
        local token,revision=r.token,r.revision;local lines,chars={},{}
        a._jev_companion_speech_serial=(a._jev_companion_speech_serial or 0)+1
        local serial=a._jev_companion_speech_serial
        for c in text:gmatch("[%z\1-\127\194-\244][\128-\191]*") do
            chars[#chars+1]=c;if #chars==60 then lines[#lines+1]={text=table.concat(chars),count=#chars};chars={} end
        end
        if #chars>0 then lines[#lines+1]={text=table.concat(chars),count=#chars} end
        local delay=0
        for _,line in ipairs(lines) do
            local duration=math.min(8,math.max(3,1.5+line.count*.09))
            local message=line.text
            a:DoTaskInTime(delay,function()
                if a:IsValid() and r.token==token and r.revision==revision and a._jev_companion_speech_serial==serial
                    and not E.dead(a) then
                    -- No talking animation: feedback must not interrupt walking or working.
                    -- Keep native networking enabled so nearby clients see the same overhead bubble.
                    a.components.talker:Say(message,duration,true,true,false)
                end
            end)
            delay=delay+duration+.15
        end
    end
    function E.snapshot(player,a,r)
        local px,pz=E.position(player)
        local state={owner={guid=player.GUID,position={x=round(px),z=round(pz)}},bound=a~=nil,revision=r.revision or 0,
            token=r.token,mode=r.mode or "stay",sampled_at=G.GetTime(),capabilities={"follow","stay","move","collect","stop","say"}}
        if not a or not a:IsValid() then state.bound=false;return state end
        local x,z=E.position(a);local c=a.components;local items,counts=inventory(a);local nearby={}
        local entities=G.TheSim:FindEntities(x,0,z,20,nil,{"INLIMBO","FX","DECOR"})
        for _,e in ipairs(entities) do local row=target(a,e);if row then nearby[#nearby+1]=row end end
        table.sort(nearby,function(l,r) return l.distance<r.distance end)
        local selected,seen,rest={},{},{}
        for _,row in ipairs(nearby) do
            if not seen[row.product] then selected[#selected+1]=row;seen[row.product]=true else rest[#rest+1]=row end
        end
        for _,row in ipairs(rest) do if #selected>=48 then break end;selected[#selected+1]=row end
        while #selected>48 do table.remove(selected) end
        state.buddy={guid=a.GUID,prefab=a.prefab,position={x=round(x),z=round(z)},inventory=counts,items=items,
            owner_distance=round(E.distance(a,player)),health=c.health.currenthealth,health_max=c.health.maxhealth,dead=E.dead(a),
            hunger=c.hunger and c.hunger.current,sanity=c.sanity and c.sanity.current}
        state.nearby=selected;state.resources_complete=#nearby<=48
        state.action=r.active and {id=r.active.id,op=r.active.op,status="running",
            work=r.active.op=="work" and r.active.args.work or nil} or nil
        state.perception=perception.observe(a,entities)
        if state.perception then state.capabilities[#state.capabilities+1]="search" end
        state.map_tracking=a._jev_companion_mapicon~=nil and a._jev_companion_mapicon:IsValid() or false
        state.map_sharing=state.map_tracking and a._jev_companion_mapicon.components.maprevealer~=nil
            and a._jev_companion_mapicon.components.maprevealer:GetPrivateOwner()==player or false
        state.map_error=r.map_error
        state.motion=E.motion(a)
        state.follow_state=r.follow_state
        for name,value in pairs(native.snapshot(a,entities)) do state[name]=value end
        state.places=places.snapshot(a,state.structures)
        state.capabilities[#state.capabilities+1]="visit_building"
        for name,value in pairs(storage.snapshot(a,entities,r)) do state[name]=value end
        for _,name in ipairs({"storage","give_owner","drop_item"}) do state.capabilities[#state.capabilities+1]=name end
        if c.trader then state.capabilities[#state.capabilities+1]="receive_item" end
        state.capabilities[#state.capabilities+1]="equip";state.capabilities[#state.capabilities+1]="unequip";state.capabilities[#state.capabilities+1]="work"
        if c.builder then for _,name in ipairs({"recipes","craft","place"}) do state.capabilities[#state.capabilities+1]=name end end
        local w=G.TheWorld.state;state.world={day=(w.cycles or 0)+1,phase=w.phase,time=w.time}
        return state
    end
    return E
end
return M
