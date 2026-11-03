"""Read installed DST minimap art; generated PNGs stay in the ignored logs cache."""
import hashlib
import json
import os
import struct
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from common.paths import PROJECT_ROOT, LOG_DIR

# Prefab names are not always the names used in Klei's map atlas.
ALIASES = {'evergreen_sparse':'evergreen_lumpy','deciduoustree':'tree_leaf','twiggytree':'twiggy',
           'sapling_moon':'sapling','rock1':'rock','rock2':'rock_gold','rock_flintless_med':'rock_flintless',
           'rock_flintless_low':'rock_flintless','sciencemachine':'researchlab','alchemyengine':'researchlab2',
           'homesign':'sign'}
WANTED = {'evergreen','evergreen_lumpy','tree_leaf','twiggy','sapling','grass','berrybush','berrybush2',
          'berrybush_juicy','rock','rock_gold','rock_flintless','rock_moon','researchlab','researchlab2',
          'researchlab3','researchlab4','firepit','sign','wilson','wendy','willow','wolfgang','wx78',
          'wickerbottom','woodie','wes','waxwell','wathgrithr','webber','winona','warly','wortox','wormwood',
          'wurt','walter','wanda'}
CACHE_VERSION = 1


def game_data_directory():
    explicit = os.environ.get('DST_GAME_DIR')
    roots = ([Path(explicit)] if explicit else []) + [PROJECT_ROOT.parents[1],
        Path(os.environ.get('ProgramFiles(x86)',r'C:\Program Files (x86)'))/'Steam/steamapps/common/Don\'t Starve Together',
        Path.home()/'.local/share/Steam/steamapps/common/Don\'t Starve Together']
    for root in roots:
        for data in (root/'data',root):
            if (data/'minimap/minimap_data.xml').is_file():
                return data
    return None


def decode_ktex(data):
    """Decode the installed PC DXT5/BC3 atlas using Pillow's built-in decoder.

    KTEX header and mip table: github.com/oblivioncth/Stexatlaser#klei-tex-format.
    Crop UVs in raw bottom-up coordinates, then flip individual sprites upright.
    """
    from PIL import Image
    if len(data)<18 or data[:4]!=b'KTEX':
        raise ValueError('Invalid KTEX header')
    header,=struct.unpack_from('<I',data,4)
    pixel_format,mips=(header>>4)&31,(header>>13)&31
    if pixel_format!=2 or not 1<=mips<=16 or len(data)<8+10*mips:
        raise ValueError('Unsupported or invalid KTEX atlas (expected PC DXT5)')
    width,height,_,length=struct.unpack_from('<HHHI',data,8)
    expected=((width+3)//4)*((height+3)//4)*16
    start=8+10*mips
    if not 1<=width<=8192 or not 1<=height<=8192 or length!=expected or start+length>len(data):
        raise ValueError('Invalid KTEX mip dimensions or truncated texture')
    return Image.frombytes('RGBA',(width,height),data[start:start+length],'bcn',(3,))


def crop_sprite(atlas, element):
    from PIL import Image
    u1,u2,v1,v2=(float(element.get(k))for k in ('u1','u2','v1','v2'))
    if not (0<=u1<u2<=1 and 0<=v1<v2<=1):
        raise ValueError('Invalid atlas UV rectangle')
    w,h=atlas.size
    icon=atlas.crop((round(u1*w),round(v1*h),round(u2*w),round(v2*h))).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    # Game textures use premultiplied alpha; PNG/Pillow compositing expects straight alpha.
    pixels=bytearray(icon.tobytes())
    for i in range(0,len(pixels),4):
        alpha=pixels[i+3]
        if 0<alpha<255:
            for channel in range(3):pixels[i+channel]=min(255,round(pixels[i+channel]*255/alpha))
    return Image.frombytes('RGBA',icon.size,bytes(pixels))


def source_signature(data_dir):
    sources=[]
    for xml in sorted((data_dir/'minimap').glob('minimap_data*.xml')):
        root=ET.fromstring(xml.read_text(encoding='utf-8'))
        tex=xml.parent/root.find('Texture').get('filename')
        if tex.resolve().parent!=xml.parent.resolve():
            continue
        sources.extend((xml,tex))
    fingerprint=[(str(p),p.stat().st_size,p.stat().st_mtime_ns)for p in sources]
    digest=hashlib.sha256(json.dumps([CACHE_VERSION,fingerprint]).encode()).hexdigest()[:20]
    return digest


@lru_cache(maxsize=2)
def _load(data_path,cache_path,signature):
    from PIL import Image
    data_dir,cache_dir=Path(data_path),Path(cache_path)
    cache_dir=cache_dir/signature
    manifest=cache_dir/'manifest.json'
    if manifest.is_file():
        try:
            names=json.loads(manifest.read_text(encoding='utf-8')).get('icons',[])
            return {name:Image.open(cache_dir/(name+'.png')).convert('RGBA')for name in names if name in WANTED}
        except (OSError,ValueError,TypeError):
            pass  # Rebuild an incomplete local cache from the installed atlas.
    icons={}
    for xml in sorted((data_dir/'minimap').glob('minimap_data*.xml')):
        root=ET.fromstring(xml.read_text(encoding='utf-8'))
        elements={Path(e.get('name','')).stem:e for e in root.findall('.//Element')}
        needed=(WANTED-icons.keys())&elements.keys()
        if not needed:continue
        tex=xml.parent/root.find('Texture').get('filename')
        if tex.resolve().parent!=xml.parent.resolve():continue
        atlas=decode_ktex(tex.read_bytes())
        for name in sorted(needed):icons[name]=crop_sprite(atlas,elements[name])
    cache_dir.mkdir(parents=True,exist_ok=True)
    for name,icon in icons.items():icon.save(cache_dir/(name+'.png'))
    manifest.write_text(json.dumps({'signature':signature,'icons':sorted(icons),
        'source':'installed DST minimap atlases; local cache only'},indent=2),encoding='utf-8')
    return icons


def load_icons(data_dir=None,cache_dir=None):
    data_dir=Path(data_dir) if data_dir else game_data_directory()
    if data_dir is None:return {},'vector_fallback'
    try:
        icons=_load(str(data_dir),str(cache_dir or LOG_DIR/'map_icons'),source_signature(data_dir))
        return icons,'game_minimap' if icons else 'vector_fallback'
    except (OSError,ValueError,ET.ParseError,KeyError,TypeError,struct.error):
        # A missing installation/cache or changed format cannot break strategic planning.
        return {},'vector_fallback'


def icon_for(prefab,icons):
    return icons.get(ALIASES.get(prefab,prefab))
