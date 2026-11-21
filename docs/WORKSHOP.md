# Steam 创意工坊发布与订阅指南 / Steam Workshop Guide

本仓库的两个模组作为 **两个独立的 Steam 创意工坊条目** 发布，且 **Steam 创意工坊与 GitHub 分发完全解耦、各自独立可用**：

| 模组目录 | 模组名称 | 类型 | 默认端口 | 游戏内命令 | 当前版本 |
| --- | --- | --- | --- | --- | --- |
| `dist/workshop/dst_buddy_agent` | **DST Buddy Agent** | 服务端/联机模组 (`all_clients_require_mod = true`) | `8766` | `/buddy setup` · `/buddy pair <token>` | `0.8.0` |
| `dist/workshop/dst_player_agent` | **DST Player Agent** | 纯客户端模组 (`client_only_mod = true`) | `8765` | `/agent setup` · `/agent pair <token>` | `1.9.0` |

---

## 一、设计原则：工坊离线自包含 + 更新不丢配置

每个创意工坊模组目录内除了 DST Lua 模组本体外，直接附带：
- 纯英文跨平台脚本：`1_install_agent.bat`、`2_run_buddy.bat`（或 `2_run_player.bat`），以及 macOS/Linux 版 `.sh`；
- 离线内置纯源码目录 `runner/`（约 200 KB 纯文本，不含 `.exe`，不依赖 GitHub 网络，**与当前模组版本 100% 对齐**）。

