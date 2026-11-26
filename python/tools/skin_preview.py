"""Offline DST skeletal animation preview renderer using real anim.bin + build.bin/KTEX."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import struct
import time
import zipfile

from PIL import Image, ImageDraw

try:
    from tools.skin_pipeline import decode_ktex, parse_build_bin
except ImportError:
    from skin_pipeline import decode_ktex, parse_build_bin


DST_ANIM_DIR = Path(
    "C:/Program Files (x86)/Steam/steamapps/common/Don't Starve Together/data/anim"
)

# Standard DST facing bitmasks (matching anim.bin facing_byte)
FACING_DOWN = 0x08   # front view
FACING_UP = 0x02     # back view
FACING_RIGHT = 0x01  # side view (facing right)
FACING_LEFT = 0x04   # side view (facing left)
FACING_SIDE = FACING_RIGHT | FACING_LEFT  # 5

DEFAULT_HIDE_LAYERS = frozenset({
    "ARM_carry",
    "HAT",
    "HEAD_HAT",
    "HEAD_HAT_NOHELM",
    "HEAD_HAT_HELM",
    "HAIR_HAT",
    "swap_hat",
    "swap_body",
    "swap_body_tall",
    "swap_object",
    "lantern_overlay",
    "fx",
})

# Layer mask when simulating holding a tool/weapon (shows ARM_carry instead of ARM_normal)
CARRY_HIDE_LAYERS = (DEFAULT_HIDE_LAYERS - {"ARM_carry"}) | {"ARM_normal"}

# Targeted extra frames ensuring 100% coverage of rare slots (foot:4, torso:9, torso_pelvis:9, ARM_carry X-ray):
# (zip_name, clip_name, facing_label, frame_idx, use_carry_layers, display_clip_key)
COVERAGE_BOOSTER_SPECS: tuple[tuple[str, str, str, int, bool, str], ...] = (
    ("player_actions_whip.zip", "whip", "front", 5, False, "whip_f5"),
    ("player_actions_bugnet.zip", "bugnet", "back", 8, False, "bugnet_back"),
    ("player_mount_shock.zip", "shock", "all", 1, True, "mount_shock_carry"),
)

# Mirror of jev_companion_presentation.lua BODY_SYMBOLS / BLANK_SYMBOLS
COMPANION_BODY_SYMBOLS = (
    "headbase", "headbase_hat", "hair", "hair_hat", "hairpigtails", "tail", "face", "cheeks",
    "torso", "torso_pelvis", "skirt", "torso_wide", "torso_pelvis_wide", "skirt_wide",
    "arm_upper", "arm_upper_skin", "arm_lower", "hand", "leg", "foot",
)
COMPANION_BLANK_SYMBOLS = ("beard", "arm_lower_cuff", "arm_lower_narrow")

# Default core DST player animation zips covering locomotion, idles, work, combat, shock, states, and emotes.
DEFAULT_REVIEW_ZIPS: tuple[str, ...] = (
    "player_idles.zip",
    "player_basic.zip",
    "player_actions.zip",
    "player_actions_axe.zip",
    "player_actions_pickaxe.zip",
    "player_actions_shovel.zip",
    "player_actions_eat.zip",
    "player_actions_item.zip",
    "player_actions_useitem.zip",
    "player_attacks.zip",
    "player_shock.zip",
    "player_emotes.zip",
    "player_idles_shiver.zip",
    "player_knockedout.zip",
    "player_frozen.zip",
)

# Curated high-signal animations for fast "core" review: (zip_name, clip_name, facing_label, frame_override_or_None)
CORE_REVIEW_SPECS: tuple[tuple[str, str, str, int | None], ...] = (
    ("player_idles.zip", "idle_loop", "front", 0),
    ("player_idles.zip", "idle_loop", "side", 0),
    ("player_idles.zip", "idle_loop", "back", 0),
    ("player_basic.zip", "run_loop", "front", 3),
    ("player_basic.zip", "run_loop", "side", 3),
    ("player_basic.zip", "run_loop", "back", 3),
    ("player_actions_axe.zip", "chop_loop", "front", None),
    ("player_actions_axe.zip", "chop_loop", "side", None),
    ("player_actions_pickaxe.zip", "pickaxe_loop", "side", None),
    ("player_actions_shovel.zip", "shovel_loop", "side", None),
    ("player_actions_item.zip", "pickup", "front", None),
    ("player_actions_item.zip", "pickup", "side", None),
    ("player_actions_item.zip", "build", "front", None),
    ("player_attacks.zip", "atk", "front", None),
    ("player_attacks.zip", "atk", "side", None),
    ("player_attacks.zip", "punch", "side", None),
    ("player_actions.zip", "hit", "front", None),
    ("player_actions.zip", "death", "front", None),
    ("player_actions.zip", "research", "front", None),
    ("player_actions_eat.zip", "eat", "front", None),
    ("player_actions_eat.zip", "hungry", "front", None),
    ("player_actions_eat.zip", "refuseeat", "front", None),
    ("player_shock.zip", "shock", "front", None),
    ("player_idles.zip", "idle_sanity_loop", "front", None),
    ("player_idles.zip", "idle_hot_loop", "front", None),
    ("player_idles_shiver.zip", "idle_shiver_loop", "front", None),
    ("player_knockedout.zip", "sleep_loop", "front", None),
    ("player_frozen.zip", "frozen", "front", None),
    ("player_emotes.zip", "emote_happycheer", "front", None),
    ("player_emotes.zip", "emote_angry", "front", None),
    ("player_emotes.zip", "emote_sad", "front", None),
    ("player_emotes.zip", "emote_waving", "front", None),
    ("player_emotes.zip", "emote_yawn", "front", None),
    ("player_emotes.zip", "emote_annoyed_facepalm", "front", None),
)

# Representative gameplay clips added on top of CORE_REVIEW_SPECS in compact mode:
# (zip_name, clip_name, facing_label, frame_override_or_None, use_carry_layers, display_clip_key)
COMPACT_EXTRA_SPECS: tuple[tuple[str, str, str, int | None, bool, str], ...] = (
    ("player_actions_item.zip", "build_loop", "front", None, False, "build_loop"),
    ("player_actions_item.zip", "give", "front", None, False, "give"),
    ("player_acting.zip", "acting_1", "front", None, False, "acting_1"),
    ("player_actions_blowdart.zip", "dart", "front", None, False, "dart"),
    ("player_actions_whip.zip", "whip", "front", 5, False, "whip"),
    ("player_actions_bugnet.zip", "bugnet", "back", 8, False, "bugnet"),
    ("player_actions_fishing.zip", "fishing_idle", "side", None, False, "fishing_idle"),
    ("player_actions_fishing.zip", "bite_light_loop", "front", None, False, "bite_light_loop"),
    ("player_actions_fishing.zip", "bite_light_loop", "side", 0, False, "bite_light_loop"),
    ("player_oar.zip", "row_medium", "side", None, False, "row_medium"),
    ("player_actions_reading.zip", "reading_loop", "front", None, False, "reading_loop"),
    ("player_actions_uniqueitem.zip", "book", "front", None, False, "book"),
    ("player_actions_till.zip", "till_loop", "side", None, False, "till_loop"),
    ("player_actions_farming.zip", "water", "side", None, False, "water"),
    ("player_emotes_sit.zip", "emote_loop_sit1", "front", None, False, "emote_loop_sit1"),
    ("player_emotes_dance0.zip", "emoteXL_loop_dance0", "front", None, False, "emoteXL_loop_dance0"),
    ("player_mount_shock.zip", "shock", "all", 1, True, "mount_shock_carry"),
)

_JUNK_CLIP_RE = re.compile(
    r"^(?:x+|xx\d+|x\d+|xxx_\d+|dd|hc|ref|ref_idle|temp_ta|wilson)$",
    re.IGNORECASE,
)
_TRANSITION_CLIP_RE = re.compile(r"(?:^|_)(?:pre|pst|lag)(?:_|$)", re.IGNORECASE)


def is_clean_review_clip(clip_name: str) -> bool:
    """Return True if a clip name is a real gameplay animation (not Klei dev junk or pre/pst/lag transition)."""
    name = clip_name.strip()
    if not name:
        return False
    if _JUNK_CLIP_RE.match(name):
        return False
    if _TRANSITION_CLIP_RE.search(name):
        return False
    return True


ANIM_TIER_META: dict[str, tuple[int, str]] = {
    "T0": (0, "T0 核心高频"),
    "T1": (1, "T1 常规玩法"),
    "T2": (2, "T2 表情/偶发"),
    "T3": (3, "T3 极低频/特殊"),
}

_T0_CORE_ZIPS = frozenset({
    "player_basic.zip",
    "player_actions_axe.zip",
    "player_actions_pickaxe.zip",
    "player_actions_shovel.zip",
    "player_attacks.zip",
    "player_idles_shiver.zip",
})
_T0_IDLE_CLIPS = frozenset({
    "idle_loop", "idle_loop_nofaced", "idle_inaction",
    "idle_inaction_sanity", "idle_hot_loop", "idle_sanity_loop",
})
_T0_ITEM_CLIPS = frozenset({"pickup", "build", "build_loop", "give"})
_T0_ACTION_CLIPS = frozenset({"hit", "death", "death2", "wakeup", "research", "fall_high"})
_T0_EAT_CLIPS = frozenset({"eat", "quick_eat", "hungry", "quick_drink", "drink"})

_T1_EXACT_ZIPS = frozenset({
    "player_oar.zip",
    "player_frozen.zip",
    "player_shock.zip",
    "player_mount_shock.zip",
    "player_sandstorm.zip",
    "player_actions_eat.zip",
    "player_actions.zip",
    "player_actions_item.zip",
    "player_idles.zip",
    "player_hotspring.zip",
    "player_float.zip",
})
_T1_PREFIXES = (
    "player_actions_fishing",
    "player_actions_farming",
    "player_actions_till",
    "player_actions_bugnet",
    "player_actions_whip",
    "player_actions_blowdart",
    "player_actions_reading",
    "player_actions_uniqueitem",
    "player_actions_feast_eat",
    "player_actions_useitem",
    "player_actions_boomerang",
    "player_boat",
    "player_encumbered",
    "player_parry",
)
_T2_EXACT_ZIPS = frozenset({
    "player_groggy.zip",
    "player_actions_pickaxe_recoil.zip",
    "player_suspended.zip",
    "player_hit_darkness.zip",
    "player_acting.zip",
    "player_pushing.zip",
    "player_teetering.zip",
})


def classify_animation_tier(anim_zip: str, clip_name: str) -> tuple[str, int, str]:
    """Classify an animation clip into (tier, tier_order, tier_label) by DST gameplay frequency."""
    z = Path(anim_zip).name.lower()
    c = clip_name.strip().lower()

    tier = "T3"
    if (
        z in _T0_CORE_ZIPS
        or (z == "player_idles.zip" and c in _T0_IDLE_CLIPS)
        or (z == "player_actions_item.zip" and c in _T0_ITEM_CLIPS)
        or (z == "player_actions.zip" and c in _T0_ACTION_CLIPS)
        or (z == "player_actions_eat.zip" and c in _T0_EAT_CLIPS)
    ):
        tier = "T0"
    elif z == "player_knockedout.zip":
        tier = "T2" if "dozy" in c else "T1"
    elif z.startswith(("player_emotes", "player_emote_", "player_sit_", "player_mount_emotes")) or z in _T2_EXACT_ZIPS:
        tier = "T2"
    elif z in _T1_EXACT_ZIPS or z.startswith(_T1_PREFIXES) or (z.startswith("player_mount") and "emote" not in c):
        tier = "T1"

    order, label = ANIM_TIER_META[tier]
    return tier, order, label


def _load_anim_cached_by_name(
    zip_name: str,
    anim_dir: Path,
    anim_cache: dict[str, dict],
) -> dict | None:
    if zip_name in anim_cache:
        return anim_cache[zip_name]
    p = Path(zip_name)
    if not p.is_file():
        p = anim_dir / zip_name
    if not p.is_file():
        return None
    try:
        with zipfile.ZipFile(p, "r") as zf:
            if "anim.bin" not in zf.namelist():
                return None
            anim_cache[zip_name] = parse_anim_bin(zf.read("anim.bin"))
    except Exception:
        return None
    return anim_cache[zip_name]


def _facing_priority(anim: dict) -> int:
    fm = anim["facing"]
    if fm == FACING_DOWN:
        return 0
    if fm == 0xFF:
        return 1
    if fm in (FACING_SIDE, FACING_RIGHT, FACING_LEFT):
        return 2
    if fm == FACING_UP:
        return 3
    return 4


def resolve_compact_specs(
    skin_build: dict,
    anim_dir: Path = DST_ANIM_DIR,
    *,
    include_mount: bool = False,
    anim_cache: dict[str, dict] | None = None,
) -> list[tuple[str, str, int, str, int | None, bool, str]]:
    """Build curated core+gameplay clips plus greedy clean cover to hit 100% skin slot coverage."""
    cache = anim_cache if anim_cache is not None else {}

    all_skin_slots: set[str] = set()
    for sym in COMPANION_BODY_SYMBOLS:
        if sym.endswith("_wide") or sym in ("headbase_hat", "hair_hat"):
            continue
        for finfo in skin_build["symbols"].get(sym, {}).values():
            if float(finfo.get("w", 0.0)) <= 4.0 and float(finfo.get("h", 0.0)) <= 4.0:
                continue
            all_skin_slots.add(f"{sym}:{finfo['framenum']}")

    def _frame_skin_slots(frame: dict, use_carry: bool = False) -> set[str]:
        hide = CARRY_HIDE_LAYERS if use_carry else DEFAULT_HIDE_LAYERS
        found: set[str] = set()
        for elem in frame["elements"]:
            if elem["layer"] in hide:
                continue
            sym = elem["symbol"]
            if sym not in COMPANION_BODY_SYMBOLS or sym.endswith("_wide") or sym in ("headbase_hat", "hair_hat"):
                continue
            sym_frames = skin_build["symbols"].get(sym)
            if not sym_frames:
                continue
            finfo = sym_frames.get(int(elem["frame_num"]))
            if finfo is not None and (float(finfo.get("w", 0.0)) > 4.0 or float(finfo.get("h", 0.0)) > 4.0):
                found.add(f"{sym}:{finfo['framenum']}")
        return found

    targets: list[tuple[str, str, int, str, int | None, bool, str]] = []
    seen_target: set[tuple[str, str, int, bool]] = set()
    covered: set[str] = set()

    def _append_seed(
        zname: str,
        clip_name: str,
        flabel: str,
        explicit_fidx: int | None,
        use_carry: bool,
        tkey: str,
    ) -> None:
        adata = _load_anim_cached_by_name(zname, anim_dir, cache)
        if adata is None:
            return
        anim = _find_anim(adata, clip_name, _facing_mask_from_label(flabel))
        if anim is None or not anim["frames"]:
            return
        key = (zname, clip_name, anim["facing"], use_carry)
        if key in seen_target:
            return
        seen_target.add(key)
        fidx = _pick_representative_frame(anim, clip_name, explicit_fidx)
        out_label = flabel if anim["facing"] != 0xFF else "all"
        targets.append((zname, clip_name, anim["facing"], out_label, explicit_fidx, use_carry, tkey))
        covered.update(_frame_skin_slots(anim["frames"][fidx % len(anim["frames"])], use_carry))

    for zname, clip, flabel, fidx in CORE_REVIEW_SPECS:
        _append_seed(zname, clip, flabel, fidx, False, clip)
    for zname, clip, flabel, fidx, use_carry, tkey in COMPACT_EXTRA_SPECS:
        _append_seed(zname, clip, flabel, fidx, use_carry, tkey)

    uncovered = all_skin_slots - covered
    if not uncovered:
        return targets

    zip_list = discover_generic_anim_zips(anim_dir, include_mount=include_mount)

    # Pass 1: primary facing with default representative frame per clean clip
    pass1_candidates: list[tuple[set[str], str, str, int, str]] = []
    for zname in zip_list:
        adata = _load_anim_cached_by_name(zname, anim_dir, cache)
        if adata is None:
            continue
        by_clip: dict[str, list[dict]] = {}
        for anim in adata["anims"]:
            if anim["frames"] and is_clean_review_clip(anim["name"]):
                by_clip.setdefault(anim["name"], []).append(anim)
        for clip_name, variants in sorted(by_clip.items()):
            best_anim = min(variants, key=_facing_priority)
            key = (zname, clip_name, best_anim["facing"], False)
            if key in seen_target:
                continue
            fidx = _pick_representative_frame(best_anim, clip_name, None)
            slots = _frame_skin_slots(best_anim["frames"][fidx % len(best_anim["frames"])], False)
            if slots:
                pass1_candidates.append(
                    (slots, zname, clip_name, best_anim["facing"], _facing_label_from_mask(best_anim["facing"]))
                )

    while uncovered:
        # Pick the candidate strictly maximizing newly covered slots, with deterministic tie-break
        scored = [
            (len(uncovered & slots), (zname, clip_name, fmask), (slots, zname, clip_name, fmask, flabel))
            for slots, zname, clip_name, fmask, flabel in pass1_candidates
            if (zname, clip_name, fmask, False) not in seen_target
        ]
        if not scored:
            break
        scored.sort(key=lambda x: (-x[0], x[1]))
        top_gain, _, (slots, zname, clip_name, fmask, flabel) = scored[0]
        if top_gain <= 0:
            break
        seen_target.add((zname, clip_name, fmask, False))
        targets.append((zname, clip_name, fmask, flabel, None, False, clip_name))
        covered.update(slots)
        uncovered -= slots

    if not uncovered:
        return targets

    # Pass 2: scan all facings and all frame indices of clean clips if any rare slot still remains
    pass2_candidates: list[tuple[set[str], str, str, int, str, int]] = []
    for zname in zip_list:
        adata = _load_anim_cached_by_name(zname, anim_dir, cache)
        if adata is None:
            continue
        for anim in adata["anims"]:
            clip_name = anim["name"]
            if not anim["frames"] or not is_clean_review_clip(clip_name):
                continue
            fmask = anim["facing"]
            flabel = _facing_label_from_mask(fmask)
            for fidx, fr in enumerate(anim["frames"]):
                slots = _frame_skin_slots(fr, False)
                if slots & uncovered:
                    pass2_candidates.append((slots, zname, clip_name, fmask, flabel, fidx))

    while uncovered:
        scored2 = [
            (len(uncovered & slots), (zname, clip_name, fmask, fidx), (slots, zname, clip_name, fmask, flabel, fidx))
            for slots, zname, clip_name, fmask, flabel, fidx in pass2_candidates
            if (zname, clip_name, fmask, False) not in seen_target
        ]
        if not scored2:
            break
        scored2.sort(key=lambda x: (-x[0], x[1]))
        top_gain, _, (slots, zname, clip_name, fmask, flabel, fidx) = scored2[0]
        if top_gain <= 0:
            break
        seen_target.add((zname, clip_name, fmask, False))
        targets.append((zname, clip_name, fmask, flabel, fidx, False, clip_name))
        covered.update(slots)
        uncovered -= slots

    return targets

XRAY_MARKER_SLOTS = frozenset({
    ("headbase", 3),
    ("torso", 10),
    ("skirt", 4),
    ("hair", 3),
    ("tail", 6),
    ("hand", 12),
})

# Filename substrings identifying character-specific, transformation, UI, or FX-only animation zips
CHARACTER_SPECIFIC_ZIP_TOKENS: tuple[str, ...] = (
    "_wilson", "_willow", "_wolfgang", "_wendy", "_wx78", "_wx", "_wickerbottom",
    "_woodie", "_wes", "_waxwell", "_wathgrithr", "_webber", "_winona", "_warly",
    "_wortox", "_wormwood", "_wurt", "_walter", "_wanda", "_wonkey",
    "werebeaver", "weregoose", "weremoose", "hermitcrab", "monkey", "merm",
    "ghost", "progressbar", "_fx", "vial_fx", "ancient_", "mighty_gym",
    "mime", "pocketwatch", "slingshot", "slurtle", "elixir", "shadow_thrall",
    "idles_ui", "wardrobe", "skin_change", "revive_fx", "witchbroom",
)


def discover_generic_anim_zips(
    anim_dir: Path = DST_ANIM_DIR,
    *,
    include_mount: bool = False,
) -> list[str]:
    """Return sorted player_*.zip filenames excluding character-specific/FX/UI packs."""
    if not anim_dir.is_dir():
        return []
    result: list[str] = []
    for p in sorted(anim_dir.glob("player_*.zip")):
        low = p.name.lower()
        if any(tok in low for tok in CHARACTER_SPECIFIC_ZIP_TOKENS):
            continue
        if not include_mount and low.startswith("player_mount"):
            continue
        result.append(p.name)
    return result


def parse_anim_bin(data: bytes) -> dict:
    """Parse Klei anim.bin (version 4) into animations, frames, elements, and hash names."""
    if len(data) < 24 or data[:4] != b"ANIM":
        raise ValueError("Invalid anim.bin header")
    version, total_elems, total_frames, total_events, num_anims = struct.unpack_from(
        "<5I", data, 4
    )
    if version != 4:
        raise ValueError(f"Unsupported anim.bin version {version}; expected 4")

    offset = 24
    raw_anims: list[dict] = []
    for _ in range(num_anims):
        name_len, = struct.unpack_from("<I", data, offset)
        offset += 4
        name = data[offset:offset + name_len].decode("utf-8", "replace")
        offset += name_len

        facing = data[offset]
        offset += 1
        root_hash, framerate, num_frames = struct.unpack_from("<IfI", data, offset)
        offset += 12

        frames: list[dict] = []
        for _ in range(num_frames):
            fx, fy, fw, fh, num_events = struct.unpack_from("<4fI", data, offset)
            offset += 20
            events = list(struct.unpack_from(f"<{num_events}I", data, offset)) if num_events else []
            offset += 4 * num_events
            num_elements, = struct.unpack_from("<I", data, offset)
            offset += 4
            elements: list[dict] = []
            for _ in range(num_elements):
                sym_hash, frame_num, layer_hash, ma, mb, mc, md, mtx, mty, z_idx = (
                    struct.unpack_from("<3I7f", data, offset)
                )
                offset += 40
                elements.append({
                    "symbol_hash": sym_hash,
                    "frame_num": frame_num,
                    "layer_hash": layer_hash,
                    "m": (ma, mb, mc, md, mtx, mty),
                    "z": z_idx,
                })
            frames.append({
                "bbox": (fx, fy, fw, fh),
                "events": events,
                "elements": elements,
            })

        raw_anims.append({
            "name": name,
            "facing": facing,
            "root_hash": root_hash,
            "framerate": framerate,
            "frames": frames,
        })

    num_hashes, = struct.unpack_from("<I", data, offset)
    offset += 4
    hash_names: dict[int, str] = {}
    for _ in range(num_hashes):
        hval, slen = struct.unpack_from("<II", data, offset)
        offset += 8
        hash_names[hval] = data[offset:offset + slen].decode("utf-8", "replace")
        offset += slen

    for anim in raw_anims:
        anim["root"] = hash_names.get(anim["root_hash"], f"0x{anim['root_hash']:08x}")
        for frame in anim["frames"]:
            for elem in frame["elements"]:
                elem["symbol"] = hash_names.get(elem["symbol_hash"], f"0x{elem['symbol_hash']:08x}")
                elem["layer"] = hash_names.get(elem["layer_hash"], f"0x{elem['layer_hash']:08x}")

    return {
        "version": version,
        "total_elements": total_elems,
        "total_frames": total_frames,
        "total_events": total_events,
        "anims": raw_anims,
        "hash_names": hash_names,
    }


def load_build(zip_path: Path) -> dict:
    """Load a DST character build.zip and crop every symbol frame to upright RGBA."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        parsed = parse_build_bin(zf.read("build.bin"))
        atlases: list[Image.Image] = []
        for atlas_name in parsed["atlases"]:
            atlases.append(decode_ktex(zf.read(atlas_name)))

    verts = parsed["verts"]
    symbols: dict[str, dict[int, dict]] = {}
    for sym_name, frames in parsed["symbols"].items():
        frame_map: dict[int, dict] = {}
        for fr in frames:
            vstart = fr["vstart"]
            vcount = fr["vcount"]
            if vcount < 3:
                continue
            fverts = verts[vstart:vstart + vcount]
            # Atlas index is encoded in vertex z (index 2)
            atlas_idx = int(round(fverts[0][2]))
            atlas = atlases[min(max(atlas_idx, 0), len(atlases) - 1)]
            aw, ah = atlas.size
            us = [v[3] for v in fverts]
            vs = [v[4] for v in fverts]
            ax0 = max(0, int(round(min(us) * aw)))
            ax1 = min(aw, int(round(max(us) * aw)))
            ay0 = max(0, int(round((1.0 - max(vs)) * ah)))
            ay1 = min(ah, int(round((1.0 - min(vs)) * ah)))
            if ax1 <= ax0 or ay1 <= ay0:
                continue
            crop = atlas.crop((ax0, ay0, ax1, ay1))
            frame_info = {
                "image": crop,
                "cx": fr["cx"],
                "cy": fr["cy"],
                "w": fr["w"],
                "h": fr["h"],
                "framenum": fr["framenum"],
                "duration": fr["duration"],
            }
            for step in range(max(1, int(fr["duration"]))):
                frame_map[fr["framenum"] + step] = frame_info
        symbols[sym_name] = frame_map

    return {
        "build_name": parsed["build_name"],
        "path": str(zip_path),
        "symbols": symbols,
    }


