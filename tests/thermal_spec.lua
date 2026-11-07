package.path="./mods/jev_dst_agent/scripts/?.lua;"..package.path
local T=require("jev_thermal")
local N=require("jev_native_executor")
local R=require("jev_refueling")
local passed=0
local function test(name,fn)
 local ok,err=pcall(fn);assert(ok,name..": "..tostring(err));passed=passed+1;print("ok "..passed.." - "..name)
end
local function fixture()
 local p={Transform={GetWorldPosition=function() return 1,0,0 end},components={},player_classified={currenttemperature=66},
 GetTemperature=function(self) return self.player_classified.currenttemperature end,
 IsOverheating=function(self) return self:GetTemperature()>70 end,IsFreezing=function(self) return self:GetTemperature()<0 end}
 local G={GetTime=function() return 10 end,TUNING={OVERHEAT_TEMP=70,DARK_CUTOFF=.1},TheWorld={state={isnight=false}},
 EQUIPSLOTS={HANDS="hands"},TheSim={FindEntities=function() return {} end,GetLightAtPoint=function() return .8 end}}
 return G,p
end
local function vitals(G,p) local v={};T.read_vitals(G,p,v);return v end
local function state(hot)
 return {world={phase="day"},player={guid=1,position={x=1,z=0},vitals={health=150,hunger=100,hunger_max=150,
 temperature=hot and 66 or 30,heat_warning_threshold=65},inventory={items={},equipped={}}},nearby={},
 thermal={active=hot,route=hot and {x=5,z=0} or nil}}
end
local function clone(t) if type(t)~="table" then return t end;local r={};for k,v in pairs(t) do r[k]=clone(v) end;return r end
local function executor(s,extra)
 local adapter={stop=function() end,issue=function() end,cooling_safe=function() return true end}
 for k,v in pairs(extra or {}) do adapter[k]=v end
 local x=N.new(adapter);local a=N.available(s).cool_down
 assert(x:start({action_id="cool",choice="cool_down",action=a},s,0));return x
