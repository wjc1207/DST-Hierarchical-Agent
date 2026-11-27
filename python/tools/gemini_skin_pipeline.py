"""Standalone Gemini character skin asset pipeline for DST (zero coupling to deepseek skin_pipeline.py)."""
from __future__ import annotations

import argparse
from collections import deque
import colorsys
import io
import json
import math
from pathlib import Path
import re
import struct
import zipfile

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VANILLA_ZIP = Path(
    "C:/Program Files (x86)/Steam/steamapps/common/Don't Starve Together/data/anim/wendy.zip"
)
DEFAULT_SRC_ASSET = ROOT / "assets" / "gemini-1.png"
DEFAULT_SRC_PORTRAIT = ROOT / "assets" / "gemini-2.jpg"
DEFAULT_REF_DIR = ROOT / "skins" / "gemini" / "reference"
DEFAULT_WORK_DIR = ROOT / "skins" / "gemini" / "work"
DEFAULT_OUT_ZIP = ROOT / "mods" / "dst_buddy_agent" / "anim" / "skin_gemini.zip"
DEFAULT_BUILD_NAME = "skin_gemini"
DEFAULT_ICON_PATH = DEFAULT_REF_DIR / "gemini_icon.png"

# Verified palette from assets/gemini-1.png (3200x2100)
PALETTE = {
    "hair_blue": (108, 120, 217),       # #6C78D9
    "hair_purple": (154, 114, 226),     # #9A72E2
    "hair_pink": (216, 156, 241),       # #D89CF1
    "navy": (41, 37, 78),               # #29254E
    "eye_gold": (217, 168, 74),         # #D9A84A
    "eye_magenta": (195, 74, 147),      # #C34A93
    "skin": (252, 238, 231),            # #FCEEE7
    "skin_shadow": (241, 210, 202),     # soft warm blush shadow
    "blush_pink": (246, 174, 194),      # soft cheek blush
    "skin_line": (74, 51, 72),          # #4A3348
    "mouth_dark": (84, 44, 70),
    "mouth_inner": (176, 86, 122),
    "tongue_pink": (242, 152, 172),
}
TARGET_SKIN_RGB = PALETTE["skin"]

# 12 core eye-distinct face expressions drawn via AI in sheet2_faces_core;
# 13 same-eye variants are Pillow-derived via `derive`; remaining same-view slots copy verbatim.
GEMINI_CORE_FACES: tuple[int, ...] = (0, 2, 4, 6, 11, 12, 13, 15, 21, 23, 25, 31)
GEMINI_FACE_MAP: dict[int, int] = {
    0: 0, 1: 0, 2: 2, 3: 0,
    4: 4, 5: 4, 6: 6, 7: 4,
    8: 0, 9: 0, 10: 0,
    11: 11, 12: 12, 13: 13,
    14: 14,
    15: 15, 16: 0, 17: 11, 18: 11, 19: 0,
    20: 0, 21: 21, 22: 20, 23: 23, 24: 16,
    25: 25, 26: 25, 27: 25, 28: 0, 29: 25, 30: 28, 31: 31, 32: 25,
}

# Exact same-pixel substitutions to eliminate redundant drawing
PIXEL_REUSE_MAP: dict[tuple[str, int], tuple[str, int]] = {
    ("arm_lower", 6): ("arm_lower", 2),
    ("hand", 14): ("hand", 4),
    ("hand", 16): ("hand", 8),
    ("hand", 17): ("hand", 13),
    ("leg", 5): ("leg", 2),
}

PIVOT_SCALED_SYMBOLS = frozenset({"hand", "foot"})

REWORK_CELL_PASS_CRITERIA: dict[str, dict[str, object]] = {
    "correct_part": {"pass": True, "description": "部件类别与槽位正确"},
    "view_and_pose_match": {"pass": True, "description": "朝向与表情/姿势核心特征与模板一致"},
    "art_style_matches_reference": {"pass": True, "description": "符合 Gemini 设定图二次元赛璐璐画风与异色瞳"},
    "contains_adjacent_parts": {"pass": False, "description": "是否混入相邻部件或多余肤色头底/背景"},
    "outline_within_cell": {"pass": True, "description": "外轮廓完全在纯绿格内且未裁切"},
    "background_drawn_as_part": {"pass": False, "description": "绿底是否被误画成零件的一部分"},
    "edge_artifacts": {"pass": False, "description": "边缘是否有杂绿、像素锯齿、黑块或碎色"},
}

SHEET_FILENAMES: dict[str, str] = {
    "sheet1_head_hair_tail": "sheet1_ai.png",
    "sheet2_faces_core": "sheet2_ai.png",
    "sheet3_body_skirt": "sheet3_ai.png",
    "sheet4_limbs": "sheet4_ai.png",
    "sheet5_hands": "sheet5_ai.png",
    "sheet6_xray": "sheet6_ai.png",
}


def klei_hash(name: str) -> int:
    """Compute Klei lowercase sdbm hash used in build.bin symbol tables."""
    value = 0
    for byte in name.lower().encode("ascii"):
        value = (byte + (value << 6) + (value << 16) - value) & 0xFFFFFFFF
    return value


def decode_ktex(raw: bytes) -> Image.Image:
    """Decode a Klei KTEX image (DXT1/3/5 or RGBA8888) into upright un-premultiplied RGBA."""
    if len(raw) < 12 or raw[:4] != b"KTEX":
        raise ValueError("Invalid KTEX header")
    header = struct.unpack_from("<I", raw, 4)[0]
    compression = (header >> 4) & 31
    mips = (header >> 13) & 31
    if mips < 1:
        raise ValueError("KTEX contains no mip levels")
    width, height, _, length = struct.unpack_from("<HHHI", raw, 8)
    offset = 8 + mips * 10
    if len(raw) < offset + length:
        raise ValueError("Truncated KTEX pixel payload")
    payload = raw[offset:offset + length]
    if compression == 4:
        expected = width * height * 4
        if len(payload) < expected:
            raise ValueError("Truncated RGBA KTEX payload")
        upright = Image.frombytes("RGBa", (width, height), payload[:expected]).transpose(
            Image.Transpose.FLIP_TOP_BOTTOM
        )
        return upright.convert("RGBA")
    if compression in (0, 1, 2):
        fourcc = (b"DXT1", b"DXT3", b"DXT5")[compression]
        dds = (
            b"DDS "
            + struct.pack("<7I", 124, 0x81007, height, width, length, 0, 1)
            + bytes(44)
            + struct.pack("<II4sI4I", 32, 4, fourcc, 0, 0, 0, 0, 0)
            + struct.pack("<5I", 4096, 0, 0, 0, 0)
        )
        with Image.open(io.BytesIO(dds + payload)) as decoded:
            raw_img = decoded.convert("RGBA").transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        return Image.frombytes("RGBa", raw_img.size, raw_img.tobytes()).convert("RGBA")
    raise ValueError(f"Unsupported KTEX compression format: {compression}")


def encode_ktex_rgba(img: Image.Image) -> bytes:
    """Encode an upright RGBA Pillow image as lossless single-mip premultiplied KTEX (fmt=4)."""
    rgba = img.convert("RGBA")
    width, height = rgba.size
    flipped = rgba.convert("RGBa").transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    payload = flipped.tobytes()
    header = (4 << 4) | (1 << 9) | (1 << 13) | (3 << 18) | (0xFFF << 20)
    metadata = struct.pack("<HHHI", width, height, width * 4, len(payload))
    return b"KTEX" + struct.pack("<I", header) + metadata + payload


def parse_build_bin(data: bytes) -> dict:
    """Parse Klei build.bin (v6) into build metadata, symbols, frames, and vertex buffer."""
    if len(data) < 16 or data[:4] != b"BILD":
        raise ValueError("Invalid build.bin header")
    version, num_symbols, num_frames = struct.unpack_from("<III", data, 4)
    if version != 6:
        raise ValueError(f"Unsupported build.bin version {version}; expected 6")
    offset = 16
    name_len, = struct.unpack_from("<I", data, offset)
    offset += 4
    build_name = data[offset:offset + name_len].decode("utf-8", "replace")
    offset += name_len

    num_atlases, = struct.unpack_from("<I", data, offset)
    offset += 4
    atlases: list[str] = []
    for _ in range(num_atlases):
        alen, = struct.unpack_from("<I", data, offset)
        offset += 4
        atlases.append(data[offset:offset + alen].decode("utf-8", "replace"))
        offset += alen

    raw_symbols: list[tuple[int, list[dict]]] = []
    for _ in range(num_symbols):
        sym_hash, sym_nf = struct.unpack_from("<II", data, offset)
        offset += 8
        frames = []
        for _ in range(sym_nf):
            framenum, duration, cx, cy, w, h, vstart, vcount = struct.unpack_from("<IIffffII", data, offset)
            offset += 32
            frames.append({
                "framenum": framenum,
                "duration": duration,
                "cx": cx,
                "cy": cy,
                "w": w,
                "h": h,
                "vstart": vstart,
                "vcount": vcount,
            })
        raw_symbols.append((sym_hash, frames))

    num_verts, = struct.unpack_from("<I", data, offset)
    offset += 4
    verts = [struct.unpack_from("<6f", data, offset + idx * 24) for idx in range(num_verts)]
    offset += num_verts * 24

    num_strings, = struct.unpack_from("<I", data, offset)
    offset += 4
    hash_names: dict[int, str] = {}
    for _ in range(num_strings):
        shash, slen = struct.unpack_from("<II", data, offset)
        offset += 8
        hash_names[shash] = data[offset:offset + slen].decode("utf-8", "replace")
        offset += slen

    symbols: dict[str, list[dict]] = {}
    for shash, frames in raw_symbols:
        name = hash_names.get(shash, f"0x{shash:08x}")
        symbols[name] = frames

    return {
        "version": version,
        "num_frames": num_frames,
        "build_name": build_name,
        "atlases": atlases,
        "symbols": symbols,
        "verts": verts,
    }


def write_build_bin(
    build_name: str,
    atlas_name: str,
    symbols: dict[str, list[dict]],
    atlas_w: int,
    atlas_h: int,
) -> bytes:
    """Serialize symbols/frames/atlas rects into Klei build.bin v6 bytes."""
    sorted_names = sorted(symbols.keys(), key=klei_hash)
    total_frames = sum(len(symbols[name]) for name in sorted_names)

    out = bytearray()
    out += b"BILD"
    out += struct.pack("<III", 6, len(sorted_names), total_frames)

    bname_bytes = build_name.encode("utf-8")
    out += struct.pack("<I", len(bname_bytes)) + bname_bytes

    aname_bytes = atlas_name.encode("utf-8")
    out += struct.pack("<II", 1, len(aname_bytes)) + aname_bytes

    verts: list[tuple[float, float, float, float, float, float]] = []
    for name in sorted_names:
        frames = symbols[name]
        out += struct.pack("<II", klei_hash(name), len(frames))
        for frame in frames:
            vstart = len(verts)
            cx = float(frame["cx"])
            cy = float(frame["cy"])
            w = float(frame["w"])
            h = float(frame["h"])
            ax0, ay0, ax1, ay1 = frame["atlas_rect"]

            x0 = cx - w / 2.0
            x1 = cx + w / 2.0
            y0 = cy - h / 2.0
            y1 = cy + h / 2.0
            u0 = ax0 / atlas_w
            u1 = ax1 / atlas_w
            v_top = 1.0 - (ay0 / atlas_h)
            v_bot = 1.0 - (ay1 / atlas_h)

            verts.extend([
                (x0, y0, 0.0, u0, v_top, 0.0),
                (x1, y0, 0.0, u1, v_top, 0.0),
                (x1, y1, 0.0, u1, v_bot, 0.0),
                (x0, y0, 0.0, u0, v_top, 0.0),
                (x1, y1, 0.0, u1, v_bot, 0.0),
                (x0, y1, 0.0, u0, v_bot, 0.0),
            ])
            out += struct.pack(
                "<IIffffII",
                int(frame["framenum"]),
                int(frame["duration"]),
                cx,
                cy,
                w,
                h,
                vstart,
                6,
            )

    out += struct.pack("<I", len(verts))
    for v in verts:
        out += struct.pack("<6f", *v)

    out += struct.pack("<I", len(sorted_names))
    for name in sorted_names:
        nbytes = name.encode("utf-8")
        out += struct.pack("<II", klei_hash(name), len(nbytes)) + nbytes

    return bytes(out)


def reconstruct_frame_sprite(
    atlas: Image.Image,
    frame: dict,
    verts: list[tuple[float, float, float, float, float, float]],
) -> Image.Image:
    """Reconstruct an upright RGBA sprite image from its build.bin quad list."""
    atlas_w, atlas_h = atlas.size
    fw = max(1, round(frame["w"]))
    fh = max(1, round(frame["h"]))
    x0 = frame["cx"] - frame["w"] / 2.0
    y0 = frame["cy"] - frame["h"] / 2.0
    canvas = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
    vstart, vcount = frame["vstart"], frame["vcount"]
    for offset in range(0, vcount, 6):
        quad = verts[vstart + offset:vstart + offset + 6]
        if len(quad) < 6:
            continue
        xs = [v[0] for v in quad]
        ys = [v[1] for v in quad]
        us = [v[3] * atlas_w for v in quad]
        vs = [(1.0 - v[4]) * atlas_h for v in quad]

        px0 = max(0, round(min(xs) - x0))
        px1 = min(fw, round(max(xs) - x0))
        py0 = max(0, round(min(ys) - y0))
        py1 = min(fh, round(max(ys) - y0))
        u0 = max(0, round(min(us)))
        u1 = min(atlas_w, round(max(us)))
        v0 = max(0, round(min(vs)))
        v1 = min(atlas_h, round(max(vs)))
        if px1 <= px0 or py1 <= py0 or u1 <= u0 or v1 <= v0:
            continue
        patch = atlas.crop((u0, v0, u1, v1))
        target_size = (px1 - px0, py1 - py0)
        if patch.size != target_size:
            patch = patch.resize(target_size, Image.Resampling.BILINEAR)
        canvas.paste(patch, (px0, py0))
    return canvas


def _load_font(size: int = 13) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _tight_bbox(img: Image.Image) -> tuple[int, int, int, int]:
    bbox = img.getchannel("A").getbbox()
    if bbox is None:
        return (0, 0, img.width, img.height)
    return bbox


