# 系统架构

```mermaid
flowchart TB
    subgraph A["Agent · 规划方"]
        I["内置 mcp_agent<br/>读取游戏聊天和模式<br/>调用 DeepSeek，处理重规划"]
        D["DSH<br/>接收指令并规划<br/>可通过 PTC 组合工具调用"]
    end

    subgraph M["MCP Server · 玩家电脑上的 Python 进程"]
        T["公开 MCP 工具<br/>读取规划状态、提交计划、回复游戏聊天<br/>查看看板、增删改单项任务"]
        S["共用 Python 任务服务<br/>展开扁平任务、校验版本、维护各角色看板<br/>后台调度并跟踪进度"]
        P["玩家桥接 · 8765"]
        B["伙伴桥接 · 8766"]
        T <--> S
        S <--> P
        S <--> B
    end

    subgraph G["游戏 Mod · 饥荒 Lua"]
        PL["DST Player Agent · 客户端<br/>读取 HUD 和状态，执行玩家角色动作"]
        BC["DST Buddy Agent · 客户端<br/>读取 HUD，转发命令和结果"]
        BS["DST Buddy Agent · 游戏服务端<br/>感知并执行伙伴动作"]
        BC <-->|Mod RPC| BS
    end

    I <-->|"stdio / MCP"| T
    D <-->|"stdio / MCP"| T
    P <-->|"本机 HTTP"| PL
    B <-->|"本机 HTTP"| BC
```

玩家版和伙伴版各自启动一个 MCP 服务进程，共用任务服务的实现。图中的两个桥接节点表示两种部署入口。每个受控角色维护自己的看板和任务进度。伙伴版只召唤一人时也使用队伍协议。

## 一次内置指令

1. 玩家在游戏 UI 切换模式或在聊天框发送指令。Lua 将控制状态和游戏快照发给本机 MCP Server。
2. `mcp_agent` 调用 `get_planning_state`，读取指令、看板、复核原因和版本，再交给 DeepSeek 规划。
3. Agent 调用 `submit_task_board` 并携带读取到的版本。MCP Server 校验、更新看板，并持续调度。Lua 接收并执行具体动作。
4. Agent 读取最新进度，通过 `send_planning_reply` 在游戏里回复。自主模式继续规划阶段目标，指令模式确认完成后等待下一条指令。

DSH 使用同一套 MCP 工具，也可用 `submit_task` 新增单项任务。游戏动作和看板工具详见 [MCP 接口](MCP.md)。
