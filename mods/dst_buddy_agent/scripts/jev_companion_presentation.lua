-- Reversible presentation layer for companion display names and custom character skins.
local M={}
M.SKINS={
    deepseek="skin_deepseek",
}
local BODY_SYMBOLS={
    "headbase","headbase_hat","hair","hair_hat","hairpigtails","tail","face","cheeks",
    "torso","torso_pelvis","skirt","torso_wide","torso_pelvis_wide","skirt_wide",
    "arm_upper","arm_upper_skin","arm_lower","hand","leg","foot",
}
local BLANK_SYMBOLS={"beard","arm_lower_cuff","arm_lower_narrow"}

local function same_override(b1,s1,b2,s2)
    return b1==b2 and s1==s2
end

function M.resolve_skin(raw)
    if raw==nil then return "" end
    if type(raw)~="string" then return nil end
    local key=raw:match("^%s*(.-)%s*$"):lower()
    if key=="" or key=="off" or key=="none" or key=="default" or key=="原版" or key=="关闭" then
        return ""
    end
    if key=="deepseek" or key=="skin_deepseek" or key=="dschan" or key=="小鲸" or key=="大肥鱼" then
        return "deepseek"
    end
    return nil
end

function M.infer_skin(name)
    if type(name)~="string" or name=="" then return "" end
    local low=name:lower()
    if low:find("deepseek",1,true) or low:find("deep_seek",1,true)
        or low:find("dschan",1,true) or name:find("小鲸",1,true) or name:find("大肥鱼",1,true) then
        return "deepseek"
    end
    return ""
end

local function release_symbol(anim,ledger,symbol)
    local saved=ledger[symbol]
    if not saved then return end
    if anim.GetSymbolOverride then
        local cur_build,cur_sym=anim:GetSymbolOverride(symbol)
        if same_override(cur_build,cur_sym,saved.ours_build,saved.ours_symbol) then
            if saved.build==nil then anim:ClearOverrideSymbol(symbol)
            elseif saved.skin and anim.OverrideSkinSymbol then anim:OverrideSkinSymbol(symbol,saved.build,saved.source)
            else anim:OverrideSymbol(symbol,saved.build,saved.source) end
        end
    else
        anim:ClearOverrideSymbol(symbol)
    end
    ledger[symbol]=nil
end

local function clear_skin(a)
    local anim=a and a.AnimState
    local ledger=a and a._skin_overrides
    if anim and ledger then
        for symbol in pairs(ledger) do release_symbol(anim,ledger,symbol) end
    end
    if a and a._skin_active then
        a._skin_active=nil
        if anim and anim.ShowSymbol then
            for _,symbol in ipairs(BLANK_SYMBOLS) do anim:ShowSymbol(symbol) end
        end
        if a.components and a.components.skinner and a.components.skinner.SetSkinMode then
            pcall(function() a.components.skinner:SetSkinMode(a.components.skinner.skin_mode) end)
        end
    end
end

local function apply_symbol(anim,ledger,symbol,build,target)
    local saved=ledger[symbol]
    local cur_build,cur_sym
    if anim.GetSymbolOverride then cur_build,cur_sym=anim:GetSymbolOverride(symbol) end
    local intact=saved~=nil and anim.GetSymbolOverride~=nil
        and same_override(cur_build,cur_sym,saved.ours_build,saved.ours_symbol)
    if not intact then
        local is_skin=cur_build~=nil and anim.IsSkinBuild~=nil and anim:IsSkinBuild(cur_build) or false
        saved={build=cur_build,source=cur_sym,skin=is_skin}
        ledger[symbol]=saved
    end
    if not intact or saved.target_build~=build or saved.target_symbol~=target then
        anim:OverrideSymbol(symbol,build,target)
        if anim.GetSymbolOverride then
            saved.ours_build,saved.ours_symbol=anim:GetSymbolOverride(symbol)
        else
            saved.ours_build,saved.ours_symbol=build,target
        end
        saved.target_build,saved.target_symbol=build,target
    end
end

