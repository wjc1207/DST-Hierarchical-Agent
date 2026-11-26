"""Generate an offline interactive HTML5 Canvas animation player for DST character skins."""
from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path
import time
import zipfile

try:
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
        build_companion_override_map,
        classify_animation_tier,
        discover_generic_anim_zips,
        is_clean_review_clip,
        load_build,
        parse_anim_bin,
        resolve_compact_specs,
    )
except ImportError:
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


def _union_bbox(frames: list[dict]) -> list[float]:
    xs0, ys0, xs1, ys1 = [], [], [], []
    for fr in frames:
        bbox = fr.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        fx, fy, fw, fh = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        if fw <= 0 and fh <= 0:
            continue
        xs0.append(fx - fw * 0.5)
        xs1.append(fx + fw * 0.5)
        ys0.append(fy - fh * 0.5)
        ys1.append(fy + fh * 0.5)
    if not xs0:
        return [0.0, -90.0, 120.0, 180.0]
    min_x, max_x = min(xs0), max(xs1)
    min_y, max_y = min(ys0), max(ys1)
    return [
        round((min_x + max_x) * 0.5, 2),
        round((min_y + max_y) * 0.5, 2),
        round(max_x - min_x, 2),
        round(max_y - min_y, 2),
    ]


def _extract_full_clip(
    anim_data: dict,
    zip_name: str,
    clip_name: str,
    base_build: dict,
    skin_build: dict,
    ref_build: dict | None,
    override_map: dict[str, str],
    sprite_index: dict[str, int],
    sprites_table: list[list],
    sprites_dir: Path | None,
    *,
    hide_layers: frozenset[str] = DEFAULT_HIDE_LAYERS,
    include_ref: bool = True,
) -> dict | None:
    """Extract every animation frame across front/side/back views into compact arrays."""

    def get_sid(src_build: dict, src_sym: str, finfo: dict) -> int:
        skey = f"{src_build['build_name']}|{src_sym}|{finfo['framenum']}"
        sid = sprite_index.get(skey)
        if sid is not None:
            return sid
        if sprites_dir is not None:
            bdir = _safe_dir_name(src_build["build_name"])
            rel_path = f"player_data/sprites/{bdir}/{src_sym}_{finfo['framenum']}.png"
            out_png = sprites_dir / bdir / f"{src_sym}_{finfo['framenum']}.png"
            out_png.parent.mkdir(parents=True, exist_ok=True)
            finfo["image"].save(out_png, format="PNG", optimize=True)
            url = rel_path
        else:
            url = _img_to_data_url(finfo["image"])
        sid = len(sprites_table)
        sprite_index[skey] = sid
        sprites_table.append([
            url,
            round(float(finfo["cx"]), 2),
            round(float(finfo["cy"]), 2),
            round(float(finfo["w"]), 2),
            round(float(finfo["h"]), 2),
            finfo["image"].width,
            finfo["image"].height,
        ])
        return sid

    def resolve_sid(sym: str, fnum: int, *, use_override: bool, build_obj: dict) -> tuple[int, bool]:
        src_build = build_obj
        src_sym = sym
        overridden = False
        if use_override and sym in override_map:
            src_build = skin_build
            src_sym = override_map[sym]
            overridden = True
        sym_frames = src_build["symbols"].get(src_sym)
        if not sym_frames:
            return -1, False
        finfo = sym_frames.get(fnum)
        if finfo is None:
            if len(sym_frames) == 1 and 0 in sym_frames and src_sym == "long_hair_blank":
                finfo = sym_frames[0]
            else:
                return -1, False
        return get_sid(src_build, src_sym, finfo), (overridden and src_sym != "long_hair_blank")

    def encode_frame(frame: dict) -> tuple[list[list], bool]:
        elems: list[list] = []
        has_overridden = False
        for elem in reversed(frame["elements"]):
            if elem["layer"] in hide_layers:
                continue
            sym = elem["symbol"]
            fnum = int(elem["frame_num"])
            t_sid, had_ov = resolve_sid(sym, fnum, use_override=True, build_obj=base_build)
            if had_ov:
                has_overridden = True
            r_sid = -1
            if include_ref and ref_build is not None:
                r_sid, _ = resolve_sid(sym, fnum, use_override=False, build_obj=ref_build)
            if t_sid < 0 and r_sid < 0:
                continue
            ma, mb, mc, md, mtx, mty = elem["m"]
            elems.append([
                t_sid,
                r_sid,
                round(float(ma), 2),
                round(float(mb), 2),
                round(float(mc), 2),
                round(float(md), 2),
                int(round(float(mtx))),
                int(round(float(mty))),
            ])
        return elems, has_overridden

    views: dict[str, dict] = {}
    fps_val = 30
    any_target = False
    for vname, fmask in (("front", FACING_DOWN), ("side", FACING_SIDE), ("back", FACING_UP)):
        anim = _find_anim(anim_data, clip_name, fmask)
        if anim is None or not anim["frames"]:
            continue
        if anim.get("framerate"):
            fps_val = int(round(float(anim["framerate"]))) or 30
        frames_out: list[list] = []
        view_has_override = False
        for fr in anim["frames"]:
            elems, had_ov = encode_frame(fr)
            if had_ov:
                view_has_override = True
            frames_out.append(elems)
        if view_has_override:
            any_target = True
        views[vname] = {
            "bbox": _union_bbox(anim["frames"]),
            "f": frames_out,
        }

    if not any_target or not views:
        return None
    return {
        "zip": zip_name,
        "clip": clip_name,
        "fps": max(1, fps_val),
        "views": views,
    }


