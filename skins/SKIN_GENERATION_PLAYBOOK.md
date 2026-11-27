# DST AI 角色皮肤生成与零耦合架构复用指南（6 表工作流 v2）

> 本手册沉淀自 **Gemini 皮肤实战及全量返工教训**（涵盖 `hairpigtails:0..4` 单侧单束规则、`tail:0/6` 横向绘制与骨骼旋转校准、`cheeks:0/1` 纳入初版 AI 母表防 `eat`/`dart` 违和，最终达成 `130/130 (100.0%)` 全覆盖与零回改前代 DeepSeek 管线），是制作下一套新角色皮肤（如 Claude / ChatGPT 等）的**标准蓝本**。
>
> - 底层全量 Sprite 语义与原版复用实测字典：[SPRITE_ATLAS_GUIDE.md](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/SPRITE_ATLAS_GUIDE.md)
> - v2 标准参考实现：[gemini_skin_pipeline.py](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/python/tools/gemini_skin_pipeline.py) ｜ 配套单测：[test_gemini_skin_pipeline.py](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/python/tests/test_gemini_skin_pipeline.py)
> - 通用离线试装/调参/播放下游：[skin_preview.py](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/python/tools/skin_preview.py) ｜ [skin_tuner.py](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/python/tools/skin_tuner.py) ｜ [skin_player.py](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/python/tools/skin_player.py)

---

## 一、零耦合多皮肤架构规范

核心原则：**通用渲染下游直接复用，每个新皮肤独占独立打包/派生脚本与资产目录，绝不回改旧皮肤代码**。

### 1.1 分层职责清单

| 层级 | 文件 / 目录 | 复用方式 | 约束 |
| :--- | :--- | :--- | :--- |
| **共享下游渲染层** | `python/tools/skin_preview.py`<br>`python/tools/skin_tuner.py`<br>`python/tools/skin_player.py` | **直接复用**（通过 `--skin` / `--work` 参数传入） | 已全面参数化，支持 20 个 `BODY_SYMBOLS` + 3 个 `BLANK_SYMBOLS`；无需因新增皮肤改动 |
| **独立管线脚本** | 复制 `python/tools/gemini_skin_pipeline.py` → `python/tools/<char>_skin_pipeline.py` | **复制后独立修改** | 替换 `PALETTE`、`TARGET_SKIN_RGB`、裁剪区域与特定修图逻辑；禁止耦合/修改前序皮肤脚本 |
| **独立测试** | `python/tests/test_<char>_skin_pipeline.py` | **独立新建** | 验证新模板、`derive_sprites`、`pack_skin`、打包产物 `build.bin` 与 Mod 注册 |
| **独立资产工作区** | `skins/<char>/reference/`<br>`skins/<char>/work/`<br>`skins/<char>/offsets.json`<br>`skins/<char>/prompts.md` | **每角色独立目录** | `work/`、`reference/`、`review_data/`、`tuner_data/`、`preview/` 等大文件已由 `.gitignore` 忽略 |
| **Mod 产物与注册** | `mods/dst_buddy_agent/anim/skin_<char>.zip` | **增量注册** | 仅向 Lua 注册表追加新条目 |

### 1.2 Mod 侧增量接入清单（3 处改动）

1. [modmain.lua](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/mods/dst_buddy_agent/modmain.lua)：在 `Assets` 中追加 `Asset("ANIM","anim/skin_<char>.zip"),`。
   - **重要防错守则**：如果 `anim/skin_<char>.zip` 尚未打包生成，**先注释该 `Asset` 行**（标 `-- Uncomment once anim/skin_<char>.zip is packed`），防止 DST 启动时因缺包报错，等首次 `pack`/`refresh` 成功产出后再取消注释。
2. [jev_companion_presentation.lua](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/mods/dst_buddy_agent/scripts/jev_companion_presentation.lua)：
   - `M.SKINS.<char> = "skin_<char>"`
   - 在 `M.resolve_skin` 与 `M.infer_skin` 中补充英文/中文别名映射。
3. [jev_companion_client.lua](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/mods/dst_buddy_agent/scripts/jev_companion_client.lua)：更新 `/buddy skin` 用法提示串。

---

## 二、新角色开工前 3 项必决项

在切图与写 Prompt 之前先锁定 3 个引擎约束相关设计决策，避免大返工：

1. **单面侧面视角（`face:4` 与头饰朝向）**：
   - DST 只有 1 套向右的侧面精灵（朝左行走由引擎水平翻转实现）。
   - 如果角色有**异色瞳**（如左红右金）或**不对称发饰**，需提前拍板侧面 `face:4` 采用哪一只瞳色（通常与官方三视图侧面一致），以及发饰在朝右侧面时如何摆放。
2. **左右共用肢体槽位简化（`leg:*` / `arm_upper:*`）**：
   - DST 左右腿共用 `leg:0..10`、左右大臂共用 `arm_upper:0..6`。若设定图上有单侧大腿环、单侧袜饰或单侧臂环，直接画进去会让**两条腿/两只手臂同时出现**；建议简化掉单侧大腿环，保留对称泡袜/小饰品。
