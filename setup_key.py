#!/usr/bin/env python3
"""交互式写入 .env —— key 不回显、不进 shell 历史、文件权限 600。"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"

PRESETS = {
    "1": ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    "2": ("OpenAI", "https://api.openai.com/v1", "gpt-4o"),
    "3": ("本地 Ollama", "http://localhost:11434/v1", "qwen2.5-coder:14b"),
}


def main() -> int:
    print("\n  选择模型服务：")
    for key, (name, url, model) in PRESETS.items():
        print(f"    [{key}] {name:<12} {url}   ({model})")
    print("    [4] 自定义")
    print()

    try:
        choice = input("  > ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print()
        return 1

    if choice in PRESETS:
        name, base_url, model = PRESETS[choice]
    else:
        name = "自定义"
        base_url = input("  base_url: ").strip() or "https://api.deepseek.com/v1"
        model = input("  model:    ").strip() or "deepseek-chat"

    try:
        api_key = getpass.getpass(f"  粘贴 {name} 的 API key（输入不回显）: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1

    if not api_key:
        print("  ✗ key 为空，已取消")
        return 1

    lines = [
        "# 由 setup_key.py 生成 —— 已在 .gitignore 中，不会被提交\n",
        f"VA_API_KEY={api_key}\n",
        f"VA_BASE_URL={base_url}\n",
        f"VA_MODEL={model}\n",
        "VA_TEMPERATURE=0\n",
    ]

    ENV_PATH.write_text("".join(lines), encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)

    print(f"\n  ✓ 已写入 {ENV_PATH}  (权限 600)")
    print(f"    模型 {model}  @  {base_url}")
    print(f"    key  {api_key[:6]}…{api_key[-4:]}  (长度 {len(api_key)})")
    print("\n  接下来运行：python3 run.py -C ../../demo-repo \"...\"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