def build_player_html(
    skin_zip: Path,
    out_html: Path,
    *,
    base_zip: Path = DST_ANIM_DIR / "wilson.zip",
    ref_zip: Path | None = DST_ANIM_DIR / "wendy.zip",
    anim_dir: Path = DST_ANIM_DIR,
    suite: str = "compact",
    include_mount: bool = False,
    include_ref: bool = True,
    external_assets: bool = True,
) -> Path:
    """Build an offline interactive 30 FPS HTML5 Canvas animation player for DST skin clips."""
    base_build = load_build(base_zip)
    skin_build = load_build(skin_zip)
    ref_build = load_build(ref_zip) if (include_ref and ref_zip and ref_zip.is_file()) else None
    override_map = build_companion_override_map()

    out_dir = out_html.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    player_data_dir = (out_dir / "player_data") if external_assets else None
    sprites_dir = (player_data_dir / "sprites") if player_data_dir is not None else None

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

    ordered_specs: list[tuple[Path, str, bool, str]] = []
    seen_keys: set[str] = set()

    if suite == "compact":
        for zname, cname, _, _, _, use_carry, out_key in resolve_compact_specs(
            skin_build,
            anim_dir,
            include_mount=include_mount,
        ):
            if out_key in seen_keys:
                continue
            seen_keys.add(out_key)
            ordered_specs.append((anim_dir / zname, cname, use_carry, out_key))
    elif suite == "core":
        for zname, cname, _, _ in CORE_REVIEW_SPECS:
            if cname in seen_keys:
                continue
            seen_keys.add(cname)
            ordered_specs.append((anim_dir / zname, cname, False, cname))
    else:
        for zname, cname, _, _ in CORE_REVIEW_SPECS:
            if cname in seen_keys:
                continue
            seen_keys.add(cname)
            ordered_specs.append((anim_dir / zname, cname, False, cname))
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
                if cname in seen_keys:
                    continue
                seen_keys.add(cname)
                ordered_specs.append((zp, cname, False, cname))
        for bz, bclip, _, _, use_carry, tkey in COVERAGE_BOOSTER_SPECS:
            if tkey in seen_keys:
                continue
            seen_keys.add(tkey)
            ordered_specs.append((anim_dir / bz, bclip, use_carry, tkey))

    sprite_index: dict[str, int] = {}
    sprites_table: list[list] = []
    clips_out: dict[str, dict] = {}

    for zp, cname, use_carry, out_key in ordered_specs:
        adata = load_anim_cached(zp)
        if adata is None:
            continue
        clip_entry = _extract_full_clip(
            adata,
            zp.name,
            cname,
            base_build,
            skin_build,
            ref_build,
            override_map,
            sprite_index,
            sprites_table,
            sprites_dir,
            hide_layers=CARRY_HIDE_LAYERS if use_carry else DEFAULT_HIDE_LAYERS,
            include_ref=(ref_build is not None),
        )
        if clip_entry is not None:
            tier, tier_order, tier_label = classify_animation_tier(zp.name, out_key)
            clip_entry["tier"] = tier
            clip_entry["tier_order"] = tier_order
            clip_entry["tier_label"] = tier_label
            clips_out[out_key] = clip_entry

    clips_out = {
        k: v
        for k, v in sorted(
            clips_out.items(),
            key=lambda kv: (kv[1].get("tier_order", 1), kv[0]),
        )
    }

    gen_ts = int(time.time())
    payload = {
        "buildName": skin_build["build_name"],
        "refName": ref_build["build_name"] if ref_build else None,
        "suite": suite,
        "generatedAt": gen_ts,
        "sprites": sprites_table,
        "clips": clips_out,
    }

    if external_assets and player_data_dir is not None:
        player_data_dir.mkdir(parents=True, exist_ok=True)
        (player_data_dir / "anims.js").write_text(
            "window.__DST_ANIM_PLAYER__ = " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + ";\n",
            encoding="utf-8",
        )
        payload_loader = f'<script src="player_data/anims.js?v={gen_ts}"></script>\n<script>\nconst DATA = window.__DST_ANIM_PLAYER__;'
    else:
        payload_loader = f"<script>\nconst DATA = {json.dumps(payload, separators=(',', ':'), ensure_ascii=False)};"

    html = _PLAYER_HTML_TEMPLATE.replace("__PAYLOAD_LOADER__", payload_loader)
    out_html.write_text(html, encoding="utf-8")
    return out_html


