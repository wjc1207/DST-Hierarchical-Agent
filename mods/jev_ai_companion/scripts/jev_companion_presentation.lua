-- One field on every Wilson, declared on both peers; ordinary players keep an empty value.
local M={}
local function apply(G,a)
    local name=a._jev_companion_displayname:value()
    if name=="" then return end
    a._jev_companion_name=name;a.name=name
    local talker=a.components.talker
    if talker then
        talker.disablefollowtext=false
        if not talker.offset and not talker.offset_fn then talker.offset=G.Vector3(0,-400,0) end
    end
end
function M.attach(G,a)
    if a._jev_companion_displayname then return end
    a._jev_companion_displayname=G.net_string(a.GUID,"jev_companion.displayname","jev_companion_namedirty")
    local previous=a.displaynamefn
    a.displaynamefn=function(inst)
        local name=inst._jev_companion_displayname:value()
        if name~="" then return name end
        return previous and previous(inst) or nil
    end
    if not G.TheWorld.ismastersim then
        a:ListenForEvent("jev_companion_namedirty",function() apply(G,a) end)
        apply(G,a)
    end
end
function M.set_name(G,a,name)
    if not G.TheWorld.ismastersim or a.userid and a.userid~="" or type(name)~="string" or name=="" then return false end
    M.attach(G,a)
    if a._jev_companion_displayname:value()~=name then a._jev_companion_displayname:set(name) end
    apply(G,a);return true
end
return M
