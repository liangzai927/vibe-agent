#!/usr/bin/env python3
"""便捷启动脚本：python run.py "任务" 或 python run.py 进入交互模式。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from va.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
