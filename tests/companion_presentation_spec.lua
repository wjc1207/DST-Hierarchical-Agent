package.path="./mods/jev_ai_companion/scripts/?.lua;"..package.path
local Presentation=require("jev_companion_presentation")
local World=require("jev_companion_world")
local passed=0
local function test(name,fn) local ok,err=pcall(fn);assert(ok,name..": "..tostring(err));passed=passed+1;print("ok "..passed.." - "..name) end
local function actor(id,name)
    local a={GUID=id,name=name,components={talker={disablefollowtext=true}},listeners={},valid=true}
    function a:IsValid() return self.valid end
    function a:ListenForEvent(event,fn) self.listeners[event]=fn end
    function a:GetDisplayName() return self.displaynamefn and self:displaynamefn() or self.name end
    return a
end
local function peer(master,fields)
    return {TheWorld={ismastersim=master},Vector3=function(x,y,z) return {x=x,y=y,z=z} end,
        net_string=function(guid,key,event)
            local id=tostring(guid)..key;fields[id]=fields[id] or {text="",sets=0}
            local row=fields[id]
            return {value=function() return row.text end,set=function(_,value) row.text=value;row.sets=row.sets+1 end}
        end}
end
test("summon names replicate to remote hover names and talker configuration without repeated writes",function()
    local fields={};local server,client=peer(true,fields),peer(false,fields)
    local a,b=actor(1,"Wilson"),actor(1,"Wilson")
    Presentation.attach(server,a);Presentation.attach(client,b)
    assert(a:GetDisplayName()=="Wilson" and b:GetDisplayName()=="Wilson")
    assert(Presentation.set_name(server,a,"小威"));b.listeners.jev_companion_namedirty()
    assert(a:GetDisplayName()=="小威" and b:GetDisplayName()=="小威" and b.name=="小威")
    assert(not a.components.talker.disablefollowtext and not b.components.talker.disablefollowtext)
    assert(b.components.talker.offset.y==-400)
    assert(Presentation.set_name(server,a,"小威"))
    for _,row in pairs(fields) do assert(row.sets==1) end
    -- A client arriving after the name was set also resolves the current value.
    local late=actor(1,"Wilson");Presentation.attach(client,late);assert(late:GetDisplayName()=="小威")
end)
test("ordinary players keep their existing names and client or owned-player renaming is rejected",function()
    local fields={};local server,client=peer(true,fields),peer(false,fields)
    local a=actor(1,"Player name");a.userid="KU_real";a.displaynamefn=function() return "Existing custom name" end
    Presentation.attach(server,a);local fn=a.displaynamefn;Presentation.attach(server,a)
    assert(a.displaynamefn==fn and a:GetDisplayName()=="Existing custom name" and a.components.talker.disablefollowtext)
    assert(not Presentation.set_name(server,a,"一号") and a.name=="Player name")
    local b=actor(2,"Wilson");Presentation.attach(client,b)
    assert(not Presentation.set_name(client,b,"Fake name") and b:GetDisplayName()=="Wilson")
end)
local function speech()
    local a=actor(2,"一号");a.tasks={};a.said={};a.dead=false
    a.components.health={IsDead=function() return a.dead end}
    a.components.talker.Say=function(_,text,duration,noanim,force,nobroadcast)
        a.said[#a.said+1]={text=text,duration=duration,noanim=noanim,force=force,nobroadcast=nobroadcast}
    end
    function a:DoTaskInTime(delay,fn) self.tasks[#self.tasks+1]={delay=delay,fn=fn} end
    local adapter=World.new({},function() end,function(v) return v end)
    return a,adapter,{token="bound",revision=1}
end
test("overhead speech is UTF8-safe and each page gets readable time with native broadcasting",function()
    local a,e,r=speech();local text=string.rep("你",125);e.say(a,text,r)
    assert(#a.tasks==3 and a.tasks[1].delay==0)
    for _,task in ipairs(a.tasks) do task.fn() end
    local pages={}
    for i,row in ipairs(a.said) do
        pages[#pages+1]=row.text
        assert(row.noanim and row.force and row.nobroadcast==false and row.duration>=3 and row.duration<=8)
        if i<#a.tasks then assert(a.tasks[i+1].delay>=a.tasks[i].delay+row.duration) end
    end
    assert(table.concat(pages)==text and #a.said[1].text==180)
end)
test("new speech cancels stale pages and intent binding death or removal suppress delayed speech",function()
    local a,e,r=speech();e.say(a,string.rep("旧",100),r);a.tasks[1].fn()
    e.say(a,"新消息",r);a.tasks[2].fn();a.tasks[3].fn()
    assert(#a.said==2 and a.said[2].text=="新消息")
    for _,field in ipairs({"revision","token","dead","valid"}) do
        local b,env,record=speech();env.say(b,"不应播放",record)
        if field=="revision" then record.revision=2 elseif field=="token" then record.token="other"
        elseif field=="dead" then b.dead=true else b.valid=false end
        b.tasks[1].fn();assert(#b.said==0)
    end
end)
print("1.."..passed)
