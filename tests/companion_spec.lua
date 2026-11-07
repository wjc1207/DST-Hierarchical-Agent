package.path="./mods/jev_ai_companion/scripts/?.lua;"..package.path
local S=require("jev_companion_server")
local C=require("jev_companion_client")
local passed=0
local function test(name,fn) local ok,error=pcall(fn);assert(ok,name..": "..tostring(error));passed=passed+1;print("ok "..passed.." - "..name) end
local function actor(id,userid)
    local a={GUID=id,userid=userid,valid=true,x=0,z=0,inventory={},stops=0}
    function a:IsValid() return self.valid end
    return a
end
local function world()
    local w={time=0,replies={},moves=0,collections=0,player=actor(1,"A"),buddy=actor(2),seq=0,revision=0}
    local function pos(a) return a.x,a.z end
    w.env={now=function() return w.time end,position=pos,
        distance=function(a,b) return math.sqrt((a.x-b.x)^2+(a.z-b.z)^2) end,
        stop=function(a) a.dest=nil;a.stops=a.stops+1 end,
        dead=function(a) return a.dead end,
        find_buddy=function() return not w.buddy._jev_companion_owner and w.buddy or nil end,
        spawn_buddy=function() return nil,"no_safe_spawn_land" end,
        snapshot=function(p,a,r) return {bound=a~=nil,revision=r.revision,token=r.token,mode=r.mode,buddy=a and {guid=a.GUID}} end,
        reply=function(p,q) w.replies[#w.replies+1]={player=p,packet=q} end,
        valid_point=function(a,x,z,radius) return (x-a.x)^2+(z-a.z)^2<=(radius or 20)^2 end,
        move=function(a,x,z) w.moves=w.moves+1;a.dest={x=x,z=z};return true end,
        arrived=function(a,x,z) return (a.x-x)^2+(a.z-z)^2<=2.5^2 end,
        collect_target=function(_,guid) return guid==99 and {guid=99,product="cutgrass"} or nil end,
        count=function(a,p) return a.inventory[p] or 0 end,
        collect=function() w.collections=w.collections+1;return true end,
        say=function(_,text) w.spoken=text end,validate_text=C.validate_text}
    w.server=S.new(w.env)
    function w:request(op,args,extra,player)
        self.time=self.time+.11;self.seq=self.seq+1
        local q={id="client:"..self.seq,seq=self.seq,op=op,args=args or {},token=self.token,revision=self.revision}
        for k,v in pairs(extra or {}) do q[k]=v end
        self.last=q;self.server:request(player or self.player,q)
        local p=self.replies[#self.replies].packet
        if (op=="bind" or op=="summon") and p.status=="completed" then self.token=p.token end
        return p
    end
    function w:step(dt) self.time=self.time+(dt or .1);self.server:tick() end
    return w
end
test("each RPC sender owns only their bound actor",function()
    local w=world();assert(w:request("bind").status=="completed")
    assert(w.buddy._jev_companion_owner=="A")
    local other=actor(3,"B")
    assert(w:request("bind",nil,{id="other:1",seq=1},other).status=="failed")
    assert(w:request("move",{x=10,z=0},{id="other:2",seq=2},other).reason=="binding_changed")
    assert(w.moves==0)
end)
test("follow is local behavior, arrival needs no model calls",function()
    local w=world();w:request("bind");w.player.x=10;w:step()
    assert(w.moves==1 and w.buddy.dest.x==10)
    w.buddy.x=8;w:step();assert(not w.buddy.dest)
end)

test("distant owners can direct local work and be followed without a distance cap",function()
    local w=world();w:request("bind");w.player.x=500;w:step()
    assert(w.moves==1 and w.buddy.dest.x==500)
    assert(w:request("move",{x=10,z=0}).status=="running")
    w.buddy.x=10;w:step()
    assert(w.replies[#w.replies].packet.status=="completed")
    assert(w:request("move",{x=40,z=0}).reason=="invalid_destination")
    assert(w:request("collect",{target_guid=99}).status=="running")
    w.buddy.inventory.cutgrass=1;w:step()
    assert(w.replies[#w.replies].packet.reason=="inventory_increased")
end)

test("native following wakes the actor before locomotion and recovers cached failed paths",function()
    local buddy,owner=actor(1),actor(2,"A");buddy.asleep=true;owner.x=500
    function buddy:IsAsleep() return self.asleep end
    function buddy:ClearBufferedAction() self.cleared=true end
    buddy.entity={SetCanSleep=function(_,value) buddy.can_sleep=value;if not value then buddy.asleep=false end end}
    local loco={path={handle=10},isupdating=false,Clear=function() buddy.cleared=true end,
        Stop=function(self) self.path=nil;buddy.stopped=true end,
        GoToEntity=function(self,target,action,run)
            assert(not buddy:IsAsleep(),"locomotion attempted on a sleeping actor")
            assert(target==owner and action==nil and run==true)
            self.isupdating=true;buddy.target=target
        end,WantsToMoveForward=function() return buddy.target~=nil end}
    buddy.components={locomotor=loco}
    local adapter=require("jev_companion_world").new({},function() end,C.validate_text)
    assert(adapter.follow(buddy,owner,true))
    assert(buddy.can_sleep==false and buddy.target==owner and buddy.stopped and not loco.path)
    local motion=adapter.motion(buddy)
    assert(motion.asleep==false and motion.updating and motion.moving and not motion.path_pending)
    adapter.deactivate(buddy);assert(buddy.can_sleep==true)
end)

test("following retries a stale path while respecting in-progress path searches",function()
    local w=world();local pending=false;local resets=0;local events={}
    w.player.x=500
    w.env.motion=function() return {asleep=false,path_pending=pending} end
    w.env.follow=function(a,owner,recover) if recover then resets=resets+1 end;return true end
    w.env.trace=function(event,data) events[#events+1]={event=event,data=data} end
    w:request("bind")
    for _=1,7 do w:request("state");w:step(1) end
    assert(resets>=1 and #events>0 and events[1].data.status=="following")
    w.buddy.x=20;w:request("state");w:step(1)
    local previous=resets
    w:request("follow");pending=true
    for _=1,10 do w:request("state");w:step(1) end
    assert(resets==previous,"a pending native search was restarted prematurely")
    for _=1,6 do w:request("state");w:step(1) end
    assert(resets>previous and w.server.owners.A.follow_state.repaths>0)
end)

test("binding keeps the server actor active and release or disconnect restores its lifecycle",function()
    local w=world()
    w.env.activate=function(a) a.keepawake=true end
    w.env.deactivate=function(a) a.keepawake=nil end
    w:request("bind");assert(w.buddy.keepawake)
    w:request("stay");w:step(5);assert(w.buddy.keepawake)
    w:request("release");assert(not w.buddy.keepawake)
    w:request("summon");assert(w.buddy.keepawake)
    w.player.valid=false;w:step();assert(not w.buddy.keepawake)
end)
test("move completes only after observing arrival and duplicate IDs never repeat",function()
    local w=world();w:request("bind");local p=w:request("move",{x=10,z=0})
    assert(p.status=="running");local q=w.last
    w.server:request(w.player,q);assert(w.moves==1)
    w:step();assert(w.server.owners.A.active)
    w.buddy.x=9;w:step();assert(w.replies[#w.replies].packet.reason=="arrived")
    w.server:request(w.player,q);assert(w.moves==1 and w.replies[#w.replies].packet.status=="completed")
end)
test("collect confirmation requires inventory increase, not just issuance",function()
    local w=world();w:request("bind");assert(w:request("collect",{target_guid=99}).status=="running")
    w:step();assert(w.server.owners.A.active)
    w.buddy.inventory.cutgrass=1;w:step();assert(w.replies[#w.replies].packet.reason=="inventory_increased")
end)

test("collection accepts real entity IDs beyond unrelated numeric limits",function()
    local w=world();w:request("bind")
    w.env.collect_target=function(_,guid) return guid==200000001 and {guid=guid,product="cutgrass"} or nil end
    assert(w:request("collect",{target_guid=200000001}).status=="running")
    w.buddy.inventory.cutgrass=1;w:step()
    assert(w.replies[#w.replies].packet.status=="completed")
end)
test("new intent cancels work and stale planning cannot resume it",function()
    local w=world();w:request("bind");w:request("move",{x=10,z=0})
    local old=w.replies[#w.replies].packet.state.version
    w.revision=1;w:request("intent")
    assert(not w.server.owners.A.active and not w.buddy.dest)
    assert(w:request("collect",{target_guid=99},{revision=0}).reason=="obsolete_intent")
    assert(w.replies[#w.replies].packet.state.version>old and w.collections==0)
end)
test("client lease and native action timeout independently stop work",function()
    local w=world();w:request("bind");w:request("move",{x=10,z=0});w:step(4.1)
    assert(w.replies[#w.replies].packet.reason=="client_lease_expired" and not w.buddy.dest)
    local stops=w.buddy.stops;w:step();assert(w.buddy.stops==stops)
    w:request("move",{x=10,z=0})
    for _=1,9 do w:request("state");w:step(.9) end
    assert(not w.server.owners.A.active)
    local found=false;for _,r in ipairs(w.replies) do if r.packet.reason=="action_timeout" then found=true end end
    assert(found)
end)
test("invalid targets do not create actions or change follow mode",function()
    local w=world();w:request("bind")
    assert(w:request("move",{x=999,z=0}).reason=="invalid_destination")
    assert(w.server.owners.A.mode=="follow")
    assert(w:request("collect",{target_guid=50}).reason=="target_unavailable" and w.collections==0)
end)
test("owner disconnect releases the actor and a stale binding token fails",function()
    local w=world();w:request("bind");local token=w.token;w:request("release");w:request("bind")
    assert(w.token~=token and w:request("stay",nil,{token=token}).reason=="binding_changed")
    w:request("move",{x=10,z=0})
    w.env.snapshot=function(player) assert(player:IsValid(),"snapshot of removed owner") return {} end
    w.player.valid=false;w:step();assert(not w.buddy._jev_companion_owner)
end)

local function client_world()
    local w={time=1,wire={},http={},notices={}}
    w.env={now=function() return w.time end,nonce="local",http_enabled=true,
        notice=function(text) w.notices[#w.notices+1]=text end,
        send=function(q) w.wire[#w.wire+1]=q end,
        http=function(payload,callback) w.http[#w.http+1]={payload=payload,callback=callback} end}
    w.client=C.new(w.env)
    function w:receive(index,status,version,revision,extra)
        local q=self.wire[index];local p={id=q.id,seq=q.seq,op=q.op,status=status,reason="test",token="bound",revision=revision or q.revision,
            state={bound=true,token="bound",version=version,revision=revision or q.revision,mode="stay"}}
        for k,v in pairs(extra or {}) do p[k]=v end
        self.client:receive(p);return p
    end
    function w:bind() self.client:command("bind");self:receive(#self.wire,"completed",1,0) end
    return w
end
test("chat raises control revision, stops first and preserves the original instruction",function()
    local w=client_world();w:bind();w.client:command("do 收集三根草")
    assert(w.wire[#w.wire].op=="intent" and w.client.control.enabled)
    w:receive(#w.wire,"completed",2,1)
    w.client:command("do 先回来")
    assert(w.client.control.revision==2 and w.client.control.instruction=="收集三根草" and #w.client.control.messages==2)
    assert(not w.client:ready())
end)
test("fresh state versions can arrive on older request IDs; cached states never freshen age",function()
    local w=client_world();w:bind()
    local first=w.client:send("move",{x=5,z=0})
    local i=#w.wire;w.client:send("state");local j=#w.wire
    w:receive(i,"running",2);w:receive(j,"completed",3)
    w.time=2;w:receive(i,"completed",4)
    assert(w.client.snapshot.version==4 and w.client.last_snapshot==2)
    w.client:receive({id=first,seq=w.wire[i].seq,op="move",status="completed",state={version=4}})
    assert(w.client.last_snapshot==2)
end)
test("late local commands are rejected and command redelivery does not duplicate RPCs",function()
    local w=client_world();w:bind()
    local before=#w.wire
    w.client:apply_command({id="obsolete",op="collect",revision=9,token="bound",args={target_guid=99}})
    assert(#w.wire==before and w.client.results.obsolete.status=="interrupted")
    local command={id="live",op="collect",revision=0,token="bound",args={target_guid=99}}
    w.client:apply_command(command);w.client:apply_command(command)
    assert(#w.wire==before+1)
end)
test("finishing a request increments intent before follow and speech, preventing old queued work",function()
    local w=client_world();w:bind();w.client.control.enabled=true
    w.client:feedback({id="done",text="已完成",revision=0,finish=true})
    assert(w.client.control.revision==1 and not w.client.control.enabled and w.wire[#w.wire].op=="intent")
    w:receive(#w.wire,"completed",2,1)
    assert(w.wire[#w.wire-1].op=="follow" and w.wire[#w.wire].op=="say")
    assert(w.wire[#w.wire].revision==1)
end)
test("local backend loss stops an AI task while manual following needs no backend",function()
    local w=client_world();w:bind();w.client.control.enabled=true;w.time=5;w.client:tick()
    assert(not w.client.control.enabled)
    local count=0;for _,q in ipairs(w.wire) do if q.op=="intent" then count=count+1 end end
    assert(count==1)
    w.client:tick();assert(not w.client.control.enabled)
end)
test("intent acknowledgement loss is retried and deferred manual behavior executes once after synchronization",function()
    local w=client_world();w:bind();w.client:command("follow")
    local first=#w.wire
    w.time=w.time+.6;w.client:tick()
    local retry
    for i=first+1,#w.wire do if w.wire[i].op=="intent" then retry=i end end
    assert(retry)
    w:receive(retry,"completed",2,1)
    local count=0;for _,q in ipairs(w.wire) do if q.op=="follow" then count=count+1 end end
    assert(count==1)
    w:receive(first,"completed",3,1)
    count=0;for _,q in ipairs(w.wire) do if q.op=="follow" then count=count+1 end end
    assert(count==1)
end)
test("unbound snapshots do not emit infinite JSON ages and text input is bounded",function()
    local w=client_world();w.client:tick()
    assert(w.http[1].payload.state.snapshot_age_ms==nil)
    assert(not C.validate_text("bad\ntext") and not C.validate_text(string.rep("中",257)))
    assert(C.validate_text("I'm collecting 草 🌲"))
end)
test("an absent local program backs off HTTP while a new explicit instruction retries promptly",function()
    local w=client_world();w:bind();w.client:tick();assert(#w.http==1)
    w.http[1].callback(nil)
    w.time=w.time+.2;w.client.dirty=true;w.client:tick();assert(#w.http==1)
    w.time=w.time+.4;w.client:tick();assert(#w.http==2)
    w.http[2].callback(nil)
    w.client:command("do 收集草");w.client:tick();assert(#w.http==3)
    assert(w.client.control.enabled)
end)
test("real world adapter offers safe live resources and counts the companion inventory",function()
    local W=require("jev_companion_world")
    local p,b,g=actor(1,"A"),actor(2),actor(99)
    for _,a in ipairs({p,b,g}) do
        a.Transform={GetWorldPosition=function() return a.x,0,a.z end}
        function a:GetDistanceSqToInst(other) return (self.x-other.x)^2+(self.z-other.z)^2 end
        function a:HasTag() return false end
    end
    g.prefab="grass";g.x=2;g.components={pickable={product="cutgrass",caninteractwith=true,
        CanBePicked=function() return true end,IsStuck=function() return false end}}
    local item={prefab="cutgrass",GUID=88,components={stackable={StackSize=function() return 2 end}}}
    b.prefab="wilson";b.sg={};b.components={inventory={FindItems=function() return {item} end},
        health={IsDead=function() return false end,currenthealth=100,maxhealth=150},
        locomotor={PushAction=function(_,action) b.issued=action end,GoToPoint=function() end,Clear=function() end,Stop=function() end}}
    local G={TheSim={FindEntities=function() return {p,b,g} end},Ents={[99]=g},GetTime=function() return 1 end,
        TheWorld={state={cycles=0,phase="day",time=.2},Map={IsPassableAtPoint=function() return true end,IsAboveGroundAtPoint=function() return true end}},
        ACTIONS={PICK="pick",PICKUP="pickup"},BufferedAction=function(a,target,action) return {doer=a,target=target,action=action} end}
    local adapter=W.new(G,function() end,C.validate_text)
    assert(adapter.find_buddy(p)==b)
    local state=adapter.snapshot(p,b,{revision=1,token="bound",mode="stay"})
    assert(state.buddy.inventory.cutgrass==2 and #state.nearby==1 and state.nearby[1].guid==99)
    local offered=adapter.collect_target(b,99);assert(offered.product=="cutgrass")
    adapter.collect(b,offered);assert(b.issued.action=="pick" and b.issued.doer==b)
    b._jev_probe_owner="A";assert(not adapter.find_buddy(p))
    b._jev_probe_owner=nil;b._jev_companion_created_for="B";assert(not adapter.find_buddy(p))
    b._jev_companion_created_for="A";assert(adapter.find_buddy(p)==b)
    g.components.pickable.CanBePicked=function() return false end;assert(not adapter.collect_target(b,99))
end)
test("Mod entry wires slash commands, real actor snapshots and compliant local HTTP without model keys",function()
    local player,buddy=actor(1,"A"),actor(2)
    local now,packets,requests,client_handlers,hooks,ticks=1,{},{},{},{},{}
    for _,a in ipairs({player,buddy}) do
        a.Transform={GetWorldPosition=function() return a.x,0,a.z end}
        function a:GetDistanceSqToInst(other) return (self.x-other.x)^2+(self.z-other.z)^2 end
        function a:HasTag() return false end
    end
    buddy.prefab="wilson";buddy.sg={}
    buddy.components={inventory={FindItems=function() return {} end},health={IsDead=function() return false end,currenthealth=150,maxhealth=150},
        locomotor={Clear=function() end,Stop=function() end,GoToPoint=function() end}}
    function buddy:ClearBufferedAction() end
    local compliant=0
    local function encode(value) packets[#packets+1]=value;return tostring(#packets) end
    local codec={encode=encode,decode=function(value) return packets[tonumber(value)] end,
        encode_compliant=function(value) compliant=compliant+1;return encode(value) end}
    local last_http
    local G={ThePlayer=player,GetTime=function() return now end,walltime=function() return now end,pcall=pcall,
        net_string=function()
            local value="";return {value=function() return value end,set=function(_,v) value=v end}
        end,
        ChatHistory={SendCommandResponse=function() end},Vector3=function(x,y,z) return {x=x,y=y,z=z} end,
        TheWorld={ismastersim=true,state={cycles=0,phase="day",time=.1},DoPeriodicTask=function(_,_,fn) ticks[#ticks+1]=fn end,
            Map={IsPassableAtPoint=function() return true end,IsAboveGroundAtPoint=function() return true end}},
        Ents={},TheSim={FindEntities=function() return {player,buddy} end}}
    G.SendModRPCToServer=function(_,encoded) requests.request(player,encoded) end
    G.SendModRPCToClient=function(_,userid,encoded) assert(userid=="A");client_handlers.reply(encoded) end
    G.TheSim.QueryServer=function(_,url,callback,method,encoded)
        assert(url=="http://127.0.0.1:8766/v1/exchange" and method=="POST")
        last_http=codec.decode(encoded)
        callback(encode({session_id=last_http.session_id,sequence=last_http.sequence,feedback={}}),true,200)
    end
    local init
    local env=setmetatable({GLOBAL=G,print=function() end,
        require=function(name)
            if name=="json" then return codec end
            if name=="jev_companion_config" then return {enabled=true,url="http://127.0.0.1:8766/v1/exchange",token=string.rep("t",32)} end
            return require(name)
        end,
        GetModRPC=function(ns,name) return ns..":"..name end,GetClientModRPC=function(ns,name) return ns..":"..name end,
        AddModRPCHandler=function(_,name,fn) requests[name]=fn end,
        AddClientModRPCHandler=function(_,name,fn) client_handlers[name]=fn end,
        AddClassPostConstruct=function(name,fn) hooks[name]=fn end,AddSimPostInit=function(fn) init=fn end},{__index=_G})
    env.AddPrefabPostInit=function(name,fn) assert(name=="wilson");fn(buddy) end
    local chunk
    if setfenv then chunk=assert(loadfile("mods/jev_ai_companion/modmain.lua"));setfenv(chunk,env)
    else chunk=assert(loadfile("mods/jev_ai_companion/modmain.lua","t",env)) end
    chunk();init()
    local text,forwarded="/buddy bind",0
    local screen={chat_edit={GetString=function() return text end},Run=function() forwarded=forwarded+1 end}
    hooks["screens/chatinputscreen"](screen);screen:Run()
    assert(buddy._jev_companion_owner=="A" and forwarded==0)
    text="/buddy do I'm collecting 草";screen:Run()
    now=now+.1;for _,fn in ipairs(ticks) do fn() end
    assert(compliant==1 and last_http.state.control.instruction=="I'm collecting 草")
    assert(last_http.state.team and last_http.state.members.buddy1.ready and last_http.state.members.buddy1.companion.buddy.guid==2)
    text="你好";screen:Run();assert(forwarded==1)
end)
local function summon_world()
    local w=world();w.buddy=nil;w.spawns=0
    w.env.find_buddy=function() return nil end
    w.env.spawn_buddy=function()
        w.spawns=w.spawns+1;w.buddy=actor(100+w.spawns);return w.buddy
    end
    return w
end

test("summon creates its own actor and RPC retries or repeat commands never duplicate it",function()
    local w=summon_world();local p=w:request("summon")
    assert(p.status=="completed" and p.reason=="summoned" and w.spawns==1)
    assert(w.buddy._jev_companion_owner=="A" and w.buddy._jev_companion_created_for=="A")
    w.server:request(w.player,w.last)
    assert(w:request("summon").reason=="bound" and w.spawns==1)
    assert(w.server.owners.A.mode=="follow")
end)

test("bind is the same summon flow including creation, retrieval and death guards",function()
    local w=summon_world();assert(w:request("bind").reason=="summoned")
    local original=w.buddy;assert(w.spawns==1)
    assert(w:request("summon").reason=="bound" and w:request("bind").reason=="bound" and w.spawns==1)
    w:request("release");assert(w:request("bind").reason=="bound" and w.buddy==original)
    original.dead=true;assert(w:request("bind").reason=="existing_companion_dead")
    w:request("release");original.dead=false;original.x=80
    assert(w:request("bind").reason=="bound" and w.spawns==1 and original.x==80)
end)

test("summon reuses a nearby free Wilson without spawning",function()
    local w=world();w.env.spawn_buddy=function() error("unnecessary spawn") end
    assert(w:request("summon").reason=="bound" and w.buddy._jev_companion_owner=="A")
end)

test("released and disconnected owners retrieve their same reserved companion",function()
    local w=summon_world();w:request("summon");local original=w.buddy
    w:request("release");assert(w:request("summon").reason=="bound")
    w.player.valid=false;w:step();assert(not w.server.owners.A)
    w.player=actor(3,"A")
    assert(w:request("summon").reason=="bound" and w.buddy==original and w.spawns==1)
    local other=actor(4,"B")
    assert(w:request("summon",nil,{id="other:1",seq=1},other).reason=="summoned")
    assert(w.server.owners.B.buddy~=original and w.server.owners.A.buddy==original and w.spawns==2)
end)

test("summon rejects dead companions and retrieves distant ones without replacement",function()
    local w=summon_world();w:request("summon");w.buddy.dead=true
    assert(w:request("summon").reason=="existing_companion_dead")
    w:request("release");assert(w:request("summon").reason=="existing_companion_dead")
    w.buddy.dead=false;w.buddy.x=80
    assert(w:request("summon").reason=="bound" and w.spawns==1 and w.buddy.x==80)
end)

test("spawn failures leave the owner unbound and allow a later safe attempt",function()
    local w=summon_world();local spawn=w.env.spawn_buddy
    w.env.spawn_buddy=function() return nil,"no_safe_spawn_land" end
    assert(w:request("summon").reason=="no_safe_spawn_land" and not w.server.owners.A.buddy)
    w.env.spawn_buddy=spawn;assert(w:request("summon").reason=="summoned")
end)

test("client summon accepts the new actor token and synchronizes its snapshot",function()
    local w=client_world();w.client:command("summon")
    assert(w.wire[1].op=="summon")
    w:receive(1,"completed",1,0,{reason="summoned"})
    assert(w.client.token=="bound" and w.client:ready() and not w.client.control.enabled)
    w.client:command("do 收集草");assert(w.wire[2].op=="intent")
end)

test("client bind alias sends the single public summon operation",function()
    local w=client_world();w.client:command("bind")
    assert(w.wire[1].op=="summon")
    w:receive(1,"completed",1,0,{reason="summoned"})
    assert(w.client.token=="bound" and w.client:ready())
end)

test("global companion marker targets only its owner and reuses the tracked actor",function()
    local owner,buddy=actor(1,"A"),actor(2);local markers={}
    local G={SpawnPrefab=function(prefab)
        assert(prefab=="globalmapiconnamed")
        local icon=actor(100+#markers);markers[#markers+1]=icon
        icon.components={}
        function icon:AddComponent(name)
            assert(name=="maprevealer")
            self.components.maprevealer={SetPrivateOwner=function(revealer,player) revealer.owner=player end,
                GetPrivateOwner=function(revealer) return revealer.owner end}
        end
        function icon:AddTag(tag) self.tag=tag end
        function icon:Remove() self.valid=false end
        function icon:TrackEntity(target,restriction,texture) self.target,self.restriction,self.texture=target,restriction,texture end
        icon.MiniMapEntity={SetPriority=function(_,priority) icon.priority=priority end}
        icon.Network={SetClassifiedTarget=function(_,player) icon.viewer=player end}
        icon._target_displayname={set=function(_,text) icon.text=text end}
        return icon
    end}
    local adapter=require("jev_companion_world").new(G,function() end,C.validate_text)
    assert(adapter.map_bind(owner,buddy));local first=markers[1]
    assert(first.target==buddy and first.viewer==owner and first.restriction=="player_A" and first.texture=="wilson.png")
    assert(first.components.maprevealer:GetPrivateOwner()==owner)
    assert(first.components.maprevealer.revealperiod==1)
    assert(first.text:find("伙伴",1,true) and first.tag=="globalmapicon_player")
    buddy.x=500 -- Tracking follows this same server actor, not an initial coordinate snapshot.
    assert(adapter.map_bind(owner,buddy) and #markers==1 and first.target.x==500)
    local replacement=actor(3,"A")
    assert(adapter.map_bind(replacement,buddy) and not first.valid and #markers==2 and markers[2].viewer==replacement)
    assert(markers[2].components.maprevealer:GetPrivateOwner()==replacement)
    adapter.unmap(buddy);assert(not markers[2].valid and not buddy._jev_companion_mapicon)
end)

test("marker lifetime follows binding, not client heartbeat or local visibility",function()
    local w=world();local marker
    w.env.map_bind=function(player,buddy) marker={owner=player,buddy=buddy};return true end
    w.env.unmap=function() marker=nil end
    w:request("bind");assert(marker.owner==w.player)
    w:step(5);assert(marker and marker.buddy==w.buddy) -- A stopped task still has a location.
    w:request("release");assert(not marker)
    w:request("summon");assert(marker)
    w.player.valid=false;w:step();assert(not marker)
end)

test("an unavailable map marker reports an explicit reason without breaking companion control",function()
    local owner,buddy=actor(1,"A"),actor(2)
    local adapter=require("jev_companion_world").new({SpawnPrefab=function() return nil end},function() end,C.validate_text)
    local ok,reason=adapter.map_bind(owner,buddy)
    assert(not ok and reason=="map_icon_unavailable" and not buddy._jev_companion_mapicon)
    local w=world();w.env.map_bind=adapter.map_bind
    local packet=w:request("bind")
    assert(packet.status=="completed" and w.server.owners.A.map_error=="map_icon_unavailable")
    assert(w:request("stay").status=="completed")
end)

test("world spawning requires safe land, avoids obstacles and rejects unusable prefabs",function()
    local p,b,obstacle=actor(1,"A"),actor(2),actor(3)
    for _,a in ipairs({p,b,obstacle}) do
        a.Transform={GetWorldPosition=function() return a.x,0,a.z end,
            SetPosition=function(_,x,y,z) a.x,a.z=x,z end}
        function a:HasTag(tag) return tag=="playerghost" and self.ghost or false end
    end
    p.components={health={IsDead=function() return false end}}
    b.sg={};b.components={health={},inventory={},locomotor={}}
    function b:Remove() self.valid=false end
    obstacle.x=2;function obstacle:GetPhysicsRadius() return 1 end
    local land,spawns,missing=true,0,false
    local G={GetTime=function() return 0 end,
        TheWorld={Map={IsPassableAtPoint=function() return land end,IsAboveGroundAtPoint=function() return land end}},
        TheSim={FindEntities=function() return {obstacle} end},
        SpawnPrefab=function(prefab) assert(prefab=="wilson");spawns=spawns+1;return not missing and b or nil end}
    local E=require("jev_companion_world").new(G,function() end,C.validate_text)
    land=false;local a,reason=E.spawn_buddy(p);assert(not a and reason=="no_safe_spawn_land" and spawns==0)
    land=true;a=E.spawn_buddy(p);assert(a==b and (b.x-2)^2+b.z^2>=1.6^2)
    assert(b.x^2+b.z^2<=16 and b.x^2+b.z^2>=4)
    missing=true;a,reason=E.spawn_buddy(p);assert(not a and reason=="spawn_failed")
    missing=false;b.sg=nil;a,reason=E.spawn_buddy(p)
    assert(not a and reason=="invalid_spawned_companion" and not b.valid)
    p.ghost=true;local before=spawns;a,reason=E.spawn_buddy(p)
    assert(not a and reason=="owner_not_alive" and spawns==before)
end)
print("1.."..passed)
