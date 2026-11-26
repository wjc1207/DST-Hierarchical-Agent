# DST AI 角色皮肤生成与精修手册（DeepSeek 实战沉淀 & 通用复用指南）

> **角色版权与参考原图**：官方设定图 [deepseek-1.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/assets/deepseek-1.png)（`3200×2100`，`CC BY-NC-SA 4.0`，角色原型：上善无形 | 女仆二设：ZipZipPipe）
>
> **分拆高清参考图（按部位单选搭配模板图作为双参考图输入，已去除文字标注与彩蛋，避免生图干扰）**：
> - [deepseek_ref_sd.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/deepseek/reference/deepseek_ref_sd.png)（`865×422`）：**SD Q 版正/侧/背三视图** — 用于 `headbase`、`hair`、`tail`、Q 版服装比例与背面蝴蝶结。
> - [deepseek_ref_normal.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/deepseek/reference/deepseek_ref_normal.png)（`1844×1110`）：**正常比例正/侧/背三视图** — 用于服饰纹理、星图裙摆、金星、围裙与褶皱细节。
> - [deepseek_ref_expressions.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/deepseek/reference/deepseek_ref_expressions.png)（`1652×315`）：**8 连高清表情参考** — 用于 `face:*`、`cheeks:*` 对齐日系二次元光滑赛璐璐眉眼鼻嘴画风。
> - [deepseek_reference.jpg](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/deepseek/reference/deepseek_reference.jpg)（`1024×672`）：旧版整张总览设定图（仅作兼容留档，**新皮肤优先使用分拆图**）。
>
> **全量 Sprite 语义与复用手册**：首次制作新皮肤前请先查阅 [SPRITE_ATLAS_GUIDE.md](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/SPRITE_ATLAS_GUIDE.md)（包含全部 `159` 个原版槽位语义、DST `250` 动画包常用度实测与同源/镜像/自动派生复用关系）。

---

## 一、通用 DST AI 皮肤制作复用指南（Playbook）

做下一套 AI 皮肤（如 Claude / ChatGPT / Gemini）时，直接按本节规范执行，可一次性绕开 DeepSeek 期间踩过的 27 轮大坑。

### 1.1 参考图预处理与生图铁律

1. **三图分拆预处理**：切勿把带文字标注、多画风混排、道具彩蛋的大设定图直接喂给生图模型；先人工或脚本拆成「Q 版三视图 / 正比例服饰细节图 / 高清表情参考栏」三张纯图。
2. **双图输入铁律**：生图模型输入**只放 2 张图**：`Image 1 = <name>_template.png`（带黑框与白字槽位标号的 `#00FF00` 纯绿幕模板），`Image 2 = 对应的官方分拆参考图`。**严禁将前序 AI 成图或拼装渲染图作为参考图喂回生图模型**，否则色偏、描边粗细漂移和边缘杂色会逐代放大。
3. **画风与绿幕底线**：
   - 背景必须是纯实色 `#00FF00`（无渐变、无阴影、无发光、无抗锯齿绿边），所有部件四边留足纯绿留白，**禁止任何像素贴到黑框边线而被齐平裁断**。
   - 线稿统一深色干净赛璐璐闭口线（DeepSeek 使用 `#141C47` 深海军蓝），禁止粗糙美式黑框或像素锯齿。

---

### 1.2 全角色兼容符号架构（与代码严格同步）

> 下述列表必须与 [jev_companion_presentation.lua](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/mods/dst_buddy_agent/scripts/jev_companion_presentation.lua) 及 [skin_preview.py](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/python/tools/skin_preview.py) 保持完全一致（有单元测试 `test_presentation_lua_and_preview_symbol_lists_stay_in_sync` 校验）：

- **`BODY_SYMBOLS`（20 个替换槽位）**：
  `headbase`, `headbase_hat`, `hair`, `hair_hat`, `hairpigtails`, `tail`, `face`, `cheeks`, `torso`, `torso_pelvis`, `skirt`, `torso_wide`, `torso_pelvis_wide`, `skirt_wide`, `arm_upper`, `arm_upper_skin`, `arm_lower`, `hand`, `leg`, `foot`
- **`BLANK_SYMBOLS`（3 个置空原版残件）**：
  `beard`, `arm_lower_cuff`, `arm_lower_narrow`（隐藏 Wilson/Woodie/Webber 等角色胡子与原版袖口窄袖）
- **管线自动派生件（无需生图）**：
  - `headbase_hat:0..3`：自动从 `headbase:0..3` 上部裁剪派生（不要放进 Sheet 1）；
  - `hair:0,1,3` / `hair_hat:0,1,3`：打包为 `4×4` 透明占位帧（`long_hair_blank:0`），屏蔽 DST 引擎在 286 个非背面动画中将 `hair` 画在 `torso` 前方的遮胸问题；`hair:2` / `hair_hat:2` 保留背面整片披发作为底层垫片；
  - `torso_wide` / `torso_pelvis_wide` / `skirt_wide`：复用对应标准件按沃尔夫冈等壮汉骨架比例缩放派生；
  - `tail:1..5, 7..11`：自动填充 `4×4` 透明占位帧，仅需画 `tail:0`（常规）与 `tail:6`（X-ray）；
  - `face:14`（夜间发光眼）：默认保留原版柔和发光眼或另行覆盖；未单独重绘的 `face` 槽位按 `DEFAULT_FACE_MAP` 映射到 10 个核心表情并保留原版视角偏移；
  - `hand` & `arm_upper_skin`：未提供 AI/Rework 图时按 `TARGET_SKIN_RGB` 自动调色，**若角色袖子覆盖大臂（如长袖女仆），必须重绘 `arm_upper_skin:0..1` 为袖色**，否则 `coach`/挥杆/钓鱼等动作会露出肉色或白棒。

