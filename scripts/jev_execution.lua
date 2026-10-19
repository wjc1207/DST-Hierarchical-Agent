-- Observe native requests and execution, including non-predicting clients.
-- A request alone is NEVER a start acknowledgement.
local M = {}
local function safe(fn, fallback)
    local ok, value = pcall(fn)
    if ok then return value end
    return fallback
end

function M.doing(player)
    local controller = player.components and player.components.playercontroller
    return safe(function() return controller:IsDoingOrWorking() end, false)
        or safe(function() return player.sg:HasStateTag("doing") end, false)
        or safe(function() return player:HasTag("doing") end, false)
end

local function native(player)
    return safe(function() return player:GetBufferedAction() end, nil)
end

local function identity(action)
    return { action = action.action and action.action.id,
        target_guid = action.target and action.target.GUID, recipe = action.recipe }
end

function M.request(player, action, now)
    local id = identity(action)
    if id.action ~= "PICK" and id.action ~= "PICKUP" and id.action ~= "BUILD" then return false end
    id.issued_at, id.saw_idle = now, not M.doing(player)
    player._jev_execution_request = id
    return true
end

function M.read(player, now)
    local doing, action = M.doing(player), native(player)
    local pending = player._jev_execution_request
    local current = action and identity(action) or {}
    if pending and now - pending.issued_at <= 3 then
        if not doing then pending.saw_idle = true end
        local matches = current.action == pending.action and current.target_guid == pending.target_guid
            and current.recipe == pending.recipe
        if doing and not pending.started_at and (matches or (pending.saw_idle and not current.action)) then
            pending.started_at = now
            pending.start_source = matches and "native_buffered" or "replicated_transition"
        end
        if pending.started_at and not doing and not pending.finished_at then pending.finished_at = now end
        if pending.finished_at == nil or now - pending.finished_at <= 0.8 then
            return { doing = doing, action = pending.action, target_guid = pending.target_guid,
                recipe = pending.recipe, issued_at = pending.issued_at, started_at = pending.started_at,
                confirmed_start = pending.started_at ~= nil, start_source = pending.start_source,
                stage = pending.finished_at and "finished_unverified" or pending.started_at and "started" or "requested" }
        end
    end
    current.doing = doing
    current.confirmed_start = doing and current.action ~= nil
    current.start_source = current.confirmed_start and "native_buffered" or nil
    return current
end

function M.attach(player, now, on_start)
    local controller = player.components and player.components.playercontroller
    if not controller or player._jev_execution_controller == controller then return end
    player._jev_execution_controller = controller
    local function request(action)
        if not M.request(player, action, now()) then return end
        local pending = player._jev_execution_request
        if player._jev_execution_poll then player._jev_execution_poll:Cancel() end
        if player.DoPeriodicTask then
            player._jev_execution_poll = player:DoPeriodicTask(1/30, function()
                if player._jev_execution_request ~= pending then return end
                M.read(player, now())
                if pending.started_at and not pending.emitted then
                    pending.emitted = true
                    if on_start then on_start() end
                end
                if pending.finished_at or now() - pending.issued_at > 3 then
                    if player._jev_execution_poll then player._jev_execution_poll:Cancel() end
                    player._jev_execution_poll = nil
                end
            end)
        end
    end
    local remote = controller.RemoteActionButton
    if remote then
        controller.RemoteActionButton = function(self, action, ...)
            if action then request(action) end
            return remote(self, action, ...)
        end
    end
    local build = controller.RemoteMakeRecipeFromMenu
    if build then
        controller.RemoteMakeRecipeFromMenu = function(self, recipe, ...)
            if recipe then request({ action = { id = "BUILD" }, recipe = recipe.name }) end
            return build(self, recipe, ...)
        end
    end
end

return M