同时彻底规避了 Steam 创意工坊更新覆盖文件的风险：
1. **Python 运行目录独立于工坊**：`1_install_agent.bat` 会把 `runner/` 纯本地复制到本机独立目录（Windows：`%USERPROFILE%\DST-Hierarchical-Agent\`；macOS/Linux：`~/.dst-hierarchical-agent/`），所有 `.venv-mcp`、`.env`、`logs/`、`data/agent_memory/` 都存在那里，**工坊更新绝不覆盖用户数据**；
2. **游戏内配对持久化**：配对口令通过饥荒原生 `TheSim:SetPersistentString` 保存在 `client_save/`，**工坊更新同样不会覆盖**，且配对命令在本地客户端直接拦截，**绝不发送到公屏或游戏服务器**；
3. **自动同步升级**：后续工坊推送更新后，玩家点击 `2_run_buddy.bat` / `2_run_player.bat`（或重新点 `1_install_agent.bat`）会自动把新版本源码增量同步到本机目录，同时完整保留已有的 `.env`、令牌与记忆。

---

## 二、创意工坊订阅玩家上手流程（全程无需访问 GitHub）

1. **订阅并启用模组**：在 Steam 创意工坊订阅 **DST Buddy Agent**（或 **DST Player Agent**），在游戏模组菜单启用后进入世界。
2. **定位模组文件夹**：在游戏聊天栏输入 `/buddy setup`（伙伴）或 `/agent setup`（角色操控），聊天栏会在本地直接显示该模组在你电脑上的精确目录路径（`MODROOT`）。
3. **点击一键安装**：打开该目录，双击 **`1_install_agent.bat`**（macOS / Linux 运行 `./1_install_agent.sh`）：
   - 自动将内置 `runner/` 源码复制到本机独立目录；
   - 自动检测 Python 3.11+（缺失时支持 `winget` 一键引导安装），建立 `.venv-mcp` 并安装依赖（默认 PyPI 失败自动回退清华镜像源）；
   - **可选在桌面创建快捷方式**（`DST Buddy Agent.lnk` / `DST Player Agent.lnk`），以后无需再找工坊目录。
4. **一键启动并配对**：双击 **`2_run_buddy.bat`** / **`2_run_player.bat`**（或桌面快捷方式）：
   - 选择后端（DeepSeek API / Claude Code CLI / Codex CLI / DSH CLI），若选 DeepSeek 且未填 Key 会引导写入本机 `.env`；
   - 启动器**自动将 `/buddy pair <token>` 或 `/agent pair <token>` 复制到剪贴板**；
   - 回到游戏按 `Y` 打开聊天栏 → `Ctrl+V` 粘贴回车，即可开玩！
5. **常用本地管理命令**：
   - 伙伴模组：`/buddy setup`（查看目录）、`/buddy pair status`（查看配对状态）、`/buddy unpair`（解除配对）、`/buddy summon 小威`（召唤伙伴）
   - 玩家模组：`/agent setup`（查看目录）、`/agent status`（查看连接状态）、`/agent unpair`（解除配对）

---

## 三、作者发布 / 更新创意工坊步骤

### 1. 一键生成所有发布产物

在仓库根目录执行：

```powershell
python .\python\tools\package_mods.py
```

脚本会自动生成并自检以下产物（已加入 `.gitignore`，不会提交到 Git）：
- **Steam 创意工坊上传目录（离线自包含）**：
  - `dist/workshop/dst_buddy_agent/`（含 Lua 模组 + `1_install_agent.bat/.sh` + `2_run_buddy.bat/.sh` + `runner/`）
  - `dist/workshop/dst_player_agent/`（含 Lua 模组 + `1_install_agent.bat/.sh` + `2_run_player.bat/.sh` + `runner/`）
- **GitHub Releases 独立产物**：
  - `dist/dst_agent_runner.zip`（独立一键运行包）
  - `dist/dst_buddy_agent.zip`、`dist/dst_player_agent.zip`（纯离线模组压缩包）

自检会自动保证：
- `preview.jpg` 为合法 JPEG 且小于 1 MB；
- `scripts/jev_*_config.lua` 已强制重写为 `enabled=false, token=""`，**零本机令牌泄漏**；
- `runner/` 不含 `.env`、`logs/`、`data/agent_memory/`、`python/tests/`、`__pycache__/`；
- 所有 `.bat` 无 UTF-8 BOM、所有 `.sh` 为 LF 换行、所有 `.ps1` 为 UTF-8 BOM（兼容 Windows PowerShell 5.1）。

### 2. 使用 Steam 的 Don't Starve Mod Tools 上传

1. 在 Steam 库左上角勾选 **工具 (Tools)**，安装并启动 **Don't Starve Mod Tools** → **Mod Uploader**。
2. **分别创建 / 更新两个条目**（不要混在一个条目里）：
   - **条目 1（DST Buddy Agent）**：指向 `dist/workshop/dst_buddy_agent`；Tags 建议勾选 `Gameplay`、`Utility`（服务端标签 `ai`, `agent`, `companion`, `buddy`）。
   - **条目 2（DST Player Agent）**：指向 `dist/workshop/dst_player_agent`；Tags 建议勾选 `Client Only`、`Utility`、`Interface`。
3. 首次发布建议先设为 **仅好友可见 (Friends-Only)**，自己从工坊订阅并实测一次 `/buddy setup` → `1_install_agent.bat` → `2_run_buddy.bat` → `/buddy pair`，确认无误后再改为 **公开 (Public)**。

---

## 四、Steam 创意工坊商店页文案（可直接复制粘贴）

### 条目 1：DST Buddy Agent

**标题**：`DST Buddy Agent - AI 伙伴 / Local AI Wilson Companions`

**简介（BBCode，可直接粘贴到 Steam Workshop Description）**：

```bbcode
[h1]DST Buddy Agent（AI 伙伴模组）[/h1]
在《饥荒联机版》世界里召唤最多 [b]2 名可命名的 AI 威尔逊伙伴[/b]，通过聊天指令或长期生存目标与你并肩生存！

