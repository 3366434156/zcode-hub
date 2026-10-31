"""网关运行日志的持久化台账（内存环 + 每日 JSONL 落盘）。

为什么需要：此前 `app/logs.py` 只有一个 500 条的内存环，进程一退出证据就没了
——「网关怎么老是自动关」根本查不出原因（Windows 事件日志里没有 python 崩溃记录，
说明是被外部终止而不是崩溃，但**被谁、什么时候、之前在干什么**无从知晓）。

三层数据，职责分明：
- **落盘（本模块）**：每天一个 JSONL（`data/logs/gateway-YYYYMMDD.jsonl`），
  保留 LOG_RETENTION_DAYS 天，启动时清理过期文件。进程死后文件还在，能查案。
- **内存环（本模块）**：最新 2000 条，给面板「网关日志」页做分页/筛选/增量拉取。
- **实时环（app/logs.py）**：控制台横幅与最近请求展示，与本模块互不依赖。

写入模型照抄 usagelog：`queue.Queue` + 单条 writer 线程，调用方 `write()` 只入队
就返回，绝不阻塞请求路径；写盘失败吞掉降级为仅内存（日志永远不能影响对话）。

脱敏：日志会混进账号名、gateway key、JWT 片段。落盘与展示前统一打码
（`sk-` 串 / 三段点分 JWT / `Bearer xxx` 保留前 4 后 4）。
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import settings

KEEP = 2000            # 内存环上限（面板分页的数据源）
MAX_MSG = 2000         # 单条消息上限：再长的堆栈也不该撑爆一行
_BATCH_MAX = 128

# ── 脱敏 ─────────────────────────────────────────────────────────────────────
def _mask(s: str) -> str:
    """打码：保留前 4 后 4，短串整体省略。"""
    return (s[:4] + "…" + s[-4:]) if len(s) > 12 else "…"
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_BEARER_RE = re.compile(r"\b(Bearer\s+)([A-Za-z0-9._-]{8,})\b", re.IGNORECASE)


def sanitize(text) -> str:
    """日志脱敏：token / key 打码，其余原样。纯字符串进出，永不抛错。"""
    if not isinstance(text, str):
        text = str(text)
    text = _JWT_RE.sub(lambda m: _mask(m.group(0)), text)
    text = _SK_RE.sub(lambda m: _mask(m.group(0)), text)
    text = _BEARER_RE.sub(lambda m: m.group(1) + _mask(m.group(2)), text)
    return text


def _level_of(raw) -> str:
    """级别归一化：只认 INFO/WARN/ERROR 三档（DEBUG/CRITICAL 折叠进近邻档）。"""
    s = str(raw or "INFO").strip().upper()
    if s in ("WARN", "WARNING", "CRITICAL", "FATAL"):
        return "WARN" if s.startswith("WARN") or s == "CRITICAL" or s == "FATAL" else "WARN"
    if s in ("ERROR", "EXCEPTION", "TRACEBACK"):
        return "ERROR"
    return "INFO"


class LogStore:
    """网关运行日志：异步落盘 + 内存环查询。"""

    def __init__(self, dir_path: Path | None = None, keep: int = KEEP) -> None:
        self.dir = Path(dir_path) if dir_path else (settings.DATA_DIR / "logs")
        self.keep = keep
        self._ring: deque = deque(maxlen=keep)
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._today = ""
        self._fh = None
        self._written_today = 0

    # ── 写入（请求路径调用，绝不阻塞）────────────────────────────────────────
    def write(self, level: str, tag: str, msg: str, req_id: str = "") -> None:
        try:
            entry = {
                "ts": round(time.time(), 3),
                "level": _level_of(level),
                "tag": (tag or "app")[:24],
                "req_id": (req_id or "")[:16],
                "msg": sanitize(msg)[:MAX_MSG],
            }
            with self._lock:
                self._ring.append(entry)
            self._ensure_thread()
            self._queue.put(entry)
        except Exception:  # noqa: BLE001 - 日志永远不能影响主流程
            pass

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="logstore", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            batch = [self._queue.get()]
            while len(batch) < _BATCH_MAX:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._write_batch(batch)
            except Exception:  # noqa: BLE001 - 单批失败丢弃，线程必须活着
                pass

    def _write_batch(self, batch: list[dict]) -> None:
        for entry in batch:
            day = datetime.fromtimestamp(entry["ts"]).strftime("%Y%m%d")
            if day != self._today or self._fh is None or self._fh.closed:
                self._rotate(day)
            if self._fh is None:
                continue
            self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._written_today += 1
        if self._fh is not None and not self._fh.closed:
            self._fh.flush()

    def _rotate(self, day: str) -> None:
        """切到新一天的文件（顺带清理过期文件）。"""
        try:
            if self._fh is not None and not self._fh.closed:
                self._fh.close()
        except OSError:
            pass
        self.dir.mkdir(parents=True, exist_ok=True)
        self._today = day
        self._written_today = 0
        self._prune_old()
        try:
            self._fh = open(self.dir / f"gateway-{day}.jsonl", "a", encoding="utf-8")
        except OSError:
            self._fh = None   # 落盘不可用：降级为仅内存，主流程不受影响

    def _prune_old(self) -> None:
        days = getattr(settings, "LOG_RETENTION_DAYS", 14)
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        try:
            for path in self.dir.glob("gateway-*.jsonl"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    continue
        except OSError:
            pass

    # ── 读取（面板查询）──────────────────────────────────────────────────────
    def query(self, level: str = "", tag: str = "", q: str = "", since: float = 0.0,
              limit: int = 200, offset: int = 0) -> dict:
        """内存环筛选：最新在前。since 只取该时刻之后的条目（增量拉取）。"""
        with self._lock:
            items = list(self._ring)
        q = (q or "").lower()
        rows = [
            e for e in items
            if (not level or e["level"] == level)
            and (not tag or e["tag"] == tag)
            and (not since or e["ts"] > since)
            and (not q or q in e["msg"].lower() or q in e["tag"].lower())
        ]
        rows.reverse()
        total = len(rows)
        rows = rows[max(0, int(offset)): max(0, int(offset)) + max(1, min(int(limit), 500))]
        return {"rows": rows, "total": total,
                "limit": max(1, min(int(limit), 500)), "offset": max(0, int(offset))}

    def stats(self) -> dict:
        with self._lock:
            items = list(self._ring)
        levels = {"INFO": 0, "WARN": 0, "ERROR": 0}
        day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = 0
        for e in items:
            levels[e["level"]] = levels.get(e["level"], 0) + 1
            if e["ts"] >= day_start:
                today += 1
        size = 0
        path = self.current_path()
        try:
            size = path.stat().st_size if path and path.exists() else 0
        except OSError:
            size = 0
        return {"levels": levels, "ring": len(items), "keep": self.keep,
                "today": today, "file": str(path or ""), "file_size": size}

    def tags(self) -> list[dict]:
        """出现过的模块标签及计数（前端据此动态生成模块筛选按钮）。"""
        with self._lock:
            items = list(self._ring)
        counts: dict[str, int] = {}
        for e in items:
            counts[e["tag"]] = counts.get(e["tag"], 0) + 1
        return [{"tag": k, "count": v} for k, v in
                sorted(counts.items(), key=lambda kv: -kv[1])]

    def current_path(self) -> Path | None:
        return self.dir / f"gateway-{self._today}.jsonl" if self._today else None

    def clear(self) -> int:
        """清空内存环 + 截断今日文件（面板「清空」按钮）。"""
        with self._lock:
            n = len(self._ring)
            self._ring.clear()
        path = self.current_path()
        try:
            if path and path.exists():
                path.write_text("", encoding="utf-8")
        except OSError:
            pass
        return n


# 单例（与 usagelog 同型：导入即建，测试里可整体替换）
logstore = LogStore()

