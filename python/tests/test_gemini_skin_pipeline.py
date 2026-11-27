"""Unit tests for the standalone Gemini skin pipeline (zero coupling to skin_pipeline.py)."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = ROOT / "python" / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

import gemini_skin_pipeline as gp
import skin_preview


class GeminiSkinPipelineTests(unittest.TestCase):
    def test_split_references_and_six_templates_exist_with_expected_counts(self) -> None:
        ref_dir = ROOT / "skins" / "gemini" / "reference"
        work_dir = ROOT / "skins" / "gemini" / "work"
        for name in (
            "gemini_ref_normal.png",
            "gemini_ref_sd.png",
            "gemini_ref_expressions.png",
            "gemini_icon.png",
        ):
            self.assertTrue((ref_dir / name).is_file(), f"Missing reference {name}")

        layouts = json.loads((work_dir / "sheets_layout.json").read_text(encoding="utf-8"))
        expected_counts = {
            "sheet1_head_hair_tail": 7,
            "sheet2_faces_core": 12,
            "sheet3_body_skirt": 17,
            "sheet4_limbs": 25,
            "sheet5_hands": 14,
            "sheet6_xray": 17,
        }
        self.assertEqual(set(layouts.keys()), set(expected_counts.keys()))
        for sheet_id, count in expected_counts.items():
            self.assertTrue((work_dir / f"{sheet_id}.png").is_file(), f"Missing template {sheet_id}.png")
            self.assertEqual(len(layouts[sheet_id]["cells"]), count, f"Wrong cell count on {sheet_id}")

    def test_derive_and_pack_roundtrip_on_synthetic_sheets(self) -> None:
        src_work = ROOT / "skins" / "gemini" / "work"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            work.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_work / "manifest.json", work / "manifest.json")
            shutil.copy2(src_work / "sheets_layout.json", work / "sheets_layout.json")
            shutil.copytree(src_work / "sprites", work / "sprites")
            for tmpl, ai_name in (
                ("sheet1_head_hair_tail.png", "sheet1_ai.png"),
                ("sheet2_faces_core.png", "sheet2_ai.png"),
                ("sheet3_body_skirt.png", "sheet3_ai.png"),
                ("sheet4_limbs.png", "sheet4_ai.png"),
                ("sheet5_hands.png", "sheet5_ai.png"),
                ("sheet6_xray.png", "sheet6_ai.png"),
            ):
                shutil.copy2(src_work / tmpl, work / ai_name)

            derived_img, derived_lay = gp.derive_sprites(work)
            self.assertTrue(derived_img.is_file())
            self.assertTrue(derived_lay.is_file())

            out_zip = Path(tmp) / "skin_gemini.zip"
            packed = gp.pack_skin(work, out_zip, "skin_gemini", icon_path=gp.DEFAULT_ICON_PATH)
            self.assertTrue(packed.is_file())

            with zipfile.ZipFile(packed) as zf:
                atlas = gp.decode_ktex(zf.read("atlas-0.tex"))
                parsed = gp.parse_build_bin(zf.read("build.bin"))

            self.assertEqual(parsed["build_name"], "skin_gemini")
            self.assertEqual(atlas.size, (2048, 2048))
            syms = parsed["symbols"]
            for req in (*skin_preview.COMPANION_BODY_SYMBOLS, "long_hair_blank", "SWAP_ICON"):
                self.assertIn(req, syms, f"Missing symbol {req} in packed build.bin")
                self.assertGreater(len(syms[req]), 0)

            def atlas_rect(sym: str, fnum: int) -> tuple[float, float, float, float]:
                for fr in syms[sym]:
                    if int(fr["framenum"]) == fnum:
                        quad = parsed["verts"][fr["vstart"]:fr["vstart"] + 6]
                        us = [round(v[3] * atlas.width) for v in quad]
                        vs = [round((1.0 - v[4]) * atlas.height) for v in quad]
                        return (min(us), min(vs), max(us), max(vs))
                raise AssertionError(f"Missing {sym}:{fnum}")

            def bounds(sym: str, fnum: int) -> tuple[float, float, float, float]:
                for fr in syms[sym]:
                    if int(fr["framenum"]) == fnum:
                        return (float(fr["cx"]), float(fr["cy"]), float(fr["w"]), float(fr["h"]))
                raise AssertionError(f"Missing {sym}:{fnum}")

            # 1. Exact pixel-reuse pairs share identical atlas tile rects
            self.assertEqual(atlas_rect("arm_lower", 6), atlas_rect("arm_lower", 2))
            self.assertEqual(atlas_rect("hand", 14), atlas_rect("hand", 4))
            self.assertEqual(atlas_rect("hand", 16), atlas_rect("hand", 8))
            self.assertEqual(atlas_rect("hand", 17), atlas_rect("hand", 13))
            self.assertEqual(atlas_rect("leg", 5), atlas_rect("leg", 2))
            self.assertEqual(atlas_rect("face", 8), atlas_rect("face", 0))
            self.assertEqual(atlas_rect("face", 24), atlas_rect("face", 16))

            # 2. Hybrid long hair + cat tail routing
            blank_rect = atlas_rect("long_hair_blank", 0)
            for idx in (0, 1, 3):
                self.assertEqual(atlas_rect("hair", idx), blank_rect)
                self.assertEqual(atlas_rect("hair_hat", idx), blank_rect)
            self.assertNotEqual(atlas_rect("hair", 2), blank_rect)
            self.assertEqual(atlas_rect("hair_hat", 2), atlas_rect("hair", 2))
            self.assertNotEqual(atlas_rect("tail", 0), blank_rect)
            self.assertNotEqual(atlas_rect("tail", 6), blank_rect)
            self.assertEqual(atlas_rect("tail", 1), blank_rect)

            # 3. Locked-eye derived face alignment (identical top edge & horizontal width for equal-width variants)
            f0 = bounds("face", 0)
            for idx in (9, 10, 16, 19):
                fi = bounds("face", idx)
                self.assertAlmostEqual(fi[0], f0[0], delta=0.5)
                self.assertAlmostEqual(fi[1] - fi[3] / 2.0, f0[1] - f0[3] / 2.0, delta=0.5)
            f20 = bounds("face", 20)
            f22 = bounds("face", 22)
            self.assertAlmostEqual(f20[0], f22[0], delta=0.25)
            self.assertAlmostEqual(f20[1] - f20[3] / 2.0, f22[1] - f22[3] / 2.0, delta=0.25)

    def test_presentation_lua_registers_gemini_skin(self) -> None:
        pres = (ROOT / "mods" / "dst_buddy_agent" / "scripts" / "jev_companion_presentation.lua").read_text(encoding="utf-8")
        self.assertIn('gemini="skin_gemini"', pres)
        self.assertIn('return "gemini"', pres)
        modmain = (ROOT / "mods" / "dst_buddy_agent" / "modmain.lua").read_text(encoding="utf-8")
        self.assertRegex(modmain, r'(?m)^\s*Asset\("ANIM",\s*"anim/skin_gemini\.zip"\)')

    def test_packed_gemini_zip_decodes_and_preserves_invariants(self) -> None:
        zip_path = ROOT / "mods" / "dst_buddy_agent" / "anim" / "skin_gemini.zip"
        self.assertTrue(zip_path.is_file(), f"Missing packed archive: {zip_path}")
        with zipfile.ZipFile(zip_path) as zf:
            atlas = gp.decode_ktex(zf.read("atlas-0.tex"))
            parsed = gp.parse_build_bin(zf.read("build.bin"))
        self.assertEqual(parsed["build_name"], "skin_gemini")
        self.assertEqual(atlas.size, (2048, 2048))
        syms = parsed["symbols"]
        for req in (*skin_preview.COMPANION_BODY_SYMBOLS, "long_hair_blank", "SWAP_ICON"):
            self.assertIn(req, syms, f"Missing symbol {req} in skin_gemini.zip")

        def atlas_rect(sym: str, fnum: int) -> tuple[float, float, float, float]:
            for fr in syms[sym]:
                if int(fr["framenum"]) == fnum:
                    quad = parsed["verts"][fr["vstart"]:fr["vstart"] + 6]
                    us = [round(v[3] * atlas.width) for v in quad]
                    vs = [round((1.0 - v[4]) * atlas.height) for v in quad]
                    return (min(us), min(vs), max(us), max(vs))
            raise AssertionError(f"Missing {sym}:{fnum}")

        def bounds(sym: str, fnum: int) -> tuple[float, float, float, float]:
            for fr in syms[sym]:
                if int(fr["framenum"]) == fnum:
                    return (float(fr["cx"]), float(fr["cy"]), float(fr["w"]), float(fr["h"]))
            raise AssertionError(f"Missing {sym}:{fnum}")

        # Exact reuse invariants
        self.assertEqual(atlas_rect("arm_lower", 6), atlas_rect("arm_lower", 2))
        self.assertEqual(atlas_rect("hand", 14), atlas_rect("hand", 4))
        self.assertEqual(atlas_rect("hand", 16), atlas_rect("hand", 8))
        self.assertEqual(atlas_rect("hand", 17), atlas_rect("hand", 13))
        self.assertEqual(atlas_rect("leg", 5), atlas_rect("leg", 2))
        self.assertEqual(atlas_rect("face", 8), atlas_rect("face", 0))
        self.assertEqual(atlas_rect("face", 24), atlas_rect("face", 16))

        # Hybrid long hair + cat tail routing
        blank_rect = atlas_rect("long_hair_blank", 0)
        for idx in (0, 1, 3):
            self.assertEqual(atlas_rect("hair", idx), blank_rect)
            self.assertEqual(atlas_rect("hair_hat", idx), blank_rect)
        self.assertEqual(atlas_rect("hair_hat", 2), atlas_rect("hair", 2))
        self.assertNotEqual(atlas_rect("hair", 2), blank_rect)
        self.assertNotEqual(atlas_rect("tail", 0), blank_rect)
        self.assertNotEqual(atlas_rect("tail", 6), blank_rect)
        self.assertEqual(atlas_rect("tail", 1), blank_rect)
        _, _, t0_w, t0_h = bounds("tail", 0)
        _, _, t6_w, t6_h = bounds("tail", 6)
        self.assertGreater(t0_w, t0_h, "tail:0 should be oriented horizontally to sweep backward")
        self.assertGreater(t6_w, t6_h, "tail:6 should be oriented horizontally to sweep backward")

        # Single-sided hairpigtails:0/1 locks vs wide unified hair:2 curtain
        _, _, hp0_w, hp0_h = bounds("hairpigtails", 0)
        _, _, hp1_w, hp1_h = bounds("hairpigtails", 1)
        _, _, h2_w, _ = bounds("hair", 2)
        self.assertLess(hp0_w, hp0_h * 0.95)
        self.assertLess(hp1_w, hp1_h * 0.95)
        self.assertGreater(h2_w, hp0_w * 1.15)

        # Locked-eye top & X alignment across derived expression groups
        for base_idx, derived_indices in ((0, (9, 10, 16, 19, 28, 30)), (11, (17, 18)), (25, (26, 27, 29))):
            bx, by, _, bh = bounds("face", base_idx)
            b_top = by - bh / 2.0
            for idx in derived_indices:
                fx, fy, _, fh = bounds("face", idx)
                self.assertAlmostEqual(fx, bx, delta=0.5, msg=f"face:{idx} cx drifted from face:{base_idx}")
                self.assertAlmostEqual(fy - fh / 2.0, b_top, delta=0.5, msg=f"face:{idx} top drifted from face:{base_idx}")
        f20 = bounds("face", 20)
        f22 = bounds("face", 22)
        self.assertAlmostEqual(f20[0], f22[0], delta=0.25)
        self.assertAlmostEqual(f20[1] - f20[3] / 2.0, f22[1] - f22[3] / 2.0, delta=0.25)


if __name__ == "__main__":
    unittest.main()
