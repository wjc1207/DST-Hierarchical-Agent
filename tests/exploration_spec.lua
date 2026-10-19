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

local function simulated_mod(passable, topology, terrain, entities)
    local handlers, captured, errors = {}, nil, {}
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
        TheInput={AddKeyUpHandler=function(_,name,fn) handlers[name]=fn end},
        GetTime=function() return now end,
        ConvertTopologyIdToData=function(id) return {task_id=id} end,
    },{__index=function(_,k)
        if string.sub(k,1,4) == "KEY_" then return k end
        return _G[k]
    end})
    local env=setmetatable({GLOBAL=G,GetModConfigData=function() return nil end,
        AddPlayerPostInit=function() end,AddSimPostInit=function() end,
        require=function(name)
            if name == "json" then return {encode=function(value) captured=value; return "{}" end} end
            return require(name)
        end,
        print=function(value) table.insert(errors,value) end,
    },{__index=_G})
    assert(loadfile("modmain.lua","t",env))()
    local function sample(ax,az)
        x,z,now=ax,az,now+1
        captured=nil
        handlers.KEY_F2()
        assert(captured,table.concat(errors,"\n"))
        return captured
    end
    return sample, G.TheWorld, G, handlers
end

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

print("PASS: " .. passed .. " Lua exploration checks")
