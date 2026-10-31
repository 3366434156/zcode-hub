"""后台管理 API：/admin/api/*（账号池、设置、用量监控）。"""

from __future__ import annotations

import asyncio
import secrets
import time

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .. import constants, logs, reqlog, settings
from ..auth_admin import verify_admin_key
from ..captcha import CaptchaSolveError
from ..claim import (
    AUTH_EXPIRED_MESSAGE,
    ClaimError,
    auto_claim_all_plans,
    billing_block_reason,
    claim_with_captcha,
    preview_plans,
    report_activation_events,
)
from ..claim import claim as do_claim
from ..modelpool import modelpool
from ..models import PROVIDERS, Status
from ..oauth import ZaiAuthFlow
from ..quota import fetch_quota, refresh_accounts
from ..store import store
from ..usagelog import usagelog
from ..version_sync import version_sync

router = APIRouter(prefix="/admin/api", dependencies=[Depends(verify_admin_key)])


# ── 鉴权探针 ─────────────────────────────────────────────────────────────────
@router.get("/verify")
async def verify():
    return {"status": "ok"}


# ── 账号列表 + 概览统计 ──────────────────────────────────────────────────────
@router.get("/accounts")
async def list_accounts():
    now = time.time()
    accounts = [a.public_view() for a in store.list_accounts()]
    stats = {"total": len(accounts), "active": 0, "exhausted": 0,
             "cooling": 0, "invalid": 0, "disabled": 0,
             "calls": 0, "fail": 0}
    for a in accounts:
        st = a["status"]
        if st in stats:
            stats[st] += 1
        stats["calls"] += a["use_count"]
        stats["fail"] += a["fail_count"]
    return {"accounts": accounts, "stats": stats, "providers": list(PROVIDERS), "ts": now}


@router.get("/status")
async def status_info():
    return {
        "providers": list(PROVIDERS),
        "gateway_key_set": bool(store.gateway_key()),
        "quota_pool": {
            p: sum(1 for a in store.list_accounts(p) if a.is_selectable())
            for p in PROVIDERS
        },
        # 客户端版本口径：钉版（实证可用的请求形状）/ 官方最新（巡检）/ 是否自动跟进
        "client_version": {
            "effective": constants.CLIENT_APP_VERSION,
            "latest": version_sync.latest,
            "follow": settings.APP_VERSION_FOLLOW,
        },
    }


# ── 新增账号 ─────────────────────────────────────────────────────────────────
@router.post("/accounts")
async def add_accounts(payload: dict = Body(...)):
    provider = payload.get("provider", "zai")
    if provider not in PROVIDERS:
        raise HTTPException(400, "不支持的 provider")
    tokens = payload.get("tokens") or []
    if isinstance(tokens, str):
        tokens = [t.strip() for t in tokens.splitlines() if t.strip()]
    tokens = [t.strip() for t in tokens if t and t.strip()]
    if not tokens:
        raise HTTPException(400, "请输入至少一个 Token / API Key")

    added = []
    existing = {a.id for a in store.list_accounts(provider)}  # 识别真新增（重复 token 跳过）
    for tok in dict.fromkeys(tokens):  # 去重保序
        name = payload.get("name") or f"{provider}-{len(store.list_accounts(provider)) + 1}"
        acc = store.add_account(provider, name, tok)
        added.append(acc.id)
    # 立即刷新一次额度（仅 zai jwt）
    fresh = [a for a in store.list_accounts(provider) if a.id in added and a.mode == "jwt"]
    if fresh:
        await refresh_accounts(fresh)
    # 真新增（id 不在加号前的集合里；重复 token 返回旧账号自动跳过）
    new_accounts = [a for a in store.list_accounts(provider)
                    if a.id in added and a.id not in existing]
    for acc in new_accounts:
        _schedule_install(acc)  # 按账号安装序（含 apiKey 账号，幂等）
        if acc.mode == "jwt":
            _schedule_auto_claim(acc)  # 授权完成即激活+自动领取，入池即吃满活动
    return {"count": len(added), "ids": added}


# ── 删除账号 ─────────────────────────────────────────────────────────────────
@router.delete("/accounts")
async def delete_accounts(ids: list[str] = Body(...)):
    deleted = 0
    for aid in ids:
        acc = store.find_any(aid)
        if acc and store.remove_account(acc.provider, aid):
            deleted += 1
    return {"deleted": deleted}


# ── 编辑账号 ─────────────────────────────────────────────────────────────────
@router.put("/accounts/{account_id}")
async def edit_account(account_id: str, payload: dict = Body(...)):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if "name" in payload and payload["name"]:
        acc.name = payload["name"].strip()
    secret = payload.get("token") or payload.get("secret")
    if secret:
        secret = secret.strip()
        is_jwt = secret.count(".") == 2 and acc.provider == "zai"
        if is_jwt:
            acc.mode = "jwt"
            acc.jwt_token = secret
            # 换 JWT 不清已有 API Key：OAuth 兑换出的回退通道要保住
        else:
            acc.mode = "apiKey"
            acc.api_key = secret
            acc.jwt_token = None
        acc.status = Status.ACTIVE
        acc.last_error = None
    store.update_account(acc)
    return {"ok": True}


