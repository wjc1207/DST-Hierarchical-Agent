# Gemini 皮肤生成与迭代档案（独立 6 表工作流）

> **角色版权与官方参考**：设定图 [gemini-1.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/assets/gemini-1.png)（`3200×2100`，二创人设：ZipZipPipe）与立绘 [gemini-2.jpg](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/assets/gemini-2.jpg)。
>
> **分拆高清参考图**（由 `gemini_skin_pipeline.py split-ref` 生成）：
> - [gemini_ref_sd.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/reference/gemini_ref_sd.png)：SD Q 版正/侧/背三视图（用于 Sheet 1 / 5 / 6）
> - [gemini_ref_normal.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/reference/gemini_ref_normal.png)：正常比例三视图（用于 Sheet 3 / 4）
> - [gemini_ref_expressions.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/reference/gemini_ref_expressions.png)：9 连表情栏（用于 Sheet 2）
> - [gemini_icon.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/reference/gemini_icon.png)：`SWAP_ICON` 头像图标
>
> **独立打包脚本**：[gemini_skin_pipeline.py](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/python/tools/gemini_skin_pipeline.py)（与 DeepSeek 的 `skin_pipeline.py` 完全解耦，互不修改）。

---

## 一、官方核定色板与造型规范

- **发色渐变**：星空蓝 `#6C78D9`（发根/猫耳外侧/猫尾上段）→ 渐变紫 `#9A72E2`（发中/猫尾中段）→ 发梢粉 `#D89CF1`（发梢/猫尾尖/猫耳内侧）
- **服饰与线稿**：深夜蓝 `#29254E`（露肩露腰短上衣、星夜不对称短裙、宽大袖套、厚底鞋、赛璐璐闭口线稿）+ 纯白荷叶边/内衬/泡泡袜 `#F8F8FF` + 金色镶边 + 蓝紫白渐变四角星（Gemini 标志星）
- **异色瞳**：
  - 正面（`face:0, 12, 21, 31` 等）：**画面左侧眼（角色右眼）= 玫红 `#C34A93`**；**画面右侧眼（角色左眼）= 琥珀金 `#D9A84A`**
  - 侧面（`face:4` 朝右单眼）：**琥珀金 `#D9A84A`**（与官图侧面三视图一致）
- **皮肤色**：`#FCEEE7`（`(252, 238, 231)`），柔和暗紫棕线稿 `#4A3348`
- **简化规则**：双腿不画不对称大腿环（避免左右复用双脚出现双腿环），仅保留光腿 + 白色泡泡袜 + 四角星袜饰；`tail:0, 6` 独立走尾巴骨骼槽，不在 `skirt:1/2` 画静态尾巴；长披发统一走混合长发架构（`hairpigtails:0..4` + `hair:2` / `hair_hat:2`，不画双马尾）。

---

## 二、6 张 AI 母表拆分（合计 88 格）+ Pillow 程序化派生（25 格）

| 母表 | 网格（有效格） | 槽位清单 | 搭配参考图 |
| :--- | :---: | :--- | :--- |
| `sheet1_head_hair_tail` | `4×2`（7 格） | `headbase:0, 1, 2`、`tail:0`、`hairpigtails:0, 1, 2` | `gemini_ref_sd.png` |
| `sheet2_faces_core` | `4×3`（12 格） | `face:0, 2, 4, 6, 11, 12, 13, 15, 21, 23, 25, 31` | `gemini_ref_expressions.png` |
| `sheet3_body_skirt` | `5×4`（17 格） | `torso:0..9`、`torso_pelvis:0, 3, 6, 9`、`skirt:0, 1, 2` | `gemini_ref_normal.png` |
| `sheet4_limbs` | `5×5`（25 格） | `arm_upper:0..4`、`arm_upper_skin:0, 1`、`arm_lower:0, 1, 2, 5`、`leg:0..4, 6..8`、`foot:0, 1, 3, 4, 5, 6` | `gemini_ref_normal.png` |
| `sheet5_hands` | `4×4`（14 格） | `hand:0..10, 13, 18, 19` | `gemini_ref_sd.png` |
| `sheet6_xray` | `5×4`（17 格） | `headbase:3`、`hairpigtails:3, 4`、`tail:6`、`torso:10`、`torso_pelvis:10`、`skirt:4`、`arm_upper:5, 6`、`arm_upper_skin:2`、`arm_lower:3, 4`、`hand:11, 12`、`leg:9, 10`、`foot:7` | `gemini_ref_sd.png` |

