-- One authoritative actor/executor per actual RPC sender.
local M={}
local OPS={summon=true,bind=true,state=true,intent=true,follow=true,stay=true,stop=true,move=true,collect=true,release=true,say=true,
    recipes=true,equip=true,unequip=true,eat=true,heal=true,work=true,craft=true,place=true,search=true,give_owner=true,drop_item=true,deposit=true,withdraw=true,visit_building=true,refuel=true,set_home=true,
    cook_load=true,start_meal=true,harvest_meal=true,ignite_tree=true,attack=true}
local NATIVE={equip=true,unequip=true,eat=true,heal=true,work=true,craft=true,place=true,give_owner=true,drop_item=true,deposit=true,withdraw=true,refuel=true,set_home=true,
    cook_load=true,start_meal=true,harvest_meal=true,ignite_tree=true,attack=true}
local function finite(v) return type(v)=="number" and v==v and math.abs(v)<1e7 end
local function valid_guid(v) return type(v)=="number" and v==v and v>=1 and v<=9007199254740991 and v%1==0 end
local function valid_id(v) return type(v)=="string" and #v<=100 and v:match("^[%w:_%-]+$") end
function M.new(env)
    local self={owners={},spawned={},serial=0}
    local function send(r,q,status,reason,data)
        if not r.player:IsValid() then q.last={id=q.id,seq=q.seq,op=q.op,status=status,reason=reason};return end
        r.snapshot_version=(r.snapshot_version or 0)+1
        local p={id=q.id,seq=q.seq,op=q.op,status=status,reason=reason,token=r.token,revision=r.revision or 0,
            state=env.snapshot(r.player,r.buddy,r),data=data}
        p.state.version=r.snapshot_version
        if q.op=="state" then p.state.last_action_result=r.last_action_result end
        q.last=p;env.reply(r.player,p)
    end
    local function halt(r)
        if r.buddy and r.buddy:IsValid() then env.stop(r.buddy) end
    end
    local function follow_state(r,status)
        local now=env.now()
        local changed=not r.follow_state or r.follow_state.status~=status
        r.follow_state={status=status,repaths=r.follow_repaths or 0}
        if env.trace and (changed or status~="arrived" and now-(r.follow_logged or -math.huge)>=10) then
            r.follow_logged=now
            local x,z=env.position(r.buddy)
            env.trace("follow_state",{buddy_guid=r.buddy.GUID,status=status,owner_distance=env.distance(r.buddy,r.player),
                x=x,z=z,motion=env.motion and env.motion(r.buddy) or nil,repaths=r.follow_repaths or 0})
        end
    end
    local function finish(r,status,reason,data)
        local q=r.active;if not q then return end
        r.active=nil
        if reason=="low_health_retreat" then
            r.combat_stance_until=nil;r.combat_min_hp=nil
        elseif status=="completed" and (q.op=="attack" or (q.op=="equip" and q.combat_gear)) then
            r.combat_stance_until=env.now()+2.0
        end
        r.last_action_result={id=q.id,seq=q.seq,op=q.op,status=status,reason=reason,data=data}
        halt(r);send(r,q,status,reason,data)
    end
    local function release(r,reason)
        finish(r,"interrupted",reason);halt(r)
        if r.buddy and r.buddy._jev_companion_owner==r.player.userid then
            if env.unmap then env.unmap(r.buddy) end
            if env.deactivate then env.deactivate(r.buddy) end
            if r.buddy:IsValid() then r.buddy._jev_companion_owner=nil end
        end
        r.map_error=nil;r.follow_progress=nil;r.follow_state=nil;r.escape=nil;r.next_threat_scan=nil
        r.combat_stance_until=nil;r.combat_min_hp=nil
        r.buddy=nil;r.token=nil;r.mode="stay";r.revision=0;r.last_action_result=nil
        r.transfer_totals=nil;r.transfer_order=nil
    end
    function self:delete(userid, actor)
        local record=self.owners[userid]
        if record and record.buddy then release(record,"deleted") end
        self.owners[userid]=nil
        self.spawned[userid]=nil
        if actor and actor:IsValid() then
            local inventory=actor.components.inventory
            if inventory and inventory.DropEverything then inventory:DropEverything() end
            actor:Remove()
        end
    end
    local function evade(r,now)
        if not env.threats or not env.escape_point then return false end
        if env.is_self and not r.active and not r.escape then return false end
        local hp_ratio=env.health_ratio and env.health_ratio(r.buddy) or 1
        if r.active and r.active.op=="attack" then
            local min_hp=tonumber((r.active.args or {}).min_health_ratio) or 0.35
            if hp_ratio>=min_hp then return false end
        elseif r.active and r.active.op=="equip" and r.active.combat_gear then
            if hp_ratio>=(r.combat_min_hp or 0.35) then return false end
        elseif r.combat_stance_until and now<r.combat_stance_until and hp_ratio>=(r.combat_min_hp or 0.35) then
            return false
        end
        if now<(r.next_threat_scan or 0) then return r.escape~=nil end
        r.next_threat_scan=now+.25
        local threats=env.threats(r.buddy)
        local escape=r.escape
        if #threats==0 then
            if not escape then return false end
            if now<escape.safe_after then return true end
            r.escape=nil;halt(r)
            if env.trace then env.trace("buddy_escape_end",{buddy_guid=r.buddy.GUID}) end
            return false
        end
        if not escape then
            escape={status="escaping",threat_prefab=threats[1].prefab}
            r.escape=escape
            if r.active then finish(r,"interrupted","enemy_targeting") else halt(r) end
            r.follow_progress=nil;r.follow_state=nil
            if env.trace then env.trace("buddy_escape_start",{buddy_guid=r.buddy.GUID,threat_prefab=escape.threat_prefab}) end
        end
        escape.safe_after=now+1.5
        if now>=(escape.next_move or 0) then
            escape.next_move=now+.5
            local point=env.escape_point(r.buddy,threats)
            if point then
                local motion=env.motion and env.motion(r.buddy) or {}
                if not escape.destination or point.id~=escape.destination or (not motion.moving and not motion.path_pending) then
                    if env.move(r.buddy,point.x,point.z)~=false then
                        escape.destination=point.id;escape.status="escaping"
                    else
                        escape.destination=nil;escape.status="blocked"
                    end
                end
            elseif escape.status~="blocked" then
                halt(r);escape.destination=nil;escape.status="blocked"
                if env.trace then env.trace("buddy_escape_blocked",{buddy_guid=r.buddy.GUID,threat_prefab=escape.threat_prefab}) end
            end
        end
        return true
    end
    function self:request(player,q)
        if not player or not player:IsValid() or type(player.userid)~="string" or player.userid==""
            or type(q)~="table" or not valid_id(q.id) or not OPS[q.op]
            or not finite(q.seq) or q.seq<1 or q.seq%1~=0 then return end
        local r=self.owners[player.userid]
        if r and r.player~=player then release(r,"owner_replaced");r=nil end
        if not r then r={player=player,cache={},order={},highest=0,mode="stay",revision=0,retired={}};self.owners[player.userid]=r end
        local cached=r.cache[q.id]
        if cached then if cached.seq==q.seq and cached.op==q.op and cached.last then env.reply(player,cached.last) end;return end
        r.cache[q.id]=q;r.order[#r.order+1]=q.id
        if #r.order>128 then r.cache[table.remove(r.order,1)]=nil end
        local epoch=q.id:match("^(.*):%d+$")
        if not epoch then send(r,q,"failed","invalid_epoch");return end
        if r.epoch~=epoch then
            if r.retired[epoch] then send(r,q,"failed","retired_client");return end
            if r.epoch then
                if q.op~="bind" and q.op~="summon" then send(r,q,"failed","rebind_required");return end
                r.retired[r.epoch]=true;r.epochs=r.epochs or {};r.epochs[#r.epochs+1]=r.epoch
                if #r.epochs>8 then r.retired[table.remove(r.epochs,1)]=nil end
                release(r,"client_restarted")
            end
            r.epoch=epoch;r.highest=0
        end
        if q.seq<=r.highest then send(r,q,"failed","out_of_order");return end
        r.highest=q.seq
        local now=env.now()
        if not r.window or now-r.window>=1 then r.window=now;r.requests=0 end
        r.requests=r.requests+1
        if r.requests>12 and q.op~="stop" and q.op~="intent" then send(r,q,"failed","rate_limited");return end
        if q.op=="bind" or q.op=="summon" then
            if r.active then send(r,q,"failed","busy");return end
            if r.buddy and r.buddy:IsValid() and env.dead(r.buddy) then send(r,q,"failed","existing_companion_dead");return end
            local created=false
            if not r.buddy or not r.buddy:IsValid() then
                local saved=self.spawned[player.userid]
                if saved and not saved:IsValid() then self.spawned[player.userid]=nil;saved=nil end
                if saved then
                    if env.dead(saved) then send(r,q,"failed","existing_companion_dead");return end
                    if saved._jev_companion_owner and saved._jev_companion_owner~=player.userid then send(r,q,"failed","companion_in_use");return end
                    r.buddy=saved
                else
                    r.buddy=env.find_buddy(player)
                end
                if not r.buddy then
                    local reason
                    r.buddy,reason=env.spawn_buddy(player)
                    if not r.buddy then send(r,q,"failed",reason or "spawn_failed");return end
                    r.buddy._jev_companion_created_for=player.userid
                    self.spawned[player.userid]=r.buddy;created=true
                end
                r.buddy._jev_companion_owner=player.userid;self.serial=self.serial+1
                r.token=tostring(r.buddy.GUID)..":"..tostring(self.serial)
            end
            r.last_seen=now;r.lease_expired=nil;r.is_self=env.is_self==true;r.mode=r.is_self and "stay" or "follow"
            r.follow_progress=nil;r.follow_state=nil;r.follow_repaths=0
            if env.activate then env.activate(r.buddy) end
            if env.map_bind then local ok,reason=env.map_bind(player,r.buddy);r.map_error=not ok and reason or nil end
            send(r,q,"completed",r.is_self and "bound" or (created and "summoned" or "bound"));return
        end
        if not r.buddy or not r.buddy:IsValid() or r.buddy._jev_companion_owner~=player.userid or q.token~=r.token then send(r,q,"failed","binding_changed");return end
        r.last_seen=now;r.lease_expired=nil
        if q.op=="state" then send(r,q,"completed","observed");return end
        if q.op=="intent" then
            if not finite(q.revision) or q.revision%1~=0 or q.revision<=r.revision then send(r,q,"failed","obsolete_intent");return end
            r.revision=q.revision;r.mode="stay";r.combat_stance_until=nil;r.combat_min_hp=nil;finish(r,"interrupted","player_message");halt(r)
            r.follow_progress=nil;r.follow_state=nil
            send(r,q,"completed","intent_updated");return
        end
        if q.revision~=r.revision then send(r,q,"failed","obsolete_intent");return end
        if q.op~="release" and env.dead(r.buddy) then send(r,q,"failed","buddy_dead");return end
        local args=type(q.args)=="table" and q.args or {}
        q.args=args
        if q.op=="recipes" then
            local data,reason=env.query(r.buddy,args)
            send(r,q,data and "completed" or "failed",reason or "recipes_observed",data);return
        end
        if q.op=="stop" or q.op=="stay" or q.op=="follow" then
            r.mode=(q.op=="follow" and not env.is_self) and "follow" or "stay";r.combat_stance_until=nil;r.combat_min_hp=nil;finish(r,"interrupted","cancelled_by_owner");halt(r)
            r.follow_progress=nil;r.follow_state=nil;r.follow_repaths=0
            if env.activate then env.activate(r.buddy) end
            send(r,q,"completed",r.mode);return
        end
        if q.op=="release" then release(r,"released");send(r,q,"completed","released");return end
        if env.is_self and q.op=="give_owner" then send(r,q,"failed","unsupported_for_self");return end
        if q.op=="say" then
            local text=env.validate_text(args.text)
            if not text then send(r,q,"failed","invalid_text");return end
            env.say(r.buddy,text,r);send(r,q,"completed","spoken");return
        end
        if r.escape and q.op~="attack" and q.op~="equip" then send(r,q,"failed","enemy_targeting");return end
        if r.active then send(r,q,"failed","busy");return end
        local function dispatch_action(rec,req,ts)
            local a_args=req.args or {}
            if req.op=="search" then
                local products=a_args.products
                if type(products)~="table" or #products<1 or #products>6 or not finite(a_args.timeout_seconds)
                    or a_args.timeout_seconds<.2 or a_args.timeout_seconds>8 then return false,"invalid_search" end
                for _,name in ipairs(products) do
                    if type(name)~="string" or #name>60 or not name:match("^[a-z0-9_]+$") then return false,"invalid_search" end
                end
                local point=env.search_point and env.search_point(rec.buddy,a_args)
                if not point then return false,"search_point_unavailable" end
                req.x,req.z=point.x,point.z;req.deadline=ts+a_args.timeout_seconds
                if env.move(rec.buddy,req.x,req.z)==false then return false,"move_rejected" end
            elseif req.op=="visit_building" then
                local accepted,reason
                if env.visit_start then accepted,reason=env.visit_start(rec.buddy,req) end
                if not accepted then return false,reason or "building_navigation_unavailable" end
            elseif req.op=="move" then
                if not finite(a_args.x) or not finite(a_args.z) or not env.valid_point(rec.buddy,a_args.x,a_args.z) then return false,"invalid_destination" end
                req.x,req.z=a_args.x,a_args.z
                if env.move(rec.buddy,a_args.x,a_args.z)==false then return false,"move_rejected" end
            elseif req.op=="collect" then
                if not valid_guid(a_args.target_guid) then return false,"invalid_target" end
                local target=env.collect_target(rec.buddy,a_args.target_guid)
                if not target then return false,"target_unavailable" end
                req.product=target.product;req.before=env.count(rec.buddy,target.product)
                if env.collect(rec.buddy,target)==false then return false,"collect_rejected" end
            elseif NATIVE[req.op] then
                local accepted,reason=env.begin(rec.buddy,req,rec)
                if not accepted then return false,reason or "native_action_rejected" end
                if req.op=="attack" or (req.op=="equip" and req.combat_gear) then
                    rec.escape=nil
                    rec.combat_min_hp=tonumber(a_args.min_health_ratio) or rec.combat_min_hp or 0.35
                    rec.combat_stance_until=ts+2.5
                end
            end
            return true
        end
        local delay_ms=type(env.action_delay_ms)=="function" and env.action_delay_ms() or tonumber(env.action_delay_ms) or 0
        if delay_ms>0 then
            if q.op=="attack" or q.op=="equip" then
                r.escape=nil
                r.combat_min_hp=tonumber(args.min_health_ratio) or r.combat_min_hp or 0.35
                r.combat_stance_until=now+2.5+delay_ms/1000
            end
            q.delayed_until=now+delay_ms/1000
            q._dispatch=dispatch_action
            r.mode="stay";r.follow_progress=nil;r.follow_state=nil;q.started=now;r.active=q
            send(r,q,"running","delayed");return
        end
        local ok,reason=dispatch_action(r,q,now)
        if not ok then send(r,q,"failed",reason or "action_rejected");return end
        r.mode="stay";r.follow_progress=nil;r.follow_state=nil;q.started=now;r.active=q;send(r,q,"running","started")
    end
    function self:tick()
        local now=env.now()
        for userid,r in pairs(self.owners) do
            if not r.player:IsValid() then release(r,"owner_disconnected");self.owners[userid]=nil
            elseif r.buddy then
                if not r.buddy:IsValid() then release(r,"buddy_removed")
                elseif now-(r.last_seen or 0)>4 then
                    if not r.lease_expired then
                        finish(r,"interrupted","client_lease_expired");halt(r);r.mode="stay";r.lease_expired=true
                        r.follow_progress=nil;r.escape=nil;follow_state(r,"client_lease_expired")
                    end
                elseif env.dead(r.buddy) then
                    finish(r,"failed","buddy_dead");halt(r);r.mode="stay";r.follow_progress=nil;r.escape=nil
                    follow_state(r,"buddy_dead")
                elseif evade(r,now) then
                    -- Emergency movement owns locomotion until the threat drops.
                elseif r.active then
                    local q=r.active
                    if q.delayed_until then
                        if now>=q.delayed_until then
                            local fn=q._dispatch;q.delayed_until=nil;q._dispatch=nil;q.started=now
                            local ok,reason=fn(r,q,now)
                            if not ok then finish(r,"failed",reason or "action_rejected") end
                        end
                    elseif q.op=="search" then
                        if now>=(q.next_sense or 0) then
                            q.next_sense=now+.25
                            if env.search_found(r.buddy,q.args.products) then finish(r,"completed","resource_found") end
                        end
                        if r.active==q then
                            if env.search_arrived(r.buddy,q.x,q.z) then finish(r,"completed","search_leg_arrived")
                            elseif now>=q.deadline then finish(r,"completed","search_leg_timeout") end
                        end
                    elseif q.op=="visit_building" then
                        local status,reason,data=env.visit_progress(r.buddy,q)
                        if status then finish(r,status,reason,data) end
                    elseif q.op=="move" and env.arrived(r.buddy,q.x,q.z) then finish(r,"completed","arrived")
                    elseif q.op=="collect" and env.count(r.buddy,q.product)>q.before then finish(r,"completed","inventory_increased")
                    elseif NATIVE[q.op] then
                        local status,reason,data=env.progress(r.buddy,q,r)
                        local limit=q.op=="attack" and 14 or 8
                        if status then finish(r,status,reason,data)
                        elseif now-q.started>=limit then finish(r,"failed","action_timeout") end
                    elseif now-q.started>=8 then finish(r,"failed","action_timeout") end
                elseif r.mode=="follow" then
                    local distance=env.distance(r.buddy,r.player)
                    if distance<=3 then halt(r);r.follow_progress=nil;follow_state(r,"arrived")
                    elseif now>=(r.next_follow or 0) then
                        r.next_follow=now+1;local x,z=env.position(r.player)
                        local bx,bz=env.position(r.buddy)
                        local progress=r.follow_progress
                        if not progress or (bx-progress.x)^2+(bz-progress.z)^2>=.5^2 then
                            progress={x=bx,z=bz,at=now};r.follow_progress=progress
                        end
                        local motion=env.motion and env.motion(r.buddy) or {}
                        local stalled=now-progress.at>=(motion.path_pending and 15 or 5)
                        if env.valid_point(r.buddy,x,z,math.huge) then
                            if stalled then
                                r.follow_repaths=(r.follow_repaths or 0)+1
                                progress.at=now
                            end
                            local accepted,reason
                            if env.follow then accepted,reason=env.follow(r.buddy,r.player,stalled or motion.asleep==true)
                            else accepted=env.move(r.buddy,x,z) end
                            follow_state(r,accepted==false and (reason or "move_rejected") or reason or
                                (stalled and "repath" or motion.path_pending and "waiting_path" or "following"))
                        else follow_state(r,"invalid_destination") end
                    end
                end
            end
        end
    end
    return self
end

function M.new_team(env)
    local Names=require("jev_companion_roster")
    local Places=require("jev_companion_places")
    local self={owners={}}
    function self:set_simulation(opts)
        if type(opts)~="table" then return end
        if tonumber(opts.scan_radius) then
            local sr=tonumber(opts.scan_radius)
            if sr<10 then sr=10 elseif sr>25 then sr=25 end
            env.scan_radius=sr
        end
        if tonumber(opts.action_delay_ms) then
            local ad=tonumber(opts.action_delay_ms)
            if ad<0 then ad=0 elseif ad>1000 then ad=1000 end
            env.action_delay_ms=ad
        end
    end
    local function get_owner(userid)
        local owner=self.owners[userid]
        if not owner then
            owner={userid=userid,slots={},roster_version=0}
            self.owners[userid]=owner
        end
        return owner
    end
    local function get_actor(owner,slot)
        local r=slot.engine.owners[owner.userid]
        return r and r.buddy or slot.engine.spawned[owner.userid] or slot.actor
    end
    local function roster(owner)
        local result={}
        for _,slot_id in ipairs({"self","buddy1","buddy2"}) do
            local slot=owner.slots[slot_id]
            if slot then
                local r=slot.engine.owners[owner.userid]
                local a=get_actor(owner,slot)
                result[#result+1]={id=slot.id,name=slot.name,is_self=(slot.id=="self"),guid=a and a:IsValid() and a.GUID or nil,
                    bound=r~=nil and r.buddy~=nil and r.buddy:IsValid(),dead=a and a:IsValid() and env.dead(a) or false}
            end
        end
        return result
    end
    local function fail(player,q,owner,reason)
        env.reply(player,{id=q.id,seq=q.seq,op=q.op,status="failed",reason=reason,buddy_id=q.buddy_id,roster_version=owner.roster_version,roster=roster(owner)})
    end
    local function create(owner,id,name)
        local slot={id=id,name=name};local wrapped={}
        for key,value in pairs(env) do wrapped[key]=value end
        wrapped.action_delay_ms=function() return env.action_delay_ms or 0 end
        local function remember(a)
            slot.actor=a
            slot.engine.spawned[owner.userid]=a
            if id=="self" then return end
            a._jev_companion_slot=slot
            a._jev_companion_id=id
            a._jev_companion_created_for=owner.userid
            a._jev_companion_name=slot.name
        end
        slot.remember=remember
        local function share(a)
            for _,other in pairs(owner.slots) do
                local r=other.engine.owners[owner.userid]
                local b=r and r.buddy or other.engine.spawned[owner.userid]
                if b and b~=a and b:IsValid() then Places.share(a,b,other.id) end
            end
        end
        wrapped.reply=function(player,packet)
            if packet.status=="failed" and (packet.op=="summon" or packet.op=="bind") then
                local r=slot.engine.owners[owner.userid]
                if not (r and r.buddy) and not slot.engine.spawned[owner.userid] then
                    owner.slots[id]=nil
                    if id=="self" then owner.roster_version=owner.roster_version+1 end
                end
            elseif id=="self" and packet.op=="release" and packet.status=="completed" then
                owner.slots["self"]=nil
                owner.roster_version=owner.roster_version+1
            end
            packet.buddy_id=id;packet.buddy_name=slot.name;packet.roster_version=owner.roster_version;packet.roster=roster(owner)
            env.reply(player,packet)
        end
        wrapped.snapshot=function(player,a,r)
            if a then remember(a);share(a) end
            local state=env.snapshot(player,a,r);state.buddy_id=id;state.buddy_name=slot.name
            return state
        end
        if id=="self" then
            wrapped.is_self=true
            wrapped.find_buddy=function(player) if not env.dead(player) then return player end end
            wrapped.spawn_buddy=function(player)
                if env.dead(player) then return nil,"owner_not_alive" end
                return player
            end
            wrapped.activate=function() end
            wrapped.deactivate=function(a) env.stop(a) end
            wrapped.map_bind=nil
            wrapped.unmap=nil
            wrapped.follow=nil
        else
            wrapped.activate=function(a)
                a._jev_companion_name=slot.name
                if env.set_name then env.set_name(a,slot.name) end
                if env.activate then env.activate(a) end
            end
            wrapped.find_buddy=function(player)
                local a=env.find_buddy(player)
                if a and (not a._jev_companion_id or a._jev_companion_id==id) then a._jev_companion_id=id;return a end
            end
            wrapped.spawn_buddy=function(player)
                local a,reason=env.spawn_buddy(player)
                if a then a._jev_companion_id=id;a._jev_companion_name=slot.name end
                return a,reason
            end
        end
        if env.visit_start then wrapped.visit_start=function(a,q) share(a);return env.visit_start(a,q) end end
        if env.search_found then
            wrapped.search_found=function(a,products)
                local claimed={}
                for other_id,other in pairs(owner.slots) do
                    local r=other.engine.owners[owner.userid]
                    local active=other_id~=id and r and r.active
                    if active and (active.op=="collect" or active.op=="work") then
                        claimed[active.args.target_guid]=true
                    end
                end
                return env.search_found(a,products,claimed)
            end
        end
        if id~="self" and env.follow then wrapped.follow=function(a,target,recover)
            -- Keep the second follower on a different approach while respecting land.
            if target.userid==owner.userid and id=="buddy2" and env.distance(a,target)>3 then
                local x,z=env.position(target);x=x-2
                if env.valid_point(a,x,z,math.huge) then
                    if recover then env.stop(a) end
                    return env.move(a,x,z)
                end
            end
            return env.follow(a,target,recover)
        end end
        slot.engine=M.new(wrapped);owner.slots[id]=slot;return slot
    end
    function self:save()
        local entries,references={},{}
        for userid,owner in pairs(self.owners) do
            for id,slot in pairs(owner.slots) do
                local actor=get_actor(owner,slot)
                local record,refs
                if actor and actor:IsValid() and actor._jev_companion_slot==slot
                    and (not actor.userid or actor.userid=="") then
                    record,refs=env.save_buddy(actor)
                elseif slot.restore_failed then
                    record=slot.restore_failed
                end
                if record then
                    entries[#entries+1]={userid=userid,id=id,name=slot.name,actor=record}
                    for _,ref in pairs(refs or {}) do references[#references+1]=ref end
                end
            end
        end
        if #entries>0 then return {version=1,buddies=entries},references end
    end
    function self:load(data,newents)
        if type(data)~="table" or data.version~=1 or type(data.buddies)~="table" then return end
        -- Inventory children and their save IDs share the native post-load lookup.
        local entities={}
        for key,entry in pairs(newents) do entities[key]=entry end
        for _,entry in ipairs(data.buddies) do
            if type(entry)=="table" and type(entry.userid)=="string" and entry.userid~=""
                and (entry.id=="buddy1" or entry.id=="buddy2") and type(entry.actor)=="table"
                and entry.actor.prefab=="wilson" then
                local name=Names.name(entry.name)
                local owner=get_owner(entry.userid)
                if name and not owner.slots[entry.id] then
                    local slot=create(owner,entry.id,name)
                    local actor=env.load_buddy(entry.actor,entities)
                    if actor then
                        slot.remember(actor)
                        if env.set_name then env.set_name(actor,name) end
                    else
                        slot.restore_failed=entry.actor
                    end
                end
            end
        end
        for key,entry in pairs(entities) do
            if newents[key]==nil and entry.entity:IsValid() then
                entry.entity:LoadPostPass(entities,entry.data)
            end
        end
    end
    function self:request(player,q)
        if not player or not player:IsValid() or type(player.userid)~="string" or player.userid=="" or type(q)~="table"
            or type(q.id)~="string" or #q.id>100 or type(q.seq)~="number" or q.seq<1 or q.seq%1~=0 then return end
        local owner=get_owner(player.userid)
        if q.op=="roster" then
            env.reply(player,{id=q.id,seq=q.seq,op=q.op,status="completed",reason="roster",roster_version=owner.roster_version,roster=roster(owner)});return
        end
        local id=q.buddy_id or "buddy1"
        if id~="self" and id~="buddy1" and id~="buddy2" then fail(player,q,owner,"invalid_buddy_id");return end
        local slot=owner.slots[id]
        if q.op=="delete" then
            if id=="self" then fail(player,q,owner,"cannot_delete_self");return end
            local args=q.args
            local actor=slot and get_actor(owner,slot)
            if not slot or type(args)~="table" or args.name~=slot.name
                or args.roster_version~=owner.roster_version
                or args.guid~=(actor and actor:IsValid() and actor.GUID or nil) then
                fail(player,q,owner,"buddy_changed");return
            end
            if actor and actor:IsValid() and (actor==player or (actor.userid and actor.userid~="")
                or (actor._jev_companion_owner and actor._jev_companion_owner~=owner.userid)
                or actor._jev_companion_slot~=slot) then
                fail(player,q,owner,"companion_in_use");return
            end
            slot.engine:delete(owner.userid,actor)
            owner.slots[id]=nil
            owner.roster_version=owner.roster_version+1
            env.reply(player,{id=q.id,seq=q.seq,op=q.op,status="completed",buddy_id=id,
                roster_version=owner.roster_version,roster=roster(owner)})
            return
        end
        if q.op=="summon" or q.op=="bind" then
            local name
            if id=="self" then
                name="自己"
            else
                local raw=(q.args or {}).name
                if raw~=nil then name=Names.name(raw) else name=slot and slot.name or (id=="buddy1" and "一号" or "二号") end
                if not name then fail(player,q,owner,"invalid_buddy_name");return end
                for other_id,other in pairs(owner.slots) do
                    if other_id~=id and other.name:lower()==name:lower() then fail(player,q,owner,"duplicate_buddy_name");return end
                end
                if slot and slot.name:lower()~=name:lower() then fail(player,q,owner,"buddy_slot_in_use");return end
            end
            if not slot then
                slot=create(owner,id,name)
                if id=="self" then owner.roster_version=owner.roster_version+1 end
            end
        end
        if not slot then fail(player,q,owner,"buddy_not_summoned");return end
        if slot.restore_failed then fail(player,q,owner,"companion_restore_failed");return end
        slot.engine:request(player,q)
    end
    function self:tick()
        for _,owner in pairs(self.owners) do for _,slot in pairs(owner.slots) do slot.engine:tick() end end
    end
    return self
end
return M
