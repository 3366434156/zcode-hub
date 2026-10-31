"""额度窗口到期过滤（2026-10-26「已经消失的额度」）。

背景：上游 billing/balance 在本地日切（09:00）之后仍会列出上一窗口，`remaining`
还是旧数字。hub 原来照单全收——后台额度面板一直显示「2.48 亿 / 3.00 亿 · 至 10/26
09:00」，而这段额度其实已经过期不可用，耗尽判定也被旧数字带偏。

修复后：到期窗口（expires_at <= now）不落库、不展示、不参与耗尽判定；balance 正常
返回时以本轮结果为准（过滤后为空也要清掉上一轮残留）。
"""

from __future__ import annotations

import time

import pytest

from app import quota
from app.models import Account, Status


class _Resp:
    def __init__(self, status: int = 200, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _FakeClient:
    """替身 httpx.AsyncClient：按 URL 后缀返回预置响应。"""

    responses: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        for suffix, resp in self.responses.items():
            if url.endswith(suffix):
                return resp
        return _Resp(404)


def _balance(*windows):
    return _Resp(200, {"code": 0, "data": {"balances": list(windows)}})


def _window(name, total, used, remaining, expires_at):
    return {"show_name": name, "total_units": total, "used_units": used,
            "remaining_units": remaining, "expires_at": expires_at}


@pytest.fixture
def account():
    acc = Account(id="acc-1", name="t", provider="zai", mode="jwt", jwt_token="a.b.c")
    return acc


@pytest.fixture
def fake_store(monkeypatch, account):
    class _Store:
        def find(self, provider, ident):
            return account

        def update_account(self, acc):
            return True

    monkeypatch.setattr(quota, "store", _Store())
    return account


def _use(monkeypatch, responses):
    monkeypatch.setattr(quota.httpx, "AsyncClient", type(
        "_BoundFakeClient", (_FakeClient,), {"responses": responses}
    ))


class TestExpiryHelpers:
    def test_epoch_seconds(self):
        assert quota._expires_ts(1_700_000_000) == 1_700_000_000
        assert quota._window_expired(1_700_000_000, 1_700_000_001) is True
        assert quota._window_expired(1_700_000_000, 1_699_999_999) is False

    def test_boundary_is_expired(self):
        now = 1_700_000_000.0
        assert quota._window_expired(now, now) is True

    def test_milliseconds_normalized(self):
        assert quota._expires_ts(1_700_000_000_000) == 1_700_000_000
        assert quota._window_expired(1_700_000_000_000, 1_700_000_001) is True

    def test_iso_string(self):
        ts = quota._expires_ts("2099-01-01T00:00:00Z")
        assert ts > time.time()
        assert quota._window_expired("2099-01-01T00:00:00Z", time.time()) is False
        assert quota._window_expired("2000-01-01T00:00:00Z", time.time()) is True

    def test_missing_or_garbage_kept(self):
        for raw in (None, "", 0, -5, "not-a-date", {}, []):
            assert quota._expires_ts(raw) == 0.0
            assert quota._window_expired(raw, time.time()) is False


class TestFetchQuotaDropsExpired:
    async def test_expired_window_dropped_live_kept(self, monkeypatch, account, fake_store):
        now = time.time()
        _use(monkeypatch, {
            "/billing/current": _Resp(200, {"code": 0, "data": {"plans": []}}),
            "/billing/balance": _balance(
                _window("GLM-5.3-Flash", 300_000_000, 51_564_044, 248_435_956, now - 3600),
                _window("GLM-5.3", 3_000_000, 0, 3_000_000, now + 3600),
            ),
        })
        res = await quota.fetch_quota(account)
        assert list(account.quota) == ["GLM-5.3"]
        assert account.quota["GLM-5.3"]["remaining"] == 3_000_000
        assert res["expired_windows"] == 1
        assert account.status == Status.ACTIVE

    async def test_all_expired_clears_stale_snapshot(self, monkeypatch, account, fake_store):
        now = time.time()
        account.quota = {"GLM-5.3-Flash": {"total": 300_000_000, "used": 1,
                                           "remaining": 299_999_999, "expires_at": now - 10}}
        _use(monkeypatch, {
            "/billing/current": _Resp(200, {"code": 0, "data": {"plans": []}}),
            "/billing/balance": _balance(
                _window("GLM-5.3-Flash", 300_000_000, 51_564_044, 248_435_956, now - 3600),
            ),
        })
        res = await quota.fetch_quota(account)
        assert account.quota == {}
        # 没有可用窗口不等于「额度用完」：不把账号打成 EXHAUSTED
        assert account.status == Status.ACTIVE
        assert res["expired_windows"] == 1

    async def test_live_window_only_no_merge_with_expired(self, monkeypatch, account, fake_store):
        """同模型新旧两窗口：只合并仍然有效的，过期那个不参与累加。"""
        now = time.time()
        _use(monkeypatch, {
            "/billing/current": _Resp(200, {"code": 0, "data": {"plans": []}}),
            "/billing/balance": _balance(
                _window("GLM-5.3-Flash", 300_000_000, 51_000_000, 249_000_000, now - 60),
                _window("GLM-5.3-Flash", 300_000_000, 0, 300_000_000, now + 3600),
            ),
        })
        await quota.fetch_quota(account)
        assert account.quota["GLM-5.3-Flash"]["total"] == 300_000_000
        assert account.quota["GLM-5.3-Flash"]["remaining"] == 300_000_000

    async def test_balance_failure_keeps_existing_snapshot(self, monkeypatch, account, fake_store):
        stale = {"GLM-5.3": {"total": 3_000_000, "used": 0, "remaining": 3_000_000,
                             "expires_at": 0}}
        account.quota = dict(stale)
        _use(monkeypatch, {
            "/billing/current": _Resp(500, None),
            "/billing/balance": _Resp(502, None),
        })
        res = await quota.fetch_quota(account)
        assert account.quota == stale, "上游取不到数据时不能凭空清空快照"
        assert res.get("error") == "无法获取额度数据"

    async def test_exhausted_still_detected_for_live_window(self, monkeypatch, account, fake_store):
        now = time.time()
        _use(monkeypatch, {
            "/billing/current": _Resp(200, {"code": 0, "data": {"plans": []}}),
            "/billing/balance": _balance(
                _window("GLM-5.3", 3_000_000, 3_000_000, 0, now + 3600),
            ),
        })
        await quota.fetch_quota(account)
        assert account.status == Status.EXHAUSTED
        assert account.last_error == "额度已用完"
