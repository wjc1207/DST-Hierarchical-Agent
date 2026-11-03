"""Render observed world cells with an explicit, camera-independent x/z grid."""
import base64
import io
import math
from pathlib import Path
from common.task_timing import number
from mcp_agent.map_icons import load_icons, icon_for, ALIASES
from functools import lru_cache

COLORS = {'forest':'#586348','grassland':'#89985a','savanna':'#b2a15c','swamp':'#685b73',
          'rocky':'#858888','deciduous_forest':'#a18b45','desert':'#c8b075','dirt':'#927654','unknown':'#666b5e'}
MAP_TOOL = {'type':'function','function':{'name':'query_map',
    'description':'View an image of OBSERVED terrain, roads, remembered resources, buildings, threats and a world x/z grid. Use before selecting a camp site. Blank is unobserved, not empty safe land. Optional viewport uses world coordinates; use a smaller span to inspect crowded candidates.',
    'parameters':{'type':'object','properties':{'center_x':{'type':'number'},'center_z':{'type':'number'},
        'span':{'type':'number','minimum':32,'maximum':512}},'additionalProperties':False}}}


def resource_kind(prefab):
    if not isinstance(prefab,str):return None
    name=ALIASES.get(prefab,prefab)
    if 'tree' in name or name.startswith(('evergreen','twiggy')):return 'tree'
    if name in ('grass','cutgrass'):return 'grass'
    if name.startswith('sapling') or name=='twigs':return 'twigs'
    if 'berry' in name:return 'berries'
    if name.startswith('rock') or name=='flint':return 'gold_rock' if name=='rock_gold' else 'rock'
    if name=='goldnugget':return 'gold'
    if 'carrot' in name:return 'carrot'
    if name=='log':return 'log'
    return None


@lru_cache(maxsize=96)
def fallback_icon(kind,size):
    """Readable symbols when a particular icon or the local game is unavailable."""
    from PIL import Image, ImageDraw
    canvas=Image.new('RGBA',(64,64));d=ImageDraw.Draw(canvas);edge='#25251b'
    if kind=='tree':
        d.rectangle((28,38,36,59),fill='#786148',outline=edge,width=2)
        for y,w in ((5,15),(16,21),(29,26)):
            d.polygon(((32,y),(32-w,y+22),(32+w,y+22)),fill='#627550',outline=edge,width=2)
    elif kind=='grass':
        for x,y in ((14,15),(24,6),(36,8),(47,17),(53,28)):
            d.polygon(((28,59),(x,y),(x+2,y+20),(36,59)),fill='#bbaa65',outline=edge,width=2)
    elif kind=='twigs':
        d.line((30,60,34,8),fill=edge,width=7);d.line((30,60,34,8),fill='#a38d6c',width=3)
        for x,y in ((12,14),(50,20),(9,35),(51,39)):
            d.line((33,y+13,x,y),fill=edge,width=6);d.line((33,y+13,x,y),fill='#a38d6c',width=2)
    elif kind=='berries':
        for x,y,r in ((20,37,15),(42,36,16),(32,20,15)):
            d.ellipse((x-r,y-r,x+r,y+r),fill='#7a855d',outline=edge,width=2)
        for x,y in ((20,30),(38,18),(35,39),(17,44),(48,35)):d.ellipse((x-3,y-3,x+3,y+3),fill='#b95643',outline=edge)
    elif kind in ('rock','gold_rock'):
        d.polygon(((7,54),(12,31),(28,10),(41,13),(49,35),(58,51),(37,58)),fill='#a4a399',outline=edge,width=3)
        if kind=='gold_rock':d.line((29,15,27,32,41,43,36,55),fill='#d5b452',width=5)
    elif kind=='gold':d.polygon(((12,46),(25,18),(45,14),(54,37),(38,54)),fill='#d6b34d',outline=edge,width=3)
    elif kind=='log':
        d.rounded_rectangle((9,23,55,48),radius=9,fill='#816344',outline=edge,width=3)
        d.ellipse((37,23,56,48),fill='#c0a06c',outline=edge,width=2)
    elif kind=='carrot':
        d.polygon(((18,22),(48,29),(28,60)),fill='#d68a3d',outline=edge,width=2)
        d.line((32,27,23,7),fill='#718849',width=5);d.line((34,26,46,9),fill='#718849',width=5)
    elif kind=='fire':
        d.line((9,56,55,46),fill='#705139',width=7);d.line((9,46,55,56),fill='#705139',width=7)
        d.polygon(((14,43),(22,21),(27,30),(36,5),(50,34),(45,50),(26,53)),fill='#dd8b35',outline=edge,width=2)
    else:
        d.rounded_rectangle((8,19,56,57),radius=4,fill='#9e8a61',outline=edge,width=3)
        d.polygon(((5,22),(32,3),(59,22)),fill='#72644a',outline=edge,width=3)
    return canvas.resize((size,size),Image.Resampling.LANCZOS)


