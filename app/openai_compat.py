"""OpenAI Chat Completions 兼容层 —— /v1/chat/completions ↔ Anthropic Messages 双向转换。

入站：OpenAI 请求体 → Anthropic messages 体（system/developer 提取为 system 参数、
content 分块、tool_calls / tool_result / 图片(data URL) best-effort 映射）。
出站：Anthropic 响应（JSON 或 SSE 事件流）→ OpenAI 格式。

与 body_transform 同一原则：对畸形输入保持宽容，映射不了的部件安静跳过，
绝不放大请求失败。
"""

from __future__ import annotations

import json
import time
import uuid

# Anthropic stop_reason → OpenAI finish_reason
_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _usage_payload(prompt: int | None, completion: int | None, cached: int | None) -> dict:
    """OpenAI 形态的 usage。

    缓存命中按 OpenAI 标准放在 prompt_tokens_details.cached_tokens：客户端
    状态栏的「命中」一栏读的就是这里，不报就等于命中率永远是 0（本机实测
    上游一直有 cache_read_input_tokens，此前全被丢掉）。
    """
    payload: dict = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": (prompt or 0) + (completion or 0),
    }
    if cached is not None:
        payload["prompt_tokens_details"] = {"cached_tokens": cached}
    return payload


def _text_from_content(content: object) -> str:
    """OpenAI content（str | 分块数组）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(p for p in parts if p)
    return ""


def _image_block(url: str) -> dict | None:
    """data:image/...;base64,xxx → Anthropic image block；外链 URL 无法回填，跳过。"""
    if not url.startswith("data:"):
        return None
    head, _, b64 = url.partition(",")
    media_type = head[5:].split(";")[0] or "image/png"
    if not b64:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


def _blocks_from_user_content(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else [{"type": "text", "text": ""}]
    if not isinstance(content, list):
        return [{"type": "text", "text": ""}]
    blocks: list[dict] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            blocks.append({"type": "text", "text": item["text"]})
        elif item.get("type") == "image_url":
            url = (item.get("image_url") or {})
            if isinstance(url, dict):
                url = url.get("url") or ""
            block = _image_block(str(url))
            if block:
                blocks.append(block)
    return blocks or [{"type": "text", "text": ""}]


def openai_to_anthropic(payload: dict) -> tuple[dict | None, str | None]:
    """OpenAI 请求体 → Anthropic messages 体。非法时返回 (None, 错误信息)。"""
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return None, "必须提供 model 参数"
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None, "必须提供 messages 数组"

    system_parts: list[str] = []
    out_msgs: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role in ("system", "developer"):
            text = _text_from_content(content)
            if text:
                system_parts.append(text)
        elif role == "tool":
            out_msgs.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": str(msg.get("tool_call_id") or ""),
                    "content": _text_from_content(content),
                }],
            })
        elif role == "assistant":
            blocks: list[dict] = []
            text = _text_from_content(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                if not isinstance(fn, dict):
                    continue
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"_raw": args}
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "input": args if isinstance(args, dict) else {},
                })
            out_msgs.append({
                "role": "assistant",
                "content": blocks or [{"type": "text", "text": ""}],
            })
        else:  # user 及未知角色一律按 user 处理
            out_msgs.append({"role": "user", "content": _blocks_from_user_content(content)})

    body: dict = {
        "model": model,
        "messages": out_msgs,
        "max_tokens": _as_int(payload.get("max_tokens") or payload.get("max_completion_tokens")) or 4096,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    try:
        if payload.get("temperature") is not None:
            body["temperature"] = float(payload["temperature"])
        if payload.get("top_p") is not None:
            body["top_p"] = float(payload["top_p"])
    except (TypeError, ValueError):
        pass
    stop = payload.get("stop")
    if isinstance(stop, str) and stop:
        body["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        body["stop_sequences"] = [str(s) for s in stop]
    if payload.get("stream"):
        body["stream"] = True

    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        mapped = []
        for t in tools:
            fn = t.get("function") if isinstance(t, dict) else None
            if isinstance(fn, dict) and fn.get("name"):
                mapped.append({
                    "name": str(fn["name"]),
                    "description": str(fn.get("description") or ""),
                    "input_schema": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object"},
                })
        if mapped:
            body["tools"] = mapped

    choice = payload.get("tool_choice")
    if choice == "none":
        body.pop("tools", None)
    elif choice == "required":
        body["tool_choice"] = {"type": "any"}
    elif isinstance(choice, dict) and choice.get("type") == "function":
        name = str((choice.get("function") or {}).get("name") or "")
        if name:
            body["tool_choice"] = {"type": "tool", "name": name}
    # "auto"/缺省：Anthropic 默认即 auto，无需显式映射
    return body, None


def anthropic_to_openai(data: dict, model: str) -> dict:
    """Anthropic message 响应 → OpenAI chat.completion。"""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            })
    usage = data.get("usage") or {}
    in_tok = _as_int(usage.get("input_tokens")) or 0
    out_tok = _as_int(usage.get("output_tokens")) or 0
    # 思考块（type=thinking）→ reasoning_content：非流式与流式保持同一套字段，
    # 否则客户端在非流式下完全看不到模型的推理内容
    reasoning = "".join(
        block["thinking"]
        for block in data.get("content") or []
        if isinstance(block, dict) and block.get("type") == "thinking"
        and isinstance(block.get("thinking"), str)
    )
    message: dict = {"role": "assistant", "content": "".join(text_parts) or None}
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": str(data.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(data.get("model") or model),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _STOP_REASON_MAP.get(data.get("stop_reason"), "stop"),
        }],
        "usage": _usage_payload(in_tok, out_tok, _as_int(usage.get("cache_read_input_tokens"))),
    }


class StreamConverter:
    """Anthropic SSE 事件流 → OpenAI chat.completion.chunk 流（有状态转换器）。

    用法：先 start() 产出 role 首 chunk，逐条 feed(event_dict) 收输出行，
    流结束后追加 done()（"data: [DONE]"）。

    思考块（content_block.type == "thinking"）转成 delta.reasoning_content：
    GLM 这类模型的思考阶段可能长达数分钟，上游只发 thinking_delta，若在此处
    丢弃，客户端在整段思考期间收不到任何字节，只会显示「未响应」——这是本机
    实测到的「老是未响应」根因（1996 条 thinking_delta 被全部丢弃，客户端静默 39s）。
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        self.finish_reason: str | None = None
        # None = 上游未上报（未知），0 = 真实为零；两者语义不同
        self.usage = {"prompt_tokens": None, "completion_tokens": None,
                      "total_tokens": None, "cached_tokens": None}
        self._tool_seq = 0
        self._thinking = False  # 当前内容块是否为思考块

    def _sync_usage_total(self) -> None:
        p, c = self.usage["prompt_tokens"], self.usage["completion_tokens"]
        self.usage["total_tokens"] = (p or 0) + (c or 0) if p is not None or c is not None else None

    def start(self) -> str:
        return self._chunk({"role": "assistant", "content": ""})

    def done(self) -> str:
        return "data: [DONE]\n\n"

    def feed(self, evt: dict) -> list[str]:
        etype = evt.get("type")
        if etype == "message_start":
            msg = evt.get("message") or {}
            if msg.get("id"):
                self.chunk_id = str(msg["id"])
            u = msg.get("usage") or {}
            prompt = _as_int(u.get("input_tokens"))
            if prompt is not None:
                self.usage["prompt_tokens"] = prompt
                self._sync_usage_total()
            cached = _as_int(u.get("cache_read_input_tokens"))
            if cached is not None:
                self.usage["cached_tokens"] = cached
            return []
        if etype == "content_block_start":
            block = evt.get("content_block") or {}
            self._thinking = block.get("type") == "thinking"
            if block.get("type") == "tool_use":
                idx = self._tool_seq
                self._tool_seq += 1
                return [self._chunk({"tool_calls": [{
                    "index": idx,
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {"name": str(block.get("name") or ""), "arguments": ""},
                }]})]
            return []
        if etype == "content_block_stop":
            self._thinking = False
            return []
        if etype == "content_block_delta":
            delta = evt.get("delta") or {}
            if delta.get("type") == "thinking_delta" and isinstance(delta.get("thinking"), str):
                return [self._chunk({"reasoning_content": delta["thinking"]})]
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                return [self._chunk({"content": delta["text"]})]
            if delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                return [self._chunk({"tool_calls": [{
                    "index": max(self._tool_seq - 1, 0),
                    "function": {"arguments": delta["partial_json"]},
                }]})]
            return []  # signature_delta / citations_delta 等无需产出
        if etype == "message_delta":
            delta = evt.get("delta") or {}
            self.finish_reason = _STOP_REASON_MAP.get(delta.get("stop_reason"), "stop")
            usage = evt.get("usage") or {}
            # 输入侧的真实数字常常只在这一帧：实测 message_start 的 input_tokens 是 0，
            # message_delta 才带 input_tokens=70 / cache_read_input_tokens=192。
            prompt = _as_int(usage.get("input_tokens"))
            if prompt is not None:
                self.usage["prompt_tokens"] = prompt
            out = _as_int(usage.get("output_tokens"))
            if out is not None:
                self.usage["completion_tokens"] = out
            self._sync_usage_total()
            cached = _as_int(usage.get("cache_read_input_tokens"))
            if cached is not None:
                self.usage["cached_tokens"] = cached
            chunk = self._chunk({}, finish_reason=self.finish_reason)
            return [chunk]
        return []  # message_stop / ping / error 等无需产出

    def _chunk(self, delta: dict, finish_reason: str | None = None) -> str:
        payload: dict = {
            "id": self.chunk_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if finish_reason is not None:
            payload["usage"] = _usage_payload(
                self.usage["prompt_tokens"], self.usage["completion_tokens"],
                self.usage["cached_tokens"],
            )
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class UsageSniffer:
    """从上游 Anthropic 响应里嗅出 usage —— 流式 SSE 与整体 JSON 都适用。

    为什么需要：`/v1/messages` 是**原样透传**（网关不解析 body），于是「请求监控」
    里这类请求的输入/输出永远是空，`/v1/messages` 的 token 消耗等于没记。这里把
    流过的字节攒起来（上限 1 MB），结束时就地解析 usage，别的什么都不改。

    只认三处（与 Anthropic 协议一致）：
      - 整体 JSON 的顶层 `usage`
      - `message_start.message.usage`（输入侧与缓存命中在这里）
      - `message_delta.usage`（输出侧在这里）
    缓存命中同时接受 `cache_read_input_tokens` 与 `cached_tokens` 两种拼写。
    """

    MAX_BUF = 1 << 20   # 1 MB：正常响应远小于此，超出就放弃统计（不拖内存）

    def __init__(self) -> None:
        self._buf = bytearray()
        self._overflow = False
        self.usage: dict = {}

    def feed(self, chunk: bytes) -> None:
        if self._overflow:
            return
        if len(self._buf) + len(chunk) > self.MAX_BUF:
            self._overflow = True
            self._buf.clear()
            return
        self._buf += chunk

    def result(self) -> dict:
        """结束解析：返回 {input_tokens, output_tokens, cache_read_input_tokens, credit}。"""
        text = bytes(self._buf).decode("utf-8", "ignore")
        self._buf.clear()
        if self._overflow:
            return dict(self.usage)
        stripped = text.strip()
        if stripped.startswith("{"):
            obj = _safe_load(stripped)
            if isinstance(obj, dict):
                self._absorb(obj.get("usage"))
            return dict(self.usage)
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            if '"usage"' not in line:
                continue
            obj = _safe_load(line[5:].strip())
            if not isinstance(obj, dict):
                continue
            self._absorb(obj.get("usage"))
            message = obj.get("message")
            if isinstance(message, dict):
                self._absorb(message.get("usage"))
        return dict(self.usage)

    def _absorb(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return
        for key in ("input_tokens", "output_tokens"):
            value = _as_int(usage.get(key))
            if value is not None:
                self.usage[key] = value
        cached = _as_int(usage.get("cache_read_input_tokens"))
        if cached is None:
            cached = _as_int(usage.get("cached_tokens"))
        if cached is not None:
            self.usage["cache_read_input_tokens"] = cached
        credit = usage.get("credit")
        if isinstance(credit, (int, float)):
            self.usage["credit"] = float(credit)


def _safe_load(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
