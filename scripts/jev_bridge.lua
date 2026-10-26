-- Single-flight asynchronous HTTP. Local leases expire; late callbacks cannot act.
local M = {}
local measurement=require("jev_timing")
-- Stable event identity, not a hash of the whole moving world. Positions,
-- nearest-target GUIDs, fuel ticks and ordinary hunger ticks are NOT events.
local function state_signature(s,status)
    local p=s.player or {}; local v=p.vitals or {}; local w=s.world or {}
    local graph=(s.navigation or {}).graph or {}
    local parts={tostring(p.guid),tostring(v.dead),tostring(v.health),tostring(w.day),tostring(w.phase),
        tostring(math.floor((w.time or 0)*4)),
        tostring(math.floor((v.hunger or 0)/math.max(v.hunger_max or 1,1)*10)),
        tostring(math.floor((v.sanity or 0)/math.max(v.sanity_max or 1,1)*4)),
        tostring(graph.current_node),tostring(graph.known_nodes),tostring(status.board_ready)}
    local inventory=p.inventory or {}; local counts={}
    for _,group in ipairs({"items","equipped"}) do for _,item in ipairs(inventory[group] or {}) do
        local key=group..":"..tostring(item.prefab)..":"..tostring(item.slot)
        counts[key]=(counts[key] or 0)+(item.count or 1)
    end end
    for key,n in pairs(counts) do parts[#parts+1]="i:"..key..":"..n end
    for key,a in pairs(status.available_actions or {}) do
        parts[#parts+1]="a:"..key..":"..tostring(a.kind)..":"..tostring(a.prefab)
    end
    for _,e in ipairs(s.nearby or {}) do
        if e.activeThreat or e.potentialThreat then
            parts[#parts+1]="t:"..tostring(e.guid)..":"..tostring(e.activeThreat)..":"..tostring(e.potentialThreat)
        end
        if e.prefab=="campfire" or e.prefab=="firepit" then
            local burning=false; for _,tag in ipairs(e.tags or {}) do if tag=="fire" then burning=true end end
            local range=(e.distance or math.huge)<=6 and "cook" or (e.distance or math.huge)<=8 and "light" or "far"
            parts[#parts+1]="f:"..tostring(e.guid)..":"..tostring(burning)..":"..range
        end
    end
    table.sort(parts); return table.concat(parts,"|")
end

local function heartbeat_control(status)
    local result={}
    for _,key in ipairs({"connected","executor_busy","board_revision","board_auto_execute",
        "board_ready","board_paused","sampled_at","action_report","transport"}) do result[key]=status[key] end
    return result
end

function M.new(config,env,executor)
    local self={session_id=env.session_id,sequence=0,request_id=0,inflight=false,
        last_ok=env.now(),connected=false,next_request=0,executor=executor,dirty=true,
        receipts={},sampling={ticks=0,sum_ms={},max_ms={}},snapshot_request_id=0,snapshot_ack_id=0}
    function self:status(state,timings)
        local ready=measurement.call(env.wall_now,timings,"board_ready_ms",function()
            return executor.board and executor:board_choice(state,env.now())~=nil or false end)
        local available=measurement.call(env.wall_now,timings,"available_actions_ms",function()
            return executor.available and executor:available(state,env.now()) or env.available(state) end)
        return {enabled=true,connected=self.connected,session_id=self.session_id,
            protocol=4,validation_owner="lua",transport_mode="event_snapshot",board_revision=executor.board_revision,
            board_auto_execute=executor.board and executor.board.auto_execute==true or false,
            board_ready=ready,
            events=executor.events,
            executor_busy=executor.active~=nil,board_paused=executor.board_paused,
            action_report=executor.report,sampled_at=env.now(),
            transport={last_round_trip_ms=self.last_round_trip_ms,measurement_version=1,
                measurement_clock=env.wall_now and "lua_real_seconds" or "unavailable"},
            available_actions=available}
    end
    local function revision()
        local report=executor.report or {}
        return table.concat({tostring(report.action_id),tostring(report.status),
            tostring(report.confirmed_started_at),tostring(executor.active and executor.active.id),
            tostring(executor.board_revision),tostring(executor.board_paused)},":")
    end
    function self:tick()
        local now=env.now()
        if self.inflight and now-self.request_at>=3 then
            self.inflight=false; self.request_id=self.request_id+1
        end
        if now-self.last_ok>=3 then
            self.connected=false
            if executor.active then executor:finish("interrupted","bridge_disconnected") end
            executor.board_paused=true
        end
        local timings={}
        local state=measurement.call(env.wall_now,timings,"state_total_ms",function() return env.state(timings) end)
        measurement.call(env.wall_now,timings,"native_tick_ms",function() executor:tick(state,now,self.connected) end)
        if self.connected then
            measurement.call(env.wall_now,timings,"board_step_ms",function() executor:board_step(state,now) end)
        end
        measurement.add(self.sampling,timings)
        local current=revision()
        if current~=self.last_revision then self.dirty=true; self.last_revision=current end
        local refresh={}
        local status=measurement.call(env.wall_now,refresh,"total_ms",function() return self:status(state,refresh) end)
        local signature=state_signature(state,status).."|"..current
        local snapshot=signature~=self.ack_signature or self.force_snapshot
            or self.snapshot_request_id>self.snapshot_ack_id
        -- Snapshot events bypass the idle heartbeat, never an in-flight request.
        -- A change during HTTP stays dirty until the next tick after its reply.
        if self.inflight or (not snapshot and not self.dirty and now<self.next_request) then return end
        self.next_request=now+0.5
        self.dirty=false
        self.inflight=true; self.request_at=now
        self.sequence=self.sequence+1; self.request_id=self.request_id+1
        local request_id,sequence=self.request_id,self.sequence
        -- env.state() preceded tick/board_step. Refresh control metadata AFTER
        -- both, without repeating expensive map/entity sampling in this frame.
        state.bridge=status
        local completed={}
        for _,r in ipairs(self.receipts) do if r.done then completed[#completed+1]=r end end
        local payload={token=config.token,session_id=self.session_id,sequence=sequence,
            kind=snapshot and "snapshot" or "heartbeat",action_report=executor.report,
            measurement={clock=env.wall_now and "lua_real_seconds" or "unavailable",
                sampling=self.sampling,refresh=refresh,previous_exchanges=completed}}
        if snapshot then
            payload.state=state; payload.snapshot_request_id=self.snapshot_request_id
            payload.snapshot_reason=self.force_snapshot and "resync"
                or self.snapshot_request_id>self.snapshot_ack_id and "requested"
                or not self.ack_signature and "initial" or "event"
        else payload.control=heartbeat_control(status) end
        local sent_snapshot_request=self.snapshot_request_id
        self.sampling={ticks=0,sum_ms={},max_ms={}}
        local receipt={sequence=sequence,done=false}
        self.receipts[#self.receipts+1]=receipt
        if #self.receipts>8 then table.remove(self.receipts,1) end
        env.request(config.url,payload,function(result,successful,code)
            if request_id~=self.request_id or not env.alive() then return end
            self.inflight=false
            if not successful or code~=200 or type(result)~="table"
                or result.session_id~=self.session_id or result.sequence~=sequence then return end
            self.last_ok=env.now(); self.connected=true
            self.last_round_trip_ms=math.max(0,(self.last_ok-now)*1000)
            local restarted=self.server_id and result.server_id~=self.server_id
            if restarted then
                self.force_snapshot=true; self.snapshot_ack_id=0; self.snapshot_request_id=0
                if executor.active then executor:finish("interrupted","bridge_server_restarted") end
            elseif snapshot then
                self.ack_signature=signature; self.snapshot_ack_id=sent_snapshot_request; self.force_snapshot=false
            end
            if result.need_snapshot then self.force_snapshot=true end
            if type(result.snapshot_request_id)=="number" and result.snapshot_request_id>=0 then
                self.snapshot_request_id=math.max(self.snapshot_request_id,result.snapshot_request_id)
            end
            self.server_id=result.server_id
            local board=type(result.task_board)=="table" and result.task_board.kind=="task_board" and result.task_board or nil
            executor:set_board(board,result.board_revision or 0)
            local command=result.command
            receipt.accepted=true
            receipt.server_id=result.server_id
            receipt.action_id=type(command)=="table" and command.action_id or nil
            if type(command)~="table" or command.session_id~=self.session_id then return end
            if command.operation=="cancel" then executor:cancel(command.action_id)
            elseif command.operation=="start" then
                -- Never use the pre-request state to execute a delayed reply.
                executor:start(command,env.state(),env.now())
            end
        end,receipt)
    end
    function self:stop()
        self.request_id=self.request_id+1; self.connected=false
        if executor.active then executor:finish("interrupted","player_removed") end
    end
    return self
end
return M
