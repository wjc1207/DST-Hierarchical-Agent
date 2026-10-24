# MCP 服务与 Lua 原生执行（1.8.8）

[English](MCP.EN.md) · [项目首页](../README.md)

当前只保留这条原生执行路径，不再提供旧键鼠控制入口：

```text
独立 AI 程序 / MCP 客户端
           │ STDIO MCP
Python MCP 服务（工具、状态缓存、动作队列）
           │ 本机 HTTP / JSON
Lua Mod（可用动作、原生执行、结果验证、事件中断）
```

Python 不再读取游戏日志或模拟键鼠；Lua 主动异步交换消息。MCP 服务本身不调用模型，不需要模型密钥。项目自带的外部控制程序 `mcp_agent/mcp_agent.py` 负责 DeepSeek/JEV 调用和自动循环，也可以替换成其他 MCP 客户端。旧键鼠控制链已删除；模型调用由独立客户端模块实现。

服务端文件放在 `mcp_server/`（入口、`game_bridge.py`、`transport_metrics.py`），客户端文件放在 `mcp_agent/`（自动循环、连接检查器）。模型和搜索意图均在 `mcp_agent/`，共享规则在 `common/`，知识与资源表在 `data/`。`.env`、`logs/` 和 `scripts/jev_bridge_config.lua` 位置不变，不要因迁移重新生成令牌。也可以从项目根目录使用 `python -m mcp_server`、`python -m mcp_agent`。

## 安装与连接

需要 Python 3.11+。在项目目录运行（也可以在自己的虚拟环境中安装）：

```powershell
python -m pip install -r requirements-mcp.txt
python .\mcp_server\mcp_server.py --configure-only --write-mod-config .\scripts\jev_bridge_config.lua
```

第二条命令创建 `logs/mcp_bridge.json` 中的本机令牌，并生成同一令牌的 Mod 配置。两者都被 Git 忽略。**不要把 API 密钥放进 Mod 配置，不要分享生成的令牌文件。** 使用同一项目和 `--config` 路径启动服务；不要从另一个安装目录另生成一套令牌。

把 `modmain.lua`、`modinfo.lua` 和 `scripts/` 复制到安装的 Mod 文件夹，覆盖相应旧版本文件；不要复制 `.env`、`logs/`、`.venv-mcp/`。首次配置时安装生成的 `scripts/jev_bridge_config.lua`；**升级时保留安装目录已有的这个配置，不要用另一目录的配置覆盖它**，也不要另生成一套令牌。完整更新 Python（包括 `mcp_agent/`、`mcp_server/`）与 Mod 文件后，重新进入世界。桥接协议为 4，详细测量版本仍为 1；新自动控制程序拒绝旧版 Mod 快照。不要仅更新一端。

## DeepSeek/JEV 自动循环

先停止已有的 `--bridge-only`、检查器、其他同端口 MCP 服务及旧控制循环。在装有 MCP 依赖的同一个 Python 环境中运行：

```powershell
python .\mcp_agent\mcp_agent.py --loop --execute
```

它自行启动 STDIO MCP 服务，读取现有 `.env` 的 `JEV_API_KEY`、`DEEPSEEK_API_KEY` 和模型配置。省略 `--execute` 只观察一次决策，不发游戏命令、不请求 DeepSeek 规划；`--no-planner` 禁用规划；`--env PATH`、`--config PATH` 可指定已有文件。没有 DeepSeek 密钥时保留 JEV 和执行器运行，不等待不存在的规划。

自动循环与外部 MCP 客户端必须二选一占用桥接端口。启动时先检查端口，HTTP 服务绑定时再次执行原子独占检查（Windows 使用 `SO_EXCLUSIVEADDRUSE`，禁用端口复用）。已有 Codex MCP、检查器、`--bridge-only` 或其他服务占用 8765 时，程序输出 `mcp_startup_error bridge_port_unavailable address=127.0.0.1:8765` 并以状态码 2 退出，不进入断线重试、不自动连接旧服务，也不终止原进程。先停用旧客户端／服务，再启动 `mcp_agent/mcp_agent.py`；旧服务若由客户端管理，直接结束进程可能被客户端再次拉起。此端口检查的 Python 文件位于 `mcp_server/`、`mcp_agent/`；不改 Mod 或令牌。

