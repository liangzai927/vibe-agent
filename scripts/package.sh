#!/usr/bin/env bash
#
# 打一个可以拷到别的机器的包。
#
#   ./scripts/package.sh                  # 从 git 已提交内容打包（推荐，绝不带密钥）
#   ./scripts/package.sh --working-tree   # 连未提交的改动一起打
#   ./scripts/package.sh --out /tmp/x.tar.gz
#
# 默认走 `git archive`：它**只包含已跟踪的文件**，所以 .env（密钥）、.venv
# （本机二进制）、.va（会话记录）这些天然进不来。用 --working-tree 时会显式排除它们。
#
# 包很小（约 200KB，纯源码），架构无关——Docker 镜像才是分架构的。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="git"
OUT=""
KEEP_SESSIONS=0

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

选项：
  --working-tree   打包工作区当前内容（含未提交改动），而不是 git 已提交的内容
  --out PATH       指定输出路径（默认 ./dist/vibe-agent-<版本>.tar.gz）
  --with-sessions  连 .va/ 会话记录一起打包（默认不带）
  -h, --help       显示这份帮助
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --working-tree) MODE="worktree" ;;
        --out) OUT="${2:-}"; shift ;;
        --with-sessions) KEEP_SESSIONS=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# ------------------------------------------------------------------ 版本号

VERSION="$(python3 -c "import sys; sys.path.insert(0, '.'); import va; print(va.__version__)" 2>/dev/null || echo "0.0.0")"
if SHA="$(git rev-parse --short HEAD 2>/dev/null)"; then
    LABEL="${VERSION}-${SHA}"
else
    LABEL="${VERSION}-$(date +%Y%m%d)"
fi

OUT="${OUT:-dist/vibe-agent-${LABEL}.tar.gz}"
mkdir -p "$(dirname "$OUT")"
OUT_ABS="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"

# ------------------------------------------------------------------ 安全检查

if [ "$MODE" = "git" ]; then
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        echo "✗ 这里不是 git 仓库，改用 --working-tree" >&2
        exit 1
    fi
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo "⚠ 有未提交的改动，git archive 不会包含它们。要一起打包请加 --working-tree。" >&2
    fi
fi

# ------------------------------------------------------------------ 打包

echo "  版本    $LABEL"
echo "  模式    $([ "$MODE" = git ] && echo 'git 已提交内容' || echo '工作区当前内容')"
echo "  输出    $OUT_ABS"
echo

if [ "$MODE" = "git" ]; then
    git archive --format=tar.gz --prefix="vibe-agent/" -o "$OUT_ABS" HEAD
else
    EXCLUDES=(
        --exclude=.venv --exclude=venv --exclude=__pycache__
        --exclude=.git --exclude='*.pyc' --exclude=.env --exclude='.env.*'
        --exclude=dist --exclude='*.egg-info' --exclude=.pytest_cache
        --exclude=.mypy_cache --exclude=.ruff_cache --exclude=.DS_Store
    )
    [ "$KEEP_SESSIONS" = "1" ] || EXCLUDES+=(--exclude=.va)
    tar "${EXCLUDES[@]}" -czf "$OUT_ABS" -C "$(dirname "$ROOT")" "$(basename "$ROOT")"
fi

# ------------------------------------------------------------------ 结果

SIZE="$(du -h "$OUT_ABS" | cut -f1)"
echo "✓ 打包完成：$OUT_ABS  ($SIZE)"

if command -v shasum >/dev/null 2>&1; then
    CHECKSUM="$(shasum -a 256 "$OUT_ABS" | awk '{print $1}')"
elif command -v sha256sum >/dev/null 2>&1; then
    CHECKSUM="$(sha256sum "$OUT_ABS" | awk '{print $1}')"
else
    CHECKSUM=""
fi

# 最后一道保险：确认包里没有密钥和会话记录。
# 模式要锚定路径边界——用 ".env" 去 grep 会命中 .env.example，
# 用 ".va/" 更是会被正则当成「任意字符 + va/」，把 vibe-agent/va/ 也算进去。
LEAKED=0
for pattern in '(^|/)\.env$' '(^|/)\.va/' '(^|/)\.venv/' '(^|/)id_rsa'; do
    if tar -tzf "$OUT_ABS" | grep -qE "$pattern"; then
        echo "✗ 包里出现了不该有的东西：$pattern" >&2
        tar -tzf "$OUT_ABS" | grep -E "$pattern" | sed 's/^/    /' >&2
        LEAKED=1
    fi
done
if [ "$LEAKED" = "1" ]; then
    rm -f "$OUT_ABS"
    echo "  已删除这个包。请检查打包排除规则。" >&2
    exit 1
fi
echo "✓ 已确认包内不含 .env / .va / .venv"
echo

cat <<EOF
下一步，在目标机器上：

  tar -xzf $(basename "$OUT_ABS")
  cd vibe-agent
  ./scripts/install.sh

API key 不在包里（这是刻意的）。install.sh 会让你现场录入，
或者你也可以先 export VA_API_KEY=... 再跑它。

迁移校验（可选）：
  ${CHECKSUM:+echo "$CHECKSUM  $(basename "$OUT_ABS")" | sha256sum -c -}
EOF

if [ -n "$CHECKSUM" ]; then
    printf '%s  %s\n' "$CHECKSUM" "$(basename "$OUT_ABS")" > "${OUT_ABS}.sha256"
    echo "  校验和已写入 ${OUT_ABS}.sha256"
fi
