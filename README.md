# vibe-agent

一个**从零构建**的终端编码 agent，对标 DSH / Codex 这一类工具。
不依赖 LangChain / LlamaIndex 等框架——工具循环、流式聚合、上下文压缩、路径沙箱、
diff 生成、文件快照、会话持久化、UI 渲染闭环全部自己实现，每一行都能看懂、能改。

支持 **Docker 一条命令在任意机器上跑起来**，也可以直接在本地用 Python 运行。

---

## 快速开始

### 方式一：Docker（推荐，无需装任何东西）

```bash
cd vibe-agent
cp .env.example .env            # 填 VA_API_KEY
docker compose build            # 首次构建约 5~10 分钟（含 Chromium）
docker compose run --rm va      # 交互模式
```

工作区默认是当前目录。要跑别的仓库：

```bash
WORKSPACE=/path/to/your/repo docker compose run --rm va
```

### 方式二：本地 Python

```bash
cd vibe-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt             # 核心依赖
pip install -r requirements-browser.txt     # 可选：UI 渲染与截图对比
cp .env.example .env                        # 或用 python3 setup_key.py 安全录入
python3 run.py
```

### 先自检一下

不管用哪种方式，先跑这个，它会告诉你这台机器还缺什么、怎么装：

```bash
python3 run.py doctor
docker compose run --rm va doctor
```

```
  vibe-agent 环境自检
  ──────────────────────────────────────────────────────────────
  工作区 /workspace

  必需
    ✓ Python       3.12.8  (Linux aarch64)
    ✓ 工作区          /workspace
    ✓ 会话目录         /workspace/.va/sessions
    ✓ git          git version 2.43.0
    ✓ git 身份       dev <dev@example.com>
    ✓ 模型连通         deepseek-chat   0.81s   → '连通'

  可选
    ✓ Node.js      v22.11.0  /  npm 10.9.0
    ✓ playwright   1.49.0
    ✓ 浏览器          Playwright 自带 Chromium（bundled）
    ✓ Pillow       11.0.0
    – 视觉模型         未配置
        可选。让 agent 把截图和设计图做比对时需要一个多模态模型，
        设置 VA_VISION_MODEL / VA_VISION_BASE_URL / VA_VISION_API_KEY

  ✓ 必需项全部通过。
  必需 6/6 · 可选 4/6 可用
```

退出码可直接用于 CI：必需项有失败才返回 1。`--json` 输出机器可读格式，
`--offline` 跳过网络检查，`--quick` 跳过浏览器实际启动探测。

---

## Docker 部署

### 为什么不是「装好 Python 就行」

编码 agent 会碰这些东西：git、Node（前端项目）、浏览器（渲染 UI）、
以及一堆系统库。在别人机器上逐个装一遍是最容易劝退的环节。所以镜像把
这些全带上，用构建参数控制取舍：

```bash
docker build -t vibe-agent .                              # 全功能（默认）
docker build -t vibe-agent --build-arg WITH_BROWSER=0 .   # 精简：不装 Chromium（省 ~1.5GB）
docker build -t vibe-agent --build-arg WITH_NODE=0 .      # 不装 Node
```

### 可选依赖在部署时安装

有两层机制，按你的场景选：

**构建时装好**（启动快，适合固定环境）——默认行为，`WITH_BROWSER=1` 时镜像里
就有 Chromium，容器起来直接用。

**运行时补装**（镜像小，适合不确定要不要用的场景）：

```bash
docker build -t vibe-agent --build-arg WITH_BROWSER=0 .
docker run --rm -it -e VA_INSTALL_BROWSER=1 -v "$PWD:/workspace" vibe-agent
```

entrypoint 会检测到缺 playwright/Chromium 并自动安装，装完留个标记文件，
重启不会重复装。下载的内容落在 `/opt/ms-playwright`，可以挂成卷复用。

### 挂载卷与权限

```yaml
volumes:
  - "${WORKSPACE:-.}:/workspace"   # 你的代码
  - va-home:/home/va               # git 配置、各类缓存
```

容器以 root 启动，但 entrypoint 会**先把内部的 `va` 用户对齐到你宿主机的
UID/GID**，然后降权执行。不对齐的话，工作区里生成的文件会归 root 所有，
你在宿主机上既改不动也删不掉——这是容器化开发工具最常见的坑。