| 组件 | 职责 |
| --- | --- |
| DeepSeek | 维护优先级目标、依赖和搜索预算；季度后台复核，不强制换目标 |
| JEV | 安全变化、导航中的新类型计划外机会，以及缺少规划时的选择 |
| Python 意图执行器 | 优先完成所有可见可执行任务，再为最高优先目标导航；保留进度及失败记忆 |
| Lua 执行器 | 接近、连续行走、采集、制作、逃跑、完成验证与实时安全中断；保留旧看板客户端兼容 |
| MCP 服务 | 原子状态／动作快照、观察凭据、单动作队列和看板交接，无模型调用 |

两种模型复用同一游戏事实投影，含 Lua 可用动作、区域生成资源先验与未支持能力。普通看板任务不逐次询问 JEV；危险、受伤、光照或紧急饥饿可交回安全决策。DeepSeek 请求在后台运行，已有看板继续执行。旧 Lua 看板暂停仍需先等确认，新版看板仅由 Python 调度。使用作出选择时的原观察凭据提交动作，Lua 按实时状态校验。世界／玩家会话变更或死亡时停止，断线时不向旧状态发命令。

按 `Ctrl+C` 停止，程序请求取消并等待确认；来不及确认则依赖服务关闭后的 3 秒租约停止。不要把关窗口等同于立即停止角色。终端输出 `TASK BOARD`、`task_board_action`、`lua_action`、`mcp_cycle` 和规划预览；细分时序保存在 `logs/mcp_timing.jsonl`，新规划保存在 `logs/task_board.json` 与 `logs/task_board.jsonl`。旧动作序列日志不代表当前活动规划。旧键鼠控制链及预测缓冲已移除。

## 优先级看板与资源导航（1.8.7）

DeepSeek 输出 `tasks`，验证后标记为 `schema=2, execution_policy=priority_goals`。每项有优先级、可验证目标、依赖和搜索预算。先完成视野内可执行任务，再为最高优先级的未完成任务导航；采草和采石头可以交错完成，不再被数组顺序锁死。

导航顺序：记忆中的具体资源位置 → 实际记录过来源的区域 → Klei 生成配置支持该资源的已知区域 → 未知分支。区域以拓扑 ID 区分，不以地皮切分；已知路线按 BFS 跳数和入口距离排序（不是全程精确最短距离）。资源记忆只来自实际观察，不读取未探索地图里的实体。完整扫描确认消失、已采集和重新生长会更新记忆；截断扫描不会把遗漏实体当作消失。

每任务搜索预算默认 120 秒／240 单位，范围分别为 10–600 秒和 8–2000 单位，只在导航时累计。区域内搜索达到 30 秒或新增 256 面积时，降低该资源在该区域的优先级，不禁止其他资源或过境。普通路线失败暂停重试，不直接使整个任务失败。每 1/4 天后台复核并合并更新，遗漏不代表取消；使用 `cancel_task_ids` 明确取消。已完成任务锁存，不因材料被消耗而重启。跨季度回复按当前玩家会话及实际状态验证，不仅因为时间跨界而丢弃。

完整 JSON 格式、约束、搜索预算和更新文件见 [Goal scheduling and navigation](GOAL_NAVIGATION.md)。请同步新增的 `mcp_agent/priority_board.py`、`mcp_agent/region_resources.py`、`data/worldgen_resources.json`、`scripts/jev_resource_memory.lua`，以及更新后的 Python 文件、`modmain.lua`、`scripts/jev_exploration.lua`、`scripts/jev_native_executor.lua` 和 `modinfo.lua`，重新进入世界并重启 Python。保留原 `.env`、桥接令牌和 `scripts/jev_bridge_config.lua`。协议仍为 4，不需要重新生成令牌。

## DeepSeek 动作序列（旧版兼容）

以下仅描述此前的严格序列，当前 DeepSeek 不再生成此格式。

旧序列输出 `steps`，严格按数组顺序执行。示例：

```json
{
  "kind": "action_sequence",
  "goal": "收集材料并准备火把",
  "steps": [
    {"id": "grass", "action": "collect", "prefab": "cutgrass", "count": 2},
    {"id": "twigs", "action": "collect", "prefab": "twigs", "count": 2},
    {"id": "torch", "action": "craft", "prefab": "torch", "count": 1},
    {"id": "survey", "action": "explore_current_region", "area": 256}
  ]
}
```

