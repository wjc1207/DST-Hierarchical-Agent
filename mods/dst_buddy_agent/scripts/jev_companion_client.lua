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
            local result=self.snapshot.last_action_result
            if result and packet.token==self.token and TERMINAL[result.status] then
                local action=self.requests[result.id]
                if action and action.backend_id and action.request.seq==result.seq and action.request.op==result.op then
                    save_result(action.backend_id,result.status,result.reason,action.request.revision,result.data)
                    self.requests[result.id]=nil
                end
            end
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
            recipes=true,equip=true,unequip=true,eat=true,work=true,craft=true,place=true,search=true,give_owner=true,drop_item=true,deposit=true,withdraw=true,visit_building=true,
            refuel=true,set_home=true,cook_load=true,start_meal=true,harvest_meal=true,ignite_tree=true})[command.op] then
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

local TEAM_ACTIVITY={
    collect="采集",search="探索",equip="装备工具",unequip="收起工具",work="工作",CHOP="砍树",MINE="挖矿",
    craft="制作",place="建造",visit_building="前往建筑",move="移动",give_owner="交付物品",
    eat="进食",drop_item="放下物品",deposit="存放物品",withdraw="取出物品",
    refuel="添燃料",set_home="设置家",cook_load="放食材",start_meal="开始烹饪",harvest_meal="收获料理",ignite_tree="点火烧炭",
}