**长发与尾巴全角色兼容要点**：
- 采用**混合长发架构（零侵入官方 `player_*.zip`）**：正面/侧面与 X-ray 长发统一走 **`hairpigtails:0..4`**（DST 引擎在 1,212 个非背面动画中将其绘制在 `torso`/`skirt` 后方，在 393 个背面动画中绘制在 `headbase` 前方，原生根治 `plank_idle_loop`、`plank_hop`、`pull_fail`、`reading_in`、`reading_loop` 等 286 个动作的头发遮胸问题），并对 `hairpigtails:0,1` 顶部 `24%` 做 smoothstep 羽化使其无缝融进头壳；同时保留 **`hair:2` / `hair_hat:2`**（全库 `0` 个露脸动画会调用 `hair:2`）与顶部 `14%` 羽化作为背面整片垫底，防止大动作背面露缝，并把 `hair:0,1,3` / `hair_hat:0,1,3` 打包为 `4×4` 透明占位帧以隐藏威尔逊等原版发型；
- 尾巴必须独立做进 `tail:0, 6` 并在 Lua/预览器中驱动，**禁止画死在 `skirt:1/3` 上**，否则会出现双尾重影或尾巴不随动作摆动。

---

### 1.3 DST 高危槽位真实语义与避坑速查表

| 槽位 | DST 真实用途与坑点 | 正确画法与硬约束 |
| :--- | :--- | :--- |
| **全局侧面** | 设定图三视图常朝左，而 DST 原版侧面精灵全部朝右（朝左靠引擎水平翻转） | **所有侧面槽位（`:1` / `:3` / `:4` / `:6` 等）必须水平镜像为朝右（facing RIGHT）**；后发须向**左下**飘，禁止向右甩到脸前 |
| `headbase:0..2` | 长发画在头上会导致低头/转头穿帮或底边被格线切平 | 仅画**短发波波头长度悬浮空头壳**（零五官），底部预留 `15–20%` 纯绿；**`headbase:0` 下巴弧线正下方 100% 纯绿净空**；`headbase:2` 背面须用**哑光赛璐璐**与 `hair:2` 统一，禁刺眼亮白高光环 |
| `hair:0..3` | 替代双马尾的整片披发（`0` 正、`1` 侧、`2` 背、`3` X-ray） | `hair:0/2` 为整片连续大波浪、`hair:1` 从右上向左下飘；打包管线会自动对 `hair:2` 顶部 `14%` 线性羽化与 `headbase:2` 无缝融合；高度须调至能露出背面大蝴蝶结 |
| `torso` / `torso_pelvis` | `0`=正、`1`=向左弯腰、`3`=侧面、`6`=背面、`9`=俯视圆、`10`=X-ray；无袖与大臂分体 | **严禁在 `torso` 画袖子**；`torso:3` 必须修身窄腰无袖（去掉背结/腰带/下摆弧）；`torso_pelvis:3` 必须是**窄小纯色梯形连接块**（勿画宽白条），否则会在 `skirt:0`/`skirt:3` 两侧外凸穿帮 |
| `skirt:0..4` | `0`=正/侧站立、`1`=背站立、`2`=正/背跑、`3`=动态宽摆、`4`=X-ray；**`skirt:1/3` 禁画静态尾巴** | **`skirt:3` 是最大高频坑！** 原版 `wendy` 的 `skirt:3`（`128×48`）用于**侧跑（与 `skirt:0` 交替）+ 正/背面蹲坐、吃东西、捕虫、挥鞭**，必须画成**宽高比 ~`2.2:1` 的宽矮上扬弧形裙**（围裙居中对称），**绝不能画成 `1:1` 窄高侧身裙**，否则侧跑按高度缩放后宽度缩水导致双腿严重穿模、正背动作朝向穿帮 |
| `arm_lower:6` | Sheet 3 中紧挨着 X-ray 槽位，模型极易误画成骨头 | **`arm_lower:6` 是普通水平袖子（带白袖口）**，只有 `arm_lower:3, 4` 才是 X-ray 前臂骨 |
| `arm_upper_skin:0..1` | 特殊抬臂动画（`coach`/钓鱼/挥杆）会调用此槽位代替 `arm_upper` | 穿袖角色必须重绘成袖子主色椭圆（不带袖口），避免动作瞬间变回肉臂/原版白袖 |
| `face:*` 基础与口型组 | `0/8/9/10` 在说话/进食/饥饿动画中高速切换；若独立生成 `9/10`，眨眼外轮廓与眼位偏移会导致**眼睛剧烈闪烁** | 1. 全部 `face` 均为**零肤色纯绿透明贴纸**，不画下巴线与肉色圆脸；<br>2. **`face:0/8/9/10` 必须像素级锁死完全相同的眉眼鼻与锚点坐标（误差 `<0.01 px`），仅替换嘴巴开合**；嘴部需紧收至下巴轮廓内（`face:10` 张嘴不破下巴） |
| `face:28` | 用于 `emoteXL_waving4`、`emoteXL_bonesaw`；容易与 `face:0` 眼色/高光/右眼造型不一致 | 原版语义是**右眼（画面右侧）、右眉与鼻尖与 `face:0` 100% 一致，仅左眼（画面左侧）眯小**配斜挑眉与微撇嘴；务必直接基于定稿 `face:0` 派生 |
| `face:2/6/12/23/31` | 常见误绘：`2` 没闭紧、`6` 画出两只眼或大白鼻、`12` 复用 `X_X`、`23` 复用哭眼、`31` 沿用欧美灰黑眼圈 | `2`=正面双目全闭；`6`=朝右侧面仅单眼闭合+单线鼻；`12`=紧张焦虑大眼**朝右下方看**（适配钓鱼咬钩）；`23`=闭眼无泪张嘴打哈欠；`31`=**去灰眼圈的纯二次元无高光淡蓝失神瞳** |
| `cheeks:0, 1` | 画成 360° 闭合描边圆球会像贴了两颗肉球 | 画成**朝嘴巴内侧完全开口的 C 形鼓腮**（内侧无描边融入头壳肤色，对齐头壳肤色与中心腮红） |
| `hand:3, 11, 12, 15` | `hand:3` 易画成手背；`11/12` 易漏 X-ray 骨架；**`15` 最易误判（原版轮廓像杯子/圆柱）** | 1. `hand:3`=**掌心朝前握拳**（拇指横压四指指腹上方）；<br>2. `hand:12`=X-ray 骷髅拳（`hand:11` 按实际模板核对是否需补 X-ray）；<br>3. **`hand:15`（`emoteXL_bonesaw` 专属）不是水杯也不是真钢锯**：原版是角色手握顶部折口拿着**一块表面用白粉笔/蜡笔画着锯子涂鸦的牛皮瓦楞纸板**（锯齿须在**左侧边缘**，且底部留足余量与外伸前臂重叠）来回锯自己手臂的喜剧道具 |
| **全部 X-ray 槽位** | Prompt 一旦出现 `electrocuted` / `lightning` / `spark` / `glow` / `flash`，生图模型必在轮廓外乱画闪电与裂纹 | **Prompt 严禁上述闪电词**；仅用：`PURE SOLID BLACK (#000000) silhouette with crisp PURE WHITE (#FFFFFF) cartoon skeleton / X-ray stripes strictly INSIDE, clean #00FF00 background, NO external VFX` |

