"""进程级运行时引用：当前 uvicorn Server 实例（供优雅停服端点使用）。

cli.py 用 uvicorn.Server 手动启动时把实例挂在这里；/admin/api/shutdown 通过
should_exit 让 uvicorn 走正常关闭流程（停止接收新连接 → 等在飞请求结束 →
关闭监听与空闲 keep-alive 连接），避免 TerminateProcess 给客户端发 RST。
"""

from __future__ import annotations

server = None
