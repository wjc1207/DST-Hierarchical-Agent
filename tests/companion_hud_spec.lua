package.path="./mods/dst_buddy_agent/scripts/?.lua;./mods/dst_player_agent/scripts/?.lua;"..package.path

local UI=require("jev_companion_ui")
local PlayerUI=require("jev_autonomy_ui")
local Autonomy=require("jev_autonomy")
local pixels,units_to_pixels,mouse_y,held=1080,0,600,false
local saved,writes={},0

local function widget(text)
    local w={text=text,children={},shown=true,enabled=true,inst={IsValid=function() return true end}}
    function w:AddChild(child)
        child.parent=self
        self.children[#self.children+1]=child
        return child
    end
    function w:SetPosition(x,y) self.x,self.y=x,y end
    function w:GetWorldPosition()
        local y=self.parent and self.parent:GetWorldPosition().y+(self.y or 0)*units_to_pixels or pixels
        return {y=y}
    end
    function w:GetScale() return {y=1} end
    function w:SetScale(scale) self.scale=scale end
    function w:Enable() self.enabled=true end
    function w:Disable() self.enabled=false end
    function w:IsSelected() return self.selected end
    function w:Select() self.selected=true end
    function w:Unselect() self.selected=false end
    function w:Show() self.shown=true end
    function w:Hide() self.shown=false end
    function w:SetText(value) self.text=value end
    function w:SetOnClick(callback) self.onclick=callback end
    w.SetString=w.SetText
    for _,method in ipairs({"SetTextSize","SetTextSelectedColour","SetRegionSize","SetHAlign","SetClickable",
        "ScaleToSize","SetTint","SetTooltip","StartUpdating","MoveToBack"}) do
        w[method]=function() end
    end
    return w
end

package.preload["widgets/widget"]=function() return widget end
package.preload["widgets/image"]=function() return widget end
package.preload["widgets/text"]=function() return widget end
package.preload["widgets/redux/templates"]=function()
    return {StandardButton=function(cb,text)
        local w=widget(text)
        w.onclick=cb
        return w
    end}
end
local dialog
package.preload["screens/redux/inputdialog"]=function()
    return function(_,buttons)
        local d={buttons=buttons,edit_text={SetTextLengthLimit=function() end}}
        function d:OverrideText(text) self.text=text end
        function d:GetActualString() return self.text end
        function d:Close() dialog=nil end
        return d
    end
end
string.utf8len=function(text)
    local _,count=text:gsub("[^\128-\191]","")
    return count
end

local G={MOUSEBUTTON_LEFT=1,TheSim={},TheInput={},ThePlayer={IsValid=function() return true end},
    TheFrontEnd={PushScreen=function(_,d) dialog=d end}}
function G.TheSim:GetScreenSize() return 1920,pixels end
function G.TheSim:GetPersistentString(key,callback)
    callback(saved[key]~=nil,saved[key])
end
function G.TheSim:SetPersistentString(key,raw,encode)
    assert(not encode)
    saved[key],writes=raw,writes+1
end
function G.TheInput:GetScreenPosition() return {y=mouse_y} end
function G.TheInput:IsMouseDown(button) assert(button==1);return held end

local function create()
    local root=widget()
    local controls={topright_root=root}
    -- The native UITransform has no GetWorldScale method.
    UI.install(G,function(name,attach)
        assert(name=="widgets/controls")
        attach(controls)
    end,function()
        return {panel_state=function() return {mode="instruction",roster={}} end}
    end)
    local dock=root.children[1]
    return dock,root.children[2],dock.children[1],dock.children[2],controls
end

local dock,panel,toggle,grip,controls=create()
assert(dock.y==-320 and not panel.shown and grip.text==nil)
units_to_pixels=1.5
dock:OnUpdate(.25)
toggle.onclick()
assert(panel.shown and dock.y==-320 and panel.y+151*panel.scale==dock.y-25)
toggle.onclick()

held=true
grip:OnMouseButton(1,true)
mouse_y=450
dock:OnUpdate(.01)
assert(dock.y==-420 and writes==0)
held=false
dock:OnUpdate(.01)
assert(saved.jev_companion_hud_position=="420" and writes==1 and not panel.shown)

local restored=create()
assert(restored.y==-420)
toggle.onclick()
assert(panel.shown and dock.y==-375 and -panel.y+151*panel.scale==702)
grip:OnMouseButton(1,true)
grip:OnMouseButton(1,false)
toggle.onclick()
assert(not panel.shown and dock.y==-420 and writes==1)

pixels=540
dock:OnUpdate(.25)
assert(dock.y==-325)
toggle.onclick()
assert(panel.shown and dock.y==-35 and -panel.y+151*panel.scale<=342)

print("ok 1 - HUD starts without GetWorldScale, drags at the measured scale and restores the saved position")
pixels=1080
dock:OnUpdate(.25)
PlayerUI.install(G,function(name,attach)
    if name=="widgets/controls" then attach(controls) end
end)
local root=controls.topright_root
local player_dock,player_panel=root.children[4],root.children[5]
local player_toggle,player_grip=player_dock.children[1],player_dock.children[2]
assert(player_toggle.text=="角色操控" and player_grip.text==nil and player_dock.y==-380)
player_toggle.onclick()
assert(player_panel.shown and not panel.shown)
toggle.onclick()
assert(panel.shown and not player_panel.shown)
player_toggle.onclick()

local function button(text)
    for _,child in ipairs(player_panel.children) do
        if child.text==text and child.onclick then
            assert(child.enabled and not child.selected and child.shown,text)
            child.onclick()
            return
        end
    end
    error("missing button "..text)
end

local state=Autonomy.get(G.ThePlayer)
assert(not state.enabled and not state.listening)
button("聊天监听：关")
assert(state.listening and not state.enabled)
button("编辑目标")
assert(dialog)
dialog.text="build camp"
dialog.buttons[1].cb()
assert(not dialog and state.goal=="build camp" and not state.enabled)
button("开始自主")
assert(state.enabled)
button("指令")
assert(state.mode=="instruction" and not state.enabled and state.goal=="build camp")
Autonomy.submit_message(G.ThePlayer,"collect logs")
player_dock:OnUpdate(.25)
button("暂停并接管")
assert(not state.enabled and state.paused)
button("继续")
assert(state.enabled and not state.paused and state.instruction=="collect logs")
assert(Autonomy.apply_feedback(G.ThePlayer,{id="done",text="done",revision=state.revision,finish=true},function() end))
player_dock:OnUpdate(.25)
for _,child in ipairs(player_panel.children) do
    if child.text=="等待指令" and child.onclick then assert(not child.enabled) end
end
player_toggle.onclick()
held=true
player_grip:OnMouseButton(1,true)
mouse_y=mouse_y-45
player_dock:OnUpdate(.01)
held=false
player_dock:OnUpdate(.01)
assert(saved.jev_player_hud_position=="410" and saved.jev_companion_hud_position=="420")
print("ok 2 - both panels share the layout with independent positions, and player controls stop and resume safely")

local function file(path)
    local f=assert(io.open(path,"rb"))
    local contents=f:read("*a")
    f:close()
    return contents
end
assert(file("mods/dst_buddy_agent/scripts/jev_hud_dock.lua")==file("mods/dst_player_agent/scripts/jev_hud_dock.lua"))
print("1..2")
