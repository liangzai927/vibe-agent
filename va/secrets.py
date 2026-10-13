"""会话记录写入前的脱敏。

**为什么要在这里拦**

会话记录会被同步到 git 远端（记忆仓库）。一旦密钥进了那里，
它就不只是「本机的一个文件」——它已经在别人的服务器上了，
而且进了 git 历史，删文件是删不掉的。

实际的泄漏路径很普通：agent 执行了一条命令（`cat .env`、
`grep -r API_KEY`、打印环境变量……），工具结果里带了密钥，
**而工具结果是原样写进会话文件的**。用户不会注意到，
直到他去看自己的记忆仓库。

所以在**写入的唯一出口**上拦一道：`SessionStore.append_message`。

────────────────────────────────────────────────────────────────

**为什么是脱敏而不是拒绝**

"发现密钥就报错不写" 会让会话记录缺一块，而且用户没有好的补救方式。
脱敏则保留了「这里原本有个密钥」这个信息，同时不带内容——
事后翻记录还能看出当时发生了什么。

**为什么模式匹配够用**

不是要做一个完美的 DLP 系统。要拦住的是**最常见的那几类**：
`KEY=value`、`sk-` 开头的令牌、Bearer 头、私钥块。
漏掉一两个不常见的格式可以接受；误伤正常内容（把普通文本里的
`key=` 也打码）会让记录变得难读——所以下面每条都尽量收紧。
"""

from __future__ import annotations

import re

#: 打码后的占位。带上原长度能看出「这里原本有东西」，
#: 但长度信息本身不敏感。
PLACEHOLDER = "[已隐藏:{kind}]"


#: 环境变量式的赋值：`SOMETHING_KEY=xxxx` / `SOMETHING_TOKEN: xxxx`
#:
#: 名字里必须出现 KEY / TOKEN / SECRET / PASSWORD 之一，值至少 8 个字符——
#: 太松会把 `VA_API_KEY=`（空值）这种也打码，反而看不出问题在哪。
_ENV_ASSIGN = re.compile(
    # 前缀那一段**必须可选**：`TOKEN=xxx` 里名字本身就是后缀，
    # 强制要求「先一个大写字母」会让它永远匹配不上
    # （实测 `API_TOKEN=` 能拦、`TOKEN=` 拦不住——裸 TOKEN 恰恰很常见）。
    r"""(?P<name>\b(?:[A-Z][A-Z0-9_]{0,40})?(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)\b)
        (?P<sep>\s*[=:]\s*)
        (?P<quote>["']?)
        (?P<value>[^\s"']{8,})
        (?P=quote)""",
    re.VERBOSE,
)

#: `sk-` 开头的 API key（OpenAI / DeepSeek / Anthropic 等都用这个形状）
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")

#: 其他常见的令牌前缀
_OTHER_TOKENS = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat|xoxb|xoxp|AKIA|LTAI|glpat)"
    r"[A-Za-z0-9_\-]{12,}\b"
)

#: `Authorization: Bearer xxxxx`
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{16,}")

#: PEM 私钥块
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("私钥", _PEM),
    ("环境变量", _ENV_ASSIGN),
    ("令牌", _SK_TOKEN),
    ("令牌", _OTHER_TOKENS),
    ("Bearer", _BEARER),
)


def redact(text: str) -> tuple[str, list[str]]:
    """把文本里的密钥替换成占位。

    返回 ``(脱敏后的文本, 命中了哪些种类)``。
    **第二个返回值是给人看的**——出现它说明 agent 差点把密钥写进记录，
    用户该知道。
    """
    if not text:
        return text, []

    found: list[str] = []
    result = text

    for kind, pattern in _PATTERNS:
        def substitute(match: re.Match[str], _kind: str = kind) -> str:
            found.append(_kind)
            placeholder = f"[已隐藏:{_kind}]"
            # 环境变量那条要保留变量名——「哪个 key 泄漏了」本身是有用信息，
            # 值才是敏感的
            if _kind == "环境变量":
                return f"{match.group('name')}={placeholder}"
            if _kind == "Bearer":
                return f"{match.group(1)}{placeholder}"
            return placeholder

        result = pattern.sub(substitute, result)

    return result, found


def looks_secret(text: str) -> bool:
    """这段文本里像不像有密钥（不修改内容）。"""
    return bool(redact(text)[1])


def redact_message(message: dict) -> tuple[dict, list[str]]:
    """对一条会话消息做脱敏。**不改原对象。**

    只处理会落盘的那些字段（content / 工具参数 / 工具结果）。
    """
    found: list[str] = []

    def clean(value):
        nonlocal found
        if isinstance(value, str):
            cleaned, hits = redact(value)
            found.extend(hits)
            return cleaned
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        return value

    return clean(dict(message)), found
