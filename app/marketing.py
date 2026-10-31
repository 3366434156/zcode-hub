"""上游运营位（marketing/touch）活动套餐发现。

2026-10-30 实证：限时套餐的投放通道从 `billing/preview` 换到了运营位——
官方客户端拉 `GET /api/v1/marketing/touch`，拿到的 deliveries 里 banner 按钮
action 形如::

    {"type": "claim_zcode_plan",
     "args": {"plan_id": "zcode-v3-start-plan-1030-wk"}}

套餐元数据（name/priority/starts_at/ends_at/entitlements）在同层
`args.zcode_plan`。**领取端点不变**：仍是 `POST billing/claim {plan_id}`
（asar marketingTouch → codingPlanSubscriptionService.claimManualPlan）。

同一时刻同账号 `billing/preview` 返回 `{"code":0}`（连 data 字段都没有），
后台的旧行为就只剩「领取失败：没有待领取的套餐」——这是本模块存在的理由：
**可领套餐 = billing/preview 投放 ∪ 运营位投放**（claim.preview_plans 合并）。

只读模块：不发 claim、不改账号状态；失败一律转 MarketingError 由调用方决定
是否阻断（claim.py 里运营位失败不阻断 billing 通道）。
"""

from __future__ import annotations

import httpx

from . import constants, logs, settings
from .models import Account

_TIMEOUT = 15


class MarketingError(Exception):
    """运营位查询失败（网络 / HTTP / JSON / 业务码）。"""


def _plan_grants(plan: dict) -> list[dict]:
    """entitlements → grants（只保留 model_usage + token，与 claim.parse_plan 同口径）。"""
    grants: list[dict] = []
    for ent in plan.get("entitlements") or []:
        if not isinstance(ent, dict):
            continue
        if ent.get("meter") != "model_usage" or ent.get("unit_type") != "token":
            continue
        name = str(ent.get("show_name") or ent.get("showName") or "").strip()
        if not name:
            continue
        units = ent.get("grant_units", ent.get("grantUnits")) or 0
        grants.append({
            "name": name,
            "units": float(units),
            "period": ent.get("period") or "one_time",
        })
    return grants


def _walk(node, claim_ids: set[str], metas: dict[str, dict], depth: int = 0) -> None:
    """递归遍历运营位载荷，收集 claim 动作的 plan_id 与同名套餐元数据。

    运营位结构由服务端下发、层级不稳定（banner / hero / popup / 新增位），
    因此按「形状」而非固定路径抓：任何 action.type == claim_zcode_plan 的
    args.plan_id 都算可领；任何带 plan_id + (name|entitlements) 的对象都当
    元数据候选。depth 上限防止异常载荷把递归打爆。
    """
    if depth > 12:
        return
    if isinstance(node, dict):
        action = node.get("action")
        if isinstance(action, dict) and action.get("type") == constants.MARKETING_CLAIM_ACTION:
            args = action.get("args")
            if isinstance(args, dict):
                plan_id = str(args.get("plan_id") or "").strip()
                if plan_id:
                    claim_ids.add(plan_id)
                nested = args.get("zcode_plan")
                if isinstance(nested, dict):
                    _walk(nested, claim_ids, metas, depth + 1)
        pid = str(node.get("plan_id") or node.get("planId") or "").strip()
        if pid and ("entitlements" in node or "name" in node):
            metas.setdefault(pid, node)
        for value in node.values():
            _walk(value, claim_ids, metas, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk(item, claim_ids, metas, depth + 1)


def extract_claim_plans(payload: dict) -> list[dict]:
    """运营位载荷 → 可领套餐列表（与 claim.parse_plan 同形的 dict）。

    只认「被 claim 动作点名」的套餐：运营位里也可能出现纯展示的商品/续费卡，
    照单全收会把不该领的东西塞进领取轮。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    claim_ids: set[str] = set()
    metas: dict[str, dict] = {}
    _walk(data, claim_ids, metas)
    plans: list[dict] = []
    for plan_id in sorted(claim_ids):
        meta = metas.get(plan_id) or {}
        plans.append({
            "plan_id": plan_id,
            "name": str(meta.get("name") or "").strip(),
            "description": str(meta.get("description") or "").strip(),
            "priority": meta.get("priority") or 0,
            "grants": _plan_grants(meta),
        })
    return plans


def _headers(account: Account) -> dict:
    from .fingerprint import profile_for

    profile = profile_for(account)
    return {
        "X-Device-Mid": profile.device_mid,
        "X-Client-Language": "zh-CN",
        "X-ZCode-App-Version": constants.BILLING_APP_VERSION,
        "User-Agent": f"ZCode/{constants.BILLING_APP_VERSION}",
        "Authorization": f"Bearer {account.jwt_token}",
    }


async def fetch_claim_plans(account: Account) -> list[dict]:
    """拉一次运营位，返回该账号当下可领的活动套餐（按优先级降序）。

    与 billing 族同样直连（trust_env=False）：运营位判定与 billing 同源，
    走代理出口会被判 unusual activity。任何失败抛 MarketingError。
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, trust_env=False) as client:
            res = await client.get(
                settings.ZCODE_MARKETING_URL,
                headers=_headers(account),
                params={"seq": "0"},
                follow_redirects=False,
            )
    except httpx.HTTPError as err:
        raise MarketingError(f"运营位查询网络错误: {err}") from err
    if res.status_code >= 400:
        raise MarketingError(f"运营位查询 HTTP {res.status_code}")
    try:
        payload = res.json()
    except ValueError:
        raise MarketingError("运营位响应非 JSON") from None
    code = payload.get("code") if isinstance(payload, dict) else None
    if code not in (0, "0", None):
        raise MarketingError(f"运营位业务码异常（{code}）: {payload.get('msg') or ''}".strip())
    plans = extract_claim_plans(payload)
    if plans:
        logs.info("claim", f"运营位投放可领套餐 {len(plans)} 个: "
                           + ", ".join(p["plan_id"] for p in plans))
    plans.sort(key=lambda p: (-p["priority"], p["plan_id"]))
    return plans
