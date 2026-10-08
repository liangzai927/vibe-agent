#!/usr/bin/env bash
#
# 容器入口。
#
# 做四件事，每一件都是为了「在任何机器上都能跑」：
#
#   1. 把容器内的 va 用户对齐到宿主机的 UID/GID。不对齐的话，挂载进来的
#      工作区里生成的文件会归 root 所有，宿主机上既改不动也删不掉。
#   2. 按需补装可选依赖（浏览器），对应「插件在部署/运行时安装」。
#      装过一次会留标记，重启不会再装。
#   3. 落地 git 的 safe.directory。挂载进来的仓库属主通常和容器内用户不一致，
#      不设的话 git 会直接甩一句 "dubious ownership" 拒绝干活。
#   4. 从 root 降权到普通用户执行真正的命令。
#
# 可用环境变量：
#   VA_UID / VA_GID              对齐到宿主机的用户/组 ID（默认 1000）
#   VA_INSTALL_BROWSER=1         运行时补装 Playwright + Chromium
#   VA_GIT_SAFE_DIRECTORY=0      关闭 safe.directory 设置
#   GIT_USER_NAME / GIT_USER_EMAIL   写进容器内 git 全局配置
set -euo pipefail

APP_DIR="${VA_APP_DIR:-/app}"
VA_USER="${VA_USER:-va}"
VA_HOME_DIR="${VA_HOME:-/home/${VA_USER}}"
PUID="${VA_UID:-1000}"
PGID="${VA_GID:-1000}"
BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/ms-playwright}"
BROWSERS_MARKER="${BROWSERS_PATH}/.installed"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# 以 va 用户身份执行（已经是 va 就直接跑）
as_user() {
    if [ "$(id -u)" = "0" ] && id "$VA_USER" >/dev/null 2>&1; then
        gosu "$VA_USER" "$@"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------- 1. 权限对齐
align_user() {
    [ "$(id -u)" = "0" ] || return 0
    id "$VA_USER" >/dev/null 2>&1 || return 0

    local cur_uid cur_gid
    cur_uid="$(id -u "$VA_USER")"
    cur_gid="$(id -g "$VA_USER")"

    if [ "$PGID" != "$cur_gid" ]; then
        groupmod -o -g "$PGID" "$VA_USER"
    fi
    if [ "$PUID" != "$cur_uid" ]; then
        usermod -o -u "$PUID" "$VA_USER"
    fi

    # 只有真的改过才递归 chown，避免每次启动都扫一遍缓存目录
    if [ "$PUID" != "$cur_uid" ] || [ "$PGID" != "$cur_gid" ]; then
        chown -R "$VA_USER:$VA_USER" "$VA_HOME_DIR" 2>/dev/null || true
        log "已对齐到 UID=${PUID} GID=${PGID}"
    fi
}

# ---------------------------------------------------------------- 2. 可选依赖
ensure_optional_deps() {
    [ "${VA_INSTALL_BROWSER:-0}" = "1" ] || return 0
    [ "$(id -u)" = "0" ] || { log "VA_INSTALL_BROWSER 需要以 root 启动容器才能安装系统库，已跳过"; return 0; }

    if ! python3 -c "import playwright" >/dev/null 2>&1; then
        log "运行时安装 playwright …"
        pip install --no-cache-dir -r "${APP_DIR}/requirements-browser.txt"
    fi

    if [ ! -f "$BROWSERS_MARKER" ]; then
        log "下载 chromium（约 150MB，只需一次）…"
        mkdir -p "$BROWSERS_PATH"
        playwright install --with-deps chromium
        chmod -R a+rX "$BROWSERS_PATH"
        touch "$BROWSERS_MARKER"
        log "chromium 就绪"
    fi
}

# ---------------------------------------------------------------- 3. git 配置
prepare_git() {
    if [ "${VA_GIT_SAFE_DIRECTORY:-1}" = "1" ]; then
        as_user git config --global --add safe.directory '*' 2>/dev/null || true
    fi
    if [ -n "${GIT_USER_NAME:-}" ]; then
        as_user git config --global user.name "$GIT_USER_NAME" 2>/dev/null || true
    fi
    if [ -n "${GIT_USER_EMAIL:-}" ]; then
        as_user git config --global user.email "$GIT_USER_EMAIL" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------- main
ensure_workspace() {
    [ -d "$VA_WORKSPACE" ] || mkdir -p "$VA_WORKSPACE"
    if [ "$(id -u)" = "0" ]; then
        chown "$VA_USER:$VA_USER" "$VA_WORKSPACE" 2>/dev/null || true
    fi
}

ensure_workspace
align_user
ensure_optional_deps
prepare_git

# 提示一下 git 身份没配 —— 不然 agent 提交时会失败在很晚的地方
if ! as_user git config --global user.email >/dev/null 2>&1; then
    if [ -n "${GIT_USER_EMAIL:-}" ]; then
        :
    else
        log "提醒：容器内没配 git 身份。需要 agent 提交代码的话，"
        log "      请传 -e GIT_USER_NAME=... -e GIT_USER_EMAIL=..."
    fi
fi

# ---------------------------------------------------------------- 降权执行
if [ "$(id -u)" = "0" ]; then
    exec gosu "$VA_USER" "$@"
fi
exec "$@"
