package.path="./mods/dst_buddy_agent/scripts/?.lua;"..package.path
local Native=require("jev_companion_actions")
local Server=require("jev_companion_server")
local passed=0
local function test(name,fn) local ok,err=pcall(fn);assert(ok,name..": "..tostring(err));passed=passed+1;print("ok "..passed.." - "..name) end
local function fixture()
    local w={ents={},items={},time=0,known=true,proto=false,ingredients=true,land=true,deploy=true,builds=0,unlocks=0}
    function w:entity(id,prefab,x,z,tags)
        local e={GUID=id,prefab=prefab,x=x or 0,z=z or 0,valid=true,components={},tags=tags or {}}
        function e:IsValid() return self.valid end
        function e:HasTag(tag) return self.tags[tag] end
        e.Transform={GetWorldPosition=function() return e.x,0,e.z end}
        self.ents[id]=e;return e
    end
    local a=w:entity(1,"wilson",0,0,{player=true});w.actor=a;a.userid=nil
    a.components.inventory={FindItems=function(_,fn) local rows={};for _,i in ipairs(w.items) do if i:IsValid() and fn(i) then rows[#rows+1]=i end end;return rows end,
        GetEquippedItem=function() return w.equipped end,IsItemEquipped=function(_,i) return w.equipped==i end}
    a.components.locomotor={PushAction=function(_,q) w.action=q end}
    a.components.builder={ingredientmod=1,accessible_tech_trees={},accessible_tech_trees_no_temp={},
        KnowsRecipe=function() return w.known end,CanLearn=function() return true end,
        IsBuildBuffered=function() return false end,HasIngredients=function() return w.ingredients end,
        EvaluateTechTrees=function() w.evaluated=true end,
        MakeRecipe=function(_,recipe,point,rot,skin,done) w.builds=w.builds+1;w.pending={recipe=recipe,point=point,done=done};return true end,
        UnlockRecipe=function() w.unlocked=true;w.unlocks=w.unlocks+1 end,AddRecipe=function() end,
        ActivateCurrentResearchMachine=function() w.activated=true end,ConsumeTempTechBonuses=function() end}
    w.recipes={axe={name="axe",product="axe",ingredients={{type="flint",amount=1}},level={}},
        researchlab={name="researchlab",product="researchlab",placer="researchlab_placer",ingredients={{type="goldnugget",amount=1}},level={SCIENCE=1}},
        boards={name="boards",product="boards",ingredients={{type="log",amount=4}},level={SCIENCE=1}}}
    local G={Ents=w.ents,AllRecipes=w.recipes,EQUIPSLOTS={HANDS="hands"},ACTIONS={EQUIP="EQUIP",CHOP="CHOP",MINE="MINE"},
        GetValidRecipe=function(id) return w.recipes[id] end,CanPrototypeRecipe=function() return w.proto end,
        TheWorld={Map={CanDeployRecipeAtPoint=function() return w.deploy end}},
        Vector3=function(x,y,z) return {x=x,y=y,z=z} end,
        TheSim={FindEntities=function(_,x,y,z,r) local rows={};for _,e in pairs(w.ents) do if e.valid and (e.x-x)^2+(e.z-z)^2<=r*r then rows[#rows+1]=e end end;return rows end},
        BufferedAction=function(doer,target,action,item)
            local q={doer=doer,target=target,action=action,invobject=item}
            function q:AddSuccessAction(fn) self.success=fn end
            function q:AddFailAction(fn) self.fail=fn end
            return q
        end}
    w.env={position=function(e) return e.x,e.z end,distance=function(e,b) return math.sqrt((e.x-b.x)^2+(e.z-b.z)^2) end,
        count=function(_,prefab) local n=0;for _,i in ipairs(w.items) do if i:IsValid() and i.prefab==prefab then n=n+(i.count or 1) end end;return n end,
        valid_point=function(_,x,z,r) return w.land and x*x+z*z<=(r or 20)^2 end}
    w.G=G;w.native=Native.new(G,w.env)
    function w:tool(id,kind)
        local item=self:entity(id,kind=="CHOP" and "axe" or "pickaxe")
        item.components.equippable={equipslot="hands",IsRestricted=function() return false end,ShouldPreventUnequipping=function() return false end}
        item.components.tool={CanDoAction=function(_,work) return kind==work end}
        self.items[#self.items+1]=item;return item
    end
    function w:workable(id,prefab,kind)
        local e=self:entity(id,prefab,3,0);e.left=4
        e.components.workable={CanBeWorked=function() return true end,GetWorkAction=function() return kind end,GetWorkLeft=function() return e.left end}
        return e
    end
    return w
end

test("work targets expose actual drops and exclude buildings, burning or distant entities",function()
    local w=fixture();local tree=w:workable(2,"evergreen","CHOP");local rock=w:workable(3,"rock2","MINE")
    assert(w.native.work_target(w.actor,tree.GUID).products[1]=="log")
    assert(w.native.work_target(w.actor,rock.GUID).products[3]=="goldnugget")
    rock.prefab="wall_stone";assert(not w.native.work_target(w.actor,rock.GUID))
    tree.components.burnable={IsBurning=function() return true end};assert(not w.native.work_target(w.actor,tree.GUID))
    tree.components.burnable=nil;tree.x=30;assert(not w.native.work_target(w.actor,tree.GUID))
end)

local function stow_fixture()
    local w=fixture();local tool=w:tool(120000,"CHOP");w.equipped=tool
    tool.components.inventoryitem={cangoincontainer=true}
    local inv=w.actor.components.inventory;inv.itemslots={};inv.maxslots=1
    inv.GetOverflowContainer=function() return nil end
    inv.CanTakeItemInSlot=function() return true end
    inv.Unequip=function() w.equipped=nil;return tool end
    inv.GiveItem=function(_,item,slot) inv.itemslots[slot]=item;return true end
    inv.Equip=function(_,item) w.equipped=item end
    return w,tool
end
test("unequip stows the real equipped tool and reports observed native progress",function()
    local w,tool=stow_fixture()
    local q={op="unequip",args={item_guid=tool.GUID}}
    assert(w.native.start(w.actor,q))
    assert(not w.equipped and w.actor.components.inventory.itemslots[1]==tool)
    local status,reason=w.native.progress(w.actor,q);assert(status=="completed" and reason=="unequipped")
end)
test("unequip rejects full inventory, restricted gear and unowned items without stripping equipment",function()
    local w,tool=stow_fixture();w.actor.components.inventory.itemslots[1]=w:entity(7,"rocks")
    local q={op="unequip",args={item_guid=tool.GUID}}
    local ok,reason=w.native.start(w.actor,q);assert(not ok and reason=="inventory_full_or_rejected" and w.equipped==tool)
    w.actor.components.inventory.itemslots={}
    tool.components.equippable.ShouldPreventUnequipping=function() return true end
    assert(not w.native.start(w.actor,q) and w.equipped==tool)
    assert(not w.native.start(w.actor,{op="unequip",args={item_guid=999999}}) and w.equipped==tool)
end)
test("unequip waits out a busy craft animation and rechecks storage before changing equipment",function()
    local w,tool=stow_fixture();local busy=true
    w.actor.sg={HasStateTag=function() return busy end}
    local q={op="unequip",args={item_guid=tool.GUID}}
    assert(w.native.start(w.actor,q) and q.deferred and w.equipped==tool)
    w.actor.components.inventory.itemslots[1]=w:entity(7,"rocks");busy=false
    local status,reason=w.native.progress(w.actor,q)
    assert(status=="failed" and reason=="inventory_full_or_rejected" and w.equipped==tool)
end)

test("equip uses only the companion inventory and confirms actual equipped state",function()
    local w=fixture();local tool=w:tool(2,"CHOP");w:entity(3,"axe")
    assert(not w.native.start(w.actor,{op="equip",args={item_guid=3}}))
    local q={op="equip",args={item_guid=tool.GUID}};assert(w.native.start(w.actor,q))
    assert(not w.native.progress(w.actor,q))
    w.equipped=tool;assert(w.native.progress(w.actor,q)=="completed")
end)

test("work requires the matching equipped tool and a real decrease in work",function()
    local w=fixture();local tree=w:workable(2,"evergreen","CHOP");w:tool(3,"MINE")
    local q={op="work",args={target_guid=2,work="CHOP"}}
    assert(not w.native.start(w.actor,q));w.equipped=w:tool(4,"CHOP")
    assert(w.native.start(w.actor,q));assert(not w.native.progress(w.actor,q))
    tree.left=3;assert(w.native.progress(w.actor,q)=="completed")
    assert(w.native.start(w.actor,q));w.action.success()
    assert(w.native.progress(w.actor,q)=="failed")
end)

test("real-world tree, mining and equipment IDs are independent of coordinate limits",function()
    local w=fixture();local tree=w:workable(109348,"evergreen","CHOP")
    local axe=w:tool(119922,"CHOP")
    local equip={op="equip",args={item_guid=axe.GUID}}
    assert(w.native.start(w.actor,equip))
    w.equipped=axe;assert(w.native.progress(w.actor,equip)=="completed")
    local q={op="work",args={target_guid=tree.GUID,work="CHOP"}}
    assert(w.native.start(w.actor,q));tree.left=tree.left-1
    assert(w.native.progress(w.actor,q)=="completed")
    local rock=w:workable(200000001,"rock2","MINE");w.equipped=w:tool(200000002,"MINE")
    q={op="work",args={target_guid=rock.GUID,work="MINE"}}
    assert(w.native.start(w.actor,q));rock.left=rock.left-1
    assert(w.native.progress(w.actor,q)=="completed")
    -- Even an existing table entry must not make a malformed ID acceptable.
    for _,id in ipairs({0,-1,1.5,math.huge,"109348",9007199254740992}) do
        w.ents[id]=tree
        assert(not w.native.start(w.actor,{op="work",args={target_guid=id,work="CHOP"}}))
    end
end)

test("final chop keeps the original counter when DST replaces the tree with a diggable stump",function()
    local w=fixture();local tree=w:workable(2,"evergreen","CHOP");w.equipped=w:tool(3,"CHOP")
    tree.left=1
    local q={op="work",args={target_guid=2,work="CHOP"}};assert(w.native.start(w.actor,q))
    local original=tree.components.workable;local remaining=1
    original.GetWorkLeft=function() return remaining end
    remaining=0;tree.tags.stump=true
    tree.components.workable={GetWorkLeft=function() return 1 end,GetWorkAction=function() return "DIG" end}
    w.action.success()
    local status,reason=w.native.progress(w.actor,q)
    assert(status=="completed" and reason=="work_finished")
end)

test("a replacement work component cannot fabricate progress in the original action",function()
    local w=fixture();local tree=w:workable(2,"evergreen","CHOP");w.equipped=w:tool(3,"CHOP")
    local q={op="work",args={target_guid=2,work="CHOP"}};assert(w.native.start(w.actor,q))
    tree.components.workable={GetWorkLeft=function() return 1 end}
    assert(not w.native.progress(w.actor,q))
    w.action.success();assert(w.native.progress(w.actor,q)=="failed")
end)

test("target removal alone is not proof that the companion finished working",function()
    local w=fixture();local tree=w:workable(2,"evergreen","CHOP");w.equipped=w:tool(3,"CHOP")
    local q={op="work",args={target_guid=2,work="CHOP"}};assert(w.native.start(w.actor,q))
    tree.valid=false;assert(not w.native.progress(w.actor,q))
    w.action.success();assert(w.native.progress(w.actor,q)=="completed")
end)

test("recipe facts and native crafting respect current materials and technology",function()
    local w=fixture();w.known=false
    local row=w.native.recipe(w.actor,"boards");assert(not row.ready and row.reason=="technology_or_unlock_required")
    assert(not w.native.start(w.actor,{op="craft",args={recipe_id="boards"}}))
    w.proto=true;w.ingredients=false;assert(w.native.recipe(w.actor,"boards").reason=="ingredients_missing")
    w.ingredients=true;local q={op="craft",args={recipe_id="boards"}}
    assert(w.native.start(w.actor,q) and w.builds==1 and not w.unlocked)
    w.pending.done();assert(w.unlocked and w.activated and not w.native.progress(w.actor,q))
    w.items[#w.items+1]=w:entity(9,"boards");assert(w.native.progress(w.actor,q)=="completed")
end)

test("special or dynamic recipes are reported unsupported and cannot be executed",function()
    local w=fixture();w.recipes.special={name="special",product="x",character_ingredients={{}},ingredients={},level={}}
    assert(not w.native.recipe(w.actor,"special").supported)
    assert(not w.native.start(w.actor,{op="craft",args={recipe_id="special"}}))
    w.recipes.dynamic={name="dynamic",product=function() end,numtogive=function() end,ingredients={},level={}}
    assert(not w.native.recipe(w.actor,"dynamic").supported)
end)

test("placement checks land and actual new output before completing",function()
    local w=fixture();local old=w:entity(9,"researchlab",2,0,{structure=true})
    local q={op="place",args={recipe_id="researchlab",x=0,z=0,radius=6}}
    w.land=false;assert(not w.native.start(w.actor,q) and w.builds==0)
    w.land=true;w.deploy=false;assert(not w.native.start(w.actor,q) and w.builds==0)
    w.deploy=true;assert(w.native.start(w.actor,q));w.pending.done()
    assert(not w.native.progress(w.actor,q))
    local point=w.pending.point;w:entity(10,"researchlab",point.x,point.z,{structure=true})
    assert(w.native.progress(w.actor,q)=="completed")
    assert(w.actor._jev_companion_last_build==10 and old.valid)
end)

test("recipe search is paginated and expands only the bounded watched set",function()
    local w=fixture()
    for i=1,80 do local id="custom_"..i;w.recipes[id]={name=id,product=id,ingredients={},level={}} end
    local result=w.native.query(w.actor,{query="custom_",limit=24});assert(result.total==80 and #result.recipes==24 and result.next_offset==24)
    w.native.query(w.actor,{query="custom_",offset=24,limit=24});w.native.query(w.actor,{query="custom_",offset=48,limit=24})
    assert(#w.actor._jev_companion_recipes==48)
    assert(not w.native.query(w.actor,{limit=1000}))
end)

test("server native dispatch is versioned, deduplicated and interrupted by a newer intent",function()
    local w=fixture();local owner=w:entity(10,"wilson");owner.userid="A"
    local replies={};local env=w.env
    env.now=function() return w.time end;env.find_buddy=function() return w.actor end
    env.dead=function() return false end;env.stop=function() w.stops=(w.stops or 0)+1 end
    env.snapshot=function() return {} end;env.reply=function(_,p) replies[#replies+1]=p end
    env.begin=w.native.start;env.progress=w.native.progress;env.query=w.native.query
    local server=Server.new(env)
    server:request(owner,{id="c:1",seq=1,op="bind"});local token=replies[#replies].token
    local q={id="c:2",seq=2,token=token,revision=0,op="craft",args={recipe_id="axe"}}
    server:request(owner,q);server:request(owner,q);assert(w.builds==1)
    server:request(owner,{id="c:3",seq=3,token=token,revision=1,op="intent"})
    assert(not server.owners.A.active and w.stops>=1)
    w.pending.done();w.items[#w.items+1]=w:entity(11,"axe");server:tick()
    assert(q.last.status=="interrupted")
end)

test("recipe wire budgets preserve pagination and bound the snapshot catalogue",function()
    local w=fixture();w.env.encoded_size=function() return 8000 end
    local result=w.native.query(w.actor,{limit=24})
    assert(#result.recipes==2 and result.next_offset==2 and result.total==3)
    local state=w.native.snapshot(w.actor,{})
    local count=0;for _ in pairs(state.recipes) do count=count+1 end
    assert(count<=3)
end)

test("chained crafting waits out busy animations and rechecks materials before starting",function()
    local w=fixture();local busy=true
    w.actor.sg={HasStateTag=function() return busy end}
    local q={op="craft",args={recipe_id="axe"}}
    assert(w.native.start(w.actor,q) and q.deferred and w.builds==0)
    assert(not w.native.progress(w.actor,q) and w.builds==0)
    busy=false;w.ingredients=false
    assert(w.native.progress(w.actor,q)=="failed" and w.builds==0)
    w.ingredients=true;assert(w.native.start(w.actor,q) and w.builds==1)
end)

print("1.."..passed)
