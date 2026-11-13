"""Road raster geometry must not invent connections, shortcuts or unexplored land."""
import base64
import io
import unittest
from unittest.mock import patch
from PIL import Image
from mcp_agent.map_view import render_map
from mcp_server.game_bridge import GameBridge
from test_mcp_agent import snapshot

TAN=(204,179,131)
GREEN=(137,152,90)


def render(rows,**opts):
    context={'world_map':{'cell_size':4,'cells':{str(i):r for i,r in enumerate(rows)}}}
    with patch('mcp_agent.map_view.load_icons',return_value=({},'vector_fallback')):
        meta,url=render_map(context,center_x=0,center_z=0,span=64,**opts)
    return meta,Image.open(io.BytesIO(base64.b64decode(url.split(',',1)[1])))


def pixel(image,x,z):return image.getpixel((round(84+(x+32)*15),round(70+(32-z)*15)))


def row(x,z,road=0,mask=None):
    r=[x,z,1,'grassland','A',road]
    if mask is not None:r.append(mask)
    return r


class RoadRenderingTests(unittest.TestCase):
    def test_legacy_flags_are_dots_without_triangles_or_diagonals(self):
        meta,im=render([row(1,1,1),row(2,1,1),row(1,2,1),row(2,2,1)])
        self.assertEqual(pixel(im,4,4),TAN)
        for x,z in [(6,4),(4,6),(6,6),(7.5,6)]:self.assertEqual(pixel(im,x,z),GREEN)
        self.assertEqual(meta['road_coarse_samples'],4)
        self.assertFalse(meta['road_connections_inferred'])

    def test_mask_uses_southwest_low_bit_and_northeast_high_bit(self):
        meta,im=render([row(-2,-2,mask=1+(1<<15))])
        self.assertEqual(pixel(im,-9.5,-9.5),TAN)
        self.assertEqual(pixel(im,-6.5,-6.5),TAN)
        self.assertEqual(pixel(im,-9.5,-6.5),GREEN)
        self.assertEqual(pixel(im,-7.5,-7.5),GREEN)
        self.assertEqual(meta['road_samples'],2)

    def test_fine_mask_finds_road_even_when_coarse_center_is_not_on_road(self):
        _,im=render([row(1,1,road=0,mask=1)])
        self.assertEqual(pixel(im,2.5,2.5),TAN)
        self.assertEqual(pixel(im,4.5,4.5),GREEN)

    def test_zero_mask_is_known_empty_and_suppresses_coarse_line(self):
        meta,im=render([row(1,1,road=1,mask=0)])
        self.assertEqual(pixel(im,4,4),GREEN)
        self.assertEqual(meta['road_coarse_samples'],0)
        self.assertEqual(meta['road_fine_cells'],1)

    def test_fine_roads_continue_across_shared_cell_edge_without_extra_edges(self):
        _,im=render([row(1,1,mask=15),row(2,1,mask=15)])
        for x in [2.5,4.5,5.9,6.1,8.5,9.5]:self.assertEqual(pixel(im,x,2.5),TAN)
        self.assertEqual(pixel(im,6,4.5),GREEN)

    def test_diagonal_samples_do_not_authorize_a_connection(self):
        _,im=render([row(1,1,mask=1),row(2,2,mask=1),row(2,1),row(1,2)])
        self.assertEqual(pixel(im,4.5,4.5),GREEN)
        self.assertEqual(pixel(im,6.5,6.5),TAN)

    def test_water_and_viewport_clip_do_not_paint_road_outside_map(self):
        water=row(1,1,mask=65535);water[2]=0
        meta,im=render([water,row(8,8,mask=65535),row(-8,-8,1)])
        self.assertNotEqual(pixel(im,4.5,4.5),TAN)
        self.assertEqual(im.getpixel((1046,68)),(32,37,31))
        self.assertEqual(im.getpixel((81,1034)),(32,37,31))
        self.assertEqual(meta['road_samples'],4)

    def test_bad_masks_fall_back_to_points_not_arbitrary_bit_patterns(self):
        for bad in [True,-1,65536,'65535',3.5]:
            with self.subTest(bad=bad):
                meta,im=render([row(1,1,road=1,mask=bad)])
                self.assertEqual(meta['road_coarse_samples'],1)
                self.assertEqual(pixel(im,2.5,2.5),GREEN)

    def test_map_patch_preserves_zero_and_fine_masks(self):
        bridge=GameBridge('road-token-'*4);s=snapshot()
        s['world_map']={'version':1,'cell_size':4,'cells':{'1:1':row(1,1)}}
        bridge.exchange({'token':bridge.token,'session_id':'road','sequence':1,'state':s})
        s['world_map']={'version':2,'cell_size':4,'road_subdivisions':4,
            'patch':{'base_version':1,'updates':{'1:1':row(1,1,mask=0),'2:1':row(2,1,mask=1)},'removed':[]}}
        bridge.exchange({'token':bridge.token,'session_id':'road','sequence':2,'state':s})
        m=bridge.get_state()['state']['world_map']
        self.assertEqual(m['cells']['1:1'][6],0)
        self.assertEqual(m['cells']['2:1'][6],1)
        self.assertEqual(m['road_subdivisions'],4)