def split_reference_sheet(
    src_sheet: Path = DEFAULT_SRC_ASSET,
    src_portrait: Path = DEFAULT_SRC_PORTRAIT,
    out_dir: Path = DEFAULT_REF_DIR,
) -> dict[str, Path]:
    """Crop assets/gemini-1.png into clean unlabelled reference sheets + SWAP_ICON."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(src_sheet) as raw:
        rgb = raw.convert("RGB")

    # Verified tight crop regions on 3200x2100 gemini-1.png (zero text labels inside boxes)
    crops = {
        "normal": ((140, 345, 1924, 1330), out_dir / "gemini_ref_normal.png"),
        "sd": ((2055, 285, 3145, 922), out_dir / "gemini_ref_sd.png"),
        "expressions": ((36, 1580, 1684, 1898), out_dir / "gemini_ref_expressions.png"),
    }
    outputs: dict[str, Path] = {}
    for key, (box, path) in crops.items():
        pad = Image.new("RGB", (box[2] - box[0] + 24, box[3] - box[1] + 24), (255, 255, 255))
        pad.paste(rgb.crop(box), (12, 12))
        pad.save(path)
        outputs[key] = path

    # Build clean circular/transparent bust SWAP_ICON from portrait or SD front view
    icon_out = out_dir / "gemini_icon.png"
    if src_portrait.is_file():
        with Image.open(src_portrait) as pr:
            pr_rgb = pr.convert("RGBA")
        # Upper character bust/head crop on 3072x5504 portrait
        pw, ph = pr_rgb.size
        crop_box = (int(pw * 0.14), int(ph * 0.02), int(pw * 0.86), int(ph * 0.41))
        bust = pr_rgb.crop(crop_box)
        # Edge-seed white background removal so SWAP_ICON blends cleanly into DST slot
        w, h = bust.size
        bpx = bust.load()
        visited = bytearray(w * h)
        q: deque[tuple[int, int]] = deque()

        def is_near_white(r: int, g: int, b: int) -> bool:
            return r >= 238 and g >= 238 and b >= 238 and (max(r, g, b) - min(r, g, b)) <= 22

        for x in range(w):
            for y in (0, h - 1):
                r, g, b, _ = bpx[x, y]
                if is_near_white(r, g, b):
                    visited[y * w + x] = 1
                    q.append((x, y))
        for y in range(h):
            for x in (0, w - 1):
                idx = y * w + x
                if not visited[idx]:
                    r, g, b, _ = bpx[x, y]
                    if is_near_white(r, g, b):
                        visited[idx] = 1
                        q.append((x, y))
        while q:
            cx, cy = q.popleft()
            bpx[cx, cy] = (0, 0, 0, 0)
            for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                if 0 <= nx < w and 0 <= ny < h:
                    idx = ny * w + nx
                    if not visited[idx]:
                        r, g, b, _ = bpx[nx, ny]
                        if is_near_white(r, g, b):
                            visited[idx] = 1
                            q.append((nx, ny))
        bbox = bust.getchannel("A").getbbox()
        if bbox:
            bust = bust.crop(bbox)
        icon = ImageOps.contain(bust, (224, 224), Image.Resampling.LANCZOS)
        square = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
        square.alpha_composite(icon, ((240 - icon.width) // 2, (240 - icon.height) // 2))
        square.save(icon_out)
        outputs["icon"] = icon_out
    return outputs


def _build_hair_guide(variant: str, *, xray: bool = False) -> Image.Image:
    """Build a unified wavy long-hair curtain silhouette guide (never split into pigtails)."""
    s = 2
    img = Image.new("RGBA", (200 * s, 200 * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = (18, 18, 22, 255) if xray else (158, 166, 184, 255)
    outline = (0, 0, 0, 255) if xray else (58, 66, 86, 255)
    if variant == "front":
        pts = [
            (54 * s, 14 * s), (146 * s, 14 * s),
            (172 * s, 72 * s), (186 * s, 128 * s), (168 * s, 174 * s),
            (134 * s, 184 * s), (100 * s, 176 * s), (66 * s, 184 * s), (32 * s, 174 * s),
            (14 * s, 128 * s), (28 * s, 72 * s),
        ]
    elif variant == "side":
        pts = [
            (92 * s, 16 * s), (166 * s, 22 * s), (174 * s, 68 * s),
            (150 * s, 120 * s), (108 * s, 162 * s), (56 * s, 184 * s),
            (24 * s, 170 * s), (30 * s, 124 * s), (60 * s, 78 * s),
        ]
    else:  # unified back sheet
        pts = [
            (48 * s, 14 * s), (152 * s, 14 * s),
            (178 * s, 70 * s), (188 * s, 134 * s), (164 * s, 180 * s),
            (126 * s, 188 * s), (100 * s, 180 * s), (74 * s, 188 * s), (36 * s, 180 * s),
            (12 * s, 134 * s), (22 * s, 70 * s),
        ]
    draw.polygon(pts, fill=fill)
    draw.line(pts + [pts[0]], fill=outline, width=3 * s, joint="curve")
    if xray:
        for dx in (-38, -14, 14, 38) if variant != "side" else (-18, 8, 30):
            if variant == "side":
                draw.line([(125 * s + dx, 42 * s), (78 * s + dx, 120 * s), (48 * s + dx, 164 * s)], fill=(255, 255, 255, 255), width=4 * s)
            else:
                draw.line([(100 * s + dx, 36 * s), (100 * s + dx * 1.25, 110 * s), (100 * s + dx * 1.05, 168 * s)], fill=(255, 255, 255, 255), width=4 * s)
    return img.resize((200, 200), Image.Resampling.LANCZOS)


def _build_tail_guide(*, xray: bool = False) -> Image.Image:
    """Build a fluffy curved cat-tail guide (thick base top-right -> upturned tip bottom-left)."""
    s = 2
    img = Image.new("RGBA", (180 * s, 180 * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fill = (18, 18, 22, 255) if xray else (158, 166, 184, 255)
    outline = (0, 0, 0, 255) if xray else (58, 66, 86, 255)
    pts = [
        (140 * s, 20 * s), (162 * s, 32 * s), (156 * s, 64 * s),
        (128 * s, 112 * s), (86 * s, 148 * s), (42 * s, 162 * s),
        (18 * s, 148 * s), (20 * s, 122 * s), (44 * s, 112 * s),
        (76 * s, 108 * s), (104 * s, 82 * s), (124 * s, 44 * s),
    ]
    draw.polygon(pts, fill=fill)
    draw.line(pts + [pts[0]], fill=outline, width=3 * s, joint="curve")
    if xray:
        spine = [(144 * s, 36 * s), (124 * s, 76 * s), (94 * s, 116 * s), (58 * s, 140 * s), (32 * s, 138 * s)]
        draw.line(spine, fill=(255, 255, 255, 255), width=4 * s)
    return img.resize((180, 180), Image.Resampling.LANCZOS)


def _compose_sheet(
    sheet_id: str,
    entries: list[dict],
    cols: int,
    cell_content_w: int,
    cell_content_h: int,
    *,
    border: int = 3,
    label_h: int = 26,
    scale_override: float | None = None,
) -> tuple[Image.Image, dict]:
    """Compose a pure #00FF00 labeled grid sheet with solid black unused cells."""
    rows = (len(entries) + cols - 1) // cols
    cell_w = cell_content_w + border * 2
    cell_h = cell_content_h + label_h + border * 2
    sheet_w = cols * cell_w
    sheet_h = rows * cell_h
    canvas = Image.new("RGBA", (sheet_w, sheet_h), (0, 0, 0, 255))
    draw = ImageDraw.Draw(canvas)
    font = _load_font(14)

    cells_meta: list[dict] = []
    for idx, entry in enumerate(entries):
        row = idx // cols
        col = idx % cols
        cx0 = col * cell_w
        cy0 = row * cell_h
        draw.rectangle([cx0 + border, cy0 + border, cx0 + cell_w - border - 1, cy0 + border + label_h - 1], fill=(20, 20, 20, 255))
        draw.text((cx0 + border + 6, cy0 + border + 4), entry["label"], fill=(255, 255, 255, 255), font=font)

        gx0 = cx0 + border
        gy0 = cy0 + border + label_h
        gx1 = cx0 + cell_w - border
        gy1 = cy0 + cell_h - border
        draw.rectangle([gx0, gy0, gx1 - 1, gy1 - 1], fill=(0, 255, 0, 255))

        sprite: Image.Image = entry["sprite"]
        tx0, ty0, tx1, ty1 = entry["tight_bbox"]
        tight = sprite.crop((tx0, ty0, tx1, ty1))
        tw, th = max(1, tight.width), max(1, tight.height)

        margin_factor = entry.get("fill_ratio", 0.72)
        max_w = cell_content_w * margin_factor
        max_h = cell_content_h * margin_factor
        scale = scale_override if scale_override is not None else entry.get("scale", min(max_w / tw, max_h / th))
        scale = min(scale, max_w / tw, max_h / th)
        dw = max(1, round(tw * scale))
        dh = max(1, round(th * scale))
        resized = tight.resize((dw, dh), Image.Resampling.LANCZOS)

        sym = entry["symbol"]
        if sym in {"headbase", "headbase_hat"}:
            rx0 = gx0 + (cell_content_w - dw) // 2
            ry0 = gy0 + round(cell_content_h * 0.84) - dh
        elif sym in {"torso_pelvis", "skirt", "hairpigtails"}:
            rx0 = gx0 + (cell_content_w - dw) // 2
            ry0 = gy0 + round(cell_content_h * 0.14)
        else:
            rx0 = gx0 + (cell_content_w - dw) // 2
            ry0 = gy0 + (cell_content_h - dh) // 2

        canvas.alpha_composite(resized, (rx0, ry0))
        orig_tw, orig_th = entry.get("orig_tight_size", (tw, th))
        cells_meta.append({
            "label": entry["label"],
            "symbol": sym,
            "frame_idx": entry["frame_idx"],
            "content_rect": [gx0, gy0, gx1, gy1],
            "ref_sprite_rect": [rx0, ry0, rx0 + dw, ry0 + dh],
            "cell_scale": scale,
            "orig_tight_size": [orig_tw, orig_th],
        })

    layout = {
        "sheet_id": sheet_id,
        "width": sheet_w,
        "height": sheet_h,
        "cols": cols,
        "rows": rows,
        "border": border,
        "label_h": label_h,
        "cells": cells_meta,
    }
    return canvas, layout


