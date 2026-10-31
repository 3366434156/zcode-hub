"""请求体变换 —— 对齐 zapi body-transformer.ts 在 anthropic 通道上的变换集。

coding-plan 通道（本服务 JWT 通道）应用三项：
  1. system 身份块：前置 ZCode 官方 system 块（CLI Prefix / Agent Identity /
     Environment Info）+ 动态 currentModel 块。网关做内容审查，请求缺这些
     身份块会被拒为 3012 "method not allowed"（镜像 zapi buildStartPlanSystem）。
  2. cache_control：最后一条非 system 消息的最后一个 content block 追加
     `cache_control: {"type": "ephemeral"}`（镜像 ZCode bundle 的 HLr，
     "finalizeLatestNonSystemCacheControl"）。Anthropic API 对低于缓存门槛的
     请求静默忽略 cache_control，因此无条件追加是安全的。
  3. metadata.user_id：JWT 账号存在 user_id 时注入（镜像 bundle 的
     `user_id: B.metadata.userId`）。user_id 每次从 JWT payload（sub / user_id
     字段）实时解出，token 刷新后自动跟随。

所有变换对畸形输入保持 no-op：解析失败返回原样，坏 body 永远不会被这里放大。
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

from . import settings

# ── ZCode 官方 system 身份块（对齐 zapi zcode_system.json，从官方客户端 bundle 提取）──
_SYSTEM_JSON = Path(__file__).with_name("zcode_system.json")
try:
    _ZCODE_SYSTEM_BLOCKS: list[dict] = json.loads(_SYSTEM_JSON.read_text("utf-8"))
except (OSError, ValueError):
    _ZCODE_SYSTEM_BLOCKS = []


def _is_plain_dict(v: object) -> bool:
    return isinstance(v, dict)


def _normalize_user_system(system: object) -> list[dict]:
    """把客户端原有 system 归一为 text block 列表（镜像 zapi normalizeUserSystem）。"""
    if system is None:
        return []
    if isinstance(system, str):
        text = system.strip()
        return [{"type": "text", "text": system}] if text else []
    if not isinstance(system, list):
        return []
    out: list[dict] = []
    for item in system:
        if isinstance(item, str):
            if item.strip():
                out.append({"type": "text", "text": item})
        elif isinstance(item, dict):
            if item.get("type") == "text" and isinstance(item.get("text"), str) and item["text"].strip():
                block = {"type": "text", "text": item["text"]}
                cc = item.get("cache_control")
                if isinstance(cc, dict):
                    block["cache_control"] = cc
                out.append(block)
    return out


def apply_start_plan_system(body: dict, model: str | None = None) -> bool:
    """前置 ZCode 官方 system 身份块 + 动态 currentModel 块。幂等（已前置则跳过）。

    网关内容审查要求 system 含官方身份块，否则 3012。保留客户端原有 system 于官方块之后。
    """
    if not _ZCODE_SYSTEM_BLOCKS:
        return False
    existing = body.get("system")
    # 幂等：首块已是官方标识则视为已注入
    if isinstance(existing, list) and existing:
        first = existing[0]
        if isinstance(first, dict) and first.get("text") == _ZCODE_SYSTEM_BLOCKS[0].get("text"):
            return False
    official = [copy.deepcopy(b) for b in _ZCODE_SYSTEM_BLOCKS]
    if isinstance(model, str) and model.strip():
        official.append({
            "type": "text",
            "text": f"- You are powered by the model named {model}.",
            "cache_control": {"type": "ephemeral"},
        })
    body["system"] = official + _normalize_user_system(existing)
    return True


def apply_cache_control(body: dict) -> bool:
    """最后一条非 system 消息的最后一个 block 加 ephemeral 缓存标记。幂等。"""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            continue

        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }]
            return True
        if isinstance(content, list) and content:
            last_block = content[-1]
            if isinstance(last_block, dict) and not last_block.get("cache_control"):
                last_block["cache_control"] = {"type": "ephemeral"}
                return True
        return False
    return False


def apply_user_id(body: dict, user_id: str) -> bool:
    """注入 metadata.user_id（保留已有 metadata 其它字段）。幂等。"""
    existing = body.get("metadata")
    if _is_plain_dict(existing) and existing.get("user_id") == user_id:
        return False
    merged = dict(existing) if _is_plain_dict(existing) else {}
    merged["user_id"] = user_id
    body["metadata"] = merged
    return True


def jwt_user_id(jwt_token: str | None) -> str | None:
    """从 JWT payload 解 user_id（sub / user_id 字段），失败返回 None。

    JWT 是账号凭证（短期有效），user_id 跟随 token 变化，因此不跨进程缓存。
    """
    if not jwt_token or jwt_token.count(".") != 2:
        return None
    try:
        payload_b64 = jwt_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("user_id") or payload.get("sub")
    return str(user_id) if user_id else None


def transform_body(body: dict, user_id: str | None = None, model: str | None = None) -> dict:
    """按 anthropic 通道变换 body（原地修改并返回）。变换失败静默保持原样。"""
    try:
        apply_start_plan_system(body, model)
        apply_cache_control(body)
        if user_id:
            apply_user_id(body, user_id)
    except Exception:  # noqa: BLE001 - 变换永不放大请求失败
        pass
    return body


# 风控校验只认官方提示词**前两块**原文（orangeTZ07/zcode-plan-bridge 抓包 + 二分实测）
_CLI_MARKER: list[dict] = _ZCODE_SYSTEM_BLOCKS[:2]


def _fold_text(body: dict, extra: list[dict]) -> None:
    """把客户端被剥掉的 system 块并进第一条 user 消息（保留指令，不留在 system 里）。

    2026-10-31 实测：上游只接受「官方前两块」构成的 system；客户端（ZCode 3.17.1 /
    Claude Code）自带的多块 prompt 会让整条请求被判 3012。但这些块是客户端的
    行为指令（编码风格、工具使用约定），直接丢掉会明显影响输出质量，所以改放
    到首条 user 消息最前面——内容照旧进模型，形状合规。
    """
    text = "\n\n".join(
        str(b.get("text") or "") for b in extra if isinstance(b, dict) and b.get("text")
    ).strip()
    if not text:
        return
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return
    target = next((m for m in messages if isinstance(m, dict) and m.get("role") == "user"), None)
    if target is None:
        return
    content = target.get("content")
    block = {"type": "text", "text": text}
    if isinstance(content, str):
        target["content"] = [block, {"type": "text", "text": content}]
    elif isinstance(content, list):
        target["content"] = [block, *content]


def apply_cli_system(body: dict, strict: bool | None = None) -> bool:
    """CLI 形态 system 收敛：**只保留官方前两块**，客户端追加的块折叠进首条 user 消息。

    strict（默认读 settings.CLI_SYSTEM_STRICT，默认开）：上游 3.17.x 起对「CLI 身份 +
    多于两块 system」直接判 3012（2026-10-31 二分实证：5 块 → 405/3012，砍成官方
    前两块 → 200，且 134 tools / thinking / metadata / output_config 全保留不受影响）。
    strict 关闭时退回旧行为（官方块 + 客户端块），只在确认上游规则回滚后再用。
    """
    if not _CLI_MARKER:
        return False
    if strict is None:
        strict = settings.CLI_SYSTEM_STRICT
    existing = body.get("system")
    if isinstance(existing, list) and existing:
        first = existing[0]
        if (len(existing) == len(_CLI_MARKER) and isinstance(first, dict)
                and first.get("text") == _CLI_MARKER[0].get("text")):
            return False  # 已经是「官方两块」形态
    user_blocks = _normalize_user_system(existing)
    body["system"] = [copy.deepcopy(b) for b in _CLI_MARKER]
    if strict:
        # 客户端首块若恰好就是官方首块（ZCode 客户端自带官方 prompt），剩余块才算追加
        extra = user_blocks
        if extra and extra[0].get("text") == _CLI_MARKER[0].get("text"):
            extra = extra[1:]
        if extra and extra[0].get("text") == _CLI_MARKER[1].get("text"):
            extra = extra[1:]
        if settings.CLI_SYSTEM_FOLD_TO_USER:
            _fold_text(body, extra)
        return True
    body["system"] += user_blocks
    return True


def transform_body_cli(body: dict) -> dict:
    """CLI 形态 body 变换：system 收敛为官方两块（多余块折叠进首条 user 消息）。

    其余字段（tools / thinking / metadata / output_config…）一概不动：2026-10-31
    实测它们都不影响上游判定，只有 system 块数是判定点。
    """
    try:
        apply_cli_system(body)
    except Exception:  # noqa: BLE001
        pass
    return body
