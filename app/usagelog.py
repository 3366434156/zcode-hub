"""请求用量台账（SQLite 持久化）——「请求监控」的数据源。

为什么单独一个库、单独一条写线程：
- **请求路径不能等磁盘**：`record()` 只是往队列里放一行，后台线程批量落库
  （一个事务多条），慢盘 / 锁竞争都不会拖住对话。
- **读也不抢写锁**：面板查询同样提交给这条线程，写与读串行，不会出现两个
  连接互相抢 SQLite 写锁的经典事故。
- **数据量比内存环大得多**：默认保留 30 天 / 5 万行，超出的按时间清理；
  内存环只留最近 500 条做「实时」视图，明细永远从库里查。

字段口径（对齐 workbuddy 网关的 usage 行，以本网关的上游形态为准）：
- `outcome` 是终态，和 `http_status` 分开记：失败的请求可能压根没有 HTTP 状态
  （连接断开 / 客户端取消），而 200 的请求也可能被客户端中途取消。
- `ttft_ms` 首字延迟；`gen_ms` 生成窗口（末字 - 首字）；`tokens_per_sec` 用生成
  窗口算——否则长 prompt 的排队时间会把速度算得毫无意义。
- `cached_tokens` / `cache_hit_pct`：上游前缀缓存命中，直接决定钱与首字。
- `usage_missing`：上游没报 usage 也照样落一行。丢掉这种请求会让请求数、成功率、
  时延样本一起失真（只剩 token 合计不准还算是轻的）。
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from contextlib import closing

from . import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    req_id        TEXT    NOT NULL,
    ts            REAL    NOT NULL,
    endpoint      TEXT    NOT NULL,
    provider      TEXT    NOT NULL DEFAULT 'zai',
    account       TEXT    NOT NULL DEFAULT '',
    account_id    TEXT    NOT NULL DEFAULT '',
    mode          TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    requested_model TEXT  NOT NULL DEFAULT '',
    stream        INTEGER NOT NULL DEFAULT 0,
    outcome       TEXT    NOT NULL DEFAULT 'completed',
    http_status   INTEGER,
    error         TEXT    NOT NULL DEFAULT '',
    total_ms      INTEGER,
    ttft_ms       INTEGER,
    gen_ms        INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    credit        REAL,
    tokens_per_sec REAL,
    cache_hit_pct REAL,
    usage_missing INTEGER NOT NULL DEFAULT 0,
    preview       TEXT    NOT NULL DEFAULT '',
    client        TEXT    NOT NULL DEFAULT '',
    channel       TEXT    NOT NULL DEFAULT 'local',
    switches      TEXT    NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_req_ts      ON requests (ts);
CREATE INDEX IF NOT EXISTS idx_req_account ON requests (account);
CREATE INDEX IF NOT EXISTS idx_req_model   ON requests (model);
CREATE INDEX IF NOT EXISTS idx_req_outcome ON requests (outcome);

-- 日聚合表：请求明细只保留 3 天（可配），Token/成败/换模型统计按「本地日 × 模型 × 来源」
-- 永久滚动累计——看板的长期视图（近 30 日 / 全部 / 热力图）从这里 + 近期明细合并而来。
CREATE TABLE IF NOT EXISTS requests_daily (
    day           INTEGER NOT NULL,          -- 本地 0 点 epoch 秒（与 heatmap 口径一致）
    model         TEXT    NOT NULL DEFAULT '',
    channel       TEXT    NOT NULL DEFAULT '',
    calls         INTEGER NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 0,
    fail          INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    credit        REAL    NOT NULL DEFAULT 0,
    fallback      INTEGER NOT NULL DEFAULT 0,
    ttft_sum      REAL    NOT NULL DEFAULT 0, -- 加权和（均值的可合并表示）
    ttft_n        INTEGER NOT NULL DEFAULT 0,
    gen_sum       REAL    NOT NULL DEFAULT 0,
    gen_n         INTEGER NOT NULL DEFAULT 0,
    total_sum     REAL    NOT NULL DEFAULT 0,
    tps_sum       REAL    NOT NULL DEFAULT 0,
    tps_n         INTEGER NOT NULL DEFAULT 0,
    tps_max       REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model, channel)
);
-- 滚动游标：已聚合到哪个时间戳（增量滚动，杜绝同一天被重复累计）
CREATE TABLE IF NOT EXISTS requests_daily_meta (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    last_ts REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO requests_daily_meta (id, last_ts) VALUES (1, 0);
"""
# 注意：channel 的索引**不能**放进 _SCHEMA —— 老库这时还没有该列（ALTER 在
# _init_db 里晚于 executescript），索引会直接报 "no such column"。见 _init_db。

