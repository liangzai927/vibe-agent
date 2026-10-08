"""浏览器界面：本地 HTTP + SSE 服务，把 agent 包成可视化应用。

终端能做的事这里都能做，终端做不到的（拖拽传图、彩色 diff、点按钮审批、
滚动回看）这里也能做。全部基于标准库，不需要任何新依赖。
"""

from .bridge import ApprovalGate, EventBus, WebUI
from .server import WebApp, serve

__all__ = ["ApprovalGate", "EventBus", "WebUI", "WebApp", "serve"]