def extract_vanilla(
    vanilla_zip: Path = DEFAULT_VANILLA_ZIP,
    out_dir: Path = DEFAULT_WORK_DIR,
) -> Path:
    """Unpack vanilla wendy.zip sprites + manifest and compose Gemini's 6 specialized green-screen sheets."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sprites_dir = out_dir / "sprites"
    sprites_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(vanilla_zip) as zf:
        atlas = decode_ktex(zf.read("atlas-0.tex"))
        parsed = parse_build_bin(zf.read("build.bin"))

    symbols = parsed["symbols"]
    verts = parsed["verts"]
    extracted_sprites: dict[tuple[str, int], Image.Image] = {}
    manifest_symbols: dict[str, list[dict]] = {}

    for sym_name, frames in symbols.items():
        rows = []
        for idx, frame in enumerate(frames):
            sprite = reconstruct_frame_sprite(atlas, frame, verts)
            extracted_sprites[(sym_name, idx)] = sprite
            sprite.save(sprites_dir / f"{sym_name}_{idx:02d}.png")
            tx0, ty0, tx1, ty1 = _tight_bbox(sprite)
            rows.append({
                "framenum": frame["framenum"],
                "duration": frame["duration"],
                "cx": frame["cx"],
                "cy": frame["cy"],
                "w": frame["w"],
                "h": frame["h"],
                "tight_bbox": [tx0, ty0, tx1, ty1],
                "orig_size": [sprite.width, sprite.height],
            })
        manifest_symbols[sym_name] = rows

    manifest = {
        "source": str(vanilla_zip),
        "build_name": parsed["build_name"],
        "key_faces": list(GEMINI_CORE_FACES),
        "symbols": manifest_symbols,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def vanilla_entry(sym: str, idx: int, fill_ratio: float = 0.72) -> dict:
        meta = manifest_symbols[sym][idx]
        return {
            "label": f"{sym}:{idx}",
            "symbol": sym,
            "frame_idx": idx,
            "sprite": extracted_sprites[(sym, idx)],
            "tight_bbox": tuple(meta["tight_bbox"]),
            "fill_ratio": fill_ratio,
        }

    def guide_entry(sym: str, idx: int, guide_img: Image.Image, dst_ref_size: tuple[int, int], fill_ratio: float = 0.76) -> dict:
        tb = _tight_bbox(guide_img)
        return {
            "label": f"{sym}:{idx}",
            "symbol": sym,
            "frame_idx": idx,
            "sprite": guide_img,
            "tight_bbox": tb,
            "orig_tight_size": dst_ref_size,
            "fill_ratio": fill_ratio,
        }

    hp0_tb = manifest_symbols["hairpigtails"][0]["tight_bbox"]
    hp1_tb = manifest_symbols["hairpigtails"][1]["tight_bbox"]
    hp2_tb = manifest_symbols["hairpigtails"][2]["tight_bbox"]

    sheet1_entries = [
        vanilla_entry("headbase", 0, 0.72),
        vanilla_entry("headbase", 1, 0.72),
        vanilla_entry("headbase", 2, 0.72),
        guide_entry("tail", 0, _build_tail_guide(xray=False), (165, 165), 0.74),
        guide_entry("hairpigtails", 0, _build_hair_guide("front", xray=False), (hp0_tb[2] - hp0_tb[0] + 40, hp0_tb[3] - hp0_tb[1] + 20), 0.76),
        guide_entry("hairpigtails", 1, _build_hair_guide("side", xray=False), (hp1_tb[2] - hp1_tb[0] + 35, hp1_tb[3] - hp1_tb[1] + 20), 0.76),
        guide_entry("hairpigtails", 2, _build_hair_guide("back", xray=False), (hp2_tb[2] - hp2_tb[0] + 45, hp2_tb[3] - hp2_tb[1] + 25), 0.76),
    ]
    sheet2_entries = [vanilla_entry("face", idx, 0.68) for idx in GEMINI_CORE_FACES]
    sheet3_entries = (
        [vanilla_entry("torso", idx, 0.72) for idx in range(10)]
        + [vanilla_entry("torso_pelvis", idx, 0.72) for idx in (0, 3, 6, 9)]
        + [vanilla_entry("skirt", idx, 0.74) for idx in (0, 1, 2)]
    )
    sheet4_entries = (
        [vanilla_entry("arm_upper", idx, 0.72) for idx in range(5)]
        + [vanilla_entry("arm_upper_skin", idx, 0.72) for idx in (0, 1)]
        + [vanilla_entry("arm_lower", idx, 0.72) for idx in (0, 1, 2, 5)]
        + [vanilla_entry("leg", idx, 0.72) for idx in (0, 1, 2, 3, 4, 6, 7, 8)]
        + [vanilla_entry("foot", idx, 0.72) for idx in (0, 1, 3, 4, 5, 6)]
    )
    sheet5_entries = [vanilla_entry("hand", idx, 0.72) for idx in (*range(11), 13, 18, 19)]
    sheet6_entries = [
        vanilla_entry("headbase", 3, 0.72),
        guide_entry("hairpigtails", 3, _build_hair_guide("front", xray=True), (hp0_tb[2] - hp0_tb[0] + 40, hp0_tb[3] - hp0_tb[1] + 20), 0.76),
        guide_entry("hairpigtails", 4, _build_hair_guide("side", xray=True), (hp1_tb[2] - hp1_tb[0] + 35, hp1_tb[3] - hp1_tb[1] + 20), 0.76),
        guide_entry("tail", 6, _build_tail_guide(xray=True), (165, 165), 0.74),
        vanilla_entry("torso", 10, 0.72),
        vanilla_entry("torso_pelvis", 10, 0.72),
        vanilla_entry("skirt", 4, 0.74),
        vanilla_entry("arm_upper", 5, 0.72),
        vanilla_entry("arm_upper", 6, 0.72),
        vanilla_entry("arm_upper_skin", 2, 0.72),
        vanilla_entry("arm_lower", 3, 0.72),
        vanilla_entry("arm_lower", 4, 0.72),
        vanilla_entry("hand", 11, 0.72),
        vanilla_entry("hand", 12, 0.72),
        vanilla_entry("leg", 9, 0.72),
        vanilla_entry("leg", 10, 0.72),
        vanilla_entry("foot", 7, 0.72),
    ]

    composed = [
        ("sheet1_head_hair_tail", sheet1_entries, 4, 420, 420),
        ("sheet2_faces_core", sheet2_entries, 4, 340, 340),
        ("sheet3_body_skirt", sheet3_entries, 5, 290, 290),
        ("sheet4_limbs", sheet4_entries, 5, 250, 250),
        ("sheet5_hands", sheet5_entries, 4, 320, 320),
        ("sheet6_xray", sheet6_entries, 5, 290, 290),
    ]
    layouts: dict[str, dict] = {}
    for sheet_id, entries, cols, cw, ch in composed:
        img, lay = _compose_sheet(sheet_id, entries, cols=cols, cell_content_w=cw, cell_content_h=ch)
        img.save(out_dir / f"{sheet_id}.png")
        layouts[sheet_id] = lay

    (out_dir / "sheets_layout.json").write_text(json.dumps(layouts, indent=2), encoding="utf-8")
    return out_dir


def is_chroma_green(r: int, g: int, b: int, *, strict: bool = False) -> bool:
    """Detect green-screen pixels while strictly protecting blue/purple/pink/gold hues."""
    green_excess = g - max(r, b)
    if strict:
        if green_excess < 48 or g < 110:
            return False
    else:
        if green_excess < 20 or g < 75:
            return False
    hue, sat, val = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    deg = hue * 360.0
    return (80.0 <= deg <= 155.0) and sat >= 0.30 and val >= 0.28


def remove_green_screen(img: Image.Image) -> Image.Image:
    """Remove #00FF00 green screen using border-seeded flood-fill + interior strict key + despill."""
    rgba = img.convert("RGBA")
    w, h = rgba.size
    pixels = rgba.load()
    removed = bytearray(w * h)
    queue: deque[tuple[int, int]] = deque()

    def try_seed(x: int, y: int) -> None:
        idx = y * w + x
        if removed[idx]:
            return
        r, g, b, a = pixels[x, y]
        if a == 0 or is_chroma_green(r, g, b, strict=False):
            removed[idx] = 1
            queue.append((x, y))

    for x in range(w):
        try_seed(x, 0)
        try_seed(x, h - 1)
    for y in range(h):
        try_seed(0, y)
        try_seed(w - 1, y)

    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h:
                idx = ny * w + nx
                if not removed[idx]:
                    r, g, b, a = pixels[nx, ny]
                    if a == 0 or is_chroma_green(r, g, b, strict=False):
                        removed[idx] = 1
                        queue.append((nx, ny))

    for y in range(h):
        for x in range(w):
            idx = y * w + x
            if not removed[idx]:
                r, g, b, a = pixels[x, y]
                if a > 0 and is_chroma_green(r, g, b, strict=True):
                    removed[idx] = 1

    out_pixels = rgba.load()
    dist1 = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            idx = y * w + x
            if removed[idx]:
                out_pixels[x, y] = (0, 0, 0, 0)
                continue
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < w and 0 <= ny < h and removed[ny * w + nx]:
                    dist1[idx] = 1
                    break

    for y in range(h):
        for x in range(w):
            idx = y * w + x
            if removed[idx]:
                continue
            near = dist1[idx]
            if not near:
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if 0 <= nx < w and 0 <= ny < h and dist1[ny * w + nx]:
                        near = 2
                        break
            if not near:
                continue
            r, g, b, a = out_pixels[x, y]
            if g > max(r, b) + 4:
                hue, sat, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                deg = hue * 360.0
                if 68.0 <= deg <= 162.0 and sat >= 0.14:
                    excess = g - max(r, b)
                    new_g = max(r, b)
                    new_a = max(0, a - min(a, int(excess * 2.2))) if near == 1 else a
                    out_pixels[x, y] = (r, new_g, b, new_a)

    return rgba


def recolor_skin_sprite(sprite: Image.Image, target_rgb: tuple[int, int, int] = TARGET_SKIN_RGB) -> Image.Image:
    """Shift vanilla skin pixels toward Gemini fair warm peach tone while preserving ink lines."""
    rgba = sprite.convert("RGBA")
    px = rgba.load()
    tr, tg, tb = target_rgb
    for y in range(rgba.height):
        for x in range(rgba.width):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
            if lum < 0.38:
                px[x, y] = (min(255, int(r * 0.92)), min(255, int(g * 0.86)), min(255, int(b * 0.96)), a)
            else:
                blend = 0.76
                nr = int(r * (1 - blend) + tr * lum * 1.12 * blend)
                ng = int(g * (1 - blend) + tg * lum * 1.12 * blend)
                nb = int(b * (1 - blend) + tb * lum * 1.12 * blend)
                px[x, y] = (max(0, min(255, nr)), max(0, min(255, ng)), max(0, min(255, nb)), a)
    return rgba


def _parse_offset_entry(raw: object) -> tuple[float, float, float]:
    """Parse an offsets.json entry ([dx, dy], [dx, dy, scale], or {dx, dy, scale})."""
    if isinstance(raw, dict):
        return (
            float(raw.get("dx", 0.0)),
            float(raw.get("dy", 0.0)),
            max(0.1, float(raw.get("scale", 1.0))),
        )
    if isinstance(raw, (list, tuple)):
        dx = float(raw[0]) if len(raw) >= 1 else 0.0
        dy = float(raw[1]) if len(raw) >= 2 else 0.0
        sc = max(0.1, float(raw[2])) if len(raw) >= 3 else 1.0
        return (dx, dy, sc)
    return (0.0, 0.0, 1.0)


def _feather_top_alpha(img: Image.Image, frac: float, *, smoothstep: bool = True) -> Image.Image:
    """Fade top fraction of an RGBA sprite's alpha so hair roots blend smoothly into headbase."""
    out = img.copy()
    if out.height <= 8 or frac <= 0.0:
        return out
    px = out.load()
    ramp_h = max(1, int(round(out.height * frac)))
    for yy in range(ramp_h):
        if smoothstep:
            t = float(yy) / float(ramp_h)
            factor = t * t * (3.0 - 2.0 * t)
        else:
            factor = float(yy + 1) / float(ramp_h + 1)
        for xx in range(out.width):
            r, g, b, a = px[xx, yy]
            if a > 0:
                px[xx, yy] = (r, g, b, max(0, min(255, int(round(a * factor)))))
    return out


def _detect_cell_green_box(sheet_img: Image.Image, row: int, col: int, rows: int, cols: int) -> tuple[int, int, int, int]:
    """Locate the pure-green interior rect inside cell (row, col), excluding grid borders and label bar."""
    cw = sheet_img.width / cols
    ch = sheet_img.height / rows
    x0 = int(round(col * cw))
    x1 = int(round((col + 1) * cw))
    y0 = int(round(row * ch))
    y1 = int(round((row + 1) * ch))
    px = sheet_img.load()

    def is_bg_green(x: int, y: int) -> bool:
        r, g, b, a = px[x, y]
        return a > 0 and g > max(r, b) + 45

    cell_w = max(1, x1 - x0)
    cell_h = max(1, y1 - y0)
    hdr_y = min(y1 - 1, y0 + max(4, int(cell_h * 0.06)))
    bot_y = max(y0, y1 - 2)
    probe_xs = [min(x1 - 1, x0 + max(4, int(cell_w * f))) for f in (0.06, 0.50, 0.94)]

    top_candidates = []
    bot_candidates = []
    for mx in probe_xs:
        for yy in range(hdr_y, y1):
            if is_bg_green(mx, yy):
                top_candidates.append(yy)
                break
        for yy in range(bot_y, hdr_y, -1):
            if is_bg_green(mx, yy):
                bot_candidates.append(yy + 1)
                break

    gy0 = (min(top_candidates) + 2) if top_candidates else (y0 + max(18, int(cell_h * 0.16)))
    gy1 = (max(bot_candidates) - 2) if bot_candidates else (y1 - 4)

    probe_ys = [min(gy1 - 1, gy0 + max(4, int((gy1 - gy0) * f))) for f in (0.12, 0.88)]
    left_x = min(x1 - 1, x0 + 2)
    right_x = max(x0, x1 - 2)
    left_candidates = []
    right_candidates = []
    for my in probe_ys:
        for xx in range(left_x, x1):
            if is_bg_green(xx, my):
                left_candidates.append(xx)
                break
        for xx in range(right_x, left_x, -1):
            if is_bg_green(xx, my):
                right_candidates.append(xx + 1)
                break

    gx0 = (min(left_candidates) + 2) if left_candidates else (x0 + 4)
    gx1 = (max(right_candidates) - 2) if right_candidates else (x1 - 4)
    if gx1 <= gx0 + 8 or gy1 <= gy0 + 8:
        return (x0 + 5, y0 + max(18, int(cell_h * 0.16)), max(x0 + 13, x1 - 5), max(y0 + 26, y1 - 5))
    return (gx0, gy0, gx1, gy1)


def _slice_sheet_cells(
    sheet_img: Image.Image,
    layout: dict,
) -> dict[tuple[str, int], tuple[Image.Image, dict]]:
    """Slice a green-screen sheet without non-uniform stretching even if AI aspect ratio differs from template."""
    cols = max(1, int(layout["cols"]))
    rows = max(1, int(layout["rows"]))
    tmpl_cell_w = float(layout["width"]) / cols
    tmpl_cell_h = float(layout["height"]) / rows
    header_h = float(layout.get("header_h", layout.get("label_h", 26)))
    tmpl_green_w = max(1.0, tmpl_cell_w - 6.0)
    tmpl_green_h = max(1.0, tmpl_cell_h - header_h - 6.0)

    out: dict[tuple[str, int], tuple[Image.Image, dict]] = {}
    for idx, cell in enumerate(layout["cells"]):
        if cell.get("skip"):
            continue
        row = idx // cols
        col = idx % cols
        crop_box = _detect_cell_green_box(sheet_img, row, col, rows, cols)
        actual_gw = max(1.0, float(crop_box[2] - crop_box[0]))
        actual_gh = max(1.0, float(crop_box[3] - crop_box[1]))
        uniform_factor = min(actual_gw / tmpl_green_w, actual_gh / tmpl_green_h)
        clean = remove_green_screen(sheet_img.crop(crop_box))
        adj_cell = dict(cell)
        adj_cell["cell_scale"] = float(cell["cell_scale"]) * max(1e-3, uniform_factor)
        if layout.get("sheet_id") == "sheet4_limbs" and cell["symbol"] == "arm_lower" and int(cell["frame_idx"]) == 2:
            clean = ImageOps.mirror(clean)
        if cell["symbol"] == "face" and layout.get("sheet_id") != "rework_derived":
            clean = _unmix_face_blush(clean)
            clean = _fill_enclosed_eye_pinholes(clean)
        out[(cell["symbol"], int(cell["frame_idx"]))] = (clean, adj_cell)
    return out


def _fill_enclosed_eye_pinholes(rgba: Image.Image, max_area: int = 16) -> Image.Image:
    """Fill tiny enclosed transparent pinholes inside bright eye iris glints with warm white."""
    out = rgba.copy()
    w, h = out.size
    px = out.load()
    seen = bytearray(w * h)
    # Flood-fill border-reachable transparent pixels first
    q: deque[tuple[int, int]] = deque()
    for x in range(w):
        for y in (0, h - 1):
            idx = y * w + x
            if not seen[idx] and px[x, y][3] <= 24:
                seen[idx] = 1
                q.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            idx = y * w + x
            if not seen[idx] and px[x, y][3] <= 24:
                seen[idx] = 1
                q.append((x, y))
    while q:
        cx, cy = q.popleft()
        for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
            if 0 <= nx < w and 0 <= ny < h:
                nidx = ny * w + nx
                if not seen[nidx] and px[nx, ny][3] <= 24:
                    seen[nidx] = 1
                    q.append((nx, ny))

    for y in range(1, h - 1):
        for x in range(1, w - 1):
            idx = y * w + x
            if seen[idx] or px[x, y][3] > 24:
                continue
            comp: list[tuple[int, int]] = []
            seen[idx] = 1
            cq: deque[tuple[int, int]] = deque([(x, y)])
            while cq:
                cx, cy = cq.popleft()
                comp.append((cx, cy))
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if 0 <= nx < w and 0 <= ny < h:
                        nidx = ny * w + nx
                        if not seen[nidx] and px[nx, ny][3] <= 24:
                            seen[nidx] = 1
                            cq.append((nx, ny))
            if len(comp) <= max_area:
                lums = []
                for cx, cy in comp:
                    for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                        if 0 <= nx < w and 0 <= ny < h and px[nx, ny][3] > 180:
                            r, g, b, _ = px[nx, ny]
                            lums.append((0.299 * r + 0.587 * g + 0.114 * b) / 255.0)
                if lums and (sum(lums) / len(lums)) >= 0.58:
                    for cx, cy in comp:
                        px[cx, cy] = (255, 252, 245, 255)
    return out


