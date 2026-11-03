# 营地建筑位置约束

建筑任务可通过 `target.placement` 指定已有建筑锚点和允许半径。任务说明中的“在家”
和顶层 `camp_site` 是规划信息，不能代替这个执行约束。

```json
{
  "id": "camp_firepit",
  "title": "Build a firepit near the science machine",
  "priority": 1,
  "focus": true,
  "target": {
    "type": "structure",
    "prefab": "firepit",
    "recipe_id": "firepit",
    "placement": {"anchor_id": "building:12345", "radius": 10}
  }
}
```

示例建筑 ID 必须替换为 `query_places` 返回的实际 ID。锚点必须是 `building:<GUID>`，
半径为 4–20 个世界单位，默认 10；不接受所在地、任意坐标或模型提供的锚点坐标。

## 调度与放置

- 角色在范围外时，调度器复用前往建筑动作，先接近锚点；材料齐备也不会就地建造。
- 进入范围后才发出建筑指令。通用放置会在角色附近重新寻找范围内的安全合法落点，
  不会沿用未绑定范围的旧配方落点。无合法点时失败，不退回范围外建造。
- MCP 根据实际建筑记忆解析锚点坐标，并在命令中冻结。Lua 检查相同建筑和坐标、
  角色所处范围，并在实际放置前复查。建筑消失或移动、角色离开范围时停止。
- 现有碰撞、路径、威胁、科技、照明及驻留范围校验继续有效。缓冲制作后遇到变化，
  可能留下尚未放置的缓冲建筑，不会自动改放到其他地方。

`place`、旧版 `build_campfire` 和 `build_sciencemachine` 均支持该约束。第一座科学
机器尚无建筑锚点，仍采用看图选址、先 `move_to` 后建造的流程。

## 完成与规划

只有相同产物的建筑位于锚点半径内才满足任务。远处同类建筑和全局已建造标记不能
完成该任务。建筑记忆中仍未标记消失的已有建筑可用于复用，避免离开视野后重复建。
同一目标的完成仍会锁存；修改锚点或半径会改变任务签名，重新核验位置。

已有科学机器或合法家锚点后，DS 规划石头火堆、烹饪锅、箱子、炼金引擎、避雷针、
冰箱时必须填写 `placement`，漏写会返回可修正的校验错误。营地营火也应填写；
临时野外应急营火仍可不绑定。提示词要求优先复用范围内的火堆、安排添燃料。

CLI 在任务下显示 `near building:<GUID> radius=10`。JEV 决策范围保持原样。

## MCP 与升级

`start_action` 增加可选参数：

```json
{
  "choice": "recipe_firepit",
  "observation_id": "本次观察凭据",
  "placement": {"anchor_id": "building:12345", "radius": 10}
}
```

它仅适用于建筑动作，表示授权在指定区域内选择合法落点。未提供该参数的普通动作
保留原有语义。看板中有范围约束的任务要求 Python 调度，不支持原生
`auto_execute=true`。游戏状态必须上报 `telemetry.placement_constraints=true`，旧模组
不能静默忽略约束。

更新后需要重新进入世界并重启 Python。离线测试包括 `tests/test_placement.py` 和
`tests/placement_spec.lua`；实机重点检查建筑与锚点的实际距离及边界无合法落点情况。
