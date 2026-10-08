# vibe-agent

一个**从零构建**的终端编码 agent，对标 DSH / Codex 这一类工具。
不依赖 LangChain / LlamaIndex 等框架——工具循环、流式聚合、上下文压缩、路径沙箱、
diff 生成、文件快照、会话持久化全部自己实现，每一行都能看懂、能改。

## 快速开始

```bash
cd vibe-agent
cp .env.example .env          # 或用 python3 setup_key.py 安全录入
python3 run.py                # 进入交互模式
python3 run.py "把 foo.py 里的 bug 修了"   # 一次性任务
python3 run.py -c             # 接着上次的会话继续
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VA_API_KEY` | — | API key（也接受 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`） |
| `VA_BASE_URL` | `https://api.deepseek.com/v1` | 任何 OpenAI 兼容接口 |
| `VA_MODEL` | `deepseek-chat` | 模型名，需支持 function calling |
| `VA_WORKSPACE` | 当前目录 | agent 的工作区（沙箱根目录） |
| `VA_TEMPERATURE` | `0` | 编码任务建议保持 0 |
| `VA_MAX_ITERATIONS` | `50` | 单任务最大步数 |
| `VA_AUTO_APPROVE` | `0` | `1` = 跳过所有审批（危险） |
| `VA_CONTEXT_BUDGET` | `60000` | 估算 token 超过此值就压缩历史 |
| `VA_SESSION_DIR` | `<工作区>/.va/sessions` | 会话文件存放位置 |

### 交互模式命令

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

## 架构

```
run.py / va/cli.py        REPL、命令行参数、会话恢复
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
        ├──► va/workspace.py   路径沙箱：所有读写必须落在工作区内
        ├──► va/diffview.py    unified diff 生成、着色、统计
        ├──► va/history.py     文件快照 + 检查点栈（撤销）
        ├──► va/session.py     JSONL 会话持久化
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

**审批看 diff，不看参数。** 工具实现 `preview(args)`，在动盘之前算出真实的
unified diff 摆给用户看。旧做法是把 `old_string` / `new_string` 两个文本块直接
倒出来让人自己比对——那不是审批，是让人签名一份看不清的合同。
预览还会**提前暴露必然失败的操作**（匹配不到、不唯一、目标是目录）。

**撤销按「文件改动」计数，不按「步骤」计数。** 实际使用中 agent 经常是
「改代码 → 跑测试 → 再改」。如果 `/undo` 按步骤回退，它会先消费掉「跑测试」
这个没有文件快照的步骤，报一句「无法撤销」，而真正该回退的编辑纹丝不动。
所以 `UndoStack.undo()` 会跳过空步骤，只在真正还原了文件时才计数。

**有些操作就是无法撤销，就如实说。** `run_bash` 跑了任意命令、`git checkout`
换了整个工作树——这些工具标记为 `opaque`，撤销时会明确报告「这一步执行过
无法追踪的操作」，而不是假装成功。

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

## 测试

```bash
python3 -m unittest discover -s tests -v
```

116 个测试，**不需要 API key**：

| 文件 | 覆盖 |
|---|---|
| `test_smoke.py` | 脚本化假 LLM 驱动真实工具：读写、精确编辑、沙箱越界、审批拒绝、未知工具、参数畸形、步数上限、git 操作 |
| `test_llm_stream.py` | 起 mock OpenAI 服务真实走 HTTP + SSE，验证流式 `tool_calls` 分片拼接 |
| `test_diff_preview.py` | diff 引擎（含末行无换行符的经典坑）、工具预览、预览在审批中真的被用上 |
| `test_undo.py` | 快照语义、多步回退顺序、空步骤跳过、不可追踪操作的如实报告 |
| `test_session.py` | 追加写、压缩重放、残缺行容错、跨进程恢复、恢复时的过期提醒 |

## 路线图

- [x] **Phase 1 · 内核** —— 工具循环、流式聚合、沙箱、审批、上下文压缩、核心工具集、CLI
- [x] **Phase 2 · 干活能力** —— diff 预览、撤销、会话持久化
- [ ] **Phase 3 · UI 还原闭环** —— 用 Node + V8（Playwright + 系统 Chrome）渲染页面截图，
      与设计图做多模态比对，让 agent 自己判断「像不像」并迭代修正
- [ ] **Phase 4 · 自主开发** —— 读需求文档 + UI 图 → 自动建分支 → 编码 → 自测 →
      提交 → 推送的完整流水线，配合 planner / coder / reviewer 多 agent 分工

## 开发过程中被真实使用抓出来的 bug

单元测试通不过的地方，往往不是真问题；真问题都是跑起来才露头的。记录在此：

1. **git porcelain 解析错位** —— `_git()` 对 stdout 做了 `strip()`，而
   `git status --porcelain` 的行首空格是有语义的（`' M'` = 未暂存，`'M '` = 已暂存）。
   结果：文件名少一个字符（`__pycache__` → `_pycache__`），且暂存状态判断完全反转。
   首次实盘测试发现，已补 4 项回归测试。

2. **多步撤销顺序反了** —— 用了 `reversed(taken)`，导致撤销 3 步后文件停在
   第 2 步的状态而不是最初状态。正确顺序是从最新往最老依次还原。

3. **撤销被空步骤吃掉** —— 见上文「撤销按文件改动计数」一节。

## 已知边界

- 只能读写工作区内文件（这是刻意的安全设计）。
- 单文件超过 2MB 拒绝读入；超过 5MB 不做撤销快照（该步标记为不可撤销）。
- 命令输出超过 30k 字符会头尾截断；单次保存会话最多保留 50 个撤销检查点。
- `run_bash` 默认超时 120s，上限 900s。
- 上下文压缩会丢失细节，摘要提示词里明确要求模型「需要细节时重新读文件」。
- 会话文件默认写在 `<工作区>/.va/sessions/`。在 git 仓库里启动时，如果没有忽略
  `.va/`，会提醒一次（不会替你改 `.gitignore`）。