默认取 `1000:1000`，宿主机用户 ID 不是的话显式传：

```bash
docker run --rm -it -e VA_UID=$(id -u) -e VA_GID=$(id -g) -v "$PWD:/workspace" vibe-agent
```

### 让 agent 能提交代码

容器里没有你的 `~/.gitconfig`，所以 git 身份得显式传：

```bash
docker run --rm -it \
  -e VA_API_KEY=sk-xxx \
  -e GIT_USER_NAME="你的名字" \
  -e GIT_USER_EMAIL="you@example.com" \
  -v "$PWD:/workspace" vibe-agent
```

不传也能跑，但 agent 执行 `git_commit` 时会失败在很晚的地方，entrypoint
启动时会提前提醒一次。

### 这套配置验证到什么程度

诚实说明，因为「没验证过」和「验证过」在出问题时差别很大：

**已经实测的**

- entrypoint 真跑过：非 root 路径、自动创建 `VA_WORKSPACE`、
  `VA_INSTALL_BROWSER=1` 但非 root 时提示后继续（而不是崩）、
  未配 git 身份时的提醒、`safe.directory` 的落地
- 基础镜像 `python:3.12-slim-bookworm` 存在，且有 `amd64` / `arm64` 两个架构
- 全部 apt 包（`ca-certificates` `curl` `git` `less` `ripgrep` `gosu` `tini` `xz-utils`）
  在 Debian bookworm 里存在
- Node tarball 的版本发现逻辑：`x64` 与 `arm64` 都能正确解析出文件名，
  下载地址返回 HTTP 200
- 容器配置的一致性检查全部通过（见下方「测试」一节）

**没有实测的（本机没装 Docker）**

- `docker build` 全流程
- 镜像内 Chromium 是否真的能启动
- UID/GID 对齐在真实挂载卷上的效果

所以**首次构建请留意输出**。如果失败，最可能的位置是 Chromium 那一步
（`playwright install --with-deps`），其次是 Node 下载。两者都可以用
构建参数关掉来隔离问题：

```bash
docker build -t vibe-agent --build-arg WITH_BROWSER=0 --build-arg WITH_NODE=0 .
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VA_API_KEY` | — | API key（也接受 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`） |
| `VA_BASE_URL` | `https://api.deepseek.com/v1` | 任何 OpenAI 兼容接口 |
| `VA_MODEL` | `deepseek-chat` | 主模型，需支持 function calling |
| `VA_VISION_MODEL` | — | 视觉模型，UI 比对用（主模型多不支持读图） |
| `VA_VISION_BASE_URL` | 同 `VA_BASE_URL` | 视觉模型接口 |
| `VA_VISION_API_KEY` | 同 `VA_API_KEY` | 视觉模型 key |
| `VA_WORKSPACE` | `/workspace` | agent 的工作区（沙箱根目录） |
| `VA_TEMPERATURE` | `0` | 编码任务建议保持 0 |
| `VA_MAX_ITERATIONS` | `50` | 单任务最大步数 |
| `VA_AUTO_APPROVE` | `0` | `1` = 跳过所有审批（危险） |
| `VA_CONTEXT_BUDGET` | `60000` | 估算 token 超过此值就压缩历史 |
| `VA_SESSION_DIR` | `<工作区>/.va/sessions` | 会话文件位置 |
| `VA_BROWSER_CHANNEL` | 自动探测 | 指定 `chrome` / `msedge` / `chromium` |
| `VA_BROWSER_EXECUTABLE` | 自动探测 | 直接指定浏览器可执行文件 |
| `VA_INSTALL_BROWSER` | `0` | `1` = 启动时自动补装浏览器 |
| `VA_UID` / `VA_GID` | `1000` | 对齐到宿主机的用户/组 ID |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | — | 写入容器内 git 配置 |

---

## UI 还原闭环

这是让 agent 真正能「照着设计稿做界面」的部分。三个工具分工：

| 工具 | 作用 | 花不花 token |
|---|---|---|
| `render_page` | 渲染页面截图，**顺带捞出控制台报错和资源加载失败** | 不花 |
| `pixel_diff` | 逐像素对比，给出差异百分比 + 差异热力图 | 不花 |
| `compare_with_design` | 调多模态模型，给出「哪里不像、该先改什么」 | 花 |

