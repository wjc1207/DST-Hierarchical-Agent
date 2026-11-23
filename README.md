# DST Hierarchical Agent

[English](README.EN.md) · [系统架构](docs/ARCHITECTURE.md) · [技术说明](docs/TECHNICAL.md) · [创意工坊指南](docs/WORKSHOP.md)

本仓库主线提供一套统一的《饥荒联机版》服务端智能体模组 **`DST Buddy Agent`**（[mods/dst_buddy_agent](mods/dst_buddy_agent)）与 `python/` 下的共用规划和任务执行框架。

| 模组 | 形态与定位 | 安装说明 |
| --- | --- | --- |
| **DST Buddy Agent** | **统一服务端智能体 Mod（主线唯一推荐，v0.9.0+）** · 单个 Mod 同时支持 **AI 托管主角自己（`/buddy self`、`@自己`）** 与 **召唤 1~2 名可命名 AI 伙伴（`buddy1`/`buddy2`）**；支持局部视野 `scan_radius` 与可选模拟延迟 `action_delay_ms` 供 Benchmark 使用（见[系统架构](docs/ARCHITECTURE.md#架构决策统一服务端智能体核心self--buddy1--buddy2与可配置仿真约束)） | [快速开始](#快速开始统一服务端) |
| **历史纯客户端归档** | 已彻底解耦迁至 [`archive/`](archive/README.md)（原纯客户端 Mod `dst_player_agent` 及重命名后的 `player_server.py`/`player_agent.py`/`player_bridge.py`/`player_planner.py`/`player_probe.py` 与独立测试套件） | [历史归档说明](#历史归档archive) |

《饥荒联机版》智能体实验项目。DeepSeek 维护有优先级的多目标看板，执行器优先完成眼前任务，并结合 Klei 区域资源配置与探索记忆寻找所需物资。MCP 服务调度看板并处理紧急安全动作，Lua 模组执行和验证动作。

已完成的阶段：[活过第一晚](https://bilibili.com/video/BV1aehE6DEji) · [制作科学机器](https://www.bilibili.com/video/BV1mFaV6aEQ2) · [制作科学机器v2](https://www.bilibili.com/video/BV1pRnR62Ex3) · 伙伴模式实机演示：[和两个 AI 队友在饥荒里过一天](https://www.bilibili.com/video/BV187Wb6oEUU)

## 快速开始（统一服务端）

1. 将仓库中的 `mods/dst_buddy_agent` 复制到《饥荒联机版》的 `mods` 目录，在游戏的“模组”菜单启用 **DST Buddy Agent**（如从 Steam 创意工坊订阅，直接打开对应目录运行 `1_install_agent.bat/.sh` 即可，无需克隆仓库）。
2. 将 `.env.example` 复制为 `.env`，填写 `DEEPSEEK_API_KEY`。不要分享或提交 `.env`。
3. 需要 Python 3.11+。在项目目录运行（也可以在自己的虚拟环境中安装）：

```powershell
python -m pip install -r python/requirements-mcp.txt
$dstMods = "C:\Program Files (x86)\Steam\steamapps\common\Don't Starve Together\mods"
python .\python\mcp_server\companion_server.py --configure-only --write-mod-config (Join-Path $dstMods "dst_buddy_agent\scripts\jev_companion_config.lua")
```

4. 在项目文件夹中打开 PowerShell 启动控制循环（同时控制 `/buddy self` 主角托管与 `/buddy summon` 伙伴；或直接打印 `/buddy pair` 口令在游戏聊天框粘贴配对）：

```powershell
python.exe .\python\mcp_agent\companion_agent.py --execute
```

按 `Ctrl+C` 停止。也可以直接双击根目录 `setup_and_run.bat`（macOS / Linux 运行 `./setup_and_run.sh`）一键配置依赖并启动。

[MCP服务器](docs/MCP.md)提供纯游戏 I/O 接口，上层可接自研 DeepSeek Harness，也可通过 `--backend claude|codex|dsh|dsh-http` 事件驱动回调外部 Agent CLI，或在 DSH / Codex 桌面端中直接作为 MCP 服务挂载。详见[Agent 记忆、上下文压缩与外部 Agent 回调](docs/AGENT_MEMORY_AND_BACKENDS.md)。

[角色操控 HUD 与聊天](docs/DIALOGUE.md)：在游戏右侧切换指令／自主、编辑长期目标或暂停接管。开启聊天监听后，用 `@自己 需求` 在游戏聊天框派活，执行中发新消息会在后台异步规划并平滑更新；显式发送 `停止/暂停/取消` 或点击暂停立即接管。

[服务端伙伴与主角托管](docs/COMPANION.md)：统一支持**主角托管（`/buddy self`、`@自己`）**与[两个命名伙伴联合规划](docs/COMPANION_TEAM.md)，`/buddy summon 名字` 自行生成并绑定威尔逊，无需其他模组；用 `@自己` / `@名字` / `@全体` 指定指令，各角色独立执行；支持跟随（伙伴）、拾取／采集、装备／卸下、砍树、挖矿、制作建造、箱子／冰箱／龙蝇箱批量存取、添燃料（火堆与吸热火堆及硝石）、设家驻留、烹饪锅多菜谱料理（肉丸、蔬菜杂烩、饺子、培根煎蛋、肉汤、蜜汁火腿、蜜汁卤肉、太妃糖、什锦干果、火龙果派）、烧树制炭、基础战斗与护主（自动换装护甲与武器、低血撤退）、医疗包扎恢复（`heal` / `health_ratio`）以及完整四季换季生存与自主救急（`thermal_comfort` 自动制作并穿戴冬帽/草帽/花伞等、制作与烤冷暖石 `heatrock`、自建营火或吸热火堆防寒避暑、春雨避雨烘干，并在冻伤/过热/淋湿时主动触发救急重规划）。并在 Mod 设置提供局部视野 `scan_radius`（10/15/20）与可选延迟 `action_delay_ms`（0~350ms）用于模拟客户端约束。模型费用由使用伙伴的各客户端承担。见[动作实测说明](docs/COMPANION_ACTIONS.md)。

[共用规划与任务层](docs/SHARED_PLANNING.md)：客户端与伙伴共用模型请求、上下文和完成规则、优先级看板与任务调度，分别适配状态和动作。

[建筑记忆、前往建筑与设置／返回家](docs/PLACES.md)由优先级看板调度。

[看图选址与前往坐标](docs/MAP_SELECTION.md)：DeepSeek 按需查看带世界坐标的已观察地图，
选择营地候选点；`move_to` 负责安全前往，到场建建筑后再设置家。

[营火／石头火堆添燃料](docs/REFUELING.md)：DS 指定建筑、常用燃料和份数，逐份原生执行并验证；支持熄灭火堆重新点火。

[营地建筑位置约束](docs/PLACEMENT.md)：建筑任务绑定已有建筑锚点和半径，先到位再建造，范围外同类建筑不能满足任务。

[任务时间与驻留](docs/TASK_TIME.md)：普通任务可设置开始时间；驻留任务可以到点回家，持续停留指定游戏时长或直到天黑／天亮，并在范围内完成兼容工作。饥饿目标可选 `duration`，在指定游戏时长内持续监测并补食；[添燃料](docs/REFUELING.md)也支持比例目标和持续维护，客户端明确显示保守估算区间。

[性能说明与 FPS 日志](docs/PERFORMANCE.md)：默认每 5 秒记录客户端帧率估计，不再自动打印完整状态。

[温度安全与火势控制](docs/THERMAL.md)：记录体温与火势，按需暂停添料，体温过高时沿安全照明路线避让。

## 打包与创意工坊

在仓库根目录运行 `python python/tools/package_mods.py`，同时生成以下分发产物：
- **Steam Mod Uploader 上传目录（自包含离线安装包）**：`dist/workshop/dst_buddy_agent/`（内含 Lua 模组、`preview.jpg`、空白无令牌配置、一键安装与运行脚本 `1_install_agent.bat/.sh` + `2_run_buddy.bat/.sh` 及离线 `runner/` 源码，工坊用户无需另去 GitHub 下载，游戏内输入 `/buddy setup` 即可查看精确目录）
- **GitHub Releases 一键运行包**：`dist/dst_agent_runner.zip`（含 `setup_and_run.bat`、`setup_and_run.sh`、`scripts/launcher.ps1` 与本机 Python 控制器）
- **离线模组压缩包**：`dist/dst_buddy_agent.zip`

GitHub 用户也可直接在 Windows PowerShell 运行一行命令或双击仓库根目录的 `setup_and_run.bat`（macOS / Linux 运行 `./setup_and_run.sh`）：

```powershell
irm https://raw.githubusercontent.com/wjc1207/DST-Hierarchical-Agent/main/install.ps1 | iex
```

完整发布步骤与商店页双语文案见 [docs/WORKSHOP.md](docs/WORKSHOP.md)。

从旧名称升级时，将旧游戏模组目录里的 `scripts/jev_bridge_config.lua`（玩家操控）或 `scripts/jev_companion_config.lua`（伙伴）复制到对应新目录的 `scripts/` 下，保留原令牌；或直接在游戏内使用新的 `/agent pair` / `/buddy pair` 命令配对。进入存档前，在模组菜单停用旧模组并启用新模组。

## 测试与文档

无需启动游戏即可运行离线回归测试：

```powershell
python.exe .\python\run_tests.py
```

### 无头真引擎 E2E 沙箱（Klei `nullrenderer`）

无需打开饥荒图形界面，直接调用本机 Steam 安装的 `dontstarve_dedicated_server_nullrenderer_x64.exe` 在真实 Klei C++/Lua 引擎里跑自动化实机回归（完全隔离在 `logs/headless_sandbox/`，不触碰真实存档和 Steam `mods/` 目录）：

```powershell
# 运行 13 场景严格单场景隔离的全自动真引擎 E2E 回归（配对、采砍挖、烧桦树制炭、批量存取+交付、添料+煮肉丸、设家+驻留、自动穿甲持矛清蜘蛛、饺子+蜜汁火腿双菜连煮、主角托管+@自己战斗与延迟模拟、冬帽+药膏回血取暖、夏装打伞+硝石吸热火坑降温、冬季造冬帽与暖石烘烤、夏造草帽自建吸热火+春雨造花伞烘干）
python .\python\tools\headless_sandbox.py --suite

# 仅跑单个场景（带详细动作回执）
python .\python\tools\headless_sandbox.py --suite --only ignite_tree_charcoal --verbose

# 启动无头常驻测试服（127.0.0.1:8766），可在另一终端直接接 companion_agent.py --execute 联调大模型
python .\python\tools\headless_sandbox.py --serve --port 8766
```

## 历史归档（`archive/`）

原纯客户端基准模组 `dst_player_agent` 及单人客户端专属 Python 模块已重命名并冻结归档至 [`archive/`](archive/README.md)，与主线 `python/` 之间完全零双向依赖（不再随主线 `package_mods.py` 打包）：
- `archive/mods/dst_player_agent`：纯客户端 Lua 模组
- `archive/python/mcp_server/player_bridge.py`、`player_server.py`、`player_task_service.py`：纯客户端桥接与 MCP 服务（原 `game_bridge.py` / `mcp_server.py`）
- `archive/python/mcp_agent/player_agent.py`、`player_planner.py`、`player_probe.py`、`map_view.py`、`model_state.py`：原单人控制器、规划器与探活脚本（原 `mcp_agent.py` / `daily_planner.py` / `mcp_probe.py`）
- `archive/python/tools/client_e2e_sandbox.py`：原 800×450 静音小窗真客户端 6 场景 E2E 沙箱；可通过 `python archive/python/run_tests.py` 独立运行归档回归测试

状态格式、动作规则、候选裁剪、探索导航与规划机制见[技术说明](docs/TECHNICAL.md)。生存与探索策略可以在 [DSTKnowledge.txt](data/DSTKnowledge.txt) 中编辑；配方材料、科技和可制作性以 `query_recipes` 实时查询为准，不在知识文件中维护重复配方表。

灵感来自 [TerraBlind](https://github.com/Reisenbug/TerraBlind)。
