local Core=require("jev_companion_client")
local Names=require("jev_companion_roster")
local M={validate_text=Core.validate_text}
local ACTIVITY={
    collect="采集",search="探索",equip="装备工具",unequip="收起工具",work="工作",CHOP="砍树",MINE="挖矿",
    craft="制作",place="建造",visit_building="前往建筑",move="移动",give_owner="交付物品",
    drop_item="放下物品",deposit="存放物品",withdraw="取出物品",
}
function M.new(env)
    local self={children={},roster={},requests={},serial=0,httpseq=0,last_http=-math.huge,last_ok=env.now(),
        last_roster=-math.huge,listening=false,summon_queue={},child_generation=0,
        control={revision=0,enabled=false,mode="instruction",messages={}}}
    local function notice(text) env.notice(text) end
    local function child(row)
        local c=self.children[row.id]
        if c and c.team_name==row.name then return c end
        self.child_generation=self.child_generation+1
        local ce={name=row.name,nonce=env.nonce..":"..row.id..":g"..self.child_generation,now=env.now,http_enabled=env.http_enabled,managed_http=true,
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
        c=Core.new(ce);c.team_name=row.name;self.children[row.id]=c;return c
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
                local activity=action and (ACTIVITY[action.work or action.op] or ACTIVITY[action.op])
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
                status=status,detail=detail,distance=(s.buddy or {}).owner_distance}
        end
        return state
    end
    function self:set_listening(enabled)
        self.listening=enabled
        notice(enabled and "伙伴聊天已开启，请用 @名字 或 @全体 开头。" or "伙伴聊天已关闭。")
    end
    function self:set_autonomy(enabled,raw_goal)
        local control=self.control
        local goal=raw_goal==nil and control.goal or Core.validate_text(raw_goal)
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
                -- Instruction revisions survive the switch and will follow on completion.
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
        self.requests[q.id]={seq=q.seq,callback=callback,at=env.now()};self.last_roster=env.now();env.send(q)
    end
    function self:receive(packet)
        if type(packet)~="table" then return end
        if packet.op=="roster" then
            local pending=self.requests[packet.id]
            if not pending or pending.seq~=packet.seq then return end
            self.requests[packet.id]=nil
            if type(packet.roster)=="table" then self.roster=packet.roster end
            if pending.callback then pending.callback(packet.status=="completed") end
            return
        end
        if type(packet.roster)=="table" then self.roster=packet.roster end
        local c=self.children[packet.buddy_id or "buddy1"]
        if c then c:receive(packet) end
    end
    function self:summon(raw)
        local name=raw~="" and Names.name(raw) or nil
        if raw~="" and not name then notice("名字须为 1–12 字、不能含空格或 @，且不能叫全体／all。");return end
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
    function self:interrupt(text,manual)
        local valid=Core.validate_text(text)
        if not valid then notice("指令须为单行，最多 256 字。");return end
        local available={}
        for _,row in ipairs(self.roster) do available[#available+1]={id=row.id,name=row.name,bound=self.children[row.id]~=nil and self.children[row.id].token~=nil} end
        local ids,body,error=Names.resolve(valid,available)
        if not ids then notice("未执行："..tostring(error));return end
        local stop={['停止']='stop',['暂停']='stay',['取消']='stop',stop='stop',pause='stay',cancel='stop'}
        manual=manual or stop[body:lower()]
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
        for _,id in ipairs(ids) do self.children[id]:interrupt(message,manual) end
        c.enabled=any_enabled()
        notice(manual and "已发送指定伙伴的控制指令。" or "已暂停指定伙伴，交给本机 DeepSeek 统一规划。")
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
        if not name then return false end
        if Names.is_all(name) then return true end
        for _,row in ipairs(self.roster) do
            if row.name:lower()==name:lower() then return true end
        end
        return false
    end
    function self:chat(text)
        if not self:accepts_chat(text) then return false end
        self:interrupt(text)
        return true
    end
    function self:tick()
        local now=env.now()
        for id,pending in pairs(self.requests) do if now-pending.at>10 then
            self.requests[id]=nil;notice("伙伴名册请求超时，请重试。")
            if pending.callback then pending.callback(false) end
        end end
        if now-self.last_roster>=3 then self:fetch_roster() end
        for _,c in pairs(self.children) do c:tick() end
        self.control.enabled=any_enabled()
        if not env.http_enabled then return end
        if self.control.enabled and now-self.last_ok>3 and (not self.task_started or now-self.task_started>5) then
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
                    snapshot_age_ms=c.snapshot and (now-c.last_snapshot)*1000 or nil}
                for _,key in ipairs(c.result_order) do
                    local row={buddy_id=id};for k,v in pairs(c.results[key]) do row[k]=v end;results[#results+1]=row
                end
            end
        end
        env.http({session_id=env.nonce,sequence=sequence,kind="snapshot",state={team=true,control=self.control,
            roster=self.roster,members=members},results=results},function(response)
            if generation~=self.http_generation then return end
            self.inflight=false
            if type(response)~="table" or response.session_id~=env.nonce or response.sequence~=sequence then self.retry_at=env.now()+.5;return end
            self.retry_at=0;self.last_ok=env.now()
            for _,message in ipairs(response.feedback or {}) do
                -- Ignore legacy team broadcasts; speech must identify an actual companion.
                if not message.team and self.children[message.buddy_id] then self.children[message.buddy_id]:feedback(message) end
            end
            for _,command in ipairs(response.commands or {}) do
                local c=self.children[command.buddy_id];if c then c:apply_command(command) end
            end
        end)
    end
    return self
end
return M
