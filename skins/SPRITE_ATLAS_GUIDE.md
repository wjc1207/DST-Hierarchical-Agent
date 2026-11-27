# DST 角色皮肤全量 Sprite 语义、复用关系与动画频率百科

> **数据来源（实测零猜测）**：
> - **动画包实测**：遍历 DST 本地 `data/anim/` 全部 **`250` 个 `player_*.zip`**（含 `169` 个通用/骑乘玩法包 + `81` 个角色专属/活动包，过滤 Klei `dev_`、`test_` 及 mid-transition 后统计所有有效动画剪辑与帧调用）；
> - **脚本依据**：核对 `data/databundles/scripts.zip` 中的 `scripts/stategraphs/SGwilson.lua`（`444` 个角色 State）、`scripts/emotes.lua` 与 `scripts/emote_items.lua`；
> - **原版图素对比**：逐一比对 [manifest.json](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/deepseek/work/manifest.json) 收录的原版 `wendy.zip` 全部 **`159` 张基础精灵**（外加通用同伴架构新增的 `hair:0..3`、`tail:0, 6`）的像素结构、余弦相似度矩阵与人工肉眼核验。
>
> 配套可复用 v2 6 表生成与零耦合架构指南请见 [SKIN_GENERATION_PLAYBOOK.md](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/SKIN_GENERATION_PLAYBOOK.md)；DeepSeek 实战/Prompt 请见 [prompts.md](file:///c:/Users/16425/Documents/Codex-workspace/DST-Hierarchical-Agent/jev_dst_agent/skins/deepseek/prompts.md)。

---

## 一、总览：下次做新皮肤，最少只需要画几张图？

原版 `wendy.zip` 包含 **`159` 张 Sprite**，再加上陪伴角色兼容架构的 `hair:0..3` 与 `tail:0, 6`，表面上看数量庞大；但**实测大量槽位在原版就是完全相同像素（相似度 `1.000`）、同眼换嘴（`>0.97`）、左右镜像（`>0.86`）或由打包管线自动派生**。

### 1.1 工作量压缩总结（165 槽位 → 核心必画集）

| 分类 | 槽位数量 | 处理方式 |
| :--- | :---: | :--- |
| **管线全自动派生（0 次生图）** | `40+` | `headbase_hat:0..3`（裁自 `headbase`）、`hair_hat:0..3`（复用 `hair`）、`torso_wide:0..10` / `torso_pelvis_wide:0..10` / `skirt_wide:0..4`（按胖瘦骨架自动缩放）、`tail:1..5, 7..11`（自动空帧）、`face:14`（夜间发光眼可留原版） |
| **原版 100% 完全相同帧（直接像素复用）** | `5 对` | `face:8 ≡ face:0`、`face:24 ≡ face:16`、`hand:14 ≡ hand:4`、`hand:16 ≡ hand:8`、`arm_lower:6 ≡ arm_lower:2` |
| **同眼换嘴 / 锁眼复用组（改嘴不重画眼）** | `19 槽位 → 4 套眼底稿` | 1. 常态睁眼底（`face:0`）→ 派生 `8, 9, 10, 15, 16, 19, 24`（仅换嘴），`28, 30` 仅缩左眼；<br>2. 闭眼底（`face:11`）→ 派生 `17, 18`；<br>3. 半睁懒散眼底（`face:20`）→ 派生 `22`（及可选基于 `face:0` 压眼皮）；<br>4. 紧闭挤眼底（`face:25`）→ 派生 `26, 27, 29, 32` |
| **镜像/同形近似复用** | `~20` | `foot:2` 与 `foot:0` 镜像、`arm_lower:5` 与 `arm_lower:1` 镜像、`hand:17 ≈ hand:13`、`hand:19 ≈ hand:18` 仰角版、`torso:5/8` 与 `torso:7` 镜像、`arm_upper_skin:0`（长袖角色可直接复用 `arm_upper:0`） |
| **纯黑白 X-ray 剪影（仅电击 2 个包使用）** | `16` | `headbase:3`, `hair:3`/`hairpigtails:3..4`, `tail:6`, `torso:10`, `torso_pelvis:10`, `skirt:4`, `arm_upper:5..6`, `arm_upper_skin:2`, `arm_lower:3..4`, `hand:11..12`, `leg:9..10`, `foot:7` |

> **下次新皮肤 MVP 建议顺序**：
> 1. **第一优先级（T0 核心日常覆盖 95%+ 游戏时间）**：`headbase:0..2` + `hairpigtails:0..2`（**单侧动态发束**）+ `hair:2`（背面一体披发；`0,1,3` 置空）+ `tail:0`（横向尾巴）+ `torso:0..6` + `torso_pelvis:0..6` + `skirt:0..3` + `arm_upper:0..3` + `arm_upper_skin:0`（长袖直接复用 `arm_upper:0`） + `arm_lower:0..2, 5` + `leg:0..4, 6, 8` + `foot:0..3, 5` + `hand:0..10` + `face:0, 2, 4, 6, 9, 10, 11, 12, 15, 21` + **`cheeks:0..1`（随初版 AI 母表出 `0` 再镜像 `1`，勿漏给代码硬画）**。
> 2. **第二优先级（T1 常用动作与受击）**：`torso:7..9`, `torso_pelvis:7..9`, `arm_upper:4`, `arm_upper_skin:1`, `arm_lower:6`（复用 `2`）, `leg:7`, `foot:6`, `hand:13, 14(=4), 16(=8), 17(≈13), 18, 19`, `face:16(=24), 17, 26, 27`, 全部 `16` 个 X-ray（仅被电击调用但极具辨识度）。
> 3. **第三优先级（T2/T3 Emote 与罕见状态）**：`face:1, 3, 5, 7, 13, 18, 19, 20, 22, 23, 25, 28, 29, 30, 31, 32` 与 `hand:15`（仅 `emoteXL_bonesaw` 纸板涂鸦骨锯）、`foot:4`、`leg:5`。

---

## 二、DST 玩家动画常用度分级（实测依据）

基于 `SGwilson.lua` 触发频率与 `250` 个 `player_*.zip` 的覆盖广度划分：

| 等级 | 游戏定位 | 代表动画包（`player_*.zip`）与核心剪辑 | 对皮肤质量的要求 |
| :--- | :--- | :--- | :--- |
| **T0 核心高频** | 站立、行走、奔跑、采集、造物、战斗、基础进食与常用自带表情 | `player_idles` (`idle_loop`, `idle_inaction`, `idle_hot_loop`, `idle_inaction_sanity`), `player_idles_shiver`, `player_basic` (`run_loop`, `walk_loop`), `player_actions` (`wakeup`, `research`, `hit`), `player_actions_item` (`pickup`, `build_loop`), `player_actions_axe` (`chop_loop`), `player_actions_pickaxe` (`pickaxe_loop`), `player_actions_shovel` (`shovel_loop`), `player_actions_eat` (`eat`, `hungry`), `player_attacks` (`atk`, `punch`, `spearjab`), `player_emotes` (`emote_waving`, `emote_happycheer`, `emote_sad`, `emote_angry`) | **极高**：玩家每秒都在看，必须像素级锁眼、零穿模、左右对称严谨 |
| **T1 常规玩法** | 垂钓、农耕、捕虫、骑牛、划船、背重物、睡眠、冰冻、电击受击 | `player_actions_fishing*`, `player_actions_farming`, `player_actions_bugnet`, `player_actions_whip`, `player_encumbered` (`heavy_walk`, `heavy_eat`), `player_knockedout` (`sleep_loop`), `player_frozen`, `player_shock` / `player_mount_shock` (`shock`), `player_mount*`, `player_boat*`, `player_actions_eat` (`refuseeat`) | **高**：高频玩法与特殊机制动作，电击 X-ray、骑乘扶缰手与拒食表情缺一不可 |
| **T2 主动 Emote 与偶发状态** | 输入 `/emote` 命令触发动作、哈欠昏睡、失眠/悬空、暗处受击、敲矿震手、高尔夫等 | `player_emotesxl` & `player_sit_*` (`emoteXL_waving4`, `emoteXL_bonesaw`, `emoteXL_facepalm`, `emoteXL_kiss`, `emoteXL_sad`, `emoteXL_angry`, `emoteXL_annoyed`), `player_emote_extra` (`emote_fistshake`, `emote_flex`), `player_knockedout` (`dozy`), `player_groggy` (`yawn`), `player_actions_pickaxe_recoil` (`pickaxe_recoil`), `player_suspended`, `player_hit_darkness`, `player_actions_golf` (`club_putt_charged`) | **中高**：多为独立特写或大表情，最容易暴露表情贴错、道具误画（如 `hand:15`）或冷门手臂穿帮 |
| **T3 极低频 / 仅特殊状态与自动派生** | 仅某角色/绝版活动/小游戏或纯由引擎/管线派生，通用动画里几乎不独立出镜 | `player_idles_<character>`, `player_transform_*`, `player_mighty_gym`, `player_ancient_*`, `player_tiptoe`, `player_receive_gift`, 以及全部 `*_wide`、`headbase_hat`、`hair_hat` | **低/自动**：保留或走派生/复用映射即可，无需投入单独重绘工时 |

---

## 三、跨 Sprite 复用与派生图谱（实测像素相似度）

### 3.1 原版实测同源视觉重复组（直接复用 / 改嘴不换眼）

```text
[100% 完全相同（直接复制 PNG/tile 复用）]
  ├─ face:0  == face:8    (similarity = 1.000)
  ├─ face:16 == face:24   (similarity = 1.000)
  ├─ hand:4  == hand:14   (similarity = 1.000)
  ├─ hand:8  == hand:16   (similarity = 1.000)
  └─ arm_lower:2 == arm_lower:6  (similarity = 1.000，注：arm_lower:6 绝对不是 X-ray！)

[共享同一双眼睛与眉毛（仅嘴巴/极微调不同，程序化改嘴即可 0 眼闪）]
  ├─ 常态睁眼家族 (eye sim 0.978–1.000):
  │     face:0(笑) ─┬─ face:8(微闭嘴)  ├─ face:9(半张)  ├─ face:10(张嘴)
  │                 ├─ face:15(大笑)   ├─ face:16/24(担忧o嘴)  └─ face:19(斜撇小嘴)
  ├─ 大小眼家族 (eye sim 0.973，右眼/右眉/鼻 100% 取自 face:0，仅缩左眼):
  │     face:28(嫌弃/斜撇嘴) ── face:30(恼火倒拱嘴)
  ├─ 平和闭眼家族 (eye sim 0.990–0.994):
  │     face:11(平静平嘴) ─┬─ face:17(伤心倒拱嘴)  └─ face:18(微张小呼嘴)
  ├─ 困倦半睁眼家族 (eye sim 0.996):
  │     face:20(昏睡/无聊小撇嘴，推荐直接压低 face:0 上眼皮) ── face:22(微张小撇嘴)
  └─ 紧闭挤眼 > < 家族 (shared > < eyes):
        face:25(挫败大喊) ─┬─ face:26(闭眼微张嘴抗拒)  ├─ face:27(咬牙用力)
                           ├─ face:29(委屈哭泣撇嘴)    └─ face:32(震惊喊叫)
```

### 3.2 四肢 / 身躯对称镜像与近似复用规律

| 部位 | 实测可复用规律 | 制作建议 |
| :--- | :--- | :--- |
| **`torso` (0..10)** | `torso:0` 正、`torso:3` 侧、`torso:6` 背为核心三视图；`torso:5` 与 `torso:7/8` 为对称倾斜（镜像 sim `0.837–0.870`）；`torso:1/2`、`4/5` 分别为微弯与大倾变体 | 重点保证 `0/1/3/6/9/10` 准确；其余偏转角按模板旋转/镜像局部修整，**严禁带袖子与围裙下摆** |
| **`torso_pelvis` (0..10)** | `0..8` 互相相似度达 `0.82–0.90`，本质是腰部衔接梯形块；只有 `9` 是俯视圆环、`10` 是 X-ray 骨盆 | 无腰饰的角色几乎可共享 1 套标准衔接梯形微调轮廓；侧面 `torso_pelvis:3` 务必做窄纯色，防腰侧外凸 |
| **`skirt` (0..4)** | `0` 正/侧立、`1` 背立、`2` 正/背跑、`3` 宽弧动态裙、`4` X-ray | 先画精 `0`（正）与 `1`（背，**不带尾巴**）；**`skirt:3` 强烈推荐从定稿 `skirt:0` 程序化扇形展开成 `~2.2:1` 宽弧裙**，100% 保证花纹连贯且跑步不穿模 |
| **`arm_upper` (0..6)** | `0/1/2/3` 为不同斜角下垂/前伸大袖，`4` 为俯视短圆形袖，`5/6` 为 X-ray | **长袖/泡泡袖角色：`arm_upper_skin:0` 可直接复用 `arm_upper:0`**（配合 `offsets.json` 对齐尺寸与中心） |
| **`arm_lower` (0..6)** | `0` 短垂直前臂、`1` 长垂直前臂、`2 ≡ 6` 水平前臂、`5` 微弯后掠前臂（与 `1` 镜像 sim `0.898`）、`3/4` X-ray | 画好 `0, 1, 2, 5` 后，`6` 直接复用 `2`，大幅减少重复生图 |
| **`hand` (0..19)** | `4 ≡ 14`、`8 ≡ 16`、`13 ≈ 17`、`18`（扶背斜指尖）与 `19`（横抚平指尖）形态高度呼应、`11/12` X-ray | `hand:3` 必须是掌心朝前握拳；`hand:15` 是纸板白粉笔涂鸦骨锯（仅 `bonesaw`） |
| **`leg` (0..10)** | `0/1/2/3/5/8` 均为主干直腿（相似度 `0.83–0.94`，仅膝盖朝向与微弯弧度不同）；`4` 屈膝、`6` 大弯步、`7` 跨蹲、`9/10` X-ray | 直腿系共用一套丝袜/裤腿配色与腿环位置，`9/10` 做成纯黑白腿骨 |
| **`foot` (0..7)** | `0` 与 `1/2` 为典型左/右镜像关系（`0` 朝右、`1/2` 朝左，镜像 sim `0.869–0.929`）；`3` 前向、`4` 踮脚、`5` 纯侧立、`6` 后脚跟/底、`7` X-ray | 定稿 `foot:0` 后可镜像参考生成 `1/2` 并微调透视 |

### 3.3 全量 X-ray 黑白透视槽位清单（共 16 个，全部仅被 `shock` 调用）

> 所有 X-ray 槽位仅出现在 `player_shock.zip:shock` 与 `player_mount_shock.zip:shock`（各包共 16 帧）。生成 Prompt 中**严禁出现 `electrocuted / lightning / spark / glow / flash`**，否则模型会在绿底上乱画闪电裂纹：
> - 头部：`headbase:3`、`headbase_hat:3`（自动裁剪）、`hair:3`（或 `hairpigtails:3, 4`）、`tail:6`
> - 身躯：`torso:10`（含 `torso_wide:10`）、`torso_pelvis:10`（含 `torso_pelvis_wide:10`）、`skirt:4`（含 `skirt_wide:4`）
> - 四肢：`arm_upper:5`、`arm_upper:6`、`arm_upper_skin:2`、`arm_lower:3`、`arm_lower:4`、`hand:11`、`hand:12`、`leg:9`、`leg:10`、`foot:7`
> - 特别提醒：**`arm_lower:6`（水平普通袖）** 与 **`face:14`（夜间微光眼）** 不是 X-ray 骷髅！

---

## 四、全量 Sprite 逐格百科（按部位完整收录）

> 说明：“实测调用”格式为 `包数/剪辑数/总帧数`（统计自 250 个 `player_*.zip` 非 transition 剪辑）。

### 4.1 头部、头发、尾巴与腮红（`headbase` / `hair` / `hairpigtails` / `tail` / `cheeks`）

| 槽位 | 原版尺寸 | 真实视觉含义与朝向 | 可复用 / 派生方案 | 常用度 | 实测调用 (`包/剪辑/帧`) & 代表动画 | 制作避坑提示 |
| :--- | :--- | :--- | :--- | :---: | :--- | :--- |
| `headbase:0` | `197×206` | 正面空白圆头壳（无五官） | 独立绘制 | T0 | `225 / 580 / 22761` (`idle_loop`, `eat`, `wakeup`) | **下巴正下方必须 100% 纯绿净空**，禁止背发连着下巴垂下遮领口 |
| `headbase:1` | `200×190` | **朝右**侧面空白头壳 | 独立绘制 | T0 | `132 / 233 / 7059` (`idle_loop` 侧, `run_loop` 侧) | 强制镜像为朝右（nose right）；后发到后颈圆润收尖，不可被边框切平 |
| `headbase:2` | `196×201` | 背面空白头壳 | 独立绘制 | T0 | `123 / 222 / 7000` (`idle_loop` 背, `idle_inaction`) | 必须与正面同画风（用哑光赛璐璐），避用过度发亮环形高光 |
| `headbase:3` | `197×206` | X-ray 正面黑头+白骷髅 | 按 `headbase:0` 轮廓填纯黑+白骨 | T1 | `2 / 2 / 16` (`shock`) | **严禁轮廓外闪电/电火花特效** |
| `headbase_hat:0..3` | `180×176`~`183×180` | 戴帽压发后的头壳顶部裁切 | **管线自动从 `headbase:0..3` 裁剪派生** | T0/T1 | `222` / `132` / `121` / `2` 包 (`eat`, `idle_loop`, `shock`) | 无需单独生图；留意发箍/头饰在裁线以上或自然保留 |
| `hair:0, 1, 3` (`hair_hat:0, 1, 3`) | (扩展) | 非背面整片长发占位槽 | **长披发角色打包为 `4×4` 透明占位帧（`long_hair_blank:0`）** | T0/T1 | `209 / 536 / 21195` | 因 `hair:0,1` 在引擎层级位于躯干前侧，长发角色若不置空会在 286 个非背面动画里遮挡胸口；动态垂发统一交由 `hairpigtails:0..4` |
| `hair:2` (`hair_hat:2`) | (扩展) | **唯一激活的背面一体连续大波浪长发** | 独立绘制（可直出 Sheet 1 或复用原背发图），顶部 `22%` smoothstep 羽化 | T0 | `114 / 209 / 6429` | 与 `hairpigtails:0, 1` 配合构成完整背发（`headbase:2 → hair:2 → 1+0`）；`hair_hat:2` 自动复用 |
| `hairpigtails:0..4` | `141×172`~`149×163` | **单侧单束动态发锁**（`0` 左单束、`1` 右/侧远单束、`2` 侧近单束、`3/4` 电击 X-ray 左右单束；**严禁双侧整片！**） | `0, 1` 按单侧（`w < h`）随 Sheet 1 生成，`2` 可复用 `0`，`3/4` 单侧 X-ray 绘或内腐蚀派生 | T0/T1 | `218 / 564` 等（`idle_loop`、`shock` 全覆盖） | **历史大坑**：DST 正面同时挂 `1+0`、侧面同时挂 `1+2`、电击同时挂 `4+3`！若将 `0/1` 画成左右双侧整片或整头宽发，会在正侧面重影翻倍爆炸 |
| `tail:0` | (扩展) | **横向舒展尾巴（`w > h`：尾根正右 3 点钟 → 向左下微弧 → 尾尖左上 9–10 点钟微翘，根→尖约 `-165°~-170°`）** | 随 Sheet 1 横向 AI 生成，禁止画死在 `skirt:1/3` | T0 | `223 / 577 / 35647` (带尾动物系角色通用槽位) | **历史大坑**：DST `tail` 骨骼自带约 `-34°` 逆时针旋转（背面水平镜像），若画成竖直 J 钩（`h > w`）会垂穿鞋底/地面线；打包须以旋转后包围盒归一化，`offsets.json` 单独精调且勿被 `tail:6` 盲继承 |
| `tail:6` | (扩展) | **横向 X-ray 纯黑+白分段尾骨（与 `tail:0` 同朝向，`w > h`）** | 按横向 `tail:0` 同角画黑剪影+白椎骨 | T1 | `2 / 2 / 16` (`shock`) | 需单独校准与 `tail:0` 对齐的旋转角；`tail:1..5, 7..11` 自动填 `4×4` 透明占位 |
| `cheeks:0` | `52×49` | 饱满圆润鼓腮（左颊 / 仅上左下外圈细线，**右侧面朝嘴巴不平切且无轮廓线**） | **必须纳入初版 AI 母表直接绘制（推荐放 Sheet 1 第 8 格锁 `headbase:0` 肤色，或放 Sheet 5 空位）** | T0/T1 | `10 / 15 / 574` (`eat`, `quick_eat`, `heavy_eat`, `dart`) | **历史大坑**：严禁漏进模板后用代码硬画！手写合成易出现右侧平切、过粉同心圆与线稿割裂导致 `eat`/`dart` 违和返工；90%+ 须匹配 `headbase:0` 肤色配极柔和腮红 |
| `cheeks:1` | `52×49` | 饱满圆润鼓腮（右颊 / **左侧面朝嘴巴无轮廓线**） | **由初版 AI `cheeks:0` 严格水平镜像（`ImageOps.mirror`）**（或直接同出 Sheet 5） | T0/T1 | `5 / 9 / 374` (`eat`, `quick_eat`, `heavy_eat`) | 同样保持饱满圆润与面朝口侧无描边 |

### 4.2 表情系统 `face:0..32`（透明纯绿底悬浮五官组件，零肤色底）

| 槽位 | 原版尺寸 | 真实视觉含义（经图素与动画双重核实） | 可复用 / 派生方案 | 常用度 | 实测调用 (`包/剪辑/帧`) & 代表动画 | 制作避坑提示 |
| :--- | :--- | :--- | :--- | :---: | :--- | :--- |
| `face:0` | `120×86` | **默认正面微笑睁眼** | 核心原画母版 A | T0 | `193 / 456 / 8070` (`idle_loop`, `emote_waving`, `shovel_loop`) | 须留足 `eye` 与 `mouth` 之间距，供后续 `face:9/10/20/28` 锁眼派生 |
| `face:1` | `125×76` | 正面**半眨眼过渡帧 1**（微眯带扁平嘴） | 可微压 `face:0` 眼皮，或暂映射至 `0` | T0/T1 | `45 / 71 / 387` (`idle_inaction`, `emote_sad`, `emote_feet`) | 每次出现仅 `1–2` 帧作眨眼中间帧；若重绘需严格对齐 `face:0` 瞳孔横向位置 |
| `face:2` | `126×60` | **正面双目完全闭合**（紧凑平静微皱眉闭眼） | 核心独立绘（闭眼弧线） | T0 | `121 / 206 / 2681` (`wakeup`, `idle_shiver_loop`, `emote_annoyed_facepalm`) | 不能有露出的眼白或瞳孔，平滑弧线禁止画成灰黑黑眼圈 |
| `face:3` | `120×70` | 正面**半眨眼过渡帧 2**（略比 `1` 眯得多一点） | 与 `face:1` 基本等同 | T1 | `32 / 49 / 232` (`emote_hat`, `emote_pants`, `emote_swoon`) | `DEFAULT_FACE_MAP` 默认复用 `face:0`，或共用 `face:1` |
| `face:4` | `68×84` | **朝右侧面默认睁眼微笑**（仅单眼） | 核心原画母版 B（侧面） | T0 | `107 / 182 / 4223` (`idle_loop` 侧, `chop_loop`, `shovel_loop`) | 强制朝右；只画一只眼睛和流畅极简侧面单线条小鼻，禁画第二只眼 |
| `face:5` | `73×74` | 朝右侧面**半眨眼过渡帧 1** | 压低 `face:4` 眼皮或复用 `4` | T1 | `15 / 19 / 131` (`shieldparry_loop`, `parry_loop`, `club_putt_charged`) | 多在格挡/蓄力中出现，复用 `4` 通常无违和感 |
| `face:6` | `74×60` | **朝右侧面完全闭眼**（单眼闭合弧线） | 核心独立绘（侧面闭眼） | T0 | `66 / 93 / 2102` (`idle_inaction_sanity`, `idle_shiver_loop`, `punch`) | AI 极易在 `face:6` 错画出两只眼或原版大三角形鼻翼，务必严控 |
| `face:7` | `72×68` | 朝右侧面**半眨眼过渡帧 2** | 与 `face:5` 等同，可复用 `4` | T2 | `13 / 15 / 400` (`idle_groggy`, `heavy_walk`, `heavy_idle`) | 多用于虚弱/扛重物侧走半眯眼 |
| `face:8` | `120×86` | 说话/进食闭合口型起始帧（**原版 ≡ `face:0`**） | **与 `face:0` 100% 完全相同复用** | T0 | `25 / 29 / 430` (`eat`, `float_mime`, `hotspring_mime`) | **必须与 `face:0` 同框同偏移零偏差**，否则说话时眼眉剧烈闪烁 |
| `face:9` | `120×94` | **说话/进食半张小嘴**（眼眉与 `face:0` 全同） | **锁定 `face:0` 眉眼鼻，仅改微张嘴** | T0 | `25 / 28 / 367` (`idle_hot_loop`, `dial_loop`, `emote_hands`, `eat`) | 严禁单独用 AI 重新生成眼睛！必须基于 `face:0` 改嘴并锁 `<0.01 px` 误差 |
| `face:10` | `120×97` | **饥饿/说话/造物大张嘴**（眼眉与 `face:0` 全同） | **锁定 `face:0` 眉眼鼻，仅改张嘴** | T0 | `55 / 87 / 1196` (`idle_hot_loop`, `hungry`, `eat`, `build_loop`) | 嘴部务必向上收紧避免在 `build_loop` 等低头动作里超出下巴边缘 |
| `face:11` | `116×68` | **平静闭眼**（微笑/平和安睡） | 核心闭合表情母版 C | T0/T1 | `35 / 59 / 1413` (`emote_strikepose`, `emote_feet`, `research`, `wakeup`) | 弧线向下弯表示宁静闭眼，零灰眼圈 |
| `face:12` | `116×88` | **慌张/焦急/缩瞳盯看**（原版向右下微缩瞳惊慌看） | 核心独立绘 | T0/T1 | `35 / 52 / 1072` (`idle_inaction_sanity`, `distress_loop`, `fall_high`, `fishing_ocean_bite`) | 不要直接套 `X_X`（那是 `13`），适合重绘成日系小瞳孔焦急感（微朝右下更佳） |
| `face:13` | `124×88` | **死亡/晕眩 `X_X` 眼** + 惊讶 `o` 嘴 | 核心独立绘 | T1/T2 | `3 / 5 / 79` (`death`, `death2`, `death_reverse`) | 专用于死亡，画成二次元细线 `X_X` 眼 |
| `face:14` | `80×29` | **夜间/电击柔和白色发光眼** | **保留原版 `sprites/face_14.png`**（不走绿幕） | T1 | `2 / 2 / 16` (`shock`) | 管线已内置直通无损叠加逻辑，不要放进绿幕表抠图 |
| `face:15` | `120×94` | **欢呼大笑 `^o^` / 睁大开心笑**（原版与 `0` 同眼 + 露舌大笑） | 锁 `face:0` 眼或独立画弯弯笑眼 `^_^` + 开心大笑嘴 | T0 | `31 / 54 / 2049` (`emote_happycheer`, `emote_swoon`, `emoteXL_loop_dance6`) | 日系画风可用微弯开心大笑嘴 + 亮晶晶眼睛/弯笑眼，非常出彩 |
| `face:16` | `120×88` | **难过/吃力担忧倒 `o` 嘴**（**原版 ≡ `face:24`**） | 锁定 `face:0` 眼 + 担忧眉与下撇小开嘴（`24` 复用之） | T1 | `9 / 14 / 277` (`heavy_walk`, `heavy_idle`, `emote_sad`, `sink`) | `heavy_walk`/`heavy_idle` 高频出现，最好别直接复用 `face:9` 微笑眼而要配担忧/委屈嘴 |
| `face:17` | `116×69` | **闭眼委屈/沉睡难过撇嘴 `︵`**（眼同 `11`） | 锁定 `face:11` 闭眼 + 改下弯撇嘴 | T1 | `16 / 20 / 418` (`sleep_loop`, `quick_drink`, `emote_sad`) | 帐篷/睡袋/晕倒后均会使用，极易与平嘴 `11` 形成柔和反差 |
| `face:18` | `116×71` | **闭眼小张嘴打盹/嘟嘴/吻预备**（眼同 `11`） | 锁定 `face:11` 闭眼 + 改小 `o` 嘴 | T2 | `8 / 12 / 186` (`dozy`, `emoteXL_kiss`) | 目前映射 `11`，有余力可由 `face:11` 改个小圆呼嘴 |
| `face:19` | `120×85` | **睁眼微撇小嘴 / 闲坐发呆**（眼同 `0`） | 锁定 `face:0` 睁眼 + 斜撇小嘴 | T2/T3 | `4 / 4 / 62` (`emote_loop_sit3`, `sink`) | 目前映射 `0` 非常合适，也可由 `face:0` 微改小嘴 |
| `face:20` | `130×66` | **困倦/无聊半睁眼 + 嘴角下垂**（`dozy` 专用） | **由 `face:0` 压低平直上眼皮遮住高光 + 改极小下垂嘴** | T2 | `14 / 24 / 565` (`emote_sleepy`, `dozy`, `emote_sad`) | 必须保留与 `face:0` 完全一致的瞳色，嘴要小巧、五官纵向紧凑 |
| `face:21` | `126×78` | **愤怒咬牙斜皱眉半睁眼** | 核心独立绘 | T0/T1 | `11 / 16 / 528` (`emote_angry`, `emote_flex`) | 不可带灰黑眼圈；用日系锐利斜挑怒眉 + 清晰咬牙或怒嘴 |
| `face:22` | `130×66` | **懒散半睁眼紧撇小嘴**（眼同 `20`） | **直接复用 `face:20`**（或紧闭小撇嘴） | T2/T3 | `5 / 5 / 72` (`emote_sad`, `dozy`, `idle_wanda`) | 建议在 `DEFAULT_FACE_MAP` 中把 `22` 改映射到 `20`（而非 `21`）！ |
| `face:23` | `110×94` | **紧闭无泪大打哈欠** | 核心独立绘 | T2 | `4 / 4 / 152` (`emote_yawn`, `yawn`) | **绝不要复用哭眼 `face:25`**，哈欠闭眼不可画泪滴 |
| `face:24` | `120×88` | **惊恐/摇晃/哭前担忧倒 `o` 嘴**（**原版 ≡ `face:16`**） | **与 `face:16` 100% 相同复用** | T1/T2 | `8 / 8 / 152` (`teeter_loop`, `emoteXL_sad`, `sink`) | `DEFAULT_FACE_MAP` 目前映射到 `9`，若单独重绘 `16` 可让 `24` 跟随 `16` |
| `face:25` | `128×86` | **挫败/愤怒/抓狂大喊**（紧闭 `> <` + 大张喊嘴，**无眼泪**） | 紧闭挤眼母版 D | T2 | `9 / 11 / 315` (`emoteXL_angry`, `emote_fistshake`) | User 实机纠偏：`emote_fistshake` 表达“挫败/愤怒/无奈”，**不要画眼泪** |
| `face:26` | `128×82` | **拒食抗拒嫌弃**（紧闭眼 + **嘴巴微张**） | 独立或由 `25` 闭眼配微张小嘴 | T1/T2 | `12 / 21 / 343` (`refuseeat`, `heavy_refuseeat`, `suspended_spit`, `grave_spawn`) | 嘴只微张传出嫌弃抗拒感，与 `25` 大喊区分 |
| `face:27` | `128×65` | **使劲用力/咬紧牙关狰狞**（紧闭眼 + 咬牙齿） | 独立或由 `25` 闭眼配白齿咬牙嘴 | T1/T2 | `22 / 23 / 334` (`pickaxe_recoil`, `emote_pants`, `sand_idle_loop`, `grave_spawn`) | 高频出现于挖矿弹开、热喘、沙尘暴与出坟动画 |
| `face:28` | `121×84` | **不对称大小眼坏笑/嫌弃/挑衅**（右眼同 `0`，仅左眼眯小） | **100% 锁死 `face:0` 右眼/右眉/鼻，仅缩左眼** | T2 | `19 / 30 / 822` (`emoteXL_waving4`, `emoteXL_bonesaw`, `emote_hands`) | 切勿自由发挥重绘整张脸导致右眼颜色与 `face:0` 脱节 |
| `face:29` | `128×64` | **紧闭挤眼抽泣撇嘴**（眼类似 `27`） | 暂随 `25` 或由 `25/27` 改撇哭嘴 | T2 | `5 / 5 / 188` (`emoteXL_sad`, `mime12`) | 游戏里会自带粒子眼泪特效，五官留小嘴即可 |
| `face:30` | `121×86` | **大小眼恼火嫌弃撇嘴**（眼同 `28`） | **直接复用/锁 `face:28` 改下垂撇嘴**（当前映 `21`） | T2 | `6 / 6 / 215` (`emoteXL_annoyed`, `emote_pants`, `refuseeat`) | `emoteXL_annoyed`（摇头说不）高光镜头，可用 `28` 换微撇嘴实现 |
| `face:31` | `116×83` | **惊愕呆滞 / 悬空催眠无神睁眼** | 核心独立绘 | T2 | `2 / 5 / 111` (`suspended`, `suspended_hit`, `insomniac_sleep_loop`) | **去灰黑眼圈！** 用纯二次元“失去高光的淡蓝缩瞳”保留神态同时不破画风 |
| `face:32` | `128×79` | **黑夜受惊/吹号大吼**（眼同 `26` + 方大喊嘴） | 复用 `25` 即可 | T2 | `5 / 5 / 67` (`hit_darkness`, `hornblow`) | 当前映射 `25` 已足够传神 |

### 4.3 躯干 `torso:0..10` 与骨盆衔接 `torso_pelvis:0..10`（全系列无袖无裙摆）

| 槽位 | 原版尺寸 | 真实视觉含义与朝向 | 可复用 / 派生方案 | 常用度 | 实测调用 (`包/剪辑/帧`) & 代表动画 | 制作避坑提示 |
| :--- | :--- | :--- | :--- | :---: | :--- | :--- |
| `torso:0` | `56×68` | 正面标准无袖上衣 | 核心母版 + 自动派生 `torso_wide:0` | T0 | `220 / 551 / 18910` (`eat`, `idle_loop`, `emote_sad`) | 绝不画袖子、不画裙摆、底部齐平收腰 |
| `torso:1` | `62×69` | 正/斜**向左前倾弯腰**（左侧微凹、领口微斜向左） | 核心变形 | T0 | `79 / 109 / 1979` (`refuseeat`, `hungry`, `emote_yawn`, `idle_inaction`) | 弯腰朝向极易画反，需紧贴模板左弯轮廓 |
| `torso:2` | `60×68` | **向右倾斜弯腰**上衣（与 `torso:1` 近似镜像） | 可微调或镜像参考 `torso:1`（注意襟口对称性） | T0 | `81 / 115 / 2493` (`idle_hot_loop`, `wakeup`, `research`, `fall_high`) | 带不对称胸针的角色不可直接无脑镜像，无左右差异则可参考 |
| `torso:3` | `54×72` | **朝右侧面瘦身无袖上衣** | 核心侧面母版 + `wide` 自动派生 | T0 | `108 / 181 / 4559` (`idle_loop` 侧, `sleep_loop`, `idle_shiver_loop`) | **高危！只画窄腰上衣，严禁把背面大蝴蝶结、白腰带或裙弧画进去** |
| `torso:4` | `68×74` | 侧前方微向**左**大仰/伸展身躯（带较大斜度） | 基于模板角度重绘 | T0 | `72 / 97 / 1947` (`idle_inaction_sanity`, `build_loop`, `shovel_loop`) | 与 `build_loop`/`shovel_loop` 关节衔接需稳住腰底宽度 |
| `torso:5` | `68×74` | 侧前方微向**右**倾弯腰上衣（与 `7` 为镜像关系） | 参照模板或镜像参考 `7` | T1 | `29 / 47 / 758` (`wakeup`, `punch`, `spearjab`, `toolpunch`) | 攻击出拳与起床过渡常用 |
| `torso:6` | `56×72` | **背面**标准无袖上衣 | 核心背面母版 + `wide` | T0 | `119 / 209 / 5928` (`idle_loop` 背, `punch`, `idle_shiver_loop` 背) | 展示背后领口与肩带细节 |
| `torso:7` | `66×70` | 侧后微向**左**倾弯曲上衣（与 `5/8` 对称，`5/8` 镜像 sim `0.87`） | 同斜姿系调优 | T1 | `36 / 44 / 636` (`idle_hot_loop`, `shovel_loop`, `chop_loop`) | 工具挥动中高频出现 |
| `torso:8` | `67×72` | 侧后微向**右**倾弯曲上衣（与 `7` 严格左右镜像 sim `0.866`） | **可由 `torso:7` 镜像参考修正正反饰物** | T1 | `26 / 29 / 470` (`chop_loop`, `atk`, `amulet_rebirth`, `shieldparry_loop`) | 砍树、格挡必用 |
| `torso:9` | `71×60` | **背面俯视/大弯腰圆形肩背** | 核心俯视件 | T1 | `8 / 8 / 71` (`pickaxe_loop` 背, `fish_nocatch`, `bugnet` 背) | **并非普通站立背！** 背面挥镐/钓鱼时露出的圆润肩背 |
| `torso:10` | `62×68` | **X-ray 黑底+白肋骨/脊柱** | 纯黑+纯白肋骨，无闪电 | T1 | `2 / 2 / 16` (`shock`) | 不含裙与手 |
| `torso_pelvis:0..8` | `59×32`~`63×38` | 正(`0`)/左弯(`1`)/右弯(`2`)/侧(`3`)/倾(`4,5,7,8`)/背(`6`) 骨盆梯形连接件 | 均可通过纯主色紧凑圆底梯形高度复用（`0..8` sim `0.82–0.90`） | T0/T1 | `107..211` 包 (`0/3/6`)、`26..81` 包 (`1,2,4,5,7,8`) | **核心原则：宁窄勿宽！特别是 `torso_pelvis:3` 不能画宽白带，否则穿出 `skirt:0/3` 两侧腰边** |
| `torso_pelvis:9` | `61×59` | **背面俯视大弯腰底圈** | 对应 `torso:9` 底部圆环 | T1 | `8 / 8 / 71` (`pickaxe_loop` 背, `fishing`, `bugnet`) | — |
| `torso_pelvis:10` | `60×33` | **X-ray 黑底+白骨盆** | 纯黑+纯白骨盆 | T1 | `2 / 2 / 16` (`shock`) | — |

### 4.4 裙摆 `skirt:0..4`

| 槽位 | 原版尺寸 | 真实视觉含义与朝向 | 可复用 / 派生方案 | 常用度 | 实测调用 (`包/剪辑/帧`) & 代表动画 | 制作避坑提示 |
| :--- | :--- | :--- | :--- | :---: | :--- | :--- |
| `skirt:0` | `84×42` | **正面 & 侧面常规站立裙** | 核心母版 + `skirt_wide:0` | T0 | `219 / 555 / 24927` (`idle_loop`, `run_loop` 等全覆盖) | 正中对称装饰（如前围裙）最百搭；不要附加尾巴 |
| `skirt:1` | `81×40` | **背面站立裙** | 复制 `skirt:0` 基型改背面细节（如背大蝴蝶结） | T0 | `126 / 222 / 6560` (`idle_loop` 背, `idle_inaction` 背) | **有 `tail` 槽时严禁在 `skirt:1` 上再画静态尾巴！** 会重影成双尾 |
| `skirt:2` | `101×50` | **正/背面跑步侧飞飘动裙** | 从 `skirt:0` 向右上甩摆变形 | T0 | `55 / 78 / 1415` (`wakeup`, `run_loop`, `atk`, `research`) | 飘逸弧度向右后扬起以覆盖跨步大腿 |
| `skirt:3` | `114×36` | **宽矮上扬飘动弧裙（`~2.2–3:1`）**，**并非纯侧裙！** | **直接用 `skirt:0` 2× 超采样扇形展开+双侧翼上扬合成** | T0 | `77 / 109 / 3003` (`run_loop` 侧 16 帧, `pickaxe_loop`, `eat`, `emote_angry`, `whip`, `bugnet`, `sit1`) | **最大暗坑之一**：绝不能画成 `1:1` 窄高侧裙，否则按高度缩放会缩窄一半导致侧跑腿部大穿模！ |
| `skirt:4` | `84×42` | **X-ray 纯黑+白竖向条纹裙** | `skirt:0` 轮廓填纯黑+白竖纹 | T1 | `2 / 2 / 16` (`shock`) | 仅 `shock` 使用，无闪电 |

### 4.5 手臂与手部（`arm_upper` / `arm_upper_skin` / `arm_lower` / `hand`）

| 槽位 | 原版尺寸 | 真实视觉含义与朝向 | 可复用 / 派生方案 | 常用度 | 实测调用 (`包/剪辑/帧`) & 代表动画 | 制作避坑提示 |
| :--- | :--- | :--- | :--- | :---: | :--- | :--- |
| `arm_upper:0` | `29×52` | 正/常规垂直大臂上袖 | 核心母版 A | T0 | `217 / 550 / 26896` (`idle_loop`, `eat`, `idle_hot_loop`) | 下端袖口弧边或花边与 `arm_lower` 自然衔接 |
| `arm_upper:1` | `32×54` | 略粗前伸/下垂大臂袖 | 同母版微变体 | T0 | `198 / 492 / 26653` (`idle_loop`, `idle_inaction`) | 与 `0` 高度近似（`sim 0.85` 家族），保持一致质感 |
| `arm_upper:2` | `28×49` | 稍细斜举大臂袖 | 同母版微变体 | T0 | `149 / 276 / 8542` (`idle_loop`, `wakeup`) | — |
| `arm_upper:3` | `35×56` | 高举/挥手大摆幅大臂袖 | 独立偏长/略膨袖 | T0 | `85 / 154 / 4436` (`emote_yawn`, `refuseeat`, `emote_waving`) | 常旋转举高，袖口方向对齐原版向下端 |
| `arm_upper:4` | `32×33` | **正对镜头缩短（foreshortened）大臂**（圆球状） | 核心透视圆袖 | T1 | `14 / 27 / 699` (`punch`, `toolpunch`, `chop_loop`, `spearjab`) | 拳头向前/向内挥动时的正面投影缩短袖 |
| `arm_upper:5, 6` | `29×52`, `32×54` | **X-ray 大臂（黑袖+白肱骨）** | `arm_upper:0, 1` 轮廓填纯黑+白肱骨 | T1 | `2 / 2 / 16` (`shock`) | 无闪电特效 |
| `arm_upper_skin:0` | `15×52` | 特殊动作替用大臂段（高尔夫/教练/钓鱼等） | **长袖/泡泡袖角色推荐直接复用 `arm_upper:0`**（设 `offsets.json` 对齐） | T0/T1 | `222 / 581 / 36014` (`idle_loop`, `club_putt_charged`, `coach`, `fishing`) | 若留给引擎肤色染色，穿袖子会露出一截细白棒/肉臂；长袖直接复用 `arm_upper:0` 最稳 |
| `arm_upper_skin:1` | `14×24` | 短节交替替用大臂段 | 4× 超采样同袖色胶囊（无袖口） | T1 | `16 / 29 / 707` (`punch`, `toolpunch`, `chop_loop`, `spearjab`) | 同袖子色板（如 `#2E3A67`），不要像素锯齿 |
| `arm_upper_skin:2` | `14×45` | **X-ray 替用大臂（黑+白肱骨）** | 保留或按 `arm_upper:5` 风格重绘黑白 | T1 | `2 / 2 / 16` (`shock`) | 仅 `shock` 使用 |
| `arm_lower:0` | `15×40` | 短节垂直前臂（含手腕花边/袖口） | 核心母版 A | T0 | `224 / 575 / 33218` (`idle_loop`, `idle_hot_loop`, `idle_shiver_loop`) | 袖口位于底部，紧接手腕 `hand` |
| `arm_lower:1` | `14×54` | 长节垂直前臂（含袖口） | 核心母版 B（微拉长 `0`） | T0 | `203 / 514 / 27435` (`idle_sanity_loop`, `idle_loop`) | — |
| `arm_lower:2` | `52×26` | **向左水平伸出前臂**（左端带袖口） | 核心母版 C（水平） | T0 | `57 / 98 / 3226` (`eat`, `refuseeat`, `death`, `death2`) | 袖口和手腕端严格在**左侧** |
| `arm_lower:3, 4` | `15×39`, `20×54` | **X-ray 前臂（黑+白尺/桡骨）** | 按 `arm_lower:0, 1` 改纯黑+白骨 | T1 | `2 / 2 / 16` (`shock`) | — |
| `arm_lower:5` | `16×50` | 微向左弧弯垂直前臂 | 与 `arm_lower:1` 镜像微弧（sim `0.898`） | T0/T1 | `24 / 36 / 1214` (`emote_waving`, `emoteXL_waving1`, `emoteXL_facepalm`) | — |
| `arm_lower:6` | `52×26` | **向左水平普通前臂（原版 `≡ arm_lower:2`）** | **直接复用 `arm_lower:2`** | T1/T2 | `9 / 12 / 302` (`emote_hat`, `emoteXL_waving4`, `emoteXL_kiss`) | **极易在 Sheet 3 挨着 `3,4` 被错画成 X-ray 骨头，务必保留为正常袖！** |
| `hand:0` | `26×28` | 自然向下微握半拳（偏正/手背朝前） | 独立绘制 | T0 | `186 / 422 / 10486` (`idle_loop`, `emote_sad`, `emote_angry`) | 匹配手腕顶部接入 |
| `hand:1` | `38×29` | 握持工具拳 / 四指并拢侧面拳 | 独立绘制 | T0 | `203 / 510 / 22849` (`idle_loop`, `emote_happycheer`, 重工具持握) | 出场极广，四指与指节需清晰干净 |
| `hand:2` | `35×28` | 自然微张俯抓 / 手背向外小手 | 独立绘制 | T0 | `107 / 173 / 4899` (`idle_shiver_loop`, `emote_pants`, `idle_loop`) | — |
| `hand:3` | `32×30` | **掌心朝前握拳**（拇指横压在卷起的四指指腹上） | 独立绘制 | T0 | `86 / 111 / 2679` (`punch`, `idle_loop`, `toolpunch`) | 严禁画成手背朝前或指节乱切 |
| `hand:4` | `44×36` | 横卧/斜侧面放松松握拳（**原版 `≡ hand:14`**） | 母版（`14` 直接复用 `4`） | T0 | `118 / 199 / 6069` (`idle_loop`, `spearjab`) | — |
| `hand:5` | `32×24` | 侧面指背朝前轻握拳 | 独立绘制 | T0 | `114 / 171 / 4761` (`idle_loop`, `emote_yawn`, `idle_shiver_loop`) | — |
| `hand:6` | `38×42` | 侧面 `C` 形张口捏物 / 翘拇指扶手 | 独立绘制 | T0 | `68 / 90 / 2042` (`idle_sanity_loop`, `build_loop`, `emote_sleepy`) | 防左下方多余畸形指肉块 |
| `hand:7` | `38×38` | 侧张虎口半合手（手心斜向右下/左） | 独立绘制 | T0 | `55 / 84 / 2248` (`idle_inaction_sanity`, `idle_hot_loop`, `emote_hat`) | — |
| `hand:8` | `37×46` | 下垂放松微张指小手（**原版 `≡ hand:16`**） | 母版（`16` 直接复用 `8`） | T0 | `113 / 212 / 7058` (`refuseeat`, `wakeup`, `sleep_loop`) | 画成可爱 Q 版 4–5 指，别画三叉爪 |
| `hand:9` | `45×34` | 向右下微张弧垂放松手 | 独立绘制 | T0 | `115 / 199 / 5820` (`idle_hot_loop`, `wakeup`, `sleep_loop`) | — |
| `hand:10` | `45×48` | 张开大抓握 / 抱手 / 吃惊摆手 | 独立绘制 | T0 | `95 / 149 / 3321` (`dozy`, `emote_hands`, `eat`, `death2`) | 同防尖爪化，拇指朝右、四指微弯向左下 |
| `hand:11` | `26×26` | **X-ray 黑底+白指骨竖拳** | 纯黑+白骨 | T1 | `2 / 2 / 16` (`shock`) | — |
| `hand:12` | `42×34` | **X-ray 黑底+白指骨横握拳** | 纯黑+白骨 | T1 | `2 / 2 / 16` (`shock`) | — |
| `hand:13` | `55×54` | **张开挥手手掌**（拇指朝左，四指舒展；**原版 `≈ hand:17`**） | 母版（`17` 可复用 `13`） | T1 | `30 / 41 / 1034` (`emote_waving`, `emoteXL_loop_dance6`, `coach`) | 注意把 5 指比例画萌画自然 |
| `hand:14` | `44×36` | 横卧放松斜握拳（**原版 `≡ hand:4`**） | **直接复用 `hand:4`** | T1/T2 | `11 / 14 / 314` (`emoteXL_waving4`, `club_set_loop`, `strum`) | 无需单独生图 |
| `hand:15` | `84×151` | **【唯一高危道具】瓦楞牛皮纸板+白色粉笔涂鸦骨锯**（用于摆出“锯自己手臂表示时间到”的戏剧动作） | 独立 4× 矢量合成（纸板 + 白粉笔锯涂鸦 + 顶部抓手） | T2 | `2 / 2 / 112` (`emoteXL_bonesaw`, `player_mount_emotesxl:emoteXL_bonesaw`) | **绝非水杯也非真钢锯！** 白涂鸦锯齿必须在**左侧边缘**，手捏住顶部深色折口，下方透视对准手臂 |
| `hand:16` | `37×46` | 下垂放松小手（**原版 `≡ hand:8`**） | **直接复用 `hand:8`** | T1 | `16 / 26 / 1153` (`pushing_idle`, `graveurn_in`, `emoteXL_waving4`) | 无需单独生图 |
| `hand:17` | `55×54` | 张开挥手手掌（**原版 `≈ hand:13`，sim `0.97`**） | **可直接复用 `hand:13`**（或极细微角度微调） | T1 | `19 / 35 / 868` (`emoteXL_waving1/2`, `pushing_idle`) | 省去一格重绘 |
| `hand:18` | `53×50` | **三指并拢前搭握持 / 扶牛鞍缰绳特写**（正面俯视） | 独立绘制 | T1 | `47 / 137 / 2357` (`player_mount_travel:walk_loop`, `heavy_walk`, `mount:idle_loop`) | 骑乘与扛重物专属指尖，注意三指并排整齐度与肤色一致 |
| `hand:19` | `68×46` | **水平展开搭手 / 抚摸宠物特写**（同风格俯贴近视角） | 由 `hand:18` 扁平横展派生 | T1/T2 | `34 / 115 / 2546` (`player_mount_groggy`, `pet_big`, `pet_small`) | 与 `18` 画风统一 |

### 4.6 腿部 `leg:0..10` 与鞋子 `foot:0..7`（以及 `SWAP_ICON:0`）

| 槽位 | 原版尺寸 | 真实视觉含义与朝向 | 可复用 / 派生方案 | 常用度 | 实测调用 (`包/剪辑/帧`) & 代表动画 | 制作避坑提示 |
| :--- | :--- | :--- | :--- | :---: | :--- | :--- |
| `leg:0` | `18×46` | 正面标准立柱小腿（含长袜与大腿露肤） | 直腿母版（与 `1,2,3,5,8` 同款微变角度） | T0 | `196 / 483 / 16360` (`refuseeat`, `emote_sad`, `idle_loop`) | 严格控制长度与大腿露肤比例匹配原版条纹边界 |
| `leg:1` | `20×51` | 微微向左斜倾直腿 | 同系列微调 | T0 | `91 / 157 / 3996` (`wakeup`, `idle_loop`, `sleep_loop`) | — |
| `leg:2` | `16×52` | 稍细微长直腿 | 同系列微调 | T0 | `85 / 148 / 3663` (`idle_loop`, `idle_shiver_loop`, `hit`) | — |
| `leg:3` | `21×54` | 前跨略宽微斜直腿 | 同系列微调 | T0 | `92 / 158 / 4144` (`idle_loop`, `idle_inaction`, `idle_shiver_loop`) | — |
| `leg:4` | `39×38` | **`L` 形 90° 弯曲跪坐/抬膝腿**（朝左折） | 核心弯腿 | T0 | `102 / 192 / 6321` (`emote_angry`, `emote_hands`, `club_putt_charged`, `punch`) | 膝关节过渡自然，短袜口不脱节 |
| `leg:5` | `16×56` | 纤细略长笔直伸展腿（开礼物/断线/弹琴） | 近似 `leg:0/2` 拉长微调 | T2/T3 | `5 / 5 / 78` (`gift_open_loop`, `line_snap`, `idle_inaction`) | 极少出现 |
| `leg:6` | `41×44` | **向左侧大角度折膝奔跑/跨步腿** | 核心奔跑弯腿 | T0 | `123 / 199 / 6822` (`idle_hot_loop`, `eat`, `shovel_loop`, `run_loop`) | 高频用于下蹲和奔跑抬脚 |
| `leg:7` | `33×40` | **向右侧深屈膝蹲坐腿**（近乎折回） | 核心深蹲腿 | T1 | `19 / 24 / 687` (`run_loop`, `death`, `emote_loop_sit1`) | `emote_loop_sit1` 与高速奔跑用 |
| `leg:8` | `21×53` | 微弯向内弧直腿（研究/死亡/砍树） | 直腿系列微弯 | T0 | `76 / 109 / 1565` (`research`, `death`, `chop_loop`) | — |
| `leg:9` | `18×46` | **X-ray 直腿（黑+白股骨/胫骨）** | `leg:0` 轮廓填纯黑+白腿骨 | T1 | `2 / 2 / 16` (`shock`) | — |
| `leg:10` | `21×54` | **X-ray 粗直腿（黑+白腿骨）** | `leg:3` 轮廓填纯黑+白腿骨 | T1 | `2 / 2 / 16` (`shock`) | — |
| `foot:0` | `28×17` | **脚尖朝右**站立鞋（最常用基准鞋） | 核心鞋母版 A | T0 | `220 / 555 / 21275` (`idle_loop`, `idle_hot_loop`, `refuseeat`) | 注意脚踝白袜边与搭扣方向 |
| `foot:1` | `30×20` | **脚尖朝左**斜向下方鞋（透视仰/俯过渡） | 可水平镜像参照 `foot:0` 后调角度 | T0 | `140 / 238 / 7261` (`eat`, `death`, `idle_loop`) | 与 `foot:2` 高度相近 (`sim 0.903`) |
| `foot:2` | `31×20` | **脚尖朝左**标准水平鞋（`foot:0` 对称脚） | **与 `foot:0` 水平镜像参考（mirror sim `0.929`）** | T0 | `114 / 196 / 5966` (`wakeup`, `idle_loop`, `sleep_loop`) | — |
| `foot:3` | `29×24` | 脚尖向左上仰起 / 倾斜侧鞋 | 独立微旋转变形 | T0 | `108 / 185 / 5652` (`idle_loop`, `idle_inaction`, `idle_hot_loop`) | — |
| `foot:4` | `25×26` | **垂直踮脚脚尖向下斜踏**（鞭打/踮脚/丢回旋镖） | 独立绘制 | T2/T3 | `7 / 7 / 62` (`careful_walk`, `whip`, `throw`) | 极其罕见，打包用 `whip_f5` 覆盖测试 |
| `foot:5` | `32×16` | 朝右平坦微弓侧面踩地鞋 | 近似 `foot:0` 扁平版 | T0/T1 | `63 / 107 / 3173` (`shovel_loop`, `pickaxe_loop`, `wakeup`, `sleep_loop`) | 挥锹/挖矿常用 |
| `foot:6` | `30×22` | **双翘/弯底正面脚掌鞋底抬起** | 独立绘制 | T1/T2 | `31 / 36 / 862` (`emote_strikepose`, `spearjab`, `shieldparry_loop`) | 格挡和摆 pose 时脚底翘起角度 |
| `foot:7` | `32×16` | **X-ray 鞋（黑底+白趾骨/跖骨）** | `foot:5` 轮廓填纯黑+白足骨 | T1 | `2 / 2 / 16` (`shock`) | — |
| `SWAP_ICON:0` | `160×189` | 角色头像图标（非骨骼动画绘制，由 `--icon` 读取自动缩放居中） | 自动读取项目 `logo-character.png` | T3 | `0 / 0 / 0`（UI/物品格切换头像） | 打包脚本自动处理，不放进绿幕生图网格 |