def _find_anim(anim_data: dict, clip_name: str, facing_mask: int) -> dict | None:
    """Find an animation clip matching name and facing bitmask."""
    exact = None
    partial = None
    for anim in anim_data["anims"]:
        if anim["name"] != clip_name:
            continue
        if anim["facing"] == facing_mask:
            exact = anim
            break
        if (anim["facing"] & facing_mask) != 0 and partial is None:
            partial = anim
    return exact or partial


def _invert_2x3(a: float, b: float, tx: float, c: float, d: float, ty: float) -> tuple[float, float, float, float, float, float] | None:
    """Invert 2D affine matrix [[a, b, tx], [c, d, ty]]."""
    det = a * d - b * c
    if abs(det) < 1e-8:
        return None
    inv_det = 1.0 / det
    ia = d * inv_det
    ib = -b * inv_det
    ic = -c * inv_det
    id_ = a * inv_det
    itx = -(ia * tx + ib * ty)
    ity = -(ic * tx + id_ * ty)
    return (ia, ib, itx, ic, id_, ity)


def render_anim_frame(
    anim_data: dict,
    base_build: dict,
    clip_name: str,
    facing_mask: int,
    frame_index: int = 0,
    override_build: dict | None = None,
    override_map: dict[str, str] | None = None,
    hide_layers: frozenset[str] = DEFAULT_HIDE_LAYERS,
    canvas_size: tuple[int, int] = (512, 512),
    scale: float = 1.12,
    bg_rgba: tuple[int, int, int, int] = (38, 42, 48, 255),
    auto_frame: bool = False,
    bbox_override: tuple[float, float, float, float] | None = None,
) -> tuple[Image.Image, dict]:
    """Render a single skeletal animation frame with exact Klei 2x3 affine transforms."""
    anim = _find_anim(anim_data, clip_name, facing_mask)
    if anim is None:
        avail = sorted({(a["name"], a["facing"]) for a in anim_data["anims"]})
        raise ValueError(f"Clip {clip_name!r} with facing {facing_mask} not found; sample={avail[:12]}")

    frame = anim["frames"][frame_index % len(anim["frames"])]
    cw, ch = canvas_size
    eff_scale = scale
    origin_x = cw * 0.5
    origin_y = ch * 0.80

    bbox_for_frame = bbox_override if bbox_override is not None else frame.get("bbox")
    if auto_frame and bbox_for_frame:
        fx, fy, fw, fh = bbox_for_frame
        # Add generous headroom for stylized chibi hair accessories / ahoge
        pad_w = max(40.0, fw * 0.28)
        pad_top = max(55.0, fh * 0.25)
        pad_bot = max(20.0, fh * 0.12)
        min_x = fx - fw * 0.5 - pad_w * 0.5
        max_x = fx + fw * 0.5 + pad_w * 0.5
        min_y = fy - fh * 0.5 - pad_top
        max_y = fy + fh * 0.5 + pad_bot
        box_w = max(1.0, max_x - min_x)
        box_h = max(1.0, max_y - min_y)

        # Keep default scale if it fits, otherwise gently shrink to fit canvas
        avail_w = max(32.0, cw - 16.0)
        avail_h = max(32.0, ch - 28.0)
        fit_scale = min(eff_scale, avail_w / box_w, avail_h / box_h)
        eff_scale = max(0.2, fit_scale)

        # Only shift origin when the frame bbox would overflow default framing
        top_px = origin_y + min_y * eff_scale
        bot_px = origin_y + max_y * eff_scale
        left_px = origin_x + min_x * eff_scale
        right_px = origin_x + max_x * eff_scale

        if top_px < 10.0 or bot_px > ch - 20.0:
            center_y_world = (min_y + max_y) * 0.5
            origin_y = (ch - 10.0) * 0.5 - center_y_world * eff_scale
        if left_px < 8.0 or right_px > cw - 8.0:
            center_x_world = (min_x + max_x) * 0.5
            origin_x = cw * 0.5 - center_x_world * eff_scale

    canvas = Image.new("RGBA", (cw, ch), bg_rgba)

    used_elements: list[dict] = []
    missing_elements: list[dict] = []

    # Klei anim.bin lists elements front-to-back (topmost first), so paint back-to-front.
    for elem in reversed(frame["elements"]):
        layer = elem["layer"]
        sym = elem["symbol"]
        fnum = elem["frame_num"]
        if layer in hide_layers:
            continue

        src_build = base_build
        src_sym = sym
        overridden = False
        if override_build is not None and override_map is not None and sym in override_map:
            src_build = override_build
            src_sym = override_map[sym]
            overridden = True

        sym_frames = src_build["symbols"].get(src_sym)
        if not sym_frames:
            missing_elements.append({"symbol": sym, "layer": layer, "frame_num": fnum, "overridden": overridden})
            continue
        finfo = sym_frames.get(fnum)
        if finfo is None:
            # Fallback to frame 0 only if symbol is a single-frame blank override
            if len(sym_frames) == 1 and 0 in sym_frames and src_sym == "long_hair_blank":
                finfo = sym_frames[0]
            else:
                missing_elements.append({"symbol": sym, "layer": layer, "frame_num": fnum, "overridden": overridden})
                continue

        img = finfo["image"]
        iw, ih = img.size
        if iw == 0 or ih == 0:
            continue

        # Local symbol coordinates: pixel (u, v) in crop -> (x_loc, y_loc)
        # x_loc = (cx - w/2) + u * (w / iw)
        # y_loc = (cy - h/2) + v * (h / ih)
        sx = finfo["w"] / float(iw)
        sy = finfo["h"] / float(ih)
        x0 = finfo["cx"] - finfo["w"] * 0.5
        y0 = finfo["cy"] - finfo["h"] * 0.5

        ma, mb, mc, md, mtx, mty = elem["m"]
        # Klei element matrix maps local (x_loc, y_loc) -> world/canvas:
        # X_anim = ma * x_loc + mc * y_loc + mtx
        # Y_anim = mb * x_loc + md * y_loc + mty
        # (DST anim.bin stores column-major 2x2 [a, b, c, d] + translation [tx, ty])
        fa = eff_scale * (ma * sx)
        fb = eff_scale * (mc * sy)
        ftx = origin_x + eff_scale * (ma * x0 + mc * y0 + mtx)
        fc = eff_scale * (mb * sx)
        fd = eff_scale * (md * sy)
        fty = origin_y + eff_scale * (mb * x0 + md * y0 + mty)

        inv = _invert_2x3(fa, fb, ftx, fc, fd, fty)
        if inv is None:
            continue

        warped = img.transform((cw, ch), Image.Transform.AFFINE, inv, resample=Image.Resampling.BICUBIC)
        canvas = Image.alpha_composite(canvas, warped)
        used_elements.append({
            "layer": layer,
            "symbol": sym,
            "source_symbol": src_sym,
            "source_build": src_build["build_name"],
            "frame_num": fnum,
            "overridden": overridden,
            "size": [round(finfo["w"], 1), round(finfo["h"], 1)],
            "pivot": [round(finfo["cx"], 1), round(finfo["cy"], 1)],
            "z": round(elem["z"], 3),
        })

    # Subtle ground line for foot alignment check (only draw if inside canvas)
    if 8 <= int(origin_y) <= ch - 8:
        draw = ImageDraw.Draw(canvas)
        draw.line([(24, int(origin_y)), (cw - 24, int(origin_y))], fill=(95, 105, 120, 140), width=1)

    report = {
        "clip": clip_name,
        "facing": anim["facing"],
        "frame_index": frame_index,
        "used_count": len(used_elements),
        "missing_count": len(missing_elements),
        "used": used_elements,
        "missing": missing_elements,
    }
    return canvas, report


