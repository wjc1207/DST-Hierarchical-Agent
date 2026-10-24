-- Run from the project root: fengari tests/exploration_spec.lua
package.path = "./scripts/?.lua;" .. package.path
local E = require("jev_exploration")
local X = require("jev_execution")
local passed = 0
local function test(name, fn)
    local ok, err=pcall(fn)
    if not ok then
        print("not ok - " .. name .. ": " .. tostring(err))
        os.exit(1)
    end
    passed = passed + 1
    print("ok " .. passed .. " - " .. name)
end
local function memory()
    return { cells = {}, graph_nodes = {}, graph_edges = {}, searches = {},
        biome_numbers = {}, current_region = "A" }
end
local function node(m, id)
    m.graph_nodes[id] = { id = id, entries = 0, observed_cells = 0, resources = {} }
end
local neighbors = {{-1,0},{1,0},{0,-1},{0,1}}
local function cell(m, x, z, region, biome, passable)
    m.cells[x .. ":" .. z] = { gx=x, gz=z, x=x*4, z=z*4,
        region=region, biome=biome, passable=passable ~= false }
end
local function frontier(x, z)
    return { key=x .. ":" .. z, x=x, z=z, information_gain=2, visits=0 }
end
local function leg(target)
    local length = math.sqrt(target.x^2 + target.z^2)
    return { x=target.x/length*math.min(length,4),
        z=target.z/length*math.min(length,4), distance=math.min(length,4) }
end
local area = {id="A", boundary_complete=false, unknown_boundary_cells=10}
local function search(m, candidates, safe)
    return E.search(m, candidates, area, 0, 0, 1, leg,
        safe or function() return true end)
end

test("the complete Mod and metadata compile", function()
    assert(loadfile("modmain.lua"))
    assert(loadfile("modinfo.lua"))
end)

test("path lookahead skips a nearby grid root behind the player", function()
    local path={{x=0,z=0},{x=0,z=4},{x=0,z=8}}
    local point=E.path_waypoint(path,0,1.8,1.5,8,function() return true end)
    assert(point.z == 8,"must not turn back toward z=0")
end)

test("path lookahead never shortcuts across blocked land or hazard", function()
    local path={{x=0,z=0},{x=0,z=4},{x=4,z=4},{x=8,z=4}}
    local point=E.path_waypoint(path,0,1.8,1.5,8,function(ax,az,bx,bz) return bx==0 end)
    assert(point.z == 4 and point.x == 0)
    assert(E.path_waypoint(path,0,1.8,1.5,8,function() return false end) == nil)
end)

test("gateway remains stable until it is no longer a valid candidate", function()
    local a={gateway_id="lower",distance=10}
    local b={gateway_id="upper",distance=10.1}
    assert(E.select_gateway({a,b},nil) == a)
    a.distance,b.distance=10.1,10
    assert(E.select_gateway({b,a},"lower") == a)
    assert(E.select_gateway({b},"lower") == b)
end)

test("gateway crossing survives leaving the source-side arrival radius", function()
    local crossing=E.gateway_crossing(false,327.3,-500.46,328,-500,324,-500,1.5)
    assert(crossing)
    assert(E.gateway_crossing(crossing,325.8,-500.46,328,-500,324,-500,1.5))
    assert(not E.gateway_crossing(crossing,340,-500,328,-500,324,-500,1.5),
        "a distant collection detour should reset approach")
    assert(not E.gateway_crossing(false,326,-506,328,-500,324,-500,1.5),
        "passing on a parallel path does not enter the gateway")
end)

test("unknown corridor frontiers agree with boundary completeness", function()
    local m=memory(); node(m,"A")
    for x=-1,3 do for z=-1,1 do cell(m,x,z,nil,nil,false) end end
    cell(m,0,0,"A","forest")
    cell(m,1,0,nil,"grassland"); cell(m,2,0,nil,"grassland")
    m.cells["3:0"]=nil
    local gains=E.frontier_gains(m,"A",neighbors)
    assert(gains["0:0"]==nil and gains["2:0"]==1)
    E.summarize(m,4,neighbors)
    assert(m.graph_nodes.A.unknown_boundary_cells==1)
    assert(m.graph_nodes.A.explored_area==16,"unlabelled cells must not inflate region area")
    cell(m,3,0,nil,nil,false)
    assert(next(E.frontier_gains(m,"A",neighbors))==nil)
    E.summarize(m,4,neighbors)
    assert(m.graph_nodes.A.boundary_complete,"observed empty labels are not new space")
end)

test("unlabelled frontier flood never crosses sea or another known room", function()
    local m=memory(); node(m,"A")
    for x=-1,4 do for z=-1,1 do cell(m,x,z,nil,nil,false) end end
    cell(m,0,0,"A","forest"); cell(m,1,0,"B","forest")
    cell(m,2,0,nil,"grassland"); m.cells["3:0"]=nil
    assert(next(E.frontier_gains(m,"A",neighbors))==nil)
    cell(m,1,0,nil,nil,false)
    assert(next(E.frontier_gains(m,"A",neighbors))==nil)
end)

test("mixed terrain remains one topology region with deduplicated area", function()
    local m=memory(); node(m,"A")
    cell(m,0,0,"A","forest"); cell(m,1,0,"A","grassland")
    E.summarize(m,4,neighbors)
    assert(m.graph_nodes.A.name == "mixed 1")
    assert(m.graph_nodes.A.explored_area == 32)
    E.summarize(m,4,neighbors)
    assert(m.graph_nodes.A.explored_area == 32)
    assert(m.graph_nodes.A.name == "mixed 1")
end)

test("same terrain in different topology regions has distinct stable names", function()
    local m=memory(); node(m,"A"); node(m,"B")
    cell(m,0,0,"A","grassland"); cell(m,1,0,"B","grassland")
    E.summarize(m,4,neighbors)
    assert(m.graph_nodes.A.name == "grassland 1")
    assert(m.graph_nodes.B.name == "grassland 2")
    local name=m.graph_nodes.A.name
    node(m,"0"); cell(m,-1,0,"0","grassland")
    E.summarize(m,4,neighbors)
    assert(m.graph_nodes.A.name == name)
    assert(m.graph_nodes["0"].name == "grassland 3")
end)

test("boundary completeness requires observed surroundings, not path failure", function()
    local m=memory(); node(m,"A"); cell(m,0,0,"A","forest")
    E.summarize(m,4,neighbors)
    assert(not m.graph_nodes.A.boundary_complete)
    for _,offset in ipairs(neighbors) do cell(m,offset[1],offset[2],nil,nil,false) end
    E.summarize(m,4,neighbors)
    assert(m.graph_nodes.A.boundary_complete)
end)

test("unknown-exit search is available without roads", function()
    local m=memory()
    local result=search(m,{frontier(12,0)})
    assert(result.status == "ready" and result.strategy == "directional")
    assert(result.heading.x == 1 and result.heading.z == 0)
    assert(result.leg.distance == 4)
end)

test("direction persists while local candidate ordering changes", function()
    local m=memory()
    search(m,{frontier(12,0),frontier(0,12)})
    local result=search(m,{frontier(0,12),frontier(12,0)})
    assert(result.heading.x == 1 and result.target.x == 12)
end)

test("blocked forward route selects a fixed boundary side and later resumes heading", function()
    local m=memory(); search(m,{frontier(12,0)})
    local function wall(ax,az,bx,bz) return bx <= 2 end
    local result=search(m,{frontier(0,12),frontier(0,-12)},wall)
    assert(result.strategy == "coast" and result.target.z == 12)
    result=search(m,{frontier(0,-12),frontier(0,12)},wall)
    assert(result.target.z == 12)
    result=search(m,{frontier(12,0),frontier(0,12)})
    assert(result.strategy == "directional" and result.heading.x == 1)
end)

test("isolated narrow obstruction does not imply coastline", function()
    local m=memory(); search(m,{frontier(12,0)})
    local function pillar(ax,az,bx,bz) return not (bx > 2 and az == 0 and bz == 0) end
    local result=search(m,{frontier(0,12)},pillar)
    assert(result.strategy ~= "coast")
end)

test("temporary obstruction does not mark boundary complete", function()
    local m=memory()
    local result=search(m,{frontier(12,0)},function() return false end)
    assert(result.status == "temporarily_blocked" and not result.boundary_complete)
    assert(search(m,{frontier(12,0)}).status == "ready")
end)

test("completed boundary suppresses unknown search", function()
    local result=E.search(memory(),{}, {id="A",boundary_complete=true},
        0,0,1,leg,function() return true end)
    assert(result.status == "boundary_complete")
end)

test("leftover road memory cannot steer directional search", function()
    local m=memory()
    m.road_target=frontier(-6,0)
    local result=search(m,{frontier(12,0)})
    assert(result.strategy == "directional" and result.heading.x == 1)
end)

test("rejected search waypoint pauses without changing local-survey target", function()
    local m=memory(); local a,b=frontier(12,0),frontier(0,12)
    m.target=a
    search(m,{a,b})
    E.reject(m,2)
    assert(m.target == a)
    local result=search(m,{a,b})
    assert(result.target.z == 12)
end)

