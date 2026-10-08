#!/usr/bin/env bash
#
# 在目标机器上一条命令装好。
#
#   ./scripts/install.sh                  # 交互式：会问你要不要装浏览器、让你录 key
#   ./scripts/install.sh --yes            # 全自动，key 从 VA_API_KEY 环境变量取
#   ./scripts/install.sh --no-browser     # 不装渲染能力（只用读写代码/跑测试）
#   ./scripts/install.sh --with-chromium  # 强制下载 Playwright 自带 Chromium
#
# 做的事：建 venv → 装依赖 →（可选）配好浏览器 → 录 API key → 自检。
# 不需要 root；除了 pip 下载依赖，不改动系统里的任何东西。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ASSUME_YES=0
BROWSER_MODE="ask"   # ask | chrome | chromium | none
VENV=".venv"

usage() {
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

选项：
  -y, --yes           全程不提问（key 从 VA_API_KEY 环境变量取）
  --no-browser        跳过浏览器相关依赖
  --with-chromium     强制下载 Playwright 自带 Chromium（约 150MB）
  --use-system-chrome 强制使用系统已装的 Chrome/Edge
  --venv PATH         虚拟环境目录（默认 .venv）
  -h, --help          显示这份帮助
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        --no-browser) BROWSER_MODE="none" ;;
        --with-chromium) BROWSER_MODE="chromium" ;;
        --use-system-chrome) BROWSER_MODE="chrome" ;;
        --venv) VENV="${2:-.venv}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

step() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

echo
echo "  vibe-agent 安装"
echo "  目录 $ROOT"

# ------------------------------------------------------------------ 1. Python

step "检查 Python"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
[ -n "$PYTHON" ] || die "找不到 Python 3.10 或更高版本。先装一个：https://www.python.org/downloads/"
ok "$("$PYTHON" --version)  ($(command -v "$PYTHON"))"

# ------------------------------------------------------------------ 2. venv

step "创建虚拟环境"
if [ -d "$VENV" ]; then
    ok "$VENV 已存在，复用"
else
    "$PYTHON" -m venv "$VENV" || die "创建 venv 失败"
    ok "已创建 $VENV"
fi
VENV_PY="$VENV/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="$VENV/Scripts/python.exe"   # Windows Git Bash
[ -x "$VENV_PY" ] || die "找不到 venv 里的 python"
"$VENV_PY" -m pip install -q --upgrade pip 2>/dev/null || warn "pip 升级失败，继续"
ok "pip $("$VENV_PY" -m pip --version | awk '{print $2}')"

# ------------------------------------------------------------------ 3. 依赖

step "安装依赖"
"$VENV_PY" -m pip install -q -r requirements.txt || die "核心依赖装不上"
ok "核心依赖"

"$VENV_PY" -m pip install -q -r requirements-docs.txt 2>/dev/null \
    && ok "文档解析（Word / PDF）" \
    || warn "文档解析依赖没装上，读 .docx/.pdf 会失败（不影响其他功能）"

if [ "$BROWSER_MODE" = "none" ]; then
    warn "按要求跳过浏览器，render_page / read_design 将不可用"
else
    if "$VENV_PY" -m pip install -q -r requirements-browser.txt 2>/dev/null; then
        ok "浏览器控制（playwright + Pillow）"
    else
        warn "playwright/Pillow 没装上，UI 渲染与截图对比不可用"
        BROWSER_MODE="none"
    fi
fi

# ------------------------------------------------------------------ 4. 浏览器

find_system_chrome() {
    for p in \
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
        "/Applications/Chromium.app/Contents/MacOS/Chromium"
    do
        [ -x "$p" ] && { printf '%s' "$p"; return 0; }
    done
    for c in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge; do
        if command -v "$c" >/dev/null 2>&1; then command -v "$c"; return 0; fi
    done
    return 1
}

CHROME_PATH=""
BROWSER_CHANNEL=""

