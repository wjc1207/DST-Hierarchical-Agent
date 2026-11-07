-- Each slot retains the tested single-actor executor and its own revision/cache.
local Core=require("jev_companion_server")
local Names=require("jev_companion_roster")
local Places=require("jev_companion_places")
local M={}
function M.new(env)
    local self={owners={}}
    local function roster(owner)
        local result={}
        for i=1,Names.MAX do
            local slot=owner.slots["buddy"..i]
            if slot then
                local r=slot.engine.owners[owner.userid]
                local a=r and r.buddy or slot.engine.spawned[owner.userid]
                result[#result+1]={id=slot.id,name=slot.name,guid=a and a:IsValid() and a.GUID or nil,
                    bound=r~=nil and r.buddy~=nil and r.buddy:IsValid(),dead=a and a:IsValid() and env.dead(a) or false}
            end
        end
        return result
    end
    local function fail(player,q,owner,reason)
        env.reply(player,{id=q.id,seq=q.seq,op=q.op,status="failed",reason=reason,buddy_id=q.buddy_id,roster=roster(owner)})
    end
    local function create(owner,id,name)
        local slot={id=id,name=name};local wrapped={}
        for key,value in pairs(env) do wrapped[key]=value end
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
            packet.buddy_id=id;packet.buddy_name=slot.name;packet.roster=roster(owner)
            env.reply(player,packet)
        end
        wrapped.snapshot=function(player,a,r)
            if a then a._jev_companion_name=slot.name;share(a) end
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
    function self:request(player,q)
        if not player or not player:IsValid() or type(player.userid)~="string" or player.userid=="" or type(q)~="table"
            or type(q.id)~="string" or #q.id>100 or type(q.seq)~="number" or q.seq<1 or q.seq%1~=0 then return end
        local owner=self.owners[player.userid]
        if not owner then owner={userid=player.userid,slots={}};self.owners[player.userid]=owner end
        if q.op=="roster" then
            env.reply(player,{id=q.id,seq=q.seq,op=q.op,status="completed",reason="roster",roster=roster(owner)});return
        end
        local id=q.buddy_id or "buddy1"
        if id~="buddy1" and id~="buddy2" then fail(player,q,owner,"invalid_buddy_id");return end
        local slot=owner.slots[id]
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
        slot.engine:request(player,q)
    end
    function self:tick()
        for _,owner in pairs(self.owners) do for _,slot in pairs(owner.slots) do slot.engine:tick() end end
    end
    return self
end
return M
