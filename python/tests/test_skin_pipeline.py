"""Tests for the DST character skin asset pipeline and DeepSeek skin archive."""
from __future__ import annotations

from pathlib import Path
import json
import re
import struct
import tempfile
import unittest
from zipfile import ZipFile

from common.paths import PROJECT_ROOT

try:
    from PIL import Image
    from tools import skin_pipeline, skin_player, skin_preview
except ImportError:  # pragma: no cover - optional Pillow dependency outside .venv-mcp
    Image = None
    skin_pipeline = None
    skin_player = None
    skin_preview = None


def _klei_hash(name: str) -> int:
    value = 0
    for byte in name.lower().encode("ascii"):
        value = (byte + (value << 6) + (value << 16) - value) & 0xFFFFFFFF
    return value


class SkinArchiveValidationTests(unittest.TestCase):
    def test_generated_deepseek_skin_archive_is_valid(self) -> None:
        zip_path = PROJECT_ROOT / "mods" / "dst_buddy_agent" / "anim" / "skin_deepseek.zip"
        self.assertTrue(zip_path.is_file(), f"Missing skin archive: {zip_path}")
        with ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())
            self.assertEqual(names, {"build.bin", "atlas-0.tex"})
            build_bytes = zf.read("build.bin")
            tex_bytes = zf.read("atlas-0.tex")

        magic, version, num_symbols, _ = struct.unpack_from("<4sIII", build_bytes, 0)
        self.assertEqual(magic, b"BILD")
        self.assertEqual(version, 6)
        self.assertGreaterEqual(num_symbols, 19)

        self.assertEqual(tex_bytes[:4], b"KTEX")
        header = struct.unpack_from("<I", tex_bytes, 4)[0]
        compression = (header >> 4) & 31
        width, height, _, _ = struct.unpack_from("<HHHI", tex_bytes, 8)
        self.assertEqual(compression, 4)
        self.assertEqual((width, height), (2048, 2048))


