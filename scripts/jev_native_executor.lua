-- Native, snapshot-verified execution. No network, keyboard or model policy.
local M = {}
local products={grass="cutgrass",sapling="twigs",sapling_moon="twigs",berrybush="berries",
    berrybush2="berries",berrybush_juicy="berries_juicy",carrot_planted="carrot",flower="petals"}
local loose={twigs=true,cutgrass=true,flint=true,log=true,rocks=true,goldnugget=true,nitre=true,
    pinecone=true,acorn=true,charcoal=true,marble=true,moonrocknugget=true,livinglog=true,
    berries=true,berries_juicy=true,carrot=true,seeds=true,petals=true,ice=true,redgem=true,bluegem=true,
    purplegem=true,thulecite=true,thulecite_pieces=true,berries_cooked=true,berries_juicy_cooked=true,
    carrot_cooked=true,seeds_cooked=true,meat=true,smallmeat=true,cookedmeat=true,cookedsmallmeat=true}
local cooked={cookedmeat=true,cookedsmallmeat=true,berries_cooked=true,berries_juicy_cooked=true,
    carrot_cooked=true,seeds_cooked=true}
local raw={meat=true,smallmeat=true,berries=true,berries_juicy=true,carrot=true,seeds=true}
local safe_raw={berries=true,berries_juicy=true,carrot=true,seeds=true}
local drops={chop_nearest_tree={log=true,pinecone=true,acorn=true,charcoal=true,livinglog=true,twigs=true},
    mine_nearest_rock={rocks=true,flint=true,nitre=true,goldnugget=true,marble=true,moonrocknugget=true,
        ice=true,redgem=true,bluegem=true,purplegem=true,thulecite=true,thulecite_pieces=true}}
local mine_products={rock1={rocks=true,flint=true,nitre=true},rock2={rocks=true,flint=true,goldnugget=true},
    rock_flintless={rocks=true},rock_flintless_med={rocks=true},rock_flintless_low={rocks=true}}
local log_trees={evergreen=true,evergreen_sparse=true,deciduoustree=true,twiggytree=true}
local function finite(x) return type(x)=="number" and x==x and math.abs(x)<1000000 end
local function count(s,prefab,hands)
    local inv=(s.player or {}).inventory or {}; local n=0
    for _,part in ipairs(hands and {"equipped"} or {"items","equipped"}) do
        for _,item in ipairs(inv[part] or {}) do if item.prefab==prefab then n=n+(item.count or 1) end end
    end
    return n
end
local function total(s,set)
    local n=0; for prefab in pairs(set) do n=n+count(s,prefab) end; return n
end
local function fire(s,cooker)
    for _,e in ipairs(s.nearby or {}) do
        if (e.prefab=="campfire" or e.prefab=="firepit") and e.distance<=(cooker and 6 or 8) then
            local tags={}; for _,t in ipairs(e.tags or {}) do tags[t]=true end
            if tags.fire and (not cooker or tags.cooker) then return true end
        end
    end
    return false
end
local function threat(s)
    for _,e in ipairs(s.nearby or {}) do if e.activeThreat then return true end end
    return false
end
local function collectable(e)
    if products[e.prefab] then return e.pickable==true end
    if not loose[e.prefab] then return false end
    for _,t in ipairs(e.tags or {}) do if t=="_inventoryitem" or t=="pickable" then return true end end
    return false
end
local function distance(a,b) return math.sqrt((a.x-b.x)^2+(a.z-b.z)^2) end
local function uses(s,prefab,maximum)
    local result=0; local inv=(s.player or {}).inventory or {}
    for _,part in ipairs({"items","equipped"}) do for _,i in ipairs(inv[part] or {}) do
        if i.prefab==prefab and finite(i.durability_percent) then
            local encoded=math.floor(i.durability_percent*100+0.5)
            for n=0,maximum do if math.floor(n/maximum*100+0.5)==encoded then
                result=math.max(result,n); break
            end end
        end
    end end
    return result
end
local function types(s)
    local set={}; for _,e in ipairs(s.nearby or {}) do
        if collectable(e) and e.collect_path and e.collect_path.status=="ready" then
            set[products[e.prefab] or e.prefab]=true
        end
    end; return set
