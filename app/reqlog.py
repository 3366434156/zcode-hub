"""请求监控 —— 网关请求的实时环形日志 + 持久化台账入口。

两层数据，职责分明：
- **内存环（本模块）**：最近 500 条，含「进行中」，给面板「实时」页 5 秒轮询；
  重启即清零，纯观测，不写库。
- **台账（app/usagelog.py）**：每个请求结束时落一行 SQLite，保留 30 天 / 5 万行，
  给面板「明细 / 账号 / 模型」页做分页、筛选与聚合。

记录口径（对齐 workbuddy 网关）：一次客户端请求一条，多账号重试不分裂，
`account` 记最终服务的账号；`outcome` 是终态（completed / client_aborted /
upstream_aborted / failed），`http_status` 单独记，二者不互相覆盖。
"""

from __future__ import annotations

import threading
import time
from collections import deque

from .usagelog import usagelog

KEEP = 500

_lock = threading.Lock()
_entries: deque = deque(maxlen=KEEP)
_inflight: dict[str, dict] = {}

# 终态取值：与 http_status 分开，避免「200 但客户端取消」这类请求被算成成功
OUTCOME_COMPLETED = "completed"
OUTCOME_CLIENT_ABORTED = "client_aborted"
OUTCOME_UPSTREAM_ABORTED = "upstream_aborted"
OUTCOME_FAILED = "failed"


def begin(req_id: str, endpoint: str, model: str, stream: bool, preview: str = "",
          provider: str = "zai", requested_model: str = "", client: str = "",
          channel: str = "local") -> None:
    """请求进入网关（鉴权通过、body 解析成功后）。

    channel：来源通道（public = Cloudflare 隧道进来的公网请求，local = 内网/本机）。
    台账按它分组，看板因此能单独看公网用量，也能看两者的合计。
    """
    entry = {
        "req_id": req_id,
        "ts": time.time(),
        "endpoint": endpoint,
        "provider": provider,
        "model": model,
        "requested_model": requested_model if requested_model != model else "",
        "stream": bool(stream),
        "preview": (preview or "")[:120],
        "client": (client or "")[:80],
        "channel": channel if channel in ("public", "local") else "local",
        "account": "",
        "account_id": "",
        "mode": "",
        "switches": [],      # 换号轨迹：[{from,to,why}]，每次换号追加一条
        "ok": None,
        "outcome": None,
        "status": None,
        "error": "",
        "t_first": None,     # 秒：拿到上游响应头（调度完成）
        "ttft": None,        # 秒：首个内容块离开网关
        "t_last": None,      # 秒：最后一个内容块离开网关
        "t_total": None,
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "credit": None,
        "tokens_per_sec": None,
        "cache_hit_pct": None,
        "usage_missing": False,
    }
    with _lock:
        _entries.append(entry)
        _inflight[req_id] = entry


def mark_account(req_id: str, account_name: str, mode: str, account_id: str = "") -> None:
    """记录实际服务该请求的账号（多账号重试时最后一次生效）。"""
    with _lock:
        entry = _inflight.get(req_id)
        if entry is not None:
            entry["account"] = account_name
            entry["mode"] = mode
            if account_id:
                entry["account_id"] = account_id


def mark_switch(req_id: str, from_account: str, to_account: str, why: str) -> None:
    """记录一次换号：从谁换成谁、为什么。

    「老是跳号」是否正常、跳得值不值，只有看到**原因**才能判断——额度用完的
    换号是该换，风控/并发的换号是必要的，轮询摊开的换号才值得警惕。同一个
    请求里可能换多次（额度→换号→又冷却→再换号），所以是列表不是单值。
    """
    if not why:
        return
    with _lock:
        entry = _inflight.get(req_id)
        if entry is None:
            return
        switches = entry.setdefault("switches", [])
        if len(switches) < 8:   # 防御：异常风暴下不要把一行日志撑成几百字
            switches.append({
                "from": (from_account or "")[:40],
                "to": (to_account or "")[:40],
                "why": str(why)[:120],
            })


def mark_model(req_id: str, model: str) -> None:
    """记录实际服务的模型（模型兜底换过之后调用）。

    `model` 换成兜底模型，`requested_model` 保留客户端原本点的那个——
    台账里两者不一致就是「这次换了模型」，前端据此打标。
    """
    with _lock:
        entry = _inflight.get(req_id)
        if entry is None or not model:
            return
        previous = entry.get("model") or ""
        if not entry.get("requested_model") and previous and previous != model:
            entry["requested_model"] = previous
        entry["model"] = model


def mark_first(req_id: str) -> None:
    """首个内容块离开网关的时刻 → 真正的首字延迟。"""
    with _lock:
        entry = _inflight.get(req_id)
        if entry is not None and entry["ttft"] is None:
            entry["ttft"] = time.time() - entry["ts"]