# ── 账号排序（人工顺序 = 网关轮询次序）───────────────────────────────────────
@router.post("/accounts/reorder")
async def reorder_accounts(payload: dict = Body(...)):
    """按前端拖拽后的 id 顺序重排账号。

    `{ids: [...]}`：ids 顺序即新顺序，第一行 = 网关优先使用的账号。只认池内
    存在的 id；未列出的账号保持原相对顺序排到末尾（前端过滤/分页漏传不会
    静默丢账号）。顺序落库（sort_order），重启后依旧生效。
    """
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "缺少 ids")
    ids = [str(i).strip() for i in ids if str(i or "").strip()]
    if not any(store.find_any(i) for i in ids):
        raise HTTPException(404, "没有匹配的账号")
    applied = store.reorder_accounts(ids)
    first = store.find_any(applied[0]) if applied else None
    logs.info("store", f"账号顺序已更新（{len(applied)} 个，第一位 {first.name if first else '-'}）")
    return {"ok": True, "ids": applied}


# ── 启用 / 禁用 ──────────────────────────────────────────────────────────────
@router.post("/accounts/{account_id}/enabled")
async def set_enabled(account_id: str, payload: dict = Body(...)):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    enabled = bool(payload.get("enabled", True))
    store.set_enabled(acc.provider, account_id, enabled)
    return {"ok": True}


# ── 客户端指纹（每账号独立设备档案）──────────────────────────────────────────
@router.post("/accounts/{account_id}/fingerprint/rotate")
async def rotate_fingerprint(account_id: str):
    """换发账号客户端指纹（下一套设备模板 + 全新 device_mid）。

    场景：账号被风控后换设备重生；或怀疑指纹污染时手动更换。
    """
    from ..fingerprint import profile_for, rotate

    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    old = profile_for(acc)
    profile = rotate(acc)
    acc.installed_at = None  # 换机后必须重跑安装序，旧 MID 的日活不能顶新设备
    store.update_account(acc)
    _schedule_install(acc)
    logs.info("fingerprint", f"账号 {acc.name} 指纹换发: "
                             f"{old.platform_full}/{old.device_mid[:8]} → "
                             f"{profile.platform_full}/{profile.device_mid[:8]}")
    return {"ok": True, "fingerprint": acc.fingerprint}


# ── 刷新额度（实时用量监控）─────────────────────────────────────────────────
@router.post("/accounts/refresh")
async def refresh(payload: dict = Body(default=None)):
    payload = payload or {}
    if payload.get("all"):
        pool = [a for a in store.list_accounts("zai") if a.mode == "jwt"]
    else:
        ids = set(payload.get("ids") or [])
        pool = [a for a in store.list_accounts() if a.id in ids and a.mode == "jwt"]
    # 失效 / 风控禁用 / 冷却 一律不打 billing（冷却期零上游流量 + 废 JWT 不再 401）
    targets = [a for a in pool if a.allows_billing()]
    skipped_cooling = sum(1 for a in pool if a.is_cooling())
    skipped_invalid = len(pool) - len(targets) - skipped_cooling
    summary = await refresh_accounts(targets)
    return {
        "summary": summary,
        "count": len(targets),
        "skipped_cooling": skipped_cooling,
        "skipped_invalid": skipped_invalid,
    }


