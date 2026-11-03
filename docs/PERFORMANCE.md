# Mod 性能与帧率日志

## 本次日志发现

2026-11-01 最近一次实测会话的 272 条 HTTP 记录中，172 条为状态快照，平均请求体约 1.19 MB。游戏客户端日志累计约 272 MB、89,218 行状态分片。最后一份完整状态包含 974 条配方；配方目录占状态体绝大部分。

旧 Lua 耗时几乎均为 0 或 1000 毫秒，不能据此准确排列热点。原版 `scripts/profiler.lua` 使用 `walltime` 计时并转换为毫秒，因此现在优先采用同一时钟。不存在时保留原实时时钟回退，并标明来源；不用模拟 tick 冒充精确耗时。

## 优化内容

- 自动采样只走 MCP 的 0.2 秒执行检查，不再另外每秒采样、编码并打印整份状态。原生执行观察回调只请求事件快照，不重入昂贵采样。
- 显式诊断输出仍保留，但省略完整配方目录。自动游戏日志仅增加小型 `[JEV_DST_PERF]` 行。
- 配方引擎查询按库存、材料修正和科技变化立即撤回过期动作，再分批刷新；常用配方优先，其他解锁／技能等变化由后台游标复核。执行前原生条件依然实时检查，危险感知和中断没有降频。
- 未改变的配方事实不产生新版本。放置坐标保留在实时动作中，不进入目录；忙碌状态与放置合法性仍每次更新。
- 配方科技表移除无意义的零级项。支持目录增量的服务端只在新版本／首次连接时接收完整目录；其他状态快照引用版本。服务重启、丢失目录或世界切换会要求重发，绝不复用其他世界的目录。旧服务端未声明支持时继续发送完整目录。
- 合并一次重复的附近实体扫描；完整采样后只生成一次桥接控制状态与动作集合。
- 服务端计时白名单补充 `recipes_ms`、`places_ms`，避免新模块耗时被过滤。

相同实测快照的离线序列化比较：配方目录从约 1.17 MB 缩为 0.50 MB；不变目录的状态快照约 29 KB。该比较不是新版本的实测带宽或 FPS。974 配方的离线稳定场景中，10 次采样的配方状态查询由 9,740 次变为 974 次。

## FPS 日志

在模组设置中选择 **Performance log interval**，默认 5 秒，可选 2／5／10 秒。

游戏 `client_log.txt` 会出现：

```text
[JEV_DST_PERF]{"source":"frontend_wall_update","available":true,"fps":29.8,"frame_ms":33.6,"p95_frame_ms":49.2,"max_frame_ms":105.1,"frames":150,"window_seconds":5.03}
```

数值仅为格式示例。

- `fps`：客户端 FrontEnd 的墙钟更新频率估计，按真实时间窗口统计；不是固定模拟 tick 率，也不是 GPU Present 计数器。
- `frame_ms`：窗口平均帧间隔。
- `max_frame_ms`：窗口最大帧间隔，可观察明显卡顿。
- `p95_frame_ms`：最近最多 600 个帧间隔的 95 分位。
- `window_seconds` / `frames`：统计窗口和间隔数量。
- `age_seconds`：有高精度时钟时，最新统计窗口的年龄。

使用高精度 `walltime` 的相邻 FrontEnd 更新间隔；没有该时钟时使用 FrontEnd 的 `dt`。没有统计窗口／界面更新源时保持未知，不输出伪造的零 FPS。后台、暂停和界面状态会影响这个更新频率，比较时应保持条件一致。

同一指标随快照及心跳写入 MCP 服务日志 `logs/mcp_transport.jsonl` 的 `lua.performance`；阶段耗时仍在 `lua.sampling` 和 `lua.refresh` 中。这样即使世界状态没有变化，也能持续记录 FPS。

## 更新与验证

同步更新 `modmain.lua`、`modinfo.lua`、`scripts/jev_recipes.lua`、`scripts/jev_bridge.lua`、新增的 `scripts/jev_performance.lua`，以及 Python 的 `mcp_server/game_bridge.py` 和 `mcp_server/transport_metrics.py`。重新进入世界并重启 Python，不覆盖 `.env`、桥接令牌或旧日志。

Python 回归：`python run_tests.py`。Lua 回归包括 `tests/performance_spec.lua`、`tests/recipes_spec.lua`、`tests/places_spec.lua`、`tests/exploration_spec.lua`。

尚需新一轮实机测试确认 FPS 改善幅度。建议在同一存档、同一地点和显示设置下采集数分钟，比较平均 FPS、最大帧间隔以及 `recipes_ms`、`places_ms`、`navigation_ms` 和编码耗时。

## 第二轮：削减刷新与编码峰值

第一轮后实测：控制运行期间平均约 57.9 FPS，平均快照约 59 KB；但配方刷新峰值约 63 ms，JSON 编码峰值约 92 ms，仍有卡顿。

第二轮将引擎配方查询拆为每次采样最多 32 条，并在有 `walltime` 时采用约 3 ms 的扫描预算。一条原生调用无法从中途打断，因此不是绝对的单帧耗时上限。批内共享库存与科技数据，避免逐配方重复读取。

目录启动时逐步建立，常用工具、材料和科学建筑优先。`query_recipes` 返回 `index` 进度；`complete=false` 时未查到的条目可能尚未索引。其他配方的复核周期随目录大小和机器速度变化，不再承诺全部配方两秒内刷新。高频库存变化不会重置后台游标，慢调用也不会无限饿死后续条目。

每个配方标记事实代次；查询将旧代次结果显示为 `fresh=false, ready=false, reason=recipe_refresh_pending`。未刷新条目不生成制作动作，提交后的动作仍实时检查。此变化只影响规划信息的新鲜度和可用性，不降低危险检查频率。

服务端声明支持时，客户端只发送变化条目与删除列表，附带基准版本。服务器基准不匹配、重启或更换世界时请求完整重同步。传输行对象不可变，库存变化不会导致全部 974 条记录重新编码；旧服务端仍使用完整目录兼容路径。

本轮额外需同步 Python 的 `common/recipes.py`、`mcp_agent/daily_planner.py`、`mcp_server/mcp_server.py`，以及更新后的 `mcp_server/game_bridge.py`、`modmain.lua`、`scripts/jev_bridge.lua`、`scripts/jev_recipes.lua`。重新进入世界并重启控制程序，实测 FPS 日志后才能评价改善幅度。
