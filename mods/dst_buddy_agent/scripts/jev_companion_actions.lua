-- Server components only. No owner inventory, free materials or console execution.
local M={}
local Storage=require("jev_companion_storage")
local TREES={evergreen={"log","pinecone"},evergreen_sparse={"log","pinecone"},deciduoustree={"log","acorn"},twiggytree={"log","twigs","twiggy_nut"}}
local ROCKS={rock1={"rocks","flint","nitre"},rock2={"rocks","flint","goldnugget"},rock_flintless={"rocks"},rock_flintless_med={"rocks"},rock_flintless_low={"rocks"},rock_ice={"ice"}}
local WORK_ACTIONS={"CHOP","MINE"}
local FIRE_PREFABS={campfire=true,firepit=true}
local FUEL_PREFABS={log=true,charcoal=true,pinecone=true,acorn=true,twiggy_nut=true,twigs=true,cutgrass=true}
local SAFE_MEALS={meatballs=true,ratatouille=true}
local IGNITE_TREES={evergreen=true,evergreen_sparse=true,deciduoustree=true,twiggytree=true}
local MEAL_CATALOG={
    {product="ratatouille",ingredients={"carrot","carrot","carrot","carrot"},cook_seconds=20},
    {product="ratatouille",ingredients={"berries","berries","carrot","carrot"},cook_seconds=20},
    {product="meatballs",ingredients={"berries","berries","berries","meat"},cook_seconds=15},
    {product="meatballs",ingredients={"berries","berries","berries","smallmeat"},cook_seconds=15},
    {product="meatballs",ingredients={"berries","berries","berries","monstermeat"},cook_seconds=15},
    {product="meatballs",ingredients={"carrot","carrot","carrot","meat"},cook_seconds=15},
    {product="meatballs",ingredients={"carrot","carrot","carrot","smallmeat"},cook_seconds=15},
    {product="meatballs",ingredients={"carrot","carrot","carrot","monstermeat"},cook_seconds=15},
    {product="meatballs",ingredients={"berries_juicy","berries_juicy","berries_juicy","meat"},cook_seconds=15},
    {product="meatballs",ingredients={"berries_juicy","berries_juicy","berries_juicy","smallmeat"},cook_seconds=15},
    {product="meatballs",ingredients={"berries_juicy","berries_juicy","berries_juicy","monstermeat"},cook_seconds=15},
}
local DEFAULT_RECIPES={"axe","pickaxe","torch","campfire","firepit","researchlab","rope","boards","cutstone","treasurechest","cookpot"}
local function finite(v) return type(v)=="number" and v==v and math.abs(v)<100000 end
-- Entity IDs are not coordinates; real DST worlds routinely use IDs >100000.
local function valid_guid(v) return type(v)=="number" and v==v and v>=1 and v<=9007199254740991 and v%1==0 end
local function identifier(v) return type(v)=="string" and #v<=100 and v:match("^[a-z0-9_]+$") end
function M.new(G,E)
    local A={}
    local function position(e) return E.position(e) end
    local function nearby(a,radius)
        local x,z=position(a);return G.TheSim:FindEntities(x,0,z,radius,nil,{"INLIMBO","FX","DECOR"})
    end
    local function usable(a,e,radius)
        return e and e:IsValid() and e~=a and not e:HasTag("INLIMBO") and not e:HasTag("fire")
            and not e:HasTag("player") and E.distance(a,e)<=(radius or 15)
            and not ((e.components or {}).burnable and e.components.burnable:IsBurning())
    end
    local function carried(a,guid)
        return a.components.inventory:FindItems(function(item) return item.GUID==guid and item:IsValid() end)[1]
    end
    local function edible_for(a,item)
        local eater=a.components.eater
        return eater and item:IsValid() and item.components.edible and not eater.eatwholestack
            and eater:CanEat(item) and eater:PrefersToEat(item)
    end
    function A.foods(a)
        local eater=a.components.eater
        if not eater then return nil end
        local foods={}
        local memory=a.components.foodmemory
        for _,item in ipairs(a.components.inventory:FindItems(function(i) return edible_for(a,i) end)) do
            local edible=item.components.edible
            local multiplier=memory and memory:GetFoodMultiplier(item.prefab) or 1
            local effects=(edible.healthvalue>=0 and edible.sanityvalue>=0) or eater:DoFoodEffects(item)
            foods[#foods+1]={guid=item.GUID,prefab=item.prefab,
                count=item.components.stackable and item.components.stackable:StackSize() or 1,
                hunger_delta=a.components.hunger and edible:GetHunger(a)*multiplier*eater.hungerabsorption or 0,
                sanity_delta=a.components.sanity and (edible.sanityvalue>=0 or effects)
                    and edible:GetSanity(a)*multiplier*eater.sanityabsorption or 0,
                health_delta=a.components.health and (edible.healthvalue>=0 or effects)
                    and edible:GetHealth(a)*multiplier*eater.healthabsorption or 0}
        end
        return foods
    end
    local function healthy(item)
        local c=item.components
        return (not c.finiteuses or c.finiteuses:GetPercent()>0) and (not c.fueled or c.fueled:GetPercent()>0)
    end
    local function can_equip(a,item)
        local equip=item.components.equippable
        return equip and healthy(item) and not equip:IsRestricted(a)
    end
    local function supports_work(item,kind)
        return item.components.tool and item.components.tool:CanDoAction(G.ACTIONS[kind])
    end
    function A.work_tools(a)
        local inv=a.components.inventory
        local held=inv:GetEquippedItem(G.EQUIPSLOTS.HANDS)
        local tools=inv:FindItems(function(item)
            return item:IsValid() and item.components.tool and (item==held and healthy(item)
                or item.components.equippable and item.components.equippable.equipslot==G.EQUIPSLOTS.HANDS and can_equip(a,item))
        end)
        local kinds={}
        for _,tool in ipairs(tools) do
            for _,kind in ipairs(WORK_ACTIONS) do
                if supports_work(tool,kind) then kinds[kind]=true end
            end
        end
        return kinds
    end
    function A.work_target(a,guid)
        local e=G.Ents[guid]
        if not usable(a,e) or e:HasTag("stump") then return end
        local burnt=e:HasTag("burnt")
        if burnt and not TREES[e.prefab] then return end
        local w=e.components.workable
        if not w or not w:CanBeWorked() or w:GetWorkLeft()<=0 then return end
        local kind,products
        if TREES[e.prefab] and w:GetWorkAction()==G.ACTIONS.CHOP then kind,products="CHOP",burnt and {"charcoal"} or TREES[e.prefab]
        elseif not burnt and ROCKS[e.prefab] and w:GetWorkAction()==G.ACTIONS.MINE then kind,products="MINE",ROCKS[e.prefab] end
        if not kind then return end
        local x,z=position(e)
        return {guid=e.GUID,prefab=e.prefab,work=kind,products=products,remaining=w:GetWorkLeft(),distance=E.distance(a,e),x=x,z=z}
    end
    local function watch(a,id)
        local ids=a._jev_companion_recipes
        if not ids then ids={};a._jev_companion_recipes=ids;for _,v in ipairs(DEFAULT_RECIPES) do ids[#ids+1]=v end end
        for i,v in ipairs(ids) do if v==id then table.remove(ids,i);break end end
        ids[#ids+1]=id
        if #ids>48 then table.remove(ids,1) end
    end
    local function prototype(builder,recipe,trees)
        return builder:CanLearn(recipe.name) and G.CanPrototypeRecipe(recipe.level,trees or builder.accessible_tech_trees)
    end
    local function read_recipe(a,id)
        local builder=a.components.builder
        local recipe=identifier(id) and G.GetValidRecipe and G.GetValidRecipe(id)
        if not recipe or not builder then return end
        local supported=type(recipe.product)=="string" and type(recipe.numtogive or 1)=="number"
            and (recipe.numtogive or 1)>0 and (recipe.numtogive or 1)<=100 and (recipe.numtogive or 1)%1==0
        local reason=not supported and "dynamic_product_or_output" or nil
        if #(recipe.character_ingredients or {})>0 or #(recipe.tech_ingredients or {})>0 then supported=false;reason="special_ingredients_not_supported" end
        if recipe.unlocks_from_skin or recipe.manufactured then supported=false;reason="special_recipe_not_supported" end
        if recipe.placer and recipe.build_mode and G.BUILDMODE and recipe.build_mode~=G.BUILDMODE.LAND then supported=false;reason="special_placement_not_supported" end
        local known=builder:KnowsRecipe(recipe)==true
        local canproto=not known and prototype(builder,recipe)==true
        local buffered=builder:IsBuildBuffered(id)==true
        local ingredients=builder:HasIngredients(recipe)==true
        local product=type(recipe.product)=="string" and recipe.product or id
        local row={recipe_id=id,product=product,name=((G.STRINGS or {}).NAMES or {})[string.upper(product)] or id,
            output_count=type(recipe.numtogive)=="number" and recipe.numtogive or 1,placement=recipe.placer~=nil,supported=supported,known=known,
            can_prototype=canproto,has_ingredients=ingredients,buffered=buffered,ingredients={},technology={},legacy=false}
        for name,value in pairs(recipe.level or {}) do if value~=0 then row.technology[name]=value end end
        for _,ing in ipairs(recipe.ingredients or {}) do
            local amount=math.max(1,(G.RoundBiasedUp or math.ceil)(ing.amount*(builder.ingredientmod or 1)))
            local count=E.count(a,ing.type)
            row.ingredients[#row.ingredients+1]={prefab=ing.type,count=amount,carried=count,missing=math.max(0,amount-count)}
        end
        row.ready=supported and (buffered or (known or canproto) and ingredients)
        row.reason=reason or (not known and not canproto and not buffered and "technology_or_unlock_required")
            or (not ingredients and not buffered and "ingredients_missing") or "ready"
        return row,recipe
    end
    function A.recipe(a,id)
        local ok,row,recipe=pcall(read_recipe,a,id)
        if ok then return row,recipe end
        return {recipe_id=id,product=id,supported=false,ready=false,reason="recipe_metadata_unavailable"}
    end
    function A.query(a,args)
        local query=args.query or "";local offset=args.offset or 0;local limit=args.limit or 12
        if type(query)~="string" or #query>300 or not finite(offset) or offset<0 or offset%1~=0
            or not finite(limit) or limit<1 or limit>24 or limit%1~=0 or args.recipe_id~=nil and not identifier(args.recipe_id) then return nil,"invalid_recipe_query" end
        local ids={};query=query:lower()
        for id,recipe in pairs(G.AllRecipes or {}) do
            local name=((G.STRINGS or {}).NAMES or {})[string.upper(type(recipe.product)=="string" and recipe.product or id)] or ""
            if (not args.recipe_id or id==args.recipe_id) and (query=="" or id:lower():find(query,1,true)
                or tostring(recipe.product):lower():find(query,1,true) or name:lower():find(query,1,true)) then ids[#ids+1]=id end
        end
        table.sort(ids);local rows={};local bytes=0;local cursor=offset
        for i=offset+1,math.min(#ids,offset+limit) do
            local row=A.recipe(a,ids[i])
            if row then
                local cost=E.encoded_size and E.encoded_size(row) or 0
                if cost>16000 then row={recipe_id=ids[i],supported=false,ready=false,reason="recipe_payload_too_large"};cost=200 end
                if bytes+cost>16000 then break end
                bytes=bytes+cost;rows[#rows+1]=row;watch(a,ids[i])
            end
            cursor=i
        end
        return {total=#ids,offset=offset,next_offset=cursor<#ids and cursor or nil,recipes=rows}
    end
    local function site(a,recipe,args)
        local cx,cz=args.x,args.z
        if not finite(cx) or not finite(cz) or not E.valid_point(a,cx,cz,12) then return end
        local radius=args.radius or 6
        if not finite(radius) or radius<2 or radius>8 then return end
        for _,distance in ipairs({2,2.5,3,4,5,6,7,8}) do
            if distance<=radius then for n=0,11 do
                local x,z=cx+math.cos(n*math.pi/6)*distance,cz+math.sin(n*math.pi/6)*distance
                local p=G.Vector3(x,0,z)
                if E.valid_point(a,x,z,20) and G.TheWorld.Map:CanDeployRecipeAtPoint(p,recipe,0,a) then
                    local clear=true
                    for _,e in ipairs(G.TheSim:FindEntities(x,0,z,4,nil,{"INLIMBO","FX","DECOR"})) do
                        local ex,ez=position(e)
                        local parent=e.entity and e.entity:GetParent()
                        local safe_pit=e.prefab=="firepit" or e.prefab=="coldfirepit"
                            or (parent and (parent.prefab=="firepit" or parent.prefab=="coldfirepit"))
                        if (not safe_pit and (e:HasTag("fire") or ((e.components or {}).burnable and e.components.burnable:IsBurning())))
                            or ((recipe.product=="campfire" or recipe.product=="firepit") and TREES[e.prefab] and (ex-x)^2+(ez-z)^2<16) then clear=false;break end
                    end
                    if clear then return p end
                end
            end end
        end
    end
    local function push(a,q,action)
        if action.AddSuccessAction then action:AddSuccessAction(function() q.native_succeeded=true end) end
        if action.AddFailAction then action:AddFailAction(function() q.native_failure="native_action_failed" end) end
        a.components.locomotor:PushAction(action,true)
        return true
    end
    local function eat(a,q,r)
        local args=q.args
        if args.container_guid~=nil then return false,"invalid_food_target" end
        local receipt,reason=Storage.item_receipt(r,args,"eat")
        if not receipt then return false,reason end
        q.receipt=receipt
        if receipt.count>=args.total_count then q.eat_complete=true;return true end
        local item=a.components.inventory:FindItems(function(i)
            return i.prefab==args.prefab and edible_for(a,i)
        end)[1]
        if not item then return false,"food_not_carried_or_inedible" end
        local action=G.BufferedAction(a,nil,G.ACTIONS.EAT,item)
        action.validfn=function() return carried(a,item.GUID)==item and edible_for(a,item) end
        action:AddSuccessAction(function()
            receipt.count=receipt.count+1
            q.eat_complete=true
        end)
        return push(a,q,action)
    end
    local function refuel(a,q,r)
        local args=q.args
        if type(args.building_id)~="string" or not FUEL_PREFABS[args.fuel_prefab] then return false,"invalid_refuel_args" end
        local target,reason=E.place_target and E.place_target(a,args.building_id)
        if not target then return false,reason or "refuel_target_missing" end
        if not FIRE_PREFABS[target.prefab] then return false,"refuel_unsupported_structure" end
        if E.distance(a,target)>4 then return false,"refuel_target_too_far" end
        local receipt,rreason=Storage.item_receipt(r,{prefab=args.fuel_prefab,total_count=args.total_count,task_key=args.task_key},"refuel")
        if not receipt then return false,rreason end
        q.receipt=receipt
        if receipt.count>=args.total_count then q.refuel_complete=true;return true end
        local item=a.components.inventory:FindItems(function(i)
            return i:IsValid() and i.prefab==args.fuel_prefab and i.components.fuel~=nil
        end)[1]
        if not item then return false,"fuel_not_carried" end
        local fuel_action=(item.GetIsWet and item:GetIsWet() and G.ACTIONS.ADDWETFUEL) or G.ACTIONS.ADDFUEL
        local action=G.BufferedAction(a,target,fuel_action,item)
        action.validfn=function() return target:IsValid() and carried(a,item.GUID)==item end
        action:AddSuccessAction(function()
            receipt.count=receipt.count+1
            q.refuel_complete=true
        end)
        return push(a,q,action)
    end
    function A.refueling(a,entities)
        local targets={}
        for _,e in ipairs(entities or {}) do
            if e and e:IsValid() and FIRE_PREFABS[e.prefab] and not e:HasTag("INLIMBO") and not e:HasTag("burnt") then
                local fueled=e.components and e.components.fueled
                local percent=fueled and fueled.GetPercent and fueled:GetPercent() or nil
                local section=fueled and fueled.GetCurrentSection and fueled:GetCurrentSection() or nil
                local lit=(fueled and fueled.IsEmpty and not fueled:IsEmpty()) or e:HasTag("fire") or false
                targets[#targets+1]={building_id="building:"..tostring(e.GUID),guid=e.GUID,prefab=e.prefab,
                    lit=lit,fuel_percent=percent,fire_section=section,fire_data_source="server_fueled"}
            end
        end
        return {available=true,ratio_available=true,targets=targets}
    end
    function A.camp_work(a,entities)
        local fixtures,trees={},{}
        for _,e in ipairs(entities or {}) do
            if e and e:IsValid() and not e:HasTag("INLIMBO") then
                if e.prefab=="cookpot" then
                    local c=e.components and e.components.container
                    local stewer=e.components and e.components.stewer
                    local phase="closed"
                    if e:HasTag("burnt") then phase="burnt"
                    elseif stewer and stewer:IsDone() or e:HasTag("donecooking") then phase="ready"
                    elseif stewer and stewer:IsCooking() or (c and c.CanBeOpened and not c:CanBeOpened()) then phase="unavailable"
                    elseif c and c:IsOpenedBy(a) then phase="open" end
                    local items={}
                    local known=phase~="burnt" and phase~="unavailable" and c~=nil and not c:IsOpenedByOthers(a)
                    if known then
                        for slot=1,(c.numslots or 4) do
                            local item=(c.slots or {})[slot]
                            if item and item:IsValid() then
                                local stack=item.components.stackable
                                items[#items+1]={slot=slot,guid=item.GUID,prefab=item.prefab,count=stack and stack:StackSize() or 1}
                            end
                        end
                    end
                    fixtures[#fixtures+1]={building_id="building:"..tostring(e.GUID),guid=e.GUID,prefab="cookpot",
                        phase=phase,contents_known=known,items=items,product=stewer and stewer.product or nil}
                elseif (IGNITE_TREES[e.prefab] or (TREES[e.prefab] and (e:HasTag("burnt") or e:HasTag("fire") or (e.components.burnable and e.components.burnable:IsBurning()))))
                    and not e:HasTag("stump") and not e:HasTag("monster") and not e:HasTag("plantcreature") and E.distance(a,e)<=15 then
                    local dist=E.distance(a,e)
                    local status="ignitable"
                    if e:HasTag("burnt") then status="burnt"
                    elseif e.components.burnable and e.components.burnable:IsBurning() or e:HasTag("fire") then status="burning"
                    elseif dist>12 or not IGNITE_TREES[e.prefab] then status=nil
                    else
                        local tx,tz=position(e)
                        for _,other in ipairs(G.TheSim:FindEntities(tx,0,tz,7.5,nil,{"INLIMBO","FX","DECOR"})) do
                            if other~=e and other~=a and other:IsValid() and (other:HasTag("structure") or other:HasTag("player") or IGNITE_TREES[other.prefab]) then
                                status="unsafe";break
                            end
                        end
                    end
                    if status then
                        local ex,ez=position(e)
                        trees[#trees+1]={guid=e.GUID,prefab=e.prefab,distance=dist,status=status,x=ex,z=ez}
                    end
                end
            end
        end
        return {available=true,fixtures=fixtures,trees=trees,meals=MEAL_CATALOG}
    end
    function A.start(a,q,r)
        local args=q.args or {};local op=q.op
        if a.sg and a.sg.HasStateTag and (a.sg:HasStateTag("busy") or a.sg:HasStateTag("nointerrupt")) then
            q.deferred=true;return true
        end
        q.deferred=nil
        if op=="eat" then
            return eat(a,q,r)
        elseif op=="refuel" then
            return refuel(a,q,r)
        elseif op=="set_home" then
            local ok,info=E.set_home and E.set_home(a,args.anchor_id)
            if not ok then return false,info or "set_home_unavailable" end
            q.home_id=info;return true
        elseif op=="cook_load" then
            local target,reason=E.place_target and E.place_target(a,args.building_id)
            if not target or target.prefab~="cookpot" then return false,reason or "cookpot_missing" end
            if E.distance(a,target)>4 then return false,"cookpot_too_far" end
            local c=target.components and target.components.container
            if not c or c:IsOpenedByOthers(a) or (c.CanBeOpened and not c:CanBeOpened()) then return false,"cookpot_busy" end
            local item=a.components.inventory:FindItems(function(i) return i:IsValid() and i.prefab==args.prefab end)[1]
            if not item then return false,"meal_ingredient_missing" end
            local slot
            for s=1,(c.numslots or 4) do
                if not (c.slots or {})[s] and c:CanTakeItemInSlot(item,s) then slot=s;break end
            end
            if not slot then return false,"cookpot_full_or_rejected" end
            local single=item.components.stackable and item.components.stackable:StackSize()>1 and item.components.stackable:Get(1)
                or a.components.inventory:RemoveItem(item,false)
            if not single then return false,"ingredient_split_failed" end
            if not c:GiveItem(single,slot) then a.components.inventory:GiveItem(single);return false,"cookpot_insert_failed" end
            q.cook_loaded=true;return true
        elseif op=="start_meal" then
            local target,reason=E.place_target and E.place_target(a,args.building_id)
            if not target or target.prefab~="cookpot" then return false,reason or "cookpot_missing" end
            if E.distance(a,target)>4 then return false,"cookpot_too_far" end
            local stewer=target.components and target.components.stewer
            if not stewer or not stewer:CanCook() then return false,"cookpot_cannot_cook" end
            stewer:StartCooking(a)
            q.meal_started=true;q.cook_seconds=30;return true
        elseif op=="harvest_meal" then
            local target,reason=E.place_target and E.place_target(a,args.building_id)
            if not target or target.prefab~="cookpot" then return false,reason or "cookpot_missing" end
            if E.distance(a,target)>4 then return false,"cookpot_too_far" end
            local stewer=target.components and target.components.stewer
            if not stewer or not stewer:IsDone() then return false,"meal_not_ready" end
            local product=stewer.product
            if not stewer:Harvest(a) then return false,"meal_harvest_failed" end
            q.harvested_product=product;return true
        elseif op=="ignite_tree" then
            local target=valid_guid(args.tree_guid) and G.Ents[args.tree_guid]
            if not target or not target:IsValid() or not IGNITE_TREES[target.prefab] or target:HasTag("burnt") or target:HasTag("stump") or target:HasTag("monster") or target:HasTag("plantcreature") then
                return false,"tree_unavailable"
            end
            if E.distance(a,target)>12 then return false,"tree_too_far" end
            local torch=a.components.inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS)
            if not torch or torch.prefab~="torch" or not healthy(torch) then return false,"equipped_torch_required" end
            q.ignite_target=target
            return push(a,q,G.BufferedAction(a,target,G.ACTIONS.LIGHT,torch))
        elseif op=="equip" then
            local item=valid_guid(args.item_guid) and carried(a,args.item_guid)
            if not item or not can_equip(a,item) then return false,"item_not_equippable" end
            q.item_guid=item.GUID
            return push(a,q,G.BufferedAction(a,nil,G.ACTIONS.EQUIP,item))
        elseif op=="unequip" then
            local item=valid_guid(args.item_guid) and carried(a,args.item_guid)
            local accepted,reason=Storage.stow(a,item)
            if not accepted then return false,reason end
            q.stowed=true;return true
        elseif op=="work" then
            local row=valid_guid(args.target_guid) and A.work_target(a,args.target_guid)
            if not row or row.work~=args.work then return false,"work_target_unavailable" end
            local tool=a.components.inventory:GetEquippedItem(G.EQUIPSLOTS.HANDS)
            if not tool or not healthy(tool) or not supports_work(tool,row.work) then return false,"equipped_tool_required" end
            q.work_target=G.Ents[row.guid];q.work_before=row.remaining
            q.work_component=q.work_target.components.workable
            return push(a,q,G.BufferedAction(a,q.work_target,G.ACTIONS[row.work],tool))
        elseif op=="craft" or op=="place" then
            local builder=a.components.builder
            if builder and builder.EvaluateTechTrees then builder:EvaluateTechTrees() end
            local row,recipe=A.recipe(a,args.recipe_id)
            if not row or not row.ready or row.placement~=(op=="place") then return false,row and row.reason or "unknown_recipe" end
            local point
            if op=="place" then point=site(a,recipe,args);if not point then return false,"no_safe_placement_site" end end
            q.product=row.product;q.before=E.count(a,row.product);q.site=point;q.preexisting={}
            if point then for _,e in ipairs(G.TheSim:FindEntities(point.x,0,point.z,2)) do q.preexisting[e.GUID]=true end end
            local knows=builder:KnowsRecipe(recipe,true)
            local canproto=prototype(builder,recipe,builder.accessible_tech_trees_no_temp)
            local usingtemp=not knows and not canproto
            local accepted=builder:MakeRecipe(recipe,point,0,nil,function()
                q.native_succeeded=true
                if usingtemp then builder:ConsumeTempTechBonuses() end
                if not knows and canproto and builder:CanLearn(recipe.name) then
                    builder:ActivateCurrentResearchMachine(recipe);builder:UnlockRecipe(recipe.name)
                elseif not recipe.nounlock then builder:AddRecipe(recipe.name) end
            end)
            if not accepted then return false,"builder_rejected" end
            watch(a,args.recipe_id)
            local action=a.GetBufferedAction and a:GetBufferedAction()
            if action and action.recipe==args.recipe_id and action.AddFailAction then action:AddFailAction(function() q.native_failure="build_failed" end) end
            return true
        end
        return false,"unsupported_action"
    end
    function A.progress(a,q,r)
        if q.deferred then
            local accepted,reason=A.start(a,q,r)
            if not accepted then return "failed",reason end
            return
        end
        if q.op=="eat" and q.eat_complete then
            return "completed","food_eaten",{task_key=q.args.task_key,count=q.receipt.count}
        elseif q.op=="refuel" and q.refuel_complete then
            return "completed","fuel_added",{task_key=q.args.task_key,count=q.receipt.count,building_id=q.args.building_id}
        elseif q.op=="set_home" and q.home_id then
            return "completed","home_set",{home_id=q.home_id,anchor_id=q.args.anchor_id}
        elseif q.op=="cook_load" and q.cook_loaded then
            return "completed","one_item_transferred",{building_id=q.args.building_id,prefab=q.args.prefab}
        elseif q.op=="start_meal" and q.meal_started then
            return "completed","pot_started",{building_id=q.args.building_id,cook_seconds=q.cook_seconds}
        elseif q.op=="harvest_meal" and q.harvested_product then
            return "completed","meal_harvested:"..tostring(q.harvested_product),{building_id=q.args.building_id,product=q.harvested_product}
        elseif q.op=="ignite_tree" then
            local target=q.ignite_target
            local lit=q.native_succeeded or (target and target:IsValid() and (target:HasTag("fire") or (target.components.burnable and target.components.burnable:IsBurning())))
            if lit then
                if not q.retreat_started then
                    q.retreat_started=true
                    q.retreat_deadline=E.now()+4
                    local ax,az=position(a)
                    local tx,tz=ax,az
                    if target and target:IsValid() then tx,tz=position(target) end
                    local base=math.atan2(az-tz,ax-tx)
                    local dest
                    for _,dist in ipairs({7,6,8,5.5}) do
                        for _,off in ipairs({0,math.pi/6,-math.pi/6,math.pi/3,-math.pi/3,math.pi/2,-math.pi/2,math.pi}) do
                            local rx,rz=tx+math.cos(base+off)*dist,tz+math.sin(base+off)*dist
                            if E.valid_point(a,rx,rz,14) then
                                local safe=true
                                for _,e in ipairs(G.TheSim:FindEntities(rx,0,rz,4.5,nil,{"INLIMBO","FX","DECOR"})) do
                                    if e~=a and e:IsValid() and (e:HasTag("fire") or ((e.components or {}).burnable and e.components.burnable:IsBurning())) then
                                        safe=false;break
                                    end
                                end
                                if safe then dest={x=rx,z=rz};break end
                            end
                        end
                        if dest then break end
                    end
                    q.retreat_dest=dest
                end
                local far=not target or not target:IsValid() or E.distance(a,target)>=5.5
                local arrived=not q.retreat_dest
                if q.retreat_dest then
                    local ax,az=position(a)
                    if (ax-q.retreat_dest.x)^2+(az-q.retreat_dest.z)^2<=1.5^2 then arrived=true end
                end
                if far or arrived or E.now()>=(q.retreat_deadline or 0) then
                    E.stop(a)
                    return "completed","tree_ignited",{tree_guid=q.args.tree_guid}
                end
                if q.retreat_dest and (not a.sg or not a.sg.HasStateTag or not a.sg:HasStateTag("busy")) then
                    E.move(a,q.retreat_dest.x,q.retreat_dest.z)
                end
                return
            end
        elseif q.op=="equip" then
            local item=carried(a,q.item_guid)
            if item and a.components.inventory:IsItemEquipped(item) then return "completed","equipped" end
        elseif q.op=="unequip" and q.stowed then return "completed","unequipped"
        elseif q.op=="work" then
            local e=q.work_target
            -- The final chop replaces CHOP workable with a fresh DIG workable on the stump.
            -- Compare the original component; the new stump's counter is unrelated.
            local w=q.work_component
            if w and w:GetWorkLeft()<q.work_before then
                return "completed",w:GetWorkLeft()<=0 and "work_finished" or "work_progressed"
            end
            if q.native_succeeded then
                if not e or not e:IsValid() or not e.components.workable or e.components.workable:GetWorkLeft()<=0 then return "completed","work_finished" end
                return "failed","work_did_not_progress"
            end
        elseif q.op=="craft" then
            if E.count(a,q.product)>q.before then return "completed","inventory_increased" end
        elseif q.op=="place" and q.native_succeeded then
            for _,e in ipairs(G.TheSim:FindEntities(q.site.x,0,q.site.z,2)) do
                if e:IsValid() and e.prefab==q.product and not q.preexisting[e.GUID] then
                    a._jev_companion_last_build=e.GUID;return "completed","new_structure_observed"
                end
            end
        end
        if q.native_failure then return "failed",q.native_failure end
    end
    function A.snapshot(a,entities)
        local works,structures,equipment,recipes={},{},{},{}
        local products={}
        if a.components.builder and G.GetValidRecipe then
            watch(a,"axe");local bytes=0
            for index=#a._jev_companion_recipes,1,-1 do
                local id=a._jev_companion_recipes[index]
                local row=A.recipe(a,id)
                if row then
                    local cost=E.encoded_size and E.encoded_size(row) or 0
                    if bytes+cost<=16000 then recipes[id]=row;bytes=bytes+cost end
                    if row.placement then products[row.product]=true end
                end
            end
        end
        for _,e in ipairs(entities) do
            local row=A.work_target(a,e.GUID);if row then works[#works+1]=row end
            local is_fire=FIRE_PREFABS[e.prefab] and e:IsValid() and not e:HasTag("INLIMBO") and not e:HasTag("burnt") and E.distance(a,e)<=20
            if (is_fire or usable(a,e,20)) and (e:HasTag("structure") or products[e.prefab] or FIRE_PREFABS[e.prefab]) then
                local x,z=position(e);structures[#structures+1]={guid=e.GUID,prefab=e.prefab,x=x,z=z}
            end
        end
        table.sort(works,function(l,r) return l.distance<r.distance end)
        local selected,seen,rest={},{},{}
        for _,row in ipairs(works) do
            if not seen[row.prefab] then selected[#selected+1]=row;seen[row.prefab]=true else rest[#rest+1]=row end
        end
        for _,row in ipairs(rest) do if #selected>=24 then break end;selected[#selected+1]=row end
        while #selected>24 do table.remove(selected) end
        works=selected
        table.sort(structures,function(l,r)
            if l.guid==a._jev_companion_last_build then return true elseif r.guid==a._jev_companion_last_build then return false end
            return l.guid<r.guid
        end)
        while #structures>24 do table.remove(structures) end
        local gear=a.components.inventory:FindItems(function(i) return i.components.equippable~=nil end)
        table.sort(gear,function(l,r)
            local le,re=not not a.components.inventory:IsItemEquipped(l),not not a.components.inventory:IsItemEquipped(r)
            if le~=re then return le end;return l.GUID<r.GUID
        end)
        for _,item in ipairs(gear) do
            if #equipment>=24 then break end
            if item.components.equippable then
            local tools={}
            for _,name in ipairs(WORK_ACTIONS) do if supports_work(item,name) then tools[#tools+1]=name end end
            local equipped=not not a.components.inventory:IsItemEquipped(item)
            local can_unequip,unequip_reason
            if equipped then can_unequip,unequip_reason=Storage.can_stow(a,item) end
            equipment[#equipment+1]={guid=item.GUID,prefab=item.prefab,slot=item.components.equippable.equipslot,
                equipped=equipped,can_unequip=can_unequip,unequip_reason=unequip_reason,usable=healthy(item),tools=tools}
            end
        end
        return {work_targets=works,structures=structures,equipment=equipment,recipes=recipes}
    end
    return A
end
return M
