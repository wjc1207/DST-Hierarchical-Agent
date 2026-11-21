-- Local loopback bridge pairing persisted in Klei client_save (survives Steam Workshop updates).
-- Credentials are strictly local: never sent to server RPCs or public chat.
local json = require("json")
local M = {}

local function valid_port(port)
    local n = tonumber(port)
    if not n or n ~= math.floor(n) or n < 1 or n > 65535 then return nil end
    return n
end

function M.validate_token(token)
    if type(token) ~= "string" then return nil end
    if #token < 32 or #token > 128 then return nil end
    if not token:match("^[A-Za-z0-9_%-]+$") then return nil end
    return token
end

function M.validate_url(url)
    if type(url) ~= "string" then return nil end
    local port = url:match("^http://127%.0%.0%.1:(%d+)/v1/exchange$")
    if not port then return nil end
    local parsed_port = valid_port(port)
    if not parsed_port then return nil end
    return url, parsed_port
end

function M.validate_config(cfg)
    if type(cfg) ~= "table" or cfg.enabled ~= true then return nil end
    local token = M.validate_token(cfg.token)
    local url, port = M.validate_url(cfg.url)
    if not token or not url then return nil end
    return {enabled = true, url = url, token = token, port = port}
end

function M.build(token, port)
    local t = M.validate_token(token)
    if not t then return nil, "token 格式无效（需 32–128 位字母/数字/_/-）。" end
    local p = valid_port(port)
    if not p then return nil, "端口无效（需 1–65535）。" end
    return {enabled = true, url = "http://127.0.0.1:" .. tostring(p) .. "/v1/exchange", token = t, port = p}
end

function M.mask(token)
    if type(token) ~= "string" or #token < 8 then return "***" end
    return token:sub(1, 4) .. "..." .. token:sub(-4)
end

-- Parse local pairing slash commands.
-- When catch_all_prefix is true (e.g. /agent), any message starting with prefix is intercepted
-- so typos can never leak tokens into public server chat.
function M.parse(text, prefix, default_port, catch_all_prefix)
    if type(text) ~= "string" then return false end
    local trimmed = text:match("^%s*(.-)%s*$")
    local usage = "用法：" .. prefix .. " setup | " .. prefix .. " pair <token> [" .. tostring(default_port) .. "] | "
        .. prefix .. " unpair | " .. prefix .. " pair status"
    if trimmed == prefix then
        if catch_all_prefix then return true, nil, nil, usage end
        return false
    end
    local rest = trimmed:match("^" .. prefix .. "%s+(.+)$")
    if not rest then return false end
    local cmd, body = rest:match("^(%S+)%s*(.-)$")
    if cmd == "setup" then
        return true, "setup", nil, nil
    elseif cmd == "pair" then
        if body == "status" then
            return true, "status", nil, nil
        end
        local args = {}
        for part in body:gmatch("%S+") do args[#args + 1] = part end
        if #args < 1 or #args > 2 then
            return true, nil, nil, "用法：" .. prefix .. " pair <token> [端口，默认 " .. tostring(default_port) .. "]"
        end
        local cfg, err = M.build(args[1], args[2] or default_port)
        if not cfg then return true, nil, nil, err end
        return true, "pair", cfg, nil
    elseif cmd == "unpair" then
        if body ~= "" then
            return true, nil, nil, "用法：" .. prefix .. " unpair"
        end
        return true, "unpair", nil, nil
    elseif catch_all_prefix and cmd == "status" then
        return true, "status", nil, nil
    elseif catch_all_prefix then
        return true, nil, nil, usage
    end
    return false
end

function M.load(G, storage_key, fallback_module, callback)
    local function load_file()
        local ok, cfg = G.pcall(require, fallback_module)
        if ok then
            local valid = M.validate_config(cfg)
            if valid then
                valid.source = "config_file"
                return valid
            end
        end
        return nil
    end
    if G.TheSim and G.TheSim.GetPersistentString then
        G.TheSim:GetPersistentString(storage_key, function(ok, raw)
            if ok and type(raw) == "string" and raw ~= "" then
                local decoded_ok, data = G.pcall(json.decode, raw)
                if decoded_ok and type(data) == "table" then
                    if data.unpaired == true then
                        callback(nil)
                        return
                    end
                    local valid = M.validate_config(data)
                    if valid then
                        valid.source = "persistent"
                        callback(valid)
                        return
                    end
                end
            end
            callback(load_file())
        end)
    else
        callback(load_file())
    end
end

function M.save(G, storage_key, cfg)
    if not (G.TheSim and G.TheSim.SetPersistentString) then return false end
    local payload = json.encode({enabled = true, url = cfg.url, token = cfg.token})
    G.TheSim:SetPersistentString(storage_key, payload, false)
    return true
end

function M.clear(G, storage_key)
    if not (G.TheSim and G.TheSim.SetPersistentString) then return false end
    G.TheSim:SetPersistentString(storage_key, json.encode({enabled = false, unpaired = true}), false)
    return true
end

return M
