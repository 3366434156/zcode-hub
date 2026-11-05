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


class TestBalancePlans:
    """billing/balance 的 data.plans[]：领取成功但 current 为空时的唯一证据。

    2026-11-02 实证：领取 zcode-v3-start-plan-1102 后 billing/current 只剩
    `{"code":0}`，balance 才带 active 套餐与 effective_at=23:00 的待生效赠送。
    只读 current 会让后台显示「无套餐数据」，账号也一直停在 exhausted。
    """

    def test_merge_prefers_balance_fields_keeps_current_extras(self):
        current = [{"plan_id": "p1", "show_name": "Start Plan", "status": "active"}]
        balance = [{"plan_id": "p1", "name": "ZCODE GLOBAL BUILD",
                    "status": "active", "starts_at": 100, "ends_at": 200,
                    "entitlements": [{"show_name": "GLM-5.3-Flash",
                                      "grant_units": 300_000_000,
                                      "period": "one_time", "effective_at": 150}]}]
        merged = quota._merge_user_plans(current, balance)
        assert len(merged) == 1
        assert merged[0]["show_name"] == "Start Plan", "current 独有字段不能丢"
        assert merged[0]["name"] == "ZCODE GLOBAL BUILD", "balance 同名字段更权威"
        assert merged[0]["entitlements"][0]["effective_at"] == 150

    async def test_balance_only_plans_land_in_account(self, monkeypatch, account, fake_store):
        now = time.time()
        balance = _Resp(200, {"code": 0, "data": {
            "balances": [],
            "plans": [{
                "user_plan_id": "upl_1", "plan_id": "zcode-v3-start-plan-1102",
                "name": "ZCODE GLOBAL BUILD", "status": "active",
                "starts_at": now - 60, "ends_at": now + 86400,
                "entitlements": [{
                    "show_name": "GLM-5.3-Flash", "meter": "model_usage",
                    "unit_type": "token", "grant_units": 300_000_000,
                    "period": "one_time", "effective_at": now + 3600,
                }],
            }],
        }})
        _use(monkeypatch, {
            # 领取后 current 实测只剩 {"code":0}（无 data）
            "/billing/current": _Resp(200, {"code": 0}),
            "/billing/balance": balance,
        })
        await quota.fetch_quota(account)
        assert [p["plan_id"] for p in account.plans] == ["zcode-v3-start-plan-1102"]
        assert account.plan["entitlements"][0]["grant_units"] == 300_000_000
        # 赠送尚未生效（effective_at 未来）：不能据此判可用，账号状态保持原样
        assert account.status == Status.ACTIVE