end
local function different(a,b)
    for k in pairs(a) do if not b[k] then return true end end
    for k in pairs(b) do if not a[k] then return true end end; return false
end

function M.available(s)
    local result={}; local v=(s.player or {}).vitals or {}
    if v.dead then return result end
    if threat(s) then
        local best,score; local p=s.player.position
        for _,route in ipairs((s.navigation or {}).escape_routes or {}) do
            local gain=math.huge
            for _,e in ipairs(s.nearby or {}) do if e.activeThreat then
                local dx,dz=route.dx*route.distance-(e.dx or 0),route.dz*route.distance-(e.dz or 0)
                gain=math.min(gain,math.sqrt(dx*dx+dz*dz)-(e.distance or 0))
            end end
            if finite(route.dx) and finite(route.dz) and (not score or gain>score) then best,score=route,gain end
        end
        if best then result.flee_from_nearest_hostile={kind="flee_from_nearest_hostile",
            x=p.x+best.dx*best.distance,z=p.z+best.dz*best.distance,
            description="Flee using a passable route that increases distance from observed targeted hostiles."} end
        return result
    end
    local night=(s.world or {}).phase=="night"
    local torch=count(s,"torch",true)>0; local lit=fire(s)
    local dark=night and not torch and not lit
    local crafting=s.crafting or {}
    local function add(id,kind,description,spec)
        local a=spec or {}; a.kind=kind; a.description=description; result[id]=a
    end
    add("wait","wait","Wait for one second; does not provide light or safety.")
    if crafting.torch and count(s,"torch")==0 then add("craft_torch","craft_torch","Craft one torch; preserve fuel outside night.") end
    if night and not lit and not torch and count(s,"torch")>0 then
        add("equip_torch","equip_torch","Equip a carried torch for night lighting.")
    end
    if torch and (not night or lit) then add("unequip_torch","unequip_torch","Store the torch to preserve fuel.") end
    if total(s,cooked)+total(s,safe_raw)>0 or (fire(s,true) and total(s,raw)>0) then
        add("eat_safe_food","eat_safe_food","Eat safe food, cooking raw food first when a fire is available.")
    end
    if fire(s,true) and total(s,raw)>0 then add("cook_food","cook_food","Cook one carried raw food item.") end
    if night and not lit and crafting.campfire then add("build_campfire","build_campfire","Build and place a campfire.") end
    if (v.hunger or 0)/math.max(v.hunger_max or 1,1)<0.30 and result.eat_safe_food then
        local emergency={eat_safe_food=result.eat_safe_food}
        if dark then for _,id in ipairs({"equip_torch","craft_torch","build_campfire"}) do emergency[id]=result[id] end end
        return emergency
    end
    if dark then return result end
    if crafting.sciencemachine then add("build_sciencemachine","build_sciencemachine","Build and place a Science Machine.") end
    for _,tool in ipairs({"axe","pickaxe"}) do if crafting[tool] and count(s,tool)==0 then
        add("craft_"..tool,"craft_"..tool,"Craft one "..tool..".")
    end end
    if not night or torch then
        local nearest={}
        for _,e in ipairs(s.nearby or {}) do
            if collectable(e) and e.collect_path and e.collect_path.status=="ready" then
                if not nearest[e.prefab] or e.distance<nearest[e.prefab].distance then nearest[e.prefab]=e end
            end
        end
        for prefab,e in pairs(nearest) do
            add("collect_"..prefab,"collect","Collect the nearest reachable "..prefab..".",
                {prefab=prefab,guid=e.guid,product=products[prefab] or prefab})
        end
        local nav=s.navigation or {}
        local function navigation(id,route,mode,node)
            if route.status=="ready" and route.leg and finite(route.leg.x) and finite(route.leg.z) then
                add(id,"explore","Continue native navigation until a resource or safety event.",
                    {mode=mode,node_id=node,x=route.leg.x,z=route.leg.z,
                        arrival=route.leg.arrival_distance or (mode=="graph" and 0.2 or 1)})
            end
        end
        navigation("explore_current_region",nav,"region")
        navigation("explore_other_region",nav.search or {},"search")
        for _,r in ipairs((nav.graph or {}).routes or {}) do
            navigation("travel_region_"..tostring(r.node_id),r,"graph",r.node_id)
        end
        for _,r in ipairs(nav.resource_routes or {}) do
            local id="navigate_resource_"..tostring(r.guid)
            navigation(id,r,"resource",r.node_id)
            if result[id] then
                result[id].prefab=r.prefab; result[id].resource_guid=r.guid; result[id].distance=r.distance
            end
        end
    end
    if not night then
        for _,w in ipairs({{"chop_nearest_tree","CHOP_workable","axe",100},
                           {"mine_nearest_rock","MINE_workable","pickaxe",33}}) do
            local best; local remaining=uses(s,w[3],w[4])
            for _,e in ipairs(s.nearby or {}) do
                local tag=false; for _,t in ipairs(e.tags or {}) do if t==w[2] then tag=true end end
                if tag and e.distance<=6 and e.work_required and remaining>=e.work_required
                    and e.collect_path and e.collect_path.status=="ready" then
                    if not best or e.distance<best.distance then best=e end
                end
            end
            if best then add(w[1],w[1],"Complete one work target, then collect its nearby drops.",
                {guid=best.guid,prefab=best.prefab}) end
        end
    end
    return result