---

### 1.4 标准工作流、AI vs 程序化精修分工与质检门禁

#### 标准工具链命令（统一使用 `.\.venv-mcp\Scripts\python.exe`）

```powershell
# 1. 提取原版模板与 3 张初始绿幕母表（skins/<skin>/work）
.\.venv-mcp\Scripts\python.exe python\tools\skin_pipeline.py extract --out skins\<skin>\work

# 2. 按需生成局部 Rework 模板（推荐单格用 --cols 1 --cell-size 680）
.\.venv-mcp\Scripts\python.exe python\tools\skin_pipeline.py rework --work skins\<skin>\work --name rework1 --cols 2 --cell-size 680 --slots "hair:0=正面整片披发" "hair:1=侧面向左下飘发"
# 或从 review.html 导出的 rework_plan.json 生成：
.\.venv-mcp\Scripts\python.exe python\tools\skin_pipeline.py rework --work skins\<skin>\work --name rework1 --cols 2 --cell-size 680 --plan skins\<skin>\rework_plan.json

# 3. 一键打包 + 生成 69 帧对比审核表 + 交互调参器 + 动画播放器（默认 compact 套件 = 100% 槽位覆盖）
.\.venv-mcp\Scripts\python.exe python\tools\skin_pipeline.py refresh --work skins\<skin>\work --out mods\dst_buddy_agent\anim\skin_<skin>.zip --build-name skin_<skin> --suite compact

# 4. 导出关键动作 GIF / 排查单个动画调用的槽位
.\.venv-mcp\Scripts\python.exe python\tools\skin_pipeline.py inspect-anim --anim "C:\Program Files (x86)\Steam\steamapps\common\Don't Starve Together\data\anim\player_emotesxl.zip" --clip emoteXL_bonesaw --frame 30
.\.venv-mcp\Scripts\python.exe python\tools\skin_pipeline.py gif --clip run_loop --facing side --skin mods\dst_buddy_agent\anim\skin_<skin>.zip

# 5. 运行皮肤管线与 Lua/Python 同步回归测试
.\.venv-mcp\Scripts\python.exe -m unittest discover -s python\tests -t python -p test_skin_pipeline.py
```

#### AI 生图 vs 程序化（Pillow 4× 超采样）分工策略

| 适合 AI 生图（模板 + 分拆官图） | 更适合 Pillow 4× 超采样合成 / 变形（零色偏 & 零闪烁） |
| :--- | :--- |
| 全新全身三表初稿（`sheet1..3`） | 说话口型组 `face:0/9/10`（像素级锁眼，仅改唇形） |
| 结构复杂的新部件（`headbase:2`、`hair:0..3`、`tail:0,6`、`torso:3`） | 单眼变化 `face:28`（100% 锁死 `face:0` 右眼与鼻，仅缩小左眼） |
| 独立手势造型（`hand:0..19` 初稿与抓握解剖） | 动态变形件 `skirt:3`（从定稿 `skirt:0` 预乘 Alpha 扇形展开至 `~2.2:1`） |
| 全新情绪眼神（`face:12`、`face:23`、`face:31`） | 几何/矢量道具与贴片（开放 C 形 `cheeks:0/1`、纸板涂鸦锯 `hand:15`） |

> **Pillow 绘图深坑提示**：不要在 RGBA 底图上直接用 `ImageDraw.Draw(img)` 绘制半透明笔触（`alpha < 255`），`ImageDraw` 默认**覆盖像素 Alpha** 而不是做 Alpha 混合，打包合成绿幕后会漏出底色斑点！内部阴影、高光、笔触**要么直接用不透明色（`a=255`）预混成实色后绘制，要么画在独立透明图层再用 `Image.alpha_composite` 合成**。

#### 质检与防回退纪律

