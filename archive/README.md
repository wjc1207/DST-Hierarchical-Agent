# Legacy Client-Only Baseline Archive (`archive/`)

本目录保存历史纯客户端模组 **`dst_player_agent`（v1.9.1）** 及其客户端专用 Python 入口、冻结依赖与回归测试脚本，**与主线 `python/` 之间零双向依赖**，可完全独立运行。

从 `DST Buddy Agent v0.9.0` 起，主线已统一为单服务端智能体核心（`mods/dst_buddy_agent`），一个 Mod 同时支持**主角托管（`/buddy self`、`@自己`）**与**1~2 名 AI 威尔逊伙伴（`buddy1`/`buddy2`）**；原纯客户端专属实现及 `game_bridge`/`daily_planner`/`map_view`/`map_icons`/`model_state` 已重命名为 `player_*` 并冻结归档至此。

## 归档内容

- [archive/mods/dst_player_agent](mods/dst_player_agent)：纯客户端 Lua 模组（`playercontroller` + `SendRPCToServer`，无服务端侵入基准）
- [archive/python/mcp_server/player_bridge.py](python/mcp_server/player_bridge.py)：原单人客户端 HTTP 桥接与动作状态机（原 `game_bridge.py`，`PlayerBridge` / `GameBridge`）
- [archive/python/mcp_server/player_server.py](python/mcp_server/player_server.py)：纯客户端桥接 MCP 服务（原 `mcp_server.py`，默认端口 `8765`，`/agent pair`）
- [archive/python/mcp_server/player_task_service.py](python/mcp_server/player_task_service.py)：纯客户端单人看板调度宿主
- [archive/python/mcp_agent/player_agent.py](python/mcp_agent/player_agent.py)：纯客户端自主/指令控制器与 DeepSeek 调度入口（原 `mcp_agent.py`）
- [archive/python/mcp_agent/player_planner.py](python/mcp_agent/player_planner.py)：原单人规划器（原 `daily_planner.py`）与冻结配套 `map_view.py` / `map_icons.py` / `model_state.py`
- [archive/python/mcp_agent/player_probe.py](python/mcp_agent/player_probe.py)：纯客户端只读探活脚本（原 `mcp_probe.py`）
- [archive/python/tools/client_e2e_sandbox.py](python/tools/client_e2e_sandbox.py)（含独立 [\_nullrenderer.py](python/tools/_nullrenderer.py)）：小窗 800×450 真客户端 6 场景 E2E 沙箱
- `archive/python/run_tests.py` 与 `archive/python/tests/*`：历史纯客户端完整离线回归套件
- `archive/tests/player_hud_spec.lua` 与 `archive/tests/*_spec.lua`：历史纯客户端 Lua 单元测试脚本

## 独立运行归档基线

请在仓库根目录运行（脚本仅加载 `archive/python/` 与 `data/`，不依赖主线 `python/`）：

```powershell
# 运行历史纯客户端完整离线回归测试
python .\archive\python\run_tests.py

# 查看配对命令
python .\archive\python\mcp_server\player_server.py --print-pair-command

# 探活与运行纯客户端控制器
python .\archive\python\mcp_agent\player_probe.py
python .\archive\python\mcp_agent\player_agent.py --loop --execute

# 运行小窗真客户端 6 场景 E2E 回归
python .\archive\python\tools\client_e2e_sandbox.py --suite
```