每批 1–12 步；每步 `timeout_seconds` 默认 120 秒、允许 5–300 秒。执行器持续推进当前步，不因材料被后面的制作消耗而重启已完成步骤，不越过失败步骤。原生动作仍逐次通过 MCP 的观察凭据和 Lua 完成验证，不是一次盲发所有按键。重规划带上已完成步骤、当前步和失败原因。跨四分之一天不会取消正在执行的序列；没有活动序列时保留原来的规划时间窗，完成／失败后异步申请下一批。

| action | 参数与完成条件 |
| --- | --- |
| `collect` | `prefab,count`：达到当前库存目标；可采集、砍伐／采矿，缺来源时复用 BFS/DFS 寻找 |
| `craft` | `prefab,count`：达到斧子、镐子或火把库存目标 |
| `build` | `prefab`：已观察或验证建成营火／科学机器 |
| `eat_safe_food` | `hunger_ratio`：达到目标饥饿比例，默认 0.7 |
| `cook_food` | `count`：本步新增指定数量的熟食 |
| `equip_torch` / `unequip_torch` | 观察到指定装备状态 |
| `travel_region` | `node_id`：实际进入已观测拓扑节点；历史 `entered` 不算完成本次前往 |
| `explore_current_region` | `area`：本步在当前区域新增指定观察面积，默认 256；或边界已完整调查 |
| `explore_other_region` | `new_regions`：本步新发现指定数量的拓扑节点，默认 1；不是前往任意旧区域 |
| `wait` | `seconds`：有界等待 1–30 秒，安全事件仍能中断 |

不会自动发明工具／配方前置任务；DeepSeek 必须明确把前置操作排在前面。暂时没有合法动作或失败／超时会停止这批并要求重规划，而不是偷偷执行后面的步骤。事件快照可能使验证晚一个上传周期，但不能靠预测状态完成步骤。计划内普通资源出现不触发 JEV；原生探索可能为资源事件返回控制，Python 会继续当前步而不调用模型。

升级此旧版功能需要同步新增的 `mcp_agent/action_sequence.py` 及更新后的 `mcp_agent/daily_planner.py`、`mcp_agent/mcp_agent.py`、`mcp_agent/navigation_intents.py`、`mcp_agent/jev_client.py`、`data/DSTKnowledge.txt`，重启 Python。桥接协议仍为 4；此功能不修改 Lua 协议。这些纯语义序列仅保留 MCP 兼容，不包含键鼠路径。

## 其他 MCP 客户端与连接检查

给自己的 MCP 客户端添加 STDIO 服务，`command` 使用安装了依赖的 Python，`args` 使用项目中 `mcp_server/mcp_server.py` 的绝对路径。已有客户端配置也必须更新此路径。例如：

```json
{
  "mcpServers": {
    "dst": {
      "command": "python",
      "args": ["C:/path/to/jev_dst_agent/mcp_server/mcp_server.py"]
    }
  }
}
```

具体配置入口由客户端决定；不需要把程序接入 Codex。MCP 客户端启动和管理该进程，不要同时在终端再启动同端口服务。日志写到 stderr，stdout 只承载 MCP。游戏桥仅监听 `127.0.0.1:8765`；更改 `--port` 后需重新生成对应 Mod 配置。

不配置其他客户端也能先用项目自带的命令行检查器。它临时启动 MCP 服务、等待 Mod 连上、打印状态与合法动作，然后退出；默认不操作游戏：

```powershell
python .\mcp_agent\mcp_probe.py
```

看到 `connected` 和 `read_only: no game action sent` 才算整条链路连通。明确要测试一个动作时可用 `python .\mcp_agent\mcp_probe.py --execute-choice wait`；其他动作必须使用当时提供的名称。检查器退出会关闭临时服务，因此不要与其他 MCP 客户端或 `--bridge-only` 同时启动。

先单独验证游戏连通性也可以运行：

```powershell
python .\mcp_server\mcp_server.py --bridge-only
```

这不会调用模型或控制角色。进入世界后日志应出现 `[JEV_DST_BRIDGE]enabled_loopback_http`；**这只代表 Mod 已启用，不证明网络已连通**。完整连接状态由 MCP `get_state` 的 `state.bridge.connected` 或 Mod 状态中的同名字段确认。连通性模式不提供 MCP；验证完 Ctrl+C 停止，再由 MCP 客户端启动正常服务。要恢复旧控制器，将安装的 `jev_bridge_config.lua` 的 `enabled` 改为 `false` 并重新进入世界。

## 工具

