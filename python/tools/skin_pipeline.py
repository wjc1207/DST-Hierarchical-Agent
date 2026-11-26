"""DST character skin asset pipeline: unpack vanilla build.bin/KTEX, build green-screen sheets, and pack skins."""
from __future__ import annotations

import argparse
from collections import deque
import colorsys
import io
import json
from pathlib import Path
import re
import struct
import zipfile

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VANILLA_ZIP = Path(
    "C:/Program Files (x86)/Steam/steamapps/common/Don't Starve Together/data/anim/wendy.zip"
)
DEFAULT_ICON_PATH = ROOT / "assets" / "logo-character.png"

CORE_BODY_SYMBOLS = (
    "headbase",
    "headbase_hat",
    "hairpigtails",
    "torso",
    "torso_pelvis",
    "skirt",
    "arm_upper",
    "arm_lower",
    "leg",
    "foot",
    "face",
    "cheeks",
)

# Representative 10 key face indices in vanilla wendy.zip (verified against wendy_face_contact.png):
# 0=front neutral, 2=front blink/squint, 4=side open, 6=side blink, 9=front talk/open mouth,
# 11=peaceful closed/sleep, 13=dizzy/KO (X_X), 15=happy smile, 21=angry glare, 25=scream/grimace.
# Frame 14 (soft glow eyes) is preserved procedurally from vanilla without AI green-screen keying.
DEFAULT_KEY_FACES: tuple[int, ...] = (0, 2, 4, 6, 9, 11, 13, 15, 21, 25)
DEFAULT_FACE_MAP: dict[int, int] = {
    0: 0, 1: 0, 2: 2, 3: 0,
    4: 4, 5: 4, 6: 6, 7: 4,
    8: 0, 9: 9, 10: 9,
    11: 11, 12: 13, 13: 13,
    14: 14,
    15: 15, 16: 9, 17: 11, 18: 11, 19: 0,
    20: 21, 21: 21, 22: 21, 23: 25, 24: 9,
    25: 25, 26: 25, 27: 25, 28: 0, 29: 25, 30: 21, 31: 13, 32: 25,
}

# DeepSeek skin palette target (warm fair skin matching assets/deepseek-1.png)
TARGET_SKIN_RGB = (247, 221, 208)