@router.post("/accounts/{account_id}/refresh")
async def refresh_one(account_id: str):
    acc = store.find_any(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if acc.mode != "jwt":
        return {"ok": False, "message": "仅 Coding Plan (JWT) 账号支持额度查询"}
    blocked = billing_block_reason(acc, action="上游刷新")
    if blocked:
        return {"ok": False, "message": blocked, "account": acc.public_view()}
    res = await fetch_quota(acc)
    return {"ok": "error" not in res, "result": res, "account": acc.public_view()}


# ── OAuth 登录（Z.AI）────────────────────────────────────────────────────────
# 授权链接有效期：上游 cli/init 发起的流程约 5 分钟过期（zcode.z.ai 行为）。
# 会话过期后 poll 返回 {"status": "expired"}（幂等，可安全重试）。
LOGIN_FLOW_TTL = 300.0
# 兑换 API Key（getCustomerInfo → api_keys → copy）总时长上限
LOGIN_EXCHANGE_TIMEOUT = 60.0

# flow_id -> {"flow": ZaiAuthFlow, "created": float, "label": str}
# 单进程内存态即可：登录会话不该跨进程存活，重启后用户重新生成链接。
_login_flows: dict[str, dict] = {}


def _login_gc() -> None:
    now = time.time()
    expired = [fid for fid, entry in _login_flows.items() if now - entry["created"] > LOGIN_FLOW_TTL]
    for fid in expired:
        _login_flows.pop(fid, None)


@router.post("/login/start")
async def login_start(payload: dict = Body(default=None)):
    """发起 Z.AI OAuth，返回授权链接供前端展示。

    payload 可选 {"label": "acct-1"} —— 作为账号名前缀入池，便于多号识别。
    """
    payload = payload or {}
    label = (payload.get("label") or "").strip()[:32]
    _login_gc()
    flow = ZaiAuthFlow()
    try:
        flow_id, authorize_url = await flow.init()
    except Exception as err:  # noqa: BLE001
        logs.warn("oauth", f"登录初始化失败: {type(err).__name__}: {err}")
        raise HTTPException(502, f"登录初始化失败: {err}") from err
    _login_flows[flow_id] = {"flow": flow, "created": time.time(), "label": label}
    logs.info("oauth", f"发起登录 flow_id={flow_id} label={label or '-'}")
    return {
        "flow_id": flow_id,
        "authorize_url": authorize_url,
        "expires_in": int(LOGIN_FLOW_TTL),
    }


@router.get("/login/poll/{flow_id}")
async def login_poll(flow_id: str):
    """轮询授权状态；JWT 入池后立即 ready，API Key 兑换/刷新在后台回填。

    返回 status ∈ pending / ready / failed / expired。
    failed 附带 message（上游拒绝原因）；expired 表示会话超时需重新发起；
    未知 flow_id 一律 expired（而非 404），前端据此提示重新生成链接。
    官方 poll HTTP 4xx 视为终态 failed；5xx/网络抖动保持 pending 并打日志。
    """
    _login_gc()
    entry = _login_flows.get(flow_id)
    if not entry:
        return {"status": "expired"}
    flow = entry["flow"]
    try:
        data = await flow.poll(flow_id)
    except httpx.HTTPStatusError as err:
        code = err.response.status_code
        logs.warn("oauth", f"poll {flow_id} 上游 HTTP {code}")
        if 400 <= code < 500:
            _login_flows.pop(flow_id, None)
            # 3004（官方 poll 4xx 承载的会话过期）：明确映射 expired 让前端提示
            # 重新生成链接，不再归为 failed（zcode-switch poll 3004 语义同形）
            try:
                body_code = (err.response.json() or {}).get("code")
            except ValueError:
                body_code = None
            if body_code == 3004:
                logs.info("oauth", f"授权会话过期 flow_id={flow_id}（上游 3004）")
                return {"status": "expired", "message": "授权会话已过期，请重新生成授权链接"}
            return {"status": "failed", "message": f"上游拒绝轮询（HTTP {code}）"}
        return {"status": "pending"}
    except Exception as err:  # noqa: BLE001 - 单次网络抖动按 pending 处理
        logs.warn("oauth", f"poll {flow_id} 抖动: {type(err).__name__}")
        return {"status": "pending"}

    state = data.get("status")
    if state == "failed":
        _login_flows.pop(flow_id, None)
        reason = (data.get("message") or data.get("reason") or "授权失败或被拒绝")
        logs.warn("oauth", f"授权失败 flow_id={flow_id}: {reason}")
        return {"status": "failed", "message": str(reason)}
    if state != "ready":
        return {"status": "pending"}

    # 会话先摘除再入池：并发/重复 poll 不会再进入兑换链。
    _login_flows.pop(flow_id, None)

    # JWT 先入池并立刻 ready；兑换 API Key / 额度刷新改后台，避免卡住前端下一轮 poll。
    zcode_jwt = data.get("token")
    access_token = (data.get("zai") or {}).get("access_token")
    label = entry.get("label") or "oauth-login"
    account = None
    if zcode_jwt:
        account = store.add_account("zai", label, zcode_jwt)
    elif access_token:
        try:
            api_key = await asyncio.wait_for(
                flow.exchange_api_key(access_token), timeout=LOGIN_EXCHANGE_TIMEOUT
            )
            account = store.add_account("zai", label, api_key)
        except Exception as err:  # noqa: BLE001
            logs.warn("oauth", f"无 JWT 时兑换 API Key 失败: {err}")

    if account is None:
        logs.warn("oauth", f"授权结果无凭证 flow_id={flow_id}")
        return {"status": "failed", "message": "未能从授权结果中获取凭证"}

    if account.mode == "jwt":
        _schedule_auto_claim(account)  # 授权完成即激活+自动领取，入池即吃满活动
    _schedule_install(account)  # 按账号安装序（幂等；apiKey 账号同样安装）
    _schedule_login_followup(account, flow, access_token if zcode_jwt else None)
    logs.info("oauth", f"授权成功入池 {account.name} ({account.id}) mode={account.mode}")
    return {"status": "ready", "account": account.public_view()}


# ── 额度领取 ─────────────────────────────────────────────────────────────────
_auto_claim_tasks: set[asyncio.Task] = set()  # 强引用防 GC
_login_followup_tasks: set[asyncio.Task] = set()


def _schedule_login_followup(account, flow, access_token: str | None) -> None:
    """ready 后后台兑换 API Key 并刷新额度；失败只打日志，不影响已入池的 JWT。"""

    async def _job():
        live = store.find("zai", account.id)
        if live is None:
            return
        if access_token:
            try:
                api_key = await asyncio.wait_for(
                    flow.exchange_api_key(access_token), timeout=LOGIN_EXCHANGE_TIMEOUT
                )
                live = store.find("zai", account.id)
                if live is None:
                    return
                live.api_key = api_key
                store.update_account(live)
            except Exception as err:  # noqa: BLE001 - 兑换失败不影响 JWT 已入池
                logs.warn("oauth", f"账号 {account.name} 兑换 API Key 失败: {err}")
        live = store.find("zai", account.id)
        if live is None:
            return
        if live.mode == "jwt":
            try:
                await refresh_accounts([live])
            except Exception as err:  # noqa: BLE001
                logs.warn("oauth", f"账号 {account.name} 入池后额度刷新失败: {err}")

    task = asyncio.create_task(_job())
    _login_followup_tasks.add(task)
    task.add_done_callback(_login_followup_tasks.discard)


def _schedule_auto_claim(account) -> None:
    """入池后调度后台自动领取（激活上报 + 全量可领套餐）。

    不阻塞入池响应（验证码求解可长达数十秒）；仅 JWT 账号。失败不影响入池。
    """
    if not (account.mode == "jwt" and account.jwt_token):
        return

    async def _job():
        live = store.find(account.provider, account.id)
        if live is None:
            return
        try:
            outcomes = await auto_claim_all_plans(live)
            live = store.find(account.provider, account.id)
            if live is None:
                return
            if outcomes:
                await refresh_accounts([live])  # 领到额度立即反映到 UI
        except Exception as err:  # noqa: BLE001 - 兜底：绝不冒泡
            logs.warn("claim", f"账号 {account.name} 自动领取任务异常: {err}")

    task = asyncio.create_task(_job())
    _auto_claim_tasks.add(task)
    task.add_done_callback(_auto_claim_tasks.discard)


def _jwt_accounts(account_ids: list[str] | None) -> list:
    accounts = store.list_accounts("zai")
    if account_ids:
        wanted = set(account_ids)
        accounts = [a for a in accounts if a.id in wanted]
    return [a for a in accounts if a.mode == "jwt" and a.jwt_token]


def _schedule_install(account) -> None:
    """入池后调度后台按账号安装序（configs + 激活事件，幂等）。

    不阻塞入池响应；任何账号模式都跑（apiKey 账号 user_id 退空串，同官方
    未登录安装形态）。失败不影响入池；重复调用安全（installed_at 幂等跳过）。
    实现收口在 install.schedule_install（风控禁用自动换设备等场景共用）。
    """
    from ..install import schedule_install

    schedule_install(account)


@router.get("/claim/preview")
async def claim_preview(account_id: str | None = None):
    """立即拉取可领取套餐（全部/单个 JWT 账号）。

    先上报激活事件（zcode-switch claim_refresh 同形，模拟官方客户端当日活跃；
    疑似活动投放资格信号），上报失败不阻断 preview。
    """
    ids = [account_id] if account_id else None
    out = []
    for acc in _jwt_accounts(ids):
        blocked = billing_block_reason(acc, action="上游查询")
        if blocked:
            out.append({"account_id": acc.id, "account_name": acc.name,
                        "plans": [], "error": blocked,
                        "activated": False, "activation_error": None})
            continue
        try:
            activation_error = await report_activation_events(acc)
        except Exception as err:  # noqa: BLE001 - 上报失败不阻断 preview
            activation_error = str(err)
        try:
            plans = await preview_plans(acc)
            out.append({"account_id": acc.id, "account_name": acc.name,
                        "plans": plans, "error": None,
                        "activated": activation_error is None,
                        "activation_error": activation_error})
        except ClaimError as err:
            out.append({"account_id": acc.id, "account_name": acc.name,
                        "plans": [], "error": str(err),
                        "activated": activation_error is None,
                        "activation_error": activation_error})
    return {"preview": out}


@router.post("/claim")
async def claim(payload: dict = Body(default=None)):
    """领取套餐（body 可选 account_ids / plan_id）；缺省对全部 JWT 账号自动选最优套餐。

    返回 outcomes[]：{account_id, account_name, ok, plan_name?, grants?, message?}。
    """
    payload = payload or {}
    account_ids = payload.get("account_ids") or None
    plan_id = (payload.get("plan_id") or "").strip() or None
    candidates = _jwt_accounts(account_ids)
    if not candidates:
        return {"outcomes": [], "summary": {"ok": 0, "fail": 0}}

    # 冷却 / 失效 JWT 不领取（billing/claim 是上游写流量，废票只会 401）
    outcomes = [
        {"account_id": a.id, "account_name": a.name, "ok": False,
         "message": billing_block_reason(a) or AUTH_EXPIRED_MESSAGE}
        for a in candidates if not a.allows_billing()
    ]
    for acc in candidates:
        if not acc.allows_billing():
            continue
        try:
            result = await do_claim(acc, plan_id)
        except ClaimError as err:
            logs.warn("claim", f"账号 {acc.name} 领取失败: {err}")
            outcome = {"account_id": acc.id, "account_name": acc.name,
                       "ok": False, "message": str(err)}
            if err.code != -1:
                outcome["code"] = err.code
            if err.next_at:
                outcome["next_at"] = err.next_at
            outcomes.append(outcome)
            continue
        except CaptchaSolveError as err:
            # 验证码求解失败（get_verify_param）：明确业务回执而非裸 500
            logs.err("claim", f"账号 {acc.name} 领取失败: {err}")
            outcomes.append({"account_id": acc.id, "account_name": acc.name,
                             "ok": False, "message": str(err)})
            continue
        except RuntimeError as err:
            # 兜底：captcha 层历史语义的运行时故障，防回归裸 500
            logs.err("claim", f"账号 {acc.name} 领取失败: {err}")
            outcomes.append({"account_id": acc.id, "account_name": acc.name,
                             "ok": False, "message": str(err)})
            continue
        await refresh_accounts([acc])
        outcomes.append({"account_id": acc.id, "account_name": acc.name,
                         "ok": True, **result})
    ok = sum(1 for o in outcomes if o["ok"])
    return {"outcomes": outcomes, "summary": {"ok": ok, "fail": len(outcomes) - ok}}


@router.get("/claim/captcha-config")
async def claim_captcha_config():
    """手动领取用：阿里验证码 SDK 初始化参数（前端浏览器内完成人机验证）。"""
    from ..captcha import captcha_manager

    config = await captcha_manager.fetch_config()
    return {
        "enabled": bool(config.get("enabled", True)),
        "scene_id": config.get("sceneId") or "",
        "region": config.get("region") or "",
        "prefix": config.get("prefix") or "",
    }


@router.post("/claim/manual")
async def claim_manual(payload: dict = Body(...)):
    """手动领取：body {account_id, captcha_verify_param, captcha_region?, plan_id?}。

    verify_param 必须来自用户浏览器内阿里 SDK 滑块成功回调（无头环境无法求解）。
    """
    account_id = (payload.get("account_id") or "").strip()
    verify_param = (payload.get("captcha_verify_param") or "").strip()
    region = (payload.get("captcha_region") or "").strip() or None
    plan_id = (payload.get("plan_id") or "").strip() or None
    if not account_id:
        raise HTTPException(400, "缺少 account_id")

    acc = store.find("zai", account_id)
    if not acc or acc.mode != "jwt" or not acc.jwt_token:
        raise HTTPException(404, "JWT 账号不存在")
    blocked = billing_block_reason(acc)
    if blocked:
        return {"outcomes": [{"account_id": acc.id, "account_name": acc.name,
                              "ok": False, "message": blocked}],
                "summary": {"ok": 0, "fail": 1}}

    try:
        result = await claim_with_captcha(acc, verify_param, region, plan_id)
    except ClaimError as err:
        return {"outcomes": [{"account_id": acc.id, "account_name": acc.name,
                              "ok": False, "message": str(err)}],
                "summary": {"ok": 0, "fail": 1}}
    await refresh_accounts([acc])
    return {"outcomes": [{"account_id": acc.id, "account_name": acc.name,
                          "ok": True, **result}],
            "summary": {"ok": 1, "fail": 0}}


# ── 客户端一键接入（magpie 等）────────────────────────────────────────────────
def _gateway_base_from_request(request: Request) -> str:
    """看板所在浏览器访问网关用的地址；非回环绑定一律回落 127.0.0.1。"""
    from .. import settings as app_settings

    host = (request.url.hostname or "").strip()
    if host in ("", "0.0.0.0", "::", "::1"):
        host = "127.0.0.1"
    port = request.url.port or app_settings.PORT
    return f"http://{host}:{port}"


def _clients():
    from .. import clients as clients_module

    return clients_module


@router.get("/agents")
async def agents_overview(request: Request):
    """客户端接入看板：安装/接入状态、网关地址、magpie 导入链接。"""
    return _clients().overview(
        base_url=_gateway_base_from_request(request),
        api_key=store.gateway_key(),
    )


@router.post("/agents/apply")
async def agents_apply(request: Request, payload: dict = Body(...)):
    """一键接入：把本网关写进客户端配置（写前备份、原子落盘、可还原）。"""
    clients = _clients()
    client_id = str(payload.get("client") or payload.get("client_id") or "").strip()
    if not client_id:
        raise HTTPException(400, "缺少 client")
    base_url = str(payload.get("base_url") or "").strip() or _gateway_base_from_request(request)
    models = payload.get("models")
    if models is not None and not isinstance(models, list):
        raise HTTPException(400, "models 必须是字符串数组")
    try:
        report = await asyncio.to_thread(
            clients.integrate, client_id,
            base_url=base_url, api_key=store.gateway_key(),
            models=[str(m) for m in models] if models else None,
        )
    except clients.AgentConfigError as err:
        raise HTTPException(400, str(err)) from None
    except OSError as err:
        raise HTTPException(500, f"写入失败: {err}") from None
    return {"ok": True, **report}


@router.post("/agents/restore")
async def agents_restore(payload: dict = Body(...)):
    """一键还原：回到首次接入前的配置（外部改过的文件只摘掉本网关的 provider）。"""
    clients = _clients()
    client_id = str(payload.get("client") or payload.get("client_id") or "").strip()
    if not client_id:
        raise HTTPException(400, "缺少 client")
    try:
        result = await asyncio.to_thread(clients.restore, client_id)
    except clients.AgentConfigError as err:
        raise HTTPException(400, str(err)) from None
    except OSError as err:
        raise HTTPException(500, f"还原失败: {err}") from None
    return {"ok": True, **result}


@router.post("/agents/magpie/share")
async def agents_magpie_share(request: Request):
    """一键把本机 magpie 设成共享端：打开局域网共享 + 备好网关 Key。

    对应 magpie 自己的「另一台电脑上的 magpie（Remote magpie）」：对端只要执行返回的
    一行命令即可用上这台机器的 provider（含 zcode2api）。
    """
    clients = _clients()
    try:
        sharing = await asyncio.to_thread(clients.enable_magpie_sharing)
        key = await asyncio.to_thread(clients.magpie_gateway_key)
    except clients.AgentConfigError as err:
        raise HTTPException(400, str(err)) from None
    except OSError as err:
        raise HTTPException(500, f"配置 magpie 失败: {err}") from None
    plan = await asyncio.to_thread(
        clients.remote_share_plan,
        base_url=_gateway_base_from_request(request),
        api_key=store.gateway_key(),
    )
    return {
        "ok": True,
        "sharing": sharing,
        "key": {"id": key["id"], "name": key["name"], "secret": key["secret"],
                "created": key["created"]},
        "magpie": plan["magpie"],
        "restart_hint": plan["magpie"].get("reachable") is False,
    }


@router.post("/agents/gateway-key")
async def agents_gateway_key(request: Request):
    """生成一个新的网关 API Key 并立即生效（其它电脑直连本网关时使用）。

    注意：一旦设置，所有调用 `/v1/*` 的客户端都必须带上这个 Key。已接入的客户端
    （本机 magpie 的 provider 条目）会就地刷新成新 Key，避免本机自己先被 401。
    """
    token = "sk-zcode-" + secrets.token_hex(16)
    store.set_setting("gateway_key", token)
    logs.step("agents", "已生成新的网关 API Key（其它电脑直连时使用）")
    clients = _clients()
    base = _gateway_base_from_request(request)
    refreshed: list[str] = []
    for client_id in clients.configured_clients():
        try:
            await asyncio.to_thread(clients.integrate, client_id,
                                    base_url=base, api_key=token)
            refreshed.append(client_id)
        except (clients.AgentConfigError, OSError) as err:  # 单个客户端失败不阻断
            logs.warn("agents", f"{client_id} 刷新新 Key 失败: {err}")
    return {"ok": True, "gateway_key": token, "masked": _mask_secret(token),
            "refreshed_clients": refreshed}


@router.post("/agents/magpie/sync-now")
async def agents_magpie_sync_now():
    """立刻做一次 magpie 加密同步（推送本机改动 / 拉取仓库改动）。"""
    clients = _clients()
    try:
        return {"ok": True, **await asyncio.to_thread(clients.magpie_sync_now)}
    except clients.AgentConfigError as err:
        raise HTTPException(400, str(err)) from None
    except OSError as err:
        raise HTTPException(500, f"同步失败: {err}") from None


@router.post("/agents/magpie/sync")
async def agents_magpie_sync(payload: dict = Body(...)):
    """从后台开启 GitHub 加密同步（token/口令经 stdin 交给 magpie，不落命令行）。"""
    clients = _clients()
    repo = str(payload.get("repo") or "").strip()
    token = str(payload.get("token") or "").strip()
    passphrase = str(payload.get("passphrase") or "").strip()
    if not repo or not token or not passphrase:
        raise HTTPException(400, "缺少 repo / token / passphrase")
    try:
        result = await asyncio.to_thread(
            clients.enable_magpie_sync, repo=repo, token=token, passphrase=passphrase,
            keys=bool(payload.get("keys", True)),
            agents=bool(payload.get("agents", True)),
            library=bool(payload.get("library", True)),
        )
    except clients.AgentConfigError as err:
        raise HTTPException(400, str(err)) from None
    except OSError as err:
        raise HTTPException(500, f"开启同步失败: {err}") from None
    return {"ok": True, **result}


# ── 设置 ─────────────────────────────────────────────────────────────────────
def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}…{value[-4:]}"