end

function M.new(adapter)
    local self={active=nil,report=nil,board=nil,board_revision=-1,board_paused=false,
        events={},event_sequence=0,paused={},last_now=0,visits_done={},science_machine_built=false,
        command_ids={},command_order={}}
    local function action_key(choice,a) return choice..":"..tostring(a.guid or a.node_id or "") end
    local function remember_command(id)
        if self.command_ids[id] then return end
        self.command_ids[id]=true
        self.command_order[#self.command_order+1]=id
        if #self.command_order>256 then
            self.command_ids[table.remove(self.command_order,1)]=nil
        end
    end
    function self:available(s,now)
        local result=M.available(s)
        for id,a in pairs(result) do
            if (self.paused[action_key(id,a)] or 0)>(now or self.last_now) then result[id]=nil end
        end
        return result
    end
    function self:finish(status,reason)
        if not self.active then return end
        if adapter.now then self.last_now=adapter.now() end
        adapter.stop()
        local run=self.active
        if status=="completed" and run.action.kind=="build_sciencemachine" then self.science_machine_built=true end
        self.report={action_id=run.id,choice=run.choice,status=status,reason=reason or "",
            started_at=run.started,confirmed_started_at=run.confirmed_started_at,
            start_source=run.start_source,finished_at=self.last_now,
            previous_action_id=run.previous_action_id,handoff_gap_ms=run.handoff_gap_ms}
        self.event_sequence=self.event_sequence+1
        self.events[#self.events+1]={sequence=self.event_sequence,action_id=run.id,choice=run.choice,
            kind=run.action.kind,status=status,reason=reason or "",board_task_id=run.task_id,
            started_at=run.started,confirmed_started_at=run.confirmed_started_at,start_source=run.start_source,
            finished_at=self.last_now,previous_action_id=run.previous_action_id,handoff_gap_ms=run.handoff_gap_ms}
        if #self.events>64 then table.remove(self.events,1) end
        self.last_finished=self.events[#self.events]
        if status=="failed" or reason=="movement_stalled" or reason=="target_path_unavailable"
            or reason=="target_changed" or reason=="navigation_path_became_unsafe" then
            self.paused[action_key(run.choice,run.action)]=self.last_now+15
        end
        if self.active.board and status~="completed" then self.board_paused=true end
        self.active=nil
    end
    function self:cancel(id)
        if self.active and self.active.id~=id then return end
        remember_command(id)
        if self.active then self:finish("cancelled","requested")
        elseif not self.report or self.report.action_id~=id then
            self.report={action_id=id,status="cancelled",reason="cancelled_before_start"}
        end
    end
    function self:interact_target(run,choice,action,after)
        local accepted=adapter.issue(choice,action)
        if action.kind=="collect" and accepted==false then
            -- The native picker temporarily has no action during work/pickup
            -- recovery and remote-control cooldown. Preserve identity and wait.
            local pending=run.collection_retry or {choice=choice,action=action,after=after,
                deadline=self.last_now+3}
            pending.next_try=self.last_now+0.2
            run.collection_retry=pending; run.stage="collection_retry"
        else
            run.collection_retry=nil; run.stage=after
        end
    end
    function self:issue_target(run,s,choice,action,after)
        for _,e in ipairs(s.nearby or {}) do if e.guid==action.guid then
            if e.distance<=1.2 then self:interact_target(run,choice,action,after); return end
            local path=e.collect_path or {}; local point=path.waypoint or {}
            if path.status~="ready" or not finite(point.x) or not finite(point.z) then
                self:finish("interrupted","target_path_unavailable"); return
            end
            run.approach={guid=action.guid,choice=choice,action=action,after=after,waypoint=point,
                last_position=s.player.position,progress_at=run.started}
            run.stage="approach"; adapter.issue("walk",{kind="explore",x=point.x,z=point.z}); return
        end end
        self:finish("failed","target_missing")
    end
    function self:start(command,s,now,board)
        self.last_now=now
        if self.active then return false end
        if self.command_ids[command.action_id] then return false end
        remember_command(command.action_id)
        local function reject(reason)
            self.report={action_id=command.action_id,choice=command.choice,status="failed",reason=reason}
            return false
        end
        local requested=command.action or {}
        if (s.player.vitals or {}).dead then return reject("player_dead") end
        if threat(s) and command.choice~="flee_from_nearest_hostile" then
            return reject("targeted_by_hostile")
        end
        local a=self:available(s,now)[command.choice]
        if not a then
            if command.choice=="flee_from_nearest_hostile" then
                return reject(threat(s) and "no_safe_escape_route" or "threat_cleared")
            end
            if requested.guid then
                local target
                for _,e in ipairs(s.nearby or {}) do if e.guid==requested.guid then target=e end end
                if not target then return reject("target_missing") end
                if requested.kind=="collect" and not collectable(target) then return reject("target_not_collectable") end
                if not target.collect_path or target.collect_path.status~="ready" then return reject("target_path_unavailable") end
            end
            return reject("action_unavailable")
        end
        if command.action and (a.guid~=requested.guid or a.node_id~=requested.node_id
            or (requested.kind and a.kind~=requested.kind) or (requested.prefab and a.prefab~=requested.prefab)
            or (requested.mode and a.mode~=requested.mode)) then
            return reject("target_changed")
        end
        -- Use the current route/recipe facts, never stale request coordinates.
        -- Target GUID/node identity is preserved; this is not a new AI choice.
        local position=s.player.position
        self.active={id=command.action_id,choice=command.choice,action=a,before=s,started=now,
            deadline=now+(drops[a.kind] and 50 or a.kind=="explore" and 120 or 12),
            origin={x=position.x,z=position.z},resources=types(s),board=board,task_id=command.task_id,
            last_position=position,progress_at=now,
            known_nodes=((s.navigation or {}).graph or {}).known_nodes or 0,
            region=((s.navigation or {}).graph or {}).current_node}
        local previous=self.last_finished
        if previous and previous.finished_at<=now then
            self.active.previous_action_id=previous.action_id
            self.active.handoff_gap_ms=(now-previous.finished_at)*1000
        end
        self.report={action_id=command.action_id,choice=command.choice,status="running",reason="",
            started_at=now,previous_action_id=self.active.previous_action_id,handoff_gap_ms=self.active.handoff_gap_ms}
        self.active.work_sequence=(s.work or {}).sequence or 0
        self.active.preexisting={}
        for _,e in ipairs(s.nearby or {}) do self.active.preexisting[e.guid]=true end
        if a.kind=="collect" or drops[a.kind] then
            self:issue_target(self.active,s,command.choice,a,nil)
        elseif a.kind=="eat_safe_food" and fire(s,true) and total(s,raw)>0 then
            self.active.stage="cook_before_eat"; adapter.issue("cook_food",{kind="cook_food"})
        elseif a.kind~="wait" then adapter.issue(command.choice,a) end
        return true
    end
    function self:set_board(board,revision)
        if revision==self.board_revision then return end
        if self.active and self.active.board then self:finish("interrupted","task_board_replaced") end
        self.board,self.board_revision,self.board_paused=board,revision,false
        self.visits_done={}
    end
    function self:tick(s,now)
        self.last_now=now
        for key,until_time in pairs(self.paused) do if until_time<=now then self.paused[key]=nil end end
        local run=self.active
        if not run then return end
        local a,b=run.action,run.before; local night=s.world.phase=="night"
        -- Accepted is not proof of native execution. Keep confirmation unknown
        -- until exact native identity/replication or movement supplies evidence.
        if not run.confirmed_started_at then
            local execution=s.execution or {}; local expected
            if a.kind=="collect" then expected=execution.action=="PICK" or execution.action=="PICKUP"
            elseif a.kind=="chop_nearest_tree" then expected=execution.action=="CHOP"
            elseif a.kind=="mine_nearest_rock" then expected=execution.action=="MINE"
            elseif string.sub(a.kind,1,6)=="craft_" then
                expected=execution.action=="BUILD" and execution.recipe==string.sub(a.kind,7)
            end
            if expected and execution.confirmed_start and (not a.guid or execution.target_guid==a.guid)
                and (not execution.issued_at or execution.issued_at>=run.started)
                and (not execution.started_at or execution.started_at>=run.started) then
                run.confirmed_started_at=execution.started_at or now
                run.start_source=execution.start_source or "native_buffered"
            elseif (a.kind=="explore" or a.kind=="flee_from_nearest_hostile")
                and distance(run.origin,s.player.position)>=0.1 then
                run.confirmed_started_at=now; run.start_source="position_progress"
            end
            if run.confirmed_started_at then
                self.report.confirmed_started_at=run.confirmed_started_at
                self.report.start_source=run.start_source
            end
        end
        local fleeing=a.kind=="flee_from_nearest_hostile"
        local light=a.kind=="craft_torch" or a.kind=="equip_torch" or a.kind=="build_campfire"
        local reason
        if s.player.guid~=b.player.guid then reason="player_changed"
        elseif s.player.vitals.dead then reason="player_dead"
        elseif threat(s) and not fleeing then reason="targeted_by_hostile"
        elseif not light and not fleeing and (s.player.vitals.health or 0)<(b.player.vitals.health or 0) then reason="health_lost"
        elseif not light and not fleeing and s.world.phase~=b.world.phase then reason="light_changed"
        elseif not light and not fleeing and night and count(s,"torch",true)==0 and not fire(s) and a.kind~="wait"
            and a.kind~="eat_safe_food" then reason="night_light_requires_decision" end
        if not reason and not light and not fleeing and a.kind~="eat_safe_food"
            and (s.player.vitals.hunger or 0)/math.max(s.player.vitals.hunger_max or 1,1)<0.30
            and M.available(s).eat_safe_food then reason="critical_hunger" end
        if reason then self:finish("interrupted",reason); return end
        if now>=run.deadline then self:finish("failed","verification_timeout"); return end
        if a.kind=="explore" and adapter.safe and not adapter.safe(a) then
            self:finish("interrupted","navigation_path_became_unsafe"); return
        end
        if a.kind=="explore" or fleeing then
            if distance(run.last_position,s.player.position)>=0.3 then
                run.last_position=s.player.position; run.progress_at=now
            elseif now-run.progress_at>=3 then self:finish("interrupted","movement_stalled"); return end
        end
        local done=false
        if fleeing then
            if not threat(s) then self:finish("completed","no_visible_targeted_hostile"); return end
            if distance(s.player.position,{x=a.x,z=a.z})<=1 then
                local next_action=M.available(s).flee_from_nearest_hostile
                if not next_action then self:finish("interrupted","no_safe_escape_route"); return end
                run.action=next_action; adapter.issue(run.choice,next_action)
            elseif now-run.started>3 and distance(run.origin,s.player.position)<0.5 then
                self:finish("interrupted","movement_stalled")
            end
            return
        elseif run.stage=="approach" then
            local approach=run.approach; local target
            if adapter.safe and not adapter.safe(approach.waypoint) then
                self:finish("interrupted","target_path_became_unsafe"); return
            end
            for _,e in ipairs(s.nearby or {}) do if e.guid==approach.guid then target=e end end
            if not target or (approach.action.kind=="collect" and not collectable(target)) then
                self:finish("interrupted","target_changed"); return
            end
            if target.distance<=1.2 then
                adapter.stop()
                self:interact_target(run,approach.choice,approach.action,approach.after); return
            end
            local path=target.collect_path or {}; local point=path.waypoint or {}
            if path.status~="ready" or not finite(point.x) or not finite(point.z) then
                self:finish("interrupted","target_path_unavailable"); return
            end
            if distance(s.player.position,approach.last_position)>=0.3 then
                approach.last_position=s.player.position; approach.progress_at=now
            elseif now-approach.progress_at>=3 then self:finish("interrupted","movement_stalled"); return end
            if distance(s.player.position,approach.waypoint)<=0.6 then
                approach.waypoint=point; adapter.issue("walk",{kind="explore",x=point.x,z=point.z})
            end
            return
        elseif run.stage=="collection_retry" then
            local pending=run.collection_retry
            local before=run.loot and run.loot.before or count(b,pending.action.product)
            if count(s,pending.action.product)>before then
                run.collection_retry=nil; run.stage=pending.after
                if not pending.after then self:finish("completed","observed_effect") end
                return
            end
            local target
            for _,e in ipairs(s.nearby or {}) do if e.guid==pending.action.guid then target=e end end
            if not target or target.prefab~=pending.action.prefab or not collectable(target) then
                self:finish("interrupted","target_changed"); return
            end
            if not target.collect_path or target.collect_path.status~="ready" then
                self:finish("interrupted","target_path_unavailable"); return
            end
            if now>=pending.deadline then self:finish("failed","collection_action_unavailable"); return end
            if now>=pending.next_try then
                self:issue_target(run,s,pending.choice,pending.action,pending.after)
            end
            return
        elseif run.stage=="cook_before_eat" then
            if total(s,cooked)>total(b,cooked) then
                run.stage="eating"; adapter.issue("eat_safe_food",a)
            end; return
        elseif run.stage=="loot" then
            if run.loot then
                if count(s,run.loot.product)<=run.loot.before then return end
                run.preexisting[run.loot.guid]=true; run.loot=nil
            end
            for _,e in ipairs(s.nearby or {}) do
                if drops[a.kind][e.prefab] and not run.preexisting[e.guid] and collectable(e)
                    and e.collect_path and e.collect_path.status=="ready"
                    and e.distance<=6 then
                    run.loot={kind="collect",prefab=e.prefab,product=e.prefab,guid=e.guid,before=count(s,e.prefab)}
                    self:issue_target(run,s,"collect_"..e.prefab,run.loot,"loot"); return
                end
            end
            self:finish("completed","work_and_drops_verified"); return
        elseif a.kind=="wait" then done=now-run.started>=1
        elseif a.kind=="collect" then done=count(s,a.product)>count(b,a.product)
        elseif a.kind=="equip_torch" then done=count(s,"torch",true)>0
        elseif a.kind=="unequip_torch" then done=count(s,"torch",true)==0 and count(s,"torch")>=count(b,"torch")
        elseif a.kind=="craft_torch" or a.kind=="craft_axe" or a.kind=="craft_pickaxe" then
            local product=string.sub(a.kind,7); done=count(s,product)>count(b,product)
            if done and product=="torch" and not night and count(s,"torch",true)>0 then
                if not run.cleanup then run.cleanup=true; adapter.issue("unequip_torch",{kind="unequip_torch"}) end
                done=false
            end
        elseif a.kind=="eat_safe_food" then done=(s.player.vitals.hunger or 0)>(b.player.vitals.hunger or 0)
        elseif a.kind=="cook_food" then done=total(s,cooked)>total(b,cooked)
        elseif a.kind=="build_campfire" or a.kind=="build_sciencemachine" then
            for _,e in ipairs(s.nearby or {}) do
                if e.prefab==string.sub(a.kind,7) and not run.preexisting[e.guid] then done=true end
            end
        elseif drops[a.kind] then
            local w=s.work or {}
            if (w.sequence or 0)>run.work_sequence and w.target_guid==a.guid then
                if w.status=="failed" then self:finish("failed",w.reason); return end
                if w.status=="completed" then run.stage="loot"; return end
            end
        elseif a.kind=="explore" then
            local p=s.player.position
            if (((s.navigation or {}).graph or {}).known_nodes or 0)>run.known_nodes then
                self:finish("interrupted","region_discovered"); return
            end
            if distance(run.origin,p)>=4 and different(run.resources,types(s)) then
                self:finish("interrupted","resource_types_changed"); return
            end
            local region=((s.navigation or {}).graph or {}).current_node
            if region~=run.region then
                if a.mode~="graph" or region==a.node_id then self:finish("completed","region_changed"); return end
                local next_action=M.available(s)[run.choice]
                if not next_action then self:finish("interrupted","region_route_unavailable"); return end
                run.region=region; run.action=next_action; adapter.issue(run.choice,next_action); return
            end
            if distance(p,{x=a.x,z=a.z})<=(a.arrival or 1) then
                adapter.stop()
                local next_action=M.available(s)[run.choice]
                if not next_action then self:finish("completed","navigation_yield"); return end
                run.action=next_action; adapter.issue(run.choice,next_action)
            end
        end
        if done then self:finish("completed","observed_effect") end
    end
    function self:board_choice(s,now)
        local board=self.board
        if not board then return end
        -- Match the planner's verified Science Machine milestone; leaving its
        -- visibility must not make a completed construction task build it again.
        for _,e in ipairs(s.nearby or {}) do
            if e.prefab=="sciencemachine" then self.science_machine_built=true end
        end
        local available=self:available(s,now); local complete={}
        for _,t in ipairs(board.tasks or {}) do
            local goal=t.target or {}; local done=false
            if goal.type=="inventory" then done=count(s,goal.prefab)>=goal.count
            elseif goal.type=="hunger_ratio" then
                done=(s.player.vitals.hunger or 0)/math.max(s.player.vitals.hunger_max or 1,1)>=goal.at_least
            elseif goal.type=="structure" then
                done=goal.prefab=="sciencemachine" and self.science_machine_built or false
                for _,e in ipairs(s.nearby or {}) do if e.prefab==goal.prefab then done=true end end
            elseif goal.type=="known_regions" then done=(((s.navigation or {}).graph or {}).known_nodes or 0)>=goal.count
            elseif goal.type=="visit_region" then
                if ((s.navigation or {}).graph or {}).current_node==goal.node_id then self.visits_done[t.id]=true end
                done=self.visits_done[t.id]==true
            end
            complete[t.id]=done
        end
        local tasks={}; for _,t in ipairs(board.tasks or {}) do tasks[#tasks+1]=t end
        table.sort(tasks,function(a,b) return a.priority<b.priority end)
        for _,t in ipairs(tasks) do
            local ready=not complete[t.id]
            for _,id in ipairs(t.depends_on or {}) do if not complete[id] then ready=false end end
            if ready then for id,a in pairs(available) do
                local goal=t.target; local matches=false
                if goal.type=="inventory" then
                    matches=(a.kind=="collect" and a.product==goal.prefab) or id=="craft_"..goal.prefab
                        or (a.kind=="chop_nearest_tree" and goal.prefab=="log" and log_trees[a.prefab])
                        or (a.kind=="mine_nearest_rock" and (mine_products[a.prefab] or {})[goal.prefab])
                elseif goal.type=="hunger_ratio" then matches=id=="eat_safe_food"
                elseif goal.type=="structure" then matches=id=="build_"..goal.prefab end
                if matches then
                    return id,a,t.id
                end
            end end
        end
    end
    function self:board_step(s,now)
        if self.active or self.board_paused or not self.board or not self.board.auto_execute then return end
        local id,a,task=self:board_choice(s,now)
        if id then self:start({action_id="board:"..self.board_revision..":"..now,
            choice=id,action=a,task_id=task},s,now,true) end
    end
    return self
end
return M