_PLAYER_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>DST Skin Animation Player</title>
<style>
  :root {
    --bg: #14171d; --panel: #1d222b; --card: #242a35; --border: #323b4a;
    --text: #e6ecf5; --muted: #93a1b8; --accent: #58c4ff; --ok: #3dd68c;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
  header { background: var(--panel); border-bottom: 1px solid var(--border); padding: 10px 16px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  header h1 { margin: 0; font-size: 15px; color: var(--accent); }
  .toolbar { display: flex; align-items: center; gap: 10px; margin-left: auto; flex-wrap: wrap; }
  input, select, button, a.btn { background: #151921; color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; font-size: 12px; text-decoration: none; }
  button, a.btn { cursor: pointer; }
  button:hover, a.btn:hover { border-color: var(--accent); background: #263040; }
  button.primary { background: #1d4e78; border-color: var(--accent); }
  .workspace { flex: 1; display: flex; min-height: 0; }
  aside { width: 290px; background: var(--panel); border-right: 1px solid var(--border); display: flex; flex-direction: column; padding: 10px; gap: 8px; }
  .clip-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 3px; }
  .clip-item { padding: 6px 8px; border-radius: 6px; font-size: 12px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; gap: 6px; border: 1px solid transparent; }
  .clip-item:hover { background: #262d3a; }
  .clip-item.active { background: #1c4469; border-color: var(--accent); font-weight: 600; }
  .clip-item .meta { font-size: 10.5px; color: var(--muted); white-space: nowrap; display: inline-flex; align-items: center; gap: 4px; }
  .tier-pill { padding: 1px 5px; border-radius: 4px; font-size: 10px; font-weight: 600; }
  .tier-T0 { background: #164e3f; color: #6ee7b7; }
  .tier-T1 { background: #1e3a5f; color: #7dd3fc; }
  .tier-T2 { background: #4a3728; color: #fcd34d; }
  .tier-T3 { background: #323744; color: #cbd5e1; }
  .stage-col { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .stage { flex: 1; overflow: auto; display: flex; align-items: center; justify-content: center; padding: 14px; gap: 12px; flex-wrap: wrap; }
  .view-card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; display: flex; flex-direction: column; }
  .view-head { padding: 6px 10px; font-size: 12px; color: var(--muted); border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; background: #181c24; }
  .canvas-pair { display: flex; }
  canvas { display: block; background: #252931; }
  .controls-bar { background: var(--panel); border-top: 1px solid var(--border); padding: 10px 16px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .scrubber { flex: 1; min-width: 180px; accent-color: var(--accent); }
  .frame-readout { font-family: Consolas, monospace; font-size: 12px; color: var(--accent); min-width: 96px; }
</style>
</head>
<body>
<header>
  <h1 id="title">DST 动画播放器</h1>
  <span id="clipStats" style="font-size:12px;color:var(--muted);"></span>
  <div class="toolbar">
    <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:5px;cursor:pointer;">
      <input id="showRef" type="checkbox" checked /> 对比 Wendy Ref
    </label>
    <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:5px;">
      倍速
      <select id="speedSel">
        <option value="0.25">0.25x</option>
        <option value="0.5">0.5x</option>
        <option value="1" selected>1.0x (30 FPS)</option>
        <option value="1.5">1.5x</option>
        <option value="2">2.0x</option>
      </select>
    </label>
    <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:5px;">
      缩放 <input id="zoomRange" type="range" min="0.7" max="2.0" step="0.05" value="1.15" style="width:76px;" />
    </label>
    <a id="tunerLink" class="btn" href="skin_tuner.html" target="_blank">去调 Offset</a>
    <a class="btn" href="review.html">返回 Review</a>
  </div>
</header>

<div class="workspace">
  <aside>
    <input id="searchBox" type="text" placeholder="搜索动作名 / 动画包..." />
    <select id="tierFilter">
      <option value="all">全部常用度</option>
      <option value="common">常用动画 (T0+T1)</option>
      <option value="T0">T0 核心高频</option>
      <option value="T1">T1 常规玩法</option>
      <option value="T2">T2 表情/偶发</option>
      <option value="T3">T3 极低频/特殊</option>
    </select>
    <select id="zipFilter"><option value="">全部动画包</option></select>
    <div id="countNote" style="font-size:11px;color:var(--muted);"></div>
    <div class="clip-list" id="clipList"></div>
  </aside>
  <div class="stage-col">
    <div class="stage" id="stage"></div>
    <div class="controls-bar">
      <button class="primary" id="playBtn">暂停 (Space)</button>
      <button id="prevBtn">上一帧</button>
      <button id="nextBtn">下一帧</button>
      <input class="scrubber" id="scrubber" type="range" min="0" max="0" value="0" />
      <span class="frame-readout" id="frameReadout">f 0 / 0</span>
      <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:4px;cursor:pointer;">
        <input id="loopChk" type="checkbox" checked /> 循环
      </label>
    </div>
  </div>
</div>

__PAYLOAD_LOADER__
const VIEW_ORDER = [
  { key: "front", label: "正面 (Front)" },
  { key: "side", label: "侧面 (Side)" },
  { key: "back", label: "背面 (Back)" },
];
const CANVAS_W = 300;
const CANVAS_H = 340;
const initialParam = new URLSearchParams(window.location.search).get("clip");
const allClipKeys = Object.keys(DATA.clips || {});
const defaultClip = (initialParam && DATA.clips[initialParam]) ? initialParam : (DATA.clips.idle_loop ? "idle_loop" : allClipKeys[0]);

const state = {
  clip: defaultClip,
  playing: true,
  frameFloat: 0,
  frameIdx: 0,
  speed: 1.0,
  zoom: 1.15,
  loop: true,
  showRef: Boolean(DATA.refName),
  images: [],
  canvases: {},
};

document.getElementById("title").textContent = `DST 动画播放器 — ${DATA.buildName}`;
document.getElementById("clipStats").textContent = `共 ${allClipKeys.length} 个通用动画 (${DATA.suite})`;
if (!DATA.refName) {
  document.getElementById("showRef").checked = false;
  document.getElementById("showRef").disabled = true;
}

const zipFilter = document.getElementById("zipFilter");
[...new Set(allClipKeys.map(k => DATA.clips[k].zip))].sort().forEach(z => {
  const opt = document.createElement("option");
  opt.value = z; opt.textContent = z;
  zipFilter.appendChild(opt);
});

function computeViewFrame(bbox, baseScale) {
  let effScale = baseScale;
  let ox = CANVAS_W * 0.5;
  let oy = CANVAS_H * 0.80;
  if (bbox && bbox.length >= 4) {
    const [fx, fy, fw, fh] = bbox;
    const padW = Math.max(40, fw * 0.28);
    const padTop = Math.max(55, fh * 0.25);
    const padBot = Math.max(20, fh * 0.12);
    const minX = fx - fw * 0.5 - padW * 0.5;
    const maxX = fx + fw * 0.5 + padW * 0.5;
    const minY = fy - fh * 0.5 - padTop;
    const maxY = fy + fh * 0.5 + padBot;
    const fit = Math.min(effScale, (CANVAS_W - 16) / Math.max(1, maxX - minX), (CANVAS_H - 28) / Math.max(1, maxY - minY));
    effScale = Math.max(0.2, fit);
    if (oy + minY * effScale < 10 || oy + maxY * effScale > CANVAS_H - 20) {
      oy = (CANVAS_H - 10) * 0.5 - (minY + maxY) * 0.5 * effScale;
    }
    if (ox + minX * effScale < 8 || ox + maxX * effScale > CANVAS_W - 8) {
      ox = CANVAS_W * 0.5 - (minX + maxX) * 0.5 * effScale;
    }
  }
  return { scale: effScale, ox, oy };
}

function drawFrameElems(ctx, elems, vf, isRef) {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#252931";
  ctx.fillRect(0, 0, CANVAS_W, CANVAS_H);
  if (vf.oy >= 8 && vf.oy <= CANVAS_H - 8) {
    ctx.strokeStyle = "rgba(120,135,155,0.45)";
    ctx.beginPath(); ctx.moveTo(20, vf.oy); ctx.lineTo(CANVAS_W - 20, vf.oy); ctx.stroke();
  }
  for (const el of elems) {
    const [tSid, rSid, ma, mb, mc, md, mtx, mty] = el;
    const sid = isRef ? rSid : tSid;
    if (sid < 0) continue;
    const sp = DATA.sprites[sid];
    const img = state.images[sid];
    if (!sp || !img || !img.complete || img.naturalWidth === 0) continue;
    const [, cx, cy, w, h, iw, ih] = sp;
    const sx = w / iw, sy = h / ih;
    const x0 = cx - w * 0.5, y0 = cy - h * 0.5;
    const fa = vf.scale * (ma * sx);
    const fb = vf.scale * (mc * sy);
    const ftx = vf.ox + vf.scale * (ma * x0 + mc * y0 + mtx);
    const fc = vf.scale * (mb * sx);
    const fd = vf.scale * (md * sy);
    const fty = vf.oy + vf.scale * (mb * x0 + md * y0 + mty);
    ctx.setTransform(fa, fc, fb, fd, ftx, fty);
    ctx.drawImage(img, 0, 0);
  }
}

function maxFramesForClip(entry) {
  let mx = 1;
  for (const v of Object.values(entry.views || {})) {
    if (v.f && v.f.length > mx) mx = v.f.length;
  }
  return mx;
}

function rebuildStage() {
  const stage = document.getElementById("stage");
  stage.innerHTML = "";
  state.canvases = {};
  const entry = DATA.clips[state.clip];
  if (!entry) return;
  document.getElementById("tunerLink").href = `skin_tuner.html?clip=${encodeURIComponent(state.clip)}`;

  const maxF = maxFramesForClip(entry);
  const scrubber = document.getElementById("scrubber");
  scrubber.max = String(Math.max(0, maxF - 1));

  for (const v of VIEW_ORDER) {
    const vd = entry.views[v.key];
    if (!vd) continue;
    const card = document.createElement("div");
    card.className = "view-card";
    card.innerHTML = `<div class="view-head"><span>${v.label}</span><span>${vd.f.length} 帧 @ ${entry.fps} FPS</span></div>`;
    const pair = document.createElement("div");
    pair.className = "canvas-pair";
    let refCanvas = null;
    if (state.showRef && DATA.refName) {
      refCanvas = document.createElement("canvas");
      refCanvas.width = CANVAS_W; refCanvas.height = CANVAS_H;
      refCanvas.style.borderRight = "1px solid #323b4a";
      pair.appendChild(refCanvas);
    }
    const skinCanvas = document.createElement("canvas");
    skinCanvas.width = CANVAS_W; skinCanvas.height = CANVAS_H;
    pair.appendChild(skinCanvas);
    card.appendChild(pair);
    stage.appendChild(card);
    state.canvases[v.key] = { skinCanvas, refCanvas };
  }
}

function renderCurrentFrame() {
  const entry = DATA.clips[state.clip];
  if (!entry) return;
  const maxF = maxFramesForClip(entry);
  const idx = Math.max(0, Math.min(maxF - 1, Math.floor(state.frameIdx)));
  document.getElementById("scrubber").value = String(idx);
  document.getElementById("frameReadout").textContent = `f ${idx + 1} / ${maxF}`;

  for (const v of VIEW_ORDER) {
    const vd = entry.views[v.key];
    const pair = state.canvases[v.key];
    if (!vd || !pair) continue;
    const vf = computeViewFrame(vd.bbox, state.zoom);
    const fidx = idx % Math.max(1, vd.f.length);
    const elems = vd.f[fidx] || [];
    drawFrameElems(pair.skinCanvas.getContext("2d"), elems, vf, false);
    if (pair.refCanvas) {
      drawFrameElems(pair.refCanvas.getContext("2d"), elems, vf, true);
    }
  }
}

function selectClip(key) {
  if (!DATA.clips[key]) return;
  state.clip = key;
  state.frameFloat = 0;
  state.frameIdx = 0;
  rebuildStage();
  renderCurrentFrame();
  renderClipList();
}

function renderClipList() {
  const q = document.getElementById("searchBox").value.trim().toLowerCase();
  const tf = document.getElementById("tierFilter").value;
  const zf = zipFilter.value;
  const list = document.getElementById("clipList");
  list.innerHTML = "";
  let matched = 0;
  for (const k of allClipKeys) {
    const entry = DATA.clips[k];
    const tier = entry.tier || "T1";
    if (tf === "common" && tier !== "T0" && tier !== "T1") continue;
    else if (tf !== "all" && tf !== "common" && tier !== tf) continue;
    if (zf && entry.zip !== zf) continue;
    if (q && !(k + " " + entry.zip + " " + tier + " " + (entry.tier_label || "")).toLowerCase().includes(q)) continue;
    matched += 1;
    const div = document.createElement("div");
    div.className = "clip-item" + (k === state.clip ? " active" : "");
    const fcount = maxFramesForClip(entry);
    div.innerHTML = `<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${k}</span><span class="meta"><span class="tier-pill tier-${tier}" title="${entry.tier_label || tier}">${tier}</span>${fcount}f</span>`;
    div.onclick = () => selectClip(k);
    list.appendChild(div);
  }
  document.getElementById("countNote").textContent = `显示 ${matched} / ${allClipKeys.length} 个动画`;
}

function togglePlay() {
  state.playing = !state.playing;
  document.getElementById("playBtn").textContent = state.playing ? "暂停 (Space)" : "播放 (Space)";
}

let lastTs = performance.now();
function tick(now) {
  const dt = Math.min(0.1, (now - lastTs) / 1000);
  lastTs = now;
  const entry = DATA.clips[state.clip];
  if (state.playing && entry) {
    const maxF = maxFramesForClip(entry);
    state.frameFloat += dt * (entry.fps || 30) * state.speed;
    if (state.frameFloat >= maxF) {
      if (state.loop) state.frameFloat = state.frameFloat % maxF;
      else { state.frameFloat = maxF - 1; state.playing = false; document.getElementById("playBtn").textContent = "播放 (Space)"; }
    }
    const nextIdx = Math.floor(state.frameFloat);
    if (nextIdx !== state.frameIdx) {
      state.frameIdx = nextIdx;
      renderCurrentFrame();
    }
  }
  requestAnimationFrame(tick);
}

document.getElementById("playBtn").onclick = togglePlay;
document.getElementById("prevBtn").onclick = () => {
  state.playing = false; document.getElementById("playBtn").textContent = "播放 (Space)";
  const maxF = maxFramesForClip(DATA.clips[state.clip]);
  state.frameIdx = (state.frameIdx - 1 + maxF) % maxF;
  state.frameFloat = state.frameIdx;
  renderCurrentFrame();
};
document.getElementById("nextBtn").onclick = () => {
  state.playing = false; document.getElementById("playBtn").textContent = "播放 (Space)";
  const maxF = maxFramesForClip(DATA.clips[state.clip]);
  state.frameIdx = (state.frameIdx + 1) % maxF;
  state.frameFloat = state.frameIdx;
  renderCurrentFrame();
};
document.getElementById("scrubber").oninput = (e) => {
  state.playing = false; document.getElementById("playBtn").textContent = "播放 (Space)";
  state.frameIdx = +e.target.value;
  state.frameFloat = state.frameIdx;
  renderCurrentFrame();
};
document.getElementById("speedSel").onchange = (e) => { state.speed = +e.target.value; };
document.getElementById("zoomRange").oninput = (e) => { state.zoom = +e.target.value; renderCurrentFrame(); };
document.getElementById("loopChk").onchange = (e) => { state.loop = e.target.checked; };
document.getElementById("showRef").onchange = (e) => { state.showRef = e.target.checked; rebuildStage(); renderCurrentFrame(); };
document.getElementById("searchBox").oninput = renderClipList;
document.getElementById("tierFilter").onchange = renderClipList;
zipFilter.onchange = renderClipList;
window.addEventListener("keydown", (e) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
  if (e.code === "Space") { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); document.getElementById("prevBtn").click(); }
  else if (e.key === "ArrowRight") { e.preventDefault(); document.getElementById("nextBtn").click(); }
});

async function init() {
  renderClipList();
  rebuildStage();
  state.images = DATA.sprites.map((sp) => {
    const img = new Image();
    img.src = sp[0] + (DATA.generatedAt ? `?v=${DATA.generatedAt}` : "");
    return img;
  });
  await Promise.all(state.images.map(img => new Promise(res => {
    if (img.complete) return res();
    img.onload = res; img.onerror = res;
  })));
  renderCurrentFrame();
  requestAnimationFrame(tick);
}
init();
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skin", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base", type=Path, default=DST_ANIM_DIR / "wilson.zip")
    parser.add_argument("--ref", type=Path, default=DST_ANIM_DIR / "wendy.zip")
    parser.add_argument("--suite", choices=("compact", "core", "full", "generic"), default="compact")
    parser.add_argument("--include-mount", action="store_true")
    parser.add_argument("--no-ref", action="store_true")
    args = parser.parse_args()
    out = build_player_html(
        skin_zip=args.skin,
        out_html=args.out,
        base_zip=args.base,
        ref_zip=args.ref,
        suite=args.suite,
        include_mount=args.include_mount,
        include_ref=not args.no_ref,
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
