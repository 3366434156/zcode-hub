"""全量 review 回归：删号写回、纯 Key 风控、设置脱敏。"""

from __future__ import annotations

import time

from app.models import Account, Status
from app.store import Store


class TestDeletedAccountNotResurrected:
    def test_update_account_refuses_deleted_id(self, fresh_app):
        acc = fresh_app.add_account("zai", "a", "jwt.token.a")
        aid = acc.id
        assert fresh_app.remove_account("zai", aid)
        assert fresh_app.find("zai", aid) is None

        acc.status = Status.EXHAUSTED
        acc.last_error = "后台任务写回"
        fresh_app.update_account(acc)

        assert fresh_app.find("zai", aid) is None
        reloaded = Store()
        assert reloaded.find("zai", aid) is None
        assert [a.id for a in reloaded.list_accounts("zai")] == []


class TestPureApiKeyRiskCooldown:
    def test_pure_apikey_not_selectable_during_risk_cooldown(self):
        acc = Account.create("zai", "t", "sk-pure-api-key")
        assert acc.mode == "apiKey"
        assert acc.has_apikey_fallback() is False
        acc.risk_penalty(base=900.0, cap=86400.0, ban_strikes=4, decay_seconds=7 * 86400)
        assert acc.status == Status.COOLING
        assert acc.risk_strikes == 1
        assert acc.enabled is True
        assert acc.is_selectable() is False
        assert acc.cooling_until is not None

    def test_jwt_with_key_cooldown_not_selectable(self):
        acc = Account.create("zai", "t", "a.b.c")
        acc.api_key = "sk-fallback"
        acc.risk_penalty(base=900.0, cap=86400.0, ban_strikes=4, decay_seconds=7 * 86400)
        assert acc.has_apikey_fallback() is True
        # 冷却期整号不可选（若可选，网关会重打 Plan 通道，风控计数持续累加）；
        # 同请求内的 Key 回退由 force_fallback 保证，不依赖 is_selectable。
        assert acc.is_selectable() is False
        assert acc.allows_billing() is False

    def test_risk_cooldown_escalates_to_disabled_at_4th_strike(self):
        acc = Account.create("zai", "t", "jwt.token")
        for strike in range(1, 5):
            acc.risk_penalty(base=900.0, cap=86400.0, ban_strikes=4, decay_seconds=7 * 86400)
            assert acc.risk_strikes == strike
        assert acc.status == Status.DISABLED
        assert acc.is_selectable() is False

    def test_risk_strikes_accumulate_within_decay_window(self):
        import time as _time
        acc = Account.create("zai", "t", "jwt.token")
        acc.risk_penalty(base=900.0, cap=86400.0, ban_strikes=4, decay_seconds=7 * 86400)
        acc.last_risk_at = _time.time() - 60  # 窗口内（1 分钟前）
        acc.risk_penalty(base=900.0, cap=86400.0, ban_strikes=4, decay_seconds=7 * 86400)
        assert acc.risk_strikes == 2  # 窗口内累计

    def test_risk_strikes_decay_after_window(self):
        import time as _time
        acc = Account.create("zai", "t", "jwt.token")
        acc.risk_penalty(base=900.0, cap=86400.0, ban_strikes=4, decay_seconds=7 * 86400)
        acc.risk_penalty(base=900.0, cap=86400.0, ban_strikes=4, decay_seconds=7 * 86400)
        assert acc.risk_strikes == 2
        acc.last_risk_at = _time.time() - 8 * 86400  # 超出 7 天衰减窗口
        acc.risk_penalty(base=900.0, cap=86400.0, ban_strikes=4, decay_seconds=7 * 86400)
        assert acc.risk_strikes == 1  # 跨窗口视为新事件，重新起算
        assert acc.status == Status.COOLING  # 一次命中不升级禁用

    def test_pure_apikey_invalid_not_selectable(self):
        acc = Account.create("zai", "t", "sk-dead-key")
        acc.status = Status.INVALID
        assert acc.is_selectable() is False


class TestCountSelectable:
    """`store.count_selectable` —— 模型兜底判断「全池问完了没」的独立口径。

    它必须和 `store.select` 的可选条件完全一致（否则兜底会在「还有一个账号没问」
    的情况下误判死），但**不推进轮询游标**（否则每次请求都会把 round-robin 带偏）。
    """

    def test_counts_only_selectable_accounts(self, fresh_app):
        """EXHAUSTED / 手动停用 / 冷却中 / 纯 Key 失效都不计入。"""
        active = fresh_app.add_account("zai", "c-ok", "a.b.ok")
        fresh_app.add_account("zai", "c-exhausted", "a.b.exhausted").status = Status.EXHAUSTED
        fresh_app.add_account("zai", "c-disabled", "a.b.disabled").enabled = False
        cooling = fresh_app.add_account("zai", "c-cooling", "a.b.cooling")
        cooling.status = Status.COOLING
        cooling.cooling_until = time.time() + 300
        fresh_app.add_account("zai", "c-invalid", "a.b.invalid").status = Status.INVALID

        assert active.is_selectable() is True
        assert fresh_app.count_selectable("zai") == 1

    def test_invalid_with_apikey_still_counted(self, fresh_app):
        """JWT 失效但配了 API Key 的账号仍可选中（走回退通道），必须计入。"""
        acc = fresh_app.add_account("zai", "c-key", "a.b.key")
        acc.api_key = "sk-fallback"
        acc.status = Status.INVALID
        assert fresh_app.count_selectable("zai") == 1

    def test_cooling_expired_is_counted(self, fresh_app):
        """冷却到期即恢复可选（否则兜底会一直以为池子小一号）。"""
        acc = fresh_app.add_account("zai", "c-cooled", "a.b.cooled")
        acc.status = Status.COOLING
        acc.cooling_until = 1.0        # 早已过期
        assert fresh_app.count_selectable("zai") == 1

    def test_does_not_advance_rotation(self, fresh_app):
        """只数不选：反复调用不得推进轮询游标（否则每个请求的选号顺序都会漂）。"""
        for index in range(3):
            fresh_app.add_account("zai", f"c-rot-{index}", f"a.b.rot{index}")
        before = fresh_app._rotation.get("zai", 0)
        for _ in range(5):
            assert fresh_app.count_selectable("zai") == 3
        assert fresh_app._rotation.get("zai", 0) == before

    def test_provider_scoped(self, fresh_app):
        """按 provider 分别计数（bigmodel 空池不能借用 zai 的数字）。"""
        fresh_app.add_account("zai", "c-zai", "a.b.zai")
        assert fresh_app.count_selectable("zai") == 1
        assert fresh_app.count_selectable("bigmodel") == 0
