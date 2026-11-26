# Agent 记忆、上下文压缩与外部 Agent 回调

[项目首页](../README.md) · [MCP 服务说明](MCP.md) · [共用规划与任务层](SHARED_PLANNING.md)

## 核心分层：规划 — 协调 — 执行（可插拔上层 Agent）

本项目采用严格的 **规划层 — 协调层 — 执行层** 三层单向分层架构，将 **高层规划 / 记忆 / 上下文管理**（`python/mcp_agent/` 或外部 Agent）与 **多角色任务协调 / 桥接**（`python/mcp_server/` + `python/executor/`）及 **游戏原生动作执行**（`mods/dst_buddy_agent`）彻底解耦：

```text
Layer 1 · 规划层（自研 DeepSeek Harness / Claude Code CLI / Codex CLI / DSH CLI & HTTP）
        │  统一规划契约：get_planning_state / submit_task_board / send_planning_reply
        ▼
Layer 2 · 协调层（companion_server.py:8766 + python/executor/）
        │  5Hz 多角色任务协调、前置依赖编排、安全监控、DirectBridgeClient 直驱内存桥接
        ▼
Layer 3 · 执行层（mods/dst_buddy_agent 统一服务端 Mod）
           Klei 原生 BufferedAction 28 个原子动作 + 底层被动避险（evade）
```

- **协调层（`mcp_server` + `executor`）不包含任何 LLM 调用与长期记忆逻辑**，只负责确定性的多角色任务协调、动作合法性校验与桥接调度。
- 无论规划层接自研 DeepSeek Harness 还是外部 Claude Code / Codex / DSH，都复用同一套协调层 MCP 接口与 Mod 执行能力。

---

## 一、Claude 风格主动记忆（`AgentMemoryStore`）

实现位于 `python/mcp_agent/agent_memory.py`，默认持久化在 `data/agent_memory/`（已在 `.gitignore` 中忽略）：

```text
data/agent_memory/
├── global/                        # 跨存档可复用玩法策略（playbook / user 偏好）
│   ├── MEMORY.md                  # 自动维护的索引
│   └── <topic>.md
└── worlds/<world_id>/             # 按 TheWorld.meta.session_identifier 严格隔离
    ├── MEMORY.md                  # 本存档索引（营地约定、玩家偏好）
    ├── <topic>.md
    ├── context_player.json        # 玩家端 append-only 上下文与压缩摘要
    └── context_companion.json     # 伙伴端 append-only 上下文与压缩摘要
```

### 4 个模型主动记忆工具（无需 `buddy_id`）
- `memory_search(query, scope?, limit?)`：按关键词检索 `global` / `world` 记忆
- `memory_read(path)`：按需读取单条 `.md` 完整内容与过期标记
- `memory_write(scope, name, description, content, kind?, source?)`：写入/更新主题 `.md` 并原子刷新 `MEMORY.md`
- `memory_delete(path)`：删除过时或错误记忆并刷新索引

### 5 层防记忆污染机制
1. **写入边界闸门**：对 `source != "user"` 的写入拦截任何运行时能力/Schema/动作报错限制类笔记（这类事实永远以实时 `supported_capabilities` 为准）。
2. **来源分级（`source: user | inferred`）**：区分玩家显式规则与模型自行归纳。
3. **压缩防放大闸门**：Pre-Compaction Memory Flush 仅允许自动沉淀 `source="user"` 规则或 `scope="world"` 营地约定，严禁向 `global/` 自动沉淀 `inferred` 禁令（防止 Bootstrap Poisoning）。
4. **能力指纹自动失效（`capabilities_hash`）**：根据当前 `target_types` / `SUPPORTED` / `capabilities` 生成指纹写入 frontmatter；代码或 Mod 升级导致指纹变化后，旧 `inferred` 条目自动标注 `[stale-capabilities: verify live or delete]`，而 `source="user"` 玩家偏好不受影响。
5. **控制面 / 数据面隔离**：记忆索引包裹在低优先级 `<agent_memory_notes priority="low">` 区块中，明确事实优先级：**实时能力与快照 > 玩家最新消息 > 记忆笔记**。

---

