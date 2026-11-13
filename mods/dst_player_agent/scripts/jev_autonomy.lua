-- Local player intent. Chat input is parsed as data and never evaluated.
local M={}
M.MAX_GOAL_CHARS=256
M.MAX_GOAL_BYTES=1024

function M.validate_goal(text)
    if type(text)~="string" or text:find("[%z\1-\31\127]") then
        return nil,"目标不能包含换行或控制字符。"
    end
    text=text:match("^%s*(.-)%s*$")
    if text=="" then return nil,"目标不能为空。" end
    local _,characters=text:gsub("[^\128-\191]","")
    if characters>M.MAX_GOAL_CHARS or #text>M.MAX_GOAL_BYTES then
        return nil,"目标最多 256 个字符。"
    end
    return text
end

function M.parse(text)
    if type(text)~="string" then return false end
    local input=text:match("^%s*(.-)%s*$")
    if not input:match("^goal%s*[%(\"']") and not input:match("^goal%s*$") then return false end
    local body=input:match("^goal%s*%(%s*(.-)%s*%)$")
    local quote=body and body:sub(1,1)
    if quote~='"' and quote~="'" then
        return true,nil,'格式应为 goal("你的长期目标")。'
    end
    local chars={}
    local i=2
    while i<=#body do
        local char=body:sub(i,i)
        if char==quote then
            if not body:sub(i+1):match("^%s*$") then
                return true,nil,"只允许一个带引号的目标，后面不能追加内容。"
            end
            local goal,error=M.validate_goal(table.concat(chars))
            return true,goal,error
        elseif char=="\\" then
            i=i+1
            char=body:sub(i,i)
            if char~="\\" and char~='"' and char~="'" then
                return true,nil,"转义只支持引号和反斜杠。"
            end
        end
        chars[#chars+1]=char
        i=i+1
    end
    return true,nil,"目标字符串缺少结束引号。"
end

function M.get(player)
    if not player._jev_autonomy then
        player._jev_autonomy={enabled=false,revision=0,mode="autonomous",messages={},listening=false,paused=false}
    end
    return player._jev_autonomy
end

function M.snapshot(player)
    local state=M.get(player)
    local messages={}
    for i,message in ipairs(state.messages or {}) do messages[i]={id=message.id,text=message.text} end
    return {enabled=state.enabled,goal=state.goal,revision=state.revision,
        mode=state.mode or "autonomous",messages=messages,instruction=state.instruction}
end

local function changed(state,reason)
    state.revision=state.revision+1
    if state.on_change then state.on_change(reason) end
end

function M.set_enabled(player,enabled)
    local state=M.get(player)
    if state.enabled~=enabled then
        state.enabled=enabled
        state.paused=not enabled
        changed(state,enabled and "autonomy_enabled" or "autonomy_disabled")
    end
end

function M.set_goal(player,text)
    local goal,error=M.validate_goal(text)
    if not goal then return nil,error end
    local state=M.get(player)
    if state.goal~=goal then
        state.goal=goal
        changed(state,"autonomy_goal_changed")
    end
    return goal
end

function M.set_mode(player,mode)
    if mode~="instruction" and mode~="autonomous" then return false end
    local state=M.get(player)
    if state.mode~=mode then
        state.mode=mode
        state.enabled=false
        state.paused=false
        state.messages={};state.instruction=nil
        changed(state,"agent_mode_changed")
    end
    return true
end

local stops={stop=true,pause=true,cancel=true,["停止"]=true,["暂停"]=true,["取消"]=true}
function M.submit_message(player,text)
    local message,error=M.validate_goal(text)
    if not message then return nil,error end
    local state=M.get(player)
    if stops[message:lower()] then
        -- Also invalidate a pending planner reply while already waiting.
        state.paused=state.enabled or state.paused
        state.enabled=false
        changed(state,"player_paused")
        return message,"paused"
    end
    if not state.enabled or (state.mode=="instruction" and not state.instruction) then
        state.instruction=state.mode=="instruction" and message or nil
        state.messages={}
    end
    state.enabled=true
    state.paused=false
    state.messages=state.messages or {}
    state.messages[#state.messages+1]={id=state.revision+1,text=message}
    while #state.messages>8 do table.remove(state.messages,1) end
    changed(state,"player_message")
    return message,"submitted"
end

function M.apply_feedback(player,feedback,respond)
    if type(feedback)~="table" or type(feedback.id)~="string" then return false end
    if feedback.finish~=nil and type(feedback.finish)~="boolean" then return false end
    local state=M.get(player)
    state.feedback_seen=state.feedback_seen or {}
    if state.feedback_seen[feedback.id] then return false end
    -- Old replies cannot speak for, or stop, a newer instruction.
    if feedback.revision~=state.revision then return false end
    local text=M.validate_goal(feedback.text)
    if not text then return false end
    if feedback.finish and state.mode~="instruction" then return false end
    if respond(text)==false then return false end
    state.feedback_seen[feedback.id]=true
    state.feedback_order=state.feedback_order or {}
    state.feedback_order[#state.feedback_order+1]=feedback.id
    if #state.feedback_order>32 then state.feedback_seen[table.remove(state.feedback_order,1)]=nil end
    if feedback.finish then
        state.enabled=false
        state.paused=false
        changed(state,"instruction_finished")
    end
    return true
end

-- Match ChatInputScreen:Run's native player chat path. Calling TheNet directly
-- avoids feeding the Agent's own response back through our chat-input hook.
function M.send_chat(G,player,text,finish)
    if not G.TheNet or type(G.TheNet.Say)~="function" or G.ThePlayer~=player
        or not player:IsValid() then return false end
    local message=M.validate_goal(text)
    if not message then return false end
    local prefix="[DeepSeek] "
    local limit=G.MAX_CHAT_INPUT_LENGTH or 150
    local capacity=limit-#prefix
    if capacity<1 then return false end
    local lines,characters={},{}
    -- Count UTF-8 code points, never cut a multibyte Chinese/emoji character.
    for character in message:gmatch("[%z\1-\127\194-\244][\128-\191]*") do
        characters[#characters+1]=character
        if #characters==capacity then
            lines[#lines+1]=prefix..table.concat(characters);characters={}
        end
    end
    if #characters>0 then lines[#lines+1]=prefix..table.concat(characters) end
    if #lines>1 and type(player.DoTaskInTime)~="function" then return false end
    local revision=M.get(player).revision
    for index,line in ipairs(lines) do
        if index==1 then G.TheNet:Say(line,false)
        else
            local expected=revision+(finish and 1 or 0)
            player:DoTaskInTime((index-1)*1.5,function()
                if G.ThePlayer==player and player:IsValid() and M.get(player).revision==expected then
                    G.TheNet:Say(line,false)
                end
            end)
        end
    end
    return true
end

return M
