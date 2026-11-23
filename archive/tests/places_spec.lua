-- lua tests/places_spec.lua
package.path="./mods/dst_player_agent/scripts/?.lua;"..package.path
local P=require("jev_places")
local N=require("jev_native_executor")
local R=require("jev_residence")
local passed=0
local function test(name,fn)
    local ok,err=pcall(fn); assert(ok,name..": "..tostring(err))
    passed=passed+1; print("ok "..passed.." - "..name)
end
local function safe() return true end
local function region() return "A" end
local function building(id,x) return {guid=id,prefab="researchlab",x=x or 8,z=0} end
local function ready(target)
    return {status="ready",action={mode="place",node_id="A",x=4,z=0,arrival=0.4}}
end
local function state(places)
    return {world={phase="day"},player={guid=1,position={x=0,z=0},vitals={health=150,hunger=100,hunger_max=150},
        inventory={items={},equipped={}}},nearby={},places=places or {},navigation={graph={current_node="A"}}}
end
test("memory is scoped to world and owner but survives Python restarts",function()
    local world={}; local m=P.memory(world,1)
    P.observe(m,{x=0,z=0},{building(42)},region,1,20,true)
    assert(P.memory(world,1).buildings["building:42"])
    assert(not next(P.memory(world,2).buildings))
    assert(not next(P.memory({},1).buildings))
end)
test("out-of-view buildings are retained, complete local scans prove missing",function()
    local m=P.memory({},1)
    P.observe(m,{x=0,z=0},{building(42)},region,1,20,true)
    P.observe(m,{x=50,z=0},{},region,2,20,true)
    assert(m.buildings["building:42"].status=="unconfirmed")
    P.observe(m,{x=0,z=0},{},region,3,20,false)
    assert(m.buildings["building:42"].status=="unconfirmed")
    P.observe(m,{x=0,z=0},{},region,4,20,true)
    assert(m.buildings["building:42"].status=="unconfirmed","one absent scan cannot delete memory")
    P.observe(m,{x=0,z=0},{},region,4.5,20,true)
    P.observe(m,{x=0,z=0},{},region,5.1,20,true)
    assert(m.buildings["building:42"].status=="unconfirmed","returning clients need time to reload replicas")
    P.observe(m,{x=0,z=0},{},region,14.1,20,true)
    assert(m.buildings["building:42"].status=="missing")
    P.observe(m,{x=0,z=0},{building(42)},region,15,20,true)
    assert(m.buildings["building:42"].status=="observed" and m.buildings["building:42"].first_seen==1)
end)
test("building memory is bounded and ignores out-of-radius entities",function()
    local m=P.memory({},1); local rows={}
    for i=1,300 do rows[#rows+1]=building(i,8) end
    rows[#rows+1]=building(999,100)
    P.observe(m,{x=0,z=0},rows,region,1,20,true)
    local count=0; for _ in pairs(m.buildings) do count=count+1 end
    assert(count==256 and not m.buildings["building:999"])
end)
test("route proximity alone does not confirm a remembered building",function()
    local m=P.memory({},1)
    P.observe(m,{x=0,z=0},{building(42)},region,1,20,true)
    P.observe(m,{x=50,z=0},{},region,2,20,true)
    local s=P.snapshot(m,{x=50,z=0},"A",function() return {status="arrived"} end,safe,2)
    assert(not s.buildings[1].arrived)
end)

test("only building anchors are offered and identity survives leaving during planning",function()
    local m=P.memory({},1)
    local s=P.snapshot(m,{x=0,z=0},"A",ready,safe,1)
    assert(#s.anchors==0 and not next(P.actions(s)),"empty ground cannot be a home anchor")
    P.observe(m,{x=0,z=0},{building(42)},region,1,20,true)
    s=P.snapshot(m,{x=0,z=0},"A",ready,safe,1)
    assert(#s.anchors==1 and s.anchors[1].id=="building:42" and not s.anchors[1].ready)
    assert(P.actions(s)["visit_building:42"] and not P.actions(s)["set_home_building:42"])
    P.observe(m,{x=50,z=0},{},region,2,20,true)
    s=P.snapshot(m,{x=50,z=0},"B",ready,safe,2)
    assert(s.anchors[1].id=="building:42" and not s.anchors[1].ready)
    P.observe(m,{x=6,z=0},{building(42)},region,3,20,true)
    s=P.snapshot(m,{x=6,z=0},"A",function() return {status="arrived"} end,safe,3)
    assert(s.anchors[1].id=="building:42" and s.anchors[1].ready)
    assert(P.set_home(m,P.actions(s)["set_home_building:42"],{x=6,z=0},"A",safe,3))
    assert(m.home.building_id=="building:42" and m.home.x==6)
end)
test("setting home requires an offered nearby safe anchor and never accepts arbitrary coordinates",function()
    local m=P.memory({},1)
    P.observe(m,{x=0,z=0},{building(42)},region,1,20,true)
    local s=P.snapshot(m,{x=0,z=0},"A",function() return {status="arrived"} end,safe,1)
    local a=P.actions(s)["set_home_building:42"]
    assert(a and a.planner_only)
    assert(not P.set_home(m,{anchor_id=a.anchor_id,x=200,z=0},{x=0,z=0},"A",safe,1))
    assert(not P.set_home(m,a,{x=10,z=0},"A",safe,1))
    assert(not P.set_home(m,a,{x=0,z=0},"A",function() return false end,1))
    assert(P.set_home(m,a,{x=0,z=0},"A",safe,1))
    assert(m.home.id=="home:1")
end)
test("home survives source-building loss and receives a new ID when reset",function()
    local m=P.memory({},1)
    P.observe(m,{x=8,z=0},{building(42)},region,1,20,true)
    local s=P.snapshot(m,{x=8,z=0},"A",function() return {status="arrived"} end,safe,1)
    assert(P.set_home(m,P.actions(s)["set_home_building:42"],{x=8,z=0},"A",safe,1))
    P.observe(m,{x=8,z=0},{},region,2,20,true)
    P.observe(m,{x=8,z=0},{},region,2.5,20,true)
    P.observe(m,{x=8,z=0},{},region,3,20,true)
    assert(m.home and m.buildings["building:42"].status=="missing")
    P.observe(m,{x=20,z=0},{building(43,20)},region,3,20,true)
    s=P.snapshot(m,{x=20,z=0},"A",function() return {status="arrived"} end,safe,3)
    for id,a in pairs(P.actions(s)) do if a.kind=="set_home" then
        assert(P.set_home(m,a,{x=20,z=0},"A",safe,3)); break
    end end
    assert(m.home.id=="home:2" and m.home.x==20)
end)

test("building absence confirmation resets on reobservation or incomplete scans",function()
    local m=P.memory({},1)
    P.observe(m,{x=0,z=0},{building(42)},region,0,20,true)
    P.observe(m,{x=0,z=0},{},region,1,20,true)
    P.observe(m,{x=0,z=0},{},region,2,20,false)
    P.observe(m,{x=0,z=0},{},region,3,20,true)
    assert(m.buildings["building:42"].missing_scans==1)
    P.observe(m,{x=0,z=0},{building(42)},region,4,20,true)
    assert(not m.buildings["building:42"].missing_scans and m.buildings["building:42"].status=="observed")
end)

test("set-home accepts nearby drift through native dispatch and writes the latest safe point",function()
    for _,offset in ipairs({0.01,0.5,2}) do
        local m=P.memory({},1)
        P.observe(m,{x=0,z=0},{building(42)},region,1,20,true)
        local function arrived() return {status="arrived"} end
        local s=state(P.snapshot(m,{x=0,z=0},"A",arrived,safe,1))
        local requested=N.available(s)["set_home_building:42"]
        s.player.position.x=offset
        s.places=P.snapshot(m,s.player.position,"A",arrived,safe,2)
        local issued=0
        local x=N.new({issue=function(_,action)
            issued=issued+1
            assert(P.set_home(m,action,s.player.position,"A",safe,2))
        end,stop=function() end})
        assert(x:start({action_id="drift",choice="set_home_building:42",action=requested},s,2))
        assert(issued==1 and m.home.x==offset and m.home.building_id=="building:42")
        s.places=P.snapshot(m,s.player.position,"A",arrived,safe,2.2)
        x:tick(s,2.2)
        assert(x.report.status=="completed")
    end
end)

test("set-home tolerance also covers a refreshed candidate between dispatch and write",function()
    local m=P.memory({},1)
    P.observe(m,{x=0,z=0},{building(42)},region,1,20,true)
    local function arrived() return {status="arrived"} end
    local s=P.snapshot(m,{x=0,z=0},"A",arrived,safe,1)
    local requested=P.actions(s)["set_home_building:42"]
    P.snapshot(m,{x=0.2,z=0.1},"A",arrived,safe,2)
    assert(P.set_home(m,requested,{x=0.2,z=0.1},"A",safe,2))
    assert(m.home.x==0.2 and m.home.z==0.1)
end)

test("set-home tolerance rejects distant invalid changed-building and unsafe requests",function()
    local m=P.memory({},1)
    P.observe(m,{x=0,z=0},{building(42)},region,1,20,true)
    local function arrived() return {status="arrived"} end
    local s=state(P.snapshot(m,{x=0,z=0},"A",arrived,safe,1))
    local requested=N.available(s)["set_home_building:42"]
    s.player.position.x=2.01
    s.places=P.snapshot(m,s.player.position,"A",arrived,safe,2)
    local x=N.new({issue=function() error("out-of-range home issued") end,stop=function() end})
    assert(not x:start({action_id="far",choice="set_home_building:42",action=requested},s,2))
    assert(x.report.reason=="target_changed" and not m.home)
    assert(not P.set_home(m,requested,s.player.position,"A",safe,2))
    assert(not P.home_point_matches({x=0,z=0},{x=1.5,z=1.5}),"range is Euclidean, not a square")
    for _,point in ipairs({{}, {x=0/0,z=0}, {x=math.huge,z=0}, {x="0",z=0}}) do
        assert(not P.home_point_matches(point,{x=0,z=0}))
    end
    assert(not P.set_home(m,{anchor_id="building:99",x=2.01,z=0},s.player.position,"A",safe,2))
    assert(not P.set_home(m,P.actions(s.places)["set_home_building:42"],s.player.position,"A",function() return false end,2))
    m.buildings["building:42"].status="missing"
    assert(not P.set_home(m,P.actions(s.places)["set_home_building:42"],s.player.position,"A",safe,2))
    assert(not m.home)
end)
test("place travel is planner-only and unavailable in unlit darkness",function()
    local places={buildings={{id="building:42",prefab="researchlab",status="unconfirmed",route=ready()}},anchors={}}
    local s=state(places)
    assert(N.available(s)["visit_building:42"].planner_only)
    s.world.phase="night"
    assert(not N.available(s)["visit_building:42"])
end)
test("native set-home action waits for observed acknowledgement",function()
    local s=state({anchors={{id="building:42",building_id="building:42",ready=true,x=0,z=0}},buildings={}})
    local issued=0; local x=N.new({issue=function() issued=issued+1 end,stop=function() end})
    local a=N.available(s)["set_home_building:42"]
    assert(x:start({action_id="set",choice="set_home_building:42",action=a},s,0))
    x:tick(s,0.2); assert(x.active and issued==1)
    s.places.home={id="home:1",anchor_id="building:42"}
    x:tick(s,0.4); assert(x.report.status=="completed")
end)
test("travel does not complete at an intermediate waypoint",function()
    local target={id="building:42",prefab="researchlab",status="unconfirmed",route=ready()}
    local s=state({buildings={target},anchors={}})
    local issued=0; local x=N.new({issue=function() issued=issued+1 end,stop=function() end})
    assert(x:start({action_id="visit",choice="visit_building:42",action=N.available(s)["visit_building:42"]},s,0))
    s.player.position.x=4; target.route.action.x=8
    x:tick(s,0.2); assert(x.active and issued==2)
    target.status="observed"; target.arrived=true
    x:tick(s,0.4); assert(x.report.status=="completed" and x.report.reason=="destination_observed")
end)
test("old return-home commands cannot switch destination when home is reset",function()
    local home={id="home:1",route=ready()}
    local s=state({home=home,anchors={},buildings={}})
    local x=N.new({issue=function() end,stop=function() end})
    local a=N.available(s).return_home
    assert(x:start({action_id="home",choice="return_home",action=a},s,0))
    s.places.home={id="home:2",route=ready()}
    x:tick(s,0.2)
    assert(x.report.status=="interrupted" and x.report.reason=="home_changed")
    assert(not x:start({action_id="stale",choice="return_home",action=a},s,0.4))
    assert(x.report.reason=="target_changed")
end)
test("lost approach routes fail without claiming arrival or deleting memory",function()
    local target={id="building:42",prefab="researchlab",status="unconfirmed",route=ready()}
    local s=state({buildings={target},anchors={}})
    local x=N.new({issue=function() end,stop=function() end})
    assert(x:start({action_id="visit",choice="visit_building:42",action=N.available(s)["visit_building:42"]},s,0))
    target.route={status="blocked"}; s.player.position.x=4
    x:tick(s,0.2)
    assert(x.report.status=="interrupted" and x.report.reason=="destination_route_unavailable")
    assert(target.status=="unconfirmed")
end)
test("entering a destination region is not confirmation of arrival",function()
    local target={id="building:42",prefab="researchlab",status="unconfirmed",route={status="ready",
        action={mode="graph",node_id="B",x=4,z=0,arrival=0.2}}}
    local s=state({buildings={target},anchors={}})
    local x=N.new({issue=function() end,stop=function() end})
    assert(x:start({action_id="region",choice="visit_building:42",action=N.available(s)["visit_building:42"]},s,0))
    s.navigation.graph.current_node="B"
    target.route={status="ready",action={mode="place",node_id="B",x=8,z=0,arrival=0.4}}
    x:tick(s,0.2)
    assert(x.report.status=="interrupted" and x.report.reason=="destination_region_entered")
    assert(x:start({action_id="approach",choice="visit_building:42",action=N.available(s)["visit_building:42"]},s,0.4))
    target.status="observed"; target.arrived=true
    x:tick(s,0.6)
    assert(x.report.status=="completed" and x.report.reason=="destination_observed")
end)
test("residence rejects long detours and outside targets before issuing",function()
    local s=state({home={id="home:1",x=0,z=0}})
    local area={home_id="home:1",x=0,z=0,radius=12}
    s.nearby={{guid=2,prefab="grass",dx=2,dz=0,distance=2,pickable=true,
        collect_path={status="ready",distance=20,waypoint={x=2,z=0}}}}
    local action=assert(N.available(s).collect_grass)
    assert(not R.allowed(action,s,area),"a centre position cannot make an overlong path safe")
    local x=N.new({issue=function() error("outside action issued") end,stop=function() end})
    action.residence=area
    assert(not x:start({action_id="far",choice="collect_grass",action=action},s,0))
    assert(x.report.reason=="residence_boundary")
end)

test("residence monitors movement and checks every internal approach step",function()
    for _,mode in ipairs({"position","walk","home"}) do
        local s=state({home={id="home:1",x=0,z=0}});s.crafting={axe=true}
        local action=assert(N.available(s).craft_axe)
        action.residence={home_id="home:1",x=0.00000001,z=0,radius=12}
        local issued,stops=0,0
        local x=N.new({issue=function() issued=issued+1 end,stop=function() stops=stops+1 end})
        assert(x:start({action_id="bound",choice="craft_axe",action=action},s,0))
        assert(issued==1)
        if mode=="position" then s.player.position.x=13;x:tick(s,.2)
        elseif mode=="home" then s.places.home.id="home:2";x:tick(s,.2)
        else x:issue("walk",{kind="explore",x=13,z=0}) end
        assert(x.report.status=="interrupted" and x.report.reason=="residence_boundary")
        assert(issued==1 and stops>0)
        x:issue("walk",{kind="explore",x=0,z=0})
        assert(issued==1,"a rejected step cannot continue issuing in the same call chain")
    end
end)

test("residence permits useful local work and excludes arbitrary or changed homes",function()
    local s=state({home={id="home:1",x=0,z=0}})
    local area={home_id="home:1",x=0,z=0,radius=12}
    assert(R.valid(area,s) and R.allowed({kind="eat_safe_food"},s,area))
    assert(R.allowed({kind="place",x=3,z=0},s,area))
    assert(not R.allowed({kind="place",x=20,z=0},s,area))
    assert(not R.valid({home_id="home:1",x=10,z=0,radius=12},s))
    assert(not R.valid({home_id="home:2",x=0,z=0,radius=12},s))
end)
test("streamed replicas retain logical building and home identity",function()
 local w={};local m=P.memory(w,1)
 P.observe(m,{x=8,z=0},{building(42)},region,1,20,true)
 local snap=P.snapshot(m,{x=8,z=0},"A",function() return {status="arrived"} end,safe,1)
 assert(P.set_home(m,P.actions(snap)["set_home_building:42"],{x=8,z=0},"A",safe,1))
 P.observe(m,{x=50,z=0},{},region,2,20,true)
 P.observe(m,{x=8,z=0},{building(88,8.01)},region,3,20,true)
 assert(m.buildings["building:42"].guid==88 and not m.buildings["building:88"])
 assert(m.buildings["building:42"].binding_revision==1 and m.buildings["building:42"].first_seen==1)
 assert(P.id_for_guid(m,88)=="building:42" and not P.id_for_guid(m,42))
 assert(m.home.id=="home:1" and m.home.anchor_id=="building:42")
 P.observe(m,{x=50,z=0},{},region,4,20,true)
 P.observe(m,{x=8,z=0},{building(99,8)},region,5,20,true)
 assert(m.buildings["building:42"].guid==99 and m.buildings["building:42"].binding_revision==2)
end)
test("neighbouring or different buildings cannot steal a remembered identity",function()
 local m=P.memory({},1);P.observe(m,{x=0,z=0},{building(42)},region,1,20,true)
 P.observe(m,{x=50,z=0},{},region,2,20,true)
 local different={guid=90,prefab="firepit",x=8,z=0}
 P.observe(m,{x=0,z=0},{building(88,8.5),different},region,3,20,true)
 assert(m.buildings["building:42"].guid==42 and m.buildings["building:88"] and m.buildings["building:90"])
end)
test("ambiguous replicas and coexisting original entities never merge",function()
 local m=P.memory({},1);P.observe(m,{x=0,z=0},{building(42)},region,1,20,true)
 P.observe(m,{x=50,z=0},{},region,2,20,true)
 P.observe(m,{x=0,z=0},{building(88,8),building(89,8.1)},region,3,20,true)
 assert(m.buildings["building:42"].guid==42 and m.buildings["building:88"] and m.buildings["building:89"])
 local m=P.memory({},1);P.observe(m,{x=0,z=0},{building(42)},region,1,20,true)
 P.observe(m,{x=50,z=0},{},region,2,20,true)
 P.observe(m,{x=0,z=0},{building(42),building(88)},region,3,20,true)
 assert(m.buildings["building:42"].guid==42 and m.buildings["building:88"])
end)
test("confirmed local destruction is not reinterpreted as a streamed replica",function()
 local m=P.memory({},1);P.observe(m,{x=0,z=0},{building(42)},region,0,20,true)
 for _,time in ipairs({1,1.5,2.1}) do P.observe(m,{x=0,z=0},{},region,time,20,true) end
 assert(m.buildings["building:42"].status=="missing")
 P.observe(m,{x=0,z=0},{building(88)},region,3,20,true)
 assert(m.buildings["building:42"].status=="missing" and m.buildings["building:88"])
end)

print("Passed "..passed.." place tests")
