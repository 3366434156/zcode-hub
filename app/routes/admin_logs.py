"""网关日志端点（/admin/api/logs）。

后台「网关日志」页的数据源。日志本身由 `app/logstore.py` 收集（内存环 + 落盘），
本模块只负责鉴权、筛选与分页——查询逻辑留在 logstore，端点保持薄。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from ..auth_admin import verify_admin_key
from ..logstore import logstore

router = APIRouter(dependencies=[Depends(verify_admin_key)])


@router.get("/logs")
async def list_logs(level: str = "", tag: str = "", q: str = "", since: float = 0.0,
                    limit: int = 200, offset: int = 0):
    """内存环筛选：级别 / 模块 / 关键字 + 分页 + 增量（since 之后的才返回）。"""
    result = logstore.query(level=level.strip().upper(), tag=tag.strip(), q=q,
                            since=since, limit=limit, offset=offset)
    result["stats"] = logstore.stats()
    result["tags"] = logstore.tags()
    return result


@router.post("/logs/clear")
async def clear_logs():
    """清空内存环并截断今日文件（面板「清空」按钮）。落盘的旧文件不动。"""
    return {"ok": True, "removed": logstore.clear()}


@router.get("/logs/download")
async def download_logs():
    """下载当日 JSONL 原文（查案用：进程死后文件还在，直接拖走）。"""
    path = logstore.current_path()
    if path is None or not path.exists():
        from fastapi import HTTPException

        raise HTTPException(404, "今天还没有日志文件")
    return FileResponse(path, media_type="application/jsonl",
                        filename=f"zcode-gateway-{path.stem.split('-')[-1]}.jsonl")
