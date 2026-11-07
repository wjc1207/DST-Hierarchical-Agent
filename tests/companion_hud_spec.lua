package.path="./mods/jev_ai_companion/scripts/?.lua;"..package.path

local UI=require("jev_companion_ui")
local pixels,units_to_pixels,mouse_y,held=1080,0,600,false
local saved,writes=nil,0

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
        "ScaleToSize","SetTint","SetTooltip","StartUpdating"}) do
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
package.preload["screens/redux/inputdialog"]=function() return function() end end

local G={MOUSEBUTTON_LEFT=1,TheSim={},TheInput={}}
function G.TheSim:GetScreenSize() return 1920,pixels end
function G.TheSim:GetPersistentString(key,callback)
    assert(key=="jev_companion_hud_position")
    callback(saved~=nil,saved)
end
function G.TheSim:SetPersistentString(key,raw,encode)
    assert(key=="jev_companion_hud_position" and not encode)
    saved,writes=raw,writes+1
end
function G.TheInput:GetScreenPosition() return {y=mouse_y} end
function G.TheInput:IsMouseDown(button) assert(button==1);return held end

local function create()
    local root=widget()
    -- The native UITransform has no GetWorldScale method.
    UI.install(G,function(name,attach)
        assert(name=="widgets/controls")
        attach({topright_root=root})
    end,function()
        return {panel_state=function() return {mode="instruction",roster={}} end}
    end)
    local dock=root.children[1]
    return dock,dock.children[1],dock.children[2],dock.children[3]
end

local dock,panel,toggle,grip=create()
assert(dock.y==-320 and not panel.shown and grip.text==nil)
units_to_pixels=1.5
dock:OnUpdate(.25)
toggle.onclick()
assert(panel.shown and dock.y==-320 and panel.y+151*panel.scale==-25)
toggle.onclick()

held=true
grip:OnMouseButton(1,true)
mouse_y=450
dock:OnUpdate(.01)
assert(dock.y==-420 and writes==0)
held=false
dock:OnUpdate(.01)
assert(saved=="420" and writes==1 and not panel.shown)

local restored=create()
assert(restored.y==-420)
toggle.onclick()
assert(panel.shown and dock.y==-375 and -(dock.y+panel.y)+151*panel.scale==702)
grip:OnMouseButton(1,true)
grip:OnMouseButton(1,false)
toggle.onclick()
assert(not panel.shown and dock.y==-420 and writes==1)

pixels=540
dock:OnUpdate(.25)
assert(dock.y==-325)
toggle.onclick()
assert(panel.shown and dock.y==-35 and -(dock.y+panel.y)+151*panel.scale<=342)

print("ok 1 - HUD starts without GetWorldScale, drags at the measured scale and restores the saved position")
print("1..1")
