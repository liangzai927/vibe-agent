"""最小 MCP server（stdio）：用来验证客户端协议。"""
import json, sys, time

TOOLS = [
    {"name": "echo", "description": "把文本原样返回",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "get_selection_code", "description": "获取当前选中图层的代码（假的）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "慢工具", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "boom", "description": "会报错", "inputSchema": {"type": "object", "properties": {}}},
]

def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue

    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-mastergo", "version": "1.0"},
        }})
    elif method == "notifications/initialized":
        sys.stderr.write("客户端已就绪\n"); sys.stderr.flush()
        # 主动发一条通知，验证客户端能处理「响应之外的消息」
        send({"jsonrpc": "2.0", "method": "notifications/message",
              "params": {"level": "info", "data": "我起来了"}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = (msg.get("params") or {}).get("name")
        args = (msg.get("params") or {}).get("arguments") or {}
        if name == "echo":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"你说的是：{args.get('text','')}"}]}})
        elif name == "get_selection_code":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "<div class=\"card\">设计稿代码</div>"},
                            {"type": "image", "mimeType": "image/png", "data": "..."}]}})
        elif name == "slow":
            time.sleep(3)
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "慢完了"}]}})
        elif name == "boom":
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": "设计稿没打开"}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"没有 {name}"}})
