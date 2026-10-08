# syntax=docker/dockerfile:1
#
# vibe-agent 容器镜像。
#
# 设计目标：在任意机器上 docker run 就能跑。
#   * 不假设宿主机装了 Python / Git / Node / Chrome
#   * 可选依赖用构建参数控制体积：WITH_NODE、WITH_BROWSER
#   * 用户 UID/GID 在启动时对齐宿主机，挂载卷不会产生 root 属主的文件
#
# 构建示例：
#   docker build -t vibe-agent .                                  # 全功能（推荐）
#   docker build -t vibe-agent --build-arg WITH_BROWSER=0 .       # 精简，不装浏览器
#
# 运行时示例：
#   docker run --rm -it -v "$PWD:/workspace" -e VA_API_KEY=sk-xxx vibe-agent

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm

ARG WITH_NODE=1
ARG WITH_BROWSER=1
ARG NODE_MAJOR=22

# DEBIAN_FRONTEND 用 ENV 而不是 ARG。
# 虽然 ARG 在 RUN 里也会作为环境变量生效，但那是公认的坑：
# playwright install --with-deps 和 apt-get 都会开子进程装包，
# ENV 能确保它们全都吃到 noninteractive，不会卡在交互式提问上。
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    VA_APP_DIR=/app \
    VA_HOME=/home/va \
    VA_WORKSPACE=/workspace \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PYTHONPATH=/app

# ---------------------------------------------------------------- 基础工具
# gosu  —— 启动时从 root 降权到普通用户（比 su 的信号处理好）
# tini  —— PID 1 收割僵尸进程，Ctrl+C 能正常传递
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        less \
        ripgrep \
        gosu \
        tini \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------- Node（可选）
# 直接取官方 tarball 而不是加 apt 源，省掉 gnupg 和 keyring 那一堆事。
#
# 开关接受 1/true/yes/on —— 只认字面 "1" 的话，`--build-arg WITH_NODE=true`
# 会静默跳过安装，用户还以为装上了。
RUN case "$WITH_NODE" in \
        1|true|TRUE|yes|YES|on|ON) \
            set -eux; \
            arch="$(dpkg --print-architecture)"; \
            case "$arch" in \
                amd64) narch=x64 ;; \
                arm64) narch=arm64 ;; \
                *) echo "不支持的架构: $arch" >&2; exit 1 ;; \
            esac; \
            base="https://nodejs.org/dist/latest-v${NODE_MAJOR}.x"; \
            sums="$(curl -fsSL "${base}/SHASUMS256.txt")"; \
            file="$(printf '%s\n' "$sums" \
                    | grep -o "node-v[0-9.]*-linux-${narch}\.tar\.xz" | head -1)"; \
            if [ -z "$file" ]; then \
                echo "没能从 SHASUMS256.txt 解析出 linux-${narch} 的 Node 包名" >&2; \
                exit 1; \
            fi; \
            expected="$(printf '%s\n' "$sums" | grep " ${file}\$" | awk '{print $1}')"; \
            if [ -z "$expected" ]; then \
                echo "SHASUMS256.txt 里没有 ${file} 的校验和" >&2; \
                exit 1; \
            fi; \
            curl -fsSL "${base}/${file}" -o /tmp/node.tar.xz; \
            echo "${expected}  /tmp/node.tar.xz" | sha256sum -c -; \
            tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1; \
            rm -f /tmp/node.tar.xz; \
            node --version; \
            npm --version ;; \
        *) \
            echo "WITH_NODE=$WITH_NODE，跳过 Node 安装" >&2 ;; \
    esac

# ---------------------------------------------------------------- 普通用户
RUN useradd --create-home --uid 1000 --shell /bin/bash va \
    && mkdir -p /workspace \
    && chown va:va /workspace

WORKDIR /app

# ---------------------------------------------------------------- Python 依赖
# 先只拷依赖清单，让这一层能被缓存住 —— 改代码不用重装依赖。
COPY requirements.txt requirements-browser.txt requirements-docs.txt ./

RUN pip install --no-cache-dir -r requirements.txt

# 文档解析依赖很小（几百 KB），不值得做成可选开关 —— 读需求文档是核心场景，
# 缺了它 agent 连 Word/PDF 都打不开。
RUN pip install --no-cache-dir -r requirements-docs.txt

# ---------------------------------------------------------------- 浏览器（可选）
# 用 Playwright 官方的 --with-deps，让它自己装 Chromium 需要的系统库
# （它会顺带装上 fonts-wqy-zenhei 等 CJK 字体，中文截图不会变豆腐块）。
# 装到 PLAYWRIGHT_BROWSERS_PATH，这样非 root 用户也能读到。
RUN case "$WITH_BROWSER" in \
        1|true|TRUE|yes|YES|on|ON) \
            set -eux; \
            pip install --no-cache-dir -r requirements-browser.txt; \
            mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"; \
            playwright install --with-deps chromium; \
            chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"; \
            touch "$PLAYWRIGHT_BROWSERS_PATH/.installed"; \
            rm -rf /var/lib/apt/lists/* ;; \
        *) \
            echo "WITH_BROWSER=$WITH_BROWSER，跳过浏览器安装（可用 VA_INSTALL_BROWSER=1 在运行时补装）" >&2 ;; \
    esac

# ---------------------------------------------------------------- 应用代码
COPY --chown=va:va . .

RUN chmod +x /app/docker/entrypoint.sh \
    && python3 -c "import va; print('vibe-agent', va.__version__)"

# ---------------------------------------------------------------- 启动
# 保持 root 启动：entrypoint 需要 root 权限来对齐 UID/GID 和补装依赖，
# 处理完会用 gosu 降权到 va 用户再执行真正的命令。
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["python3", "-m", "va", "-C", "/workspace"]

LABEL org.opencontainers.image.title="vibe-agent" \
      org.opencontainers.image.description="从零构建的终端编码 agent，支持 UI 渲染还原闭环" \
      org.opencontainers.image.source="https://github.com/example/vibe-agent"