闭环是：**改代码 → render_page → pixel_diff / compare_with_design → 按差异改 → 再渲染**。

![UI 还原闭环](docs/ui-loop.png)

上图是一次真实跑通的结果：左边是设计稿，第二个是故意埋了 3 个 bug 的实现
（圆角 8px 应为 16px、内边距 16px 应为 32px、按钮色写成了橙色），
第三个是修完之后的，最右边是差异热力图——红色精准框出了按钮、内边距和圆角三处。

像素差异从 **14.31% 降到 0.00%**。

### 为什么图片不进主对话

主模型往往不支持读图（`deepseek-chat` 就不支持），而且把 base64 塞进消息历史
会同时撑爆上下文和会话文件。所以读图只发生在 `compare_with_design` 内部，
对外只返回文字结论。这样主模型用纯文本模型也能享受完整的视觉迭代能力。

### 为什么客观指标和主观判断分开

`pixel_diff` 给数字——「改了之后是不是更接近了」有个客观判据，不依赖模型自觉。
`compare_with_design` 给语义——「哪个位置、什么现象、应该是什么」。

两个都要。只看模型描述容易自我感觉良好，只看像素数字又不知道该改哪儿。

### 没有视觉模型怎么办

`pixel_diff` 完全不需要模型，照样能驱动迭代（差异百分比在降就是在收敛）。
只是少了「该改什么」的自然语言提示。

### 浏览器怎么找

不假设任何环境，按优先级探测，并把「试过哪些、为什么失败」如实报出来：

1. `VA_BROWSER_EXECUTABLE` 显式指定
2. `VA_BROWSER_CHANNEL` 显式指定
3. 系统 Chrome / Edge（macOS 的 `.app`、Linux 的常见命令名都会找）
4. Playwright 自带 Chromium

一个都找不到时会给出可执行的修复命令，而不是丢一句
`Executable doesn't exist` 让人自己猜。

---

## 交互模式命令

```
/help            帮助                 /tools        列出工具（含副作用标记）
/diff            本次会话所有文件改动   /undo [N]     撤销最近 N 次文件改动
/checkpoints     列出可撤销的步骤       /sessions     列出历史会话
/reset           清空对话历史          /compact      立即压缩上下文
/init            生成 AGENTS.md        /cwd          显示工作区
/exit            退出
```

`/init` 会扫描项目并生成 `AGENTS.md`。之后每次启动，这个文件都会被自动读进系统提示词——
**这是让 agent 理解你项目约定最有效的一招**。

---

## 架构

```
run.py / va/cli.py        REPL、命令行、doctor、会话恢复
        │
        ▼
va/loop.py   Agent        主循环：调模型 → 执行工具 → 结果回灌 → 再调模型
        │                 （含审批、参数容错、上下文压缩、快照、落盘）
        ├──► va/llm.py         OpenAI 兼容流式客户端，聚合 tool_calls 分片
        ├──► va/tools/         工具实现
        │      fs.py           read_file / write_file / edit_file / list_dir
        │      search.py       glob / grep
        │      shell.py        run_bash（超时、进程组清理、危险命令拦截）
        │      git.py          status / diff / log / 建分支 / 提交 / 推送
        │      render.py       render_page / pixel_diff / compare_with_design
        ├──► va/workspace.py   路径沙箱：所有读写必须落在工作区内
        ├──► va/diffview.py    unified diff 生成、着色、统计
        ├──► va/history.py     文件快照 + 检查点栈（撤销）
        ├──► va/session.py     JSONL 会话持久化
        ├──► va/browser.py     浏览器探测与启动（不假设环境）
        ├──► va/imaging.py     图片编码与像素对比（Pillow 可选）
        ├──► va/doctor.py      环境自检
        ├──► va/prompts.py     系统提示词
        └──► va/ui.py          终端渲染、diff 展示、审批询问
```

### 几个刻意的设计选择

**消息结构与 SDK 解耦。** `llm.py` 只产出 `AssistantTurn`，主循环只消费它。
换掉 openai SDK 或接 Anthropic，只需要改 `llm.py` 一个文件。

