"""公网隧道管理接口：/admin/api/tunnel（全部需要后台密钥）。

单独一个 router 的理由和 admin_logs 一样：它管的是一个外部子进程，
和账号/用量这些业务数据没有交集，混进 admin_api 只会让那个文件更难读。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth_admin import verify_admin_key
from ..tunnel import TunnelError, tunnel

router = APIRouter(
    prefix="/admin/api/tunnel",
    dependencies=[Depends(verify_admin_key)],
)


@router.get("")
async def tunnel_status():
    return tunnel.status()


@router.post("/start")
async def tunnel_start(payload: dict = Body(default={})):
    mode = str(payload.get("mode") or "quick").strip().lower()
    token = str(payload.get("token") or "").strip()
    try:
        state = tunnel.start(mode=mode, token=token)
    except TunnelError as err:
        raise HTTPException(400, str(err)) from None
    return {"ok": True, **state}


@router.post("/stop")
async def tunnel_stop():
    # stop 会等子进程收尾（最长 5s），不放进事件循环里阻塞其它请求
    state = await asyncio.to_thread(tunnel.stop)
    return {"ok": True, **state}