def _unmix_face_blush(rgba: Image.Image) -> Image.Image:
    """Remove olive/green-screen bleed from soft semi-transparent cheek blush on face decals."""
    out = rgba.copy()
    tb = _tight_bbox(out)
    th = max(1, tb[3] - tb[1])
    px = out.load()

    def is_ink(r: int, g: int, b: int, a: int) -> bool:
        if a < 120:
            return False
        lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
        return lum < 0.52

    for x in range(tb[0], tb[2]):
        dark_ys = [y for y in range(tb[1], tb[3]) if is_ink(*px[x, y])]
        for y in range(tb[1], tb[3]):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            # Protect amber-gold iris core (high R, much lower B: b/r < 0.56 and R - G >= 26)
            if r > 0 and (b / float(r)) < 0.56 and (r - g) >= 26:
                continue
            # Protect any pale cream/white highlight enclosed inside an eye (ink both above and closely below)
            if any(0 < (y - dy) <= 95 for dy in dark_ys) and any(0 < (dy - y) <= 22 for dy in dark_ys):
                continue
            ry = (y - tb[1]) / float(th)
            # Detect blush/anti-aliased pixels contaminated by #00FF00 background green
            if g > b + 12 and g >= r - 48 and ry >= 0.42:
                if b / float(max(1, r)) >= 0.56 or g >= r - 14:
                    bg_frac = max(0.0, min(0.95, (g - 0.88 * b) / 255.0))
                    fg_a = max(0.08, 1.0 - bg_frac)
                    nr = max(0, min(255, int(round(r / fg_a))))
                    nb = max(0, min(255, int(round(b / fg_a))))
                    ng = max(0, min(nr, int(round(nb * 0.84))))
                    # Attenuate faint low-red olive bridge across nose on distressed faces cleanly
                    red_sig = max(0.0, min(1.0, (nr - nb) / 55.0))
                    atten = (fg_a ** 1.25) * (0.45 + 0.55 * red_sig)
                    na = max(0, min(255, int(round(a * atten))))
                    px[x, y] = (nr, ng, nb, na) if na > 2 else (0, 0, 0, 0)
    return out


def _align_sprite_to_vanilla(
    clean_cell_sprite: Image.Image,
    cell_meta: dict,
    orig_frame: dict,
    symbol: str,
    frame_idx: int,
    extra_offset: tuple[float, float] | tuple[float, float, float] = (0.0, 0.0, 1.0),
) -> tuple[Image.Image, float, float, float, float]:
    """Crop tight AI sprite, compute DST world size/center, and align pivot cleanly to vanilla joints."""
    bbox = clean_cell_sprite.getchannel("A").getbbox()
    if bbox is None:
        return Image.new("RGBA", (4, 4), (0, 0, 0, 0)), orig_frame["cx"], orig_frame["cy"], 4.0, 4.0

    cropped = clean_cell_sprite.crop(bbox)
    gw_px, gh_px = cropped.size

    scale = max(1e-3, float(cell_meta["cell_scale"]))
    orig_tw, orig_th = cell_meta["orig_tight_size"]
    tx0, ty0, tx1, ty1 = orig_frame["tight_bbox"]
    orig_fw, orig_fh = float(orig_frame["w"]), float(orig_frame["h"])
    orig_dst_cx = float(orig_frame["cx"]) - orig_fw / 2.0 + (tx0 + tx1) / 2.0
    orig_dst_cy = float(orig_frame["cy"]) - orig_fh / 2.0 + (ty0 + ty1) / 2.0
    orig_dst_top = float(orig_frame["cy"]) - orig_fh / 2.0 + ty0
    orig_dst_bot = float(orig_frame["cy"]) - orig_fh / 2.0 + ty1

    dst_w = gw_px / scale
    dst_h = gh_px / scale

    ratio_w = dst_w / max(1.0, orig_tw)
    ratio_h = dst_h / max(1.0, orig_th)
    max_ratio = 1.45 if symbol in {"headbase", "headbase_hat", "hairpigtails", "skirt"} else 1.22
    min_ratio = 0.80
    clamp_s = 1.0
    peak = max(ratio_w, ratio_h)
    if peak > max_ratio:
        clamp_s = max_ratio / peak
    elif min(ratio_w, ratio_h) < min_ratio:
        clamp_s = min_ratio / max(1e-3, min(ratio_w, ratio_h))
    dst_w *= clamp_s
    dst_h *= clamp_s

    dx_off, dy_off, extra_scale = _parse_offset_entry(extra_offset)
    if symbol in {"headbase", "headbase_hat"}:
        new_cx = orig_dst_cx + dx_off
        new_cy = (orig_dst_bot - dst_h / 2.0) + dy_off
    elif symbol in {"hairpigtails", "torso_pelvis", "skirt"}:
        new_cx = orig_dst_cx + dx_off
        new_cy = (orig_dst_top + dst_h / 2.0) + dy_off
    elif symbol in PIVOT_SCALED_SYMBOLS:
        new_cx = orig_dst_cx * extra_scale + dx_off
        new_cy = orig_dst_cy * extra_scale + dy_off
    else:
        new_cx = orig_dst_cx + dx_off
        new_cy = orig_dst_cy + dy_off

    dst_w *= extra_scale
    dst_h *= extra_scale
    return cropped, new_cx, new_cy, dst_w, dst_h


def _shelf_pack_tiles(
    tiles: dict[str, Image.Image],
    atlas_size: int = 2048,
    padding: int = 4,
    supersample: float = 1.0,
    tile_dst_sizes: dict[str, tuple[float, float]] | None = None,
) -> tuple[Image.Image, dict[str, tuple[int, int, int, int]]] | None:
    """Shelf-pack tiles onto a single square RGBA atlas; returns None if atlas overflows."""
    ordered = []
    for key, img in tiles.items():
        if tile_dst_sizes and key in tile_dst_sizes:
            dw, dh = tile_dst_sizes[key]
            target_w = max(2, round(dw * supersample))
            target_h = max(2, round(dh * supersample))
            pw = min(img.width, target_w)
            ph = min(img.height, target_h)
            resized = img if img.size == (pw, ph) else img.resize((pw, ph), Image.Resampling.LANCZOS)
        else:
            resized = img
        ordered.append((key, resized))

    ordered.sort(key=lambda item: (-item[1].height, -item[1].width, item[0]))
    atlas = Image.new("RGBA", (atlas_size, atlas_size), (0, 0, 0, 0))
    rects: dict[str, tuple[int, int, int, int]] = {}
    x = padding
    y = padding
    row_h = 0

    for key, patch in ordered:
        pw, ph = patch.size
        if pw + padding * 2 > atlas_size or ph + padding * 2 > atlas_size:
            return None
        if x + pw + padding > atlas_size:
            x = padding
            y += row_h + padding
            row_h = 0
        if y + ph + padding > atlas_size:
            return None
        atlas.paste(patch, (x, y))
        rects[key] = (x, y, x + pw, y + ph)
        x += pw + padding
        row_h = max(row_h, ph)

    return atlas, rects


def _skin_root_from_work(work_dir: Path) -> Path:
    return work_dir.parent if work_dir.name == "work" else (ROOT / "skins" / work_dir.name)


def _rework_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"rework(\d+)", path.stem, re.IGNORECASE)
    return (int(match.group(1)) if match else 0, path.name)


def _find_rework_ai(work_dir: Path, stem: str) -> Path | None:
    """Resolve an AI rework image in ``work_dir``, preferring ``.png`` then ``.jpg``."""
    for ext in (".png", ".jpg"):
        cand = work_dir / f"{stem}_ai{ext}"
        if cand.is_file():
            return cand
    return None


def _load_source_cells(work_dir: Path, *, include_reworks: bool = True, exclude_derived: bool = False) -> dict[tuple[str, int], tuple[Image.Image, dict]]:
    """Slice Gemini base sheets (+ optional reworks) into clean RGBA cells."""
    layouts = json.loads((work_dir / "sheets_layout.json").read_text(encoding="utf-8"))
    sliced_clean: dict[tuple[str, int], tuple[Image.Image, dict]] = {}
    for sheet_id, layout in layouts.items():
        fname = SHEET_FILENAMES.get(sheet_id)
        if not fname:
            continue
        ai_path = work_dir / fname
        if not ai_path.is_file():
            raise FileNotFoundError(f"Missing AI sheet: {ai_path}")
        with Image.open(ai_path) as raw:
            sliced_clean.update(_slice_sheet_cells(raw.convert("RGBA"), layout))

    if include_reworks:
        rework_items: list[tuple[Path, Path]] = []
        derived_lp = work_dir / "rework_derived_layout.json"
        derived_ai = work_dir / "rework_derived_ai.png"
        if not exclude_derived and derived_lp.is_file() and derived_ai.is_file():
            rework_items.append((derived_lp, derived_ai))
        for lp in sorted(work_dir.glob("rework*_layout.json"), key=_rework_sort_key):
            if lp.name == "rework_derived_layout.json":
                continue
            stem = lp.name[: -len("_layout.json")]
            ai_path = _find_rework_ai(work_dir, stem)
            if ai_path is not None:
                rework_items.append((lp, ai_path))
        for lp, ai_path in rework_items:
            rlayout = json.loads(lp.read_text(encoding="utf-8"))
            with Image.open(ai_path) as raw_rw:
                sliced_clean.update(_slice_sheet_cells(raw_rw.convert("RGBA"), rlayout))
    return sliced_clean


def _connected_components(rgba: Image.Image, alpha_thresh: int = 24) -> list[dict]:
    """Find 8-connected alpha components on an RGBA image."""
    w, h = rgba.size
    apx = rgba.getchannel("A").load()
    seen = bytearray(w * h)
    comps: list[dict] = []
    for y in range(h):
        for x in range(w):
            idx = y * w + x
            if seen[idx] or apx[x, y] <= alpha_thresh:
                continue
            q = deque([(x, y)])
            seen[idx] = 1
            pts: list[tuple[int, int]] = []
            x0, y0, x1, y1 = x, y, x + 1, y + 1
            sx, sy = 0.0, 0.0
            while q:
                cx, cy = q.popleft()
                pts.append((cx, cy))
                sx += cx
                sy += cy
                if cx < x0:
                    x0 = cx
                if cx + 1 > x1:
                    x1 = cx + 1
                if cy < y0:
                    y0 = cy
                if cy + 1 > y1:
                    y1 = cy + 1
                for dy in (-1, 0, 1):
                    ny = cy + dy
                    if 0 <= ny < h:
                        for dx in (-1, 0, 1):
                            nx = cx + dx
                            if 0 <= nx < w:
                                nidx = ny * w + nx
                                if not seen[nidx] and apx[nx, ny] > alpha_thresh:
                                    seen[nidx] = 1
                                    q.append((nx, ny))
            n = max(1, len(pts))
            comps.append({
                "pts": pts,
                "area": len(pts),
                "bbox": (x0, y0, x1, y1),
                "cx": sx / n,
                "cy": sy / n,
            })
    return comps


def _extract_locked_eyes_face(face_rgba: Image.Image) -> tuple[Image.Image, tuple[float, float, int]]:
    """Strip ONLY the lower-center mouth components from a base face RGBA, returning (eyes_only, mouth_anchor)."""
    tb = _tight_bbox(face_rgba)
    tight = face_rgba.crop(tb).copy()
    w, h = tight.size
    comps = _connected_components(tight, alpha_thresh=20)
    # Locate central tiny nose tick to separate upper eyes/nose from lower mouth cleanly
    nose_cands = [
        c for c in comps
        if abs(c["cx"] - w * 0.5) <= w * 0.06
        and c["area"] <= max(140, int(w * h * 0.004))
        and 0.35 * h <= c["cy"] <= 0.82 * h
    ]
    nose_bot = max((c["bbox"][3] for c in nose_cands), default=int(round(h * 0.58)))

    # Collect all central mouth components below the nose (handles split curve smiles and wide shouts)
    mouth_candidates = [
        c for c in comps
        if abs(c["cx"] - w * 0.5) <= w * 0.24
        and c["cy"] > max(nose_bot + 2, h * 0.62)
        and c["bbox"][1] >= nose_bot - 4
        and (c["bbox"][2] - c["bbox"][0]) <= w * 0.78
    ]
    if not mouth_candidates:
        cut_y = max(nose_bot + 6, int(round(h * 0.78)))
        px = tight.load()
        for yy in range(cut_y, h):
            for xx in range(int(w * 0.28), int(w * 0.72)):
                px[xx, yy] = (0, 0, 0, 0)
        return tight, (w * 0.5, float(cut_y + 4), max(14, int(w * 0.16)))

    px = tight.load()
    mx0 = min(c["bbox"][0] for c in mouth_candidates)
    my0 = min(c["bbox"][1] for c in mouth_candidates)
    mx1 = max(c["bbox"][2] for c in mouth_candidates)
    my1 = max(c["bbox"][3] for c in mouth_candidates)
    for comp in mouth_candidates:
        for mx, my in comp["pts"]:
            px[mx, my] = (0, 0, 0, 0)
    for yy in range(max(0, my0 - 2), min(h, my1 + 2)):
        for xx in range(max(0, mx0 - 2), min(w, mx1 + 2)):
            if px[xx, yy][3] < 110:
                px[xx, yy] = (0, 0, 0, 0)
    anchor_x = (mx0 + mx1) * 0.5
    anchor_y = max(float(nose_bot + 10), float(my0))
    ref_mw = max(14, min(mx1 - mx0, int(w * 0.34)))
    return tight, (anchor_x, anchor_y, ref_mw)


def _locate_eye_components(eyes_rgba: Image.Image) -> tuple[list[dict], list[dict]]:
    """Split upper eye+brow components into viewer-left and viewer-right clusters."""
    w, h = eyes_rgba.size
    comps = _connected_components(eyes_rgba, alpha_thresh=24)
    major = [c for c in comps if c["area"] >= max(12, (w * h) * 0.0025) and c["cy"] < h * 0.78]
    left = [c for c in major if c["cx"] < w * 0.48]
    right = [c for c in major if c["cx"] > w * 0.52]
    return left, right


