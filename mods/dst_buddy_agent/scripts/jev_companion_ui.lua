-- Client HUD backed by the same controller as /buddy.
local Names=require("jev_companion_roster")
local Hud=require("jev_hud_dock")
local M={}

function M.install(G,postconstruct,get_client)
    postconstruct("widgets/controls",function(controls)
        local refresh
        local panel=Hud.new(G,controls,{
            name="JEVCompanion",title="伙伴",expanded_title="收起伙伴",
            position_key="jev_companion_hud_position",depth=320,width=436,height=302,
        },function() refresh() end)
        local button,set_button,short=Hud.button,Hud.set_button,Hud.short
        local function label(parent,x,y,width,size)
            return Hud.label(G,parent,x,y,width,size)
        end
        local title=label(panel,0,126,394,24)
        local goal_text=label(panel,-55,53,284,19)
        local target_text=label(panel,54,16,276,19)
        local empty=label(panel,0,-22,394,19)
        empty:SetString("尚无伙伴，点击下方的「召唤伙伴」。")

        local selected,selected_id
        local function act(fn)
            return function()
                local controller=get_client()
                if controller then fn(controller) end
                refresh()
            end
        end

        local function input(controller,title_text,initial,limit,submit)
            Hud.prompt(G,title_text,initial,limit,function(text)
                if get_client()~=controller then return true end
                if not submit(text) then return false end
                refresh()
                return true
            end)
        end

        local function edit_goal(controller)
            input(controller,"伙伴的长期目标",controller.control.goal,256,function(text)
                return controller:set_autonomy(true,text)
            end)
        end

        local instruction=button(panel,"指令", -151,89,100,act(function(c) c:set_autonomy(false) end))
        local autonomous=button(panel,"自主",-43,89,100,act(function(c)
            if c.control.goal then c:set_autonomy(true) else edit_goal(c) end
        end))
        local listening=button(panel,"聊天监听：关",114,89,164,act(function(c) c:set_listening(not c.listening) end))
        listening:SetTooltip("开启后，聊天请以 @伙伴名 或 @all 开头。")
        local edit=button(panel,"编辑目标",143,53,108,act(edit_goal))

        local all=button(panel,"全体",-151,16,100,function() selected_id=nil;refresh() end)
        local slots={}
        for i=1,Names.MAX do
            local slot={}
            local y=-20-(i-1)*35
            slot.name=button(panel,"",-123,y,155,function()
                selected_id=slot.id
                refresh()
            end)
            slot.status=label(panel,37,y,150,19)
            slot.delete=button(panel,"删除",156,y,78,function()
                local controller,row=get_client(),slot.row
                if not controller or not row or row.deleting then return end
                local Popup=require("screens/redux/popupdialog")
                G.TheFrontEnd:PushScreen(Popup("删除伙伴", "确定删除「"..row.name.."」？随身物品会留在原地。",{
                    {text="删除",cb=function()
                        G.TheFrontEnd:PopScreen()
                        if get_client()==controller then controller:delete(row);refresh() end
                    end},
                    {text="取消",cb=function() G.TheFrontEnd:PopScreen() end},
                }))
            end)
            slots[i]=slot
        end

        local function manual(command)
            return act(function(c)
                c:command(command..(selected and " @"..selected.name or " @all"))
            end)
        end
        local stop=button(panel,"停止并待命",-134,-94,130,manual("stop"))
        local follow=button(panel,"跟随我",0,-94,116,manual("follow"))
        local summon=button(panel,"召唤伙伴",134,-94,128,act(function(c)
            input(c,"伙伴名字（1–12 字）",nil,12,function(text)
                if text:match("^%s*$") then return end
                return c:summon(text)
            end)
        end))
        local recall=button(panel,"召回所选",-70,-130,123,act(function(c)
            if selected then c:summon(selected.name) end
        end))
        local release=button(panel,"解除绑定",70,-130,123,manual("release"))

        refresh=function()
            local controller=get_client()
            if not controller then return end
            local state=controller:panel_state()
            local is_auto=state.mode=="autonomous"
            title:SetString(state.paused and "伙伴控制 · 自主已暂停" or "伙伴控制")
            set_button(instruction,true,not is_auto)
            autonomous:SetText(state.paused and "恢复自主" or "自主")
            listening:SetText(state.listening and "聊天监听：开" or "聊天监听：关")
            goal_text:SetString(state.goal and short((is_auto and "目标：" or "已保存：")..state.goal,15) or "长期目标未设置")
            edit:SetTooltip(state.goal or "设置伙伴的长期目标。")

            selected=nil
            local any_bound=false
            for i,slot in ipairs(slots) do
                local row=state.roster[i]
                slot.id=row and row.id
                slot.row=row
                if row then
                    if selected_id==row.id then selected=row end
                    any_bound=any_bound or (row.bound and not row.dead)
                    slot.name:SetText(short(row.name,8))
                    slot.name:SetTooltip(row.name.." · "..row.status)
                    slot.status:SetString(short(row.status,12))
                    slot.name:Show()
                    slot.status:Show()
                    slot.delete:Show()
                    set_button(slot.delete,not row.deleting)
                else
                    slot.name:Hide()
                    slot.status:Hide()
                    slot.delete:Hide()
                end
                set_button(slot.name,true,row~=nil and selected_id==row.id)
            end
            if not selected then selected_id=nil end
            if #state.roster==0 then empty:Show() else empty:Hide() end
            set_button(all,true,selected==nil)
            target_text:SetString("操作对象："..(selected and selected.name or "全体"))
            local can_control=selected==nil and any_bound or selected~=nil and selected.bound and not selected.dead
            local can_autonomy=any_bound and state.bridge_enabled
            set_button(autonomous,can_autonomy,is_auto and not state.paused)
            autonomous:SetTooltip(not any_bound and "请先召唤伙伴。"
                or not state.bridge_enabled and "请先配置本机桥接。"
                or "围绕长期目标持续行动。")
            set_button(edit,can_autonomy and is_auto)
            set_button(stop,can_control)
            set_button(follow,can_control)
            set_button(summon,#state.roster<Names.MAX)
            if selected then
                if selected.bound then release:Show() else release:Hide() end
                if not selected.bound and not selected.dead then recall:Show() else recall:Hide() end
            else
                recall:Hide()
                release:Hide()
            end
        end

    end)
end

return M