def render_map(context, center_x=None, center_z=None, span=None, output_dir=None):
    from PIL import Image, ImageDraw, ImageFont
    world_map = context.get('world_map') or {}
    size = world_map.get('cell_size')
    cells = list((world_map.get('cells') or {}).values())
    if not number(size) or size <= 0 or not cells:
        raise ValueError('Observed map unavailable; update Lua Mod and re-enter the world')
    if len(cells)>4096:
        raise ValueError('Map exceeds cell budget')
    if (center_x is None)!=(center_z is None):
        raise ValueError('Supply both center_x and center_z')
    if any(v is not None and (not number(v) or abs(v)>100000) for v in (center_x,center_z)):
        raise ValueError('Invalid map center')
    if span is not None and (not number(span) or not 32<=span<=512):
        raise ValueError('Map span must be 32-512 world units')
    rows = [r for r in cells if isinstance(r,list) and len(r)>=6 and number(r[0]) and number(r[1])]
    if not rows:
        raise ValueError('No observed map cells')
    xs,zs = [r[0]*size for r in rows],[r[1]*size for r in rows]
    cx = (min(xs)+max(xs))/2 if center_x is None else center_x
    cz = (min(zs)+max(zs))/2 if center_z is None else center_z
    span = span or min(512,max(64,math.ceil((max(max(xs)-min(xs),max(zs)-min(zs))+size*4)/16)*16))
    bounds = {'min_x':cx-span/2,'max_x':cx+span/2,'min_z':cz-span/2,'max_z':cz+span/2}
    icons,icon_source=load_icons()
    image = Image.new('RGB',(1120,1360),'#20251f');draw = ImageDraw.Draw(image,'RGBA')
    font = ImageFont.load_default(size=20);small = ImageFont.load_default(size=15)
    left,top,side = 84,70,960
    scale = side/span
    rendered_game,rendered_fallback=0,0
    def sprite(prefab,kind,pixels):
        nonlocal rendered_game,rendered_fallback
        original=icon_for(prefab,icons)
        if original is not None:
            rendered_game+=1
            resized=original.copy();resized.thumbnail((pixels,pixels),Image.Resampling.LANCZOS)
            return resized
        rendered_fallback+=1
        return fallback_icon(kind,pixels)
    def paste_icon(icon,px,pz):
        # Clip to the map, so icons on the viewport edge never cover axis labels.
        ox,oy=round(px-icon.width/2),round(pz-icon.height/2)
        box=(max(0,left-ox),max(0,top-oy),min(icon.width,left+side-ox),min(icon.height,top+side-oy))
        if box[2]>box[0] and box[3]>box[1]:
            piece=icon.crop(box);image.paste(piece,(ox+box[0],oy+box[1]),piece)
    def xy(x,z):
        return left+(x-bounds['min_x'])*scale,top+(bounds['max_z']-z)*scale
    def visible(x,z):
        return bounds['min_x']<=x<=bounds['max_x'] and bounds['min_z']<=z<=bounds['max_z']
    draw.rectangle((left,top,left+side,top+side),fill='#262c35')
    for r in rows:
        x,z=r[0]*size,r[1]*size
        if not visible(x,z):continue
        a,b=xy(x-size/2,z+size/2),xy(x+size/2,z-size/2)
        rect=(max(left,a[0]),max(top,a[1]),min(left+side,b[0]),min(top+side,b[1]))
        draw.rectangle(rect,fill=COLORS.get(r[3],COLORS['unknown']) if r[2]==1 else '#29475d')
    # A road flag samples one point, not an edge. Never join neighbouring flags.
    # Fine masks encode actual 4x4 subcell probes, z rows increasing upwards.
    road_fine_cells=road_samples=coarse_samples=0
    for r in rows:
        if r[2]!=1:continue
        x,z=r[0]*size,r[1]*size
        mask=r[6] if len(r)>6 else None
        if type(mask) is int and 0<=mask<65536:
            road_fine_cells+=1
            unit=size/4
            for bit in range(16):
                if not (mask>>bit)&1:continue
                sx=x-size/2+(bit%4+.5)*unit
                sz=z-size/2+(bit//4+.5)*unit
                a,b=xy(sx-unit/2,sz+unit/2),xy(sx+unit/2,sz-unit/2)
                rect=(max(left,a[0]),max(top,a[1]),min(left+side,b[0]),min(top+side,b[1]))
                if rect[0]<rect[2] and rect[1]<rect[3]:
                    draw.rectangle(rect,fill='#ccb383');road_samples+=1
        elif r[5] and visible(x,z):
            coarse_samples+=1
            px,pz=xy(x,z);d=max(1,min(3,size*scale*.08))
            # Drawing on a clipped layer also keeps viewport-edge dots off labels.
            box=(max(left,px-d),max(top,pz-d),min(left+side,px+d),min(top+side,pz+d))
            draw.ellipse(box,fill='#ccb383')
    step = next(v for v in (8,16,32,64,128) if span/v<=10)
    for axis in ('x','z'):
        start,end = bounds['min_'+axis],bounds['max_'+axis]
        for value in range(math.ceil(start/step)*step,math.floor(end/step)*step+1,step):
            if axis=='x':
                px,_=xy(value,cz);draw.line((px,top,px,top+side),fill=(235,230,202,32),width=1)
                draw.text((px-13,top+side+8),str(value),fill='white',font=small)
            else:
                _,pz=xy(cx,value);draw.line((left,pz,left+side,pz),fill=(235,230,202,32),width=1)
                draw.text((8,pz-6),str(value),fill='white',font=small)
    position = context.get('position') or {}
    resources = {}
    for e in world_map.get('sources',[]):
        if number(e.get('x')) and number(e.get('z')):resources[(e['x'],e['z'],e.get('prefab'))]=(e,False)
    for e in context.get('nearby',[]):
        x=e.get('x',position.get('x',0)+e.get('dx',0));z=e.get('z',position.get('z',0)+e.get('dz',0))
        # Snapshot rounding must not draw a remembered resource twice beside itself.
        for key in list(resources):
            if key[2]==e.get('prefab') and math.hypot(key[0]-x,key[1]-z)<=.15:resources.pop(key)
        resources[(x,z,e.get('prefab'))]=({**e,'x':x,'z':z},True)
    groups={}
    for e,fresh in resources.values():
        x,z=e.get('x'),e.get('z');prefab=e.get('prefab','')
        if not number(x) or not number(z) or not visible(x,z):continue
        kind=resource_kind(prefab)
        if not kind:continue
        px,pz=xy(x,z)
        key=(prefab,int(px//16),int(pz//16))
        if key in groups:
            groups[key]['count']+=1;groups[key]['fresh']|=fresh
        else:groups[key]={'prefab':prefab,'kind':kind,'x':px,'z':pz,'fresh':fresh,'count':1}
    for item in sorted(groups.values(),key=lambda e:e['z']):
        px,pz=item['x'],item['z']
        pixels=round(max(32 if item['kind']=='tree' else 26,min(48,scale*(3.5 if item['kind']=='tree' else 2.5))))
        icon=sprite(item['prefab'],item['kind'],pixels)
        paste_icon(icon,px,pz)
        if not item['fresh']:
            bx,by=px+pixels*.34,pz-pixels*.34
            draw.ellipse((bx-4,by-4,bx+4,by+4),fill='#28291f',outline='#e4c886',width=2)
        if item['count']>1:
            draw.text((px+pixels*.25,pz+pixels*.16),str(item['count']),fill='white',stroke_width=2,stroke_fill='#24291f',font=small)
    for e in context.get('hazards',[]):
        x,z=e.get('x'),e.get('z')
        if number(x) and number(z) and visible(x,z):
            px,pz=xy(x,z);draw.ellipse((px-11,pz-11,px+11,pz+11),outline='#ef8070',width=2)
            draw.text((px-4,pz-10),'!',fill='#ffb09d',font=font,stroke_width=1,stroke_fill='#34251e')
    places=context.get('places') or {}
    buildings=[]
    for e in places.get('buildings',[]):
        x,z=e.get('x'),e.get('z')
        if e.get('status')=='missing' or not number(x) or not number(z) or not visible(x,z):continue
        label='B'+str(len(buildings)+1)
        buildings.append({'label':label,'id':e.get('id'),'prefab':e.get('prefab'),'x':x,'z':z})
        px,pz=xy(x,z)
        paste_icon(sprite(e.get('prefab'), 'fire' if e.get('prefab')=='campfire' else 'building',44),px,pz)
        draw.text((px+20,pz+9),label,fill='#fff3d4',stroke_width=2,stroke_fill='#25291f',font=small)
    home=places.get('home') or {}
    for e,label,color in ((home,'H','#ffffff'),(position,'P','#ff6464')):
        if number(e.get('x')) and number(e.get('z')) and visible(e['x'],e['z']):
            px,pz=xy(e['x'],e['z'])
            portrait=icon_for(context.get('character'),icons) if label=='P' else None
            if portrait is not None:paste_icon(portrait.resize((46,46),Image.Resampling.LANCZOS),px,pz)
            else:draw.ellipse((px-12,pz-12,px+12,pz+12),outline=color,width=3)
            draw.text((px+21,pz-17),label,fill=color,font=font,stroke_width=2,stroke_fill='#25291f')
    world=context.get('world') or {}
    draw.text((24,18),f"OBSERVED MAP  Day {world.get('day','?')} {world.get('phase','?')} | +x right, +z up | world units",fill='#f2ecd7',font=font)
    draw.text((left+side/2,top+side+28),'x',fill='white',font=font);draw.text((25,top-22),'z',fill='white',font=font)
    legend=[('evergreen','tree','Tree'),('sapling','twigs','Twigs'),('grass','grass','Grass'),('berrybush','berries','Berries'),
            ('rock','rock','Rock'),('rock_gold','gold_rock','Gold rock'),('researchlab','building','Science'),('firepit','fire','Fire pit')]
    for i,(prefab,kind,label) in enumerate(legend):
        px=28+i*135;icon=sprite(prefab,kind,30);image.paste(icon,(px,1095),icon)
        draw.text((px+35,1102),label,fill='#e5e1cb',font=small)
    lines=['P player | H home | B# buildings | ! threat | amber hollow badge = historical resource',
           'Small counts group overlapping icons. Records do not prove current stock or safe building space.',
           'Dark blank = unobserved | blue = blocked/water | tan areas = sampled road | tan dots = coarse samples']
    for i,line in enumerate(lines):draw.text((24,1144+i*23),line,fill='#d8dccd',font=small)
    for i,(name,color) in enumerate(COLORS.items()):
        px,pz=24+(i%5)*216,1220+(i//5)*25
        draw.rectangle((px,pz,px+13,pz+13),fill=color)
        draw.text((px+19,pz),name.replace('_forest',''),fill='#d2d8e1',font=small)
    for i,b in enumerate(buildings[:2]):
        draw.text((24,1280+i*24),f"{b['label']} {b['prefab']} | {b['id']} | ({b['x']:g}, {b['z']:g})",fill='#d8dccd',font=small)
    draw.text((24,1334),'Only observed cells are shown. Inspect on arrival before building.',fill='#d8dccd',font=small)
    stream=io.BytesIO();image.save(stream,format='PNG');png=stream.getvalue()
    metadata={'observed_at':context.get('observed_at'),'map_version':world_map.get('version'),
        'bounds':bounds,'axes':'+x right, +z up; game world units','cell_size':size,'cells':len(rows),
        'truncated':bool(world_map.get('truncated')),'image_size':list(image.size),
        'icon_source':icon_source,'game_icons':rendered_game,'fallback_icons':rendered_fallback,
        'resource_groups':len(groups),'buildings':buildings,
        'road_rendering':'observed_subcell_mask','road_subdivisions':4,
        'road_fine_cells':road_fine_cells,'road_samples':road_samples,
        'road_coarse_samples':coarse_samples,'road_connections_inferred':False,
        'instructions':'Select world x/z, not image pixels. Validate observed land, path and safety. No automatic home anchor.'}
    if output_dir is not None:
        path=Path(output_dir);path.mkdir(parents=True,exist_ok=True)
        (path/'planner_map.png').write_bytes(png)
        import json
        (path/'planner_map.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    return metadata,'data:image/png;base64,'+base64.b64encode(png).decode('ascii')