def _draw_mouth_overlay(base_img: Image.Image, anchor: tuple[float, float, int], style: str) -> Image.Image:
    """Composite a 4x-supersampled crisp vector anime mouth onto a locked-eye face base."""
    ax, ay, ref_mw = anchor
    w, h = base_img.size
    extra_bot = max(12, int(round(h * 0.18)))
    s = 4
    layer = Image.new("RGBA", (w * s, (h + extra_bot) * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx = ax * s
    cy = ay * s
    unit = max(14.0, float(ref_mw)) * s * 0.55
    line_c = PALETTE["mouth_dark"] + (255,)
    fill_c = PALETTE["mouth_inner"] + (255,)
    tongue_c = PALETTE["tongue_pink"] + (255,)
    tooth_c = (255, 255, 255, 255)
    sw = max(3, int(round(unit * 0.14)))

    if style == "talk_half":
        rx, ry = unit * 0.62, unit * 0.42
        draw.ellipse([cx - rx, cy, cx + rx, cy + ry * 2.0], fill=fill_c, outline=line_c, width=sw)
        draw.chord([cx - rx * 0.68, cy + ry * 0.9, cx + rx * 0.68, cy + ry * 2.05], 180, 360, fill=tongue_c)
        draw.ellipse([cx - rx, cy, cx + rx, cy + ry * 2.0], outline=line_c, width=sw)
    elif style == "talk_open":
        rx, ry = unit * 0.78, unit * 0.66
        draw.ellipse([cx - rx, cy - unit * 0.06, cx + rx, cy + ry * 1.9], fill=fill_c, outline=line_c, width=sw)
        draw.chord([cx - rx * 0.72, cy + ry * 0.72, cx + rx * 0.72, cy + ry * 1.95], 180, 360, fill=tongue_c)
        draw.ellipse([cx - rx, cy - unit * 0.06, cx + rx, cy + ry * 1.9], outline=line_c, width=sw)
    elif style == "worried_o":
        rx, ry = unit * 0.52, unit * 0.46
        draw.ellipse([cx - rx, cy + unit * 0.04, cx + rx, cy + unit * 0.04 + ry * 1.8], fill=fill_c, outline=line_c, width=sw)
    elif style == "frown":
        rx = unit * 0.68
        draw.arc([cx - rx, cy + unit * 0.06, cx + rx, cy + unit * 1.05], 200, 340, fill=line_c, width=sw + 1)
    elif style == "kiss_o":
        rx = unit * 0.34
        draw.ellipse([cx - rx, cy + unit * 0.06, cx + rx, cy + unit * 0.06 + rx * 1.9], fill=fill_c, outline=line_c, width=sw)
    elif style == "pout":
        pts = [(cx - unit * 0.68, cy + unit * 0.25), (cx - unit * 0.15, cy + unit * 0.08), (cx + unit * 0.65, cy + unit * 0.22)]
        draw.line(pts, fill=line_c, width=sw + 1, joint="curve")
    elif style == "sigh_open":
        rx, ry = unit * 0.46, unit * 0.34
        pts = [
            (cx - rx, cy + ry * 0.9),
            (cx - rx * 0.5, cy + unit * 0.02),
            (cx + rx * 0.5, cy + unit * 0.02),
            (cx + rx, cy + ry * 0.9),
            (cx + rx * 0.6, cy + ry * 1.7),
            (cx - rx * 0.6, cy + ry * 1.7),
        ]
        draw.polygon(pts, fill=fill_c)
        draw.line(pts + [pts[0]], fill=line_c, width=sw, joint="curve")
    elif style == "smirk":
        pts = [(cx - unit * 0.72, cy + unit * 0.18), (cx - unit * 0.12, cy + unit * 0.34), (cx + unit * 0.68, cy + unit * 0.04)]
        draw.line(pts, fill=line_c, width=sw + 1, joint="curve")
    elif style == "clench":
        rx, ry = unit * 0.92, unit * 0.42
        draw.rounded_rectangle([cx - rx, cy + unit * 0.04, cx + rx, cy + unit * 0.04 + ry * 2], radius=int(ry * 0.55), fill=tooth_c, outline=line_c, width=sw)
        ymid = cy + unit * 0.04 + ry
        draw.line([(cx - rx + sw, ymid), (cx + rx - sw, ymid)], fill=line_c, width=max(2, sw // 2))
        for vx in (-0.36, 0.36):
            draw.line([(cx + rx * vx, cy + unit * 0.04 + sw), (cx + rx * vx, cy + unit * 0.04 + ry * 2 - sw)], fill=line_c, width=max(2, sw // 2))
    elif style == "cry_small":
        rx = unit * 0.62
        draw.arc([cx - rx, cy + unit * 0.1, cx + rx, cy + unit * 1.05], 195, 345, fill=line_c, width=sw + 1)

    mouth_1x = layer.resize((w, h + extra_bot), Image.Resampling.LANCZOS)
    out = Image.new("RGBA", (w, h + extra_bot), (0, 0, 0, 0))
    out.alpha_composite(base_img, (0, 0))
    out.alpha_composite(mouth_1x, (0, 0))
    return out


def _make_drowsy_eyes_from_face0(eyes0_rgba: Image.Image) -> Image.Image:
    """Create half-lidded bored/drowsy eyes directly from face:0 by flattening top eyelids over highlights."""
    w, h = eyes0_rgba.size
    left_comps, right_comps = _locate_eye_components(eyes0_rgba)
    out = eyes0_rgba.copy()
    s = 4
    lid_overlay = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(lid_overlay)
    apx = eyes0_rgba.getchannel("A").load()
    px = out.load()
    lid_line = PALETTE["navy"] + (255,)
    for side_comps in (left_comps, right_comps):
        if not side_comps:
            continue
        # Eye cluster is typically the tallest upper component in that half
        eye_c = max(side_comps, key=lambda c: (c["bbox"][3] - c["bbox"][1], c["area"]))
        ex0, ey0, ex1, ey1 = eye_c["bbox"]
        eh = max(6, ey1 - ey0)
        cut_y = ey0 + int(round(eh * 0.42))
        # Clear top 42% of iris/lash (and floating brow just above if desired; keep brow untouched!)
        for yy in range(ey0, min(h, cut_y)):
            for xx in range(max(0, ex0 - 2), min(w, ex1 + 2)):
                px[xx, yy] = (0, 0, 0, 0)
        # Draw crisp slightly sleepy downward-slanted upper eyelid across the cut line
        pad_x = max(2, int((ex1 - ex0) * 0.08))
        lw = max(4, int(round(eh * 0.14 * s)))
        draw.line(
            [((ex0 - pad_x) * s, (cut_y + 1) * s), ((ex1 + pad_x) * s, (cut_y + 3) * s)],
            fill=lid_line,
            width=lw,
            joint="curve",
        )
    out.alpha_composite(lid_overlay.resize((w, h), Image.Resampling.LANCZOS))
    return out


def _make_smirk_eyes_from_face0(eyes0_rgba: Image.Image) -> Image.Image:
    """Keep face:0 right amber-gold eye/brow/nose 100% locked; squint ONLY viewer-left magenta eye."""
    w, h = eyes0_rgba.size
    left_comps, _ = _locate_eye_components(eyes0_rgba)
    if not left_comps:
        return eyes0_rgba.copy()
    eye_c = max(left_comps, key=lambda c: (c["bbox"][3] - c["bbox"][1], c["area"]))
    ex0, ey0, ex1, ey1 = eye_c["bbox"]
    out = eyes0_rgba.copy()
    sub = out.crop((ex0, ey0, ex1, ey1))
    # Clear old viewer-left eye and paste vertically squinted (52%) copy anchored to lower half
    px = out.load()
    for mx, my in eye_c["pts"]:
        px[mx, my] = (0, 0, 0, 0)
    squint_h = max(6, round((ey1 - ey0) * 0.54))
    squinted = sub.resize((sub.width, squint_h), Image.Resampling.LANCZOS)
    target_y = ey0 + int(round((ey1 - ey0) * 0.36))
    out.alpha_composite(squinted, (ex0, target_y))
    return out


def _derive_cheeks(left_side: bool = True) -> Image.Image:
    """4x supersampled open-C puffed cheek matching Gemini's #FCEEE7 skin (zero inner mouth stroke)."""
    s = 4
    w, h = 160, 150
    base = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    fill_layer = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(fill_layer)
    # Soft puffed oval shifted left (open on right edge facing mouth)
    box = [18 * s, 16 * s, 142 * s, 134 * s]
    draw.ellipse(box, fill=PALETTE["skin"] + (255,))
    # Pre-blended opaque center blush (no alpha-overwrite artifacts)
    for step, ratio, mix in ((1, 0.76, 0.28), (2, 0.54, 0.52), (3, 0.32, 0.72)):
        br = tuple(int(PALETTE["skin"][i] * (1 - mix) + PALETTE["blush_pink"][i] * mix) for i in range(3)) + (255,)
        inset = int(34 * s * (1 - ratio))
        draw.ellipse([box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset], fill=br)
    # Trim right inner opening cleanly so no hard stroke faces the mouth
    draw.rectangle([108 * s, 0, w * s, h * s], fill=(0, 0, 0, 0))
    # Outer C-arc outline on top/left/bottom only
    draw.arc(box, 68, 292, fill=PALETTE["skin_line"] + (255,), width=7 * s)
    base.alpha_composite(fill_layer)
    out = base.resize((w, h), Image.Resampling.LANCZOS)
    if not left_side:
        out = ImageOps.mirror(out)
    return out


def _derive_skirt_wide_fan(skirt0: Image.Image) -> Image.Image:
    """Premultiplied-alpha 2x horizontal/arc fan-warp from finalized skirt:0 into ~2.2:1 skirt:3."""
    tb = _tight_bbox(skirt0)
    src = skirt0.crop(tb)
    sw, sh = src.size
    up_w, up_h = sw * 2, sh * 2
    hi = src.resize((up_w, up_h), Image.Resampling.LANCZOS).convert("RGBa")
    spx = hi.load()

    out_w = max(4, round(up_w * 1.48))
    out_h = max(4, round(up_h * 0.88))
    dst = Image.new("RGBa", (out_w, out_h), (0, 0, 0, 0))
    dpx = dst.load()

    for dy in range(out_h):
        v = float(dy) / max(1, out_h - 1)
        for dx in range(out_w):
            u = float(dx) / max(1, out_w - 1) * 2.0 - 1.0  # -1 .. +1
            # Lift side wing tips upward in crescent arc and compress bottom curve
            lift = (u * u) * 0.28 * (0.35 + 0.65 * v)
            sv = (v + lift * 0.85 - 0.05) / 0.92
            su = 0.5 + u * (0.48 - 0.06 * v)
            sx = su * (up_w - 1)
            sy = sv * (up_h - 1)
            if 0.0 <= sx < up_w - 1 and 0.0 <= sy < up_h - 1:
                ix, iy = int(sx), int(sy)
                fx, fy = sx - ix, sy - iy
                c00, c10 = spx[ix, iy], spx[ix + 1, iy]
                c01, c11 = spx[ix, iy + 1], spx[ix + 1, iy + 1]
                col = tuple(
                    max(0, min(255, int(round(
                        c00[k] * (1 - fx) * (1 - fy)
                        + c10[k] * fx * (1 - fy)
                        + c01[k] * (1 - fx) * fy
                        + c11[k] * fx * fy
                    ))))
                    for k in range(4)
                )
                dpx[dx, dy] = col

    res = dst.convert("RGBA").resize((max(4, out_w // 2), max(4, out_h // 2)), Image.Resampling.LANCZOS)
    rb = _tight_bbox(res)
    return res.crop(rb)


def _derive_bonesaw_hand() -> Image.Image:
    """4x supersampled corrugated kraft cardboard prop with white chalk bonesaw doodle (teeth on LEFT)."""
    s = 4
    w, h = 210, 370
    canvas = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    kraft_mid = (188, 146, 98, 255)
    kraft_edge = (152, 114, 74, 255)
    kraft_dark = (88, 60, 38, 255)
    tape_tan = (224, 198, 152, 255)
    chalk = (252, 252, 250, 255)
    fold_dark = (128, 92, 58, 255)

    # Corrugated board polygon (narrower at wrist top, flaring cleanly downward; teeth along LEFT doodle)
    board = [(76 * s, 56 * s), (138 * s, 54 * s), (168 * s, 344 * s), (42 * s, 348 * s)]
    side_strip = [(138 * s, 54 * s), (150 * s, 60 * s), (180 * s, 340 * s), (168 * s, 344 * s)]
    draw.polygon(side_strip, fill=kraft_edge)
    draw.polygon(board, fill=kraft_mid)
    # Corrugation lines on right thickness
    for yy in range(66 * s, 336 * s, 10 * s):
        draw.line([(142 * s, yy), (174 * s, yy - 2 * s)], fill=kraft_dark, width=2 * s)
    draw.line(board + [board[0]], fill=kraft_dark, width=5 * s, joint="curve")
    draw.line(side_strip + [side_strip[0]], fill=kraft_dark, width=4 * s, joint="curve")

    # Dark creased top fold tab
    draw.polygon([(74 * s, 50 * s), (140 * s, 48 * s), (146 * s, 82 * s), (70 * s, 84 * s)], fill=fold_dark, outline=kraft_dark, width=4 * s)
    # Small kraft masking tape accents
    draw.rectangle([126 * s, 122 * s, 156 * s, 140 * s], fill=tape_tan, outline=kraft_dark, width=3 * s)

    # White chalk bonesaw doodle strictly on front board, serrated teeth along LEFT edge
    blade = [
        (82 * s, 102 * s), (126 * s, 104 * s), (138 * s, 316 * s),
        (76 * s, 316 * s),
    ]
    # Build saw teeth on left side going downward
    left_teeth: list[tuple[int, int]] = [(82 * s, 118 * s)]
    for idx in range(11):
        y_top = (122 + idx * 17) * s
        y_mid = (130 + idx * 17) * s
        left_teeth.append((62 * s, y_mid))
        left_teeth.append((80 * s, y_top + 17 * s))
    chalk_poly = [(82 * s, 102 * s), (128 * s, 104 * s), (142 * s, 318 * s), (78 * s, 318 * s)] + list(reversed(left_teeth))
    draw.polygon(chalk_poly, fill=chalk)
    # Small oval hole near tip
    draw.ellipse([96 * s, 284 * s, 118 * s, 304 * s], fill=kraft_mid, outline=chalk, width=3 * s)
    # Chalk handle below grip
    draw.rounded_rectangle([90 * s, 88 * s, 122 * s, 112 * s], radius=4 * s, fill=chalk)

    # Cute chibi Gemini hand pinching top dark fold tab
    hand_color = PALETTE["skin"] + (255,)
    hand_line = PALETTE["skin_line"] + (255,)
    draw.ellipse([78 * s, 16 * s, 134 * s, 68 * s], fill=hand_color, outline=hand_line, width=5 * s)
    for fx in (84, 98, 112):
        draw.ellipse([fx * s, 44 * s, (fx + 14) * s, 68 * s], fill=hand_color, outline=hand_line, width=4 * s)

    out = canvas.resize((w, h), Image.Resampling.LANCZOS)
    return out.crop(_tight_bbox(out))


def _derive_xray_hair_lock(lock_rgba: Image.Image, *, tilt_left: bool) -> Image.Image:
    """Create pure #000000 X-ray hair lock with crisp #FFFFFF streaks masked strictly inside silhouette."""
    tb = _tight_bbox(lock_rgba)
    src = lock_rgba.crop(tb)
    w, h = src.size
    alpha = src.getchannel("A")
    black_base = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    black_base.putalpha(alpha)

    # Erode silhouette inward so white X-ray streaks never bleed onto green edges
    eroded = alpha.filter(ImageFilter.MinFilter(size=13))
    streaks = Image.new("RGBA", (w * 2, h * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(streaks)
    sw = max(4, int(round(min(w, h) * 0.032 * 2)))
    if tilt_left:
        curves = [
            [(w * 1.48, h * 0.24), (w * 1.04, h * 0.88), (w * 0.62, h * 1.38), (w * 0.44, h * 1.76)],
            [(w * 1.66, h * 0.48), (w * 1.28, h * 1.06), (w * 0.92, h * 1.54), (w * 0.78, h * 1.82)],
            [(w * 1.24, h * 0.36), (w * 0.80, h * 0.96), (w * 0.44, h * 1.46)],
        ]
    else:
        curves = [
            [(w * 0.52, h * 0.24), (w * 0.96, h * 0.88), (w * 1.38, h * 1.38), (w * 1.56, h * 1.76)],
            [(w * 0.34, h * 0.48), (w * 0.72, h * 1.06), (w * 1.08, h * 1.54), (w * 1.22, h * 1.82)],
            [(w * 0.76, h * 0.36), (w * 1.20, h * 0.96), (w * 1.56, h * 1.46)],
        ]
    for pts in curves:
        draw.line(pts, fill=(255, 255, 255, 255), width=sw, joint="curve")
    streaks_1x = streaks.resize((w, h), Image.Resampling.LANCZOS)
    masked_streaks = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    masked_streaks.paste(streaks_1x, (0, 0), mask=eroded)
    black_base.alpha_composite(masked_streaks)
    return black_base


def _derive_tilted_pelvis(pelvis0: Image.Image, pelvis3: Image.Image, pelvis6: Image.Image, angle_deg: float, use_side: bool = False, use_back: bool = False) -> Image.Image:
    """Create clean compact narrow midnight-blue waist connectors by rotating base pelvis pieces."""
    src = pelvis3 if use_side else (pelvis6 if use_back else pelvis0)
    clean = src.crop(_tight_bbox(src))
    if abs(angle_deg) > 0.5:
        clean = clean.rotate(angle_deg, resample=Image.Resampling.BICUBIC, expand=True)
    return clean.crop(_tight_bbox(clean))


def derive_sprites(work_dir: Path = DEFAULT_WORK_DIR) -> tuple[Path, Path]:
    """Synthesize Pillow 4x derived sprites (`rework_derived_ai.png` + exact lossless patches) cleanly."""
    manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest_symbols = manifest["symbols"]
    sliced = _load_source_cells(work_dir, include_reworks=True, exclude_derived=True)

    derived_dir = work_dir / "derived_sprites"
    derived_dir.mkdir(parents=True, exist_ok=True)

    # Build locked-eye bases from face:0, face:11, face:25
    f0_img, f0_meta = sliced[("face", 0)]
    f11_img, _ = sliced[("face", 11)]
    f25_img, _ = sliced[("face", 25)]
    f0_tb = _tight_bbox(f0_img)
    f0_tight = f0_img.crop(f0_tb)
    eyes0, anchor0 = _extract_locked_eyes_face(f0_img)
    eyes11, anchor11 = _extract_locked_eyes_face(f11_img)
    eyes25, anchor25 = _extract_locked_eyes_face(f25_img)
    eyes20 = _make_drowsy_eyes_from_face0(eyes0)
    eyes28 = _make_smirk_eyes_from_face0(eyes0)

    def finalize_locked_face(canvas_img: Image.Image, ref_slot: tuple[str, int], ref_tight_size: tuple[int, int]) -> tuple[Image.Image, dict]:
        # Extract non-transparent bbox relative to ref_tight top-left so eyes lock to 0.00 px
        tb = _tight_bbox(canvas_img)
        cropped = canvas_img.crop(tb)
        return cropped, {
            "lock_ref": list(ref_slot),
            "lock_dx_px": tb[0],
            "lock_dy_px": tb[1],
            "lock_ref_size": list(ref_tight_size),
        }

    items: list[dict] = []

    def push(sym: str, idx: int, sprite: Image.Image, *, lock_meta: dict | None = None, note: str = "") -> None:
        png_path = derived_dir / f"{sym}_{idx:02d}.png"
        sprite.save(png_path)
        if sym in manifest_symbols and idx < len(manifest_symbols[sym]):
            meta = manifest_symbols[sym][idx]
            orig_tw = meta["tight_bbox"][2] - meta["tight_bbox"][0]
            orig_th = meta["tight_bbox"][3] - meta["tight_bbox"][1]
        else:
            orig_tw, orig_th = (200, 200)
        entry: dict = {
            "label": f"{sym}:{idx} ({note})" if note else f"{sym}:{idx}",
            "symbol": sym,
            "frame_idx": idx,
            "sprite": sprite,
            "tight_bbox": _tight_bbox(sprite),
            "orig_tight_size": (orig_tw, orig_th),
            "fill_ratio": 0.72,
            "png_rel": f"derived_sprites/{png_path.name}",
        }
        if lock_meta:
            entry.update(lock_meta)
        items.append(entry)

    # 13 locked-eye derived faces
    for fidx, base_eyes, anc, style, note in [
        (9, eyes0, anchor0, "talk_half", "lock0 talk"),
        (10, eyes0, anchor0, "talk_open", "lock0 open"),
        (16, eyes0, anchor0, "worried_o", "lock0 sad o"),
        (19, eyes0, anchor0, "pout", "lock0 pout"),
        (20, eyes20, anchor0, "sigh_open", "lock0 drowsy open"),
        (22, eyes20, anchor0, "frown", "lock20 drowsy frown"),
        (28, eyes28, anchor0, "smirk", "lock0r squintl smirk"),
        (30, eyes28, anchor0, "frown", "lock28 annoyed"),
    ]:
        rendered = _draw_mouth_overlay(base_eyes, anc, style)
        spr, lmeta = finalize_locked_face(rendered, ("face", 0), f0_tight.size)
        push("face", fidx, spr, lock_meta=lmeta, note=note)

    f11_tight = f11_img.crop(_tight_bbox(f11_img))
    for fidx, style, note in ((17, "frown", "lock11 sad"), (18, "kiss_o", "lock11 kiss/o")):
        rendered = _draw_mouth_overlay(eyes11, anchor11, style)
        spr, lmeta = finalize_locked_face(rendered, ("face", 11), f11_tight.size)
        push("face", fidx, spr, lock_meta=lmeta, note=note)

    f25_tight = f25_img.crop(_tight_bbox(f25_img))
    for fidx, style, note in ((26, "worried_o", "lock25 refuse"), (27, "clench", "lock25 effort"), (29, "cry_small", "lock25 cry")):
        rendered = _draw_mouth_overlay(eyes25, anchor25, style)
        spr, lmeta = finalize_locked_face(rendered, ("face", 25), f25_tight.size)
        push("face", fidx, spr, lock_meta=lmeta, note=note)

    # Vector/warp/mirror parts
    # Split Sheet 1 front hair curtain cleanly into single-sided hairpigtails:0..4 locks
    # while preserving Sheet 1's unified back curtain as hair:2
    hp0_raw, _ = sliced[("hairpigtails", 0)]
    hp0_tight = hp0_raw.crop(_tight_bbox(hp0_raw))
    mid_x = max(1, hp0_tight.width // 2)
    left_raw = hp0_tight.crop((0, 0, mid_x, hp0_tight.height))
    left_crop = left_raw.crop(_tight_bbox(left_raw))
    left_lock = left_crop.rotate(-12.0, resample=Image.Resampling.BICUBIC, expand=True)
    left_lock = left_lock.crop(_tight_bbox(left_lock))

    right_raw = hp0_tight.crop((mid_x, 0, hp0_tight.width, hp0_tight.height))
    right_crop = right_raw.crop(_tight_bbox(right_raw))
    right_lock = right_crop.rotate(12.0, resample=Image.Resampling.BICUBIC, expand=True)
    right_lock = right_lock.crop(_tight_bbox(right_lock))

    hp2_raw, _ = sliced[("hairpigtails", 2)]
    back_full = hp2_raw.crop(_tight_bbox(hp2_raw))

    push("hair", 2, back_full, note="back full unified")
    push("hairpigtails", 0, left_lock, note="left lock")
    push("hairpigtails", 1, right_lock, note="right lock")
    push("hairpigtails", 2, left_lock, note="side near lock")
    push("hairpigtails", 3, _derive_xray_hair_lock(left_lock, tilt_left=True), note="xray left lock")
    push("hairpigtails", 4, _derive_xray_hair_lock(right_lock, tilt_left=False), note="xray right lock")

    skirt0_img, _ = sliced[("skirt", 0)]
    skirt3_img = _derive_skirt_wide_fan(skirt0_img)
    push("skirt", 3, skirt3_img, note="fan warp ~2.2:1")

    push("cheeks", 0, _derive_cheeks(left_side=True), note="open-C L")
    push("cheeks", 1, _derive_cheeks(left_side=False), note="open-C R")
    push("hand", 15, _derive_bonesaw_hand(), note="cardboard bonesaw")

    foot0_img, _ = sliced[("foot", 0)]
    foot2_img = ImageOps.mirror(foot0_img.crop(_tight_bbox(foot0_img)))
    push("foot", 2, foot2_img, note="mirror foot:0")

    pelvis0_img, _ = sliced[("torso_pelvis", 0)]
    pelvis3_img, _ = sliced[("torso_pelvis", 3)]
    pelvis6_img, _ = sliced[("torso_pelvis", 6)]
    for pidx, ang, side_flag, note in [
        (1, 7.0, False, "tilt L"),
        (2, -7.0, False, "tilt R"),
        (4, 12.0, True, "side tilt L"),
        (5, -12.0, True, "side tilt R"),
        (7, 10.0, True, "tilt back L"),
        (8, -10.0, True, "tilt back R"),
    ]:
        push("torso_pelvis", pidx, _derive_tilted_pelvis(pelvis0_img, pelvis3_img, pelvis6_img, ang, use_side=side_flag), note=note)

    sheet_img, layout = _compose_sheet("rework_derived", items, cols=5, cell_content_w=280, cell_content_h=280)
    # Persist exact lossless path & lock metadata per cell
    for cell_meta, item in zip(layout["cells"], items):
        cell_meta["direct_png"] = item["png_rel"]
        for key in ("lock_ref", "lock_dx_px", "lock_dy_px", "lock_ref_size"):
            if key in item:
                cell_meta[key] = item[key]

    img_path = work_dir / "rework_derived_ai.png"
    layout_path = work_dir / "rework_derived_layout.json"
    sheet_img.save(img_path)
    layout_path.write_text(json.dumps(layout, indent=2), encoding="utf-8")
    return img_path, layout_path


def prepare_rework(
    work_dir: Path,
    items: list[tuple[str, int, str]],
    *,
    name: str = "rework1",
    cols: int = 3,
    cell_size: int = 340,
) -> tuple[Path, Path]:
    """Compose a focused high-res green-screen rework sheet for selected symbol:frame slots."""
    manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest_symbols = manifest["symbols"]
    sprites_dir = work_dir / "sprites"

    entries: list[dict] = []
    for sym, fidx, note in items:
        if sym == "tail" and fidx in (0, 6):
            sprite = _build_tail_guide(xray=(fidx == 6))
            tb = _tight_bbox(sprite)
            entries.append({
                "label": f"{sym}:{fidx} ({note})" if note else f"{sym}:{fidx}",
                "symbol": sym,
                "frame_idx": fidx,
                "sprite": sprite,
                "tight_bbox": tb,
                "orig_tight_size": (165, 165),
                "fill_ratio": 0.76,
            })
            continue
        if sym == "hairpigtails" and fidx in (0, 1, 2, 3, 4):
            variant = "front" if fidx in (0, 3) else ("side" if fidx in (1, 4) else "back")
            sprite = _build_hair_guide(variant, xray=(fidx in (3, 4)))
            tb = _tight_bbox(sprite)
            hp_tb = manifest_symbols["hairpigtails"][min(fidx, len(manifest_symbols["hairpigtails"]) - 1)]["tight_bbox"]
            entries.append({
                "label": f"{sym}:{fidx} ({note})" if note else f"{sym}:{fidx}",
                "symbol": sym,
                "frame_idx": fidx,
                "sprite": sprite,
                "tight_bbox": tb,
                "orig_tight_size": (hp_tb[2] - hp_tb[0] + 40, hp_tb[3] - hp_tb[1] + 20),
                "fill_ratio": 0.76,
            })
            continue
        if sym not in manifest_symbols or fidx >= len(manifest_symbols[sym]):
            raise ValueError(f"Unknown slot {sym}:{fidx}")
        meta = manifest_symbols[sym][fidx]
        sp_path = sprites_dir / f"{sym}_{fidx:02d}.png"
        with Image.open(sp_path) as sp_raw:
            sprite = sp_raw.convert("RGBA")
        entries.append({
            "label": f"{sym}:{fidx} ({note})" if note else f"{sym}:{fidx}",
            "symbol": sym,
            "frame_idx": fidx,
            "sprite": sprite,
            "tight_bbox": tuple(meta["tight_bbox"]),
            "fill_ratio": 0.76,
        })

    sheet_img, layout = _compose_sheet(name, entries, cols, cell_size, cell_size)
    img_path = work_dir / f"{name}_template.png"
    layout_path = work_dir / f"{name}_layout.json"
    sheet_img.save(img_path)
    layout_path.write_text(json.dumps(layout, indent=2), encoding="utf-8")
    return img_path, layout_path


def render_assembled_preview(
    symbols: dict[str, list[dict]],
    sprites_by_key: dict[tuple[str, int], Image.Image],
    title_label: str = "Wendy vs Gemini Skin",
    *,
    vanilla_symbols: dict[str, list[dict]] | None = None,
    vanilla_sprites: dict[tuple[str, int], Image.Image] | None = None,
) -> Image.Image:
    """Render an assembled Front/Side/Back character comparison strip for visual alignment QA."""
    views = [
        ("Front", 0, 0, 0, 0, 0, 0, 0, 0),
        ("Side", 1, 1, 1, 4, 1, 1, 1, 1),
        ("Back", 2, 2, 2, None, 2, 2, 2, 2),
    ]

    def draw_pose(syms, sprs, pose) -> Image.Image:
        _, hb_i, hp_i, t_i, f_i, p_i, s_i, l_i, ft_i = pose
        pose_canvas = Image.new("RGBA", (320, 420), (235, 228, 210, 255))
        origin_x, origin_y = 160, 290
        if hb_i == 2:
            layers = [
                ("foot", min(ft_i, len(syms["foot"]) - 1), -12, 56),
                ("foot", min(ft_i, len(syms["foot"]) - 1), 14, 56),
                ("leg", min(l_i, len(syms["leg"]) - 1), -12, 24),
                ("leg", min(l_i, len(syms["leg"]) - 1), 14, 24),
                ("torso_pelvis", min(p_i, len(syms["torso_pelvis"]) - 1), 0, -2),
                ("skirt", min(s_i, len(syms["skirt"]) - 1), 0, 2),
                ("arm_upper", min(hb_i, len(syms["arm_upper"]) - 1), -26, -38),
                ("arm_upper", min(hb_i, len(syms["arm_upper"]) - 1), 26, -38),
                ("torso", min(t_i, len(syms["torso"]) - 1), 0, -34),
                ("headbase", min(hb_i, len(syms["headbase"]) - 1), 0, -48),
                ("hairpigtails", min(hp_i, len(syms["hairpigtails"]) - 1), 46, -124),
            ]
        else:
            layers = [
                ("hairpigtails", min(hp_i, len(syms["hairpigtails"]) - 1), 0, -142),
                ("foot", min(ft_i, len(syms["foot"]) - 1), -12, 56),
                ("foot", min(ft_i, len(syms["foot"]) - 1), 14, 56),
                ("leg", min(l_i, len(syms["leg"]) - 1), -12, 24),
                ("leg", min(l_i, len(syms["leg"]) - 1), 14, 24),
                ("torso_pelvis", min(p_i, len(syms["torso_pelvis"]) - 1), 0, -2),
                ("skirt", min(s_i, len(syms["skirt"]) - 1), 0, 2),
                ("arm_upper", min(hb_i, len(syms["arm_upper"]) - 1), -26, -38),
                ("torso", min(t_i, len(syms["torso"]) - 1), 0, -34),
                ("arm_upper", min(hb_i, len(syms["arm_upper"]) - 1), 26, -38),
                ("headbase", min(hb_i, len(syms["headbase"]) - 1), 0, -48),
            ]
            if f_i is not None:
                layers.append(("face", min(f_i, len(syms["face"]) - 1), 0, -126))
        if "tail" in syms and (0 if hb_i != 2 else 0) < len(syms["tail"]) and ("tail", 0) in sprs:
            layers.insert(0 if hb_i != 2 else 6, ("tail", 0, -18, 4))

        for sym, idx, ox, oy in layers:
            fr = syms[sym][idx]
            img = sprs[(sym, idx)]
            w = max(1, round(fr["w"]))
            h = max(1, round(fr["h"]))
            if img.size != (w, h):
                img = img.resize((w, h), Image.Resampling.LANCZOS)
            px = round(origin_x + ox + fr["cx"] - w / 2.0)
            py = round(origin_y + oy + fr["cy"] - h / 2.0)
            pose_canvas.alpha_composite(img, (px, py))
        return pose_canvas

    panels = [draw_pose(symbols, sprites_by_key, pose) for pose in views]
    if vanilla_symbols and vanilla_sprites:
        panels.extend(draw_pose(vanilla_symbols, vanilla_sprites, pose) for pose in views)

    cols = 3
    rows = 2 if (vanilla_symbols and vanilla_sprites) else 1
    out = Image.new("RGBA", (320 * cols, 420 * rows + 32), (28, 28, 32, 255))
    draw = ImageDraw.Draw(out)
    draw.text((12, 6), title_label, fill=(255, 255, 255, 255), font=_load_font(16))
    for idx, panel in enumerate(panels):
        out.paste(panel, ((idx % cols) * 320, 32 + (idx // cols) * 420))
    return out


def pack_skin(
    work_dir: Path = DEFAULT_WORK_DIR,
    out_zip: Path = DEFAULT_OUT_ZIP,
    build_name: str = DEFAULT_BUILD_NAME,
    *,
    offsets_path: Path | None = None,
    icon_path: Path = DEFAULT_ICON_PATH,
    face_map: dict[int, int] = GEMINI_FACE_MAP,
) -> Path:
    """Slice Gemini's 6 sheets + derived/rework sheets, align pivots, pack atlas + build.bin."""
    manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest_symbols = manifest["symbols"]
    sprites_dir = work_dir / "sprites"

    skin_offsets = _skin_root_from_work(work_dir) / "offsets.json"
    offsets_file = offsets_path or (skin_offsets if skin_offsets.is_file() else (work_dir / "offsets.json"))
    extra_offsets: dict[str, list[float]] = (
        json.loads(offsets_file.read_text(encoding="utf-8")) if offsets_file.is_file() else {}
    )

    sliced_clean = _load_source_cells(work_dir, include_reworks=True, exclude_derived=False)

    # Load lossless derived RGBA patches when present (zero green-fringe quantization & eye-lock metadata)
    derived_meta: dict[tuple[str, int], dict] = {}
    derived_lp = work_dir / "rework_derived_layout.json"
    if derived_lp.is_file():
        dlayout = json.loads(derived_lp.read_text(encoding="utf-8"))
        for cell in dlayout.get("cells", []):
            key = (cell["symbol"], int(cell["frame_idx"]))
            # Only use lossless PNG if not overridden by a subsequent manual numbered rework
            if sliced_clean.get(key, (None, {}))[1].get("label", "") == cell.get("label", ""):
                derived_meta[key] = cell
                png_rel = cell.get("direct_png")
                if png_rel and (work_dir / png_rel).is_file():
                    with Image.open(work_dir / png_rel) as dimg:
                        clean_d = dimg.convert("RGBA")
                    sliced_clean[key] = (clean_d, dict(cell))

    tiles: dict[str, Image.Image] = {}
    tile_dst_sizes: dict[str, tuple[float, float]] = {}
    build_symbols: dict[str, list[dict]] = {}
    preview_sprites: dict[tuple[str, int], Image.Image] = {}

    def add_frame(sym: str, idx: int, tile_key: str, img: Image.Image, cx: float, cy: float, w: float, h: float) -> None:
        orig = manifest_symbols[sym][idx] if sym in manifest_symbols and idx < len(manifest_symbols[sym]) else {
            "framenum": idx,
            "duration": 1,
        }
        if tile_key not in tiles:
            tiles[tile_key] = img
            tile_dst_sizes[tile_key] = (w, h)
        preview_sprites[(sym, idx)] = img
        build_symbols.setdefault(sym, []).append({
            "framenum": orig["framenum"],
            "duration": orig["duration"],
            "cx": cx,
            "cy": cy,
            "w": w,
            "h": h,
            "tile_key": tile_key,
        })

    # 1. Core AI + derived + direct-pixel-reused body symbols
    for sym in ("headbase", "headbase_hat", "hairpigtails", "torso", "torso_pelvis", "skirt", "arm_upper", "arm_lower", "leg", "foot", "cheeks"):
        for idx, orig in enumerate(manifest_symbols[sym]):
            if sym == "headbase_hat" and (sym, idx) not in sliced_clean:
                base_frame = build_symbols["headbase"][idx]
                base_img = preview_sprites[("headbase", idx)]
                head_orig = manifest_symbols["headbase"][idx]
                head_th = max(1, head_orig["tight_bbox"][3] - head_orig["tight_bbox"][1])
                hat_th = max(1, orig["tight_bbox"][3] - orig["tight_bbox"][1])
                keep_ratio = max(0.72, min(0.84, float(hat_th) / float(head_th) * 0.94))
                crop_top = max(0, int(round(base_img.height * (1.0 - keep_ratio))))
                cropped_hat = base_img.crop((0, crop_top, base_img.width, base_img.height))
                hat_bbox = cropped_hat.getbbox()
                if hat_bbox:
                    cropped_hat = cropped_hat.crop(hat_bbox)
                scale_y = float(cropped_hat.height) / max(1.0, float(base_img.height))
                scale_x = float(cropped_hat.width) / max(1.0, float(base_img.width))
                dw = float(base_frame["w"]) * scale_x
                dh = float(base_frame["h"]) * scale_y
                base_bot = float(base_frame["cy"]) + float(base_frame["h"]) / 2.0
                dx_off, dy_off, extra_scale = _parse_offset_entry(
                    extra_offsets.get(f"headbase_hat:{idx}", extra_offsets.get("headbase_hat", (0.0, 0.0, 1.0)))
                )
                add_frame(sym, idx, f"{sym}:{idx}", cropped_hat, float(base_frame["cx"]) + dx_off, (base_bot - dh / 2.0) + dy_off, dw * extra_scale, dh * extra_scale)
                continue

            if (sym, idx) in PIXEL_REUSE_MAP and (sym, idx) not in sliced_clean:
                src_sym, src_idx = PIXEL_REUSE_MAP[(sym, idx)]
                src_frame = build_symbols[src_sym][src_idx]
                src_img = preview_sprites[(src_sym, src_idx)]
                dx_off, dy_off, extra_scale = _parse_offset_entry(
                    extra_offsets.get(f"{sym}:{idx}", extra_offsets.get(sym, (0.0, 0.0, 1.0)))
                )
                add_frame(sym, idx, f"{src_sym}:{src_idx}", src_img, src_frame["cx"] + dx_off, src_frame["cy"] + dy_off, src_frame["w"] * extra_scale, src_frame["h"] * extra_scale)
                continue

            clean_cell, cell_meta = sliced_clean[(sym, idx)]
            off = _parse_offset_entry(extra_offsets.get(f"{sym}:{idx}", extra_offsets.get(sym, (0.0, 0.0, 1.0))))
            cropped, cx, cy, dw, dh = _align_sprite_to_vanilla(clean_cell, cell_meta, orig, sym, idx, extra_offset=off)
            if sym == "hairpigtails" and idx in (0, 1, 2) and cropped.height > 8:
                cropped = _feather_top_alpha(cropped, 0.24, smoothstep=True)
            add_frame(sym, idx, f"{sym}:{idx}", cropped, cx, cy, dw, dh)

    # 2. Faces: align core expressions first, then derived locked-eye expressions, then verbatim same-view fallbacks
    face_aligned: dict[int, tuple[Image.Image, float, float, float, float]] = {}
    for idx, orig in enumerate(manifest_symbols["face"]):
        if (idx in GEMINI_CORE_FACES or ("face", idx) in sliced_clean) and ("face", idx) not in derived_meta:
            clean_cell, cell_meta = sliced_clean[("face", idx)]
            off = _parse_offset_entry(extra_offsets.get(f"face:{idx}", extra_offsets.get("face", (0.0, 0.0, 1.0))))
            aligned = _align_sprite_to_vanilla(clean_cell, cell_meta, orig, "face", idx, extra_offset=off)
            face_aligned[idx] = aligned
            img, cx, cy, dw, dh = aligned
            add_frame("face", idx, f"face:{idx}", img, cx, cy, dw, dh)

    # Apply mathematically eye-locked derived faces (using parent aligned world coords + pixel delta)
    for idx, orig in enumerate(manifest_symbols["face"]):
        if idx in face_aligned or ("face", idx) not in derived_meta:
            continue
        dmeta = derived_meta[("face", idx)]
        ref_idx = int(dmeta["lock_ref"][1])
        ref_img, ref_cx, ref_cy, ref_dw, ref_dh = face_aligned[ref_idx]
        ref_pw, ref_ph = dmeta["lock_ref_size"]
        sx = ref_dw / max(1.0, float(ref_pw))
        sy = ref_dh / max(1.0, float(ref_ph))
        child_img, _ = sliced_clean[("face", idx)]
        ref_x0 = ref_cx - ref_dw / 2.0
        ref_y0 = ref_cy - ref_dh / 2.0
        cx0 = ref_x0 + float(dmeta["lock_dx_px"]) * sx
        cy0 = ref_y0 + float(dmeta["lock_dy_px"]) * sy
        dw = float(child_img.width) * sx
        dh = float(child_img.height) * sy
        slot_dx, slot_dy, slot_sc = (0.0, 0.0, 1.0)
        if f"face:{idx}" in extra_offsets:
            slot_dx, slot_dy, slot_sc = _parse_offset_entry(extra_offsets[f"face:{idx}"])
        cx = cx0 + dw / 2.0 + slot_dx
        cy = cy0 + dh / 2.0 + slot_dy
        aligned = (child_img, cx, cy, dw * slot_sc, dh * slot_sc)
        face_aligned[idx] = aligned
        add_frame("face", idx, f"face:{idx}", child_img, cx, cy, dw * slot_sc, dh * slot_sc)

    for idx, orig in enumerate(manifest_symbols["face"]):
        if idx in face_aligned:
            continue
        if idx == 14:
            with Image.open(sprites_dir / "face_14.png") as raw_glow:
                glow = raw_glow.convert("RGBA")
            tx0, ty0, tx1, ty1 = orig["tight_bbox"]
            tight = glow.crop((tx0, ty0, tx1, ty1))
            dx_off, dy_off, extra_scale = _parse_offset_entry(extra_offsets.get("face:14", extra_offsets.get("face", (0.0, 0.0, 1.0))))
            cx = float(orig["cx"]) - float(orig["w"]) / 2.0 + (tx0 + tx1) / 2.0 + dx_off
            cy = float(orig["cy"]) - float(orig["h"]) / 2.0 + (ty0 + ty1) / 2.0 + dy_off
            add_frame("face", idx, "face:14_vanilla", tight, cx, cy, float(tx1 - tx0) * extra_scale, float(ty1 - ty0) * extra_scale)
            continue
        mapped_key = face_map.get(idx, 0)
        if mapped_key not in face_aligned:
            mapped_key = 0
        img, cx, cy, dw, dh = face_aligned[mapped_key]
        slot_dx, slot_dy, slot_sc = (0.0, 0.0, 1.0)
        if f"face:{idx}" in extra_offsets:
            slot_dx, slot_dy, slot_sc = _parse_offset_entry(extra_offsets[f"face:{idx}"])
        add_frame("face", idx, f"face:{mapped_key}", img, cx + slot_dx, cy + slot_dy, dw * slot_sc, dh * slot_sc)

    # 3. Procedural wide-body slots reusing generated torso/pelvis/skirt tiles
    for wide_sym, base_sym in (("torso_wide", "torso"), ("torso_pelvis_wide", "torso_pelvis"), ("skirt_wide", "skirt")):
        for idx, orig in enumerate(manifest_symbols[wide_sym]):
            base_orig = manifest_symbols[base_sym][idx]
            base_frame = build_symbols[base_sym][idx]
            sw = float(orig["w"]) / max(1.0, float(base_orig["w"]))
            sh = float(orig["h"]) / max(1.0, float(base_orig["h"]))
            cx = base_frame["cx"] + (float(orig["cx"]) - float(base_orig["cx"]))
            cy = base_frame["cy"] + (float(orig["cy"]) - float(base_orig["cy"]))
            add_frame(wide_sym, idx, base_frame["tile_key"], tiles[base_frame["tile_key"]], cx, cy, base_frame["w"] * sw, base_frame["h"] * sh)

    # 3b. Hybrid hair routing: blank hair:0,1,3 + hair_hat:0,1,3; reuse hairpigtails:2 for hair:2 + hair_hat:2
    blank_img = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    back_hair_src = ("hair", 2) if ("hair", 2) in sliced_clean else ("hairpigtails", 2)
    for idx in (0, 1, 2, 3):
        if idx == 2 and back_hair_src in sliced_clean:
            clean_cell, _ = sliced_clean[back_hair_src]
            bbox = clean_cell.getchannel("A").getbbox()
            if bbox is not None:
                cropped = _feather_top_alpha(clean_cell.crop(bbox), 0.22, smoothstep=True)
                def_cx, def_cy, max_w, max_h = (0.0, 68.0, 215.0, 215.0)
                dx_off, dy_off, extra_scale = _parse_offset_entry(extra_offsets.get("hair:2", extra_offsets.get("hair", (0.0, 0.0, 1.0))))
                fit_scale = min(max_w / max(1.0, float(cropped.width)), max_h / max(1.0, float(cropped.height)))
                dw = float(cropped.width) * fit_scale * extra_scale
                dh = float(cropped.height) * fit_scale * extra_scale
                cx = def_cx + dx_off
                cy = def_cy + dy_off
                add_frame("hair", 2, "hair:2", cropped, cx, cy, dw, dh)
                hat_dx, hat_dy, hat_scale = _parse_offset_entry(extra_offsets.get("hair_hat:2", extra_offsets.get("hair_hat", (0.0, 0.0, 1.0))))
                add_frame("hair_hat", 2, "hair:2", cropped, cx + hat_dx, cy + hat_dy, dw * hat_scale, dh * hat_scale)
                continue
        add_frame("hair", idx, "long_hair_blank:0", blank_img, 0.0, 0.0, 4.0, 4.0)
        add_frame("hair_hat", idx, "long_hair_blank:0", blank_img, 0.0, 0.0, 4.0, 4.0)

    # 3c. Animated Gemini cat tail (tail:0 normal, tail:6 X-ray, tail:1..5 & 7..11 blank)
    if ("tail", 0) in sliced_clean:
        def _pack_cat_tail(
            clean_cell: Image.Image,
            extra_off: tuple[float, float, float],
            *,
            rot_deg: float = -65.0,
        ) -> tuple[Image.Image, float, float, float, float]:
            bbox = clean_cell.getchannel("A").getbbox()
            if bbox is None:
                return blank_img, 0.0, 0.0, 4.0, 4.0
            cropped = clean_cell.crop(bbox)
            pre_rot = cropped.rotate(rot_deg, resample=Image.Resampling.BICUBIC, expand=True)
            rbox = pre_rot.getchannel("A").getbbox()
            if rbox is not None:
                pre_rot = pre_rot.crop(rbox)
            dx_off, dy_off, extra_scale = _parse_offset_entry(extra_off)
            base_fit = 88.0 / max(1.0, float(max(pre_rot.width, pre_rot.height)))
            dw = float(pre_rot.width) * base_fit * extra_scale
            dh = float(pre_rot.height) * base_fit * extra_scale
            cx = -dw * 0.44 + dx_off
            cy = -dh * 0.14 + dy_off
            return pre_rot, cx, cy, dw, dh

        tail0_cell, _ = sliced_clean[("tail", 0)]
        t0_img, t0_cx, t0_cy, t0_w, t0_h = _pack_cat_tail(
            tail0_cell,
            extra_offsets.get("tail:0", extra_offsets.get("tail", (0.0, 0.0, 1.0))),
            rot_deg=-65.0,
        )
        tail6_key = ("tail", 6) if ("tail", 6) in sliced_clean else ("tail", 0)
        tail6_cell, _ = sliced_clean[tail6_key]
        t6_img, t6_cx, t6_cy, t6_w, t6_h = _pack_cat_tail(
            tail6_cell,
            extra_offsets.get("tail:6", extra_offsets.get("tail", (0.0, 0.0, 1.0))),
            rot_deg=-52.0 if tail6_key == ("tail", 6) else -65.0,
        )
        for tidx in range(12):
            if tidx == 0:
                add_frame("tail", 0, "tail:0", t0_img, t0_cx, t0_cy, t0_w, t0_h)
            elif tidx == 6:
                add_frame("tail", 6, "tail:6", t6_img, t6_cx, t6_cy, t6_w, t6_h)
            else:
                add_frame("tail", tidx, "long_hair_blank:0", blank_img, 0.0, 0.0, 4.0, 4.0)

    # 4. Hand & arm_upper_skin (AI / derived / PIXEL_REUSE_MAP, fallback recolor)
    for skin_sym in ("hand", "arm_upper_skin"):
        for idx, orig in enumerate(manifest_symbols[skin_sym]):
            if (skin_sym, idx) in PIXEL_REUSE_MAP and (skin_sym, idx) not in sliced_clean:
                src_sym, src_idx = PIXEL_REUSE_MAP[(skin_sym, idx)]
                src_frame = build_symbols[src_sym][src_idx]
                src_img = preview_sprites[(src_sym, src_idx)]
                dx_off, dy_off, extra_scale = _parse_offset_entry(
                    extra_offsets.get(f"{skin_sym}:{idx}", extra_offsets.get(skin_sym, (0.0, 0.0, 1.0)))
                )
                add_frame(skin_sym, idx, f"{src_sym}:{src_idx}", src_img, src_frame["cx"] + dx_off, src_frame["cy"] + dy_off, src_frame["w"] * extra_scale, src_frame["h"] * extra_scale)
                continue
            if (skin_sym, idx) in sliced_clean:
                clean_cell, cell_meta = sliced_clean[(skin_sym, idx)]
                off = _parse_offset_entry(extra_offsets.get(f"{skin_sym}:{idx}", extra_offsets.get(skin_sym, (0.0, 0.0, 1.0))))
                cropped, cx, cy, dw, dh = _align_sprite_to_vanilla(clean_cell, cell_meta, orig, skin_sym, idx, extra_offset=off)
                add_frame(skin_sym, idx, f"{skin_sym}:{idx}", cropped, cx, cy, dw, dh)
                continue
            with Image.open(sprites_dir / f"{skin_sym}_{idx:02d}.png") as raw_spr:
                recolored = recolor_skin_sprite(raw_spr)
            tx0, ty0, tx1, ty1 = orig["tight_bbox"]
            tight = recolored.crop((tx0, ty0, tx1, ty1))
            orig_fw, orig_fh = float(orig["w"]), float(orig["h"])
            dx_off, dy_off, extra_scale = _parse_offset_entry(extra_offsets.get(f"{skin_sym}:{idx}", extra_offsets.get(skin_sym, (0.0, 0.0, 1.0))))
            base_cx = float(orig["cx"]) - orig_fw / 2.0 + (tx0 + tx1) / 2.0
            base_cy = float(orig["cy"]) - orig_fh / 2.0 + (ty0 + ty1) / 2.0
            if skin_sym in PIVOT_SCALED_SYMBOLS:
                cx = base_cx * extra_scale + dx_off
                cy = base_cy * extra_scale + dy_off
            else:
                cx = base_cx + dx_off
                cy = base_cy + dy_off
            add_frame(skin_sym, idx, f"{skin_sym}:{idx}", tight, cx, cy, float(tx1 - tx0) * extra_scale, float(ty1 - ty0) * extra_scale)

    add_frame("long_hair_blank", 0, "long_hair_blank:0", blank_img, 0.0, 0.0, 4.0, 4.0)

    if icon_path.is_file():
        with Image.open(icon_path) as raw_icon:
            icon_img = ImageOps.contain(raw_icon.convert("RGBA"), (240, 240), Image.Resampling.LANCZOS)
    else:
        icon_img = Image.new("RGBA", (128, 128), PALETTE["hair_purple"] + (255,))
    add_frame("SWAP_ICON", 0, "SWAP_ICON:0", icon_img, 0.0, 0.0, float(icon_img.width), float(icon_img.height))

    packed = None
    for supersample in (1.75, 1.5, 1.25, 1.0):
        packed = _shelf_pack_tiles(tiles, atlas_size=2048, padding=4, supersample=supersample, tile_dst_sizes=tile_dst_sizes)
        if packed is not None:
            break
    if packed is None:
        raise ValueError("Sprites overflow 2048x2048 atlas even at 1.0x scale")
    atlas_img, rects = packed

    for frames in build_symbols.values():
        for frame in frames:
            frame["atlas_rect"] = rects[frame.pop("tile_key")]

    build_bytes = write_build_bin(build_name, "atlas-0.tex", build_symbols, 2048, 2048)
    tex_bytes = encode_ktex_rgba(atlas_img)

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("build.bin", build_bytes)
        zf.writestr("atlas-0.tex", tex_bytes)

    atlas_img.save(work_dir / "packed_atlas_preview.png")

    vanilla_sprites = {}
    for sym, frames in manifest_symbols.items():
        for idx in range(len(frames)):
            p = sprites_dir / f"{sym}_{idx:02d}.png"
            if p.is_file():
                with Image.open(p) as sp:
                    vanilla_sprites[(sym, idx)] = sp.convert("RGBA")

    preview = render_assembled_preview(
        build_symbols,
        preview_sprites,
        title_label=f"Skin Preview ({build_name}) vs Vanilla Wendy",
        vanilla_symbols=manifest_symbols,
        vanilla_sprites=vanilla_sprites,
    )
    preview.save(work_dir / "preview_assembled.png")
    return out_zip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sr = sub.add_parser("split-ref", help="Split assets/gemini-1.png into clean SD/normal/expressions reference images + icon")
    sr.add_argument("--src", type=Path, default=DEFAULT_SRC_ASSET)
    sr.add_argument("--portrait", type=Path, default=DEFAULT_SRC_PORTRAIT)
    sr.add_argument("--out", type=Path, default=DEFAULT_REF_DIR)

    ext = sub.add_parser("extract", help="Unpack vanilla wendy.zip and compose Gemini's 6 green-screen sheets")
    ext.add_argument("--vanilla-zip", type=Path, default=DEFAULT_VANILLA_ZIP)
    ext.add_argument("--out", type=Path, default=DEFAULT_WORK_DIR)

    drv = sub.add_parser("derive", help="Synthesize locked-eye faces, fan-warped skirt:3, open-C cheeks, cardboard bonesaw, foot mirror & pelvis tilts")
    drv.add_argument("--work", type=Path, default=DEFAULT_WORK_DIR)

    rw = sub.add_parser("rework", help="Compose a focused high-res green-screen rework sheet for selected symbol:frame slots")
    rw.add_argument("--work", type=Path, default=DEFAULT_WORK_DIR)
    rw.add_argument("--name", default="rework1")
    rw.add_argument("--cols", type=int, default=3)
    rw.add_argument("--cell-size", type=int, default=340)
    rw.add_argument("--slots", nargs="+", required=True, help="Slots as symbol:frame or symbol:frame=note")

    pck = sub.add_parser("pack", help="Pack Gemini's 6 sheets + derived/rework sheets into skin_gemini.zip")
    pck.add_argument("--work", type=Path, default=DEFAULT_WORK_DIR)
    pck.add_argument("--out", type=Path, default=DEFAULT_OUT_ZIP)
    pck.add_argument("--build-name", default=DEFAULT_BUILD_NAME)
    pck.add_argument("--offsets", type=Path, default=None)
    pck.add_argument("--icon", type=Path, default=DEFAULT_ICON_PATH)

    ref = sub.add_parser("refresh", help="One-shot repack skin_gemini.zip + regenerate review.html + skin_tuner.html + anim_player.html")
    ref.add_argument("--work", type=Path, default=DEFAULT_WORK_DIR)
    ref.add_argument("--out", type=Path, default=DEFAULT_OUT_ZIP)
    ref.add_argument("--build-name", default=DEFAULT_BUILD_NAME)
    ref.add_argument("--offsets", type=Path, default=None)
    ref.add_argument("--icon", type=Path, default=DEFAULT_ICON_PATH)
    ref.add_argument("--base", type=Path, default=DEFAULT_VANILLA_ZIP.parent / "wilson.zip")
    ref.add_argument("--ref", type=Path, default=DEFAULT_VANILLA_ZIP)
    ref.add_argument("--suite", choices=("compact", "core", "full", "generic"), default="compact")
    ref.add_argument("--include-mount", action="store_true")
    ref.add_argument("--scale", type=float, default=1.45)

    args = parser.parse_args()
    if args.cmd == "split-ref":
        outputs = split_reference_sheet(args.src, args.portrait, args.out)
        for k, p in outputs.items():
            print(f"{k}: {p}")
    elif args.cmd == "extract":
        out = extract_vanilla(args.vanilla_zip, args.out)
        print(f"Extracted Gemini 6-sheet templates into {out}")
    elif args.cmd == "derive":
        img_p, lay_p = derive_sprites(args.work)
        print(f"Synthesized derived Gemini sprites: {img_p} ({lay_p})")
    elif args.cmd == "rework":
        items: list[tuple[str, int, str]] = []
        for raw in args.slots:
            spec, _, note = raw.partition("=")
            sym, _, fstr = spec.partition(":")
            items.append((sym.strip(), int(fstr.strip()), note.strip()))
        img_p, lay_p = prepare_rework(args.work, items, name=args.name, cols=args.cols, cell_size=args.cell_size)
        print(f"Prepared rework sheet: {img_p} ({lay_p})")
    elif args.cmd == "pack":
        out = pack_skin(args.work, args.out, args.build_name, offsets_path=args.offsets, icon_path=args.icon)
        print(f"Packed DST skin archive: {out} ({out.stat().st_size} bytes)")
    elif args.cmd == "refresh":
        try:
            from tools import skin_player, skin_preview, skin_tuner
        except ImportError:
            import skin_player  # type: ignore[no-redef]
            import skin_preview  # type: ignore[no-redef]
            import skin_tuner  # type: ignore[no-redef]
        skin_dir = _skin_root_from_work(args.work)
        packed = pack_skin(args.work, args.out, args.build_name, offsets_path=args.offsets, icon_path=args.icon)
        print(f"[1/4] Packed DST skin archive: {packed} ({packed.stat().st_size} bytes)")
        res = skin_preview.render_animation_review(
            base_zip=args.base,
            skin_zip=packed,
            ref_zip=args.ref,
            out_dir=skin_dir / "review_data",
            suite=args.suite,
            include_mount=args.include_mount,
            scale=args.scale,
            html_out=skin_dir / "review.html",
        )
        cov = res["summary"]["coverage"]
        print(f"[2/4] Review HTML: {res['review_html']} ({res['summary']['total_rendered']} frames, coverage {cov['coverage_pct']}%)")
        src_offsets = args.offsets or (skin_dir / "offsets.json")
        tuner_out = skin_tuner.build_tuner_html(
            skin_zip=packed,
            out_html=skin_dir / "skin_tuner.html",
            base_zip=args.base,
            ref_zip=args.ref,
            offsets_json=src_offsets if src_offsets.is_file() else (args.work / "offsets.json"),
            suite=args.suite,
            include_mount=args.include_mount,
        )
        print(f"[3/4] Tuner HTML:  {tuner_out}")
        player_out = skin_player.build_player_html(
            skin_zip=packed,
            out_html=skin_dir / "anim_player.html",
            base_zip=args.base,
            ref_zip=args.ref,
            anim_dir=args.base.parent,
            suite=args.suite,
            include_mount=args.include_mount,
        )
        print(f"[4/4] Player HTML: {player_out}")


if __name__ == "__main__":
    main()
