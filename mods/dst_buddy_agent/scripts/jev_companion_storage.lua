-- Local chest facts and explicit outgoing handoffs. Never remove owner items.
local M={}
M.ops={give_owner=true,drop_item=true,deposit=true,withdraw=true}
local CHESTS={treasurechest=true,dragonflychest=true}
local function guid(v) return type(v)=="number" and v>=1 and v<=9007199254740991 and v%1==0 end
local function size(item) return item.components.stackable and item.components.stackable:StackSize() or 1 end
local function transferable(item)
    local c=item and item.components or {}
    return item and item:IsValid() and c.inventoryitem and not c.inventoryitem.islockedinslot
        and not c.container and not c.itemmimic and not item:HasTag("heavy")
end
local function stores(inv)
    local result={inv}
    local overflow=inv.GetOverflowContainer and inv:GetOverflowContainer()
    if overflow then result[#result+1]=overflow end
    return result
end
local function rows(store) return store.itemslots or store.slots or {} end
local function count(store,prefab)
    local result=0
    for _,item in pairs(rows(store)) do if item:IsValid() and item.prefab==prefab then result=result+size(item) end end
    return result
end
local function inventory_count(inv,prefab)
    local result=0;for _,store in ipairs(stores(inv)) do result=result+count(store,prefab) end
    for _,item in pairs(inv.equipslots or {}) do if item:IsValid() and item.prefab==prefab then result=result+size(item) end end
    local active=inv.activeitem
    if active and active:IsValid() and active.prefab==prefab then result=result+size(active) end
    return result
end
local function source(stores_,prefab)
    for _,store in ipairs(stores_) do
        local maximum=store.maxslots or store.numslots or 0
        for slot=1,maximum do
            local item=rows(store)[slot]
            if item and item.prefab==prefab and transferable(item) then return store,item,slot end
        end
    end
end
local function destination(stores_,item,amount)
    amount=amount or 1
    -- Prefer compatible partial stacks, then empty slots, including a worn backpack.
    for _,empty in ipairs({false,true}) do for _,store in ipairs(stores_) do
        for slot=1,(store.maxslots or store.numslots or 0) do
            local other=rows(store)[slot]
            if store:CanTakeItemInSlot(item,slot) and ((empty and not other and (amount==1 or store.acceptsstacks~=false)) or (not empty and other
                and other.components.stackable and other.components.stackable:CanStackWith(item)
                and other.components.stackable:RoomLeft()>=amount)) then return store,slot end
        end
    end end
end
local function stow_site(a,item)
    local inv=a.components.inventory;local c=item and item.components or {}
    if not item or not item:IsValid() or not c.equippable or not inv:IsItemEquipped(item) then return nil,nil,"item_not_equipped" end
    if c.equippable:ShouldPreventUnequipping() then return nil,nil,"unequipping_prevented" end
    -- Backpacks cannot be stowed inside themselves, and may require a ground drop.
    if not transferable(item) or not c.inventoryitem.cangoincontainer then return nil,nil,"item_cannot_be_stowed" end
    local dest,slot=destination(stores(inv),item,size(item))
    return dest,slot,not dest and "inventory_full_or_rejected" or nil
end
function M.can_stow(a,item)
    local dest,_,reason=stow_site(a,item);return dest~=nil,reason
end
function M.stow(a,item)
    local dest,slot,reason=stow_site(a,item)
    if not dest then return false,reason end
    local inv=a.components.inventory;local amount=size(item);local before=0
    for _,store in ipairs(stores(inv)) do before=before+count(store,item.prefab) end
    local removed=inv:Unequip(item.components.equippable.equipslot)
    if removed~=item then return false,"unequip_rejected" end
    item.prevslot=nil;item.prevcontainer=nil
    dest:GiveItem(item,slot,nil,false)
    local after=0;for _,store in ipairs(stores(inv)) do after=after+count(store,item.prefab) end
    if not inv:IsItemEquipped(item) and after-before==amount then return true end
    if item:IsValid() then inv:Equip(item) end
    return false,"unequip_not_stowed"
end
function M.new(G,E)
    local S={}
    local function chest(a,id)
        local e=guid(id) and G.Ents[id]
        if not e or not e:IsValid() or not CHESTS[e.prefab] or e:HasTag("INLIMBO")
            or e:HasTag("burnt") or E.distance(a,e)>20 then return nil,"chest_unavailable" end
        local c=e.components.container
        if not c or c.readonlycontainer or c.canbeopened==false or c.CanBeOpened and not c:CanBeOpened()
            or e.components.burnable and e.components.burnable:IsBurning() then return nil,"chest_unavailable" end
        if c:IsOpenedByOthers(a) then return nil,"chest_busy" end
        return e
    end
    local function ledger(r,args,op)
        if type(args.task_key)~="string" or #args.task_key<1 or #args.task_key>64 or not args.task_key:match("^[%w_%-]+$")
            or type(args.prefab)~="string" or #args.prefab>60 or not args.prefab:match("^[a-z0-9_]+$")
            or type(args.total_count)~="number" or args.total_count<1 or args.total_count>100 or args.total_count%1~=0 then return nil,"invalid_transfer" end
        r.transfer_totals=r.transfer_totals or {};r.transfer_order=r.transfer_order or {}
        local row=r.transfer_totals[args.task_key]
        if row and (row.op~=op or row.prefab~=args.prefab or row.container_guid~=args.container_guid) then return nil,"transfer_identity_changed" end
        if not row then
            -- Do not evict receipts: forgetting one would permit replaying its quantity.
            if #r.transfer_order>=128 then return nil,"transfer_receipt_limit" end
            row={op=op,prefab=args.prefab,container_guid=args.container_guid,count=0}
            r.transfer_totals[args.task_key]=row
        end
        for i,key in ipairs(r.transfer_order) do if key==args.task_key then table.remove(r.transfer_order,i);break end end
        r.transfer_order[#r.transfer_order+1]=args.task_key
        return row
    end
    function S.start(a,q,r)
        local reason
        if q.args.container_guid~=nil and q.op~="deposit" and q.op~="withdraw" then return false,"invalid_transfer_target" end
        if q.op=="deposit" or q.op=="withdraw" then
            local e; e,reason=chest(a,q.args.container_guid)
            if not e then return false,reason end
            q.transfer_target=e
        elseif q.op=="give_owner" then
            if not r.player:IsValid() or E.dead(r.player) or E.distance(a,r.player)>20 then return false,"owner_out_of_range_or_dead" end
            q.transfer_target=r.player
        end
        local row;row,reason=ledger(r,q.args,q.op)
        if not row then return false,reason end
        q.receipt=row
        return true
    end
    function S.progress(a,q,r)
        local row=q.receipt
        local function data() return {task_key=q.args.task_key,count=row.count} end
        if row.count>=q.args.total_count then return "completed","transfer_total_already_met",data() end
        if a.sg and (a.sg:HasStateTag("busy") or a.sg:HasStateTag("nointerrupt")) then return end
        local target=q.transfer_target
        if q.op=="give_owner" then
            if not target:IsValid() or target~=r.player or E.dead(target) then return "failed","owner_unavailable",data() end
        elseif target then
            local e,reason=chest(a,q.args.container_guid)
            if not e or e~=target then return "failed",reason or "chest_changed",data() end
        end
        if target and E.distance(a,target)>3 then
            if not q.next_walk or E.now()>=q.next_walk then
                q.next_walk=E.now()+1
                if E.follow(a,target,false)==false then return "failed","transfer_path_rejected",data() end
            end
            return
        end
        if target then E.stop(a) end
        local inv=a.components.inventory
        local src,item,srcslot=source(q.op=="withdraw" and {target.components.container} or stores(inv),q.args.prefab)
        if not item then return "failed","transfer_source_missing",data() end
        local before=inventory_count(inv,q.args.prefab)
        if q.op=="give_owner" then
            local receiver=target.components.inventory
            if not receiver or not target.components.trader or receiver:CanAcceptCount(item,1)<1 then return "failed","recipient_full_or_unavailable",data() end
            local owner_before=inventory_count(receiver,q.args.prefab)
            local accepted=G.ACTIONS.GIVETOPLAYER.fn({doer=a,target=target,invobject=item})
            if not accepted or before-inventory_count(inv,q.args.prefab)~=1 or inventory_count(receiver,q.args.prefab)-owner_before~=1 then
                return "failed","give_not_confirmed",data()
            end
        elseif q.op=="drop_item" then
            local x,z=E.position(a)
            if not E.valid_point(a,x,z,0) then return "failed","unsafe_drop_position",data() end
            local dropped=inv:DropItem(item,false,false,G.Vector3(x,0,z),true)
            if not dropped or not dropped:IsValid() or dropped.prefab~=q.args.prefab
                or dropped.components.inventoryitem.owner~=nil or before-inventory_count(inv,q.args.prefab)~=1 then return "failed","drop_not_confirmed",data() end
        else
            local c=target.components.container
            local dest,slot=destination(q.op=="deposit" and {c} or stores(inv),item)
            if not dest then return "failed","destination_full_or_rejected",data() end
            local chest_before=count(c,q.args.prefab)
            local was_open=c:IsOpenedBy(a)
            if not was_open then c:Open(a) end
            if not c:IsOpenedBy(a) then return "failed","chest_open_rejected",data() end
            local part=src:RemoveItem(item,false)
            if part then
                part.prevslot=nil;part.prevcontainer=nil
                dest:GiveItem(part,slot,nil,false)
                -- Native containers can reject; restore an unaccepted unit to its source.
                if part:IsValid() and part.components.inventoryitem.owner==nil then
                    part.prevslot=nil;part.prevcontainer=nil;src:GiveItem(part,srcslot,nil,false)
                end
            end
            if not was_open then c:Close(a) end
            local direction=q.op=="deposit" and 1 or -1
            if not part or direction*(before-inventory_count(inv,q.args.prefab))~=1
                or direction*(count(c,q.args.prefab)-chest_before)~=1 then return "failed","storage_transfer_not_confirmed",data() end
        end
        row.count=row.count+1
        return "completed",q.op=="drop_item" and "one_item_dropped" or "one_item_transferred",data()
    end
    function S.snapshot(a,entities,r)
        local containers={}
        local candidates={}
        for _,e in ipairs(entities) do
            if e:IsValid() and CHESTS[e.prefab] and not e:HasTag("INLIMBO") then candidates[#candidates+1]=e end
        end
        table.sort(candidates,function(l,b) return E.distance(a,l)<E.distance(a,b) end)
        for i=1,math.min(8,#candidates) do
            local e=candidates[i];local live,reason=chest(a,e.GUID);local x,z=E.position(e)
            local row={guid=e.GUID,prefab=e.prefab,x=x,z=z,distance=E.distance(a,e),contents_known=live~=nil,reason=reason,items={}}
            if reason=="chest_busy" then
                row.busy_openers={}
                local c=e.components.container
                for _,opener in ipairs(c.GetOpeners and c:GetOpeners() or {}) do
                    if opener~=a and opener:IsValid() and #row.busy_openers<8 then
                        row.busy_openers[#row.busy_openers+1]={guid=opener.GUID,is_owner=opener==r.player}
                    end
                end
                row.opener_count=c.opencount
            end
            if live then
                local c=e.components.container;row.slot_count=c.numslots;row.free_slots=c.numslots
                for slot=1,c.numslots do
                    local item=c.slots[slot]
                    if item and item:IsValid() then
                        row.free_slots=row.free_slots-1
                        if #row.items<40 then row.items[#row.items+1]={slot=slot,guid=item.GUID,prefab=item.prefab,count=size(item)} end
                    end
                end
                row.items_complete=c.numslots<=40
            end
            containers[#containers+1]=row
        end
        local receipts={}
        local order=r.transfer_order or {}
        for i=math.max(1,#order-31),#order do
            local key=order[i];receipts[#receipts+1]={task_key=key,count=r.transfer_totals[key].count}
        end
        return {containers=containers,transfer_receipts=receipts}
    end
    return S
end
return M
