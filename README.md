# DST Hierarchical Agent

[English](README.EN.md) · [技术说明](docs/TECHNICAL.md)

本仓库包含两套《饥荒联机版》模组，共用 `python/` 下的规划和任务执行代码。

| 模组 | 用途 | 安装说明 |
| --- | --- | --- |
| DST Player Agent | AI 操作玩家自己的角色 | [玩家操作快速开始](#玩家操作快速开始) |
| DST Buddy Agent | 召唤一个或两个 AI 伙伴 | [伙伴安装说明](#ai伙伴快速开始) |

《饥荒联机版》智能体实验项目。DeepSeek 维护有优先级的多目标看板，执行器优先完成眼前任务，并结合 Klei 区域资源配置与探索记忆寻找所需物资。Lua Mod 原生执行并验证动作，JEV 处理危险，并在 DeepSeek 规划空窗期临时接手。长期目标是制作炼金引擎，当前已支持科学机器，并新增 DeepSeek 侧的[通用配方查询、制作与放置](docs/RECIPES.md)；炼金引擎尚待完整实机验证。

已完成的阶段：[活过第一晚](https://bilibili.com/video/BV1aehE6DEji) · [制作科学机器](https://www.bilibili.com/video/BV1mFaV6aEQ2)  · [制作科学机器v2](https://www.bilibili.com/video/BV1pRnR62Ex3)

伙伴模式实机演示：[和两个 AI 队友在饥荒里过一天](https://www.bilibili.com/video/BV187Wb6oEUU)

## 玩家操作快速开始

1. 将仓库中的 `mods/dst_player_agent` 复制到《饥荒联机版》的 `mods` 目录，在游戏的“模组”菜单启用 **DST Player Agent**。
2. 将 `.env.example` 复制为 `.env`，填写 `JEV_API_KEY`。如需启用规划，再填写 `DEEPSEEK_API_KEY`。不要分享或提交 `.env`。
3. 需要 Python 3.11+。在项目目录运行（也可以在自己的虚拟环境中安装）：

```powershell
python -m pip install -r python/requirements-mcp.txt
$dstMods = "C:\Program Files (x86)\Steam\steamapps\common\Don't Starve Together\mods"
python .\python\mcp_server\mcp_server.py --configure-only --write-mod-config (Join-Path $dstMods "dst_player_agent\scripts\jev_bridge_config.lua")
```
4. 在项目文件夹中打开 PowerShell，先检查连接，再启动控制循环：

```powershell
python.exe .\python\mcp_agent\mcp_probe.py
python.exe .\python\mcp_agent\mcp_agent.py --loop --execute
```

最后一条命令会向游戏发送实际操作；按 `Ctrl+C` 停止。若只想观察决策而不操作游戏，去掉 `--execute`。

## AI伙伴快速开始

1. 将仓库中的 `mods/dst_buddy_agent` 复制到《饥荒联机版》的 `mods` 目录，在游戏的“模组”菜单启用 **DST Buddy Agent**。
2. 将 `.env.example` 复制为 `.env`，填写 `DEEPSEEK_API_KEY`。不要分享或提交 `.env`。
3. 需要 Python 3.11+。在项目目录运行（也可以在自己的虚拟环境中安装）：

```powershell
python -m pip install -r python/requirements-mcp.txt
$dstMods = "C:\Program Files (x86)\Steam\steamapps\common\Don't Starve Together\mods"
python .\python\mcp_server\companion_server.py --configure-only --write-mod-config (Join-Path $dstMods "dst_buddy_agent\scripts\jev_companion_config.lua")
```

4. 在项目文件夹中打开 PowerShell，先检查连接，再启动控制循环：

```powershell
python.exe .\python\mcp_agent\companion_agent.py --execute
```

最后一条命令会向游戏发送实际操作；按 `Ctrl+C` 停止。

[MCP服务器](docs/MCP.md)提供面向agent的接口，可以连接原生agent、DSH、CodeX或者Claude Code.

[角色操控 HUD 与聊天](docs/DIALOGUE.md)：在游戏右侧切换指令／自主、编辑长期目标或暂停接管。开启聊天监听后，用 `@自己 需求` 在游戏聊天框派活。指令完成后待命，自主模式持续生存。

[服务端伙伴](docs/COMPANION.md)：支持[两个命名伙伴与统一规划](docs/COMPANION_TEAM.md)，`/buddy summon 名字` 自行生成并绑定威尔逊，无需其他模组；用 `@名字` 指定指令，各伙伴独立执行；支持跟随、拾取／采集、装备、砍树、挖矿、普通配方制作及陆地建造。模型费用由使用伙伴的各客户端承担。见[动作实测说明](docs/COMPANION_ACTIONS.md)。

[共用规划与任务层](docs/SHARED_PLANNING.md)：客户端与伙伴共用模型请求、上下文和完成规则、优先级看板与任务调度，分别适配状态和动作。

[建筑记忆、前往建筑与设置／返回家](docs/PLACES.md)由优先级看板调度，JEV 候选范围保持不变。

[看图选址与前往坐标](docs/MAP_SELECTION.md)：DeepSeek 按需查看带世界坐标的已观察地图，
选择营地候选点；`move_to` 负责安全前往，到场建建筑后再设置家。

[营火／石头火堆添燃料](docs/REFUELING.md)：DS 指定建筑、常用燃料和份数，逐份原生执行并验证；支持熄灭火堆重新点火。

[营地建筑位置约束](docs/PLACEMENT.md)：建筑任务绑定已有建筑锚点和半径，先到位再建造，范围外同类建筑不能满足任务。

[任务时间与驻留](docs/TASK_TIME.md)：普通任务可设置开始时间；驻留任务可以到点回家，持续停留指定游戏时长或直到天黑／天亮，并在范围内完成兼容工作。饥饿目标可选 `duration`，在指定游戏时长内持续监测并补食；[添燃料](docs/REFUELING.md)也支持比例目标和持续维护，客户端明确显示保守估算区间。

[性能说明与 FPS 日志](docs/PERFORMANCE.md)：默认每 5 秒记录客户端帧率估计，不再自动打印完整状态。

[温度安全与火势控制](docs/THERMAL.md)：记录体温与火势，按需暂停添料，体温过高时沿安全照明路线避让。

## 打包

在仓库根目录运行 `python python/tools/package_mods.py`，生成 `dist/dst_player_agent.zip` 和 `dist/dst_buddy_agent.zip`。首次安装时，把所选压缩包解压到游戏的 `mods` 目录，再按对应安装说明配置桥接。Python 控制器从同一仓库启动。

从旧名称升级时，将旧游戏模组目录里的 `scripts/jev_bridge_config.lua`（玩家操控）或 `scripts/jev_companion_config.lua`（伙伴）复制到对应新目录的 `scripts/` 下，保留原令牌。进入存档前，在模组菜单停用旧模组并启用新模组。

## 测试与文档

无需启动游戏即可运行离线回归测试：

```powershell
python.exe .\python\run_tests.py
```

状态格式、动作规则、候选裁剪、探索导航与规划机制见[技术说明](docs/TECHNICAL.md)。生存与探索策略可以在 [DSTKnowledge.txt](data/DSTKnowledge.txt) 中编辑；配方材料、科技和可制作性以 `query_recipes` 实时查询为准，不在知识文件中维护重复配方表。

灵感来自 [TerraBlind](https://github.com/Reisenbug/TerraBlind)。
