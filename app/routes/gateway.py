"""核心网关：/v1/messages（Anthropic 风格）与 /v1/chat/completions（OpenAI 风格）。

共用多账号轮询 + 额度用完自动换号 + 阿里无痕验证自动续期；OpenAI 端点由
openai_compat 做双向格式转换，调度与错误处理策略完全一致。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import affinity, constants, logs, reqlog, settings
from ..agent import build_request
from ..auth_admin import verify_gateway_key
from ..captcha import captcha_manager
from ..modelpool import modelpool
from ..models import Account, Status
from ..openai_compat import (
    StreamConverter,
    UsageSniffer,
    anthropic_to_openai,
    openai_to_anthropic,
)
from ..quota import fetch_quota
from ..store import store

_sleep = asyncio.sleep  # 模块级引用：测试可 patch 此名而免污染全局 asyncio

router = APIRouter()

MAX_CAPTCHA_RETRIES = 3
MAX_ACCOUNT_ATTEMPTS = 5

# 常量收口：模型表与被拒信号关键字统一在 app/constants.py
MODEL_NAME_MAP = constants.MODEL_NAME_MAP
AVAILABLE_MODELS = constants.AVAILABLE_MODELS
_EXHAUST_KEYWORDS = constants.EXHAUST_KEYWORDS


def _detect_provider(body: dict, headers) -> str:
    model = body.get("model") or ""
    if model.startswith("bigmodel/") or headers.get("x-provider") == "bigmodel":
        return "bigmodel"
    return "zai"


def _shape_note(body: dict, incoming_headers: dict | None = None) -> str:
    """请求形状指纹（风控 3012 诊断用）。

    上游 3.16.x 起对 messages 校验「官方 CLI 请求形状」，形状不对就是 405/3012，
    与账号、额度无关（2026-10-26 / 10-30 两次实证）。形状差异只能从客户端来，
    所以这里只记录**可判定、可对比**的维度，不落正文：
      - body 顶层键（新增字段 = 客户端升版最直观的信号）
      - system 块数 / 首块长度 / 是否带 cache_control
      - tools 数量 + 前 5 个工具名（工具集变化同样会撞形状校验）
      - max_tokens / stream / thinking 形态
      - 客户端透传头里与形状相关的两个（anthropic-beta / anthropic-version）
    """
    if not isinstance(body, dict):
        return "body=<非对象>"
    keys = ",".join(sorted(body.keys()))
    system = body.get("system")
    if isinstance(system, list):
        blocks = len(system)
        first_len = len(str(((system[0] or {}).get("text") if isinstance(system[0], dict) else system[0]) or ""))
        cached = sum(1 for b in system if isinstance(b, dict) and b.get("cache_control"))
        system_note = f"system={blocks}块/首块{first_len}字/缓存{cached}"
    else:
        system_note = f"system={type(system).__name__}"
    tools = body.get("tools")
    if isinstance(tools, list):
        names = [str((t or {}).get("name") or "?") for t in tools[:5] if isinstance(t, dict)]
        tools_note = f"tools={len(tools)}({','.join(names)})"
    else:
        tools_note = "tools=0"
    thinking = body.get("thinking")
    think_note = "thinking=" + (str((thinking or {}).get("type")) if isinstance(thinking, dict) else str(thinking))
    headers = {k.lower(): v for k, v in (incoming_headers or {}).items()}
    beta = str(headers.get("anthropic-beta") or "-")[:60]
    version = str(headers.get("anthropic-version") or "-")[:20]
    return (f"keys=[{keys}] {system_note} {tools_note} {think_note} "
            f"max_tokens={body.get('max_tokens')} stream={body.get('stream')} "
            f"anthropic-beta={beta} anthropic-version={version}")


def _dump_risk_body(req_id: str, body: dict, incoming_headers: dict | None = None) -> str:
    """命中风控时把**这一次的真实请求体**落盘（data/debug/），返回文件路径。

    300 字以内的形状快照只能看出「变没变」，看不出「哪一行不对」；真要定形状问题
    必须拿到 body 做字段级对比。只在 3012/405 分支调用：正常请求一行都不落盘。
    只保留最近 BODY_TRACE_KEEP 份（默认 5），避免长期堆积用户内容。
    """
    import json as _json
    import time as _time

    from .. import settings as _settings

    if not _settings.BODY_TRACE:
        return ""
    out_dir = _settings.DATA_DIR / "debug"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"risk-body-{stamp}-{req_id}.json"
        headers = {k.lower(): v for k, v in (incoming_headers or {}).items()}
        keep_headers = {k: v for k, v in headers.items()
                        if k.startswith(("anthropic", "x-", "content-", "accept"))}
        path.write_text(_json.dumps({
            "req_id": req_id,
            "ts": _time.time(),
            "shape": _shape_note(body, incoming_headers),
            "headers": keep_headers,
            "body": body,
        }, ensure_ascii=False), "utf-8")
        # 轮转：只留最近 N 份
        files = sorted(out_dir.glob("risk-body-*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - _settings.BODY_TRACE_KEEP)]:
            try:
                old.unlink()
            except OSError:
                pass
        return str(path)
    except Exception as err:  # noqa: BLE001 - 诊断落盘失败不能影响请求
        return f"<落盘失败: {err}>"


def _request_channel(request) -> str:
    """来源通道：public = Cloudflare 隧道进来的公网请求，local = 内网/本机直连。

    判据（任一命中即公网）：
      - cloudflared 注入的 cf-connecting-ip / cf-ray / cdn-loop 头；
      - Host 等于当前隧道域名（自定义域名 + quick 隧道都覆盖）。
    只用于台账分组（看板「公网 / 内网 / 合计」），**不参与鉴权与限流判定**：
    这些头客户端也能伪造，但伪造它只会把自己的用量记到公网那一栏。
    """
    headers = request.headers
    if headers.get("cf-connecting-ip") or headers.get("cf-ray") or headers.get("cdn-loop"):
        return "public"
    host = (headers.get("host") or "").split(":")[0].strip().lower()
    if host:
        from ..tunnel import tunnel

        public = tunnel.public_url().strip().lower()
        if public:
            public_host = public.split("://", 1)[-1].split("/", 1)[0]
            if public_host and host == public_host:
                return "public"
    return "local"


def _normalize_body(body: dict) -> dict:
    model = body.get("model")
    if isinstance(model, str) and "/" in model:
        model = "/".join(model.split("/")[1:])
    if isinstance(model, str):
        model = MODEL_NAME_MAP.get(model.lower(), model)
        body["model"] = model

    # 上游对 max_tokens 有硬校验（400 code 1210），钳制到合法区间并记录钳制动作
    raw = body.get("max_tokens")
    if raw is not None and not isinstance(raw, bool):
        try:
            mt = int(float(raw))
        except (TypeError, ValueError):
            mt = None
        if mt is not None:
            clamped = max(1, min(mt, constants.MAX_TOKENS_LIMIT))
            if clamped != mt:
                logs.warn("gateway", f"max_tokens {mt} 超出上游范围 [1,{constants.MAX_TOKENS_LIMIT}]，钳制为 {clamped}")
            body["max_tokens"] = clamped

    messages = body.get("messages")
    if isinstance(messages, list):
        bridged = []
        for msg in messages:
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                bridged.append({**msg, "content": [{"type": "text", "text": msg["content"]}]})
            else:
                bridged.append(msg)
        body["messages"] = bridged
    return body


def _is_captcha_error(text: str) -> bool:
    low = text.lower()
    return "captcha" in low or "verify token" in low or "verify failed" in low


def _is_model_unavailable(status_code: int, business_code: int | None, text: str) -> bool:
    """上游是否在说「这个账号的这个模型不可用」（→ 换号，全池如此则换模型）。

    1005 = 额度用尽，3006 = 模型不在套餐内。两者都不是账号故障（账号对其他模型
    仍然可用），所以不能走 `_is_exhausted` 把整号踢出轮询；也不能回 4xx 给客户端
    ——池子里往往还有别的模型能用。3009（并发租约）不在此列，它属于「等一下就好」。

    429 / 405 一律不算：429 是频控信号（body 里带额度文案也照旧按频控重试，
    与 `_is_exhausted` 同一原则），405 是风控——把频控当成「模型全池不可用」会
    白白把一个好模型判死 TTL 那么久。
    """
    if status_code == 429 or status_code in constants.RISK_CONTROL_HTTP_STATUSES:
        return False
    if business_code in constants.MODEL_UNAVAILABLE_CODES:
        return True
    low = (text or "").lower()
    if any(marker in low for marker in constants.MODEL_UNAVAILABLE_MARKERS):
        return True
    return any(f'"code":{code}' in low or f'"code": {code}' in low
               for code in constants.MODEL_UNAVAILABLE_CODES)


def _fallback_disabled(headers) -> bool:
    """单请求关掉模型兜底：`x-zcode-no-fallback: 1` 或 `x-zcode-model-fallback: off`。

    有些客户端（评测、回归对比）必须拿到自己点的那个模型，宁可失败也不要被换。
    """
    if not headers:
        return False
    raw = str(headers.get("x-zcode-no-fallback") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    mode = str(headers.get("x-zcode-model-fallback") or "").strip().lower()
    return mode in ("off", "0", "false", "none", "disable", "disabled")


def _model_headers(up: _Upstream) -> dict:
    """如实回报实际服务的模型（换了模型时客户端能看见）。"""
    headers: dict[str, str] = {}
    if up.model:
        headers["x-zcode-model"] = up.model
    if up.fallback_from:
        headers["x-zcode-model-requested"] = up.fallback_from
    return headers


def _detect_captcha_challenge(resp: httpx.Response, text: str | None = None) -> str | None:
    """验证码挑战双检测（对齐 zapi handler.ts）。

    三种形态：
      1. 响应头 x-aliyun-captcha-verify-param 存在（官方挑战信号）
      2. HTTP 400/403 + body {"code":3007}（2026-08 观测的 body 内挑战）
      3. HTTP 403 + 文案 captcha/verify（老检测，保留兼容）
    返回挑战标记（非 None 即挑战），否则 None。
    """
    # 1) challenge 响应头
    header_val = resp.headers.get(constants.CAPTCHA_HEADER)
    if header_val and header_val.strip():
        return "header"

    if text is None:
        return None
    low = text.lower()

    # 2) body code 3007（400/403 任意状态）
    if resp.status_code in (400, 403) and any(m in text for m in constants.CAPTCHA_BODY_MARKERS):
        return "in-body-3007"

    # 3) 403 + 挑战文案
    if resp.status_code == 403 and _is_captcha_error(low):
        return "text"

    return None


def _is_exhausted(status_code: int, text: str) -> bool:
    # 429 是频控信号，优先于一切 body 关键词：429 body 带额度文案时
    # （api.z.ai 实测形态）必须走频控重试，不得判成额度耗尽踢号。
    if status_code == 429:
        return False
    if status_code in constants.EXHAUST_HTTP_STATUSES:
        return True
    low = text.lower()
    return any(k in low for k in _EXHAUST_KEYWORDS)


def _is_risk_control(status_code: int, text: str) -> bool:
    """风控信号判定（3012「unusual activity」/ messages 端点 405）。

    与验证码挑战互斥：调用点已先排除 challenge 形态。命中即账号级风控，
    需指数退避冷却，而非直接回传客户端错误（会导致下次立刻重打、加剧风控）。
    """
    if status_code in constants.RISK_CONTROL_HTTP_STATUSES:
        return True
    low = text.lower()
    return any(m.lower() in low for m in constants.RISK_CONTROL_MARKERS)


def _parse_retry_after(value: str | None) -> int | None:
    """解析 Retry-After（仅秒数形态；HTTP-date 形态少见，放弃即用默认重试等待）。

    非正数不采信；超长值封顶采信 —— 尊重上游意图的同时防止把客户端吊死。
    """
    if not value:
        return None
    try:
        secs = int(float(value.strip()))
    except (ValueError, AttributeError):
        return None
    return min(secs, settings.RETRY_429_WAIT_MAX) if secs > 0 else None


def _mark(account: Account, status_value: str, error: str | None = None) -> None:
    account.status = status_value
    account.last_error = error
    if status_value == Status.COOLING:
        account.cooling_until = time.time() + settings.COOLING_SECONDS
    store.update_account(account)


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text", "")
    return ""


@router.get("/v1/models", dependencies=[Depends(verify_gateway_key)])
async def list_models():
    """列出可用模型（Anthropic /v1/models 风格）。"""
    return {
        "object": "list",
        "data": [
            {"id": i, "type": "model", "display_name": i, "created_at": "2025-01-01T00:00:00Z"}
            for i in AVAILABLE_MODELS
        ],
    }


@router.post("/v1/messages", dependencies=[Depends(verify_gateway_key)])
async def messages(request: Request):
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request"}}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}},
            status_code=400,
        )

    incoming_headers = dict(request.headers)
    provider = _detect_provider(body, request.headers)
    requested_model = str(body.get("model") or "")   # 归一化前客户端点的模型
    body = _normalize_body(body)
    # 验证码页面由本服务托管，端口取实际请求端口（兼容任意启动端口）
    port = request.url.port or settings.PORT

    req_id = secrets.token_hex(8)
    logs.req(req_id, str(body.get("model") or "-"), bool(body.get("stream")), _last_user_text(body))
    reqlog.begin(req_id, "messages", str(body.get("model") or "-"),
                 bool(body.get("stream")), _last_user_text(body),
                 provider=provider, requested_model=requested_model,
                 client=request.headers.get("user-agent", ""),
                 channel=_request_channel(request))

    try:
        result = await _dispatch(req_id, body, incoming_headers, port, provider,
                                 affinity.key_of(body),
                                 no_fallback=_fallback_disabled(request.headers),
                                 pin=_pinned_prefer(provider, request.headers))
    except asyncio.CancelledError:
        # 客户端在调度期间断开（429 重试/验证码等待可达数分钟）——CancelError
        # 是 BaseException，不兜底会让监控条目永久滞留「进行中」
        reqlog.finish_error(req_id, "客户端断开", status=499,
                            outcome=reqlog.OUTCOME_CLIENT_ABORTED)
        raise
    except Exception as err:  # noqa: BLE001 - 调度层意外异常也要收口监控条目
        reqlog.finish_error(req_id, f"网关内部错误: {err}", status=500)
        return JSONResponse(
            {"error": {"message": "网关内部错误", "type": "internal_error"}},
            status_code=500,
        )
    if isinstance(result, _Upstream):
        # 流式重试上下文：只在尚未向客户端出字时重开上游，工厂重走完整调度
        session = _StreamSession(
            lambda: _dispatch(req_id, body, incoming_headers, port, provider,
                              affinity.key_of(body),
                              no_fallback=_fallback_disabled(request.headers),
                              pin=_pinned_prefer(provider, request.headers))
        )
        # dispatch 返回与流式生成器启动之间的取消窗口：兜底关闭释放并发槽位
        try:
            return result.to_streaming(req_id, session)
        except asyncio.CancelledError:
            await result.close()
            raise
    return result


@router.post("/v1/chat/completions", dependencies=[Depends(verify_gateway_key)])
async def chat_completions(request: Request):
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": {"message": "请求体不是合法 JSON", "type": "invalid_request_error"}}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": {"message": "请求体必须是 JSON 对象", "type": "invalid_request_error"}}, status_code=400)

    body, err = openai_to_anthropic(payload)
    if err or body is None:
        return JSONResponse({"error": {"message": err or "请求体不合法", "type": "invalid_request_error"}}, status_code=400)

    incoming_headers = dict(request.headers)
    provider = _detect_provider(body, request.headers)
    body = _normalize_body(body)
    port = request.url.port or settings.PORT

    req_id = secrets.token_hex(8)
    logs.req(req_id, str(body.get("model") or "-"), bool(payload.get("stream")), _last_user_text(body))
    reqlog.begin(req_id, "chat", str(body.get("model") or "-"),
                 bool(payload.get("stream")), _last_user_text(body),
                 provider=provider, requested_model=str(payload.get("model") or ""),
                 client=request.headers.get("user-agent", ""),
                 channel=_request_channel(request))

    try:
        result = await _dispatch(req_id, body, incoming_headers, port, provider,
                                 affinity.key_of(body),
                                 no_fallback=_fallback_disabled(request.headers),
                                 pin=_pinned_prefer(provider, request.headers))
    except asyncio.CancelledError:
        reqlog.finish_error(req_id, "客户端断开", status=499,
                            outcome=reqlog.OUTCOME_CLIENT_ABORTED)
        raise
    except Exception as err:  # noqa: BLE001 - 调度层意外异常也要收口监控条目
        reqlog.finish_error(req_id, f"网关内部错误: {err}", status=500)
        return JSONResponse(
            {"error": {"message": "网关内部错误", "type": "internal_error"}},
            status_code=500,
        )
    if not isinstance(result, _Upstream):
        return result

    # 兜底换过模型时这里是**实际服务**的模型（客户端点的是 requested_model）
    model = str(body.get("model") or "")
    if payload.get("stream"):
        session = _StreamSession(
            lambda: _dispatch(req_id, body, incoming_headers, port, provider,
                              affinity.key_of(body),
                              no_fallback=_fallback_disabled(request.headers),
                              pin=_pinned_prefer(provider, request.headers))
        )
        try:
            return _openai_stream_response(result, model, req_id, session)
        except asyncio.CancelledError:
            await result.close()
            raise

    try:
        raw = await result.read()
        logs.req_ok(req_id)
    except asyncio.CancelledError:
        reqlog.finish_error(req_id, "客户端断开", status=499, t_first=result.t_first,
                            outcome=reqlog.OUTCOME_CLIENT_ABORTED)
        raise
    except Exception as err:  # noqa: BLE001
        logs.req_err(req_id, f"读取上游响应失败: {err}")
        reqlog.finish_error(req_id, f"读取上游响应失败: {err}", status=502,
                            outcome=reqlog.OUTCOME_UPSTREAM_ABORTED)
        return JSONResponse({"error": {"message": f"读取上游响应失败: {err}", "type": "upstream_error"}}, status_code=502)
    finally:
        await result.close()
    data = _safe_json(raw.decode("utf-8", "ignore"))
    if not isinstance(data, dict) or data.get("type") != "message":
        reqlog.finish_error(req_id, "上游响应格式异常", status=502, t_first=result.t_first,
                            outcome=reqlog.OUTCOME_UPSTREAM_ABORTED)
        return JSONResponse({"error": {"message": "上游响应格式异常", "type": "upstream_error"}}, status_code=502)
    usage = data.get("usage") or {}
    reqlog.mark_first(req_id)
    reqlog.mark_last(req_id)
    reqlog.note_usage(req_id, **_usage_kwargs(usage))
    reqlog.finish_ok(req_id, t_first=result.t_first, status=result.resp.status_code)
    return JSONResponse(anthropic_to_openai(data, model), headers=_model_headers(result))


def _usage_kwargs(usage: dict) -> dict:
    """上游 usage 块 → reqlog.note_usage 的关键字参数（缺字段就不传）。"""
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cached_tokens": usage.get("cache_read_input_tokens", usage.get("cached_tokens")),
        "credit": usage.get("credit") if isinstance(usage.get("credit"), (int, float)) else None,
    }


def _openai_stream_response(up: _Upstream, model: str, req_id: str,
                            session: "_StreamSession | None" = None) -> StreamingResponse:
    """把上游 Anthropic SSE 事件流转换为 OpenAI chunk 流。

    上游中途断开（incomplete chunked read）且尚未产出任何内容时，经 session
    透明重开上游；已经输出过内容则不能重放（会重复），发 error chunk 后收口。
    """
    conv = StreamConverter(model)
    start_chunk = conv.start()

    async def _iter():
        nonlocal up, conv
        emitted = False
        try:
            yield start_chunk
            while True:
                try:
                    async for line in _paced(up.lines()):
                        if line is _KEEPALIVE:
                            yield line  # 上游静默期的心跳，原样透给客户端
                            continue
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str:
                            continue
                        evt = _safe_json(data_str)
                        if isinstance(evt, dict):
                            out = conv.feed(evt)
                            if out:
                                emitted = True
                                reqlog.mark_first(req_id)
                                reqlog.mark_last(req_id)
                            for chunk in out:
                                yield chunk
                    yield conv.done()
                    logs.req_ok(req_id)
                    reqlog.note_usage(req_id, input_tokens=conv.usage.get("prompt_tokens"),
                                      output_tokens=conv.usage.get("completion_tokens"),
                                      cached_tokens=conv.usage.get("cached_tokens"))
                    reqlog.finish_ok(req_id, t_first=up.t_first, status=up.resp.status_code)
                    return
                except asyncio.CancelledError:
                    reqlog.finish_error(req_id, "客户端断开", status=499, t_first=up.t_first,
                                        outcome=reqlog.OUTCOME_CLIENT_ABORTED)
                    raise
                except Exception as err:  # noqa: BLE001
                    if not emitted and session is not None:
                        replacement = await session.reopen(up, str(err))
                        if replacement is not None:
                            # 沿用同一个 chunk_id/created：客户端视角仍是同一条回复
                            old_id, old_created = conv.chunk_id, conv.created
                            conv = StreamConverter(model)
                            conv.chunk_id, conv.created = old_id, old_created
                            up = replacement
                            continue
                    logs.req_err(req_id, f"流传输中断: {err}")
                    reqlog.note_usage(req_id, input_tokens=conv.usage.get("prompt_tokens"),
                                      output_tokens=conv.usage.get("completion_tokens"),
                                      cached_tokens=conv.usage.get("cached_tokens"))
                    reqlog.finish_error(req_id, f"流传输中断: {err}", t_first=up.t_first,
                                        outcome=reqlog.OUTCOME_UPSTREAM_ABORTED)
                    yield _sse_error_chunk()
                    return
        finally:
            await up.close()

    return StreamingResponse(_iter(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", **_model_headers(up)})


def _sticky_prefer(provider: str) -> str:
    """全局粘号候选（未开或没有记住时返回 ""，调用方自然落回轮询）。"""
    if not store.account_sticky_enabled():
        return ""
    return store.sticky_account(provider) or ""


def _pinned_prefer(provider: str, headers) -> str:
    """显式指定账号（请求头 x-zcode-account: <账号 id 或 name>）。

    烧额度/排障用：把一条车道钉在指定账号上，上游前缀缓存才能稳定命中。
    仅在网关密钥鉴权通过后生效；找不到账号时返回 ""，调用方回落到会话粘性 /
    全局粘号 / 轮询。该头属于 x-zcode-* 族，agent 层不会透传到上游。
    """
    try:
        raw = str(headers.get("x-zcode-account") or "").strip()
    except Exception:  # noqa: BLE001 - header 容器形态异常时按未指定处理
        return ""
    if not raw:
        return ""
    # 账号 id 形如「寂川-1f359bea」含中文，HTTP 头只能是 latin-1/ASCII，
    # 客户端需 percent-encode；纯 ASCII 的 id 原样也能解。
    from urllib.parse import unquote

    try:
        raw = unquote(raw)
    except Exception:  # noqa: BLE001
        pass
    acc = store.find(provider, raw)
    return acc.id if acc is not None else ""


async def _dispatch(req_id, body, incoming_headers, port, provider, conv_key: str = "",
                    no_fallback: bool = False, pin: str = ""):
    """调度：先按模型兜底链逐模型试，每个模型内部再多账号轮询。

    返回 _Upstream（成功）或 JSONResponse（错误）。

    **第一层：换模型**（app/modelpool.py）。额度是按「账号 × 模型」计的，所以
    「所有账号的这个模型都没了」是一个正常状态，而不是故障：客户端要 GLM-5.3，
    池子里 GLM-5.3 全空但 GLM-5.3-Flash 还有额度时，直接换过去服务，客户端无感。
    只有整轮每个账号都被上游判成「这个模型不可用」（1005 / 3006）才算该模型全池
    不可用——并发满（3009）、限流、风控、鉴权失败都照旧只换号，绝不换模型。

    **第二层：换号**（_dispatch_model）。选号顺序：会话粘性记住的账号优先（同一段
    对话回到同一账号，上游前缀缓存才能继续命中），否则 round-robin。单账号并发
    限制：选号后若该账号在飞请求已达上限（store.account_concurrency，0 = 不限），
    先换下一个账号；**一圈下来每个号都满**时不再直接 503，而是排队等任一槽释放
    （QUEUE_WAIT_MAX，0 = 不排队）。满号跳过不计入 MAX_ACCOUNT_ATTEMPTS（只计
    真正进入 _try_account 的次数）。无号可用 / 排队超时 / 排队已满 → 503。

    模型级并发（上游 3009）：实测「上一个请求刚结束的 1–2 秒内」再发，所有账号
    都会回 3009——那是上游按模型计的并发租约还没释放，不是账号坏了。此时直接
    回 503 太浪费，整轮短等再来（合计最多 MODEL_BUSY_ROUNDS × MODEL_BUSY_WAIT）；
    只有整轮失败**全部**是 3009 才这样重试，别的原因照旧立刻收口。
    """
    requested = str(body.get("model") or "")
    models = [requested] if no_fallback else modelpool.candidates(provider, requested)
    if not models:
        models = [requested]
    # 选号优先级：显式钉号（烧额度车道）> 会话粘性（这段对话上次谁答的，
    # 上游前缀缓存能命中）> 全局粘号（上次成功服务的是谁）> 轮询。
    # 粘号候选不可用时 store.select 会自然落到轮询，这里只负责给出"想让谁上"。
    prefer = pin or affinity.prefer(provider, conv_key) or _sticky_prefer(provider)
    # 换号原因的出口：最后一个账号为什么不可用（模型额度用尽等），503 时如实回给客户端
    reason: list[str] = [""]
    dead_models: list[str] = []

    for idx, model in enumerate(models):
        # 必须无条件改写 body["model"]：请求模型本身被判死时 models[0] 就已经是兜底
        # 模型了（candidates 会把 dead 的剔掉），只在 idx>0 时改写会让这一轮仍然打
        # 原来那个模型——上游继续回 1005，兜底形同虚设（probe 里踩到过）。
        body["model"] = model
        if model != requested:
            reqlog.mark_model(req_id, model)
            # 上一轮真正判死的那个模型（链上第 2 个之后就不是 requested 了）；
            # 请求模型自己一开始就被判死时没有"上一轮"的上游原因，用 modelpool
            # 记住的判死理由，别只写一句"模型不可用"
            previous = dead_models[-1] if dead_models else requested
            why = reason[0] or modelpool.describe(provider, previous) or "模型不可用"
            logs.warn(
                req_id,
                f"{previous} 全池不可用（{why}），"
                f"自动切换到 {model}（第 {idx + 1}/{len(models)} 个模型）",
            )
        reason[0] = ""
        result = await _dispatch_model(
            req_id, body, incoming_headers, port, provider, prefer, conv_key, reason,
        )
        if isinstance(result, _Upstream):
            modelpool.revive(provider, model)   # 实测可用：清死标记，记信任票
            result.model = model
            result.fallback_from = requested if model != requested else ""
            return result
        if result is _MODEL_DEAD:
            modelpool.mark_dead(provider, model, reason[0] or "模型不可用")
            dead_models.append(model)
            continue
        if result is None:
            break       # 失败原因不是「模型不可用」：换模型救不了，收口
        return result

    detail = reason[0] or "无可用账号 / 额度均已耗尽 / 并发已满"
    if dead_models:
        detail = f"{'、'.join(dead_models)} 全池不可用；{detail}"
    logs.req_err(req_id, detail)
    reqlog.finish_error(req_id, detail, status=503)
    message = detail or "所有账号均不可用、额度已用完或并发已满，请在后台检查账号状态"
    return JSONResponse(
        {"error": {"message": message, "type": "no_available_account"}},
        status_code=503,
    )


# ── 排队：全池并发占满时等一个槽空出来 ───────────────────────────────────────
# 请求挂在 future 上等，不占 _inflight、不碰上游连接；槽释放即时唤醒重扫。
# 抢到槽的那一路是在同步段里 check+inc 的（asyncio 单线程），不会出现多个等待者
# 同时拿到同一个槽。唤醒是「叫醒所有人重扫」而不是逐个分配：waiters 数量有
# QUEUE_MAX_WAITERS 上限，重扫的开销可以忽略。
_slot_waiters: set[asyncio.Future] = set()

# 单次等待的兜底上限：唤醒是即时的，这里只防「漏唤醒」（比如释放方在别的线程）
# 把请求永久挂住——超时醒来最多多扫一遍池子。
_QUEUE_POLL = 5.0


def _wake_slot_waiters() -> None:
    while _slot_waiters:
        fut = _slot_waiters.pop()
        if fut.done():
            continue
        try:
            fut.set_result(None)
        except Exception:  # noqa: BLE001 - 唤醒失败由 _QUEUE_POLL 兜底
            pass


async def _wait_slot_turn(timeout: float) -> bool:
    """等一个并发槽释放；返回 False 表示等满 timeout 仍无空位。"""
    fut = asyncio.get_running_loop().create_future()
    _slot_waiters.add(fut)
    try:
        await asyncio.wait_for(fut, timeout)
        return True
    except TimeoutError:
        return False
    finally:
        _slot_waiters.discard(fut)
        if not fut.done():
            fut.cancel()


def _pool_unavailable_reason(provider: str) -> str:
    """池子里一个可轮询账号都没有时，给出**具体原因**（而不是笼统的 503 文案）。

    典型场景（2026-11-02 23:44 实测）：上游整片回 502/503，所有账号被 5xx
    保护冻进 cooling——后台额度都正常，客户端却只收到「无可用账号/额度均已
    耗尽/并发已满」，极易误判成账号或额度坏了。
    """
    accounts = [a for a in store.list_accounts(provider) if a.enabled]
    if not accounts:
        return "没有启用的账号"
    now = time.time()
    cooling = [a for a in accounts if a.is_cooling(now)]
    if len(cooling) == len(accounts):
        left = max(0, min(float(a.cooling_until or 0) for a in cooling) - now)
        return f"所有账号冷却中（上游 5xx 保护），约 {int(left)}s 后自动恢复"
    if all(a.status == Status.EXHAUSTED for a in accounts):
        return "所有账号额度已耗尽（等待额度重置或领取赠送）"
    if all(a.status in (Status.INVALID, Status.DISABLED) for a in accounts):
        return "所有账号凭证失效或已停用，请在后台重新授权"
    bad = [a.name for a in accounts
           if a.status in (Status.INVALID, Status.DISABLED, Status.EXHAUSTED)
           or a.is_cooling(now)]
    if bad:
        head = "、".join(bad[:5]) + (f" 等 {len(bad)} 个账号" if len(bad) > 5 else "")
        return f"{head}不可用"
    return "无可用账号（检查后台账号状态/启用开关）"


_last_pool_probe = 0.0


def _pool_probe_account(provider: str) -> Account | None:
    """全池 5xx 冷却时的半开探测：每 COOLING_PROBE_INTERVAL 秒放一个请求试探。

    只对「所有启用账号都因上游 5xx 在冷却」生效——风控/人工冷却绝不绕过。
    探测前先放开这个号的冷却（半开）：成功由 _try_account 正常洗成 active；
    失败会重新冷却，下一次探测再等一个间隔。这样上游恢复后最多 ~30s 解冻，
    而不是硬等满 COOLING_SECONDS。
    """
    global _last_pool_probe
    now = time.time()
    interval = max(1, int(getattr(settings, "COOLING_PROBE_INTERVAL", 30)))
    if now - _last_pool_probe < interval:
        return None
    enabled = [a for a in store.list_accounts(provider) if a.enabled]
    cooling = [a for a in enabled if a.is_cooling(now)]
    if not enabled or len(cooling) != len(enabled):
        return None
    # last_error 由 5xx 分支写成「上游 HTTP 502/503 ... 冷却」；风控/连接失败的
    # 冷却不得用探测绕过
    five_xx = [a for a in cooling if "上游 HTTP 5" in (a.last_error or "")]
    if len(five_xx) != len(cooling):
        return None
    _last_pool_probe = now
    probe = min(five_xx, key=lambda a: a.cooling_until or 0)
    probe.cooling_until = None
    if probe.status == Status.COOLING:
        probe.status = Status.ACTIVE
    probe.last_error = None
    store.update_account(probe)
    logs.warn("gateway", f"全池因上游 5xx 冷却，半开探测账号 {probe.name}"
                         f"（每 {interval}s 一次）")
    return probe


async def _dispatch_model(req_id, body, incoming_headers, port, provider, prefer, conv_key,
                          reason: list[str]):
    """单个模型内的多账号轮询。

    返回 _Upstream / JSONResponse（其它错误直接回传）/ _NEXT_MODEL 语义的
    `_MODEL_DEAD`（整轮每个账号都是「这个模型不可用」）/ None（失败但非模型问题）。

    满号处理：一圈扫下来每个号都在飞（attempts == 0 且 skipped_busy > 0）时按
    QUEUE_WAIT_MAX 排队等槽（0 = 不排队直接返回 None → 503），排到某号空出就
    重扫；超时或排队人数超过 QUEUE_MAX_WAITERS 才收口，原因写进 reason[0]。
    """
    limit = _limit()
    # 换号轨迹的记账本：上一个真正尝试过的账号 + 它为什么被放弃。
    # 记在调度层而不是每个错误分支里，才能保证「从谁换成谁、为什么」必然成对出现
    #（错误分支有 19 处，逐处记漏一处轨迹就断链）。
    prev_name = ""
    prev_why = ""
    guard_logged = False   # 风控最后一格保护：一次请求只告警一次，别刷屏

    # 排队预算：只有「一圈下来每个号都在飞」（attempts == 0 且有号被满号跳过）
    # 才启用；计的是进入调度以来的总等待，与 3009 短等轮次互不消耗。
    queue_wait_max = float(settings.QUEUE_WAIT_MAX or 0)
    queue_deadline = (time.monotonic() + queue_wait_max) if queue_wait_max > 0 else 0.0
    queue_logged = False
    queue_since = 0.0            # 首次进入排队的时刻（0 = 没排过队）
    model_rounds = 0             # 3009 短等已用轮次

    while True:
        tried: set[str] = set()
        attempts = 0
        busy_hits = 0
        model_hits = 0
        skipped_busy = 0         # 因为本账号并发满而跳过的次数
        guarded = 0              # 因为「风控次数到阈值」被保护性跳过的次数
        # 「这一轮该问几个账号」的独立口径：不能靠 select 返回 None 来判断问完了
        # ——账号数正好等于 MAX_ACCOUNT_ATTEMPTS 时，循环先被 attempts 上限截断，
        # 永远等不到那个 None（实测 5 个账号时兜底因此完全不触发）。
        pool_size = store.count_selectable(provider)

        while attempts < MAX_ACCOUNT_ATTEMPTS:
            account = store.select(provider, skip_ids=tried, prefer=prefer)
            if account is None:
                # 全池 5xx 冷却：半开探测（每 COOLING_PROBE_INTERVAL 秒最多一个）
                account = _pool_probe_account(provider) if attempts == 0 else None
                if account is None:
                    break
            tried.add(account.id)
            if limit > 0 and _inflight.get(account.id, 0) >= limit:
                # 粘号在忙：**等它**而不是跳到别的号去。用户要的就是「正常情况下
                # 一直用一个账号」——这一路请求只是排在它后面，而不是被赶到另一个
                # 账号（那会让上游缓存换一份、后台看起来一直在跳号）。
                # 只有粘号自己值得等：其它号满了照旧换下一个。
                if (prefer and account.id == prefer and queue_deadline
                        and time.monotonic() < queue_deadline
                        and len(_slot_waiters) < settings.QUEUE_MAX_WAITERS):
                    left = queue_deadline - time.monotonic()
                    if not queue_logged:
                        queue_logged = True
                        queue_since = time.monotonic()
                        logs.warn(
                            req_id,
                            f"粘号 {account.name} 并发已满（{_inflight.get(account.id, 0)}/{limit}），"
                            f"排队等它（最多 {queue_wait_max:g}s）而不是换号",
                        )
                    tried.discard(account.id)   # 下一轮还要能选中它
                    await _wait_slot_turn(min(left, _QUEUE_POLL))
                    continue
                # 排队期间每轮重扫都会再扫到这些满号：只报第一条，别把日志刷满。
                if not queue_logged:
                    logs.warn(req_id, f"账号 {account.name} 并发已满（{_inflight.get(account.id, 0)}/{limit}），切换下一个")
                skipped_busy += 1
                continue
            if prev_name and prev_name != account.name:
                # 前一个账号被放弃、换这一个：把原因记进请求轨迹（面板悬停可查）。
                reqlog.mark_switch(req_id, prev_name, account.name,
                                   prev_why or reason[0] or "上一账号不可用")
            # 风控最后一格保护：strikes 逼近禁用阈值（默认 3/4）就不再打上游。
            # 上游规则变化时（3012 频发）客户端会连续重试，不拦就会把整池推进
            # 「禁用 + 自动换设备指纹」，恢复成本远高于暂时 503。
            guard = settings.RISK_STRIKE_GUARD
            if guard > 0 and account.risk_strikes >= guard:
                quiet = time.time() - (account.last_risk_at or 0)
                if quiet < settings.RISK_STRIKE_GUARD_WINDOW:
                    guarded += 1
                    reason[0] = (
                        f"账号 {account.name} 风控 {account.risk_strikes}/"
                        f"{settings.RISK_BAN_STRIKES} 次，{int(settings.RISK_STRIKE_GUARD_WINDOW - quiet)}s 内"
                        f"为免升级禁用已暂停打上游（冷却后重试，或在后台换号）"
                    )
                    if not guard_logged:
                        guard_logged = True
                        logs.warn(req_id, reason[0])
                    continue
            prev_name = account.name
            prev_why = ""
            attempts += 1
            needs_captcha = provider == "zai" and account.uses_plan_channel()

            slot_box: list[str | None] = [None]
            if limit > 0:
                _inflight[account.id] = _inflight.get(account.id, 0) + 1
                slot_box[0] = account.id
            try:
                result = await _try_account(
                    req_id, account, body, incoming_headers, port, needs_captcha, slot_box,
                    reason,
                )
            except BaseException:
                if slot_box[0] is not None:
                    _release_slot(slot_box[0])
                    slot_box[0] = None
                raise
            prev_why = reason[0]
            if result is _NEXT_BUSY:
                busy_hits += 1
            elif result is _NEXT_MODEL:
                model_hits += 1
            if result is _NEXT_ACCOUNT or result is _NEXT_BUSY or result is _NEXT_MODEL:
                if slot_box[0] is not None:
                    _release_slot(slot_box[0])
                    slot_box[0] = None
                continue
            if isinstance(result, _Upstream):
                affinity.remember(provider, conv_key, account.id)
                # 全局粘号：这次成功证明了「这个号现在能用」，把全局粘号钉到它上面，
                # 后续别的会话也优先用它——而不是轮询把流量摊开（上游按账号 × 模型
                # 计并发租约，摊开换不来更高并发，只会看起来一直在跳号）。
                if store.account_sticky_enabled():
                    store.set_sticky_account(provider, account.id)
                held = slot_box[0]
                slot_box[0] = None
                if held is not None:
                    result.on_close = _make_slot_releaser(held)
                if queue_since:
                    logs.ok(
                        req_id,
                        f"排队 {time.monotonic() - queue_since:.1f}s 后拿到并发槽"
                        f"（{account.name}）",
                    )
                return result
            if slot_box[0] is not None:
                _release_slot(slot_box[0])
                slot_box[0] = None
            return result

        # ① 一圈下来每个号都在飞：这不是故障，是排队信号。请求还没碰上游
        #   （attempts == 0），挂起等任一槽释放，比 503 让客户端自己重试更好。
        if skipped_busy and attempts == 0:
            reason[0] = f"所有账号并发已满（上限 {limit}/号）"
            if queue_deadline:
                left = queue_deadline - time.monotonic()
                if left <= 0:
                    reason[0] = (
                        f"所有账号并发已满（上限 {limit}/号），"
                        f"排队 {queue_wait_max:g}s 未等到空位"
                    )
                    return None
                if len(_slot_waiters) >= settings.QUEUE_MAX_WAITERS:
                    reason[0] = (
                        f"所有账号并发已满（上限 {limit}/号），"
                        f"排队已满（{len(_slot_waiters)}/{settings.QUEUE_MAX_WAITERS}）"
                    )
                    return None
                if not queue_logged:
                    queue_logged = True
                    queue_since = time.monotonic()
                    logs.warn(
                        req_id,
                        f"全体账号并发已满（上限 {limit}/号），"
                        f"排队等待空位（最多 {queue_wait_max:g}s）",
                    )
                # 槽释放会立刻唤醒；_QUEUE_POLL 只是漏唤醒时的兜底重扫
                await _wait_slot_turn(min(left, _QUEUE_POLL))
                continue
            return None

        # ② 试过的号全回 3009（模型并发租约还没释放）才值得等一下再来
        if busy_hits and busy_hits == attempts and model_rounds < settings.MODEL_BUSY_ROUNDS:
            model_rounds += 1
            logs.warn(
                req_id,
                f"模型 {body.get('model') or '-'} 都在上游并发上限内（3009），"
                f"{settings.MODEL_BUSY_WAIT:g}s 后重试整轮"
                f"（{model_rounds}/{settings.MODEL_BUSY_ROUNDS}）",
            )
            await _sleep(settings.MODEL_BUSY_WAIT)
            continue
        # 整轮每个账号都被判「这个模型不可用」→ 交给 _dispatch 换模型。
        # 两个前提缺一不可：真的把可轮询的账号都问过了（attempts >= pool_size），
        # 且没有账号因为本账号并发满而被跳过（skipped_busy）——那两种情况只是
        # 「没问到」，不足以断言模型全池不可用，贸然判死会把流量赶去更弱的模型。
        if model_hits and model_hits == attempts and attempts >= pool_size and not skipped_busy:
            return _MODEL_DEAD
        # 一个号都没问到（全在冷却/耗尽/失效）：把池子的真实状态写进 reason，
        # 503 回给客户端时就能直接说清「是上游 5xx 冷却，不是账号没了」
        if attempts == 0 and not reason[0]:
            reason[0] = _pool_unavailable_reason(provider)
        break
    return None


def _release_slot(account_id: str) -> None:
    n = _inflight.get(account_id, 0) - 1
    if n <= 0:
        _inflight.pop(account_id, None)
    else:
        _inflight[account_id] = n
    # 排队者只可能因为「每个号都满」在等；任一槽释放就该重扫一遍池子
    if _slot_waiters:
        _wake_slot_waiters()


def _park_slot(slot_box: list[str | None] | None) -> None:
    """等待（429/验证码/5xx）前释放并发槽，避免把账号冻住数分钟。"""
    if slot_box and slot_box[0] is not None:
        _release_slot(slot_box[0])
        slot_box[0] = None


def _reacquire_slot(account: Account, slot_box: list[str | None] | None) -> bool:
    """等待结束后重新占槽；占不到则让调用方换号。"""
    if slot_box is None:
        return True
    limit = _limit()
    if limit <= 0:
        return True
    if _inflight.get(account.id, 0) >= limit:
        return False
    _inflight[account.id] = _inflight.get(account.id, 0) + 1
    slot_box[0] = account.id
    return True


def _make_slot_releaser(account_id: str):
    def _release() -> None:
        _release_slot(account_id)
    return _release


_NEXT_ACCOUNT = object()
# 换号的另一种原因：这个账号卡的只是「模型并发」（上游 3009），等一两秒就能再用。
# 单独一个哨兵，_dispatch 才知道整轮失败是否值得短等重来。
_NEXT_BUSY = object()
# 换模型的原因：这个账号的**这个模型**不可用（1005 额度用尽 / 3006 不在套餐）。
# 整轮都是它 → 这个模型全池不可用，_dispatch 换下一个模型。
_NEXT_MODEL = object()
# _dispatch_model 的整轮结论：该模型全池不可用（换模型）。
_MODEL_DEAD = object()


# fire-and-forget 后台任务强引用：事件循环对 task 只持弱引用，裸 create_task
# 会被 GC 中途静默丢弃（同 main.py 启动安装序已修过的缺陷，2026-09 review）。
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# 账号并发限制：account_id → 在飞请求数。asyncio 单线程事件循环下
# check+inc 原子；释放走 _Upstream.close / 失败路径，泄漏面在测试钉住。
_inflight: dict[str, int] = {}


def _limit() -> int:
    """当前并发上限（0 = 不限），实时读取设置（后台改后即生效）。"""
    return store.account_concurrency()


# SSE 注释帧：所有 SSE 客户端都会跳过，它只表示「连接还活着」。
# 结尾不留空行——有些客户端（openai-go v2）把空行当事件解析，会报 JSON 错
# （magpie 的 sseWriter.comment 同样只写 ": keepalive\n"）。
_KEEPALIVE = ": keep-alive\n"


async def _paced(source, idle: float | None = None, stall: float | None = None):
    """把上游分块流转发给客户端，同时做两件事。

    1. 客户端空闲满 idle 秒就补一条 SSE 注释帧。模型思考阶段上游可能只有
       thinking 事件（甚至什么都没有），没有注释帧，客户端与中间代理都无从
       判断连接是否还活着，界面只能显示「未响应」。
    2. 上游彻底静默满 stall 秒即判定连接已死并抛错。httpx 的 read 超时是
       None（长回复不能设总时长上限），缺了这层看门狗，半死的 TCP 连接会把
       请求永久挂住——客户端也就永久「未响应」。

    上游读取放在独立 task 里当生产者，消费侧只等队列：空闲超时因此不会取消
    上游的读操作（取消读会破坏 httpx 的流状态）。
    """
    idle = idle if idle is not None else settings.SSE_KEEPALIVE_IDLE
    stall = stall if stall is not None else settings.UPSTREAM_STALL_SECONDS
    queue: asyncio.Queue = asyncio.Queue()
    eof = object()

    async def pump() -> None:
        try:
            async for item in source:
                await queue.put(item)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - 读上游失败交给消费侧统一收口
            await queue.put(err)
        finally:
            await queue.put(eof)

    task = asyncio.create_task(pump())
    silent = 0.0
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), idle)
            except TimeoutError as err:
                silent += idle
                if silent >= stall:
                    raise TimeoutError(
                        f"上游 {stall:.0f}s 没有任何数据，判定连接已死"
                    ) from err
                yield _KEEPALIVE
                continue
            silent = 0.0
            if item is eof:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        task.cancel()


def _sse_error_frame() -> str:
    """上游中断的显式 SSE error 帧（Anthropic /v1/messages 协议）。

    流已经被上游掐断时，直接把 HTTP 流截断会让部分客户端把半截回复当成
    完整结果；补一条标准 error 事件，客户端能明确「这次回答不完整」并重试。
    """
    payload = {"type": "error", "error": {
        "type": "api_error", "message": "上游连接中断，输出被截断，请重试"}}
    return "event: error\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _sse_error_chunk() -> str:
    """OpenAI chat.completion.chunk 形态的错误帧。"""
    payload = {"error": {"message": "上游连接中断，输出被截断，请重试",
                         "type": "api_error"}}
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


class _StreamSession:
    """流式请求的重试上下文：只在**尚未向客户端输出任何内容**时重开上游。

    2026-11-02 实证：上游偶发在 SSE 中途断开（httpx `incomplete chunked read`）。
    已经流出去的内容不能重放（会重复输出），所以只处理「一个字都没发」的情况：
    关闭旧连接 → 重新走 _dispatch（换号/换连接/重新过验证码）→ 消费方继续。
    重试次数由 settings.STREAM_RETRY_TIMES 控制；耗尽后按上游中断如实收口。
    """

    def __init__(self, factory) -> None:
        self._factory = factory
        self._left = max(0, int(getattr(settings, "STREAM_RETRY_TIMES", 2)))
        self.attempts = 0

    async def reopen(self, failed: "_Upstream", reason: str) -> "_Upstream | None":
        try:
            await failed.close()   # 先释放旧连接的并发槽位，再抢新槽位
        except Exception as err:  # noqa: BLE001 - 关闭半死连接失败不阻断重试
            logs.warn("gateway", f"关闭中断的上游流失败: {err}")
        if self._left <= 0:
            return None
        self._left -= 1
        self.attempts += 1
        logs.warn("gateway", f"上游流未出字即中断（{str(reason)[:100]}），"
                             f"重新调度（第 {self.attempts} 次）")
        try:
            up = await self._factory()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - 重试调度失败按中断收口
            logs.warn("gateway", f"流重试调度失败: {err}")
            return None
        return up if isinstance(up, _Upstream) else None


class _Upstream:
    """已建立的上游成功流：由调用方消费并负责关闭。

    `preloaded` 是调度阶段就已经读进内存的响应体（非 SSE 的响应）：上游对业务
    错误也回 200（body 形如 `{"code":1005,"msg":"exceed quota limit"}`），要判断
    是错误还是整体回复，只能先读一次——读完就把内容挂在这里，消费方别再读流。
    """

    __slots__ = ("resp", "cm", "client", "t_first", "account_name", "mode", "on_close",
                 "preloaded", "model", "fallback_from", "_closed")

    def __init__(self, resp: httpx.Response, cm, client: httpx.AsyncClient,
                 t_first: float | None = None, account_name: str = "", mode: str = "",
                 on_close=None, preloaded: bytes | None = None,
                 model: str = "", fallback_from: str = "") -> None:
        self.resp = resp
        self.cm = cm
        self.client = client
        self.t_first = t_first
        self.account_name = account_name
        self.mode = mode
        self.on_close = on_close
        self.preloaded = preloaded
        self.model = model
        self.fallback_from = fallback_from
        self._closed = False

    async def read(self) -> bytes:
        """整体读取响应体（调度阶段已读过的直接返回，避免重复消费）。"""
        if self.preloaded is not None:
            return self.preloaded
        return await self.resp.aread()

    async def chunks(self):
        """响应体的字节流（透传路径用）。"""
        if self.preloaded is not None:
            if self.preloaded:
                yield self.preloaded
            return
        async for chunk in self.resp.aiter_bytes():
            yield chunk

    async def lines(self):
        """响应体的文本行流（OpenAI 转换路径用）。"""
        if self.preloaded is not None:
            for line in self.preloaded.decode("utf-8", "ignore").splitlines():
                yield line
            return
        async for line in self.resp.aiter_lines():
            yield line

    async def close(self) -> None:
        """幂等关闭：释放上游流与并发槽位（on_close），重复调用安全。"""
        if self._closed:
            return
        self._closed = True
        await self.cm.__aexit__(None, None, None)
        await self.client.aclose()
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:  # noqa: BLE001 - 槽位释放失败不掩盖主流程
                pass

    def to_streaming(self, req_id: str,
                     session: "_StreamSession | None" = None) -> StreamingResponse:
        """原样透传（/v1/messages 直通路径）。

        透传不解析 body，于是 token 用量只能靠旁路嗅探：`UsageSniffer` 把流过的
        字节攒起来（上限 1 MB），结束时解析出 usage 落台账——否则这类请求在
        「请求监控」里的输入/输出永远是空（旧实现在这里直接丢掉了用量）。

        上游中途断开时：尚未向客户端发出任何字节 → 经 session 重开上游；
        已经发出过字节 → 不能重放（会重复 message_start/正文），补 error 帧收口。
        """
        up = self
        sniffer = UsageSniffer()

        async def _body_iter():
            nonlocal up, sniffer
            emitted = False
            try:
                while True:
                    try:
                        async for chunk in _paced(up.chunks()):
                            if chunk is _KEEPALIVE:
                                yield chunk  # 静默期心跳：不算内容，不影响首字计时
                                continue
                            sniffer.feed(chunk if isinstance(chunk, bytes)
                                         else chunk.encode("utf-8"))
                            if not emitted:
                                emitted = True
                                reqlog.mark_first(req_id)
                            reqlog.mark_last(req_id)
                            yield chunk
                        reqlog.note_usage(req_id, **_usage_kwargs(sniffer.result()))
                        logs.req_ok(req_id)
                        reqlog.finish_ok(req_id, t_first=up.t_first,
                                         status=up.resp.status_code)
                        return
                    except asyncio.CancelledError:
                        reqlog.note_usage(req_id, **_usage_kwargs(sniffer.result()))
                        reqlog.finish_error(req_id, "客户端断开", status=499,
                                            t_first=up.t_first,
                                            outcome=reqlog.OUTCOME_CLIENT_ABORTED)
                        raise
                    except Exception as err:  # noqa: BLE001
                        if not emitted and session is not None:
                            replacement = await session.reopen(up, str(err))
                            if replacement is not None:
                                up = replacement
                                sniffer = UsageSniffer()  # 旧流没有可用 usage
                                continue
                        logs.req_err(req_id, f"流传输中断: {err}")
                        reqlog.note_usage(req_id, **_usage_kwargs(sniffer.result()))
                        reqlog.finish_error(req_id, f"流传输中断: {err}", t_first=up.t_first,
                                            outcome=reqlog.OUTCOME_UPSTREAM_ABORTED)
                        yield _sse_error_frame()
                        return
            finally:
                await up.close()

        return StreamingResponse(_body_iter(), status_code=up.resp.status_code,
                                 media_type=up.resp.headers.get("content-type", "application/json"),
                                 headers={"Cache-Control": "no-cache", **_model_headers(up)})


async def _try_account(req_id, account, body, incoming_headers, port, needs_captcha,
                       slot_box: list | None = None, reason: list | None = None):
    """尝试用单个账号转发，含验证码续期与可配置重试。

    错误处理策略（参数见 settings，均可用环境变量调整）：
      - 验证码挑战：清池换码重建请求，最多 MAX_CAPTCHA_RETRIES 次
      - 429 频控：**不冷却账号**，按上游 Retry-After（封顶 RETRY_429_WAIT_MAX）
        或 RETRY_429_WAIT 等待后原地重试，最多 RETRY_429_TIMES 次；
        耗尽后换下一个账号，账号保持可用。Plan 通道耗尽且有 API Key 时切
        回退通道重试（force_fallback 显式路由——429 不改账号状态，不能靠
        status 推导通道；回退通道自己的 429 重试预算独立计满后再换号）
      - 5xx 等一般错误：重试最多 RETRY_5XX_TIMES 次；耗尽后账号冷却
        COOLING_SECONDS 并换下一个账号
      - 风控（3012/405「unusual activity」）：指数退避冷却（base 900s 起，封顶
        24h），累计 RISK_BAN_STRIKES 次升级禁用（UI 展示），人工确认恢复
    """
    captcha_retries = 0
    retries_429 = 0
    retries_5xx = 0
    force_fallback = False  # 本请求瞬态走 Key 回退（不改账号持久化状态）
    model_name = str(body.get("model") or "-")
    while True:
        attempt_t0 = time.time()
        reqlog.mark_account(req_id, account.name, account.mode, account.id)
        verify_param = verify_region = None
        # CLI 形态（默认）实测不需要无痕验证码：省掉 10–40s 的 Node+浏览器求解。
        # desktop 形态保留原求解流程。
        if needs_captcha and settings.MESSAGES_STYLE != "cli":
            _park_slot(slot_box)
            try:
                verify_param, verify_region = await captcha_manager.get_verify_param(port)
            except Exception as err:  # noqa: BLE001
                logs.req_err(req_id, f"人机校验失败: {err}")
                reqlog.finish_error(req_id, f"人机校验失败: {err}", status=500)
                return JSONResponse(
                    {"error": {"message": f"无法完成人机校验: {err}", "type": "captcha_error"}},
                    status_code=500,
                )
            if not _reacquire_slot(account, slot_box):
                logs.warn(req_id, f"账号 {account.name} 验证码等待后并发已满，切换下一个")
                if reason is not None:
                    reason[0] = "验证码等待后本账号并发已满"
                return _NEXT_ACCOUNT

        try:
            url, headers, payload = build_request(account, body, verify_param,
                                                  incoming_headers, verify_region,
                                                  force_fallback=force_fallback)
        except RuntimeError as err:
            account.record_result(False, f"凭证无效: {err}")
            _mark(account, Status.INVALID, str(err))
            if reason is not None:
                reason[0] = f"凭证无效（{str(err)[:60]}）"
            logs.warn(req_id, f"账号 {account.name} 凭证无效，切换下一个")
            return _NEXT_ACCOUNT

        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0))
        cm = client.stream("POST", url, headers=headers, content=payload)
        try:
            resp = await cm.__aenter__()
        except httpx.HTTPError as err:
            await client.aclose()
            account.record_result(False, f"连接失败: {err}")
            # 废 JWT / 风控禁用走 Key 回退失败时不得洗成 cooling，否则冷却结束会重开 Plan
            if account.status in (Status.INVALID, Status.DISABLED):
                store.update_account(account)
            else:
                _mark(account, Status.COOLING, f"连接失败: {err}")
            logs.warn(req_id, f"账号 {account.name} 连接失败，切换下一个")
            if reason is not None:
                reason[0] = f"连接失败（{str(err)[:60]}）"
            return _NEXT_ACCOUNT

        status_code = resp.status_code
        preloaded: bytes | None = None
        business_code: int | None = None

        if status_code < 400:
            # 上游对业务错误也回 200：body 是 {"code":1005,"msg":"exceed quota limit"}。
            # 只看 content-type 就能分辨——SSE 是 text/event-stream，整体回复是
            # application/json（可能正常，也可能是这种错误体）。不读一次就分不清，
            # 于是这类请求会被当成成功透传出去、台账里也没有任何用量。
            content_type = (resp.headers.get("content-type") or "").lower()
            if "text/event-stream" not in content_type:
                preloaded = await resp.aread()
                business_code = _business_code(preloaded)
                if business_code not in (None, 0):
                    # 归一成 HTTP 语义走同一套错误分类（验证码/风控/额度/鉴权/其它）
                    status_code = 400

        if status_code >= 400:
            text = (preloaded.decode("utf-8", "ignore") if preloaded is not None
                    else (await resp.aread()).decode("utf-8", "ignore"))
            await cm.__aexit__(None, None, None)
            await client.aclose()

            # 模型级额度用尽（上游 200 + code 1005「exceed quota limit」）/ 模型不在
            # 套餐内（3006）/ 模型并发已满（429 + code 3009）：都是「这个账号对这个
            # 模型」的局部状态，账号本身仍然可用。实测同一账号 GLM-5.3 还有 300 万、
            # GLM-5.3-Flash 已用完——按 _is_exhausted 判成账号耗尽会把整号踢出轮询，
            # 其他模型跟着挂；而对 3009 原地等 60s 会让客户端几分钟收不到一个字节
            #（2026-10-27 实测非流式请求因此吊死）。两种都只换号，不动状态。
            busy = business_code == 3009 or "concurrency limit" in text.lower()
            if busy:
                account.record_result(False, f"模型并发已满: {model_name}")
                logs.warn(
                    req_id,
                    f"账号 {account.name} 的 {model_name} 并发已满"
                    f"（上游 {business_code or status_code}），换下一个账号",
                )
                if reason is not None:
                    # 不带「换号后仍无可用」的尾巴：这条 reason 只描述**这个账号**
                    # 为什么被放弃，换成谁、最终成没成是调度层自己的事（每次换号
                    # 的轨迹由 mark_switch 单独记录）
                    reason[0] = (f"{model_name} 并发已满"
                                 f"（上游 code {business_code or status_code}）")
                # 并发已满 ≠ 账号不可用：交给 _dispatch 决定「短等重试整轮」还是收口
                return _NEXT_BUSY

            if _is_model_unavailable(status_code, business_code, text):
                label = "额度用尽" if business_code == 1005 or "quota" in text.lower() else "不在套餐内"
                account.record_result(False, f"模型{label}: {model_name}")
                logs.warn(
                    req_id,
                    f"账号 {account.name} 的 {model_name} {label}"
                    f"（上游 {business_code or status_code}），换下一个账号",
                )
                if reason is not None:
                    reason[0] = (f"{model_name} {label}"
                                 f"（上游 code {business_code or status_code}）")
                _spawn_bg(_safe_refresh(account))   # 额度变了，后台刷新余额
                # 整轮都是这个信号 → _dispatch 会判「该模型全池不可用」并换模型
                return _NEXT_MODEL

            # 验证码挑战：三形态任一命中即清池重试（不改账号状态）
            challenge = _detect_captcha_challenge(resp, text) if needs_captcha else None
            if challenge:
                captcha_manager.invalidate()
                captcha_retries += 1
                if captcha_retries >= MAX_CAPTCHA_RETRIES:
                    account.record_result(False, "验证码挑战连续失败")
                    logs.warn(req_id, f"账号 {account.name} 验证码连续失败，切换下一个")
                    if reason is not None:
                        reason[0] = "验证码挑战连续失败"
                    return _NEXT_ACCOUNT
                logs.warn(req_id, f"账号 {account.name} 验证码挑战（{challenge}），刷新重试")
                continue  # 同账号重建请求重试

            # 风控（3012「unusual activity」/ 405）：指数退避冷却自动恢复，累计
            # RISK_BAN_STRIKES 次升级禁用（人工恢复）。不立即重试（避免对风控
            # 账号持续施压）；冷却期零上游流量（is_cooling 全通道门禁）。
            if _is_risk_control(status_code, text):
                # 3012 的判定点是「请求形状」而不是账号，但形状差异只有客户端知道。
                # 这里把可判定的形状指纹落进日志：下次再中招，一眼能看出是换了客户端 /
                # 多了哪些字段 / 工具数或 system 块变了 —— 否则只能猜。
                logs.warn(req_id, f"风控形状快照（{account.name}）: {_shape_note(body, incoming_headers)}")
                dumped = _dump_risk_body(req_id, body, incoming_headers)
                if dumped:
                    logs.warn(req_id, f"风控请求体已落盘: {dumped}")
                account.record_result(False, f"风控 HTTP {status_code}（3012/unusual activity）")
                account.risk_penalty(
                    settings.RISK_COOLDOWN_BASE, settings.RISK_COOLDOWN_MAX,
                    settings.RISK_BAN_STRIKES, settings.RISK_STRIKE_DECAY_SECONDS,
                )
                if account.status == Status.DISABLED:
                    account.last_error = (
                        f"风控封禁 (3012/unusual activity) HTTP {status_code}，"
                        f"确认恢复后请在后台手动启用（第 {account.risk_strikes} 次）"
                    )
                    penalty_note = f"已升级禁用（第 {account.risk_strikes} 次）"
                    if settings.RISK_AUTO_ROTATE:
                        # 「风控后换设备重生」自动化：升级禁用即换发全新设备
                        # （新 SKU + 新 device_mid），后台补跑安装序；账号重新
                        # 启用时已是全新身份（billing/claim 头同源跟随档案）
                        from ..fingerprint import profile_for, rotate
                        from ..install import schedule_install

                        old_mid = profile_for(account).device_mid[:8]
                        rotate(account)
                        account.installed_at = None
                        schedule_install(account)
                        logs.warn(
                            req_id,
                            f"账号 {account.name} 风控禁用，已自动换发设备指纹"
                            f"（旧 mid {old_mid}）",
                        )
                else:
                    cool_until = time.strftime(
                        "%H:%M", time.localtime(account.cooling_until)
                    ) if account.cooling_until else "?"
                    account.last_error = (
                        f"命中风控 (3012/unusual activity) HTTP {status_code}，"
                        f"指数退避冷却至 {cool_until}（第 {account.risk_strikes} 次）"
                    )
                    penalty_note = f"冷却至 {cool_until}（第 {account.risk_strikes} 次）"
                store.update_account(account)
                if needs_captcha and account.has_apikey_fallback():
                    logs.warn(
                        req_id,
                        f"账号 {account.name} 命中风控 HTTP {status_code}，{penalty_note}，切 API Key 回退",
                    )
                    needs_captcha = False
                    force_fallback = True
                    continue
                logs.warn(
                    req_id,
                    f"账号 {account.name} 命中风控 HTTP {status_code}，{penalty_note}，切换下一个",
                )
                if reason is not None:
                    reason[0] = f"风控 HTTP {status_code}（{penalty_note}）"
                return _NEXT_ACCOUNT

            if _is_exhausted(status_code, text):
                account.record_result(False, f"额度用完 HTTP {status_code}")
                if account.status in (Status.INVALID, Status.DISABLED):
                    store.update_account(account)
                else:
                    _mark(account, Status.EXHAUSTED, "额度已用完")
                    _spawn_bg(_safe_refresh(account))
                logs.warn(req_id, f"账号 {account.name} 额度用完，切换下一个")
                if reason is not None:
                    reason[0] = "账号额度用完"
                return _NEXT_ACCOUNT

            if status_code == 401:
                account.record_result(False, "鉴权失败 HTTP 401")
                _mark(account, Status.INVALID, "鉴权失败 HTTP 401")
                if needs_captcha and account.has_apikey_fallback():
                    logs.warn(req_id, f"账号 {account.name} 鉴权失败 401，切 API Key 回退")
                    needs_captcha = False
                    force_fallback = True
                    continue
                logs.warn(req_id, f"账号 {account.name} 鉴权失败 401，切换下一个")
                if reason is not None:
                    reason[0] = "鉴权失败 HTTP 401"
                return _NEXT_ACCOUNT

            if status_code == 403:
                # 403 已排除挑战形态（上方 challenge 分支），此处为真实鉴权拒绝
                account.record_result(False, "鉴权失败 HTTP 403")
                _mark(account, Status.INVALID, "鉴权失败 HTTP 403")
                if needs_captcha and account.has_apikey_fallback():
                    logs.warn(req_id, f"账号 {account.name} 鉴权失败 403，切 API Key 回退")
                    needs_captcha = False
                    force_fallback = True
                    continue
                logs.warn(req_id, f"账号 {account.name} 鉴权失败 403，切换下一个")
                if reason is not None:
                    reason[0] = "鉴权拒绝 HTTP 403"
                return _NEXT_ACCOUNT

            if status_code == 429:
                # 1113「无余额/无资源包」是 Key 的永久状态（api.z.ai 实测秒回），
                # 原地等待重试毫无意义，只会把客户端吊死在 RETRY_429_TIMES×WAIT 里
                # ——快速换号/失败。
                if "1113" in text or "insufficient balance" in text.lower():
                    account.record_result(False, "429 1113 无余额（Key 无资源包，永久态）")
                    logs.warn(req_id, f"账号 {account.name} 429 为 1113 无余额，跳过等待重试")
                    if needs_captcha and account.has_apikey_fallback():
                        needs_captcha = False
                        force_fallback = True
                        retries_429 = 0
                        continue
                    return _NEXT_ACCOUNT
                # 频控不是账号故障：不冷却，原地等一等再试，耗尽后换号且账号保持可用。
                # Plan 通道耗尽 ≠ Key 回退也耗尽：同账号切回退并归还该通道的重试预算
                #（与上方 3012/401/403 切回退同一语义）
                if retries_429 < settings.RETRY_429_TIMES:
                    retries_429 += 1
                    wait = _parse_retry_after(resp.headers.get("retry-after")) or settings.RETRY_429_WAIT
                    logs.warn(
                        req_id,
                        f"账号 {account.name} 被限流 429，{wait}s 后重试"
                        f"（{retries_429}/{settings.RETRY_429_TIMES}）",
                    )
                    _park_slot(slot_box)
                    await _sleep(wait)
                    if not _reacquire_slot(account, slot_box):
                        logs.warn(req_id, f"账号 {account.name} 429 等待后并发已满，切换下一个")
                        if reason is not None:
                            reason[0] = "限流等待后本账号并发已满"
                        return _NEXT_ACCOUNT
                    continue
                if needs_captcha and account.has_apikey_fallback():
                    account.record_result(False, "Plan 通道 429 耗尽，切 API Key 回退")
                    logs.warn(req_id, f"账号 {account.name} Plan 通道 429 耗尽，切 API Key 回退")
                    needs_captcha = False
                    force_fallback = True
                    retries_429 = 0
                    continue
                account.record_result(False, f"429 重试 {settings.RETRY_429_TIMES} 次耗尽")
                store.update_account(account)
                logs.warn(
                    req_id,
                    f"账号 {account.name} 429 重试 {settings.RETRY_429_TIMES} 次耗尽，"
                    f"切换下一个（账号保持可用）",
                )
                if reason is not None:
                    reason[0] = f"限流 429 重试 {settings.RETRY_429_TIMES} 次耗尽"
                return _NEXT_ACCOUNT

            if status_code >= 500:
                # 一般性上游错误：重试，耗尽才冷却账号并换号
                if retries_5xx < settings.RETRY_5XX_TIMES:
                    retries_5xx += 1
                    logs.warn(
                        req_id,
                        f"账号 {account.name} 上游 HTTP {status_code}，"
                        f"{settings.RETRY_5XX_WAIT}s 后重试（{retries_5xx}/{settings.RETRY_5XX_TIMES}）",
                    )
                    _park_slot(slot_box)
                    await _sleep(settings.RETRY_5XX_WAIT)
                    if not _reacquire_slot(account, slot_box):
                        logs.warn(req_id, f"账号 {account.name} 5xx 等待后并发已满，切换下一个")
                        if reason is not None:
                            reason[0] = "上游 5xx 等待后本账号并发已满"
                        return _NEXT_ACCOUNT
                    continue
                account.record_result(False, f"HTTP {status_code} 重试 {settings.RETRY_5XX_TIMES} 次耗尽，冷却")
                if account.status in (Status.INVALID, Status.DISABLED) or account.is_cooling():
                    # Key 回退 5xx 不得覆盖废 JWT / 风控禁用，否则冷却结束会重开 Plan；
                    # 风控冷却同理（冷却期不覆盖，避免缩短风控退避）
                    store.update_account(account)
                    logs.warn(req_id, f"账号 {account.name} 上游 {status_code} 重试耗尽，Plan 已停用，切换下一个")
                else:
                    cool = settings.COOLING_SECONDS
                    account.status = Status.COOLING
                    account.cooling_until = time.time() + cool
                    account.last_error = f"上游 HTTP {status_code} 重试 {settings.RETRY_5XX_TIMES} 次耗尽，冷却"
                    store.update_account(account)
                    logs.warn(req_id, f"账号 {account.name} 上游 {status_code} 重试耗尽，冷却 {cool}s，切换下一个")
                if reason is not None:
                    reason[0] = f"上游 HTTP {status_code} 重试 {settings.RETRY_5XX_TIMES} 次耗尽"
                return _NEXT_ACCOUNT

            # 其它 4xx：直接回传客户端；响应体全量落日志供排查
            # （错误 JSON 通常很小；防御性上限 4KB，超长按 HTML 类 WAF 页处理只留头部）
            account.fail_count += 1
            account.record_result(False, f"HTTP {status_code}: {text[:120]}".replace("\n", " "))
            store.update_account(account)
            logs.req_err(req_id, f"上游错误 HTTP {status_code}（账号 {account.name}）")
            body_log = text if len(text) <= 4000 else text[:4000] + f"...(共 {len(text)} 字节，疑似 WAF 页)"
            logs.warn(req_id, f"上游 {status_code} 完整响应体: {body_log}")
            reqlog.finish_error(req_id, f"HTTP {status_code}: {text[:120]}".replace("\n", " "),
                                status=status_code, t_first=time.time() - attempt_t0)
            return JSONResponse(
                _safe_json(text) or {"error": {"message": text[:500], "type": "upstream_error"}},
                status_code=status_code,
            )

        # 成功：记录用量并把打开的上游流交给调用方
        account.use_count += 1
        account.last_used_at = time.time()
        account.record_result(True, f"HTTP 200 · {model_name} · {time.time() - attempt_t0:.1f}s")
        # API Key 回退成功不得把废 JWT / 风控禁用洗成 active，也不得清风控计数；
        # 风控冷却未到期同样不洗（上游风控标记为小时级粘性，回退通道成功不代表
        # Plan 通道已解除，洗掉会立刻重打 Plan 并累加计数）。冷却已过期后的
        # Plan 通道成功即证明恢复，全量清理（清风控计数）。
        if account.status not in (Status.INVALID, Status.DISABLED) and not account.is_cooling():
            account.risk_strikes = 0
            account.last_risk_at = None
            account.last_error = None
            account.cooling_until = None
            if account.status in (Status.COOLING, Status.EXHAUSTED):
                account.status = Status.ACTIVE
        store.update_account(account)
        _spawn_bg(_safe_refresh(account))

        return _Upstream(resp, cm, client, t_first=time.time() - attempt_t0,
                         account_name=account.name, mode=account.mode,
                         preloaded=preloaded)


def _safe_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _business_code(raw: bytes) -> int | None:
    """从整体响应体里取上游业务码（`{"code":1005,...}`），取不到返回 None。

    zcode-plan 的业务错误走 HTTP 200 + body 里的 code：1005=额度用尽、
    3007=验证码、1001=参数……不认这个 code，失败就会被当成成功。
    """
    data = _safe_json(raw.decode("utf-8", "ignore"))
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    return code if isinstance(code, int) else None


async def _safe_refresh(account: Account) -> None:
    try:
        live = store.find(account.provider, account.id)
        if live is None:
            return
        if live.provider == "zai" and live.allows_billing():
            # 去抖：每条消息都刷 billing 是流量放大器（会加剧风控），与 monitor 共享
            # last_checked_at，最小间隔内的刷新直接跳过
            last = live.last_checked_at
            if last and time.time() - last < settings.BILLING_REFRESH_MIN_INTERVAL:
                return
            await fetch_quota(live)
    except Exception:  # noqa: BLE001
        pass
