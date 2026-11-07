# 共用规划与任务层

客户端 Agent 与服务端伙伴已共用规划规则和优先级任务看板。入口仍分别为 `mcp_agent/mcp_agent.py` 和 `mcp_agent/companion_agent.py`，各玩家仍调用自己的模型。

| 层 | 共用实现 | 分别适配 |
|---|---|---|
| 规划 | `planning_core.py`：提示词基本契约、完整轮次历史裁剪、DSTKnowledge 读取、默认模型及输出预算 | 当前状态、能力限制、查询工具 |
| 请求 | 同一个 `request_json_plan`：工具查询、响应校验、一次修复重试、过期取消、用量日志 | 旧客户端保留配方／地点／地图等查询；伙伴已接入服务端配方、已见建筑和局部箱子查询，全图地图尚未迁移 |
| 回复 | 同一个 `validate_reply`：reply/completed/blocked，完成需证据且不可遗留未完成任务 | 客户端完成后停止，伙伴完成后跟随 |
| 任务 | `common/task_board.py` 与 `priority_board.py`：目标格式、校验、依赖、优先级、专注、定时开始、完成记录、合并 | 各端提供目标校验与进度计算 |
| 执行 | 由公共任务层选择可执行任务 | 客户端原生动作与导航；伙伴服务端 RPC |

伙伴通过 `companion_tasks.py` 接入同一个 `PriorityBoardExecutor`，不再自行维护顺序任务调度规则。两端的异步主循环、查询工具和原生动作选择仍分别维护。

## 多伙伴仍复用任务层

0.6.0 在原 `CompanionAgent` 外增加 `CompanionTeamAgent`。本机只有一个真实 `CompanionTeamPlanner`：各角色请求先进入统一队列，一次模型响应包含每名待规划伙伴的原格式看板，校验完整批次后分发。每名伙伴仍用原来的 `CompanionTasks`、`PriorityBoardExecutor` 与原生执行器；模型调用串行，动作并行，等待中的角色不会冻结另一名角色。

团队响应外层为 `team_plan`，角色内层仍是以下 `task_board` 或原 `agent_reply`。每人最多六任务、两专注，任务依赖限于自己看板；队伍状态只共享建筑事实，库存与科技按角色隔离。`@名字` 只递增指定成员版本并暂停其动作，过期规划不会覆盖新指令。详见[多伙伴说明](COMPANION_TEAM.md)。

请求仍复用 `request_json_plan`、历史裁剪和公共知识。首次推理为 medium，修复为 none；单角色原输出预算 4096 不变，团队响应预算为 8192，历史仍保留三轮并受字符上限裁剪。

0.6.2 的团队外层不再输出玩家可见回复。各 `LOCAL_PLAN.reply` 为对应伙伴的第一人称发言，经原 `buddy_reply` 及原生实体输出；共同任务各说分工，指定角色重新规划不会使未指定角色重复发言。公共任务格式与单角色执行器保持复用。

## 伙伴目标格式

```json
{
  "kind": "task_board",
  "goal": "采三根草后返回主人",
  "reply": "我采三根草，再回来。",
  "tasks": [
    {"id": "grass", "title": "Collect grass", "priority": 2, "focus": true,
     "target": {"type": "inventory", "prefab": "cutgrass", "count": 3}},
    {"id": "back", "title": "Return to owner", "priority": 2,
     "depends_on": ["grass"], "target": {"type": "return_owner"}}
  ]
}
```

收集数量是背包总量。先后关系用 `depends_on`，优先级本身不表达依赖。普通任务可以带 `start_at`，专注任务最多两项。这些规则都直接使用公共任务层。

伙伴适配支持：`inventory {prefab,count,recipe_id?,allow_search?}`、`structure {prefab,recipe_id,x,z,radius?}`、`equip {prefab}`、`move_to {x,z,arrival_radius:2.5}`、`return_owner`、`follow`、`stay`。`return_owner` 要实际进入主人 4 单位内；库存、装备、建筑与位置以真实观察判断。依赖满足前，即使后续目标恰好已满足，也不能提前锁定完成，避免出发前就把“采集后返回”标记完成。参见[动作迁移](COMPANION_ACTIONS.md)。

最新服务端 `capabilities` 决定可计划的能力。没有完整地图或饥饿上限遥测时，适配器不会伪造字段。烹饪、战斗、家、全图导航等未实现的伙伴动作仍不开放；0.3.0 提供局部资源搜索，0.4.0 提供物品交接和箱子查存取，0.5.0 提供已见建筑记忆与返回。伙伴当前目标不支持 `duration/end_at`，错误时间属性由公共校验标记为任务失败。旧伙伴原型格式仅保留入口转换兼容，执行仍用公共看板。

## 中断与重新规划

同一个伙伴的新消息会取消过期规划，先用最新观察刷新已完成进度，再交给 DS 修改。合并看板保留未完成任务，删除需 `cancel_task_ids`。换伙伴或会话时清除旧角色看板和对话历史。

看板完成后还需检查完整玩家要求。只有定时／不可推进任务时，使用公共空闲原因触发一次评估；相同等待安排不会反复调用模型，到达开始时间后自动执行。附近没有目标资源时默认局部搜索；搜索未开启或没有安全的新落点时说明限制并归队。

伙伴的 `companion_search.py` 维护局部网格与来源记忆，复用原探索器的资源对应关系；旧客户端的区域图探索器仍独立适配。两端共用 `PriorityBoardExecutor` 的搜索时间／距离、15 秒冷却恢复和新近资源解除冷却的规则。普通任务允许顺路采集其他可执行任务所需资源，专注任务限制搜索和采集范围；空间记忆不会因预算冷却而清空。详见[感知与搜索](COMPANION_PERCEPTION.md)。

## 后续修改

伙伴通过 `progress_evaluator` 将服务端交接回执接入同一个 `PriorityBoardExecutor`。`give_owner/drop_item/deposit/withdraw` 使用与原存取任务一致的累计完成规则；签名包括箱子 GUID，换箱子不继承旧数量。重复结果或同一回执只取累计最大值，数量修订保留进度。`receive_item` 只等待玩家主动给予后的背包总量，不自动采集或从玩家背包取物。详见[交接契约](COMPANION_STORAGE.md)。

模型请求、历史长度、重试、回复规则修改 `planning_core.py`；任务格式和调度修改公共看板与 `PriorityBoardExecutor`；新增实际能力需完善对应执行端及遥测，再开放能力校验。公共层优化仍应运行两端回归测试。

公共层迁移本身只修改 Python。0.2.0 动作迁移同时更新 Lua，需重进世界并重启本机 Python 程序。

伙伴 0.6.4 在完成确认中继续保留同 ID、同目标的历史完成记录；模型校验复用 merge_goals 和执行器当前视图，不创建空执行器重算旧库存。共同规划的单人成果可以先确认，全队 working/completed 状态由有效成员成果约束。旧客户端的批次切换规则保持原样。
