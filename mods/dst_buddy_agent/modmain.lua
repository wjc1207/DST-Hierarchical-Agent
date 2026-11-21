local G=GLOBAL
local json=require("json")
local Server=require("jev_companion_server")
local Client=require("jev_companion_client")
local World=require("jev_companion_world")
local Presentation=require("jev_companion_presentation")
local UI=require("jev_companion_ui")
local Pairing=require("jev_bridge_pairing")
local MOD_DIR=type(MODROOT)=="string" and MODROOT or ""
local NS="jev_ai_companion"
local config={}
local pairing_version=0
local pairing_loaded=false
do
    local configured,file_cfg=G.pcall(require,"jev_companion_config")
    if configured then
        local valid=Pairing.validate_config(file_cfg)
        if valid then valid.source="config_file";config=valid end
    end
end
local client
local clock=G.walltime or function() return G.GetTime() end
local function game_paused()
    return G.TheNet and G.TheNet:IsServerPaused() or false
end
local function notice(text) if G.ChatHistory then G.ChatHistory:SendCommandResponse("[Companion] "..text) end end
local function log(event,reason,details)
    local row={event=event,reason=reason,version="0.8.0"}
    for key,value in pairs(details or {}) do row[key]=value end
    print("[JEV_COMPANION]"..json.encode(row))
end
local function apply_config(cfg)
    config=cfg or {}
    local enabled=Pairing.validate_config(config)~=nil
    if client then client:set_http_enabled(enabled) end
end
local function ensure_pairing_loaded()
    if pairing_loaded or not (G.TheSim and G.TheSim.GetPersistentString) then return end
    pairing_loaded=true
    local version=pairing_version
    Pairing.load(G,"jev_buddy_bridge_v1","jev_companion_config",function(loaded)
        if version~=pairing_version then return end
        apply_config(loaded)
    end)
end
AddPrefabPostInit("wilson",function(inst) Presentation.attach(G,inst) end)
local function install_server(world)
    if not world.ismastersim or world.components.jev_companion_persistence then return end
    world:AddComponent("jev_companion_persistence")
    world.components.jev_companion_persistence.server=Server.new_team(World.new(G,function(player,packet)
        G.SendModRPCToClient(GetClientModRPC(NS,"reply"),player.userid,json.encode(packet))
    end,Client.validate_text,function(value) return #json.encode(value) end,function(event,details) log(event,nil,details) end))
end
AddPrefabPostInit("forest",install_server)
AddPrefabPostInit("cave",install_server)
AddPrefabPostInit("world",install_server)
local function get_server()
    local persistence=G.TheWorld and G.TheWorld.components.jev_companion_persistence
    return persistence and persistence.server
end
local function ensure_client()
    local player=G.ThePlayer
    if not player or not player:IsValid() then return end
    ensure_pairing_loaded()
    if client and client.player==player then return client end
    local enabled=Pairing.validate_config(config)~=nil
    client=Client.new_team({nonce=tostring(player.GUID)..":"..string.format("%.0f",clock()*1000),now=clock,sim_now=G.GetTime,paused=game_paused,http_enabled=enabled,
        notice=notice,send=function(q) G.SendModRPCToServer(GetModRPC(NS,"request"),json.encode(q)) end,
        http=function(payload,callback)
            local active=Pairing.validate_config(config)
            if not active then callback(nil);return end
            payload.token=active.token
            local encode=json.encode_compliant or json.encode
            local body=encode(payload)
            G.TheSim:QueryServer(active.url,function(raw,ok,code)
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
    local service=get_server()
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
        local p_matched,p_action,p_cfg,p_err=Pairing.parse(text,"/buddy",8766,false)
        if p_matched then
            if p_err then notice(p_err);return end
            if p_action=="pair" then
                pairing_version=pairing_version+1
                pairing_loaded=true
                p_cfg.source="persistent"
                Pairing.save(G,"jev_buddy_bridge_v1",p_cfg)
                apply_config(p_cfg)
                ensure_client()
                notice("已配对本机伙伴桥接（127.0.0.1:"..tostring(p_cfg.port).."，"..Pairing.mask(p_cfg.token).."），已立即生效，工坊更新不会覆盖。")
            elseif p_action=="unpair" then
                pairing_version=pairing_version+1
                pairing_loaded=true
                Pairing.clear(G,"jev_buddy_bridge_v1")
                apply_config(nil)
                notice("已解除本机伙伴桥接配对并停止本机桥接请求。")
            elseif p_action=="setup" then
                if MOD_DIR~="" then
                    notice("模组目录："..MOD_DIR)
                else
                    notice("模组目录：请在 Steam 创意工坊目录 steamapps/workshop/content/322330/ 下找到本模组文件夹。")
                end
                notice("安装步骤：打开该目录双击 1_install_agent.bat（macOS/Linux 运行 1_install_agent.sh），再双击 2_run_buddy.bat，最后粘贴配对口令 /buddy pair <token> 即可。")
            elseif p_action=="status" then
                ensure_pairing_loaded()
                local active=Pairing.validate_config(config)
                if not active then
                    notice("本机伙伴桥接：未配对（输入 /buddy setup 查看安装目录，或使用 /buddy pair <token> 配对）。")
                else
                    notice("本机伙伴桥接：已配对（127.0.0.1:"..tostring(active.port).."，"
                        ..Pairing.mask(active.token).."，来源："
                        ..(config.source=="persistent" and "本地持久化" or "配置文件").."）。")
                end
            end
            return
        end
        local command=text:match("^/buddy%s+(.+)$")
        local controller=ensure_client()
        if text=="/buddy" or command then if controller then controller:command(command or "help") end;return end
        if controller and not text:match("^/") then controller:chat(text) end
        return run(self,...)
    end
end)
AddSimPostInit(function()
    if not G.TheWorld then return end
    if not (G.TheNet and G.TheNet:IsDedicated()) then ensure_pairing_loaded() end
    local server=get_server()
    log("loaded",G.TheWorld.ismastersim and "server" or "client")
    local world=G.TheWorld
    local frontend=G.TheFrontEnd
    if frontend and frontend.StartUpdatingWidget and not (G.TheNet and G.TheNet:IsDedicated()) then
        local heartbeat={enabled=true,OnUpdate=function()
            if game_paused() and client and client.player==G.ThePlayer and client.player:IsValid() then
                client:tick(true)
            end
        end}
        frontend:StartUpdatingWidget(heartbeat)
        world:ListenForEvent("onremove",function()
            heartbeat.enabled=false
            frontend:StopUpdatingWidget(heartbeat)
        end)
    end
    -- The simulation scheduler itself stops while the server is paused.
    world:DoPeriodicTask(.1,function()
        if server then
            local ok,error=G.pcall(function() server:tick() end)
            if not ok then log("tick_error",tostring(error):sub(1,200)) end
        end
        if client and client.player:IsValid() and not game_paused() then
            local ok,error=G.pcall(function() client:tick() end)
            if not ok then log("client_error",tostring(error):sub(1,200)) end
        end
    end)
end)
