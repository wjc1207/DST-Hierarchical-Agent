-- Client HUD and local chat-command integration.
local autonomy=require("jev_autonomy")
local M={}

function M.install(G,postconstruct)
    local function respond(message)
        if G.ChatHistory then G.ChatHistory:SendCommandResponse("[Agent] "..message) end
    end

    postconstruct("screens/chatinputscreen",function(screen)
        local run=screen.Run
        screen.Run=function(self,...)
            local text=self.chat_edit:GetString()
            if self._jev_companion_accepts_chat and self._jev_companion_accepts_chat(text) then
                return run(self,...)
            end
            local matched,goal,error=autonomy.parse(text)
            local player=G.ThePlayer
            if not matched then
                if player and player:IsValid() and not text:match("^%s*/") then
                    local state=autonomy.get(player)
                    if state.mode=="instruction" or state.enabled then
                        local message,status=autonomy.submit_message(player,text)
                        if not message then respond(status);return end
                        respond(status=="paused" and "已停止动作，等待下一条指令。" or "已收到消息，暂停当前安排并请 DeepSeek 重新规划。")
                    end
                end
                return run(self,...)
            end
            if not player or not player:IsValid() then respond("请先进入游戏。");return end
            if not goal then respond(error);return end
            autonomy.set_goal(player,goal)
            respond("长期目标已设置："..goal)
        end
    end)

    postconstruct("widgets/controls",function(controls)
        local TextButton=require("widgets/textbutton")
        local button=controls.topleft_root:AddChild(TextButton("JEVAutonomy"))
        button:SetPosition(125,-255,0)
        button:SetFont(G.UIFONT)
        button:SetTextSize(24)
        local mode_button=controls.topleft_root:AddChild(TextButton("JEVAgentMode"))
        mode_button:SetPosition(125,-290,0)
        mode_button:SetFont(G.UIFONT)
        mode_button:SetTextSize(22)
        local last_label,last_tooltip,last_mode_label
        local function refresh()
            local player=G.ThePlayer
            if not player or not player:IsValid() then button:Hide();mode_button:Hide();return end
            button:Show();mode_button:Show()
            local state=autonomy.get(player)
            local label=state.mode=="instruction" and (state.enabled and "指令模式：执行中" or "指令模式：待命")
                or (state.enabled and "自主模式：开启" or "自主模式：关闭")
            if label~=last_label then button:SetText(label);last_label=label end
            local tooltip="点击暂停或恢复。聊天可修改安排；输入 停止 / 暂停 / 取消 立即停下。\n长期目标："..(state.goal or "自由生存")
                ..'\n在聊天框输入 goal("你的长期目标") 修改目标'
            if tooltip~=last_tooltip then button:SetTooltip(tooltip);last_tooltip=tooltip end
            local mode_label=state.mode=="instruction" and "切换为自主模式" or "切换为指令模式"
            if mode_label~=last_mode_label then mode_button:SetText(mode_label);last_mode_label=mode_label end
        end
        button:SetOnClick(function()
            local player=G.ThePlayer
            if not player or not player:IsValid() then return end
            local state=autonomy.get(player)
            autonomy.set_enabled(player,not state.enabled)
            respond(state.enabled and "Agent 已恢复。" or "Agent 已暂停，已停止模组动作。")
            refresh()
        end)
        mode_button:SetTooltip("指令模式完成玩家要求后停止；自主模式继续生存。切换后先暂停。")
        mode_button:SetOnClick(function()
            local player=G.ThePlayer
            if not player or not player:IsValid() then return end
            local mode=autonomy.get(player).mode=="instruction" and "autonomous" or "instruction"
            autonomy.set_mode(player,mode)
            respond(mode=="instruction" and "已切换为指令模式，请在聊天框输入需求。" or "已切换为自主模式，点击开启后继续生存。")
            refresh()
        end)
        local elapsed=0
        button.OnUpdate=function(_,dt)
            elapsed=elapsed+dt
            if elapsed>=.25 then elapsed=0;refresh() end
        end
        button:StartUpdating()
        refresh()
    end)
end

return M
