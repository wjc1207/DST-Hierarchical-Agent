# DeepSeek 配方查询、通用制作与放置

本功能扩展优先级看板的制作能力。JEV 继续使用原有本地动作候选，不接收新增的通用制作／放置候选，也不接收完整配方目录。

## 配方查询

Lua 从当前游戏的 `AllRecipes` / `GetValidRecipe` 读取有效配方，结合角色 builder 上报材料、科技、解锁／原型条件、缓存建造和本地放置可行性。配方 ID 与产物 ID 分开记录，不依赖 Python 手写配方表。

`data/DSTKnowledge.txt` 仅保留生存、照明、工具来源和探索策略，不再维护材料数量与科技要求的静态配方表；规划提示中的紧急照明材料也要求先查询，不硬编码成本。熟悉的配方同样查询，避免角色、技能或辅助模组改变配方后沿用旧数据。运行时支持范围、专注和预算等控制规则由系统提示与能力字段定义。

MCP 新增只读工具：

```text
query_recipes(query="", recipe_id=null, offset=0, limit=12, refresh=false)
```

- `query` 匹配配方 ID、产物 ID或当前游戏提供的名称。
- `recipe_id` 精确匹配；每页最多 24 条，返回 `total`、`next_offset`。
- 返回当前快照的 `session_id`、`age_seconds`；`refresh=true` 请求新快照。
- 每条记录包含 `ingredients`、`output_count`、`technology`、`known`、`can_prototype`、`has_ingredients`、`buffered`、`supported`、`ready` 和 `reason`。
- 查询不会取得控制权或向游戏提交动作。

DeepSeek 请求中注册同名只读 function tool，以该次规划的不可变游戏快照回答查询。完整目录留在 Python，既不注入初始提示词，也不写入历史对话。一次规划最多 8 轮模型响应，每轮最多 4 次查询；不能无限调用。

## 看板目标

普通制作和建筑仍使用可验证的库存／实体目标，新增 `recipe_id` 绑定：

```json
{
  "goal": "准备木板并建造炼金引擎",
  "tasks": [
    {
      "id": "boards",
      "title": "准备4块木板",
      "priority": 2,
      "target": {"type": "inventory", "prefab": "boards", "count": 4, "recipe_id": "boards"}
    },
    {
      "id": "alchemy",
      "title": "建造炼金引擎",
      "priority": 3,
      "depends_on": ["boards"],
      "target": {"type": "structure", "prefab": "researchlab2", "recipe_id": "researchlab2"}
    }
  ]
}
```

这只是目标格式示例，不是完整材料计划。DeepSeek 必须查询实际配方，明确规划其他材料、中间产物和科技前置；执行器不会自动递归补全材料链。库存数量是当前存量目标，按需重复执行单次制作，直至观察到达标。

最终模型输出会核对该次快照中的配方存在性、支持状态、产物和制作／放置类型。当前未就绪的受支持配方可以作为未来目标，但无法凭空执行。

火把、斧子、镐子、营火、科学机器继续使用原有目标和动作，查询返回 `legacy=true`。这些目标省略 `recipe_id`，使用 `target_prefab`，例如科学机器仍为 `sciencemachine`，真实配方／产物为 `researchlab`。原有工具数量与营火安全规则保留。

## 执行与验证

- Lua 提供 `recipe_<recipe_id>` 动作，携带 `kind=craft|place`、`recipe_id`、`product`、产量和已验证放置点，并标记 `planner_only=true`。
- 继续通过 `start_action(choice, observation_id)` 提交，不新增任意配方执行或任意坐标入口。
- Python 看板执行器按目标选择动作；JEV 的候选和状态投影均过滤 `planner_only` 动作。
- 普通陆地放置在角色周围 2–5 单位搜索合法、可接近且避开已知危险的位置，旋转暂固定为 0。已选位置仍在附近且安全、可建时保持固定，避免角色微动和配方刷新让选址不断漂移；失效后才重新选点。观察后候选位置真正改变时仍拒绝旧动作，不静默换点；仅允许每轴最多 0.0001 世界单位的序列化精度误差。
- 执行前复查材料／科技；放置缓冲后再次检查位置，延迟回调遵守取消代次。已有缓存建筑不再消耗材料。
- 制作必须观察到足额产物进入主背包、装备栏或溢出背包；请求发送／材料减少不算完成。产物落地未入包会超时失败，不假报成功。
- 放置必须观察到目标位置附近出现此前不存在、产物 ID 匹配的新实体。已有同类建筑不会被当作本次动作的结果。
- 建筑实体使用独立扫描，避免常规附近实体数量上限漏掉新建筑；看板完成后仍沿用已验证完成锁存。

配方状态使用渐进索引与库存／科技驱动缓存，旧代次查询显示 `fresh=false`，索引进度见 `index`；原生执行仍实时验证。目录按版本和条目补丁传输，详见 [性能说明](PERFORMANCE.md)。

## 当前边界与更新

支持普通物品制作和通用陆地建筑。角色生命等特殊材料、特殊科技材料、动态产物／产量及非陆地放置等配方不开放通用执行，查询会标注原因。普通科技解锁、附近原型制作仍由游戏原生条件判断；不绕过科技或角色限制。

升级需同时更新 Python、`modmain.lua` 和 `scripts/`（新增 `jev_recipes.lua`），重新进入世界并重启控制循环。桥接协议仍为 4，保留 `.env`、桥接令牌与安装目录原有 `jev_bridge_config.lua`。

离线验证：`python run_tests.py`；Lua 回归运行 `tests/recipes_spec.lua` 及 `tests/exploration_spec.lua`。尚需在真实游戏中验证原型制作、背包复制时序和建筑放置表现。