- **Pillow 4× 程序化派生（`gemini_skin_pipeline.py derive`，零色偏零闪烁）**：
  - 锁眼表情 13 格：`face:9, 10, 16, 19, 20, 22, 28, 30`（锁 `face:0`）、`face:17, 18`（锁 `face:11`）、`face:26, 27, 29`（锁 `face:25`）
  - 矢量与变形 12 格：`skirt:3`（`skirt:0` 2× 扇形展开）、`cheeks:0, 1`（开放 C 形）、`hand:15`（纸板涂鸦骨锯）、`foot:2`（`foot:0` 镜像）、`torso_pelvis:1, 2, 4, 5, 7, 8`（窄腰倾角）
- **像素直通复用（0 生图）**：`hair:2 ≡ hair_hat:2 ≡ hairpigtails:2`、`arm_lower:6 ≡ 2`、`hand:14 ≡ 4`、`hand:16 ≡ 8`、`hand:17 ≡ 13`、`leg:5 ≡ 2`、`face:8 ≡ 0`、`face:24 ≡ 16`、`face:1,3 ≡ 0`、`face:5,7 ≡ 4`、`face:32 ≡ 25`、`face:14` 保留原版微光眼。

---

## 三、标准工具链命令

```powershell
.\.venv-mcp\Scripts\python.exe python\tools\gemini_skin_pipeline.py split-ref
.\.venv-mcp\Scripts\python.exe python\tools\gemini_skin_pipeline.py extract
# 生成 sheet1_ai.png .. sheet6_ai.png 后：
.\.venv-mcp\Scripts\python.exe python\tools\gemini_skin_pipeline.py derive
.\.venv-mcp\Scripts\python.exe python\tools\gemini_skin_pipeline.py refresh --suite compact
```

---

## 四、6 张母表完整生图提示词（可直接复制使用）

> 所有母表均严格采用**双图输入**（`IMAGE 1` = 绿幕模板，`IMAGE 2` = 官方分拆参考图），生成后保存为 `skins/gemini/work/sheet1_ai.png` .. `sheet6_ai.png`。

### 4.1 Sheet 1 — `sheet1_head_hair_tail`（`4×2`，7 格）

- `IMAGE 1`: [sheet1_head_hair_tail.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/work/sheet1_head_hair_tail.png)
- `IMAGE 2`: [gemini_ref_sd.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/reference/gemini_ref_sd.png)
- 保存路径：`skins/gemini/work/sheet1_ai.png`