3. **长发与尾巴架构确认**：
   - **长披发角色**：启用混合长发架构——`hairpigtails:0..4` **必须为单侧单束动态垂发**（因引擎正/侧面会同时挂载两束；严禁画成双侧整片或两撮重影），背面一体大波浪垫底走 `hair:2` / `hair_hat:2`，而 `hair:0,1,3` / `hair_hat:0,1,3` 打包为 `4×4` 透明占位帧（`long_hair_blank:0`），根治 `286` 个非背面动画的头发遮胸穿模。
   - **有尾角色**：独立做进 `tail:0`（常态）与 `tail:6`（X-ray），`skirt:1/2/3` 上严禁画死静态尾巴；因 DST `tail` 骨骼自带约 `-34°` 逆时针旋转，**`tail:0` 与 `tail:6` 必须直接画成横向舒展（`w > h`，尾根在正右侧 3 点钟、中段向左下微垂、尾尖向左上 9–10 点钟微翘）**，严禁画成竖直 J 钩（`h > w`）。

---

## 三、6 张高清专项母表拆分（93 格）与双图生图指南

相比早期“全角色硬塞 3 张混排大表”，v2 方案按**参考图来源**与**视觉域（彩色 vs 黑白 X-ray）**拆成 **6 张专项母表**：单格分辨率更高，且彻底消灭了“彩色 `arm_lower:6` 紧挨着 X-ray 格被误画成骨头”的问题；同时利用 Sheet 1 第 8 格（或 Sheet 5 空位）直接出 `cheeks:0`，省去后期再为 `eat` / `dart` 局部返工。

### 3.1 预处理参考分拆（`split-ref`）

先通过脚本从设定原图无损裁出 **4 张干净输入（严格去除文字说明、标签与彩蛋）**：
- `<char>_ref_sd.png`：SD Q 版正/侧/背三视图
- `<char>_ref_normal.png`：正常比例服饰细节三视图
- `<char>_ref_expressions.png`：纯表情栏
- `<char>_icon.png`：`240×240` 去白底透明头像（供 `SWAP_ICON:0` 使用）

### 3.2 6 张 AI 母表规格总览

| 母表 | 文件名 | 网格（有效格） | 建议画幅 | 槽位清单 | 搭配 `IMAGE 2` | 核心设计目的 |
| :---: | :--- | :---: | :---: | :--- | :--- | :--- |
| **1** | `sheet1_head_hair_tail` | `4×2`（**8 格满**） | `16:9` | `headbase:0, 1, 2`、`tail:0`（横向 `w > h`）、`hairpigtails:0`（左单束）、`hairpigtails:1`（右单束）、`hair:2`（背面一体大波浪整片）、`cheeks:0`（左鼓腮） | `<char>_ref_sd.png` | 毛发（猫耳/发饰/短波波头壳/单侧长发/横向尾巴）与 `cheeks:0` 同表生成，发色渐变、赛璐璐质感、面颊肤色/柔光腮红/下巴线稿 100% 锁死一致；`cheeks:1` 由 `derive` 严格水平镜像，`hairpigtails:2` 复用 `0`（若无尾则第 4 格也可直接放 `cheeks:1`）。 |
| **2** | `sheet2_faces_core` | `4×3`（12 格满） | `4:3` | **4 套锁眼母版**：`0, 4, 11, 25`<br>**8 个独立情绪**：`2, 6, 12, 13, 15, 21, 23, 31` | `<char>_ref_expressions.png` | 只画**眼型真正独立**的 12 格；其余 13 格“同眼换嘴/半睁”全部留给 Pillow 锁眼派生，彻底零眼闪。 |
| **3** | `sheet3_body_skirt` | `5×4`（17 格 + 3 黑空） | `4:3` | `torso:0..9`、`torso_pelvis:0, 3, 6, 9`、`skirt:0, 1, 2` | `<char>_ref_normal.png` | 剥离冗余 `torso_pelvis`、X-ray 和极易画歪的 `skirt:3`（改用 `skirt:0` 扇形展开），服饰星纹/褶皱更清晰。 |
| **4** | `sheet4_limbs` | `5×5`（25 格满） | `1:1` | `arm_upper:0..4`、`arm_upper_skin:0, 1`、`arm_lower:0, 1, 2, 5`、`leg:0..4, 6..8`、`foot:0, 1, 3, 4, 5, 6` | `<char>_ref_normal.png` | **零 X-ray 污染**；`arm_upper_skin:0, 1` 直接随初表生成防抬臂露肉；`arm_lower:6≡2`、`leg:5≡2`、`foot:2=mirror(0)` 免重画。 |
| **5** | `sheet5_hands` | `4×4`（14 格 + 2 黑空） | `1:1` | `hand:0..10, 13, 18, 19` | `<char>_ref_sd.png` | 剔除重复手（`14, 16, 17`）、X-ray（`11, 12`）与道具（`15`），单格像素倍增，严格杜绝多出一截小臂/三叉爪；若 Sheet 1 已满也可把 `cheeks:0, 1` 放入此表 2 个空位。 |
| **6** | `sheet6_xray` | `5×4`（17 格 + 3 黑空） | `4:3` | `headbase:3`、`hairpigtails:3, 4`（左右**单侧单束**）、`tail:6`（**横向 `w > h`**）、`torso:10`、`torso_pelvis:10`、`skirt:4`、`arm_upper:5, 6`、`arm_upper_skin:2`、`arm_lower:3, 4`、`hand:11, 12`、`leg:9, 10`、`foot:7` | `<char>_ref_sd.png` | 全部黑白电击骨骼独立隔离成表，整表只有 `#000000` + `#FFFFFF` + `#00FF00`，零彩色串色、零外部闪电杂线；`hairpigtails:3, 4` 保持单侧、`tail:6` 保持横向与 `tail:0` 同角。 |