| 工具 | 含义 |
| --- | --- |
| `get_state(refresh=false)` | 最新事件快照及其实际年龄；`refresh=true` 按需请求新快照，最多等待 3 秒；连接断开时报错 |
| `get_available_actions(refresh=false)` | 缓存游戏事实与动作、快照年龄、最新控制状态及短期 `observation_id`；`refresh=true` 等 Mod 上传新快照 |
| `start_action(choice, observation_id)` | 排队执行一个已提供动作；返回 `action_id`，不是完成证明 |
| `get_action_status(action_id)` | 查询状态、原因及连接状态 |
| `cancel_action(action_id)` | 请求取消，需等 Lua 确认，不提前声称已经停止 |
| `submit_task_board(board, observation_id, auto_execute=false)` | 验证并替换看板；显式设 true 才允许 Lua 自动推进可见的本地任务 |

典型调用：读取状态 → 读取动作 → 用返回的 observation ID 选择动作 → 查询动作状态。状态包括 `queued`、`running`、`cancel_requested`、`completed`、`failed`、`interrupted`、`cancelled`。失败／中断后重新观察，不重放旧动作。

自动看板启用期间保留执行器控制权，`busy` 包含这项占用。要手动提交动作，先重新提交 `auto_execute=false` 的看板并等 Lua 停止活动动作；不能让手动命令和后台看板竞争。

观察凭据最长有效 10 秒，绑定会话、玩家与当时提供的动作；掉血、阶段、库存及敌人变化不使其提前失效。服务仅验证连接新鲜度、凭据归属／期限、原动作白名单和单执行器占用；排队的是原目标，不用最新同名动作覆盖 GUID。Lua 用实时状态检查可行性和原目标身份，可拒绝过时意图；Python 不提前将“已排队”视为合法或完成。服务保留最多 128 个观察凭据，普通心跳序号增加不会单独推翻决策。

## 当前支持范围

- 原生采集和路径接近，检查 pickable、目标 GUID 和已验证路径；库存增长才算成功。
- 制作工具、火把，装备／卸下火把，吃／烹饪，建造营火与科学机器；制作火把的非夜晚自动卸下清理仍保留。
- 砍树／采矿与随后可见、可达的新掉落物收集，保守检查工具耐久。
- 当前区域探索、寻找其他区域，以及沿 Mod 已报告路线前往已知区域；Lua 连续推进路段，资源产品集合变化且累计位移达到 4 单位时交回决策。光照、掉血、敌人等危险不等位移保底。
- 基于可通行逃跑候选的有界撤离；“没有可见锁定敌人”不等于证明敌人已永久消除仇恨。
- 看板默认只保存；显式启用后自动执行视野内的采集、制作、进食和建造任务。通过重新提交 `auto_execute=false` 的看板暂停；活动的看板动作会停止。

**尚未把完整 DFS/BFS 和历史来源失败记忆迁到 Lua**：自带控制程序暂时复用 Python 意图选择器，经 MCP 交给 Lua 行走，不依赖键鼠控制。攻击／装备武器、自动工具前置链、炼金引擎动作尚未提供；Lua 看板遇到缺失来源／需导航／不支持的任务交回外部控制程序，而不是自己请求模型。可用动作不接受任意 Lua 代码、文件路径或自定义坐标。

## 安全与时序

桥接令牌放在请求体以适配游戏 HTTP 接口，不进入日志；服务只绑定回环地址。不要把该内部 HTTP 端口公开到公网。会话编号、单调序号、短期观察、目标匹配及单执行队列防止旧世界／重复消息／并发命令误执行。取消只有收到 Lua 终态报告后才确认。

Lua 一次仅发一个 HTTP 请求，约 0.5 秒发送一次轻量心跳；执行器仍约 0.2 秒本地感知和控制，不等待 Python 提供每段移动。完整状态仅在事件、按需刷新和初次／重连时上传。动作开始、确认开始、结束或看板控制权变化优先附带快照上报；有请求在途则保留变化，在回调后的下一执行 tick 补发，不并发或递归请求。上传前在执行器更新之后刷新桥接控制状态，避免 `completed` 与旧的 `executor_busy=true` 同时出现，也避免看板已经开始却仍上传空闲。Python 等待动作时轮询缓存控制状态，不因此要求 Mod 重传完整状态。

