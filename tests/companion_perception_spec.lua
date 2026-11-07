package.path="./mods/jev_ai_companion/scripts/?.lua;"..package.path
local P=require("jev_companion_perception")
local S=require("jev_companion_server")
local passed=0
local function test(name,fn) local ok,err=pcall(fn);assert(ok,name..": "..tostring(err));passed=passed+1;print("ok "..passed.." - "..name) end
local function actor(id,x,z)
    local a={GUID=id,x=x or 0,z=z or 0,valid=true,components={},tags={}}
    function a:IsValid() return self.valid end
    function a:HasTag(tag) return self.tags[tag] or false end
    function a:GetDistanceSqToInst(other) return (self.x-other.x)^2+(self.z-other.z)^2 end
    a.Transform={GetWorldPosition=function() return a.x,0,a.z end}
    return a
end
local function fixture()
    local w={now=0,calls=0,entities={},actor=actor(1),land=function() return true end,clear=true}
    w.actor.components.locomotor={pathcaps={player=true}}
    w.G={TheWorld={state={},Map={IsPassableAtPoint=function(_,x,_,z) w.calls=w.calls+1;return w.land(x,z) end,
        IsAboveGroundAtPoint=function() return true end,GetTileAtPoint=function(_,x) return x<0 and 1 or 2 end},
        Pathfinder={IsClear=function(_,ax,ay,az,x,y,z,caps) assert(caps==w.actor.components.locomotor.pathcaps);return w.clear end}},
        TheSim={FindEntities=function() return w.entities end}}
    w.env={now=function() return w.now end,position=function(a) return a.x,a.z end}
    w.sensor=P.new(w.G,w.env)
    return w
end
test("perception exposes bounded local terrain and only walkable offered legs",function()
    local w=fixture();w.land=function(x) return x<=8 end
    local data=w.sensor.observe(w.actor,{})
    assert(data.cell_size==4 and data.radius==20 and #data.cells<=81 and #data.points<=16)
    local water=false
    for _,cell in ipairs(data.cells) do if not cell.land then water=true end end
    assert(water and #data.points>0)
    for _,p in ipairs(data.points) do assert(p.x<=8 and p.x^2+p.z^2<=400) end
    w.clear=false;w.now=3;assert(#w.sensor.observe(w.actor,{}).points==0)
end)
test("terrain sampling is cached but hazards are freshly checked",function()
    local w=fixture();local first=w.sensor.observe(w.actor,{});local calls=w.calls
    w.now=.5;w.sensor.observe(w.actor,{});assert(w.calls==calls)
    local fire=actor(2,8,0);fire.tags.fire=true;w.entities={fire}
    assert(not w.sensor.point(w.actor,{point_id="4:0",x=16,z=0}))
    local north=w.sensor.point(w.actor,{point_id="0:4",x=0,z=16});assert(north)
    w.now=3;w.sensor.observe(w.actor,{});assert(w.calls>calls)
end)
test("search point credentials cannot invent a landing point or cross a new obstacle",function()
    local w=fixture();w.sensor.observe(w.actor,{})
    assert(not w.sensor.point(w.actor,{point_id="fake",x=16,z=0}))
    assert(not w.sensor.point(w.actor,{point_id="4:0",x=100,z=0}))
    w.land=function(x) return x<6 or x>10 end
    assert(not w.sensor.point(w.actor,{point_id="4:0",x=16,z=0}))
end)
test("missing path telemetry does not advertise a fabricated safe search",function()
    local w=fixture();w.G.TheWorld.Pathfinder=nil
    assert(not w.sensor.observe(w.actor,{}))
end)
local function server_world()
    local w={time=0,seq=0,replies={},actor=actor(2),player=actor(1),found=false,moves=0}
    w.player.userid="A"
    w.env={now=function() return w.time end,position=function(a) return a.x,a.z end,
        find_buddy=function() return w.actor end,dead=function() return false end,
        stop=function() w.stopped=true end,distance=function(a,b) return math.sqrt(a:GetDistanceSqToInst(b)) end,
        snapshot=function() return {} end,reply=function(_,packet) w.replies[#w.replies+1]=packet end,
        search_point=function(_,args) return args.point_id=="4:0" and args.x==16 and args.z==0 and {x=16,z=0} or nil end,
        search_found=function(_,products) assert(products[1]=="cutgrass");return w.found end,
        move=function(_,x,z) w.moves=w.moves+1;w.destination={x=x,z=z};return true end,
        arrived=function(a,x,z) return a.x==x and a.z==z end}
    w.server=S.new(w.env)
    function w:request(op,args,revision)
        self.seq=self.seq+1
        local q={id="test:"..self.seq,seq=self.seq,op=op,args=args,token=self.token,revision=revision or 0}
        self.server:request(self.player,q);self.last=q
        local packet=self.replies[#self.replies]
        if op=="bind" then self.token=packet.token end
        return packet
    end
    w:request("bind")
    w.args={point_id="4:0",x=16,z=0,products={"cutgrass"},timeout_seconds=2}
    return w
end
test("search yields on resource discovery without claiming arrival or collected inventory",function()
    local w=server_world();assert(w:request("search",w.args).status=="running")
    local q=w.last;w.server:request(w.player,q);assert(w.moves==1)
    w.actor.x=5;w.found=true;w.time=.3;w.server:tick()
    assert(q.last.status=="completed" and q.last.reason=="resource_found" and w.stopped)
    assert(w.actor.x~=16)
end)
test("search timeout and a newer intent both stop the native leg",function()
    local w=server_world();w:request("search",w.args);w.time=2.1;w.server:tick()
    assert(w.last.last.reason=="search_leg_timeout" and w.stopped)
    w:request("search",w.args);local q=w.last
    w:request("intent",{},1)
    assert(q.last.status=="interrupted" and not w.server.owners.A.active)
end)
test("invalid or stale search points never start movement",function()
    local w=server_world();w.args.x=100
    assert(w:request("search",w.args).reason=="search_point_unavailable" and w.moves==0)
    w.args.timeout_seconds=50
    assert(w:request("search",w.args).reason=="invalid_search" and w.moves==0)
end)
test("resource perception retains rare products despite a crowded foreground",function()
    local w=fixture();local a=w.actor
    a.components.health={currenthealth=150,maxhealth=150,IsDead=function() return false end}
    a.components.inventory={FindItems=function() return {} end}
    w.G.GetTime=function() return w.now end;w.G.Ents={[1]=a};w.G.ACTIONS={}
    local entities={}
    for i=1,60 do
        local e=actor(100+i,1,0);e.prefab="flint"
        e.components.inventoryitem={canbepickedup=true,IsHeld=function() return false end}
        entities[#entities+1]=e;w.G.Ents[e.GUID]=e
    end
    local grass=actor(200,10,0);grass.prefab="grass"
    grass.components.pickable={product="cutgrass",caninteractwith=true,CanBePicked=function() return true end,IsStuck=function() return false end}
    entities[#entities+1]=grass;w.G.Ents[grass.GUID]=grass;w.entities=entities
    local adapter=require("jev_companion_world").new(w.G,function() end,function(v) return v end)
    local state=adapter.snapshot(a,a,{})
    assert(#state.nearby==48 and not state.resources_complete and state.perception)
    local found=false;for _,row in ipairs(state.nearby) do if row.product=="cutgrass" then found=true end end
    assert(found)
end)
print("1.."..passed)