def inspect_anim_slots(
    anim_zip: Path,
    clip_name: str = "idle_loop",
    frame_index: int = 0,
    hide_layers: frozenset[str] = DEFAULT_HIDE_LAYERS,
) -> dict[str, list[tuple[str, int, str]]]:
    """Return ordered (symbol, frame_num, layer) tuples used by front/side/back views."""
    with zipfile.ZipFile(anim_zip, "r") as zf:
        anim_data = parse_anim_bin(zf.read("anim.bin"))
    result: dict[str, list[tuple[str, int, str]]] = {}
    for vname, fmask in (("front", FACING_DOWN), ("side", FACING_SIDE), ("back", FACING_UP)):
        anim = _find_anim(anim_data, clip_name, fmask)
        if anim is None:
            result[vname] = []
            continue
        frame = anim["frames"][frame_index % len(anim["frames"])]
        entries: list[tuple[str, int, str]] = []
        for elem in reversed(frame["elements"]):
            if elem["layer"] in hide_layers:
                continue
            entries.append((elem["symbol"], int(elem["frame_num"]), elem["layer"]))
        result[vname] = entries
    return result


def build_companion_override_map() -> dict[str, str]:
    """Build symbol->target_symbol override map matching jev_companion_presentation.lua."""
    mapping = {sym: sym for sym in COMPANION_BODY_SYMBOLS}
    for sym in COMPANION_BLANK_SYMBOLS:
        mapping[sym] = "long_hair_blank"
    return mapping


