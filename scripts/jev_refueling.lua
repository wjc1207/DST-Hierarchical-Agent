-- Bounded, explicit one-item ADDFUEL/ADDWETFUEL using the native controller.
local M={}
local thermal=require("jev_thermal")
local places=require("jev_places")
local function identity(G,guid) return places.id_for_guid(G.TheWorld and G.TheWorld._jev_places,guid) end
M.fuels={log=true,charcoal=true,pinecone=true,acorn=true,twiggy_nut=true,twigs=true,cutgrass=true}
local function fire(entity)
    return entity and entity:IsValid() and (entity.prefab=="campfire" or entity.prefab=="firepit")
end
local function inventory_items(player)
    local inv=player.replica and player.replica.inventory
    local items={}
    if inv then
        for _,item in pairs(inv:GetItems() or {}) do items[#items+1]=item end
        local bag=inv.GetOverflowContainer and inv:GetOverflowContainer()
        if bag then for _,item in pairs(bag:GetItems() or {}) do items[#items+1]=item end end
    end
    table.sort(items,function(a,b) return a.GUID<b.GUID end)
    return items
end
local function native(G,pc,item,target)
    local action=pc:GetItemUseAction(item,target)
    if action and action.target==target and action.invobject==item
        and (action.action==G.ACTIONS.ADDFUEL or action.action==G.ACTIONS.ADDWETFUEL) then return action end
end
function M.scan(G,player,nearby,safe)
    local pc=player.components and player.components.playercontroller
    local result={available=pc~=nil and pc.GetItemUseAction~=nil,options={},targets={},ratio_available=true}
    if not result.available then return result end
    local facts_by_guid={}
    for _,row in ipairs(nearby or {}) do
        local target=G.Ents and G.Ents[row.guid]
        if fire(target) and identity(G,target.GUID) then
            local facts=row.lit~=nil and {lit=row.lit,fire_section=row.fire_section,
                fuel_percent=row.fuel_percent,fire_data_source=row.fire_data_source,
                fuel_min=row.fuel_min,fuel_max=row.fuel_max,fuel_estimated=row.fuel_estimated,
                fuel_lower_inclusive=row.fuel_lower_inclusive,fuel_upper_inclusive=row.fuel_upper_inclusive} or thermal.fire(G,target)
            facts_by_guid[target.GUID]=facts
            facts.building_id=identity(G,target.GUID)
            facts.guid=target.GUID
            facts.reason=thermal.fuel_reason(G,player,target,facts) or ""
            facts.ratio_reason=thermal.fuel_reason(G,player,target,facts,true) or ""
            result.targets[#result.targets+1]=facts
        end
    end
    if pc:IsBusy() then return result end
    local items=inventory_items(player)
    local targets=0
    for _,row in ipairs(nearby or {}) do
        local target=G.Ents and G.Ents[row.guid]
        local facts=target and facts_by_guid[target.GUID]
        local legacy=facts and facts.reason==""
        local ratio=facts and facts.ratio_reason==""
        if row.distance<=3 and fire(target) and safe(target) and (legacy or ratio) then
            targets=targets+1
            if targets>4 then break end
            local seen={}
            for _,item in ipairs(items) do
                if item:IsValid() and M.fuels[item.prefab] and not seen[item.prefab] then
                    local action=native(G,pc,item,target)
                    if action then
                        seen[item.prefab]=true
                        local offered={kind="add_fuel",guid=target.GUID,prefab=target.prefab,
                            building_id=facts.building_id,fuel_prefab=item.prefab,fuel_guid=item.GUID,
                            native_action=action.action.id,planner_only=true,
                            description="Add ONE "..item.prefab.." to "..target.prefab.." "..tostring(target.GUID).."."}
                        if legacy then result.options[#result.options+1]=offered end
                        if ratio then
                            local copy={};for key,value in pairs(offered) do copy[key]=value end
                            copy.ratio_refuel=true;result.options[#result.options+1]=copy
                        end
                    end
                end
            end
        end
    end
    return result
end
function M.key(action) return (action.ratio_refuel and "add_fuel_ratio:" or "add_fuel:")..tostring(action.guid)..":"..action.fuel_prefab end
function M.actions(s)
    local result={}
    for _,action in ipairs((s.refueling or {}).options or {}) do result[M.key(action)]=action end
    return result
end
function M.issue(G,player,action,safe)
    local pc=player.components and player.components.playercontroller
    local target=G.Ents[action.guid]
    if not pc or pc:IsBusy() then return false,"refuel_controller_busy" end
    if not fire(target) or target.prefab~=action.prefab or not safe(target)
        or action.building_id~=identity(G,target.GUID) then return false,"refuel_target_changed" end
    local heat_reason=thermal.fuel_reason(G,player,target,nil,action.ratio_refuel)
    if heat_reason then return false,heat_reason end
    local item
    for _,candidate in ipairs(inventory_items(player)) do
        if candidate.GUID==action.fuel_guid and candidate.prefab==action.fuel_prefab and candidate:IsValid() then item=candidate;break end
    end
    if not item or not M.fuels[item.prefab] then return false,"refuel_item_changed" end
    local buffered=native(G,pc,item,target)
    if not buffered or buffered.action.id~=action.native_action then return false,"refuel_native_action_unavailable" end
    require("jev_execution").request(player,buffered,G.GetTime())
    if pc.ismastersim then pc:DoAction(buffered)
    else pc:RemoteControllerUseItemOnSceneFromInvTile(buffered,item) end
    player._jev_refuel_after=player._jev_refuel_after or {}
    player._jev_refuel_after[target.GUID]=G.GetTime()+2
    return true
end
return M
