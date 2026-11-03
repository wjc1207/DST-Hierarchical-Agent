package.path="./scripts/?.lua;"..package.path
local R=require("jev_refueling")
local N=require("jev_native_executor")
local residence=require("jev_residence")
local passed=0
local function test(name,fn)
    local ok,err=pcall(fn);assert(ok,name..": "..tostring(err))
    passed=passed+1;print("ok "..passed.." - "..name)
end
local function fixture()
    local item={GUID=20,prefab="log",IsValid=function() return true end}
    local target={GUID=40,prefab="firepit",IsValid=function() return true end}
    local G={Ents={[40]=target},GetTime=function() return 0 end,
        ACTIONS={ADDFUEL={id="ADDFUEL"},ADDWETFUEL={id="ADDWETFUEL"}}}
    local pc={IsBusy=function() return false end}
    pc.GetItemUseAction=function(_,fuel,fire) return {action=G.ACTIONS.ADDFUEL,invobject=fuel,target=fire} end
    pc.RemoteControllerUseItemOnSceneFromInvTile=function(self,action,fuel) self.sent={action=action,fuel=fuel} end
    pc.DoAction=function(self,action) self.sent={action=action} end
    local inventory={GetItems=function() return {item} end,GetOverflowContainer=function() end}
    local player={replica={inventory=inventory},components={playercontroller=pc}}
    local nearby={{guid=40,prefab="firepit",distance=2,dx=2,dz=0,tags={"fire"}}}
    return G,player,item,target,nearby,pc
end
local function state()
    local G,p,_,_,nearby=fixture()
    return {world={phase="day"},player={guid=1,position={x=0,z=0},vitals={health=150,hunger=100,hunger_max=150},
        inventory={items={{prefab="log",count=3}},equipped={}}},recipe_inventory={log=3},nearby=nearby,
        refueling=R.scan(G,p,nearby,function() return true end)}
end
local function copy(t)
    if type(t)~="table" then return t end
    local r={};for k,v in pairs(t) do r[k]=copy(v) end;return r