### 3.3 双图生图操作铁律（API 子代理 / 网页版通用）

无论用内置生图子代理还是 Gemini 网页版 / AI Studio：
1. **每张母表独立开一个空白新对话**：防止上下文遗留前一张表的构图导致串格子或色偏。
2. **严格只传 2 张输入图（`IMAGE 1` = 绿幕模板，`IMAGE 2` = 对应分拆参考图）**：严禁把整张带文字大设定图、也不许把前序 AI 渲染图作为输入喂回生图模型。
3. **画幅选择**：按上表推荐比例（`16:9` / `4:3` / `1:1`）输出最高分辨率；下载 `.jpg` 或 `.webp` 后可直接转存 `.png`（注意：`sheet1` 若无暗底框文本，见 §5.1 自动处理）。

---

## 四、零生图派生体系（Pillow 4× `derive` + 像素直通复用）

全部剩余槽位由 `<char>_skin_pipeline.py derive`（4× 超采样，输出高清 `derived_sprites/*.png` + 可视化 `rework_derived_ai.png`）与 `pack_skin` 零损耗直通完成：

### 4.1 13 格像素锁眼表情派生

- `_extract_locked_eyes_face` 自动定位鼻点以下全部中央嘴部连通域（兼容 `face:11` 双半弧微笑与 `face:25` 超宽大喊嘴），干净剥离旧嘴后保留 `100%` 原像素眉眼鼻，再用 4× 矢量画新嘴：
  - 锁 `face:0`：`face:9`（半张说话）、`face:10`（大张饥饿/造物）、`face:16`（担忧倒 `o`）、`face:19`（闲坐微撇）
  - 锁 `face:0` 压平直上眼皮遮高光：**`face:20`（无聊半睁叹气）**、**`face:22`（同眼闭口撇嘴 `︵`）**（根治下睫毛/眼眶闪光）
  - 锁 `face:0` 右眼 + 压窄左眼：**`face:28`（大小眼坏笑）**、**`face:30`（大小眼恼火撇嘴）**
  - 锁 `face:11` 闭眼：`face:17`（委屈撇嘴）、`face:18`（小 `o` 呼嘴）
  - 锁 `face:25` `>_<` 挤眼：`face:26`（微张嘴抗拒）、`face:27`（咬紧白牙用力）、`face:29`（委屈小撇嘴）
- 通过 `lock_ref` / `lock_dx_px` / `lock_dy_px` / `lock_ref_size` 直接依据母版表情在 DST 世界坐标系的 `(cx, cy, dw, dh)` 反推派生框精确锚点，实现 **< 0.5 px 零眼跳**。

### 4.2 关键架构沉淀：`hairpigtails:0..4` 单侧单束 + `hair:2` 一体背发保留

这是长披发角色极易踩中的结构坑（Gemini 实机验证后确认）：
- **DST 引擎真实挂载行为**（实测 `idle_loop` / `shock`）：
  - `front`：**同时挂载 `hairpigtails:1`（右）+ `hairpigtails:0`（左）**；`hair:0` 由引擎放身体前侧（故置空）。
  - `side`：**同时挂载 `hairpigtails:1`（远侧）+ `hairpigtails:2`（近侧）**；`hair:1` 置空。
  - `back`：`headbase:2` → `hair:2`（整片背面居中底帘）→ `hairpigtails:1` + `hairpigtails:0`。
  - `shock`：**同时挂载 `hairpigtails:4` + `hairpigtails:3`** 分列左/右，`hair:3` 置空。
- **v2.1 标准最佳实践（推荐从模板与 Prompt 一步到位）**：
  1. Sheet 1 直接按**单侧单束**出图：`hairpigtails:0` 画成向左下微弧的**左单侧发束**（`w < h`），`hairpigtails:1` 画成水平对称的**右单侧发束**（`w < h`），`hair:2` 画成完整无分叉的**整片背面大波浪披发**（对应 `hair_hat:2`，顶部 `22%` smoothstep 羽化融进 `headbase:2`）；
  2. `derive` 中让 `hairpigtails:2` 直接复用 `hairpigtails:0`（作为侧身近侧垂发），或用 `_derive_xray_hair_lock`（`#000000` 单发锁剪影 + `ImageFilter.MinFilter(size=13)` 内腐蚀遮罩绘 3 条 `#FFFFFF` 射线）派生/校验单侧 X-ray `hairpigtails:3, 4`；
  3. `pack_skin` 对 `hairpigtails:0, 1, 2` 统一做顶部 `24%` smoothstep 羽化融进头壳。
