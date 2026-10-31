"""会话粘性（affinity）：同一段对话尽量留在同一个账号上。

为什么需要（对齐 magpie 的 internal/gateway/affinity.go）：
上游按「账号 + prompt 前缀」缓存整段对话，命中后那段 prompt 几乎免费、首字节
也快。严格轮询意味着每发一条消息就换一个账号，于是上游每次都把整段对话重新
读一遍——缓存命中归零、钱照付、首字节变慢，后台看起来还像「账号一直在跳」。
所以轮询只该在两种情况生效：这段对话还没人回答过，或原账号不可用（冷却 /
额度用完 / 并发满）。

怎么认「同一段对话」：system 提示词 + 第一条 user 消息的哈希。多轮对话里
messages 会不断增长，但这两样不变，正是上游缓存前缀的起点（与 magpie 一样，
从对话本身推，不依赖客户端会话头）。

只放内存：缓存本身也就几分钟寿命（Anthropic / OpenAI 都是 5 分钟冷掉），
网关重启后重新学一遍的代价远小于维护一份落盘状态。
"""

from __future__ import annotations

import hashlib
import threading
import time

from . import settings

# 会话指纹上限：超过就顺手清一遍过期记录，避免长跑进程无界增长
MAX_KEYS = 512

_lock = threading.Lock()
# provider -> {会话指纹: (回答它的账号 id, 记住的时刻)}
_sticky: dict[str, dict[str, tuple[str, float]]] = {}


def key_of(body: dict) -> str:
    """会话指纹：system 提示词 + 第一条 user 文本 → 16 位十六进制。

    取不到任何文本时返回空串，调用方按「无粘性」处理（退回轮询）。
    """
    parts: list[str] = []
    system = body.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
        break  # 只看第一条 user：它是前缀起点，后续轮次的增长不影响指纹
    if not parts:
        return ""
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


def prefer(provider: str, key: str) -> str | None:
    """这段对话上次由谁回答（未过期才算），没有则 None。"""
    if not key:
        return None
    with _lock:
        rec = _sticky.get(provider, {}).get(key)
        if rec is None:
            return None
        account_id, at = rec
        if time.time() - at > settings.AFFINITY_TTL:
            _sticky[provider].pop(key, None)
            return None
        return account_id


def remember(provider: str, key: str, account_id: str) -> None:
    """记下这段对话这次由谁回答（成功拿到 200 之后才调用）。"""
    if not key:
        return
    with _lock:
        table = _sticky.setdefault(provider, {})
        table[key] = (account_id, time.time())
        if len(table) > MAX_KEYS:
            now = time.time()
            for stale in [k for k, (_, at) in table.items() if now - at > settings.AFFINITY_TTL]:
                table.pop(stale, None)


def reset() -> None:
    """测试夹具：清空全部粘性记录。"""
    with _lock:
        _sticky.clear()
