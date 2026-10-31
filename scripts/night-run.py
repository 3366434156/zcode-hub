#!/usr/bin/env python
"""夜间批量跑任务，把 ZCode 额度尽量用在有沉淀的产出上。

走本机 zcode2api 网关（默认 http://127.0.0.1:3000），因此天然享受：
多账号轮询、单号并发上限、429 不冷却、3012 风控隔离/冷却、额度耗尽自动跳过。

两条硬约束（2026-10-26 实测）：
  * **单账号只能一路对话** → 并发上限 = 账号数（网关 account_concurrency 已设 1）。
    实测单路输出 ~40-55 tok/s，5 路 ≈ 200-280 tok/s，整夜 7.5h ≈ 500-800 万 token。
  * **输入 token 同样计入额度** → 每个请求塞长上下文（--context-chars）比纯堆输出
    快一个数量级：4 万输入 token 的请求只需数秒预填充，却记为 4 万额度消耗。

设计取向：
  * **限速优先于吞吐**：每 worker 请求间隔默认 2s，遇 429/405 指数退避（60s→900s）。
  * **到点必停**：--until 08:40 硬截止，不会在额度失效后继续空转。
  * **产出落盘**：每条回答写进 out 目录，避免“烧掉了但什么都没留下”。

用法：
  python scripts/night-run.py --until 08:40                       # 纯输出模式
  python scripts/night-run.py --until 08:40 --context-chars 40000  # 长上下文模式（烧得快）
  python scripts/night-run.py --until 08:40 --context-file big.md --workers 5
  python scripts/night-run.py --max-minutes 2 --workers 5          # 冒烟自测

  # 任务文件：每行一个 prompt；空行与 # 开头忽略。不传则用内置的“有产出”任务池。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

DEFAULT_TASKS = [
    "写一份 3000 字以上的技术长文：如何设计一个高可用、多账号轮询的 LLM API 网关。"
    "要求：架构图（ASCII）、关键数据结构、失败模式与退避策略、指标与可观测性、部署与容量规划。",
    "写一份 3000 字以上的技术长文：Anthropic Messages API 与 OpenAI Chat Completions 的协议差异全集，"
    "逐字段对比（system/tools/内容块/流式事件/错误码/用量统计），并给出双向转换的伪代码与坑点。",
    "写一份 2500 字以上的工程指南：Windows 上长期运行的 Python 服务运维手册，涵盖进程守护、日志轮转、"
    "端口冲突排查、计划任务、掉电恢复、升级回滚。要求可直接照做。",
    "写一份 3000 字以上的教程：用 Python + FastAPI 从零实现带账号池的 OpenAI 兼容网关，"
    "给出完整可运行代码骨架（含限流、重试、SSE 流式透传、鉴权、管理接口）。",
    "写一份 2500 字以上的复盘：HTTP 405 + code 3012 这类“反作弊拦截”的排查方法论。"
    "要求：假设树、二分实验设计、证据链记录、误判陷阱、可复用的排查模板。",
    "写一份 3000 字以上的调研报告：2026 年主流编码 Agent（Claude Code / Cline / Codex CLI / opencode）"
    "在协议、上下文管理、工具调用、成本控制上的差异，并给出选型矩阵。",
    "写一份 2000 字以上的技术笔记：AES-256-GCM 与 PBKDF2 在“配置包加密”场景的正确用法，"
    "包含 envelope 设计、口令派生参数选择、常见错误（nonce 复用/tag 处理/迭代次数）。",
    "写一份 2500 字以上的实践指南：如何给既有 Python 项目补齐测试（pytest 夹具、Mock 上游、"
    "异步测试、覆盖率门禁、CI 集成），以一个 FastAPI 网关为例逐段示范。",
]


def parse_deadline(until: str | None, max_minutes: float | None) -> float:
    """返回截止的 epoch 秒。"""
    now = time.time()
    if max_minutes:
        return now + max_minutes * 60
    if not until:
        raise SystemExit("必须给 --until HH:MM 或 --max-minutes N")
    hh, _, mm = until.partition(":")
    target = datetime.now().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)  # 已过点则视为次日
    return target.timestamp()


def load_tasks(path: str | None) -> list[str]:
    if not path:
        return list(DEFAULT_TASKS)
    lines = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines or list(DEFAULT_TASKS)


def build_context(args) -> str:
    """长上下文素材：优先 --context-file，其次用 --context-chars 生成填充文本。"""
    if args.context_file:
        return Path(args.context_file).read_text(encoding="utf-8", errors="ignore")
    if args.context_chars > 0:
        unit = (
            "以下为项目内部技术文档摘要，仅供背景参考：网关需要把账号池、并发控制、"
            "失败退避、额度核算四件事解耦，各自可独立观测、独立回滚；任何跨层耦合都会在"
            "长时间运行中放大成不可定位的故障。"
        )
        reps = max(1, args.context_chars // len(unit) + 1)
        return (unit * reps)[: args.context_chars]
    return ""


def context_for_request(body: str) -> str:
    """每次请求追加随机标记：避免上游 prompt cache 命中后这部分输入不再计费。"""
    if not body:
        return ""
    return (f"{body}\n\n[req-{random.random():.16f}]"
            "\n\n（以上为背景资料，不要复述。请完成下面的任务。）")


class Stats:
    def __init__(self) -> None:
        self.requests = 0
        self.ok = 0
        self.failed = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.backoffs = 0
        self.by_status: dict[str, int] = {}
        self.started = time.time()

    def hit(self, status: str) -> None:
        self.by_status[status] = self.by_status.get(status, 0) + 1

    def summary(self) -> str:
        dur = max(1e-6, time.time() - self.started)
        total = self.tokens_in + self.tokens_out
        return (
            f"请求 {self.requests}（成功 {self.ok} / 失败 {self.failed}），"
            f"输入 {self.tokens_in:,} + 输出 {self.tokens_out:,} = {total:,} tok；"
            f"耗时 {dur / 60:.1f} 分钟，均速 {total / dur:.0f} tok/s；退避 {self.backoffs} 次；"
            f"状态分布 {self.by_status}"
        )


async def worker(idx: int, args, tasks: list[str], stats: Stats, stop: asyncio.Event,
                 out_dir: Path) -> None:
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if args.gateway_key:
        headers["x-api-key"] = args.gateway_key
    backoff = 0.0
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        while not stop.is_set():
            prompt = random.choice(tasks)
            payload = {
                "model": args.model,
                "max_tokens": args.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            ctx = context_for_request(args._ctx_body)
            if ctx:
                payload["system"] = [{"type": "text", "text": ctx}]
            try:
                r = await client.post(f"{args.base_url.rstrip('/')}/v1/messages",
                                      headers=headers, json=payload)
                stats.requests += 1
                stats.hit(str(r.status_code))
                if r.status_code == 200:
                    data = r.json()
                    usage = data.get("usage") or {}
                    stats.ok += 1
                    stats.tokens_in += int(usage.get("input_tokens") or 0)
                    stats.tokens_out += int(usage.get("output_tokens") or 0)
                    backoff = 0.0
                    body = "".join(
                        (b.get("text") or "") for b in (data.get("content") or [])
                        if isinstance(b, dict)
                    )
                    stamp = datetime.now().strftime("%H%M%S")
                    (out_dir / f"w{idx}-{stamp}.md").write_text(
                        f"# prompt\n{prompt}\n\n# answer\n{body}\n", encoding="utf-8"
                    )
                    print(f"[w{idx}] ok  out={usage.get('output_tokens')}  "
                          f"累计 {(stats.tokens_in + stats.tokens_out):,} tok", flush=True)
                elif r.status_code in (429, 405):
                    # 429 频控 / 405+3012 风控：该号已被网关冷却，整体降速等待
                    stats.failed += 1
                    stats.backoffs += 1
                    backoff = min(args.backoff_max, max(args.backoff_min, backoff * 2 or args.backoff_min))
                    print(f"[w{idx}] {r.status_code} 退避 {backoff:.0f}s（{r.text[:120]}）", flush=True)
                    await asyncio.sleep(backoff)
                elif r.status_code == 503:
                    stats.failed += 1
                    print(f"[w{idx}] 503 无可用账号，等待 30s", flush=True)
                    await asyncio.sleep(30)
                else:
                    stats.failed += 1
                    print(f"[w{idx}] {r.status_code} {r.text[:160]}", flush=True)
                    await asyncio.sleep(10)
            except Exception as err:  # noqa: BLE001 - 网络异常不终止夜间任务
                stats.failed += 1
                print(f"[w{idx}] 请求异常：{err}", flush=True)
                await asyncio.sleep(10)
            await asyncio.sleep(args.gap + random.uniform(0, args.jitter))


async def report_accounts(args) -> None:
    """结束时按账号打印额度消耗（admin API）。"""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{args.base_url.rstrip('/')}/admin/api/accounts",
                            headers={"Authorization": f"Bearer {args.admin_key}"})
        data = r.json()
        rows = data.get("accounts") or data
        print("\n=== 账号额度（截至结束）===")
        for a in rows:
            quota = a.get("quota") or {}
            brief = ", ".join(
                f"{m} {int((q or {}).get('remaining') or 0):,}/{(q or {}).get('total') or 0:,}"
                for m, q in quota.items()
            )
            print(f"  {a.get('name','?'):10s} {a.get('status',''):9s} {brief}")
    except Exception as err:  # noqa: BLE001
        print(f"（额度汇总失败：{err}）")


async def main() -> None:
    ap = argparse.ArgumentParser(description="夜间批量跑任务消耗 ZCode 额度")
    ap.add_argument("--base-url", default="http://127.0.0.1:3000")
    ap.add_argument("--model", default="GLM-5.3-Flash")
    ap.add_argument("--workers", type=int, default=5,
                    help="并发 worker 数 = 账号数（单账号只能一路对话，别再往上加）")
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--context-chars", type=int, default=0,
                    help="每次请求塞入的填充上下文长度（字符）。4 万字符 ≈ 2.6 万输入 token，"
                         "是纯输出模式的数倍消耗速度")
    ap.add_argument("--context-file", help="用该文件内容作为长上下文素材（优先于 --context-chars）")
    ap.add_argument("--until", help="本地时间硬截止，如 08:40")
    ap.add_argument("--max-minutes", type=float, help="跑多少分钟后停（冒烟自测用）")
    ap.add_argument("--tasks", help="任务文件（每行一个 prompt）")
    ap.add_argument("--gap", type=float, default=3.0, help="每个 worker 两次请求的最小间隔秒")
    ap.add_argument("--jitter", type=float, default=2.0, help="间隔随机抖动上限秒")
    ap.add_argument("--backoff-min", type=float, default=60.0)
    ap.add_argument("--backoff-max", type=float, default=900.0)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--gateway-key", default="", help="网关 API Key（未设置则不校验）")
    ap.add_argument("--admin-key", default="zcode", help="后台密码，用于结束时汇总额度")
    ap.add_argument("--out", default="night-out", help="产出目录")
    args = ap.parse_args()

    deadline = parse_deadline(args.until, args.max_minutes)
    tasks = load_tasks(args.tasks)
    args._ctx_body = build_context(args)  # noqa: SLF001 - 单次预读，worker 只追加随机标记
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ctx_note = f"，上下文 {len(args._ctx_body):,} 字符" if args._ctx_body else ""
    print(f"目标 {args.base_url} / {args.model} / {args.workers} workers{ctx_note} / "
          f"截止 {datetime.fromtimestamp(deadline).strftime('%m-%d %H:%M')}")
    print(f"任务池 {len(tasks)} 条；产出目录 {out_dir.resolve()}")

    stats = Stats()
    stop = asyncio.Event()
    workers = [asyncio.create_task(worker(i + 1, args, tasks, stats, stop, out_dir))
               for i in range(args.workers)]
    try:
        while time.time() < deadline:
            await asyncio.sleep(min(30, max(1, deadline - time.time())))
            print(f"· {stats.summary()}", flush=True)
    except asyncio.CancelledError:
        pass
    finally:
        stop.set()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    print("\n=== 结束 ===")
    print(stats.summary())
    await report_accounts(args)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
