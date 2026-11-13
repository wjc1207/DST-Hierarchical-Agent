package.path='./mods/dst_player_agent/scripts/?.lua;'..package.path
local C=require('jev_coordinates')
local N=require('jev_native_executor')
local passed=0
local function test(name,fn) local ok,err=pcall(fn);assert(ok,name..': '..tostring(err));passed=passed+1;print('ok '..passed..' - '..name) end
local function state()
 return {world={phase='day'},player={position={x=0,z=0},vitals={health=150,hunger=100,hunger_max=150},inventory={items={},equipped={}}},nearby={},navigation={graph={current_node='A'}}}
end
local function command(id) return {action_id=id or 'move',choice='move_to',action={kind='explore',mode='coordinate',coordinate_target={x=8,z=0,arrival_radius=0.5},navigation_targets={}}} end
local function destination(p,s)
 if math.abs(s.player.position.x-p.x)<=p.arrival_radius then return {arrived=true,route={status='arrived'}} end
 return {arrived=false,route={status='ready',action={mode='place',node_id='A',x=math.min(p.x,s.player.position.x+4),z=0,arrival=.4}}}
end
test('coordinates reject unobserved water nonfinite values and invalid radius',function()
 local m={cells={['0:0']={passable=true,region='A'},['1:0']={passable=false}}}
 assert(C.validate({x=0,z=0},m,4));assert(not C.validate({x=4,z=0},m,4));assert(not C.validate({x=8,z=0},m,4))
 assert(not C.point({x=0/0,z=0}));assert(not C.point({x=0,z=0,arrival_radius=9}))
end)
test('known endpoints cannot authorize crossing an unobserved gap',function()
 local m={cells={['0:0']={passable=true},['2:0']={passable=true}}}
 assert(not C.segment(m,4,0,0,8,0));m.cells['1:0']={passable=true};assert(C.segment(m,4,0,0,8,0))
end)
test('map exports are immutable during in-flight updates and bounded',function()
 local m={cells={}}
 C.observe(m,{gx=0,gz=0,passable=true,biome='grassland',region='A'},false)
 local first=C.snapshot(m,4)
 C.observe(m,{gx=0,gz=0,passable=false,biome='grassland',region='A'},true)
 local second=C.snapshot(m,4)
 assert(first.cells['0:0'][3]==1 and second.cells['0:0'][3]==0 and second.version>first.version)
 for i=1,4200 do C.observe(m,{gx=i,gz=0,passable=true,region='A'},false) end
 local count=0;for _ in pairs(C.snapshot(m,4).cells) do count=count+1 end
 assert(count==4096 and C.snapshot(m,4).truncated)
end)
test('native coordinate walk follows intermediate legs and completes only at destination',function()
 local s=state();local issued=0
 local e=N.new({coordinate_destination=destination,issue=function() issued=issued+1 end,stop=function() end})
 assert(e:start(command(),s,0));assert(issued==1)
 s.player.position.x=4;e:tick(s,.2);assert(e.active and issued==2)
 s.player.position.x=7;e:tick(s,.4);assert(e.active,'arrival radius must not silently become 1.5')
 s.player.position.x=8;e:tick(s,.6);assert(not e.active and e.report.status=='completed' and e.report.reason=='coordinate_arrived')
end)
test('night coordinates require the same equipped light as existing navigation',function()
 local s=state();s.world.phase='night';local issued=0
 local e=N.new({coordinate_destination=destination,issue=function() issued=issued+1 end,stop=function() end})
 assert(not e:start(command(),s,0));assert(issued==0 and e.report.reason=='night_requires_portable_light')
 s.player.inventory.equipped={{prefab='torch',count=1}}
 assert(e:start(command('lit'),s,.1));assert(issued==1)
end)
test('route loss and new threats stop coordinate movement',function()
 local s=state();local blocked=false
 local e=N.new({coordinate_destination=function(p,current)
  if blocked then return nil,'coordinate_route_not_observed' end return destination(p,current)
 end,issue=function() end,stop=function() end})
 assert(e:start(command(),s,0));blocked=true;e:tick(s,.2)
 assert(e.report.status=='interrupted' and e.report.reason=='coordinate_route_not_observed')
 blocked=false;assert(e:start(command('threat'),s,.3));s.nearby={{activeThreat=true}};e:tick(s,.5)
 assert(e.report.status=='interrupted' and e.report.reason=='targeted_by_hostile')
end)
test('road masks probe inside the observed cell and preserve orientation',function()
 local cell={gx=-2,gz=3,x=-8,z=12,passable=true};local points={}
 local mask=C.sample_road(cell,4,function(x,z)
  assert(x>-10 and x<-6 and z>10 and z<14)
  points[#points+1]={x=x,z=z};return x==-9.5 and z==10.5 or x==-6.5 and z==13.5
 end,1,{remaining=1})
 assert(#points==16 and mask==1+2^15)
end)
test('road sampling has a shared hard budget and cached cells do not spend it',function()
 local budget={remaining=C.ROAD_SAMPLE_BUDGET};local calls=0;local cells={}
 local probe=function() calls=calls+1;return false end
 for i=1,40 do
  cells[i]={gx=i,gz=0,x=i*4,z=0,passable=true}
  C.sample_road(cells[i],4,probe,1,budget)
 end
 assert(calls==128 and budget.remaining==0 and cells[8]._road_mask==0 and cells[9]._road_mask==nil)
 local next_budget={remaining=8};C.sample_road(cells[1],4,probe,2,next_budget)
 assert(calls==128 and next_budget.remaining==8)
 C.sample_road(cells[9],4,probe,2,next_budget);assert(calls==144 and next_budget.remaining==7)
end)
test('road refresh and failed probes never fabricate empty-road observations',function()
 local c={gx=0,gz=0,x=0,z=0,passable=true}
 local probe=function() return true end
 assert(C.sample_road(c,4,probe,0,{remaining=1})==65535)
 assert(C.sample_road(c,4,function() return false end,29,{remaining=1})==65535)
 assert(C.sample_road(c,4,function() return nil end,31,{remaining=1})==65535)
 assert(c._road_sample_at==0)
 assert(C.sample_road(c,4,function() return false end,32,{remaining=1})==0)
 c.passable=false;local calls=0
 assert(C.sample_road(c,4,function() calls=calls+1;return true end,33,{remaining=1})==0 and calls==0)
end)
test('fine road changes advance immutable map rows and survive snapshots',function()
 local m={};local c={gx=0,gz=0,passable=true,biome='grassland',region='A'}
 C.observe(m,c,false);local old=C.snapshot(m,4)
 C.observe(m,c,false,0);local empty=C.snapshot(m,4)
 assert(old.cells['0:0'][7]==nil and empty.cells['0:0'][7]==0 and empty.version>old.version)
 C.observe(m,c,false,1);local fine=C.snapshot(m,4)
 assert(fine.cells['0:0'][7]==1 and empty.cells['0:0'][7]==0 and fine.road_subdivisions==4)
 local v=fine.version;C.observe(m,c,false,1);assert(C.snapshot(m,4).version==v)
end)
print('Passed '..passed..' coordinate tests')