1. **子代理 7 项自检门禁**：每次 AI 生成 `reworkN_ai.png` 后，必须读取 `reworkN_review.example.json` 逐格核验并输出 `reworkN_review.json`，7 项全过才允许合入工作目录：`correct_part` / `view_and_pose_match` / `art_style_matches_reference` / `outline_within_cell` 为 `true`，`contains_adjacent_parts` / `background_drawn_as_part` / `edge_artifacts` 为 `false`。
2. **`offsets.json` 保护原则**：手动在 `skin_tuner.html` 调好的偏移保存在 `skins/<skin>/offsets.json`（高于 `work/` 优先级），任何自动化脚本与 rework 仅允许新增/更新当前精修槽位，**严禁覆盖用户已调定的偏移参数**。
3. **最终实机形态核验**：每次 `refresh` 后检查 `review_data/review_report.md` 确认 `Coverage = 100.0%`、`Missing frames = 0`，并在 `anim_player.html` 检查 `idle_loop`、`run_loop (side)`、`chop_loop`、`eat`、`build_loop`、`emoteXL_bonesaw`、`shock`、`emote_yawn` 等关键连续帧。

---

## 二、可复用英文 Prompt 模板（已融合全部修正）

> **复用说明**：替换下方 `[CHARACTER_NAME]`、`[PALETTE_AND_OUTFIT_RULES]` 与 `Image 2` 参考图路径即可直接用于新角色（模板路径已统一对齐为 `skins/<skin>/work/...`）。

### 2.1 Sheet 1 — 头部 / 10 核心表情 / 开放腮红 / 骷髅 X-ray（6×4）

- **Image 1**：`skins/<skin>/work/sheet1_head_face.png`（6×4 网格；`headbase_hat` 自动裁剪，`hairpigtails` 已替换为 `hair:0..3` 单独生成）
- **Image 2**：角色 SD Q 版三视图 + 表情参考图

```text
Task: Redraw every populated sprite cell in the provided 6x4 DST template grid (Image 1: solid black empty cells preserved at bottom-right) into the chibi character "[CHARACTER_NAME]" from Image 2, strictly matching the SD Q-chibi proportions and official color palette.

STRICT LAYOUT & CHROMA-KEY RULES:
- Preserve the exact grid layout, black cell borders, dark top header bars with white slot labels, solid black empty cells, and pure solid #00FF00 neon green background inside every populated cell.
- Clean uniform anime cel-shaded outlines, zero drop shadows, zero glow, zero gradients on the green background.
- Leave 15-20% clean #00FF00 green padding around every sprite (especially below the chin and hair tips)—NO part may touch or be flat-clipped by the borders.

CHARACTER DESIGN & PALETTE ([CHARACTER_NAME]):
- [PALETTE_AND_OUTFIT_RULES: Hair colors, signature headpiece/ears/ahoge, warm peach skin tone, eye colors]

PER-GROUP CELL RULES:
1. headbase:0 (front), headbase:1 (side), headbase:2 (back):
   - Rounded Q-chibi BLANK head shell ONLY (NO eyes, NO eyebrows, NO mouth, NO blush). Match Image 2's matte anime cel-shading cleanly with 4-6 broad hair locks—no over-bright white halo ring on back hair.
   - CRITICAL ANTI-BLOCKING: Draw as FLOATING SHORT-BOB-LENGTH HEADS with 15-20% empty #00FF00 green below the chin/nape.
   - headbase:0 (front): 100% pure #00FF00 green space IMMEDIATELY underneath the dark chin curve outline—ZERO back-hair fill behind the neck/chin.
   - headbase:1 (side): STRICTLY FACING RIGHT (nose/chin points RIGHT; back hair/bow sit on LEFT and taper smoothly around nape height).
   - headbase:2 (back): Matte cel-shaded full back head with signature headpiece/ears; clean soft hair tips at bottom.

2. headbase:3 (X-RAY SKULL):
   - Keep [CHARACTER_NAME]'s exact chibi head silhouette filled in PURE SOLID BLACK (#000000), with a crisp PURE WHITE (#FFFFFF) cute cartoon skull + hollow eye sockets + clenched teeth strictly INSIDE. Pure #00FF00 green outside—ZERO lightning bolts, sparks, cracks, glow, or external VFX.

3. face:0, 2, 4, 6, 9, 11, 13, 15, 21, 25 (TRANSPARENT FLOATING DECALS — ZERO SKIN PIXELS):
   - ALL face cells MUST have 100% pure solid #00FF00 neon green between and around eyes, brows, blush, and mouth—ZERO peach skin fill, ZERO face oval, ZERO chin outline, NO heavy dark circles around eyes, and NO pixel-art jaggedness. Smooth anime cel-shaded linework matching Image 2's expression sheet!
   - CRITICAL LIP-SYNC LOCK RULE: face:0 (closed smile) and face:9 (half-open mouth) MUST share IDENTICAL eye shape, eye size, eyebrows, and tiny nose alignment—ONLY the mouth changes!
   - * face:0: default front neutral/cheerful expression (signature anime gradient irises + highlights, thin brows, tiny point nose, small gentle smile).
   - * face:2: front COMPLETELY CLOSED eyes (smooth downward-curved lashes, subtle concerned brows, small mouth; zero pupils).
   - * face:4: side-view default face STRICTLY FACING RIGHT (single right-facing anime eye, brow, subtle blush, tiny profile smile).
   - * face:6: side-view COMPLETELY CLOSED SINGLE EYE STRICTLY FACING RIGHT (only ONE closed eye curve + delicate single-line anime nose—NO second eye, NO thick triangular cartoon nose).
   - * face:9: front talking / half-open mouth (IDENTICAL eyes/brows/nose to face:0, with a compact open mouth tucked cleanly above chin height).
   - * face:11: front peaceful sleeping closed eyes (gentle relaxed curves + calm mouth).
   - * face:13: front dizzy / knocked-out X_X eyes (clean dark anime X_X eyes, worried brows, tiny wavy mouth).
   - * face:15: front ecstatic laughing face (joyful upward-curved ^_^ or sparkling wide eyes + open happy laugh mouth).
   - * face:21: front angry / grumpy face (angled brows, sharp half-narrowed signature irises, small frown).
   - * face:25: front wailing / shouting face (tightly squeezed >_< closed eyes + wide open distressed mouth).

4. cheeks:0, cheeks:1 (OPEN C-SHAPE PUFFED CHEEKS — NOT FULL CLOSED BALLS):
   - Draw bloated puffy anime cheeks matching [CHARACTER_NAME]'s skin fill and soft pink center blush, with crisp dark-brown outline ONLY on the outer/top/bottom arc, leaving the inner edge facing the mouth COMPLETELY OPEN (no outline stroke on the inner edge) so it blends seamlessly into headbase skin!
```

