-- Native, snapshot-verified execution. No network, keyboard or model policy.
local M = {}
local tree_work=require("jev_tree_work")
local products={grass="cutgrass",sapling="twigs",sapling_moon="twigs",berrybush="berries",
    berrybush2="berries",berrybush_juicy="berries_juicy",carrot_planted="carrot",flower="petals"}
local loose={twigs=true,cutgrass=true,flint=true,log=true,rocks=true,goldnugget=true,nitre=true,
    pinecone=true,acorn=true,twiggy_nut=true,nightmarefuel=true,charcoal=true,marble=true,moonrocknugget=true,livinglog=true,
    berries=true,berries_juicy=true,carrot=true,seeds=true,petals=true,ice=true,redgem=true,bluegem=true,
    purplegem=true,thulecite=true,thulecite_pieces=true,berries_cooked=true,berries_juicy_cooked=true,
    carrot_cooked=true,seeds_cooked=true,meat=true,smallmeat=true,cookedmeat=true,cookedsmallmeat=true}
local cooked={cookedmeat=true,cookedsmallmeat=true,berries_cooked=true,berries_juicy_cooked=true,
    carrot_cooked=true,seeds_cooked=true}
local raw={meat=true,smallmeat=true,berries=true,berries_juicy=true,carrot=true,seeds=true}
local safe_raw={berries=true,berries_juicy=true,carrot=true,seeds=true}
local drops={chop_nearest_tree={log=true,pinecone=true,acorn=true,twiggy_nut=true,nightmarefuel=true,charcoal=true,livinglog=true,twigs=true},
    mine_nearest_rock={rocks=true,flint=true,nitre=true,goldnugget=true,marble=true,moonrocknugget=true,
        ice=true,redgem=true,bluegem=true,purplegem=true,thulecite=true,thulecite_pieces=true}}
local ground_harvest={berrybush_juicy={berries_juicy=true}}
local mine_products={rock1={rocks=true,flint=true,nitre=true},rock2={rocks=true,flint=true,goldnugget=true},
    rock_flintless={rocks=true},rock_flintless_med={rocks=true},rock_flintless_low={rocks=true}}
local log_trees={evergreen=true,evergreen_sparse=true,deciduoustree=true,twiggytree=true}
local function finite(x) return type(x)=="number" and x==x and math.abs(x)<1000000 end
local function critical(v,name)
    local value,maximum=v[name],v[name.."_max"]
    return finite(value) and finite(maximum) and maximum>0 and value/maximum<0.30
end
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

local function can_interact(target,action)
    if drops[action.kind] then
        -- GetActionButtonAction(force_target) selects work within six units.
        -- Its target-based BufferedAction/locomotor owns the final approach and
        -- physics radii. WALKTO the centre can never reach a boulder's interior.
        -- Finish verified detours first; do not hand a hazardous shortcut to it.
        local path=target.collect_path or {}
        return target.distance<6 and path.status=="ready"
            and (path.reason==nil or path.reason=="direct")
    end
    return target.distance<=1.2
end

local FLEE_STALL_SECONDS=0.8
local FLEE_DIRECTION_RETRY_SECONDS=8
local FLEE_DIRECTION_RESET_DISTANCE=2

local function flee_action(s,blocked,now)
    local p=s.player.position
    local best,score,fallback,fallback_until,fallback_score
    for _,route in ipairs((s.navigation or {}).escape_routes or {}) do
        if finite(route.dx) and finite(route.dz) and finite(route.distance) and route.distance>=1.5 then
            local length=math.sqrt(route.dx*route.dx+route.dz*route.dz)
            if length>0 then
                local nx,nz=route.dx/length,route.dz/length
                local gain=math.huge
                for _,e in ipairs(s.nearby or {}) do if e.activeThreat then
                    local dx,dz=nx*route.distance-(e.dx or 0),nz*route.distance-(e.dz or 0)
                    gain=math.min(gain,math.sqrt(dx*dx+dz*dz)-(e.distance or 0))
                end end
                local until_time=0
                for _,failed in ipairs(blocked or {}) do
                    if (now or 0)<failed.until_time and distance(p,failed.position)<=FLEE_DIRECTION_RESET_DISTANCE
                        and nx*failed.dx+nz*failed.dz>0.98 then
                        until_time=math.max(until_time,failed.until_time)
                    end
                end
                local action={kind="flee_from_nearest_hostile",x=p.x+nx*route.distance,z=p.z+nz*route.distance,
                    dx=nx,dz=nz,description="Flee along a passable route, changing direction if blocked."}
                if until_time==0 then
                    if not score or gain>score then best,score=action,gain end
                elseif not fallback_until or until_time<fallback_until
                    or (until_time==fallback_until and gain>fallback_score) then
                    fallback,fallback_until,fallback_score=action,until_time,gain
                end
            end
        end
    end
    -- Every candidate still came from the live passability scan. If all local
    -- directions were tried, retry the oldest rather than disabling all escape.
    return best or fallback
