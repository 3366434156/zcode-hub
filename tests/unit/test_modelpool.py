"""ModelPool 纯单元测试 —— 「模型兜底」的判定与排序规则。

不碰网络、不碰 store：账号用 `accounts_getter` 注入，三项设置（enabled / chain /
ttl）直接打桩到实例上，时钟也换成可控的假时钟。因此每个用例只钉一条规则，失败
时能立刻指出是哪条规则漂了。

对应契约（task-2）：
- 反应式证据：`mark_dead` / `revive` / TTL 到期自动恢复；
- 主动式证据：`account.quota` 快照里该模型全池 remaining <= 0 → dead；
- 状态三态 quota_ok / unknown / dead 与 candidates 排序；
- 后台展示用的 snapshot 字段。
"""

from __future__ import annotations

import pytest

from app import modelpool as modelpool_module
from app.modelpool import (
    STATE_DEAD,
    STATE_QUOTA_OK,
    STATE_UNKNOWN,
    ModelPool,
)
from app.models import Account

_GLM53 = "GLM-5.3"
_FLASH = "GLM-5.3-Flash"
_GLM52 = "GLM-5.2"
_TURBO = "GLM-5-Turbo"


class _Clock:
    """可控时钟：替掉 modelpool 模块里的 `time`，TTL 用例不必真的 sleep。"""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(modelpool_module, "time", fake)
    return fake


def _account(name: str, quota: dict | None = None, enabled: bool = True) -> Account:
    """手搓账号：只有 quota 快照参与模型判定。"""
    account = Account.create("zai", name, f"a.b.{name}")
    account.quota = dict(quota or {})
    account.enabled = enabled
    return account


def _window(remaining) -> dict:
    """额度窗口快照（形状同 quota.py 落库：total/used/remaining/expires_at）。"""
    return {"total": 100, "used": 100 - (remaining or 0), "remaining": remaining,
            "expires_at": None}


def _pool(accounts=(), *, enabled: bool = True, chain: list[str] | None = None,
          ttl: int = 600) -> ModelPool:
    """注入假账号 + 打桩三项设置（生产里它们读 store 单例）。"""
    pool = ModelPool(accounts_getter=lambda provider: list(accounts))
    pool._setting_enabled = lambda: enabled
    pool._setting_chain = lambda provider: list(chain or [])
    pool._setting_ttl = lambda: ttl
    return pool


# ── 配置 ─────────────────────────────────────────────────────────────────────
class TestConfig:
    def test_enabled_follows_setting(self):
        """enabled() 如实反映设置开关（关闭即完全不兜底）。"""
        assert _pool(enabled=True).enabled() is True
        assert _pool(enabled=False).enabled() is False

    def test_chain_normalizes_case_and_dedupes(self):
        """兜底链归一化成上游大小写、去重、丢空项（配置写 glm-5.3 也能命中）。"""
        pool = _pool(chain=["glm-5.3", "GLM-5.3", "glm-5.3-flash", "", "GLM-5.2"])
        assert pool.chain("zai") == [_GLM53, _FLASH, _GLM52]

    def test_chain_keeps_unknown_model_names(self):
        """不在映射表里的模型名原样保留（新模型上线不必先改映射表）。"""
        pool = _pool(chain=["GLM-9.9"])
        assert pool.chain("zai") == ["GLM-9.9"]


