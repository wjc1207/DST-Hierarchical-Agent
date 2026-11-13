-- Client-side storage, crock pot and controlled charcoal production.
local places=require("jev_places")
local fire_siting=require("jev_campfire_siting")
local M={}
local RANGE=3
local kinds={open_container=true,store_item=true,take_item=true,start_meal=true,harvest_meal=true,ignite_tree=true}
local trees={evergreen=true,evergreen_sparse=true}
local fixtures={treasurechest=true,cookpot=true}
local safe_meals={meatballs=true,ratatouille=true}
local TREE_SPREAD_MAX=7 -- Vanilla evergreen uses MakeMediumPropagator (5..7).
local SPREAD_MARGIN=.5
local HOME_MARGIN=1
local IGNITION_RANGE=12
M.safe_meals=safe_meals
M.kinds=kinds

local function entity(G,guid)
    local e=G.Ents and G.Ents[guid]
    return e and e:IsValid() and e or nil
end

local function distance(player,e)
    return math.sqrt(player:GetDistanceSqToInst(e))
end

local function amount(item)
    local stack=item.replica and item.replica.stackable
    return stack and stack:StackSize() or 1
end

local function slots(replica)
    local result={}
    for slot,item in pairs(replica:GetItems() or {}) do
        if type(slot)=="number" and item and item:IsValid() then
            result[#result+1]={slot=slot,guid=item.GUID,prefab=item.prefab,count=amount(item)}
        end
    end
    table.sort(result,function(a,b) return a.slot<b.slot end)
    return result
end

local function stamp(items)
    local parts={}
    for _,item in ipairs(items) do
        parts[#parts+1]=item.slot..":"..item.guid..":"..item.prefab..":"..item.count
    end
    return table.concat(parts,";")
end

local function names(items)
    local result={}
    for _,item in ipairs(items) do
        if item.count~=1 then return {} end
        result[#result+1]=item.prefab
    end
    return result
end

-- Reuse the game recipe predicates, but never roll the gameplay RNG to preview.
-- Canonical ingredients only: aliases that lack ingredient metadata are rejected.
function M.predict(cooking,ingredients)
    if #ingredients~=4 or not cooking or not cooking.recipes or not cooking.ingredients then return end
    local totals,tags={},{}
    for _,prefab in ipairs(ingredients) do
        local ingredient=cooking.ingredients[prefab]
        if not ingredient then return end
        totals[prefab]=(totals[prefab] or 0)+1
        for tag,value in pairs(ingredient.tags or {}) do tags[tag]=(tags[tag] or 0)+value end
    end
    local best,product,cooktime,ambiguous
    for key,recipe in pairs(cooking.recipes.cookpot or {}) do
        local ok,matches=pcall(recipe.test,"cookpot",totals,tags)
        if not ok then return end
        if matches then
            local priority=recipe.priority or 0
            if not best or priority>best then
                best,product,cooktime,ambiguous=priority,recipe.name or key,recipe.cooktime or 1,false
            elseif priority==best then
                ambiguous=true
            end
        end
    end
    if not ambiguous then return product,cooktime end
end

local function seconds(G,multiplier)
    return (G.TUNING and G.TUNING.BASE_COOK_TIME or 20)*(multiplier or 1)
end

local meal_cache={}
local function meal_options(G,cooking)
    if not cooking then return {} end
    if meal_cache[cooking] then return meal_cache[cooking] end
    local candidates={{"carrot","carrot","carrot","carrot"},{"berries","berries","carrot","carrot"}}
    for _,meat in ipairs({"meat","smallmeat","monstermeat"}) do
        for _,filler in ipairs({{"berries","berries","berries"},{"carrot","carrot","carrot"},
                                {"berries","berries","carrot"},{"berries","carrot","carrot"},
                                {"berries_juicy","berries_juicy","berries_juicy"}}) do
            candidates[#candidates+1]={meat,filler[1],filler[2],filler[3]}
        end
    end
    local result={}
    for _,ingredients in ipairs(candidates) do
        local product,multiplier=M.predict(cooking,ingredients)
        if safe_meals[product] then
            result[#result+1]={product=product,ingredients=ingredients,cook_seconds=seconds(G,multiplier)}
        end
    end
    meal_cache[cooking]=result
    return result
end

local function phase(e,c,player)
    if e:HasTag("burnt") then return "burnt" end
    if e.prefab=="cookpot" then
        if e:HasTag("donecooking") then return "ready" end
        if not c:CanBeOpened() then return "unavailable" end
    end
    return c:IsOpenedBy(player) and "open" or "closed"
end

local function inventory(player)
    local inv=player.replica and player.replica.inventory
    if not inv or not inv.GetActiveItem or not inv.GetOverflowContainer then return end
    return inv,inv:GetOverflowContainer()
end

local function has_hand_item(inv)
    return inv:GetActiveItem()~=nil
end

local function can_fit(container,item,empty_only)
    for slot=1,container:GetNumSlots() do
        if container:CanTakeItemInSlot(item,slot) then
            local existing=container:GetItemInSlot(slot)
            local stack=existing and existing.replica and existing.replica.stackable
            if not existing or (not empty_only and stack and not stack:IsFull() and stack:CanStackWith(item)) then
                return true
            end
        end
    end
    return false
end

local function native_offer(result,kind,row,extra,suffix)
    local a=extra or {}
    a.kind,a.guid,a.building_id,a.planner_only=kind,row.guid,row.building_id,true
    a.prefab=row.prefab
    a.description=kind.." at "..row.building_id
    result[kind..":"..row.guid..(suffix or "")]=a
end

local function valid_tree(e)
    return e and trees[e.prefab] and e:HasTag("CHOP_workable") and e:HasTag("canlight")
        and not e:HasTag("burnt") and not e:HasTag("fire") and not e:HasTag("smolder")
        and not e:HasTag("stump") and not e:HasTag("plantcreature")
end

local function ignition_radius(G,e)
    local propagator=e.components and e.components.propagator
    local radius=propagator and propagator.propagaterange
    if type(radius)~="number" or radius~=radius or radius<0 or radius>=math.huge then radius=TREE_SPREAD_MAX end
    if G.TheWorld and G.TheWorld.state.isspring then
        local factor=G.TUNING and G.TUNING.SPRING_FIRE_RANGE_MOD
        if type(factor)=="number" and factor==factor and factor>1 and factor<math.huge then radius=radius*factor end
    end
    return radius+SPREAD_MARGIN
end

local function ignition_hazards(G,player,x,z,radius)
    local hazards={}
    for _,other in ipairs(G.TheSim:FindEntities(x,0,z,radius,nil,{"INLIMBO","FX"})) do
        if other~=player and other:IsValid() and other.Transform
            and (fire_siting.combustible(other) or other:HasTag("structure")) then
            local ox,_,oz=other.Transform:GetWorldPosition()
            hazards[#hazards+1]={entity=other,x=ox,z=oz}
        end
    end
    return hazards
end

local function ignition_safe(G,player,e,memory,hazards)
    if not valid_tree(e) or not G.TheSim then return false,{reason="tree_not_ignitable"} end
    local x,y,z=e.Transform:GetWorldPosition()
    local radius=ignition_radius(G,e)
    local function within(point,r)
        return point and type(point.x)=="number" and type(point.z)=="number"
            and (point.x-x)^2+(point.z-z)^2<r*r
    end
    if within(memory and memory.home,radius+HOME_MARGIN) then
        return false,{reason="near_home",clearance=radius+HOME_MARGIN}
    end
    for _,building in pairs(memory and memory.buildings or {}) do
        if building.status~="missing" and within(building,radius) then
            return false,{reason="near_remembered_building",blocker_prefab=building.prefab,
                blocker_guid=building.guid,clearance=radius}
        end
    end
    hazards=hazards or ignition_hazards(G,player,x,z,radius)
    for _,hazard in ipairs(hazards) do
        local other=hazard.entity
        if other~=e and within(hazard,radius) then
            return false,{reason=other:HasTag("structure") and "near_structure" or "near_combustible",
                blocker_prefab=other.prefab,blocker_guid=other.GUID,
                blocker_distance=math.sqrt((hazard.x-x)^2+(hazard.z-z)^2),clearance=radius}
        end
    end
    return true,{clearance=radius}
end

local function torch(inv,G)
    local e=inv and G.EQUIPSLOTS and inv:GetEquippedItem(G.EQUIPSLOTS.HANDS)
    return e and e:IsValid() and e.prefab=="torch" and e:HasTag("lighter") and e or nil
end

function M.scan(G,player,state,memory,cooking)
    local result={available=true,fixtures={},trees={},options={},meals=meal_options(G,cooking)}
    local inv,bag=inventory(player)
    if not inv then return result end
    local room=not has_hand_item(inv)
    local sources
    local function carried()
        if not sources then
            sources=slots(inv)
            for _,item in ipairs(sources) do item.source="inventory" end
            if bag then for _,item in ipairs(slots(bag)) do item.source="backpack"; sources[#sources+1]=item end end
        end
        return sources
    end
    local visible={}
    for _,building in ipairs((state.places or {}).buildings or {}) do
        if fixtures[building.prefab] and building.status=="observed" then
            local e=entity(G,building.guid)
            local c=e and e.replica and e.replica.container
            if c then visible[#visible+1]={building=building,e=e,c=c,d=distance(player,e)} end
        end
    end
    table.sort(visible,function(a,b) return a.d==b.d and a.e.GUID<b.e.GUID or a.d<b.d end)
    for i=1,math.min(8,#visible) do
        local v=visible[i]
        local building,e,c,d=v.building,v.e,v.c,v.d
        local status=phase(e,c,player)
        local row={building_id=building.id,guid=e.GUID,prefab=e.prefab,phase=status,
            distance=d,contents_known=status=="open" and not c:IsBusy()}
        result.fixtures[#result.fixtures+1]=row
        if row.contents_known then
            row.items=slots(c)
            row.contents_key=stamp(row.items)
        end
        if room and d<=RANGE then
            local opts=result.options
            if status=="closed" and c:CanBeOpened() then
                native_offer(opts,"open_container",row)
            elseif status=="ready" and not inv:IsFull() then
                native_offer(opts,"harvest_meal",row)
            elseif row.contents_known then
                for _,item in ipairs(row.items) do
                    if row.prefab=="treasurechest" and can_fit(inv,c:GetItemInSlot(item.slot)) then
                        native_offer(opts,"take_item",row,{slot=item.slot,item_guid=item.guid,product=item.prefab,
                            contents_key=row.contents_key},":"..item.slot)
                    end
                end
                for _,item in ipairs(carried()) do
                    local from=item.source=="inventory" and inv or bag
                    local inst=from:GetItemInSlot(item.slot)
                    if inst and can_fit(c,inst,row.prefab=="cookpot") then
                        native_offer(opts,"store_item",row,{slot=item.slot,item_guid=item.guid,source=item.source,
                            product=item.prefab,contents_key=row.contents_key},":"..item.source..":"..item.slot)
                    end
                end
                if row.prefab=="cookpot" and #row.items==4 then
                    local product,multiplier=M.predict(cooking,names(row.items))
                    if safe_meals[product] then
                        native_offer(opts,"start_meal",row,{product=product,contents_key=row.contents_key,
                            cook_seconds=seconds(G,multiplier)})
                    end
                end
            end
        end
    end
    local day=state.world.phase=="day" or state.world.phase=="dusk"
    local held=day and torch(inv,G)
    local carried_torch=day and not held and inv:FindItem(function(item) return item.prefab=="torch" end)
    local axe=day and (inv:FindItem(function(item) return item.prefab=="axe" end)
        or (G.EQUIPSLOTS and inv:GetEquippedItem(G.EQUIPSLOTS.HANDS)))
    local candidates={}
    for _,row in ipairs(state.nearby or {}) do
        if trees[row.prefab] then
            local e=entity(G,row.guid)
            if e then
                local burning=e:HasTag("fire") or e:HasTag("smolder")
                local burnt=e:HasTag("burnt")
                if (burning or burnt) and #result.trees<16 then
                    result.trees[#result.trees+1]={guid=row.guid,prefab=row.prefab,distance=row.distance,
                        status=burnt and "burnt" or "burning"}
                end
                if axe and axe.prefab=="axe" and burnt and not burning and row.collect_path and row.collect_path.status=="ready" then
                    result.options["chop_charcoal:"..row.guid]={kind="chop_nearest_tree",guid=row.guid,prefab=row.prefab,
                        tree_stage="burnt",charcoal_work=true,planner_only=true,description="Chop this burnt tree for charcoal."}
                end
                if (held or carried_torch) and row.distance<=IGNITION_RANGE and valid_tree(e)
                    and row.collect_path and row.collect_path.status=="ready" then
                    candidates[#candidates+1]={entity=e,row=row}
                end
            end
        end
    end
    if #candidates>0 and G.TheSim then
        -- One shared query covers every candidate instead of querying four
        -- nearest trees repeatedly and never reaching a safe fifth tree.
        local radius=0
        for _,candidate in ipairs(candidates) do
            radius=math.max(radius,ignition_radius(G,candidate.entity))
        end
        local px,_,pz=player.Transform:GetWorldPosition()
        local hazards=ignition_hazards(G,player,px,pz,IGNITION_RANGE+radius)
        local rejected={}
        for _,candidate in ipairs(candidates) do
            local row=candidate.row
            local safe,detail=ignition_safe(G,player,candidate.entity,memory,hazards)
            local observation={guid=row.guid,prefab=row.prefab,distance=row.distance,
                status=safe and "ignitable" or "blocked",reason=detail.reason,
                clearance=detail.clearance,blocker_prefab=detail.blocker_prefab,
                blocker_guid=detail.blocker_guid,blocker_distance=detail.blocker_distance}
            if safe then
                if #result.trees<16 then result.trees[#result.trees+1]=observation end
                if held then
                    result.options["ignite_tree:"..row.guid]={kind="ignite_tree",guid=row.guid,prefab=row.prefab,
                        torch_guid=held.GUID,planner_only=true,description="Light the isolated tree with the equipped torch."}
                else
                    result.options.equip_ignition_torch={kind="equip_torch",planner_only=true,
                        ignition_torch=true,torch_guid=carried_torch.GUID,
                        description="Equip a carried torch to light an isolated tree."}
                end
            else rejected[#rejected+1]=observation end
        end
        for _,observation in ipairs(rejected) do
            if #result.trees>=16 then break end
            result.trees[#result.trees+1]=observation
        end
    end
    return result
end

local function scene(G,player,pc,e,wanted)
    local picker=player.components and player.components.playeractionpicker
    if not picker then return false end
    local x,_,z=e.Transform:GetWorldPosition()
    -- The server recomputes this pair and removes duplicate right-click actions.
    -- A raw scene candidate is not necessarily a valid right-click action.
    local left,right=picker:DoGetMouseActions(G.Vector3(x,0,z),e)
    local action=left and left.action==wanted and left or right and right.action==wanted and right
    if not action then return false end
    local right_click=action==right
    if not pc.ismastersim then
        local function send()
            local platform,click_x,click_z=pc:GetPlatformRelativePosition(x,z)
            local relative=platform~=nil
            if right_click then
                G.SendRPCToServer(G.RPC.RightClick,wanted.code,click_x,click_z,e,nil,true,
                    pc:EncodeControlMods(),wanted.canforce,wanted.mod_name,platform,relative)
            else
                G.SendRPCToServer(G.RPC.LeftClick,wanted.code,click_x,click_z,e,true,
                    pc:EncodeControlMods(),wanted.canforce,wanted.mod_name,platform,relative)
            end
        end
        if pc.locomotor==nil then action.non_preview_cb=send else action.preview_cb=send end
    end
    pc:DoAction(action)
    return true
end

function M.issue(G,player,a,memory,cooking,safe)
    local pc=player.components and player.components.playercontroller
    local inv,bag=inventory(player)
    if not pc or not inv or pc:IsBusy() or has_hand_item(inv) then return false,"camp_interaction_busy" end
    local e=entity(G,a.guid)
    if not e or e.prefab~=a.prefab or distance(player,e)>RANGE then return false,"camp_target_changed_or_out_of_range" end
    if safe and not safe(e) then return false,"camp_interaction_path_unsafe" end
    if a.kind=="ignite_tree" then
        local held=torch(inv,G)
        local world=G.TheWorld and G.TheWorld.state
        if not held or held.GUID~=a.torch_guid or not world or (world.phase~="day" and world.phase~="dusk")
            or not ignition_safe(G,player,e,memory) then return false,"ignition_no_longer_safe" end
        local action=pc:GetItemUseAction(held,e)
        if not action or action.action~=G.ACTIONS.LIGHT then return false,"native_light_unavailable" end
        if pc.ismastersim then pc:DoAction(action) else pc:RemoteControllerUseItemOnSceneFromInvTile(action,held) end
        return true
    end
    if not fixtures[e.prefab] or places.id_for_guid(memory,e.GUID)~=a.building_id then
        return false,"camp_building_binding_changed"
    end
    local c=e.replica and e.replica.container
    if not c then return false,"container_unavailable" end
    local status=phase(e,c,player)
    if a.kind=="open_container" then
        if status=="closed" and c:CanBeOpened() and scene(G,player,pc,e,G.ACTIONS.RUMMAGE) then return true end
        return false,"native_open_unavailable"
    elseif a.kind=="harvest_meal" then
        if status=="ready" and not inv:IsFull() and scene(G,player,pc,e,G.ACTIONS.HARVEST) then return true end
        return false,"native_harvest_unavailable"
    end
    if status~="open" or c:IsBusy() then return false,"container_not_open_or_busy" end
    local items=slots(c)
    if stamp(items)~=a.contents_key then return false,"container_contents_changed" end
    if a.kind=="store_item" then
        local from=a.source=="inventory" and inv or a.source=="backpack" and bag
        local item=from and from:GetItemInSlot(a.slot)
        if not item or item.GUID~=a.item_guid or item.prefab~=a.product then return false,"inventory_slot_changed" end
        if not can_fit(c,item,e.prefab=="cookpot") then return false,"container_full_or_rejects_item" end
        from:MoveItemFromCountOfSlot(a.slot,e,1)
    elseif a.kind=="take_item" and e.prefab=="treasurechest" then
        local item=c:GetItemInSlot(a.slot)
        if not item or item.GUID~=a.item_guid or item.prefab~=a.product then return false,"container_slot_changed" end
        if not can_fit(inv,item) then return false,"inventory_full" end
        c:MoveItemFromCountOfSlot(a.slot,player,1)
    elseif a.kind=="start_meal" and e.prefab=="cookpot" then
        if M.predict(cooking,names(items))~=a.product then return false,"meal_prediction_changed" end
        local widget=c:GetWidget()
        local button=widget and widget.buttoninfo
        if not button or not button.validfn(e) then return false,"native_cook_unavailable" end
        button.fn(e,player)
    else return false,"unknown_camp_action" end
    return true
end

local function row_for(s,a)
    for _,row in ipairs((s.camp_work or {}).fixtures or {}) do
        if row.guid==a.guid and row.building_id==a.building_id then return row end
    end
end

local function count(items,product)
    local total=0
    for _,item in ipairs(items or {}) do if item.prefab==product then total=total+item.count end end
    return total
end

function M.verify(a,b,s)
    if a.kind=="ignite_tree" then
        for _,tree in ipairs((s.camp_work or {}).trees or {}) do
            if tree.guid==a.guid and (tree.status=="burning" or tree.status=="burnt") then return "tree_ignited" end
        end
        return
    end
    local row=row_for(s,a)
    if not row then return end
    local before=row_for(b,a)
    if a.kind=="open_container" and row.contents_known then return "container_contents_observed" end
    if a.kind=="start_meal" and before and before.contents_known
        and (row.phase=="unavailable" or row.phase=="ready") then return "pot_started" end
    if a.kind=="harvest_meal" and before and before.phase=="ready" and (row.phase=="closed" or row.phase=="open") then
        for product in pairs(safe_meals) do
            if ((s.recipe_inventory or {})[product] or 0)>((b.recipe_inventory or {})[product] or 0) then
                return "meal_harvested:"..product
            end
        end
    end
    if before and before.contents_known and row.contents_known and (a.kind=="store_item" or a.kind=="take_item") then
        local sign=a.kind=="store_item" and 1 or -1
        local delta=count(row.items,a.product)-count(before.items,a.product)
        local pack=((s.recipe_inventory or {})[a.product] or 0)-((b.recipe_inventory or {})[a.product] or 0)
        if delta==sign and pack==-sign then return "one_item_transferred" end
    end
end

function M.same(a,b)
    for _,key in ipairs({"item_guid","slot","source","contents_key","torch_guid","product","charcoal_work","ignition_torch"}) do
        if a[key]~=b[key] then return false end
    end
    return true
end

return M
