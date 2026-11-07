-- One authoritative actor/executor per actual RPC sender.
local M={}
local OPS={summon=true,bind=true,state=true,intent=true,follow=true,stay=true,stop=true,move=true,collect=true,release=true,say=true,
    recipes=true,equip=true,unequip=true,work=true,craft=true,place=true,search=true,give_owner=true,drop_item=true,deposit=true,withdraw=true,visit_building=true}
local NATIVE={equip=true,unequip=true,work=true,craft=true,place=true,give_owner=true,drop_item=true,deposit=true,withdraw=true}
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
        r.active=nil;halt(r);send(r,q,status,reason,data)
    end
    local function release(r,reason)
        finish(r,"interrupted",reason);halt(r)
        if r.buddy and r.buddy._jev_companion_owner==r.player.userid then
            if env.unmap then env.unmap(r.buddy) end
            if env.deactivate then env.deactivate(r.buddy) end
            if r.buddy:IsValid() then r.buddy._jev_companion_owner=nil end
        end
        r.map_error=nil;r.follow_progress=nil;r.follow_state=nil
        r.buddy=nil;r.token=nil;r.mode="stay";r.revision=0
        r.transfer_totals=nil;r.transfer_order=nil
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
            r.last_seen=now;r.lease_expired=nil;r.mode="follow"
            r.follow_progress=nil;r.follow_state=nil;r.follow_repaths=0
            if env.activate then env.activate(r.buddy) end
            if env.map_bind then local ok,reason=env.map_bind(player,r.buddy);r.map_error=not ok and reason or nil end
            send(r,q,"completed",created and "summoned" or "bound");return
        end
        if not r.buddy or not r.buddy:IsValid() or r.buddy._jev_companion_owner~=player.userid or q.token~=r.token then send(r,q,"failed","binding_changed");return end
        r.last_seen=now;r.lease_expired=nil
        if q.op=="state" then send(r,q,"completed","observed");return end
        if q.op=="intent" then
            if not finite(q.revision) or q.revision%1~=0 or q.revision<=r.revision then send(r,q,"failed","obsolete_intent");return end
            r.revision=q.revision;r.mode="stay";finish(r,"interrupted","player_message");halt(r)
            r.follow_progress=nil;r.follow_state=nil
            send(r,q,"completed","intent_updated");return
        end
        if q.revision~=r.revision then send(r,q,"failed","obsolete_intent");return end
        local args=type(q.args)=="table" and q.args or {}
        q.args=args
        if q.op=="recipes" then
            local data,reason=env.query(r.buddy,args)
            send(r,q,data and "completed" or "failed",reason or "recipes_observed",data);return
        end
        if q.op=="stop" or q.op=="stay" or q.op=="follow" then
            r.mode=q.op=="follow" and "follow" or "stay";finish(r,"interrupted","cancelled_by_owner");halt(r)
            r.follow_progress=nil;r.follow_state=nil;r.follow_repaths=0
            if env.activate then env.activate(r.buddy) end
            send(r,q,"completed",r.mode);return
        end
        if q.op=="release" then release(r,"released");send(r,q,"completed","released");return end
        if q.op=="say" then
            local text=env.validate_text(args.text)
            if not text then send(r,q,"failed","invalid_text");return end
            env.say(r.buddy,text,r);send(r,q,"completed","spoken");return
        end
        if r.active then send(r,q,"failed","busy");return end
        if q.op=="search" then
            local products=args.products
            if type(products)~="table" or #products<1 or #products>6 or not finite(args.timeout_seconds)
                or args.timeout_seconds<.2 or args.timeout_seconds>8 then send(r,q,"failed","invalid_search");return end
            for _,name in ipairs(products) do
                if type(name)~="string" or #name>60 or not name:match("^[a-z0-9_]+$") then send(r,q,"failed","invalid_search");return end
            end
            local point=env.search_point and env.search_point(r.buddy,args)
            if not point then send(r,q,"failed","search_point_unavailable");return end
            q.x,q.z=point.x,point.z;q.deadline=now+args.timeout_seconds
            if env.move(r.buddy,q.x,q.z)==false then send(r,q,"failed","move_rejected");return end
        elseif q.op=="visit_building" then
            local accepted,reason
            if env.visit_start then accepted,reason=env.visit_start(r.buddy,q) end
            if not accepted then send(r,q,"failed",reason or "building_navigation_unavailable");return end
        elseif q.op=="move" then
            if not finite(args.x) or not finite(args.z) or not env.valid_point(r.buddy,args.x,args.z) then send(r,q,"failed","invalid_destination");return end
            q.x,q.z=args.x,args.z
            if env.move(r.buddy,args.x,args.z)==false then send(r,q,"failed","move_rejected");return end
        elseif q.op=="collect" then
            if not valid_guid(args.target_guid) then send(r,q,"failed","invalid_target");return end
            local target=env.collect_target(r.buddy,args.target_guid)
            if not target then send(r,q,"failed","target_unavailable");return end
            q.product=target.product;q.before=env.count(r.buddy,target.product)
            if env.collect(r.buddy,target)==false then send(r,q,"failed","collect_rejected");return end
        elseif NATIVE[q.op] then
            local accepted,reason=env.begin(r.buddy,q,r)
            if not accepted then send(r,q,"failed",reason or "native_action_rejected");return end
        end
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
                        r.follow_progress=nil;follow_state(r,"client_lease_expired")
                    end
                elseif env.dead(r.buddy) then
                    finish(r,"failed","buddy_dead");halt(r);r.mode="stay";r.follow_progress=nil
                    follow_state(r,"buddy_dead")
                elseif r.active then
                    local q=r.active
                    if q.op=="search" then
                        if now>=(q.next_sense or 0) then
                            q.next_sense=now+.25
                            if env.search_found(r.buddy,q.args.products) then finish(r,"completed","resource_found") end
                        end
                        if r.active==q then
                            if env.arrived(r.buddy,q.x,q.z) then finish(r,"completed","search_leg_arrived")
                            elseif now>=q.deadline then finish(r,"completed","search_leg_timeout") end
                        end
                    elseif q.op=="visit_building" then
                        local status,reason,data=env.visit_progress(r.buddy,q)
                        if status then finish(r,status,reason,data) end
                    elseif q.op=="move" and env.arrived(r.buddy,q.x,q.z) then finish(r,"completed","arrived")
                    elseif q.op=="collect" and env.count(r.buddy,q.product)>q.before then finish(r,"completed","inventory_increased")
                    elseif NATIVE[q.op] then
                        local status,reason,data=env.progress(r.buddy,q,r)
                        if status then finish(r,status,reason,data)
                        elseif now-q.started>=8 then finish(r,"failed","action_timeout") end
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
return M