- **历史兜底（Gemini 实战踩坑记录）**：若外部生图意外把 `hairpigtails:0` 画成左右对称双束发帘，无需重新生图，可在 `derive` 中从中心纯绿缝隙（`mid_x = w // 2`）切为左/右两束独立单发锁，分别 `-12°` / `+12°` 旋正输出为 `hairpigtails:0, 2` 与 `hairpigtails:1`，再自动派生单侧 `hairpigtails:3, 4`（参见 `gemini_skin_pipeline.py`）。

### 4.3 其余程序化派生与 100% 像素直通

- **AI 镜像直通件**：
  - `cheeks:1`：`ImageOps.mirror(cheeks:0)` —— **严禁省略 `cheeks` 后改用 Pillow 代码硬画**！程序绘图的竖直截断切口、过饱和粉红同心圆与粗线条会在 `eat` / `dart` 动画中与 AI `headbase:0` 严重割裂（见 §5 第 9 条）；务必由 AI 随 Sheet 1 第 8 格（或 Sheet 5 空位）直接画出 `cheeks:0` 再镜像得 `cheeks:1`。
- **Pillow 4× 矢量/变形件**：
  - `skirt:3`：`skirt:0` 的 2× 预乘 Alpha（`RGBa`）扇形上扬变形至 `~2.2:1` 宽月牙摆，花纹连贯且侧跑零穿腿；
  - `hand:15`：4× 牛皮瓦楞纸板 + **左侧锯齿**白粉笔涂鸦骨锯 + 顶部角色小手捏折；
  - `foot:2`：`foot:0` 严密水平镜像；
  - `torso_pelvis:1, 2, 4, 5, 7, 8`：由 `0/3/6` 窄腰梯形按对应偏转角旋转派生。
- **零计算直通映射**：
  - `arm_lower:6 ≡ arm_lower:2`、`hand:14 ≡ hand:4`、`hand:16 ≡ hand:8`、`hand:17 ≡ hand:13`、`leg:5 ≡ leg:2`
  - `face:8 ≡ face:0`、`face:24 ≡ face:16`、`face:1,3 ≡ 0`、`face:5,7 ≡ 4`、`face:32 ≡ 25`、`face:14` 保留原版柔和白光眼
  - `headbase_hat:0..3`、`*_wide` 3 组、`tail:1..5, 7..11`（透明占位）全部全自动派生。

---

## 五、实战避坑与自动后处理清单（9 条必查）

