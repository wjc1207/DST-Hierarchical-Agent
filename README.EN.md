# JEV DST Agent

[中文版](README.md) · [Technical reference](docs/TECHNICAL.EN.md)

An experimental Don't Starve Together agent. DeepSeek maintains prioritized resource goals. The executor completes visible tasks and searches for missing resources using Klei room-generation priors and observed map memory. Lua executes native actions and verifies effects; JEV handles emergencies and temporarily takes over during DeepSeek planning gaps. The long-term goal is an Alchemy Engine; Science Machine construction is currently supported.

Previous milestones: [survive the first night](https://bilibili.com/video/BV1aehE6DEji) · [build a Science Machine](https://www.bilibili.com/video/BV1mFaV6aEQ2)

## Quick start

1. Copy the entire `jev_dst_agent` folder into Don't Starve Together's `mods` directory. Enable **JEV DST Agent** in the game's Mods menu and enter a local or private world.
2. Copy `.env.example` to `.env` and fill in `JEV_API_KEY`. Add `DEEPSEEK_API_KEY` if you want periodic planning. Do not share or commit `.env`.
3. Complete [MCP bridge setup](docs/MCP.EN.md), then open PowerShell in the project folder, check connectivity and start the loop:

```powershell
python.exe .\mcp_agent\mcp_probe.py
python.exe .\mcp_agent\mcp_agent.py --loop --execute
```

The last command sends real game inputs; press `Ctrl+C` to stop. Omit `--execute` to observe decisions without controlling the game.

## Execution mode

Priority goals use the DeepSeek/JEV autonomous loop over MCP, with native Lua
execution and effect verification instead of keyboard/mouse simulation. After
[configuring the bridge](docs/MCP.EN.md) and updating the Mod, run:

```powershell
python .\mcp_agent\mcp_agent.py --loop --execute
```

This starts its own MCP service. Stop `--bridge-only`, probes and Codex's DST MCP connection first. The new board scheduler requires this MCP loop.
See [MCP setup, goal format, navigation and limitations](docs/MCP.EN.md).

## Tests and documentation

Run offline regression tests without launching the game:

```powershell
python.exe .\run_tests.py
```

See the [technical reference](docs/TECHNICAL.EN.md) for state data, action rules, candidate pruning, exploration and planning. Edit [DSTKnowledge.txt](data/DSTKnowledge.txt) to change the game knowledge supplied to the planner.

Inspired by [TerraBlind](https://github.com/Reisenbug/TerraBlind).