def mark_last(req_id: str) -> None:
    """最后一个内容块的时刻（存成"距请求开始的秒数"，与 ttft 同口径）。"""
    with _lock:
        entry = _inflight.get(req_id)
        if entry is not None:
            entry["t_last"] = time.time() - entry["ts"]


def note_usage(req_id: str, input_tokens: int | None = None,
               output_tokens: int | None = None, cached_tokens: int | None = None,
               credit: float | None = None) -> None:
    """上游上报的用量（流式可能在最后一帧才拿到，因此允许后到）。"""
    with _lock:
        entry = _inflight.get(req_id)
        if entry is None:
            return
        if input_tokens is not None:
            entry["input_tokens"] = int(input_tokens)
        if output_tokens is not None:
            entry["output_tokens"] = int(output_tokens)
        if cached_tokens is not None:
            entry["cached_tokens"] = int(cached_tokens)
        if credit is not None:
            entry["credit"] = float(credit)


def finish_ok(req_id: str, t_first: float | None = None,
              status: int | None = None) -> None:
    """请求正常结束（用量由 note_usage 事先记录）。"""
    _finish(req_id, OUTCOME_COMPLETED, "", status, t_first)


def finish_error(req_id: str, error: str, status: int | None = None,
                 t_first: float | None = None,
                 outcome: str = OUTCOME_FAILED) -> None:
    """请求失败结束：4xx/5xx、调度失败、流中断、客户端断开都走这里。"""
    _finish(req_id, outcome, error, status, t_first)


def _finish(req_id: str, outcome: str, error: str, status: int | None,
            t_first: float | None) -> None:
    now = time.time()
    with _lock:
        entry = _inflight.pop(req_id, None)
        if entry is None:
            return
        if t_first is not None and entry["t_first"] is None:
            entry["t_first"] = t_first
        entry["t_total"] = now - entry["ts"]
        entry["outcome"] = outcome
        entry["ok"] = outcome == OUTCOME_COMPLETED
        if status is not None:
            entry["status"] = status
        if error:
            entry["error"] = error[:300]
    # 队列 put，永不阻塞请求路径
    usagelog.record(_row(entry))


def _row(entry: dict) -> dict:
    """内存条目 → 台账行（只做派生计算，不改原条目）。"""
    ttft = entry["ttft"]
    total = entry["t_total"]
    out_tok = entry["output_tokens"]
    in_tok = entry["input_tokens"]
    cached = entry["cached_tokens"]

    # 非流式没有「首字」可言：整段响应一次到齐，此时把总耗时当生成窗口，
    # tok/s 才是端到端吞吐（而不是把排队时间也算进去后得到一个无意义的数）。
    if not entry["stream"]:
        ttft = None
        gen = total
    else:
        end = entry["t_last"]
        gen = end - ttft if (ttft is not None and end is not None and end > ttft) else None

    tps = None
    if gen and gen > 0 and out_tok:
        tps = round(out_tok / gen, 2)

    hit = None
    if cached is not None and (in_tok is not None or cached):
        prompt_all = (in_tok or 0) + (cached or 0)
        if prompt_all > 0:
            hit = round(cached * 100.0 / prompt_all, 1)

    return {
        "req_id": entry["req_id"],
        "ts": entry["ts"],
        "endpoint": entry["endpoint"],
        "provider": entry["provider"],
        "account": entry["account"],
        "account_id": entry["account_id"],
        "mode": entry["mode"],
        "switches": entry.get("switches") or [],
        "model": entry["model"],
        "requested_model": entry["requested_model"],
        "stream": entry["stream"],
        "outcome": entry["outcome"] or OUTCOME_FAILED,
        "http_status": entry["status"],
        "error": entry["error"],
        "total_ms": round(total * 1000) if total is not None else None,
        "ttft_ms": round(ttft * 1000) if ttft is not None else None,
        "gen_ms": round(gen * 1000) if gen is not None else None,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cached_tokens": cached,
        "credit": entry["credit"],
        "tokens_per_sec": tps,
        "cache_hit_pct": hit,
        # 有输出却没有 usage：上游没报（不该因此丢掉这条请求的计数与时延样本）
        "usage_missing": 1 if (out_tok is None and in_tok is None) else 0,
        "preview": entry["preview"],
        "client": entry["client"],
        "channel": entry.get("channel") or "local",
    }


def snapshot() -> list[dict]:
    """全部条目，最新在前（含在途）。"""
    with _lock:
        return [dict(e) for e in reversed(_entries)]


def clear() -> None:
    """清空实时环与持久台账（面板「清空」按钮）。"""
    with _lock:
        _entries.clear()
        _inflight.clear()
    usagelog.clear()