1. **无暗栏标签文本清洗**：部分网页版生图（如 `sheet1`）会把 `headbase:0` 黑字直接写在绿幕上方。切勿盲目写死像素高度裁剪；应先逐列扫描非绿文字 Y 区间，确认下方存在与呆毛/发丝隔离的**纯绿安全带**后再贴 `(20, 20, 20, 255)` 实色暗底栏，零伤发丝。
2. **半透明腮红绿晕反解（`_unmix_face_blush`）**：面部贴纸上的柔光腮红与 `#00FF00` 混叠后会产生橄榄绿灰边。用 Porter-Duff 反解恢复粉红与衰减透明度，**必须加入“封闭眼眶内上下墨线区间保护（`has_ink_above and has_ink_below`）+ 高饱和金瞳比值保护（`b/r < 0.56`）”**，防止误把金色虹膜底部亮黄反光染成粉色。
3. **眼内星光封闭透明针孔补白（`_fill_enclosed_eye_pinholes`）**：眼眶高光边缘的极亮黄白星点易被绿幕色键当成内部绿孔抠透。从画布四边 BFS 标记所有外部透明连通域后，对内部未连通且面积 `<= 16 px`、周围平均亮度 `>= 0.58` 的微型封闭孔洞统一补回暖白 `(255, 252, 245, 255)`，根治派生表情眼底冒绿点。
4. **`arm_lower:2` 袖口方向核查**：DST 横向前臂 `arm_lower:2` 的手腕袖口必须在**左侧**（手臂右侧接肘）。AI 常把喇叭袖口画在右侧；一旦实机帧 `book`、`notes_loop`、`eat`、`emoteXL_bonesaw` 中看到宽袖口朝向胳膊肘，在切片读取阶段对 `("arm_lower", 2)` 做一次 `ImageOps.mirror` 即可（`arm_lower:6` 自动同步）。
5. **Pillow RGBA 半透明绘图大坑**：严禁直接对 RGBA 底图用 `ImageDraw.Draw(img)` 绘制 `alpha < 255` 笔触（`ImageDraw` 默认直接覆盖 alpha 通道会导致贴图漏出绿底色斑）；内部阴影/腮红渐变必须先与底色**预混为不透明 `a=255` 实色**，或单独建透明图层再 `Image.alpha_composite`。
6. **`offsets.json` 与 `rig_data.js` 幂等机制**：`skin_tuner.html` 生成 `tuner_data/rig_data.js` 时会先把当前 `offsets.json` 从 `build.bin` 坐标中反减回去保存基准坐标（配合 `initialOffsets`），因此在做一致性校验时应核算 `(rig_data.js + initialOffsets) == build.bin`。
7. **`hairpigtails:0..4` 必须单侧单束（防正/侧面双倍重影）**：DST `front` 同时挂载 `hairpigtails:1 + 0`、`side` 同时挂载 `1 + 2`、`shock` 同时挂载 `4 + 3`、`back` 为 `headbase:2 → hair:2 → 1 + 0`。`hairpigtails:0/1/2/3/4` **必须严格为单侧窄长发锁（`w < h`）**，整片背面大披发仅由 `hair:2` / `hair_hat:2` 承担；切忌把 `0/1` 画成两侧对称双帘，否则正侧面会叠加出 4 束乱发。
8. **`tail:0` / `tail:6` 必须横向绘制并按槽校准旋转（防垂穿鞋底/地面线）**：DST `tail` 骨骼在 `idle_loop` 与 `shock` 中自带约 `-34°` 逆时针屏幕旋转（`m ≈ [0.857, -0.592, 0.568, 0.821]`，`back` 水平镜像 `det ≈ -1.04`）。若原图画成竖直 J 钩（`h > w`），上机后会直插鞋底下方。**`tail:0` 与 `tail:6` 在 AI 母表中必须画成横向舒展（`w > h`，粗尾根在正右 3 点钟、向左上 9–10 点钟微翘，根→尖角约 `-165° ~ -170°`）**；打包 `_pack_cat_tail` 若需代码微调角度，`base_fit` 必须按旋转后 `max(pre_rot.width, pre_rot.height)` 归一化，`tail:6` 须独立校准旋转角且不盲目继承 `tail:0` 的 `offsets.json` 临时偏移，并在单测强制断言 `t0_w > t0_h` 与 `t6_w > t6_h`。
9. **`cheeks:0/1` 必须随初表 AI 绘制（严禁漏进模板后改由代码硬画）**：Gemini 初版 6 表未包含 `cheeks:0/1`，改用 Pillow `4×` 合成了内侧竖直平切、腮红过饱和且粗描边的半圆片，导致 `eat` / `dart` 动画极不自然并被迫二次返工。**正确做法：将 `cheeks:0` 直接放进 Sheet 1 第 8 格（或 `cheeks:0, 1` 放 Sheet 5 空位）随初表同风生成**，保持饱满圆润（内侧不平切）、90%+ 匹配 `headbase:0` 肤色 + 极柔和渐变腮红 + 仅外侧（上/左/下）C 弧细线；若需后续局部 `rework`，务必把已定稿的 `headbase_0.png` 作为 `IMAGE 2` 喂给 AI 锁色与线宽，并通过 `_find_rework_ai`（同时兼容 `.png` / `.jpg`）自动覆盖。

---

## 六、6 张母表通用英文 Prompt 骨架模板

使用时先在文本中替换下列占位符：
- `[CHARACTER_NAME]`：角色名
- `[HEAD_FEATURES]`：耳朵/头饰/呆毛/发箍描述
- `[HAIR_GRADIENT]`：发色与渐变描述
- `[SKIN_HEX]` / `[OUTLINE_HEX]` / `[LINE_SKIN_HEX]`：肤色与线稿色板
- `[LEFT_EYE_DESC]` / `[RIGHT_EYE_DESC]` / `[SIDE_EYE_DESC]`：异色瞳/同色瞳及侧面眼描述
- `[OUTFIT_TOP_RULES]` / `[OUTFIT_SKIRT_RULES]` / `[LIMB_RULES]`：上衣、裙摆、袖套、袜鞋规则

### 6.1 Sheet 1 — `sheet1_head_hair_tail`（`4×2` 满格，配 `<char>_ref_sd.png`）