# ── 反应式标记 ───────────────────────────────────────────────────────────────
class TestDeadMarker:
    def test_mark_dead_then_state_is_dead(self):
        """整轮都被上游判「这个模型不可用」→ mark_dead，state 立即变 dead 并留原因。"""
        pool = _pool(chain=[_GLM53, _FLASH])
        pool.mark_dead("zai", _GLM53, "额度用尽")
        assert pool.state("zai", _GLM53) == STATE_DEAD
        assert pool.reason("zai", _GLM53) == "额度用尽"

    def test_mark_dead_ttl_zero_is_noop(self):
        """TTL=0 表示「不记忆」：照试不跳过（后台可关掉记忆，只看单请求结果）。"""
        pool = _pool(ttl=0)
        pool.mark_dead("zai", _GLM53, "额度用尽")
        assert pool.state("zai", _GLM53) == STATE_UNKNOWN

    def test_dead_expires_after_ttl(self, clock):
        """TTL 到期自动恢复：不做人工干预，下个请求会重新尝试这个模型。"""
        pool = _pool(chain=[_GLM53, _FLASH], ttl=10)
        pool.mark_dead("zai", _GLM53, "额度用尽")
        assert pool.state("zai", _GLM53) == STATE_DEAD
        clock.advance(10.5)
        assert pool.state("zai", _GLM53) == STATE_UNKNOWN

    def test_revive_clears_dead_and_records_trust(self, clock):
        """实测成功一次即 revive：清死标记，并在 TTL 内记「这个模型确实可用」的信任票。

        信任票优先于额度快照——快照可能陈旧（日切、刚领套餐），实测成功才是硬证据。
        """
        accounts = [_account("a", {_GLM53: _window(0)})]   # 快照说没额度
        pool = _pool(accounts, chain=[_GLM53], ttl=10)
        assert pool.state("zai", _GLM53) == STATE_DEAD    # 额度证据判死
        pool.revive("zai", _GLM53)
        assert pool.state("zai", _GLM53) == STATE_QUOTA_OK
        clock.advance(10.5)                                # 信任票到期 → 回到额度证据
        assert pool.state("zai", _GLM53) == STATE_DEAD

    def test_clear_all_and_by_provider(self):
        """clear() 清空全部；clear(provider) 只清该 provider（bigmodel 不受影响）。"""
        pool = _pool()
        pool.mark_dead("zai", _GLM53, "x")
        pool.mark_dead("bigmodel", _GLM53, "x")
        pool.clear("zai")
        assert pool.state("zai", _GLM53) == STATE_UNKNOWN
        assert pool.state("bigmodel", _GLM53) == STATE_DEAD
        pool.clear()
        assert pool.state("bigmodel", _GLM53) == STATE_UNKNOWN

    def test_empty_model_name_is_ignored(self):
        """空模型名不记账（防御脏配置，避免污染 key 空间）。"""
        pool = _pool()
        pool.mark_dead("zai", "", "x")
        assert pool.reason("zai", "") == ""


# ── 主动式额度证据 ───────────────────────────────────────────────────────────
class TestQuotaEvidence:
    def test_all_accounts_zero_remaining_is_dead(self):
        """所有账号该模型 remaining <= 0 → 额度证据判 dead（零上游请求就能跳过）。"""
        accounts = [_account("a", {_GLM53: _window(0)}), _account("b", {_GLM53: _window(0)})]
        pool = _pool(accounts, chain=[_GLM53])
        assert pool.state("zai", _GLM53) == STATE_DEAD

    def test_one_account_with_remaining_is_quota_ok(self):
        """只要还有一个账号有剩余额度就是 quota_ok（排到兜底链前面）。"""
        accounts = [_account("a", {_GLM53: _window(0)}), _account("b", {_GLM53: _window(5)})]
        pool = _pool(accounts, chain=[_GLM53])
        assert pool.state("zai", _GLM53) == STATE_QUOTA_OK

    def test_no_snapshot_is_unknown(self):
        """没有任何账号有该模型快照（从没刷新过额度）→ unknown：先照试，不瞎猜。"""
        accounts = [_account("a", {}), _account("b", {_FLASH: _window(9)})]
        pool = _pool(accounts, chain=[_GLM53])
        assert pool.state("zai", _GLM53) == STATE_UNKNOWN

    def test_disabled_accounts_are_not_evidence(self):
        """被禁用/停用的账号不参与证据（它的额度数字不代表池子可用性）。"""
        accounts = [_account("a", {_GLM53: _window(0)}),
                    _account("b", {_GLM53: _window(99)}, enabled=False)]
        pool = _pool(accounts, chain=[_GLM53])
        assert pool.state("zai", _GLM53) == STATE_DEAD

    def test_remaining_sums_snapshot(self):
        """remaining() 汇总全池剩余；无快照返回 None（未知 ≠ 0）。"""
        accounts = [_account("a", {_GLM53: _window(0)}), _account("b", {_GLM53: _window(7)})]
        pool = _pool(accounts, chain=[_GLM53])
        assert pool.remaining("zai", _GLM53) == 7
        assert pool.remaining("zai", _TURBO) is None

    def test_quota_key_is_case_insensitive(self):
        """快照里的模型名大小写不敏感匹配（上游 show_name 与请求名大小写可能不同）。"""
        accounts = [_account("a", {"glm-5.3": _window(3)})]
        pool = _pool(accounts, chain=[_GLM53])
        assert pool.state("zai", _GLM53) == STATE_QUOTA_OK


