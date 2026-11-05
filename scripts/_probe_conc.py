"""并发探针：N 路大上下文请求打网关，测账号分配 / 排队 / 上游限流。

每个请求首条 user 文本互不相同（避免 affinity 粘到同一账号），内容是同量级
的长上下文；读 responses 的 usage 汇总实际计费口径（input+cache_read+output）。

用法：python scripts/_probe_conc.py [n] [chars] [max_tokens]
"""
from __future__ import annotations

import concurrent.futures as cf
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:3000"
_ROOT = Path(__file__).resolve().parents[1]


def _meta(key: str) -> str:
    conn = sqlite3.connect(_ROOT / "data" / "accounts.db")
    try:
        row = conn.execute("select value from meta where key=?", (key,)).fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


KEY = _meta("gateway_key")
_FILES = [
    "docs/development/05-upstream-protocols.md",
    "docs/development/06-dev-guide.md",
    "app/routes/gateway.py",
    "app/burner.py",
    "app/quota.py",
    "README.md",
]


def build_context(chars: int) -> str:
    parts: list[str] = []
    total = 0
    for f in _FILES:
        try:
            text = (_ROOT / f).read_text(encoding="utf-8")
        except OSError:
            continue
        parts.append(f"### FILE: {f}\n{text}")
        total += len(text)
        if total >= chars:
            break
    blob = "\n\n".join(parts)
    if len(blob) < chars:
        blob = (blob + "\n") * (chars // max(1, len(blob)) + 1)
    return blob[:chars]


def one(i: int, ctx: str, max_tokens: int) -> dict:
    # 首条消息带唯一标头 → affinity 指纹唯一，请求按轮询分散到不同账号
    text = f"[lane {i} {time.time_ns()}]\n{ctx}"
    body = {"model": "GLM-5.3-Flash", "max_tokens": max_tokens, "stream": False,
            "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}/v1/messages",
                       headers={"Authorization": f"Bearer {KEY}"},
                       json=body, timeout=600)
    except Exception as err:  # noqa: BLE001
        return {"i": i, "code": -1, "dt": time.time() - t0, "err": str(err)[:120]}
    dt = time.time() - t0
    try:
        data = r.json()
    except ValueError:
        return {"i": i, "code": r.status_code, "dt": dt, "err": r.text[:120]}
    usage = data.get("usage") or {}
    if r.status_code != 200:
        return {"i": i, "code": r.status_code, "dt": dt,
                "err": str(data)[:120],
                "units": 0}
    return {"i": i, "code": 200, "dt": dt,
            "inp": usage.get("input_tokens", 0),
            "out": usage.get("output_tokens", 0),
            "cached": usage.get("cache_read_input_tokens", 0),
            "units": (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                      + usage.get("cache_read_input_tokens", 0))}


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    chars = int(sys.argv[2]) if len(sys.argv) > 2 else 400_000
    max_tokens = int(sys.argv[3]) if len(sys.argv) > 3 else 16
    ctx = build_context(chars)
    print(f"{n} 路并发, {chars} chars, max_tokens={max_tokens}")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=n) as pool:
        rows = list(pool.map(lambda i: one(i, ctx, max_tokens), range(n)))
    wall = time.time() - t0
    total_units = sum(r.get("units", 0) for r in rows)
    for r in sorted(rows, key=lambda x: x["i"]):
        print(r)
    ok = sum(1 for r in rows if r["code"] == 200)
    print(f"ok={ok}/{n} wall={wall:.1f}s units={total_units} "
          f"rate={total_units / wall * 3600 / 1e6:.1f}M/h")


if __name__ == "__main__":
    main()