test("recorded destinations include a former region and multi-hop regions", function()
    local m=memory(); node(m,"A"); node(m,"B"); node(m,"C")
    m.graph_nodes.B.entries=1
    m.graph_edges.AB={{a_id="A",b_id="B"}}
    m.graph_edges.BC={{a_id="B",b_id="C"}}
    E.summarize(m,4,neighbors)
    local first={distance=8,target={x=8,z=0},leg={x=4,z=0,distance=4}}
    local result=E.destinations(m,"A",{B=first})
    assert(#result == 2 and result[1].node_id == "B" and result[1].entered)
    assert(result[2].node_id == "C" and result[2].hops == 2)
    assert(result[2].next_node_id == "B" and result[2].target == first.target)
    assert(#E.destinations(m,"A",{}) == 0)
end)

test("unknown topology on passable neighboring land prevents completeness", function()
    local m=memory(); node(m,"A"); cell(m,0,0,"A","forest")
    for _,offset in ipairs(neighbors) do cell(m,offset[1],offset[2],nil,nil,false) end
    cell(m,1,0,nil,"forest",true)
    E.summarize(m,4,neighbors)
    assert(not m.graph_nodes.A.boundary_complete)
end)

test("trajectory loops pause the old frontier instead of bouncing forever", function()
    local m=memory(); search(m,{frontier(12,0)})
    m.searches.A.history={{x=0,z=0},{x=4,z=0},{x=8,z=0},{x=8,z=4},{x=4,z=4},{x=0,z=4}}
    local result=search(m,{frontier(12,0),frontier(0,12)})
    assert(result.status == "ready" and result.target.z == 12)
    assert(m.searches.A.pauses["12:0"] == 31)
end)

local function simulated_mod(passable, topology, terrain, entities, bridge_config)
    local handlers, captured, errors = {}, nil, {}
    local sim_init
    local x,z,now=0,0,0
    local ground={FOREST=1,GRASS=2,SAVANNA=3,MARSH=4,ROCKY=5,DECIDUOUS=6,DESERT_DIRT=7,DIRT=8}
    local map={
        IsPassableAtPoint=function(_,ax,ay,az) return passable(ax,az) end,
        GetTopologyIDAtPoint=function(_,ax,ay,az) return topology(ax,az) end,
        GetTileAtPoint=function(_,ax,ay,az) return terrain(ax,az) end,
    }
    local player={GUID=1,prefab="wilson",replica={},IsValid=function() return true end,
        Transform={GetWorldPosition=function() return x,0,z end}}
    local G=setmetatable({GROUND=ground,ThePlayer=player,TheWorld={Map=map,state={phase="day"}},
        TheSim={FindEntities=function() return entities or {} end},
        GetTime=function() return now end,
        GetTimeRealSeconds=bridge_config and bridge_config.measurement_clock,
        ConvertTopologyIdToData=function(id) return {task_id=id} end,
    },{__index=function(_,k)
        if string.sub(k,1,4) == "KEY_" then return k end
        return _G[k]
    end})
    local env=setmetatable({GLOBAL=G,GetModConfigData=function() return nil end,
        AddPlayerPostInit=function() end,AddSimPostInit=function(fn) sim_init=fn end,
        require=function(name)
            if name == "json" then return {encode=function(value) captured=value; return "{}" end,
                decode=function() return bridge_config and bridge_config.reply end} end
            if name == "jev_bridge_config" and bridge_config then return bridge_config end
            return require(name)
        end,
        print=function(value) table.insert(errors,value) end,
    },{__index=_G})
    local native=assert(loadfile("modmain.lua","t",env))()
    -- Test-only aliases preserve existing native-action scenarios, not key events.
    handlers={KEY_F2=native.emit_state,KEY_F4=native.cancel,
        KEY_KP_MULTIPLY=native.craft_torch,KEY_KP_DIVIDE=native.equip_torch,
        KEY_KP_MINUS=native.unequip_torch,KEY_KP_PLUS=native.craft_axe,KEY_KP_PERIOD=native.craft_pickaxe,
        KEY_F6=native.build_campfire,KEY_F7=native.eat_safe_food,KEY_F8=native.chop,KEY_F9=native.mine,
        KEY_F10=native.attack,KEY_F11=native.equip_weapon,KEY_F12=native.build_sciencemachine,
        KEY_F3=native.cook_food,KEY_F5=native.reject_frontier,KEY_F1=native.reject_search}
    local function sample(ax,az)
        x,z,now=ax,az,now+1
        captured=nil
        handlers.KEY_F2()
        assert(captured,table.concat(errors,"\n"))
        return captured
    end
    return sample, G.TheWorld, G, handlers, function() sim_init(); return captured end, function() return captured end
end

test("real Mod keeps escape routes for observed targeted hostiles beyond twelve units",function()
    local x,targeted,blocked=11.9,true,false
    local enemy={GUID=22,prefab="frog",IsValid=function() return true end,
        HasTag=function(_,tag) return tag=="hostile" end,
        Transform={GetWorldPosition=function() return x,0,0 end},replica={}}
    local sample,_,G=simulated_mod(function() return not blocked end,function() return "A" end,
        function() return 2 end,{enemy})
    enemy.replica.combat={GetTarget=function() return targeted and G.ThePlayer or nil end}
    G.TheCamera={GetRightVec=function() return {x=1,z=0} end,
        GetDownVec=function() return {x=0,z=-1} end}
    for _,distance in ipairs({11.9,12,12.1,13,20}) do
        x=distance
        local s=sample(0,0)
        assert(s.nearby[1].activeThreat and s.nearby[1].distance==distance)
        assert(#s.navigation.escape_routes==8,"observed threat lost routes at distance "..distance)
        assert(require("jev_native_executor").available(s).flee_from_nearest_hostile)
    end
    targeted=false
    assert(#sample(0,0).navigation.escape_routes==0,"unlocked hostiles do not require escape routes")
    targeted=true; blocked=true
    assert(#sample(0,0).navigation.escape_routes==0,"impassable ground must still block escape routes")
end)

test("juicy berry telemetry respects replicated tags and CanBePicked", function()
    local ripe=true
    local bush={prefab="berrybush_juicy",GUID=22,IsValid=function() return true end,
        HasTag=function(_,tag) return (tag=="pickable" and ripe) or tag=="DIG_workable" end,
        Transform={GetWorldPosition=function() return 2,0,0 end}}
    local sample=simulated_mod(function() return true end,function() return "A" end,
        function() return 2 end,{bush})
    local ready=sample(0,0)
    assert(ready.nearby[1].pickable==true)
    assert(ready.nearby[1].collect_path.status=="ready")
    assert(ready.navigation.graph.current.resources_seen[1]=="berrybush_juicy")
    ripe=false
    assert(sample(0,0).nearby[1].pickable==false)
    ripe=true
    bush.components={pickable={CanBePicked=function() return false end}}
    assert(sample(0,0).nearby[1].pickable==false,"server component must also permit picking")
end)

test("execution telemetry exposes doing, exact buffered target and recipe", function()
    local sample,_,G=simulated_mod(function() return true end,function() return "A" end,
        function() return 2 end)
    local doing=false
    local action={action={id="PICK"},target={GUID=22}}
    G.ThePlayer.GetBufferedAction=function() return action end
    G.ThePlayer.sg={HasStateTag=function(_,tag) return doing and tag=="doing" end}
    assert(not sample(0,0).execution.doing)
    doing=true
    local pick=sample(0,0).execution
    assert(pick.doing and pick.action=="PICK" and pick.target_guid==22)
    action={action={id="BUILD"},recipe="axe"}
    local build=sample(0,0).execution
    assert(build.doing and build.action=="BUILD" and build.recipe=="axe")
    action=nil; doing=false
    assert(sample(0,0).execution.action==nil)
end)

test("ordinary flowers export CanBePicked and disappear safely after picking", function()
    local ripe=true
    local flower={prefab="flower",GUID=22,IsValid=function() return true end,
        HasTag=function(_,tag) return tag=="pickable" end,
        components={pickable={CanBePicked=function() return ripe end}},
        Transform={GetWorldPosition=function() return 2,0,0 end}}
    local entities={flower}
    local sample=simulated_mod(function() return true end,function() return "A" end,
        function() return 2 end,entities)
    local ready=sample(0,0)
    assert(ready.nearby[1].pickable==true and ready.nearby[1].collect_path.status=="ready")
    assert(ready.navigation.graph.current.resources_seen[1]=="flower")
    ripe=false
    assert(sample(0,0).nearby[1].pickable==false)
    table.remove(entities,1)
    assert(#sample(0,0).nearby==0)
end)

test("non-predicting client confirms a native request only after replicated doing", function()
    local now, doing, callback, starts, sent = 1, false, nil, 0, 0
    local player={components={},HasTag=function(_,tag) return doing and tag=="doing" end,
        GetBufferedAction=function() return nil end}
    local task={Cancel=function(self) self.cancelled=true end}
    player.DoPeriodicTask=function(_,interval,fn) callback=fn; return task end
    local pc={RemoteActionButton=function(_,action,released) sent=sent+1; return "original",released end,
        RemoteMakeRecipeFromMenu=function(_,recipe,skin) return skin end}
    player.components.playercontroller=pc
    X.attach(player,function() return now end,function() starts=starts+1 end)
    local original,release=pc:RemoteActionButton({action={id="PICK"},target={GUID=22}},true)
    assert(original=="original" and release==true and sent==1,"wrapper must preserve native return values")
    assert(X.read(player,now).stage=="requested")
    assert(not X.read(player,now).confirmed_start,"a sent RPC is not an acknowledgement")
    now=1.1; callback(); assert(starts==0)
    doing=true; now=1.2; callback()
    local confirmed=X.read(player,now)
    assert(starts==1 and confirmed.confirmed_start and confirmed.action=="PICK")
    assert(confirmed.target_guid==22 and confirmed.start_source=="replicated_transition")
    callback(); assert(starts==1,"start must be emitted once")
    pc:RemoteActionButton(nil,true)
    assert(X.read(player,now).target_guid==22,"key release must not replace the request")
    doing=false; now=1.3; callback()
    assert(task.cancelled)
    local latched=X.read(player,1.4)
    assert(not latched.doing and latched.confirmed_start and latched.stage=="finished_unverified")
    assert(not X.read(player,2.2).confirmed_start,"old start latch must expire")
end)

test("busy state before request and unrelated actions cannot acknowledge it", function()
    local doing=true
    local action=nil
    local player={HasTag=function(_,tag) return doing and tag=="doing" end,
        GetBufferedAction=function() return action end}
    X.request(player,{action={id="PICK"},target={GUID=22}},1)
    assert(not X.read(player,1.1).confirmed_start)
    doing=false; X.read(player,1.2)
    action={action={id="PICK"},target={GUID=23}}; doing=true
    assert(not X.read(player,1.3).confirmed_start,"wrong buffered target must not match")
    doing=false; action=nil
    assert(not X.read(player,4.1).confirmed_start,"rejected request must expire without an acknowledgement")
end)

test("non-predicting client tracks exact remotely requested build recipe", function()
    local doing=false
    local player={components={},HasTag=function(_,tag) return doing and tag=="doing" end}
    local pc={RemoteMakeRecipeFromMenu=function(_,recipe,skin) return recipe.name,skin end}
    player.components.playercontroller=pc
    X.attach(player,function() return 1 end)
    local name,skin=pc:RemoteMakeRecipeFromMenu({name="axe"},"skin")
    assert(name=="axe" and skin=="skin")
    assert(not X.read(player,1.1).confirmed_start)
    doing=true
    local status=X.read(player,1.2)
    assert(status.confirmed_start and status.recipe=="axe" and status.action=="BUILD")
end)

test("real Mod eats raw or cooked juicy berries and cooks raw juicy berries", function()
    local fire={prefab="campfire",GUID=99,IsValid=function() return true end,
        HasTag=function(_,tag) return tag=="fire" or tag=="cooker" end}
    local _,_,G,handlers=simulated_mod(function() return true end,function() return "A" end,
        function() return 2 end,{fire})
    local food={prefab="berries_juicy",GUID=30,IsValid=function() return true end,
        HasTag=function(_,tag) return tag=="cookable" end}
    local eaten,cooked
    G.ThePlayer.replica.inventory={
        FindItem=function(_,fn) if fn(food) then return food end end,
        UseItemFromInvTile=function(_,item) eaten=item end}
    G.ThePlayer.GetDistanceSqToInst=function() return 4 end
    G.ACTIONS={COOK={}}
    G.ThePlayer.components={playercontroller={ismastersim=true,IsBusy=function() return false end,
        GetItemUseAction=function() return {action=G.ACTIONS.COOK} end,
        DoAction=function(_,action) cooked=action end}}
    handlers.KEY_F7(); assert(eaten==food)
    handlers.KEY_F3(); assert(cooked and cooked.action==G.ACTIONS.COOK)
    food.prefab="berries_juicy_cooked"; eaten=nil
    handlers.KEY_F7(); assert(eaten==food)
end)

test("real Mod telemetry keeps mixed terrain together and unifies round-trip routes", function()
    local sample=simulated_mod(function(x,z) return x>=-24 and x<=48 and math.abs(z)<=24 end,
        function(x,z) return x<20 and "A" or "B" end,
        function(x,z) if x>=20 then return 5 end; return z<0 and 1 or 2 end)
    local initial=sample(0,0)
    assert(initial.schema == 10 and initial.navigation.graph.current.id == "A")
    assert(initial.navigation.graph.current.name == "mixed 1")
    assert(initial.navigation.search.status == "ready")
    assert(initial.navigation.road == nil and initial.navigation.on_road == nil)
    local border=sample(16,0)
    assert(#border.navigation.graph.routes == 1)
    assert(border.navigation.graph.routes[1].node_id == "B")
    assert(border.navigation.search.status == "ready")
    sample(25,0)
    local other=sample(25,0)
    assert(other.navigation.graph.current.id == "B")
    assert(#other.navigation.graph.routes == 1)
    assert(other.navigation.graph.routes[1].node_id == "A")
    assert(other.navigation.graph.routes[1].entered)
    assert(other.navigation.graph.current.explored_area>0)
end)

test("real Mod does not connect visually adjacent land across a narrow sea gap", function()
    local sample=simulated_mod(function(x,z) return not (x>1.5 and x<2.5) end,
        function(x,z) return x<2 and "A" or "B" end,function() return 2 end)
    local result=sample(0,0)
    assert(result.navigation.graph.known_nodes == 2)
    assert(#result.navigation.graph.routes == 0)
end)

test("real Mod exhausts a fully observed island without inventing unknown exits", function()
    local sample=simulated_mod(function(x,z) return math.abs(x)<=4 and math.abs(z)<=4 end,
        function() return "A" end,function() return 2 end)
    local result=sample(0,0)
    assert(result.navigation.graph.current.boundary_complete)
    assert(result.navigation.search.status == "boundary_complete")
end)

test("real Mod offers exploration along an observed unlabelled land corridor", function()
    local sample=simulated_mod(function(x,z) return x>=-4 and math.abs(z)<=4 end,
        function(x,z) if x<=0 then return "A" end end,function() return 2 end)
    local result=sample(0,0)
    assert(result.navigation.status=="ready")
    assert(result.navigation.search.status=="ready")
    assert(not result.navigation.graph.current.boundary_complete)
    assert(result.navigation.target.x>0)
    assert(result.navigation.graph.current.explored_area>0)
end)

test("real Mod crossing keeps destination-side legs until topology settles", function()
    local sample,world=simulated_mod(function() return true end,
        function(x,z) return x<20 and "A" or "B" end,function() return 2 end)
    sample(0,0)
    local m=world._jev_dst_navigation
    node(m,"B")
    m.graph_edges={AB={{a_id="A",b_id="B",ax=16,az=0,bx=20,bz=0}}}
    local first=sample(15.3,0).navigation.graph.routes[1]
    assert(first.leg.x==19.3 or math.abs(first.leg.x-19.3)<0.01)
    local mid=sample(18.2,0).navigation.graph.routes[1]
    assert(mid and mid.leg.x>18.2,"crossing must not turn back to x=16")
    local debounce=sample(20,0)
    assert(debounce.navigation.graph.current_node=="A")
    local settled=sample(20,0)
    assert(settled.navigation.graph.current_node=="B")
    for id in pairs(m.gateway_crossings) do assert(string.sub(id,1,2)=="B>") end
end)

test("a blocked crossing never falls back to walking toward its source side", function()
    local blocked=false
    local sample,world=simulated_mod(function(x,z) return not (blocked and x>=19 and x<=21) end,
        function(x,z) return x<20 and "A" or "B" end,function() return 2 end)
    sample(0,0)
    local m=world._jev_dst_navigation; node(m,"B")
    m.graph_edges={AB={{a_id="A",b_id="B",ax=16,az=0,bx=20,bz=0}}}
    sample(15.3,0)
    blocked=true
    local result=sample(18.2,0)
    for _,route in ipairs(result.navigation.graph.routes) do
        assert(route.gateway_id~="A>B:16:0:20:0",
            "blocked crossing must not be revived by a reverse source-side leg")
    end
end)

test("known-region travel detours around a hazard instead of discarding a valid route", function()
    local sample, world=simulated_mod(function() return true end,
        function(x,z) return x<24 and "A" or "B" end,function() return 2 end)
    sample(0,0)
    local m=world._jev_dst_navigation
    node(m,"B")
    for gx=-5,9 do
        for gz=-8,8 do
            local region=gx<6 and "A" or "B"
            cell(m,gx,gz,region,"grassland")
            m.cells[gx .. ":" .. gz].node=region
        end
    end
    m.graph_edges={AB={{a_id="A",b_id="B",ax=20,az=24,bx=24,bz=24}}}
    m.hazards[77]={guid=77,prefab="killerbee",x=12,z=0,last_seen=1}
    local result=sample(0,0)
    assert(#result.navigation.graph.routes>0,"safe northern detour was omitted")
    local leg=result.navigation.graph.routes[1].leg
    assert(leg.detour and math.sqrt((leg.x-12)^2+leg.z^2)>12.5,
        "first leg should move safely away around the hazard")
end)

test("safe observed detour may cross another topology room instead of treating labels as walls", function()
    local function region(x,z) if x>=24 then return "C" end; return z>=8 and "B" or "A" end
    local function land(x,z) return not (x>=2 and x<=6 and z<6) end
    local sample,world=simulated_mod(land,region,function() return 2 end)
    sample(0,0)
    local m=world._jev_dst_navigation
    node(m,"B"); node(m,"C")
    for gx=-2,8 do
        for gz=0,5 do
            local id=region(gx*4,gz*4)
            cell(m,gx,gz,id,"grassland",land(gx*4,gz*4))
            m.cells[gx .. ":" .. gz].node=id
        end
    end
    m.graph_edges={AC={{a_id="A",b_id="C",ax=20,az=0,bx=24,bz=0}}}
    local result=sample(0,0)
    local found=false
    for _,route in ipairs(result.navigation.graph.routes) do
        if route.node_id == "C" then found=route.leg.detour end
    end
    assert(found,"reachable destination across an observed neighboring room was lost")
    assert(result.navigation.diagnostics.graph_paths.detour_ready>0)
    local progressed=sample(0,5.8)
    local forward=false
    for _,route in ipairs(progressed.navigation.graph.routes) do
        if route.node_id == "C" then forward=route.leg.z>5.8 end
    end
    assert(forward,"recomputed BFS root at z=4 must not pull the player backwards")
end)

test("reobserved empty positions clear obsolete hazard memory but remote positions do not", function()
    local sample,world=simulated_mod(function() return true end,
        function() return "A" end,function() return 2 end)
    sample(0,0)
    local m=world._jev_dst_navigation
    m.hazards[1]={guid=1,prefab="killerbee",x=4,z=0,last_seen=1}
    m.hazards[2]={guid=2,prefab="killerbee",x=24,z=0,last_seen=1}
    local result=sample(0,0)
    assert(m.hazards[1] == nil,"observed-empty old position should not remain hazardous")
    assert(m.hazards[2] ~= nil,"unseen remote hazard must retain its safety memory")
    assert(result.navigation.diagnostics.hazards.observed_absent == 1)
    assert(result.navigation.diagnostics.hazard_memory.retained == 1)
end)

test("real Mod keeps one gateway through small distance changes and abandons it when unsafe", function()
    local sample,world=simulated_mod(function() return true end,
        function(x,z) return x<20 and "A" or "B" end,function() return 2 end)
    sample(0,0)
    local m=world._jev_dst_navigation
    node(m,"B")
    m.graph_edges={AB={
        {a_id="A",b_id="B",ax=16,az=-4,bx=20,bz=-4},
        {a_id="A",b_id="B",ax=16,az=4,bx=20,bz=4},
    }}
    local first=sample(0,-0.1).navigation.graph.routes[1]
    assert(first.target.z == -4)
    local second=sample(0,0.1).navigation.graph.routes[1]
    assert(second.gateway_id == first.gateway_id,"minor distance change switched the gateway")
    m.hazards[8]={guid=8,prefab="killerbee",x=24,z=-16,last_seen=3}
    local rerouted=sample(0,0.1).navigation.graph.routes[1]
    assert(rerouted and rerouted.target.z == 4,"unsafe cached gateway was not replaced")
end)

test("local board work gets passable-ground paths and cancellation capability", function()
    local entities={}
    for i,tag in ipairs({"CHOP_workable","MINE_workable"}) do
        entities[i]={GUID=500+i,prefab=i==1 and "evergreen" or "rock1",
            IsValid=function() return true end,
            HasTag=function(_,name) return name==tag end,
            Transform={GetWorldPosition=function() return i*2,0,0 end}}
    end
    local sample=simulated_mod(function() return true end,function() return "A" end,
        function() return 2 end,entities)
    local ready=sample(0,0)
    assert(ready.telemetry.cancel_work==true)
    assert(#ready.nearby==2)
    for _,entity in ipairs(ready.nearby) do assert(entity.collect_path.status=="ready") end
    local blocked=simulated_mod(function(x) return x<1 end,function() return "A" end,
        function() return 2 end,entities)(0,0)
    for _,entity in ipairs(blocked.nearby) do assert(entity.collect_path.status=="blocked") end
end)

test("F4 cancels queued work and stops controls before handing back to JEV", function()
    local _,_,G,handlers=simulated_mod(function() return true end,function() return "A" end,
        function() return 2 end)
    local cancelled,cleared,remote,stopped=0,0,0,0
    G.ThePlayer._jev_dst_work_task={Cancel=function() cancelled=cancelled+1 end}
    G.ThePlayer._jev_dst_work={status="started"}
    G.ThePlayer.ClearBufferedAction=function() cleared=cleared+1 end
    G.ThePlayer.components={playercontroller={RemoteStopAllControls=function() remote=remote+1 end,
        locomotor={Stop=function() stopped=stopped+1 end}}}
    handlers.KEY_F4()
    assert(cancelled==1 and G.ThePlayer._jev_dst_work_task==nil)
    assert(G.ThePlayer._jev_dst_work.status=="failed")
    assert(G.ThePlayer._jev_dst_work.reason=="controller_interrupted")
    assert(cleared==1 and remote==1 and stopped==1)
    handlers.KEY_F4(); assert(cancelled==1,"cancel must be idempotent")
    G.ThePlayer=nil; handlers.KEY_F4()
end)

test("repeated work expires even while busy or lacking an action", function()
    for _,mode in ipairs({"busy","no_action","working"}) do
        local target={GUID=22,prefab="evergreen",IsValid=function() return true end,
            HasTag=function(_,tag) return tag=="CHOP_workable" end,
            Transform={GetWorldPosition=function() return 2,0,0 end}}
        local _,_,G,handlers=simulated_mod(function() return true end,function() return "A" end,
            function() return 2 end,{target})
        local clock,queued,issued,stopped=0,nil,0,0
        G.GetTime=function() return clock end
        G.EQUIPSLOTS={HANDS="hands"}; G.ACTIONS={CHOP={id="CHOP"}}
        local tool={prefab="axe",IsValid=function() return true end,
            components={finiteuses={GetPercent=function() return 1 end}}}
        G.ThePlayer.replica.inventory={GetEquippedItem=function() return tool end,
            GetItems=function() return {} end}
        G.ThePlayer.GetDistanceSqToInst=function() return 4 end
        G.ThePlayer.DoTaskInTime=function(_,delay,fn)
            queued=fn; return {Cancel=function() queued=nil end}
        end
        G.ThePlayer.ClearBufferedAction=function() end
        G.ThePlayer.components={playercontroller={ismastersim=true,
            IsBusy=function() return mode=="busy" end,
            GetActionButtonAction=function()
                if mode~="no_action" then return {action=G.ACTIONS.CHOP} end
            end,
            DoAction=function() issued=issued+1 end,
            RemoteStopAllControls=function() stopped=stopped+1 end,
            locomotor={Stop=function() end}}}
        handlers.KEY_F8()
        assert(queued and G.ThePlayer._jev_dst_work.status=="started")
        clock=29.9; local retry=queued; queued=nil; retry()
        assert(queued and G.ThePlayer._jev_dst_work.status=="started")
        clock=30; retry=queued; queued=nil; retry()
        assert(queued==nil and stopped==1)
        assert(G.ThePlayer._jev_dst_work.status=="failed")
        assert(G.ThePlayer._jev_dst_work.reason=="completion_timeout")
        local count=issued
        retry(); assert(issued==count and queued==nil,"failed work must not restart")
        handlers.KEY_F8(); retry=queued
        assert(retry)
        handlers.KEY_F4(); queued=nil
        retry(); assert(queued==nil,"cancelled callback must not restart work")
    end
end)

test("gateway continuation is fixed and never steers back after overshoot", function()
    local target=E.gateway_destination(16,0,20,0,2)
    assert(target.x==22 and target.z==0)
    assert(not E.gateway_reached(21,0,16,0,22,0,0.2))
    assert(E.gateway_reached(22,0,16,0,22,0,0.2))
    assert(E.gateway_reached(22.7,0,16,0,22,0,0.2))
    assert(not E.gateway_reached(22.7,4,16,0,22,0,0.2),"parallel paths are not arrivals")
end)

test("real border replay retains its direct gateway inside old 1.5-unit arrival radius", function()
    local sample,world=simulated_mod(function() return true end,
        function(x,z) if z<100 then return "C" end; return x>=24 and "B" or "A" end,
        function() return 2 end)
    sample(20,104.69)
    local m=world._jev_dst_navigation; node(m,"B"); node(m,"C")
    m.graph_edges={AB={{a_id="A",b_id="B",ax=20,az=108,bx=24,bz=104}},
        AC={{a_id="A",b_id="C",ax=12,az=100,bx=12,bz=96}},
        CB={{a_id="C",b_id="B",ax=20,az=96,bx=24,bz=96}}}
    m.gateway_targets.A={B="A>B:20:108:24:104"}
    for _,x in ipairs({22.08,22.77,23.07}) do
        local result=sample(x,104.69)
        local found
        for _,r in ipairs(result.navigation.graph.routes) do if r.node_id=="B" then found=r end end
        assert(found and found.gateway_id=="A>B:20:108:24:104","direct gateway disappeared near arrival")
        assert(found.hops==1 and found.leg.x>x,"routing must keep crossing, not select a westward detour")
        assert(found.leg.arrival_distance==0.2)
    end
    sample(24.2,103.5)
    assert(sample(24.2,103.5).navigation.graph.current_node=="B")
end)

test("gateway stays present while awaiting topology confirmation at fixed continuation", function()
    local sample,world=simulated_mod(function() return true end,function() return "A" end,
        function() return 2 end)
    sample(0,0)
    local m=world._jev_dst_navigation; node(m,"B")
    m.graph_edges={AB={{a_id="A",b_id="B",ax=16,az=0,bx=20,bz=0}}}
    sample(18.2,0)
    for _,x in ipairs({22,22.7}) do
        local result=sample(x,0)
        local route=result.navigation.graph.routes[1]
        assert(result.navigation.graph.current_node=="A")
        assert(route and route.node_id=="B" and route.leg.awaiting_region)
        assert(route.leg.distance==0 and route.leg.x==x,"do not move backwards or extend indefinitely")
    end
end)

test("unsafe gateway overshoot falls back to the passable endpoint without crossing sea", function()
    local sample,world=simulated_mod(function(x) return x<=20.1 end,
        function(x) return x<20 and "A" or "B" end,function() return 2 end)
    sample(0,0)
    local m=world._jev_dst_navigation; node(m,"B")
    m.graph_edges={AB={{a_id="A",b_id="B",ax=16,az=0,bx=20,bz=0}}}
    local result=sample(18.2,0)
    local route=result.navigation.graph.routes[1]
    assert(route and route.leg.x<=20.1 and route.leg.x==20)
    assert(route.leg.arrival_distance==0.2)
end)

local N=require("jev_native_executor")
local B=require("jev_bridge")
local function bridge_state()
    return {player={guid=1,position={x=0,z=0},vitals={health=150,hunger=100,hunger_max=150},
        inventory={items={},equipped={}}},world={phase="day"},crafting={},nearby={},navigation={}}
end
local function native_test()
    local calls,stops={},0
    local executor=N.new({issue=function(id,a) calls[#calls+1]={id=id,action=a} end,
        stop=function() stops=stops+1 end})
    return executor,calls,function() return stops end
end
test("real Mod HTTP callback contains native dispatch exceptions instead of crashing",function()
    local cfg={enabled=true,url="http://127.0.0.1:8765/v1/exchange",token=string.rep("x",32)}
    local plant={GUID=22,prefab="grass",IsValid=function() return true end,
        HasTag=function(_,tag) return tag=="pickable" end,
        Transform={GetWorldPosition=function() return 1,0,0 end}}
    local _,world,G,_,start,captured=simulated_mod(function() return true end,function() return "A" end,
        function() return 2 end,{plant},cfg)
    local ticks,callback={},nil
    world.IsValid=function() return true end
    world.DoTaskInTime=function(_,delay,fn) fn() end
    G.ThePlayer.ListenForEvent=function() end
    G.ThePlayer.DoPeriodicTask=function(_,interval,fn) ticks[interval]=fn; return {Cancel=function() end} end
    G.ThePlayer.GetDistanceSqToInst=function() return 1 end
    G.ThePlayer.components={playercontroller={IsBusy=function() return false end,
        GetActionButtonAction=function() error("test_native_fault") end}}
    G.Ents={[22]=plant}
    G.TheSim.QueryServer=function(_,url,fn,method,body)
        assert(url==cfg.url and method=="POST" and body=="{}"); callback=fn
    end
    start(); assert(ticks[0.2])
    ticks[0.2](); local sent=captured(); assert(callback and sent.token==cfg.token)
    cfg.reply={server_id="s",session_id=sent.session_id,sequence=sent.sequence,board_revision=0,
        command={session_id=sent.session_id,operation="start",action_id="a",choice="collect_grass",action={guid=22}}}
    assert(pcall(function() callback("{}",true,200) end),"asynchronous native errors escaped into the game")
    ticks[1](G.ThePlayer)
    assert(captured().bridge.action_report.status=="failed")
    assert(captured().bridge.action_report.reason=="native_exception")
end)

test("real Mod defers busy or cooling-down pickup and preserves native client RPC wiring",function()
    for _,mode in ipairs({"master","predicting","nonpredicting"}) do
        local cfg={enabled=true,url="http://127.0.0.1:8765/v1/exchange",token=string.rep("x",32)}
        local item={GUID=22,prefab="log",IsValid=function() return true end,
            HasTag=function(_,tag) return tag=="_inventoryitem" end,
            Transform={GetWorldPosition=function() return 1,0,0 end}}
        local _,world,G,handlers,start,captured=simulated_mod(function() return true end,
            function() return "A" end,function() return 2 end,{item},cfg)
        local ticks,callback={},nil
        local clock,busy,ready,issued,rpcs,picker_calls=0,true,false,0,0,0
        world.IsValid=function() return true end
        world.DoTaskInTime=function(_,delay,fn) fn() end
        G.GetTime=function() return clock end
        G.ACTIONS={PICK={id="PICK"},PICKUP={id="PICKUP"}}
        G.Ents={[22]=item}
        G.ThePlayer.ListenForEvent=function() end
        G.ThePlayer.DoPeriodicTask=function(_,interval,fn) ticks[interval]=fn; return {Cancel=function() end} end
        G.ThePlayer.GetDistanceSqToInst=function() return 1 end
        G.ThePlayer.components={playercontroller={ismastersim=mode=="master",
            locomotor=mode=="predicting" and {} or nil,
            IsBusy=function() return busy end,
            GetActionButtonAction=function(_,target)
                picker_calls=picker_calls+1; assert(target==item)
                if ready then return {action=G.ACTIONS.PICKUP,target=item} end
            end,
            DoAction=function(_,action)
                issued=issued+1
                if mode=="predicting" then assert(action.preview_cb); action.preview_cb()
                elseif mode=="nonpredicting" then assert(action.non_preview_cb); action.non_preview_cb()
                else assert(not action.preview_cb and not action.non_preview_cb) end
            end,
            RemoteActionButton=function(_,action,released)
                assert(action.target==item and action.action==G.ACTIONS.PICKUP and released); rpcs=rpcs+1
            end}}
        G.TheSim.QueryServer=function(_,url,fn) callback=fn end
        start(); ticks[0.2](); local sent=captured()
        cfg.reply={server_id="s",session_id=sent.session_id,sequence=sent.sequence,board_revision=0,
            command={session_id=sent.session_id,operation="start",action_id="a",choice="collect_log",action={guid=22}}}
        callback("{}",true,200)
        assert(issued==0 and picker_calls==0,"busy must defer pickup without an exception")
        busy=false; clock=0.3; ticks[0.2]()
        assert(issued==0 and picker_calls==1,"missing native action is a retry, not success")
        ready=true; clock=0.6; ticks[0.2]()
        assert(issued==1 and rpcs==(mode=="master" and 0 or 1))
        ticks[1](G.ThePlayer)
        assert(captured().bridge.action_report.status=="running","pickup still needs inventory verification")
        handlers.KEY_F4(); clock=1; ticks[0.2](); assert(issued==1,"cancelled pickup must not retry")
    end
end)
test("Lua action availability checks pickability, tools and portable light",function()
    local s=bridge_state()
    s.nearby={{prefab="grass",guid=2,distance=1,pickable=false,collect_path={status="ready"}},
        {prefab="rock1",guid=3,distance=1,work_required=6,tags={"MINE_workable"},collect_path={status="ready"}}}
    assert(not N.available(s).collect_grass and not N.available(s).mine_nearest_rock)
    s.nearby[1].pickable=true
    s.player.inventory.items={{prefab="pickaxe",durability_percent=1}}
    assert(N.available(s).collect_grass and N.available(s).mine_nearest_rock)
    s.world.phase="night"; s.player.inventory.items={{prefab="torch"}}
    assert(N.available(s).equip_torch and not N.available(s).collect_grass)
    s.player.inventory.equipped={{prefab="torch"}}
    assert(N.available(s).collect_grass and not N.available(s).unequip_torch)
end)

test("Lua accepts flee after damage and uses the latest route rather than requested coordinates",function()
    local s=bridge_state(); local executor,calls=native_test()
    s.player.vitals.health=50
    s.nearby={{guid=99,prefab="killerbee",activeThreat=true,distance=2,dx=2,dz=0}}
    s.navigation.escape_routes={{dx=-1,dz=0,distance=6}}
    assert(executor:start({action_id="escape",choice="flee_from_nearest_hostile",
        action={kind="flee_from_nearest_hostile",x=999,z=999}},s,1))
    assert(#calls==1 and calls[1].action.x==-6 and calls[1].action.z==0)
    s.player.vitals.health=20; s.player.position.x=-1; s.world.phase="dusk"
    s.nearby[1].guid=100
    executor:tick(s,2)
    assert(executor.active and executor.report.status=="running","damage must not invalidate an ongoing escape")
end)

test("Lua rejects changed targets without switching to the new nearest plant",function()
    local s=bridge_state(); local executor,calls=native_test()
    s.nearby={{prefab="grass",guid=21,distance=1,pickable=true,collect_path={status="ready"}}}
    assert(not executor:start({action_id="old-target",choice="collect_grass",
        action={kind="collect",guid=20,prefab="grass"}},s,1))
    assert(#calls==0 and executor.report.reason=="target_changed")
    assert(executor.report.choice=="collect_grass" and executor.report.status=="failed")
end)

test("Lua rejects stale pickability, recipes, danger and death before native input",function()
    for _,case in ipairs({"picked","missing","recipe","danger","dead"}) do
        local s=bridge_state(); local executor,calls=native_test()
        s.nearby={{prefab="grass",guid=20,distance=1,pickable=true,collect_path={status="ready"}}}
        local choice,action,reason="collect_grass",{kind="collect",guid=20,prefab="grass"},nil
        if case=="picked" then s.nearby[1].pickable=false; reason="target_not_collectable"
        elseif case=="missing" then s.nearby={}; reason="target_missing"
        elseif case=="recipe" then choice="craft_torch"; action={kind="craft_torch"}; reason="action_unavailable"
        elseif case=="danger" then s.nearby[2]={guid=99,activeThreat=true}; reason="targeted_by_hostile"
        else s.player.vitals.dead=true; reason="player_dead" end
        assert(not executor:start({action_id=case,choice=choice,action=action},s,1))
        assert(#calls==0 and executor.report.reason==reason,case)
    end
end)

test("Lua rejects obsolete flee intent when threats cleared or no safe route remains",function()
    local s=bridge_state(); local executor,calls=native_test()
    assert(not executor:start({action_id="clear",choice="flee_from_nearest_hostile",
        action={kind="flee_from_nearest_hostile"}},s,1))
    assert(executor.report.reason=="threat_cleared" and #calls==0)
    s.nearby={{guid=99,activeThreat=true}}
    assert(not executor:start({action_id="blocked",choice="flee_from_nearest_hostile",
        action={kind="flee_from_nearest_hostile"}},s,2))
    assert(executor.report.reason=="no_safe_escape_route" and #calls==0)
end)

test("Lua remembers completed and rejected command IDs across later reports",function()
    local s=bridge_state(); local executor,calls=native_test()
    assert(executor:start({action_id="a",choice="wait"},s,0))
    executor:tick(s,1)
    assert(not executor:start({action_id="b",choice="craft_torch"},s,2))
    assert(executor:start({action_id="c",choice="wait"},s,3))
    executor:tick(s,4)
    assert(not executor:start({action_id="a",choice="wait"},s,5))
    executor:cancel("cancelled-before-start")
    assert(executor:start({action_id="d",choice="wait"},s,6))
    executor:tick(s,7)
    assert(not executor:start({action_id="cancelled-before-start",choice="wait"},s,8))
    s.crafting.torch=true
    assert(not executor:start({action_id="b",choice="craft_torch"},s,5))
    assert(#calls==0,"replayed rejected command must not craft later")
end)

test("Lua HTTP delivery validates live facts rather than the uploaded observation",function()
    local live=bridge_state(); local executor,calls=native_test()
    live.nearby={{prefab="grass",guid=20,distance=1,pickable=true,collect_path={status="ready"}}}
    local offered=N.available(live).collect_grass
    local callback,sent
    local env={session_id="live",now=function() return 1 end,state=function() return live end,
        alive=function() return true end,available=N.available,
        request=function(_,payload,fn) sent=payload; callback=fn end}
    local bridge=B.new({},env,executor)
    bridge:tick()
    live=bridge_state(); live.nearby={{guid=99,prefab="killerbee",activeThreat=true}}
    live.player.vitals.health=50
    callback({session_id="live",sequence=sent.sequence,server_id="server",board_revision=0,
        command={session_id="live",operation="start",action_id="stale-pick",
            choice="collect_grass",action=offered}},true,200)
    assert(#calls==0 and executor.report.reason=="targeted_by_hostile")
    assert(executor.report.choice=="collect_grass" and not executor.active)
end)
test("Lua collection approaches a verified waypoint and requires actual inventory gain",function()
    local s=bridge_state(); local executor,calls=native_test()
    s.nearby={{prefab="grass",guid=2,distance=5,pickable=true,
        collect_path={status="ready",waypoint={x=4,z=0}}}}
    assert(executor:start({action_id="a",choice="collect_grass",action={guid=2}},s,0))
    assert(calls[1].id=="walk")
    s=bridge_state(); s.player.position.x=4
    s.nearby={{prefab="grass",guid=2,distance=1,pickable=true,collect_path={status="ready"}}}
    executor:tick(s,1); assert(calls[2].id=="collect_grass")
    s.nearby[1].pickable=false; executor:tick(s,2)
    assert(executor.active,"emptying the plant alone is not verified success")
    s.player.inventory.items={{prefab="cutgrass",count=1}}
    executor:tick(s,3); assert(executor.report.status=="completed")
    assert(not executor:start({action_id="a",choice="wait"},s,4),"repeated command must not execute twice")
end)
test("Lua work is interrupted by night without choosing or equipping light",function()
    local s=bridge_state(); local executor,calls,stops=native_test()
    s.player.inventory.items={{prefab="axe",durability_percent=1},{prefab="torch"}}
    s.nearby={{prefab="evergreen",guid=2,distance=1,work_required=15,tags={"CHOP_workable"},collect_path={status="ready"}}}
    executor:start({action_id="a",choice="chop_nearest_tree",action={guid=2}},s,0)
    local night=bridge_state(); night.world.phase="night"; night.player.inventory.items={{prefab="torch"}}
    executor:tick(night,1)
    assert(executor.report.status=="interrupted" and executor.report.reason=="light_changed")
    assert(#calls==1 and stops()==1)
    executor:start({action_id="b",choice="equip_torch"},night,2)
    night.player.inventory.equipped={{prefab="torch"}}
    executor:tick(night,3); assert(executor.report.status=="completed")
end)
test("Lua torch craft cleanup waits through inventory replication gaps",function()
    local s=bridge_state(); local executor,calls=native_test()
    s.crafting.torch=true
    executor:start({action_id="a",choice="craft_torch"},s,0)
    local next=bridge_state()
    next.player.inventory.equipped={{prefab="torch"}}
    executor:tick(next,1); assert(calls[2].id=="unequip_torch")
    next.player.inventory.equipped={}; executor:tick(next,2); assert(executor.active)
    next.player.inventory.items={{prefab="torch"}}; executor:tick(next,3)
    assert(executor.report.status=="completed")
    next.crafting.torch=true; assert(not N.available(next).craft_torch)
end)
test("Lua mining collects different observed drops before reporting completion",function()
    local s=bridge_state(); local executor,calls=native_test()
    s.player.inventory.items={{prefab="pickaxe",durability_percent=1}}
    s.nearby={{prefab="rock1",guid=2,distance=1,work_required=6,tags={"MINE_workable"},collect_path={status="ready"}}}
    executor:start({action_id="a",choice="mine_nearest_rock",action={guid=2}},s,0)
    s.work={sequence=1,target_guid=2,status="completed"}; executor:tick(s,1)
    s.nearby={{prefab="nitre",guid=3,distance=1,tags={"_inventoryitem"},collect_path={status="ready"}},
        {prefab="flint",guid=4,distance=1,tags={"_inventoryitem"},collect_path={status="ready"}}}
    executor:tick(s,2); assert(calls[2].id=="collect_nitre")
    s.player.inventory.items[#s.player.inventory.items+1]={prefab="nitre"}
    executor:tick(s,3); assert(calls[3].id=="collect_flint" and executor.active)
    s.player.inventory.items[#s.player.inventory.items+1]={prefab="flint"}
    executor:tick(s,4); assert(executor.report.status=="completed")
end)

test("Lua chopping retries busy loot dispatch and waits for each inventory effect",function()
    local s=bridge_state(); local calls,ready={},false
    local executor=N.new({stop=function() end,issue=function(id,a)
        calls[#calls+1]={id=id,action=a}
        if a.kind=="collect" then return ready end
    end})
    s.player.inventory.items={{prefab="axe",durability_percent=1}}
    s.nearby={{prefab="evergreen",guid=2,distance=1,work_required=10,
        tags={"CHOP_workable"},collect_path={status="ready"}}}
    assert(executor:start({action_id="chop",choice="chop_nearest_tree",action={guid=2}},s,0))
    s.work={sequence=1,target_guid=2,status="completed"}; executor:tick(s,1)
    s.nearby={{prefab="log",guid=3,distance=1,tags={"_inventoryitem"},collect_path={status="ready"}},
        {prefab="pinecone",guid=4,distance=1,tags={"_inventoryitem"},collect_path={status="ready"}}}
    executor:tick(s,1.2)
    assert(executor.active and executor.active.stage=="collection_retry","busy chop recovery is not failure")
    local n=#calls; executor:tick(s,1.3); assert(#calls==n,"do not spam retries between ticks")
    ready=true; executor:tick(s,1.5)
    assert(calls[#calls].action.guid==3 and executor.active.stage=="loot")
    n=#calls; executor:tick(s,1.8); assert(#calls==n and executor.active,"dispatch alone is not pickup")
    s.player.inventory.items[#s.player.inventory.items+1]={prefab="log"}; ready=false
    executor:tick(s,2)
    assert(executor.active.stage=="collection_retry" and calls[#calls].action.guid==4)
    ready=true; executor:tick(s,2.3)
    assert(executor.active.stage=="loot" and calls[#calls].action.guid==4)
    s.player.inventory.items[#s.player.inventory.items+1]={prefab="pinecone"}
    executor:tick(s,2.5)
    assert(executor.report.status=="completed" and executor.report.reason=="work_and_drops_verified")
end)

test("Lua collection retry is bounded and never retargets or survives cancellation",function()
    for _,mode in ipairs({"timeout","changed","cancel","danger"}) do
        local s=bridge_state(); local calls=0
        local executor=N.new({stop=function() end,issue=function() calls=calls+1; return false end})
        s.nearby={{prefab="log",guid=3,distance=1,tags={"_inventoryitem"},collect_path={status="ready"}}}
        assert(executor:start({action_id="pickup",choice="collect_log",action={guid=3}},s,0))
        assert(executor.active.stage=="collection_retry")
        if mode=="changed" then s.nearby[1].guid=4
        elseif mode=="cancel" then executor:cancel("pickup")
        elseif mode=="danger" then s.nearby[2]={guid=9,activeThreat=true} end
        executor:tick(s,mode=="timeout" and 3 or 0.5)
        assert(not executor.active and calls==1)
        local reason=mode=="timeout" and "collection_action_unavailable"
            or mode=="changed" and "target_changed" or mode=="cancel" and "requested" or "targeted_by_hostile"
        assert(executor.report.reason==reason,executor.report.reason)
    end
end)
test("Lua local board execution is opt-in and never silently explores for missing goals",function()
    local s=bridge_state(); local executor,calls=native_test()
    s.nearby={{prefab="grass",guid=2,distance=1,pickable=true,collect_path={status="ready"}}}
    local board={kind="task_board",auto_execute=false,tasks={{id="grass",priority=1,depends_on={},
        target={type="inventory",prefab="cutgrass",count=2}}}}
    executor:set_board(board,1); executor:board_step(s,0); assert(#calls==0)
    board.auto_execute=true; executor:set_board(board,2); executor:board_step(s,1)
    assert(calls[1].id=="collect_grass")
    executor:set_board(nil,3); assert(executor.report.status=="interrupted")
    s.nearby={}; executor:set_board(board,4); executor:board_step(s,2)
    assert(executor.active==nil and #calls==1)
end)
test("Lua exploration uses type sets, the displacement floor and immediate discovery events",function()
    local s=bridge_state(); local executor=native_test()
    s.navigation={status="ready",leg={x=4,z=0},graph={known_nodes=1,current_node="A"}}
    executor:start({action_id="a",choice="explore_current_region"},s,0)
    local fresh=bridge_state(); fresh.navigation=s.navigation
    fresh.player.position.x=2
    fresh.nearby={{prefab="grass",guid=2,pickable=true,collect_path={status="ready"}}}
    executor:tick(fresh,1); assert(executor.active,"resource event must honor the displacement floor")
    fresh.player.position.x=4; executor:tick(fresh,2)
    assert(executor.report.reason=="resource_types_changed")
    executor:start({action_id="b",choice="explore_current_region"},s,3)
    fresh.player.position.x=1; fresh.navigation={graph={known_nodes=2,current_node="A"}}
    executor:tick(fresh,4); assert(executor.report.reason=="region_discovered")
end)
test("Lua bounded flee survives damage and night changes until observed threat clears",function()
    local s=bridge_state(); local executor,calls=native_test()
    s.nearby={{prefab="hound",guid=8,distance=2,dx=2,dz=0,activeThreat=true}}
    s.navigation={escape_routes={{dx=-1,dz=0,distance=6}}}
    assert(N.available(s).flee_from_nearest_hostile and not N.available(s).wait)
    executor:start({action_id="a",choice="flee_from_nearest_hostile"},s,0)
    assert(calls[1].action.x==-6)
    local next=bridge_state(); next.world.phase="night"; next.player.vitals.health=50
    next.nearby=s.nearby; next.player.position.x=-2
    executor:tick(next,1); assert(executor.active)
    next.nearby={}; executor:tick(next,2)
    assert(executor.report.status=="completed" and executor.report.reason=="no_visible_targeted_hostile")
end)
test("Lua transport discards timed-out callbacks, expires its lease and rejects old worlds",function()
    local clock,callbacks,starts,interrupts=0,{},0,0
    local executor={active=nil,report=nil,board_paused=false,
        tick=function() end,board_step=function() end,set_board=function() end,
        finish=function(self) self.active=nil; interrupts=interrupts+1 end,
        start=function() starts=starts+1 end}
    local env={session_id="one",now=function() return clock end,state=bridge_state,alive=function() return true end,
        available=N.available,request=function(_,payload,fn) callbacks[#callbacks+1]={payload=payload,fn=fn} end}
    local bridge=B.new({url="local",token="test"},env,executor)
    bridge:tick(); assert(#callbacks==1)
    clock=4; executor.active={}; bridge:tick()
    assert(interrupts==1 and #callbacks==2)
    callbacks[1].fn({session_id="one",sequence=1,command={operation="start",session_id="one"}},true,200)
    assert(starts==0 and not bridge.connected)
    callbacks[2].fn({session_id="wrong-world",sequence=2},true,200); assert(not bridge.connected)
    clock=5; bridge:tick()
    callbacks[3].fn({session_id="one",sequence=3,server_id="server",command={operation="start",session_id="one"}},true,200)
    assert(starts==1 and bridge.connected)
    bridge:stop(); callbacks[3].fn({session_id="one",sequence=3},true,200)
    assert(not bridge.connected)
end)
test("Lua reports atomic protocol capabilities, board readiness and retained terminal events",function()
    local s=bridge_state(); local executor=native_test()
    local board={kind="task_board",auto_execute=false,tasks={{id="grass",priority=1,depends_on={},
        target={type="inventory",prefab="cutgrass",count=2}},
        {id="regions",priority=2,depends_on={},target={type="known_regions",count=2}}}}
    s.nearby={{prefab="grass",guid=2,distance=1,pickable=true,collect_path={status="ready"}}}
    executor:set_board(board,3)
    assert(executor:board_choice(s,0)=="collect_grass")
    local env={session_id="test",now=function() return 2 end,available=N.available}
    local bridge=B.new({},env,executor)
    local status=bridge:status(s)
    assert(status.protocol==4 and status.board_revision==3 and status.board_ready and not status.board_auto_execute)
    executor:start({action_id="a",choice="collect_grass",task_id="grass"},s,0,true)
    local fresh=bridge_state(); fresh.player.inventory.items={{prefab="cutgrass"}}
    executor:tick(fresh,1)
    assert(executor.events[1].choice=="collect_grass" and executor.events[1].board_task_id=="grass")
    executor:start({action_id="b",choice="wait"},fresh,1)
    executor:tick(fresh,2)
    assert(#executor.events==2 and executor.events[1].status=="completed")
end)

test("Lua failed target pauses expire and do not suppress a different GUID",function()
    local s=bridge_state(); local executor=native_test()
    s.nearby={{prefab="grass",guid=2,distance=1,pickable=true,collect_path={status="ready"}}}
    executor:start({action_id="a",choice="collect_grass"},s,0)
    executor:tick(s,12)
    assert(executor.report.status=="failed" and not executor:available(s,13).collect_grass)
    s.nearby[1].guid=3; assert(executor:available(s,13).collect_grass)
    s.nearby[1].guid=2; assert(executor:available(s,28).collect_grass)
end)

test("Lua board retains verified Science Machine progress after leaving visibility",function()
    local s=bridge_state(); local executor=native_test()
    s.crafting.sciencemachine=true
    executor:start({action_id="science",choice="build_sciencemachine"},s,0)
    s.nearby={{prefab="sciencemachine",guid=2,distance=1}}
    executor:tick(s,1); assert(executor.report.status=="completed")
    s.nearby={}
    executor:set_board({kind="task_board",tasks={{id="science",priority=1,depends_on={},
        target={type="structure",prefab="sciencemachine"}}}},1)
    assert(not executor:board_choice(s,2),"a built machine leaving sight must not trigger another build")
end)

test("Lua uploads completion and idle metadata atomically before the heartbeat deadline",function()
    local s=bridge_state(); local executor=native_test()
    s.nearby={{prefab="grass",guid=2,distance=1,pickable=true,collect_path={status="ready"}}}
    executor:start({action_id="a",choice="collect_grass"},s,0)
    local clock,requests,bridge=0,{},nil
    local env={session_id="one",now=function() return clock end,alive=function() return true end,
        available=N.available,state=function() s.bridge=bridge:status(s); return s end,
        request=function(_,payload,fn) requests[#requests+1]={payload=payload,fn=fn} end}
    bridge=B.new({},env,executor); bridge.connected=true
    bridge:tick(); assert(#requests==1 and requests[1].payload.state.bridge.executor_busy)
    clock=0.1
    requests[1].fn({session_id="one",sequence=1,server_id="s",board_revision=-1},true,200)
    clock=0.2; s=bridge_state(); s.player.inventory.items={{prefab="cutgrass"}}
    bridge:tick()
    assert(#requests==2,"completion must bypass the 0.5-second idle heartbeat")
    local packet=requests[2].payload
    assert(packet.action_report.status=="completed" and not packet.state.bridge.executor_busy)
    assert(packet.state.bridge.action_report==packet.action_report)
    assert(packet.state.bridge.sampled_at==0.2)
    assert(math.abs(packet.state.bridge.transport.last_round_trip_ms-100)<0.001)
    clock=0.3; bridge:tick(); assert(#requests==2,"urgent changes cannot overlap requests")
end)

test("Lua retains an urgent completion while HTTP is in flight and rejects late replies",function()
    local s=bridge_state(); local executor=native_test()
    s.nearby={{prefab="grass",guid=2,distance=1,pickable=true,collect_path={status="ready"}}}
    executor:start({action_id="a",choice="collect_grass"},s,0)
    local clock,requests=0,{}
    local bridge=B.new({}, {session_id="one",now=function() return clock end,state=function() return s end,
        alive=function() return true end,available=N.available,
        request=function(_,payload,fn) requests[#requests+1]={payload=payload,fn=fn} end},executor)
    bridge:tick()
    clock=0.2; s=bridge_state(); s.player.inventory.items={{prefab="cutgrass"}}; bridge:tick()
    assert(#requests==1 and bridge.dirty)
    clock=0.3; requests[1].fn({session_id="one",sequence=1,server_id="s",board_revision=-1},true,200)
    clock=0.4; bridge:tick()
    assert(#requests==2 and requests[2].payload.action_report.status=="completed")
    clock=4; bridge:tick(); assert(#requests==3)
    requests[2].fn({session_id="one",sequence=2,server_id="s"},true,200)
    assert(not bridge.connected,"timed-out callback must not reconnect the bridge")
end)

test("Lua board starts are uploaded busy rather than as the pre-tick idle state",function()
    local s=bridge_state(); local executor=native_test()
    s.nearby={{prefab="grass",guid=2,distance=1,pickable=true,collect_path={status="ready"}}}
    executor:set_board({kind="task_board",auto_execute=true,tasks={{id="grass",priority=1,depends_on={},
        target={type="inventory",prefab="cutgrass",count=2}}}},1)
    local packet,bridge
    bridge=B.new({}, {session_id="one",now=function() return 0 end,alive=function() return true end,
        available=N.available,state=function() s.bridge=bridge:status(s); return s end,
        request=function(_,payload) packet=payload end},executor)
    bridge.connected=true; bridge:tick()
    assert(packet.state.bridge.executor_busy and packet.action_report.status=="running")
    assert(packet.action_report.action_id==executor.active.id)
end)

test("Lua distinguishes command acceptance, confirmed native start and next-action gap",function()
    local s=bridge_state(); local executor=native_test()
    s.nearby={{prefab="grass",guid=2,distance=1,pickable=true,collect_path={status="ready"}}}
    executor:start({action_id="a",choice="collect_grass"},s,1)
    assert(executor.report.started_at==1 and not executor.report.confirmed_started_at)
    s.execution={action="PICK",target_guid=2,confirmed_start=true,issued_at=0.5,started_at=0.7}
    executor:tick(s,1.2); assert(not executor.report.confirmed_started_at,"old starts cannot acknowledge a new request")
    s.execution={action="PICK",target_guid=2,confirmed_start=true,issued_at=1,started_at=1.3,start_source="replicated_transition"}
    executor:tick(s,1.4); assert(executor.report.confirmed_started_at==1.3)
    s=bridge_state(); s.player.inventory.items={{prefab="cutgrass"}}; executor:tick(s,1.6)
    assert(executor.events[1].confirmed_started_at==1.3 and executor.events[1].finished_at==1.6)
    executor:start({action_id="b",choice="wait"},s,2)
    assert(executor.report.previous_action_id=="a")
    assert(math.abs(executor.report.handoff_gap_ms-400)<0.001)
    executor:tick(s,3.1)
    assert(not executor.report.confirmed_started_at,"timer completion is not native-start evidence")
end)

local function event_peer(s)
    local clock,requests=0,{}
    local executor=native_test()
    local bridge=B.new({}, {session_id="events",now=function() return clock end,
        alive=function() return true end,available=N.available,state=function() return s end,
        request=function(_,payload,fn,receipt) requests[#requests+1]={payload=payload,fn=fn,receipt=receipt} end},executor)
    local function tick(t) clock=t; bridge:tick(); return requests[#requests].payload end
    local function reply(extra,success)
        local request=requests[#requests]
        local result={session_id="events",sequence=request.payload.sequence,server_id="s",board_revision=-1}
        for k,v in pairs(extra or {}) do result[k]=v end
        request.fn(result,success~=false,success==false and 500 or 200)
        request.receipt.done=true
    end
    return bridge,requests,tick,reply
end

test("Lua sends stable moving-world facts once then uses small control heartbeats",function()
    local s=bridge_state()
    s.nearby={{prefab="grass",guid=20,distance=1,pickable=true,collect_path={status="ready"}}}
    local bridge,requests,tick,reply=event_peer(s)
    assert(tick(0).kind=="snapshot"); reply()
    for i=1,8 do
        s.player.position.x=i
        s.nearby[1].guid=20+i
        local packet=tick(i*0.6)
        assert(packet.kind=="heartbeat" and packet.state==nil)
        assert(packet.control and packet.control.available_actions==nil and packet.control.events==nil)
        assert(packet.control.player==nil and packet.control.sampled_at==i*0.6)
        reply()
    end
    assert(#requests==9 and bridge.connected)
end)

test("Lua snapshot requests bypass heartbeat delay and are acknowledged only once",function()
    local _,requests,tick,reply=event_peer(bridge_state())
    tick(0); reply()
    assert(tick(0.6).kind=="heartbeat"); reply({snapshot_request_id=1})
    local packet=tick(0.7)
    assert(#requests==3 and packet.kind=="snapshot" and packet.snapshot_request_id==1)
    reply({snapshot_request_id=1})
    assert(tick(1.3).kind=="heartbeat"); reply({snapshot_request_id=1})
    tick(1.4); assert(#requests==4,"acknowledged request must not keep uploading full state")
end)

test("Lua uploads inventory, danger, light and quarter events without waiting for heartbeat",function()
    for _,case in ipairs({"inventory","threat","health","phase","quarter","resource"}) do
        local s=bridge_state(); local _,requests,tick,reply=event_peer(s)
        tick(0); reply()
        if case=="inventory" then s.player.inventory.items={{prefab="cutgrass",count=2}}
        elseif case=="threat" then s.nearby={{guid=9,prefab="frog",activeThreat=true}}
        elseif case=="health" then s.player.vitals.health=140
        elseif case=="phase" then s.world.phase="night"
        elseif case=="quarter" then s.world.time=0.25
        else s.nearby={{prefab="berrybush",guid=2,distance=1,pickable=true,collect_path={status="ready"}}} end
        assert(tick(0.1).kind=="snapshot" and #requests==2,case)
    end
end)

test("Lua retries unacknowledged snapshots and keeps changes arriving during flight",function()
    local s=bridge_state(); local _,requests,tick,reply=event_peer(s)
    tick(0); reply(nil,false)
    assert(tick(0.1).kind=="snapshot" and #requests==2)
    s.player.vitals.health=100
    tick(0.2); assert(#requests==2)
    reply()
    assert(tick(0.3).kind=="snapshot" and #requests==3)
    reply()
end)

test("Lua server restart on a heartbeat forces full-state resynchronization",function()
    local _,requests,tick,reply=event_peer(bridge_state())
    tick(0); reply()
    assert(tick(0.6).kind=="heartbeat"); reply({server_id="new",need_snapshot=true})
    assert(tick(0.7).kind=="snapshot" and #requests==3)
    reply({server_id="new"})
    assert(tick(1.3).kind=="heartbeat")
end)

test("Lua real-time measurements aggregate tick work without altering request cadence",function()
    local clock,wall,requests=0,0,{}
    local s=bridge_state(); local executor=native_test()
    local bridge=B.new({}, {session_id="one",now=function() return clock end,wall_now=function() return wall end,
        alive=function() return true end,available=N.available,
        state=function(fields) wall=wall+0.003; fields.navigation_ms=2; return s end,
        request=function(_,payload,fn,receipt) requests[#requests+1]={payload=payload,fn=fn,receipt=receipt} end},executor)
    bridge:tick(); assert(#requests==1)
    local m=requests[1].payload.measurement
    assert(m.clock=="lua_real_seconds" and m.sampling.ticks==1)
    assert(math.abs(m.sampling.sum_ms.state_total_ms-3)<0.001 and m.sampling.sum_ms.navigation_ms==2)
    requests[1].receipt.done=true; requests[1].receipt.encode_ms=1
    requests[1].fn({session_id="one",sequence=1,server_id="s",board_revision=-1},true,200)
    clock=0.2; bridge:tick(); assert(#requests==1)
    clock=0.4; bridge:tick(); assert(#requests==1)
    clock=0.6; bridge:tick(); assert(#requests==2)
    m=requests[2].payload.measurement
    assert(m.sampling.ticks==3 and #m.previous_exchanges==1)
    assert(m.previous_exchanges[1].sequence==1 and m.previous_exchanges[1].encode_ms==1)
end)

test("Lua missing real-time clocks stay unknown rather than measuring frozen simulation ticks",function()
    local packet
    local executor=native_test()
    local bridge=B.new({}, {session_id="one",now=function() return 0 end,state=bridge_state,
        alive=function() return true end,available=N.available,
        request=function(_,payload) packet=payload end},executor)
    bridge:tick()
    assert(packet.measurement.clock=="unavailable" and packet.measurement.sampling.ticks==1)
    assert(packet.measurement.sampling.sum_ms.state_total_ms==nil)
end)

test("real Mod measures JSON and QueryServer callback work without game inputs",function()
    local wall=0
    local cfg={enabled=true,url="http://127.0.0.1:8765/v1/exchange",token=string.rep("x",32),
        measurement_clock=function() return wall end}
    local _,world,G,_,start,captured=simulated_mod(function() return true end,function() return "A" end,
        function() return 2 end,{},cfg)
    local tick,callback
    world.IsValid=function() return true end
    world.DoTaskInTime=function(_,delay,fn) fn() end
    G.ThePlayer.ListenForEvent=function() end
    G.ThePlayer.DoPeriodicTask=function(_,interval,fn)
        if interval==0.2 then tick=fn end; return {Cancel=function() end}
    end
    G.TheSim.FindEntities=function() wall=wall+0.004; return {} end
    G.TheSim.QueryServer=function(_,url,fn) callback=fn end
    start(); tick(); local sent=captured(); assert(sent.sequence==1 and callback)
    cfg.reply={session_id=sent.session_id,sequence=1,server_id="s",board_revision=0}
    wall=wall+0.1; callback("{}",true,200)
    tick(); sent=captured()
    assert(sent.sequence==2)
    local r=sent.measurement.previous_exchanges[1]
    assert(r.sequence==1 and r.accepted and r.response_bytes==2)
    assert(math.abs(r.query_to_callback_ms-100)<0.001 and r.encode_ms==0)
    assert(r.decode_ms==0 and r.callback_dispatch_ms==0)
    assert(sent.measurement.sampling.sum_ms.entities_ms>=3.99)
end)

local R=require("jev_resource_memory")
test("resource memory stores only observed coordinates and invalidates picked plants",function()
    local m={}; local p={x=10,z=20}
    R.observe(m,p,{{guid=1,prefab="grass",pickable=true,dx=5,dz=0,tags={"pickable"}}},function() return "A" end,1,15,true)
    assert(m.sources[1].x==15 and m.sources[1].node_id=="A")
    R.observe(m,p,{{guid=1,prefab="grass",pickable=false,dx=5,dz=0,tags={"pickable"}}},function() return "A" end,2,15,true)
    assert(not m.sources[1].available)
    assert(#R.candidates(m,{x=40,z=20},"A",{})==0)
end)
test("resource disappearance needs an uncapped nearby scan and regeneration needs live evidence",function()
    local m={}; local e={guid=1,prefab="grass",pickable=true,dx=1,dz=0,tags={"pickable"}}
    local region=function() return "A" end
    R.observe(m,{x=0,z=0},{e},region,1,15,true)
    R.observe(m,{x=0,z=0},{},region,2,15,false)
    assert(m.sources[1])
    R.observe(m,{x=0,z=0},{},region,3,15,true)
    assert(not m.sources[1])
    e.pickable=false; R.observe(m,{x=0,z=0},{e},region,4,15,true)
    assert(not m.sources[1].available)
    e.pickable=true; R.observe(m,{x=0,z=0},{e},region,5,15,true)
    assert(m.sources[1].available)
end)
test("remembered source candidates are current-room scoped and include distant visible work",function()
    local m={}; local region=function(x) return x<20 and "A" or "B" end
    local seen=R.observe(m,{x=0,z=0},{
        {guid=1,prefab="evergreen",dx=10,dz=0,work_required=15,tags={"CHOP_workable"}},
        {guid=2,prefab="rock2",dx=30,dz=0,work_required=6,tags={"MINE_workable"}}},region,1,15,true)
    local found=R.candidates(m,{x=0,z=0},"A",seen)
    assert(#found==1 and found[1].guid==1 and found[1].distance==10)
end)
test("remembered source diversity and memory size stay bounded",function()
    local m={}; local entities={}
    for i=1,520 do entities[i]={guid=i,prefab="log",dx=i,dz=0,tags={"_inventoryitem"}} end
    entities[520].prefab="goldnugget"
    R.observe(m,{x=0,z=0},entities,function() return "A" end,1,15,false)
    local n=0; for _ in pairs(m.sources) do n=n+1 end; assert(n==512)
    -- Give gold a fresh observation so it remains among bounded memory entries.
    R.observe(m,{x=0,z=0},{entities[520]},function() return "A" end,2,15,false)
    local candidates=R.candidates(m,{x=0,z=0},"A",{})
    assert(#candidates==2 and candidates[2].prefab=="goldnugget")
end)
test("native navigation offers only ready remembered resource paths and keeps identity",function()
    local N=require("jev_native_executor")
    local s={player={inventory={items={},equipped={}},vitals={hunger=100,hunger_max=150}},world={phase="day"},nearby={},
        navigation={resource_routes={{status="ready",guid=12,prefab="grass",distance=20,node_id="A",leg={x=4,z=0}},
                                    {status="blocked",guid=13,prefab="rock2",node_id="A",leg={x=4,z=0}}}}}
    local a=N.available(s)
    assert(a.navigate_resource_12.mode=="resource" and a.navigate_resource_12.resource_guid==12)
    assert(a.navigate_resource_12.prefab=="grass" and not a.navigate_resource_13)
    s.world.phase="night"
    assert(not N.available(s).navigate_resource_12,"memory never bypasses light gating")
end)
print("PASS: " .. passed .. " Lua exploration/bridge checks")
