name = "DST Buddy Agent"
description = [[
在世界里召唤最多 2 名可命名的 AI 威尔逊伙伴，陪你采集、砍树、挖矿、烧炭、寻物、建造、存取箱子、烹饪与长期自主生存！支持伙伴 HUD 面板、/buddy 指令和 @伙伴名 / @all 聊天对话。

【创意工坊自包含一键安装（无需另去 GitHub 下载）】
1. 进游戏聊天栏输入 /buddy setup 查看本模组在电脑上的精确目录路径；
2. 打开该目录双击 1_install_agent.bat（macOS/Linux 运行 1_install_agent.sh），将内置 Python 控制器安装到本机独立目录（支持创建桌面快捷方式，工坊更新不丢配置）；
3. 双击 2_run_buddy.bat（或桌面快捷方式）启动并自动复制配对口令，回游戏按 Y 粘贴 /buddy pair <token> 回车（纯本地拦截，绝不发到公屏或服务器），再输入 /buddy summon 名字 召唤伙伴！

Summon up to two named AI Wilson companions for gathering, crafting, building, storage, cooking, and autonomous survival.
Self-contained Workshop setup: type /buddy setup in-game to see the mod folder, run 1_install_agent.bat (or .sh), then 2_run_buddy.bat, and paste /buddy pair <token> in chat (local-only, persists across Workshop updates).
Open-source repo: https://github.com/wjc1207/DST-Hierarchical-Agent
]]
author = "Retard Wang"
version = "0.8.0"
forumthread = ""

icon_atlas = "modicon.xml"
icon = "modicon.tex"

api_version = 10
dst_compatible = true
client_only_mod = false
all_clients_require_mod = true
server_filter_tags = {"ai", "agent", "companion", "buddy"}
