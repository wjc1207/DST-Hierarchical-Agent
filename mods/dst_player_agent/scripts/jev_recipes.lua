-- Recipe facts and ordinary native crafting. No model policy or key input.
local M = {}
local placement=require("jev_placement")
local legacy={torch=true,axe=true,pickaxe=true,campfire=true,researchlab=true}
local function call(fn, fallback)
    local ok,value=pcall(fn); if ok and value~=nil then return value end
    return fallback
end
local function accessible(G,builder,recipe)
    return builder:KnowsRecipe(recipe)==true or
        (builder:CanLearn(recipe.name)==true and G.CanPrototypeRecipe(recipe.level,builder:GetTechTrees())==true)
end
local function counts(player)
    local result={}
    local inv=player.replica and player.replica.inventory
    if inv then
        local function add(items)
            for _,item in pairs(items or {}) do
                local stack=item.replica and item.replica.stackable
                result[item.prefab]=(result[item.prefab] or 0)+(stack and stack:StackSize() or 1)
            end
        end
        add(inv:GetItems()); add(inv:GetEquips())
        local overflow=inv:GetOverflowContainer()
        if overflow then add(overflow:GetItems()) end
    end
    return result
end

M.inventory = counts

function M.scan(G,player,site,selection,facts)
    local catalog={}
    local builder=player.replica and player.replica.builder
    if not builder then return catalog end
    local carried=facts and facts.carried or counts(player)
    local modifier=facts and facts.modifier or call(function() return builder:IngredientMod() end,1)
    local busy
    if facts then busy=facts.busy else busy=builder:IsBusy() end
    for id in pairs(selection or G.AllRecipes or {}) do
        local recipe=G.GetValidRecipe(id)
        if recipe then
            local ok,entry=pcall(function()
                local product=recipe.product
                local output=recipe.numtogive or 1
                local supported=type(product)=="string" and type(output)=="number"
                    and output>0 and output<10000 and output%1==0
                local reason=not supported and "dynamic_product_or_output" or nil
                if #(recipe.character_ingredients or {})>0 or #(recipe.tech_ingredients or {})>0 then
                    supported=false; reason="special_ingredients_not_supported"
                end
                if recipe.placer and recipe.build_mode~=nil and G.BUILDMODE
                    and recipe.build_mode~=G.BUILDMODE.LAND then
                    supported=false; reason="special_placement_not_supported"
                end
                local row={recipe_id=id,product=type(product)=="string" and product or id,
                    name=(G.STRINGS.NAMES or {})[string.upper(type(product)=="string" and product or id)] or id,
                    output_count=type(recipe.numtogive)=="number" and recipe.numtogive or 1,
                    placement=recipe.placer~=nil,ingredients={},technology={},
                    builder_tag=recipe.builder_tag,builder_skill=recipe.builder_skill,
                    supported=supported,legacy=legacy[id]==true,
                    target_prefab=id=="researchlab" and "sciencemachine" or product}
                for tech,level in pairs(recipe.level or {}) do if level~=0 then row.technology[tech]=level end end
                for _,ingredient in ipairs(recipe.ingredients or {}) do
                    local amount=math.max(1,G.RoundBiasedUp(ingredient.amount*modifier))
                    row.ingredients[#row.ingredients+1]={prefab=ingredient.type,count=amount,
                        carried=carried[ingredient.type] or 0,
                        missing=math.max(0,amount-(carried[ingredient.type] or 0))}
                end
                row.known=builder:KnowsRecipe(recipe,nil,facts and facts.known_tech_cache)==true
                row.can_prototype=not row.known and builder:CanLearn(recipe.name)==true
                    and G.CanPrototypeRecipe(recipe.level,facts and facts.tech or builder:GetTechTrees())==true
                row.has_ingredients=builder:HasIngredients(recipe)==true
                row.buffered=builder:IsBuildBuffered(id)==true
                row.ready=supported and not busy and
                    (row.buffered or ((row.known or row.can_prototype) and row.has_ingredients))
                row.reason=reason or (not row.known and not row.can_prototype and not row.buffered and "technology_or_unlock_required")
                    or (not row.has_ingredients and not row.buffered and "ingredients_missing")
                    or (busy and "builder_busy") or "ready"
                if row.ready and row.placement and not row.legacy then
                    local previous=facts and facts.catalog and facts.catalog[id]
                    row.site=site(recipe,previous and previous.site)
                    if not row.site then row.ready=false; row.reason="no_safe_placement_site" end
                end
                return row
            end)
            if ok then catalog[id]=entry else
                catalog[id]={recipe_id=id,supported=false,ready=false,reason="recipe_metadata_unavailable"}
            end
        end
    end
    return catalog
end