@router.get("/settings")
async def get_settings():
    from .. import settings as app_settings

    admin_key = store.admin_key()
    gateway_key = store.gateway_key()
    return {
        "admin_key_set": bool(admin_key),
        "admin_key_masked": _mask_secret(admin_key),
        "admin_key_is_default": bool(admin_key) and admin_key == app_settings.DEFAULT_ADMIN_KEY,
        "gateway_key_set": bool(gateway_key),
        "gateway_key_masked": _mask_secret(gateway_key),
        "quota_refresh_interval": store.quota_refresh_interval(),
        "account_concurrency": store.account_concurrency(),
        "claim_round_interval": store.claim_round_interval(),
        # 全局粘号：所有会话的流量都尽量留在同一个账号（只有它不可用才轮询）
        "account_sticky": store.account_sticky_enabled(),
        # 回填「归一化后」的链：modelpool.chain 会把小写别名映射成上游官方名、
        # 去重、去空。若原样回填用户写的 "glm-5.3"，输入框里看到的名字和网关
        # 实际请求的 "GLM-5.3" 不一致，改完保存还以为没生效。
        "model_fallback_enabled": store.model_fallback_enabled(),
        "model_fallback_chain": ",".join(modelpool.chain("zai")),
        "model_fallback_ttl": store.model_fallback_ttl(),
    }