## 二、现代上下文压缩 Harness（`ContextHarness`）

实现位于 `python/mcp_agent/context_harness.py`：

- **Append-Only 保护 Prompt Cache**：不再使用固定 3 轮滑动窗口，而是按 `world_id` 持久追加完整对话轮次。
- **基于真实 `prompt_tokens` 阈值触发压缩**：默认窗口 `128,000` tokens（可通过环境变量 `CONTEXT_WINDOW_TOKENS` 覆盖），达到 75%（`96,000`）或字符上限时触发一次压缩，将较早轮次折叠为结构化 `compacted_summary`，原样保留最近 `4` 轮（`keep_recent=4`）。
- **Pre-Compaction Memory Flush**：压缩的同时筛选 0–2 条真正长期有效的玩家规则/营地约定写入 `MEMORY.md`。
- **离线/异常降级**：若压缩请求失败，自动回退到确定性摘要，不中断主循环。

---

## 三、事件驱动外部 Agent 回调（`--backend`）

实现位于 `python/mcp_agent/external_backends.py`，玩家端与伙伴端均支持：

```powershell
# 默认：轻量自研 DeepSeek Harness
python .\python\mcp_agent\companion_agent.py --execute
python .\python\mcp_agent\mcp_agent.py --loop --execute

# Claude Code CLI（stdin 管道传参，按 world_id 自动 --resume 会话）
python .\python\mcp_agent\companion_agent.py --execute --backend claude
python .\python\mcp_agent\mcp_agent.py --loop --execute --backend claude

# Codex CLI（exec --json -，按 world_id 自动 resume thread）
python .\python\mcp_agent\companion_agent.py --execute --backend codex

# DSH CLI（默认调用 dsh-tui run --output-format json，通过 stdin 传参并按 --session-id 续会）或本地 HTTP 回调
python .\python\mcp_agent\companion_agent.py --execute --backend dsh
python .\python\mcp_agent\companion_agent.py --execute --backend dsh-http --dsh-url http://127.0.0.1:PORT/path
```

- **仅在需要规划的事件发生时拉起一次外部进程**，新玩家指令到来时立即终止过期的后台子进程。
- **Windows 兼容**：`claude` / `codex` 通过 `stdin` 传递大体积状态 JSON（绕开 `cmd.exe` 8,191 字符限制），并自动将 `.ps1` 解析为同目录 `.cmd`/`.exe`。
- 如果使用 **GUI 桌面版**（DSH 桌面端 / Codex App 等），则直接在 GUI 的 MCP 配置中挂载 `mcp_server.py` 或 `companion_server.py` 即可。

---

## 四、两端能力对齐与执行改进摘要

- **玩家端（`dst_player_agent`）**：
  - 新增显式 `equip` / `unequip` / `eat` / `drop_item` 任务目标与原生客户端执行（支持主背包与溢出背包）。
  - 升级为与伙伴端一致的 **`dialogue_revision` 后台异步规划**：执行任务期间发普通 `@自己 ...` 消息不中断当前动作，仅显式 `停止/暂停/取消` 或 HUD 暂停才立刻停下。
  - 容器存取扩展至 `treasurechest` / `dragonflychest` / `icebox`，并支持 `transfer_count`（1..40）单次批量存取。
- **伙伴端（`dst_buddy_agent`）**：
  - 补齐 `refuel`（份数与比例维护、兼容潮湿燃料）、`set_home` / `return_home` / `stay_home`（含驻留时长与 `end_at`）、`hunger_ratio`（自动避开任务预留料理）、`cook_meal`（`buddy_meals` 查询 + 装料/开煮/收菜）、`ignite_tree`（支持常青树/桦树/多枝树，点火后即时撤离火焰、等待烧尽后砍焦树收炭）、自定义 `move_to.arrival_radius`、建筑相对位置约束 `structure.placement`（石头火坑免受 4 格野火禁放区限制）。
  - 容器存取支持统一 `building_id`、扩展至 `icebox` / `dragonflychest`，并在单次开箱周期内批量搬运。
  - 合并删除历史冗余的 `companion_team_*` 与 `action_sequence.py` 模块，两端共用 `planning_client.run_planning_loop()`。