超过 3 秒未收到有效响应会停止活动动作并暂停看板。晚到的回调不能复活动作；切换世界或服务重启会使旧工作失效。普通工作遇到危险交回控制，不自动替 AI 举火把；应急照明、逃跑不会被它们正在处理的条件误中断。

命令发送与动作完成是两件事。库存、饥饿、装备、结构实体、工作及位置变化用于验证；没有可确认效果会超时失败。放置建筑的延迟回调也受取消代次保护。MCP 传输或任务取消不等于游戏已停，必须查询执行状态。

### 动作交接计时（1.8.2）

`logs/mcp_timing.jsonl` 每轮新增 `events`：`action.terminal_observed`、`decision.ready`、`action.command_submitted`、`action.command_queued`、`action.lua_accept_observed` 和有证据时的 `action.native_start_observed`；看板切换记录 `board.sync_requested`／`board.sync_acknowledged`。事件的 `at_ms` 是本轮 Python 时间偏移；动作 ID、上一动作 ID 用于跨轮关联，不能直接用两轮的偏移相减。

- `lua_handoff_gap_ms`：上个动作完成到 Lua 接受当前命令的游戏时间间隔；**不是**精确的动画开始间隔。终端显示为 `mcp_handoff ... accepted_gap=...`。
- `lua_started_at` 是 Lua 接受命令，`lua_confirmed_started_at` 是按准确目标／配方的原生状态或实际位移确认的开始。没有证据就缺省，不用请求成功或计时器到期冒充开始。
- `lua_execution_ms`：接受到结果验证完成，含接近、行走及游戏动作，不应当作纯动画时长。
- `lua_finish_to_exchange_ms`：完成后等待发出状态的游戏时间；`lua_previous_http_round_trip_ms` 是前一有效 HTTP 请求的游戏时间往返，用于定位引擎回调／交换等待，暂停时不等于墙钟网络延迟。
- `queue_to_delivery_ms`：服务排队到首次把命令放进 HTTP 响应；`delivery_to_running_report_ms`：响应准备到收到 running 回报（包含传输及回报等待）；`terminal_cache_ms`：服务收到终态到当前读取；均使用服务自己的单调时钟。
- `host_observed_to_submit_ms`：Python 得知上一终态到提交下一命令的时间；可结合 JEV／看板同步事件定位空档。

Lua 游戏时钟、服务时钟和宿主时钟不互相相减。各字段可能覆盖同一段等待，不要直接相加；旧的 `action.execute` 仍是宿主等待整个动作的时长，不能作为角色实际行动时间。本次仅优化交接并增加测量，决策预测缓冲仍待接入 MCP 路径。

### 细分测量（1.8.3，仅增加测量）

启动命令不变。新增 `logs/mcp_transport.jsonl`，位置与 `--config` 的令牌文件在同一目录，记录每次 HTTP 处理；原 `logs/mcp_timing.jsonl` 继续记录宿主轮次和模型后台请求。默认不打印每次 HTTP 的明细，避免刷屏。仅记录阶段耗时、字节数和关联 ID，不记录请求体、响应体、模型文本、令牌或 API 密钥。测量日志写入失败只告警一次，不改变动作结果。

| 范围 | 字段 |
| --- | --- |
| Lua 每次采样 | `entities_ms`、`navigation_ms`、`collection_paths_ms`、`vitals_ms`、`inventory_ms`、`crafting_ms`、`execution_read_ms`、`state_total_ms` |
| Lua 动作与候选 | `native_tick_ms`、`board_step_ms`、`board_ready_ms`、`available_actions_ms`、`control_status_ms` |
| Lua 请求与回调 | `encode_ms`、`query_call_ms`、`query_to_callback_ms`、`decode_ms`、`callback_dispatch_ms`、请求／响应字节数 |
| Python HTTP | `body_read_ms`、`json_decode_ms`、`lock_wait_ms`、`state_copy_ms`、`exchange_ms`、`json_encode_ms`、`headers_write_ms`、`body_write_ms`、`server_total_ms` |
| JEV 后台线程 | `worker_queue_ms`、状态／请求编码、`api.response_headers`、`api.read_body`、`api.parse_response` |
| 宿主发现模型结果 | `decision.result_observed.result_ready_to_notice_ms` |