end

function M.available(s,blocked_escapes,now)
    local result={}; local v=(s.player or {}).vitals or {}
    if v.dead then return result end
    if threat(s) then
        result.flee_from_nearest_hostile=flee_action(s,blocked_escapes,now)
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
    local site=(s.construction or {}).campfire
    if night and not lit and crafting.campfire and (not site or site.status=="ready") then
        add("build_campfire","build_campfire","Build a campfire at a locally verified site clear of combustible items and entities.")
    end
    if critical(v,"hunger") and result.eat_safe_food then
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
            add("collect_"..prefab,"collect",ground_harvest[prefab]
                and "Pick the nearest reachable juicy berry bush, then collect its fallen fruit."
                or "Collect the nearest reachable "..prefab..".",
                {prefab=prefab,guid=e.guid,product=products[prefab] or prefab})
        end
        local nav=s.navigation or {}
        local function navigation(id,route,mode,node)
            if route.status=="ready" and route.leg and finite(route.leg.x) and finite(route.leg.z) then
                add(id,"explore","Continue native navigation until a resource or safety event.",
                    {mode=mode,node_id=node,x=route.leg.x,z=route.leg.z,
                        gateway_id=route.gateway_id,next_node_id=route.next_node_id,
                        source_node_id=route.source_node_id,entrance=route.entrance,
                        awaiting_region=route.leg.awaiting_region,
                        remaining_distance=route.remaining_distance,
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
                if tag and (w[1]=="chop_nearest_tree" or e.distance<=6)
                    and e.work_required and remaining>=e.work_required
                    and e.collect_path and e.collect_path.status=="ready" then
                    if (w[1]=="chop_nearest_tree" and tree_work.prefer(e,best))
                        or (w[1]~="chop_nearest_tree" and (not best or e.distance<best.distance)) then best=e end
                end
            end
            if best then add(w[1],w[1],w[1]=="chop_nearest_tree"
                and "Prefer a larger reachable tree with sufficient axe durability, then collect nearby drops."
                or "Complete one work target, then collect its nearby drops.",
                {guid=best.guid,prefab=best.prefab,tree_stage=best.tree_stage}) end
        end
    end
    return result
end

local function valid_navigation_targets(targets)
    if type(targets)~="table" or #targets>6 then return false end
    local size=0
    for index,t in pairs(targets) do
        if type(index)~="number" or index%1~=0 or index<1 or index>#targets or type(t)~="table" then return false end
        size=size+1
        if t.type=="inventory" then
            if not (loose[t.prefab] or t.prefab=="torch" or t.prefab=="axe" or t.prefab=="pickaxe")
                or not finite(t.count) or t.count%1~=0 or t.count<1 or t.count>1000 then return false end
        elseif t.type=="structure" then
            if t.prefab~="campfire" and t.prefab~="firepit" and t.prefab~="sciencemachine" then return false end
        elseif t.type=="hunger_ratio" then
            if not finite(t.at_least) or t.at_least<=0 or t.at_least>1 then return false end
        else return false end
    end
    return size==#targets
end

local function goal_action_available(s,targets,available)
    for _,t in ipairs(targets) do
        if t.type=="inventory" and count(s,t.prefab)<t.count then
            for id,a in pairs(available) do
                if (a.kind=="collect" and (a.product or a.prefab)==t.prefab) or id=="craft_"..t.prefab
                    or (t.prefab=="log" and a.kind=="chop_nearest_tree" and log_trees[a.prefab])
                    or (a.kind=="mine_nearest_rock" and (mine_products[a.prefab] or {})[t.prefab]) then return true end
            end
        elseif t.type=="structure" and available["build_"..t.prefab] then
            local exists=false
            for _,e in ipairs(s.nearby or {}) do if e.prefab==t.prefab then exists=true end end
            if not exists then return true end
        elseif t.type=="hunger_ratio" then
            local v=s.player.vitals
            if finite(v.hunger) and finite(v.hunger_max) and v.hunger_max>0 and v.hunger/v.hunger_max<t.at_least then
                if available.eat_safe_food then return true end
                for _,a in pairs(available) do
                    if a.kind=="collect" and (cooked[a.product or a.prefab] or safe_raw[a.product or a.prefab]) then return true end
                end
            end
        end
    end
    return false
end

function M.new(adapter)
    local self={active=nil,report=nil,board=nil,board_revision=-1,board_paused=false,
        events={},event_sequence=0,paused={},blocked_escapes={},last_now=0,visits_done={},science_machine_built=false,
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
        local result=M.available(s,self.blocked_escapes,now or self.last_now)
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
            previous_action_id=run.previous_action_id,handoff_gap_ms=run.handoff_gap_ms,
            escape_reroutes=run.escape_reroutes}
        self.event_sequence=self.event_sequence+1
        self.events[#self.events+1]={sequence=self.event_sequence,action_id=run.id,choice=run.choice,
            kind=run.action.kind,status=status,reason=reason or "",board_task_id=run.task_id,
            started_at=run.started,confirmed_started_at=run.confirmed_started_at,start_source=run.start_source,
            finished_at=self.last_now,previous_action_id=run.previous_action_id,handoff_gap_ms=run.handoff_gap_ms,
            escape_reroutes=run.escape_reroutes}
        if #self.events>64 then table.remove(self.events,1) end
        self.last_finished=self.events[#self.events]
        if run.action.kind~="flee_from_nearest_hostile" and (status=="failed" or reason=="movement_stalled"
            or reason=="target_path_unavailable" or reason=="target_changed" or reason=="navigation_path_became_unsafe") then
            self.paused[action_key(run.choice,run.action)]=self.last_now+15
        end
        if self.active.board and status~="completed" then self.board_paused=true end
        self.active=nil
    end
    function self:redirect_escape(run,s,now,blocked)
        if blocked then
            local a=run.action; local p=s.player.position
            self.blocked_escapes[#self.blocked_escapes+1]={dx=a.dx,dz=a.dz,
                position={x=p.x,z=p.z},until_time=now+FLEE_DIRECTION_RETRY_SECONDS}
            if #self.blocked_escapes>16 then table.remove(self.blocked_escapes,1) end
        end
        local next_action=self:available(s,now).flee_from_nearest_hostile
        if not next_action then self:finish("interrupted","no_safe_escape_route"); return end
        if blocked then
            adapter.stop()
            run.escape_reroutes=(run.escape_reroutes or 0)+1
            self.report.escape_reroutes=run.escape_reroutes
        end
        run.action=next_action
        run.last_position={x=s.player.position.x,z=s.player.position.z}; run.progress_at=now
        adapter.issue(run.choice,next_action)
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
        if run.loot==action then
            action.pickup_deadline=action.pickup_deadline or self.last_now+4
            action.next_retry=self.last_now+1
        end
        local accepted=adapter.issue(choice,action)
        if accepted~=false and action==run.action and ground_harvest[action.prefab] then
            run.harvest_issued_at=self.last_now
        end
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
            if can_interact(e,action) then self:interact_target(run,choice,action,after); return end
            local path=e.collect_path or {}; local point=path.waypoint or {}
            if path.status~="ready" or not finite(point.x) or not finite(point.z) then
                self:finish("interrupted","target_path_unavailable"); return
            end
            run.approach={guid=action.guid,choice=choice,action=action,after=after,waypoint=point,
                last_position=s.player.position,progress_at=self.last_now}
            run.stage="approach"; adapter.issue("walk",{kind="explore",x=point.x,z=point.z}); return
        end end
        self:finish("failed","target_missing")
    end
    function self:begin_loot(run,now)
        run.stage="loot"; run.loot_phase=true; run.loot_settle_until=now+1
        run.work_origin=run.work_origin or run.origin
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
            if command.choice=="build_campfire" then
                local site=(s.construction or {}).campfire
                if site and site.status~="ready" then return reject(site.reason or "no_safe_campfire_position") end
            end
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
        if requested.navigation_targets~=nil and (a.kind~="explore"
            or not valid_navigation_targets(requested.navigation_targets)) then
            return reject("invalid_navigation_targets")
        end
        -- Use the current route/recipe facts, never stale request coordinates.
        -- Target GUID/node identity is preserved; this is not a new AI choice.
        local position=s.player.position
        local harvest=a.kind=="collect" and ground_harvest[a.prefab] or nil
        self.active={id=command.action_id,choice=command.choice,action=a,before=s,started=now,
            deadline=now+(drops[a.kind] and 50 or harvest and 30 or a.kind=="explore" and 120 or 12),
            loot_products=drops[a.kind] or harvest,
            harvest_inventory_before=harvest and count(s,a.product) or nil,
            origin={x=position.x,z=position.z},resources=types(s),board=board,task_id=command.task_id,
            navigation_targets=requested.navigation_targets,
            last_position={x=position.x,z=position.z},progress_at=now,
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
        for _,e in ipairs(s.nearby or {}) do
            self.active.preexisting[e.guid]=true
            if self.active.loot_products and e.guid==a.guid then
                self.active.work_origin={x=position.x+(e.dx or 0),z=position.z+(e.dz or 0)}
            end
        end
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
    function self:tick(s,now,control_enabled)
        self.last_now=now
        for key,until_time in pairs(self.paused) do if until_time<=now then self.paused[key]=nil end end
        local run=self.active
        -- Daylight fuel saving is executor maintenance, including while idle
        -- or fleeing. Preserve the current action/report and never stop movement.
        -- Craft cleanup and explicit unequip already own their replication wait.
        if control_enabled~=false and s.world.phase=="day" and not s.player.vitals.dead
            and count(s,"torch",true)>0
            and not (run and (run.action.kind=="craft_torch" or run.action.kind=="unequip_torch")) then
            if now>=(self.daylight_cleanup_after or 0) then
                self.daylight_cleanup_after=now+1
                adapter.issue("unequip_torch",{kind="unequip_torch",automatic_reason="daylight"})
            end
        else self.daylight_cleanup_after=nil end
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
        if not reason and not light and not fleeing then
            for _,name in ipairs({"health","hunger","sanity"}) do
                local current,before=s.player.vitals,b.player.vitals
                local remedy=(name=="hunger" and a.kind=="eat_safe_food")
                    or (name=="sanity" and a.kind=="collect" and a.prefab=="flower")
                if not remedy and critical(current,name) and (not critical(before,name)
                    or math.floor(current[name]/current[name.."_max"]*10)
                        <math.floor(before[name]/before[name.."_max"]*10)) then
                    reason="critical_"..name; break
                end
            end
        end
        if reason then self:finish("interrupted",reason); return end
        if now>=run.deadline then self:finish("failed","verification_timeout"); return end
        if run.harvest_issued_at and not run.loot_phase then
            local plant
            for _,e in ipairs(s.nearby or {}) do if e.guid==a.guid then plant=e; break end end
            if (plant and plant.pickable==false)
                or (not plant and run.confirmed_started_at)
                or count(s,a.product)>run.harvest_inventory_before then
                -- PICK may drop fruit instead of inserting it into inventory.
                -- Depleting the requested bush starts cleanup; it is not success.
                self:begin_loot(run,now); run.harvest_loot_deadline=now+3
            end
        end
        if run.loot_phase and adapter.loot then
            local items=adapter.loot(run.work_origin)
            if not items then self:finish("failed","loot_scan_unavailable"); return end
            -- Only replace this tick's local view, never the shared snapshot or
            -- the original safety observation used above.
            local current={}; for key,value in pairs(s) do current[key]=value end
            current.nearby=items; s=current
        end
        if a.kind=="explore" and adapter.safe and not adapter.safe(a) then
            self:finish("interrupted","navigation_path_became_unsafe"); return
        end
        if a.kind=="explore" and not a.awaiting_region then
            if distance(run.last_position,s.player.position)>=0.3 then
                run.last_position=s.player.position; run.progress_at=now
            elseif now-run.progress_at>=3 then self:finish("interrupted","movement_stalled"); return end
        end
        local done=false
        if fleeing then
            if not threat(s) then self:finish("completed","no_visible_targeted_hostile"); return end
            if distance(run.last_position,s.player.position)>=0.3 then
                run.last_position={x=s.player.position.x,z=s.player.position.z}; run.progress_at=now
            elseif now-run.progress_at>=FLEE_STALL_SECONDS then
                self:redirect_escape(run,s,now,true); return
            end
            if distance(s.player.position,{x=a.x,z=a.z})<=1 then
                self:redirect_escape(run,s,now,false)
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
            if can_interact(target,approach.action) then
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
            if run.loot and run.loot.pickup_deadline and now>=run.loot.pickup_deadline then
                self:finish("failed","loot_inventory_not_increased"); return
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
                if count(s,run.loot.product)<=run.loot.before then
                    if now>=run.loot.pickup_deadline then
                        self:finish("failed","loot_inventory_not_increased")
                    elseif now>=run.loot.next_retry and not (s.execution or {}).doing then
                        -- Ground removal can precede inventory replication.
                        -- Keep waiting for the gain rather than treating it as target loss.
                        for _,e in ipairs(s.nearby or {}) do if e.guid==run.loot.guid then
                            self:issue_target(run,s,"collect_"..run.loot.prefab,run.loot,"loot"); break
                        end end
                    end
                    return
                end
                -- Inventory and ground removal can replicate in different frames.
                -- A GUID is not permanently consumed: partial stacks must be revisited.
                run.loot_cooldown=run.loot_cooldown or {}
                run.loot_cooldown[run.loot.guid]=now+0.6
                run.loot=nil; run.loot_settle_until=now+0.6
            end
            local blocked,pending=false,false
            for _,e in ipairs(s.nearby or {}) do
                local origin=run.work_origin or run.origin
                local in_range=(e.dx and e.dz) and distance(origin,
                    {x=s.player.position.x+e.dx,z=s.player.position.z+e.dz})<=6 or not e.dx and e.distance<=6
                if run.loot_products[e.prefab] and in_range and collectable(e) then
                    if (run.loot_cooldown or {})[e.guid] and run.loot_cooldown[e.guid]>now then pending=true
                    elseif e.collect_path and e.collect_path.status=="ready" then
                        run.loot={kind="collect",prefab=e.prefab,product=e.prefab,guid=e.guid,before=count(s,e.prefab)}
                        self:issue_target(run,s,"collect_"..e.prefab,run.loot,"loot"); return
                    else blocked=true end
                end
            end
            if pending or now<run.loot_settle_until then return end
            if blocked then self:finish("interrupted","target_path_unavailable"); return end
            if run.harvest_issued_at and count(s,a.product)<=run.harvest_inventory_before then
                if now<run.harvest_loot_deadline then return end
                self:finish("failed","harvest_loot_not_received"); return
            end
            self:finish("completed",run.harvest_issued_at and "harvest_and_drops_verified" or "work_and_drops_verified"); return
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
                if w.status=="completed" then
                    if finite(w.x) and finite(w.z) then run.work_origin={x=w.x,z=w.z} end
                    self:begin_loot(run,now)
                    return
                end
            end
        elseif a.kind=="explore" then
            local p=s.player.position
            local route_pause=(((s.navigation or {}).graph or {}).route_pause or {})
            if a.mode=="graph" and a.gateway_id and route_pause.gateway_id==a.gateway_id
                and (route_pause.remaining_seconds or 0)>0 then
                self:finish("interrupted",route_pause.reason); return
            end
            if (((s.navigation or {}).graph or {}).known_nodes or 0)>run.known_nodes then
                self:finish("interrupted","region_discovered"); return
            end
            if distance(run.origin,p)>=4 then
                if run.navigation_targets~=nil then
                    -- An actionable board goal, not a changed scenery set.
                    -- The offered actions already enforce paths, tools and durability.
                    if #run.navigation_targets>0 and goal_action_available(s,run.navigation_targets,self:available(s,now)) then
                        self:finish("interrupted","goal_action_available"); return
                    end
                elseif different(run.resources,types(s)) then
                    self:finish("interrupted","resource_types_changed"); return
                end
            end
            local region=((s.navigation or {}).graph or {}).current_node
            if region~=run.region then
                if a.mode~="graph" or region==a.node_id then self:finish("completed","region_changed"); return end
                local next_action=M.available(s)[run.choice]
                if not next_action then self:finish("interrupted","region_route_unavailable"); return end
                run.region=region; run.action=next_action; adapter.issue(run.choice,next_action); return
            end
            if distance(p,{x=a.x,z=a.z})<=(a.arrival or 1) then
                local next_action=M.available(s)[run.choice]
                if not next_action then self:finish("completed","navigation_yield"); return end
                if next_action.awaiting_region then
                    run.action=next_action
                    return
                end
                adapter.stop()
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
