-- Offline recipe/native-executor regression: lua tests/recipes_spec.lua
package.path="./mods/jev_dst_agent/scripts/?.lua;"..package.path
local R=require("jev_recipes")
local N=require("jev_native_executor")
local passed=0
local function test(name,fn)
    local ok,err=pcall(fn)
    assert(ok,name..": "..tostring(err))
    passed=passed+1; print("ok "..passed.." - "..name)
end
local function fixture()
    local buffered=false
    local builder={IngredientMod=function() return 1 end,KnowsRecipe=function() return true end,
        HasIngredients=function() return true end,CanLearn=function() return true end,
        GetTechTrees=function() return {} end,IsBusy=function() return false end,
        IsBuildBuffered=function() return buffered end,
        BufferBuild=function() buffered=true end,CanBuildAtPoint=function() return true end,
        MakeRecipeFromMenu=function(self) self.crafted=true end,
        MakeRecipeAtPoint=function(self) self.placed=true end}
    local inventory={GetItems=function() return {} end,GetEquips=function() return {} end,
        GetOverflowContainer=function() end}
    local controller={StartBuildPlacementMode=function() end,CancelPlacement=function() end}
    local player={replica={builder=builder,inventory=inventory},components={playercontroller=controller},
        IsValid=function() return true end,DoTaskInTime=function(self,_,fn) self.pending=fn end}
    local recipes={boards={name="boards",product="boards",numtogive=1,ingredients={{type="log",amount=4}},level={}},
        researchlab2={name="researchlab2",product="researchlab2",placer="researchlab2_placer",ingredients={},level={}},
        odd={name="odd",product="odd",ingredients={},character_ingredients={{type="health",amount=10}},level={}}}
    local G={AllRecipes=recipes,GetValidRecipe=function(id) return recipes[id] end,
        STRINGS={NAMES={}},RoundBiasedUp=function(n) return math.floor(n+0.5) end,
        Vector3=function(x,y,z) return {x=x,y=y,z=z} end,
        CanPrototypeRecipe=function() return true end}
    return G,player,builder
end
local function state(catalog)
    return {world={phase="day"},player={guid=1,position={x=0,z=0},vitals={health=150,hunger=100,hunger_max=150},
        inventory={items={},equipped={}}},nearby={},recipes=catalog or {},recipe_entities={}}
end
local function site() return {x=2,z=0} end
test("queries include missing ingredients and reject special costs",function()
    local G,p,b=fixture()
    b.HasIngredients=function() return false end
    local rows=R.scan(G,p,site)
    assert(rows.boards.ingredients[1].missing==4 and not rows.boards.ready)
    assert(not rows.odd.supported and rows.odd.reason=="special_ingredients_not_supported")
end)
test("prototype technology is usable without falsely marking recipe known",function()
    local G,p,b=fixture(); b.KnowsRecipe=function() return false end
    local row=R.scan(G,p,site).boards
    assert(not row.known and row.can_prototype and row.ready)
    b.CanLearn=function() return false end
    assert(not R.scan(G,p,site).boards.ready)
end)
test("placement requires a legal site, buffered structures need no new ingredients",function()
    local G,p,b=fixture()
    assert(not R.scan(G,p,function() end).researchlab2.ready)
    b.IsBuildBuffered=function() return true end
    b.HasIngredients=function() return false end
    assert(R.scan(G,p,site).researchlab2.ready)
end)
test("generic actions carry recipe identity and planner-only marker",function()
    local G,p=fixture(); local actions=N.available(state(R.scan(G,p,site)))
    assert(actions.recipe_boards.kind=="craft" and actions.recipe_boards.planner_only)
    assert(actions.recipe_researchlab2.kind=="place" and actions.recipe_researchlab2.x==2)
    assert(not actions.recipe_odd)
end)
test("craft requires observed complete output, not request or material consumption",function()
    local G,p=fixture(); local s=state(R.scan(G,p,site)); local issued=0
    local x=N.new({stop=function() end,issue=function() issued=issued+1 end})
    assert(x:start({action_id="c",choice="recipe_boards",action=N.available(s).recipe_boards},s,0))
    x:tick(s,0.2); assert(x.active and issued==1)
    local after=state(s.recipes); after.recipe_inventory={boards=1}
    x:tick(after,0.4); assert(x.report.status=="completed")
end)
test("placement verifies a new matching entity at the requested site",function()
    local G,p=fixture(); local s=state(R.scan(G,p,site))
    s.recipe_entities={{guid=1,prefab="researchlab2",x=2,z=0}}
    local x=N.new({stop=function() end,issue=function() end})
    assert(x:start({action_id="p",choice="recipe_researchlab2",action=N.available(s).recipe_researchlab2},s,0))
    x:tick(s,0.2); assert(x.active)
    s.recipe_entities={{guid=2,prefab="researchlab2",x=10,z=0}}
    x:tick(s,0.4); assert(x.active)
    s.recipe_entities={{guid=2,prefab="researchlab2",x=2,z=0}}
    x:tick(s,0.6); assert(x.report.status=="completed")
end)
test("stale placement cannot silently move to a different site",function()
    local G,p=fixture(); local s=state(R.scan(G,p,site))
    local old=N.available(s).recipe_researchlab2
    s.recipes.researchlab2.site={x=3,z=0}
    local x=N.new({stop=function() end,issue=function() error("must not execute") end})
    assert(not x:start({action_id="old",choice="recipe_researchlab2",action=old},s,0))
    assert(x.report.reason=="target_changed")
end)
test("delayed placement rechecks legality and honors generation cancellation",function()
    local G,p,b=fixture(); local action=R.actions(R.scan(G,p,site)).recipe_researchlab2
    local reason
    assert(R.issue(G,p,action,function(r) reason=r end))
    b.CanBuildAtPoint=function() return false end
    p.pending(); assert(reason=="placement_changed" and not b.placed)
    G,p,b=fixture(); p._jev_bridge_generation=1
    assert(R.issue(G,p,action,function() error("cancel should be silent") end))
    p._jev_bridge_generation=2; p.pending(); assert(not b.placed)
end)

test("cached placement keeps its offered point across movement and facts refresh",function()
    local G,p=fixture();local px=0
    local function select_site(_,previous)
        return previous or {x=px+2,z=0}
    end
    local rows=R.cached_scan(G,p,select_site,0)
    assert(rows.researchlab2.site.x==2)
    px=.1
    rows=R.cached_scan(G,p,select_site,.2)
    assert(rows.researchlab2.site.x==2)
    rows=R.cached_scan(G,p,select_site,3)
    assert(rows.researchlab2.site.x==2,"a periodic engine recheck must preserve the offered site")
end)

test("placement ignores numeric serialization noise but not a changed site",function()
    local G,p=fixture(); local s=state(R.scan(G,p,site))
    local old=N.available(s).recipe_researchlab2
    old.x=old.x+0.00000001
    local issued=0;local x=N.new({stop=function() end,issue=function() issued=issued+1 end})
    assert(x:start({action_id="precision",choice="recipe_researchlab2",action=old},s,0))
    assert(issued==1)
end)
test("native crafting repeats feasibility checks",function()
    local G,p,b=fixture(); local action=R.actions(R.scan(G,p,site)).recipe_boards
    b.HasIngredients=function() return false end
    local reason
    assert(not R.issue(G,p,action,function(r) reason=r end))
    assert(reason=="recipe_no_longer_craftable" and not b.crafted)
end)
print("Passed "..passed.." recipe tests")