### 2.2 Sheet 2 — 无袖上衣 / 紧凑骨盆 / 无尾裙摆 / 骨骼 X-ray（6×5）

- **Image 1**：`skins/<skin>/work/sheet2_body_skirt.png`（6×5 网格）
- **Image 2**：角色三视图与服饰细节参考图

```text
Task: Redraw every populated sprite cell in the provided 6x5 DST body/skirt template grid (Image 1) into the outfit of "[CHARACTER_NAME]" from Image 2.

STRICT LAYOUT & CHROMA-KEY RULES:
- Preserve the exact 6x5 grid, black borders, dark label headers, solid black empty cells, and pure #00FF00 green background.
- Every torso cell contains ONLY the isolated sleeveless bodice shown in Image 1 (NO head, NO arms/sleeves, NO hands, NO legs/shoes, NO tail).

OUTFIT & ALIGNMENT RULES:
- [PALETTE_AND_OUTFIT_RULES: Dress colors, bodice embroidery, collar bow, apron/belt details, hem patterns]
- torso:0 (front), torso:6 (back), torso:1, 2, 4, 5, 7, 8, 9: Isolated SLEEVELESS bodice matching the exact lean/twist angle of each corresponding vanilla cell in Image 1.
- torso:3 (SLIM SIDE BODICE — FACING RIGHT): Slim, narrow right-facing side-view sleeveless bodice matching Image 1's narrow upright torso:3 silhouette ONLY—front apron/bib details on the RIGHT, fitted bodice back on the LEFT, ending cleanly and narrowly at the waist. STRICTLY NO sleeves, NO bulky back waist bow, NO lower skirt flare.
- torso_pelvis:0..9: Compact waist/hip connector bands matching skirt waist width; torso_pelvis:3 MUST be a slim, narrow solid-toned trapezoid with crisp outline (NO wide bulging side panels/bows) so it tucks cleanly inside skirt:0 and skirt:3!
- skirt:0 (front/side stand): Crisp pleated skirt + centered front apron/detail + hem lace/pattern (NO tail).
- skirt:1 (back stand): Clean back pleated skirt + large back waist bow (CRITICAL: STRICTLY NO TAIL attached to skirt:1—tail is rigged separately in tail:0!).
- skirt:2 (front/back run wide): Wider flared skirt for forward/back running (NO tail).
- skirt:3 (WIDE FLARED RUNNING/CROUCHING SKIRT — CRITICAL ~2.2:1 ASPECT RATIO!):
  * Match vanilla skirt:3's WIDE, SHALLOW, UPWARD-FLARED CRESCENT SILHOUETTE (~2.2:1 width-to-height ratio, wide wing tips lifting outward on BOTH left and right, symmetrical centered front detail)—STRICTLY NOT a tall narrow 1:1 side-skirt and NO attached tail! Must cleanly cover striding legs in side run_loop AND work symmetrically in eat/crouch/sit/whip/bugnet.

X-RAY SKELETON CELLS (NO LIGHTNING BOLTS, NO SPARKS, NO CRACKS OUTSIDE SILHOUETTE):
- torso:10: PURE SOLID BLACK (#000000) sleeveless bodice silhouette + PURE WHITE (#FFFFFF) cartoon ribcage/spine strictly inside.
- torso_pelvis:10: PURE SOLID BLACK (#000000) pelvis silhouette + PURE WHITE (#FFFFFF) hip bone strictly inside.
- skirt:4: PURE SOLID BLACK (#000000) flared skirt silhouette + stark PURE WHITE (#FFFFFF) vertical X-ray stripe bands strictly inside.
```

### 2.3 Sheet 3 — 手臂 / 腿 / 鞋子 / 骨骼 X-ray（7×5）

- **Image 1**：`skins/<skin>/work/sheet3_limbs.png`（7×5 网格）
- **Image 2**：角色三视图参考图

```text
Task: Redraw every populated limb sprite cell in the provided 7x5 DST template grid (Image 1) into the sleeves, socks/legs, and shoes of "[CHARACTER_NAME]" from Image 2.

STRICT LAYOUT & CHROMA-KEY RULES:
- Preserve exact 7x5 grid, black borders, dark label headers, solid black empty cells, and pure #00FF00 green background.
- Match the EXACT length, thickness, joint angle, and orientation of every single limb piece in Image 1 so animations stay aligned without popping.

NORMAL LIMB DESIGN:
- arm_upper:0..4: [CHARACTER_NAME] upper sleeves/arms matching Image 1's angle and thickness.
- arm_lower:0, 1, 2, 5, AND 6 (CRITICAL NOTE: arm_lower:6 IS A NORMAL HORIZONTAL SLEEVE WITH CUFF — NOT X-RAY!): Fitted forearm sleeves ending in signature cuffs at the wrist end matching Image 1.
- leg:0..8: Knee-high socks/tights and matching skin tone strictly where vanilla shows thigh/knee segments.
- foot:0..6: Signature shoes with ankle strap/cuff strictly matching each cell's left/right/front/side direction from Image 1.

X-RAY SKELETON CELLS (PURE #000000 + #FFFFFF ONLY, NO EXTERNAL VFX):
- arm_upper:5 & arm_upper:6: PURE SOLID BLACK (#000000) upper-arm silhouette + PURE WHITE (#FFFFFF) humerus bone strictly inside.
- arm_lower:3 & arm_lower:4: PURE SOLID BLACK (#000000) forearm silhouette + PURE WHITE (#FFFFFF) radius/ulna bones strictly inside.
- leg:9 & leg:10: PURE SOLID BLACK (#000000) leg silhouette + PURE WHITE (#FFFFFF) femur/tibia bones strictly inside.
- foot:7: PURE SOLID BLACK (#000000) shoe silhouette + PURE WHITE (#FFFFFF) foot/toe skeleton bones strictly inside.
```

