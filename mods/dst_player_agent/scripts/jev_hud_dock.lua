-- Shared HUD layout. Each independently installable mod ships this module.
local M={}
local EDGE_MARGIN,BUTTON_HEIGHT,PANEL_GAP=18,34,8

function M.button(parent,text,x,y,width,onclick)
    local Templates=require("widgets/redux/templates")
    local b=parent:AddChild(Templates.StandardButton(onclick,text,{width,BUTTON_HEIGHT}))
    b:SetPosition(x,y,0)
    b:SetTextSize(19)
    b:SetTextSelectedColour(1,.81,.44,1)
    return b
end

function M.label(G,parent,x,y,width,size)
    local Text=require("widgets/text")
    local t=parent:AddChild(Text(G.CHATFONT,size,""))
    t:SetPosition(x,y,0)
    t:SetRegionSize(width,27)
    t:SetHAlign(G.ANCHOR_LEFT)
    t:SetClickable(false)
    return t
end

function M.short(text,limit)
    return text:utf8len()>limit and text:utf8sub(1,limit-1).."…" or text
end

function M.set_button(b,enabled,selected)
    if enabled and not b.enabled then b:Enable()
    elseif not enabled and b.enabled then b:Disable() end
    if selected and not b:IsSelected() then b:Select()
    elseif not selected and b:IsSelected() then b:Unselect() end
end

function M.prompt(G,title,initial,limit,submit)
    local InputDialog=require("screens/redux/inputdialog")
    local dialog
    local function confirm()
        if submit(dialog:GetActualString()) then dialog:Close() end
    end
    dialog=InputDialog(title,{
        {text="确认",cb=confirm},
        {text="取消",cb=function() dialog:Close() end},
    },true)
    dialog.edit_text:SetTextLengthLimit(limit)
    dialog:OverrideText(initial or "")
    dialog.edit_text.OnTextEntered=confirm
    G.TheFrontEnd:PushScreen(dialog)
end

function M.new(G,controls,options,refresh)
    local Widget=require("widgets/widget")
    local Image=require("widgets/image")
    local root=controls.topright_root
    local dock=root:AddChild(Widget(options.name.."Dock"))
    local panel=root:AddChild(Widget(options.name.."Panel"))
    panel:MoveToBack() -- Keep both launchers clickable over an open panel.
    local background=panel:AddChild(Image("images/global.xml","square.tex"))
    background:ScaleToSize(options.width,options.height)
    background:SetTint(.12,.09,.06,.96)
    background.OnMouseButton=function() return true end

    local toggle=M.button(dock,options.title,-110,0,118)
    local grip=M.button(dock,nil,-31,0,36)
    grip.move_on_click=false
    grip:SetTooltip("按住左键上下拖动，松开后保存位置。")
    local open=false
    local depth,drag,position_changed=options.depth,nil,false

    -- Measure pixels per HUD unit with the supported position transform.
    local probe=root:AddChild(Widget(options.name.."ScaleProbe"))
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
        local scale=math.min(1,math.max(.1,available/options.height))
        local panel_top=BUTTON_HEIGHT/2+PANEL_GAP
        local bottom_margin=EDGE_MARGIN+(open and panel_top+options.height*scale or BUTTON_HEIGHT/2)
        local top_margin=math.min(EDGE_MARGIN+BUTTON_HEIGHT/2,height/2)
        local shown_depth=math.max(top_margin,math.min(height-bottom_margin,wanted_depth))
        if height==last_height and shown_depth==last_depth then return shown_depth end
        last_height,last_depth=height,shown_depth
        dock:SetPosition(0,-shown_depth,0)
        panel:SetScale(scale)
        panel:SetPosition(-EDGE_MARGIN-options.width*scale/2,-shown_depth-panel_top-options.height*scale/2,0)
        return shown_depth
    end

    local function close()
        open=false
        if controls._jev_close_hud_panel==close then controls._jev_close_hud_panel=nil end
        place(viewport(),depth)
        panel:Hide()
        toggle:SetText(options.title)
    end

    toggle:SetOnClick(function()
        if open then close();return end
        if controls._jev_close_hud_panel then controls._jev_close_hud_panel() end
        controls._jev_close_hud_panel=close
        open=true
        place(viewport(),depth)
        panel:Show()
        toggle:SetText(options.expanded_title)
        refresh()
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
            G.TheSim:SetPersistentString(options.position_key,tostring(math.floor(depth+.5)),false)
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
    G.TheSim:GetPersistentString(options.position_key,function(ok,raw)
        if not ok or position_changed or not dock.inst:IsValid() then return end
        local saved=tonumber(raw)
        if saved and saved>0 and saved<math.huge then
            depth=saved
            place(viewport(),depth)
        end
    end)
    dock:StartUpdating()
    return panel
end

return M
