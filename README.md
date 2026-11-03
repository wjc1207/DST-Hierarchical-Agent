# JEV DST Agent

[English](README.EN.md) · [技术说明](docs/TECHNICAL.md)

《饥荒联机版》智能体实验项目。DeepSeek 维护有优先级的多目标看板，执行器优先完成眼前任务，并结合 Klei 区域资源配置与探索记忆寻找所需物资。Lua Mod 原生执行并验证动作，JEV 处理危险，并在 DeepSeek 规划空窗期临时接手。长期目标是制作炼金引擎，当前已支持科学机器，并新增 DeepSeek 侧的[通用配方查询、制作与放置](docs/RECIPES.md)；炼金引擎尚待完整实机验证。

已完成的阶段：[活过第一晚](https://bilibili.com/video/BV1aehE6DEji) · [制作科学机器](https://www.bilibili.com/video/BV1mFaV6aEQ2)  · [制作科学机器v2](https://www.bilibili.com/video/BV1pRnR62Ex3)

## 快速开始

1. 将整个 `jev_dst_agent` 文件夹复制到《饥荒联机版》的 `mods` 目录，在游戏的“模组”菜单启用 **JEV DST Agent**，然后进入本地或私人世界。
2. 将 `.env.example` 复制为 `.env`，填写 `JEV_API_KEY`。如需启用规划，再填写 `DEEPSEEK_API_KEY`。不要分享或提交 `.env`。
3. 完成 [MCP 连接配置](docs/MCP.md)，在项目文件夹中打开 PowerShell，先检查连接，再启动控制循环：

```powershell
python.exe .\mcp_agent\mcp_probe.py
python.exe .\mcp_agent\mcp_agent.py --loop --execute
```

最后一条命令会向游戏发送实际操作；按 `Ctrl+C` 停止。若只想观察决策而不操作游戏，去掉 `--execute`。

[建筑记忆、前往建筑与设置／返回家](docs/PLACES.md)由优先级看板调度，JEV 候选范围保持不变。

[看图选址与前往坐标](docs/MAP_SELECTION.md)：DeepSeek 按需查看带世界坐标的已观察地图，
选择营地候选点；`move_to` 负责安全前往，到场建建筑后再设置家。

[营火／石头火堆添燃料](docs/REFUELING.md)：DS 指定建筑、常用燃料和份数，逐份原生执行并验证；支持熄灭火堆重新点火。

[营地建筑位置约束](docs/PLACEMENT.md)：建筑任务绑定已有建筑锚点和半径，先到位再建造，范围外同类建筑不能满足任务。

[任务时间与驻留](docs/TASK_TIME.md)：普通任务可设置开始时间；驻留任务可以到点回家，持续停留指定游戏时长或直到天黑／天亮，并在范围内完成兼容工作。饥饿目标可选 `duration`，在指定游戏时长内持续监测并补食；[添燃料](docs/REFUELING.md)也支持比例目标和持续维护，客户端明确显示保守估算区间。

## 执行方式

优先级看板使用 MCP 主循环，由 Lua 原生执行和验证动作，不依赖模拟键鼠。完成[连接配置](docs/MCP.md)并更新程序后运行：

```powershell
python .\mcp_agent\mcp_agent.py --loop --execute
```

它会自行启动 MCP 服务；先关闭 `--bridge-only`、检查器、Codex 的 DST MCP 连接及旧控制循环，不要重复启动。新版看板仅由 MCP 主循环调度。任务可设置 `focus: true`（最多两个），专注期间只推进这些任务及其前置任务；普通模式允许顺路完成其他看板工作。搜索预算耗尽后会冷却并自动重试。安装、看板格式、导航策略和当前限制见 [MCP 使用说明](docs/MCP.md)。

[性能说明与 FPS 日志](docs/PERFORMANCE.md)：默认每 5 秒记录客户端帧率估计，不再自动打印完整状态。

[温度安全与火势控制](docs/THERMAL.md)：记录体温与火势，按需暂停添料，体温过高时沿安全照明路线避让。

## 测试与文档

无需启动游戏即可运行离线回归测试：

```powershell
python.exe .\run_tests.py
```

状态格式、动作规则、候选裁剪、探索导航与规划机制见[技术说明](docs/TECHNICAL.md)。生存与探索策略可以在 [DSTKnowledge.txt](data/DSTKnowledge.txt) 中编辑；配方材料、科技和可制作性以 `query_recipes` 实时查询为准，不在知识文件中维护重复配方表。

灵感来自 [TerraBlind](https://github.com/Reisenbug/TerraBlind)。
