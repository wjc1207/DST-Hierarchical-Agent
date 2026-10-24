# 技术说明（1.8.8，MCP-only）

[项目首页](../README.md) · [English](TECHNICAL.EN.md)

当前只保留 MCP → Lua 原生执行。旧键鼠输入、窗口查找／聚焦、游戏日志轮询、预测动作缓冲及其专属测试已移除。游戏日志仍可用于人工排查，但不再作为 Python 的控制通道。

## 目录与职责

```text
mcp_agent/   模型调用、规划、优先级看板、搜索意图、连接检查
mcp_server/  MCP 工具、状态缓存、动作队列、本机 HTTP 桥接
common/      游戏事实规则、看板格式、配置路径、时间测量
scripts/     Lua 感知、地图寻路、原生执行、实时安全验证
data/        DSTKnowledge.txt、Klei 区域资源配置快照
tests/       当前 MCP、共享规则及 Lua 的离线回归
tools/       从本地原版代码提取资源配置
docs/        技术说明
```

根目录保留 Mod 入口、README、依赖文件和测试入口。`.env` 与 `logs/` 位置不变。

| 职责 | 实现 |
| --- | --- |
| 长期需求、优先级、依赖、预算、后台复核 | `mcp_agent/daily_planner.py` |
| 现场机会、安全变化时选择本地动作 | `mcp_agent/jev_client.py` |
| 先完成可见任务，再为最高优先任务寻找来源 | `mcp_agent/priority_board.py` |
| 选择搜索目标、区域目的地、已知来源／未知分支 | `mcp_agent/navigation_intents.py` |
| 两个模型使用同一事实投影 | `mcp_agent/model_state.py` |
| 通行检查、区域内前沿、绕障、路线与连续移动 | `modmain.lua`、`scripts/jev_exploration.lua` |
| 动作合法性、接近、采集／制作、验证和紧急中断 | `scripts/jev_native_executor.lua` |

Python 决定为何、往哪里寻找资源；Lua 决定具体怎么走。`navigation_intents.py` 只调度 Mod 提供的合法导航选项，不计算人物行走路径。共享规则不调用游戏、窗口或操作系统输入接口。

## 调度、导航与安全

完整看板格式、搜索预算和 Klei 先验见 [目标与导航设计](GOAL_NAVIGATION.md)。DS 后台维护持久优先级目标；每 1/4 天复核，不清空有效目标和进度。执行器先选可见任务，没有可推进任务时，查位置记忆、已知来源区域、生成先验，再探索未知分支。Lua 连续执行移动和原生动作；危险或状态变化可立即中断。JEV 处理安全和有意义的计划外机会，不逐棵决定看板内采集。

每次动作使用作出选择时的 `observation_id` 和选项名称；服务检查所有权和时效，Lua 根据实时状态最终验证。排队／运行不等于完成，必须等待原生结果。同一执行器同时只执行一个动作。

纯语义动作序列为已有 MCP 调用方兼容，不包含旧键鼠路径。新的 DS 输出是优先级看板。外部看板工具保留显式自动执行语义，不能与 Python 看板调度竞争控制权。

## 启动、配置与排查

从项目根目录运行：

```powershell
python .\mcp_agent\mcp_probe.py
python .\mcp_agent\mcp_agent.py --loop --execute
```

自动循环自行启动 `mcp_server/mcp_server.py`，不要再运行同端口服务或 Codex 的 DST MCP 连接。工具契约见 [MCP 说明](MCP.md)。

- 模型密钥：根目录 `.env`，也可用 `--env` 指定。
- 桥接令牌：`logs/mcp_bridge.json` 与安装的 `scripts/jev_bridge_config.lua`；升级保留原文件。
- 规划知识：[data/DSTKnowledge.txt](../data/DSTKnowledge.txt)。
- 区域先验：[data/worldgen_resources.json](../data/worldgen_resources.json)，不是当前存档的实体清单。
- 规划进度：`logs/task_board.json`、`logs/task_board.jsonl`。
- 计时：`logs/mcp_timing.jsonl`、`logs/mcp_transport.jsonl`。

Python 通过 MCP 读取状态，不轮询 `client_log.txt`。Mod 日志是诊断，不是另一套控制接口。旧数字小键盘和 F 键动作入口也已移除，取消使用 MCP `cancel_action`。

升级时复制 `mcp_agent/`、`mcp_server/`、`common/`、`data/`、Mod 入口和 `scripts/`，保留原配置和日志，重新进入世界并重启 Python。不要覆盖 `.env` 和生成的桥接配置。

## 测试

运行 `python run_tests.py`。测试使用假模型、假游戏或本地协议模拟，不调用真实模型或操作游戏。Lua 检查可用 Fengari 运行 `tests/exploration_spec.lua`，直接调用原生动作句柄，不注册键盘事件。
