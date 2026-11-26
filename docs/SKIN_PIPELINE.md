# DST Buddy Agent 角色皮肤制作工具链

本工具链用于将角色参考图转换为轻量级 `OverrideSymbol` 角色外观包（单皮肤约 1.8 MB，无需修改 200+ 原版 `player_*.zip` 动作文件）。

## 工具文件

- [python/tools/skin_pipeline.py](../python/tools/skin_pipeline.py)：统一命令行入口，覆盖 `extract` → `rework` → `pack` → `preview` → `inspect-anim`。
- [python/tools/skin_preview.py](../python/tools/skin_preview.py)：基于 DST 真实 `anim.bin`（v4）骨骼变换矩阵的离线试装渲染器（纯 Python + Pillow）。
- [mods/dst_buddy_agent/scripts/jev_companion_presentation.lua](../mods/dst_buddy_agent/scripts/jev_companion_presentation.lua)：游戏内可逆 `OverrideSymbol` 换肤层与 `/buddy skin` 映射。

> **环境要求**：需使用包含 Pillow 的环境运行，例如 `.\.venv-mcp\Scripts\python.exe`。

---

## 标准制作流程

### 1. 解包原版骨架模板（生成 3 张绿幕图谱）
```powershell
.\.venv-mcp\Scripts\python.exe python/tools/skin_pipeline.py extract --out skins/<name>/work
```
默认基于原版 `wendy.zip`（含 `skirt` 与 `hairpigtails` 槽位），输出 `sheet1..3_template.png`、`manifest.json`、`sheets_layout.json`。

### 2. AI 转换与打包
将 AI 生成的 3 张绿幕图放入 `skins/<name>/work/sheet{1,2,3}_ai.png`，然后打包：
```powershell
.\.venv-mcp\Scripts\python.exe python/tools/skin_pipeline.py pack `
  --work skins/<name>/work `
  --out mods/dst_buddy_agent/anim/skin_<name>.zip `
  --build-name skin_<name>
```
- 支持在 `skins/<name>/offsets.json` 中按 `"symbol:frame": [dx, dy, scale]` 微调单格锚点（如 `{"face:4": [-4.0, 0.0, 0.85]}`）。
- 绿幕抠图使用边界洪水填充 + 内部严格色键 + 2px 边缘去溢色（严格保护青蓝色系发色）。

### 3. 真实骨骼离线试装预览
在进游戏前直接用原版 `player_idles.zip` / `player_basic.zip` 渲染站姿与跑步三视图（上排原版 Wendy 参照，下排 `wilson + 皮肤` 实机叠加）：
```powershell
# 站姿三视图
.\.venv-mcp\Scripts\python.exe python/tools/skin_pipeline.py preview `
  --skin mods/dst_buddy_agent/anim/skin_<name>.zip `
  --clip idle_loop --frame 0 `
  --out skins/<name>/preview

# 跑步三视图
.\.venv-mcp\Scripts\python.exe python/tools/skin_pipeline.py preview `
  --anim "C:\Program Files (x86)\Steam\steamapps\common\Don't Starve Together\data\anim\player_basic.zip" `
  --skin mods/dst_buddy_agent/anim/skin_<name>.zip `
  --clip run_loop --frame 3 `
  --out skins/<name>/preview
```

### 4. 局部返工（只重画有问题的槽位）
先用 `inspect-anim` 核对动画实际引用的槽位编号，**切勿凭直觉猜编号**：
```powershell
.\.venv-mcp\Scripts\python.exe python/tools/skin_pipeline.py inspect-anim `
  --clip idle_loop --symbols torso skirt hairpigtails face
```
导出高分辨率局部返工板：
```powershell
.\.venv-mcp\Scripts\python.exe python/tools/skin_pipeline.py rework `
  --work skins/<name>/work --name rework1 --cols 3 --cell-size 340 `
  --slots "torso:3=side chest" "skirt:2=front run" "skirt:3=side run" "face:4=side face"
