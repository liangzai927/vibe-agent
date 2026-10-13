"""**测试不许碰真实的 .env。**

这条不是「测试准不准」的问题，是**测试会不会改坏用户的配置**的问题。

踩过：`WebApp.env_path` 默认指向包目录下的 `.env`，而 `tests/helpers.py`
只隔离了 `VA_HOME`、没隔离配置写入路径。于是
`update_settings({"api_key": "sk-new-key-0987654321"})` 这样的测试代码
**直接把用户的真实密钥覆盖成了测试用的假 key**。

所以这里守两道：
  1. 结构性防护：helpers 必须把 `VA_ENV_FILE` 指到临时目录
  2. 行为验证：跑一遍会写配置的操作，确认真实 `.env` 一个字节都没变
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402

#: 包里那份真实的 .env
REAL_ENV = Path(__file__).resolve().parent.parent / ".env"


class TestEnvIsolation(AgentTestCase):
    def test_env_file_points_into_the_temp_home(self):
        """**结构性防护。** 漏了这条，任何 `update_settings` 测试都会
        写进真实配置——而且不会有任何提示。"""
        from va.config import Config

        resolved = Config.env_file()
        self.assertNotEqual(
            resolved, REAL_ENV, "Config.env_file() 指向了真实的 .env"
        )
        self.assertIn(
            os.environ.get("VA_HOME", ""), str(resolved),
            "配置写入路径没有被重定向到测试临时目录",
        )

    def test_updating_settings_does_not_touch_the_real_env(self):
        """行为验证：真写一次，确认真实文件没动。"""
        before = REAL_ENV.read_bytes() if REAL_ENV.is_file() else None

        from va.web.server import WebApp

        app = WebApp(self.cfg, self.workspace)
        app.update_settings(
            {"api_key": "sk-should-never-land-in-the-real-env", "base_url": "https://x/y"}
        )

        after = REAL_ENV.read_bytes() if REAL_ENV.is_file() else None
        self.assertEqual(before, after, "测试改动了真实的 .env")

    def test_the_webapp_env_path_is_inside_the_temp_home(self):
        """WebApp 自己的 env_path 也要在测试目录里——不能只靠 Config。"""
        from va.web.server import WebApp

        app = WebApp(self.cfg, self.workspace)
        self.assertNotEqual(app.env_path, REAL_ENV)
        self.assertIn(os.environ.get("VA_HOME", ""), str(app.env_path))


class TestHelpersSetsItUp(unittest.TestCase):
    """防止有人以后把 helpers 里那行删了。"""

    def test_helpers_redirects_the_env_file(self):
        source = (Path(__file__).resolve().parent / "helpers.py").read_text(encoding="utf-8")
        self.assertIn(
            'os.environ["VA_ENV_FILE"]', source,
            "helpers 没有重定向配置写入路径——测试会改坏真实 .env",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
