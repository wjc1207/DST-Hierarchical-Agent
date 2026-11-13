package.path="./mods/dst_buddy_agent/scripts/?.lua;"..package.path
local Names=require("jev_companion_roster")
local Server=require("jev_companion_team_server")
local Client=require("jev_companion_team_client")
local Places=require("jev_companion_places")
local passed=0
local function test(name,fn) local ok,err=pcall(fn);assert(ok,name..": "..tostring(err));passed=passed+1;print("ok "..passed.." - "..name) end
local function fixture()
    local w={time=1,spawns=0,ents={},notices={},packets={},sent={}}
    function w:actor(id,userid)
        local a={GUID=id,userid=userid,x=0,z=0,valid=true,components={}}
        function a:IsValid() return self.valid end
        function a:Remove() self.valid=false end
        function a:HasTag() return false end
        self.ents[id]=a;return a
    end
    w.owner=w:actor(1,"A")
    local env={now=function() return w.time end,position=function(a) return a.x,a.z end,
        distance=function(a,b) return math.abs(a.x-b.x) end,dead=function(a) return a.dead==true end,
        stop=function(a) a.stopped=true end,find_buddy=function() return nil end,
        spawn_buddy=function() w.spawns=w.spawns+1;return w:actor(100+w.spawns) end,
        follow=function() return true end,move=function() return true end,valid_point=function() return true end,
        snapshot=function(player,a,r) return {bound=a~=nil,token=r.token,revision=r.revision,mode=r.mode,
            capabilities={"collect","follow","stay","move"},world={day=1,phase="day",time=.2},
            buddy=a and {guid=a.GUID,inventory={},position={x=a.x,z=a.z},owner_distance=0},nearby={}} end,
        reply=function(_,packet) w.packets[#w.packets+1]=packet;if w.client then w.client:receive(packet) end end,
        set_name=function(a,name) a.displayed_name=name end,
        say=function(a,text) w.spoken={guid=a.GUID,text=text};w.speeches=w.speeches or {};w.speeches[#w.speeches+1]=w.spoken end,validate_text=Client.validate_text,
        collect_target=function(_,id) return {guid=id,product="cutgrass"} end,count=function() return 0 end,collect=function() return true end}
    w.env=env;w.network={};w.server=Server.new(env)
    w.client=Client.new({nonce="client",now=env.now,http_enabled=true,notice=function(text) w.notices[#w.notices+1]=text end,
        send=function(q) w.sent[#w.sent+1]=q;if w.delay then w.network[#w.network+1]=q else w.server:request(w.owner,q) end end,
        http=function(payload,callback) w.http=payload;callback({session_id=payload.session_id,sequence=payload.sequence,commands={},feedback=w.feedback or {}}) end})
    function w:record(id) return self.server.owners.A.slots[id].engine.owners.A end
    return w
end
test("mentions resolve exact owner-local names, multiple targets and all; unknown never widens scope",function()
    local roster={{id="buddy1",name="小威"},{id="buddy2",name="二号"}}
    local ids,body=Names.resolve("@小威 采草",roster);assert(#ids==1 and ids[1]=="buddy1" and body=="采草")
    ids=Names.resolve("@小威 @二号 回来",roster);assert(#ids==2)
    ids=Names.resolve("@全体 停止",roster);assert(#ids==2)
    assert(not Names.resolve("@陌生人 采草",roster))
    assert(not Names.name("全体") and not Names.name("自己") and not Names.name("bad name") and not Names.name("@小威"))
    assert(Names.name("小威"))
end)
test("delete clears a dead or released actor, leaves other slots, and rejects a stale delete",function()
    local w=fixture()
    w.client:summon("一号")
    w.client:summon("二号")
    local first,second=w:record("buddy1").buddy,w:record("buddy2").buddy
    first.dead=true
    local drops=0
    second.components.inventory={DropEverything=function() drops=drops+1 end}
    local row=w.client:panel_state().roster[1]
    w.client:delete(row)
    assert(not first:IsValid() and second:IsValid() and not w.client.children.buddy1)
    assert(#w.client.roster==1 and w.client.roster[1].id=="buddy2")
    w.client:summon("一号")
    local replacement=w:record("buddy1").buddy
    w.client:delete(row)
    assert(replacement:IsValid() and #w.client.roster==2)
    w.client:command("release @二号")
    local released=w.client:panel_state().roster[2]
    assert(not released.bound and second:IsValid())
    w.client:delete(released)
    assert(not second:IsValid() and drops==1 and replacement:IsValid())
end)
test("two named summons create two real actors and repeated summons reuse their exact slots",function()
    local w=fixture();w.client:command("summon 小威");w.client:command("summon 二号")
    assert(w.spawns==2 and w:record("buddy1").buddy~=w:record("buddy2").buddy)
    assert(w:record("buddy1").buddy.displayed_name=="小威" and w:record("buddy2").buddy.displayed_name=="二号")
    w.client:command("summon 小威");assert(w.spawns==2)
    w.client:command("summon 第三位");assert(w.spawns==2)
    assert(w.client.children.buddy1.token and w.client.children.buddy2.token)
end)
test("rapid summons are serialized against the authoritative roster even when RPC replies are delayed",function()
    local w=fixture();w.delay=true
    w.client:command("summon 小威");w.client:command("summon 二号")
    for _=1,12 do
        if #w.network>0 then w.server:request(w.owner,table.remove(w.network,1)) end
    end
    assert(w.spawns==2 and w.server.owners.A.slots.buddy1.name=="小威" and w.server.owners.A.slots.buddy2.name=="二号")
    assert(w.client.children.buddy1.token and w.client.children.buddy2.token)
end)
test("a failed spawn frees its roster slot and a new name gets a new client epoch",function()
    local w=fixture();local spawn=w.env.spawn_buddy
    w.env.spawn_buddy=function() return nil,"no_safe_spawn_land" end
    w.client:command("summon 小威");assert(not w.server.owners.A.slots.buddy1)
    local generation=w.client.child_generation
    w.env.spawn_buddy=spawn;w.client:command("summon 二号")
    assert(w.server.owners.A.slots.buddy1.name=="二号" and w.client.child_generation>generation)
    assert(w.spawns==1 and w.client.children.buddy1.token)
end)
test("a targeted instruction increments only that actor revision while the other follows",function()
    local w=fixture();w.client:command("summon 小威");w.client:command("summon 二号")
    w.client:command("do @小威 收集草")
    assert(w:record("buddy1").revision==1 and w:record("buddy1").mode=="stay")
    assert(w:record("buddy2").revision==0 and w:record("buddy2").mode=="follow")
    assert(w.client.control.target_ids[1]=="buddy1")
end)
test("no mention targets the whole bound team and native all-stop works without a model",function()
    local w=fixture();w.client:command("summon 小威");w.client:command("summon 二号")
    w.client:command("do 一起收集材料");assert(w:record("buddy1").revision==1 and w:record("buddy2").revision==1)
    w.client:command("stop @全体")
    assert(w:record("buddy1").mode=="stay" and w:record("buddy2").mode=="stay")
    assert(not w.client.children.buddy1.control.enabled and not w.client.children.buddy2.control.enabled)
end)
test("unknown names do not pause either actor and released slots do not block all-stop",function()
    local w=fixture();w.client:command("summon 小威");w.client:command("summon 二号")
    local before=#w.sent;w.client:command("do @陌生人 停止");assert(#w.sent==before)
    w.client:command("release @小威");assert(not w.client.children.buddy1.token)
    w.client:command("stop");assert(w:record("buddy2").revision==1)
end)
test("team HTTP carries two independent observations and can dispatch two native actions",function()
    local w=fixture();w.client:command("summon 小威");w.client:command("summon 二号")
    w.client:command("do 一起采草");w.client:tick()
    assert(w.http.state.team and w.http.state.members.buddy1.ready and w.http.state.members.buddy2.ready)
    assert(w.http.state.roster[1].name=="小威" and #w.http.state.control.target_ids==2)
    for _,id in ipairs({"buddy1","buddy2"}) do
        local c=w.client.children[id]
        c:apply_command({id="act_"..id,op="collect",token=c.token,revision=c.control.revision,args={target_guid=99}})
    end
    assert(w:record("buddy1").active and w:record("buddy2").active)
end)
test("actor feedback reaches the matching native speaker once, with no legacy team broadcast",function()
    local w=fixture();w.client:command("summon 小威");w.client:command("summon 二号")
    w.client:command("do 一起采草")
    w.feedback={{id="legacy",revision=1,team=true,text="队伍消息不应显示"},
        {id="legacy_actor",revision=1,team=true,buddy_id="buddy1",text="也不能冒充角色"},
        {id="speech",revision=1,buddy_id="buddy1",text="我去采草。"},
        {id="speech",revision=1,buddy_id="buddy2",text="我去找树枝。"},
        {id="unknown",revision=1,buddy_id="buddy3",text="未知伙伴"}}
    w.client:tick();w.time=w.time+.25;w.client:tick()
    assert(#w.speeches==2 and w.speeches[1].guid==w:record("buddy1").buddy.GUID
        and w.speeches[2].guid==w:record("buddy2").buddy.GUID)
    assert(w.speeches[1].text=="我去采草。" and w.speeches[2].text=="我去找树枝。")
    local own,other=0,0
    for _,s in ipairs(w.notices) do
        assert(not s:find("队伍消息不应显示",1,true) and not s:find("也不能冒充角色",1,true))
        if s=="小威：我去采草。" then own=own+1 end
        if s=="二号：我去找树枝。" then other=other+1 end
    end
    assert(own==1 and other==1)
end)

test("one companion's completion speech resumes its follow without stopping the other",function()
    local w=fixture();w.client:command("summon 小威");w.client:command("summon 二号")
    w.client:command("do 一起采草")
    w.client.children.buddy2:send("collect",{target_guid=99})
    local active=w:record("buddy2").active
    w.feedback={{id="done",revision=1,buddy_id="buddy1",finish=true,text="我采好了，回来啦。"}}
    w.client:tick();w.time=w.time+.25;w.client:tick()
    assert(w:record("buddy1").mode=="follow" and not w.client.children.buddy1.control.enabled)
    assert(w:record("buddy2").active==active and w.client.children.buddy2.control.enabled)
    assert(#w.speeches==1 and w.speeches[1].guid==w:record("buddy1").buddy.GUID)
end)

test("team autonomy works with one or two buddies and resumes after a scoped instruction",function()
    local w=fixture()
    local goal="维持营地补给"
    w.client:command("summon 小威")
    w.client:command("auto "..goal)
    local first=w.client.children.buddy1
    assert(first.control.enabled and first.control.mode=="autonomous" and first.control.goal==goal)
    w.client:command("summon 二号")
    local second=w.client.children.buddy2
    assert(second.control.enabled and second.control.mode=="autonomous" and second.control.goal==goal)
    second:send("collect",{target_guid=99})
    local active=w:record("buddy2").active
    w.client:command("do @小威 收集草")
    assert(first.control.mode=="instruction" and first.control.instruction=="收集草" and not first.control.goal)
    local revision=first.control.revision
    w.feedback={{id="done",revision=revision,buddy_id="buddy1",finish=true,text="我采好了。"}}
    w.client:tick()
    assert(first.control.mode=="autonomous" and first.control.enabled and first.control.goal==goal)
    assert(first.control.revision==revision+1 and not first.control.instruction and #first.control.messages==0)
    assert(w:record("buddy2").active==active)
    assert(#w.speeches==1 and w.speeches[1].guid==w:record("buddy1").buddy.GUID)

    w.client:command("auto off")
    assert(w.client.control.mode=="instruction" and not w.client.control.enabled)
    for _,id in ipairs({"buddy1","buddy2"}) do
        assert(not w.client.children[id].control.enabled and w:record(id).mode=="stay")
    end
    w.time=w.time+.25
    w.client:tick()
    assert(not first.control.enabled and #w.speeches==1)
    w.client:command("auto")
    assert(first.control.enabled and first.control.goal==goal and second.control.enabled)
end)

test("auto off keeps an active temporary instruction and returns its actor to follow",function()
    local w=fixture();w.client:command("summon 小威");w.client:command("summon 二号")
    w.client:command("auto 维持营地补给")
    w.client:command("do @小威 采草")
    local first=w.client.children.buddy1
    first:send("collect",{target_guid=99})
    local active,revision=w:record("buddy1").active,first.control.revision
    w.client:command("auto off")
    local panel=w.client:panel_state()
    assert(panel.mode=="instruction" and panel.goal=="维持营地补给")
    assert(panel.roster[1].status=="临时指令 · 采草" and panel.roster[2].status=="待命")
    assert(first.control.enabled and first.control.revision==revision and w:record("buddy1").active==active)
    assert(not w.client.children.buddy2.control.enabled and w:record("buddy2").mode=="stay")
    w.feedback={{id="done-after-off",revision=revision,buddy_id="buddy1",finish=true,text="我采好了。"}}
    w.client:tick()
    assert(not first.control.enabled and w:record("buddy1").mode=="follow")
end)

test("same-name summon preserves a bound actor's active instruction",function()
    local w=fixture();w.client:command("summon 小威")
    w.client:command("do @小威 采草")
    local first=w.client.children.buddy1
    first:send("collect",{target_guid=99})
    local active,revision=w:record("buddy1").active,first.control.revision
    w.client:command("summon 小威")
    assert(w.spawns==1 and first.control.enabled and first.control.revision==revision)
    assert(active and w:record("buddy1").active==active)
end)

test("chat listening routes only addressed requests and mirrors the slash toggle",function()
    local w=fixture();w.client:command("summon 小威");w.client:command("summon 二号")
    assert(not w.client:chat("@小威 采草"))
    w.client:command("ai")
    assert(w.client:panel_state().listening)
    local before=#w.sent
    assert(not w.client:chat("大家好") and not w.client:chat("@其他玩家 你好")
        and not w.client:chat("@自己 采草") and #w.sent==before)
    assert(w.client:chat("@小威 采草") and w:record("buddy1").revision==0 and w:record("buddy2").revision==0)
    assert(w.client.children.buddy1.control.dialogue_revision==1)
    assert(not w.client.children.buddy2.control.enabled)
    w.client:command("stop @all")
    assert(w:record("buddy2").revision==1)
    w.client:set_listening(false)
    assert(not w.client:panel_state().listening and not w.client:chat("@小威 采草"))
    w.client:command("do @小威 采草")
    assert(w.client.children.buddy1.control.enabled and not w.client.children.buddy2.control.enabled)
end)

test("dialogue preserves native work and rejects stale finish feedback",function()
    local w=fixture();w.client:command("summon 小威");w.client:command("auto 维持营地补给")
    w.client:set_listening(true)
    local c=w.client.children.buddy1
    c:send("collect",{target_guid=99})
    local record=w:record("buddy1")
    local active,revision,sent=record.active,record.revision,#w.sent
    assert(w.client:chat("@小威 采了几根草？"))
    local dialogue=c.control.dialogue_revision
    assert(record.active==active and record.revision==revision and #w.sent==sent)
    assert(c.control.mode=="autonomous" and c.control.goal=="维持营地补给")
    assert(w.client:chat("@小威 停止"))
    c:feedback({id="stale",revision=revision,dialogue_revision=dialogue,finish=true,text="旧回复"})
    assert(record.active==active and #w.sent==sent)
    w.client:command("stop @小威")
    assert(not record.active and record.revision==revision+1 and not c.control.enabled)
end)
test("an idle conversation reply preserves the buddy's current movement mode",function()
    local w=fixture();w.client:command("summon 小威");w.client:command("stay @小威")
    w.client:set_listening(true);assert(w.client:chat("@小威 你好"))
    local c=w.client.children.buddy1
    local revision=c.control.revision
    c:feedback({id="hello",revision=revision,dialogue_revision=c.control.dialogue_revision,
        finish=true,keep_mode=true,text="你好！"})
    assert(w:record("buddy1").mode=="stay" and c.control.revision==revision and not c.control.enabled)
    assert(#w.speeches==1 and w.speeches[1].text=="你好！")
end)

test("server validates names and isolates owner identities even when slot IDs match",function()
    local w=fixture();w.client:command("summon 小威")
    w.server:request(w.owner,{id="x:1",seq=1,op="summon",buddy_id="buddy2",args={name="小威"}})
    assert(w.packets[#w.packets].reason=="duplicate_buddy_name")
    w.server:request(w.owner,{id="x:2",seq=2,op="summon",buddy_id="buddy2",args={name="@bad"}})
    assert(w.packets[#w.packets].reason=="invalid_buddy_name")
    local other=w:actor(8,"B")
    w.server:request(other,{id="b:1",seq=1,op="state",buddy_id="buddy1",token=w.client.children.buddy1.token,revision=0})
    assert(w.packets[#w.packets].reason=="buddy_not_summoned")
end)
test("a heartbeat or new message for one actor cannot keep or cancel another actor's action",function()
    local w=fixture();w.client:command("summon 小威");w.client:command("summon 二号")
    for _,id in ipairs({"buddy1","buddy2"}) do
        local c=w.client.children[id];c:send("collect",{target_guid=99})
    end
    w.client:command("do @小威 回来")
    assert(not w:record("buddy1").active and w:record("buddy2").active)
    w.time=4;w.client.children.buddy2:send("state")
    w.time=5.2;w.server:tick()
    assert(w:record("buddy1").lease_expired and not w:record("buddy2").lease_expired)
end)
test("search does not yield for a resource another teammate is working on",function()
    local w=fixture()
    w.env.search_point=function(_,args) return {x=args.x,z=args.z} end
    w.env.search_found=function(_,products,claimed)
        assert(products[1]=="cutgrass")
        return not claimed[99]
    end
    w.env.search_arrived=function() return false end
    w.client:command("summon 小威");w.client:command("summon 二号")
    w.client.children.buddy2:send("collect",{target_guid=99})
    w.client.children.buddy1:send("search",{point_id="4:0",x=16,z=0,products={"cutgrass"},timeout_seconds=2})
    local record=w:record("buddy1")
    local search=assert(record.active)
    w.server:tick()
    assert(record.active==search and search.last.status=="running")
    w.client:command("stop @二号")
    w.time=w.time+.3;w.server:tick()
    assert(not record.active and search.last.reason=="resource_found")
end)
test("shared building facts retain provenance and are not another actor's inventory or technology",function()
    local a={_jev_companion_places={rows={},order={}}};local b={_jev_companion_places={rows={},order={}}}
    b._jev_companion_places.rows["building:99"]={id="building:99",guid=99,prefab="researchlab",x=1,z=2,last_seen_at=3}
    b._jev_companion_places.order={"building:99"}
    Places.share(a,b,"buddy2")
    assert(a._jev_companion_places.rows["building:99"].shared_from=="buddy2")
    a._jev_companion_places.rows["building:99"].x=10
    assert(b._jev_companion_places.rows["building:99"].x==1)
end)
print("1.."..passed)
