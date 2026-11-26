"""Generate a standalone interactive HTML rig/offset tuner for DST character skins."""
from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path
import zipfile

try:
    from tools.skin_pipeline import PIVOT_SCALED_SYMBOLS, _parse_offset_entry
    from tools.skin_preview import (
        CARRY_HIDE_LAYERS,
        CORE_REVIEW_SPECS,
        COVERAGE_BOOSTER_SPECS,
        DEFAULT_HIDE_LAYERS,
        DST_ANIM_DIR,
        FACING_DOWN,
        FACING_SIDE,
        FACING_UP,
        _JUNK_CLIP_RE,
        _find_anim,
        _pick_representative_frame,
        build_companion_override_map,
        classify_animation_tier,
        discover_generic_anim_zips,
        is_clean_review_clip,
        load_build,
        parse_anim_bin,
        resolve_compact_specs,
    )
except ImportError:
    from skin_pipeline import PIVOT_SCALED_SYMBOLS, _parse_offset_entry
    from skin_preview import (
        CARRY_HIDE_LAYERS,
        CORE_REVIEW_SPECS,
        COVERAGE_BOOSTER_SPECS,
        DEFAULT_HIDE_LAYERS,
        DST_ANIM_DIR,
        FACING_DOWN,
        FACING_SIDE,
        FACING_UP,
        _JUNK_CLIP_RE,
        _find_anim,
        _pick_representative_frame,
        build_companion_override_map,
        classify_animation_tier,
        discover_generic_anim_zips,
        is_clean_review_clip,
        load_build,
        parse_anim_bin,
        resolve_compact_specs,
    )