```text
Task: Redraw every populated sprite cell in IMAGE 1's 4x2 DST template grid (keep the solid black bottom-right empty cell untouched!) into the chibi character "Gemini" from IMAGE 2, strictly matching the SD Q-chibi proportions and official starry-blue to purple to pastel-pink gradient hair palette.

STRICT LAYOUT & CHROMA-KEY RULES:
- Preserve the exact 4x2 grid (4 columns x 2 rows: top row headbase:0, headbase:1, headbase:2, tail:0; bottom row hairpigtails:0, hairpigtails:1, hairpigtails:2, plus the solid black empty cell at bottom-right). Keep black borders, dark top header strips with white labels, and PURE SOLID #00FF00 neon green background inside every populated cell.
- Smooth uniform dark midnight-blue (#29254E) anime cel-shaded outlines, zero drop shadows, zero glow, zero gradients on #00FF00, zero pixel-art jaggedness.
- Leave 12-18% clean #00FF00 green padding around every sprite—NO part may touch borders.

CHARACTER HAIR & TAIL PALETTE (Gemini from IMAGE 2):
- Hair & fluffy cat tail gradient: starry periwinkle blue (#6C78D9) at roots/top -> luminous lavender-purple (#9A72E2) in mid-lengths -> soft pastel pink-lilac (#D89CF1) at wavy tips.
- Signature head features: fluffy cat ears (starry blue outer, soft pastel pink-white inner fur), single curved ahoge on top, small twin side hair buns, and a crisp blue-purple-white four-pointed Gemini star ornament tucked below the viewer's-right cat ear; thin dark navy band across back crown. Fair warm peach skin (#FCEEE7).

PER-CELL RULES:
1. headbase:0 (front), headbase:1 (side), headbase:2 (back):
   - Rounded Q-chibi BLANK head shell ONLY (NO eyes, NO brows, NO mouth, NO blush). Matte anime cel-shading matching IMAGE 2 (no harsh white halo ring on back hair).
   - FLOATING SHORT-BOB-LENGTH HEADS ONLY (long flowing hair goes separately in hairpigtails cells below!).
   - headbase:0 (front): 100% pure #00FF00 green space IMMEDIATELY underneath the curved chin outline—ZERO back-hair fill behind neck/chin.
   - headbase:1 (side): STRICTLY FACING RIGHT (chin/nose point RIGHT; cat ears, bun, star ornament, and nape taper sit on the LEFT).
   - headbase:2 (back): Full matte cel-shaded back head with cat ears, twin buns, thin dark navy crown band, and clean tapered bob tips.
2. tail:0 (standalone fluffy cat tail):
   - Isolated fluffy blue -> purple -> pastel-pink cat tail ONLY matching IMAGE 1's silhouette orientation (thick base anchored at TOP-RIGHT curving gracefully down to BOTTOM-LEFT with upturned fluffy pink-lilac tip). NO skirt, NO legs, NO body.
3. hairpigtails:0 (front hanging hair), hairpigtails:1 (side hanging hair), hairpigtails:2 (back unified hair):
   - ISOLATED voluminous wavy starry-blue -> purple -> pastel-pink long hair curtains ONLY (NO head, NO ears, NO torso). Soft un-outlined top roots so they tuck cleanly under the head.
   - hairpigtails:0: single wide symmetrical wavy back-hair curtain (NOT twin pigtails!).
   - hairpigtails:1: right-facing side flowing wavy hair curtain with roots at TOP-RIGHT streaming down-left to BOTTOM-LEFT.
   - hairpigtails:2: single continuous unified fluffy wavy back hair curtain matching IMAGE 1's wide shape (STRICTLY NOT split into two pigtails).
```

### 4.2 Sheet 2 — `sheet2_faces_core`（`4×3`，12 格）

- `IMAGE 1`: [sheet2_faces_core.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/work/sheet2_faces_core.png)
- `IMAGE 2`: [gemini_ref_expressions.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/reference/gemini_ref_expressions.png)
- 保存路径：`skins/gemini/work/sheet2_ai.png`

```text
Task: Redraw all 12 labeled facial expression cells in IMAGE 1's 4x3 DST template grid into the anime chibi expressions of "Gemini" from IMAGE 2.

STRICT FLOATING-DECAL & CHROMA-KEY RULES:
- Preserve exact 4x3 grid (row 1: face:0, face:2, face:4, face:6; row 2: face:11, face:12, face:13, face:15; row 3: face:21, face:23, face:25, face:31), black borders, dark header bars with white labels, and PURE SOLID #00FF00 neon green background.
- ZERO SKIN PIXELS: Every cell MUST have 100% pure solid #00FF00 green between and around eyes, brows, blush, and mouth—ZERO peach skin fill, ZERO face oval, ZERO chin outline, ZERO hair bangs, NO dark grey eye bags, NO pixel-art jaggedness. Clean anime cel-shaded linework matching IMAGE 2!
- CRITICAL HETEROCHROMIA RULE (ALL front open-eye cells):
  * Viewer's LEFT eye (character's right eye) = vibrant magenta-pink gradient iris (#C34A93) with crisp white anime highlights & starry reflection.
  * Viewer's RIGHT eye (character's left eye) = luminous amber-gold gradient iris (#D9A84A) with crisp white anime highlights & starry reflection.
  * Thin periwinkle-navy eyebrows (#4A4A8A), tiny delicate anime dot/tick nose, subtle soft light-pink cheek blush strokes only (no opaque skin patch).

PER-CELL SPECIFICATIONS:
- face:0 (row1 col1): Front default cheerful smile (wide sparkling magenta-left / amber-gold-right eyes, small gentle cat-like smile).
- face:2 (row1 col2): Front COMPLETELY CLOSED eyes (smooth downward-curved dark lashes, slightly concerned brows, tiny flat closed mouth; ZERO pupils).
- face:4 (row1 col3): Side-view default open eye STRICTLY FACING RIGHT (ONLY ONE anime eye with AMBER-GOLD #D9A84A gradient iris, one thin brow, delicate tiny profile tick nose & small smile on the right—NO second eye).
- face:6 (row1 col4): Side-view COMPLETELY CLOSED SINGLE EYE STRICTLY FACING RIGHT (ONLY ONE smooth closed lash curve + delicate single-line anime profile nose & tiny calm mouth—NO second eye, NO thick triangular cartoon nose).
- face:11 (row2 col1): Front peaceful sleeping closed eyes (gentle relaxed downward curves + soft calm smile).
- face:12 (row2 col2): Front flustered/panicked wide eyes with shrunk magenta-left / amber-gold-right pupils glancing slightly DOWN-RIGHT, worried angled brows, tiny wavy anxious mouth.
- face:13 (row2 col3): Front dizzy / KO X_X expression (clean dark anime X_X eyes, worried brows, tiny wavy mouth).
- face:15 (row2 col4): Front ecstatic laughing expression (joyful upward-curved ^_^ closed smiling eyes + wide happy open laugh mouth with cute pink tongue).
- face:21 (row3 col1): Front grumpy/angry expression (sharply angled brows, narrowed intense magenta-left / amber-gold-right eyes, small fierce clenched frown mouth; ZERO dark circles).
- face:23 (row3 col2): Front big sleepy yawn (tightly closed dry curved >_< or U_U eyes with ZERO tears + wide vertical oval yawning mouth).
- face:25 (row3 col3): Front frustrated shouting expression (tightly squeezed >_< closed eyes with ZERO tears + wide open shouting mouth).
- face:31 (row3 col4): Front stunned / blank dazed stare (wide open eyes with shrunk highlight-less dull magenta-left / amber-gold-right pupils, flat blank brows, tiny flat mouth—ZERO grey under-eye circles).
```