def render_comparison_sheet(
    anim_zip: Path,
    base_zip: Path,
    skin_zip: Path | None,
    ref_zip: Path | None,
    out_dir: Path,
    clip_name: str = "idle_loop",
    frame_index: int = 0,
    scale: float = 1.12,
) -> dict:
    """Render front/side/back comparison sheet (reference vs base+skin) + JSON report."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(anim_zip, "r") as zf:
        anim_data = parse_anim_bin(zf.read("anim.bin"))

    base_build = load_build(base_zip)
    skin_build = load_build(skin_zip) if skin_zip else None
    ref_build = load_build(ref_zip) if ref_zip else None
    override_map = build_companion_override_map() if skin_build else None

    views = [
        ("front", FACING_DOWN),
        ("side", FACING_SIDE),
        ("back", FACING_UP),
    ]

    cell_w, cell_h = 420, 460
    rows: list[tuple[str, dict, dict | None, dict | None]] = []
    if ref_build is not None:
        rows.append((f"Reference ({ref_build['build_name']})", ref_build, None, None))
    if skin_build is not None:
        rows.append((f"In-Game ({base_build['build_name']} + {skin_build['build_name']})", base_build, skin_build, override_map))
    else:
        rows.append((f"Base ({base_build['build_name']})", base_build, None, None))

    sheet = Image.new("RGBA", (cell_w * len(views), cell_h * len(rows)), (28, 32, 38, 255))
    draw = ImageDraw.Draw(sheet)
    reports: dict[str, dict] = {}

    for r_idx, (row_label, b_build, ov_build, ov_map) in enumerate(rows):
        row_key = "reference" if ov_build is None and ref_build is not None and r_idx == 0 else "target"
        reports[row_key] = {}
        for c_idx, (vname, fmask) in enumerate(views):
            frame_img, rep = render_anim_frame(
                anim_data=anim_data,
                base_build=b_build,
                clip_name=clip_name,
                facing_mask=fmask,
                frame_index=frame_index,
                override_build=ov_build,
                override_map=ov_map,
                canvas_size=(cell_w, cell_h),
                scale=scale,
            )
            frame_img.save(out_dir / f"{row_key}_{clip_name}_{vname}.png")
            x0, y0 = c_idx * cell_w, r_idx * cell_h
            sheet.paste(frame_img, (x0, y0))
            draw.rectangle([x0, y0, x0 + cell_w - 1, y0 + cell_h - 1], outline=(70, 78, 90, 255), width=1)
            draw.text((x0 + 12, y0 + 10), f"{row_label} | {clip_name} ({vname})", fill=(235, 240, 248, 255))
            reports[row_key][vname] = rep

    sheet_path = out_dir / f"preview_{clip_name}_f{frame_index}.png"
    sheet.save(sheet_path)
    report_path = out_dir / f"preview_{clip_name}_f{frame_index}.json"
    report_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "sheet": str(sheet_path),
        "report": str(report_path),
        "reports": reports,
    }


def _facing_mask_from_label(label: str) -> int:
    key = label.strip().lower()
    if key == "front":
        return FACING_DOWN
    if key == "side":
        return FACING_SIDE
    if key == "back":
        return FACING_UP
    return 0xFF


def _facing_label_from_mask(mask: int) -> str:
    if mask == 0xFF:
        return "all"
    if mask == FACING_DOWN:
        return "front"
    if mask == FACING_UP:
        return "back"
    if mask in (FACING_SIDE, FACING_RIGHT, FACING_LEFT):
        return "side"
    if mask & FACING_DOWN:
        return "front"
    if mask & FACING_SIDE:
        return "side"
    if mask & FACING_UP:
        return "back"
    return f"f{mask}"


def _pick_representative_frame(anim: dict, clip_name: str, explicit_frame: int | None = None) -> int:
    """Choose the most informative single frame for visual skin QA."""
    frames = anim["frames"]
    total = len(frames)
    if total <= 1:
        return 0
    if explicit_frame is not None:
        return explicit_frame % total

    # Prefer actual X-ray skeleton frames for shock / electrocuted clips
    for idx, fr in enumerate(frames):
        for elem in fr["elements"]:
            if (elem["symbol"], int(elem["frame_num"])) in XRAY_MARKER_SLOTS:
                return idx

    # For idle loops frame 0 is canonical; for action/emote/movement clips mid-motion reveals joints best
    if clip_name in ("idle", "idle_loop", "idle_loop_nofaced"):
        return 0
    if clip_name == "run_loop":
        return min(3, total - 1)
    return min(max(1, total // 2), total - 1)


def _render_pair_tile(
    anim_data: dict,
    base_build: dict,
    skin_build: dict | None,
    ref_build: dict | None,
    override_map: dict[str, str] | None,
    clip_name: str,
    facing_mask: int,
    frame_index: int,
    *,
    pane_size: tuple[int, int] = (260, 300),
    scale: float = 0.78,
    hide_layers: frozenset[str] = DEFAULT_HIDE_LAYERS,
) -> tuple[Image.Image, dict]:
    """Render a side-by-side (ref vs target) tile for a single animation frame."""
    pw, ph = pane_size
    if ref_build is not None and skin_build is not None:
        ref_img, _ = render_anim_frame(
            anim_data=anim_data,
            base_build=ref_build,
            clip_name=clip_name,
            facing_mask=facing_mask,
            frame_index=frame_index,
            hide_layers=hide_layers,
            canvas_size=(pw, ph),
            scale=scale,
            bg_rgba=(34, 38, 44, 255),
            auto_frame=True,
        )
        tgt_img, rep = render_anim_frame(
            anim_data=anim_data,
            base_build=base_build,
            clip_name=clip_name,
            facing_mask=facing_mask,
            frame_index=frame_index,
            override_build=skin_build,
            override_map=override_map,
            hide_layers=hide_layers,
            canvas_size=(pw, ph),
            scale=scale,
            bg_rgba=(42, 46, 54, 255),
            auto_frame=True,
        )
        tile = Image.new("RGBA", (pw * 2, ph), (28, 32, 38, 255))
        tile.paste(ref_img, (0, 0))
        tile.paste(tgt_img, (pw, 0))
        draw = ImageDraw.Draw(tile)
        draw.line([(pw, 0), (pw, ph)], fill=(75, 84, 98, 255), width=1)
        draw.text((8, ph - 18), "Ref", fill=(165, 175, 190, 255))
        draw.text((pw + 8, ph - 18), "Skin", fill=(120, 220, 255, 255))
        return tile, rep

    target_img, rep = render_anim_frame(
        anim_data=anim_data,
        base_build=base_build,
        clip_name=clip_name,
        facing_mask=facing_mask,
        frame_index=frame_index,
        override_build=skin_build,
        override_map=override_map,
        hide_layers=hide_layers,
        canvas_size=(pw * 2, ph),
        scale=scale,
        auto_frame=True,
    )
    return target_img, rep


REVIEW_PASS_CRITERIA: dict[str, dict[str, object]] = {
    "face_alignment_ok": {
        "pass": True,
        "description": "五官/脸部位置与比例贴合头型，无明显偏移、悬空或超出轮廓",
    },
    "head_hair_join_ok": {
        "pass": True,
        "description": "头部、双马尾/后发衔接自然，无明显错位或穿帮",
    },
    "limb_hand_join_ok": {
        "pass": True,
        "description": "手臂、手掌、躯干、裙摆、脚部关节衔接正常，无断层/悬浮/比例失调",
    },
    "view_and_pose_match": {
        "pass": True,
        "description": "朝向、镜像、表情语义与左侧 Ref 一致",
    },
    "no_visual_artifacts": {
        "pass": True,
        "description": "无绿边、黑块、残缺、拉伸变形或错误图层穿帮",
    },
}


def build_review_template(summary: dict, *, per_page: int = 12) -> dict:
    """Build an unapproved structured review.example.json template from review summary."""
    step = max(1, int(per_page))
    items_out: dict[str, dict] = {}
    for item in summary.get("items", []):
        idx = int(item.get("index", len(items_out) + 1))
        sheet_num = ((idx - 1) // step) + 1
        items_out[item["id"]] = {
            "index": idx,
            "clip": item["clip"],
            "facing_label": item["facing_label"],
            "frame_index": item["frame_index"],
            "sheet": f"sheets/review_sheet_{sheet_num:02d}.png",
            "frame_rel_path": item["frame_rel_path"],
            "overridden_slots": list(item.get("overridden_slots", [])),
            "approved": False,
            "severity": None,
            "checks": {k: None for k in REVIEW_PASS_CRITERIA},
            "suspect_slots": [],
            "issue": "",
            "expected": "",
        }
    return {
        "schema": "dst_skin_anim_review_v1",
        "skin_build": summary.get("skin_build"),
        "suite": summary.get("suite"),
        "generated_at": summary.get("generated_at"),
        "total_rendered": summary.get("total_rendered", len(items_out)),
        "pass_criteria": REVIEW_PASS_CRITERIA,
        "items": items_out,
    }


def _load_review_items_for_html(out_dir: Path) -> dict[str, dict]:
    """Load optional out_dir/review.json entries in a compact map for review.html."""
    review_path = out_dir / "review.json"
    if not review_path.is_file():
        return {}
    try:
        raw = json.loads(review_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    raw_items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(raw_items, dict):
        return {}
    compact: dict[str, dict] = {}
    for item_id, entry in raw_items.items():
        if not isinstance(entry, dict):
            continue
        compact[item_id] = {
            "approved": bool(entry.get("approved", False)),
            "severity": entry.get("severity"),
            "issue": str(entry.get("issue") or "").strip(),
            "expected": str(entry.get("expected") or "").strip(),
            "suspect_slots": list(entry.get("suspect_slots") or []),
        }
    return compact


def _build_review_html(
    summary: dict,
    out_html: Path,
    *,
    asset_prefix: str = "",
    tuner_href: str = "../skin_tuner.html",
    player_href: str = "../anim_player.html",
    review_items: dict[str, dict] | None = None,
) -> Path:
    """Write a self-contained offline interactive HTML animation review board."""
    payload = dict(summary)
    payload["asset_prefix"] = asset_prefix
    payload["tuner_href"] = tuner_href
    payload["player_href"] = player_href
    payload["review_items"] = review_items or {}
    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>DST Skin Animation Review - {summary.get('skin_build', 'skin')}</title>
<style>
  :root {{ --bg:#161920; --panel:#1e232d; --card:#242a36; --border:#333c4d; --text:#e6ecf5; --muted:#95a2b8; --accent:#58c4ff; --warn:#ff6b6b; --ok:#4ade80; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--text); }}
  header {{ position:sticky; top:0; z-index:10; background:var(--panel); border-bottom:1px solid var(--border); padding:12px 18px; display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:space-between; }}
  h1 {{ margin:0; font-size:16px; }}
  .stats {{ font-size:12px; color:var(--muted); display:flex; gap:14px; flex-wrap:wrap; }}
  .controls {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
  input, select, button {{ background:#141820; color:var(--text); border:1px solid var(--border); border-radius:6px; padding:6px 10px; font-size:12px; }}
  button {{ cursor:pointer; }}
  button.primary {{ background:#1c4b6e; border-color:#2d7cb8; }}
  button.warn {{ background:#5c2026; border-color:#a13640; }}
  main {{ padding:16px; }}
  .rework-bar {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:12px 14px; margin-bottom:14px; display:none; }}
  .rework-bar.visible {{ display:block; }}
  .rework-bar code {{ display:block; margin-top:8px; padding:8px; background:#11141a; border-radius:6px; font-size:12px; color:#8be9fd; word-break:break-all; }}
  .plan-rows {{ margin-top:8px; display:flex; flex-direction:column; gap:6px; }}
  .plan-row {{ display:grid; grid-template-columns:130px 1fr 1fr 28px; gap:6px; align-items:center; }}
  .plan-row .slot-tag {{ font-family:monospace; font-size:12px; color:var(--accent); }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(460px, 1fr)); gap:12px; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:8px; overflow:hidden; display:flex; flex-direction:column; }}
  .card.flagged {{ border-color:var(--warn); box-shadow:0 0 0 1px var(--warn); }}
  .card-head {{ padding:8px 10px; display:flex; justify-content:space-between; align-items:center; gap:8px; border-bottom:1px solid var(--border); font-size:12px; }}
  .badge {{ padding:2px 6px; border-radius:4px; background:#2f3747; color:var(--accent); font-size:11px; }}
  .badge.tier-T0 {{ background:#164e3f; color:#6ee7b7; font-weight:600; }}
  .badge.tier-T1 {{ background:#1e3a5f; color:#7dd3fc; font-weight:600; }}
  .badge.tier-T2 {{ background:#4a3728; color:#fcd34d; }}
  .badge.tier-T3 {{ background:#323744; color:#cbd5e1; }}
  .badge.missing {{ background:#5c2026; color:#ffb3b3; }}
  .badge.sev-critical {{ background:#7f1d1d; color:#fecaca; font-weight:600; }}
  .badge.sev-major {{ background:#78350f; color:#fde68a; font-weight:600; }}
  .badge.sev-minor {{ background:#1e3a5f; color:#bae6fd; }}
  .issue-banner {{ padding:6px 10px; background:#2b1f24; border-top:1px solid #4a2a33; color:#fecdd3; font-size:11px; line-height:1.4; }}
  .issue-banner.minor {{ background:#1c2536; border-top-color:#2b3b54; color:#cbd5e1; }}
  .card img {{ width:100%; height:auto; display:block; background:#1b1f27; cursor:zoom-in; }}
  .slots {{ padding:8px 10px; font-size:11px; color:var(--muted); display:flex; flex-wrap:wrap; gap:4px; }}
  .slot-chip {{ background:#191e28; border:1px solid #2e3646; border-radius:4px; padding:2px 5px; cursor:pointer; }}
  .slot-chip.selected {{ background:#5c2026; border-color:#ff6b6b; color:#fff; }}
  .tuner-link {{ text-decoration:none; background:#1c4b6e; border:1px solid #2d7cb8; color:#e6ecf5; border-radius:6px; padding:4px 8px; font-size:11px; }}
  .tuner-link:hover {{ background:#26639c; }}
  .lightbox {{ position:fixed; inset:0; background:rgba(10,12,16,0.88); display:none; align-items:center; justify-content:center; z-index:100; padding:20px; cursor:zoom-out; }}
  .lightbox.open {{ display:flex; }}
  .lightbox img {{ max-width:95vw; max-height:92vh; border:1px solid var(--border); border-radius:8px; box-shadow:0 12px 40px rgba(0,0,0,0.6); }}
</style>
</head>
<body>
<header>
  <div>
    <h1>DST Skin Animation Review — <span id="skinName"></span></h1>
    <div class="stats" id="stats"></div>
  </div>
  <div class="controls">
    <input id="search" type="text" placeholder="搜索 clip / zip / 槽位 (如 hand:3)" style="width:220px" />
    <select id="tierFilter">
      <option value="all">全部常用度</option>
      <option value="common">常用动画 (T0+T1)</option>
      <option value="T0">T0 核心高频</option>
      <option value="T1">T1 常规玩法</option>
      <option value="T2">T2 表情/偶发</option>
      <option value="T3">T3 极低频/特殊</option>
    </select>
    <select id="zipFilter"><option value="">全部动画包</option></select>
    <select id="flagFilter">
      <option value="all">全部显示</option>
      <option value="flagged">仅看已标记问题</option>
      <option value="missing">仅看缺失槽位</option>
    </select>
    <div id="pager" style="display:flex;gap:6px;align-items:center;font-size:12px;">
      <button id="prevPage">上一页</button>
      <span id="pageInfo" style="color:var(--muted);">1/1</span>
      <button id="nextPage">下一页</button>
    </div>
    <button id="clearFlags">清空标记</button>
    <span style="font-size:11px;color:var(--muted);">提示：点击卡片下方槽位标签（如 <code>cheeks:0</code>）可展开返工计划并导出 <code>rework_plan.json</code></span>
  </div>
</header>
<main>
  <div class="rework-bar" id="reworkBar">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
      <strong id="reworkTitle">已选择待修补槽位：</strong>
      <div style="display:flex;gap:6px;flex-wrap:wrap;">
        <button class="primary" id="downloadPlan">导出 rework_plan.json</button>
        <button id="copyCmd">复制 --plan 命令</button>
      </div>
    </div>
    <div class="plan-rows" id="planRows"></div>
    <code id="reworkCmd"></code>
  </div>
  <div class="grid" id="grid"></div>
</main>
<div class="lightbox" id="lightbox"><img id="lightboxImg" src="" alt="preview" /></div>
<script>
const DATA = {payload_json};
const flaggedAnims = new Set();
const selectedSlots = new Map(); // slot -> {{ issue, expected }}
for (const [id, rv] of Object.entries(DATA.review_items || {{}})) {{
  if (rv && rv.approved === false && rv.severity && rv.severity !== 'ok') {{
    flaggedAnims.add(id);
  }}
}}

document.getElementById('skinName').textContent = DATA.skin_build + ' (' + DATA.suite + ')';
const tc = DATA.tier_counts || {{}};
document.getElementById('stats').innerHTML =
  `<span>动画帧数: <b>${{DATA.total_rendered}}</b></span>` +
  `<span>分级: <b>T0:${{tc.T0 || 0}} · T1:${{tc.T1 || 0}} · T2:${{tc.T2 || 0}} · T3:${{tc.T3 || 0}}</b></span>` +
  `<span>皮肤槽位覆盖: <b>${{DATA.coverage.covered_count}}/${{DATA.coverage.total_skin_slots}}</b> (${{DATA.coverage.coverage_pct}}%)</span>` +
  `<span>缺失贴图帧: <b>${{DATA.missing_frames_count}}</b></span>`;

const zipSel = document.getElementById('zipFilter');
[...new Set(DATA.items.map(i => i.anim_zip))].sort().forEach(z => {{
  const opt = document.createElement('option');
  opt.value = z; opt.textContent = z;
  zipSel.appendChild(opt);
}});

function buildPlanObject() {{
  const items = [];
  for (const [slot, meta] of [...selectedSlots.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {{
    const [symbol, fstr] = slot.split(':');
    items.push({{
      slot,
      symbol,
      frame: Number(fstr),
      issue: (meta.issue || '').trim(),
      expected: (meta.expected || '').trim(),
      anims: DATA.slot_to_anims[slot] || [],
    }});
  }}
  return {{
    skin_build: DATA.skin_build,
    suite: DATA.suite,
    items,
  }};
}}

function updateReworkBar() {{
  const bar = document.getElementById('reworkBar');
  const rows = document.getElementById('planRows');
  const entries = [...selectedSlots.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  if (!entries.length) {{ bar.classList.remove('visible'); rows.innerHTML = ''; return; }}
  bar.classList.add('visible');
  document.getElementById('reworkTitle').textContent = `已选 ${{entries.length}} 个待重绘槽位（可填写具体问题与期望效果，导出 rework_plan.json）：`;
  rows.innerHTML = '';
  for (const [slot, meta] of entries) {{
    const row = document.createElement('div');
    row.className = 'plan-row';
    row.innerHTML = `
      <span class="slot-tag">${{slot}}</span>
      <input type="text" data-k="issue" placeholder="issue: 观察到的具体错误（英文或中文）" />
      <input type="text" data-k="expected" placeholder="expected: 期望修正后的可观察效果" />
      <button title="移除">×</button>
    `;
    const inIssue = row.querySelector('[data-k="issue"]');
    const inExp = row.querySelector('[data-k="expected"]');
    inIssue.value = meta.issue || '';
    inExp.value = meta.expected || '';
    inIssue.oninput = () => {{ meta.issue = inIssue.value; }};
    inExp.oninput = () => {{ meta.expected = inExp.value; }};
    row.querySelector('button').onclick = () => {{
      selectedSlots.delete(slot);
      updateReworkBar();
      render();
    }};
    rows.appendChild(row);
  }}
  const slotArgs = entries.map(([s]) => s).join(' ');
  const cmd = `.\\\\.venv-mcp\\\\Scripts\\\\python.exe python/tools/skin_pipeline.py rework --name rework_next --plan rework_plan.json  # 或 --slots ${{slotArgs}}`;
  document.getElementById('reworkCmd').textContent = cmd;
}}

document.getElementById('downloadPlan').onclick = () => {{
  const blob = new Blob([JSON.stringify(buildPlanObject(), null, 2)], {{ type: 'application/json' }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = 'rework_plan.json'; a.click();
  URL.revokeObjectURL(url);
}};
document.getElementById('copyCmd').onclick = () => {{
  navigator.clipboard?.writeText(document.getElementById('reworkCmd').textContent);
}};
document.getElementById('clearFlags').onclick = () => {{
  flaggedAnims.clear(); selectedSlots.clear(); updateReworkBar(); render();
}};

const PAGE_SIZE = 48;
let currentPage = 1;

function render() {{
  const q = document.getElementById('search').value.trim().toLowerCase();
  const tf = document.getElementById('tierFilter').value;
  const zf = zipSel.value;
  const ff = document.getElementById('flagFilter').value;
  const matched = [];
  for (const item of DATA.items) {{
    const tier = item.tier || 'T1';
    if (tf === 'common' && tier !== 'T0' && tier !== 'T1') continue;
    else if (tf !== 'all' && tf !== 'common' && tier !== tf) continue;
    if (zf && item.anim_zip !== zf) continue;
    const isFlagged = flaggedAnims.has(item.id);
    if (ff === 'flagged' && !isFlagged) continue;
    if (ff === 'missing' && item.missing_count === 0) continue;
    const hay = (item.anim_zip + ' ' + item.clip + ' ' + tier + ' ' + (item.tier_label || '') + ' ' + item.facing_label + ' ' + item.overridden_slots.join(' ')).toLowerCase();
    if (q && !hay.includes(q)) continue;
    matched.push(item);
  }}
  const totalPages = Math.max(1, Math.ceil(matched.length / PAGE_SIZE));
  if (currentPage > totalPages) currentPage = totalPages;
  if (currentPage < 1) currentPage = 1;
  document.getElementById('pageInfo').textContent = `${{currentPage}}/${{totalPages}} (共${{matched.length}}项)`;
  document.getElementById('prevPage').disabled = currentPage <= 1;
  document.getElementById('nextPage').disabled = currentPage >= totalPages;

  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  const slice = matched.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  for (const item of slice) {{
    const isFlagged = flaggedAnims.has(item.id);
    const rv = (DATA.review_items || {{}})[item.id] || null;
    const sev = rv && rv.severity && rv.severity !== 'ok' ? String(rv.severity).toLowerCase() : '';
    const sevBadge = sev ? `<span class="badge sev-${{sev}}">${{sev.toUpperCase()}}</span>` : '';
    const issueHtml = rv && rv.issue
      ? `<div class="issue-banner ${{sev === 'minor' ? 'minor' : ''}}"><b>${{sev ? sev.toUpperCase() : 'NOTE'}}:</b> ${{rv.issue}}${{rv.suspect_slots && rv.suspect_slots.length ? ` <code>[${{rv.suspect_slots.join(', ')}}]</code>` : ''}}</div>`
      : '';

    const imgUrl = (DATA.asset_prefix || '') + item.frame_rel_path + '?v=' + (DATA.generated_at || 0);
    const tunerUrl = (DATA.tuner_href || '../skin_tuner.html') + '?clip=' + encodeURIComponent(item.clip);
    const playerUrl = (DATA.player_href || '../anim_player.html') + '?clip=' + encodeURIComponent(item.clip);
    const tier = item.tier || 'T1';
    const tierBadge = `<span class="badge tier-${{tier}}" title="${{item.tier_label || tier}}">${{tier}}</span>`;
    const card = document.createElement('div');
    card.className = 'card' + (isFlagged ? ' flagged' : '');
    card.innerHTML = `
      <div class="card-head">
        <div><strong>#${{item.index}} ${{item.clip}}</strong> ${{tierBadge}} <span class="badge">${{item.facing_label}} f${{item.frame_index}}/${{item.total_frames}}</span> ${{sevBadge}} ${{item.missing_count ? `<span class="badge missing">MISS ${{item.missing_count}}</span>` : ''}}</div>
        <div style="display:flex;gap:6px;align-items:center;">
          <span style="color:var(--muted);font-size:11px;">${{item.anim_zip.replace('.zip','')}}</span>
          <a class="tuner-link" href="${{playerUrl}}" target="_blank" title="在动画播放器中查看完整序列帧动画">播放</a>
          <a class="tuner-link" href="${{tunerUrl}}" target="_blank" title="在骨骼调试器中检查/调整此动画的 Offset">调 Offset</a>
          <button class="${{isFlagged ? 'warn' : ''}}" data-flag="${{item.id}}">${{isFlagged ? '已标记' : '标记问题'}}</button>
        </div>
      </div>
      <img loading="lazy" src="${{imgUrl}}" alt="${{item.clip}}" title="点击放大查看高清原图" />
      ${{issueHtml}}
      <div class="slots"></div>
    `;
    card.querySelector('img').onclick = () => {{
      const lb = document.getElementById('lightbox');
      document.getElementById('lightboxImg').src = imgUrl;
      lb.classList.add('open');
    }};
    card.querySelector('[data-flag]').onclick = () => {{
      if (flaggedAnims.has(item.id)) flaggedAnims.delete(item.id);
      else flaggedAnims.add(item.id);
      render();
    }};
    const slotWrap = card.querySelector('.slots');
    for (const s of item.overridden_slots) {{
      const chip = document.createElement('span');
      chip.className = 'slot-chip' + (selectedSlots.has(s) ? ' selected' : '');
      chip.textContent = s;
      chip.onclick = () => {{
        if (selectedSlots.has(s)) selectedSlots.delete(s);
        else selectedSlots.set(s, {{ issue: '', expected: '' }});
        updateReworkBar();
        render();
      }};
      slotWrap.appendChild(chip);
    }}
    grid.appendChild(card);
  }}
}}

document.getElementById('lightbox').onclick = () => {{
  document.getElementById('lightbox').classList.remove('open');
}};
window.addEventListener('keydown', (e) => {{
  if (e.key === 'Escape') document.getElementById('lightbox').classList.remove('open');
}});

document.getElementById('search').oninput = () => {{ currentPage = 1; render(); }};
document.getElementById('tierFilter').onchange = () => {{ currentPage = 1; render(); }};
zipSel.onchange = () => {{ currentPage = 1; render(); }};
document.getElementById('flagFilter').onchange = () => {{ currentPage = 1; render(); }};
document.getElementById('prevPage').onclick = () => {{ currentPage -= 1; render(); window.scrollTo(0, 0); }};
document.getElementById('nextPage').onclick = () => {{ currentPage += 1; render(); window.scrollTo(0, 0); }};
render();
</script>
</body>
</html>
"""
    out_html.write_text(html, encoding="utf-8")
    return out_html