@router.put("/settings")
async def update_settings(payload: dict = Body(...)):
    if "admin_key" in payload:
        key = (payload["admin_key"] or "").strip()
        if not key:
            raise HTTPException(400, "后台密钥不能为空")
        if "…" in key or key == "••••":
            pass  # 前端回填的掩码，不改密
        else:
            store.set_setting("admin_key", key)
    if "gateway_key" in payload:
        key = (payload["gateway_key"] or "").strip()
        if "…" in key or key == "••••":
            pass
        else:
            store.set_setting("gateway_key", key)
    if "quota_refresh_interval" in payload:
        try:
            interval = max(0, int(payload["quota_refresh_interval"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "刷新间隔必须是非负整数") from None
        store.set_setting("quota_refresh_interval", str(interval))
    if "account_concurrency" in payload:
        try:
            concurrency = max(0, int(payload["account_concurrency"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "账号并发必须是非负整数（0 = 不限）") from None
        store.set_setting("account_concurrency", str(concurrency))
    if "claim_round_interval" in payload:
        try:
            interval = max(0, int(payload["claim_round_interval"]))
        except (TypeError, ValueError):
            raise HTTPException(400, "自动领取轮间隔必须是非负整数（0 = 关闭）") from None
        store.set_setting("claim_round_interval", str(interval))
    if payload.get("model_fallback_enabled") is not None:
        # 前端勾选框给真布尔，脚本/配置文件给 "1"/"0"，两种都收（统一落成 "1"/"0"，
        # store 侧只认这一种存储形态）。null 视为「没提这个字段」而不是「关掉」——
        # 把 null 当 False 会让只想改兜底链的脚本顺手把功能关掉，很难查。
        raw = payload["model_fallback_enabled"]
        if isinstance(raw, bool):
            enabled = raw
        else:
            enabled = str(raw).strip().lower() in ("1", "true", "yes", "on")
        store.set_setting("model_fallback_enabled", "1" if enabled else "0")
    if payload.get("model_fallback_chain") is not None:
        raw = payload["model_fallback_chain"]
        items = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
        # 空串是合法值（= 不兜底），所以只去每项空白、不去掉「整体为空」
        chain = ",".join(str(m).strip() for m in items if str(m).strip())
        store.set_setting("model_fallback_chain", chain)
    if payload.get("model_fallback_ttl") is not None:
        try:
            ttl = int(payload["model_fallback_ttl"])
        except (TypeError, ValueError):
            raise HTTPException(400, "模型兜底冷却必须是非负整数") from None
        if ttl < 0:
            raise HTTPException(400, "模型兜底冷却必须是非负整数")
        store.set_setting("model_fallback_ttl", str(ttl))
    if payload.get("account_sticky") is not None:
        # 与模型兜底开关同一口径：bool 或 "0"/"1"，null 视为没提这个字段
        raw = payload["account_sticky"]
        enabled = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
        store.set_setting("account_sticky", "1" if enabled else "0")
    return {"ok": True}


# ── 模型兜底（自动换模型）────────────────────────────────────────────────────
@router.get("/models")
async def list_models():
    """模型兜底实时台账：配置 + 每个模型的可用性证据。

    后台的「状态表」与网关的调度必须看到同一份证据（额度快照 / 反应式死标记），
    所以这里直接复用 modelpool.snapshot，而不是前端自己拼账号额度。
    """
    return {
        "enabled": store.model_fallback_enabled(),
        "chain": modelpool.chain("zai"),
        "ttl": store.model_fallback_ttl(),
        "models": modelpool.snapshot("zai"),
    }


@router.post("/models/revive")
async def revive_models(payload: dict | None = Body(default=None)):
    """人工「立即恢复」：给一个模型清死标记，不给则清空整个 provider。"""
    model = str((payload or {}).get("model") or "").strip()
    if model:
        modelpool.revive("zai", model)
    else:
        modelpool.clear("zai")
    return {"ok": True}


# ── 导入 / 导出 ─────────────────────────────────────────────────────────────
@router.get("/export")
async def export_accounts():
    return store.export()


@router.post("/import")
async def import_accounts(payload: dict = Body(...)):
    existing = {a.id for a in store.list_accounts()}
    count = store.import_accounts(payload)
    # 导入的新账号：安装序（幂等）+ JWT 账号激活+自动领取（幂等：重复 token 不新增）
    imported = [a for a in store.list_accounts("zai") if a.id not in existing]
    for acc in imported:
        _schedule_install(acc)
        if acc.mode == "jwt":
            _schedule_auto_claim(acc)
    return {"count": count}


# ── 请求监控 ─────────────────────────────────────────────────────────────────
_RANGE_SECONDS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "d3": 3 * 86400,
                  "7d": 7 * 86400, "30d": 30 * 86400}


def _range_start(range_key: str) -> float:
    """时间范围 → 起始时间戳。today = 本地零点（用户看到的是本地日期）。"""
    if range_key == "today":
        lt = time.localtime()
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    seconds = _RANGE_SECONDS.get(range_key)
    return time.time() - seconds if seconds else 0.0


def _usage_filter(range_key: str, outcome: str, account: str, model: str,
                  q: str, channel: str = "") -> tuple[str, tuple]:
    """拼出明细/聚合共用的 WHERE（两处口径必须一致，否则统计与列表对不上）。

    channel: "public"（Cloudflare 隧道进来的公网请求）/ "local"（内网、本机）
    或空/`all`（合计）。来源作为可筛选维度，与其它条件正交。
    """
    clauses: list[str] = ["ts >= ?"]
    params: list = [_range_start(range_key)]
    if outcome == "fail":
        clauses.append("outcome <> 'completed'")
    elif outcome and outcome != "all":
        clauses.append("outcome = ?")
        params.append(outcome)
    if account:
        clauses.append("account = ?")
        params.append(account)
    if model:
        clauses.append("model = ?")
        params.append(model)
    if channel in ("public", "local"):
        clauses.append("channel = ?")
        params.append(channel)
    if q:
        clauses.append("(preview LIKE ? OR error LIKE ? OR model LIKE ? OR account LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like])
    return " AND ".join(clauses), tuple(params)


@router.get("/monitoring")
async def monitoring():
    """实时视图：内存环（含进行中，重启清零）。明细/聚合请用 /usage。"""
    return {"entries": reqlog.snapshot(), "keep": reqlog.KEEP,
            "inflight": reqlog.inflight_count()}


@router.get("/usage")
async def usage(range: str = "today", outcome: str = "all", account: str = "",
                model: str = "", q: str = "", channel: str = "all", limit: int = 100,
                offset: int = 0, heatmap_days: int = 84):
    """请求台账（数据看板）：分页明细 + 区间聚合 + 时序 + 每日热力图。

    一次请求把看板要的所有聚合都带上：KPI 卡、趋势图、维度表、热力图各自取字段，
    前端 5 秒轮询只打这一个端点（少一轮往返，口径也天然一致）。

    channel：`public` / `local` / `all`（默认合计）。无论当前筛选哪个来源，
    `by_channel` 都给「公网 / 内网 / 合计」三栏的区间聚合（KPI 卡与来源面板用）。
    """
    usagelog.flush()   # 让刚结束的请求当场可见（写入是异步批量的）
    where, params = _usage_filter(range, outcome, account, model, q, channel)
    start = _range_start(range)
    # 联邦维度：带账号/关键词过滤的查询不联邦（日聚合表没有这两个粒度）
    dims = None
    if not account and not q and outcome in ("", "all", "fail", "completed"):
        dims = {"model": model, "channel": channel if channel in ("public", "local") else ""}
    # 来源对比必须忽略 channel 筛选本身，否则筛了公网就只剩公网一栏
    base_where, base_params = _usage_filter(range, outcome, account, model, q, "")
    rows, total = usagelog.query(where, params, limit=limit, offset=offset)
    return {
        "rows": rows,
        "total": total,
        "limit": max(1, min(int(limit), usagelog.MAX_PAGE)),
        "offset": max(0, int(offset)),
        "stats": usagelog.stats(where, params, start=start, dims=dims),
        "by_account": usagelog.group_by("account", where, params),
        "by_model": usagelog.group_by("model", where, params, start=start, dims=dims),
        "by_endpoint": usagelog.group_by("endpoint", where, params),
        "by_channel": usagelog.group_by("channel", base_where, base_params),
        "series": usagelog.series(where, params, start=start, dims=dims),
        "fallbacks": usagelog.fallbacks(where, params, limit=50),
        "retention_days": settings.USAGE_RETENTION_DAYS,
        "heatmap": usagelog.heatmap(heatmap_days),
    }


@router.post("/monitoring/clear")
async def monitoring_clear():
    """清空实时环与持久台账。"""
    reqlog.clear()
    return {"ok": True}


# ── 续派监督开关（着色器项目 _sup 目录的文件开关，v0.2 语义）────────────────
# enabled: continue.enabled 总闸；loop: continue.loop 循环模式；
# prompt: next-prompt.txt 一次性指令（由 Stop 钩子消费后自动归档）。

from ..settings import SUP_DIR as _SUP_DIR  # data/_sup（续派监督文件协议）


def _sup_read(name: str):
    import pathlib
    try:
        return (pathlib.Path(_SUP_DIR) / name).read_text(encoding="utf-8").strip()
    except Exception:
        return None


@router.get("/supervisor")
async def supervisor_status():
    import json as _json
    import pathlib
    sup = pathlib.Path(_SUP_DIR)
    state = {}
    try:
        state = _json.loads((sup / "hook-state.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    return {
        "enabled": (sup / "continue.enabled").exists(),
        "loop": (sup / "continue.loop").exists(),
        "prompt": _sup_read("next-prompt.txt"),
        "count": state.get("count", 0),
        "lastAt": state.get("lastAt", 0),
    }


@router.post("/supervisor/enabled")
async def supervisor_set_enabled(payload: dict = Body(...)):
    import pathlib
    import time as _time
    sup = pathlib.Path(_SUP_DIR)
    flag = sup / "continue.enabled"
    want = bool(payload.get("enabled", True))
    if want and not flag.exists():
        flag.write_text("", encoding="utf-8")
    elif not want and flag.exists():
        flag.rename(sup / f"continue.enabled.disabled-{_time.strftime('%Y%m%d-%H%M%S')}")
    return {"ok": True, "enabled": flag.exists()}


@router.post("/supervisor/loop")
async def supervisor_set_loop(payload: dict = Body(...)):
    import pathlib
    import time as _time
    sup = pathlib.Path(_SUP_DIR)
    flag = sup / "continue.loop"
    want = bool(payload.get("loop", False))
    if want and not flag.exists():
        flag.write_text("", encoding="utf-8")
    elif not want and flag.exists():
        flag.rename(sup / f"continue.loop.disabled-{_time.strftime('%Y%m%d-%H%M%S')}")
    return {"ok": True, "loop": flag.exists()}


@router.post("/supervisor/prompt")
async def supervisor_set_prompt(payload: dict = Body(...)):
    import pathlib
    sup = pathlib.Path(_SUP_DIR)
    text = str(payload.get("text", "")).strip()
    flag = sup / "next-prompt.txt"
    if text:
        flag.write_text(text + "\n", encoding="utf-8")
    elif flag.exists():
        flag.unlink()
    return {"ok": True, "prompt": text or None}