```text
Task: Redraw all 8 sprite cells in IMAGE 1's 4x2 DST template grid into the chibi character "[CHARACTER_NAME]" from IMAGE 2, strictly matching the SD Q-chibi proportions and official color palette.

STRICT LAYOUT & CHROMA-KEY RULES:
- Preserve the exact 4x2 grid (top row: headbase:0, headbase:1, headbase:2, tail:0; bottom row: hairpigtails:0, hairpigtails:1, hair:2, cheeks:0). Keep black borders, dark top header strips with white labels, and PURE SOLID #00FF00 neon green background inside every cell.
- Smooth uniform [OUTLINE_HEX] anime cel-shaded outlines, zero drop shadows, zero glow, zero gradients on #00FF00, zero pixel-art jaggedness.
- Leave 12-18% clean #00FF00 green padding around every sprite—NO part may touch borders.

CHARACTER HAIR, SKIN & TAIL PALETTE ([CHARACTER_NAME]):
- Hair & tail colors: [HAIR_GRADIENT].
- Signature head features: [HEAD_FEATURES].
- Face & cheek skin tone: [SKIN_HEX] with thin warm skin lineart [LINE_SKIN_HEX].

PER-CELL RULES:
1. headbase:0 (front), headbase:1 (side), headbase:2 (back):
   - Rounded Q-chibi BLANK head shell ONLY (NO eyes, NO brows, NO mouth; very soft natural cheek blush matching IMAGE 2 if applicable). Matte anime cel-shading matching IMAGE 2 (no harsh white halo ring on back hair).
   - FLOATING SHORT-BOB-LENGTH HEADS ONLY (long flowing hair goes separately in the hair cells below!).
   - headbase:0 (front): 100% pure #00FF00 green space IMMEDIATELY underneath the curved chin outline—ZERO back-hair fill behind neck/chin.
   - headbase:1 (side): STRICTLY FACING RIGHT (chin/nose point RIGHT; headpiece/nape sit on the LEFT).
   - headbase:2 (back): Full matte cel-shaded back head with signature accessories and clean tapered bob tips.
2. tail:0 (HORIZONTAL standalone tail — omit or place cheeks:1 here if character has no tail):
   - Isolated HORIZONTAL sweeping tail (STRICTLY WIDER THAN TALL, w > h, matching IMAGE 1's horizontal orientation).
   - Thick base/root anchored at MIDDLE-RIGHT (3 o'clock), sweeping gently DOWN-LEFT in a shallow curve with the fluffy tip curling UP-LEFT (9–10 o'clock).
   - STRICTLY NOT a tall vertical J-hook (so DST's -34deg tail bone places it gracefully behind the waist instead of under the shoes). NO skirt, NO legs, NO body.
3. hairpigtails:0 (SINGLE LEFT LOCK), hairpigtails:1 (SINGLE RIGHT LOCK), hair:2 (UNIFIED BACK CURTAIN):
   - ISOLATED voluminous wavy long hair ONLY (NO head, NO ears, NO torso) with soft un-outlined top roots so they tuck cleanly under the head.
   - hairpigtails:0: STRICTLY ONE SINGLE LEFT-SIDE hanging wavy hair lock ONLY (narrower than tall, w < h, sweeping slightly outward to bottom-left). STRICTLY ONE SIDE ONLY—DO NOT draw both left and right curtains in this cell!
   - hairpigtails:1: STRICTLY ONE SINGLE RIGHT-SIDE hanging wavy hair lock ONLY (narrower than tall, w < h, mirror of hairpigtails:0 sweeping slightly outward to bottom-right). STRICTLY ONE SIDE ONLY!
   - hair:2: single continuous unified wide fluffy wavy back hair curtain matching IMAGE 1's wide shape (ONLY for back view—NOT split into twin pigtails).
4. cheeks:0 (left puffed eating cheek — bottom-right cell):
   - ONLY ONE plump, full circular hamster-like puffed cheek pouch for eating/blowdart animations. STRICTLY NO eyes, NO nose, NO mouth, NO hair, NO full head.
   - COLOR MATCH TO headbase:0: Over 90% MUST be exact pure skin [SKIN_HEX] with ONLY a tiny ultra-soft smoothly diffused blush glow in the center—NO bubblegum pink, NO hard concentric rings.
   - FULL ROUND SILHOUETTE + OPEN-C LINEART (NOT flat-chopped): thin crisp [LINE_SKIN_HEX] outline matching headbase:0's chin stroke weight along TOP, LEFT, and BOTTOM outer edge, while the RIGHT (mouth-facing) edge stays cleanly open/unlined.
```

### 6.2 Sheet 2 — `sheet2_faces_core`（`4×3`，配 `<char>_ref_expressions.png`）

```text
Task: Redraw all 12 labeled facial expression cells in IMAGE 1's 4x3 DST template grid into the anime chibi expressions of "[CHARACTER_NAME]" from IMAGE 2.

STRICT FLOATING-DECAL & CHROMA-KEY RULES:
- Preserve exact 4x3 grid (row 1: face:0, face:2, face:4, face:6; row 2: face:11, face:12, face:13, face:15; row 3: face:21, face:23, face:25, face:31), black borders, dark header bars with white labels, and PURE SOLID #00FF00 neon green background.
- ZERO SKIN PIXELS: Every cell MUST be transparent floating facial decals on #00FF00 green—ZERO skin fill, ZERO face oval, ZERO chin outline, ZERO bangs, NO dark grey eye bags, NO pixel-art jaggedness.
- CRITICAL EYE PALETTE RULE (ALL front open-eye cells):
  * Viewer's LEFT eye (character's right eye) = [LEFT_EYE_DESC].
  * Viewer's RIGHT eye (character's left eye) = [RIGHT_EYE_DESC].

PER-CELL SPECIFICATIONS:
- face:0 (row1 col1): Front default bright cheerful smile.
- face:2 (row1 col2): Front COMPLETELY CLOSED eyes (smooth downward-curved lashes, tiny mouth; ZERO pupils).
- face:4 (row1 col3): Side-view default open eye STRICTLY FACING RIGHT (ONLY ONE eye: [SIDE_EYE_DESC], one brow, tiny profile tick nose & small smile on the right—NO second eye).
- face:6 (row1 col4): Side-view COMPLETELY CLOSED SINGLE EYE STRICTLY FACING RIGHT (ONLY ONE smooth closed lash curve + delicate single-line anime profile nose & tiny mouth—NO second eye, NO thick triangular nose).
- face:11 (row2 col1): Front peaceful sleeping closed eyes.
- face:12 (row2 col2): Front flustered/panicked wide eyes with shrunk pupils glancing slightly DOWN-RIGHT, worried angled brows, tiny wavy anxious mouth.
- face:13 (row2 col3): Front dizzy / KO X_X eyes + small wavy mouth.
- face:15 (row2 col4): Front ecstatic laughing ^_^ closed smiling eyes + wide happy open laugh mouth.
- face:21 (row3 col1): Front grumpy/angry narrowed eyes + angled brows + fierce small frown mouth (NO dark eye circles).
- face:23 (row3 col2): Front big sleepy yawn (tightly closed dry eyes with ZERO tears + wide oval yawning mouth).
- face:25 (row3 col3): Front frustrated shouting (tightly squeezed >_< closed eyes with ZERO tears + wide open shouting mouth).
- face:31 (row3 col4): Front stunned / blank dazed stare (shrunk highlight-less dull irises, flat brows, tiny flat mouth—ZERO grey under-eye circles).
```

