package.path="./mods/jev_ai_companion/scripts/?.lua;"..package.path
local Places=require("jev_companion_places")
local Server=require("jev_companion_server")
local passed=0
local function test(name,fn) local ok,err=pcall(fn);assert(ok,name..": "..tostring(err));passed=passed+1;print("ok "..passed.." - "..name) end
local function fixture()
    local w={time=0,ents={},follows=0,stops=0}
    function w:entity(id,prefab,x)
        local e={GUID=id,prefab=prefab,x=x or 0,z=0,valid=true,components={},tags={}}
        function e:IsValid() return self.valid end
        function e:HasTag(tag) return self.tags[tag] or false end
        self.ents[id]=e;return e
    end
    w.a=w:entity(1,"wilson");w.owner=w:entity(2,"wendy");w.owner.userid="A"
    w.lab=w:entity(123456,"researchlab",0)
    w.a.components.builder={EvaluateTechTrees=function() w.tech_refreshed=true end}
    w.env={now=function() return w.time end,position=function(e) return e.x,e.z end,
        distance=function(a,b) return math.sqrt((a.x-b.x)^2+(a.z-b.z)^2) end,
        valid_point=function(a,x,z,r) return not w.water and math.abs(a.x-x)<=(r or 20) end,
        follow=function(_,e,recover) w.target=e;w.recover=recover;w.follows=w.follows+1;return true end,
        stop=function() w.stops=w.stops+1 end,dead=function() return false end}
    w.native=Places.new({Ents=w.ents,TheSim={FindEntities=function() error("global scans are forbidden") end}},w.env)
    function w:snapshot(near)
        local rows=near and {{guid=self.lab.GUID,prefab=self.lab.prefab,x=self.lab.x,z=self.lab.z}} or {}
        return self.native.snapshot(self.a,rows)
    end
    function w:request(id)
        local q={op="visit_building",args={building_id=id or "building:123456"}}
        local ok,reason=self.native.start(self.a,q);return ok,reason,q
    end
    return w
end
test("seen buildings retain exact identity outside observation and never become missing just from absence",function()
    local w=fixture();local first=w:snapshot(true)
    assert(first.buildings[1].id=="building:123456" and first.buildings[1].status=="observed")
    w.a.x=200;w.time=5;local far=w:snapshot(false)
    assert(far.buildings[1].status=="unconfirmed" and far.buildings[1].x==0 and far.buildings[1].last_seen_at==0)
    assert(not far.buildings[1].arrived and far.buildings[1].distance==200)
end)
test("native travel accepts a remote previously observed building but never an unseen GUID",function()
    local w=fixture();w:snapshot(true);w.a.x=200
    assert(w:request())
    w:entity(234567,"researchlab",100);assert(not w:request("building:234567"))
    local other=w:entity(4,"wilson",0)
    assert(not w.native.start(other,{args={building_id="building:123456"}}))
end)
test("travel targets the building entity and arrival refreshes actual technology within three units",function()
    local w=fixture();w:snapshot(true);w.a.x=200
    local ok,_,q=w:request();assert(ok)
    assert(not w.native.progress(w.a,q) and w.target==w.lab and not w.tech_refreshed)
    w.time=40;w.a.x=100;assert(not w.native.progress(w.a,q))
    w.time=70;w.a.x=2.9
    local status,reason,data=w.native.progress(w.a,q);assert(status=="completed" and reason=="building_arrived" and w.tech_refreshed)
    assert(data.building_id=="building:123456" and data.arrived and data.distance<=3)
    assert(w:snapshot(true).buildings[1].arrived)
end)
test("absence is uncertain but checking a removed known entity proves missing",function()
    local w=fixture();w:snapshot(true);w.lab.valid=false;w.a.x=200
    assert(w:snapshot(false).buildings[1].status=="unconfirmed")
    local ok,reason=w:request();assert(not ok and reason=="building_missing")
    assert(w:snapshot(false).buildings[1].status=="missing")
end)
test("blocked travel repaths and stops after twenty seconds without progress",function()
    local w=fixture();w:snapshot(true);w.a.x=100
    local _,_,q=w:request();w.native.progress(w.a,q)
    w.time=6;assert(not w.native.progress(w.a,q) and w.recover)
    w.time=20;local status,reason=w.native.progress(w.a,q)
    assert(status=="failed" and reason=="building_travel_stalled")
end)
test("motion does not permit indefinite travel and water destinations are rejected",function()
    local w=fixture();w:snapshot(true);w.a.x=100
    local _,_,q=w:request()
    w.time=301;w.a.x=90;local status,reason=w.native.progress(w.a,q)
    assert(status=="failed" and reason=="building_travel_timeout")
    w.water=true;assert(not w:request())
end)
test("building memory is bounded without revealing unrelated world entities",function()
    local w=fixture()
    for i=1,80 do w.native.snapshot(w.a,{{guid=1000+i,prefab="researchlab",x=i,z=0}}) end
    local memory=w.native.snapshot(w.a,{})
    assert(#memory.buildings==64 and memory.buildings[1].id=="building:1017")
end)
test("server travel stays active beyond eight seconds and newer intents interrupt it",function()
    local w=fixture();w:snapshot(true);w.a.x=200;local replies={};local env=w.env
    env.find_buddy=function() return w.a end
    env.snapshot=function() return {places=w:snapshot(math.abs(w.a.x-w.lab.x)<=20)} end
    env.reply=function(_,p) replies[#replies+1]=p end
    env.visit_start=w.native.start;env.visit_progress=w.native.progress
    local server=Server.new(env)
    server:request(w.owner,{id="c:1",seq=1,op="summon"});local token=replies[#replies].token
    local q={id="c:2",seq=2,op="visit_building",token=token,revision=0,args={building_id="building:123456"}}
    server:request(w.owner,q);server:tick();assert(q.last.status=="running")
    for i=1,12 do
        w.time=i;w.a.x=200-i*3
        server:request(w.owner,{id="c:"..(i+2),seq=i+2,op="state",token=token,revision=0});server:tick()
    end
    assert(server.owners.A.active==q and q.last.status=="running")
    server:request(w.owner,{id="c:15",seq=15,op="intent",token=token,revision=1})
    local follows=w.follows;w.time=13;server:tick()
    assert(q.last.status=="interrupted" and not server.owners.A.active and w.follows==follows)
end)
test("server reports the real missing-building reason instead of a generic destination failure",function()
    local w=fixture();local replies={};w.env.find_buddy=function() return w.a end
    w.env.snapshot=function() return {} end;w.env.reply=function(_,p) replies[#replies+1]=p end
    w.env.visit_start=w.native.start;w.env.visit_progress=w.native.progress
    local server=Server.new(w.env);server:request(w.owner,{id="c:1",seq=1,op="summon"})
    server:request(w.owner,{id="c:2",seq=2,op="visit_building",token=replies[#replies].token,revision=0,args={building_id="building:123456"}})
    assert(replies[#replies].reason=="building_not_remembered")
end)
print("1.."..passed)