**沙箱是唯一入口。** 工具层不允许自己拼 `Path`，一律走 `Workspace.resolve()`。
越界访问只可能在一个地方发生，也只可能在一个地方被拦住。

**工具永不抛异常穿循环。** `safe_run()` 把所有异常转成模型能读的文本。
模型看到 `"错误：找不到 old_string"` 会自己调整，而不是让整个任务崩掉。

**可选依赖一律降级而不是崩溃。** 没装 playwright、没装 Pillow、没配视觉模型——
每种情况都给出「装什么 / 配什么」的具体命令，并告诉用户还有哪条不需要依赖的替代路径。

**审批看 diff，不看参数。** 工具实现 `preview(args)`，在动盘之前算出真实的
unified diff 摆给用户看。旧做法是把 `old_string` / `new_string` 两个文本块直接
倒出来让人自己比对——那不是审批，是让人签名一份看不清的合同。
预览还会**提前暴露必然失败的操作**（匹配不到、不唯一、目标是目录）。

**撤销按「文件改动」计数，不按「步骤」计数。** 实际使用中 agent 经常是
「改代码 → 跑测试 → 再改」。如果 `/undo` 按步骤回退，它会先消费掉「跑测试」
这个没有文件快照的步骤，报一句「无法撤销」，而真正该回退的编辑纹丝不动。

**有些操作就是无法撤销，就如实说。** `run_bash` 跑了任意命令、`git checkout`
换了整个工作树——标记为 `opaque`，撤销时明确报告，而不是假装成功。

**上下文压缩必须保住消息配对。** OpenAI 协议要求 `tool` 消息前面必须有对应的
`assistant.tool_calls`。压缩时 `_safe_cut_index()` 会往前找到一条 `user` 消息作为
切点，保证不会切出孤儿 `tool` 消息——否则下一次请求会直接 400。

**会话文件是追加写的。** JSONL，每轮往末尾 append 一行，写入 O(1)，
崩溃最多丢最后一行（读取时跳过残缺行）。压缩是整体替换，所以单独用
`compact` 记录存一份完整快照。

**恢复会话时最重要的是提醒模型「你记的东西可能过期了」。** 历史里的
`FAILED (failures=2)` 可能早就被修好了。恢复时会对比 git HEAD，
把漂移情况注入系统提示词，并要求模型动手前重新读文件。

**危险命令是硬拦截，不是审批。** `rm -rf /`、`mkfs`、fork 炸弹这类命令，
即使开了 `VA_AUTO_APPROVE` 也会被拒绝。

---

## 测试

```bash
python3 -m unittest discover -s tests -v     # 用现有环境，缺依赖的用例会跳过
make test-full                                # 装齐依赖后跑，零跳过
```

215 个测试，**不需要 API key**。缺可选依赖的用例会优雅跳过而不是变红——
同一个套件在「只有核心依赖」和「装了全部依赖」的环境下都能跑：

```
系统 python（仅核心）         215 tests, OK (skipped=14)
venv（playwright+Pillow+pyyaml）215 tests, OK (skipped=1)
```

| 文件 | 覆盖 |
|---|---|
| `test_smoke.py` | 假 LLM 驱动真实工具：读写、精确编辑、沙箱越界、审批拒绝、未知工具、参数畸形、步数上限、git 操作 |
| `test_llm_stream.py` | mock OpenAI 服务真实走 HTTP + SSE，验证流式 `tool_calls` 分片拼接 |
| `test_diff_preview.py` | diff 引擎（含末行无换行符的经典坑）、工具预览、预览在审批中真的被用上 |
| `test_undo.py` | 快照语义、多步回退顺序、空步骤跳过、不可追踪操作的如实报告 |
| `test_session.py` | 追加写、压缩重放、残缺行容错、跨进程恢复、恢复时的过期提醒 |
| `test_render.py` | URL 解析、缺依赖的报错文案、真渲染截图、控制台报错捕获、元素截图、像素对比阈值 |
| `test_environment.py` | 自检报告聚合、各项检查、浏览器候选优先级与去重、缺依赖提示 |
| `test_docker.py` | 容器配置一致性（见下） |

### 为什么给容器配置写测试

