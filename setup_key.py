#!/usr/bin/env python3
"""配置模型服务 —— 这是 `python3 run.py setup` 的快捷方式。

保留这个文件是因为很多地方（README、.env.example、之前的说明）都提到它。
真正的实现在 va/setup.py，这里只做转发，避免两套逻辑各说各话。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from va.setup import run  # noqa: E402

if __name__ == "__main__":
    directory = Path(__file__).resolve().parent
    result = run(directory / ".env", directory=directory)
    sys.exit(0 if result.ok else 1)