### 2.4 独立补件模板（`hair` / `tail` / `hand` / `arm_upper_skin`）

当角色需要**整片披发**、**独立动态尾巴**、**全套定制 Q 版手部**或**长袖防露臂**时，按以下 4 套标准网格模板补充（推荐作为 `rework` 层自动覆盖，或单独生成后由管线加载）：

1. **`hair:0..3`（2×2 整片披发，彻底取代双马尾）**：
   - `hair:0`：正面视角用的对称宽幅波浪大长发帘（纯绿底，零头壳与身体）。
   - `hair:1`：右向侧面视角用的长披发帘，发根位于单元格**右上方**，向**左下方**自然飘散波浪卷曲。
   - `hair:2`：背面视角用的**完整单片一体大波浪背发**（严禁中分劈叉或双马尾，顶部留柔和原色不画粗黑封闭边线，高度与头壳搭配以便露出背面大蝴蝶结）。
   - `hair:3`：纯黑剪影 `#000000` + 内部纯白曲线 X-ray 高光条纹，无电击闪电特效。
2. **`tail:0, 6`（2×1 独立动态大尾巴，如需要）**：
   - `tail:0`：粗根部位于**右上方**、流线弯向**左下方**并向左上扬起尾鳍的独立大尾巴（对齐三视图配色，纯绿底无裙摆/身体）。
   - `tail:6`：完全一致轮廓的纯黑剪影 `#000000` + 内部纯白分段尾脊骨 X-ray，无电击闪电特效。
3. **`hand:0..19`（5×4 Q 版手部全集，重点修正 3 处高危手型）**：
   - 统一使用角色暖桃肤色（如 `#FCE3D4`）与细腻暗棕/深海军蓝赛璐璐线稿，不画袖口（袖口已在 `arm_lower` 上），100% 对齐 Image 1 每格的手腕连接方向与手指姿势；
   - `hand:3`：**PALM-FACING 掌心朝前握拳**（拇指横压在四只向上卷曲的圆润指腹上方，切勿画成手背）；
   - `hand:12`（及按模板核对后的 `hand:11`）：纯黑 `#000000` + 内部纯白 `#FFFFFF` 骷髅指骨 X-ray（零闪电）；
   - **`hand:15`（`emoteXL_bonesaw` 专属搞笑道具 — 瓦楞纸板白粉笔涂鸦骨锯）**：**禁止画成纸杯或真钢锯！** 严格绘制角色小手捏住顶部深色折口、竖直略微上窄下宽的**牛皮瓦楞纸板模型**（带右侧瓦楞切面纹与胶带），纸板正面用**不透明白粉笔/蜡笔**涂鸦出一把骨锯图案（锯柄在手下方、**锯齿必须在左侧边缘**、末端带小圆孔），确保动画旋转后锯齿贴紧手臂。
4. **`arm_upper_skin:0..1`（2×1 长袖角色防穿帮）**：
   - 直接按角色上臂袖子的纯袖色与阴影重绘两节椭圆（零肤色、零白袖口），消除 `coach`/高尔夫/钓鱼等动画露原版臂膀的问题。

### 2.5 局部 Rework 通用提示词头部与 7 项自检门禁

每次使用 `skin_pipeline.py rework --plan ...` 生成 `reworkN_template.png` 时，提示词头部固定如下：

```text
IMAGE 1 is a <R>x<C> labeled DST sprite rework template on pure #00FF00 green screen.
IMAGE 2 is the approved official character reference sheet ([ref_sd / ref_normal / ref_expressions]).
Redraw ONLY the listed sprite cells to match IMAGE 2's art style, palette, and proportions.
Keep every cell strictly separated inside its own #00FF00 green box; preserve the black header bar, white labels, grid borders, and pose/orientation of IMAGE 1.
Do NOT add neighboring body parts, full bodies, extra props, lightning/electric VFX, pixel-art jaggedness, or skin ovals on face cells.
```

> 每次生成完毕必须由生图子代理或主控读取 `reworkN_review.example.json` 逐格填写 7 项检查输出 `reworkN_review.json` 后方可打包上线。

---

## 三、DeepSeek 皮肤专属档案与精简迭代日志

### 3.1 DeepSeek 专属色板与视觉规范

- **发色**：浓郁宝蓝/女仆蓝 `#2B4EA2` ~ `#3D68CC` 主体，仅最底端发梢渐变为冰青蓝 `#7BD8EE`（**严禁偏绿 / teal**）。
- **线稿**：均匀细腻深海军蓝 `#141C47` 赛璐璐闭口线稿（手部/腮红皮肤线可用暖深棕 `#4B341E`）。
- **头饰与特征**：白色褶边女仆发箍 + 左侧浅蓝小蝴蝶结 + 顶部单根弯曲呆毛 + 对称下垂鲸鳍耳（外侧深蓝、内侧雪白）。
- **皮肤与面妆**：暖桃肤色 `#FCE3D4` / `#FFD6BD`；宝石蓝渐变虹膜 + 亮白高光点，零灰黑眼圈。
- **女仆装与大鲸尾**：深海蓝维多利亚紧身女仆上衣（白立领 + 小领结 + 金星纽扣）、纯白蕾丝褶边围裙（正面右侧带一只小深蓝鲸鱼刺绣）、金色星图裙摆刺绣与白蕾丝衬裙边；背面大白腰结 + 大深海蓝鲸尾（浅冰银灰腹面 `#C8D8F0`）。

