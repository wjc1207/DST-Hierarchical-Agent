-- Client HUD and local chat-command integration.
local autonomy=require("jev_autonomy")
local Hud=require("jev_hud_dock")
local M={}

function M.install(G,postconstruct)
    local function respond(message)
        if G.ChatHistory then G.ChatHistory:SendCommandResponse("[Agent] "..message) end
    end
    local function local_player()
        local player=G.ThePlayer
        if player and player:IsValid() then return player end
    end

    postconstruct("screens/chatinputscreen",function(screen)
        local run=screen.Run
        screen.Run=function(self,...)
            local text=self.chat_edit:GetString()
            if self._jev_companion_accepts_chat and self._jev_companion_accepts_chat(text) then
                return run(self,...)
            end
            local player=local_player()
            local matched,goal,err=autonomy.parse(text)
            if matched then
                if not player then respond("请先进入游戏。");return end
                if not goal then respond(err);return end
                autonomy.set_goal(player,goal)
                respond("长期目标已设置："..goal)
                return
            end
            local recipient,request
            if type(text)=="string" then recipient,request=text:match("^%s*@(%S+)%s*(.-)%s*$") end
            if player and recipient=="自己" then
                local state=autonomy.get(player)
                if state.listening then
                    if state.mode=="autonomous" and not state.enabled then
                        respond("自主模式尚未运行，请在角色操控面板开始或继续。")
                    else
                        local message,status=autonomy.submit_message(player,request)
                        if not message then respond(status);return end
                        respond(status=="paused" and "已暂停动作，角色由你控制。"
                            or "已收到消息，暂停当前安排并请 DeepSeek 重新规划。")
                    end
                end
            end
            return run(self,...)
        end
    end)

    postconstruct("widgets/controls",function(controls)
        local refresh
        local panel=Hud.new(G,controls,{
            name="JEVPlayerControl",title="角色操控",expanded_title="收起操控",
            position_key="jev_player_hud_position",depth=380,width=436,height=194,
        },function() refresh() end)
        local function act(action)
            return function()
                local player=local_player()
                if player then action(player,autonomy.get(player));refresh() end
            end
        end
        local title=Hud.label(G,panel,0,73,394,24)
        local instruction=Hud.button(panel,"指令",-151,36,100,act(function(player)
            autonomy.set_mode(player,"instruction")
        end))
        local autonomous=Hud.button(panel,"自主",-43,36,100,act(function(player)
            autonomy.set_mode(player,"autonomous")
        end))
        local listening=Hud.button(panel,"聊天监听：关",114,36,164,act(function(_,state)
            state.listening=not state.listening
        end))
        listening:SetTooltip("开启后，在游戏聊天输入 @自己 加空格和指令。")

        local goal_text=Hud.label(G,panel,-55,-5,284,19)
        local edit=Hud.button(panel,"编辑目标",143,-5,108,act(function(player,state)
            Hud.prompt(G,"角色的长期目标",state.goal,autonomy.MAX_GOAL_CHARS,function(text)
                if local_player()~=player then return true end
                local goal,err=autonomy.set_goal(player,text)
                if not goal then respond(err);return false end
                refresh()
                return true
            end)
        end))
        local run=Hud.button(panel,"开始自主",124,-55,144,act(function(player,state)
            if state.enabled or state.paused or state.mode=="autonomous" then
                autonomy.set_enabled(player,not state.enabled)
            end
        end))
        run:SetTooltip("暂停会立即停止模组动作，并交还角色控制权。")
        local hint=Hud.label(G,panel,-76,-55,242,18)

        refresh=function()
            local player=local_player()
            local state=player and autonomy.get(player)
            local is_auto=state and state.mode=="autonomous"
            local status=state and (state.enabled and "执行中" or state.paused and "已暂停"
                or is_auto and "自主未启动" or "等待指令") or "等待进入世界"
            title:SetString("角色操控 · "..status)
            Hud.set_button(instruction,state~=nil,state~=nil and not is_auto)
            Hud.set_button(autonomous,state~=nil,is_auto)
            Hud.set_button(listening,state~=nil)
            listening:SetText(state and state.listening and "聊天监听：开" or "聊天监听：关")
            Hud.set_button(edit,is_auto)
            if is_auto then
                goal_text:SetString("目标："..Hud.short(state.goal or "自由生存",13))
                edit:SetTooltip(state.goal or "设置角色的长期目标。")
                goal_text:Show()
                edit:Show()
            else
                goal_text:Hide()
                edit:Hide()
            end
            local resumable=state and (state.enabled or state.paused or is_auto)
            Hud.set_button(run,resumable)
            run:SetText(state and (state.enabled and "暂停并接管" or state.paused and "继续"
                or is_auto and "开始自主" or "等待指令") or "等待进入世界")
            hint:SetString(state and state.listening and "聊天：@自己 + 指令" or "聊天监听已关闭")
        end
    end)
end

return M
