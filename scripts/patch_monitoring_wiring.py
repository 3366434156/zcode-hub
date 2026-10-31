#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监控四项后端接线补丁：reqlog 计数 / settings 保留期 / admin_api 传参与新增字段。"""
import sys
import pathlib

sys.stdout.reconfigure(encoding="utf-8")
ROOT = pathlib.Path(__file__).resolve().parents[1]

# ── 1. reqlog.inflight_count ──
p = ROOT / "app" / "reqlog.py"
t = p.read_text(encoding="utf-8")
old = '''def snapshot() -> list[dict]:
    """全部条目，最新在前（含在途）。"""
    with _lock:
        return [dict(e) for e in reversed(_entries)]'''
new = '''def snapshot() -> list[dict]:
    """全部条目，最新在前（含在途）。"""
    with _lock:
        return [dict(e) for e in reversed(_entries)]


def inflight_count() -> int:
    """当前正在请求中的数量（看板「进行中」徽章的数据源）。"""
    with _lock:
        return len(_inflight)'''
assert old in t, "reqlog 锚点未命中"
t = t.replace(old, new, 1)
p.write_text(t, encoding="utf-8")
print("reqlog.inflight_count 完成")

# ── 2. settings 保留期默认 3 天 ──
p = ROOT / "app" / "settings.py"
t = p.read_text(encoding="utf-8")
old = 'USAGE_RETENTION_DAYS = _int("ZCODE_USAGE_RETENTION_DAYS", 30)'
new = ('# 请求明细只保留 3 天（Token/成败统计在清理前滚动进日聚合表 requests_daily，永久保留）\n'
       'USAGE_RETENTION_DAYS = _int("ZCODE_USAGE_RETENTION_DAYS", 3)')
assert old in t, "settings 锚点未命中"
t = t.replace(old, new, 1)
p.write_text(t, encoding="utf-8")
print("settings 保留期 3 天完成")

# ── 3. admin_api：usage 传 start/dims + fallbacks；monitoring 加 inflight ──
p = ROOT / "app" / "routes" / "admin_api.py"
t = p.read_text(encoding="utf-8")
old = '''    where, params = _usage_filter(range, outcome, account, model, q, channel)
    base_where, base_params = _usage_filter(range, outcome, account, model, q, "")'''
new = '''    where, params = _usage_filter(range, outcome, account, model, q, channel)
    base_where, base_params = _usage_filter(range, outcome, account, model, q, "")
    start = _range_start(range)
    # 联邦维度：带账号/关键词过滤的查询不联邦（日聚合表没有这两个粒度）
    dims = None
    if not account and not q and outcome in ("", "all", "fail", "completed"):
        dims = {"model": model, "channel": channel if channel in ("public", "local") else ""}'''
assert old in t, "admin_api 锚点1未命中"
t = t.replace(old, new, 1)

old = '''        "by_endpoint": usagelog.group_by("endpoint", where, params),
        "by_channel": usagelog.group_by("channel", base_where, base_params),
        "series": usagelog.series(where, params),
        "heatmap": usagelog.heatmap(heatmap_days),
    }'''
new = '''        "by_endpoint": usagelog.group_by("endpoint", where, params),
        "by_channel": usagelog.group_by("channel", base_where, base_params),
        "by_model": usagelog.group_by("model", where, params, start=start, dims=dims),
        "series": usagelog.series(where, params, start=start, dims=dims),
        "heatmap": usagelog.heatmap(heatmap_days),
        "fallbacks": usagelog.fallbacks(where, params, limit=50),
        "retention_days": settings.USAGE_RETENTION_DAYS,
    }'''
assert old in t, "admin_api 锚点2未命中"
t = t.replace(old, new, 1)

old = '''    return {"entries": reqlog.snapshot(), "keep": reqlog.KEEP}'''
new = '''    return {"entries": reqlog.snapshot(), "keep": reqlog.KEEP,
            "inflight": reqlog.inflight_count()}'''
assert old in t, "admin_api 锚点3未命中"
t = t.replace(old, new, 1)
p.write_text(t, encoding="utf-8")
print("admin_api 接线完成")

# ── 4. .env.example 文档 ──
p = ROOT / ".env.example"
t = p.read_text(encoding="utf-8")
if "ZCODE_USAGE_RETENTION_DAYS" not in t:
    t = t.rstrip() + ("\n\n# 请求明细保留天数（Token/成败统计会先滚动进日聚合表，永久保留）\n"
                      "# ZCODE_USAGE_RETENTION_DAYS=3\n"
                      "# 请求明细行数上限（超出按最老删除，同样先聚合再删）\n"
                      "# ZCODE_USAGE_MAX_ROWS=50000\n")
    p.write_text(t, encoding="utf-8")
    print(".env.example 文档完成")