本机没装 Docker，镜像从没被真正构建过。但容器化事故绝大多数来自**不一致**
而不是复杂逻辑，而不一致是能在本地断言的：

- Dockerfile `COPY` 了不存在的文件 → 构建到一半才炸
- `.dockerignore` 忘了排除 `.env` → 密钥被烤进镜像层
- `.dockerignore` 忘了排除 `.venv` → 宿主机二进制被带进 Linux 镜像
- **代码读了某个 `VA_*` 变量，compose 却没透传** → 「本地能跑、容器里怎么都不生效」
- entrypoint 没有可执行位 → 容器以 126 退出
- 关键步骤被 `|| true` 吞掉 → 现象和真因对不上

最后那条环境变量一致性检查是真的扫描源码：Python 里的 `os.environ.get(...)`
加上 shell 里的 `${VAR:-默认}`，再和 compose 的 `environment` 列表做差集。
它当场就抓到了一个真实遗漏——`config.py` 支持 `OPENAI_API_KEY` /
`DEEPSEEK_API_KEY` 作为兜底，compose 却只透传了 `VA_API_KEY`。

---

## 路线图

- [x] **Phase 1 · 内核** —— 工具循环、流式聚合、沙箱、审批、上下文压缩、核心工具集、CLI
- [x] **Phase 2 · 干活能力** —— diff 预览、撤销、会话持久化
- [x] **Phase 3 · 可移植 + UI 闭环** —— Docker 化、环境自检、浏览器探测、
      渲染截图、像素对比、设计稿多模态比对
- [ ] **Phase 4 · 自主开发** —— 读需求文档 + UI 图 → 自动建分支 → 编码 → 自测 →
      提交 → 推送的完整流水线，配合 planner / coder / reviewer 多 agent 分工

---

## 开发过程中被真实使用抓出来的 bug

单元测试通不过的地方，往往不是真问题；真问题都是跑起来才露头的。记录在此：

1. **git porcelain 解析错位** —— `_git()` 对 stdout 做了 `strip()`，而
   `git status --porcelain` 的行首空格是有语义的（`' M'` = 未暂存，`'M '` = 已暂存）。
   结果文件名少一个字符（`__pycache__` → `_pycache__`），且暂存状态判断完全反转。

2. **多步撤销顺序反了** —— 用了 `reversed(taken)`，撤销 3 步后文件停在第 2 步的
   状态而不是最初状态。正确顺序是从最新往最老依次还原。

3. **撤销被空步骤吃掉** —— `/undo` 消费掉了没有文件快照的 `run_bash` 步骤，
   真正的编辑纹丝不动。改成按「文件改动」计数。

4. **嵌套 Playwright 实例** —— `render_page` 在 `with sync_playwright()` 里面调
   `detect()`，而后者自己又开一个实例；sync API 不允许重入，导致**所有**候选浏览器
   都以 `Error` 失败。必须先探测、再进上下文。

5. **异常类名毫无信息量** —— 上面那个 bug 一开始报的是
   `没有找到可启动的浏览器（试过：Google Chrome: Error、系统 chrome: Error…）`。
   Playwright 的异常类名就叫 `Error`，只报类名等于没报。改成取消息首行。

---

## 已知边界

- 只能读写工作区内文件（这是刻意的安全设计）。
- 单文件超过 2MB 拒绝读入；超过 5MB 不做撤销快照（该步标记为不可撤销）。
- 命令输出超过 30k 字符会头尾截断；最多保留 50 个撤销检查点。
- `run_bash` 默认超时 120s，上限 900s。
- 上下文压缩会丢失细节，摘要提示词里明确要求模型「需要细节时重新读文件」。
- 会话文件默认写在 `<工作区>/.va/sessions/`，截图在 `<工作区>/.va/screenshots/`。
  在 git 仓库里启动时，如果没忽略 `.va/`，会提醒一次（不会替你改 `.gitignore`）。
- `pixel_diff` 对**抗锯齿和字体渲染**敏感，默认阈值 16 是为了压掉这类噪声；
  但它仍不适合判断「设计上是否等价」，只能判断「像素上是否接近」。
- 容器镜像全功能版约 2GB（其中 Chromium 约 1.5GB），精简版约 300MB。