```
将返工图保存为 `skins/<name>/work/rework1_ai.png`，再次运行 `pack` 时会自动发现并合并 `rework*_layout.json` + `rework*_ai.png`（可在 `rework*_layout.json` 中对不需要覆盖的格加 `"skip": true`）。

---

## 已验证的 DST 核心槽位映射

| 部位 | 正面站姿 (`idle_loop`) | 侧面站姿 (`idle_loop`) | 背面站姿 (`idle_loop`) | 正面跑步 (`run_loop`) | 侧面跑步 (`run_loop`) |
|---|---|---|---|---|---|
| `headbase` | `0` | `1` | `2` | `0` | `1` |
| `face` | `0` | `4` | — | `0` | `4` |
| `torso` / `torso_pelvis` | `0` | `3` | `6` | `0` | `3` |
| `skirt` | `0` | `0` | `1` | `2` | `3` |
| `hairpigtails` | `0`, `1` | `1`, `2` | `0`, `1` | `0`, `1` | `1`, `2` |

---

## 接入模组与版权说明

- 接入新皮肤：在 [modmain.lua](../mods/dst_buddy_agent/modmain.lua) 的 `Assets` 注册 `anim/skin_<name>.zip`，并在 [jev_companion_presentation.lua](../mods/dst_buddy_agent/scripts/jev_companion_presentation.lua) 的 `M.SKINS` / `resolve_skin` / `infer_skin` 中添加映射，随后运行 `python python/tools/package_mods.py` 打包。
- 版权与署名：`assets/` 中的参考图带有二创署名信息（例如 DeepSeek 女仆参考图注明 `角色原型：上善无形 | 女仆二设：ZipZipPipe`，`CC BY-NC-SA 4.0`），对外公开发布创意工坊或仓库时请遵守非商用与署名要求。

## 6. 全动画 Review 机制（逐动画渲染检查 + 闭环修补）

单看 `idle_loop` 与 `run_loop` 容易漏掉工具动作、受击、电击 X-ray、表情和手部穿帮。使用 `review` 子命令可自动为每个动画渲染一帧（左：原版 Wendy 参考，右：当前皮肤），生成分页拼图、单帧图、槽位覆盖率报告与离线交互式审查看板：

```powershell
# 快速核心集（~34 个高频代表动画帧：待机/奔跑/采集/攻击/受击/吃喝/电击X-ray/表情/状态）
.\.venv-mcp\Scripts\python.exe python/tools/skin_pipeline.py review --suite core

# 全量集（扫描 15 个核心 player_*.zip 中的每一个动画 clip，各自渲染最具代表性一帧）
.\.venv-mcp\Scripts\python.exe python/tools/skin_pipeline.py review --suite full

# 通用全覆盖集（自动扫描除角色专属/特效外的全部通用 player_*.zip，跳过 pre/pst/lag 过渡重复帧，覆盖率 ~97%+）
.\.venv-mcp\Scripts\python.exe python/tools/skin_pipeline.py review --suite generic
# 改完 offsets.json 后一键刷新（自动依次执行 pack + review + tuner）
.\.venv-mcp\Scripts\python.exe python/tools/skin_pipeline.py refresh
```

默认扁平目录为 `skins/<skin>/`：
- `skins/<skin>/review.html`：离线可双击打开的交互看板，支持按槽位（如 `hand:3`）反查受影响动画、点击「调 Offset」直达同级 `skin_tuner.html`、点击图片全屏放大，以及填写 `issue`/`expected` 导出 `rework_plan.json`。
- `skins/<skin>/skin_tuner.html` & `skins/<skin>/offsets.json`：骨骼偏移调试器与偏移配置。
- `skins/<skin>/review_data/`：高清 `frames/*.png`、分页对比拼图 `sheets/review_sheet_*.png`、`review_report.md` / `review_report.json`。
- 直接从 `rework_plan.json` 生成返工模板与逐格精准英文提示词：
  ```powershell
  .\.venv-mcp\Scripts\python.exe python/tools/skin_pipeline.py rework --name rework6 --plan rework_plan.json
  ```
  自动生成 `rework6_template.png`、`rework6_layout.json` 与 `rework6_prompt.txt`。
