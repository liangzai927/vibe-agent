# vibe-agent

一个**从零构建**的终端编码 agent，对标 DSH / Codex 这一类工具。
不依赖 LangChain / LlamaIndex 等框架——工具循环、流式聚合、上下文压缩、路径沙箱全部自己实现，
每一行都能看懂、能改。

## 快速开始

```bash
cd vibe-agent
cp .env.example .env          # 填入你的 API key
python3 run.py                # 进入交互模式
python3 run.py "把 foo.py 里的 bug 修了"   # 一次性任务
```

也可以当模块跑：

```bash
python3 -m va -C /path/to/repo
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

### 交互模式命令

```
/help     帮助            /tools    列出工具
/reset    清空历史        /compact  立即压缩上下文
/init     生成 AGENTS.md  /cwd      显示工作区
/exit     退出
```

`/init` 会扫描项目并生成 `AGENTS.md`。之后每次启动，这个文件都会被自动读进系统提示词——
**这是让 agent 理解你项目约定最有效的一招**。

## 架构

```
run.py / va/cli.py        REPL、命令行参数
        │
        ▼
va/loop.py   Agent        主循环：调模型 → 执行工具 → 结果回灌 → 再调模型
        │                 （含审批、参数容错、上下文压缩）
        ├──► va/llm.py        OpenAI 兼容流式客户端，聚合 tool_calls 分片
        ├──► va/tools/        工具实现
        │      fs.py          read_file / write_file / edit_file / list_dir
        │      search.py      glob / grep
        │      shell.py       run_bash（超时、进程组清理、危险命令拦截）
        │      git.py         status / diff / log / 建分支 / 提交 / 推送
        ├──► va/workspace.py  路径沙箱：所有读写必须落在工作区内
        ├──► va/prompts.py    系统提示词
        └──► va/ui.py         终端渲染与审批询问
```

### 几个刻意的设计选择

**消息结构与 SDK 解耦。** `llm.py` 只产出 `AssistantTurn`，主循环只消费它。
换掉 openai SDK 或接 Anthropic，只需要改 `llm.py` 一个文件。

**沙箱是唯一入口。** 工具层不允许自己拼 `Path`，一律走 `Workspace.resolve()`。
越界访问只可能在一个地方发生，也只可能在一个地方被拦住。

**工具永不抛异常穿循环。** `safe_run()` 把所有异常转成模型能读的文本。
模型看到 `"错误：找不到 old_string"` 会自己调整，而不是让整个任务崩掉。

**上下文压缩必须保住消息配对。** OpenAI 协议要求 `tool` 消息前面必须有对应的
`assistant.tool_calls`。压缩时 `_safe_cut_index()` 会往前找到一条 `user` 消息作为切点，
保证不会切出孤儿 `tool` 消息——否则下一次请求会直接 400。

**危险命令是硬拦截，不是审批。** `rm -rf /`、`mkfs`、fork 炸弹这类命令，
即使开了 `VA_AUTO_APPROVE` 也会被拒绝。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

37 个测试，**不需要 API key**：

- `tests/test_smoke.py` —— 用脚本化的假 LLM 驱动真实工具与主循环，
  覆盖文件读写、精确编辑、沙箱越界、审批拒绝、未知工具、参数畸形、
  步数上限、上下文压缩、git 操作。
- `tests/test_llm_stream.py` —— 起一个 mock OpenAI 服务，真实走 HTTP + SSE，
  专门验证流式响应里被切碎的 `tool_calls` 能否正确拼回完整调用。
  这是整个系统最容易出 bug 的一层。

## 路线图

- [x] **Phase 1 · 内核** —— 工具循环、流式聚合、沙箱、审批、上下文压缩、核心工具集、CLI
- [ ] **Phase 2 · 干活能力** —— diff 预览、撤销、会话持久化、项目索引、多轮任务规划
- [ ] **Phase 3 · UI 还原闭环** —— 用 Node + V8（Playwright）渲染页面截图，
      与设计图做多模态比对，让 agent 自己判断「像不像」并迭代修正
- [ ] **Phase 4 · 自主开发** —— 读需求文档 + UI 图 → 自动建分支 → 编码 → 自测 →
      提交 → 推送的完整流水线，配合 planner / coder / reviewer 多 agent 分工

## 已知边界

- 只能读写工作区内文件（这是刻意的安全设计）。
- 单文件超过 2MB 拒绝读入，命令输出超过 30k 字符会头尾截断。
- `run_bash` 默认超时 120s，上限 900s。
- 上下文压缩会丢失细节，摘要提示词里明确要求模型「需要细节时重新读文件」。