def render_animation_review(
    base_zip: Path,
    skin_zip: Path,
    ref_zip: Path | None,
    out_dir: Path,
    *,
    anim_dir: Path = DST_ANIM_DIR,
    suite: str = "compact",
    anim_zips: list[str] | None = None,
    all_facings: bool = False,
    include_mount: bool = False,
    cols: int = 3,
    rows_per_sheet: int = 4,
    pane_size: tuple[int, int] = (480, 540),
    scale: float = 1.45,
    html_out: Path | None = None,
) -> dict:
    """Render 1 representative frame per animation clip with ref comparison, contact sheets, coverage report, and HTML board."""
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = out_dir / "frames"
    sheets_dir = out_dir / "sheets"
    frames_dir.mkdir(parents=True, exist_ok=True)
    sheets_dir.mkdir(parents=True, exist_ok=True)
    for old_png in frames_dir.glob("*.png"):
        old_png.unlink(missing_ok=True)
    for old_png in sheets_dir.glob("*.png"):
        old_png.unlink(missing_ok=True)

    base_build = load_build(base_zip)
    skin_build = load_build(skin_zip)
    ref_build = load_build(ref_zip) if (ref_zip and ref_zip.is_file()) else None
    override_map = build_companion_override_map()

    # Cache parsed anim.bin per zip
    anim_cache: dict[str, dict] = {}

    def get_anim_data(zip_name: str) -> dict | None:
        return _load_anim_cached_by_name(zip_name, anim_dir, anim_cache)

    # Build ordered list of (zip_name, clip_name, facing_mask, facing_label, explicit_frame, use_carry, tuner_clip_key)
    targets: list[tuple[str, str, int, str, int | None, bool, str]] = []
    if suite == "core" and not anim_zips:
        for zname, clip, flabel, fidx in CORE_REVIEW_SPECS:
            adata = get_anim_data(zname)
            if adata is None:
                continue
            fmask = _facing_mask_from_label(flabel)
            anim = _find_anim(adata, clip, fmask)
            if anim is None:
                continue
            targets.append((zname, clip, anim["facing"], flabel if anim["facing"] != 0xFF else "all", fidx, False, clip))
    elif suite == "compact" and not anim_zips:
        targets = resolve_compact_specs(
            skin_build,
            anim_dir,
            include_mount=include_mount,
            anim_cache=anim_cache,
        )
    else:
        if anim_zips:
            zip_list = list(anim_zips)
        else:
            zip_list = discover_generic_anim_zips(anim_dir, include_mount=include_mount)

        for zname in zip_list:
            adata = get_anim_data(zname)
            if adata is None:
                continue
            by_clip: dict[str, list[dict]] = {}
            for anim in adata["anims"]:
                if anim["frames"]:
                    by_clip.setdefault(anim["name"], []).append(anim)
            if not by_clip:
                continue
            clip_names = sorted(by_clip.keys())
            if not anim_zips:
                clean_names = [c for c in clip_names if is_clean_review_clip(c)]
                if clean_names:
                    clip_names = clean_names
                else:
                    non_junk = [c for c in clip_names if not _JUNK_CLIP_RE.match(c)]
                    clip_names = non_junk if non_junk else clip_names[:1]

            for clip_name in clip_names:
                variants = by_clip[clip_name]
                if all_facings:
                    chosen_variants = sorted(variants, key=lambda a: (_facing_priority(a), a["facing"]))
                else:
                    chosen_variants = [min(variants, key=_facing_priority)]
                for anim in chosen_variants:
                    flabel = _facing_label_from_mask(anim["facing"])
                    targets.append((zname, clip_name, anim["facing"], flabel, None, False, clip_name))

        # Append targeted booster frames in full/generic mode so rare slots (foot:4, torso:9, ARM_carry X-ray) hit 100%
        if suite in ("full", "generic") and not anim_zips:
            for bz, bclip, bflabel, bfidx, use_carry, tkey in COVERAGE_BOOSTER_SPECS:
                adata = get_anim_data(bz)
                if adata is None:
                    continue
                fmask = _facing_mask_from_label(bflabel)
                anim = _find_anim(adata, bclip, fmask)
                if anim is None or not anim["frames"]:
                    continue
                targets.append((bz, bclip, anim["facing"], bflabel if anim["facing"] != 0xFF else "all", bfidx, use_carry, tkey))

    # Stable-sort rendered targets by gameplay frequency tier (T0 -> T1 -> T2 -> T3)
    targets = [
        row
        for _, row in sorted(
            enumerate(targets),
            key=lambda pair: (classify_animation_tier(pair[1][0], pair[1][6])[1], pair[0]),
        )
    ]

    def _render_job(job: tuple[int, str, str, int, str, int | None, bool, str]) -> tuple[dict, Image.Image] | None:
        idx, zname, clip_name, fmask, flabel, explicit_frame, use_carry, tkey = job
        adata = anim_cache.get(zname)
        if adata is None:
            return None
        anim = _find_anim(adata, clip_name, fmask)
        if anim is None or not anim["frames"]:
            return None
        fidx = _pick_representative_frame(anim, clip_name, explicit_frame)
        tile_img, rep = _render_pair_tile(
            anim_data=adata,
            base_build=base_build,
            skin_build=skin_build,
            ref_build=ref_build,
            override_map=override_map,
            clip_name=clip_name,
            facing_mask=fmask,
            frame_index=fidx,
            pane_size=pane_size,
            scale=scale,
            hide_layers=CARRY_HIDE_LAYERS if use_carry else DEFAULT_HIDE_LAYERS,
        )
        overridden_slots = sorted({
            f"{e['source_symbol']}:{e['frame_num']}"
            for e in rep["used"]
            if e["overridden"] and e["source_symbol"] != "long_hair_blank"
        })
        if not anim_zips and suite in ("compact", "full", "generic") and not overridden_slots:
            return None

        zip_stem = Path(zname).stem
        safe_clip = "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in tkey)
        frame_name = f"{idx:03d}_{zip_stem}__{safe_clip}__{flabel}_f{fidx}.png"
        frame_path = frames_dir / frame_name
        tile_img.save(frame_path)

        skin_missing = [m for m in rep["missing"] if m.get("overridden")]
        tier, tier_order, tier_label = classify_animation_tier(Path(zname).name, tkey)
        item_id = f"{zip_stem}:{tkey}:{flabel}"
        item_meta = {
            "index": idx,
            "id": item_id,
            "anim_zip": Path(zname).name,
            "clip": tkey,
            "tier": tier,
            "tier_order": tier_order,
            "tier_label": tier_label,
            "facing_mask": fmask,
            "facing_label": flabel,
            "frame_index": fidx,
            "total_frames": len(anim["frames"]),
            "frame_rel_path": f"frames/{frame_name}",
            "used_count": rep["used_count"],
            "missing_count": len(skin_missing),
            "missing": skin_missing,
            "overridden_slots": overridden_slots,
        }
        return item_meta, tile_img

    jobs = [(idx, z, c, fm, fl, ef, uc, tk) for idx, (z, c, fm, fl, ef, uc, tk) in enumerate(targets, start=1)]
    max_workers = min(8, max(1, os.cpu_count() or 4))
    if len(jobs) > 4 and max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            raw_results = list(pool.map(_render_job, jobs))
    else:
        raw_results = [_render_job(j) for j in jobs]

    items: list[dict] = []
    tile_entries: list[tuple[dict, Image.Image]] = []
    covered_slots: set[str] = set()
    slot_to_anims: dict[str, list[str]] = {}
    missing_frames_count = 0

    for res_entry in raw_results:
        if res_entry is None:
            continue
        item_meta, tile_img = res_entry
        item_meta["index"] = len(items) + 1
        for slot_key in item_meta["overridden_slots"]:
            covered_slots.add(slot_key)
            slot_to_anims.setdefault(slot_key, []).append(item_meta["id"])
        if item_meta["missing_count"] > 0:
            missing_frames_count += 1
        items.append(item_meta)
        tile_entries.append((item_meta, tile_img))

    # Compose paginated contact sheets (downscale high-res tiles cleanly for compact overview sheets)
    per_page = max(1, cols * rows_per_sheet)
    header_h = 26
    footer_h = 20
    raw_tile_w = pane_size[0] * 2
    raw_tile_h = pane_size[1]
    sheet_tile_w = min(raw_tile_w, 560)
    sheet_tile_h = max(1, int(round(raw_tile_h * (sheet_tile_w / float(raw_tile_w)))))
    cell_w = sheet_tile_w
    cell_h = header_h + sheet_tile_h + footer_h
    sheet_paths: list[str] = []

    for page_idx in range(0, len(tile_entries), per_page):
        chunk = tile_entries[page_idx:page_idx + per_page]
        n_rows = (len(chunk) + cols - 1) // cols
        sheet = Image.new("RGBA", (cols * cell_w, n_rows * cell_h), (22, 25, 32, 255))
        draw = ImageDraw.Draw(sheet)
        for local_i, (meta, tile_img) in enumerate(chunk):
            r = local_i // cols
            c = local_i % cols
            x0, y0 = c * cell_w, r * cell_h
            # Header bar
            hdr_bg = (92, 32, 38, 255) if meta["missing_count"] > 0 else (30, 36, 46, 255)
            draw.rectangle([x0, y0, x0 + cell_w - 1, y0 + header_h - 1], fill=hdr_bg)
            title = f"#{meta['index']:02d} [{meta.get('tier', 'T1')}] {meta['clip']} ({meta['facing_label']} f{meta['frame_index']}/{meta['total_frames']}) [{meta['anim_zip'].replace('.zip', '')}]"
            draw.text((x0 + 6, y0 + 6), title[:72], fill=(235, 242, 250, 255))
            pasted = tile_img if tile_img.size == (sheet_tile_w, sheet_tile_h) else tile_img.resize((sheet_tile_w, sheet_tile_h), Image.Resampling.LANCZOS)
            sheet.paste(pasted, (x0, y0 + header_h))
            # Footer slot summary
            draw.rectangle([x0, y0 + header_h + sheet_tile_h, x0 + cell_w - 1, y0 + cell_h - 1], fill=(24, 28, 36, 255))
            key_slots = [s for s in meta["overridden_slots"] if s.startswith(("headbase:", "face:", "hand:", "arm_lower:", "torso:", "skirt:"))]
            footer_txt = " ".join(key_slots[:6])
            if meta["missing_count"] > 0:
                footer_txt = f"MISSING({meta['missing_count']}) | " + footer_txt
            draw.text((x0 + 6, y0 + header_h + sheet_tile_h + 4), footer_txt[:78], fill=(165, 180, 200, 255))
            draw.rectangle([x0, y0, x0 + cell_w - 1, y0 + cell_h - 1], outline=(68, 76, 92, 255), width=1)

        page_num = (page_idx // per_page) + 1
        sp = sheets_dir / f"review_sheet_{page_num:02d}.png"
        sheet.save(sp)
        sheet_paths.append(str(sp))

    # Coverage analysis against all editable symbols in skin_build
    all_skin_slots: set[str] = set()
    for sym in COMPANION_BODY_SYMBOLS:
        if sym.endswith("_wide") or sym in ("headbase_hat", "hair_hat"):
            continue
        for fnum, finfo in skin_build["symbols"].get(sym, {}).items():
            if float(finfo.get("w", 0.0)) <= 4.0 and float(finfo.get("h", 0.0)) <= 4.0:
                continue
            all_skin_slots.add(f"{sym}:{finfo['framenum']}")

    uncovered_slots = sorted(all_skin_slots - covered_slots)
    cov_pct = round(100.0 * len(covered_slots & all_skin_slots) / max(1, len(all_skin_slots)), 1)

    tier_counts = {k: 0 for k in ANIM_TIER_META}
    for it in items:
        t = it.get("tier", "T1")
        tier_counts[t] = tier_counts.get(t, 0) + 1

    summary = {
        "skin_build": skin_build["build_name"],
        "base_build": base_build["build_name"],
        "ref_build": ref_build["build_name"] if ref_build else None,
        "suite": suite,
        "generated_at": int(time.time()),
        "total_rendered": len(items),
        "tier_counts": tier_counts,
        "missing_frames_count": missing_frames_count,
        "sheets": [f"sheets/{Path(p).name}" for p in sheet_paths],
        "coverage": {
            "covered_count": len(covered_slots & all_skin_slots),
            "total_skin_slots": len(all_skin_slots),
            "coverage_pct": cov_pct,
            "uncovered_slots": uncovered_slots,
        },
        "slot_to_anims": {k: sorted(v) for k, v in sorted(slot_to_anims.items())},
        "items": items,
    }

    report_json = out_dir / "review_report.json"
    report_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    review_example_json = out_dir / "review.example.json"
    review_template = build_review_template(summary, per_page=per_page)
    review_example_json.write_text(json.dumps(review_template, ensure_ascii=False, indent=2), encoding="utf-8")

    # Markdown report
    md_lines = [
        f"# DST Skin Animation Review: `{skin_build['build_name']}` ({suite})",
        "",
        f"- Rendered animation frames: **{len(items)}**",
        f"- Contact sheets: **{len(sheet_paths)}**",
        f"- Skin slot coverage: **{summary['coverage']['covered_count']}/{summary['coverage']['total_skin_slots']} ({cov_pct}%)**",
        f"- Frames with missing symbols: **{missing_frames_count}**",
        "",
        "## Contact Sheets",
    ]
    for sp in sheet_paths:
        md_lines.append(f"- `{Path(sp).name}`")
    if uncovered_slots:
        md_lines.extend(["", "## Uncovered Skin Slots", ", ".join(f"`{s}`" for s in uncovered_slots)])
    md_lines.extend(["", "## Animation Index", "", "| # | Tier | Zip | Clip | Facing | Frame | Missing | Key Overridden Slots |", "|---:|:---:|---|---|---|---:|---:|---|"])
    for it in items:
        ks = ", ".join(it["overridden_slots"][:8])
        md_lines.append(
            f"| {it['index']} | {it.get('tier', 'T1')} | `{it['anim_zip']}` | `{it['clip']}` | {it['facing_label']} | {it['frame_index']}/{it['total_frames']} | {it['missing_count']} | {ks} |"
        )
    report_md = out_dir / "review_report.md"
    report_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    target_html = html_out or (out_dir / "review.html")
    target_html.parent.mkdir(parents=True, exist_ok=True)
    if target_html.parent.resolve() == out_dir.resolve():
        asset_prefix = ""
        tuner_href = "../skin_tuner.html"
        player_href = "../anim_player.html"
    else:
        rel = out_dir.resolve().relative_to(target_html.parent.resolve()).as_posix()
        asset_prefix = rel.rstrip("/") + "/"
        tuner_href = "skin_tuner.html"
        player_href = "anim_player.html"
    review_html = _build_review_html(
        summary,
        target_html,
        asset_prefix=asset_prefix,
        tuner_href=tuner_href,
        player_href=player_href,
        review_items=_load_review_items_for_html(out_dir),
    )

    return {
        "out_dir": str(out_dir),
        "sheets": sheet_paths,
        "report_json": str(report_json),
        "review_example_json": str(review_example_json),
        "report_md": str(report_md),
        "review_html": str(review_html),
        "summary": summary,
    }


def rebuild_review_html(out_dir: Path, html_out: Path | None = None, *, per_page: int = 12) -> Path:
    """Rebuild review.html (and missing review.example.json) from existing review_report.json & review.json."""
    summary = json.loads((out_dir / "review_report.json").read_text(encoding="utf-8"))
    example_path = out_dir / "review.example.json"
    if not example_path.is_file():
        template = build_review_template(summary, per_page=per_page)
        example_path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")

    target_html = html_out or (out_dir / "review.html")
    target_html.parent.mkdir(parents=True, exist_ok=True)
    if target_html.parent.resolve() == out_dir.resolve():
        asset_prefix = ""
        tuner_href = "../skin_tuner.html"
        player_href = "../anim_player.html"
    else:
        rel = out_dir.resolve().relative_to(target_html.parent.resolve()).as_posix()
        asset_prefix = rel.rstrip("/") + "/"
        tuner_href = "skin_tuner.html"
        player_href = "anim_player.html"
    return _build_review_html(
        summary,
        target_html,
        asset_prefix=asset_prefix,
        tuner_href=tuner_href,
        player_href=player_href,
        review_items=_load_review_items_for_html(out_dir),
    )


def _compute_clip_union_bbox(anim: dict) -> tuple[float, float, float, float] | None:
    xs0, ys0, xs1, ys1 = [], [], [], []
    for fr in anim.get("frames", []):
        bbox = fr.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        fx, fy, fw, fh = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
        if fw <= 0 and fh <= 0:
            continue
        xs0.append(fx - fw * 0.5)
        xs1.append(fx + fw * 0.5)
        ys0.append(fy - fh * 0.5)
        ys1.append(fy + fh * 0.5)
    if not xs0:
        return None
    min_x, max_x = min(xs0), max(xs1)
    min_y, max_y = min(ys0), max(ys1)
    return ((min_x + max_x) * 0.5, (min_y + max_y) * 0.5, max_x - min_x, max_y - min_y)


def render_clip_gif(
    anim_zip: Path,
    base_zip: Path,
    skin_zip: Path,
    out_gif: Path,
    *,
    ref_zip: Path | None = None,
    clip_name: str = "idle_loop",
    facing: str = "front",
    scale: float = 1.15,
    pane_size: tuple[int, int] = (280, 320),
    fps: float | None = None,
    hide_layers: frozenset[str] = DEFAULT_HIDE_LAYERS,
) -> Path:
    """Render every frame of a DST animation clip into a smooth looping GIF (optionally Ref vs Skin)."""
    with zipfile.ZipFile(anim_zip, "r") as zf:
        anim_data = parse_anim_bin(zf.read("anim.bin"))

    base_build = load_build(base_zip)
    skin_build = load_build(skin_zip)
    ref_build = load_build(ref_zip) if (ref_zip and ref_zip.is_file()) else None
    override_map = build_companion_override_map()

    fmask = _facing_mask_from_label(facing)
    anim = _find_anim(anim_data, clip_name, fmask)
    if anim is None or not anim.get("frames"):
        raise ValueError(f"Clip {clip_name!r} with facing {facing!r} not found in {anim_zip}")

    union_bbox = _compute_clip_union_bbox(anim)
    eff_fps = float(fps) if (fps and fps > 0) else float(anim.get("framerate") or 30.0)
    frame_ms = max(20, int(round(1000.0 / max(1.0, eff_fps))))
    pw, ph = pane_size
    gif_frames: list[Image.Image] = []

    for fidx in range(len(anim["frames"])):
        skin_img, _ = render_anim_frame(
            anim_data=anim_data,
            base_build=base_build,
            clip_name=clip_name,
            facing_mask=anim["facing"],
            frame_index=fidx,
            override_build=skin_build,
            override_map=override_map,
            hide_layers=hide_layers,
            canvas_size=(pw, ph),
            scale=scale,
            bg_rgba=(42, 46, 54, 255),
            auto_frame=True,
            bbox_override=union_bbox,
        )
        if ref_build is not None:
            ref_img, _ = render_anim_frame(
                anim_data=anim_data,
                base_build=ref_build,
                clip_name=clip_name,
                facing_mask=anim["facing"],
                frame_index=fidx,
                hide_layers=hide_layers,
                canvas_size=(pw, ph),
                scale=scale,
                bg_rgba=(34, 38, 44, 255),
                auto_frame=True,
                bbox_override=union_bbox,
            )
            tile = Image.new("RGBA", (pw * 2, ph), (28, 32, 38, 255))
            tile.paste(ref_img, (0, 0))
            tile.paste(skin_img, (pw, 0))
            draw = ImageDraw.Draw(tile)
            draw.line([(pw, 0), (pw, ph)], fill=(75, 84, 98, 255), width=1)
            draw.text((8, ph - 18), "Ref", fill=(165, 175, 190, 255))
            draw.text((pw + 8, ph - 18), "Skin", fill=(120, 220, 255, 255))
            frame_rgba = tile
        else:
            frame_rgba = skin_img
        gif_frames.append(frame_rgba.convert("P", palette=Image.Palette.ADAPTIVE, colors=255))

    out_gif.parent.mkdir(parents=True, exist_ok=True)
    gif_frames[0].save(
        out_gif,
        save_all=True,
        append_images=gif_frames[1:],
        duration=frame_ms,
        loop=0,
        disposal=2,
    )
    return out_gif


def main() -> int:
    parser = argparse.ArgumentParser(description="DST skeletal animation offline preview renderer")
    parser.add_argument("--anim", type=Path, default=DST_ANIM_DIR / "player_idles.zip")
    parser.add_argument("--base", type=Path, default=DST_ANIM_DIR / "wilson.zip")
    parser.add_argument("--skin", type=Path, default=None)
    parser.add_argument("--ref", type=Path, default=DST_ANIM_DIR / "wendy.zip")
    parser.add_argument("--clip", default="idle_loop")
    parser.add_argument("--frame", type=int, default=0)
    parser.add_argument("--scale", type=float, default=1.12)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    result = render_comparison_sheet(
        anim_zip=args.anim,
        base_zip=args.base,
        skin_zip=args.skin,
        ref_zip=args.ref,
        out_dir=args.out,
        clip_name=args.clip,
        frame_index=args.frame,
        scale=args.scale,
    )
    print(result["sheet"])
    print(result["report"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
