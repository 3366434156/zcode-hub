"""任务工单 API（zburn-hub 合并进来的队列/派单/验收）。

挂在 /admin/api/burner/*，与后台同一套 Bearer 鉴权；续派总闸/一次性指令仍在
/admin/api/supervisor/*（与设置页共用的文件协议），本模块只负责工单队列。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import burner, reviver
from ..auth_admin import verify_admin_key

router = APIRouter(prefix="/admin/api/burner",
                   dependencies=[Depends(verify_admin_key)])


@router.get("/state")
async def state():
    snap = burner.snapshot()
    snap["poller"] = burner.runner.status()
    snap["reviver"] = reviver.status()
    return snap


@router.get("/quota")
async def quota():
    """轻量额度探针：Stop 钩子每次回合结束都会查，别让它拉整个 accounts 快照。"""
    return {"remaining": burner.quota_remaining()}


@router.post("/enqueue")
async def enqueue(payload: dict = Body(...)):
    try:
        order = burner.enqueue(
            title=str(payload.get("title") or ""),
            prompt=str(payload.get("prompt") or ""),
            watch=list(payload.get("watch") or []),
            verify_cmd=str(payload.get("verify_cmd") or ""),
            cwd=str(payload.get("cwd") or ""),
            retries=int(payload.get("retries") or 0),
            slot=str(payload.get("slot") or ""),
        )
    except (ValueError, TypeError) as err:
        raise HTTPException(400, str(err)) from None
    return {"ok": True, "order": order}


@router.post("/order/{order_id}/cancel")
async def cancel(order_id: str):
    try:
        return {"ok": True, "order": burner.cancel(order_id)}
    except ValueError as err:
        raise HTTPException(404, str(err)) from None


@router.post("/order/{order_id}/retry")
async def retry(order_id: str):
    try:
        return {"ok": True, "order": burner.retry(order_id)}
    except ValueError as err:
        raise HTTPException(404, str(err)) from None


@router.post("/dispatch-now")
async def dispatch_now():
    # tick 可能跑验收子进程（最长 VERIFY_TIMEOUT）：放线程里，别卡事件循环
    return {"ok": True, **await asyncio.to_thread(burner.tick, True)}


@router.post("/clear-done")
async def clear_done():
    return {"ok": True, "removed": burner.clear_done()}


# ── 额度自动派单：剩余额度高于阈值时自动生成「先总结、再执行下一项」工单 ──────
@router.post("/auto")
async def set_auto(payload: dict = Body(...)):
    if payload.get("enabled") is not None:
        burner.set_auto(bool(payload["enabled"]))
    if any(k in payload for k in ("minRemaining", "maxRounds", "maxSameTask")):
        burner.set_auto_config(
            min_remaining=payload.get("minRemaining"),
            max_rounds=payload.get("maxRounds"),
            max_same_task=payload.get("maxSameTask"),
        )
    return {"ok": True, "auto": burner.auto_snapshot(
        slot=str(payload.get("slot") or "main"))}


@router.post("/auto/task")
async def set_auto_task(payload: dict = Body(...)):
    return {"ok": True, "auto": burner.set_auto_task(
        str(payload.get("text") or ""), slot=str(payload.get("slot") or "main"))}


@router.post("/revive")
async def set_revive(payload: dict = Body(...)):
    return {"ok": True, "reviver": {
        **(reviver.status()), "enabled": reviver.set_enabled(bool(payload.get("enabled", True))) }}


@router.post("/revive/tick")
async def revive_tick():
    """立即巡检一次（不启动/停止后台线程），用于夜间开跑前手动验活。"""
    return {"ok": True, **await asyncio.to_thread(reviver.tick)}


@router.post("/revive/guard")
async def revive_guard():
    """立即跑一次通道硬闸（查在跑会话到底走的哪条通道）。"""
    return {"ok": True, **await asyncio.to_thread(reviver.guard_tick)}


@router.post("/revive/unblock")
async def revive_unblock(payload: dict = Body(default={})):
    """解除通道封锁并恢复监听；GUI 当前模型改对之后再用。"""
    slot = str((payload or {}).get("slot") or "").strip()
    return {"ok": True, "reviver": await asyncio.to_thread(reviver.unblock, slot)}


@router.post("/auto/reset")
async def reset_auto(payload: dict = Body(default={})):
    slot = str((payload or {}).get("slot") or "").strip()
    return {"ok": True, "auto": burner.reset_auto(slot or None)}