if [ "$BROWSER_MODE" != "none" ]; then
    step "配置浏览器"
    if CHROME_PATH="$(find_system_chrome)"; then
        ok "发现系统浏览器：$CHROME_PATH"
    else
        warn "系统里没找到 Chrome / Edge"
    fi

    if [ "$BROWSER_MODE" = "ask" ]; then
        if [ -n "$CHROME_PATH" ]; then
            printf '  用系统浏览器就够了，还要额外下载 Chromium（约 150MB）吗？[y/N] '
            read -r answer || answer="n"
            case "$answer" in y|Y|yes|YES) BROWSER_MODE="chromium" ;; *) BROWSER_MODE="chrome" ;; esac
        else
            printf '  没有系统浏览器，下载 Playwright 自带 Chromium（约 150MB）？[Y/n] '
            read -r answer || answer="y"
            case "$answer" in n|N|no|NO) BROWSER_MODE="none" ;; *) BROWSER_MODE="chromium" ;; esac
        fi
    fi

    if [ "$BROWSER_MODE" = "chromium" ]; then
        echo "  下载 Chromium（可能需要几分钟）…"
        if "$VENV/bin/playwright" install chromium 2>/dev/null; then
            ok "Chromium 已安装"
        else
            warn "Chromium 下载失败。可以稍后手动跑：$VENV/bin/playwright install chromium"
        fi
    elif [ "$BROWSER_MODE" = "chrome" ] && [ -n "$CHROME_PATH" ]; then
        BROWSER_CHANNEL="chrome"
        ok "将使用系统浏览器"
    fi
fi

# ------------------------------------------------------------------ 5. API key

step "配置 API key"
ENV_FILE=".env"

set_env() {
    "$VENV_PY" - "$ENV_FILE" "$1" "$2" <<'PY'
import sys, pathlib
path, key, value = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
out, found = [], False
for line in lines:
    if line.strip().startswith(f"{key}=") or line.strip().startswith(f"# {key}="):
        out.append(f"{key}={value}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"{key}={value}")
path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
try:
    path.chmod(0o600)
except OSError:
    pass
PY
}

WROTE_KEY=0
if [ -n "${VA_API_KEY:-}" ]; then
    set_env VA_API_KEY "$VA_API_KEY"
    ok "已从环境变量 VA_API_KEY 写入 .env"
    WROTE_KEY=1
elif [ "$ASSUME_YES" = "1" ]; then
    warn "没有 VA_API_KEY 环境变量，跳过（之后可以手动写进 .env）"
elif [ -f "$ENV_FILE" ] && grep -qE '^VA_API_KEY=.+' "$ENV_FILE"; then
    ok ".env 里已有 key，保持不变"
    WROTE_KEY=1
else
    echo "  key 不会显示在屏幕上，也不会进 shell 历史。"
    printf '  粘贴 API key（直接回车跳过）: '
    read -rs API_KEY || API_KEY=""
    echo
    if [ -n "$API_KEY" ]; then
        set_env VA_API_KEY "$API_KEY"
        ok "已写入 $ENV_FILE（权限 600）"
        WROTE_KEY=1
    else
        warn "跳过了。之后可以手动创建 .env，或参考 .env.example"
    fi
fi

if [ -z "${VA_BASE_URL:-}" ] && [ -f "$ENV_FILE" ] && ! grep -qE '^VA_BASE_URL=.+' "$ENV_FILE"; then
    echo "VA_BASE_URL=https://api.deepseek.com/v1" >> "$ENV_FILE"
    echo "VA_MODEL=deepseek-chat" >> "$ENV_FILE"
    ok "已写入默认 base_url / model（DeepSeek），要换别的服务改 .env"
fi

if [ -n "$BROWSER_CHANNEL" ]; then
    set_env VA_BROWSER_CHANNEL "$BROWSER_CHANNEL"
    ok "已记录浏览器通道：$BROWSER_CHANNEL"
fi

if [ ! -f .gitignore ] || ! grep -q '^\.env$' .gitignore 2>/dev/null; then
    warn ".gitignore 里没有 .env —— 确认一下别把密钥提交出去"
fi

# ------------------------------------------------------------------ 6. 收尾

step "环境自检"
"$VENV_PY" run.py doctor --quick || true

VRUN="$VENV/bin/python"
[ -x "$VRUN" ] || VRUN="$VENV/Scripts/python.exe"

cat <<EOF

  ────────────────────────────────────────────────
  装好了。日常用法：

    cd $ROOT
    $VRUN run.py                      # 对着当前目录开工
    $VRUN run.py -C /你的/项目         # 指定项目
    $VRUN run.py doctor               # 随时自检

  想少敲一点，加个别名：

    echo "alias va='$ROOT/$VENV/bin/python $ROOT/run.py'" >> ~/.zshrc
    source ~/.zshrc
    cd /你的/项目 && va

  第一次进项目建议先跑 \`/init\` 生成 AGENTS.md——
  agent 靠它了解你的项目约定。
  ────────────────────────────────────────────────

EOF

[ "$WROTE_KEY" = "1" ] || warn "还没有可用的 API key，agent 现在起不来"