# ── 兜底顺序 ─────────────────────────────────────────────────────────────────
class TestCandidates:
    def test_requested_model_is_always_first(self):
        """客户端点的模型永远排第一：先照办，再谈兜底。"""
        pool = _pool(chain=[_GLM53, _FLASH, _GLM52])
        assert pool.candidates("zai", _FLASH)[0] == _FLASH

    def test_quota_ok_sorts_before_unknown(self):
        """有余额证据的模型排在未知前面，同级保持配置顺序（稳定排序）。"""
        accounts = [_account("a", {_TURBO: _window(10)})]
        pool = _pool(accounts, chain=[_GLM53, _GLM52, _TURBO])
        assert pool.candidates("zai", "GLM-4.7") == ["GLM-4.7", _TURBO, _GLM53, _GLM52]

    def test_dead_model_is_dropped(self):
        """被判死的模型从候选里剔除：后续请求不再拿账号去撞同一堵墙。"""
        pool = _pool(chain=[_GLM53, _FLASH, _GLM52])
        pool.mark_dead("zai", _GLM53, "额度用尽")
        assert pool.candidates("zai", _GLM53) == [_FLASH, _GLM52]

    def test_all_dead_falls_back_to_requested(self):
        """整条链都死了就只留请求模型：让上游给出诚实的错误，而不是瞎换。"""
        pool = _pool(chain=[_GLM53, _FLASH])
        pool.mark_dead("zai", _GLM53, "x")
        pool.mark_dead("zai", _FLASH, "x")
        assert pool.candidates("zai", _GLM53) == [_GLM53]

    def test_empty_chain_means_no_fallback(self):
        """兜底链为空 = 该 provider 不兜底（bigmodel 默认如此）。"""
        pool = _pool(chain=[])
        assert pool.candidates("zai", _GLM53) == [_GLM53]

    def test_disabled_returns_requested_only(self):
        """总开关关闭：候选里只有请求模型，保持旧行为。"""
        pool = _pool(chain=[_GLM53, _FLASH], enabled=False)
        assert pool.candidates("zai", _GLM53) == [_GLM53]

    def test_empty_requested_model(self):
        """请求没带模型名时返回空候选（调用方自行兜底成 503）。"""
        pool = _pool(chain=[_GLM53])
        assert pool.candidates("zai", "") == []


# ── 后台展示 ─────────────────────────────────────────────────────────────────
class TestSnapshot:
    def test_snapshot_rows_and_order(self, clock):
        """snapshot 是后台状态表的数据源：链上模型 + 快照里出现过的模型，按状态排序。"""
        accounts = [
            _account("a", {_GLM53: _window(0), _GLM52: _window(500)}),
            _account("b", {_GLM53: _window(0)}),
        ]
        pool = _pool(accounts, chain=[_GLM53, _FLASH])
        rows = pool.snapshot("zai")
        # quota_ok 在前、unknown 居中、dead 在后
        assert [row["model"] for row in rows] == [_GLM52, _FLASH, _GLM53]

        by_model = {row["model"]: row for row in rows}
        dead = by_model[_GLM53]
        assert dead["state"] == STATE_DEAD
        assert dead["alive_accounts"] == 0 and dead["accounts_total"] == 2
        assert dead["remaining"] == 0
        assert dead["in_chain"] is True
        assert dead["dead_until"] is None          # 额度证据判死，没有 TTL 标记

        ok = by_model[_GLM52]
        assert ok["state"] == STATE_QUOTA_OK
        assert ok["alive_accounts"] == 1 and ok["remaining"] == 500
        assert ok["in_chain"] is False             # 只在快照里出现，不在配置链上

        unknown = by_model[_FLASH]
        assert unknown["state"] == STATE_UNKNOWN
        assert unknown["remaining"] is None

    def test_snapshot_reports_dead_until_and_reason(self, clock):
        """反应式判死要带 dead_until 与原因，后台据此显示「还剩多久重试」。"""
        pool = _pool(chain=[_GLM53], ttl=600)
        pool.mark_dead("zai", _GLM53, "GLM-5.3 额度用尽（上游 code 1005）")
        row = next(r for r in pool.snapshot("zai") if r["model"] == _GLM53)
        assert row["state"] == STATE_DEAD
        assert row["dead_until"] is not None and row["dead_until"] > clock.now
        assert "1005" in row["reason"]

    def test_snapshot_clears_reason_after_revive(self, clock):
        """revive 之后原因字段必须清空，否则后台会显示「已死但没原因」的矛盾状态。"""
        pool = _pool(chain=[_GLM53], ttl=600)
        pool.mark_dead("zai", _GLM53, "额度用尽")
        pool.revive("zai", _GLM53)
        row = next(r for r in pool.snapshot("zai") if r["model"] == _GLM53)
        assert row["state"] == STATE_QUOTA_OK
        assert row["reason"] == "" and row["dead_until"] is None
