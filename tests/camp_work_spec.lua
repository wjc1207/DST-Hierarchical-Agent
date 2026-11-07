package.path="./mods/jev_dst_agent/scripts/?.lua;"..package.path
local camp=require("jev_camp_work")
local count=0
local function test(name,fn)
    local ok,err=pcall(fn); assert(ok,name..": "..tostring(err))
    count=count+1; print("ok "..count.." - "..name)
end

local function fixture()
    local function ent(guid,prefab,tags)
        local e={GUID=guid,prefab=prefab,tags=tags or {},replica={}}
        e.IsValid=function() return true end
        e.HasTag=function(self,tag) return self.tags[tag]==true end
        e.Transform={GetWorldPosition=function() return 2,0,0 end}
        return e
    end
    local function items(n)
        local c={items={},opened=false,openable=true}
        function c:GetItems() return self.items end
        function c:GetItemInSlot(i) return self.items[i] end
        function c:GetNumSlots() return n end
        function c:CanTakeItemInSlot() return true end
        function c:CanBeOpened() return self.openable end
        function c:IsOpenedBy() return self.opened end
        function c:IsBusy() return not self.opened end
        function c:IsFull() return false end
        function c:MoveItemFromCountOfSlot(slot,dest,amount) self.sent={slot,dest,amount} end
        return c
    end
    local p,box,pot,tree=ent(1,"wilson"),ent(2,"treasurechest"),ent(3,"cookpot"),
        ent(4,"evergreen",{CHOP_workable=true,canlight=true})
    function p:GetDistanceSqToInst() return 4 end
    local inv,c,k=items(15),items(9),items(4)
    p.replica.inventory,box.replica.container,pot.replica.container=inv,c,k
    function inv:GetActiveItem() end
    function inv:GetOverflowContainer() end
    function inv:GetEquippedItem() return self.held end
    function inv:FindItem(fn)
        for _,i in pairs(self.items) do if fn(i) then return i end end
    end
    local torch=ent(6,"torch",{lighter=true}); inv.held=torch
    local pc={ismastersim=true}
    function pc:IsBusy() return false end
    function pc:DoAction(a) self.sent=a end
    local G={Ents={[2]=box,[3]=pot,[4]=tree},EQUIPSLOTS={HANDS=1},
        ACTIONS={RUMMAGE={},HARVEST={},LIGHT={}},TheWorld={state={phase="day"}},
        Vector3=function(x,y,z) return {x=x,y=y,z=z} end}
    local hazard
    G.TheSim={FindEntities=function() return {p,tree,hazard} end}
    local function set_hazard() hazard=ent(8,"structure",{structure=true}) end
    p.components={playercontroller=pc,playeractionpicker={GetSceneActions=function(_,e,right)
        return right and {{action=e:HasTag("donecooking") and G.ACTIONS.HARVEST or G.ACTIONS.RUMMAGE}} or {}
    end}}
    p.components.playeractionpicker.DoGetMouseActions=function(_,position,e)
        return {action=e:HasTag("donecooking") and G.ACTIONS.HARVEST or G.ACTIONS.RUMMAGE,target=e}
    end
    function pc:GetItemUseAction() return {action=G.ACTIONS.LIGHT} end
    function k:GetWidget() return {buttoninfo={validfn=function() return true end,fn=function() self.started=true end}} end
    local cooking={ingredients={meat={tags={meat=1}},berries={tags={fruit=.5}}},
        recipes={cookpot={meatballs={priority=0,test=function(_,_,tags) return tags.meat end}}}}
    local s={world={phase="day"},nearby={{guid=4,prefab="evergreen",distance=2,collect_path={status="ready"}}},
        places={buildings={{id="building:2",guid=2,prefab="treasurechest",status="observed"},
                           {id="building:3",guid=3,prefab="cookpot",status="observed"}}}}
    local function scan(pack,memory)
        local state={camp_work=camp.scan(G,p,s,memory,cooking),recipe_inventory=pack or {}}
        return state,state.camp_work.options
    end
    local function issue(action) return camp.issue(G,p,action,nil,cooking,function() return true end) end
    return inv,c,k,pc,G,tree,ent,scan,issue,set_hazard,pot,p,s
end