### 6.3 Sheet 3 — `sheet3_body_skirt`（`5×4`，配 `<char>_ref_normal.png`）

```text
Task: Redraw every populated sprite cell in IMAGE 1's 5x4 DST body/skirt template grid (keep the 3 solid black empty cells untouched!) into the outfit of "[CHARACTER_NAME]" from IMAGE 2.

STRICT LAYOUT & CHROMA-KEY RULES:
- Preserve exact 5x4 grid, black borders, dark label headers, solid black empty cells, and pure solid #00FF00 green background. Smooth [OUTLINE_HEX] anime cel-shaded linework.
- Every torso cell contains ONLY the isolated SLEEVELESS bodice (NO head, NO arms/sleeves, NO hands, NO legs, NO tail).

OUTFIT & ALIGNMENT RULES:
- [OUTFIT_TOP_RULES]
- torso:0 (front), torso:6 (back), torso:1, 2, 4, 5, 7, 8, 9: Isolated SLEEVELESS bodice strictly matching the lean/twist/top-down angle of each vanilla cell in IMAGE 1.
- torso:3 (SLIM SIDE BODICE — FACING RIGHT): Narrow upright right-facing sleeveless bodice matching IMAGE 1's slim silhouette ONLY (front details on RIGHT, clean fitted back on LEFT). NO sleeves, NO bulging bow, NO skirt flare.
- torso_pelvis:0, 3, 6, 9: Compact narrow waist/hip connector matching skirt waist width; torso_pelvis:3 MUST be a slim narrow solid-toned trapezoid with crisp outline so it tucks cleanly inside skirt:0 and skirt:3.
- [OUTFIT_SKIRT_RULES]
- skirt:0 (front/side stand), skirt:1 (back stand, STRICTLY NO TAIL), skirt:2 (running flared skirt sweeping toward top-right, NO tail).
```

### 6.4 Sheet 4 — `sheet4_limbs`（`5×5`，配 `<char>_ref_normal.png`）

```text
Task: Redraw all 25 limb sprite cells in IMAGE 1's 5x5 DST template grid into the sleeves, legs/socks, and shoes of "[CHARACTER_NAME]" from IMAGE 2.

STRICT LAYOUT & CHROMA-KEY RULES:
- Preserve exact 5x5 grid, black borders, dark label headers, and pure #00FF00 green background.
- Match the EXACT length, thickness, joint angle, and left/right orientation of every vanilla piece in IMAGE 1. ZERO X-ray bones on this sheet—ALL 25 cells are normal colored limbs!

LIMB DESIGN:
- [LIMB_RULES]
- CRITICAL ORIENTATION CHECK: For arm_lower:2 (horizontal forearm), the wrist cuff MUST sit strictly at the LEFT end (elbow at RIGHT).
```

### 6.5 Sheet 5 — `sheet5_hands`（`4×4`，配 `<char>_ref_sd.png`）

```text
Task: Redraw all 14 populated hand sprite cells in IMAGE 1's 4x4 DST template grid (keep the 2 solid black empty cells untouched!) into the cute anime chibi hands of "[CHARACTER_NAME]" from IMAGE 2.

STRICT LAYOUT & CHROMA-KEY RULES:
- Preserve exact 4x4 grid, black borders, dark headers with white labels, solid black empty cells, and pure solid #00FF00 green background.
- Fair skin [SKIN_HEX] with delicate [LINE_SKIN_HEX] outlines and subtle fingertip/knuckle shading.
- CRITICAL WRIST RULE: Draw ONLY the isolated hand ending cleanly at the wrist joint matching IMAGE 1—ZERO sleeves, ZERO cuffs, ZERO extra forearm/wrist stumps!
- Match the EXACT hand pose, rounded chibi fingers (4-5 fingers, never claws), and wrist attachment angle of every cell in IMAGE 1:
  * hand:3 MUST be a PALM-FACING clenched fist (thumb tucked across curled fingertips facing viewer—NOT back of hand).
  * hand:13: wide open friendly waving palm (thumb on LEFT, 4 fingers spread naturally, clean short wrist base).
  * hand:18 & hand:19: top-down grasping/petting fingertips matching IMAGE 1 cleanly.
```