local function apply_skin(G,a)
    if not a or not a:IsValid() then return end
    local skin_id=a._jev_companion_skin and a._jev_companion_skin:value() or ""
    local build=M.SKINS[skin_id]
    local anim=a.AnimState
    local dead_or_alt=a:HasTag("playerghost") or a:HasTag("corpse") or a:HasTag("wereplayer")
        or (a.components and a.components.health and a.components.health:IsDead())
    if not build or not anim or dead_or_alt then
        clear_skin(a)
        if not build and a._skin_task then
            a._skin_task:Cancel();a._skin_task=nil
        end
        return
    end
    a._skin_active=true
    a._skin_overrides=a._skin_overrides or {}
    local ledger=a._skin_overrides
    if anim.ClearSymbolExchanges then anim:ClearSymbolExchanges() end
    for _,symbol in ipairs(BODY_SYMBOLS) do
        apply_symbol(anim,ledger,symbol,build,symbol)
        if anim.ShowSymbol then anim:ShowSymbol(symbol) end
    end
    for _,symbol in ipairs(BLANK_SYMBOLS) do
        apply_symbol(anim,ledger,symbol,build,"long_hair_blank")
        if anim.HideSymbol then anim:HideSymbol(symbol) end
    end
    if not a._skin_task and not (G.TheNet and G.TheNet:IsDedicated()) and a.DoPeriodicTask then
        a._skin_task=a:DoPeriodicTask(0.25,function(inst) apply_skin(G,inst) end)
    end
end

local function apply(G,a)
    local name=a._jev_companion_displayname and a._jev_companion_displayname:value() or ""
    if name=="" then return end
    a._jev_companion_name=name;a.name=name
    local talker=a.components.talker
    if talker then
        talker.disablefollowtext=false
        if not talker.offset and not talker.offset_fn then talker.offset=G.Vector3(0,-400,0) end
    end
end

function M.attach(G,a)
    if not a then return end
    if not a._jev_companion_displayname then
        a._jev_companion_displayname=G.net_string(a.GUID,"jev_companion.displayname","jev_companion_namedirty")
        local previous=a.displaynamefn
        a.displaynamefn=function(inst)
            local name=inst._jev_companion_displayname and inst._jev_companion_displayname:value() or ""
            if name~="" then return name end
            return previous and previous(inst) or nil
        end
        if not G.TheWorld.ismastersim then
            a:ListenForEvent("jev_companion_namedirty",function() apply(G,a) end)
            apply(G,a)
        end
    end
    if not a._jev_companion_skin then
        a._jev_companion_skin=G.net_string(a.GUID,"jev_companion.skin","jev_companion_skindirty")
        if not G.TheWorld.ismastersim then
            a:ListenForEvent("jev_companion_skindirty",function() apply_skin(G,a) end)
        end
        local function refresh() a:DoTaskInTime(0,function() apply_skin(G,a) end) end
        a:ListenForEvent("equip",refresh)
        a:ListenForEvent("unequip",refresh)
        a:ListenForEvent("ms_respawnedfromghost",refresh)
        a:ListenForEvent("transformnormal",refresh)
        a:ListenForEvent("mightiness_statechange",refresh)
        a:ListenForEvent("bloomchange",refresh)
        a:ListenForEvent("death",function() clear_skin(a) end)
        a:ListenForEvent("ms_becameghost",function() clear_skin(a) end)
        a:ListenForEvent("transformwere",function() clear_skin(a) end)
        apply_skin(G,a)
    end
end

function M.set_name(G,a,name)
    if not G.TheWorld.ismastersim or a.userid and a.userid~="" or type(name)~="string" or name=="" then return false end
    M.attach(G,a)
    if a._jev_companion_displayname:value()~=name then a._jev_companion_displayname:set(name) end
    apply(G,a);return true
end

function M.set_skin(G,a,skin_id)
    if not G.TheWorld.ismastersim or not a or not a:IsValid() then return false,"invalid_actor" end
    local clean=M.resolve_skin(skin_id)
    if clean==nil then return false,"unknown_skin" end
    M.attach(G,a)
    if a._jev_companion_skin:value()~=clean then a._jev_companion_skin:set(clean) end
    apply_skin(G,a)
    return true,clean
end

return M