-- Cache expensive engine-wide recipe facts; live execution always rechecks.
-- Inventory/technology changes invalidate immediately, skills/unlocks get a
-- bounded two-second fallback refresh. Busy/site checks remain live each tick.
local function same_facts(a,b)
    if type(a)~=type(b) then return false end
    if type(a)~="table" then return a==b end
    for key,value in pairs(a) do if key~="site" and not same_facts(value,b[key]) then return false end end
    for key in pairs(b) do if key~="site" and a[key]==nil then return false end end
    return true
end
-- Bound engine work per tick. A catalogue is progressively indexed instead
-- of querying every recipe synchronously. Old offers are withdrawn on changed
-- inventory/technology; native execution independently revalidates every action.
local PRIORITY={"torch","axe","pickaxe","campfire","researchlab","boards","rope","cutstone","transistor","researchlab2"}
function M.cached_scan(G,player,site,now)
    local builder=player.replica and player.replica.builder
    if not builder then return {},0 end
    local facts={carried=counts(player),tech=call(function() return builder:GetTechTrees() end,{}),
        modifier=call(function() return builder:IngredientMod() end,1),busy=builder:IsBusy(),known_tech_cache={}}
    local parts={tostring(facts.modifier)}
    for prefab,count in pairs(facts.carried) do parts[#parts+1]="i:"..prefab..":"..count end
    for tech,level in pairs(facts.tech) do
        if level~=0 then parts[#parts+1]="t:"..tech..":"..level end
    end
    table.sort(parts)
    local key=table.concat(parts,"|")
    local cache=player._jev_recipe_cache
    if not cache then
        cache={catalog={},version=0,key=key,cursor=1,ids={},checked={},epoch=1,scans=0,hits=0,dirty={}}
        for id in pairs(G.AllRecipes or {}) do cache.ids[#cache.ids+1]=id end
        table.sort(cache.ids)
        player._jev_recipe_cache=cache
    end
    local changed=false
    facts.catalog=cache.catalog
    if not cache.next_index or now>=cache.next_index then
        local ids={}
        for id in pairs(G.AllRecipes or {}) do ids[#ids+1]=id end
        table.sort(ids);cache.ids=ids;cache.next_index=now+10
        cache.cursor=#ids>0 and (cache.cursor-1)%#ids+1 or 1
        cache.indexed=0;for _,id in ipairs(ids) do if cache.checked[id] then cache.indexed=cache.indexed+1 end end
        for id in pairs(cache.catalog) do
            if not (G.AllRecipes or {})[id] then cache.catalog[id]=nil;cache.dirty[id]=true;changed=true end
        end
    end
    local priority={}
    if cache.key~=key then
        cache.epoch=cache.epoch+1; cache.key=key; changed=true
        for id,row in pairs(cache.catalog) do
            if row.ready then priority[#priority+1]=id end
            row.fresh=false; row.ready=false
        end
        table.sort(priority)
    else cache.hits=cache.hits+1 end
    local queue,queued={},{}
    local function enqueue(id)
        local check=cache.checked[id]
        if not queued[id] and (G.AllRecipes or {})[id] and
            (not check or check.epoch~=cache.epoch or now-check.at>=2) then
            queue[#queue+1]=id; queued[id]=true
        end
    end
    local background_next={}
    local function background()
        local cursor=cache.cursor
        local pending=false
        local added=0
        for _=1,#cache.ids do
            local id=cache.ids[cursor]
            cursor=cursor%#cache.ids+1
            if cache.indexed>=#cache.ids or not cache.checked[id] then
                enqueue(id)
                if queued[id] then background_next[id]=cursor;pending=true;added=added+1 end
            end
            if added>=32 then break end
        end
        if not pending then cache.cursor=cursor end
    end
    -- Alternate priority order so even a very slow engine call cannot starve
    -- background indexing. Advance only past work actually completed.
    cache.turn=(cache.turn or 0)+1
    if cache.turn%2==0 then background() end
    for _,id in ipairs(PRIORITY) do enqueue(id) end
    for _,id in ipairs(priority) do enqueue(id) end
    if cache.turn%2==1 then background() end
    local clock=G.walltime
    local started=clock and clock()
    local processed=0
    for _,id in ipairs(queue) do
        if processed>=32 or (processed>0 and started and clock()-started>=.003) then break end
        local row=M.scan(G,player,site,{[id]=true},facts)[id]
        if not cache.checked[id] then cache.indexed=cache.indexed+1 end
        cache.checked[id]={epoch=cache.epoch,at=now}
        if background_next[id] then cache.cursor=background_next[id] end
        if row then
            row.fresh=true; row.fact_epoch=cache.epoch
            if not same_facts(row,cache.catalog[id]) then
                cache.catalog[id]=row; cache.dirty[id]=true; changed=true
            end
        elseif cache.catalog[id] then cache.catalog[id]=nil;cache.dirty[id]=true;changed=true end
        cache.scans=cache.scans+1;processed=processed+1
    end
    -- Cheap readiness maintenance; no engine-wide query or repeated placement
    -- search for a row whose facts are stale.
    local busy=builder:IsBusy()
    for id,row in pairs(cache.catalog) do
        if row.supported and row.fresh~=false then
            local ready=not busy and (row.buffered or ((row.known or row.can_prototype) and row.has_ingredients))
            local reason=(not row.known and not row.can_prototype and not row.buffered and "technology_or_unlock_required")
                or (not row.has_ingredients and not row.buffered and "ingredients_missing")
                or (busy and "builder_busy") or "ready"
            if ready and row.placement and not row.legacy then
                row.site=site(G.GetValidRecipe(row.recipe_id),row.site)
                if not row.site then ready=false; reason="no_safe_placement_site" end
            end
            if row.ready~=ready or row.reason~=reason then changed=true;cache.dirty[id]=true end
            row.ready,row.reason=ready,reason
        end
    end
    if changed then cache.version=cache.version+1 end
    cache.processed=processed
    return cache.catalog,cache.version
end

-- Immutable row objects allow the transport to identify changed recipes by
-- reference, without deep comparison/serialization of 974 rows on each change.
function M.wire_catalog(player,catalog,version)
    local cache=player._jev_recipe_cache
    if cache and cache.wire_version==version then return cache.wire end
    local wire={}
    if cache and cache.wire then for id,row in pairs(cache.wire) do wire[id]=row end end
    for id in pairs(cache and cache.dirty or catalog) do
        local row=catalog[id]
        if row then
            local item={}
            for key,value in pairs(row) do if key~="site" and key~="fresh" then item[key]=value end end
            wire[id]=item
        else wire[id]=nil end
    end
    if cache then cache.wire,cache.wire_version,cache.dirty=wire,version,{} end
    return wire
end

function M.actions(catalog)
    local actions={}
    for id,row in pairs(catalog or {}) do
        if row.ready and not row.legacy then
            local kind=row.placement and "place" or "craft"
            actions["recipe_"..id]={kind=kind,recipe_id=id,product=row.product,
                output_count=row.output_count,x=row.site and row.site.x,z=row.site and row.site.z,
                rotation=0,planner_only=true,
                description=kind.." "..row.product.." using recipe "..id}
        end
    end
    return actions
end

function M.issue(G,player,action,fail)
    local builder=player.replica and player.replica.builder
    local recipe=G.GetValidRecipe(action.recipe_id)
    local function reject(reason) fail(reason); return false end
    if not builder or not recipe or recipe.product~=action.product then return reject("recipe_changed") end
    if builder:IsBusy() then return reject("builder_busy") end
    if not builder:IsBuildBuffered(recipe.name) and
        (not accessible(G,builder,recipe) or not builder:HasIngredients(recipe)) then
        return reject("recipe_no_longer_craftable")
    end
    if action.kind=="craft" then
        if recipe.placer then return reject("placement_required") end
        builder:MakeRecipeFromMenu(recipe)
        return true
    end
    local controller=player.components and player.components.playercontroller
    if not recipe.placer or not controller then return reject("placement_unavailable") end
    local point=G.Vector3(action.x,0,action.z)
    if not placement.live(G,player,action.placement,point) then return reject("placement_anchor_changed_or_outside") end
    if not builder:CanBuildAtPoint(point,recipe,action.rotation) then return reject("placement_invalid") end
    local was_known=builder:KnowsRecipe(recipe)
    if not builder:IsBuildBuffered(recipe.name) then builder:BufferBuild(recipe.name) end
    if not builder:IsBuildBuffered(recipe.name) then return reject("buffer_failed") end
    controller:StartBuildPlacementMode(recipe,nil)
    -- Match native prototype placement on a locally hosted world as well.
    local component=player.components and player.components.builder
    if not was_known and component then
        component:ActivateCurrentResearchMachine(recipe)
        component:UnlockRecipe(recipe.name)
    end
    local generation=player._jev_bridge_generation
    player:DoTaskInTime(0.35,function()
        if player._jev_bridge_generation~=generation then return end
        if not placement.live(G,player,action.placement,point) then
            controller:CancelPlacement(); fail("placement_anchor_changed_or_outside");return
        end
        if not player:IsValid() or not builder:IsBuildBuffered(recipe.name)
            or not builder:CanBuildAtPoint(point,recipe,action.rotation) then
            controller:CancelPlacement(); fail("placement_changed"); return
        end
        if controller.placer then
            controller.placer.Transform:SetPosition(point.x,0,point.z)
            controller.placer.Transform:SetRotation(action.rotation)
        end
        builder:MakeRecipeAtPoint(recipe,point,action.rotation)
        controller:CancelPlacement()
    end)
    return true
end
return M