### 3.2 最终生效槽位来源与核心 `offsets.json` 映射表

打包管线（`pack_skin`）按“**3 张母表打底 → `rework1..rework33` 按自然数字顺序增量覆盖 → 程序化派生件 → `skins/deepseek/offsets.json` 锚点微调**”生成最终 [skin_deepseek.zip](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/mods/dst_buddy_agent/anim/skin_deepseek.zip)：

| 槽位组 | 最终生效数据源 | 关键 `offsets.json` 锁定参数 | 说明 |
| :--- | :--- | :--- | :--- |
| `headbase:0, 1, 3` | `rework2_ai.png` (`0`) + `sheet1_ai.png` (`1, 3`) | `0: [0, 11, 0.9]`, `1: [-6, 12.5, 0.9]` | 下巴正下方净空无垂发；`headbase_hat:0..3` 自动派生 |
| `headbase:2` | `rework20_ai.png`（分拆 `deepseek_ref_sd.png`） | `2: [0, 18]`（手动保护项） | 哑光二次元赛璐璐背面短发，去亮白高光环 |
| `hair:0..3` + `hair_hat` | `rework21_ai.png` | `0/3: [0, -6, 0.94]`, `1: [4, -10, 0.94]`, `2: [0, -6, 0.85]` | 取代 `hairpigtails` 的整片披散长卷发，`hair:2` 顶部 14% 羽化 |
| `tail:0, 6` | `rework22_ai.png` | `tail:0: [2.5, 15.5, 0.5]`, `tail:6: [0, -6]` | 独立动态大鲸尾（源图预旋 `-22°`），`1..5, 7..11` 自动空帧 |
| `torso:0, 2, 4..10` | `sheet2_ai.png` + `rework1_ai.png` (`10`) + `rework10_ai.png` (`1`) | `torso:1: [-10, 4.5, 0.8]` | 无袖女仆上衣，X-ray 仅黑白肋骨无闪电 |
| `torso:3`, `torso_pelvis:3` | `rework24_ai.png` | `torso:3: [-2, 0, 0.82]`, `torso_pelvis:3: [0, 0, 0.68]` | 窄腰修身侧上衣 + 紧凑纯深蓝梯形骨盆，消灭腰侧外凸 |
| `skirt:0, 1, 2, 4` | `sheet2_ai.png` + `rework1` (`4`) + `rework23` (`1`) | `skirt:0: [0, -12]`（手动保护项） | 背面 `skirt:1` 含大白蝴蝶结但**无静态尾** |
| `skirt:3` | `rework25_ai.png`（`sheet2` `skirt:0` 2× 超采样扇形展开） | `skirt:3: [-5, -10, 0.94]` | `542×244`（`2.221:1`）宽矮飘扬弧裙，彻底消除侧跑穿模 |
| `face:0, 9, 10` | `rework14_ai.png`（基于 `rework11` 上收嘴距 `15 px`） | `0: [-11.5, 11, 0.9]`, `9: [-11.5, 8.42, 0.9]`, `10: [-11.5, 8.8, 0.9]` | 三口型像素级锁眼，高频率说话零眼闪，张嘴收于下巴内 |
| `face:28` | `rework26_ai.png`（基于 `rework14` `face:0` 直改） | `face:28: [-10.93, 11.35, 0.8571]` | 右眼/右眉/鼻与 `face:0` 100% 一致（中心误差 `<0.01 px`），仅左眼眯小 |
| `face:2, 6` / `12` / `20` / `23` / `25` / `26` / `27` / `31` | `rework9` (`2,6`) + `rework33` (`12`) + `rework31` (`20`) + `rework7` (`23`) + `rework30` (`25,26`) + `rework29` (`27`) + `rework19` (`31`) | `12: [-10, 9.5, 0.85]`, `20: [-12.73, -1.22, 0.8221]`, `25: [-9.0, 29.5, 0.8869]`, `26: [-12.0, 16.5, 0.8557]`, `27: [-9.56, -8.7, 1.2505]` | `12` 为正视受惊缩瞳（紧凑宽高比 `1.583`）；`20` 基于 `face:0` 直改紧凑半睁眼+小下撇嘴（100% 锁瞳色）；`25/26/27/31` 全无灰圈 |
| `cheeks:0, 1` | `rework12_ai.png`（4× 超采样） | `0: [-0.5, 6.5, 0.65]`, `1: [-23.5, 12.5, 0.65]` | 朝嘴开口的 C 形鼓腮，对齐 `#FFD6BD` 肤色 |
| `arm_upper_skin:0, 1` | `rework28_ai.png`（`0` 复用 `sheet3` `arm_upper:0`，`1` 4× 超采样） | `arm_upper_skin:0: [-0.55, 0.07, 1.9333]` | `0` 直接复用深蓝泡泡袖 `arm_upper:0`（全角色/动作质感统一）；`1` 为 `#2E3A67` 光滑短袖管 |
| `arm_lower:6` | `rework1_ai.png` | 无 | 修正原 Sheet 3 误画的骨骼为正常深蓝袖+白袖口 |
| `hand:0..19` | `rework3` → `rework4` → `rework5` (`3`) → `rework6` (`11`) → `rework27` (`15`) | `0/2/3/5/6/8/10/13/14` 微调 | `hand:3` 掌心朝前；`hand:15` 为 4× 超采样瓦楞纸板白粉笔涂鸦骨锯 |

### 3.3 精简迭代日志（v1–v6 & Rework 1–33）