# Terminal single-joint limbs whose ankle/wrist joint sits at local (0, 0);
# scale them around (0, 0) rather than around bbox center so scaling never pulls joints apart.
PIVOT_SCALED_SYMBOLS = frozenset({"hand", "foot"})


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
        # Klei stores textures bottom-up with premultiplied alpha.
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
            # DST build coordinates: Y points downward, so y0 < y1 maps to v_top > v_bot.
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
    """Compose a pure #00FF00 labeled grid sheet and return (image, layout_metadata)."""
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
        # Draw header strip
        draw.rectangle([cx0 + border, cy0 + border, cx0 + cell_w - border - 1, cy0 + border + label_h - 1], fill=(20, 20, 20, 255))
        draw.text((cx0 + border + 6, cy0 + border + 4), entry["label"], fill=(255, 255, 255, 255), font=font)

        # Draw pure #00FF00 green cell interior
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

        # Anchor inside cell: headbase/headbase_hat sit slightly lower so ahoge/ears fit above.
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
        cells_meta.append({
            "label": entry["label"],
            "symbol": sym,
            "frame_idx": entry["frame_idx"],
            "content_rect": [gx0, gy0, gx1, gy1],
            "ref_sprite_rect": [rx0, ry0, rx0 + dw, ry0 + dh],
            "cell_scale": scale,
            "orig_tight_size": [tw, th],
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


def render_assembled_preview(
    symbols: dict[str, list[dict]],
    sprites_by_key: dict[tuple[str, int], Image.Image],
    title_label: str = "Wendy vs Skin",
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

        # Approximate Klei character hierarchy offsets in DST world units (1 DST unit ~ 1 px at 1x)
        # Body layers from back to front depending on view
        layers: list[tuple[str, int, float, float]] = []
        if hb_i == 2:
            # Back view: legs/feet -> skirt/pelvis -> torso -> arms -> head -> hairpigtails in front!
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

    panels: list[Image.Image] = []
    font = _load_font(16)
    for pose in views:
        panels.append(draw_pose(symbols, sprites_by_key, pose))
    if vanilla_symbols and vanilla_sprites:
        for pose in views:
            panels.append(draw_pose(vanilla_symbols, vanilla_sprites, pose))

    cols = 3
    rows = 2 if (vanilla_symbols and vanilla_sprites) else 1
    out = Image.new("RGBA", (320 * cols, 420 * rows + 32), (28, 28, 32, 255))
    draw = ImageDraw.Draw(out)
    draw.text((12, 6), title_label, fill=(255, 255, 255, 255), font=font)
    for idx, panel in enumerate(panels):
        r = idx // cols
        c = idx % cols
        out.paste(panel, (c * 320, 32 + r * 420))
    return out


def extract_vanilla(
    vanilla_zip: Path,
    out_dir: Path,
    *,
    key_faces: tuple[int, ...] = DEFAULT_KEY_FACES,
) -> Path:
    """Extract vanilla wendy.zip sprites + manifest + 3 compact green-screen sheets."""
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
        "key_faces": list(key_faces),
        "symbols": manifest_symbols,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Contact sheet of all 33 face frames for manual/visual inspection
    if "face" in symbols:
        cols, cw, ch = 6, 160, 140
        rows = (len(symbols["face"]) + cols - 1) // cols
        contact = Image.new("RGBA", (cols * cw, rows * ch), (220, 220, 220, 255))
        cdraw = ImageDraw.Draw(contact)
        cfont = _load_font(14)
        for idx, frame_meta in enumerate(manifest_symbols["face"]):
            r, c = idx // cols, idx % cols
            x0, y0 = c * cw, r * ch
            cdraw.rectangle([x0, y0, x0 + cw - 1, y0 + ch - 1], outline=(80, 80, 80, 255))
            cdraw.text((x0 + 6, y0 + 4), f"face:{idx}", fill=(15, 15, 15, 255), font=cfont)
            spr = extracted_sprites[("face", idx)]
            sx = x0 + (cw - spr.width) // 2
            sy = y0 + 24 + (ch - 26 - spr.height) // 2
            contact.alpha_composite(spr, (sx, sy))
        contact.save(out_dir / "wendy_face_contact.png")

    def make_entries(specs: list[tuple[str, list[int], float]]) -> list[dict]:
        items: list[dict] = []
        for sym, indices, scale in specs:
            for idx in indices:
                meta = manifest_symbols[sym][idx]
                items.append({
                    "label": f"{sym}:{idx}",
                    "symbol": sym,
                    "frame_idx": idx,
                    "sprite": extracted_sprites[(sym, idx)],
                    "tight_bbox": tuple(meta["tight_bbox"]),
                    "scale": scale,
                })
        return items

    sheet1_entries = make_entries([
        ("headbase", list(range(len(symbols["headbase"]))), 1.0),
        ("hairpigtails", list(range(len(symbols["hairpigtails"]))), 1.0),
        ("face", list(key_faces), 1.35),
        ("cheeks", list(range(len(symbols["cheeks"]))), 1.8),
    ])
    sheet2_entries = make_entries([
        ("torso", list(range(len(symbols["torso"]))), 1.85),
        ("torso_pelvis", list(range(len(symbols["torso_pelvis"]))), 1.85),
        ("skirt", list(range(len(symbols["skirt"]))), 1.65),
    ])
    sheet3_entries = make_entries([
        ("arm_upper", list(range(len(symbols["arm_upper"]))), 2.2),
        ("arm_lower", list(range(len(symbols["arm_lower"]))), 2.2),
        ("leg", list(range(len(symbols["leg"]))), 2.2),
        ("foot", list(range(len(symbols["foot"]))), 2.5),
    ])

    s1_img, s1_layout = _compose_sheet("sheet1_head_face", sheet1_entries, cols=6, cell_content_w=252, cell_content_h=252)
    s2_img, s2_layout = _compose_sheet("sheet2_body_skirt", sheet2_entries, cols=6, cell_content_w=228, cell_content_h=228)
    s3_img, s3_layout = _compose_sheet("sheet3_limbs", sheet3_entries, cols=7, cell_content_w=192, cell_content_h=192)

    s1_img.save(out_dir / "sheet1_head_face.png")
    s2_img.save(out_dir / "sheet2_body_skirt.png")
    s3_img.save(out_dir / "sheet3_limbs.png")

    layouts = {
        "sheet1_head_face": s1_layout,
        "sheet2_body_skirt": s2_layout,
        "sheet3_limbs": s3_layout,
    }
    (out_dir / "sheets_layout.json").write_text(json.dumps(layouts, indent=2), encoding="utf-8")

    # Also save vanilla Wendy assembled preview as baseline
    vanilla_preview = render_assembled_preview(manifest_symbols, extracted_sprites, title_label="Vanilla Wendy Baseline")
    vanilla_preview.save(out_dir / "wendy_assembled.png")
    return out_dir


def is_chroma_green(r: int, g: int, b: int, *, strict: bool = False) -> bool:
    """Detect green-screen pixels while strictly protecting cyan-blue (#55B8E0) hues."""
    # Cyan/blue has blue >= green or blue close to green; true green-screen has strong green excess.
    green_excess = g - max(r, b)
    if strict:
        # Strict check safe for isolated interior holes (e.g. between curls or skirt folds)
        if green_excess < 48 or g < 110:
            return False
    else:
        if green_excess < 20 or g < 75:
            return False
    hue, sat, val = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    deg = hue * 360.0
    # True green occupies ~75..158 deg; cyan-blue hair starts >180 deg.
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

    # Also catch enclosed gaps inside hair curls / skirt corners with strict pure-green threshold
    for y in range(h):
        for x in range(w):
            idx = y * w + x
            if not removed[idx]:
                r, g, b, a = pixels[x, y]
                if a > 0 and is_chroma_green(r, g, b, strict=True):
                    removed[idx] = 1

    # Clear background and despill 2px green fringe touching removed pixels
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
                    if near == 1:
                        new_a = max(0, a - min(a, int(excess * 2.2)))
                    else:
                        new_a = a
                    out_pixels[x, y] = (r, new_g, b, new_a)

    # Remove tiny 1-2px isolated speckles if far smaller than main sprite
    return rgba


def recolor_skin_sprite(sprite: Image.Image, target_rgb: tuple[int, int, int] = TARGET_SKIN_RGB) -> Image.Image:
    """Shift vanilla skin pixels toward target skin tone while preserving dark charcoal outlines."""
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
                # Preserve ink linework slightly warmed
                px[x, y] = (min(255, int(r * 0.95)), min(255, int(g * 0.92)), min(255, int(b * 0.95)), a)
            else:
                blend = 0.72
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
    """Fade top fraction of an RGBA sprite's alpha so hair roots blend cleanly into headbase."""
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

    # Softly clamp extreme AI scale drift per category so frames stay consistent in motion
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

    # Anchor selection prevents tall ahoge/hats or flowing tails from pushing joints out of place
    dx_off, dy_off, extra_scale = _parse_offset_entry(extra_offset)
    if symbol in {"headbase", "headbase_hat"}:
        # Lock chin/neck bottom to vanilla bottom so hair accessories grow upward
        new_cx = orig_dst_cx + dx_off
        new_cy = (orig_dst_bot - dst_h / 2.0) + dy_off
    elif symbol in {"hairpigtails", "torso_pelvis", "skirt"}:
        # Lock crown / waist seam to top so long curls & skirt tails extend downward
        new_cx = orig_dst_cx + dx_off
        new_cy = (orig_dst_top + dst_h / 2.0) + dy_off
    elif symbol in PIVOT_SCALED_SYMBOLS:
        # Scale terminal hands/feet anchored at local (0, 0) wrist/ankle pivot
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
            tw = max(2, min(img.width, round(dw * supersample))) if supersample > 0 else img.width
            th = max(2, min(img.height, round(dh * supersample))) if supersample > 0 else img.height
            # Always keep at least original DST size (1.0x) unless scaled down cleanly
            target_w = max(2, round(dw * supersample))
            target_h = max(2, round(dh * supersample))
            # Don't upscale blurry pixels beyond source cell resolution
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

    # Start safely inside THIS cell's dark header strip (~6% height) and outer borders,
    # then scan inward until reaching pure #00FF00 green. Neither black header nor white text triggers is_bg_green.
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

    probe_ys = [
        min(gy1 - 1, gy0 + max(4, int((gy1 - gy0) * f)))
        for f in (0.12, 0.88)
    ]
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


def prepare_rework(
    work_dir: Path,
    items: list[tuple[str, int, str]],
    *,
    name: str = "rework1",
    cols: int = 3,
    cell_size: int = 340,
) -> tuple[Path, Path]:
    """Compose a high-res focused green-screen rework sheet for selected symbol:frame slots."""
    manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest_symbols = manifest["symbols"]
    sprites_dir = work_dir / "sprites"

    entries: list[dict] = []
    for sym, fidx, note in items:
        if sym not in manifest_symbols or fidx >= len(manifest_symbols[sym]):
            raise ValueError(f"Unknown slot {sym}:{fidx}")
        meta = manifest_symbols[sym][fidx]
        sp_path = sprites_dir / f"{sym}_{fidx:02d}.png"
        with Image.open(sp_path) as sp_raw:
            sprite = sp_raw.convert("RGBA")
        label = f"{sym}:{fidx} ({note})" if note else f"{sym}:{fidx}"
        entries.append({
            "label": label,
            "symbol": sym,
            "frame_idx": fidx,
            "sprite": sprite,
            "tight_bbox": tuple(meta["tight_bbox"]),
            "fill_ratio": 0.78,
        })

    sheet_img, layout = _compose_sheet(
        name,
        entries,
        cols,
        cell_size,
        cell_size,
    )
    img_path = work_dir / f"{name}_template.png"
    layout_path = work_dir / f"{name}_layout.json"
    sheet_img.save(img_path)
    layout_path.write_text(json.dumps(layout, indent=2), encoding="utf-8")
    return img_path, layout_path


REWORK_CELL_PASS_CRITERIA: dict[str, dict[str, object]] = {
    "correct_part": {"pass": True, "description": "部件类别与槽位正确"},
    "view_and_pose_match": {"pass": True, "description": "朝向与表情/姿势核心特征（如大小眼、惊恐、闭眼）与模板一致"},
    "art_style_matches_reference": {"pass": True, "description": "严格符合角色原版设定图的二次元光滑赛璐璐画风，非像素风/欧美卡通风"},
    "contains_adjacent_parts": {"pass": False, "description": "是否混入相邻部件或多余肤色头底/背景"},
    "outline_within_cell": {"pass": True, "description": "外轮廓完全在纯绿格内且未裁切"},
    "background_drawn_as_part": {"pass": False, "description": "绿底是否被误画成零件的一部分"},
    "edge_artifacts": {"pass": False, "description": "边缘是否有杂绿、像素锯齿、黑块或碎色"},
}


def prepare_rework_from_plan(
    work_dir: Path,
    plan_path: Path,
    *,
    name: str = "rework_plan",
    cols: int | None = None,
    cell_size: int = 340,
) -> tuple[Path, Path, Path]:
    """Build a focused rework template + structured per-cell prompt.txt from an exported rework_plan.json."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    raw_items = plan.get("items") or []
    if not raw_items:
        raise ValueError(f"Rework plan {plan_path} contains no items")

    parsed_items: list[tuple[str, int, str]] = []
    plan_details: list[dict] = []
    for entry in raw_items:
        if "symbol" in entry and "frame" in entry:
            sym = str(entry["symbol"]).strip()
            fidx = int(entry["frame"])
        else:
            slot = str(entry.get("slot") or entry.get("id") or "").strip()
            sym, _, fstr = slot.partition(":")
            fidx = int(fstr.strip())
        note = str(entry.get("note") or entry.get("label") or "").strip()
        issue = str(entry.get("issue") or "").strip()
        expected = str(entry.get("expected") or "").strip()
        parsed_items.append((sym, fidx, note))
        plan_details.append({
            "slot": f"{sym}:{fidx}",
            "symbol": sym,
            "frame": fidx,
            "note": note,
            "issue": issue,
            "expected": expected,
        })

    eff_cols = cols if (cols is not None and cols > 0) else min(3, max(1, len(parsed_items)))
    img_path, layout_path = prepare_rework(
        work_dir,
        parsed_items,
        name=name,
        cols=eff_cols,
        cell_size=cell_size,
    )

    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    rows = layout.get("rows", 1)
    actual_cols = layout.get("cols", eff_cols)
    lines = [
        f"IMAGE 1 is a {actual_cols}x{rows} labeled DST sprite rework template on pure #00FF00 green screen.",
        "IMAGE 2 is the approved character reference sheet.",
        "Redraw ONLY the listed sprite cells to match IMAGE 2's art style, palette, and proportions.",
        "Keep every cell strictly separated inside its own #00FF00 green box; preserve the black header bar, white labels, grid borders, and pose/orientation of IMAGE 1.",
        "Do NOT add neighboring body parts, full bodies, extra props, lightning VFX, or skin ovals on face cells.",
        "",
    ]
    for idx, (cell, detail) in enumerate(zip(layout.get("cells", []), plan_details), start=1):
        r = int(cell.get("row", (idx - 1) // actual_cols)) + 1
        c = int(cell.get("col", (idx - 1) % actual_cols)) + 1
        line = f"CELL #{idx} (row {r}, col {c}) `{detail['slot']}`:"
        parts = []
        if detail["issue"]:
            parts.append(f"Observed issue: {detail['issue']}")
        if detail["expected"]:
            parts.append(f"Expected fix: {detail['expected']}")
        if not parts:
            parts.append("Match the reference style while keeping the exact silhouette orientation and pose of IMAGE 1.")
        lines.append(f"{line} " + " | ".join(parts))

    prompt_path = work_dir / f"{name}_prompt.txt"
    prompt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    review_example = {
        "schema": "dst_skin_rework_cell_review_v1",
        "rework_name": name,
        "pass_criteria": REWORK_CELL_PASS_CRITERIA,
        "cells": {
            detail["slot"]: {
                "index": idx,
                "row": int(cell.get("row", (idx - 1) // actual_cols)) + 1,
                "col": int(cell.get("col", (idx - 1) % actual_cols)) + 1,
                "issue": detail["issue"],
                "expected": detail["expected"],
                "approved": False,
                "checks": {k: None for k in REWORK_CELL_PASS_CRITERIA},
                "notes": "",
            }
            for idx, (cell, detail) in enumerate(zip(layout.get("cells", []), plan_details), start=1)
        },
    }
    (work_dir / f"{name}_review.example.json").write_text(
        json.dumps(review_example, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return img_path, layout_path, prompt_path


def _slice_sheet_cells(
    sheet_img: Image.Image,
    layout: dict,
) -> dict[tuple[str, int], tuple[Image.Image, dict]]:
    """Slice a green-screen sheet without non-uniform stretching even if AI aspect ratio differs from template."""
    cols = max(1, int(layout["cols"]))
    rows = max(1, int(layout["rows"]))
    tmpl_cell_w = float(layout["width"]) / cols
    tmpl_cell_h = float(layout["height"]) / rows
    header_h = float(layout.get("header_h", 28))
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
        # Preserve native unstretched aspect ratio using the tighter dimension scale
        uniform_factor = min(actual_gw / tmpl_green_w, actual_gh / tmpl_green_h)
        clean = remove_green_screen(sheet_img.crop(crop_box))
        adj_cell = dict(cell)
        adj_cell["cell_scale"] = float(cell["cell_scale"]) * max(1e-3, uniform_factor)
        out[(cell["symbol"], int(cell["frame_idx"]))] = (clean, adj_cell)
    return out


def _skin_root_from_work(work_dir: Path) -> Path:
    """Resolve skins/<name> root from either skins/<name>/work or legacy work_dir."""
    if work_dir.name == "work":
        return work_dir.parent
    return ROOT / "skins" / work_dir.name


def _rework_sort_key(path: Path) -> tuple[int, str]:
    """Natural sort key so rework10/rework11 apply after rework2..rework9."""
    match = re.search(r"rework(\d+)", path.stem, re.IGNORECASE)
    return (int(match.group(1)) if match else 0, path.name)


def pack_skin(
    work_dir: Path,
    out_zip: Path,
    build_name: str,
    *,
    sheet1_path: Path | None = None,
    sheet2_path: Path | None = None,
    sheet3_path: Path | None = None,
    rework_sheets: list[tuple[Path, Path]] | None = None,
    offsets_path: Path | None = None,
    icon_path: Path = DEFAULT_ICON_PATH,
    face_map: dict[int, int] = DEFAULT_FACE_MAP,
) -> Path:
    """Slice AI-generated green-screen sheets, align pivots, pack atlas + build.bin, and render QA preview."""
    manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))
    layouts = json.loads((work_dir / "sheets_layout.json").read_text(encoding="utf-8"))
    manifest_symbols = manifest["symbols"]
    sprites_dir = work_dir / "sprites"

    skin_offsets = _skin_root_from_work(work_dir) / "offsets.json"
    offsets_file = offsets_path or (skin_offsets if skin_offsets.is_file() else (work_dir / "offsets.json"))
    extra_offsets: dict[str, list[float]] = (
        json.loads(offsets_file.read_text(encoding="utf-8")) if offsets_file.is_file() else {}
    )

    sheet_inputs = {
        "sheet1_head_face": sheet1_path or (work_dir / "sheet1_ai.png"),
        "sheet2_body_skirt": sheet2_path or (work_dir / "sheet2_ai.png"),
        "sheet3_limbs": sheet3_path or (work_dir / "sheet3_ai.png"),
    }

    sliced_clean: dict[tuple[str, int], tuple[Image.Image, dict]] = {}
    for sheet_id, layout in layouts.items():
        ai_path = sheet_inputs[sheet_id]
        if not ai_path.is_file():
            raise FileNotFoundError(f"Missing AI sheet: {ai_path}")
        with Image.open(ai_path) as raw_sheet:
            sheet_img = raw_sheet.convert("RGBA")
        sliced_clean.update(_slice_sheet_cells(sheet_img, layout))

    # Apply focused rework sheets (explicit or auto-discovered rework*_layout.json + rework*_ai.png)
    rework_pairs: list[tuple[Path, Path]] = list(rework_sheets or [])
    if not rework_pairs:
        for lp in sorted(work_dir.glob("rework*_layout.json"), key=_rework_sort_key):
            stem = lp.name[:-len("_layout.json")]
            ai_candidate = work_dir / f"{stem}_ai.png"
            if ai_candidate.is_file():
                rework_pairs.append((lp, ai_candidate))
    for lp, ai_path in rework_pairs:
        rlayout = json.loads(lp.read_text(encoding="utf-8"))
        with Image.open(ai_path) as raw_rework:
            rsheet = raw_rework.convert("RGBA")
        sliced_clean.update(_slice_sheet_cells(rsheet, rlayout))

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

    # 1. Core AI-generated symbols
    for sym in ("headbase", "headbase_hat", "hairpigtails", "torso", "torso_pelvis", "skirt", "arm_upper", "arm_lower", "leg", "foot", "cheeks"):
        for idx, orig in enumerate(manifest_symbols[sym]):
            if sym == "headbase_hat" and (sym, idx) not in sliced_clean:
                # Derive headbase_hat directly from headbase by cropping the top headband/ahoge region
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
                cx = float(base_frame["cx"]) + dx_off
                cy = (base_bot - dh / 2.0) + dy_off
                add_frame(sym, idx, f"{sym}:{idx}", cropped_hat, cx, cy, dw * extra_scale, dh * extra_scale)
                continue
            clean_cell, cell_meta = sliced_clean[(sym, idx)]
            off = _parse_offset_entry(extra_offsets.get(f"{sym}:{idx}", extra_offsets.get(sym, (0.0, 0.0, 1.0))))
            cropped, cx, cy, dw, dh = _align_sprite_to_vanilla(clean_cell, cell_meta, orig, sym, idx, extra_offset=off)
            if sym == "hairpigtails" and idx in (0, 1) and cropped.height > 8:
                cropped = _feather_top_alpha(cropped, 0.24, smoothstep=True)
            add_frame(sym, idx, f"{sym}:{idx}", cropped, cx, cy, dw, dh)

    # 2. Faces: align the 10 key generated expressions, then map all 33 vanilla face slots
    face_aligned: dict[int, tuple[Image.Image, float, float, float, float]] = {}
    for key_idx in manifest["key_faces"]:
        orig = manifest_symbols["face"][key_idx]
        clean_cell, cell_meta = sliced_clean[("face", key_idx)]
        off = _parse_offset_entry(extra_offsets.get(f"face:{key_idx}", extra_offsets.get("face", (0.0, 0.0, 1.0))))
        face_aligned[key_idx] = _align_sprite_to_vanilla(clean_cell, cell_meta, orig, "face", key_idx, extra_offset=off)

    for idx, orig in enumerate(manifest_symbols["face"]):
        if ("face", idx) in sliced_clean and idx not in manifest["key_faces"]:
            clean_cell, cell_meta = sliced_clean[("face", idx)]
            off = _parse_offset_entry(extra_offsets.get(f"face:{idx}", extra_offsets.get("face", (0.0, 0.0, 1.0))))
            img, cx, cy, dw, dh = _align_sprite_to_vanilla(clean_cell, cell_meta, orig, "face", idx, extra_offset=off)
            add_frame("face", idx, f"face:{idx}", img, cx, cy, dw, dh)
            continue
        if idx == 14 and 14 not in manifest["key_faces"]:
            # Preserve soft vanilla glow eyes (frame 14) directly without green-screen roundtrip.
            with Image.open(sprites_dir / "face_14.png") as raw_glow:
                glow = raw_glow.convert("RGBA")
            tx0, ty0, tx1, ty1 = orig["tight_bbox"]
            tight = glow.crop((tx0, ty0, tx1, ty1))
            dx_off, dy_off, extra_scale = _parse_offset_entry(
                extra_offsets.get("face:14", extra_offsets.get("face", (0.0, 0.0, 1.0)))
            )
            cx = float(orig["cx"]) - float(orig["w"]) / 2.0 + (tx0 + tx1) / 2.0 + dx_off
            cy = float(orig["cy"]) - float(orig["h"]) / 2.0 + (ty0 + ty1) / 2.0 + dy_off
            add_frame("face", idx, "face:14_vanilla", tight, cx, cy, float(tx1 - tx0) * extra_scale, float(ty1 - ty0) * extra_scale)
            continue
        mapped_key = face_map.get(idx, manifest["key_faces"][0])
        if mapped_key not in face_aligned:
            mapped_key = manifest["key_faces"][0]
        img, cx, cy, dw, dh = face_aligned[mapped_key]
        # Preserve each vanilla face slot's own relative offset (e.g. side vs front face position)
        ref_orig = manifest_symbols["face"][mapped_key]
        dx = float(orig["cx"]) - float(ref_orig["cx"])
        dy = float(orig["cy"]) - float(ref_orig["cy"])
        scale_w = float(orig["w"]) / max(1.0, float(ref_orig["w"]))
        scale_h = float(orig["h"]) / max(1.0, float(ref_orig["h"]))
        # Apply per-slot offset override for derived face frames (idempotent with skin_tuner.py)
        slot_dx, slot_dy, slot_sc = (0.0, 0.0, 1.0)
        if idx != mapped_key and f"face:{idx}" in extra_offsets:
            slot_dx, slot_dy, slot_sc = _parse_offset_entry(extra_offsets[f"face:{idx}"])
        add_frame(
            "face",
            idx,
            f"face:{mapped_key}",
            img,
            cx + dx + slot_dx,
            cy + dy + slot_dy,
            dw * scale_w * slot_sc,
            dh * scale_h * slot_sc,
        )

    # 3. Procedural wide-body slots reusing generated torso/pelvis/skirt tiles with vanilla wide scale
    for wide_sym, base_sym in (("torso_wide", "torso"), ("torso_pelvis_wide", "torso_pelvis"), ("skirt_wide", "skirt")):
        for idx, orig in enumerate(manifest_symbols[wide_sym]):
            base_orig = manifest_symbols[base_sym][idx]
            base_frame = build_symbols[base_sym][idx]
            sw = float(orig["w"]) / max(1.0, float(base_orig["w"]))
            sh = float(orig["h"]) / max(1.0, float(base_orig["h"]))
            cx = base_frame["cx"] + (float(orig["cx"]) - float(base_orig["cx"]))
            cy = base_frame["cy"] + (float(orig["cy"]) - float(base_orig["cy"]))
            add_frame(
                wide_sym,
                idx,
                base_frame["tile_key"],
                tiles[base_frame["tile_key"]],
                cx,
                cy,
                base_frame["w"] * sw,
                base_frame["h"] * sh,
            )

    # 3b. Hybrid hair routing: blank hair:0,1,3 (+ hair_hat:0,1,3) so hair never renders in front
    # of torso in 286 DST clips, while keeping hair:2 (+ hair_hat:2) as seamless back-view fill
    blank_hair = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    for idx in (0, 1, 2, 3):
        if idx == 2 and ("hair", 2) in sliced_clean:
            clean_cell, _ = sliced_clean[("hair", 2)]
            bbox = clean_cell.getchannel("A").getbbox()
            if bbox is not None:
                cropped = _feather_top_alpha(clean_cell.crop(bbox), 0.14, smoothstep=False)
                def_cx, def_cy, max_w, max_h = (0.0, 68.0, 215.0, 215.0)
                dx_off, dy_off, extra_scale = _parse_offset_entry(
                    extra_offsets.get("hair:2", extra_offsets.get("hair", (0.0, 0.0, 1.0)))
                )
                fit_scale = min(max_w / max(1.0, float(cropped.width)), max_h / max(1.0, float(cropped.height)))
                dw = float(cropped.width) * fit_scale * extra_scale
                dh = float(cropped.height) * fit_scale * extra_scale
                cx = def_cx + dx_off
                cy = def_cy + dy_off
                add_frame("hair", 2, "hair:2", cropped, cx, cy, dw, dh)
                hat_dx, hat_dy, hat_scale = _parse_offset_entry(
                    extra_offsets.get("hair_hat:2", extra_offsets.get("hair_hat", (0.0, 0.0, 1.0)))
                )
                add_frame("hair_hat", 2, "hair:2", cropped, cx + hat_dx, cy + hat_dy, dw * hat_scale, dh * hat_scale)
                continue
        add_frame("hair", idx, "long_hair_blank:0", blank_hair, 0.0, 0.0, 4.0, 4.0)
        add_frame("hair_hat", idx, "long_hair_blank:0", blank_hair, 0.0, 0.0, 4.0, 4.0)

    # 3c. Optional animated whale tail (tail:0 normal, tail:6 X-ray, tail:1..5 & 7..11 blank)
    if ("tail", 0) in sliced_clean:
        blank_tail = Image.new("RGBA", (4, 4), (0, 0, 0, 0))

        def _pack_whale_tail_sprite(clean_cell: Image.Image, extra_off: tuple[float, float, float]) -> tuple[Image.Image, float, float, float, float]:
            bbox = clean_cell.getchannel("A").getbbox()
            if bbox is None:
                return blank_tail, 0.0, 0.0, 4.0, 4.0
            cropped = clean_cell.crop(bbox)
            pre_rot = cropped.rotate(-22.0, resample=Image.Resampling.BICUBIC, expand=True)
            rbox = pre_rot.getchannel("A").getbbox()
            if rbox is not None:
                pre_rot = pre_rot.crop(rbox)
            dx_off, dy_off, extra_scale = _parse_offset_entry(extra_off)
            base_fit = 148.0 / max(1.0, float(max(cropped.width, cropped.height)))
            dw = float(pre_rot.width) * base_fit * extra_scale
            dh = float(pre_rot.height) * base_fit * extra_scale
            cx = -dw * 0.44 + dx_off
            cy = -dh * 0.20 + dy_off
            return pre_rot, cx, cy, dw, dh

        tail0_cell, _ = sliced_clean[("tail", 0)]
        t0_img, t0_cx, t0_cy, t0_w, t0_h = _pack_whale_tail_sprite(
            tail0_cell,
            extra_offsets.get("tail:0", extra_offsets.get("tail", (0.0, 0.0, 1.0))),
        )
        tail6_key = ("tail", 6) if ("tail", 6) in sliced_clean else ("tail", 0)
        tail6_cell, _ = sliced_clean[tail6_key]
        t6_img, t6_cx, t6_cy, t6_w, t6_h = _pack_whale_tail_sprite(
            tail6_cell,
            extra_offsets.get("tail:6", extra_offsets.get("tail:0", extra_offsets.get("tail", (0.0, 0.0, 1.0)))),
        )
        for tidx in range(12):
            if tidx == 0:
                add_frame("tail", 0, "tail:0", t0_img, t0_cx, t0_cy, t0_w, t0_h)
            elif tidx == 6:
                add_frame("tail", 6, "tail:6", t6_img, t6_cx, t6_cy, t6_w, t6_h)
            else:
                add_frame("tail", tidx, "long_hair_blank:0", blank_tail, 0.0, 0.0, 4.0, 4.0)

    # 4. Hand & arm_upper_skin: prefer AI-generated rework cells when provided, else recolor vanilla
    for skin_sym in ("hand", "arm_upper_skin"):
        for idx, orig in enumerate(manifest_symbols[skin_sym]):
            if (skin_sym, idx) in sliced_clean:
                clean_cell, cell_meta = sliced_clean[(skin_sym, idx)]
                off = _parse_offset_entry(
                    extra_offsets.get(f"{skin_sym}:{idx}", extra_offsets.get(skin_sym, (0.0, 0.0, 1.0)))
                )
                cropped, cx, cy, dw, dh = _align_sprite_to_vanilla(clean_cell, cell_meta, orig, skin_sym, idx, extra_offset=off)
                add_frame(skin_sym, idx, f"{skin_sym}:{idx}", cropped, cx, cy, dw, dh)
                continue
            with Image.open(sprites_dir / f"{skin_sym}_{idx:02d}.png") as raw_spr:
                recolored = recolor_skin_sprite(raw_spr)
            tx0, ty0, tx1, ty1 = orig["tight_bbox"]
            tight = recolored.crop((tx0, ty0, tx1, ty1))
            orig_fw, orig_fh = float(orig["w"]), float(orig["h"])
            dx_off, dy_off, extra_scale = _parse_offset_entry(
                extra_offsets.get(f"{skin_sym}:{idx}", extra_offsets.get(skin_sym, (0.0, 0.0, 1.0)))
            )
            base_cx = float(orig["cx"]) - orig_fw / 2.0 + (tx0 + tx1) / 2.0
            base_cy = float(orig["cy"]) - orig_fh / 2.0 + (ty0 + ty1) / 2.0
            if skin_sym in PIVOT_SCALED_SYMBOLS:
                cx = base_cx * extra_scale + dx_off
                cy = base_cy * extra_scale + dy_off
            else:
                cx = base_cx + dx_off
                cy = base_cy + dy_off
            add_frame(skin_sym, idx, f"{skin_sym}:{idx}", tight, cx, cy, float(tx1 - tx0) * extra_scale, float(ty1 - ty0) * extra_scale)

    # 5. long_hair_blank (hides Wilson's native spiky hair & beard cleanly)
    blank_img = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    add_frame("long_hair_blank", 0, "long_hair_blank:0", blank_img, 0.0, 0.0, 4.0, 4.0)

    # 6. SWAP_ICON
    if icon_path.is_file():
        with Image.open(icon_path) as raw_icon:
            icon_img = ImageOps.contain(raw_icon.convert("RGBA"), (240, 240), Image.Resampling.LANCZOS)
    else:
        icon_img = Image.new("RGBA", (128, 128), (60, 95, 168, 255))
    add_frame("SWAP_ICON", 0, "SWAP_ICON:0", icon_img, 0.0, 0.0, float(icon_img.width), float(icon_img.height))

    # Shelf-pack onto 2048x2048 lossless RGBA atlas, trying high supersampling first for crisp ink lines
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

    # Side-by-side assembled QA preview (top row = generated skin, bottom row = vanilla Wendy)
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

    ext = sub.add_parser("extract", help="Unpack vanilla wendy.zip into 3 compact green-screen sheets")
    ext.add_argument("--vanilla-zip", type=Path, default=DEFAULT_VANILLA_ZIP)
    ext.add_argument("--out", type=Path, default=ROOT / "skins" / "deepseek" / "work")

    rw = sub.add_parser("rework", help="Compose a focused high-res green-screen rework sheet for selected slots or a rework_plan.json")
    rw.add_argument("--work", type=Path, default=ROOT / "skins" / "deepseek" / "work")
    rw.add_argument("--name", default="rework1")
    rw.add_argument("--cols", type=int, default=3)
    rw.add_argument("--cell-size", type=int, default=340)
    rw.add_argument("--slots", nargs="+", default=None, help="Slots as symbol:frame or symbol:frame=note")
    rw.add_argument("--plan", type=Path, default=None, help="Structured rework_plan.json exported from review.html (also generates <name>_prompt.txt)")

    pck = sub.add_parser("pack", help="Slice AI sheets, clean green screen, align pivots, and pack anim zip")
    pck.add_argument("--work", type=Path, default=ROOT / "skins" / "deepseek" / "work")
    pck.add_argument("--out", type=Path, default=ROOT / "mods" / "dst_buddy_agent" / "anim" / "skin_deepseek.zip")
    pck.add_argument("--build-name", default="skin_deepseek")
    pck.add_argument("--sheet1", type=Path)
    pck.add_argument("--sheet2", type=Path)
    pck.add_argument("--sheet3", type=Path)
    pck.add_argument("--offsets", type=Path, default=None, help="Optional offsets.json path (defaults to skins/<skin>/offsets.json)")
    pck.add_argument("--icon", type=Path, default=DEFAULT_ICON_PATH)

    prv = sub.add_parser("preview", help="Render skeletal front/side/back comparison using real DST anim.bin")
    prv.add_argument("--anim", type=Path, default=DEFAULT_VANILLA_ZIP.parent / "player_idles.zip")
    prv.add_argument("--base", type=Path, default=DEFAULT_VANILLA_ZIP.parent / "wilson.zip")
    prv.add_argument("--skin", type=Path, default=ROOT / "mods" / "dst_buddy_agent" / "anim" / "skin_deepseek.zip")
    prv.add_argument("--ref", type=Path, default=DEFAULT_VANILLA_ZIP)
    prv.add_argument("--clip", default="idle_loop")
    prv.add_argument("--frame", type=int, default=0)
    prv.add_argument("--scale", type=float, default=1.12)
    prv.add_argument("--out", type=Path, default=ROOT / "skins" / "deepseek" / "preview")

    insp = sub.add_parser("inspect-anim", help="Print exact (symbol, frame_num) slots used by a DST animation clip")
    insp.add_argument("--anim", type=Path, default=DEFAULT_VANILLA_ZIP.parent / "player_idles.zip")
    insp.add_argument("--clip", default="idle_loop")
    insp.add_argument("--frame", type=int, default=0)
    insp.add_argument("--symbols", nargs="*", help="Optional filter for specific symbol names")

    tun = sub.add_parser("tuner", help="Generate standalone interactive HTML rig/offset tuner for offsets.json")
    tun.add_argument("--skin", type=Path, default=ROOT / "mods" / "dst_buddy_agent" / "anim" / "skin_deepseek.zip")
    tun.add_argument("--work", type=Path, default=ROOT / "skins" / "deepseek" / "work")
    tun.add_argument("--out", type=Path, default=None)
    tun.add_argument("--base", type=Path, default=DEFAULT_VANILLA_ZIP.parent / "wilson.zip")
    tun.add_argument("--ref", type=Path, default=DEFAULT_VANILLA_ZIP)
    tun.add_argument("--suite", choices=("compact", "core", "full", "generic"), default="compact")
    tun.add_argument("--include-mount", action="store_true")

    ply = sub.add_parser("player", help="Generate offline 30 FPS HTML5 Canvas animation player covering all clips")
    ply.add_argument("--skin", type=Path, default=ROOT / "mods" / "dst_buddy_agent" / "anim" / "skin_deepseek.zip")
    ply.add_argument("--work", type=Path, default=ROOT / "skins" / "deepseek" / "work")
    ply.add_argument("--out", type=Path, default=None)
    ply.add_argument("--base", type=Path, default=DEFAULT_VANILLA_ZIP.parent / "wilson.zip")
    ply.add_argument("--ref", type=Path, default=DEFAULT_VANILLA_ZIP)
    ply.add_argument("--suite", choices=("compact", "core", "full", "generic"), default="compact")
    ply.add_argument("--include-mount", action="store_true")
    ply.add_argument("--no-ref", action="store_true")

    gf = sub.add_parser("gif", help="Export a single animation clip as a looping animated GIF")
    gf.add_argument("--anim", type=Path, default=DEFAULT_VANILLA_ZIP.parent / "player_idles.zip")
    gf.add_argument("--clip", default="idle_loop")
    gf.add_argument("--facing", default="front")
    gf.add_argument("--skin", type=Path, default=ROOT / "mods" / "dst_buddy_agent" / "anim" / "skin_deepseek.zip")
    gf.add_argument("--base", type=Path, default=DEFAULT_VANILLA_ZIP.parent / "wilson.zip")
    gf.add_argument("--ref", type=Path, default=DEFAULT_VANILLA_ZIP)
    gf.add_argument("--out", type=Path, default=None)
    gf.add_argument("--scale", type=float, default=1.15)
    gf.add_argument("--fps", type=float, default=None)
    gf.add_argument("--no-ref", action="store_true")

    rev = sub.add_parser("review", help="Render 1 representative frame per animation clip with ref comparison, contact sheets, coverage report, and interactive review.html")
    rev.add_argument("--skin", type=Path, default=ROOT / "mods" / "dst_buddy_agent" / "anim" / "skin_deepseek.zip")
    rev.add_argument("--work", type=Path, default=ROOT / "skins" / "deepseek" / "work")
    rev.add_argument("--out", type=Path, default=None, help="Review data directory (defaults to skins/<skin>/review_data, with HTML at skins/<skin>/review.html)")
    rev.add_argument("--base", type=Path, default=DEFAULT_VANILLA_ZIP.parent / "wilson.zip")
    rev.add_argument("--ref", type=Path, default=DEFAULT_VANILLA_ZIP)
    rev.add_argument("--anim-dir", type=Path, default=DEFAULT_VANILLA_ZIP.parent)
    rev.add_argument("--suite", choices=("compact", "core", "full", "generic"), default="compact", help="'compact' (~50 curated + 100% auto-cover), 'core' (~34 frames), 'full'/'generic' (all cleaned non-character-specific DST player packs)")
    rev.add_argument("--zips", nargs="*", default=None, help="Optional list of player_*.zip names to scan (overrides default list)")
    rev.add_argument("--all-facings", action="store_true", help="In full/generic/custom mode, render every facing variant per clip instead of primary facing")
    rev.add_argument("--include-mount", action="store_true", help="In compact/full/generic mode, also include player_mount_*.zip packs")
    rev.add_argument("--cols", type=int, default=3, help="Columns per contact sheet")
    rev.add_argument("--rows", type=int, default=4, help="Rows per contact sheet")
    rev.add_argument("--scale", type=float, default=1.45)

    ref = sub.add_parser("refresh", help="One-shot repack skin zip + regenerate review.html + skin_tuner.html + anim_player.html")
    ref.add_argument("--work", type=Path, default=ROOT / "skins" / "deepseek" / "work")
    ref.add_argument("--out", type=Path, default=ROOT / "mods" / "dst_buddy_agent" / "anim" / "skin_deepseek.zip")
    ref.add_argument("--build-name", default="skin_deepseek")
    ref.add_argument("--offsets", type=Path, default=None, help="Optional offsets.json path (defaults to skins/<skin>/offsets.json)")
    ref.add_argument("--icon", type=Path, default=DEFAULT_ICON_PATH)
    ref.add_argument("--base", type=Path, default=DEFAULT_VANILLA_ZIP.parent / "wilson.zip")
    ref.add_argument("--ref", type=Path, default=DEFAULT_VANILLA_ZIP)
    ref.add_argument("--suite", choices=("compact", "core", "full", "generic"), default="compact")
    ref.add_argument("--include-mount", action="store_true")
    ref.add_argument("--scale", type=float, default=1.45)

    args = parser.parse_args()
    if args.cmd == "extract":
        out = extract_vanilla(args.vanilla_zip, args.out)
        print(f"Extracted sheets and manifest into {out}")
    elif args.cmd == "rework":
        if args.plan is not None:
            img_p, lay_p, prompt_p = prepare_rework_from_plan(
                args.work,
                args.plan,
                name=args.name,
                cols=args.cols,
                cell_size=args.cell_size,
            )
            print(f"Prepared rework sheet from plan: {img_p} ({lay_p})")
            print(f"Generated structured prompt:     {prompt_p}")
        else:
            if not args.slots:
                parser.error("rework requires either --plan or --slots")
            items: list[tuple[str, int, str]] = []
            for raw in args.slots:
                spec, _, note = raw.partition("=")
                sym, _, fstr = spec.partition(":")
                items.append((sym.strip(), int(fstr.strip()), note.strip()))
            img_p, lay_p = prepare_rework(args.work, items, name=args.name, cols=args.cols, cell_size=args.cell_size)
            print(f"Prepared rework sheet: {img_p} ({lay_p})")
    elif args.cmd == "pack":
        out = pack_skin(
            args.work,
            args.out,
            args.build_name,
            sheet1_path=args.sheet1,
            sheet2_path=args.sheet2,
            sheet3_path=args.sheet3,
            offsets_path=args.offsets,
            icon_path=args.icon,
        )
        print(f"Packed DST skin archive: {out} ({out.stat().st_size} bytes)")
    elif args.cmd == "preview":
        try:
            from tools import skin_preview
        except ImportError:
            import skin_preview  # type: ignore[no-redef]
        res = skin_preview.render_comparison_sheet(
            anim_zip=args.anim,
            base_zip=args.base,
            skin_zip=args.skin,
            ref_zip=args.ref,
            out_dir=args.out,
            clip_name=args.clip,
            frame_index=args.frame,
            scale=args.scale,
        )
        print(res["sheet"])
        print(res["report"])
    elif args.cmd == "inspect-anim":
        try:
            from tools import skin_preview
        except ImportError:
            import skin_preview  # type: ignore[no-redef]
        slots = skin_preview.inspect_anim_slots(args.anim, clip_name=args.clip, frame_index=args.frame)
        filt = set(args.symbols) if args.symbols else None
        for view, entries in slots.items():
            filtered = [(s, f) for s, f, _ in entries if filt is None or s in filt]
            print(f"{view}: {filtered}")
    elif args.cmd == "tuner":
        try:
            from tools import skin_tuner
        except ImportError:
            import skin_tuner  # type: ignore[no-redef]
        skin_dir = _skin_root_from_work(args.work)
        out_html = args.out or (skin_dir / "skin_tuner.html")
        skin_offsets = out_html.parent / "offsets.json"
        src_offsets = skin_offsets if skin_offsets.is_file() else (args.work / "offsets.json")
        out = skin_tuner.build_tuner_html(
            skin_zip=args.skin,
            out_html=out_html,
            base_zip=args.base,
            ref_zip=args.ref,
            offsets_json=src_offsets,
            suite=args.suite,
            include_mount=args.include_mount,
        )
        print(f"Generated interactive HTML tuner ({args.suite}): {out}")
    elif args.cmd == "player":
        try:
            from tools import skin_player
        except ImportError:
            import skin_player  # type: ignore[no-redef]
        skin_dir = _skin_root_from_work(args.work)
        out_html = args.out or (skin_dir / "anim_player.html")
        out = skin_player.build_player_html(
            skin_zip=args.skin,
            out_html=out_html,
            base_zip=args.base,
            ref_zip=None if args.no_ref else args.ref,
            anim_dir=args.base.parent,
            suite=args.suite,
            include_mount=args.include_mount,
            include_ref=not args.no_ref,
        )
        print(f"Generated offline HTML5 animation player ({args.suite}): {out}")
    elif args.cmd == "gif":
        try:
            from tools import skin_preview
        except ImportError:
            import skin_preview  # type: ignore[no-redef]
        out_gif = args.out or (ROOT / "skins" / "deepseek" / "gifs" / f"{args.clip}_{args.facing}.gif")
        out = skin_preview.render_clip_gif(
            anim_zip=args.anim,
            base_zip=args.base,
            skin_zip=args.skin,
            out_gif=out_gif,
            ref_zip=None if args.no_ref else args.ref,
            clip_name=args.clip,
            facing=args.facing,
            scale=args.scale,
            fps=args.fps,
        )
        print(f"Exported animated GIF: {out}")
    elif args.cmd == "review":
        try:
            from tools import skin_preview
        except ImportError:
            import skin_preview  # type: ignore[no-redef]
        skin_dir = _skin_root_from_work(args.work)
        out_dir = args.out or (skin_dir / "review_data")
        html_out = (skin_dir / "review.html") if args.out is None else (out_dir / "review.html")
        res = skin_preview.render_animation_review(
            base_zip=args.base,
            skin_zip=args.skin,
            ref_zip=args.ref,
            out_dir=out_dir,
            anim_dir=args.anim_dir,
            suite=args.suite,
            anim_zips=args.zips,
            all_facings=args.all_facings,
            include_mount=args.include_mount,
            cols=args.cols,
            rows_per_sheet=args.rows,
            scale=args.scale,
            html_out=html_out,
        )
        cov = res["summary"]["coverage"]
        print(f"Rendered {res['summary']['total_rendered']} animation frames into {res['out_dir']}")
        print(f"Contact sheets ({len(res['sheets'])}): {', '.join(Path(p).name for p in res['sheets'])}")
        print(f"Coverage: {cov['covered_count']}/{cov['total_skin_slots']} ({cov['coverage_pct']}%) | Missing frames: {res['summary']['missing_frames_count']}")
        print(f"Review HTML: {res['review_html']}")
        print(f"Report MD:   {res['report_md']}")
    elif args.cmd == "refresh":
        try:
            from tools import skin_preview, skin_tuner
        except ImportError:
            import skin_preview  # type: ignore[no-redef]
            import skin_tuner  # type: ignore[no-redef]
        skin_dir = _skin_root_from_work(args.work)
        packed = pack_skin(
            args.work,
            args.out,
            args.build_name,
            offsets_path=args.offsets,
            icon_path=args.icon,
        )
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
        try:
            from tools import skin_player
        except ImportError:
            import skin_player  # type: ignore[no-redef]
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
