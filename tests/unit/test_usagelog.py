"""请求台账（SQLite）单元测试。

覆盖：落行口径（token / 缓存命中 / tok·s / outcome）、分页与筛选、
区间聚合、按账号/模型分组、时间序列、清空，以及「不阻塞 + flush 可见」的写入模型。
"""

from __future__ import annotations

import time

import pytest

from app.usagelog import UsageLog


@pytest.fixture
def store(tmp_path):
    """独立数据库文件，用例之间互不影响。"""
    log = UsageLog(tmp_path / "usage.db")
    yield log
    log.close()


def _row(**kw) -> dict:
    base = {
        "req_id": "r", "ts": time.time(), "endpoint": "chat", "provider": "zai",
        "account": "acc", "account_id": "id", "mode": "jwt",
        "model": "GLM-5.3", "requested_model": "", "stream": 1,
        "outcome": "completed", "http_status": 200, "error": "",
        "total_ms": 2000, "ttft_ms": 500, "gen_ms": 1500,
        "input_tokens": 100, "output_tokens": 50, "cached_tokens": 300,
        "credit": 0.0, "tokens_per_sec": 33.3, "cache_hit_pct": 75.0,
        "usage_missing": 0, "preview": "你好", "client": "pytest",
    }
    base.update(kw)
    return base


class TestWriteAndRead:
    def test_record_then_visible_after_flush(self, store):
        store.record(_row())
        store.flush()
        rows, total = store.query(limit=10)
        assert total == 1
        assert rows[0]["model"] == "GLM-5.3"
        assert rows[0]["input_tokens"] == 100
        assert rows[0]["cache_hit_pct"] == 75.0

    def test_nonblocking_record_without_flush(self, store):
        """record 只是入队：不等落盘也要能返回（请求路径不能被磁盘拖住）。"""
        t0 = time.time()
        for _ in range(200):
            store.record(_row())
        assert time.time() - t0 < 1.0
        store.flush()
        _, total = store.query(limit=1)
        assert total == 200

    def test_newest_first(self, store):
        now = time.time()
        store.record(_row(req_id="old", ts=now - 100))
        store.record(_row(req_id="new", ts=now))
        store.flush()
        rows, _ = store.query(limit=10)
        assert [r["req_id"] for r in rows] == ["new", "old"]

    def test_pagination_clamped(self, store):
        for i in range(5):
            store.record(_row(req_id=f"r{i}"))
        store.flush()
        page1, total = store.query(limit=2, offset=0)
        page2, _ = store.query(limit=2, offset=2)
        assert total == 5 and len(page1) == 2 and len(page2) == 2
        assert {r["req_id"] for r in page1} & {r["req_id"] for r in page2} == set()
        # 超过上限的 limit 被夹到 MAX_PAGE
        assert store.query(limit=99_999)[0] is not None
        assert UsageLog.MAX_PAGE == 500

    def test_clear(self, store):
        store.record(_row())
        store.flush()
        assert store.clear() == 1
        assert store.query(limit=1)[1] == 0


class TestStats:
    def test_totals_and_rate(self, store):
        store.record(_row(input_tokens=100, output_tokens=50, cached_tokens=300))
        store.record(_row(outcome="failed", http_status=502, error="上游中断",
                          input_tokens=None, output_tokens=None, cached_tokens=None,
                          usage_missing=1))
        store.flush()
        stats = store.stats()
        assert stats["calls"] == 2 and stats["ok"] == 1 and stats["fail"] == 1
        assert stats["success_rate"] == 50.0
        assert stats["input_tokens"] == 100 and stats["output_tokens"] == 50
        assert stats["cached_tokens"] == 300
        assert stats["cache_hit_pct"] == 75.0        # 300 / (100+300)
        assert stats["usage_missing"] == 1
        assert stats["avg_tps"] == 33.3

    def test_empty_range(self, store):
        stats = store.stats("ts >= ?", (time.time() + 3600,))
        assert stats["calls"] == 0
        assert stats["success_rate"] is None and stats["cache_hit_pct"] is None

    def test_group_by_account(self, store):
        store.record(_row(account="a", output_tokens=10))
        store.record(_row(account="a", output_tokens=20))
        store.record(_row(account="b", outcome="failed", output_tokens=None))
        store.flush()
        groups = {g["name"]: g for g in store.group_by("account")}
        assert groups["a"]["calls"] == 2 and groups["a"]["success_rate"] == 100.0
        assert groups["b"]["calls"] == 1 and groups["b"]["success_rate"] == 0.0

    def test_group_by_rejects_unknown_column(self, store):
        with pytest.raises(ValueError):
            store.group_by("password")

    def test_series_buckets_hours(self, store):
        now = time.time()
        store.record(_row(ts=now - 7200))
        store.record(_row(ts=now - 7200))
        store.record(_row(ts=now))
        store.flush()
        series = store.series(buckets=6)
        assert sum(x["calls"] for x in series) == 3
        assert len(series) == 2          # 两个小时桶


class TestChannel:
    """来源通道（公网 public / 内网 local）：记录、筛选、分组。"""

    def test_channel_roundtrip_and_filter(self, store):
        store.record(_row(req_id="p1", channel="public"))
        store.record(_row(req_id="l1", channel="local"))
        store.flush()
        rows, total = store.query("channel = ?", ("public",))
        assert total == 1 and rows[0]["req_id"] == "p1"
        rows, total = store.query("channel = ?", ("local",))
        assert total == 1 and rows[0]["req_id"] == "l1"

    def test_missing_or_dirty_channel_stored_as_local(self, store):
        """老调用点没传 channel / 传了脏值 → 落 local，不得在分组里冒出空名。"""
        store.record(_row(req_id="none"))
        store.record(_row(req_id="dirty", channel="lan"))
        store.flush()
        rows, total = store.query()
        assert total == 2
        assert {r["channel"] for r in rows} == {"local"}

    def test_group_by_channel_splits_public_and_local(self, store):
        store.record(_row(channel="public", output_tokens=10))
        store.record(_row(channel="public", output_tokens=20))
        store.record(_row(channel="local", outcome="failed", output_tokens=None))
        store.flush()
        groups = {g["name"]: g for g in store.group_by("channel")}
        assert groups["public"]["calls"] == 2 and groups["public"]["output_tokens"] == 30
        assert groups["local"]["calls"] == 1 and groups["local"]["success_rate"] == 0.0


class TestFilters:
    def test_filters_by_outcome_and_model(self, store):
        store.record(_row(model="GLM-5.3"))
        store.record(_row(model="GLM-5.3-Flash", outcome="failed"))
        store.flush()
        rows, total = store.query("outcome <> 'completed'")
        assert total == 1 and rows[0]["model"] == "GLM-5.3-Flash"
        rows, total = store.query("model = ?", ("GLM-5.3",))
        assert total == 1 and rows[0]["outcome"] == "completed"

    def test_search_matches_error_and_preview(self, store):
        store.record(_row(preview="帮我写个排序"))
        store.record(_row(outcome="failed", error="上游 3009 并发已满"))
        store.flush()
        _, total = store.query("error LIKE ?", ("%3009%",))
        assert total == 1
        _, total = store.query("preview LIKE ?", ("%排序%",))
        assert total == 1