### 4.3 Sheet 3 — `sheet3_body_skirt`（`5×4`，17 格）

- `IMAGE 1`: [sheet3_body_skirt.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/work/sheet3_body_skirt.png)
- `IMAGE 2`: [gemini_ref_normal.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/reference/gemini_ref_normal.png)
- 保存路径：`skins/gemini/work/sheet3_ai.png`

```text
Task: Redraw every populated sprite cell in IMAGE 1's 5x4 DST body/skirt template grid (keep the 3 solid black empty cells at bottom-right untouched!) into the starry night outfit of "Gemini" from IMAGE 2.

STRICT LAYOUT & CHROMA-KEY RULES:
- Preserve exact 5x4 grid, black borders, dark label headers, solid black empty cells, and pure solid #00FF00 green background. Smooth #29254E dark navy cel-shaded linework, zero shadows or gradients on green background.
- Every torso cell contains ONLY the isolated SLEEVELESS cropped bodice (NO head, NO arms/detached sleeves, NO hands, NO legs, NO cat tail).

OUTFIT & ALIGNMENT RULES (Gemini from IMAGE 2):
- Bodice palette: deep midnight blue (#29254E) halter/cropped starry bodice with crisp gold trim, small four-pointed blue-purple-white Gemini star clasp at chest/collar, luminous lavender-purple (#9A72E2) diagonal sash ribbon, and small fair peach midriff skin (#FCEEE7) cleanly matching the waist cut from IMAGE 2.
- torso:0 (front), torso:6 (back), torso:1, 2, 4, 5, 7, 8, 9: Isolated SLEEVELESS bodice strictly matching the exact lean/twist/top-down angle of each vanilla cell in IMAGE 1.
- torso:3 (SLIM SIDE BODICE — FACING RIGHT): Narrow upright right-facing sleeveless bodice matching IMAGE 1's slim silhouette ONLY—star clasp front on RIGHT, clean fitted back on LEFT, ending narrowly at the waist. NO sleeves, NO bulging bow, NO skirt flare.
- torso_pelvis:0, 3, 6, 9: Compact narrow midnight-blue waist/hip connector matching skirt waist width; torso_pelvis:3 MUST be a slim narrow solid-toned trapezoid with crisp outline (NO wide bulging sashes) so it tucks cleanly inside skirt:0 and skirt:3.
- skirt:0 (front/side stand): Crisp pleated midnight-blue asymmetrical mini-skirt with gold trim, layered fluffy white ruffled petticoat hem, small centered Gemini star buckle, and compact symmetrical ribbon accents (NO cat tail, NO long dangling tails extending below hem).
- skirt:1 (back stand): Clean back pleated midnight-blue & white ruffled mini-skirt with back ribbon accents (STRICTLY NO CAT TAIL attached—tail is rigged separately in tail:0!).
- skirt:2 (running flared skirt): Flared dynamic midnight-blue & white ruffled skirt sweeping toward top-right for running motion (NO tail).
```

