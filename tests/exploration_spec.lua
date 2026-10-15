-- Run from the project root: fengari tests/exploration_spec.lua
package.path = "./scripts/?.lua;" .. package.path
local E = require("jev_exploration")
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

local function simulated_mod(passable, topology, terrain)
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
        TheSim={FindEntities=function() return {} end},
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
    return sample, G.TheWorld
end

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

print("PASS: " .. passed .. " Lua exploration checks")