function M.new_team(env)
    local Names=require("jev_companion_roster")
    local self={children={},roster={},roster_version=0,requests={},deleting={},serial=0,httpseq=0,last_http=-math.huge,last_ok=env.now(),
        last_roster=-math.huge,listening=false,summon_queue={},child_generation=0,
        control={revision=0,enabled=false,mode="instruction",messages={}}}
    local function notice(text) env.notice(text) end
    local function child(row)
        local c=self.children[row.id]
        if c and c.team_name==row.name then return c end
        self.child_generation=self.child_generation+1
        local ce={name=row.name,nonce=env.nonce..":"..row.id..":g"..self.child_generation,now=env.sim_now or env.now,http_enabled=env.http_enabled,managed_http=true,
            notice=notice,resume_goal=function()
                local buddy=(c.snapshot or {}).buddy
                if self.control.mode=="autonomous" and c.control.mode=="instruction"
                    and c.control.enabled and not (buddy and buddy.dead) then
                    return self.control.goal
                end
            end,send=function(q)
                q.buddy_id=row.id
                if q.op=="summon" or q.op=="bind" then q.args.name=row.name end
                env.send(q)
            end}
        c=M.new(ce);c.team_name=row.name;self.children[row.id]=c;return c
    end
    local function any_enabled()
        for _,c in pairs(self.children) do if c.control.enabled then return true end end;return false
    end
    function self:panel_state()
        local state={mode=self.control.mode,goal=self.control.goal,listening=self.listening,bridge_enabled=env.http_enabled,
            paused=self.control.mode=="autonomous" and not any_enabled(),roster={}}
        for _,row in ipairs(self.roster) do
            local c=self.children[row.id]
            local s=c and c.snapshot or {}
            local bound=row.bound and c~=nil and c.token~=nil
            local status,detail
            local dead=row.dead or (s.buddy or {}).dead
            if dead then
                status="已死亡"
            elseif not bound then
                status="可召回"
            elseif c.control.enabled then
                local action=s.revision==c.control.revision and s.action
                local activity=action and (TEAM_ACTIVITY[action.work or action.op] or TEAM_ACTIVITY[action.op])
                if c.control.mode=="autonomous" then
                    status="自主 · "..(activity or "安排任务")
                else
                    local last=c.control.messages[#c.control.messages]
                    detail=last and last.text or c.control.instruction
                    status="临时指令 · "..(activity or detail or "安排任务")
                end
            else
                status=s.mode=="follow" and "跟随" or "待命"
            end
            state.roster[#state.roster+1]={id=row.id,name=row.name,bound=bound,dead=dead,
                status=status,detail=detail,guid=row.guid,roster_version=self.roster_version,deleting=self.deleting[row.id],distance=(s.buddy or {}).owner_distance}
        end
        return state
    end
    function self:set_listening(enabled)
        self.listening=enabled
        notice(enabled and "伙伴聊天已开启，请用 @名字 或 @全体 开头。" or "伙伴聊天已关闭。")
    end
    function self:set_autonomy(enabled,raw_goal)
        local control=self.control
        local goal=raw_goal==nil and control.goal or M.validate_text(raw_goal)
        if enabled and not goal then
            notice("用法：/buddy auto 长期目标（单行，最多 256 字）")
            return
        end
        if enabled and not env.http_enabled then notice("本机桥接未配置。");return end
        if not enabled and control.mode~="autonomous" then notice("当前为伙伴指令模式。");return end
        local ids={}
        for _,row in ipairs(self.roster) do
            local companion=self.children[row.id]
            if companion and companion.token then ids[#ids+1]=row.id end
        end
        if enabled and #ids==0 then notice("请先 /buddy summon 召唤伙伴。");return end

        control.mode=enabled and "autonomous" or "instruction"
        if enabled then control.goal=goal end
        control.instruction=nil
        control.messages={}
        control.revision=control.revision+1
        control.target_ids=ids
        control.request_kind=enabled and "autonomy" or "manual"
        self.task_started=enabled and env.now() or nil
        for _,id in ipairs(ids) do
            local companion=self.children[id]
            if enabled then
                companion:interrupt(nil,nil,nil,goal)
            elseif companion.control.mode=="autonomous" then
                companion:interrupt(nil,"stay")
            end
        end
        control.enabled=any_enabled()
        notice(enabled and ("伙伴自主模式已开启。长期目标："..goal) or "伙伴已切回指令模式，临时指令继续执行。")
        return true
    end
    function self:fetch_roster(callback)
        self.serial=self.serial+1
        local q={id=env.nonce..":roster:"..self.serial,seq=self.serial,op="roster"}
        self.requests[q.id]={seq=q.seq,callback=callback,at=(env.sim_now or env.now)()};self.last_roster=self.requests[q.id].at;env.send(q)
    end
    function self:delete(row)
        if self.deleting[row.id] then return end
        self.serial=self.serial+1
        local q={id=env.nonce..":delete:"..self.serial,seq=self.serial,op="delete",buddy_id=row.id,
            args={name=row.name,guid=row.guid,roster_version=row.roster_version}}
        self.deleting[row.id]=true
        self.requests[q.id]={seq=q.seq,at=(env.sim_now or env.now)(),op=q.op,callback=function(ok)
            self.deleting[row.id]=nil
            notice(ok and ("已删除伙伴 "..row.name.."。") or ("未能删除伙伴 "..row.name.."，请刷新后重试。"))
        end}
        env.send(q)
    end
    local function accept_roster(packet)
        local version=packet.roster_version or 0
        if type(packet.roster)=="table" and version>=self.roster_version then
            self.roster=packet.roster
            if version>self.roster_version then
                local present={}
                for _,row in ipairs(self.roster) do present[row.id]=true end
                for id in pairs(self.children) do
                    if not present[id] then self.children[id]=nil end
                end
            end
            self.roster_version=version
        end
    end
    function self:receive(packet)
        if type(packet)~="table" then return end
        if packet.op=="roster" or packet.op=="delete" then
            local pending=self.requests[packet.id]
            if not pending or pending.seq~=packet.seq or (pending.op or "roster")~=packet.op then return end
            self.requests[packet.id]=nil
            accept_roster(packet)
            if packet.op=="delete" and packet.status=="completed" then
                self.children[packet.buddy_id]=nil
            end
            if pending.callback then pending.callback(packet.status=="completed") end
            return
        end
        accept_roster(packet)
        local c=self.children[packet.buddy_id or "buddy1"]
        if c then c:receive(packet) end
    end
    function self:summon(raw)
        local name=raw~="" and Names.name(raw) or nil
        if raw~="" and not name then notice("名字须为 1–12 字、不能含空格或 @，且不能叫全体／all／自己。");return end
        if #self.summon_queue>=4 then notice("召唤请求正在处理，请稍候。");return end
        self.summon_queue[#self.summon_queue+1]=name or ""
        self:next_summon()
        return true
    end
    function self:next_summon()
        if self.summon_pending or #self.summon_queue==0 then return end
        local name=table.remove(self.summon_queue,1);if name=="" then name=nil end
        self.summon_pending=true
        local function finished() self.summon_pending=false;self:next_summon() end
        self:fetch_roster(function(ok)
            if ok==false then finished();return end
            local selected
            for _,row in ipairs(self.roster) do if not name or row.name:lower()==name:lower() then selected=row;break end end
            if not selected then
                local occupied={};for _,row in ipairs(self.roster) do occupied[row.id]=true end
                for i=1,Names.MAX do if not occupied["buddy"..i] then selected={id="buddy"..i,name=name or (i==1 and "一号" or "二号")};break end end
            end
            if not selected then notice("当前最多支持两个伙伴，请使用已有名字召回伙伴。");finished();return end
            local existing=self.children[selected.id]
            if selected.bound and not selected.dead and existing and existing.token and existing.team_name==selected.name then
                notice("伙伴 "..selected.name.." 已绑定。")
                finished();return
            end
            local join_autonomy=self.control.mode=="autonomous" and any_enabled()
            local team_revision=self.control.revision
            local c=child(selected)
            c:send("summon",{},function(packet)
                if packet.status=="completed" then
                    notice("伙伴："..selected.name.."（@"..selected.name.." 可指定指令）")
                    if join_autonomy and self.control.revision==team_revision then
                        c:interrupt(nil,nil,nil,self.control.goal)
                    end
                else
                    notice("召唤失败："..tostring(packet.reason))
                end
                finished()
            end)
        end)
    end
    function self:interrupt(text,manual,dialogue)
        local valid=M.validate_text(text)
        if not valid then notice("指令须为单行，最多 256 字。");return end
        local available={}
        for _,row in ipairs(self.roster) do available[#available+1]={id=row.id,name=row.name,bound=self.children[row.id]~=nil and self.children[row.id].token~=nil} end
        local ids,body,error=Names.resolve(valid,available)
        if not ids then notice("未执行："..tostring(error));return end
        local stop={['停止']='stop',['暂停']='stay',['取消']='stop',stop='stop',pause='stay',cancel='stop'}
        if not dialogue then manual=manual or stop[body:lower()] end
        for _,id in ipairs(ids) do
            if not self.children[id] or not self.children[id].token then notice("伙伴尚未绑定，请先 /buddy summon 对应名字。");return end
        end
        if not manual and not env.http_enabled then notice("本机桥接未配置。");return end
        local c=self.control
        if not manual and not c.enabled and c.mode=="instruction" then c.instruction=body;c.messages={} end
        c.revision=c.revision+1;c.target_ids=ids;c.request_kind=manual and "manual" or "message"
        if not manual then
            self.task_started=env.now()
        end
        c.messages[#c.messages+1]={id=c.revision,text=body,target_ids=ids}
        while #c.messages>8 do table.remove(c.messages,1) end
        local message;if not manual then message=body end
        for _,id in ipairs(ids) do
            if dialogue then self.children[id]:dialogue(message)
            else self.children[id]:interrupt(message,manual) end
        end
        c.enabled=any_enabled()
        if not dialogue then notice(manual and "已发送指定伙伴的控制指令。" or "已暂停指定伙伴，交给本机 DeepSeek 统一规划。") end
        return true
    end
    function self:command(command)
        local name,body=command:match("^(%S+)%s*(.-)$")
        if name=="summon" or name=="bind" then self:summon(body)
        elseif name=="do" then self:interrupt(body)
        elseif name=="auto" then self:set_autonomy(body:lower()~="off",body~="" and body or nil)
        elseif name=="stop" or name=="stay" or name=="follow" or name=="release" then
            self:interrupt((body~="" and body.." " or "")..name,name)
        elseif name=="list" or name=="status" then self:fetch_roster(function()
            local state=self:panel_state()
            if #state.roster==0 then notice("尚无伙伴：/buddy summon 名字");return end
            if state.mode=="autonomous" then
                notice((state.paused and "伙伴自主已暂停" or "伙伴自主模式").."。长期目标："..state.goal)
            else
                notice("伙伴指令模式。")
            end
            for _,row in ipairs(state.roster) do
                notice("@"..row.name.." ["..row.id.."] "..row.status.."；距离："..tostring(row.distance or "-"))
            end
        end)
        elseif name=="ai" then self:set_listening(body:lower()~="off")
        else notice("用法：/buddy summon 名字 | list | do [@名字] 需求 | auto [长期目标|off] | stop [@名字] | follow | stay | release | ai [off]") end
    end
    function self:accepts_chat(text)
        if not self.listening then return false end
        local name=text:match("^%s*@(%S+)")
        if not name or name=="自己" then return false end
        if Names.is_all(name) then return true end
        for _,row in ipairs(self.roster) do
            if row.name:lower()==name:lower() then return true end
        end
        return false
    end
    function self:chat(text)
        if not self:accepts_chat(text) then return false end
        self:interrupt(text,nil,true)
        return true
    end
    function self:tick(paused)
        local now=env.now()
        local sim_now=env.sim_now and env.sim_now() or now
        local frozen_gap=self.last_tick and now-self.last_tick>2
            and sim_now-self.last_sim_tick<.5
        self.last_tick,self.last_sim_tick=now,sim_now
        if paused or frozen_gap then
            if paused and self.resume_started then self.resume_versions=nil end
            if not self.resume_versions then
                self.resume_versions={}
                for id,c in pairs(self.children) do
                    if c.token then self.resume_versions[id]=c.seq end
                end
            end
        end
        if not paused then
            if self.resume_versions and not self.resume_started then
                self.resume_started=now
                self.last_ok=now
                self.inflight=false
                self.http_generation=(self.http_generation or 0)+1
                self.retry_at=0
                for _,c in pairs(self.children) do
                    if c.state_pending then c.requests[c.state_pending]=nil;c.state_pending=nil end
                    c.last_state=-math.huge
                end
            end
            for id,pending in pairs(self.requests) do if sim_now-pending.at>10 then
                self.requests[id]=nil;notice("伙伴名册请求超时，请重试。")
                if pending.callback then pending.callback(false) end
            end end
            if sim_now-self.last_roster>=3 then self:fetch_roster() end
            for _,c in pairs(self.children) do c:tick() end
            if self.resume_versions then
                for id,seq in pairs(self.resume_versions) do
                    local c=self.children[id]
                    if not c or not c.token or c.snapshot_seq>seq then self.resume_versions[id]=nil end
                end
                if next(self.resume_versions)==nil then
                    self.resume_versions,self.resume_started=nil,nil
                end
            end
        else
            self.resume_started=nil
        end
        self.control.enabled=any_enabled()
        if not env.http_enabled then return end
        local suspended=paused==true or self.resume_versions~=nil
        if not paused and self.control.enabled and now-self.last_ok>3 and (not self.task_started or now-self.task_started>5) then
            self:interrupt("@全体 暂停","stay");notice("本机 AI 未连接，已暂停伙伴任务。")
        end
        if self.inflight and now-self.http_at>3 then self.inflight=false;self.http_generation=self.http_generation+1 end
        if self.inflight or now-self.last_http<.2 or now<(self.retry_at or 0) then return end
        self.inflight=true;self.http_at=now;self.last_http=now
        self.http_generation=(self.http_generation or 0)+1;local generation=self.http_generation
        self.httpseq=self.httpseq+1;local sequence=self.httpseq
        local members,results={},{}
        for id,c in pairs(self.children) do
            if c.token then
                members[id]={companion=c.snapshot,control=c.control,ready=c:ready()==true,
                    snapshot_age_ms=c.snapshot and math.max(0,sim_now-c.last_snapshot)*1000 or nil}
                for _,key in ipairs(c.result_order) do
                    local row={buddy_id=id};for k,v in pairs(c.results[key]) do row[k]=v end;results[#results+1]=row
                end
            end
        end
        env.http({session_id=env.nonce,sequence=sequence,kind="snapshot",state={team=true,paused=suspended,control=self.control,
            roster=self.roster,members=members},results=results},function(response)
            if generation~=self.http_generation then return end
            self.inflight=false
            if type(response)~="table" or response.session_id~=env.nonce or response.sequence~=sequence then self.retry_at=env.now()+.5;return end
            self.retry_at=0;self.last_ok=env.now()
            if suspended or self.resume_versions or (env.paused and env.paused()) then return end
            for _,message in ipairs(response.feedback or {}) do
                if not message.team and not self.deleting[message.buddy_id] and self.children[message.buddy_id] then self.children[message.buddy_id]:feedback(message) end
            end
            for _,command in ipairs(response.commands or {}) do
                local c=self.children[command.buddy_id];if c and not self.deleting[command.buddy_id] then c:apply_command(command) end
            end
        end)
    end
    return self
end
return M
