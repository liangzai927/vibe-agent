"""git 报错的翻译。

**为什么值得单独一个模块**：git 的报错末尾总挂着一段固定套话——

    fatal: Could not read from remote repository.

    Please make sure you have the correct access rights
    and the repository exists.

用户真正需要的病因在**第一行**（``Connection closed by ... port 22``），
而「取最后一行」这个偷懒写法恰好只留下了套话。结果就是推送失败时
界面显示一句「and the repository exists.」——什么都没说。

所以这里的规矩是：**先在所有行里找已知病因，找不到才兜底，
且兜底也要跳过套话行。**
"""

from __future__ import annotations

# git 的通用尾句。这些行本身不含任何信息，绝不能作为结论返回给用户。
_FILLER = (
    "please make sure you have the correct access rights",
    "and the repository exists",
    "could not read from remote repository",
    "failed to push some refs to",
    "unable to access",
)

# (匹配的片段, 给用户的话)。顺序有意义：更具体的放前面。
_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("connection closed", "connection refused", "connection reset"),
        "连不上远端（22 端口被挡了，或者网络不通）。"
        "如果用的是 SSH 地址，可以改用 HTTPS：git remote set-url origin https://github.com/…",
    ),
    (
        ("connection timed out", "timed out"),
        "连接超时（网络不通，或者端口被防火墙挡了）",
    ),
    (
        ("could not resolve host", "name or service not known"),
        "域名解析不了（检查网络或 DNS）",
    ),
    (
        ("permission denied (publickey)", "publickey"),
        "SSH key 没被远端认。检查公钥有没有加到 GitHub 账号里："
        "ssh -T git@github.com 应该回应一句问候",
    ),
    (
        ("authentication failed", "invalid username or password", "403"),
        "认证失败（检查 token 或密码）",
    ),
    (
        ("repository not found", "does not appear to be a git repository"),
        "远端仓库不存在或地址不对",
    ),
    (
        ("non-fast-forward", "rejected", "fetch first"),
        "远端有你本地没有的提交，先拉取再推（git pull --rebase 然后重试）",
    ),
    (
        ("src refspec", "no commits yet"),
        "分支不存在（仓库里可能还没有提交）",
    ),
    (
        ("nothing to commit",),
        "没有要提交的改动",
    ),
    (
        ("host key verification failed",),
        "远端主机的 SSH 指纹没被信任（先手动 ssh 一次确认）",
    ),
)


def explain(stderr: str, *, fallback: str = "git 没有给出原因") -> str:
    """把 git 的 stderr 翻成一句人能看懂的话。"""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return fallback

    joined = " ".join(lines).lower()
    for needles, message in _PATTERNS:
        if any(needle in joined for needle in needles):
            return message

    # 兜底：取第一条**有信息量**的，跳过 git 的套话
    for line in lines:
        lowered = line.lower().rstrip(".")
        if any(lowered.startswith(filler) for filler in _FILLER):
            continue
        return line[:200]
    return lines[0][:200]