def _img_to_data_url(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _safe_dir_name(name: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in str(name)).strip("_") or "build"


def _extract_clip_payload(
    anim_zip: Path,
    clip_name: str,
    frame_index: int | None,
    base_build: dict,
    skin_build: dict,
    ref_build: dict | None,
    override_map: dict[str, str],
    existing_offsets: dict[str, tuple[float, float, float]],
    sprites_out: dict[str, dict],
    sprites_dir: Path | None = None,
    anim_data: dict | None = None,
    hide_layers: frozenset[str] = DEFAULT_HIDE_LAYERS,
) -> dict:
    if anim_data is None:
        with zipfile.ZipFile(anim_zip, "r") as zf:
            anim_data = parse_anim_bin(zf.read("anim.bin"))

    views_data: dict[str, dict] = {}
    for vname, fmask in (("front", FACING_DOWN), ("side", FACING_SIDE), ("back", FACING_UP)):
        anim = _find_anim(anim_data, clip_name, fmask)
        if anim is None or not anim["frames"]:
            views_data[vname] = {"target": [], "ref": []}
            continue
        fidx = _pick_representative_frame(anim, clip_name, frame_index)
        frame = anim["frames"][fidx % len(anim["frames"])]

        def collect(use_override: bool, build_obj: dict) -> list[dict]:
            elems: list[dict] = []
            for elem in reversed(frame["elements"]):
                layer = elem["layer"]
                sym = elem["symbol"]
                fnum = int(elem["frame_num"])
                if layer in hide_layers:
                    continue
                src_build = build_obj
                src_sym = sym
                overridden = False
                if use_override and sym in override_map:
                    src_build = skin_build
                    src_sym = override_map[sym]
                    overridden = True
                sym_frames = src_build["symbols"].get(src_sym)
                if not sym_frames:
                    continue
                finfo = sym_frames.get(fnum)
                if finfo is None:
                    if len(sym_frames) == 1 and 0 in sym_frames and src_sym == "long_hair_blank":
                        finfo = sym_frames[0]
                    else:
                        continue
                sprite_key = f"{src_build['build_name']}|{src_sym}|{finfo['framenum']}"
                slot_key = f"{src_sym}:{finfo['framenum']}"
                if sprite_key not in sprites_out:
                    cx = float(finfo["cx"])
                    cy = float(finfo["cy"])
                    w = float(finfo["w"])
                    h = float(finfo["h"])
                    # Undo currently baked offsets so the HTML tuner edits offsets.json idempotently
                    if overridden:
                        dx0, dy0, s0 = existing_offsets.get(
                            slot_key, existing_offsets.get(src_sym, (0.0, 0.0, 1.0))
                        )
                        if src_sym in PIVOT_SCALED_SYMBOLS:
                            cx = (cx - dx0) / max(1e-6, s0)
                            cy = (cy - dy0) / max(1e-6, s0)
                        else:
                            cx -= dx0
                            cy -= dy0
                        w /= s0
                        h /= s0
                    if sprites_dir is not None:
                        bdir = _safe_dir_name(src_build["build_name"])
                        rel_path = f"{sprites_dir.parent.name}/{sprites_dir.name}/{bdir}/{src_sym}_{finfo['framenum']}.png" if sprites_dir.parent.name == "tuner_data" else f"sprites/{bdir}/{src_sym}_{finfo['framenum']}.png"
                        out_png = sprites_dir / bdir / f"{src_sym}_{finfo['framenum']}.png"
                        out_png.parent.mkdir(parents=True, exist_ok=True)
                        finfo["image"].save(out_png, format="PNG", optimize=True)
                        sprite_url = rel_path
                    else:
                        sprite_url = _img_to_data_url(finfo["image"])
                    sprites_out[sprite_key] = {
                        "url": sprite_url,
                        "iw": finfo["image"].width,
                        "ih": finfo["image"].height,
                        "cx": round(cx, 3),
                        "cy": round(cy, 3),
                        "w": round(w, 3),
                        "h": round(h, 3),
                    }
                elems.append({
                    "sprite": sprite_key,
                    "slot": slot_key,
                    "symbol": src_sym,
                    "layer": layer,
                    "overridden": overridden,
                    "m": [round(float(v), 6) for v in elem["m"]],
                })
            return elems

        views_data[vname] = {
            "bbox": [round(float(v), 2) for v in frame.get("bbox", (0.0, 0.0, 0.0, 0.0))],
            "target": collect(True, base_build),
            "ref": collect(False, ref_build) if ref_build is not None else [],
        }
    return views_data


def build_tuner_html(
    skin_zip: Path,
    out_html: Path,
    *,
    base_zip: Path = DST_ANIM_DIR / "wilson.zip",
    ref_zip: Path = DST_ANIM_DIR / "wendy.zip",
    idle_anim_zip: Path = DST_ANIM_DIR / "player_idles.zip",
    run_anim_zip: Path = DST_ANIM_DIR / "player_basic.zip",
    offsets_json: Path | None = None,
    external_assets: bool = True,
    suite: str = "compact",
    include_mount: bool = False,
) -> Path:
    """Generate an interactive HTML rig/offset tuner with externalized sprites and configs."""
    base_build = load_build(base_zip)
    skin_build = load_build(skin_zip)
    ref_build = load_build(ref_zip) if ref_zip and ref_zip.is_file() else None
    override_map = build_companion_override_map()

    out_dir = out_html.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    local_offsets = out_dir / "offsets.json"

    raw_offsets: dict = {}
    if offsets_json and offsets_json.is_file():
        raw_offsets = json.loads(offsets_json.read_text(encoding="utf-8"))
    elif local_offsets.is_file():
        raw_offsets = json.loads(local_offsets.read_text(encoding="utf-8"))
    parsed_offsets: dict[str, tuple[float, float, float]] = {
        k: _parse_offset_entry(v) for k, v in raw_offsets.items()
    }

    tuner_data_dir = (out_dir / "tuner_data") if external_assets else None
    sprites_dir = (tuner_data_dir / "sprites") if tuner_data_dir is not None else None
    sprites: dict[str, dict] = {}
    clips: dict[str, dict] = {}
    clip_meta: dict[str, dict] = {}
    anim_cache: dict[Path, dict] = {}

    def load_anim_cached(p: Path) -> dict | None:
        if not p.is_file():
            return None
        if p not in anim_cache:
            try:
                with zipfile.ZipFile(p, "r") as zf:
                    if "anim.bin" not in zf.namelist():
                        return None
                    anim_cache[p] = parse_anim_bin(zf.read("anim.bin"))
            except Exception:
                return None
        return anim_cache[p]

    # Always include idle_loop and run_loop first: (anim_zip, clip_name, frame_override, use_carry, output_key)
    ordered_clip_specs: list[tuple[Path, str, int | None, bool, str]] = [
        (idle_anim_zip, "idle_loop", 0, False, "idle_loop"),
        (run_anim_zip, "run_loop", 3, False, "run_loop"),
    ]
    seen_clips = {"idle_loop", "run_loop"}
    anim_dir = idle_anim_zip.parent

    if suite == "compact":
        for zname, cname, _, _, foverride, use_carry, out_key in resolve_compact_specs(
            skin_build,
            anim_dir,
            include_mount=include_mount,
        ):
            if out_key in seen_clips:
                continue
            seen_clips.add(out_key)
            ordered_clip_specs.append((anim_dir / zname, cname, foverride, use_carry, out_key))
    elif suite == "core":
        for zname, cname, _, foverride in CORE_REVIEW_SPECS:
            if cname in seen_clips:
                continue
            seen_clips.add(cname)
            ordered_clip_specs.append((anim_dir / zname, cname, foverride, False, cname))
    else:
        # Include all core clips first in familiar order, then cleaned generic player clips
        for zname, cname, _, foverride in CORE_REVIEW_SPECS:
            if cname in seen_clips:
                continue
            seen_clips.add(cname)
            ordered_clip_specs.append((anim_dir / zname, cname, foverride, False, cname))
        for zname in discover_generic_anim_zips(anim_dir, include_mount=include_mount):
            zp = anim_dir / zname
            adata = load_anim_cached(zp)
            if adata is None:
                continue
            all_names = sorted({a["name"] for a in adata["anims"] if a["frames"]})
            clean_names = [c for c in all_names if is_clean_review_clip(c)]
            if clean_names:
                chosen_names = clean_names
            else:
                non_junk = [c for c in all_names if not _JUNK_CLIP_RE.match(c)]
                chosen_names = non_junk if non_junk else all_names[:1]
            for cname in chosen_names:
                if cname in seen_clips:
                    continue
                seen_clips.add(cname)
                ordered_clip_specs.append((zp, cname, None, False, cname))
        for bz, bclip, _, bfidx, use_carry, tkey in COVERAGE_BOOSTER_SPECS:
            if tkey in seen_clips:
                continue
            seen_clips.add(tkey)
            ordered_clip_specs.append((anim_dir / bz, bclip, bfidx, use_carry, tkey))

    for azip, cname, foverride, use_carry, out_key in ordered_clip_specs:
        adata = load_anim_cached(azip)
        if adata is None:
            continue
        payload_clip = _extract_clip_payload(
            azip, cname, foverride, base_build, skin_build, ref_build,
            override_map, parsed_offsets, sprites, sprites_dir,
            anim_data=adata,
            hide_layers=CARRY_HIDE_LAYERS if use_carry else DEFAULT_HIDE_LAYERS,
        )
        # Skip empty clips that render no overridden body parts
        has_target = any(
            any(e.get("overridden") and e.get("symbol") != "long_hair_blank" for e in vd.get("target", []))
            for vd in payload_clip.values()
        )
        if has_target:
            tier, tier_order, tier_label = classify_animation_tier(azip.name, out_key)
            clip_meta[out_key] = {
                "zip": azip.name,
                "tier": tier,
                "tier_order": tier_order,
                "tier_label": tier_label,
            }
            clips[out_key] = payload_clip

    clips = {
        k: v
        for k, v in sorted(
            clips.items(),
            key=lambda kv: (clip_meta[kv[0]]["tier_order"], 0 if kv[0] in ("idle_loop", "run_loop") else 1, kv[0]),
        )
    }

    initial_offsets = {
        k: {"dx": round(v[0], 2), "dy": round(v[1], 2), "scale": round(v[2], 3)}
        for k, v in parsed_offsets.items()
    }
    payload = {
        "buildName": skin_build["build_name"],
        "offsetsFileName": "offsets.json",
        "initialOffsets": initial_offsets,
        "sprites": sprites,
        "clipMeta": clip_meta,
        "clips": clips,
    }

    if external_assets and tuner_data_dir is not None:
        tuner_data_dir.mkdir(parents=True, exist_ok=True)
        (tuner_data_dir / "rig_data.js").write_text(
            "window.__DST_TUNER_PAYLOAD__ = " + json.dumps(payload, ensure_ascii=False) + ";\n",
            encoding="utf-8",
        )
        if not local_offsets.is_file() or (offsets_json and offsets_json.is_file() and offsets_json.resolve() != local_offsets.resolve()):
            local_offsets.write_text(
                json.dumps(raw_offsets, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        payload_loader = '<script src="tuner_data/rig_data.js"></script>\n<script>\nconst PAYLOAD = window.__DST_TUNER_PAYLOAD__;'
    else:
        payload_json = json.dumps(payload, ensure_ascii=False)
        payload_loader = f"<script>\nconst PAYLOAD = {payload_json};"

    html = _HTML_TEMPLATE.replace("__PAYLOAD_LOADER__", payload_loader)
    out_html.write_text(html, encoding="utf-8")
    return out_html


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>DST Skin Assembly Tuner</title>
<style>
  :root {
    --bg: #15181e; --panel: #1e232c; --card: #262c37; --border: #343c4a;
    --text: #e6ecf5; --muted: #93a0b4; --accent: #56b6f7; --accent2: #3dd68c;
    --warn: #f5a623;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column;
    overflow: hidden;
  }
  header {
    display: flex; align-items: center; gap: 12px; padding: 10px 16px;
    background: var(--panel); border-bottom: 1px solid var(--border); flex-wrap: wrap;
  }
  header h1 { margin: 0; font-size: 15px; font-weight: 600; color: var(--accent); }
  .toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-left: auto; }
  button, select, label.btn {
    background: var(--card); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 11px; font-size: 12.5px; cursor: pointer;
    transition: border-color .15s, background .15s;
  }
  button:hover, select:hover, label.btn:hover { border-color: var(--accent); background: #2d3544; }
  button.primary { background: #1e4d7a; border-color: var(--accent); }
  button.primary:hover { background: #26639c; }
  button.success { background: #1b563c; border-color: var(--accent2); }
  button.success:hover { background: #226d4c; }
  .main { display: flex; flex: 1; min-height: 0; }
  .stage-wrap {
    flex: 1; display: flex; flex-direction: column; padding: 12px; gap: 10px;
    overflow: auto; align-items: center; justify-content: center;
  }
  .views-grid { display: flex; gap: 12px; flex-wrap: wrap; justify-content: center; }
  .view-box {
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    overflow: hidden; display: flex; flex-direction: column;
  }
  .view-box .vhead {
    padding: 6px 10px; font-size: 12px; color: var(--muted); background: #191d25;
    border-bottom: 1px solid var(--border); display: flex; justify-content: space-between;
  }
  canvas { display: block; cursor: grab; background: #262a30; }
  canvas:active { cursor: grabbing; }
  .hint { font-size: 12px; color: var(--muted); }
  aside {
    width: 340px; background: var(--panel); border-left: 1px solid var(--border);
    display: flex; flex-direction: column; padding: 12px; gap: 10px; overflow-y: auto;
  }
  .section {
    background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 10px;
  }
  .section h3 { margin: 0 0 8px 0; font-size: 13px; color: var(--accent); }
  .slot-list {
    display: flex; flex-wrap: wrap; gap: 5px; max-height: 150px; overflow-y: auto;
  }
  .slot-chip {
    padding: 4px 8px; border-radius: 5px; font-size: 11.5px; cursor: pointer;
    background: #1b2029; border: 1px solid var(--border); color: var(--text);
  }
  .slot-chip.active { background: #1e4d7a; border-color: var(--accent); font-weight: 600; }
  .slot-chip.modified { border-color: var(--warn); color: #ffd485; }
  .ctrl-row {
    display: grid; grid-template-columns: 52px 1fr 64px; align-items: center; gap: 8px;
    margin-bottom: 8px; font-size: 12.5px;
  }
  .ctrl-row input[type="number"] {
    width: 100%; background: #151920; color: var(--text); border: 1px solid var(--border);
    border-radius: 4px; padding: 4px 6px; font-size: 12px;
  }
  .ctrl-row input[type="range"] { width: 100%; accent-color: var(--accent); }
  .btn-row { display: flex; gap: 6px; flex-wrap: wrap; }
  textarea {
    width: 100%; height: 145px; background: #12151b; color: #b9e3ff;
    border: 1px solid var(--border); border-radius: 6px; padding: 8px;
    font-family: Consolas, Menlo, monospace; font-size: 11.5px; resize: vertical;
  }
  .status { font-size: 11.5px; color: var(--accent2); min-height: 16px; }
</style>
</head>
<body>
<header>
  <h1 id="title">DST 皮肤装配调试器</h1>
  <div class="toolbar">
    <select id="tierFilter">
      <option value="all">全部常用度</option>
      <option value="common">常用动画 (T0+T1)</option>
      <option value="T0">T0 核心高频</option>
      <option value="T1">T1 常规玩法</option>
      <option value="T2">T2 表情/偶发</option>
      <option value="T3">T3 极低频/特殊</option>
    </select>
    <select id="clipSelect"></select>
    <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:5px;">
      Wendy对照 <input id="ghostAlpha" type="range" min="0" max="0.8" step="0.05" value="0" style="width:72px;" />
    </label>
    <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:5px;">
      画布缩放 <input id="viewScale" type="range" min="0.8" max="2.0" step="0.05" value="1.15" style="width:72px;" />
    </label>
    <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:4px;cursor:pointer;">
      <input id="showBox" type="checkbox" checked /> 选中框
    </label>
    <label class="btn">加载 JSON<input id="fileInput" type="file" accept=".json,application/json" hidden /></label>
    <button class="success" id="saveBtn">下载 offsets.json</button>
    <button class="primary" id="copyBtn">复制 JSON</button>
  </div>
</header>

<div class="main">
  <div class="stage-wrap">
    <div class="views-grid" id="viewsGrid"></div>
    <div class="hint">操作：点击右侧槽位（或在画布拖拽当前选中部件）调整位置；方向键微调 1px（Shift+方向键 5px）。保存 offsets.json 后执行 pack 即可生效。</div>
  </div>

  <aside>
    <div class="section">
      <h3>可调槽位（当前动作）</h3>
      <div class="slot-list" id="slotList"></div>
    </div>

    <div class="section">
      <h3 id="selectedLabel">当前选中：-</h3>
      <div class="ctrl-row">
        <span>dx</span>
        <input id="dxRange" type="range" min="-80" max="80" step="0.5" value="0" />
        <input id="dxNum" type="number" step="0.5" value="0" />
      </div>
      <div class="ctrl-row">
        <span>dy</span>
        <input id="dyRange" type="range" min="-80" max="80" step="0.5" value="0" />
        <input id="dyNum" type="number" step="0.5" value="0" />
      </div>
      <div class="ctrl-row">
        <span>scale</span>
        <input id="scRange" type="range" min="0.5" max="1.6" step="0.01" value="1" />
        <input id="scNum" type="number" step="0.01" value="1" />
      </div>
      <div class="btn-row">
        <button id="resetSlotBtn">重置当前槽位</button>
        <button id="resetAllBtn">全部清空</button>
      </div>
    </div>

    <div class="section">
      <h3>offsets.json 实时预览</h3>
      <textarea id="jsonBox" spellcheck="false"></textarea>
      <div class="btn-row" style="margin-top:6px;">
        <button id="applyJsonBtn">从文本框应用 JSON</button>
      </div>
      <div class="status" id="statusMsg"></div>
    </div>
  </aside>
</div>

__PAYLOAD_LOADER__
const VIEW_ORDER = [
  { key: "front", label: "正面 Front" },
  { key: "side", label: "侧面 Side" },
  { key: "back", label: "背面 Back" },
];
const CANVAS_W = 360, CANVAS_H = 420;

const _urlClip = new URLSearchParams(window.location.search).get("clip");
const state = {
  clip: (_urlClip && PAYLOAD.clips[_urlClip]) ? _urlClip : (Object.keys(PAYLOAD.clips)[0] || "idle_loop"),
  selectedSlot: "face:0",
  ghostAlpha: 0,
  viewScale: 1.15,
  showBox: true,
  offsets: JSON.parse(JSON.stringify(PAYLOAD.initialOffsets || {})),
  images: {},
};

const clipSelect = document.getElementById("clipSelect");
const tierFilter = document.getElementById("tierFilter");
const viewsGrid = document.getElementById("viewsGrid");
const slotList = document.getElementById("slotList");
const selectedLabel = document.getElementById("selectedLabel");
const dxRange = document.getElementById("dxRange"), dxNum = document.getElementById("dxNum");
const dyRange = document.getElementById("dyRange"), dyNum = document.getElementById("dyNum");
const scRange = document.getElementById("scRange"), scNum = document.getElementById("scNum");
const jsonBox = document.getElementById("jsonBox");
const statusMsg = document.getElementById("statusMsg");
const canvases = {};
const viewScales = {};

function computeViewFrame(bbox, baseScale) {
  let effScale = baseScale;
  let ox = CANVAS_W * 0.5;
  let oy = CANVAS_H * 0.80;
  if (Array.isArray(bbox) && bbox.length === 4 && (bbox[2] > 0 || bbox[3] > 0)) {
    const [fx, fy, fw, fh] = bbox;
    const padW = Math.max(40, fw * 0.28);
    const padTop = Math.max(55, fh * 0.25);
    const padBot = Math.max(20, fh * 0.12);
    const minX = fx - fw * 0.5 - padW * 0.5;
    const maxX = fx + fw * 0.5 + padW * 0.5;
    const minY = fy - fh * 0.5 - padTop;
    const maxY = fy + fh * 0.5 + padBot;
    const boxW = Math.max(1, maxX - minX);
    const boxH = Math.max(1, maxY - minY);
    const fitScale = Math.min(effScale, (CANVAS_W - 16) / boxW, (CANVAS_H - 28) / boxH);
    effScale = Math.max(0.2, fitScale);

    const topPx = oy + minY * effScale;
    const botPx = oy + maxY * effScale;
    const leftPx = ox + minX * effScale;
    const rightPx = ox + maxX * effScale;
    if (topPx < 10 || botPx > CANVAS_H - 20) {
      oy = (CANVAS_H - 10) * 0.5 - ((minY + maxY) * 0.5) * effScale;
    }
    if (leftPx < 8 || rightPx > CANVAS_W - 8) {
      ox = CANVAS_W * 0.5 - ((minX + maxX) * 0.5) * effScale;
    }
  }
  return { ox, oy, scale: effScale };
}

document.getElementById("title").textContent = `DST 皮肤装配调试器 — ${PAYLOAD.buildName}`;

function setStatus(msg) {
  statusMsg.textContent = msg;
  clearTimeout(setStatus._t);
  setStatus._t = setTimeout(() => { statusMsg.textContent = ""; }, 3500);
}

function getOff(slot) {
  const o = state.offsets[slot];
  return o ? { dx: +o.dx || 0, dy: +o.dy || 0, scale: +o.scale || 1 } : { dx: 0, dy: 0, scale: 1 };
}

function isModified(slot) {
  const o = getOff(slot);
  return Math.abs(o.dx) > 1e-4 || Math.abs(o.dy) > 1e-4 || Math.abs(o.scale - 1) > 1e-4;
}

function setOff(slot, dx, dy, scale) {
  dx = Math.round(dx * 2) / 2;
  dy = Math.round(dy * 2) / 2;
  scale = Math.max(0.2, Math.min(2.5, Math.round(scale * 100) / 100));
  if (Math.abs(dx) < 1e-4 && Math.abs(dy) < 1e-4 && Math.abs(scale - 1) < 1e-4) {
    delete state.offsets[slot];
  } else {
    state.offsets[slot] = { dx, dy, scale };
  }
  syncControls();
  renderAll();
}

function exportCleanJson() {
  const out = {};
  for (const k of Object.keys(state.offsets).sort()) {
    const o = getOff(k);
    if (!isModified(k)) continue;
    if (Math.abs(o.scale - 1) < 1e-4) {
      out[k] = [o.dx, o.dy];
    } else {
      out[k] = [o.dx, o.dy, o.scale];
    }
  }
  return JSON.stringify(out, null, 2);
}

function importRawJson(obj) {
  const next = {};
  for (const [k, v] of Object.entries(obj || {})) {
    if (Array.isArray(v)) {
      next[k] = { dx: +(v[0] || 0), dy: +(v[1] || 0), scale: +(v[2] ?? 1) };
    } else if (v && typeof v === "object") {
      next[k] = { dx: +(v.dx || 0), dy: +(v.dy || 0), scale: +(v.scale ?? 1) };
    }
  }
  state.offsets = next;
  syncControls();
  renderAll();
}

function syncControls() {
  const o = getOff(state.selectedSlot);
  selectedLabel.textContent = `当前选中：${state.selectedSlot}`;
  dxRange.value = o.dx; dxNum.value = o.dx;
  dyRange.value = o.dy; dyNum.value = o.dy;
  scRange.value = o.scale; scNum.value = o.scale;
  jsonBox.value = exportCleanJson();
  renderSlotList();
}

function getActiveSlots() {
  const clipData = PAYLOAD.clips[state.clip] || {};
  const seen = new Set();
  const ordered = [];
  const priority = ["face", "headbase", "hairpigtails", "torso", "torso_pelvis", "skirt", "arm_upper", "arm_lower", "hand", "leg", "foot"];
  for (const v of VIEW_ORDER) {
    const elems = (clipData[v.key] && clipData[v.key].target) || [];
    for (const el of elems) {
      if (!el.overridden || el.symbol === "long_hair_blank") continue;
      if (!seen.has(el.slot)) { seen.add(el.slot); ordered.push(el.slot); }
    }
  }
  ordered.sort((a, b) => {
    const [sa, ia] = a.split(":"), [sb, ib] = b.split(":");
    const pa = priority.indexOf(sa), pb = priority.indexOf(sb);
    if (pa !== pb) return (pa < 0 ? 99 : pa) - (pb < 0 ? 99 : pb);
    return (+ia || 0) - (+ib || 0);
  });
  return ordered;
}

function renderSlotList() {
  const slots = getActiveSlots();
  if (!slots.includes(state.selectedSlot) && slots.length) {
    state.selectedSlot = slots[0];
  }
  slotList.innerHTML = "";
  for (const s of slots) {
    const div = document.createElement("div");
    div.className = "slot-chip" + (s === state.selectedSlot ? " active" : "") + (isModified(s) ? " modified" : "");
    div.textContent = s;
    div.onclick = () => { state.selectedSlot = s; syncControls(); renderAll(); };
    slotList.appendChild(div);
  }
}

const PIVOT_SCALED_SYMBOLS = new Set(["hand", "foot"]);

function drawElements(ctx, elems, applyOffsets, highlightSlot, vf) {
  const scale = vf.scale;
  const ox = vf.ox, oy = vf.oy;
  let highlightRect = null;
  const highlightPivots = [];

  for (const el of elems) {
    const sp = PAYLOAD.sprites[el.sprite];
    const img = state.images[el.sprite];
    if (!sp || !img) continue;
    let cx = sp.cx, cy = sp.cy, w = sp.w, h = sp.h;
    if (applyOffsets && el.overridden) {
      const o = getOff(el.slot);
      if (PIVOT_SCALED_SYMBOLS.has(el.symbol)) {
        cx = cx * o.scale + o.dx;
        cy = cy * o.scale + o.dy;
      } else {
        cx += o.dx;
        cy += o.dy;
      }
      w *= o.scale; h *= o.scale;
    }
    const sx = w / sp.iw, sy = h / sp.ih;
    const x0 = cx - w * 0.5, y0 = cy - h * 0.5;
    const [ma, mb, mc, md, mtx, mty] = el.m;
    const fa = scale * (ma * sx);
    const fb = scale * (mc * sy);
    const ftx = ox + scale * (ma * x0 + mc * y0 + mtx);
    const fc = scale * (mb * sx);
    const fd = scale * (md * sy);
    const fty = oy + scale * (mb * x0 + md * y0 + mty);

    ctx.save();
    ctx.setTransform(fa, fc, fb, fd, ftx, fty);
    ctx.drawImage(img, 0, 0);
    ctx.restore();

    if (highlightSlot && el.slot === highlightSlot && el.overridden) {
      const corners = [
        [0, 0], [sp.iw, 0], [sp.iw, sp.ih], [0, sp.ih]
      ].map(([u, v]) => [fa * u + fb * v + ftx, fc * u + fd * v + fty]);
      highlightRect = corners;
      highlightPivots.push([ox + scale * mtx, oy + scale * mty]);
    }
  }
  return highlightRect ? { rect: highlightRect, pivots: highlightPivots } : null;
}

function renderAll() {
  const clipData = PAYLOAD.clips[state.clip] || {};
  for (const v of VIEW_ORDER) {
    const canvas = canvases[v.key];
    if (!canvas) continue;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = "#262a30";
    ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);

    const vd = clipData[v.key] || { target: [], ref: [] };
    const vf = computeViewFrame(vd.bbox, state.viewScale);
    viewScales[v.key] = vf.scale;

    // Ground baseline (only draw when visible inside canvas)
    if (vf.oy >= 8 && vf.oy <= CANVAS_H - 8) {
      ctx.strokeStyle = "rgba(120,135,155,0.45)";
      ctx.beginPath(); ctx.moveTo(24, vf.oy); ctx.lineTo(CANVAS_W - 24, vf.oy); ctx.stroke();
    }

    if (state.ghostAlpha > 0 && vd.ref.length) {
      ctx.save();
      ctx.globalAlpha = state.ghostAlpha;
      drawElements(ctx, vd.ref, false, null, vf);
      ctx.restore();
    }
    const hl = drawElements(ctx, vd.target, true, state.showBox ? state.selectedSlot : null, vf);
    if (hl) {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.strokeStyle = "#56b6f7";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(hl.rect[0][0], hl.rect[0][1]);
      for (let i = 1; i < 4; i++) ctx.lineTo(hl.rect[i][0], hl.rect[i][1]);
      ctx.closePath();
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.strokeStyle = "#ff4d4f";
      ctx.lineWidth = 1.5;
      for (const [px, py] of hl.pivots) {
        ctx.beginPath();
        ctx.moveTo(px - 7, py); ctx.lineTo(px + 7, py);
        ctx.moveTo(px, py - 7); ctx.lineTo(px, py + 7);
        ctx.stroke();
      }
    }
  }
}

function rebuildClipSelect() {
  const tf = tierFilter ? tierFilter.value : "all";
  const metaMap = PAYLOAD.clipMeta || {};
  const tierLabels = {
    T0: "T0 核心高频",
    T1: "T1 常规玩法",
    T2: "T2 表情/偶发",
    T3: "T3 极低频/特殊",
  };
  const groups = { T0: [], T1: [], T2: [], T3: [] };
  for (const clipKey of Object.keys(PAYLOAD.clips)) {
    const t = (metaMap[clipKey] && metaMap[clipKey].tier) || "T1";
    if (tf === "common" && t !== "T0" && t !== "T1") continue;
    else if (tf !== "all" && tf !== "common" && t !== tf) continue;
    (groups[t] || groups.T3).push(clipKey);
  }
  clipSelect.innerHTML = "";
  const visibleKeys = [];
  for (const t of ["T0", "T1", "T2", "T3"]) {
    if (!groups[t].length) continue;
    const og = document.createElement("optgroup");
    og.label = `${tierLabels[t]} (${groups[t].length})`;
    for (const clipKey of groups[t]) {
      const opt = document.createElement("option");
      opt.value = clipKey;
      opt.textContent = clipKey === "idle_loop" ? "站姿 (idle_loop)" : clipKey === "run_loop" ? "跑步 (run_loop)" : `[${t}] ${clipKey}`;
      og.appendChild(opt);
      visibleKeys.push(clipKey);
    }
    clipSelect.appendChild(og);
  }
  if (visibleKeys.length && !visibleKeys.includes(state.clip)) {
    state.clip = visibleKeys[0];
  }
  clipSelect.value = state.clip;
}

function setupViews() {
  rebuildClipSelect();
  clipSelect.onchange = () => { state.clip = clipSelect.value; syncControls(); renderAll(); };
  if (tierFilter) {
    tierFilter.onchange = () => { rebuildClipSelect(); syncControls(); renderAll(); };
  }

  for (const v of VIEW_ORDER) {
    const box = document.createElement("div");
    box.className = "view-box";
    box.innerHTML = `<div class="vhead"><span>${v.label}</span><span>拖拽微调</span></div>`;
    const c = document.createElement("canvas");
    c.width = CANVAS_W; c.height = CANVAS_H;
    box.appendChild(c);
    viewsGrid.appendChild(box);
    canvases[v.key] = c;

    let drag = null;
    c.addEventListener("mousedown", (e) => {
      const o = getOff(state.selectedSlot);
      const elems = ((PAYLOAD.clips[state.clip] || {})[v.key] || {}).target || [];
      let mat = [1, 0, 0, 1];
      for (let idx = elems.length - 1; idx >= 0; idx--) {
        const el = elems[idx];
        if (el.overridden && el.slot === state.selectedSlot && Array.isArray(el.m)) {
          mat = el.m;
          break;
        }
      }
      drag = { x: e.clientX, y: e.clientY, dx: o.dx, dy: o.dy, scale: o.scale, m: mat };
    });
    window.addEventListener("mousemove", (e) => {
      if (!drag) return;
      const sc = viewScales[v.key] || state.viewScale || 1;
      const ddxW = (e.clientX - drag.x) / sc;
      const ddyW = (e.clientY - drag.y) / sc;
      const [ma, mb, mc, md] = drag.m;
      const det = ma * md - mb * mc;
      let ddx = ddxW, ddy = ddyW;
      if (Math.abs(det) > 1e-6) {
        ddx = (md * ddxW - mc * ddyW) / det;
        ddy = (-mb * ddxW + ma * ddyW) / det;
      }
      setOff(state.selectedSlot, drag.dx + ddx, drag.dy + ddy, drag.scale);
    });
    window.addEventListener("mouseup", () => { drag = null; });
  }
}

function bindControls() {
  const onChange = () => setOff(state.selectedSlot, +dxNum.value, +dyNum.value, +scNum.value);
  dxRange.oninput = () => { dxNum.value = dxRange.value; onChange(); };
  dyRange.oninput = () => { dyNum.value = dyRange.value; onChange(); };
  scRange.oninput = () => { scNum.value = scRange.value; onChange(); };
  dxNum.oninput = onChange; dyNum.oninput = onChange; scNum.oninput = onChange;

  document.getElementById("ghostAlpha").oninput = (e) => { state.ghostAlpha = +e.target.value; renderAll(); };
  document.getElementById("viewScale").oninput = (e) => { state.viewScale = +e.target.value; renderAll(); };
  document.getElementById("showBox").onchange = (e) => { state.showBox = e.target.checked; renderAll(); };

  document.getElementById("resetSlotBtn").onclick = () => {
    setOff(state.selectedSlot, 0, 0, 1);
    setStatus(`已重置 ${state.selectedSlot}`);
  };
  document.getElementById("resetAllBtn").onclick = () => {
    state.offsets = {}; syncControls(); renderAll(); setStatus("已清空全部偏移");
  };
  document.getElementById("applyJsonBtn").onclick = () => {
    try {
      importRawJson(JSON.parse(jsonBox.value || "{}"));
      setStatus("已从文本框加载 JSON");
    } catch (err) {
      setStatus("JSON 格式错误：" + err.message);
    }
  };
  document.getElementById("fileInput").onchange = (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        importRawJson(JSON.parse(reader.result));
        setStatus(`已加载 ${f.name}`);
      } catch (err) {
        setStatus("加载失败：" + err.message);
      }
    };
    reader.readAsText(f, "utf-8");
    e.target.value = "";
  };
  document.getElementById("saveBtn").onclick = () => {
    const blob = new Blob([exportCleanJson() + "\n"], { type: "application/json;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = PAYLOAD.offsetsFileName || "offsets.json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    setStatus("已导出 offsets.json");
  };
  document.getElementById("copyBtn").onclick = async () => {
    try {
      await navigator.clipboard.writeText(exportCleanJson());
      setStatus("已复制 JSON 到剪贴板");
    } catch {
      jsonBox.select(); document.execCommand("copy"); setStatus("已复制 JSON");
    }
  };
  window.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    const step = e.shiftKey ? 5 : 1;
    const o = getOff(state.selectedSlot);
    if (e.key === "ArrowLeft") { e.preventDefault(); setOff(state.selectedSlot, o.dx - step, o.dy, o.scale); }
    else if (e.key === "ArrowRight") { e.preventDefault(); setOff(state.selectedSlot, o.dx + step, o.dy, o.scale); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setOff(state.selectedSlot, o.dx, o.dy - step, o.scale); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setOff(state.selectedSlot, o.dx, o.dy + step, o.scale); }
  });
}

async function init() {
  setupViews();
  bindControls();
  const entries = Object.entries(PAYLOAD.sprites);
  await Promise.all(entries.map(([k, sp]) => new Promise((resolve) => {
    const img = new Image();
    img.onload = () => { state.images[k] = img; resolve(); };
    img.onerror = resolve;
    img.src = sp.url;
  })));
  syncControls();
  renderAll();
}
init();
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate standalone HTML DST skin assembly tuner")
    parser.add_argument("--skin", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base", type=Path, default=DST_ANIM_DIR / "wilson.zip")
    parser.add_argument("--ref", type=Path, default=DST_ANIM_DIR / "wendy.zip")
    parser.add_argument("--offsets", type=Path, default=None)
    parser.add_argument("--suite", choices=("compact", "core", "full", "generic"), default="compact")
    args = parser.parse_args()
    out = build_tuner_html(
        skin_zip=args.skin,
        out_html=args.out,
        base_zip=args.base,
        ref_zip=args.ref,
        offsets_json=args.offsets,
        suite=args.suite,
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