@unittest.skipIf(skin_pipeline is None, "Pillow not installed in this Python environment")
class SkinPipelineCodecTests(unittest.TestCase):
    def test_klei_hash_is_case_insensitive(self) -> None:
        self.assertEqual(skin_pipeline.klei_hash("HeadBase"), skin_pipeline.klei_hash("headbase"))
        self.assertEqual(skin_pipeline.klei_hash("long_hair_blank"), _klei_hash("LONG_HAIR_BLANK"))

    def test_ktex_rgba_roundtrip_preserves_opaque_and_transparent_pixels(self) -> None:
        img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        # Navy hair, cyan tip, and fair skin
        img.putpixel((2, 3), (30, 39, 73, 255))
        img.putpixel((5, 7), (110, 208, 230, 255))
        img.putpixel((9, 11), (247, 221, 208, 255))
        encoded = skin_pipeline.encode_ktex_rgba(img)
        decoded = skin_pipeline.decode_ktex(encoded)
        self.assertEqual(decoded.size, (16, 16))
        self.assertEqual(decoded.getpixel((0, 0)), (0, 0, 0, 0))
        self.assertEqual(decoded.getpixel((2, 3)), (30, 39, 73, 255))
        self.assertEqual(decoded.getpixel((5, 7)), (110, 208, 230, 255))
        self.assertEqual(decoded.getpixel((9, 11)), (247, 221, 208, 255))

    def test_build_bin_roundtrip_preserves_sorted_hashes_and_uvs(self) -> None:
        symbols = {
            "torso": [
                {"framenum": 0, "duration": 1, "cx": 1.0, "cy": -40.0, "w": 64.0, "h": 80.0, "atlas_rect": (10, 20, 74, 100)},
            ],
            "headbase": [
                {"framenum": 0, "duration": 1, "cx": -10.0, "cy": -78.0, "w": 200.0, "h": 220.0, "atlas_rect": (100, 200, 300, 420)},
            ],
            "long_hair_blank": [
                {"framenum": 0, "duration": 1, "cx": 0.0, "cy": 0.0, "w": 4.0, "h": 4.0, "atlas_rect": (0, 0, 4, 4)},
            ],
        }
        raw = skin_pipeline.write_build_bin("skin_test", "atlas-0.tex", symbols, 2048, 2048)
        parsed = skin_pipeline.parse_build_bin(raw)
        self.assertEqual(parsed["build_name"], "skin_test")
        self.assertEqual(set(parsed["symbols"]), {"torso", "headbase", "long_hair_blank"})
        hb = parsed["symbols"]["headbase"][0]
        quad = parsed["verts"][hb["vstart"]:hb["vstart"] + hb["vcount"]]
        self.assertEqual(len(quad), 6)
        # Top edge (smaller Y) must map to larger V = 1 - 200/2048
        top_vs = [v[4] for v in quad if v[1] < hb["cy"]]
        bot_vs = [v[4] for v in quad if v[1] > hb["cy"]]
        self.assertTrue(all(abs(v - (1.0 - 200.0 / 2048.0)) < 1e-5 for v in top_vs))
        self.assertTrue(all(abs(v - (1.0 - 420.0 / 2048.0)) < 1e-5 for v in bot_vs))

    def test_remove_green_screen_protects_deepseek_cyan_hair(self) -> None:
        img = Image.new("RGBA", (32, 32), (0, 255, 0, 255))
        for y in range(8, 24):
            for x in range(8, 24):
                # Deep navy and bright cyan hair gradient (#6ED0E6)
                img.putpixel((x, y), (110, 208, 230, 255) if y >= 16 else (30, 39, 73, 255))
        cleaned = skin_pipeline.remove_green_screen(img)
        self.assertEqual(cleaned.getpixel((0, 0))[3], 0)
        self.assertGreater(cleaned.getpixel((16, 12))[3], 240)
        self.assertGreater(cleaned.getpixel((16, 20))[3], 240)
        r, g, b, _ = cleaned.getpixel((16, 20))
        self.assertGreater(b, g)

    def test_packed_deepseek_zip_decodes_cleanly(self) -> None:
        zip_path = PROJECT_ROOT / "mods" / "dst_buddy_agent" / "anim" / "skin_deepseek.zip"
        with ZipFile(zip_path, "r") as zf:
            parsed = skin_pipeline.parse_build_bin(zf.read("build.bin"))
            atlas = skin_pipeline.decode_ktex(zf.read("atlas-0.tex"))
        self.assertEqual(atlas.size, (2048, 2048))
        expected = {
            "headbase", "headbase_hat", "hair", "hair_hat", "tail", "hairpigtails",
            "torso", "torso_pelvis", "skirt", "torso_wide", "torso_pelvis_wide", "skirt_wide",
            "arm_upper", "arm_upper_skin", "arm_lower", "hand", "leg", "foot",
            "face", "cheeks", "long_hair_blank", "SWAP_ICON",
        }
        self.assertTrue(expected.issubset(set(parsed["symbols"])))
        self.assertEqual(len(parsed["symbols"]["face"]), 33)
        self.assertEqual(len(parsed["symbols"]["hair"]), 4)
        self.assertEqual(len(parsed["symbols"]["hair_hat"]), 4)
        self.assertEqual(len(parsed["symbols"]["hairpigtails"]), 5)
        self.assertEqual(len(parsed["symbols"]["tail"]), 12)
        # Hybrid long-hair layout: front/side/X-ray hair slots are 4x4 blanks, back hair:2 is populated.
        hair_frames = {f["framenum"]: f for f in parsed["symbols"]["hair"]}
        hair_hat_frames = {f["framenum"]: f for f in parsed["symbols"]["hair_hat"]}
        for blank_idx in (0, 1, 3):
            self.assertLessEqual(hair_frames[blank_idx]["w"], 4.0)
            self.assertLessEqual(hair_frames[blank_idx]["h"], 4.0)
            self.assertLessEqual(hair_hat_frames[blank_idx]["w"], 4.0)
            self.assertLessEqual(hair_hat_frames[blank_idx]["h"], 4.0)
        self.assertGreater(hair_frames[2]["w"], 50.0)
        self.assertGreater(hair_frames[2]["h"], 50.0)
        self.assertGreater(hair_hat_frames[2]["w"], 50.0)
        self.assertGreater(hair_hat_frames[2]["h"], 50.0)

        # Face alignment stability: derived same-view faces share exact source anchors (zero blink/eye jump),
        # and angry shouting group (25, 26, 27) shares the exact top eye edge & horizontal center.
        self.assertEqual(skin_pipeline.DEFAULT_FACE_MAP[22], 20)
        self.assertEqual(skin_pipeline.DEFAULT_FACE_MAP[30], 28)
        face_frames = {f["framenum"]: f for f in parsed["symbols"]["face"]}
        for src_idx, dst_idx in ((0, 1), (28, 30), (25, 32)):
            for attr in ("cx", "cy", "w", "h"):
                self.assertAlmostEqual(
                    face_frames[dst_idx][attr],
                    face_frames[src_idx][attr],
                    places=2,
                    msg=f"face:{dst_idx}.{attr} drifted from face:{src_idx}",
                )
        # Bored/drowsy pair (20=small open sighing mouth, 22=closed frown) share identical upper eyes
        top_20 = face_frames[20]["cy"] - face_frames[20]["h"] * 0.5
        top_22 = face_frames[22]["cy"] - face_frames[22]["h"] * 0.5
        self.assertLess(abs(face_frames[20]["cx"] - face_frames[22]["cx"]), 0.15)
        self.assertLess(abs(top_20 - top_22), 0.15)
        self.assertLess(abs(face_frames[20]["w"] - face_frames[22]["w"]), 0.20)
        self.assertGreater(face_frames[20]["h"], face_frames[22]["h"])
        top_25 = face_frames[25]["cy"] - face_frames[25]["h"] * 0.5
        for grp_idx in (26, 27):
            top_g = face_frames[grp_idx]["cy"] - face_frames[grp_idx]["h"] * 0.5
            self.assertLess(abs(face_frames[grp_idx]["cx"] - face_frames[25]["cx"]), 0.15)
            self.assertLess(abs(top_g - top_25), 0.15)
            self.assertLess(abs(face_frames[grp_idx]["w"] - face_frames[25]["w"]), 0.20)

    def test_remove_green_screen_despills_2px_dark_green_fringe(self) -> None:
        img = Image.new("RGBA", (24, 24), (0, 255, 0, 255))
        # Dark outline interior with a 2px greenish spill border
        for y in range(6, 18):
            for x in range(6, 18):
                img.putpixel((x, y), (30, 39, 73, 255))
        for x in range(6, 18):
            img.putpixel((x, 6), (25, 70, 30, 255))
            img.putpixel((x, 7), (25, 60, 30, 255))
        cleaned = skin_pipeline.remove_green_screen(img)
        # 2px inward pixel must have its green channel clamped down to <= max(r, b)
        r2, g2, b2, a2 = cleaned.getpixel((12, 7))
        self.assertGreater(a2, 200)
        self.assertLessEqual(g2, max(r2, b2))

    def test_prepare_rework_writes_template_and_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            sprites = work / "sprites"
            sprites.mkdir()
            sp = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
            for y in range(10, 30):
                for x in range(10, 30):
                    sp.putpixel((x, y), (30, 40, 80, 255))
            sp.save(sprites / "skirt_02.png")
            manifest = {
                "symbols": {
                    "skirt": [
                        {"framenum": i, "duration": 1, "tight_bbox": [10, 10, 30, 30]}
                        for i in range(4)
                    ]
                }
            }
            (work / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            img_p, lay_p = skin_pipeline.prepare_rework(work, [("skirt", 2, "front run")], name="rework_t", cols=1, cell_size=120)
            self.assertTrue(img_p.is_file())
            layout = json.loads(lay_p.read_text(encoding="utf-8"))
            self.assertEqual(layout["sheet_id"], "rework_t")
            self.assertEqual(len(layout["cells"]), 1)
            self.assertEqual(layout["cells"][0]["symbol"], "skirt")
            self.assertEqual(layout["cells"][0]["frame_idx"], 2)

    def test_parse_anim_bin_v4_roundtrip(self) -> None:
        root_h = _klei_hash("wilson")
        sym_h = _klei_hash("torso")
        lay_h = _klei_hash("torso")
        clip_name = b"idle_loop"
        parts = [
            b"ANIM",
            struct.pack("<5I", 4, 1, 1, 0, 1),
            struct.pack("<I", len(clip_name)),
            clip_name,
            struct.pack("<BIfI", 0x08, root_h, 30.0, 1),
            struct.pack("<4fII", 0.0, 0.0, 100.0, 200.0, 0, 1),
            struct.pack("<3I7f", sym_h, 3, lay_h, 1.0, 0.0, 0.0, 1.0, 5.0, -10.0, 0.0),
            struct.pack("<I", 2),
            struct.pack("<II", root_h, 6), b"wilson",
            struct.pack("<II", sym_h, 5), b"torso",
        ]
        parsed = skin_preview.parse_anim_bin(b"".join(parts))
        self.assertEqual(parsed["version"], 4)
        self.assertEqual(len(parsed["anims"]), 1)
        anim = parsed["anims"][0]
        self.assertEqual(anim["name"], "idle_loop")
        self.assertEqual(anim["facing"], 0x08)
        self.assertEqual(anim["root"], "wilson")
        elem = anim["frames"][0]["elements"][0]
        self.assertEqual(elem["symbol"], "torso")
        self.assertEqual(elem["frame_num"], 3)

    def test_pick_representative_frame_prefers_xray_slot(self) -> None:
        anim = {
            "frames": [
                {"elements": [{"symbol": "headbase", "frame_num": 0}]},
                {"elements": [{"symbol": "headbase", "frame_num": 3}, {"symbol": "torso", "frame_num": 10}]},
                {"elements": [{"symbol": "headbase", "frame_num": 0}]},
            ]
        }
        self.assertEqual(skin_preview._pick_representative_frame(anim, "shock"), 1)
        self.assertEqual(skin_preview._pick_representative_frame(anim, "shock", explicit_frame=2), 2)

    def test_render_animation_review_synthetic_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # Build minimal valid build.zip (base + skin)
            atlas = Image.new("RGBA", (32, 32), (80, 140, 220, 255))
            tex_bytes = skin_pipeline.encode_ktex_rgba(atlas)
            syms = {
                "torso": [{"framenum": 0, "duration": 1, "cx": 0.0, "cy": 0.0, "w": 32.0, "h": 32.0, "atlas_rect": (0, 0, 32, 32)}],
                "long_hair_blank": [{"framenum": 0, "duration": 1, "cx": 0.0, "cy": 0.0, "w": 2.0, "h": 2.0, "atlas_rect": (0, 0, 2, 2)}],
            }
            base_zip = tmp / "wilson.zip"
            skin_zip = tmp / "skin_test.zip"
            for zp, bname in ((base_zip, "wilson"), (skin_zip, "skin_test")):
                with ZipFile(zp, "w") as zf:
                    zf.writestr("build.bin", skin_pipeline.write_build_bin(bname, "atlas-0.tex", syms, 32, 32))
                    zf.writestr("atlas-0.tex", tex_bytes)

            # Build minimal valid anim.zip with 1 clip
            root_h = _klei_hash("wilson")
            sym_h = _klei_hash("torso")
            lay_h = _klei_hash("torso")
            clip_name = b"test_clip"
            anim_bytes = b"".join([
                b"ANIM",
                struct.pack("<5I", 4, 1, 1, 0, 1),
                struct.pack("<I", len(clip_name)),
                clip_name,
                struct.pack("<BIfI", 0x08, root_h, 30.0, 1),
                struct.pack("<4fII", 0.0, 0.0, 64.0, 64.0, 0, 1),
                struct.pack("<3I7f", sym_h, 0, lay_h, 1.0, 0.0, 0.0, 1.0, 0.0, -50.0, 0.0),
                struct.pack("<I", 2),
                struct.pack("<II", root_h, 6), b"wilson",
                struct.pack("<II", sym_h, 5), b"torso",
            ])
            anim_zip = tmp / "player_test.zip"
            with ZipFile(anim_zip, "w") as zf:
                zf.writestr("anim.bin", anim_bytes)

            out_dir = tmp / "review_out"
            res = skin_preview.render_animation_review(
                base_zip=base_zip,
                skin_zip=skin_zip,
                ref_zip=base_zip,
                out_dir=out_dir,
                anim_dir=tmp,
                suite="full",
                anim_zips=["player_test.zip"],
                cols=1,
                rows_per_sheet=1,
                pane_size=(80, 90),
            )
            self.assertEqual(res["summary"]["total_rendered"], 1)
            self.assertEqual(res["summary"]["missing_frames_count"], 0)
            self.assertEqual(res["summary"]["coverage"]["covered_count"], 1)
            self.assertTrue(Path(res["review_html"]).is_file())
            self.assertTrue(Path(res["report_md"]).is_file())
            self.assertTrue(Path(res["report_json"]).is_file())
            self.assertTrue(Path(res["review_example_json"]).is_file())
            self.assertTrue(Path(res["sheets"][0]).is_file())
            tpl = json.loads(Path(res["review_example_json"]).read_text(encoding="utf-8"))
            self.assertEqual(tpl["schema"], "dst_skin_anim_review_v1")
            self.assertEqual(len(tpl["items"]), 1)
            only_entry = next(iter(tpl["items"].values()))
            self.assertIs(only_entry["approved"], False)
            self.assertTrue(all(v is None for v in only_entry["checks"].values()))

    def test_prepare_rework_from_plan_writes_prompt_and_template(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            sprites = work / "sprites"
            sprites.mkdir()
            img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
            for y in range(10, 30):
                for x in range(10, 30):
                    img.putpixel((x, y), (200, 100, 80, 255))
            img.save(sprites / "cheeks_00.png")
            manifest = {
                "symbols": {
                    "cheeks": [{"framenum": 0, "duration": 1, "tight_bbox": [10, 10, 30, 30]}]
                }
            }
            (work / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            plan_path = work / "rework_plan.json"
            plan_path.write_text(
                json.dumps({
                    "items": [
                        {
                            "slot": "cheeks:0",
                            "symbol": "cheeks",
                            "frame": 0,
                            "issue": "Drawn as orange blush dots instead of puffed cheeks.",
                            "expected": "Skin-toned puffed cheek arcs matching face skin tone.",
                        }
                    ]
                }),
                encoding="utf-8",
            )
            img_p, lay_p, prompt_p = skin_pipeline.prepare_rework_from_plan(work, plan_path, name="rework6", cell_size=120)
            self.assertTrue(img_p.is_file())
            self.assertTrue(lay_p.is_file())
            self.assertTrue(prompt_p.is_file())
            prompt_txt = prompt_p.read_text(encoding="utf-8")
            self.assertIn("cheeks:0", prompt_txt)
            self.assertIn("Observed issue: Drawn as orange blush dots", prompt_txt)
            self.assertIn("Expected fix: Skin-toned puffed cheek arcs", prompt_txt)
            rev_ex_p = work / "rework6_review.example.json"
            self.assertTrue(rev_ex_p.is_file())
            rev_ex = json.loads(rev_ex_p.read_text(encoding="utf-8"))
            self.assertEqual(rev_ex["schema"], "dst_skin_rework_cell_review_v1")
            cell0 = rev_ex["cells"]["cheeks:0"]
            self.assertIs(cell0["approved"], False)
            self.assertTrue(all(v is None for v in cell0["checks"].values()))

    def test_render_anim_frame_auto_frame_keeps_high_jump_inside_canvas(self) -> None:
        atlas = Image.new("RGBA", (40, 40), (220, 60, 60, 255))
        base_build = {
            "build_name": "test",
            "symbols": {
                "torso": {
                    0: {"image": atlas, "cx": 0.0, "cy": 0.0, "w": 40.0, "h": 40.0, "framenum": 0, "duration": 1}
                }
            },
        }
        # High-jump frame located far above ground (y = -450)
        anim_data = {
            "anims": [
                {
                    "name": "jump_high",
                    "facing": 0x08,
                    "root": "wilson",
                    "frames": [
                        {
                            "bbox": (0.0, -450.0, 80.0, 120.0),
                            "events": [],
                            "elements": [
                                {
                                    "symbol": "torso",
                                    "frame_num": 0,
                                    "layer": "torso",
                                    "m": (1.0, 0.0, 0.0, 1.0, 0.0, -450.0),
                                    "z": 0.0,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        img, _ = skin_preview.render_anim_frame(
            anim_data=anim_data,
            base_build=base_build,
            clip_name="jump_high",
            facing_mask=0x08,
            canvas_size=(200, 220),
            scale=0.8,
            bg_rgba=(0, 0, 0, 0),
            auto_frame=True,
        )
        # With auto_frame=True, the red torso should be visible inside the canvas rather than clipped off top
        bbox = img.getchannel("R").getbbox()
        self.assertIsNotNone(bbox)
        self.assertGreater(bbox[1], 10)
        self.assertLess(bbox[3], 210)

    def test_slice_sheet_cells_preserves_aspect_ratio_across_different_sheet_ratios(self) -> None:
        # 3x1 template (1080x390) vs 16:9 AI output (1376x768) containing a 100x100 square sprite
        sheet = Image.new("RGBA", (1376, 768), (0, 255, 0, 255))
        # Black header strip at top of 16:9 image
        for y in range(40):
            for x in range(1376):
                sheet.putpixel((x, y), (20, 20, 20, 255))
        # Draw a perfect 120x120 red square inside cell 0 (x in 0..458)
        for y in range(300, 420):
            for x in range(160, 280):
                sheet.putpixel((x, y), (220, 50, 50, 255))
        layout = {
            "width": 1080,
            "height": 390,
            "cols": 3,
            "rows": 1,
            "header_h": 30,
            "cells": [
                {"symbol": "cheeks", "frame_idx": 0, "cell_scale": 2.0, "orig_tight_size": [60, 60]}
            ],
        }
        sliced = skin_pipeline._slice_sheet_cells(sheet, layout)
        clean, adj = sliced[("cheeks", 0)]
        bb = clean.getchannel("A").getbbox()
        self.assertIsNotNone(bb)
        w = bb[2] - bb[0]
        h = bb[3] - bb[1]
        # Must remain 1:1 square, not stretched horizontally to 1.54:1
        self.assertAlmostEqual(w / float(h), 1.0, delta=0.05)
        self.assertGreater(adj["cell_scale"], 2.0)

    def test_discover_generic_anim_zips_filters_character_and_mount(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for name in (
                "player_basic.zip",
                "player_actions_eat.zip",
                "player_idles_wendy.zip",
                "player_wolfgang.zip",
                "player_mount_actions.zip",
                "player_revive_fx.zip",
            ):
                (d / name).write_bytes(b"")
            no_mount = skin_preview.discover_generic_anim_zips(d, include_mount=False)
            self.assertEqual(no_mount, ["player_actions_eat.zip", "player_basic.zip"])
            with_mount = skin_preview.discover_generic_anim_zips(d, include_mount=True)
            self.assertEqual(with_mount, ["player_actions_eat.zip", "player_basic.zip", "player_mount_actions.zip"])

    def _write_synthetic_player_fixture(self, tmp: Path) -> tuple[Path, Path, Path]:
        atlas = Image.new("RGBA", (32, 32), (80, 140, 220, 255))
        tex_bytes = skin_pipeline.encode_ktex_rgba(atlas)
        syms = {
            "torso": [{"framenum": 0, "duration": 1, "cx": 0.0, "cy": 0.0, "w": 32.0, "h": 32.0, "atlas_rect": (0, 0, 32, 32)}],
            "long_hair_blank": [{"framenum": 0, "duration": 1, "cx": 0.0, "cy": 0.0, "w": 2.0, "h": 2.0, "atlas_rect": (0, 0, 2, 2)}],
        }
        base_zip = tmp / "wilson.zip"
        skin_zip = tmp / "skin_test.zip"
        for zp, bname in ((base_zip, "wilson"), (skin_zip, "skin_test")):
            with ZipFile(zp, "w") as zf:
                zf.writestr("build.bin", skin_pipeline.write_build_bin(bname, "atlas-0.tex", syms, 32, 32))
                zf.writestr("atlas-0.tex", tex_bytes)

        root_h = _klei_hash("wilson")
        sym_h = _klei_hash("torso")
        lay_h = _klei_hash("torso")
        clip_name = b"idle_loop"
        frame_bytes = b"".join([
            struct.pack("<4fII", 0.0, -50.0, 64.0, 64.0, 0, 1),
            struct.pack("<3I7f", sym_h, 0, lay_h, 1.0, 0.0, 0.0, 1.0, 0.0, -50.0, 0.0),
            struct.pack("<4fII", 0.0, -55.0, 64.0, 64.0, 0, 1),
            struct.pack("<3I7f", sym_h, 0, lay_h, 1.0, 0.0, 0.0, 1.0, 4.0, -55.0, 0.0),
        ])
        anim_bytes = b"".join([
            b"ANIM",
            struct.pack("<5I", 4, 2, 2, 0, 1),
            struct.pack("<I", len(clip_name)),
            clip_name,
            struct.pack("<BIfI", 0x08, root_h, 30.0, 2),
            frame_bytes,
            struct.pack("<I", 2),
            struct.pack("<II", root_h, 6), b"wilson",
            struct.pack("<II", sym_h, 5), b"torso",
        ])
        anim_zip = tmp / "player_idles.zip"
        with ZipFile(anim_zip, "w") as zf:
            zf.writestr("anim.bin", anim_bytes)
        return base_zip, skin_zip, anim_zip

    def test_build_player_html_generates_player_and_anims_js(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            base_zip, skin_zip, _ = self._write_synthetic_player_fixture(tmp)
            out_html = skin_player.build_player_html(
                skin_zip=skin_zip,
                out_html=tmp / "anim_player.html",
                base_zip=base_zip,
                ref_zip=base_zip,
                anim_dir=tmp,
                suite="core",
            )
            self.assertTrue(out_html.is_file())
            anims_js = tmp / "player_data" / "anims.js"
            self.assertTrue(anims_js.is_file())
            raw = anims_js.read_text(encoding="utf-8")
            payload = json.loads(raw.removeprefix("window.__DST_ANIM_PLAYER__ = ").rstrip(";\n"))
            self.assertIn("idle_loop", payload["clips"])
            self.assertEqual(len(payload["clips"]["idle_loop"]["views"]["front"]["f"]), 2)

    def test_render_clip_gif_writes_animated_gif(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            base_zip, skin_zip, anim_zip = self._write_synthetic_player_fixture(tmp)
            gif_path = skin_preview.render_clip_gif(
                anim_zip=anim_zip,
                base_zip=base_zip,
                skin_zip=skin_zip,
                out_gif=tmp / "out.gif",
                ref_zip=base_zip,
                clip_name="idle_loop",
                facing="front",
                pane_size=(80, 90),
            )
            self.assertTrue(gif_path.is_file())
            with Image.open(gif_path) as im:
                self.assertEqual(getattr(im, "n_frames", 1), 2)

    def test_is_clean_review_clip_filters_junk_and_transitions(self) -> None:
        for bad in (
            "x", "x1", "xxx", "xxx_2", "dd", "hc", "ref", "ref_idle", "temp_TA", "wilson",
            "emoteXL_pre_dance0", "emote_pst_sit2", "sit_lag_nofaced", "chop_pre",
        ):
            self.assertFalse(skin_preview.is_clean_review_clip(bad), f"Expected False for {bad!r}")
        for good in ("idle_loop", "build_loop", "acting_1", "bite_light_loop", "test_clip"):
            self.assertTrue(skin_preview.is_clean_review_clip(good), f"Expected True for {good!r}")

    def test_resolve_compact_specs_covers_slots_and_skips_junk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            atlas = Image.new("RGBA", (32, 32), (80, 140, 220, 255))
            skin_build = {
                "build_name": "skin_test",
                "symbols": {
                    "torso": {0: {"image": atlas, "cx": 0.0, "cy": 0.0, "w": 32.0, "h": 32.0, "framenum": 0, "duration": 1}},
                    "hand": {0: {"image": atlas, "cx": 0.0, "cy": 0.0, "w": 16.0, "h": 16.0, "framenum": 0, "duration": 1}},
                },
            }
            root_h = _klei_hash("wilson")
            torso_h = _klei_hash("torso")
            hand_h = _klei_hash("hand")

            def _write_anim_zip(zp: Path, clips: list[tuple[str, int, int]]) -> None:
                chunks = [b"ANIM", struct.pack("<5I", 4, len(clips), len(clips), 0, len(clips))]
                for cname, sym_h, fnum in clips:
                    cb = cname.encode("ascii")
                    chunks.extend([
                        struct.pack("<I", len(cb)),
                        cb,
                        struct.pack("<BIfI", 0x08, root_h, 30.0, 1),
                        struct.pack("<4fII", 0.0, -50.0, 64.0, 64.0, 0, 1),
                        struct.pack("<3I7f", sym_h, fnum, sym_h, 1.0, 0.0, 0.0, 1.0, 0.0, -50.0, 0.0),
                    ])
                chunks.extend([
                    struct.pack("<I", 3),
                    struct.pack("<II", root_h, 6), b"wilson",
                    struct.pack("<II", torso_h, 5), b"torso",
                    struct.pack("<II", hand_h, 4), b"hand",
                ])
                with ZipFile(zp, "w") as zf:
                    zf.writestr("anim.bin", b"".join(chunks))

            _write_anim_zip(tmp / "player_idles.zip", [("idle_loop", torso_h, 0)])
            _write_anim_zip(
                tmp / "player_extra.zip",
                [("xxx", hand_h, 0), ("foo_pre", hand_h, 0), ("clean_grab", hand_h, 0)],
            )
            specs = skin_preview.resolve_compact_specs(skin_build, anim_dir=tmp)
            clip_keys = [row[6] for row in specs]
            self.assertEqual(clip_keys, ["idle_loop", "clean_grab"])

    def test_presentation_lua_and_preview_symbol_lists_stay_in_sync(self) -> None:
        lua_path = PROJECT_ROOT / "mods" / "dst_buddy_agent" / "scripts" / "jev_companion_presentation.lua"
        lua_text = lua_path.read_text(encoding="utf-8")

        def _extract_lua_list(var_name: str) -> tuple[str, ...]:
            m = re.search(rf"local\s+{var_name}\s*=\s*\{{(.*?)\}}", lua_text, re.S)
            self.assertIsNotNone(m, f"Missing {var_name} in {lua_path}")
            return tuple(re.findall(r'"([^"]+)"', m.group(1)))

        self.assertEqual(_extract_lua_list("BODY_SYMBOLS"), skin_preview.COMPANION_BODY_SYMBOLS)
        self.assertEqual(_extract_lua_list("BLANK_SYMBOLS"), skin_preview.COMPANION_BLANK_SYMBOLS)
        self.assertIn("hairpigtails", skin_preview.COMPANION_BODY_SYMBOLS)
        self.assertIn("hair", skin_preview.COMPANION_BODY_SYMBOLS)
        self.assertIn("hair_hat", skin_preview.COMPANION_BODY_SYMBOLS)
        self.assertNotIn("hairpigtails", skin_preview.COMPANION_BLANK_SYMBOLS)
        for guard in ("ClearSymbolExchanges", "ShowSymbol", "HideSymbol"):
            self.assertIn(guard, lua_text)

    def test_classify_animation_tier_distinguishes_common_and_rare_clips(self) -> None:
        self.assertEqual(skin_preview.classify_animation_tier("player_idles.zip", "idle_loop")[0], "T0")
        self.assertEqual(skin_preview.classify_animation_tier("player_basic.zip", "run_loop")[0], "T0")
        self.assertEqual(skin_preview.classify_animation_tier("player_actions_eat.zip", "eat")[0], "T0")
        self.assertEqual(skin_preview.classify_animation_tier("player_actions_eat.zip", "refuseeat")[0], "T1")
        self.assertEqual(skin_preview.classify_animation_tier("player_shock.zip", "shock")[0], "T1")
        self.assertEqual(skin_preview.classify_animation_tier("player_knockedout.zip", "sleep_loop")[0], "T1")
        self.assertEqual(skin_preview.classify_animation_tier("player_knockedout.zip", "dozy")[0], "T2")
        self.assertEqual(skin_preview.classify_animation_tier("player_emotesxl.zip", "emoteXL_bonesaw")[0], "T2")
        self.assertEqual(skin_preview.classify_animation_tier("player_actions_golf.zip", "club_putt_charged")[0], "T3")

    def test_pivot_scaled_symbols_anchor_terminal_limbs_and_tuner_roundtrip(self) -> None:
        from PIL import Image
        from tools import skin_tuner

        cell = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        cell.paste(Image.new("RGBA", (40, 40), (255, 220, 200, 255)), (30, 30))
        meta = {"cell_scale": 2.0, "orig_tight_size": (20, 20)}
        # Hand sits below wrist (0,0): top edge at y=-2, bottom at y=28 -> orig_dst_cy = 13.0
        orig = {"cx": 2.0, "cy": 13.0, "w": 40.0, "h": 40.0, "tight_bbox": (10, 10, 30, 30)}
        _, cx_hand, cy_hand, w_hand, h_hand = skin_pipeline._align_sprite_to_vanilla(
            cell, meta, orig, "hand", 0, extra_offset=(0.0, 0.0, 0.5)
        )
        self.assertAlmostEqual(cx_hand, 1.0, places=4)
        self.assertAlmostEqual(cy_hand, 6.5, places=4)
        self.assertAlmostEqual(w_hand, 10.0, places=4)
        self.assertAlmostEqual(h_hand, 10.0, places=4)
        # Wrist (top edge = cy - h/2) stays locked at 1.5 (-2 * 0.5 + ... -> closer to 0)
        self.assertAlmostEqual(cy_hand - h_hand / 2.0, (13.0 - 10.0) * 0.5, places=4)

        # Non-terminal segment keeps bbox-center scaling
        _, cx_arm, cy_arm, w_arm, h_arm = skin_pipeline._align_sprite_to_vanilla(
            cell, meta, orig, "arm_lower", 0, extra_offset=(0.0, 0.0, 0.5)
        )
        self.assertAlmostEqual(cx_arm, 2.0, places=4)
        self.assertAlmostEqual(cy_arm, 13.0, places=4)
        self.assertAlmostEqual(w_arm, 10.0, places=4)
        self.assertAlmostEqual(h_arm, 10.0, places=4)

        # Undo baked offset math matches skin_tuner._HTML_TEMPLATE forward math
        baked_cx, baked_cy, s0, dx0, dy0 = cx_hand + 1.5, cy_hand - 2.0, 0.5, 1.5, -2.0
        unbaked_cx = (baked_cx - dx0) / s0
        unbaked_cy = (baked_cy - dy0) / s0
        self.assertAlmostEqual(unbaked_cx * s0 + dx0, baked_cx, places=6)
        self.assertAlmostEqual(unbaked_cy * s0 + dy0, baked_cy, places=6)
        self.assertIn("PIVOT_SCALED_SYMBOLS", skin_tuner._HTML_TEMPLATE)
        self.assertIn("highlightPivots", skin_tuner._HTML_TEMPLATE)

    def test_feather_top_alpha_ramps_root_to_zero_and_keeps_body_opaque(self) -> None:
        from PIL import Image

        img = Image.new("RGBA", (16, 40), (35, 80, 180, 255))
        feathered = skin_pipeline._feather_top_alpha(img, 0.25, smoothstep=True)
        # Top row smoothsteps to 0, mid-ramp rises strictly, body below 25% stays 255
        self.assertEqual(feathered.getpixel((8, 0))[3], 0)
        self.assertGreater(feathered.getpixel((8, 5))[3], 0)
        self.assertLess(feathered.getpixel((8, 5))[3], 255)
        self.assertEqual(feathered.getpixel((8, 12))[3], 255)
        self.assertEqual(feathered.getpixel((8, 39))[3], 255)

        linear = skin_pipeline._feather_top_alpha(img, 0.20, smoothstep=False)
        self.assertGreater(linear.getpixel((8, 0))[3], 0)
        self.assertLess(linear.getpixel((8, 0))[3], linear.getpixel((8, 4))[3])
        self.assertEqual(linear.getpixel((8, 10))[3], 255)


if __name__ == "__main__":
    unittest.main()