[h2]核心功能[/h2]
[list]
[*] [b]召唤与面板管理[/b]：右上角「伙伴」HUD 面板或输入 [code]/buddy summon 名字[/code] 即可召唤/召回伙伴，支持多伙伴分工与独立执行。
[*] [b]聊天对话与异步规划[/b]：开启聊天监听后，直接在聊天框用 [code]@伙伴名 需求[/code] 或 [code]@all 需求[/code] 下达指令，伙伴边干活边异步重新规划。
[*] [b]原版动作全覆盖[/b]：支持跟随、拾取/采集、砍树、挖矿、烧树制炭、寻物探索、制作与建造、箱子/冰箱/龙蝇箱批量存取、营火/火坑添燃料、烹饪锅做饭、设家驻留与自动进食回血。
[*] [b]多后端支持 & 隐私安全[/b]：所有 AI 调用仅在你自己的电脑上运行（支持 DeepSeek API、Claude Code CLI、Codex CLI、DSH 或任意 MCP 客户端），游戏服务器不接触你的 API Key，联机时每位玩家只为自己的伙伴付费。
[/list]

[h2]如何启用（本模组已自带离线安装脚本，无需另去 GitHub 下载）[/h2]
[olist]
[*] 订阅并启用本模组，进入世界后在聊天栏输入 [code]/buddy setup[/code]，即可在本地查看本模组在你电脑上的精确文件夹路径。
[*] 打开该文件夹，双击 [code]1_install_agent.bat[/code]（macOS/Linux 运行 [code]./1_install_agent.sh[/code]）：自动安装本机 Python 虚拟环境（支持国内清华镜像），并可选择在桌面创建快捷方式。
[*] 双击 [code]2_run_buddy.bat[/code]（或桌面快捷方式）选择后端启动，配对口令会自动复制到剪贴板。
[*] 回到游戏按 [b]Y[/b] 打开聊天栏，直接 [b]Ctrl+V[/b] 粘贴 [code]/buddy pair <token>[/code] 回车，再输入 [code]/buddy summon 小威[/code] 即可召唤伙伴！
[/olist]
[i]安全与更新提示：/buddy setup 与 /buddy pair 均在本地客户端直接拦截，绝不发到公屏或服务器；Python 环境、API Key、记忆与配对信息全部保存在工坊目录之外，创意工坊更新不会覆盖。开源代码托管于 [url=https://github.com/wjc1207/DST-Hierarchical-Agent]GitHub[/url]。[/i]

[hr][/hr]

[h1]DST Buddy Agent (English)[/h1]
Summon up to [b]two named AI Wilson companions[/b] in Don't Starve Together that gather, chop, mine, burn trees for charcoal, craft, build, cook in Crock Pots, manage chests/iceboxes, refuel fires, and pursue long-term survival goals!

[h2]Self-Contained Setup (Bundled Installer & Runner)[/h2]
[olist]
[*] Subscribe, enable the mod, enter a world, and type [code]/buddy setup[/code] in chat to print this mod's exact folder path on your computer.
[*] Open that folder and run [code]1_install_agent.bat[/code] (macOS/Linux: [code]./1_install_agent.sh[/code]) to install the bundled local Python runner into your user folder (with an optional Desktop shortcut).
[*] Run [code]2_run_buddy.bat[/code] (or your Desktop shortcut) and choose your backend (DeepSeek API, Claude Code CLI, Codex CLI, or DSH). Your [code]/buddy pair <token>[/code] command is copied to the clipboard automatically.
[*] Press [b]Y[/b] in-game, paste and press Enter (intercepted locally—never sent to public chat or the server, and persists across Workshop updates).
[/olist]
Open-source repository: [url=https://github.com/wjc1207/DST-Hierarchical-Agent]https://github.com/wjc1207/DST-Hierarchical-Agent[/url]
```

---

### 条目 2：DST Player Agent

**标题**：`DST Player Agent - AI 角色操控 / Local AI & MCP Player Controller`

**简介（BBCode，可直接粘贴到 Steam Workshop Description）**：

```bbcode
[h1]DST Player Agent（纯客户端 AI 角色操控模组）[/h1]
一款 [b]纯客户端（Client-Only）[/b] 的《饥荒联机版》本地 MCP 桥接模组：让本机 AI 智能体在原生游戏规则下协助操控你的角色，完成日常采集、建造、做饭、整理箱子或自主生存！

[h2]核心功能[/h2]
[list]
[*] [b]纯客户端、零作弊[/b]：不修改服务器，不模拟键鼠外挂，所有动作全部通过饥荒原生动作接口执行与实时校验。
[*] [b]角色操控 HUD & 聊天指令[/b]：右上角「角色操控」面板随时切换「指令 / 自主」模式、编辑长期生存目标或一键暂停立即接管；开启聊天监听后输入 [code]@自己 需求[/code] 即可实时派活，输入「停止/暂停」立刻交还控制权。
[*] [b]多任务看板与长期记忆[/b]：支持采集、砍树、挖矿、烧树制炭、探索建图、营地建造、箱子存取、营火添料、烹饪锅做饭、温控避险与跨会话记忆。
[*] [b]多后端 & 开放 MCP 接口[/b]：支持 DeepSeek API、Claude Code CLI、Codex CLI、DSH 或任何标准 STDIO MCP 客户端接入。
[/list]

[h2]如何启用（本模组已自带离线安装脚本，无需另去 GitHub 下载）[/h2]
[olist]
[*] 订阅并启用本模组，进入世界后在聊天栏输入 [code]/agent setup[/code]，即可在本地查看本模组在你电脑上的精确文件夹路径。
[*] 打开该文件夹，双击 [code]1_install_agent.bat[/code]（macOS/Linux 运行 [code]./1_install_agent.sh[/code]）：自动安装本机 Python 虚拟环境（支持国内清华镜像），并可选择在桌面创建快捷方式。
[*] 双击 [code]2_run_player.bat[/code]（或桌面快捷方式）选择后端启动，配对口令会自动复制到剪贴板。
[*] 回到游戏按 [b]Y[/b] 打开聊天栏，直接 [b]Ctrl+V[/b] 粘贴 [code]/agent pair <token>[/code] 回车，打开右上角「角色操控」面板即可！
[/olist]
[i]安全与更新提示：/agent setup 与 /agent pair 均在本地客户端直接拦截，绝不发到公屏；Python 环境、API Key、记忆与配对信息全部保存在工坊目录之外，创意工坊更新不会覆盖。随时可用 /agent status 查看状态或 /agent unpair 解除配对。开源代码托管于 [url=https://github.com/wjc1207/DST-Hierarchical-Agent]GitHub[/url]。[/i]

[hr][/hr]

[h1]DST Player Agent (English)[/h1]
A [b]client-only[/b] local loopback MCP bridge for Don't Starve Together. Control your own character with a local AI planner via the Player Control HUD, [code]@自己[/code] chat instructions, or autonomous long-term survival goals—using native game actions only.

[h2]Self-Contained Setup (Bundled Installer & Runner)[/h2]
[olist]
[*] Subscribe, enable the mod, enter a world, and type [code]/agent setup[/code] in chat to print this mod's exact folder path on your computer.
[*] Open that folder and run [code]1_install_agent.bat[/code] (macOS/Linux: [code]./1_install_agent.sh[/code]) to install the bundled local Python runner into your user folder (with an optional Desktop shortcut).
[*] Run [code]2_run_player.bat[/code] (or your Desktop shortcut) and choose your backend (DeepSeek API, Claude Code CLI, Codex CLI, or DSH). Your [code]/agent pair <token>[/code] command is copied to the clipboard automatically.
[*] Press [b]Y[/b] in-game, paste and press Enter (intercepted locally—never sent to public chat, and persists across Workshop updates).
[/olist]
Open-source repository: [url=https://github.com/wjc1207/DST-Hierarchical-Agent]https://github.com/wjc1207/DST-Hierarchical-Agent[/url]
```