- **母表架构阶段（v1–v6）**：确立宝蓝→冰青发色、侧面朝右、短波波头 `headbase`、`face` 纯绿悬浮贴纸；v4 从 Sheet 1 拆出 `headbase_hat` 改由管线裁剪派生；v5/v6 补全三张表全部 X-ray 槽位并清除所有闪电/电击关键词，明确 `arm_lower:6` 为普通水平袖。
- **Rework 1–5（修坏格 & 全手重构）**：`Rework 1` 去除 X-ray 闪电并修回 `arm_lower:6`；`Rework 2` 净空 `headbase:0` 下巴下方纯绿；`Rework 3–5` 重绘全套 20 帧 Q 版手部并修正畸形手指与 `hand:3` 掌心朝前握拳。
- **Rework 6–10（高危缺口 & 全动画审计）**：`Rework 6` 修复 `hand:11` X-ray 与 `cheeks:0/1` 单侧鼓腮；`Rework 7` 补齐 `arm_upper_skin:0/1` 与 `face:12/23/28`；`Rework 8–9` 引入 `review.example.json` **7 项自检门禁**重绘光滑日系 `face:12/28/2/6`；`Rework 10` 重绘左倾弯腰 `torso:1`。
- **Rework 11–14（程序化像素锁眼 & 开放腮）**：`Rework 11` 锁死 `face:0/9/10` 眉眼与坐标并修复自然数字排序；`Rework 12` 将闭合圆球鼓腮改回 4× 超采样开放 C 形；`Rework 13` 修正 `face:12` 3/4 朝右下紧张看；`Rework 14` 将口型组嘴距向上收紧 `15 px`，兼顾说话不闪与 `build_loop` 张嘴不破下巴。
- **Rework 15 / 17 / 18–20（背面头壳质感 & 失神眼画风统一）**：`Rework 15` 补纹理但高光过亮；`Rework 18–19` 将 `face:31` 从美式灰黑眼圈彻底重构为纯二次元无高光失神蓝瞳；`Rework 20` 改用去字分拆图 `deepseek_ref_sd.png` 重绘哑光赛璐璐版 `headbase:2` 统一画风。
- **Rework 21–23（披发 & 独立鲸尾架构升级）**：Lua 与 Python 全面上线整片披发 `hair:0..3` + 独立动态鲸尾 `tail:0, 6`，置空 `hairpigtails` 等 4 项原版部件，清除 `skirt:1/3` 静态尾彻底解决背面双马尾裂开与双尾重影。
- **Rework 24–27（实机细节最终打磨）**：
  - `Rework 24`：瘦身侧面 `torso:3` & 紧凑纯深蓝 `torso_pelvis:3`，根治腰部外凸穿帮；
  - `Rework 25`：将 `skirt:3` 扇形展开为 `542×244`（`2.221:1`）宽矮飘扬弧裙，彻底消除 `run_loop` 等 8 组动作腿部穿模；
  - `Rework 26`：基于 `rework14` 的 `face:0` 直接像素级锁定 `face:28` 右眼、右眉与鼻尖（中心误差 `<0.01 px`），仅左眼眯小；
  - `Rework 27`：推翻误绘纸杯与真钢锯，4× 超采样重构原版语义还原的“牛皮瓦楞纸板 + 左侧齿白粉笔涂鸦锯子”喜剧道具 `hand:15`（同步验证 `a=255` 内部绘制防透绿技巧）；
  - `Rework 28`：`arm_upper_skin:0` 直接复用 `sheet3_ai.png` 的 `arm_upper:0` 深蓝女仆泡泡袖（配合 `offsets.json` `[-0.55, 0.07, 1.9333]` 对齐原位与尺寸），`arm_upper_skin:1` 以 4× 超采样重构对齐 `#2E3A67` 的光滑短袖管；
  - `Rework 29`：用 `rework29_template.png` + 官方高清表情图 `deepseek_ref_expressions.png` 重绘 `face:20`（`dozy` 昏昏欲睡无聊半睁眼 + 嘴角下垂）与 `face:27`（`pickaxe_recoil` `> <` 紧闭双眼 + 白齿咬牙用力狰狞嘴），零灰眼圈、零肤底并在 `dozy` 第 8 帧与 `pickaxe_recoil` 第 18 帧实机核验对齐无误；
  - `Rework 30`：重绘 `face:25`（`emote_fistshake` 去眼泪的挫败愤怒无奈大喊，保留中心锁定确保继承 `face:29`/`32` 不漂移）与 `face:26`（`refuse_eat` 眼睛紧闭、嘴巴微张的抗拒嫌弃神态，无眼泪），实机复检 `refuseeat` 与 `emote_fistshake` 全部贴合；
  - `Rework 31`：改用 `rework14` 的高清 `face:0` 直接 4× 超采样合成 `face:20`（`dozy` 昏昏欲睡），100% 锁定 `face:0` 宝蓝→冰青虹膜色板、压平上眼睑遮去高光、下移贴合眉毛并替换为极小下垂嘴（`110.4 × 47.4` 紧凑比例），实机复检 `dozy` 第 8 帧眼色与紧凑度彻底统一；
  - `Rework 32`：用 `rework32_template.png`（1×1 原版 `face:12`）与 `deepseek_ref_expressions.png` 重绘 `face:12` 探索缩瞳受惊与紧凑间距；
  - `Rework 33`：接入用户网页版生成的正视受惊缩瞳定稿（无侧偏、自然紧凑五官 `801×506` / 宽高比 `1.583`，对齐贴合 `rework33_ai.png` `346×372`，打包尺寸 `176×111` / 逻辑 `100.83×63.58`），实机复检 `034` (`bite_light_loop`) 与 `idle_inaction_sanity` 第 5 帧均干净无杂边。
