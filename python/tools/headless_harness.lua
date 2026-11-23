-- Headless real-engine test harness loaded inside Klei's dontstarve_dedicated_server_nullrenderer.
-- Exercises the unmodified mods/dst_buddy_agent Lua modules directly against real DST physics,
-- prefabs, StateGraphs, containers, burnables, cooking pots, and local HTTP exchange.
local json = require("json")

local HEADLESS = rawget(_G, "HEADLESS") or {}
rawset(_G, "HEADLESS", HEADLESS)

local function round2(v)
    return math.floor(v * 100 + 0.5) / 100
end

local function emit(tag, data)
    local encode = json.encode_compliant or json.encode
    print("[HEADLESS]" .. tag .. " " .. encode(data))
end

local chunk_seq = 0
local function emit_chunked(tag, data)
    local encode = json.encode_compliant or json.encode
    local raw = encode(data)
    chunk_seq = chunk_seq + 1
    local cid = chunk_seq
    local step = 1800
    local total = math.max(1, math.ceil(#raw / step))
    for i = 1, total do
        local slice = raw:sub((i - 1) * step + 1, i * step)
        print("[HEADLESS]CHUNK " .. tostring(cid) .. " " .. tag .. " " .. tostring(i) .. "/" .. tostring(total) .. " " .. slice)
    end
end

local function is_clear_land(x, z, radius)
    local map = TheWorld and TheWorld.Map
    if not map then return false end
    for dx = -radius, radius, 4 do
        for dz = -radius, radius, 4 do
            if dx * dx + dz * dz <= radius * radius then
                if not map:IsPassableAtPoint(x + dx, 0, z + dz) or not map:IsAboveGroundAtPoint(x + dx, 0, z + dz) then
                    return false
                end
            end
        end
    end
    return true
end

local function find_arena_center()
    local bx, bz = 0, 0
    for _, ent in pairs(Ents or {}) do
        if ent and ent:IsValid() and ent.prefab == "multiplayer_portal" then
            bx, _, bz = ent.Transform:GetWorldPosition()
            break
        end
    end
    for r = 0, 120, 8 do
        for step = 0, 15 do
            local ang = step * math.pi / 8
            local cx = bx + math.cos(ang) * r
            local cz = bz + math.sin(ang) * r
            if is_clear_land(cx, cz, 14) then
                return round2(cx), round2(cz)
            end
        end
    end
    return round2(bx), round2(bz)
end

local function inventory_counts(ent)
    local counts = {}
    local inv = ent and ent:IsValid() and ent.components and ent.components.inventory
    if not inv then return counts end
    for _, item in ipairs(inv:FindItems(function() return true end)) do
        if item and item:IsValid() then
            local n = item.components and item.components.stackable and item.components.stackable:StackSize() or 1
            counts[item.prefab] = (counts[item.prefab] or 0) + n
        end
    end
    for _, slot in pairs(EQUIPSLOTS or {}) do
        local eq = inv:GetEquippedItem(slot)
        if eq and eq:IsValid() then
            local n = eq.components and eq.components.stackable and eq.components.stackable:StackSize() or 1
            counts[eq.prefab] = (counts[eq.prefab] or 0) + n
        end
    end
    return counts
end

local function clear_inventory(ent)
    local inv = ent and ent:IsValid() and ent.components and ent.components.inventory
    if not inv then return end
    for _, slot in pairs(EQUIPSLOTS or {}) do
        local eq = inv:Unequip(slot)
        if eq and eq:IsValid() then eq:Remove() end
    end
    for _, item in ipairs(inv:FindItems(function() return true end)) do
        if item and item:IsValid() then
            inv:RemoveItem(item, true)
            item:Remove()
        end
    end
end

local function give_items(ent, prefab, count)
    local inv = ent and ent:IsValid() and ent.components and ent.components.inventory
    if not inv then return false end
    local remaining = count or 1
    while remaining > 0 do
        local item = SpawnPrefab(prefab)
        if not item then return false end
        if item.components and item.components.stackable then
            local maxsize = item.components.stackable.maxsize or 20
            local take = math.min(remaining, maxsize)
            item.components.stackable:SetStackSize(take)
            remaining = remaining - take
        else
            remaining = remaining - 1
        end
        inv:GiveItem(item)
    end
    return true
end

local function get_bound_buddy(id)
    local srv = HEADLESS.server
    if not srv or not srv.owners then return nil end
    local owner_rec = srv.owners[HEADLESS.owner.userid]
    if not owner_rec or not owner_rec.slots then return nil end
    local slot = owner_rec.slots[id or "buddy1"]
    if not slot or not slot.engine or not slot.engine.owners then return nil end
    local r = slot.engine.owners[HEADLESS.owner.userid]
    if r and r.buddy and r.buddy:IsValid() then return r.buddy end
    return nil
end

function HEADLESS.init(cfg)
    if HEADLESS.initialized then
        emit("READY", {x = HEADLESS.ax, z = HEADLESS.az, owner_guid = HEADLESS.owner.GUID})
        return
    end
    package.path = package.path .. ";" .. cfg.repo_buddy_scripts .. "/?.lua"
    for _, name in ipairs({
        "jev_companion_server", "jev_companion_world", "jev_companion_client",
        "jev_companion_actions", "jev_companion_storage", "jev_companion_places",
        "jev_companion_perception", "jev_companion_presentation", "jev_companion_roster",
        "jev_bridge_pairing",
    }) do
        package.loaded[name] = nil
    end

    local Server = require("jev_companion_server")
    local World = require("jev_companion_world")
    local Client = require("jev_companion_client")
    local Pairing = require("jev_bridge_pairing")

    HEADLESS.Pairing = Pairing
    HEADLESS.config = {enabled = true, url = cfg.url, token = cfg.token}

    local ax, az = find_arena_center()
    HEADLESS.ax, HEADLESS.az = ax, az

    local owner = SpawnPrefab("wilson")
    owner.userid = "KU_HEADLESS_OWNER"
    owner.Transform:SetPosition(ax, 0, az)
    if owner.entity and owner.entity.SetCanSleep then owner.entity:SetCanSleep(false) end
    if owner.components and owner.components.health then owner.components.health:SetInvincible(true) end
    if owner.components and owner.components.hunger then owner.components.hunger:Pause() end
    if owner.components and owner.components.inventory and owner.components.inventory.Open then
        owner.components.inventory:Open()
    end
    HEADLESS.owner = owner

    local client
    local function reply_fn(player, packet)
        local encoded = (json.encode_compliant or json.encode)(packet)
        TheWorld:DoTaskInTime(0, function()
            if not client then return end
            local ok, decoded = pcall(json.decode, encoded)
            if ok and decoded then client:receive(decoded) end
        end)
    end
    local function trace_fn(event, details)
        local row = {event = event}
        for k, v in pairs(details or {}) do row[k] = v end
        emit("TRACE", row)
    end

    local world_env = World.new(_G, reply_fn, Client.validate_text, function(v)
        return #((json.encode_compliant or json.encode)(v))
    end, trace_fn)

    -- Guard private map marker binding for synthetic headless owner without a real client socket.
    local orig_map_bind = world_env.map_bind
    world_env.map_bind = function(player, a)
        local ok, res, reason = pcall(orig_map_bind, player, a)
        if not ok then return false, "headless_no_client_map" end
        return res, reason
    end

    local server = Server.new_team(world_env)
    HEADLESS.world_env = world_env
    HEADLESS.server = server

    local clock = rawget(_G, "walltime") or function() return GetTime() end
    HEADLESS._http_callbacks = {}
    HEADLESS._http_req_id = 0
    local transport_mode = cfg.transport or "http"
    client = Client.new_team({
        nonce = "headless:" .. tostring(owner.GUID) .. ":" .. string.format("%.0f", clock() * 1000),
        now = clock,
        sim_now = GetTime,
        paused = function() return false end,
        http_enabled = true,
        notice = function(text) emit("NOTICE", {text = tostring(text)}) end,
        send = function(q)
            local encoded = (json.encode_compliant or json.encode)(q)
            TheWorld:DoTaskInTime(0, function()
                local ok, decoded = pcall(json.decode, encoded)
                if ok and decoded then
                    local handled, err = pcall(function() server:request(owner, decoded) end)
                    if not handled then emit("ERROR", {stage = "server_request", detail = tostring(err)}) end
                end
            end)
        end,
        http = function(payload, callback)
            payload.token = HEADLESS.config.token
            if transport_mode == "console" then
                HEADLESS._http_req_id = HEADLESS._http_req_id + 1
                local req_id = HEADLESS._http_req_id
                HEADLESS._http_callbacks[req_id] = callback
                emit_chunked("HTTP_REQ", {req_id = req_id, payload = payload})
                return
            end
            local body = (json.encode_compliant or json.encode)(payload)
            TheSim:QueryServer(HEADLESS.config.url, function(raw, ok, code)
                if not ok or code ~= 200 then
                    callback(nil)
                    return
                end
                local decoded_ok, result = pcall(json.decode, raw)
                callback(decoded_ok and result or nil)
            end, "POST", body)
        end,
    })
    client.player = owner
    HEADLESS.client = client

    function HEADLESS.http_reply(req_id, raw_json)
        local cb = HEADLESS._http_callbacks[req_id]
        HEADLESS._http_callbacks[req_id] = nil
        if not cb then return end
        if type(raw_json) ~= "string" then cb(nil); return end
        local ok, decoded = pcall(json.decode, raw_json)
        cb(ok and decoded or nil)
    end

    if cfg.time_scale and TheSim and TheSim.SetTimeScale then
        TheSim:SetTimeScale(cfg.time_scale)
    end

    HEADLESS.tick_task = TheWorld:DoPeriodicTask(0.1, function()
        local ok1, err1 = pcall(function() server:tick() end)
        if not ok1 then emit("ERROR", {stage = "server_tick", detail = tostring(err1)}) end
        local ok2, err2 = pcall(function() client:tick(false) end)
        if not ok2 then emit("ERROR", {stage = "client_tick", detail = tostring(err2)}) end
    end)

    HEADLESS.initialized = true
    emit("READY", {
        x = ax,
        z = az,
        owner_guid = owner.GUID,
        time_scale = cfg.time_scale or 1,
        world_id = TheWorld.meta and TheWorld.meta.session_identifier or nil,
    })
end

function HEADLESS.setup_arena(spec_json)
    local spec = json.decode(spec_json) or {}
    local ax, az = HEADLESS.ax, HEADLESS.az
    local radius = spec.clear_radius or 18
    local keep = {[HEADLESS.owner] = true}
    for _, id in ipairs({"buddy1", "buddy2"}) do
        local b = get_bound_buddy(id)
        if b then keep[b] = true end
    end

    for _, ent in ipairs(TheSim:FindEntities(ax, 0, az, radius, nil, {"INLIMBO"})) do
        if ent and ent:IsValid() and not keep[ent] and ent.prefab ~= "multiplayer_portal"
            and not ent:HasTag("player") and not ent:HasTag("CLASSIFIED") then
            ent:Remove()
        end
    end

    if spec.clear_owner_inventory then
        clear_inventory(HEADLESS.owner)
    end
    HEADLESS.owner.Transform:SetPosition(ax, 0, az)
    if spec.reset_owner then
        local o = HEADLESS.owner
        if o.components and o.components.health then o.components.health:SetPercent(1) end
        if o.components and o.components.hunger then o.components.hunger:SetPercent(1) end
        if o.components and o.components.sanity then o.components.sanity:SetPercent(1) end
        if o.components and o.components.temperature then o.components.temperature:SetTemperature(25) end
        o:ClearBufferedAction()
        if o.components and o.components.locomotor then
            o.components.locomotor:Clear()
            o.components.locomotor:Stop()
        end
    end

    if spec.reset_buddy then
        local b = get_bound_buddy("buddy1")
        if b then
            clear_inventory(b)
            b.Transform:SetPosition(ax + 2, 0, az)
            if b.components and b.components.health then b.components.health:SetPercent(1) end
            if b.components and b.components.hunger then b.components.hunger:SetPercent(1) end
            if b.components and b.components.sanity then b.components.sanity:SetPercent(1) end
            if b.components and b.components.temperature then b.components.temperature:SetTemperature(25) end
            b:ClearBufferedAction()
            if b.components and b.components.locomotor then
                b.components.locomotor:Clear()
                b.components.locomotor:Stop()
            end
        end
    end

    local spawned = {}
    for _, row in ipairs(spec.spawns or {}) do
        local ent = SpawnPrefab(row.prefab)
        if ent then
            local sx = ax + (row.dx or 0)
            local sz = az + (row.dz or 0)
            ent.Transform:SetPosition(sx, 0, sz)
            if ent.entity and ent.entity.SetCanSleep then ent.entity:SetCanSleep(false) end
            if row.fuel_percent and ent.components and ent.components.fueled then
                ent.components.fueled:SetPercent(row.fuel_percent)
            end
            if row.items and ent.components and ent.components.container then
                for _, it in ipairs(row.items) do
                    local obj = SpawnPrefab(it.prefab)
                    if obj then
                        if obj.components and obj.components.stackable and it.count then
                            obj.components.stackable:SetStackSize(it.count)
                        end
                        ent.components.container:GiveItem(obj)
                    end
                end
            end
            spawned[#spawned + 1] = {
                tag = row.tag or row.prefab,
                prefab = ent.prefab,
                guid = ent.GUID,
                x = round2(sx),
                z = round2(sz),
            }
        end
    end

    local b = get_bound_buddy("buddy1")
    if b then
        for _, it in ipairs(spec.give_buddy or {}) do
            give_items(b, it.prefab, it.count or 1)
        end
    end
    for _, it in ipairs(spec.give_owner or {}) do
        give_items(HEADLESS.owner, it.prefab, it.count or 1)
    end

    emit("ARENA_READY", {tag = spec.tag or "default", spawned = spawned})
end

function HEADLESS.command(cmd)
    if HEADLESS.client then HEADLESS.client:command(cmd) end
end

function HEADLESS.chat(text)
    if HEADLESS.client then HEADLESS.client:chat(text) end
end

function HEADLESS.set_listening(on)
    if HEADLESS.client then HEADLESS.client:set_listening(on == true) end
end

function HEADLESS.set_simulation(scan_radius, action_delay_ms)
    if HEADLESS.server and HEADLESS.server.set_simulation then
        HEADLESS.server:set_simulation({scan_radius = scan_radius, action_delay_ms = action_delay_ms})
    end
end

function HEADLESS.give_buddy(prefab, count, buddy_id)
    local b = get_bound_buddy(buddy_id or "buddy1")
    local ok = b ~= nil and give_items(b, prefab, count or 1) or false
    emit("GIVE_BUDDY", {ok = ok, prefab = prefab, count = count or 1})
end

function HEADLESS.inspect(tag)
    local ax, az = HEADLESS.ax, HEADLESS.az
    local b = get_bound_buddy("buddy1")
    local buddy_info = nil
    if b then
        local bx, _, bz = b.Transform:GetWorldPosition()
        buddy_info = {
            guid = b.GUID,
            x = round2(bx),
            z = round2(bz),
            health = b.components.health and round2(b.components.health.currenthealth) or nil,
            hunger = b.components.hunger and round2(b.components.hunger.current) or nil,
            inventory = inventory_counts(b),
        }
    end

    local buildings = {}
    local chests = {}
    for _, ent in ipairs(TheSim:FindEntities(ax, 0, az, 22, nil, {"INLIMBO", "FX", "DECOR"})) do
        if ent and ent:IsValid() then
            local ex, _, ez = ent.Transform:GetWorldPosition()
            if ent.prefab == "firepit" or ent.prefab == "campfire" or ent.prefab == "cookpot"
                or ent.prefab == "treasurechest" or ent.prefab == "icebox" or ent.prefab == "researchlab" then
                local row = {
                    guid = ent.GUID,
                    prefab = ent.prefab,
                    x = round2(ex),
                    z = round2(ez),
                    fuel = ent.components.fueled and round2(ent.components.fueled:GetPercent()) or nil,
                }
                if ent.components and ent.components.container then
                    local c_counts = {}
                    for slot = 1, ent.components.container:GetNumSlots() do
                        local item = ent.components.container:GetItemInSlot(slot)
                        if item and item:IsValid() then
                            local n = item.components.stackable and item.components.stackable:StackSize() or 1
                            c_counts[item.prefab] = (c_counts[item.prefab] or 0) + n
                        end
                    end
                    row.contents = c_counts
                    chests[#chests + 1] = row
                end
                buildings[#buildings + 1] = row
            end
        end
    end

    emit("INSPECT", {
        tag = tag or "inspect",
        sim_time = round2(GetTime()),
        owner_inventory = inventory_counts(HEADLESS.owner),
        owner = {
            guid = HEADLESS.owner.GUID,
            health = HEADLESS.owner.components.health and round2(HEADLESS.owner.components.health.currenthealth) or nil,
            hunger = HEADLESS.owner.components.hunger and round2(HEADLESS.owner.components.hunger.current) or nil,
            inventory = inventory_counts(HEADLESS.owner),
        },
        buddy = buddy_info,
        buildings = buildings,
        chests = chests,
    })
end

function HEADLESS.test_pairing()
    local P = HEADLESS.Pairing
    local tok = string.rep("a1b2c3d4", 8) -- 64 chars
    local ok, err = pcall(function()
        local m, act, cfg, e = P.parse("/buddy pair " .. tok, "/buddy", 8766, false)
        assert(m == true and act == "pair" and cfg and cfg.port == 8766 and cfg.token == tok and e == nil, "buddy pair default")

        local m2, act2, cfg2, e2 = P.parse("/agent pair " .. tok .. " 9001", "/agent", 8765, true)
        assert(m2 == true and act2 == "pair" and cfg2 and cfg2.port == 9001 and e2 == nil, "agent pair custom")

        local m3, act3, _, e3 = P.parse("/buddy setup", "/buddy", 8766, false)
        assert(m3 == true and act3 == "setup" and e3 == nil, "buddy setup")

        local m4, act4, _, e4 = P.parse("/agent setup", "/agent", 8765, true)
        assert(m4 == true and act4 == "setup" and e4 == nil, "agent setup")

        local m5, act5, _, e5 = P.parse("/agent unpair", "/agent", 8765, true)
        assert(m5 == true and act5 == "unpair" and e5 == nil, "agent unpair")

        local m6, act6, _, e6 = P.parse("/agent pair short", "/agent", 8765, true)
        assert(m6 == true and act6 == nil and type(e6) == "string", "agent reject short token locally")

        local m7 = P.parse("/buddy summon Wilson", "/buddy", 8766, false)
        assert(m7 == false, "buddy summon passes through")

        assert(P.mask(tok) == "a1b2...c3d4", "mask format")
    end)
    emit("PAIRING_TEST", {ok = ok, detail = ok and "passed" or tostring(err)})
end