Lua 采用已从本机游戏脚本确认单位的 `GetTimeRealSeconds`（或 `GetTimeReal()/1000`），不再用帧内不前进的 `GetTime()` 计 CPU／请求耗时。没有实时时钟则标记 `unavailable`。`query_call_ms` 是原生 API 返回前的同步调用时间；`query_to_callback_ms` 是调用到 Lua 回调入口，**仍包含引擎排队、实际 HTTP 与回调派发，不能独立称为网络延迟**。回调解码和控制派发另计。

HTTP 接收行按 `(server_id, session_id, sequence)` 标识。Lua 编码与回调耗时只有完成后才知道，所以由**下一次请求**的 `lua.previous_exchanges` 补报，保留最近 8 条；按回执自身的 `server_id`／`sequence` 去重并与原 HTTP 行关联，不要误用承载回执那次请求的耗时。结束测试时最后一条回执可能尚未来得及补报。采样每 0.2 秒执行而非每次都发 HTTP，因此 `lua.sampling` 是自上次发送以来的 tick 数和各阶段累计／最大值；`lua.refresh` 是本次发送前的控制状态刷新。

服务端在写出响应后才落日志，`server_total_ms` 不含这次诊断文件写入；写 socket 完成并不证明 Lua 已收到响应。`exchange_ms` 包含锁等待和状态复制；`state_total_ms` 包含子采样、控制状态计算包含候选计算，这些父子阶段不能重复相加。只比较各自时钟中的持续时间，不直接相减不同进程的绝对时间戳。

JEV 的每次请求有独立 `request_id`。`decision.submitted` 记录来源游戏会话、状态消息序号和上一动作 ID；`decision.worker_completed.worker_timing` 保存独立后台计时，不能再加到主循环耗时中。`api.response_headers` 包含连接、上传、服务端处理至收到响应头的整体，并不等于纯模型推理。结果已完成却要等到下次轮询才发现的时间单列；丢弃／失败的请求仍保留已完成的测量，原异常类型不变。DeepSeek 沿用已有后台规划总耗时，本版不改变规划或决策策略，也未启用预测缓冲。

### HTTP 上传握手（1.8.4）

Python 回环服务改用 HTTP/1.1，在合法的 `Expect: 100-continue` 请求上传正文之前立即返回临时的 `100 Continue`。旧的 HTTP/1.0 默认配置不会处理该握手，等待 Continue 的客户端可能在超时后才上传正文。最终响应仍显式关闭连接；不启用长连接、不改游戏采样间隔、决策或动作策略。没有 Expect 的请求以及 HTTP/1.0 请求仍可用。临时响应不代表鉴权通过，正文令牌仍需校验；上传前检查端点、正文大小和不受支持／有歧义的分帧。

`mcp_transport.jsonl` 的 `http` 字段只记录 `request_version`、`response_version`、`expect`（`none`／`100-continue`／`other`）和 `continue_sent`。不记录任意请求头、Authorization、Cookie 或令牌。下一轮游戏测试应确认实际是否出现 `expect=100-continue`、`continue_sent=true`，并比较 `body_read_ms` 是否不再固定约 1000 毫秒。本地模拟测试证明握手已修复，但不代替真实游戏的发送／回调测量。

1.8.3 实测中 Lua 实时时钟的结果呈整秒刻度；时钟接口名称和单位不保证毫秒精度。`0` 不能解读为没有耗时，`1000` 也不能证明该段工作精确执行了一秒。本次不替换 Lua 时钟，毫秒级定位优先使用 Python 单调时钟；不要据 Lua 子阶段值断言感知或解码的 CPU 开销。

### 实时校验归 Mod（1.8.5）

模型请求不再使用 Python 的整份上下文相等检查。JEV 只能返回本次请求提供过的选项，但目标是否仍可采、材料是否足够、当前危险／照明是否允许，由 Lua 收到命令后判定。MCP 工具同样适用，不限于内置控制循环。目标 GUID、prefab、动作种类、导航节点／模式保持一致；允许按当前事实刷新路线坐标，不悄悄更换采集对象或导航目的区域。

例如，选择逃跑后生命下降或敌人更换，Python 仍提交原逃跑意图，Lua 重算当前可通行路线；逃跑过程中继续掉血不会因此中断。若敌人已经消失则返回 `threat_cleared`，无路可走返回 `no_safe_escape_route`，不自动改选等待或采集。选择采草后目标已空、消失或更换，则分别返回 `target_not_collectable`、`target_missing`、`target_changed`；遭敌人锁定则返回 `targeted_by_hostile`；其他当前不可用条件返回 `action_unavailable`。终端保留动作名称和明确拒绝原因，下一轮从新状态重新决策。

