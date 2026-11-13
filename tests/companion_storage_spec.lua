package.path="./mods/dst_buddy_agent/scripts/?.lua;"..package.path
local Storage=require("jev_companion_storage")
local Server=require("jev_companion_server")
local passed=0
local function test(name,fn) local ok,err=pcall(fn);assert(ok,name..": "..tostring(err));passed=passed+1;print("ok "..passed.." - "..name) end
local function fixture()
    local w={ents={},time=0,serial=1000,stops=0}
    function w:entity(id,prefab,x)
        local e={GUID=id,prefab=prefab,x=x or 0,z=0,components={},tags={},valid=true}
        function e:IsValid() return self.valid end
        function e:HasTag(tag) return self.tags[tag] or false end
        self.ents[id]=e;return e
    end
    function w:item(prefab,n,skin)
        self.serial=self.serial+1;local item=self:entity(self.serial,prefab)
        item.components.inventoryitem={owner=nil,cangoincontainer=true};item.skin=skin or "normal"
        if n then
            item.n=n
            item.components.stackable={StackSize=function() return item.n end,RoomLeft=function() return 40-item.n end,
                CanStackWith=function(_,other) return item.prefab==other.prefab and item.skin==other.skin end}
        end
        return item
    end
    function w:inventory(e,slots,iscontainer)
        local c={inst=e,maxslots=not iscontainer and slots or nil,numslots=iscontainer and slots or nil,
            itemslots=not iscontainer and {} or nil,slots=iscontainer and {} or nil,opened={},isopen=false}
        local function items() return c.itemslots or c.slots end
        function c:GetOverflowContainer() return self.overflow end
        function c:IsItemEquipped(item) for slot,equipped in pairs(self.equipslots or {}) do if equipped==item then return slot end end end
        function c:Unequip(slot) local item=self.equipslots[slot];self.equipslots[slot]=nil;return item end
        function c:Equip(item) self.equipslots=self.equipslots or {};self.equipslots[item.components.equippable.equipslot]=item end
        function c:CanTakeItemInSlot(item,slot) return not self.reject and item.components.inventoryitem.cangoincontainer end
        function c:IsOpenedBy(actor) return iscontainer and self.opened[actor]==true or not iscontainer and self.isopen and actor==e end
        function c:IsOpenedByOthers(actor) for who in pairs(self.opened) do if who~=actor then return true end end;return false end
        function c:GetOpeners() local result={};for who in pairs(self.opened) do result[#result+1]=who end;return result end
        function c:Open(actor) if iscontainer then self.opened[actor]=true else self.isopen=true end end
        function c:Close(actor) if iscontainer then self.opened[actor]=nil else self.isopen=false end end
        function c:GiveItem(item,slot)
            if self.reject then return false end
            local selected=slot
            if not selected then
                for i=1,slots do local other=items()[i]
                    if other and other.components.stackable and other.components.stackable:CanStackWith(item) and other.n<40 then selected=i;break end
                end
                if not selected then for i=1,slots do if not items()[i] then selected=i;break end end end
            end
            if not selected then return false end
            local old=items()[selected]
            if old then
                if not old.components.stackable or not old.components.stackable:CanStackWith(item) or old.n+ (item.n or 1)>40 then return false end
                old.n=old.n+(item.n or 1);item.valid=false
            else items()[selected]=item;item.components.inventoryitem.owner=e end
            return true
        end
        function c:RemoveItem(item,whole)
            for slot,old in pairs(items()) do if old==item then
                if not whole and item.n and item.n>1 then
                    item.n=item.n-1;return w:item(item.prefab,1,item.skin)
                end
                items()[slot]=nil;item.components.inventoryitem.owner=nil;return item
            end end
        end
        function c:CanAcceptCount(item)
            if self.reject then return 0 end
            for i=1,slots do local old=items()[i];if not old or old.n and old.n<40 and old.components.stackable:CanStackWith(item) then return 1 end end
            if self.overflow then return self.overflow:CanAcceptCount(item) end
            return 0
        end
        function c:DropItem(item,whole)
            local part=self:RemoveItem(item,whole);if part then part.x=e.x;w.last_drop=part end;return part
        end
        e.components[iscontainer and "container" or "inventory"]=c;return c
    end
    w.actor=w:entity(1,"wilson");w.owner=w:entity(2,"wendy");w.owner.userid="owner"
    w.actor.components.inventory=w:inventory(w.actor,3);w.actor.components.trader={}
    w.owner.components.inventory=w:inventory(w.owner,3);w.owner.components.inventory:Open();w.owner.components.trader={}
    w.chest=w:entity(3,"treasurechest",2);w:inventory(w.chest,3,true)
    w.env={now=function() return w.time end,position=function(e) return e.x,e.z end,distance=function(a,b) return math.abs(a.x-b.x) end,
        dead=function(e) return e.dead==true end,stop=function() w.stops=w.stops+1 end,valid_point=function() return true end,
        follow=function(_,e) w.walk_target=e;return true end}
    w.G={Ents=w.ents,Vector3=function(x,y,z) return {x=x,y=y,z=z} end,ACTIONS={GIVETOPLAYER={fn=function(act)
        local inv=act.target.components.inventory
        if not inv:IsOpenedBy(act.target) or inv:CanAcceptCount(act.invobject)<1 then return false end
        local item=act.doer.components.inventory:RemoveItem(act.invobject,false)
        if not item then local over=act.doer.components.inventory:GetOverflowContainer();item=over and over:RemoveItem(act.invobject,false) end
        return item and inv:GiveItem(item) or false
    end}}}
    w.storage=Storage.new(w.G,w.env);w.r={player=w.owner}
    function w:request(op,extra)
        local args={task_key="task",prefab="cutgrass",total_count=3};for k,v in pairs(extra or {}) do args[k]=v end
        if op=="deposit" or op=="withdraw" then args.container_guid=args.container_guid or self.chest.GUID end
        return {op=op,args=args}
    end
    function w:step(op,extra)
        local q=self:request(op,extra);local ok,why=self.storage.start(self.actor,q,self.r)
        if not ok then return "rejected",why end
        return self.storage.progress(self.actor,q,self.r)
    end
    return w
end
test("local closed chest slots are facts, busy contents are unknown, other containers excluded",function()
    local w=fixture();w.chest.components.container:GiveItem(w:item("cutgrass",5),1)
    local pot=w:entity(8,"cookpot");w:inventory(pot,4,true)
    local data=w.storage.snapshot(w.actor,{w.chest,pot},w.r)
    assert(#data.containers==1 and data.containers[1].contents_known and data.containers[1].items[1].count==5)
    w.chest.components.container:Open(w.owner)
    data=w.storage.snapshot(w.actor,{w.chest},w.r)
    assert(not data.containers[1].contents_known and #data.containers[1].items==0 and data.containers[1].reason=="chest_busy")
    assert(data.containers[1].busy_openers[1].guid==w.owner.GUID and data.containers[1].busy_openers[1].is_owner)
    assert(w:step("withdraw")=="rejected")
end)
test("closing the actual opener restores availability and does not leave a companion lock",function()
    local w=fixture();w.actor.components.inventory:GiveItem(w:item("cutgrass",3),1)
    w.chest.components.container:Open(w.owner)
    assert(w:step("deposit")=="rejected")
    w.chest.components.container:Close(w.owner)
    assert(w.storage.snapshot(w.actor,{w.chest},w.r).containers[1].contents_known)
    assert(w:step("deposit")=="completed")
    assert(not w.chest.components.container:IsOpenedBy(w.actor))
    assert(#w.chest.components.container:GetOpeners()==0)
end)
test("deposit counts exactly one unit and retrying a completed total cannot duplicate it",function()
    local w=fixture();w.actor.components.inventory:GiveItem(w:item("cutgrass",5),1)
    for i=1,3 do local status,reason,data=w:step("deposit");assert(status=="completed" and data.count==i) end
    assert(w.actor.components.inventory.itemslots[1].n==2 and w.chest.components.container.slots[1].n==3)
    assert(not w.chest.components.container:IsOpenedBy(w.actor))
    local _,reason,data=w:step("deposit");assert(reason=="transfer_total_already_met" and data.count==3)
    assert(w.chest.components.container.slots[1].n==3)
end)
test("full and item-restricted chest destinations leave source intact",function()
    local w=fixture();local item=w:item("cutgrass",5);w.actor.components.inventory:GiveItem(item,1)
    for i=1,3 do w.chest.components.container:GiveItem(w:item("rocks",40),i) end
    assert(w:step("deposit")=="failed" and item.n==5 and w.r.transfer_totals.task.count==0)
    w.chest.components.container.slots={};w.chest.components.container.reject=true
    assert(w:step("deposit")=="failed" and item.n==5)
end)
test("withdraw preserves stack metadata and only counts real recipient increments",function()
    local w=fixture();local stack=w:item("cutgrass",5,"special");w.chest.components.container:GiveItem(stack,1)
    assert(w:step("withdraw")=="completed")
    assert(stack.n==4 and w.actor.components.inventory.itemslots[1].skin=="special")
    assert(w.actor.components.inventory.itemslots[1].n==1)
    w.actor.components.inventory.reject=true
    assert(w:step("withdraw")=="failed" and stack.n==4)
end)
test("rejected native insertion restores the detached unit",function()
    local w=fixture();local item=w:item("cutgrass",5);w.actor.components.inventory:GiveItem(item,1)
    w.chest.components.container.GiveItem=function() return false end
    assert(w:step("deposit")=="failed" and item.n==5 and w.r.transfer_totals.task.count==0)
end)
test("give only targets the RPC owner, never reads or removes owner source items",function()
    local w=fixture();local owned=w:item("cutgrass",5);w.actor.components.inventory:GiveItem(owned,1)
    w.owner.components.inventory:GiveItem(w:item("cutgrass",7),1)
    local stranger=w:entity(7,"wendy");w:inventory(stranger,3)
    assert(w:step("give_owner",{owner_guid=stranger.GUID})=="completed")
    assert(owned.n==4 and w.owner.components.inventory.itemslots[1].n==8 and not stranger.components.inventory.itemslots[1])
    for i=1,3 do w.owner.components.inventory.itemslots[i]=w:item("rocks",40) end
    assert(w:step("give_owner")=="failed" and owned.n==4)
end)
test("dropping is a ground result, not a player receipt, and only drops requested total",function()
    local w=fixture();local stack=w:item("cutgrass",5);w.actor.components.inventory:GiveItem(stack,1)
    local status,reason,data=w:step("drop_item",{total_count=1})
    assert(status=="completed" and reason=="one_item_dropped" and data.count==1)
    assert(stack.n==4 and w.last_drop.components.inventoryitem.owner==nil and not w.owner.components.inventory.itemslots[1])
    w:step("drop_item",{total_count=1});assert(stack.n==4)
end)
test("equipped, cursor-held, locked and nested container items are not outgoing sources",function()
    local w=fixture();local inv=w.actor.components.inventory
    inv.equipslots={hands=w:item("cutgrass",5)};inv.activeitem=w:item("cutgrass",5)
    assert(w:step("give_owner")=="failed")
    local item=w:item("cutgrass",5);item.components.inventoryitem.islockedinslot=true;inv:GiveItem(item,1)
    assert(w:step("give_owner")=="failed" and item.n==5)
    item.components.inventoryitem.islockedinslot=nil;item.components.container={}
    assert(w:step("give_owner")=="failed" and item.n==5)
end)
test("transfer walks into real range and rechecks a moving owner before touching items",function()
    local w=fixture();w.owner.x=10;local item=w:item("cutgrass",5);w.actor.components.inventory:GiveItem(item,1)
    local q=w:request("give_owner");assert(w.storage.start(w.actor,q,w.r))
    assert(not w.storage.progress(w.actor,q,w.r) and w.walk_target==w.owner and item.n==5)
    w.owner.x=12;w.time=1;assert(not w.storage.progress(w.actor,q,w.r) and item.n==5)
    w.actor.x=10;assert(w.storage.progress(w.actor,q,w.r)=="completed" and item.n==4)
end)
test("removed/burnt/distant/wrong-type chests and changed receipt identity are rejected",function()
    local w=fixture();local item=w:item("cutgrass",5);w.actor.components.inventory:GiveItem(item,1)
    for _,id in ipairs({-1,1.5,9007199254740992,999}) do assert(w:step("deposit",{container_guid=id})=="rejected") end
    w.chest.x=21;assert(w:step("deposit")=="rejected");w.chest.x=2
    w.chest.tags.burnt=true;assert(w:step("deposit")=="rejected");w.chest.tags.burnt=nil
    assert(w:step("deposit",{task_key="valid"})=="completed")
    assert(w:step("drop_item",{task_key="valid"})=="rejected" and item.n==4)
end)
test("main inventory and worn backpack are supported without opening arbitrary sources",function()
    local w=fixture();local bag=w:entity(8,"backpack");local over=w:inventory(bag,2,true)
    w.actor.components.inventory.overflow=over;over:GiveItem(w:item("cutgrass",3),1)
    assert(w:step("deposit")=="completed" and over.slots[1].n==2)
    for i=1,3 do w.actor.components.inventory:GiveItem(w:item("rocks",40),i) end
    assert(w:step("withdraw",{task_key="take"})=="completed" and over.slots[1].n==3)
end)
test("stowing into a worn backpack preserves the same tool before handing it to the owner",function()
    local w=fixture();local inv=w.actor.components.inventory
    for i=1,3 do inv:GiveItem(w:item("rocks",40),i) end
    local bag=w:entity(8,"backpack");local over=w:inventory(bag,2,true);inv.overflow=over
    local tool=w:item("axe");tool.components.equippable={equipslot="hands",ShouldPreventUnequipping=function() return false end};inv:Equip(tool)
    assert(Storage.can_stow(w.actor,tool))
    assert(Storage.stow(w.actor,tool) and over.slots[1]==tool and not inv:IsItemEquipped(tool))
    -- Use a main slot for the native-give mock; the real game resolves the worn bag itself.
    inv.itemslots[1]=nil;over:RemoveItem(tool,true);inv:GiveItem(tool,1)
    assert(w:step("give_owner",{prefab="axe",total_count=1})=="completed")
    assert(w.owner.components.inventory.itemslots[1]==tool)
end)
test("unequip refuses backpack self-storage and rolls a rejected insertion back to equipped",function()
    local w=fixture();local inv=w.actor.components.inventory
    local tool=w:item("axe");tool.components.equippable={equipslot="hands",ShouldPreventUnequipping=function() return false end};inv:Equip(tool)
    tool.components.container={};assert(not Storage.stow(w.actor,tool) and inv:IsItemEquipped(tool))
    tool.components.container=nil;inv.GiveItem=function() return false end
    assert(not Storage.stow(w.actor,tool) and inv:IsItemEquipped(tool))
end)
test("receipt history is bounded on the wire and old totals cannot be evicted and replayed",function()
    local w=fixture()
    for i=1,128 do
        local q=w:request("drop_item",{task_key="k"..i});assert(w.storage.start(w.actor,q,w.r))
    end
    assert(#w.storage.snapshot(w.actor,{},w.r).transfer_receipts==32)
    assert(not w.storage.start(w.actor,w:request("drop_item",{task_key="overflow"}),w.r))
    assert(w.storage.start(w.actor,w:request("drop_item",{task_key="k1"}),w.r))
    local data=w.storage.snapshot(w.actor,{},w.r);assert(data.transfer_receipts[#data.transfer_receipts].task_key=="k1")
end)
test("new intent interrupts an approach before any inventory mutation; cached request cannot replay",function()
    local w=fixture();w.actor.components.inventory:GiveItem(w:item("cutgrass",5),1);w.owner.x=10
    local replies={};local env=w.env
    env.find_buddy=function() return w.actor end;env.snapshot=function() return w.storage.snapshot(w.actor,{w.chest},w.r) end
    env.reply=function(_,p) replies[#replies+1]=p end;env.begin=w.storage.start;env.progress=w.storage.progress
    local server=Server.new(env)
    server:request(w.owner,{id="c:1",seq=1,op="summon"});local token=replies[#replies].token
    local q=w:request("give_owner");q.id="c:2";q.seq=2;q.token=token;q.revision=0
    server:request(w.owner,q);server:tick();assert(w.walk_target==w.owner)
    server:request(w.owner,{id="c:3",seq=3,op="intent",token=token,revision=1})
    w.actor.x=10;server:tick();server:request(w.owner,q)
    assert(q.last.status=="interrupted" and w.actor.components.inventory.itemslots[1].n==5)
end)
test("binding opens native player receiving inventory and release restores its previous closed state",function()
    local w=fixture();local inv=w.actor.components.inventory
    local E=require("jev_companion_world").new({},function() end,function() end)
    E.activate(w.actor);assert(inv:IsOpenedBy(w.actor))
    E.deactivate(w.actor);assert(not inv:IsOpenedBy(w.actor))
    inv:Open();E.activate(w.actor);E.deactivate(w.actor);assert(inv:IsOpenedBy(w.actor))
end)
print("1.."..passed)
