"""ZCode 额度 / 余额 / 用量查询，以及账号状态判定。

在查询基础上提供「额度用完自动标记 exhausted」的监控能力。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime

import httpx

from . import constants, logs, settings
from .models import Account, Status
from .store import store

_DEVICE_MID: str | None = None


def device_mid() -> str:
    """本机设备 ID（ZCode 客户端 telemetry 同款语义）。

    billing 全家桶（current/balance/usage/preview/claim）必需 X-Device-Mid，
    缺失时上游返回 code=3001 parameter error。首次生成后持久化到 data 目录。
    """
    global _DEVICE_MID
    if _DEVICE_MID:
        return _DEVICE_MID
    path = settings.DATA_DIR / "device_mid"
    try:
        _DEVICE_MID = path.read_text().strip()
        if _DEVICE_MID:
            return _DEVICE_MID
    except OSError:
        pass
    _DEVICE_MID = str(uuid.uuid4())
    try:
        os.makedirs(settings.DATA_DIR, exist_ok=True)
        with open(path, "x") as f:
            f.write(_DEVICE_MID)
    except OSError as err:  # noqa: BLE001 - 生成失败不阻断查询，仅进程内复用
        logs.warn("quota", f"device_mid 持久化失败（仅进程内生效）: {err}")
    return _DEVICE_MID


def _auth_headers(account: Account) -> dict:
    """billing 族请求头（对齐 zcode-switch zai_billing_headers 实证形态）。

    UA/版本走 BILLING_APP_VERSION（官方桌面端现行版，与 messages 指纹刻意
    分离）；平台/os/语言/时区/设备 ID 按账号指纹档案出值（每账号独立设备，
    见 fingerprint.py）；每请求全新 x-request-id。
    """
    from .fingerprint import profile_for

    profile = profile_for(account)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"ZCode/{constants.BILLING_APP_VERSION}",
        "HTTP-Referer": constants.ZCODE_ORIGIN,
        "X-Title": constants.BILLING_TITLE,
        "X-ZCode-App-Version": constants.BILLING_APP_VERSION,
        "X-Platform": profile.platform_full,
        "X-Release-Channel": constants.BILLING_RELEASE_CHANNEL,
        "X-Client-Language": profile.language,
        "X-Client-Timezone": profile.timezone,
        "X-Os-Category": profile.os_category,
        "X-Os-Version": profile.os_version,
        "X-Device-Mid": profile.device_mid,
        "x-request-id": str(uuid.uuid4()),
    }
    if account.mode == "jwt" and account.jwt_token:
        headers["Authorization"] = f"Bearer {account.jwt_token}"
    elif account.api_key:
        headers["x-api-key"] = account.api_key
    return headers


def _expires_ts(raw) -> float:
    """`expires_at` → epoch 秒；无法解析返回 0（视作「不过期」，保守保留）。

    上游实测两种形态：`billing/balance` 的窗口是 unix 秒；部分字段（以及测试桩）
    是 ISO 字符串。毫秒时间戳（> 1e12）顺手归一。
    """
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts <= 0:
            return 0.0
        return ts / 1000.0 if ts > 1e12 else ts
    if isinstance(raw, str) and raw.strip():
        text = raw.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _window_expired(expires_at, now: float) -> bool:
    """窗口是否已到期（到期时刻 <= now）。

    上游 `billing/balance` 在日切（本地 09:00）之后仍会把上一窗口列出来、且
    `remaining` 还是旧数字——那部分额度已经不可用，展示与统计都必须忽略，
    否则后台会长期显示「已经消失的额度」。
    """
    ts = _expires_ts(expires_at)
    return bool(ts) and ts <= now


def _bonus_active(plan: dict, now: float) -> bool:
    """plan 是否带有已生效的一次性赠送授权（balance 不含这类额度）。

    用于额度耗尽判定：日窗口用完但赠送池有效时，账号仍有真实可用额度。
    """
    for e in (plan or {}).get("entitlements") or []:
        if e.get("period") != "one_time":
            continue
        eff = e.get("effective_at") or 0
        ends = e.get("ends_at") or e.get("expires_at") or 0
        if eff and eff <= now and (not ends or now <= ends):
            return True
    return False


async def fetch_quota(account: Account) -> dict:
    """拉取单个账号的 方案 / 余额 / 用量，写回账号状态并持久化。

    返回结构: {"billing":..., "balance":..., "usage":..., "error":...}
    """
    live = store.find(account.provider, account.id)
    if live is None:
        return {"error": "账号已删除"}
    if live is not account:
        account = live
    if not account.allows_billing() and not account.is_cooling():
        # 与 admin/monitor 同一扇门。冷却账号仍允许查询（测试与兜底约定：
        # 拿额度不得提前解除冷却）；废 JWT / 风控 / 手动停用一律不打 billing。
        from .claim import AUTH_EXPIRED_MESSAGE, billing_block_reason

        return {
            "error": account.last_error
            or billing_block_reason(account, action="上游查询")
            or AUTH_EXPIRED_MESSAGE
        }
    headers = _auth_headers(account)
    base = settings.ZCODE_BILLING_BASE
    result: dict = {}

    async with httpx.AsyncClient(timeout=20) as client:
        async def _get(path: str):
            try:
                return await client.get(f"{base}{path}", headers=headers)
            except httpx.HTTPError:
                return None

        billing_res, balance_res, usage_res = await asyncio.gather(
            _get("/billing/current"),
            _get("/billing/balance"),
            _get("/usage"),
        )

    live = store.find(account.provider, account.id)
    if live is None:
        return {"error": "账号已删除"}
    if live is not account:
        account = live

    now = time.time()
    account.last_checked_at = now

    # 鉴权失败 → 标记 invalid
    if billing_res is not None and billing_res.status_code in (401, 403):
        body = (billing_res.text or "").lower()
        if "captcha" not in body and "verify" not in body:
            from .claim import AUTH_EXPIRED_MESSAGE

            account.status = Status.INVALID
            account.last_error = AUTH_EXPIRED_MESSAGE
            store.update_account(account)
            return {"error": account.last_error}

    if billing_res is not None and billing_res.status_code == 200:
        try:
            data = billing_res.json()
            result["billing"] = data
            plans = (data.get("data") or {}).get("plans") or []
            # 全量保留（多套餐时 entitlements 不丢）；plan 兼容保留首个
            account.plans = plans
            account.plan = plans[0] if plans else {}
        except (ValueError, KeyError):
            pass

    quota_map: dict = {}
    balance_ok = False
    expired_windows = 0
    if balance_res is not None and balance_res.status_code == 200:
        try:
            data = balance_res.json()
            result["balance"] = data
            balance_ok = True
            for bal in (data.get("data") or {}).get("balances") or []:
                name = bal.get("show_name") or bal.get("model") or "model"
                window = {
                    "total": bal.get("total_units"),
                    "used": bal.get("used_units"),
                    "remaining": bal.get("remaining_units"),
                    "expires_at": bal.get("expires_at"),
                }
                if _window_expired(window.get("expires_at"), now):
                    # 日切后上游 balance 仍会列出上一窗口且 remaining 是旧数字：
                    # 额度已不可用，展示 / 统计 / 耗尽判定一律忽略（不落库）
                    expired_windows += 1
                    continue
                prev = quota_map.get(name)
                if prev:
                    # 防御：同模型多窗口（如日窗 + 一次性）合并，避免后者覆盖前者
                    for k in ("total", "used", "remaining"):
                        prev[k] = (prev.get(k) or 0) + (window.get(k) or 0)
                    prev["expires_at"] = max(prev.get("expires_at") or 0, window.get("expires_at") or 0)
                    logs.warn("quota", f"balance 同名窗口 {name} 已合并")
                else:
                    quota_map[name] = window
            if expired_windows:
                result["expired_windows"] = expired_windows
        except (ValueError, KeyError):
            balance_ok = False

    if usage_res is not None and usage_res.status_code == 200:
        try:
            account.usage = usage_res.json().get("data") or {}
            result["usage"] = account.usage
        except (ValueError, KeyError):
            pass

    if balance_ok:
        # 以本次 balance 为准落库（哪怕过滤后为空）：清掉上一轮残留的过期窗口，
        # 否则后台会一直显示「已经消失的额度」，耗尽判定也会被旧数字带偏
        account.quota = quota_map

    if quota_map:
        # 额度耗尽判定：所有日窗口剩余 <= 0。注意 balance 不含一次性赠送池——
        # 赠送池有效时不判耗尽，否则账号会被路由跳过而实际仍有 3 亿级可用额度
        now = time.time()
        remainings = [
            q.get("remaining") for q in quota_map.values() if q.get("remaining") is not None
        ]
        daily_exhausted = bool(remainings) and all((r or 0) <= 0 for r in remainings)
        has_bonus = _bonus_active(account.plan, now) or any(
            _bonus_active(p, now) for p in account.plans
        )
        has_daily = bool(remainings) and any((r or 0) > 0 for r in remainings)
        if daily_exhausted and not has_bonus:
            account.status = Status.EXHAUSTED
            account.last_error = "额度已用完"
        elif account.status in (Status.EXHAUSTED, Status.COOLING) and (
            has_daily or has_bonus
        ):
            # 额度恢复（窗口重置 / 赠送池生效）→ 重新激活。冷却期内不提前解除；
            # 废 JWT / 风控禁用绝不能因额度数字复活 Plan 通道。
            if not account.is_cooling():
                account.status = Status.ACTIVE
                account.last_error = None
                account.cooling_until = None

    store.update_account(account)
    return result or {"error": "无法获取额度数据"}


async def refresh_accounts(accounts: list[Account]) -> dict:
    """并发刷新一批账号，返回汇总。"""
    if not accounts:
        return {"ok": 0, "fail": 0}
    sem = asyncio.Semaphore(8)

    async def _one(acc: Account) -> bool:
        async with sem:
            res = await fetch_quota(acc)
            return "error" not in res

    results = await asyncio.gather(*[_one(a) for a in accounts], return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    return {"ok": ok, "fail": len(accounts) - ok}


class QuotaMonitor:
    """后台周期性刷新可管理账号的额度，实现实时用量监控。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        # 启动后先等几秒，避免与服务启动争抢
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5)
            return
        except TimeoutError:
            pass

        while not self._stop.is_set():
            interval = store.quota_refresh_interval()  # 实时读取设置，改后即生效
            if interval > 0:
                try:
                    accounts = [
                        a for a in store.list_accounts("zai")
                        if a.mode == "jwt" and a.allows_billing()
                    ]
                    if accounts:
                        await refresh_accounts(accounts)
                except Exception as err:  # noqa: BLE001 - 后台任务需吞掉异常继续运行
                    logs.err("quota", f"后台刷新出错: {err}")
            # interval<=0 视为关闭：仍周期性回看设置，便于随时启用
            wait = interval if interval > 0 else 30
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


monitor = QuotaMonitor()

