"""reqlog 请求监控环形日志单元测试。

覆盖：环内生命周期、账号标记、终止态（outcome）、错误截断、
以及「内存条目 → 台账行」的派生口径（首字 / 生成窗口 / tok·s / 缓存命中 / 非流式）。
"""

from __future__ import annotations

import time

import pytest

from app import reqlog


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    """隔离台账：reqlog.clear() 会连持久台账一起清，绝不能碰真实 usage.db。"""
    from app import usagelog as usagelog_module

    log = usagelog_module.UsageLog(tmp_path / "usage.db")
    monkeypatch.setattr(reqlog, "usagelog", log)
    reqlog.clear()
    yield
    reqlog.clear()
    log.close()


class TestRingBuffer:
    def test_begin_finish_ok_lifecycle(self):
        reqlog.begin("r1", "messages", "GLM-5.3-Flash", True, "你好")
        reqlog.mark_account("r1", "acc-1", "jwt", "id-1")
        reqlog.finish_ok("r1", t_first=0.5, status=200)
        snap = reqlog.snapshot()
        assert len(snap) == 1
        e = snap[0]
        assert e["req_id"] == "r1"
        assert e["ok"] is True and e["outcome"] == reqlog.OUTCOME_COMPLETED
        assert e["status"] == 200
        assert e["account"] == "acc-1" and e["mode"] == "jwt" and e["account_id"] == "id-1"
        assert e["t_first"] == 0.5
        assert e["t_total"] is not None and e["t_total"] >= 0
        assert e["stream"] is True and e["endpoint"] == "messages"

    def test_inflight_entry_visible(self):
        reqlog.begin("r2", "chat", "GLM-5.3", False, "hi")
        snap = reqlog.snapshot()
        assert len(snap) == 1
        assert snap[0]["ok"] is None
        assert snap[0]["outcome"] is None
        assert snap[0]["t_total"] is None

    def test_finish_error_truncates_message(self):
        reqlog.begin("r3", "messages", "GLM-5.3", False, "")
        reqlog.finish_error("r3", "x" * 500, status=400)
        e = reqlog.snapshot()[0]
        assert e["ok"] is False and e["status"] == 400
        assert e["outcome"] == reqlog.OUTCOME_FAILED
        assert len(e["error"]) == 300

    def test_client_abort_is_its_own_outcome(self):
        """客户端断开不是「上游失败」也不是「成功」：单独一个终态。"""
        reqlog.begin("r4", "chat", "GLM-5.3", True, "")
        reqlog.finish_error("r4", "客户端断开", status=499,
                            outcome=reqlog.OUTCOME_CLIENT_ABORTED)
        e = reqlog.snapshot()[0]
        assert e["outcome"] == "client_aborted" and e["ok"] is False

    def test_finish_unknown_req_id_noop(self):
        reqlog.finish_ok("ghost")
        reqlog.finish_error("ghost", "x")
        assert reqlog.snapshot() == []

    def test_ring_buffer_cap(self):
        for i in range(reqlog.KEEP + 50):
            reqlog.begin(f"r{i}", "messages", "m", False, "")
            reqlog.finish_ok(f"r{i}")
        snap = reqlog.snapshot()
        assert len(snap) == reqlog.KEEP
        assert snap[0]["req_id"] == f"r{reqlog.KEEP + 49}"  # 最新在前

    def test_clear(self):
        reqlog.begin("r", "messages", "m", False, "")
        reqlog.clear()
        assert reqlog.snapshot() == []

    def test_mark_account_last_wins(self):
        reqlog.begin("r", "messages", "m", False, "")
        reqlog.mark_account("r", "a", "jwt")
        reqlog.mark_account("r", "b", "apiKey")
        reqlog.finish_ok("r")
        assert reqlog.snapshot()[0]["account"] == "b"

    def test_timestamps_monotonic(self):
        t0 = time.time()
        reqlog.begin("r", "messages", "m", False, "")
        reqlog.finish_ok("r")
        e = reqlog.snapshot()[0]
        assert t0 - 1 <= e["ts"] <= time.time() + 1


class TestLedgerRow:
    """内存条目 → 台账行的派生口径（_row 是纯函数，直接喂条目最清楚）。"""

    @staticmethod
    def _row(stream: bool = True, usage: dict | None = None, ttft: float | None = None,
             t_last: float | None = None, total: float = 3.0,
             outcome: str | None = None, status: int | None = 200,
             error: str = "") -> dict:
        reqlog.begin("x", "chat", "GLM-5.3", stream, "预览")
        entry = reqlog._inflight["x"]
        if usage:
            reqlog.note_usage("x", **usage)
        entry["ttft"] = ttft
        entry["t_last"] = t_last
        entry["t_total"] = total
        entry["outcome"] = outcome or reqlog.OUTCOME_COMPLETED
        entry["status"] = status
        entry["error"] = error
        return reqlog._row(entry)

    def test_stream_rates(self):
        row = self._row(ttft=1.0, t_last=3.0,
                        usage={"input_tokens": 100, "output_tokens": 100,
                               "cached_tokens": 300})
        assert row["ttft_ms"] == 1000 and row["gen_ms"] == 2000
        assert row["tokens_per_sec"] == 50.0           # 100 tok / 2s
        assert row["cache_hit_pct"] == 75.0            # 300 / (100+300)
        assert row["usage_missing"] == 0
        assert row["outcome"] == reqlog.OUTCOME_COMPLETED

    def test_nonstream_has_no_ttft_and_uses_total_window(self):
        """非流式没有首字可言：ttft 置空，生成窗口退化为总耗时。"""
        row = self._row(stream=False, total=2.0,
                        usage={"input_tokens": 200, "output_tokens": 40})
        assert row["ttft_ms"] is None
        assert row["gen_ms"] == 2000
        assert row["tokens_per_sec"] == 20.0           # 40 tok / 2s

    def test_usage_missing_is_kept(self):
        """上游没报 usage 也落行：调用数/成功率/时延样本不能因此消失。"""
        row = self._row(ttft=1.0, t_last=2.0, usage={})
        assert row["usage_missing"] == 1
        assert row["input_tokens"] is None and row["output_tokens"] is None
        assert row["tokens_per_sec"] is None

    def test_failed_request_keeps_outcome_and_status(self):
        row = self._row(stream=False, total=1.0, outcome=reqlog.OUTCOME_FAILED,
                        status=429, error="上游 3009 并发已满")
        assert row["outcome"] == reqlog.OUTCOME_FAILED
        assert row["http_status"] == 429
        assert row["error"] == "上游 3009 并发已满"
