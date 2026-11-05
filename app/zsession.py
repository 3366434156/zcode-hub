"""读取 ZCode 本地会话库（只读），给「会话监控」页用。

ZCode 把每个会话完整写进 `~/.zcode/cli/db/db.sqlite`（session / message / part）。
本模块只读 session 表 + message 计数，用于在后台列出对话、勾选要监听的会话；
数据库以只读 URI + WAL 打开，读不到就返回错误，不影响网关主流程。

监听名单落在 `_sup/continue.sessions`（一行一个 session id），Stop 钩子读取它
后只对名单内的会话续派；名单为空 = 不按会话过滤（沿用项目白名单）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB_PATH: Path = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"ZCode 会话库不存在: {DB_PATH}")
    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    return con


def list_sessions(limit: int = 80, q: str = "") -> list[dict]:
    """最近更新的会话（未删除），带消息数；按时间倒序。"""
    limit = max(1, min(int(limit or 80), 300))
    q = (q or "").strip()
    sql = ("select id,title,directory,time_created,time_updated,last_outcome "
           "from session where time_deleted is null")
    params: list = []
    if q:
        sql += " and (title like ? or directory like ?)"
        like = f"%{q}%"
        params += [like, like]
    sql += " order by time_updated desc limit ?"
    params.append(limit)

    con = _connect()
    try:
        rows = [dict(r) for r in con.execute(sql, params)]
        ids = [r["id"] for r in rows]
        counts: dict[str, int] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            counts = {r[0]: r[1] for r in con.execute(
                f"select session_id, count(*) from message "
                f"where session_id in ({marks}) group by session_id", ids)}
    finally:
        con.close()

    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "title": (r.get("title") or "").strip() or "(无标题)",
            "directory": r.get("directory") or "",
            "created_at": (r.get("time_created") or 0) / 1000,
            "updated_at": (r.get("time_updated") or 0) / 1000,
            "outcome": r.get("last_outcome") or "",
            "messages": int(counts.get(r["id"], 0)),
        })
    return out


def get_session(session_id: str) -> dict | None:
    """单个会话的目录/标题/最后活动时间（唤醒器需要 cwd 与活跃度）。"""
    con = _connect()
    try:
        row = con.execute(
            "select id,title,directory,last_outcome,time_updated "
            "from session where id=? and time_deleted is null", (session_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "title": (row["title"] or "").strip() or "(无标题)",
        "directory": row["directory"] or "",
        "outcome": row["last_outcome"] or "",
        "updated_at": float(row["time_updated"] or 0) / 1000,
    }


def last_message(session_id: str) -> dict | None:
    """最近一条消息的 role/finish/时间戳；用于判断会话是「回合进行中」还是「已停下」。

    ZCode 的 message.data 是 JSON：assistant 消息有 finish（stop/tool-calls/None），
    user 消息（含工具结果）没有 finish。最后一条是 user 或 finish=tool-calls →
    还在回合中；最后一条是 assistant 且 finish=stop/None → 回合已结束。
    """
    con = _connect()
    try:
        row = con.execute(
            "select data,time_updated from message where session_id=? "
            "order by sequence desc limit 1",
            (session_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    times = data.get("time") or {}
    raw_tokens = data.get("tokens") or {}
    tokens = {}
    if isinstance(raw_tokens, dict):
        for key in ("total", "input", "output"):
            try:
                tokens[key] = int(raw_tokens.get(key) or 0)
            except (TypeError, ValueError):
                tokens[key] = 0
    return {
        "role": str(data.get("role") or ""),
        "finish": data.get("finish"),
        "model": str(data.get("modelId") or ""),
        "tokens": tokens,
        "created_at": float(times.get("created") or 0) / 1000,
        "completed_at": float(times.get("completed") or 0) / 1000,
        "updated_at": float(row["time_updated"] or 0) / 1000,
    }


def context_tokens(session_id: str, scan: int = 60) -> int:
    """会话当前上下文规模（最近一条**成功** assistant 消息的 input tokens）。

    失败回合的 tokens 全是 0（上游没返回 usage），所以不能只看最后一条：
    2026-11-04 01:26 风控时五炉位最后一条都是 error/tokens=0，必须往回扫。
    """
    con = _connect()
    try:
        rows = con.execute(
            "select data from message where session_id=? "
            "order by sequence desc limit ?", (session_id, max(1, int(scan)))).fetchall()
    finally:
        con.close()
    for row in rows:
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        raw = data.get("tokens")
        if not isinstance(raw, dict):
            continue
        try:
            value = int(raw.get("input") or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def last_model(session_id: str) -> dict | None:
    """会话最近一次实际调用的 provider/model（通道硬闸用）。

    ZCode 每个模型请求都会写 model_usage；provider_id 是通道（magpie / wb-proxy-cn…），
    model_id 是模型（`zcode2api/GLM-5.3-Flash`、`hy4-preview`…）。2026-11-05 事故里
    轮换会话的 provider 全是 wb-proxy-cn，靠这张表能一眼看出走了哪条通道。
    """
    if not session_id:
        return None
    con = _connect()
    try:
        row = con.execute(
            "select provider_id, model_id, started_at from model_usage "
            "where session_id=? order by started_at desc limit 1",
            (session_id,)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    return {
        "provider": str(row["provider_id"] or ""),
        "model": str(row["model_id"] or ""),
        "at": float(row["started_at"] or 0) / 1000,
    }


def latest_session_in(directory: str, since: float = 0.0) -> str | None:
    """某工作目录下 time_created >= since 的最新会话 id（换新会话后认领用）。"""
    if not directory:
        return None
    con = _connect()
    try:
        row = con.execute(
            "select id from session where directory=? and time_deleted is null "
            "and time_created >= ? order by time_created desc limit 1",
            (directory, int(since * 1000))).fetchone()
    finally:
        con.close()
    return row["id"] if row else None
