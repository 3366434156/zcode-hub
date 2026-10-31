"""模型兜底池 —— 「这个模型全池都没了，就换下一个模型」。

## 为什么需要

额度是**按账号 × 模型**计的（实测同一账号 GLM-5.3 还有 300 万、GLM-5.3-Flash
已用完）。原网关只会换号：4 个账号的 GLM-5.3 都用完时，客户端收到 503，
哪怕池子里另一个模型（如刚重置的 GLM-5.3-Flash）完全可用。

本模块给「模型」这一维补上轮询：请求模型在全池都不可用时，按配置顺序换到
下一个模型，客户端无感（响应头 `x-zcode-model` 如实回报实际服务的模型）。

## 两类证据

1. **反应式**：一次调度里每个账号都被上游判成「这个模型不可用」
   （1005 exceed quota limit / 3006 model not allowed）→ `mark_dead`，
   在 TTL 内直接跳过该模型，不再拿 N 个账号去撞同一堵墙。
2. **主动式**：账号额度快照（`account.quota`，quota.py 落库）里该模型所有
   账号 `remaining <= 0` → 判 dead，连上游请求都不用发。

成功一次就 `revive`（记一条「实测可用」的信任票），因此额度快照陈旧、
窗口刚重置、新领了套餐这几种情况都能自愈——不需要人工干预。

## 与「换号」的分工

| 上游信号 | 含义 | 动作 |
| --- | --- | --- |
| 1005 / 3006 | 这个账号的这个模型不可用 | 换号；全池都如此 → **换模型** |
| 3009 | 这个模型的并发租约被占 | 短等重试整轮（不换模型、不换号） |
| 402 / insufficient balance | 账号整体没额度了 | 换号（换模型也没用） |
| 3012 / 401 / 403 / 5xx | 账号或通道问题 | 换号 / 冷却 |
"""

from __future__ import annotations

import threading
import time

from . import constants

# 模型状态（后台展示与 candidates 排序共用）
STATE_QUOTA_OK = "quota_ok"   # 有额度快照证明还有剩余，或刚成功过一次
STATE_UNKNOWN = "unknown"     # 无证据（模型不在余额表里 / 从没刷新过额度）
STATE_DEAD = "dead"           # 全池不可用（反应式标记或额度证明）

# 状态 → 排序权重（小的先用）：有余额证明的优先，其次未知
_PRIORITY = {STATE_QUOTA_OK: 0, STATE_UNKNOWN: 1, STATE_DEAD: 2}


