# JEV DST Agent

[English](README.EN.md) · [技术说明](docs/TECHNICAL.md)

《饥荒联机版》智能体实验项目。DeepSeek 维护有优先级的多目标看板，执行器优先完成眼前任务，并结合 Klei 区域资源配置与探索记忆寻找所需物资。Lua Mod 原生执行并验证动作，JEV 处理紧急变化和计划外机会，不必逐次决定计划内采集。长期目标是制作炼金引擎，当前已支持科学机器。

已完成的阶段：[活过第一晚](https://bilibili.com/video/BV1aehE6DEji) · [制作科学机器](https://www.bilibili.com/video/BV1mFaV6aEQ2)

## 快速开始

1. 将整个 `jev_dst_agent` 文件夹复制到《饥荒联机版》的 `mods` 目录，在游戏的“模组”菜单启用 **JEV DST Agent**，然后进入本地或私人世界。
2. 将 `.env.example` 复制为 `.env`，填写 `JEV_API_KEY`。如需启用规划，再填写 `DEEPSEEK_API_KEY`。不要分享或提交 `.env`。
3. 完成 [MCP 连接配置](docs/MCP.md)，在项目文件夹中打开 PowerShell，先检查连接，再启动控制循环：

```powershell
python.exe .\mcp_agent\mcp_probe.py
python.exe .\mcp_agent\mcp_agent.py --loop --execute
```

最后一条命令会向游戏发送实际操作；按 `Ctrl+C` 停止。若只想观察决策而不操作游戏，去掉 `--execute`。

## 执行方式

优先级看板使用 MCP 主循环，由 Lua 原生执行和验证动作，不依赖模拟键鼠。完成[连接配置](docs/MCP.md)并更新程序后运行：

```powershell
python .\mcp_agent\mcp_agent.py --loop --execute
```

它会自行启动 MCP 服务；先关闭 `--bridge-only`、检查器、Codex 的 DST MCP 连接及旧控制循环，不要重复启动。新版看板仅由 MCP 主循环调度。安装、看板格式、导航策略和当前限制见 [MCP 使用说明](docs/MCP.md)。

## 测试与文档

无需启动游戏即可运行离线回归测试：

```powershell
python.exe .\run_tests.py
```

状态格式、动作规则、候选裁剪、探索导航与规划机制见[技术说明](docs/TECHNICAL.md)。规划用的游戏知识可以在 [DSTKnowledge.txt](data/DSTKnowledge.txt) 中编辑。

灵感来自 [TerraBlind](https://github.com/Reisenbug/TerraBlind)。