end
test("native client temperature and warning thresholds are observed",function()
 local G,p=fixture();local v=vitals(G,p)
 assert(v.temperature==66 and v.overheating==false and v.freezing==false and T.hot(v))
 assert(v.heat_recovery_threshold==60 and v.temperature_source=="player_native")
 p.player_classified.currenttemperature=71;assert(vitals(G,p).overheating)
 p.player_classified.currenttemperature=-1;assert(vitals(G,p).freezing)
end)
test("missing classified does not expose GetTemperature fallback as a reading",function()
 local G,p=fixture();p.player_classified=nil;p.GetTemperature=function() return 35 end
 assert(vitals(G,p).temperature==nil)
end)
test("server component threshold overrides tuning and getter fallback works",function()
 local G,p=fixture();p.components.temperature={overheattemp=80,GetCurrent=function() return 76 end}
 p.GetTemperature=nil;p.IsOverheating=nil;p.IsFreezing=nil
 local v=vitals(G,p);assert(v.temperature==76 and v.heat_warning_threshold==75 and v.overheating==false)
end)
test("cooling uses hysteresis and stops only at recovery threshold",function()
 local G,p=fixture();local v=vitals(G,p)
 assert(T.sample(G,p,v,{},function() return true end).active)
 p.player_classified.currenttemperature=63
 assert(T.sample(G,p,vitals(G,p),{},function() return true end).active)
 p.player_classified.currenttemperature=60
 assert(not T.sample(G,p,vitals(G,p),{},function() return true end).active)
end)
test("fire stages use native fueled values without inventing percentages",function()
 local G=fixture();local f={HasTag=function() return true end,components={fueled={
 GetCurrentSection=function() return 3 end,GetPercent=function() return .5 end}}}
 local r=T.fire(G,f);assert(r.lit and r.fire_section==3 and r.fuel_percent==.5)
 f.components=nil;f.Transform={GetWorldPosition=function() return 0,0,0 end}
 G.TheSim.FindEntities=function() return {{prefab="campfirefire",AnimState={IsCurrentAnimation=function(_,name) return name=="level4" end}}} end
 r=T.fire(G,f);assert(r.fire_section==4 and r.fuel_percent==nil and r.fire_data_source=="observed_fx")
 G.TheSim.FindEntities=function() return {} end
 r=T.fire(G,f);assert(r.lit and r.fire_section==nil and r.fuel_percent==nil)
 f.HasTag=function() return false end;assert(T.fire(G,f).fire_section==0)
end)
test("fuel pauses at sufficient or unknown fire stage and at high temperature",function()
 local G,p=fixture();p.player_classified.currenttemperature=35;local f={GUID=40}
 assert(T.fuel_reason(G,p,f,{lit=true,fire_section=2})=="fire_already_sufficient")
 assert(T.fuel_reason(G,p,f,{lit=true})=="fire_already_sufficient")
 assert(T.fuel_reason(G,p,f,{lit=true,fire_section=1})==nil)
 assert(T.fuel_reason(G,p,f,{lit=false,fire_section=0})==nil)
 p.player_classified.currenttemperature=66
 assert(T.fuel_reason(G,p,f,{lit=false})=="temperature_too_high")
end)
test("one fuel dispatch waits for replication before any second dispatch",function()
 local G,p=fixture();p.player_classified.currenttemperature=35
 local f={GUID=40,prefab="firepit",IsValid=function() return true end,HasTag=function() return false end}
 local item={GUID=20,prefab="log",IsValid=function() return true end};local sent=0
 G.Ents={[40]=f};G.ACTIONS={ADDFUEL={id="ADDFUEL"},ADDWETFUEL={id="ADDWETFUEL"}}
 local pc={IsBusy=function() return false end,GetItemUseAction=function() return {action=G.ACTIONS.ADDFUEL,target=f,invobject=item} end,
 RemoteControllerUseItemOnSceneFromInvTile=function() sent=sent+1 end}
 p.components.playercontroller=pc;p.replica={inventory={GetItems=function() return {item} end}}
 local a=R.scan(G,p,{{guid=40,distance=2}},function() return true end).options[1]
 assert(R.issue(G,p,a,function() return true end))
 local ok,reason=R.issue(G,p,a,function() return true end);assert(not ok and reason=="fuel_settling" and sent==1)
 G.GetTime=function() return 13 end;assert(R.issue(G,p,a,function() return true end) and sent==2)
end)
test("cooling route moves away from observed fire and respects passability",function()
 local G,p=fixture();local nearby={{prefab="firepit",lit=true,dx=-1,dz=0}}
 local r=T.sample(G,p,vitals(G,p),nearby,function() return true end)
 assert(r.route.x>1 and math.abs(r.route.z)<.001)
 assert(not T.sample(G,p,vitals(G,p),nearby,function() return false end).route)
end)
test("route cannot pass closer to heat even when endpoint is farther away",function()
 local G,p=fixture();p._jev_cooling_sources={{x=0,z=0}}
 assert(not T.route_safe(G,p,{x=-3,z=0},function() return true end))
end)
test("night checks the entire actual lit path, not only destination",function()
 local G,p=fixture();G.TheWorld.state.isnight=true
 G.TheSim.GetLightAtPoint=function(_,x) return x>2 and x<3 and 0 or .8 end
 assert(not T.route_safe(G,p,{x=5,z=0},function() return true end))
 G.TheSim.GetLightAtPoint=function() return .8 end
 assert(T.route_safe(G,p,{x=5,z=0},function() return true end))
 G.TheSim.GetLightAtPoint=nil;assert(not T.route_safe(G,p,{x=5,z=0},function() return true end))
end)
test("equipped torch supports night cooling but carried torch does not",function()
 local G,p=fixture();G.TheWorld.state.isnight=true;G.TheSim.GetLightAtPoint=function() return 0 end
 local held=nil;p.replica={inventory={GetEquippedItem=function() return held end}}
 assert(not T.route_safe(G,p,{x=5,z=0},function() return true end))
 held={prefab="torch",IsValid=function() return true end}
 assert(T.route_safe(G,p,{x=5,z=0},function() return true end))
end)
test("unknown heat source holds without inventing a destination",function()
 local G,p=fixture();local r=T.sample(G,p,vitals(G,p),{},function() return true end)
 assert(r.active and not r.route and r.reason=="no_safe_lit_cooling_route")
end)
test("failed cooling point is avoided for five seconds",function()
 local G,p=fixture();local nearby={{prefab="firepit",lit=true,dx=-1,dz=0}}
 local r=T.sample(G,p,vitals(G,p),nearby,function() return true end)
 T.failed(p,r.route,10);local next=T.sample(G,p,vitals(G,p),nearby,function() return true end)
 assert(next.route.x~=r.route.x or next.route.z~=r.route.z)
 G.GetTime=function() return 16 end
 assert(T.sample(G,p,vitals(G,p),nearby,function() return true end).route.x==r.route.x)
end)
test("hot snapshot exposes only thermal actions; night may equip torch beside fire",function()
 local s=state(true);s.world.phase="night";s.player.inventory.items={{prefab="torch",count=1}}
 s.nearby={{prefab="firepit",distance=1,tags={"fire"}}}
 local a=N.available(s);assert(a.cool_down.planner_only and a.equip_torch.thermal_safety and not a.explore)
 s.nearby[2]={activeThreat=true,dx=1,dz=0,distance=1}
 assert(not N.available(s).cool_down)
end)
test("native ordinary action interrupts when temperature becomes unsafe",function()
 local s=state(false);local x=N.new({stop=function() end,issue=function() end})
 assert(x:start({action_id="wait",choice="wait",action={kind="wait"}},s,0))
 x:tick(state(true),.2);assert(x.report.reason=="thermal_warning" and not x.active)
end)
test("verified fuel addition is retained when heat warning arrives",function()
 local s=state(false);s.player.inventory.items={{prefab="log",count=2}};s.recipe_inventory={log=2}
 s.nearby={{guid=40,prefab="firepit",distance=2,dx=-1,dz=0,tags={"fire"}}}
 local a={kind="add_fuel",guid=40,prefab="firepit",building_id="building:40",fuel_prefab="log",fuel_guid=20,native_action="ADDFUEL",planner_only=true}
 s.refueling={options={a}};local x=N.new({stop=function() end,issue=function() end})
 assert(x:start({action_id="fuel",choice="add_fuel:40:log",action=a},s,0))
 local after=clone(s);after.player.vitals.temperature=66;after.recipe_inventory.log=1
 after.execution={action="ADDFUEL",target_guid=40,confirmed_start=true,started_at=.1,issued_at=0}
 x:tick(after,.2);assert(x.report.status=="completed")
end)
test("cooling continues through heat damage and completion means one retreat step",function()
 local s=state(true);local x=executor(s);local after=clone(s)
 after.player.position.x=2;after.player.vitals.health=149;x:tick(after,.3);assert(x.active)
 after.player.position.x=5;x:tick(after,.6);assert(not x.active and x.report.reason=="cooling_step_completed")
 assert(N.available(after).cool_down)
end)
test("cooling stops if illumination path or player identity changes",function()
 local s=state(true);local valid=true;local x=executor(s,{cooling_safe=function() return valid end})
 valid=false;x:tick(s,.2);assert(x.report.reason=="cooling_route_unsafe")
 x=executor(s);local after=clone(s);after.player.guid=2;x:tick(after,.2);assert(x.report.reason=="player_changed")
end)
test("cooling yields immediately to hostile and ends when body is safe",function()
 local s=state(true);local x=executor(s);local after=clone(s);after.nearby={{activeThreat=true}}
 x:tick(after,.2);assert(x.report.reason=="targeted_by_hostile")
 x=executor(s);x:tick(state(false),.2);assert(x.report.reason=="temperature_safe")
end)
test("stalled cooling remembers bad destination without global retry delay",function()
 local s=state(true);local bad=0;local x=executor(s,{cooling_failed=function() bad=bad+1 end})
 x:tick(s,1.1);assert(x.report.reason=="movement_stalled" and bad==1)
 assert(x:available(s,1.2).cool_down)
end)
test("heat warning cannot interrupt hostile escape",function()
 local s=state(true);s.nearby={{guid=2,prefab="hound",activeThreat=true,distance=2,dx=-2,dz=0}}
 s.navigation={escape_routes={{dx=1,dz=0,distance=6}}}
 local x=N.new({stop=function() end,issue=function() end})
 assert(x:start({action_id="flee",choice="flee_from_nearest_hostile"},s,0))
 local after=clone(s);after.player.position.x=2;x:tick(after,.2)
 assert(x.active and x.active.action.kind=="flee_from_nearest_hostile")
end)
test("burning trees cause retreat before overheating, even without a temperature replica",function()
 local G,p=fixture()
 p.player_classified=nil
 local tree={prefab="evergreen",dx=2,dz=0,tags={"fire"}}
 local result=T.sample(G,p,{}, {tree},function() return true end)
 assert(result.active and result.flame_danger and result.available)
 assert(result.reason=="move_away_from_flames" and result.route.x<1)
 assert(N.available({world={phase="day"},player={vitals={},inventory={items={},equipped={}}},
   nearby={},thermal=result}).cool_down)
 tree.dx=5
 assert(not T.sample(G,p,{}, {tree},function() return true end).active)
 G.TheWorld.state.isspring=true;G.TUNING.SPRING_FIRE_RANGE_MOD=1.5
 assert(T.sample(G,p,{}, {tree},function() return true end).flame_danger)
end)
test("flame retreat avoids unlit paths and cannot approach another burning object",function()
 local G,p=fixture();p.player_classified.currenttemperature=30
 local tree={prefab="evergreen",dx=2,dz=0,tags={"fire"}}
 assert(not T.sample(G,p,vitals(G,p),{tree},function() return false end).route)
 G.TheWorld.state.isnight=true
 G.TheSim.GetLightAtPoint=function() return 0 end
 assert(not T.sample(G,p,vitals(G,p),{tree},function() return true end).route)
 G.TheWorld.state.isnight=false
 p._jev_cooling_sources={{x=0,z=0,flame_radius=4},{x=6,z=0,flame_radius=4}}
 assert(not T.route_safe(G,p,{x=5,z=0},function() return true end))
end)
test("a confirmed ignition immediately retreats and reports success only after getting clear",function()
 local s=state(false);local calls={}
 local action={kind="ignite_tree",guid=5,prefab="evergreen",torch_guid=10,planner_only=true}
 s.camp_work={options={ignite=action},trees={}}
 s.nearby={{guid=5,prefab="evergreen",distance=2,collect_path={status="ready"}}}
 local x=N.new({stop=function() end,issue=function(_,a) calls[#calls+1]=a.kind;return true end,
   cooling_safe=function() return true end})
 assert(x:start({action_id="ignite",choice="ignite",action=action},s,0))
 local burning=clone(s)
 burning.player.vitals.health=149
 burning.camp_work.trees={{guid=5,status="burning"}}
 burning.thermal={active=true,flame_danger=true,route={x=-3,z=0}}
 x:tick(burning,.2)
 assert(x.active and x.report.status=="running" and calls[2]=="cool_down")
 assert(x.active.ignition_confirmed)
 local clear=clone(burning)
 clear.player.position={x=-3,z=0}
 clear.thermal={active=false,flame_danger=false}
 x:tick(clear,1)
 assert(not x.active and x.report.status=="completed" and x.report.reason=="tree_ignited")
end)
test("ignition retreat continues in bounded safe steps until outside flame range",function()
 local s=state(false);local calls=0
 local action={kind="ignite_tree",guid=5,prefab="evergreen",planner_only=true}
 s.camp_work={options={ignite=action},trees={}}
 s.nearby={{guid=5,prefab="evergreen",distance=2,collect_path={status="ready"}}}
 local x=N.new({stop=function() end,issue=function() calls=calls+1;return true end,
   cooling_safe=function() return true end})
 assert(x:start({action_id="ignite",choice="ignite",action=action},s,0))
 s.camp_work.trees={{guid=5,status="burning"}}
 s.thermal={active=true,flame_danger=true,route={x=-1,z=0}}
 x:tick(s,.2)
 s.player.position={x=-1,z=0}
 s.thermal.route={x=-3,z=0}
 x:tick(s,.6)
 assert(x.active and calls==3 and x.active.action.x==-3)
end)
test("fuel intervals preserve quarter boundaries and never label estimates exact",function()
 for section=0,4 do
  local low,high,exact=T.fuel_bounds({fire_section=section})
  assert(low==math.max(0,(section-1)/4) and high==section/4 and not exact)
 end
 local low,high,exact=T.fuel_bounds({fire_section=4,fuel_percent=.91})
 assert(low==.91 and high==.91 and exact)
 assert(T.fuel_bounds({fire_section=1.5})==nil)
end)
test("ratio policy can top up stage two but stops at unverifiable stage four",function()
 local G,p=fixture();p.player_classified.currenttemperature=35;local f={GUID=40}
 assert(T.fuel_reason(G,p,f,{lit=true,fire_section=2})=="fire_already_sufficient")
 assert(T.fuel_reason(G,p,f,{lit=true,fire_section=2},true)==nil)
 assert(T.fuel_reason(G,p,f,{lit=true,fire_section=4},true)=="fuel_ratio_unverifiable")
 assert(T.fuel_reason(G,p,f,{lit=true},true)=="fuel_ratio_unavailable")
 p.player_classified.currenttemperature=66
 assert(T.fuel_reason(G,p,f,{lit=true,fire_section=2},true)=="temperature_too_high")
end)
print("thermal tests passed: "..passed)
