name = "DST Player Agent"
description = [[
通过本机回环 MCP 桥接让 AI 操控你的角色：支持角色操控 HUD、@自己 聊天指令、长期生存目标与原版动作执行（采集、砍树、挖矿、生火、烹饪、存取箱子、建造、撤退等）。

【创意工坊自包含一键安装（无需另去 GitHub 下载）】
1. 进游戏聊天栏输入 /agent setup 查看本模组在电脑上的精确目录路径；
2. 打开该目录双击 1_install_agent.bat（macOS/Linux 运行 1_install_agent.sh），将内置 Python 控制器安装到本机独立目录（支持创建桌面快捷方式，工坊更新不丢配置）；
3. 双击 2_run_player.bat（或桌面快捷方式）启动并自动复制配对口令，回游戏按 Y 粘贴 /agent pair <token> 回车即可（纯本地拦截，绝不发到公屏）。

Control your DST character through a local loopback MCP bridge with HUD controls, @自己 chat instructions, and long-term survival goals.
Self-contained Workshop setup: type /agent setup in-game to see the mod folder, run 1_install_agent.bat (or .sh), then 2_run_player.bat, and paste /agent pair <token> in chat (local-only, persists across Workshop updates).
Open-source repo: https://github.com/wjc1207/DST-Hierarchical-Agent
]]
author = "Retard Wang"
version = "1.9.0"
forumthread = ""

icon_atlas = "modicon.xml"
icon = "modicon.tex"

api_version = 10
dst_compatible = true
client_only_mod = true
all_clients_require_mod = false

server_filter_tags = {}

configuration_options =
{
    {
        name = "performance_interval",
        label = "Performance log interval",
        hover = "Seconds between compact client FPS summaries; full state dumps are disabled.",
        options =
        {
            { description = "2 seconds", data = 2.0 },
            { description = "5 seconds", data = 5.0 },
            { description = "10 seconds", data = 10.0 },
        },
        default = 5.0,
    },
    {
        name = "scan_radius",
        label = "Nearby scan radius",
        hover = "World units searched for useful resources, light and danger.",
        options =
        {
            { description = "10", data = 10 },
            { description = "15", data = 15 },
            { description = "20", data = 20 },
            { description = "25", data = 25 }
        },
        default = 15,
    },
}
