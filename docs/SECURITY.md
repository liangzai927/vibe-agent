# 密钥泄漏：怎么处理

## 已经做了什么（代码层面）

**会话记录写入前会脱敏。** 落在 `SessionStore` 的写入出口上——
那是唯一的落盘路径，压缩快照也走它。

| 拦什么 | 例子 |
|---|---|
| 环境变量式赋值 | `VA_API_KEY=sk-...`、`TOKEN=...`、`PASSWORD=...` |
| `sk-` 开头的令牌 | OpenAI / DeepSeek / Anthropic 都是这个形状 |
| 其他常见前缀 | `ghp_`（GitHub）、`AKIA`（AWS）、`glpat-`（GitLab）…… |
| Bearer 头 | `Authorization: Bearer eyJ...` |
| 私钥块 | `-----BEGIN RSA PRIVATE KEY-----` |

**变量名保留，值替换成 `[已隐藏:环境变量]`**——「哪个 key 泄漏了」
本身是有用信息。

**做的是脱敏不是拒绝**：拒绝会让会话记录缺一块，而且用户没有好的补救方式。

## 你要做的（如果你已经泄漏过）

### 1. 立刻轮换那个 key

**这是唯一真正重要的一步。**

推到远端仓库就等于已经在别人的服务器上了。改代码拦不住已经发生的事，
删文件也删不掉 git 历史——**只能让那个 key 失效**。

去服务商后台重新生成一个，旧的就撤销。

### 2. 清掉本地会话记录里的

```bash
# 看哪些会话文件里有
cd <你的数据根>/remotes/<记忆仓库>/
grep -rl 'VA_API_KEY=' projects/ 2>/dev/null
```

那些文件可以直接删（它们是历史记录，删了不影响以后）。

### 3. 清 git 历史（可选，但建议）

删文件不够——git 历史里还在。用 `git filter-repo`：

```bash
cd <数据根>/remotes/<记忆仓库>

# 先备份
cp -r . /tmp/memory-backup

# 把所有会话记录从历史里彻底移除
git filter-repo --path-glob 'projects/*/sessions/*.jsonl' --invert-paths

# 强推（会重写远端历史）
git push --force origin main
```

⚠️ **强推是不可逆的**，而且如果别人克隆过这个仓库，他们那边的历史还是旧的。
所以第 1 步（轮换）才是根本。

### 4. 检查一下还有没有别的地方

```bash
# 记忆文件里有没有
grep -rlE 'sk-[A-Za-z0-9]{20,}|_KEY=|_TOKEN=' \
  <数据根>/projects/*/memory/ <数据根>/memory/ 2>/dev/null
```

## 为什么会在那儿

泄漏路径很普通：agent 执行了一条命令（`cat .env`、`grep -r API_KEY`、
打印环境变量……），**工具结果原样写进了会话文件**，然后自动同步推上去。

用户不会注意到，直到他去看自己的记忆仓库。

## 还不够的地方

* **脱敏只在写入时做**。已经在文件里的旧内容不会被动到——需要手动清。
* **界面还没提示**。`SessionStore.redactions` 记了「这次拦下了什么」，
  但还没接到界面上。用户不该在毫不知情的情况下被保护。
* **模式匹配不是 DLP**。不常见的格式可能漏掉。要拦的是最常见的那几类，
  不是做到完备。