test("closed chest is unknown; reject a stale open slot and require both transfer deltas",function()
    local inv,c,_,pc,G,_,ent,scan,issue=fixture()
    inv.items[1]=ent(10,"log")
    local b,actions=scan({log=2})
    assert(not b.camp_work.fixtures[1].contents_known)
    assert(issue(actions["open_container:2"]) and pc.sent.action==G.ACTIONS.RUMMAGE)
    c.opened=true
    b,actions=scan({log=2})
    local deposit=actions["store_item:2:inventory:1"]
    c.items[2]=ent(12,"rocks")
    local ok,reason=issue(deposit)
    assert(not ok and reason=="container_contents_changed" and not inv.sent)
    c.items[2]=nil
    assert(issue(deposit) and inv.sent[2]==G.Ents[2] and inv.sent[3]==1)
    c.items[1]=ent(11,"log")
    assert(not camp.verify(deposit,b,scan({log=2})))
    assert(camp.verify(deposit,b,scan({log=1}))=="one_item_transferred")
    b,actions=scan({log=1})
    local take=actions["take_item:2:1"]
    assert(issue(take) and c.sent[2].GUID==1 and c.sent[3]==1)
    c.items[1]=nil
    assert(camp.verify(take,b,scan({log=2}))=="one_item_transferred")
end)

test("start the four-slot recipe; confirm harvest only once a dish enters inventory",function()
    local _,_,k,pc,G,_,ent,scan,issue,_,pot=fixture()
    k.opened=true
    for i,name in ipairs({"meat","berries","berries","berries"}) do k.items[i]=ent(i+10,name) end
    local b,actions=scan()
    local start=actions["start_meal:3"]
    assert(start.product=="meatballs" and issue(start) and k.started)
    k.opened,k.openable,k.items=false,false,{}
    assert(camp.verify(start,b,scan())=="pot_started")
    pot.tags.donecooking=true
    b,actions=scan()
    local harvest=actions["harvest_meal:3"]
    assert(issue(harvest) and pc.sent.action==G.ACTIONS.HARVEST)
    pot.tags.donecooking=nil
    k.openable=true
    assert(not camp.verify(harvest,b,scan()))
    assert(camp.verify(harvest,b,scan({meatballs=1}))=="meal_harvested:meatballs")
end)

test("remote chest and harvest clicks include the required platform flag",function()
    local _,_,_,pc,G,_,_,scan,issue,_,pot,player=fixture()
    pc.ismastersim=false
    pc.locomotor={}
    G.RPC={RightClick=1,LeftClick=2}
    G.ACTIONS.RUMMAGE.code,G.ACTIONS.HARVEST.code=10,20
    function pc:EncodeControlMods() return 0 end
    local platform,call
    function pc:GetPlatformRelativePosition(x,z)
        if platform then return platform,x+3,z+4 end
        return nil,x,z
    end
    G.SendRPCToServer=function(...) call={...} end
    local _,actions=scan()
    assert(issue(actions["open_container:2"]))
    assert(pc.sent.preview_cb and not pc.sent.non_preview_cb)
    pc.sent.preview_cb()
    assert(call[1]==G.RPC.LeftClick and call[2]==10 and call[5]==G.Ents[2])
    assert(call[6]==true and call[10]==nil and call[11]==false)
    player.components.playeractionpicker.DoGetMouseActions=function()
        return nil,{action=G.ACTIONS.HARVEST}
    end
    pc.locomotor=nil
    pot.tags.donecooking=true
    platform={}
    _,actions=scan()
    assert(issue(actions["harvest_meal:3"]))
    assert(pc.sent.non_preview_cb and not pc.sent.preview_cb)
    pc.sent.non_preview_cb()
    assert(call[1]==G.RPC.RightClick and call[2]==20 and call[3]==5 and call[4]==4 and call[5]==pot)
    assert(call[7]==true and call[11]==platform and call[12]==true)
end)

test("duplicate raw scene actions open both fixtures through the server's surviving left action",function()
    for _,predicting in ipairs({true,false}) do
        local _,chest,pot,pc,G,_,_,scan,issue,_,_,player=fixture()
        pc.ismastersim=false
        pc.locomotor=predicting and {} or nil
        G.RPC={LeftClick=1,RightClick=2}
        G.ACTIONS.RUMMAGE.code=10
        function pc:EncodeControlMods() return 0 end
        function pc:GetPlatformRelativePosition(x,z) return nil,x,z end
        local picker=player.components.playeractionpicker
        function picker:GetSceneActions(target,right)
            return {{action=G.ACTIONS.RUMMAGE,target=target}}
        end
        function picker:DoGetMouseActions(position,target)
            local left=self:GetSceneActions(target,false)[1]
            local right=self:GetSceneActions(target,true)[1]
            if left.action==right.action then right=nil end
            return left,right
        end
        G.SendRPCToServer=function(rpc,code,x,z,target)
            local left,right=picker:DoGetMouseActions(G.Vector3(x,0,z),target)
            local selected=rpc==G.RPC.LeftClick and left or right
            if selected and selected.action.code==code then target.replica.container.opened=true end
        end
        function pc:DoAction(action)
            if predicting then action.preview_cb() else action.non_preview_cb() end
        end
        for _,guid in ipairs({2,3}) do
            local before,actions=scan()
            local action=actions["open_container:"..guid]
            assert(issue(action))
            assert(camp.verify(action,before,scan())=="container_contents_observed")
        end
        assert(chest.opened and pot.opened)
    end
end)

