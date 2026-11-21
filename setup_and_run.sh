#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

MODE=""
BACKEND=""
CHECK_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --mode) MODE="${2:-}"; shift 2 ;;
        --backend) BACKEND="${2:-}"; shift 2 ;;
        --check-only) CHECK_ONLY=1; shift ;;
        *) shift ;;
    esac
done

echo "============================================================"
echo "  DST Hierarchical Agent 一键安装与启动器 (macOS / Linux)"
echo "============================================================"

find_python() {
    for cmd in python3.12 python3.11 python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            if "$cmd" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

VENV_DIR="$PROJECT_ROOT/.venv-mcp"
VENV_PY="$VENV_DIR/bin/python"

if [ ! -x "$VENV_PY" ]; then
    PY_CMD="$(find_python || true)"
    if [ -z "$PY_CMD" ]; then
        echo "错误：未检测到 Python 3.11+，请先安装 Python 3.11 或更高版本。" >&2
        exit 1
    fi
    echo "正在创建虚拟环境 .venv-mcp ..."
    "$PY_CMD" -m venv "$VENV_DIR"
fi

if ! "$VENV_PY" -c "import mcp" 2>/dev/null; then
    echo "正在安装 MCP 依赖 (python/requirements-mcp.txt)..."
    "$VENV_PY" -m pip install --upgrade pip -q
    if ! "$VENV_PY" -m pip install -r "$PROJECT_ROOT/python/requirements-mcp.txt"; then
        echo "默认 PyPI 安装失败，尝试使用清华镜像源重试..."
        "$VENV_PY" -m pip install -r "$PROJECT_ROOT/python/requirements-mcp.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
    fi
fi

copy_clipboard() {
    local text="$1"
    if command -v pbcopy >/dev/null 2>&1; then
        printf "%s" "$text" | pbcopy && return 0
    elif command -v wl-copy >/dev/null 2>&1; then
        printf "%s" "$text" | wl-copy && return 0
    elif command -v xclip >/dev/null 2>&1; then
        printf "%s" "$text" | xclip -selection clipboard && return 0
    fi
    return 1
}

show_pair() {
    local target="$1"
    local script label cmd
    if [ "$target" = "buddy" ]; then
        script="$PROJECT_ROOT/python/mcp_server/companion_server.py"
        label="DST Buddy Agent (AI 伙伴)"
    else
        script="$PROJECT_ROOT/python/mcp_server/mcp_server.py"
        label="DST Player Agent (AI 角色操控)"
    fi
    cmd="$("$VENV_PY" "$script" --print-pair-command)"
    echo ""
    echo "[$label] 游戏内配对口令（仅本机有效，请勿发给他人）："
    echo "  $cmd"
    if copy_clipboard "$cmd"; then
        echo "已自动复制到剪贴板！进游戏按 Y 打开聊天栏粘贴回车即可完成配对。"
    else
        echo "请复制上方整行命令，进游戏按 Y 打开聊天栏粘贴回车即可配对。"
    fi
    echo ""
}

ensure_deepseek_env() {
    if [ -n "${DEEPSEEK_API_KEY:-}" ]; then return 0; fi
    if [ -f "$PROJECT_ROOT/.env" ] && grep -Eq '^[[:space:]]*DEEPSEEK_API_KEY=[[:space:]]*[^[:space:]#]+' "$PROJECT_ROOT/.env"; then
        return 0
    fi
    echo ""
    echo "尚未检测到 DEEPSEEK_API_KEY（仅保存在本机 .env，不会上传）。"
    read -r -p "请粘贴你的 DeepSeek API Key: " api_key
    if [ -z "$api_key" ]; then
        echo "未输入 DEEPSEEK_API_KEY，已取消。" >&2
        exit 1
    fi
    if [ ! -f "$PROJECT_ROOT/.env" ]; then
        cat > "$PROJECT_ROOT/.env" <<EOF
DEEPSEEK_API_KEY=$api_key
DEEPSEEK_API_URL=https://api.deepseek.com/chat/completions
DEEPSEEK_MODEL=deepseek-flash
EOF
    else
        printf "\nDEEPSEEK_API_KEY=%s\n" "$api_key" >> "$PROJECT_ROOT/.env"
    fi
    echo "已保存到本机 .env。"
}

if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "运行环境已就绪：$VENV_PY"
    exit 0
fi

if [ -z "$MODE" ]; then
    echo ""
    echo "请选择要启动的模式："
    echo "  [1] DST Buddy Agent  - AI 伙伴模式（默认）"
    echo "  [2] DST Player Agent - AI 角色操控模式"
    echo "  [3] 仅显示游戏内配对口令"
    read -r -p "请输入编号 [默认 1]: " mode_choice

    case "${mode_choice:-1}" in
        1) MODE="buddy" ;;
        2) MODE="player" ;;
        3)
            show_pair player
            show_pair buddy
            exit 0
            ;;
        *) echo "无效选项：$mode_choice" >&2; exit 1 ;;
    esac
fi

if [ -z "$BACKEND" ]; then
    echo ""
    echo "请选择规划后端："
    echo "  [1] DeepSeek API（默认）"
    echo "  [2] Claude Code CLI (--backend claude)"
    echo "  [3] Codex CLI (--backend codex)"
    echo "  [4] DSH CLI (--backend dsh)"
    read -r -p "请输入编号 [默认 1]: " backend_choice

    case "${backend_choice:-1}" in
        1) BACKEND="deepseek" ;;
        2) BACKEND="claude" ;;
        3) BACKEND="codex" ;;
        4) BACKEND="dsh" ;;
        *) echo "无效选项：$backend_choice" >&2; exit 1 ;;
    esac
fi

if [ "$BACKEND" = "deepseek" ]; then
    ensure_deepseek_env
fi

show_pair "$MODE"
echo "正在启动控制器（按 Ctrl+C 停止）..."
if [ "$MODE" = "buddy" ]; then
    exec "$VENV_PY" "$PROJECT_ROOT/python/mcp_agent/companion_agent.py" --execute --backend "$BACKEND"
else
    exec "$VENV_PY" "$PROJECT_ROOT/python/mcp_agent/mcp_agent.py" --loop --execute --backend "$BACKEND"
fi
