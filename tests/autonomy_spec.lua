package.path="./mods/dst_player_agent/scripts/?.lua;"..package.path
local A=require("jev_autonomy")
local N=require("jev_native_executor")
local B=require("jev_bridge")
local passed=0
local function test(name,fn)
    local ok,error=pcall(fn);assert(ok,name..": "..tostring(error))
    passed=passed+1;print("ok "..passed.." - "..name)
end
local function state()
    return {autonomy={enabled=false,revision=0},world={day=1,phase="day",time=.1},
        player={guid=1,position={x=0,z=0},vitals={health=100,hunger=100,hunger_max=150},
            inventory={items={},equipped={}}},nearby={},navigation={}}
end

test("goal parser accepts quoted Unicode text and rejects malformed or executable input",function()
    local matched,goal=A.parse(' goal ( "在营地造锅，做一份肉丸" ) ')
    assert(matched and goal=="在营地造锅，做一份肉丸")
    matched,goal=A.parse([[goal('bring "my" logs')]])
    assert(matched and goal=='bring "my" logs')
    matched,goal=A.parse([[goal("say \"hello\"")]])
    assert(matched and goal=='say "hello"')
    for _,text in ipairs({'goal','goal()','goal(build)','goal("")','goal("   ")',
        'goal("one", "two")','goal("one"); os.execute("bad")','goal("unfinished)',
        'goal("line\nbreak")',[[goal("bad\n")]],'goal("'..string.rep("中",257)..'")'}) do
        local handled,value,error=A.parse(text)
        assert(handled and value==nil and error,text)
    end
    assert(A.validate_goal(string.rep("中",256)))
    assert(not A.parse("大家回家吧") and not A.parse("/help") and not A.parse("goalkeeper"))
end)

