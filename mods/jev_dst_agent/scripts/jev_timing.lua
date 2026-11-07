-- Optional real-time measurements only. Missing clocks stay unknown, not zero.
local M={}
function M.elapsed(clock,started)
    if not clock or not started then return nil end
    local value=(clock()-started)*1000
    if value==value and value>=0 and value<1e12 then return value end
end
function M.call(clock,fields,key,fn)
    local started=clock and fields and clock()
    local value=fn()
    if started then fields[key]=M.elapsed(clock,started) end
    return value
end
function M.add(summary,fields)
    summary.ticks=summary.ticks+1
    for key,value in pairs(fields) do
        if type(value)=="number" then
            summary.sum_ms[key]=(summary.sum_ms[key] or 0)+value
            summary.max_ms[key]=math.max(summary.max_ms[key] or 0,value)
        end
    end
end
return M
