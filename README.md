# vibe-agent

一个**从零手写、零框架**的编码 agent：给它一个任务，它自己读代码、改代码、跑测试、
看到界面效果，需要动你的文件时先把 diff 摆出来问你。

Python 3.10+ / 一个第三方依赖 / 可 Docker 部署 / 终端和浏览器两种界面。

**中文** · [English](#english)

---

## 目录

- [功能清单](#功能清单)
- [快速开始](#快速开始)
- [它能做什么](#它能做什么)
- [记忆和会话存哪](#记忆和会话存哪)
- [目录结构](#目录结构)
- [常用命令](#常用命令)
- [测试](#测试)
- [已知边界](#已知边界)
- [设计取舍](#设计取舍)

---

## 功能清单

### 已完成

**核心**
- [x] 零框架的工具调用循环（不依赖 LangChain / LlamaIndex 之类）
- [x] 流式输出（含推理过程），OpenAI 兼容接口
- [x] 上下文自动压缩（超预算时摘要旧历史，不破坏消息配对）
- [x] 会话持久化（JSONL，一个会话一个文件）
- [x] 启动自动恢复上次会话
- [x] 原地打转检测（同一工具同样参数连续 3 次就停，而不是按步数一刀切）
- [x] 模型清单与切换（`/models`，认得出哪些能读图）

**安全**
- [x] 路径沙箱（三级权限：用户代码只写工作区 / agent 产物另存 / 读取按策略）
- [x] 写操作审批，审批框里显示 **diff** 而不是原始参数
- [x] 撤销：每步开始前抓快照，`/undo` 整步回退
- [x] 读工作区外文件的三档策略（全部 / 只列出的目录 / 只工作区内）
- [x] 密钥不进仓库（`.env` 默认被忽略，记忆写入时也会拦截密钥）

**工具（23 个）**
- [x] 文件：读 / 写 / 精确编辑 / 列目录 / glob / grep
- [x] 执行：`run_bash`
- [x] **后台长驻进程**：启动 / 读日志 / 等端口就绪 / 停止 / 查看
      （`run_bash` 是「跑完才返回」，起 dev server 会挂住）
- [x] git：status / diff / log / 建分支 / 切换 / 提交 / 推送 / 初始化
- [x] UI 还原：渲染截图 / 像素对比 / 读设计稿 / 不用模型读图片信息
- [x] 文档：Markdown / 纯文本 / HTML / Word / PDF
- [x] **浏览器操作原语**：点击 / 输入 / 下拉 / 等待 / 断言 / 截图
      （一次调用跑完整条流程，不是一次一个动作）
- [x] 记忆：`remember`（两级作用域）
- [x] 委派：子 agent（可限制嵌套深度）
- [x] **MCP 客户端**：接任意 MCP 服务器（MasterGo 设计稿就是这么接的）

**界面**
- [x] 浏览器界面（拖拽传图、彩色 diff、点按钮审批、SSE 实时推送）
- [x] 可视化设置面板（不用去翻 `.env`）
- [x] 系统原生文件夹选择框（浏览器拿不到真实路径，由服务端弹）
- [x] 断开重连（事件带序号，刷新/重连不丢消息）

**数据**
- [x] 长期记忆：跨项目通用 + 项目级，两级作用域
- [x] 记忆和会话同步到一个 git 仓库（多台机器共享）
- [x] 一键初始化仓库（确认空白 → 建结构 → 首次推送）
- [x] 接管已有仓库（靠标识文件判断是不是本 agent 的）
- [x] 切换存储位置时自动迁移已有内容
- [x] 每轮回复结束自动推送（可关）
- [x] 启动自检：会话实际写在哪、会不会自动推，直接显示出来

**工程**
- [x] Docker 部署（含 UID/GID 对齐）
- [x] 900+ 测试，跑在真实文件系统 / 真实 git 仓库 / 真实浏览器上
- [x] 部署前的环境自检（`va doctor`）

### 规划中

- [ ] 多个独立会话并行（现在一次只能跑一个）
- [ ] 会话列表与搜索
- [x] `@文件` 引用（输入 `@` 弹补全）
- [ ] 用量与成本统计
- [ ] hooks / 自定义命令
- [ ] 测试驱动循环（改完自动跑测试并迭代到绿）
- [ ] 并行工具执行（现在同一轮内的工具是顺序跑的）
- [ ] 更细的权限控制（按工具、按路径模式）
- [x] MCP 支持（接外部工具的标准协议）
- [ ] 桌面应用打包（macOS / Windows）

---

## 快速开始

### 1. 装

**本地 Python**（推荐先试这个）

```bash
git clone https://github.com/liangzai927/vibe-agent.git
cd vibe-agent

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

唯一的必需依赖是 `openai`。想要浏览器截图 / 像素对比能力再装：

```bash
pip install -r requirements-browser.txt   # playwright + pillow
playwright install chromium               # 不装也行，会退回用系统 Chrome/Edge
```

**Docker**（不想装 Python 就用这个）

```bash
cd vibe-agent
cp .env.example .env               # 填好 VA_API_KEY
docker compose run --rm va "帮我把这个项目的测试跑通"
```

### 2. 配模型

跑一遍向导，或者直接编辑 `.env`：

```bash
python3 run.py setup
```

`.env` 里最少的四项：

```bash
VA_API_KEY=sk-xxxxxxxx
VA_BASE_URL=https://api.deepseek.com/v1
VA_MODEL=deepseek-chat
VA_TEMPERATURE=0
```

任何 **OpenAI 兼容**接口都行（DeepSeek / OpenAI / 本地 Ollama / 自建网关）。
完整变量说明见 [.env.example](.env.example)。

先自检一下：

```bash
python3 run.py doctor
```

### 3. 用

```bash
cd 你的项目目录
python3 /path/to/vibe-agent/run.py "把登录页圆角改成设计稿那样"
```

一条命令就是一个任务。省略任务就进交互模式：

```bash
python3 run.py
```

**浏览器界面**（有拖拽传图、彩色 diff、点按钮审批）：

```bash
python3 run.py web
# 自动打开 http://127.0.0.1:8420
```

### 4. 建议：装成命令

```bash
echo 'alias va="python3 /path/to/vibe-agent/run.py"' >> ~/.zshrc
source ~/.zshrc

va "跑一下测试"
va web
```

---

## 它能做什么

### 一次任务大概长这样

```
$ va "把 demo-repo 的两个失败测试修好"

  I'll start by looking at the test failures.
  ├─ [1/2] run_bash(pytest -q)
  │  ✓ 2 failed, 8 passed
  ├─ [2/2] read_file(src/calc.py)
  │  ✓ 第 1-40 行 / 共 40 行

  ┌ 改动预览 ─────────────────────────
  │ src/calc.py
  │ -    return a + b + 1
  │ +    return a + b
  └────────────────────────────────────
  允许这次修改吗？[y/N]
```

**需要动你的文件时一定会问**，而且给你看 diff 而不是工具参数。

### 界面还原闭环

给一张设计稿，它能自己迭代到像素级接近：

```
inspect_image    → 不用模型，先拿到画布尺寸、主色 hex、布局分区
read_design      → 需要读懂文字和语义时才用（要配视觉模型）
render_page      → 无头浏览器渲染，拿到截图 + 控制台报错
pixel_diff       → 逐像素对比，给差异百分比和热力图
```

`inspect_image` 和 `pixel_diff` **不需要模型、不花 token**——
没有视觉模型也能做 UI 还原，只是读不懂设计稿上的文字。

### `@文件` 引用

消息里写 `@路径`，文件内容会**直接并进这条消息**：

```
你：看一下 @src/auth.ts 里的刷新逻辑
```

比写完整路径快，而且**省掉 agent 找文件的两轮往返**（先 `glob` 找、再 `read_file` 读）。
界面里输入 `@` 会弹补全，**每敲一个字实时过滤**，方向键选、Enter 插入。
补全时**第一次**要遍历整个工作区（几百毫秒，会显示「正在读取文件列表…」），
之后走缓存，逐字过滤都在本地——所以打字不会有延迟。选中的文件在输入框里
显示成一个**带图标的 chip** 而不是一长串路径——只显示名字，
完整路径放进悬停提示。文件和目录用不同的图标和颜色区分：

```
◈auth.ts     文件（蓝色）
▸src/        目录（琥珀色；引用整个目录时 agent 用 list_dir 自己看）
```

分界很清楚：

| 情况 | 处理 |
|---|---|
| 小文件 | 内联进消息（agent 一处往返都不用） |
| 大文件 | 只给路径 + 提示「需要时用 read_file」 |
| 二进制 / 目录 | 给路径，说明为什么不内联 |
| 工作区**外** | **拒绝**——`@` 是 agent 替用户静默读文件，和 `read_file` 一个口径 |

内联一个 2000 行的文件等于把上下文预算吃掉一块，而你可能只是想问
「这个文件是干嘛的」——所以大文件不内联。

### 工作流定义

分支怎么命名、commit 什么格式、mock 放哪、测试怎么跑、提测推到哪——
这些是**团队约定**，写下来 agent 就照着做，不用每次交代。

```bash
va workflow --init     # 生成一份带注释的模板（<项目>/.vibe/workflow.yaml）
va workflow            # 看当前生效的值 + 每条来自哪个文件
va workflow --check    # 只校验
```

两份配置，**项目级覆盖用户级**：

```
<数据根>/workflow.yaml          你个人的习惯
<项目>/.vibe/workflow.yaml      这个团队的约定（进 git）
```

合并规则：**字典递归合并，列表整体替换**——「我加了第三条」不该变成
「原来的三条加上第三条」，这种意外在配置里特别难查。

模板里**字段全是注释、段落是真的**，取消哪条哪条生效，其余走默认：

```yaml
branch:
  # 分支名模板。可用占位符：{ticket} 需求号、{slug} 标题短横线化
  # pattern: "feature/{ticket}-{slug}"
  # 从哪个分支切出
  base: develop          ← 只取消了这一条
```

**不把默认值写进模板**是有意的：写进去的话①每个键的来源都变成「项目级」，
看不出自己改了什么 ②以后工具改了默认值，老项目会被文件里的旧值钉住。

配置错了会明确报错并给行号：

```
✗ YAML 语法错误：found unexpected end of stream（.vibe/workflow.yaml 第 3 行）
```

还会做跨字段检查，比如「门禁开着却没配任何测试」「要求带需求号但模板里
没有 {ticket}」「配了就绪端口却没配启动命令」。

### 提测（推送 / CI 校验 / 通知）

**每一步都是可选的。没配就跳过、不报错、也不执行。**

提测是往**外部**推东西——推错了收不回来。所以这里**不设「合理的默认值」**：
没配 `target_branch` 就**不提测**，而不是「默认推到 test」。
一个刚接入的项目不该因为跑了一次 `va workflow` 就被推到 test 分支去。

```yaml
# 什么都不配 → 直接结束：
提测（配置为空，什么都没做）
  · 目标分支：跳过（未配置 submit.target_branch）
  · CI：跳过（同上，没有提测目标）
  · 通知：跳过（同上）
```

```yaml
# 配了之后
submit:
  target_branch: test          # 空 = 不提测
  remote: origin
  push_mode: direct            # merge_request 需平台 API，尚未实现
  source_branch: ""            # 空 = 当前分支
  require_gate: true           # 推之前过测试门禁
  ci_command: npm run lint     # 本地校验，失败就停下不推
  wait_for_ci: false           # 读远端 CI 状态需平台 API，尚未实现
  notify_env: VA_TEST_WEBHOOK  # webhook 地址从哪个环境变量读
  notify_template: "提测：{task} → {branch}"
```

```
提测
  ✓ 测试门禁：通过
  ✓ 本地校验：通过
  ✓ 推送：origin feature/ORD-1 → test
  · CI：跳过（未配置 wait_for_ci）
  · 通知：跳过（未配置 submit.notify_env）
```

**失败就停，后面的事不做**：

```
  ✓ 测试门禁：通过
  ✗ 本地校验：失败（退出码 1）
  · 推送：跳过（本地校验没过）

**在「本地 CI 校验」这一步停下**——后面的事没做。
```

**没实现的能力明确说没实现**，不假装：`push_mode: merge_request` 和
`wait_for_ci: true` 会明确告诉你「需要对接平台 API，这个能力还没实现」——
默默改成 direct 推上去比报错更糟。

**通知失败不影响提测本身**：代码已经推出去了，不能因为消息没发出去
就说提测失败。所以那一步仍然算成功，只在说明里提一句。

### 样式还原（截图稳定化）

**不做稳定化的话，像素差异的数字没有意义。** 时间是动的、动画截到中间帧、
随机数据每次不同——实测同一页面连截两次能差 **0.247%**，
门禁会被这个噪声反复误伤，最后你会习惯性绕过它，等于没有。

`browser_run` 的 `snapshot` 动作会先**冻住页面**：

| 冻什么 | 怎么冻 |
|---|---|
| 时间 | `Date.now()` 固定 |
| 随机 | `Math.random()` **返回常量** |
| 动画/过渡/光标 | 注入 CSS 全停 |
| 字体 | 等 `document.fonts.ready` |

**随机为什么是常量而不是「固定种子的序列」**：序列看起来更正确，但两次截图
之间 `setInterval` 触发的次数不同，序列走到第几个数就不同，文本还是每次都变
（实测 0.11%，压不到 0.1% 的线）。像素比对要的是**确定性**，不是真实性。

稳定化前后（同一个有动画+时间+随机的页面）：

```
稳定化开启连截两次：0.000%     ← 达到 0.1% 的合格线
稳定化关闭连截两次：0.247%     ← 这是「噪声地板」，低于它的改动看不见
改一个颜色：        3.80%      ← 信号远高于噪声
```

**比对要能指导修改**：「差异 3.2%」没法动手，「差异集中在右上角
y40-180 x980-1360」才能改。所以报告差异最大的几块。

**尺寸不一致就不比**：750×1624（移动端设计稿）和 1440×900（桌面渲染）
是两个不同的东西，比出来的百分比只会误导人——宁可明确拒绝。

配置在 `workflow.yaml`：

```yaml
test:
  visual_threshold: 0.01            # 差异阈值
  visual_stabilize: true            # 关掉的话数字基本没意义
  visual_freeze_time: "2026-01-01T00:00:00"
  visual_seed: 42
```

### agent 提议改配置

**不是直接写文件，是提议**——算 diff → 你确认 → 落盘 + 记「为什么改的」。

```
提议：mock 目录和实际不符

.vibe/workflow.yaml
@@ -0,0 +1,2 @@
+mock:
+  dir: src/__mocks__

[同意] [拒绝]
```

**为什么不让它直接 `edit_file`**：

1. **改动要能 review** —— 配置文件是团队约定，改错了整个流程跑不动
2. **要能归因** —— 半年后看到 `gate: warn`，你得知道「这是当初因为样式
   测试跑不起来才降的」，否则你会不敢动它，或者误以为它该是 `strict`
3. **要能拒绝** —— 模型对「你们的约定该是什么」的判断不能自动生效

**改 YAML 时保留注释**（`va/workflow_edit.py` 是个外科式编辑器）。
模板里**字段全是注释**（每个字段上面一行说明），重新生成整个文件
等于把你手写的所有说明都抹掉——配置文件不是数据文件，注释是它的一半价值。

要处理的四种情况：

| 情况 | 例子 |
|---|---|
| 键就在那儿 | `  base: main` → 换值 |
| **键是注释掉的**（模板生成的） | `  # base: main` → 取消注释并设值 |
| 段落在、键不在 | 在段落末尾插入 |
| 段落都没有 | 追加 |

第二种最要紧——不处理它，「改一个已有配置」会变成「在文件末尾多出一个重复的段」。

**落盘前先校验**（复用阶段 A 的 SCHEMA 规则，不另写一套）：
类型不对、正则编译不了、跨字段矛盾（「要求带需求号但模板里没有 `{ticket}`」）
都会在写之前拦住。

**改动记在 `.vibe/workflow-history.json`**，还有 `git` 的历史——
两层都能回滚。

### 约定校验（分支名 / commit 格式）

规则来自 `workflow.yaml`，**校验在工具层**（和门禁同一个道理——
提示词里写「请遵守命名规范」是祈祷，工具直接拒绝才是约束）。

```
git_create_branch(ticket="ORD-312", title="订单列表按状态筛选")
  → feature/ORD-312-订单列表按状态筛选     ← 按模板生成，不会写错

git_create_branch(name="my-branch")
  ✗ 分支名 'my-branch' 不符合团队约定（模板 'feature/{ticket}-{slug}'）。
    照这个改：feature/ORD-312-示例标题

git_commit(kind="feat", scope="order", subject="列表支持筛选", ticket="ORD-312")
  → feat(order): 列表支持筛选 (#ORD-312)
```

**拒绝时必须给可用的例子**——只说「不合规」会让人（和模型）反复试错。

**「生效的默认值」和「模板里的推荐值」是两件事**：

- `commit.pattern` 默认是**空**（不校验格式），模板里却展示推荐的写法
- `commit.require_ticket` 默认 **false**，模板里展示 `true`

混在一起会导致**没配过 workflow.yaml 的项目也被凭空要求 commit 格式**——
和「没配规则就不校验」直接矛盾。`Field.example` 就是为这个加的。

唯一的例外是 `branch.require_clean`（默认 true）：它是**安全默认**而不是
约定——带着未提交的改动切分支会丢东西，保守的默认是好的。

**不让绕过 pre-commit hook**：代码里没有 `--no-verify`，宁可失败。

### 任务与监控

「这个任务走到哪了」是独立于对话的——存在项目里 `.vibe/tasks/`，
**换个会话、过一周回来都还在**（任务状态是项目的事，不是某次对话的事）。

```
task_advance(task_id="ORD-312", title="订单列表按状态筛选")
task_status(task_id="ORD-312", timeline=true)
task_block(task_id="ORD-312", what="订单接口", check="GET /api/orders 返回 200 且含 total")
```

```
任务 ORD-312  订单列表按状态筛选
  状态：提交
  进度：✓读需求 → ✓建分支 → ✓计划 → ✓实现 → ✓联调 → ✓测试 → ▶提交 → ·提测 → ·完成
  下一步：提测
  推进 6 次，其中 6 次无干预（100%）
```

**状态和门禁是代码，每一步「怎么做」是模型。** 状态机是确定性的
（「测试没通过不许提交」如果能被模型绕过就不叫门禁），而怎么写代码
交给模型。这也正是「流程可进化」的正确形态：改的是状态机定义（数据），
不是模型的行为（不可控）。

**推进到「提交」会被测试门禁拦住**——和 `git_commit` 用的是同一个台账。

**挂起必须给可判定的条件**：「等后端」会变成永远等下去，
「GET /api/orders 返回 200 且含 total」才行。

**界面看板**：顶栏「▤ 任务」按钮，徽标显示进行中的任务数。点开能看到
每个任务的状态、进度、在等什么、无干预率，以及底部那块最该看的
**「哪一步最常被干预」**——那就是流程定义最该改的地方。

**推进记录里记「有没有被干预」**：

```
none 无干预 / rejected 否决过 / edited 手改过产出
interrupted 打断过 / rerun 要求重跑 / redirected 改了方向
```

这个记录是为了「**挣来的自主权**」——先人工审核，连续一周无干预率 ≥95%
（且没有严重事故）才建议开自动推进。**必须从现在开始记**，
不记的话一周后算不出来。它同时也能告诉你**哪一步最常被干预**，
那就是流程定义最该改的地方。

### 测试门禁（不绿不许提交）

提示词里写「请确保测试通过」是**祈祷**——模型可能看错、可能乐观、可能忘了跑。
所以门禁**拦在 `git_commit` 里面**：测试没过，提交这个动作本身失败。

```yaml
# .vibe/workflow.yaml
test:
  gate: strict              # strict 拦 / warn 只警告 / off 关闭
  logic: npm run test:unit  # 逻辑测试命令
  start: npm run dev        # 起项目（功能测试用）
  e2e_base_url: http://localhost:5173
  visual_designs: docs/mockups
```

```
run_tests(kind="logic")   # 跑并把结果记进台账
git_commit(...)           # 门禁在这里检查
```

**两条判定，缺一不可**：

1. **该跑的测试都跑了、都过了** —— 跑哪些由配置决定；
   **没配测试的项目门禁自动关闭**，不能因为它没配就挡住提交
2. **测试发生在最后一次代码改动之后** —— 这条挡的是最危险的情况：
   **改完代码 → 拿旧结果过门禁 → 提交了从没测过的代码**

第 2 条靠**指纹**实现：测试时记下当时的代码版本，提交前重算一遍。
指纹取的是「将要提交的东西」（`git status` 里那些文件的内容哈希），
所以**你在编辑器里手改一个文件同样会让旧结果失效**。

```
测试门禁没过：
  ✓ 逻辑测试  42 通过 / 0 失败  （15:13:17）
  ✗ 逻辑测试：跑完之后代码又改过（测试结果已失效，需要重跑）
```

**允许豁免，但必须留痕**：`override_gate="纯文档改动"` 能提交，
理由会写进**会话记录和 commit message**——事后查 `git log` 也看得见。

**台账是会话级的，不持久化**：进程重启后门禁要求重跑。
拿旧结果提交有损失，重跑一遍没有。

### 功能测试（操作页面）

`render_page` 只能渲染截图。要验证「点保存后列表刷新了」这种，需要能操作：

```
browser_run(
  url="http://localhost:5173/orders",
  actions=[
    {"do": "select", "by": "label", "value": "状态", "option": "done"},
    {"do": "assert", "by": "testid", "value": "row", "count": 3},
    {"do": "click",  "by": "role", "value": "button", "name": "保存"},
    {"do": "wait_idle"},
    {"do": "assert", "by": "css", "value": ".toast", "text": "保存成功"},
    {"do": "snapshot", "label": "保存后"},
  ],
)
```

**一次调用跑完整条流程**，不是一次一个动作——十步的流程十次往返太慢。

定位优先用 `role` / `text` 而不是 CSS：和「验收标准是人话」更接近
（「点『保存』按钮」），而且类名一改就失效，角色和文案不会随便动。

**失败时告诉你的不只是「失败了」**：

```
✗ 2. click（text=保存）
    Timeout 15000ms exceeded；匹配到 0 个元素
    → 定位不对。先用 snapshot 截图看看实际结构
```

0 个（定位错了）和 8 个（定位太宽）是完全不同的问题。**有动作失败就抛错**，
所以后面的测试门禁能机器判定。

### 把项目跑起来

`run_bash` 是「跑完才返回」——起 `npm run dev` 只会挂到超时。所以另有一套：

```
start_process   启动 dev server / mock 服务 / 后端，立刻返回句柄
read_output     读它新产生的日志（增量）
wait_for_port   等端口真的可连接
stop_process    停止（连同派生子进程）
list_processes  看有哪些在跑
```

**等就绪是探测而不是 sleep**：慢机器上 5 秒不够、快机器上白等，
更要命的是**服务崩了也会继续往下走**。探测的同时盯着进程死活，
所以「配置错导致起不来」会**立刻**返回并带上真实日志，而不是等满 60 秒。

**`start_process` 可以直接带 `expect_port`**，起了又等就绪一步到位：

```
已启动 p1（web）
  $ npm run dev
  ✓ 端口 5173 已就绪（等了 0.2s）
```

三个容易做错的地方（都有测试盯着）：**必须持续读走 stdout**
（缓冲区满了子进程会阻塞在写上，话多的服务会卡死）、
**必须杀进程组**（只杀父进程会留下孤儿占着端口，下次启动就报「端口被占用」）、
**必须探测而不是 sleep**。

### 常用交互命令

进入交互模式后：

| 命令 | 作用 |
|---|---|
| `/help` | 列出全部命令 |
| `/sessions` | 历史会话 |
| `/undo` | 回退上一步（整步回退，含文件改动） |
| `/diff` | 看当前改动 |
| `/compact` | 手动压缩上下文 |
| `/models` | 列出可用模型并切换 |
| `/feature` | 走一遍功能开发流水线（建分支 → 实现 → 审查 → 提交） |
| `/paste` | 把剪贴板里的图片发给 agent |
| `/exit` | 退出 |

---

## 记忆和会话存哪

**默认都在项目之外**（`~/.vibe-agent/`）——放项目里会让 `git status` 一直脏着。

在设置面板里可以改成同步到一个 git 仓库，多台机器共享：

```
设置 → 记忆和会话存哪 → 同步到一个 Git 仓库
  [git@github.com:you/ai-memory.git]   [初始化并同步]
  ☑ 每次回复后自动同步
```

点按钮后自动跑完：

1. **确认远端是空白仓库**（是我们的就接管；别人的就拒绝并列出里面有什么）
2. 建好目录结构并首次推送
3. 把本地已有的记忆和会话搬过去

之后每次回复结束自动推送。仓库结构：

```
ai-memory/
├── .vibe-agent.json                     标识（判断能不能接管靠它）
├── memory/                              全局偏好（所有项目都读）
└── projects/<项目名-哈希>/
    ├── memory/                          这个项目自己的偏好
    └── sessions/*.jsonl                 这个项目的对话记录
```

**先按项目分、项目下再分**，同名但不同路径的项目也不会撞（目录名带路径哈希）。

> 这是**记忆和会话记录**的仓库，**不是你项目代码的 git 远端**。两者互不相干。

---

## 目录结构

```
vibe-agent/
├── run.py                  启动入口
├── va/
│   ├── cli.py              命令行、子命令、交互模式
│   ├── loop.py             agent 主循环
│   ├── prompts.py          系统提示词
│   ├── workspace.py        路径沙箱（三级权限模型）
│   ├── session.py          会话持久化
│   ├── session_sync.py     git 克隆/提交/推送
│   ├── data_repo.py        记忆仓库的初始化工作流
│   ├── memory.py           长期记忆（两级作用域）
│   ├── config.py           配置
│   ├── tools/              23 个工具
│   └── web/                浏览器界面（服务端 + 单文件前端）
├── tests/                  900+ 测试
└── docs/DESIGN.md          设计笔记（为什么这么做）
└── docs/PRODUCTION-WORKFLOW.md  生产工作流开发计划
```

**运行时的数据**（不在仓库里）：

```
~/.vibe-agent/
├── memory/                 全局偏好
├── projects/<项目>/memory/  项目级偏好
├── sessions/               会话记录（配了仓库则在仓库克隆里）
├── screenshots/            渲染截图
├── designs/                设计稿规格
└── inbox/                  剪贴板 / 拖拽进来的图片
```

---

## 常用命令

```bash
va "任务"                 # 一次性任务
va                        # 交互模式
va web                    # 浏览器界面
va web --new              # 开新会话（默认接着上次）
va doctor                 # 环境自检
va setup                  # 配置向导
va feature "做个登录页"    # 功能开发流水线
va --continue             # 接着上次会话
va --resume <ID>          # 恢复指定会话
va -y "任务"              # 自动批准所有操作（谨慎）
va --show-prompt          # 打印完整系统提示词（调试用）
```

---

## 测试

```bash
python3 -m unittest discover -s tests
```

900+ 用例。**不 mock 关键外部依赖**：

- 文件操作跑在真实文件系统上（用临时目录）
- git 操作用真实的本地仓库（`git init --bare` 造远端）
- 浏览器行为用真实 Chrome 加载页面（`render_page`、审批弹窗、SSE 重连）

装了 playwright 会多跑一些浏览器相关的用例，没装就跳过。

---

## 已知边界

- **写操作永远锁在工作区内**（除了 agent 自己的产物目录）。这是刻意的。
- **`read_file` / `grep` / `glob` 不能越界**——它们没有审批环节，
  放开了就没法拦。只有用户明确指过来的图片和文档能越界。
- **同一轮内的多个工具是顺序执行的**，不是并行。
- **一次只能跑一个任务**。运行中再发消息会被挡回来。
- Docker 部署验证到「入口控制流 / 基础镜像 / apt 可用性 / 配置一致性」，
  **没有在真实 Docker 里跑过完整 build**（开发机没装 Docker）。
- 只在 macOS 上实际用过。Windows 和 Linux 的代码路径有，但没实测。

---

## 设计取舍

完整的推理在 [docs/DESIGN.md](docs/DESIGN.md)，这里只列结论：

| 选择 | 而不是 | 因为 |
|---|---|---|
| 零框架手写循环 | LangChain 之类 | 出问题时能顺着调用栈读到底，不用猜框架做了什么 |
| 写操作显示 diff | 显示工具参数 | `{"path":"a.py","old_string":"..."}` 人看不懂 |
| 审批时阻塞线程 | 异步排队 | 不阻塞的话 agent 会一路跑完，审批变成装饰品 |
| 会话存项目外 | 存项目里 | 否则 `git status` 永远脏着 |
| 记忆用 Markdown | 用数据库 | 你要能打开看、直接改、git diff |
| 打转检测而非步数上限 | 固定步数 | 大任务跑 80 步正常，原地转三圈不正常 |
| 事件带序号可重放 | 纯队列 | 浏览器刷新/重连不丢消息 |

---

<br>

# English

A coding agent **written from scratch, no framework**: give it a task and it reads code,
edits files, runs tests, and looks at rendered UI — asking you with a diff before it
touches your files.

Python 3.10+ / one required dependency / Docker-ready / terminal and browser UIs.

[中文](#vibe-agent) · **English**

---

## Table of contents

- [Features](#features)
- [Quick start](#quick-start)
- [What it can do](#what-it-can-do)
- [Where memory and sessions live](#where-memory-and-sessions-live)
- [Project layout](#project-layout)
- [Commands](#commands)
- [Tests](#tests)
- [Known limits](#known-limits)
- [Design tradeoffs](#design-tradeoffs)

---

## Features

### Done

**Core**
- [x] Zero-framework tool-calling loop (no LangChain / LlamaIndex)
- [x] Streaming output including reasoning; any OpenAI-compatible endpoint
- [x] Automatic context compaction (summarizes old history without breaking tool pairing)
- [x] Session persistence (JSONL, one file per session)
- [x] Resumes the previous session on startup
- [x] Loop detection (same tool + same args three times in a row stops it — not a step cap)
- [x] Model catalog and switching (`/models`, knows which models can read images)

**Safety**
- [x] Path sandbox (three-tier: user code is write-locked to the workspace, agent artifacts go elsewhere, reads follow a policy)
- [x] Approval before writes, showing a **diff** rather than raw arguments
- [x] Undo: snapshot before each step, `/undo` rolls back a whole step
- [x] Three read policies for files outside the workspace (all / listed dirs / workspace only)
- [x] Secrets stay out of the repo (`.env` ignored; memory writes refuse secret-looking content)

**Tools (23)**
- [x] Files: read / write / exact edit / list / glob / grep
- [x] Shell: `run_bash`
- [x] Git: status / diff / log / branch / checkout / commit / push / init
- [x] UI fidelity: render screenshot / pixel diff / read a design / image info without a model
- [x] Documents: Markdown / plain text / HTML / Word / PDF
- [x] Memory: `remember` (two scopes)
- [x] Delegation: subagents with a nesting limit

**Interfaces**
- [x] Browser UI (drag-and-drop images, colored diffs, click-to-approve, SSE streaming)
- [x] Visual settings panel (no need to edit `.env` by hand)
- [x] Native folder picker (browsers can't see real paths, so the server opens the dialog)
- [x] Reconnect-safe event stream (sequenced events; refresh without losing messages)

**Data**
- [x] Long-term memory: global and per-project scopes
- [x] Sync memory and sessions to one git repo (share across machines)
- [x] One-click repo setup (verify empty → build structure → first push)
- [x] Adopt an existing repo (identified by a marker file)
- [x] Migrate existing content when you change the storage location
- [x] Auto-push after every reply (optional)
- [x] Startup self-check showing where sessions actually go

**Engineering**
- [x] Docker deployment (with UID/GID alignment)
- [x] 900+ tests against real filesystems, real git repos, and a real browser
- [x] Pre-flight environment check (`va doctor`)

### Planned

- [ ] Multiple concurrent sessions (currently one at a time)
- [ ] Session list and search
- [ ] `@file` references (only `@clip` for images today)
- [ ] Usage and cost tracking
- [ ] Hooks / custom commands
- [ ] Test-driven loop (run tests after edits and iterate to green)
- [ ] Parallel tool execution (tools within a turn run sequentially today)
- [ ] Finer-grained permissions (per tool, per path pattern)
- [ ] MCP support
- [ ] Desktop app packaging (macOS / Windows)

---

## Quick start

### 1. Install

**Local Python** (start here)

```bash
git clone https://github.com/liangzai927/vibe-agent.git
cd vibe-agent

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

The only required dependency is `openai`. For browser screenshots and pixel diffs:

```bash
pip install -r requirements-browser.txt   # playwright + pillow
playwright install chromium               # optional; falls back to system Chrome/Edge
```

**Docker**

```bash
cd vibe-agent
cp .env.example .env               # fill in VA_API_KEY
docker compose run --rm va "get the tests passing in this project"
```

### 2. Configure a model

Run the wizard, or edit `.env` directly:

```bash
python3 run.py setup
```

Minimum in `.env`:

```bash
VA_API_KEY=sk-xxxxxxxx
VA_BASE_URL=https://api.deepseek.com/v1
VA_MODEL=deepseek-chat
VA_TEMPERATURE=0
```

Any **OpenAI-compatible** endpoint works (DeepSeek / OpenAI / local Ollama / your own gateway).
See [.env.example](.env.example) for every variable.

Check your setup:

```bash
python3 run.py doctor
```

### 3. Run it

```bash
cd your-project
python3 /path/to/vibe-agent/run.py "round the login button corners to match the design"
```

One command, one task. Omit the task for interactive mode:

```bash
python3 run.py
```

**Browser UI** (drag-and-drop images, colored diffs, click-to-approve):

```bash
python3 run.py web
# opens http://127.0.0.1:8420
```

### 4. Optional: make it a command

```bash
echo 'alias va="python3 /path/to/vibe-agent/run.py"' >> ~/.zshrc
source ~/.zshrc

va "run the tests"
va web
```

---

## What it can do

### A typical task

```
$ va "fix the two failing tests in demo-repo"

  I'll start by looking at the test failures.
  ├─ [1/2] run_bash(pytest -q)
  │  ✓ 2 failed, 8 passed
  ├─ [2/2] read_file(src/calc.py)
  │  ✓ lines 1-40 of 40

  ┌ Proposed change ──────────────────
  │ src/calc.py
  │ -    return a + b + 1
  │ +    return a + b
  └────────────────────────────────────
  Allow this change? [y/N]
```

**It always asks before touching your files**, and shows you a diff rather than tool arguments.

### UI fidelity loop

Hand it a design mockup and it iterates to near pixel-perfect:

```
inspect_image    → no model needed: canvas size, dominant hex colors, layout regions
read_design      → only when text/semantics matter (needs a vision model)
render_page      → headless browser render, returns screenshot + console errors
pixel_diff       → per-pixel comparison with a difference percentage and heatmap
```

`inspect_image` and `pixel_diff` **cost no tokens and need no model** —
you can do UI work without a vision model; you just can't read text off the mockup.

### Interactive commands

| Command | What it does |
|---|---|
| `/help` | List all commands |
| `/sessions` | Past sessions |
| `/undo` | Roll back the last step (including file changes) |
| `/diff` | Show current changes |
| `/compact` | Compact context manually |
| `/models` | List and switch models |
| `/feature` | Run the feature pipeline (branch → implement → review → commit) |
| `/paste` | Send an image from the clipboard |
| `/exit` | Quit |

---

## Where memory and sessions live

**Outside your project by default** (`~/.vibe-agent/`) — keeping them inside would
leave `git status` permanently dirty.

In the settings panel you can point them at a git repo to share across machines:

```
Settings → Where memory and sessions go → Sync to a Git repository
  [git@github.com:you/ai-memory.git]   [Initialize and sync]
  ☑ Auto-sync after every reply
```

One button runs the whole flow:

1. **Verify the remote is an empty repo** (adopt if it's ours; refuse and list contents if it isn't)
2. Build the directory structure and push for the first time
3. Copy existing memory and sessions over

After that, every reply is pushed automatically. Repo layout:

```
ai-memory/
├── .vibe-agent.json                     marker (how adoption is decided)
├── memory/                              global preferences (read by every project)
└── projects/<name-hash>/
    ├── memory/                          preferences for this project only
    └── sessions/*.jsonl                 this project's transcripts
```

**Projects come first, then contents** — two projects with the same name in different
directories don't collide (the directory name carries a path hash).

> This is the repo for **memory and transcripts**, **not your project's git remote**.
> The two are unrelated.

---

## Project layout

```
vibe-agent/
├── run.py                  entry point
├── va/
│   ├── cli.py              CLI, subcommands, interactive mode
│   ├── loop.py             the agent loop
│   ├── prompts.py          system prompt
│   ├── workspace.py        path sandbox (three-tier permission model)
│   ├── session.py          session persistence
│   ├── session_sync.py     git clone/commit/push
│   ├── data_repo.py        memory repo setup workflow
│   ├── memory.py           long-term memory (two scopes)
│   ├── config.py           configuration
│   ├── tools/              the 23 tools
│   └── web/                browser UI (server + single-file frontend)
├── tests/                  900+ tests
└── docs/DESIGN.md          design notes (the "why")
```

**Runtime data** (not in the repo):

```
~/.vibe-agent/
├── memory/                 global preferences
├── projects/<p>/memory/    project-level preferences
├── sessions/               transcripts (in the repo clone when configured)
├── screenshots/            rendered screenshots
├── designs/                design specs
└── inbox/                  pasted / dropped images
```

---

## Commands

```bash
va "task"                 # one-off task
va                        # interactive mode
va web                    # browser UI
va web --new              # start a new session (default resumes the last one)
va doctor                 # environment check
va setup                  # configuration wizard
va feature "build a login page"   # feature pipeline
va --continue             # continue the last session
va --resume <ID>          # resume a specific session
va -y "task"              # auto-approve everything (careful)
va --show-prompt          # print the full system prompt (debugging)
```

---

## Tests

```bash
python3 -m unittest discover -s tests
```

900+ cases. **Key external dependencies are not mocked**:

- File operations run against a real filesystem (temp directories)
- Git operations use real local repos (`git init --bare` for remotes)
- Browser behavior loads real Chrome (`render_page`, approval dialogs, SSE reconnect)

With playwright installed, additional browser tests run; without it they're skipped.

---

## Known limits

- **Writes are always locked to the workspace** (except the agent's own artifact
  directories). This is deliberate.
- **`read_file` / `grep` / `glob` cannot escape the workspace** — they have no
  approval step, so there'd be nothing to stop them. Only images and documents the
  user explicitly points at can go outside.
- **Tools within a single turn run sequentially**, not in parallel.
- **One task at a time.** Messages sent while running are rejected.
- Docker deployment is verified down to "entrypoint control flow / base image /
  apt availability / config consistency" but **a full `docker build` has not been
  run** (no Docker on the dev machine).
- Only actually used on macOS. Windows and Linux code paths exist but are untested.

---

## Design tradeoffs

Full reasoning in [docs/DESIGN.md](docs/DESIGN.md) (Chinese). Conclusions:

| Choice | Instead of | Because |
|---|---|---|
| Hand-written loop, no framework | LangChain etc. | When something breaks you can read the whole call stack instead of guessing |
| Show a diff for writes | Show tool arguments | `{"path":"a.py","old_string":"..."}` is unreadable |
| Block the thread on approval | Queue it asynchronously | Otherwise the agent runs to completion and approval becomes decorative |
| Sessions outside the project | Inside it | `git status` would never be clean |
| Memory as Markdown | A database | You need to open it, edit it, and `git diff` it |
| Loop detection instead of a step cap | Fixed step limit | An 80-step refactor is normal; going in circles is not |
| Sequenced, replayable events | A plain queue | Browser refresh/reconnect must not lose messages |

---

## License

MIT
