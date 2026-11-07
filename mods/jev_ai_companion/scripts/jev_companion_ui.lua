-- Client HUD backed by the same controller as /buddy.
local Names=require("jev_companion_roster")
local M={}
local POSITION_KEY="jev_companion_hud_position"
local DEFAULT_DEPTH=320
local PANEL_WIDTH,PANEL_HEIGHT=436,302
local EDGE_MARGIN=18
local BUTTON_HEIGHT,PANEL_GAP=34,8

local function clamp_depth(depth,height,bottom_margin)
    local top_margin=math.min(EDGE_MARGIN+BUTTON_HEIGHT/2,height/2)
    return math.max(top_margin,math.min(height-bottom_margin,depth))
end

function M.install(G,postconstruct,get_client)
    postconstruct("widgets/controls",function(controls)
        local Widget=require("widgets/widget")
        local Image=require("widgets/image")
        local Text=require("widgets/text")
        local Templates=require("widgets/redux/templates")
        local InputDialog=require("screens/redux/inputdialog")

        local function button(parent,text,x,y,width,onclick)
            local b=parent:AddChild(Templates.StandardButton(onclick,text,{width,BUTTON_HEIGHT}))
            b:SetPosition(x,y,0)
            b:SetTextSize(19)
            b:SetTextSelectedColour(1,.81,.44,1)
            return b
        end

        local function label(parent,x,y,width,size)
            local t=parent:AddChild(Text(G.CHATFONT,size,""))
            t:SetPosition(x,y,0)
            t:SetRegionSize(width,27)
            t:SetHAlign(G.ANCHOR_LEFT)
            t:SetClickable(false)
            return t
        end

        local function short(text,limit)
            return text:utf8len()>limit and text:utf8sub(1,limit-1).."…" or text
        end

        local function set_button(b,enabled,selected)
            if enabled and not b.enabled then b:Enable()
            elseif not enabled and b.enabled then b:Disable() end
            if selected and not b:IsSelected() then b:Select()
            elseif not selected and b:IsSelected() then b:Unselect() end
        end

        local root=controls.topright_root
        local dock=root:AddChild(Widget("JEVCompanionDock"))
        local panel=dock:AddChild(Widget("JEVCompanionPanel"))
        local background=panel:AddChild(Image("images/global.xml","square.tex"))
        background:ScaleToSize(PANEL_WIDTH,PANEL_HEIGHT)
        background:SetTint(.12,.09,.06,.96)
        background.OnMouseButton=function() return true end
        local title=label(panel,0,126,394,24)
        local goal_text=label(panel,-55,53,284,19)
        local target_text=label(panel,54,16,276,19)
        local empty=label(panel,0,-22,394,19)
        empty:SetString("尚无伙伴，点击下方的「召唤伙伴」。")

        local selected,selected_id
        local refresh
        local function act(fn)
            return function()
                local controller=get_client()
                if controller then fn(controller) end
                refresh()
            end
        end

        local function input(controller,title_text,initial,limit,submit)
            local dialog
            local function confirm()
                if get_client()~=controller then dialog:Close();return end
                if submit(dialog:GetActualString()) then dialog:Close();refresh() end
            end
            dialog=InputDialog(title_text,{
                {text="确认",cb=confirm},
                {text="取消",cb=function() dialog:Close() end},
            },true)
            dialog.edit_text:SetTextLengthLimit(limit)
            dialog:OverrideText(initial or "")
            dialog.edit_text.OnTextEntered=confirm
            G.TheFrontEnd:PushScreen(dialog)
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
            slot.status=label(panel,83,y,240,19)
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
                if row then
                    if selected_id==row.id then selected=row end
                    any_bound=any_bound or (row.bound and not row.dead)
                    slot.name:SetText(short(row.name,8))
                    slot.name:SetTooltip(row.name.." · "..row.status)
                    slot.status:SetString(short(row.status,12))
                    slot.name:Show()
                    slot.status:Show()
                else
                    slot.name:Hide()
                    slot.status:Hide()
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

        local open=false
        local toggle=button(dock,"伙伴",-110,0,118)
        local grip=button(dock,nil,-31,0,36)
        grip.move_on_click=false
        grip:SetTooltip("按住左键上下拖动，松开后保存位置。")
        local depth,drag,position_changed=DEFAULT_DEPTH,nil,false

        -- Measure pixels per HUD unit with the supported position transform.
        local probe=root:AddChild(Widget("JEVCompanionScaleProbe"))
        probe:SetPosition(0,-100,0)
        probe:SetClickable(false)

        local function viewport()
            local _,pixels=G.TheSim:GetScreenSize()
            local scale=(root:GetWorldPosition().y-probe:GetWorldPosition().y)/100
            if scale<=0 then scale=math.max(.01,root:GetScale().y) end
            return math.max(1,pixels)/scale,scale
        end

        local last_height,last_depth
        local function place(height,wanted_depth)
            local available=height-2*EDGE_MARGIN-BUTTON_HEIGHT-PANEL_GAP
            local scale=math.min(1,math.max(.1,available/PANEL_HEIGHT))
            local panel_top=BUTTON_HEIGHT/2+PANEL_GAP
            local bottom_margin=EDGE_MARGIN+(open and panel_top+PANEL_HEIGHT*scale or BUTTON_HEIGHT/2)
            local shown_depth=clamp_depth(wanted_depth,height,bottom_margin)
            if height==last_height and shown_depth==last_depth then return shown_depth end
            last_height,last_depth=height,shown_depth
            dock:SetPosition(0,-shown_depth,0)
            panel:SetScale(scale)
            panel:SetPosition(-EDGE_MARGIN-PANEL_WIDTH*scale/2,-panel_top-PANEL_HEIGHT*scale/2,0)
            return shown_depth
        end

        toggle:SetOnClick(function()
            open=not open
            place(viewport(),depth)
            if open then
                panel:Show()
                toggle:SetText("收起伙伴")
                refresh()
            else
                panel:Hide()
                toggle:SetText("伙伴")
            end
        end)

        local function update_drag()
            local height,scale=viewport()
            local delta=drag.mouse_y-G.TheInput:GetScreenPosition().y
            local shown_depth=place(height,drag.depth+delta/scale)
            depth=shown_depth==drag.depth and drag.saved_depth or shown_depth
        end

        local function finish_drag()
            if not drag then return end
            update_drag()
            if depth~=drag.saved_depth then
                G.TheSim:SetPersistentString(POSITION_KEY,tostring(math.floor(depth+.5)),false)
            end
            drag=nil
        end

        grip.OnMouseButton=function(_,mouse_button,down)
            if mouse_button~=G.MOUSEBUTTON_LEFT then return false end
            if down then
                position_changed=true
                drag={depth=place(viewport(),depth),saved_depth=depth,mouse_y=G.TheInput:GetScreenPosition().y}
            else
                finish_drag()
            end
            return true
        end

        local elapsed=0
        dock.OnUpdate=function(_,dt)
            if drag then
                if G.TheInput:IsMouseDown(G.MOUSEBUTTON_LEFT) then update_drag() else finish_drag() end
            end
            elapsed=elapsed+dt
            if elapsed>=.25 then
                elapsed=0
                if not drag then place(viewport(),depth) end
                if open then refresh() end
            end
        end

        panel:Hide()
        place(viewport(),depth)
        G.TheSim:GetPersistentString(POSITION_KEY,function(ok,raw)
            if not ok or position_changed or not dock.inst:IsValid() then return end
            local saved=tonumber(raw)
            if saved and saved>0 and saved<math.huge then
                depth=saved
                place(viewport(),depth)
            end
        end)
        dock:StartUpdating()
    end)
end

return M
