"""着色器工单接口：/admin/api/burner（全部需要后台密钥）。

单独一个 router（与 admin_tunnel 同理）：它管的是工单队列这个独立子系统，
不往 admin_api 大文件里混。派单通道复用 /admin/api/supervisor 管的 _sup 文件协议。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import burner_core
from ..auth_admin import verify_admin_key
from ..burner import burner

router = APIRouter(
    prefix="/admin/api/burner",
    dependencies=[Depends(verify_admin_key)],
)


@router.get("")
async def burner_status():
    snap = burner_core.snapshot()
    snap["poller"] = burner.status()
    return snap


@router.post("/enqueue")
async def burner_enqueue(payload: dict = Body(...)):
    try:
        order = burner_core.enqueue(
            title=str(payload.get("title") or ""),
            prompt=str(payload.get("prompt") or ""),
            watch=list(payload.get("watch") or []),
            verify_cmd=str(payload.get("verify_cmd") or ""),
            cwd=str(payload.get("cwd") or ""),
            retries=int(payload.get("retries") or 0),
        )
    except ValueError as err:
        raise HTTPException(400, str(err)) from None
    return {"ok": True, "order": order}


@router.post("/order/{order_id}/cancel")
async def burner_cancel(order_id: str):
    try:
        order = burner_core.cancel(order_id)
    except ValueError as err:
        raise HTTPException(404, str(err)) from None
    return {"ok": True, "order": order}


@router.post("/order/{order_id}/retry")
async def burner_retry(order_id: str):
    try:
        order = burner_core.retry(order_id)
    except ValueError as err:
        raise HTTPException(404, str(err)) from None
    return {"ok": True, "order": order}


@router.post("/dispatch-now")
async def burner_dispatch_now():
    result = burner_core.tick(force=True)
    if result.get("action") in ("wait", "idle"):
        # wait/idle 不是错误但要让人看懂原因
        return {"ok": True, **result}
    return {"ok": True, **result}


@router.post("/clear-done")
async def burner_clear_done():
    removed = burner_core.clear_done()
    return {"ok": True, "removed": removed}
