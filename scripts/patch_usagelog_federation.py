#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""usagelog.py 联邦查询改造补丁（A/B/C 三段），幂等：带锚点断言。"""
import sys
import pathlib

sys.stdout.reconfigure(encoding="utf-8")
p = pathlib.Path(__file__).resolve().parents[1] / "app" / "usagelog.py"
t = p.read_text(encoding="utf-8")

# ── A. _prev_stats 加联邦参数 ──
old = '''    def _prev_stats(self, where: str, params: tuple) -> dict:
        """上一个等长周期的同口径聚合（拿不到就返回空 dict，前端显示「—」）。"""
        try:
            if not where or not params:
                return {}
            # where 形如 "ts >= ? AND ..."：把起点往前推一个区间长度即可
            start = float(params[0])
            span = max(1.0, time.time() - start)
            prev_where = "ts >= ? AND ts < ?" + (
                (" AND " + where.split(" AND ", 1)[1]) if " AND " in where else ""
            )
            prev_params = (start - span, start) + tuple(params[1:])
            data = self._stats_once(prev_where, prev_params)
            # 只留前端要用的几个，省得响应体翻倍
            keep = ("calls", "ok", "fail", "input_tokens", "output_tokens",
                    "cached_tokens", "fallback", "success_rate", "cache_hit_pct",
                    "avg_tps", "avg_ttft_ms", "avg_gen_ms", "avg_total_ms")
            return {k: data.get(k) for k in keep}
        except (TypeError, ValueError, sqlite3.Error):
            return {}'''
new = '''    def _prev_stats(self, where: str, params: tuple, start: float | None = None,
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
            return {}'''
assert old in t, "A 锚点未命中"
t = t.replace(old, new, 1)

# ── B. _stats_once 改和式表示 ──
old = '''    def _stats_once(self, where: str, params: tuple) -> dict:
        """单次区间聚合（stats 与「上一周期」共用同一段 SQL，口径不可能漂）。"""
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
                      AVG(ttft_ms)  AS avg_ttft_ms,
                      AVG(gen_ms)   AS avg_gen_ms,
                      AVG(total_ms) AS avg_total_ms,
                      AVG(tokens_per_sec) AS avg_tps,
                      MAX(tokens_per_sec) AS peak_tps
                    FROM requests{clause}""",
                params,
            ).fetchone()
        out = dict(row)
        for key in ("calls", "ok", "fail", "input_tokens", "output_tokens",
                    "cached_tokens", "usage_missing", "fallback"):
            out[key] = int(out.get(key) or 0)
        out["credit"] = float(out.get("credit") or 0.0)
        done = out["ok"] + out["fail"]
        out["success_rate"] = round(out["ok"] * 100.0 / done, 1) if done else None
        prompt_all = out["input_tokens"] + out["cached_tokens"]
        out["cache_hit_pct"] = (
            round(out["cached_tokens"] * 100.0 / prompt_all, 1) if prompt_all else None
        )
        for key in ("avg_ttft_ms", "avg_gen_ms", "avg_total_ms", "avg_tps", "peak_tps"):
            out[key] = round(out[key], 1) if out.get(key) is not None else None
        return out'''
new = '''    def _stats_once(self, where: str, params: tuple) -> dict:
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
        return _finalize(out)'''
assert old in t, "B 锚点未命中"
t = t.replace(old, new, 1)

# ── C. 模块级 _finalize / _merge_stats ──
anchor = "\n\ndef _normalize(key: str, value):"
helpers = '''

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
    out["avg_total_ms"] = round(out["total_sum"] / out["gen_n"], 1) \\
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

'''
assert anchor in t, "C 锚点未命中"
t = t.replace(anchor, helpers + anchor, 1)
p.write_text(t, encoding="utf-8")
print("A/B/C 三段完成")
