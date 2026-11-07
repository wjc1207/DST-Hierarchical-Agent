local G=GLOBAL
local json=require("json")
local Server=require("jev_companion_team_server")
local Client=require("jev_companion_team_client")
local World=require("jev_companion_world")
local Presentation=require("jev_companion_presentation")
local UI=require("jev_companion_ui")
local NS="jev_ai_companion"
local configured,config=G.pcall(require,"jev_companion_config")
if not configured or type(config)~="table" then config={} end
local server,client
local clock=G.walltime or function() return G.GetTime() end
local function notice(text) if G.ChatHistory then G.ChatHistory:SendCommandResponse("[Companion] "..text) end end
local function log(event,reason,details)
    local row={event=event,reason=reason,version="0.7.0"}
    for key,value in pairs(details or {}) do row[key]=value end
    print("[JEV_COMPANION]"..json.encode(row))
end
AddPrefabPostInit("wilson",function(inst) Presentation.attach(G,inst) end)
local function ensure_server()
    if not server and G.TheWorld and G.TheWorld.ismastersim then
        server=Server.new(World.new(G,function(player,packet)
            G.SendModRPCToClient(GetClientModRPC(NS,"reply"),player.userid,json.encode(packet))
        end,Client.validate_text,function(value) return #json.encode(value) end,function(event,details) log(event,nil,details) end))
    end
    return server
end
local function ensure_client()
    local player=G.ThePlayer
    if not player or not player:IsValid() then return end
    if client and client.player==player then return client end
    local enabled=config.enabled==true and type(config.token)=="string" and #config.token>=32
        and type(config.url)=="string" and config.url:match("^http://127%.0%.0%.1:%d+/v1/exchange$")~=nil
    client=Client.new({nonce=tostring(player.GUID)..":"..string.format("%.0f",clock()*1000),now=clock,http_enabled=enabled,
        notice=notice,send=function(q) G.SendModRPCToServer(GetModRPC(NS,"request"),json.encode(q)) end,
        http=function(payload,callback)
            payload.token=config.token
            local encode=json.encode_compliant or json.encode
            local body=encode(payload)
            G.TheSim:QueryServer(config.url,function(raw,ok,code)
                if not ok or code~=200 then callback(nil);return end
                local decoded,result=G.pcall(json.decode,raw)
                callback(decoded and result or nil)
            end,"POST",body)
        end})
    client.player=player;return client
end
AddModRPCHandler(NS,"request",function(player,encoded)
    if type(encoded)~="string" or #encoded>4096 then return end
    local ok,q=G.pcall(json.decode,encoded)
    local service=ensure_server()
    if ok and service then
        local handled,error=G.pcall(function() service:request(player,q) end)
        if not handled then log("rpc_error",tostring(error):sub(1,200)) end
    end
end)
AddClientModRPCHandler(NS,"reply",function(encoded)
    if not client or type(encoded)~="string" or #encoded>65536 then return end
    local ok,packet=G.pcall(json.decode,encoded)
    if ok then client:receive(packet) end
end)
UI.install(G,AddClassPostConstruct,ensure_client)
AddClassPostConstruct("screens/chatinputscreen",function(screen)
    local run=screen.Run
    screen._jev_companion_accepts_chat=function(text)
        return client and client.player==G.ThePlayer and client:accepts_chat(text)
    end
    screen.Run=function(self,...)
        local text=self.chat_edit:GetString():match("^%s*(.-)%s*$")
        local command=text:match("^/buddy%s+(.+)$")
        local controller=ensure_client()
        if text=="/buddy" or command then if controller then controller:command(command or "help") end;return end
        if controller and not text:match("^/") then controller:chat(text) end
        return run(self,...)
    end
end)
AddSimPostInit(function()
    if not G.TheWorld then return end
    ensure_server();log("loaded",G.TheWorld.ismastersim and "server" or "client")
    G.TheWorld:DoPeriodicTask(.1,function()
        if server then
            local ok,error=G.pcall(function() server:tick() end)
            if not ok then log("tick_error",tostring(error):sub(1,200)) end
        end
        if client and client.player:IsValid() then
            local ok,error=G.pcall(function() client:tick() end)
            if not ok then log("client_error",tostring(error):sub(1,200)) end
        end
    end)
end)
