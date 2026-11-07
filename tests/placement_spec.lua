package.path="./mods/jev_dst_agent/scripts/?.lua;"..package.path
local P=require("jev_placement")
local N=require("jev_native_executor")
local R=require("jev_recipes")
local passed=0
local function test(name,fn)
    local ok,err=pcall(fn);assert(ok,name..": "..tostring(err))
    passed=passed+1;print("ok "..passed.." - "..name)
end
local function state(x)
    return {world={phase="day"},player={guid=1,position={x=x or 2,z=0},
        vitals={health=150,hunger=100,hunger_max=150},inventory={items={},equipped={}}},nearby={},
        places={buildings={{id="building:40",guid=40,prefab="researchlab",status="observed",x=0,z=0}}},
        recipes={firepit={recipe_id="firepit",product="firepit",placement=true,ready=true,
            output_count=1,site={x=32,z=0}}},recipe_entities={}}
end
local function area() return {anchor_id="building:40",radius=10,x=0,z=0} end
local function action(s)
    local a=N.available(s).recipe_firepit;a.placement=area();return a
end
local function executor(point)
    return N.new({stop=function() end,issue=function() end,
        placement_site=function() return point or {x=4,z=0} end})
end
test("native build rejects player outside frozen anchor area",function()
    local s=state(30);local x=executor()
    assert(not x:start({action_id="far",choice="recipe_firepit",action=action(s)},s,0))
    assert(x.report.reason=="placement_anchor_changed_or_outside")
end)
test("scoped build selects a fresh in-range site instead of the unbound offered point",function()
    local s=state();local x=executor()
    assert(x:start({action_id="near",choice="recipe_firepit",action=action(s)},s,0))
    assert(x.active.action.x==4 and x.active.action.placement.anchor_id=="building:40")
end)
test("no legal in-range site rejects before issuing a build",function()
    local s=state();local x=executor({x=12,z=0})
    assert(not x:start({action_id="badsite",choice="recipe_firepit",action=action(s)},s,0))
    assert(x.report.reason=="no_valid_site_in_placement_area")
end)
test("moved or missing anchor invalidates command instead of retargeting",function()
    for _,missing in ipairs({false,true}) do
        local s=state();local a=action(s)
        if missing then s.places.buildings[1].status="missing" else s.places.buildings[1].x=5 end
        local x=executor()
        assert(not x:start({action_id="stale",choice="recipe_firepit",action=a},s,0))
        assert(x.report.reason=="placement_anchor_changed_or_outside")
    end
end)
test("a nearby new structure outside scope does not prove scoped completion",function()
    local s=state();local x=executor({x=9,z=0})
    assert(x:start({action_id="verify",choice="recipe_firepit",action=action(s)},s,0))
    s.recipe_entities={{guid=50,prefab="firepit",x=10.5,z=0}}
    x:tick(s,.2);assert(x.active)
    s.recipe_entities={{guid=51,prefab="firepit",x=8,z=0}}
    x:tick(s,.4);assert(x.report.status=="completed")
end)
test("leaving scope or losing anchor interrupts active building",function()
    local s=state();local x=executor()
    assert(x:start({action_id="leave",choice="recipe_firepit",action=action(s)},s,0))
    s.player.position.x=11;x:tick(s,.2)
    assert(x.report.status=="interrupted" and x.report.reason=="placement_anchor_changed_or_outside")
end)
test("live area validates actual entity and both player and placement coordinates",function()
    local anchor={IsValid=function() return true end,Transform={GetWorldPosition=function() return 0,0,0 end}}
    local G={Ents={[40]=anchor}}
    local player={IsValid=function() return true end,Transform={GetWorldPosition=function() return 2,0,0 end}}
    assert(P.live(G,player,area(),{x=4,z=0}))
    assert(not P.live(G,player,area(),{x=11,z=0}))
    anchor.Transform.GetWorldPosition=function() return 1,0,0 end
    assert(not P.live(G,player,area(),{x=4,z=0}))
end)
test("delayed recipe placement rechecks live anchor before spending buffered build",function()
    local buffered=false
    local builder={IsBusy=function() return false end,KnowsRecipe=function() return true end,
        IsBuildBuffered=function() return buffered end,HasIngredients=function() return true end,
        CanBuildAtPoint=function() return true end,BufferBuild=function() buffered=true end,
        MakeRecipeAtPoint=function(self) self.placed=true end}
    local pc={StartBuildPlacementMode=function() end,CancelPlacement=function() end}
    local recipe={name="firepit",product="firepit",placer="firepit_placer"}
    local anchor={IsValid=function() return true end,Transform={GetWorldPosition=function() return 0,0,0 end}}
    local G={Ents={[40]=anchor},GetValidRecipe=function() return recipe end,
        Vector3=function(x,y,z) return {x=x,y=y,z=z} end}
    local player={IsValid=function() return true end,Transform={GetWorldPosition=function() return 2,0,0 end},
        replica={builder=builder},components={playercontroller=pc},DoTaskInTime=function(self,_,fn) self.pending=fn end}
    local a={kind="place",recipe_id="firepit",product="firepit",x=4,z=0,rotation=0,placement=area()}
    local reason
    assert(R.issue(G,player,a,function(r) reason=r end))
    G.Ents[40]=nil;player.pending()
    assert(not builder.placed and reason=="placement_anchor_changed_or_outside")
end)
test("live placement anchor follows stable site binding, not the ID suffix",function()
 local P=require("jev_places");local m=P.memory({},1)
 P.observe(m,{x=0,z=0},{{guid=40,prefab="researchlab",x=0,z=0}},function() return "A" end,1,20,true)
 P.observe(m,{x=50,z=0},{},function() return "A" end,2,20,true)
 P.observe(m,{x=0,z=0},{{guid=80,prefab="researchlab",x=0,z=0}},function() return "A" end,3,20,true)
 local entity={IsValid=function() return true end,Transform={GetWorldPosition=function() return 0,0,0 end}}
 local G={TheWorld={_jev_places=m},Ents={[80]=entity}}
 local player={GUID=1,IsValid=function() return true end,Transform={GetWorldPosition=function() return 1,0,0 end}}
 local area={anchor_id="building:40",x=0,z=0,radius=8}
 assert(require("jev_placement").live(G,player,area,{x=3,z=0}))
 G.Ents={};assert(not require("jev_placement").live(G,player,area,{x=3,z=0}))
end)

print("placement tests passed: "..passed)