end
local function action(s) return N.available(s)["add_fuel:40:log"] end
test("native compatibility provides explicit one-item planner-only action",function()
    local s=state();local a=action(s)
    assert(a and a.kind=="add_fuel" and a.fuel_guid==20 and a.building_id=="building:40" and a.planner_only)
end)
test("extinguished firepit and wet fuel use actual native action",function()
    local G,p,item,target,nearby,pc=fixture();nearby[1].tags={}
    pc.GetItemUseAction=function(_,fuel,fire) return {action=G.ACTIONS.ADDWETFUEL,invobject=fuel,target=fire} end
    local a=R.scan(G,p,nearby,function() return true end).options[1]
    assert(a.native_action=="ADDWETFUEL")
    assert(R.issue(G,p,a,function() return true end))
    assert(pc.sent.action.target==target and pc.sent.fuel==item)
end)
test("valuable fuel, distant or unsafe fires and incompatible actions are excluded",function()
    local G,p,item,_,nearby,pc=fixture()
    item.prefab="livinglog";assert(#R.scan(G,p,nearby,function() return true end).options==0)
    item.prefab="log";nearby[1].distance=4
    assert(#R.scan(G,p,nearby,function() return true end).options==0)
    nearby[1].distance=2;assert(#R.scan(G,p,nearby,function() return false end).options==0)
    pc.GetItemUseAction=function() return {action={id="LIGHT"}} end
    assert(#R.scan(G,p,nearby,function() return true end).options==0)
end)
test("overflow inventory is supported and exact item replacement is rejected",function()
    local G,p,item,_,nearby=fixture()
    p.replica.inventory.GetItems=function() return {} end
    p.replica.inventory.GetOverflowContainer=function() return {GetItems=function() return {item} end} end
    local a=R.scan(G,p,nearby,function() return true end).options[1]
    item.GUID=21
    local ok,reason=R.issue(G,p,a,function() return true end)
    assert(not ok and reason=="refuel_item_changed")
end)
test("target and fuel identity cannot silently change between offer and start",function()
    local s=state();local a=copy(action(s));a.fuel_guid=21
    local x=N.new({stop=function() end,issue=function() error("must not issue") end})
    assert(not x:start({action_id="stale",choice="add_fuel:40:log",action=a},s,0))
    assert(x.report.reason=="target_changed")
end)
test("consumption without confirmed native execution is not completion",function()
    local s=state();local x=N.new({stop=function() end,issue=function() end})
    assert(x:start({action_id="fuel",choice="add_fuel:40:log",action=action(s)},s,0))
    local after=copy(s);after.recipe_inventory.log=2
    x:tick(after,.2);assert(x.active)
    after.execution={action="ADDFUEL",target_guid=41,confirmed_start=true,issued_at=0,started_at=.1}
    x:tick(after,.4);assert(x.active)
    after.execution.target_guid=40
    x:tick(after,.6);assert(not x.active and x.report.status=="completed")
end)
test("extinguished target must be burning and exact fuel consumed",function()
    local s=state();s.nearby[1].tags={}
    local x=N.new({stop=function() end,issue=function() end})
    assert(x:start({action_id="relight",choice="add_fuel:40:log",action=action(s)},s,0))
    local after=copy(s);after.execution={action="ADDFUEL",target_guid=40,confirmed_start=true,issued_at=0,started_at=.1}
    x:tick(after,.2);assert(x.active)
    after.recipe_inventory.log=2;x:tick(after,.4);assert(x.active)
    after.nearby[1].tags={"fire"};x:tick(after,.6)
    assert(x.report.status=="completed")
end)
test("missing target fails and cancellation never consumes a second item",function()
    local s=state();local issued=0
    local x=N.new({stop=function() end,issue=function() issued=issued+1 end})
    assert(x:start({action_id="missing",choice="add_fuel:40:log",action=action(s)},s,0))
    local after=copy(s);after.nearby={};x:tick(after,.2)
    assert(x.report.status=="failed" and x.report.reason=="refuel_target_missing")
    x=N.new({stop=function() end,issue=function() issued=issued+1 end})
    x:start({action_id="cancel",choice="add_fuel:40:log",action=action(s)},s,1)
    x:cancel("cancel");x:tick(s,2)
    assert(x.report.status=="cancelled" and issued==2)
end)
test("lit-fire night refueling is legal without broadening night exploration",function()
    local s=state();s.world.phase="night"
    assert(action(s))
    s.nearby[1].tags={};assert(not action(s))
end)
test("native residence rejects outside target and allows bounded nearby refueling",function()
    local s=state();local a=action(s);local area={x=0,z=0,radius=12}
    assert(residence.allowed(a,s,area))
    s.nearby[1].dx=12;assert(not residence.allowed(a,s,area))
end)
test("native dispatch uses server controller locally and rechecks changed native action",function()
    local G,p,_,target,nearby,pc=fixture();pc.ismastersim=true
    local a=R.scan(G,p,nearby,function() return true end).options[1]
    assert(R.issue(G,p,a,function() return true end) and pc.sent.action.target==target)
    G.GetTime=function() return 3 end -- replication settling has elapsed
    pc.GetItemUseAction=function() return nil end
    local ok,reason=R.issue(G,p,a,function() return true end)
    assert(not ok and reason=="refuel_native_action_unavailable")
end)
test("fire approach remains planner-only and needs equipped night light",function()
    local s=state();s.places={buildings={{id="building:40",prefab="firepit",status="observed",x=6,z=0,
        route={status="ready",distance=3.5,action={mode="place",x=3.5,z=0}}}}}
    local a=N.available(s)["visit_building:40"]
    assert(a and a.refuel_approach and a.planner_only)
    s.world.phase="night"
    assert(not N.available(s)["visit_building:40"])
    s.player.inventory.equipped={{prefab="torch",count=1}}
    assert(N.available(s)["visit_building:40"])
end)

test("native residence validates entire fire approach and preserves ordinary navigation ban",function()
    local s=state();s.places={buildings={{id="building:40",prefab="firepit",status="observed",x=6,z=0,
        route={status="ready",distance=3.5,action={mode="place",x=3.5,z=0}}}}}
    local a=N.available(s)["visit_building:40"];local area={x=0,z=0,radius=12}
    assert(residence.allowed(a,s,area))
    a.mode="graph";assert(not residence.allowed(a,s,area));a.mode="place"
    a.refuel_approach=nil;assert(not residence.allowed(a,s,area));a.refuel_approach=true
    s.places.buildings[1].route.distance=15;assert(not residence.allowed(a,s,area))
    s.places.buildings[1].route.distance=3.5;s.places.buildings[1].x=11
    assert(not residence.allowed(a,s,area))
end)
test("ratio offers are separate planner-only actions with fresh stage rechecks",function()
 local G,p,_,target,nearby=fixture();local stage=2
 target.HasTag=function() return true end
 target.components={fueled={GetCurrentSection=function() return stage end}}
 local scan=R.scan(G,p,nearby,function() return true end)
 assert(scan.ratio_available and scan.targets[1].reason=="fire_already_sufficient" and scan.targets[1].ratio_reason=="")
 assert(#scan.options==1 and scan.options[1].ratio_refuel and scan.options[1].planner_only)
 local a=scan.options[1];assert(R.key(a)=="add_fuel_ratio:40:log")
 assert(R.issue(G,p,a,function() return true end))
 G.GetTime=function() return 3 end;stage=4
 local ok,reason=R.issue(G,p,a,function() return true end)
 assert(not ok and reason=="fuel_ratio_unverifiable")
end)
test("native ratio marker cannot be downgraded or forged on a legacy action",function()
 local s=state();local a=copy(action(s));a.ratio_refuel=true
 local x=N.new({stop=function() end,issue=function() error("must not issue") end})
 assert(not x:start({action_id="forged",choice="add_fuel:40:log",action=a},s,0))
 assert(x.report.reason=="target_changed")
end)
test("refueling keeps logical fire ID while execution binds only the fresh GUID",function()
 local P=require("jev_places");local G,p,_,target,nearby=fixture()
 G.TheWorld={};local m=P.memory(G.TheWorld,1)
 P.observe(m,{x=0,z=0},{{guid=40,prefab="firepit",x=2,z=0}},function() return "A" end,1,20,true)
 local old=R.scan(G,p,nearby,function() return true end).options[1]
 P.observe(m,{x=50,z=0},{},function() return "A" end,2,20,true)
 target.GUID=80;G.Ents={[80]=target};nearby[1].guid=80
 P.observe(m,{x=0,z=0},{{guid=80,prefab="firepit",x=2,z=0}},function() return "A" end,3,20,true)
 local scan=R.scan(G,p,nearby,function() return true end);local a=scan.options[1]
 assert(a.guid==80 and a.building_id=="building:40" and scan.targets[1].building_id=="building:40")
 local ok,reason=R.issue(G,p,old,function() return true end)
 assert(not ok and reason=="refuel_target_changed")
 assert(R.issue(G,p,a,function() return true end))
end)

print("refueling tests passed: "..passed)
