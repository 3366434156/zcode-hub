"""usagelog 日聚合滚动保留 + 跨期联邦查询单测。

口径：明细行 3 天滚动进 requests_daily（永久），看板长期视图 = 日聚合 ∪ 近期明细。
"""

from __future__ import annotations

import time

import pytest

from app import settings, usagelog


@pytest.fixture()
def ulog(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "USAGE_RETENTION_DAYS", 3)
    u = usagelog.UsageLog(path=tmp_path / "usage.db")
    monkeypatch.setattr(usagelog, "usagelog", u)  # 兜底：内部模块级引用
    yield u
    u.close()


def _row(ts: float, model: str = "GLM-5.3", requested: str = "", outcome: str = "completed",
         inp: int = 100, out: int = 50, cached: int = 0, error: str = "",
         preview: str = "", account: str = "tester", channel: str = "local") -> dict:
    return {
        "req_id": f"r-{ts}-{model}-{outcome}-{len(preview)}", "ts": ts,
        "endpoint": "messages", "model": model, "requested_model": requested,
        "outcome": outcome, "error": error, "input_tokens": inp,
        "output_tokens": out, "cached_tokens": cached, "preview": preview,
        "account": account, "channel": channel,
    }


def test_old_rows_rollup_and_purge(ulog):
    now = time.time()
    old = now - 5 * 86400
    for i in range(3):
        ulog.record(_row(old + i * 10, model="GLM-5.3", inp=200, out=100))
    ulog.record(_row(old + 60, model="GLM-5.2", requested="GLM-5.3"))  # 换模型
    ulog.record(_row(now - 3600, model="GLM-5.3"))                     # 近期，保留
    ulog.flush()
    ulog.close()  # 停后台写线程，消除写锁竞争
    usagelog._last_prune = 0
    conn = ulog._connect()
    try:
        ulog._maybe_prune(conn)
        conn.commit()
    finally:
        conn.close()

    with ulog._connect() as conn:
        left = conn.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
        daily = conn.execute("SELECT COUNT(*) AS n FROM requests_daily").fetchone()["n"]
        toks = conn.execute(
            "SELECT SUM(input_tokens) AS s FROM requests_daily").fetchone()["s"]
    assert left == 1, "只剩近期 1 行"
    assert toks == 3 * 200 + 100, "旧行 token 已滚动进日聚合表（含换模型行）"

    # 联邦 stats：30 天窗口 = 滚动部分 + 近期部分
    s = ulog.stats("ts >= ?", (now - 30 * 86400,), start=now - 30 * 86400,
                   dims={"model": "", "channel": ""})
    assert s["calls"] == 5
    assert s["fallback"] == 1
    assert s["input_tokens"] == 3 * 200 + 100 + 100


def test_recent_view_skips_federation(ulog):
    now = time.time()
    ulog.record(_row(now - 3600))
    ulog.flush()
    # start 在滚动游标之后 → 纯行级查询，不走联邦
    s = ulog.stats("ts >= ?", (now - 7200,), start=now - 7200,
                   dims={"model": "", "channel": ""})
    assert s["calls"] == 1


def test_series_day_buckets_for_long_range(ulog):
    now = time.time()
    old = now - 5 * 86400
    ulog.record(_row(old, model="GLM-5.3", inp=300, out=100))
    ulog.record(_row(now - 3600, model="GLM-5.3"))
    ulog.flush()
    ulog.close()  # 停后台写线程：消除手动 prune 的写锁竞争（database is locked）
    usagelog._last_prune = 0
    conn = ulog._connect()
    try:
        ulog._maybe_prune(conn)
        conn.commit()
    finally:
        conn.close()

    rows = ulog.series("ts >= ?", (now - 30 * 86400,), start=now - 30 * 86400,
                       dims={"model": "", "channel": ""})
    assert rows and all(r["granularity"] == "day" for r in rows)
    tokens = {r["bucket"]: r["tokens"] for r in rows}
    assert sum(tokens.values()) == 300 + 100 + 150


def test_heatmap_federates_daily(ulog):
    now = time.time()
    old = now - 5 * 86400
    ulog.record(_row(old))
    ulog.record(_row(old + 10))
    ulog.record(_row(now - 3600))
    ulog.flush()
    ulog.close()  # 停后台写线程：消除手动 prune 的写锁竞争（database is locked）
    usagelog._last_prune = 0
    conn = ulog._connect()
    try:
        ulog._maybe_prune(conn)
        conn.commit()
    finally:
        conn.close()

    heat = {h["day"]: h["calls"] for h in ulog.heatmap(30)}
    assert sum(heat.values()) == 3


def test_fallbacks_lists_model_switches(ulog):
    now = time.time()
    old = now - 5 * 86400
    ulog.record(_row(old, model="GLM-5.2", requested="GLM-5.3"))
    ulog.record(_row(now - 3600, model="GLM-5.2", requested="GLM-5.3"))
    ulog.flush()
    ulog.close()  # 停后台写线程：消除手动 prune 的写锁竞争（database is locked）
    usagelog._last_prune = 0
    conn = ulog._connect()
    try:
        ulog._maybe_prune(conn)
        conn.commit()
    finally:
        conn.close()
    # 换模型明细：仅查近期明细（日聚合只有计数）；旧行即使滚动丢失计数也已入账
    rows = ulog.fallbacks("ts >= ?", (now - 7200,))
    assert len(rows) == 1
    assert rows[0]["requested_model"] == "GLM-5.3"
    assert rows[0]["model"] == "GLM-5.2"


def test_fallback_count_in_federated_stats(ulog):
    now = time.time()
    old = now - 5 * 86400
    ulog.record(_row(old, model="GLM-5.2", requested="GLM-5.3"))
    ulog.record(_row(now - 3600, model="GLM-5.2", requested="GLM-5.3"))
    ulog.flush()
    ulog.close()  # 停后台写线程：消除手动 prune 的写锁竞争（database is locked）
    usagelog._last_prune = 0
    conn = ulog._connect()
    try:
        ulog._maybe_prune(conn)
        conn.commit()
    finally:
        conn.close()
    s = ulog.stats("ts >= ?", (now - 30 * 86400,), start=now - 30 * 86400,
                   dims={"model": "", "channel": ""})
    assert s["fallback"] == 2
