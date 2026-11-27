name = "DST Buddy Agent"
description = [[
统一服务端 AI 智能体模组：既可以一键托管你的主角（/buddy self、@自己），也可以召唤最多 2 名可命名的 AI 威尔逊伙伴与你联机协作！支持采集、砍树、挖矿、烧炭、寻物、建造、存取箱子、烹饪锅 10 道原版菜谱、基础战斗与护主（自动穿甲换武器+低血撤退）、设家驻留与长期自主生存；并内置视野半径限制与可选模拟延迟配置，兼容 Benchmark 评测。

【创意工坊自包含一键安装（无需另去 GitHub 下载）】
1. 进游戏聊天栏输入 /buddy setup 查看本模组在电脑上的精确目录路径；
2. 打开该目录双击 1_install_agent.bat（macOS/Linux 运行 1_install_agent.sh），将内置 Python 控制器安装到本机独立目录（支持创建桌面快捷方式，工坊更新不丢配置）；
3. 双击 2_run_buddy.bat（或桌面快捷方式）启动并自动复制配对口令，回游戏按 Y 粘贴 /buddy pair <token> 回车（纯本地拦截，绝不发到公屏或服务器），输入 /buddy self 托管自己或 /buddy summon 名字 召唤伙伴！

Unified server-side DST AI agent mod: pilot your own character (/buddy self, @自己) and/or summon up to two named AI Wilson companions for gathering, crafting, building, storage, 10 Crock Pot recipes, combat & defense, and autonomous survival. Includes local vision-radius limits and optional simulated client latency for benchmarking.
Open-source repo: https://github.com/wjc1207/DST-Hierarchical-Agent
]]
author = "Retard Wang"
version = "0.11.0"
forumthread = ""

icon_atlas = "modicon.xml"
icon = "modicon.tex"

api_version = 10
dst_compatible = true
client_only_mod = false
all_clients_require_mod = true
server_filter_tags = {"ai", "agent", "companion", "buddy"}

configuration_options =
{
    {
        name = "scan_radius",
        label = "Local vision radius",
        hover = "Strict server-side local perception radius (no global map cheating).",
        options =
        {
            { description = "10 (tight)", data = 10 },
            { description = "15 (client-like)", data = 15 },
            { description = "20 (default)", data = 20 },
        },
        default = 20,
    },
    {
        name = "action_delay_ms",
        label = "Simulated client latency",
        hover = "Optional delay before executing actions on the server (useful for client-like benchmarks).",
        options =
        {
            { description = "0 ms (default)", data = 0 },
            { description = "100 ms", data = 100 },
            { description = "200 ms", data = 200 },
            { description = "350 ms", data = 350 },
        },
        default = 0,
    },
}
