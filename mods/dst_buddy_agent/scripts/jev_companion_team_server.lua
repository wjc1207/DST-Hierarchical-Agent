-- Each slot retains the tested single-actor executor and its own revision/cache.
local Core=require("jev_companion_server")
local Names=require("jev_companion_roster")
local Places=require("jev_companion_places")
local M={}
function M.new(env)
    local self={owners={}}
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
        for i=1,Names.MAX do
            local slot=owner.slots["buddy"..i]
            if slot then
                local r=slot.engine.owners[owner.userid]
                local a=get_actor(owner,slot)
                result[#result+1]={id=slot.id,name=slot.name,guid=a and a:IsValid() and a.GUID or nil,
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
        local function remember(a)
            slot.actor=a
            slot.engine.spawned[owner.userid]=a
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
                if not (r and r.buddy) and not slot.engine.spawned[owner.userid] then owner.slots[id]=nil end
            end
            packet.buddy_id=id;packet.buddy_name=slot.name;packet.roster_version=owner.roster_version;packet.roster=roster(owner)
            env.reply(player,packet)
        end
        wrapped.snapshot=function(player,a,r)
            if a then remember(a);share(a) end
            local state=env.snapshot(player,a,r);state.buddy_id=id;state.buddy_name=slot.name
            return state
        end
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
        if env.follow then wrapped.follow=function(a,target,recover)
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
        slot.engine=Core.new(wrapped);owner.slots[id]=slot;return slot
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
        if id~="buddy1" and id~="buddy2" then fail(player,q,owner,"invalid_buddy_id");return end
        local slot=owner.slots[id]
        if q.op=="delete" then
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
            local raw=(q.args or {}).name
            local name
            if raw~=nil then name=Names.name(raw) else name=slot and slot.name or (id=="buddy1" and "一号" or "二号") end
            if not name then fail(player,q,owner,"invalid_buddy_name");return end
            for other_id,other in pairs(owner.slots) do
                if other_id~=id and other.name:lower()==name:lower() then fail(player,q,owner,"duplicate_buddy_name");return end
            end
            if slot and slot.name:lower()~=name:lower() then fail(player,q,owner,"buddy_slot_in_use");return end
            if not slot then slot=create(owner,id,name) end
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