Python 保留会话／玩家变更、死亡停止、观察凭据过期、断线、取消与单控制权约束。Lua 仍保留租约、晚到回调与重复命令保护，记住最近 256 个已接受、已拒绝或已取消的命令 ID。该改动不启用旧版预测缓冲，也不改变看板启停优先级；旧按键控制器和预请求实现已移除。

### 事件快照与轻量心跳（1.8.6，协议 4）

- `kind=snapshot`：携带完整 `state`，初次连接、关键状态变化、动作／看板状态变化或收到刷新请求时发送。
- `kind=heartbeat`：不携带 `state`、实体列表、导航地图、动作候选或历史事件环；只带控制状态、最近动作报告和数值诊断。用来续租、收取命令、取消和看板版本，不重复发送未变化的看板正文。
- 事件包括阶段／天数／四分之一天变化、生命变化、敌人集合、库存／装备种类和数量、可用动作类型、营火燃烧／距离分区、所在拓扑节点和已知节点数量。普通位置变化、同类资源最近 GUID 切换、每次饥饿或燃料小幅变化不是独立上传事件；饥饿／精神值按区间变化上报。本地感知和 Lua 执行前校验仍持续运行。
- 上传失败不会确认快照；在途期间的新变化不会被旧回调吞掉。服务重启后先强制补发完整状态；无初始快照时不能仅凭心跳发动作。

`state_version` 是完整快照的消息序号，`transport_sequence` 是最新交换序号；`state_age_ms`／`get_state.age_seconds` 从完整快照接收时起计算，`connection_age_ms` 从最近有效消息起计算。心跳只刷新连接和控制字段，不把旧的世界、库存、位置或动作候选伪装成实时数据。超过 3 秒无消息仍判断线；10 秒观察凭据期限绑定其来源快照，而不是不断读取缓存就能续期。

外部 MCP 客户端需要当前事实时调用 `get_state(refresh=true)` 或 `get_available_actions(refresh=true)`。服务通过下一次回包请求快照，Lua 优先回应；只有携带相应 `snapshot_request_id` 的完整快照才完成刷新，心跳不能冒充刷新，超时明确报错。异步 MCP 工具等待刷新时不阻塞其他工具。自动控制程序在准备行动且缓存超过 1 秒时、或提交看板前按需刷新；等待模型／动作以及仅能等待的夜晚不会每轮请求完整快照。

`mcp_transport.jsonl` 新增 `message_kind`，完整快照还记录白名单 `snapshot_reason`（`initial`、`event`、`requested`、`resync`），用于统计两类消息数量和字节数。1.8.4 的 Continue 握手修复保留。此版只改通信与缓存读取，不增加机会授权复用、应急模型抢占或预测缓冲，也不减少 Lua 本地感知频率。

### 工作结束后的拾取衔接

砍伐／采矿结束时，角色可能仍在收尾动画或原生交互冷却中，此时原生动作查询暂时返回空。执行器保留原掉落物 GUID，每 0.2 秒重试，最多等待 3 秒恢复可交互；不抛出 `native_exception`，也不切换到另一个同类物品。重试前检查目标、可采状态和可达路径，遇敌、光照变化或取消仍立即中断。超过等待期限明确返回 `collection_action_unavailable`。

发出拾取指令后仍等待背包对应物品数量增加，再继续处理下一件掉落物；不会因为重试或发送成功而提前报告工作完成。砍伐和采矿共用该流程，普通采集同样支持短暂交互冷却。更新 `modmain.lua` 和 `scripts/jev_native_executor.lua` 后重新进入世界；无需修改 Python 或桥接配置。

## 验证

```powershell
python .\run_tests.py
python -m unittest discover -s tests -p test_game_bridge.py -v
python -m unittest discover -s tests -p test_mcp_agent.py -v
```

未装 MCP SDK 时仅跳过真实 MCP 协议测试；其余桥接状态机测试仍运行。安装后会额外验证官方 SDK 工具发现、真实 STDIO 子进程、回环 HTTP、模拟 Lua 消息、取消确认和鉴权。Lua 回归在 `tests/exploration_spec.lua` 中覆盖原生执行与异步回调；可用 Lua/Fengari 运行。所有这些都使用模拟世界，不替代安装新版 Mod 后对游戏内 `QueryServer`、原生移动、采集及服务器复制的实测。