test("ignition uses the current season and reports the actual blocking entity",function()
    local _,_,_,_,G,tree,ent,scan,issue=fixture()
    local blocker=ent(50,"grass",{canlight=true})
    local separation=8
    blocker.Transform.GetWorldPosition=function() return 2+separation,0,0 end
    G.TUNING={SPRING_FIRE_RANGE_MOD=1.5}
    G.TheSim.FindEntities=function() return {tree,blocker} end
    local state,actions=scan()
    assert(actions["ignite_tree:4"],"spring tuning must not enlarge autumn clearance")
    assert(state.camp_work.trees[1].clearance==7.5)
    G.TheWorld.state.isspring=true
    state,actions=scan()
    assert(not actions["ignite_tree:4"])
    local rejected=state.camp_work.trees[1]
    assert(rejected.status=="blocked" and rejected.reason=="near_combustible")
    assert(rejected.blocker_prefab=="grass" and rejected.blocker_distance==8 and rejected.clearance==11)
    G.TheWorld.state.isspring=false
    state,actions=scan()
    separation=7
    local ok,reason=issue(actions["ignite_tree:4"])
    assert(not ok and reason=="ignition_no_longer_safe","recheck fresh distances at dispatch")
end)

test("camp protection follows spread distance and remembers unloaded buildings",function()
    local _,_,_,_,_,_,_,scan=fixture()
    local state,actions=scan(nil,{home={x=11,z=0}})
    assert(actions["ignite_tree:4"],"a tree nine units from home is no longer rejected by an eighteen-unit ban")
    state,actions=scan(nil,{home={x=10,z=0}})
    assert(not actions["ignite_tree:4"] and state.camp_work.trees[1].reason=="near_home")
    state,actions=scan(nil,{buildings={one={prefab="treasurechest",x=8,z=0,status="unconfirmed"}}})
    assert(not actions["ignite_tree:4"] and state.camp_work.trees[1].reason=="near_remembered_building")
end)

test("a safe fifth tree is found with one shared hazard query",function()
    local _,_,_,_,G,_,ent,scan,_,_,_,player,state=fixture()
    player.Transform.GetWorldPosition=function() return 0,0,0 end
    state.nearby={}
    local objects={}
    for i=1,5 do
        local tree=ent(50+i,"evergreen",{CHOP_workable=true,canlight=true})
        local x,z=i==5 and -8 or 8,i==5 and 0 or i-1
        tree.Transform.GetWorldPosition=function() return x,0,z end
        G.Ents[tree.GUID]=tree;objects[#objects+1]=tree
        state.nearby[#state.nearby+1]={guid=tree.GUID,prefab=tree.prefab,distance=math.sqrt(x*x+z*z),
            collect_path={status="ready"}}
    end
    local queries=0
    G.TheSim.FindEntities=function() queries=queries+1;return objects end
    local observed,actions=scan()
    assert(queries==1 and actions["ignite_tree:55"])
    assert(observed.camp_work.trees[1].guid==55 and observed.camp_work.trees[1].status=="ignitable")
    assert(not actions["ignite_tree:51"] and observed.camp_work.trees[2].status=="blocked")
end)

test("ignition rechecks daylight and nearby structures before issuing LIGHT",function()
    local _,_,_,pc,G,tree,_,scan,issue,hazard=fixture()
    local b,actions=scan()
    local light=actions["ignite_tree:4"]
    assert(light and light.planner_only)
    G.TheWorld.state.phase="night"
    assert(not issue(light) and not pc.sent)
    G.TheWorld.state.phase="day"
    assert(issue(light) and pc.sent.action==G.ACTIONS.LIGHT)
    hazard()
    assert(not issue(light))
    tree.tags.fire=true
    assert(camp.verify(light,b,scan())=="tree_ignited")
end)

print("1.."..count)
