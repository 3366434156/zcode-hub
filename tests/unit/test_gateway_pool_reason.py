"""503 文案：池子无可用账号时必须说清原因（2026-11-02 上游 5xx 全池冷却）。"""

from __future__ import annotations

import time

from app.models import Account, Status
from app.routes import gateway


def _acc(name: str, status: str = Status.ACTIVE, *, cooling_left: int = 0) -> Account:
    acc = Account.create("zai", name, "a.b.c")
    acc.status = status
    acc.cooling_until = (time.time() + cooling_left) if cooling_left else None
    return acc


def _patch(monkeypatch, accounts):
    class _Store:
        @staticmethod
        def list_accounts(_provider):
            return accounts

    monkeypatch.setattr(gateway, "store", _Store())


def test_all_cooling_reports_upstream_5xx_and_eta(monkeypatch):
    _patch(monkeypatch, [_acc(f"a{i}", Status.COOLING, cooling_left=120) for i in range(3)])
    reason = gateway._pool_unavailable_reason("zai")
    assert "冷却中" in reason and "上游 5xx" in reason
    assert "自动恢复" in reason
    assert "无可用账号" not in reason, "不能再回笼统文案，否则会被当成额度/账号坏了"


def test_all_exhausted(monkeypatch):
    _patch(monkeypatch, [_acc("a", Status.EXHAUSTED), _acc("b", Status.EXHAUSTED)])
    assert "额度已耗尽" in gateway._pool_unavailable_reason("zai")


def test_all_invalid(monkeypatch):
    _patch(monkeypatch, [_acc("a", Status.INVALID), _acc("b", Status.DISABLED)])
    assert "重新授权" in gateway._pool_unavailable_reason("zai")


def test_no_enabled_accounts(monkeypatch):
    acc = _acc("off")
    acc.enabled = False
    _patch(monkeypatch, [acc])
    assert "没有启用的账号" in gateway._pool_unavailable_reason("zai")


def test_mixed_reason_lists_bad_accounts(monkeypatch):
    _patch(monkeypatch, [
        _acc("ok", Status.ACTIVE),
        _acc("cool", Status.COOLING, cooling_left=60),
        _acc("dead", Status.INVALID),
    ])
    reason = gateway._pool_unavailable_reason("zai")
    assert "cool" in reason and "dead" in reason
    assert "ok" not in reason


class _PoolStore:
    def __init__(self, accounts):
        self.accounts = accounts
        self.updated = []

    def list_accounts(self, _provider):
        return self.accounts

    def update_account(self, acc):
        self.updated.append(acc.name)
        return True


def _five_xx(name: str, code: int = 503, left: int = 300) -> Account:
    acc = _acc(name, Status.COOLING, cooling_left=left)
    acc.last_error = f"上游 HTTP {code} 重试 3 次耗尽，冷却"
    return acc


def test_probe_opens_one_five_xx_cooling_account(monkeypatch):
    a, b = _five_xx("a", 502), _five_xx("b", 503, left=120)
    store = _PoolStore([a, b])
    monkeypatch.setattr(gateway, "store", store)
    monkeypatch.setattr(gateway, "_last_pool_probe", 0.0)

    probe = gateway._pool_probe_account("zai")
    assert probe is b, "优先探测最早到期的那个号"
    assert probe.cooling_until is None and probe.status == Status.ACTIVE
    assert store.updated == ["b"], "半开状态需要落库"
    # 冷却间隔内不再探测（避免对上游连续施压）
    assert gateway._pool_probe_account("zai") is None


def test_probe_skips_risk_or_manual_cooling(monkeypatch):
    a = _acc("risk", Status.COOLING, cooling_left=300)
    a.last_error = "风控 3012，冷却中"
    b = _five_xx("five", 503)
    monkeypatch.setattr(gateway, "store", _PoolStore([a, b]))
    monkeypatch.setattr(gateway, "_last_pool_probe", 0.0)
    assert gateway._pool_probe_account("zai") is None, "风控冷却不得用探测绕过"


def test_probe_only_when_whole_pool_cooling(monkeypatch):
    a = _five_xx("a")
    ok = _acc("ok", Status.ACTIVE)
    monkeypatch.setattr(gateway, "store", _PoolStore([a, ok]))
    monkeypatch.setattr(gateway, "_last_pool_probe", 0.0)
    assert gateway._pool_probe_account("zai") is None, "还有可用号时不需要半开探测"