class ModelPool:
    """线程安全的模型可用性台账 + 兜底顺序计算。"""

    def __init__(self, accounts_getter=None) -> None:
        self._lock = threading.RLock()
        # (provider, model_lower) → 到期时间戳
        self._dead: dict[tuple[str, str], float] = {}
        self._dead_reason: dict[tuple[str, str], str] = {}
        # 实测可用的信任票：额度快照说 0 但请求真的成功了，别把好模型锁死
        self._alive: dict[tuple[str, str], float] = {}
        self._accounts_getter = accounts_getter

    # ── 依赖注入（测试用；生产走 store 单例）─────────────────────────────────
    def _accounts(self, provider: str) -> list:
        if self._accounts_getter is not None:
            return list(self._accounts_getter(provider))
        from .store import store

        return store.list_accounts(provider)

    def _setting_enabled(self) -> bool:
        from .store import store

        return store.model_fallback_enabled()

    def _setting_chain(self, provider: str) -> list[str]:
        from .store import store

        return store.model_fallback_chain(provider)

    def _setting_ttl(self) -> int:
        from .store import store

        return store.model_fallback_ttl()

    # ── 配置 ─────────────────────────────────────────────────────────────────
    def enabled(self) -> bool:
        return self._setting_enabled()

    def chain(self, provider: str) -> list[str]:
        """兜底顺序（已归一化上游大小写、去重、去掉空项）。"""
        seen: set[str] = set()
        out: list[str] = []
        for raw in self._setting_chain(provider):
            model = constants.MODEL_NAME_MAP.get(raw.lower(), raw)
            if not model or model.lower() in seen:
                continue
            seen.add(model.lower())
            out.append(model)
        return out

    # ── 标记 ─────────────────────────────────────────────────────────────────
    def mark_dead(self, provider: str, model: str, reason: str = "",
                  ttl: float | None = None) -> None:
        """记「这个模型全池不可用」，TTL 内跳过它。"""
        if not model:
            return
        ttl = self._setting_ttl() if ttl is None else ttl
        if ttl <= 0:
            return
        key = (provider, model.lower())
        with self._lock:
            self._dead[key] = time.time() + ttl
            self._dead_reason[key] = str(reason or "全池不可用")[:200]
            self._alive.pop(key, None)

    def revive(self, provider: str, model: str, ttl: float | None = None) -> None:
        """请求成功 → 这个模型现在确实可用（清反应式标记，并记信任票）。"""
        if not model:
            return
        ttl = self._setting_ttl() if ttl is None else ttl
        key = (provider, model.lower())
        with self._lock:
            self._dead.pop(key, None)
            self._dead_reason.pop(key, None)
            if ttl > 0:
                self._alive[key] = time.time() + ttl

    def clear(self, provider: str | None = None) -> None:
        with self._lock:
            if provider is None:
                self._dead.clear()
                self._dead_reason.clear()
                self._alive.clear()
                return
            for key in [k for k in self._dead if k[0] == provider]:
                self._dead.pop(key, None)
                self._dead_reason.pop(key, None)
            for key in [k for k in self._alive if k[0] == provider]:
                self._alive.pop(key, None)

    def reason(self, provider: str, model: str) -> str:
        with self._lock:
            return self._dead_reason.get((provider, (model or "").lower()), "")

    def describe(self, provider: str, model: str) -> str:
        """人类可读的「为什么这个模型不可用」（日志与后台卡片共用）。

        反应式标记带的是上游原话（`GLM-5.3 额度用尽（上游 code 1005）`）；主动式
        判死没有上游信号，就给一句额度快照的解释，否则后台只看到一个红点。
        """
        text = self.reason(provider, model)
        if text:
            return text
        if self.state(provider, model) == STATE_DEAD:
            return "额度快照显示该模型全池剩余为 0"
        return ""

    # ── 状态 ─────────────────────────────────────────────────────────────────
    def _quota_evidence(self, provider: str, model: str) -> tuple[str, int, int, int | None]:
        """额度快照证据 → (状态, 有余额账号数, 有该模型快照的账号数, 剩余合计)。"""
        accounts = [a for a in self._accounts(provider) if getattr(a, "enabled", True)]
        if not accounts:
            return STATE_UNKNOWN, 0, 0, None
        want = (model or "").lower()
        alive = 0
        seen = 0
        remaining = 0
        for account in accounts:
            entry = None
            for name, window in (getattr(account, "quota", None) or {}).items():
                if str(name).lower() == want:
                    entry = window
                    break
            if not isinstance(entry, dict):
                continue
            left = entry.get("remaining")
            if left is None:
                # 窗口没有剩余字段 = 无法证明耗尽，不能拿它当「已用完」的证据
                continue
            try:
                left = int(left)
            except (TypeError, ValueError):
                continue
            seen += 1
            remaining += left
            if left > 0:
                alive += 1
        if alive:
            return STATE_QUOTA_OK, alive, seen, remaining
        if seen:
            return STATE_DEAD, 0, seen, remaining
        return STATE_UNKNOWN, 0, 0, None

    def state(self, provider: str, model: str) -> str:
        """模型当前可用性：quota_ok / unknown / dead。"""
        if not model:
            return STATE_UNKNOWN
        key = (provider, model.lower())
        now = time.time()
        with self._lock:
            alive_until = self._alive.get(key)
            if alive_until and alive_until > now:
                return STATE_QUOTA_OK
            dead_until = self._dead.get(key)
            if dead_until and dead_until > now:
                return STATE_DEAD
        state, _, _, _ = self._quota_evidence(provider, model)
        return state

    def remaining(self, provider: str, model: str) -> int | None:
        _, _, seen, remaining = self._quota_evidence(provider, model)
        return remaining if seen else None

    # ── 兜底顺序 ─────────────────────────────────────────────────────────────
    def candidates(self, provider: str, requested: str) -> list[str]:
        """本次请求要依次尝试的模型列表（第一个永远是客户端点的那个）。

        规则：
        - 请求模型排第一（客户端明确要的，先照办）；
        - 其后按配置链顺序，去掉 dead 的；
        - 整体排序：`quota_ok` 在前、`unknown` 在后（稳定排序，不打乱配置顺序）；
        - 一个活的都没有 → 只留请求模型（让上游给出诚实的错误，而不是瞎换）。
        """
        requested = requested or ""
        if not requested or not self.enabled():
            return [requested] if requested else []
        chain = self.chain(provider)
        if not chain:
            return [requested]
        order: list[str] = [requested]
        seen = {requested.lower()}
        for model in chain:
            if model.lower() in seen:
                continue
            seen.add(model.lower())
            order.append(model)

        scored: list[tuple[int, str, str]] = []
        for idx, model in enumerate(order):
            scored.append((idx, model, self.state(provider, model)))
        alive = [item for item in scored if item[2] != STATE_DEAD]
        if not alive:
            return [requested]
        head = [item for item in alive if item[0] == 0]
        rest = sorted(
            (item for item in alive if item[0] != 0),
            key=lambda item: (_PRIORITY.get(item[2], 1), item[0]),
        )
        return [model for _, model, _ in head + rest]

    # ── 后台展示 ─────────────────────────────────────────────────────────────
    def snapshot(self, provider: str) -> list[dict]:
        """兜底链的实时视图（后台「模型兜底」卡片）。"""
        now = time.time()
        models: list[str] = []
        seen: set[str] = set()
        for model in self.chain(provider):
            if model.lower() not in seen:
                seen.add(model.lower())
                models.append(model)
        for account in self._accounts(provider):
            for name in (getattr(account, "quota", None) or {}):
                name = str(name)
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    models.append(name)

        rows: list[dict] = []
        for model in models:
            key = (provider, model.lower())
            with self._lock:
                dead_until = self._dead.get(key)
                reason = self._dead_reason.get(key, "")
                alive_until = self._alive.get(key)
            if dead_until and dead_until > now:
                state = STATE_DEAD
            elif alive_until and alive_until > now:
                state = STATE_QUOTA_OK
                reason = ""
            else:
                state, _, _, _ = self._quota_evidence(provider, model)
                if state != STATE_DEAD:
                    reason = ""
            if state == STATE_DEAD and not reason:
                reason = "额度快照显示该模型全池剩余为 0"
            ok_accounts, total_accounts = self._account_counts(provider, model)
            rows.append({
                "model": model,
                "state": state,
                "reason": reason,
                "dead_until": round(dead_until, 3) if (dead_until and dead_until > now) else None,
                "alive_accounts": ok_accounts,
                "accounts_total": total_accounts,
                "remaining": self.remaining(provider, model),
                "in_chain": model.lower() in {m.lower() for m in self.chain(provider)},
            })
        rows.sort(key=lambda row: (_PRIORITY.get(row["state"], 1),
                                   0 if row["in_chain"] else 1, row["model"]))
        return rows

    def _account_counts(self, provider: str, model: str) -> tuple[int, int]:
        accounts = [a for a in self._accounts(provider) if getattr(a, "enabled", True)]
        want = (model or "").lower()
        ok = 0
        for account in accounts:
            for name, window in (getattr(account, "quota", None) or {}).items():
                if str(name).lower() != want or not isinstance(window, dict):
                    continue
                left = window.get("remaining")
                if left is None or (isinstance(left, (int, float)) and left > 0):
                    ok += 1
                break
        return ok, len(accounts)


# 单例
modelpool = ModelPool()