### 4.4 Sheet 4 — `sheet4_limbs`（`5×5`，25 格）

- `IMAGE 1`: [sheet4_limbs.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/work/sheet4_limbs.png)
- `IMAGE 2`: [gemini_ref_normal.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/reference/gemini_ref_normal.png)
- 保存路径：`skins/gemini/work/sheet4_ai.png`

```text
Task: Redraw all 25 limb sprite cells in IMAGE 1's 5x5 DST template grid into the detached wide sleeves, bare legs with white slouch socks, and iridescent platform shoes of "Gemini" from IMAGE 2.

STRICT LAYOUT & CHROMA-KEY RULES:
- Preserve exact 5x5 grid, black borders, dark label headers, and pure #00FF00 green background.
- Match the EXACT length, thickness, joint angle, and left/right orientation of every vanilla piece in IMAGE 1. ZERO X-ray bones on this sheet—ALL 25 cells are normal colored limbs!

LIMB DESIGN (Gemini from IMAGE 2):
- arm_upper:0..4 (upper detached sleeve segments): Small fair-peach exposed shoulder top (#FCEEE7) transitioning into a white ruffled upper arm cuff and deep midnight-blue (#29254E) sleeve with gold trim & lavender-purple accent, matching IMAGE 1's exact pose/thickness.
- arm_upper_skin:0, 1 (alternate upper-arm segments): Match arm_upper's exact midnight-blue sleeve & peach-top palette seamlessly (NO white wrist cuff, NO plain vanilla skin stick).
- arm_lower:0, 1, 2, 5 (forearm wide bell-sleeves): Deep midnight-blue (#29254E) flared sleeves with gold border trim and crisp white inner lining at the wrist end matching IMAGE 1 (arm_lower:2 cuff strictly at the LEFT end).
- leg:0..4, 6..8 (legs + white slouch socks, NO THIGH RING): Clean fair peach (#FCEEE7) smooth anime thighs/knees (strictly NO garter/thigh ring on thigh!) transitioning at mid-calf into cute wrinkled white slouch socks (#F8F8FF) with small blue-purple four-pointed star charm accents. Follow vanilla thigh/sock proportions and knee bends closely.
- foot:0, 1, 3, 4, 5, 6 (platform shoes): Deep midnight-blue (#29254E) Mary-Jane platform shoes with lavender-blue-pink iridescent thick soles (#6C78D9 / #9A72E2 / #D89CF1), white sock collar edge, and dark navy ribbon bow at heel—strictly matching each cell's left/right/front/tiptoe/sole-up direction in IMAGE 1.
```

### 4.5 Sheet 5 — `sheet5_hands`（`4×4`，14 格）

- `IMAGE 1`: [sheet5_hands.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/work/sheet5_hands.png)
- `IMAGE 2`: [gemini_ref_sd.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/reference/gemini_ref_sd.png)
- 保存路径：`skins/gemini/work/sheet5_ai.png`

```text
Task: Redraw all 14 populated hand sprite cells in IMAGE 1's 4x4 DST template grid (keep the 2 solid black empty cells at bottom-right untouched!) into the cute anime chibi hands of "Gemini" from IMAGE 2.

STRICT LAYOUT & CHROMA-KEY RULES:
- Preserve exact 4x4 grid, black borders, dark headers with white labels, solid black empty cells, and pure solid #00FF00 green background.
- Cute soft anime chibi hands in fair warm peach skin (#FCEEE7) with delicate warm dark plum-brown outlines (#4A3348) and subtle soft pink fingertip/knuckle shading.
- CRITICAL WRIST RULE: Draw ONLY the isolated hand ending cleanly at the wrist joint matching IMAGE 1—ZERO sleeves, ZERO cuffs, ZERO extra forearm/wrist stumps!
- Match the EXACT hand pose, finger count (cute 4-5 rounded chibi fingers, never claws), and wrist attachment angle of every cell in IMAGE 1:
  * hand:3 MUST be a PALM-FACING clenched fist (thumb tucked across the four curled fingertips facing the viewer—NOT back of hand).
  * hand:13: wide open friendly waving palm (thumb on LEFT, 4 fingers spread naturally, clean short wrist base).
  * hand:18 & hand:19: cute top-down grasping/petting fingertips matching IMAGE 1's mount-reins / pet-stroke pose cleanly.
```

