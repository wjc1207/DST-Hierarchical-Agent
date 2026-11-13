package.path="./mods/dst_player_agent/scripts/?.lua;"..package.path
local F=require("jev_performance")
local R=require("jev_recipes")
local passed=0
local function test(name,fn)
    local ok,err=pcall(fn); assert(ok,name..": "..tostring(err))
    passed=passed+1; print("ok "..passed.." - "..name)
end
test("FPS counts frontend wall frames rather than simulation ticks",function()
    local now=0; local monitor=F.new(function() return now end,5)
    monitor:frame(0)
    for i=1,101 do now=i*.05; monitor:frame(1/30) end
    local result=monitor:read()
    assert(result.available and math.abs(result.fps-20)<.001)
    assert(math.abs(result.frame_ms-50)<.001 and result.source=="frontend_wall_update")
end)
test("no clock falls back to frontend dt and warmup stays unknown",function()
    local monitor=F.new(nil,1)
    assert(not monitor:read().available and not monitor:read().fps)
    for i=1,21 do monitor:frame(.05) end
    assert(math.abs(monitor:read().fps-20)<.001)
end)
test("long frame stalls remain in the window and storage is bounded",function()
    local monitor=F.new(nil,2)
    for i=1,1000 do monitor:frame(.001) end
    assert(#monitor.samples<=600)
    monitor:frame(1.1)
    assert(monitor:read().max_frame_ms==1100)
    assert(#monitor.samples==0)
end)
local function fixture()
    local items={}; local tech=0; local calls=0; local known=true; local busy=false
    local builder={IngredientMod=function() return 1 end,IsBusy=function() return busy end,
        KnowsRecipe=function() calls=calls+1; return known end,
        CanLearn=function() return true end,GetTechTrees=function() return {SCIENCE=tech} end,
        HasIngredients=function() return true end,IsBuildBuffered=function() return false end}
    local inv={GetItems=function() return items end,GetEquips=function() return {} end,GetOverflowContainer=function() end}
    local player={replica={builder=builder,inventory=inv}}
    local recipe={name="boards",product="boards",ingredients={{type="log",amount=4}},level={SCIENCE=0}}
    local G={AllRecipes={boards=recipe},GetValidRecipe=function() return recipe end,STRINGS={NAMES={}},
        RoundBiasedUp=function(x) return math.floor(x+.5) end,CanPrototypeRecipe=function() return false end}
    return G,player,function() return calls end,function() items[1]={prefab="log"} end,
        function() tech=1 end,function() known=false end,function(value) busy=value end
end
test("idle samples reuse recipe facts, refreshes with equal facts keep the wire version",function()
    local G,p,calls=fixture()
    local rows,version=R.cached_scan(G,p,function() end,0)
    for i=1,9 do local _,v=R.cached_scan(G,p,function() end,i*.2); assert(v==version) end
    assert(calls()==1)
    local _,v=R.cached_scan(G,p,function() end,2.1)
    assert(calls()==2 and v==version)
    assert(rows.boards.technology.SCIENCE==nil)
end)
test("inventory and technology invalidate immediately and unlock changes refresh within two seconds",function()
    local G,p,calls,inventory,technology,unlock=fixture()
    R.cached_scan(G,p,function() end,0)
    inventory(); R.cached_scan(G,p,function() end,.2); assert(calls()==2)
    technology(); R.cached_scan(G,p,function() end,.4); assert(calls()==3)
    unlock(); local rows=R.cached_scan(G,p,function() end,2.5)
    assert(not rows.boards.known and not rows.boards.ready)
end)
test("busy changes update offered readiness without another catalogue scan",function()
    local G,p,calls,_,_,_,busy=fixture()
    local rows,version=R.cached_scan(G,p,function() end,0)
    busy(true)
    local updated,newversion=R.cached_scan(G,p,function() end,.2)
    assert(not updated.boards.ready and newversion>version and calls()==1)
    busy(false); updated=R.cached_scan(G,p,function() end,.4)
    assert(updated.boards.ready and calls()==1)
end)
test("wire metadata excludes changing site coordinates but preserves live action sites",function()
    local G,p=fixture(); local rows,version=R.cached_scan(G,p,function() end,0)
    rows.boards.site={x=2,z=0}
    local wire=R.wire_catalog(p,rows,version)
    assert(not wire.boards.site and rows.boards.site.x==2)
    assert(R.wire_catalog(p,rows,version)==wire)
end)
test("974-recipe idle workload avoids nine repeated engine-wide scans",function()
    local G,p,baseline=fixture()
    for i=1,973 do G.AllRecipes["recipe_"..i]=G.AllRecipes.boards end
    for i=0,9 do R.scan(G,p,function() end) end
    assert(baseline()==9740)
    local G2,p2,cached=fixture()
    for i=1,973 do G2.AllRecipes["recipe_"..i]=G2.AllRecipes.boards end
    for i=0,9 do R.cached_scan(G2,p2,function() end,i*.2) end
    assert(cached()<=320)
    print("BENCH recipe_checks baseline="..baseline().." cached="..cached().." ticks=10 recipes=974")
    for i=10,39 do R.cached_scan(G2,p2,function() end,i*.2) end
    local loaded=0;for _ in pairs(p2._jev_recipe_cache.catalog) do loaded=loaded+1 end
    assert(loaded==974,"incremental indexing must eventually visit the entire catalogue")
end)
test("one inventory change does not rebuild every immutable wire row",function()
    local G,p,_,inventory=fixture()
    for i=1,973 do G.AllRecipes["recipe_"..i]=G.AllRecipes.boards end
    local rows,version
    for i=0,39 do rows,version=R.cached_scan(G,p,function() end,i*.2) end
    local before=R.wire_catalog(p,rows,version)
    inventory()
    rows,version=R.cached_scan(G,p,function() end,8.1)
    local after=R.wire_catalog(p,rows,version)
    local changed=0
    for id,row in pairs(after) do if before[id]~=row then changed=changed+1 end end
    assert(changed<=32 and changed>0)
end)

test("wall-time budget yields while background indexing still progresses",function()
    local G,p=fixture(); local clock=0
    for i=1,100 do G.AllRecipes["recipe_"..i]=G.AllRecipes.boards end
    G.walltime=function() clock=clock+.004; return clock end
    for i=0,249 do
        R.cached_scan(G,p,function() end,i*.2)
        assert(p._jev_recipe_cache.processed<=1)
    end
    local loaded=0;for _ in pairs(p._jev_recipe_cache.catalog) do loaded=loaded+1 end
    assert(loaded==101,"slow calls must not starve the background index")
end)
test("continuous inventory invalidation does not starve late recipe IDs",function()
    local G,p=fixture()
    for i=1,973 do G.AllRecipes["recipe_"..i]=G.AllRecipes.boards end
    for tick=1,60 do
        p.replica.inventory.GetItems=function() return {{prefab="log",replica={stackable={StackSize=function() return tick end}}}} end
        R.cached_scan(G,p,function() end,tick*.2)
    end
    local loaded=0;for _ in pairs(p._jev_recipe_cache.catalog) do loaded=loaded+1 end
    assert(loaded==974)
end)
print("Passed "..passed.." performance tests")