### 6.6 Sheet 6 — `sheet6_xray`（`5×4`，配 `<char>_ref_sd.png`）

```text
Task: Redraw all 17 populated X-ray skeleton sprite cells in IMAGE 1's 5x4 DST template grid (keep the 3 solid black empty cells untouched!) for "[CHARACTER_NAME]" from IMAGE 2.

STRICT X-RAY STYLE & CHROMA-KEY RULES:
- Preserve exact 5x4 grid, black borders, dark headers with white labels, solid black empty cells, and pure solid #00FF00 green background.
- STRICT COLOR LIMIT: ONLY PURE SOLID BLACK (#000000) silhouette filled with crisp PURE WHITE (#FFFFFF) cartoon skeleton bones/X-ray lines STRICTLY INSIDE the silhouette.
- ZERO colored pixels, ZERO grey gradients, ZERO external VFX, ZERO lightning/sparks/aura outside the black silhouette on #00FF00.

PER-CELL SILHOUETTES:
- headbase:3: [CHARACTER_NAME]'s exact chibi head silhouette (keeping ears/accessories/ahoge) in #000000 black + #FFFFFF skull/eye sockets/teeth inside.
- hairpigtails:3 & hairpigtails:4: SINGLE-SIDED narrow curved #000000 hair lock silhouettes matching IMAGE 1 (`hairpigtails:3` = LEFT side lock ONLY, `hairpigtails:4` = RIGHT side lock ONLY; w < h) + #FFFFFF curved X-ray highlight streaks strictly inside (NOT double-sided curtains or a full wig).
- tail:6: HORIZONTAL #000000 tail silhouette (STRICTLY WIDER THAN TALL, w > h, root anchored at MIDDLE-RIGHT 3 o'clock, shallow curve dipping down-left, tip curling UP-LEFT 9–10 o'clock—STRICTLY NOT vertical) + #FFFFFF segmented tail vertebrae strictly inside.
- torso:10, torso_pelvis:10, skirt:4: #000000 silhouettes + #FFFFFF ribcage/spine, hip bone, and vertical skirt X-ray stripes.
- arm_upper:5, 6, arm_upper_skin:2, arm_lower:3, 4, hand:11, 12, leg:9, 10, foot:7: #000000 limb/hand/shoe silhouettes + corresponding #FFFFFF bones strictly inside.
```

---

## 七、标准命令行与最终验收门禁

```powershell
# 1. 分拆官图并生成 6 张纯净绿幕母表（skins/<char>/work）
.\.venv-mcp\Scripts\python.exe python\tools\<char>_skin_pipeline.py split-ref
.\.venv-mcp\Scripts\python.exe python\tools\<char>_skin_pipeline.py extract

# 2. 将 6 张 AI 成图保存到 skins/<char>/work/sheet{1..6}_ai.png（如为 .jpg 先核图去无暗栏黑字后转 PNG）

# 3. 执行 Pillow 4x 程序化派生（生成 rework_derived_ai.png + derived_sprites/*.png）
.\.venv-mcp\Scripts\python.exe python\tools\<char>_skin_pipeline.py derive

# 4. 取消 modmain.lua 注释后，一键打包 + 生成审核对比与交互调参器
.\.venv-mcp\Scripts\python.exe python\tools\<char>_skin_pipeline.py refresh --suite compact

# 5. 回归与独立单测（PowerShell 5.1 语法）
.\.venv-mcp\Scripts\python.exe -m unittest discover -s python\tests -t python -p test_<char>_skin_pipeline.py; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; .\.venv-mcp\Scripts\python.exe -m unittest discover -s python\tests -t python -p test_skin_pipeline.py
```

**上线前 5 项硬门禁**：
1. **覆盖率与缺帧**：`skins/<char>/review_data/review_report.md` 显示 `130/130 (100.0%)` 且 `Frames with missing symbols: 0`。
2. **肉眼关键动作与核心构型全过**：核查 `review_sheet_01..06.png` 中的 `idle_loop`（正/侧/背）、`run_loop (side)`、`eat`、`dart`、`book`、`notes_loop`、`shock`、`emoteXL_bonesaw`、`emote_sad`、`emote_impatient`、`emoteXL_angry`——确认 `hairpigtails:0/1` 为单侧窄长发束（`w < h`）而 `hair:2` 为宽背面披发、`tail:0/6` 为横向舒展（`w > h` 且高于地面线）、`cheeks:0/1` 与脸颊肤色柔顺融为一体。
3. **Tuner 一致性**：`tuner_data/rig_data.js`（叠加 `initialOffsets`）对比 `build.bin` 达到 **`mismatches=0`**。
4. **单元测试全绿**：新皮肤独立测试 + 既有 `test_skin_pipeline.py` 全部通过（含 `t0_w > t0_h`、`t6_w > t6_h`、`hp0_w < hp0_h` 与锁眼误差 `< 0.5 px` 断言）。
5. **代码零耦合**：`git diff` 中前序皮肤脚本与资产目录均未受牵连修改。