test("player chat is opt-in, uses an exact mention and keeps goal commands",function()
    local hooks,messages={},{}
    local player={IsValid=function() return true end}
    local G={ThePlayer=player,ChatHistory={SendCommandResponse=function(_,msg) messages[#messages+1]=msg end}}
    local state=A.get(player)
    local input,forwarded="",0
    local screen={chat_edit={GetString=function() return input end},Run=function() forwarded=forwarded+1 end,
        _jev_companion_accepts_chat=function(text) return text:match("^@小威 ")~=nil end}
    require("jev_autonomy_ui").install(G,function(name,fn) hooks[name]=fn end)
    hooks["screens/chatinputscreen"](screen)

    input="@自己 做一把火把";screen:Run()
    assert(forwarded==1 and not state.listening and not state.enabled and state.revision==0)
    state.listening=true
    screen:Run()
    assert(forwarded==2 and not state.enabled and state.revision==0 and #messages==1)
    input='goal("建造烹饪锅")';screen:Run()
    assert(forwarded==2 and state.goal=="建造烹饪锅" and not state.enabled)
    local revision=state.revision
    input='goal("broken"';screen:Run()
    assert(state.revision==revision and state.goal=="建造烹饪锅")

    A.set_mode(player,"instruction")
    revision=state.revision
    for _,text in ipairs({"普通聊天","/help","@自己人 你好","@小威 砍树","@all 停止"}) do
        input=text;screen:Run()
    end
    assert(forwarded==7 and state.revision==revision and not state.enabled)
    input="@自己 收集五个木头";screen:Run()
    assert(forwarded==8 and state.enabled and state.instruction=="收集五个木头")
    input="@自己 停止";screen:Run()
    assert(forwarded==9 and not state.enabled and state.paused and state.revision==revision+2)
    A.set_enabled(player,true)
    assert(state.enabled and not state.paused and state.instruction=="收集五个木头")
end)

test("chat interruptions retain the original request until completion and reject stale feedback",function()
    local player={};local reasons,messages={},{}
    A.get(player).on_change=function(reason) reasons[#reasons+1]=reason end
    A.set_mode(player,"instruction")
    A.submit_message(player,"收集二十个木头")
    A.submit_message(player,"先回家")
    local state=A.get(player)
    assert(state.instruction=="收集二十个木头" and #state.messages==2)
    local snapshot=A.snapshot(player)
    snapshot.messages[1].text="mutated"
    assert(state.messages[1].text=="收集二十个木头")
    local feedback={id="first",revision=state.revision-1,text="旧回复",finish=true}
    local function respond(text) messages[#messages+1]=text end
    assert(not A.apply_feedback(player,feedback,respond) and state.enabled)
    feedback.revision=state.revision;feedback.finish=false
    assert(A.apply_feedback(player,feedback,respond))
    assert(not A.apply_feedback(player,feedback,respond) and #messages==1)
    feedback={id="finished",revision=state.revision,text="已回家。",finish=true}
    assert(A.apply_feedback(player,feedback,respond) and not state.enabled)
    assert(reasons[#reasons]=="instruction_finished")
    A.submit_message(player,"做一个肉丸")
    assert(state.instruction=="做一个肉丸" and #state.messages==1)
    for i=1,12 do A.submit_message(player,"补充"..i) end
    assert(#state.messages==8 and state.instruction=="做一个肉丸")
    A.set_mode(player,"autonomous");A.set_enabled(player,true)
    assert(not A.apply_feedback(player,{id="wrong-mode",revision=state.revision,text="停",finish=true},respond))
end)

test("native executor rejects disabled and obsolete intent and stops all automatic maintenance",function()
    local s=state();local issued=0
    local executor=N.new({issue=function() issued=issued+1;return true end,stop=function() end})
    assert(not executor:start({action_id="off",choice="wait",autonomy_revision=0},s,0))
    assert(executor.report.reason=="autonomy_disabled")
    s.autonomy={enabled=true,revision=1}
    assert(not executor:start({action_id="stale",choice="wait",autonomy_revision=0},s,0))
    assert(executor.report.reason=="autonomy_settings_changed")
    assert(executor:start({action_id="live",choice="wait",autonomy_revision=1},s,0))
    s.autonomy={enabled=false,revision=2}
    s.player.inventory.equipped={{prefab="torch"}}
    executor:tick(s,.2,true)
    assert(not executor.active and executor.report.reason=="autonomy_disabled" and issued==0)
end)

test("DeepSeek uses native player chat and splits UTF8 replies at the actual chat limit",function()
    local sent,tasks={},{}
    local player={IsValid=function() return true end,
        DoTaskInTime=function(_,delay,fn) tasks[#tasks+1]={delay=delay,fn=fn} end}
    local G={ThePlayer=player,MAX_CHAT_INPUT_LENGTH=150,
        TheNet={Say=function(_,text,whisper) sent[#sent+1]=text;assert(whisper==false) end}}
    local message=string.rep("树",145).."🌲"..string.rep("草",100)
    assert(A.send_chat(G,player,message,false))
    assert(#sent==1 and #tasks==1 and tasks[1].delay==1.5)
    tasks[1].fn()
    local combined=""
    for _,line in ipairs(sent) do
        local _,count=line:gsub("[^\128-\191]","")
        assert(count<=150 and line:sub(1,11)=="[DeepSeek] ")
        combined=combined..line:sub(12)
    end
    assert(combined==message and A.get(player).revision==0)
    assert(A.send_chat(G,player,"停止",false))
    assert(sent[#sent]=="[DeepSeek] 停止" and A.get(player).enabled==false)
end)

test("pending reply chunks survive completion but cannot speak after a newer player instruction",function()
    local sent,tasks={},{}
    local player={IsValid=function() return true end,
        DoTaskInTime=function(_,_,fn) tasks[#tasks+1]=fn end}
    local G={ThePlayer=player,TheNet={Say=function(_,text) sent[#sent+1]=text end}}
    A.set_mode(player,"instruction");A.submit_message(player,"回家")
    local function finish_reply(id)
        local feedback={id=id,revision=A.get(player).revision,text=string.rep("好",160),finish=true}
        assert(A.apply_feedback(player,feedback,function(text) return A.send_chat(G,player,text,true) end))
    end
    finish_reply("done-1")
    assert(not A.get(player).enabled and #sent==1)
    tasks[1]();assert(#sent==2)
    A.submit_message(player,"做饭");finish_reply("done-2")
    A.submit_message(player,"先砍树")
    local count=#sent;tasks[2]();assert(#sent==count)
end)

test("unavailable chat delivery does not consume a reply or finish control",function()
    local player={IsValid=function() return true end}
    A.set_mode(player,"instruction");A.submit_message(player,"做火把")
    local feedback={id="retry",revision=A.get(player).revision,text="完成",finish=true}
    assert(not A.apply_feedback(player,feedback,function(text) return A.send_chat({ThePlayer=player},player,text,true) end))
    assert(A.get(player).enabled and not A.get(player).feedback_seen.retry)
    assert(A.apply_feedback(player,feedback,function() end) and not A.get(player).enabled)
end)

test("control revisions force snapshots and reject delayed HTTP commands",function()
    local s,now,requests=state(),0,{}
    local executor=N.new({issue=function() error("stale command issued") end,stop=function() end})
    s.autonomy={enabled=true,revision=1}
    local bridge=B.new({token="test"},{session_id="test",now=function() return now end,
        state=function() return s end,alive=function() return true end,available=N.available,
        request=function(_,payload,callback) requests[#requests+1]={payload=payload,callback=callback} end},executor)
    bridge:tick()
    assert(requests[1].payload.kind=="snapshot")
    s.autonomy={enabled=false,revision=2}
    requests[1].callback({session_id="test",sequence=1,server_id="server",
        command={session_id="test",operation="start",action_id="late",choice="wait",autonomy_revision=1}},true,200)
    assert(not executor.active and executor.report.reason=="autonomy_disabled")
    now=.2;bridge:tick()
    assert(requests[2].payload.kind=="snapshot" and requests[2].payload.state.autonomy.revision==2)
end)

test("native automatic boards require the current intent revision",function()
    local s=state()
    s.autonomy={enabled=true,revision=1}
    s.player.inventory.items={{prefab="berries",count=1}}
    local executor=N.new({issue=function() return true end,stop=function() end})
    local board={kind="task_board",auto_execute=true,autonomy_revision=0,
        tasks={{id="eat",priority=1,depends_on={},target={type="hunger_ratio",at_least=.9}}}}
    executor:set_board(board,1)
    assert(not executor:board_choice(s,0))
    board.autonomy_revision=1
    assert(executor:board_choice(s,0)=="eat_safe_food")
    executor:board_step(s,0)
    assert(executor.active and executor.active.autonomy_revision==1)
    s.autonomy.revision=2
    executor:tick(s,.2,true)
    assert(not executor.active and executor.report.reason=="autonomy_settings_changed")
end)

print("1.."..passed)