### 4.6 Sheet 6 — `sheet6_xray`（`5×4`，17 格）

- `IMAGE 1`: [sheet6_xray.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/work/sheet6_xray.png)
- `IMAGE 2`: [gemini_ref_sd.png](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/gemini/reference/gemini_ref_sd.png)
- 保存路径：`skins/gemini/work/sheet6_ai.png`

```text
Task: Redraw all 17 populated X-ray skeleton sprite cells in IMAGE 1's 5x4 DST template grid (keep the 3 solid black empty cells untouched!) for the chibi character "Gemini" from IMAGE 2.

STRICT X-RAY STYLE & CHROMA-KEY RULES:
- Preserve exact 5x4 grid, black borders, dark headers with white labels, solid black empty cells, and pure solid #00FF00 green background.
- STRICT COLOR LIMIT: Every sprite uses ONLY PURE SOLID BLACK (#000000) silhouette filled with crisp PURE WHITE (#FFFFFF) cartoon skeleton bones/X-ray lines STRICTLY INSIDE the silhouette.
- ZERO colored pixels, ZERO grey gradients, ZERO external VFX, ZERO aura, ZERO cracks or zigzag lines outside the black silhouette on the #00FF00 green background.

PER-CELL SILHOUETTES:
- headbase:3: Gemini's exact chibi head silhouette WITH cat ears, twin side buns, and ahoge in pure #000000 black + crisp #FFFFFF cute cartoon skull, hollow eye sockets, and clenched teeth inside.
- hairpigtails:3 & hairpigtails:4: Pure #000000 unified wavy long-hair curtain silhouettes matching IMAGE 1 + clean #FFFFFF curved flowing X-ray highlight streaks inside (NOT split pigtails).
- tail:6: Pure #000000 fluffy cat-tail silhouette (base TOP-RIGHT curving down to BOTTOM-LEFT) + crisp #FFFFFF segmented curved tail vertebrae bones inside.
- torso:10, torso_pelvis:10, skirt:4: Pure #000000 bodice / pelvis / flared-skirt silhouettes + #FFFFFF cartoon ribcage/spine, hip bone, and stark vertical white X-ray skirt stripes strictly inside.
- arm_upper:5, 6 & arm_upper_skin:2: Pure #000000 upper-arm silhouettes + #FFFFFF humerus bone inside.
- arm_lower:3, 4: Pure #000000 forearm silhouettes + #FFFFFF radius/ulna bones inside.
- hand:11, 12: Pure #000000 clenched-fist silhouettes + #FFFFFF hand/finger bones inside.
- leg:9, 10 & foot:7: Pure #000000 leg and platform-shoe silhouettes + #FFFFFF femur/tibia and foot/toe skeleton bones strictly inside.
```

---

## 五、首轮打包与视觉精修记录

- **6 张 AI 母表就位**：`skins/gemini/work/sheet1_ai.jpg`..`sheet6_ai.jpg` 已转存为纯绿幕 `sheet1_ai.png`..`sheet6_ai.png`，并为 `sheet1_ai.png` 顶部文本标签区域补齐实色暗栏。
- **程序化质检修正（`gemini_skin_pipeline.py`）**：
  1. **腮红反解 + 眼内护白**：`_unmix_face_blush` 清除半透明腮红在 `#00FF00` 上的橄榄绿晕（保护封闭眼眶高光与金色虹膜），`_fill_enclosed_eye_pinholes` 自动填补 `face:0` 右眼底星形高光的 `<=16 px` 封闭透明针孔。
  2. **多段/宽嘴精确剥离**：`_extract_locked_eyes_face` 按中央小鼻点以下定位嘴部区域，同时支持 `face:11` 双半弧嘴与 `face:25` 宽喊嘴，零残留派生 `face:17, 18, 26, 27, 29`。
  3. **`arm_lower:2` 袖口方向**：水平镜像修正使宽白喇叭袖口对齐左侧手腕端（`arm_lower:6` 同步继承），`book`/`notes_loop`/`eat`/`emoteXL_bonesaw` 手腕穿接自然。
  4. **后发无缝羽化**：`hair:2` / `hair_hat:2` 顶部 `22%` smoothstep 羽化融进 `headbase:2`。
