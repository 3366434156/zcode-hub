#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""usagelog.py 联邦查询改造补丁（D 段：series/heatmap/group_by/fallbacks）。"""
import sys
import pathlib

sys.stdout.reconfigure(encoding="utf-8")
p = pathlib.Path(__file__).resolve().parents[1] / "app" / "usagelog.py"
t = p.read_text(encoding="utf-8")

# ── D1. series：自适应时/日桶 + 联邦 ──
old = '''    def series(self, where: str = "", params: tuple = (), buckets: int = 24) -> list[dict]:
        """按小时分桶的时序（Token / 请求数 / 吞吐量，一次查全，前端切换指标不再打请求）。

        `hour` 是 epoch 秒整除 3600，前端自己转本地时间——服务端不做时区假设。
        """
        clause = f" WHERE {where}" if where else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT CAST(ts / 3600 AS INTEGER) AS hour,
                          COUNT(*) AS calls,
                          SUM(CASE WHEN outcome <> 'completed' THEN 1 ELSE 0 END) AS fail,
                          SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0))
                              AS tokens,
                          SUM(COALESCE(cached_tokens, 0)) AS cached_tokens,
                          SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                          AVG(tokens_per_sec) AS tps
                    FROM requests{clause}
                    GROUP BY hour ORDER BY hour DESC LIMIT ?""",
                (*params, max(1, min(int(buckets), 240))),
            ).fetchall()
        out = []
        for r in reversed(rows):
            item = dict(r)
            item["tps"] = round(item["tps"], 1) if item.get("tps") is not None else None
            out.append(item)
        return out'''
new = '''    def series(self, where: str = "", params: tuple = (), buckets: int = 24,
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
        return out'''
assert old in t, "D1 锚点未命中"
t = t.replace(old, new, 1)

# ── D2. heatmap：日聚合 ∪ 近期行 ──
old = '''    def heatmap(self, days: int = 84) -> list[dict]:
        """最近 N 天每日请求数（看板「活动热力图」/ 年度回溯用）。

        按本地日期切分：用户看到的是自己日历上的「哪一天」。
        """
        days = max(7, min(int(days), 370))
        since = time.time() - days * 86400
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT CAST((ts + ?) / 86400 AS INTEGER) AS day_index,
                          COUNT(*) AS calls,
                          SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) AS tokens
                   FROM requests WHERE ts >= ? GROUP BY day_index ORDER BY day_index""",
                (-time.timezone, since),
            ).fetchall()
        # day_index → 本地日期（0 点 epoch 秒），前端直接用
        return [
            {"day": int(r["day_index"]) * 86400 + time.timezone,
             "calls": int(r["calls"]), "tokens": int(r["tokens"] or 0)}
            for r in rows
        ]'''
new = '''    def heatmap(self, days: int = 84) -> list[dict]:
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
                """SELECT day AS day_index, SUM(calls) AS calls, SUM(tokens) AS tokens
                   FROM (
                     SELECT day, calls,
                            input_tokens + output_tokens AS tokens
                     FROM requests_daily WHERE day >= ?
                     UNION ALL
                     SELECT CAST((ts + ?) / 86400 AS INTEGER) AS day_index,
                            COUNT(*) AS calls,
                            SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) AS tokens
                     FROM requests WHERE ts > ? AND ts >= ? GROUP BY day_index
                   ) GROUP BY day_index ORDER BY day_index""",
                (since_day, tz, rolled_to, since),
            ).fetchall()
        # day_index → 本地日期（0 点 epoch 秒），前端直接用
        return [
            {"day": int(r["day_index"]) * 86400 + time.timezone,
             "calls": int(r["calls"]), "tokens": int(r["tokens"] or 0)}
            for r in rows
        ]'''
assert old in t, "D2 锚点未命中"
t = t.replace(old, new, 1)

# ── D3. group_by('model') 联邦 + fallbacks() ──
old = '''    def group_by(self, column: str, where: str = "", params: tuple = (),
                 limit: int = 50) -> list[dict]:
        """按账号 / 模型分组：调用数、成败、token、首字、tok/s。"""
        if column not in ("account", "model", "mode", "endpoint", "outcome", "channel"):
            raise ValueError(f"不支持的分组列: {column}")'''
new = '''    def fallbacks(self, where: str = "", params: tuple = (), limit: int = 50) -> list[dict]:
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
            raise ValueError(f"不支持的分组列: {column}")'''
assert old in t, "D3 锚点未命中"
t = t.replace(old, new, 1)

# ── D4. group_by 尾部合并（model 联邦）──
old = '''        out = []
        for r in rows:
            item = dict(r)
            done = (item.get("ok") or 0) + (item.get("fail") or 0)
            item["success_rate"] = round(item["ok"] * 100.0 / done, 1) if done else None
            item["avg_ttft_ms"] = round(item["avg_ttft_ms"], 1) if item.get("avg_ttft_ms") is not None else None
            item["avg_tps"] = round(item["avg_tps"], 1) if item.get("avg_tps") is not None else None
            out.append(item)
        return out'''
new = '''        out = []
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
        return {r["name"]: dict(r) for r in rows}'''
assert old in t, "D4 锚点未命中"
t = t.replace(old, new, 1)

p.write_text(t, encoding="utf-8")
print("D1-D4 完成")
