# 部署到另一台机器

代码本身只有 **188KB**（`.
venv` 是 229MB，那是本机的，不要带）。所以「部署」这件事的难点不在传输，
而在目标机器的运行环境。

先想清楚一件事：**目标机器上有没有 Docker？**

| | 有 Docker | 没有 Docker |
|---|---|---|
| 推荐方式 | [方式 A：容器](#方式-a容器推荐) | [方式 B：原生 Python](#方式-b原生-python) |
| 目标机器需要 | 只装 Docker | Python 3.10+ |
| 首次耗时 | 构建镜像 5~10 分钟 | 装依赖 2~3 分钟 |
| 磁盘占用 | 约 2GB（含 Chromium） | 约 400MB |

---

## 方式 A：容器（推荐）

目标机器只需要有 Docker，别的什么都不用装。

### A1. 用 compose 构建并运行

把源码拷过去，然后：

```bash
# 在目标机器上
tar -xzf vibe-agent-0.1.0-xxxxxxx.tar.gz
cd vibe-agent

cp .env.example .env      # 填 VA_API_KEY
docker compose build      # 首次 5~10 分钟（含下载 Chromium）
docker compose run --rm va
```

指定要改的仓库：

```bash
WORKSPACE=/path/to/your/repo docker compose run --rm va
```

### A2. 只传镜像（目标机器不用构建）

镜像里什么都有，目标机器连源码都不需要：

```bash
# 在构建机上
docker build -t vibe-agent:0.1.0 .
docker save vibe-agent:0.1.0 | gzip > vibe-agent-image.tar.gz   # 约 700MB~1GB

# 拷过去之后，在目标机器上
gunzip -c vibe-agent-image.tar.gz | docker load
docker run --rm -it \
  -e VA_API_KEY=sk-xxx \
  -e VA_UID=$(id -u) -e VA_GID=$(id -g) \
  -e GIT_USER_NAME="你的名字" -e GIT_USER_EMAIL="you@example.com" \
  -v "$PWD:/workspace" -v vibe-agent-home:/home/va \
  vibe-agent:0.1.0
```

### A3. 跨架构

`docker save` 出来的镜像**绑定架构**。这台机器是 `arm64`（Apple Silicon），
导出的镜像拿到 amd64 的 Linux 服务器上跑不起来。

三个办法：

```bash
# 1. 在目标机器上构建（最省事，推荐）
#    直接把源码拷过去 docker compose build

# 2. 用 buildx 交叉构建
docker buildx build --platform linux/amd64 -t vibe-agent:amd64 --load .

# 3. 构建并推多架构镜像到 registry
docker buildx build --platform linux/amd64,linux/arm64 \
  -t your-registry/vibe-agent:0.1.0 --push .
```

> ⚠️ **这套 Docker 配置在本机从未真正构建过**（开发机没装 Docker）。
> 静态检查都过了、entrypoint 也真跑过，但 `docker build` 全流程没验证。
> 首次构建请留意输出，出问题最可能在 Chromium 那一步或 Node 下载。
> 可以用构建参数逐步排除：
> ```bash
> docker build -t vibe-agent --build-arg WITH_BROWSER=0 --build-arg WITH_NODE=0 .
> ```

---

## 方式 B：原生 Python

目标机器需要 **Python 3.10+**。不需要预先装 git / Node / Chrome。

```bash
# 1. 打包（在开发机上）
cd vibe-agent
./scripts/package.sh
# → dist/vibe-agent-0.1.0-xxxxxxx.tar.gz  (188KB)

# 2. 拷过去（scp / U 盘 / 微信传文件都行）
scp dist/vibe-agent-*.tar.gz user@target:/tmp/

# 3. 在目标机器上安装
tar -xzf /tmp/vibe-agent-*.tar.gz -C ~/apps
cd ~/apps/vibe-agent
./scripts/install.sh
```

`install.sh` 会依次做：查 Python 版本 → 建 `.venv` → 装依赖 →
配浏览器 → 录 API key → 跑自检。**不需要 root**，除 pip 下载外不改动系统。

几个常用开关：

```bash
./scripts/install.sh --yes                # 不提问，key 从 VA_API_KEY 环境变量取
./scripts/install.sh --no-browser         # 只用读写代码/跑测试，不装渲染能力
./scripts/install.sh --with-chromium      # 强制下载 Playwright 自带 Chromium
./scripts/install.sh --use-system-chrome  # 用系统已装的 Chrome/Edge
```

### 浏览器怎么办

UI 相关的工具（`render_page` / `pixel_diff` / `read_design` / `compare_with_design`）
需要一个浏览器。`install.sh` 会自己找：

| 目标机器的情况 | install.sh 怎么做 |
|---|---|
| 有 Chrome / Edge | 直接复用，**不下载**（省 150MB） |
| 没有，但你同意 | `playwright install chromium`，装到 `~/.cache/ms-playwright` |
| 你说 `--no-browser` | 跳过，UI 相关工具不可用，其他功能不受影响 |

`playwright install` 在 Linux 上还需要一些系统库。如果它报缺库：

```bash
sudo .venv/bin/playwright install-deps chromium
```

---

## 方式 C：走 Git

适合目标机器能访问你的代码仓库、并且你要长期维护的情况。

```bash
# 开发机：推到一个远端（GitHub / 内网 GitLab 都行）
git remote add origin git@github.com:you/vibe-agent.git
git push -u origin main

# 目标机器
git clone git@github.com:you/vibe-agent.git
cd vibe-agent
./scripts/install.sh
```

好处是以后 `git pull` 就能升级。`.gitignore` 已经排除了 `.env` 和 `.venv`，
所以密钥不会被推上去。

---

## 密钥怎么带过去

**不要放进包里，也不要提交。** 三种方式，按场景选：

```bash
# 1. 环境变量（适合脚本化部署）
export VA_API_KEY=sk-xxx
./scripts/install.sh --yes

# 2. 交互录入（install.sh 会问，输入不回显、不进 shell 历史）
./scripts/install.sh

# 3. 手动写 .env（复制 .env.example 改）
cp .env.example .env && chmod 600 .env && vi .env
```

Docker 场景下直接在 `docker run` / compose 里传 `-e VA_API_KEY=...`，
或把 `.env` 放在 compose 同目录（compose 会自动读，但**不会**打进镜像——
`.dockerignore` 排除了它）。

`install.sh` 写完 `.env` 会设成 `600` 权限。

---

## 装完怎么确认

```bash
.venv/bin/python run.py doctor
```

```
  vibe-agent 环境自检
  ──────────────────────────────────────────────
  必需
    ✓ Python       3.12.8  (Linux aarch64)
    ✓ 工作区          /home/you/apps/vibe-agent
    ✓ 会话目录         /home/you/apps/vibe-agent/.va/sessions
    ✓ git          git version 2.43.0
    ✓ 模型连通         deepseek-chat   0.81s   → '连通'

  可选
    ✓ 浏览器          Google Chrome
    ✓ playwright   1.49.0
    ✓ Pillow       11.0.0
    – 视觉模型         未配置
        可选。让 agent 把截图和设计图做比对时需要一个多模态模型……

  ✓ 必需项全部通过。
  必需 5/5 · 可选 3/5 可用
```

必需项全绿就能用。退出码可用于脚本：必需项有失败才返回 1。

---

## 升级

```bash
# 容器
docker compose build && docker compose run --rm va

# 原生
./scripts/package.sh          # 开发机重新打包
# 拷过去覆盖，然后重跑：
./scripts/install.sh          # 会复用已有的 .venv，只更新依赖
```

`.env`、`.va/`（会话记录）、`.venv` 都不在包里，覆盖不会被冲掉。

---

## 什么不要带过去

| 东西 | 为什么 |
|---|---|
| `.env` | 里面有你的 API key |
| `.venv/` | 里面是**本机架构**的二进制，换台机器直接报错 |
| `.va/` | 会话记录，通常没必要搬（想接着聊可以 `--with-sessions`） |
| `dist/` | 打包产物本身 |
| `__pycache__/` | Python 版本不同会用不了 |

`./scripts/package.sh` 已经把这些全排除了，并且打完会**再检查一遍**确认没漏。

---

# 发给别人用

**和「部署到我自己另一台机器」是两件事。** 别人需要的是：他自己的 key、
他自己的数据，以及**一条命令就能装好**。

## 先搞清楚：什么会跟着走，什么不会

| | 会不会给对方 |
|---|---|
| 程序代码 | ✅ 会 |
| **你的 API Key** | ❌ **不会**（打包脚本从 git 已提交内容打，`.env` 本来就在 `.gitignore` 里） |
| **你的会话记录 / 记忆** | ❌ 不会（在你自己机器的数据根里） |
| **你的任务状态 / 事故日志** | ❌ 不会（同上） |

对方装完之后是**完全独立的一份**：他自己的 key、他自己的记录，
看不到你的任何东西。

## 三种给法，按「对方是什么人」选

### ① 对方会敲命令 → 一行装好（推荐）

```bash
pipx install "git+https://github.com/liangzai927/vibe-agent.git"
```

`pipx` 会把它装在独立环境里、不污染对方的 Python，并且直接给出 `va` 命令。
（没有 pipx 的话：`pip install --user "git+..."`）

装完：

```bash
va web          # 起界面，在「设置 → 模型接口」里填自己的 key
```

**这是最省事的**，因为后续 `pipx upgrade` 就能更新。

> 如果仓库是私有的，对方需要自己的 Git 访问权限，或者你改用方式 ②。

### ② 对方不太会敲命令 → 发一个压缩包

```bash
make package
# → dist/vibe-agent-0.1.0-<commit>.tar.gz（约 190KB）
#   末尾还会打印 sha256，方便对方校验
```

把 tar.gz 发给对方（微信、邮件、内网盘都行），对方解压后：

```bash
cd vibe-agent
./scripts/install.sh        # 建 venv、装依赖、问你要 key
```

`install.sh` 会一步步引导：Python 版本、依赖、API Key、浏览器（可选）。
全程不用记命令。

### ③ 对方机器上不想装 Python → Docker

```bash
docker compose up -d
```

镜像里 Python、Node、Chromium 都有。适合团队统一部署或者给非开发同学。

（细节见上面的「方式 A」。）

## 对方第一次跑会看到什么

```
$ va web
还没有配置模型服务——界面能打开，但跑任务前要先配。
  打开后：设置 → 模型接口，填 API Key
  或者先跑向导：va setup
  配置文件：~/.vibe-agent/.env
```

**界面能打开**（这是故意的）——设置页就是用来配 key 的。

## 对方需要准备什么

| | 必需 | 说明 |
|---|---|---|
| Python 3.10+ | ✅ | 方式 ① ② 需要；方式 ③ 不用 |
| 一个模型 API Key | ✅ | DeepSeek / OpenAI / 任何兼容接口都行 |
| Node 18+ | 可选 | 只有接 MCP（比如 MasterGo 设计稿）才需要 |
| 浏览器 | 可选 | 只有做 UI 渲染 / 功能测试才需要，`make install-browser` |

## 出了问题让对方跑这个

```bash
va doctor
```

会逐项检查 Python、依赖、git、浏览器、API 连通性，并给出修复建议。

## 你要注意的几件事

**别把 `.env` 一起打包。** `make package` 默认从 **git 已提交内容**打包，
而 `.env` 在 `.gitignore` 里——所以默认是安全的。
用 `--working-tree` 会连未提交的改动一起打，**那时候要自己确认 `.env` 没被带上**。

**对方的数据在你管不着的地方。** 每人一份独立的，出问题让他跑 `va doctor`
并把输出贴给你。

**版本要对得上。** 包名里带 commit 号（`vibe-agent-0.1.0-ef05be7.tar.gz`），
让对方报这个号，你就知道他在跑哪一版。