- **骨骼位置精调（`skins/gemini/offsets.json`）**：收敛 `hairpigtails:1`（`[-14, -24, 0.86]` 贴合侧脑后防垂地/探前）、`cheeks:0/1`（`0.72` 缩放对齐脸颊）、`hair:2`/`hairpigtails:2`、`tail:0` 及 `face:14` 电击夜视眼居中。
- **最终打包产物**：`mods/dst_buddy_agent/anim/skin_gemini.zip`（`69` 帧审图全覆盖 `130/130 (100.0%)`，`Missing frames = 0`）。
- **单束化拆分 `hairpigtails:0..4`（零重新生图）**：根据 DST 骨骼双侧挂载机制（正面 `0+1`、侧面 `1+2`、背面 `hair:2` 居中 + `0/1` 两侧、电击 `3+4`），在 `derive` 中将 `sheet1_ai` 的 `hairpigtails:0` 按中缝干净拆分为左/右单束波浪发锁并旋正，自动派生 X-ray 单束发锁 `hairpigtails:3, 4`，同时完整保留整片波浪背发至 `hair:2` / `hair_hat:2`（配合 `offsets.json` 对齐）。

---

### Rework 1: Puffed Cheeks (`rework1_cheeks`)

- **模板文件**：`skins/gemini/work/rework1_cheeks_template.png`（`2x1` 格，`cheeks:0` / `cheeks:1`）
- **风格与肤色参考图**：`skins/gemini/tuner_data/sprites/skin_gemini/headbase_0.png`（实测肤色 `#FDEADE`、柔和腮红 `#FED2C2`、下颌温棕线稿 `#693027`）
- **生成输出路径**：`skins/gemini/work/rework1_cheeks_ai.jpg`（或 `.png`）

```text
2D chibi game character part rework sprite sheet on a FLAT SOLID CHROMA-KEY GREEN (#00FF00) background, matching the exact anime cel-shaded skin color, soft peach blush, and thin warm-brown lineart of IMAGE 2 (Gemini's headbase_0 face).

ATTACHED IMAGES:
- IMAGE 1 (2x1 green-screen layout template): Preserve the exact 2x1 grid, black cell border, dark grey top label bars (`cheeks:0 (left puffed cheek)` on left, `cheeks:1 (right puffed cheek)` on right), and pure solid #00FF00 background inside both cells.
- IMAGE 2 (Gemini face color & lineart reference): Match the EXACT pale warm ivory-peach skin tone (#FDEADE), the super soft subtly-blended warm peach cheek blush (#FED2C2), and the thin clean warm-brown outline color (#693027) used on the chin/cheek contour of IMAGE 2.

CRITICAL DRAWING RULES (ONLY TWO PUFFED CHEEKS — NOTHING ELSE):
- Draw ONLY puffed hamster-like eating cheeks (mouthful cheek puffs for Don't Starve eating & blowdart animations).
- STRICTLY NO eyes, NO nose, NO mouth, NO lips, NO teeth, NO hair, NO ears, NO full head — just the round puffed cheek pouches matching the template positions/sizes.
- COLOR & SHADING: Over 90% of each cheek MUST be pure light warm ivory-peach skin (#FDEADE) matching IMAGE 2's face fill, with ONLY a tiny, very soft, smoothly diffused warm peach blush (#FED2C2) in the center — NOT bubblegum pink, NO hard concentric oval rings, NO chalky white highlights.
- SHAPE & OPEN-C LINEART (DO NOT flatten the inner edge):
  - Keep a full, plump circular/round puffed cheek silhouette inside each cell (like the template's round contour, not flat-chopped).
  - Left cell (`cheeks:0`): Thin crisp warm-brown lineart (#693027, matching IMAGE 2's chin stroke weight) forms an open C-arc along the TOP, LEFT, and BOTTOM outer edge, while the RIGHT (inner/mouth-facing) edge stays open/unlined or softly skin-faded so it blends seamlessly into the face and mouth.
  - Right cell (`cheeks:1`): Exact horizontal mirror of `cheeks:0` — thin crisp `#693027` C-arc outline along TOP, RIGHT, and BOTTOM outer edge, with the LEFT (inner/mouth-facing) edge open/unlined.
- BACKGROUND & CLEANLINESS: Pure flat solid #00FF00 chroma-key green in both cells. NO drop shadow, NO dark halo, NO semi-transparent outer blur, NO green reflection on the skin.
```