_COLUMNS = (
    "req_id", "ts", "endpoint", "provider", "account", "account_id", "mode",
    "model", "requested_model", "stream", "outcome", "http_status", "error",
    "total_ms", "ttft_ms", "gen_ms", "input_tokens", "output_tokens", "cached_tokens",
    "credit", "tokens_per_sec", "cache_hit_pct", "usage_missing", "preview", "client",
    "channel", "switches",
)

# 每批最多写多少行（一次事务 / 一次 fsync）
_BATCH_MAX = 128
# 清理节流：几秒一次 DELETE 足够，不必每批都跑
_PRUNE_INTERVAL = 60.0
# 面板一次最多返回多少行（前端分页）
MAX_PAGE = 500


class UsageLog:
    """请求台账：非阻塞写入 + 串行读。"""

    # 面板一页最多多少行（对外暴露，供路由层夹取 limit）
    MAX_PAGE = MAX_PAGE

    def __init__(self, path=None) -> None:
        self.path = str(path or settings.USAGE_DB_PATH)
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_prune = 0.0
        self._init_db()

    # ── SQLite 基础 ──────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            # 旧库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加新列。
            # 逐列尝试、重复列报错吞掉 —— 新装库走 _SCHEMA 已有列，这里只为存量库兜底。
            for stmt in (
                "ALTER TABLE requests ADD COLUMN switches TEXT NOT NULL DEFAULT '[]'",
                # 来源通道（public = Cloudflare 隧道进来的公网流量，local = 内网/本机直连）：
                # 老库补列时历史行统一落 'local'（那时还没有区分口径）
                "ALTER TABLE requests ADD COLUMN channel TEXT NOT NULL DEFAULT 'local'",
            ):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_req_channel ON requests (channel)")
            except sqlite3.OperationalError:
                pass
            conn.commit()

    # ── 写 ───────────────────────────────────────────────────────────────────
    def record(self, row: dict) -> None:
        """把一行放进队列就返回：永不阻塞请求路径。"""
        try:
            self._ensure_thread()
            self._queue.put(("row", row, None))
        except Exception:  # noqa: BLE001 - 记录失败不能影响对话
            pass

    def flush(self, timeout: float = 5.0) -> None:
        """等队列落盘（测试与「查询前先看到自己的写入」用）。"""
        done = threading.Event()
        self._ensure_thread()
        self._queue.put(("flush", None, done))
        done.wait(timeout)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="usagelog", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        conn = self._connect()
        try:
            while True:
                batch = self._drain()
                if batch is None:      # 收到结束信号
                    return
                for op in batch:
                    if op is None:      # 结束信号可能夹在批次里
                        return
                    kind, payload, done = op
                    if kind == "flush":
                        conn.commit()
                        done.set()
                        continue
                    self._write(conn, payload)
                conn.commit()
                self._maybe_prune(conn)
        finally:
            conn.close()

    def _drain(self) -> list | None:
        """取一批待写操作；空队列则阻塞等待第一条。"""
        first = self._queue.get()
        if first is None:
            return None
        batch = [first]
        while len(batch) < _BATCH_MAX:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    @staticmethod
    def _write(conn: sqlite3.Connection, row: dict) -> None:
        values = [_normalize(col, row.get(col)) for col in _COLUMNS]
        placeholders = ",".join("?" * len(_COLUMNS))
        conn.execute(
            f"INSERT INTO requests ({','.join(_COLUMNS)}) VALUES ({placeholders})",
            values,
        )

    def _maybe_prune(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        if now - self._last_prune < _PRUNE_INTERVAL:
            return
        self._last_prune = now
        try:
            if settings.USAGE_RETENTION_DAYS > 0:
                cutoff = now - settings.USAGE_RETENTION_DAYS * 86400
                # 先把要删的行按「本地日 × 模型 × 来源」滚动累计进日聚合表（增量，
                # 游标 last_ts 保证同一行只会被累计一次），再删除——Token 统计永久保留。
                last = self._rolled_to()
                self._rollup(conn, ts_from=last, ts_to=cutoff)
                conn.execute("DELETE FROM requests WHERE ts <= ?", (cutoff,))
                self._set_rolled_to(conn, cutoff)
            if settings.USAGE_MAX_ROWS > 0:
                cap = conn.execute(
                    """SELECT id, ts FROM requests ORDER BY id DESC LIMIT 1 OFFSET ?""",
                    (settings.USAGE_MAX_ROWS,),
                ).fetchone()
                if cap is not None:
                    # 行数超限被删的行同样要先聚合（此时它们的 ts 必然 > 游标，
                    # 因为更老的已被上一条按天清掉了）；游标取滚动集的真实 MAX(ts)
                    last = self._rolled_to()
                    rolled_max = self._rollup(conn, ts_from=last, ts_to=None, max_id=cap["id"])
                    conn.execute("DELETE FROM requests WHERE id <= ?", (cap["id"],))
                    if rolled_max is not None:
                        self._set_rolled_to(conn, max(self._rolled_to(), rolled_max))
            conn.commit()
        except sqlite3.Error:
            pass

    @staticmethod
    def _rolled_to(conn: sqlite3.Connection) -> float:
        row = conn.execute("SELECT last_ts FROM requests_daily_meta WHERE id = 1").fetchone()
        return float(row["last_ts"]) if row else 0.0

    @staticmethod
    def _set_rolled_to(conn: sqlite3.Connection, ts: float) -> None:
        conn.execute("UPDATE requests_daily_meta SET last_ts = ? WHERE id = 1", (ts,))

    @staticmethod
    def _rollup(conn: sqlite3.Connection, ts_from: float, ts_to: float | None,
                max_id: int | None = None) -> float | None:
        """把 (ts_from, ts_to]（或 id <= max_id）的行聚合进日聚合表（增量 UPSERT）。

        本地日切分公式与 heatmap 完全一致：CAST((ts + tz)/86400)*86400 - tz。
        """
        tz = time.timezone
        conds, params = [], []
        if ts_to is not None:
            conds.append("ts <= ?")
            params.append(ts_to)
        if max_id is not None:
            conds.append("id <= ?")
            params.append(max_id)
        conds.append("ts > ?")
        params.append(ts_from)
        where = " AND ".join(conds)
        conn.execute(
            f"""INSERT INTO requests_daily (
                    day, model, channel, calls, ok, fail,
                    input_tokens, output_tokens, cached_tokens, credit, fallback,
                    ttft_sum, ttft_n, gen_sum, gen_n, total_sum, tps_sum, tps_n, tps_max)
                SELECT day, model, channel, calls, ok, fail,
                       input, output, cached, credit, fallback,
                       ttft, ttft_n, gen, gen_n, total, tps, tps_n, tps_m
                FROM (
                    SELECT CAST(strftime('%s', CAST(ts AS INTEGER), 'unixepoch', 'localtime', 'start of day')
                            AS INTEGER) AS day,
                           model, channel,
                           COUNT(*) AS calls,
                           SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END) AS ok,
                           SUM(CASE WHEN outcome <> 'completed' THEN 1 ELSE 0 END) AS fail,
                           SUM(COALESCE(input_tokens, 0))  AS input,
                           SUM(COALESCE(output_tokens, 0)) AS output,
                           SUM(COALESCE(cached_tokens, 0)) AS cached,
                           SUM(COALESCE(credit, 0))        AS credit,
                           SUM(CASE WHEN requested_model <> '' AND requested_model <> model
                                    THEN 1 ELSE 0 END) AS fallback,
                           SUM(COALESCE(ttft_ms, 0)) AS ttft,
                           SUM(CASE WHEN ttft_ms IS NOT NULL THEN 1 ELSE 0 END) AS ttft_n,
                           SUM(COALESCE(gen_ms, 0)) AS gen,
                           SUM(CASE WHEN gen_ms IS NOT NULL THEN 1 ELSE 0 END) AS gen_n,
                           SUM(COALESCE(total_ms, 0)) AS total,
                           SUM(COALESCE(tokens_per_sec, 0)) AS tps,
                           SUM(CASE WHEN tokens_per_sec IS NOT NULL THEN 1 ELSE 0 END) AS tps_n,
                           MAX(COALESCE(tokens_per_sec, 0)) AS tps_m
                    FROM requests WHERE {where}
                    GROUP BY day, model, channel
                ) WHERE true
                ON CONFLICT(day, model, channel) DO UPDATE SET
                    calls         = calls         + excluded.calls,
                    ok            = ok            + excluded.ok,
                    fail          = fail          + excluded.fail,
                    input_tokens  = input_tokens  + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    cached_tokens = cached_tokens + excluded.cached_tokens,
                    credit        = credit        + excluded.credit,
                    fallback      = fallback      + excluded.fallback,
                    ttft_sum      = ttft_sum      + excluded.ttft_sum,
                    ttft_n        = ttft_n        + excluded.ttft_n,
                    gen_sum       = gen_sum       + excluded.gen_sum,
                    gen_n         = gen_n         + excluded.gen_n,
                    total_sum     = total_sum     + excluded.total_sum,
                    tps_sum       = tps_sum       + excluded.tps_sum,
                    tps_n         = tps_n         + excluded.tps_n,
                    tps_max       = MAX(tps_max, excluded.tps_max)""",
            (*params,),
        )
        row = conn.execute(
            f"SELECT MAX(ts) AS m FROM requests WHERE {where}", params
        ).fetchone()
        return float(row["m"]) if row and row["m"] is not None else None

    def close(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._queue.put(None)
        thread.join(timeout=5.0)

    # ── 读 ───────────────────────────────────────────────────────────────────
    def query(self, where: str = "", params: tuple = (), limit: int = 200,
              offset: int = 0) -> tuple[list[dict], int]:
        """明细：返回 (rows, total)。最新在前。"""
        limit = max(1, min(int(limit), MAX_PAGE))
        offset = max(0, int(offset))
        clause = f" WHERE {where}" if where else ""
        with closing(self._connect()) as conn:
            total = conn.execute(f"SELECT COUNT(*) AS n FROM requests{clause}", params).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM requests{clause} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [self._decode(dict(r)) for r in rows], int(total)

    @staticmethod
    def _decode(row: dict) -> dict:
        """读出侧反归一化：switches 从 JSON 文本还原成列表（坏数据当空）。"""
        raw = row.get("switches")
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
                row["switches"] = parsed if isinstance(parsed, list) else []
            except (ValueError, TypeError):
                row["switches"] = []
        elif not isinstance(raw, list):
            row["switches"] = []
        return row

    def stats(self, where: str = "", params: tuple = (), start: float | None = None,
              dims: dict | None = None) -> dict:
        """区间聚合：总量、成功率、时延、token、缓存命中、tok/s。

        where 里第一个条件固定是 `ts >= ?`（见 admin_api._usage_filter），
        这里顺手用它做「上一等长周期」的对比查询——KPI 卡上的 ↑↓ 就靠它。

        start/dims：跨期联邦参数。明细行只保留 3 天（滚动进日聚合表），
        start 早于滚动游标时把日聚合表中该范围内的天并入——Token 长期统计因此不丢。
        dims 只支持 model/channel 两个维度（日聚合表的粒度）；带 account/关键词
        搜索的查询不联邦（dims=None，自动退化为仅近期明细）。
        """
        out = self._stats_once(where, params)
        if start is not None and dims is not None:
            rolled_to = self._rolled_to()
            day_from, day_to = self._federate_days(start, rolled_to)
            if day_from is not None:
                daily = self._daily_stats(day_from, day_to,
                                          model=dims.get("model", ""),
                                          channel=dims.get("channel", ""))
                out = _merge_stats(out, daily)
        out["prev"] = self._prev_stats(where, params, start=start, dims=dims)
        return out

    def _rolled_to(self) -> float:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT last_ts FROM requests_daily_meta WHERE id = 1").fetchone()
        return float(row["last_ts"]) if row else 0.0

    @staticmethod
    def _federate_days(start: float, rolled_to: float) -> tuple[int | None, int | None]:
        """联邦的本地日范围：start 所在当天如果已(部分)滚动，为避免重复计数
        从次日起算（边界日最多损失 <1 天的旧数据，宁缺勿重）。"""
        if rolled_to <= 0 or start >= rolled_to:
            return None, None
        day0 = int((start + time.timezone) // 86400) * 86400 - time.timezone
        day_from = day0 + 86400 if start > day0 else day0
        day_to = int((rolled_to + time.timezone) // 86400) * 86400 - time.timezone
        if day_from > day_to:
            return None, None
        return day_from, day_to

    def _daily_stats(self, day_from: int, day_to: int,
                     model: str = "", channel: str = "") -> dict:
        clause = " WHERE day >= ? AND day <= ?"
        params: list = [day_from, day_to]
        if model:
            clause += " AND model = ?"
            params.append(model)
        if channel in ("public", "local"):
            clause += " AND channel = ?"
            params.append(channel)
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"""SELECT SUM(calls) AS calls, SUM(ok) AS ok, SUM(fail) AS fail,
                           SUM(input_tokens) AS input_tokens,
                           SUM(output_tokens) AS output_tokens,
                           SUM(cached_tokens) AS cached_tokens,
                           SUM(credit) AS credit, SUM(fallback) AS fallback,
                           SUM(ttft_sum) AS ttft_sum, SUM(ttft_n) AS ttft_n,
                           SUM(gen_sum) AS gen_sum, SUM(gen_n) AS gen_n,
                           SUM(total_sum) AS total_sum,
                           SUM(tps_sum) AS tps_sum, SUM(tps_n) AS tps_n,
                           MAX(tps_max) AS tps_max, 0 AS usage_missing
                    FROM requests_daily{clause}""",
                params,
            ).fetchone()
        out = {k: (row[k] or 0) for k in
               ("calls", "ok", "fail", "input_tokens", "output_tokens", "cached_tokens",
                "fallback", "usage_missing")}
        out["credit"] = float(row["credit"] or 0.0)
        for k in ("ttft_sum", "ttft_n", "gen_sum", "gen_n", "total_sum",
                  "tps_sum", "tps_n"):
            out[k] = float(row[k] or 0)
        out["tps_max"] = float(row["tps_max"] or 0) or None
        return out

    def _prev_stats(self, where: str, params: tuple, start: float | None = None,
                    dims: dict | None = None) -> dict:
        """上一个等长周期的同口径聚合（拿不到就返回空 dict，前端显示「—」）。

        start/dims 透传联邦：对比周期早于滚动游标的部分从日聚合表并进来。
        """
        try:
            if not where or not params:
                return {}
            # where 形如 "ts >= ? AND ..."：把起点往前推一个区间长度即可
            start0 = float(params[0])
            span = max(1.0, time.time() - start0)
            prev_where = "ts >= ? AND ts < ?" + (
                (" AND " + where.split(" AND ", 1)[1]) if " AND " in where else ""
            )
            prev_params = (start0 - span, start0) + tuple(params[1:])
            data = self._stats_once(prev_where, prev_params)
            if start is not None and dims is not None:
                rolled_to = self._rolled_to()
                day_from, day_to = self._federate_days(start0 - span, rolled_to)
                if day_from is not None and start0 - span < rolled_to:
                    daily = self._daily_stats(day_from, day_to,
                                              model=dims.get("model", ""),
                                              channel=dims.get("channel", ""))
                    # 对比周期上限 start0 内的整日并入；边界误差 ≤1 天，宁缺勿重
                    data = _merge_stats(data, daily, end_cap=min(start0, rolled_to))
            # 只留前端要用的几个，省得响应体翻倍
            keep = ("calls", "ok", "fail", "input_tokens", "output_tokens",
                    "cached_tokens", "fallback", "success_rate", "cache_hit_pct",
                    "avg_tps", "avg_ttft_ms", "avg_gen_ms", "avg_total_ms")
            return {k: data.get(k) for k in keep}
        except (TypeError, ValueError, sqlite3.Error):
            return {}

    def _stats_once(self, where: str, params: tuple) -> dict:
        """单次区间聚合（行级，返回「可合并的和式表示」——均值的跨表合并靠它）。"""
        clause = f" WHERE {where}" if where else ""
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"""SELECT
                      COUNT(*) AS calls,
                      SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END) AS ok,
                      SUM(CASE WHEN outcome <> 'completed' THEN 1 ELSE 0 END) AS fail,
                      SUM(COALESCE(input_tokens, 0))  AS input_tokens,
                      SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                      SUM(COALESCE(cached_tokens, 0)) AS cached_tokens,
                      SUM(COALESCE(credit, 0))        AS credit,
                      SUM(CASE WHEN usage_missing = 1 THEN 1 ELSE 0 END) AS usage_missing,
                      SUM(CASE WHEN requested_model <> '' AND requested_model <> model
                               THEN 1 ELSE 0 END) AS fallback,
                      SUM(COALESCE(ttft_ms, 0)) AS ttft_sum,
                      SUM(CASE WHEN ttft_ms IS NOT NULL THEN 1 ELSE 0 END) AS ttft_n,
                      SUM(COALESCE(gen_ms, 0)) AS gen_sum,
                      SUM(CASE WHEN gen_ms IS NOT NULL THEN 1 ELSE 0 END) AS gen_n,
                      SUM(COALESCE(total_ms, 0)) AS total_sum,
                      SUM(COALESCE(tokens_per_sec, 0)) AS tps_sum,
                      SUM(CASE WHEN tokens_per_sec IS NOT NULL THEN 1 ELSE 0 END) AS tps_n,
                      MAX(tokens_per_sec) AS tps_max
                    FROM requests{clause}""",
                params,
            ).fetchone()
        out = {k: int(row[k] or 0) for k in
               ("calls", "ok", "fail", "input_tokens", "output_tokens",
                "cached_tokens", "usage_missing", "fallback", "ttft_n", "gen_n", "tps_n")}
        for k in ("ttft_sum", "gen_sum", "total_sum", "tps_sum"):
            out[k] = float(row[k] or 0)
        out["credit"] = float(row["credit"] or 0.0)
        out["tps_max"] = float(row["tps_max"] or 0) or None
        return _finalize(out)

    def fallbacks(self, where: str = "", params: tuple = (), limit: int = 50) -> list[dict]:
        """换模型明细：区间内 requested_model ≠ 实际 model 的请求，最新在前。"""
        base = f"({where}) AND " if where else ""
        clause = f" WHERE {base}requested_model <> '' AND requested_model <> model"
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT ts, account, requested_model, model, outcome, switches
                    FROM requests{clause} ORDER BY ts DESC LIMIT ?""",
                (*params, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._decode(dict(r)) for r in rows]

    def group_by(self, column: str, where: str = "", params: tuple = (),
                 limit: int = 50, start: float | None = None,
                 dims: dict | None = None) -> list[dict]:
        """按账号 / 模型分组：调用数、成败、token、首字、tok/s。

        column='model' 且给出联邦参数时，3 天前的份额从日聚合表并进来
        （时延均值只来自近期行，历史行没有时延样本）。账号分组不联邦。
        """
        if column not in ("account", "model", "mode", "endpoint", "outcome", "channel"):
            raise ValueError(f"不支持的分组列: {column}")
        clause = f" WHERE {where}" if where else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT {column} AS name,
                          COUNT(*) AS calls,
                          SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END) AS ok,
                          SUM(CASE WHEN outcome <> 'completed' THEN 1 ELSE 0 END) AS fail,
                          SUM(COALESCE(input_tokens, 0))  AS input_tokens,
                          SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                          SUM(COALESCE(cached_tokens, 0)) AS cached_tokens,
                          AVG(ttft_ms) AS avg_ttft_ms,
                          AVG(tokens_per_sec) AS avg_tps,
                          MAX(ts) AS last_ts
                    FROM requests{clause}
                    GROUP BY {column}
                    ORDER BY calls DESC
                    LIMIT ?""",
                (*params, max(1, min(int(limit), 200))),
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            done = (item.get("ok") or 0) + (item.get("fail") or 0)
            item["success_rate"] = round(item["ok"] * 100.0 / done, 1) if done else None
            item["avg_ttft_ms"] = round(item["avg_ttft_ms"], 1) if item.get("avg_ttft_ms") is not None else None
            item["avg_tps"] = round(item["avg_tps"], 1) if item.get("avg_tps") is not None else None
            out.append(item)
        # 模型分组联邦：把 3 天前的日聚合份额并进对应模型行（时延均值保持近期口径）
        if column == "model" and start is not None and dims is not None:
            rolled_to = self._rolled_to()
            day_from, day_to = self._federate_days(start, rolled_to)
            if day_from is not None:
                daily = self._daily_group_model(day_from, day_to,
                                                channel=dims.get("channel", ""))
                by_name = {item["name"]: item for item in out}
                for name, d in daily.items():
                    item = by_name.get(name)
                    if item is None:
                        item = {"name": name, "calls": 0, "ok": 0, "fail": 0,
                                "input_tokens": 0, "output_tokens": 0,
                                "cached_tokens": 0, "avg_ttft_ms": None,
                                "avg_tps": None, "last_ts": None,
                                "success_rate": None}
                        out.append(item)
                        by_name[name] = item
                    for k in ("calls", "ok", "fail", "input_tokens",
                              "output_tokens", "cached_tokens"):
                        item[k] = int(item.get(k) or 0) + int(d.get(k) or 0)
                    done = item["ok"] + item["fail"]
                    item["success_rate"] = (
                        round(item["ok"] * 100.0 / done, 1) if done else None)
            out.sort(key=lambda x: int(x.get("calls") or 0), reverse=True)
        return out

    def _daily_group_model(self, day_from: int, day_to: int,
                           channel: str = "") -> dict[str, dict]:
        clause = " WHERE day >= ? AND day <= ?"
        params: list = [day_from, day_to]
        if channel in ("public", "local"):
            clause += " AND channel = ?"
            params.append(channel)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT model AS name, SUM(calls) AS calls, SUM(ok) AS ok,
                           SUM(fail) AS fail, SUM(input_tokens) AS input_tokens,
                           SUM(output_tokens) AS output_tokens,
                           SUM(cached_tokens) AS cached_tokens
                    FROM requests_daily{clause} GROUP BY model""",
                params,
            ).fetchall()
        return {r["name"]: dict(r) for r in rows}

    def series(self, where: str = "", params: tuple = (), buckets: int = 24,
               start: float | None = None, dims: dict | None = None) -> list[dict]:
        """时序（Token / 请求数 / 吞吐量，一次查全，前端切换指标不再打请求）。

        自适应粒度：start 早于滚动游标（即查询跨度覆盖了已进日聚合表的老数据）
        时返回「日桶」（granularity='day'，bucket=本地 0 点 epoch 秒），否则维持
        「时桶」（granularity='hour'，bucket=epoch 秒整除 3600）。前端按
        granularity 选标签格式（日 → MM-DD，时 → HH:MM）。
        """
        clause = f" WHERE {where}" if where else ""
        rolled_to = self._rolled_to()
        if start is None or dims is None or start >= rolled_to:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    f"""SELECT CAST(ts / 3600 AS INTEGER) * 3600 AS bucket,
                              'hour' AS granularity,
                              COUNT(*) AS calls,
                              SUM(CASE WHEN outcome <> 'completed' THEN 1 ELSE 0 END) AS fail,
                              SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0))
                                  AS tokens,
                              SUM(COALESCE(cached_tokens, 0)) AS cached_tokens,
                              SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                              AVG(tokens_per_sec) AS tps
                        FROM requests{clause}
                        GROUP BY bucket ORDER BY bucket DESC LIMIT ?""",
                    (*params, max(1, min(int(buckets), 240))),
                ).fetchall()
            out = []
            for r in reversed(rows):
                item = dict(r)
                item["tps"] = round(item["tps"], 1) if item.get("tps") is not None else None
                out.append(item)
            return out

        # 日桶：日聚合表（≤ 游标）∪ 未滚动行（> 游标，含 start 边界日）
        tz = time.timezone
        day_from, _ = self._federate_days(start, rolled_to)
        if day_from is None:
            day_from = int((start + tz) // 86400) * 86400 - tz
        dim_m, dim_c = dims.get("model", ""), dims.get("channel", "")
        dim_sql = ""
        dim_params: list = []
        if dim_m:
            dim_sql += " AND model = ?"
            dim_params.append(dim_m)
        if dim_c in ("public", "local"):
            dim_sql += " AND channel = ?"
            dim_params.append(dim_c)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT day AS bucket, 'day' AS granularity,
                           SUM(calls) AS calls, SUM(fail) AS fail,
                           SUM(input_tokens + output_tokens) AS tokens,
                           SUM(cached_tokens) AS cached_tokens,
                           SUM(output_tokens) AS output_tokens,
                           CASE WHEN SUM(tps_n) > 0
                                THEN SUM(tps_sum) / SUM(tps_n) ELSE NULL END AS tps
                    FROM requests_daily
                    WHERE day >= ? AND day <= ?{dim_sql}
                    GROUP BY day
                    UNION ALL
                    SELECT CAST((ts + ?) / 86400 AS INTEGER) * 86400 - ? AS bucket,
                           'day' AS granularity,
                           COUNT(*) AS calls,
                           SUM(CASE WHEN outcome <> 'completed' THEN 1 ELSE 0 END) AS fail,
                           SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) AS tokens,
                           SUM(COALESCE(cached_tokens, 0)) AS cached_tokens,
                           SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                           AVG(tokens_per_sec) AS tps
                    FROM requests
                    WHERE ts > ? AND ts >= ?{dim_sql}
                    GROUP BY bucket
                    ORDER BY bucket""",
                (day_from, int((rolled_to + tz) // 86400) * 86400 - tz, *dim_params,
                 tz, tz, rolled_to, start, *dim_params),
            ).fetchall()
        out = []
        for r in reversed(rows):
            item = dict(r)
            item["tps"] = round(item["tps"], 1) if item.get("tps") is not None else None
            out.append(item)
        return out

    def heatmap(self, days: int = 84) -> list[dict]:
        """最近 N 天每日请求数（看板「活动热力图」/ 年度回溯用）。

        按本地日期切分：用户看到的是自己日历上的「哪一天」。
        数据源 = 日聚合表（3 天前，永久）∪ 未滚动行（最近 3 天），合并成完整日历。
        """
        days = max(7, min(int(days), 370))
        since = time.time() - days * 86400
        tz = time.timezone
        since_day = int((since + tz) // 86400) * 86400 - tz
        rolled_to = self._rolled_to()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT day, SUM(calls) AS calls, SUM(tokens) AS tokens
                   FROM (
                     SELECT day, calls,
                            input_tokens + output_tokens AS tokens
                     FROM requests_daily WHERE day >= ?
                     UNION ALL
                     SELECT CAST(strftime('%s', CAST(ts AS INTEGER), 'unixepoch',
                                         'localtime', 'start of day') AS INTEGER) AS day,
                            COUNT(*) AS calls,
                            SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) AS tokens
                     FROM requests WHERE ts > ? AND ts >= ? GROUP BY day
                   ) GROUP BY day ORDER BY day""",
                (since_day, rolled_to, since),
            ).fetchall()
        # day = 本地 0 点 epoch 秒，前端直接用
        return [
            {"day": int(r["day"]), "calls": int(r["calls"]),
             "tokens": int(r["tokens"] or 0)}
            for r in rows
        ]

    def clear(self) -> int:
        with closing(self._connect()) as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
            conn.execute("DELETE FROM requests")
            conn.commit()
        return int(n)


def _finalize(out: dict) -> dict:
    """和式 → 面板口径（成功率/缓存命中/均值由和与计数现算）。"""
    out["calls"] = int(out.get("calls") or 0)
    for k in ("ok", "fail", "input_tokens", "output_tokens", "cached_tokens",
              "usage_missing", "fallback", "ttft_n", "gen_n", "tps_n"):
        out[k] = int(out.get(k) or 0)
    out["credit"] = float(out.get("credit") or 0.0)
    done = out["ok"] + out["fail"]
    out["success_rate"] = round(out["ok"] * 100.0 / done, 1) if done else None
    prompt_all = out["input_tokens"] + out["cached_tokens"]
    out["cache_hit_pct"] = (
        round(out["cached_tokens"] * 100.0 / prompt_all, 1) if prompt_all else None
    )
    out["avg_ttft_ms"] = round(out["ttft_sum"] / out["ttft_n"], 1) if out.get("ttft_n") else None
    out["avg_gen_ms"] = round(out["gen_sum"] / out["gen_n"], 1) if out.get("gen_n") else None
    out["avg_total_ms"] = round(out["total_sum"] / out["gen_n"], 1) \
        if out.get("gen_n") and out.get("total_sum") else None
    out["avg_tps"] = round(out["tps_sum"] / out["tps_n"], 1) if out.get("tps_n") else None
    out["peak_tps"] = round(out["tps_max"], 1) if out.get("tps_max") else None
    return out


def _merge_stats(a: dict, b: dict) -> dict:
    """两个「和式表示」合并（行级 + 日聚合 → 全量视图）。"""
    out = dict(a)
    for k in ("calls", "ok", "fail", "input_tokens", "output_tokens", "cached_tokens",
              "usage_missing", "fallback", "ttft_n", "gen_n", "tps_n"):
        out[k] = int(out.get(k) or 0) + int(b.get(k) or 0)
    for k in ("credit", "ttft_sum", "gen_sum", "total_sum", "tps_sum"):
        out[k] = float(out.get(k) or 0) + float(b.get(k) or 0)
    m = max(float(a.get("tps_max") or 0), float(b.get("tps_max") or 0))
    out["tps_max"] = m or None
    return _finalize(out)



def _normalize(key: str, value):
    """按列语义归一化：数据库里不放大对象、不写 None 到 NOT NULL 列。"""
    if key in ("stream", "usage_missing"):
        return 1 if value else 0
    if key == "switches":
        # 换号轨迹以 JSON 文本落库；空列表归一成 '[]'（列是 NOT NULL）
        return json.dumps(value or [], ensure_ascii=False, separators=(",", ":"))
    if key == "channel":
        # 来源只有 public / local 两值；缺失或脏值一律按 local（内网）落库，
        # 否则老调用点（没传 channel）会在按来源分组里冒出一个空名分组
        return value if value in ("public", "local") else "local"
    if value is None:
        return None if key in ("http_status", "total_ms", "ttft_ms", "gen_ms",
                               "input_tokens", "output_tokens", "cached_tokens",
                               "credit", "tokens_per_sec", "cache_hit_pct") else ""
    return value


usagelog = UsageLog()
