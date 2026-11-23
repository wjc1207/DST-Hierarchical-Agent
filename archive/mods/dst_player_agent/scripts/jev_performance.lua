-- Client frontend wall-update cadence, independent of simulation tick rate.
local M={}
local function valid(n) return type(n)=="number" and n==n and n>=0 and n<1e12 end
function M.new(clock,interval)
    local self={clock=clock,interval=interval or 5,frames=0,elapsed=0,max_ms=0,samples={}}
    function self:frame(dt)
        local now=self.clock and self.clock()
        local delta
        if now then
            if self.last then delta=now-self.last end
            self.last=now
        elseif valid(dt) then delta=dt end
        if not valid(delta) or delta<=0 then return end
        self.frames=self.frames+1; self.elapsed=self.elapsed+delta
        local ms=delta*1000
        self.max_ms=math.max(self.max_ms,ms)
        -- Bounded reservoir of recent intervals; no sort on each frame.
        self.samples[(self.frames-1)%600+1]=ms
        if self.elapsed<self.interval then return end
        table.sort(self.samples)
        self.latest={source="frontend_wall_update",fps=self.frames/self.elapsed,
            frame_ms=self.elapsed*1000/self.frames,max_frame_ms=self.max_ms,
            p95_frame_ms=self.samples[math.max(1,math.ceil(#self.samples*.95))],
            frames=self.frames,window_seconds=self.elapsed,sampled_at=now}
        self.frames,self.elapsed,self.max_ms,self.samples=0,0,0,{}
        return self:read()
    end
    function self:read()
        local result={source="frontend_wall_update",available=self.latest~=nil}
        for k,v in pairs(self.latest or {}) do result[k]=v end
        if self.clock and result.sampled_at then result.age_seconds=math.max(0,self.clock()-result.sampled_at) end
        return result
    end
    return self
end
return M
