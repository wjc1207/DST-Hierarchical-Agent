"""Local game texture decoding and visual map semantics without installed assets."""
import base64
import io
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from mcp_agent.map_icons import decode_ktex, crop_sprite, load_icons, icon_for
from mcp_agent.map_view import render_map, resource_kind


def block(color):
    return struct.pack('<BB6sHHI',255,0,b'\0'*6,color,0,0)


def texture(top=0xf800,bottom=0x07e0):
    return b'KTEX'+struct.pack('<I',2<<4|1<<13)+struct.pack('<HHHI',4,8,16,32)+block(top)+block(bottom)


class IconTests(unittest.TestCase):
    def test_decode_actual_bc3_shape_uv_flip_and_alpha(self):
        atlas=decode_ktex(texture())
        self.assertEqual(atlas.size,(4,8));self.assertEqual(atlas.getpixel((0,0)),(255,0,0,255))
        sprite=crop_sprite(atlas,{'u1':'0','u2':'1','v1':'0','v2':'1'})
        self.assertEqual(sprite.getpixel((0,0)),(0,255,0,255))
        translucent=Image.new('RGBA',(2,2),(60,30,15,128))
        restored=crop_sprite(translucent,{'u1':'0','u2':'1','v1':'0','v2':'1'})
        self.assertEqual(restored.getpixel((0,0)),(120,60,30,128))

    def test_bad_or_unsupported_textures_and_uv_are_rejected(self):
        for raw in (b'PNG',texture()[:25],b'KTEX'+b'\0'*30):
            with self.assertRaises(ValueError):decode_ktex(raw)
        with self.assertRaises(ValueError):crop_sprite(Image.new('RGBA',(4,4)),{'u1':'0','u2':'2','v1':'0','v2':'1'})

    def test_installed_atlas_cache_refreshes_when_game_files_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);mini=root/'data/minimap';mini.mkdir(parents=True)
            tex=mini/'minimap_atlas.tex';tex.write_bytes(texture())
            (mini/'minimap_data.xml').write_text('<Atlas><Texture filename="minimap_atlas.tex"/><Elements>'
                '<Element name="grass.png" u1="0" u2="1" v1="0" v2="0.5"/></Elements></Atlas>')
            first,source=load_icons(root/'data',root/'cache')
            self.assertEqual(source,'game_minimap');self.assertEqual(first['grass'].getpixel((0,0)),(255,0,0,255))
            self.assertTrue(list((root/'cache').glob('*/grass.png')))
            tex.write_bytes(texture(top=0x001f));clock=tex.stat().st_mtime_ns+10000000
            os.utime(tex,ns=(clock,clock))
            second,source=load_icons(root/'data',root/'cache')
            self.assertEqual(second['grass'].getpixel((0,0)),(0,0,255,255))
            missing,source=load_icons(root/'missing',root/'cache')
            self.assertEqual(missing,{});self.assertEqual(source,'vector_fallback')

    def test_prefab_aliases_do_not_confuse_tree_types_or_permanent_fires(self):
        icons={'evergreen_lumpy':object(),'tree_leaf':object(),'rock_gold':object(),'firepit':object()}
        self.assertIs(icon_for('deciduoustree',icons),icons['tree_leaf'])
        self.assertIs(icon_for('rock2',icons),icons['rock_gold'])
        self.assertIsNone(icon_for('campfire',icons))
        self.assertEqual(resource_kind('evergreen_sparse'),'tree')
        self.assertEqual(resource_kind('sapling'),'twigs')

    def test_fallback_map_keeps_resources_and_deduplicates_rounded_observations(self):
        context={'world_map':{'version':1,'cell_size':4,'cells':{'0:0':[0,0,1,'grassland','A',0]},
                   'sources':[{'x':0,'z':0,'prefab':'grass'},{'x':.3,'z':.3,'prefab':'grass'}]},
                 'position':{'x':12,'z':12},'nearby':[{'x':.01,'z':.01,'prefab':'grass'}]}
        with patch('mcp_agent.map_view.load_icons',return_value=({},'vector_fallback')):
            meta,url=render_map(context,center_x=0,center_z=0,span=64)
        self.assertEqual(meta['icon_source'],'vector_fallback');self.assertEqual(meta['resource_groups'],1)
        self.assertGreater(meta['fallback_icons'],0)
        image=Image.open(io.BytesIO(base64.b64decode(url.split(',',1)[1])))
        self.assertEqual(image.size,(1120,1360))
        self.assertNotEqual(image.getpixel((564,550)),(137,152,90))
