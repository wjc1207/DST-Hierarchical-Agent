-- Client intent, RPC correlation and optional authenticated local HTTP transport.
local M={}
local TERMINAL={completed=true,failed=true,interrupted=true}
function M.validate_text(text)
    if type(text)~="string" or text:find("[%z\1-\31\127]") then return end
    text=text:match("^%s*(.-)%s*$");local _,n=text:gsub("[^\128-\191]","")
    if text~="" and n<=256 and #text<=1024 then return text end
end
function M.new(env)
    local self={seq=0,httpseq=0,requests={},results={},result_order={},seen={},seen_order={},
        control={revision=0,enabled=false,mode="instruction",messages={}},listening=false,
        last_state=0,last_http=0,last_ok=env.now(),last_snapshot=-math.huge,snapshot_seq=0,dirty=true}
    local function notice(text) env.notice(text) end
    function self:send(op,args,callback,backend_id)
        self.seq=self.seq+1
        local q={id=env.nonce..":"..self.seq,seq=self.seq,op=op,args=args or {},token=self.token,revision=self.control.revision}
        self.requests[q.id]={request=q,at=env.now(),callback=callback,backend_id=backend_id}
        env.send(q);return q.id
    end
    local function save_result(id,status,reason,revision,data)
        if not self.results[id] then self.result_order[#self.result_order+1]=id end
        self.results[id]={id=id,status=status,reason=reason,revision=revision,data=data}
        if #self.result_order>32 then self.results[table.remove(self.result_order,1)]=nil end
        self.dirty=true
    end
    function self:receive(packet)
        if type(packet)~="table" then return end
        local pending=self.requests[packet.id]
        if not pending or pending.request.seq~=packet.seq or pending.request.op~=packet.op then return end
        if type(packet.state)=="table" and ((packet.op=="bind" or packet.op=="summon") and packet.status=="completed"
            or (packet.state.version or 0)>((self.snapshot or {}).version or -1)) then
            local previous=self.snapshot and self.snapshot.buddy
            self.snapshot=packet.state;self.snapshot_seq=packet.seq;self.last_snapshot=env.now();self.dirty=true
            local buddy=self.snapshot.buddy
            if buddy and buddy.dead and (not previous or previous.guid~=buddy.guid or not previous.dead) then
                notice((env.name or "伙伴").."已死亡，当前任务停止。")
            end
        end
        if packet.status=="completed" and (packet.op=="bind" or packet.op=="summon") then
            self.token=packet.token;self.control.revision=packet.revision or 0;self.control.enabled=false
            self.control.mode="instruction";self.control.goal=nil
            self.control.instruction=nil;self.control.messages={};self.control.dialogue_revision=nil
        end
        if pending.backend_id then save_result(pending.backend_id,packet.status,packet.reason,pending.request.revision,packet.data) end
        if TERMINAL[packet.status] then
            self.requests[packet.id]=nil
            if packet.op=="state" then self.state_pending=nil end
            if packet.op=="release" and packet.status=="completed" then self.token=nil;self.snapshot=nil end
            if pending.callback then pending.callback(packet) end
        end
        if packet.reason=="binding_changed" and pending.request.token==self.token and not (packet.state or {}).bound
            and ((packet.state or {}).version or 0)>=((self.snapshot or {}).version or 0) then
            self.token=nil;self.control.enabled=false;self.after_intent=nil;notice("伙伴绑定已失效，请重新 /buddy summon。")
        end
        local deferred=self.after_intent
        if deferred and deferred.revision==self.control.revision and self:ready() then
            self.after_intent=nil
            if deferred.manual then self:send(deferred.manual,{},function(p) notice(deferred.manual.."："..tostring(p.reason)) end) end
            if deferred.after then deferred.after() end
        end
    end
    function self:interrupt(text,manual,after,autonomous_goal)
        if not self.token then notice("请先输入 /buddy summon 生成或找回自己的伙伴。");return end
        local message=text and M.validate_text(text)
        if text and not message then notice("指令须为单行，最多 256 个字符。");return end
        local c=self.control
        local mode=autonomous_goal and "autonomous" or "instruction"
        if autonomous_goal or (message and (not c.enabled or c.mode~=mode)) then
            c.instruction=message
            c.messages={}
        end
        if c.dialogue_revision or not message then c.messages={} end
        c.dialogue_revision=nil
        c.mode=mode
        c.goal=autonomous_goal
        c.revision=c.revision+1
        c.enabled=message~=nil or autonomous_goal~=nil
        if message then
            self.task_started=env.now();self.http_retry_at=0
            c.messages[#c.messages+1]={id=c.revision,text=message}
            while #c.messages>8 do table.remove(c.messages,1) end
        end
        self.dirty=true
        self.after_intent={revision=c.revision,manual=manual,after=after}
        self.last_intent_at=env.now()
        self:send("intent",{},function(packet)
            if packet.revision~=c.revision then return end
            if packet.status~="completed" and (packet.state or {}).revision~=c.revision and packet.reason~="rpc_timeout" then
                c.enabled=false;self.after_intent=nil;notice("暂停／同步指令失败："..tostring(packet.reason))
            end
        end)
        return true
    end
    function self:dialogue(text)
        if not self.token then notice("请先 /buddy summon 召唤伙伴。");return end
        local message=M.validate_text(text)
        if not message then notice("消息须为单行，最多 256 个字符。");return end
        local c=self.control
        if not c.enabled then
            c.instruction=message
            c.messages={}
        end
        local last=c.messages[#c.messages]
        c.dialogue_revision=math.max(c.dialogue_revision or 0,last and last.id or 0)+1
        c.messages[#c.messages+1]={id=c.dialogue_revision,text=message}
        while #c.messages>8 do table.remove(c.messages,1) end
        c.enabled=true
        self.task_started=env.now();self.http_retry_at=0;self.dirty=true
        return true
    end
    function self:command(command)
        local name,body=command:match("^(%S+)%s*(.-)$")
        if name=="bind" then name="summon" end -- Compatibility alias for the single public entry point.
        if name=="summon" then
            self.control.enabled=false
            self:send(name,{},function(p)
                local messages={summoned="伙伴已生成并开始跟随。",bound="已绑定现有伙伴并开始跟随。",
                    no_safe_spawn_land="附近没有安全的陆地生成位置，请换个位置再试。",
                    existing_companion_dead="已有伙伴已死亡，不会直接生成第二个。",owner_not_alive="请在角色存活时召唤伙伴。"}
                notice(messages[p.reason] or (name.."："..tostring(p.reason)))
            end)
        elseif name=="follow" or name=="stay" or name=="stop" or name=="release" then self:interrupt(nil,name)
        elseif name=="do" then
            if not env.http_enabled then notice("本机桥接未配置，请先运行配置命令。");return end
            if self:interrupt(body) then notice("已暂停伙伴，交给本机 DS 重新规划。") end
        elseif name=="ai" then
            self.listening=body~="off"
            if not self.listening then self:interrupt(nil,"stay") end
            notice(self.listening and "伙伴聊天输入已开启，普通聊天会交给你本机的 DS。" or "伙伴聊天输入已关闭。")
        elseif name=="status" then self:send("state",{},function(p)
            local s=p.state or {};local motion=s.motion or {}
            notice("状态："..tostring(p.reason).."；行为："..tostring(s.mode)
                .."；跟随："..tostring((s.follow_state or {}).status or "-")
                .."；距离："..tostring((s.buddy or {}).owner_distance or "-")
                .."；休眠："..tostring(motion.asleep))
        end)
        else notice("用法：/buddy summon | follow | stay | stop | release | do 需求 | ai [off] | status") end
    end
    function self:chat(text)
        if not self.listening then return false end
        if env.http_enabled then self:dialogue(text) else notice("本机桥接未配置。") end
        return true
    end
    function self:ready()
        local s=self.snapshot
        return s and s.bound and s.token==self.token and s.revision==self.control.revision and env.now()-self.last_snapshot<=2 or false
    end
    function self:apply_command(command)
        if type(command)~="table" or type(command.id)~="string" then return end
        if self.seen[command.id] then return end
        self.seen[command.id]=true;self.seen_order[#self.seen_order+1]=command.id
        if #self.seen_order>128 then self.seen[table.remove(self.seen_order,1)]=nil end
        local binding=command.op=="bind" or command.op=="summon"
        if command.revision~=self.control.revision or command.token~=self.token or (not binding and not self:ready()) then
            save_result(command.id,"interrupted","client_intent_changed",command.revision);return
        end
        if not ({summon=true,bind=true,move=true,collect=true,follow=true,stay=true,stop=true,
            recipes=true,equip=true,unequip=true,work=true,craft=true,place=true,search=true,give_owner=true,drop_item=true,deposit=true,withdraw=true,visit_building=true})[command.op] then
            save_result(command.id,"failed","unsupported_action",command.revision);return
        end
        self:send(command.op,command.args,nil,command.id)
    end
    function self:feedback(message)
        if type(message)~="table" or type(message.id)~="string" or self.seen[message.id]
            or message.revision~=self.control.revision or message.dialogue_revision~=self.control.dialogue_revision then return end
        if self.snapshot and self.snapshot.buddy and self.snapshot.buddy.dead then return end
        local text=M.validate_text(message.text);if not text then return end
        self.seen[message.id]=true;self.seen_order[#self.seen_order+1]=message.id
        if #self.seen_order>128 then self.seen[table.remove(self.seen_order,1)]=nil end
        notice((env.name or "威尔逊").."："..text)
        if message.finish and message.keep_mode then
            self.control.enabled=false;self.control.instruction=nil;self.control.messages={}
            self.dirty=true
            self:send("say",{text=text})
        elseif message.finish then
            local goal=env.resume_goal and env.resume_goal()
            self:interrupt(nil,not goal and "follow" or nil,function() self:send("say",{text=text}) end,goal)
        else
            self:send("say",{text=text})
        end
    end
    function self:tick()
        local now=env.now()
        local expired={}
        for id,pending in pairs(self.requests) do if now-pending.at>10 then expired[#expired+1]=id end end
        for _,id in ipairs(expired) do
            local p=self.requests[id];self.requests[id]=nil
            if p.request.op=="state" then self.state_pending=nil end
            if p.backend_id then save_result(p.backend_id,"failed","rpc_timeout",p.request.revision) end
            if p.callback then p.callback({status="failed",reason="rpc_timeout",revision=p.request.revision}) end
        end
        if self.state_pending and not self.requests[self.state_pending] then self.state_pending=nil end
        if self.token and self.after_intent and not self:ready() and now-(self.last_intent_at or 0)>=.5 then
            self.last_intent_at=now;self:send("intent")
        end
        if self.token and not self.state_pending and now-self.last_state>=.5 then
            self.last_state=now;self.state_pending=self:send("state")
        end
        if not env.http_enabled or env.managed_http then return end
        local lost=now-self.last_ok>3 and (not self.task_started or now-self.task_started>5)
        if self.control.enabled and lost then
            self:interrupt(nil,"stay");notice("本机 AI 控制程序未连接，已停止伙伴任务。")
        end
        if self.inflight and now-self.http_at>3 then self.inflight=false;self.http_generation=self.http_generation+1 end
        if now<(self.http_retry_at or 0) then return end
        if self.inflight or (not self.dirty and now-self.last_http<.2) then return end
        self.inflight=true;self.http_at=now;self.last_http=now;self.dirty=false
        self.http_generation=(self.http_generation or 0)+1;local generation=self.http_generation
        self.httpseq=self.httpseq+1;local sequence=self.httpseq
        local results={};for _,id in ipairs(self.result_order) do results[#results+1]=self.results[id] end
        env.http({session_id=env.nonce,sequence=sequence,kind="snapshot",state={
            companion=self.snapshot,control=self.control,ready=self:ready(),snapshot_age_ms=self.snapshot and (now-self.last_snapshot)*1000 or nil},results=results},function(response)
            if generation~=self.http_generation then return end
            self.inflight=false
            if type(response)~="table" or response.session_id~=env.nonce or response.sequence~=sequence then
                self.http_backoff=math.min(3,(self.http_backoff or .25)*2)
                self.http_retry_at=env.now()+self.http_backoff
                return
            end
            self.http_backoff=nil;self.http_retry_at=0
            self.last_ok=env.now()
            for _,message in ipairs(response.feedback or {}) do self:feedback(message) end
            self:apply_command(response.command)
        end)
    end
    return self
end
return M
