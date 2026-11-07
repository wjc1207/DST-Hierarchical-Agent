local M={MAX=2}
function M.is_all(name)
    return name=="全体" or name:lower()=="all"
end
function M.name(value)
    if type(value)~="string" then return end
    value=value:match("^%s*(.-)%s*$")
    local _,chars=value:gsub("[^\128-\191]","")
    if chars<1 or chars>12 or #value>48 or value:find("[%z\1-\32\127@/,%[%]\"']")
        or M.is_all(value) then return end
    return value
end
function M.resolve(text,roster)
    local selected,seen={},{};local body=text:match("^%s*(.-)%s*$")
    while body:sub(1,1)=="@" do
        local name,rest=body:match("^@(%S+)%s*(.*)$")
        if not name then return nil,nil,"invalid_mention" end
        if M.is_all(name) then
            for _,row in ipairs(roster) do if row.bound~=false and not seen[row.id] then seen[row.id]=true;selected[#selected+1]=row.id end end
        else
            local found
            for _,row in ipairs(roster) do if row.name:lower()==name:lower() then found=row.id;break end end
            if not found then return nil,nil,"unknown_buddy:"..name end
            if not seen[found] then seen[found]=true;selected[#selected+1]=found end
        end
        body=rest
    end
    if #selected==0 then for _,row in ipairs(roster) do if row.bound~=false then selected[#selected+1]=row.id end end end
    if #selected==0 then return nil,nil,"no_companions" end
    if body=="" then return nil,nil,"empty_instruction" end
    return selected,body
end
return M
