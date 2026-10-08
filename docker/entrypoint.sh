#!/usr/bin/env bash
#
# 容器入口。
#
# 做五件事，每一件都是为了「在任何机器上都能跑」：
#
#   1. 把容器内的 va 用户对齐到宿主机的 UID/GID。不对齐的话，挂载进来的
#      工作区里生成的文件会归 root 所有，宿主机上既改不动也删不掉。
#   2. 确保工作区存在且可写（只有在写不进去时才动属主，见下方注释）。
#   3. 按需补装可选依赖（浏览器），对应「插件在部署/运行时安装」。
#   4. 落地 git 的 safe.directory 与用户身份。
#   5. 从 root 降权到普通用户执行真正的命令。
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

# 注意：compose 里如果写 `- VA_WORKSPACE`（只有 key 没有值），宿主机又没导出
# 这个变量，docker 会把镜像的 ENV 直接**删掉**。在 set -u 下裸用 $VA_WORKSPACE
# 会让容器以 "unbound variable" 退出 1。所以这里必须带默认值。
WS="${VA_WORKSPACE:-/workspace}"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# 接受 1/true/yes/on，跟代码里的 VA_* 开关保持一致。
# 只认字面 "1" 的话，用户传 `=true` 会被静默忽略。
is_true() {
    case "${1:-}" in
        1 | true | TRUE | yes | YES | on | ON) return 0 ;;
        *) return 1 ;;
    esac
}

# gosu 在 Debian 上装在 /usr/sbin（不是 /usr/bin）。root 的 PATH 通常包含它，
# 但依赖 PATH 太脆 —— 显式解析一次，后面都用绝对路径。
GOSU_BIN=""
for candidate in "$(command -v gosu 2>/dev/null || true)" /usr/sbin/gosu /usr/bin/gosu /sbin/gosu; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        GOSU_BIN="$candidate"
        break
    fi
done

# 以 va 用户身份执行（已经是 va 就直接跑）
as_user() {
    if [ "$(id -u)" = "0" ] && id "$VA_USER" >/dev/null 2>&1; then
        if [ -z "$GOSU_BIN" ]; then
            log "警告：找不到 gosu，将以 root 执行（挂载卷里会留下 root 属主的文件）"
            "$@"
        else
            "$GOSU_BIN" "$VA_USER" "$@"
        fi
    else
        "$@"
    fi
}

# ---------------------------------------------------------------- 1. 权限对齐
# 必须排在任何按用户名 chown 的动作之前 —— 否则会先按旧的 uid(1000) 改了属主，
# 再 usermod 改成宿主机 uid，结果容器用户反而写不了自己的工作区，
# 更糟的是在 Linux 上会把挂载进来的宿主机目录属主改坏。
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

# ---------------------------------------------------------------- 2. 工作区
ensure_workspace() {
    [ -d "$WS" ] || mkdir -p "$WS"

    [ "$(id -u)" = "0" ] || return 0
    id "$VA_USER" >/dev/null 2>&1 || return 0

    # 只在真的写不进去时才动属主。
    # 无条件 chown 很危险：$WS 通常是挂载进来的宿主机目录，属主本来就该是
    # 宿主用户；在 Linux 上 chown 会真的改掉宿主机上那个目录的属主。
    if ! as_user test -w "$WS"; then
        if chown -R "$VA_USER:$VA_USER" "$WS" 2>/dev/null; then
            log "已把 $WS 的属主改为 $VA_USER"
        else
            log "警告：$WS 对 $VA_USER 不可写，且 chown 失败，agent 可能无法写入。"
            log "      可以显式传 -e VA_UID=\$(id -u) -e VA_GID=\$(id -g) 让容器用户与宿主机对齐。"
        fi
    fi
}

# ---------------------------------------------------------------- 3. 可选依赖
ensure_optional_deps() {
    is_true "${VA_INSTALL_BROWSER:-0}" || return 0
    [ "$(id -u)" = "0" ] || {
        log "VA_INSTALL_BROWSER 需要以 root 启动容器才能安装系统库，已跳过"
        return 0
    }

    # 镜像构建时已经装好了（标记在镜像层里），什么都不用做
    if [ -f "${PLAYWRIGHT_BROWSERS_PATH:-/opt/ms-playwright}/.installed" ]; then
        return 0
    fi

    # 运行时补装：装进家目录。compose 里 /home/va 是命名卷，
    # 这样 `docker compose run --rm` 之后不用重下一遍 150MB。
    if ! python3 -c "import playwright" >/dev/null 2>&1; then
        log "运行时安装 playwright …"
        pip install --no-cache-dir -r "${APP_DIR}/requirements-browser.txt"
    fi

    local runtime_path="${VA_HOME_DIR}/.cache/ms-playwright"
    export PLAYWRIGHT_BROWSERS_PATH="$runtime_path"

    if [ ! -f "${runtime_path}/.installed" ]; then
        log "下载 chromium 到 ${runtime_path}（约 150MB，只需一次）…"
        mkdir -p "$runtime_path"
        playwright install --with-deps chromium
        chmod -R a+rX "$runtime_path"
        touch "${runtime_path}/.installed"
        log "chromium 就绪"
    fi
}

# ---------------------------------------------------------------- 4. git 配置
prepare_git() {
    if is_true "${VA_GIT_SAFE_DIRECTORY:-1}"; then
        # 用 --replace-all 而不是 --add。家目录是持久卷，--add 每次启动都追加
        # 一行，.gitconfig 会无限膨胀。
        as_user git config --global --replace-all safe.directory '*' 2>/dev/null || true
    fi
    if [ -n "${GIT_USER_NAME:-}" ]; then
        as_user git config --global user.name "$GIT_USER_NAME" 2>/dev/null || true
    fi
    if [ -n "${GIT_USER_EMAIL:-}" ]; then
        as_user git config --global user.email "$GIT_USER_EMAIL" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------- 5. 参数兜底
# `docker compose run SERVICE COMMAND` 会用 COMMAND **替换** CMD（不是追加）。
# 所以 `docker compose run --rm va doctor` 到了这里，参数就是 ["doctor"]，
# 直接 exec 会去 PATH 里找一个叫 doctor 的可执行文件，然后失败。
# 这里做个兜底：第一个参数如果不是可执行文件，就当成 va 的参数。
if [ "$#" -eq 0 ]; then
    set -- python3 -m va -C "$WS"
elif ! command -v "$1" >/dev/null 2>&1; then
    set -- python3 -m va -C "$WS" "$@"
fi

# ---------------------------------------------------------------- main
align_user
ensure_workspace
ensure_optional_deps
prepare_git

# 提示一下 git 身份没配 —— 不然 agent 提交时会失败在很晚的地方
if ! as_user git config --global user.email >/dev/null 2>&1; then
    if [ -z "${GIT_USER_EMAIL:-}" ]; then
        log "提醒：容器内没配 git 身份。需要 agent 提交代码的话，"
        log "      请传 -e GIT_USER_NAME=... -e GIT_USER_EMAIL=..."
    fi
fi

# ---------------------------------------------------------------- 降权执行
if [ "$(id -u)" = "0" ]; then
    if [ -z "$GOSU_BIN" ]; then
        log "找不到 gosu，无法降权，直接以 root 执行"
        exec "$@"
    fi
    exec "$GOSU_BIN" "$VA_USER" "$@"
fi
exec "$@"
