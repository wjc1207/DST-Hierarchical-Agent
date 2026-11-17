local Persistence=Class(function(self,inst)
    self.inst=inst
end)

function Persistence:OnSave()
    return self.server:save()
end

function Persistence:LoadPostPass(newents,data)
    -- World entities, including boats, are available by this stage.
    self.server:load(data,newents)
end

return Persistence
