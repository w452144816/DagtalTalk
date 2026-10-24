"""OpenAI 兼容的 /v1/chat/completions（流式），按当前选择转发到方舟豆包或 MiniMax。

OpenTalking 只认一个 OpenAI-compatible 端点，所以它的 OPENTALKING_LLM_BASE_URL 指向这里，
由页面切换实际引擎。方舟本身就是 OpenAI 协议（关掉深度思考保证首字延迟）；
MiniMax 走 Anthropic 兼容接口，这里把 Anthropic 流事件转成 OpenAI chunk。
"""
from __future__ import annotations

import base64
import json
import re
from collections import deque
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from config import LLMS, ProviderConfig

log = logging.getLogger("llm_router")
TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def _chunk(cid: str, model: str, text: str = "", finish: str | None = None) -> str:
    delta = {"content": text} if text else {}
    body = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


async def _ark(cfg: ProviderConfig, messages: list[dict[str, Any]], max_tokens: int) -> AsyncIterator[str]:
    body = {"model": cfg.model, "messages": messages, "stream": True, "max_tokens": max_tokens,
            "thinking": {"type": "disabled"}}
    async with httpx.AsyncClient(timeout=TIMEOUT, proxy=cfg.proxy or None, trust_env=False) as c:
        async with c.stream("POST", f"{cfg.base_url.rstrip('/')}/chat/completions", json=body,
                            headers={"Authorization": f"Bearer {cfg.api_key}"}) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"{cfg.label} {r.status_code}: {(await r.aread()).decode()[:300]}")
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    yield line + "\n\n"


async def _minimax(cfg: ProviderConfig, messages: list[dict[str, Any]], max_tokens: int) -> AsyncIterator[str]:
    system = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    turns = [{"role": m["role"], "content": str(m.get("content") or "")}
             for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
    body: dict[str, Any] = {"model": cfg.model, "max_tokens": max_tokens, "messages": turns, "stream": True}
    if system:
        body["system"] = system
    cid, sent = f"chatcmpl-{uuid.uuid4().hex[:24]}", False
    async with httpx.AsyncClient(timeout=TIMEOUT, proxy=cfg.proxy or None, trust_env=False) as c:
        async with c.stream("POST", f"{cfg.base_url.rstrip('/')}/v1/messages", json=body,
                            headers={"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01"}) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"{cfg.label} {r.status_code}: {(await r.aread()).decode()[:300]}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                delta = ev.get("delta") or {}
                if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    sent = True
                    yield _chunk(cid, cfg.model, delta.get("text", ""))
                elif ev.get("type") == "error":
                    raise RuntimeError(f"{cfg.label}: {ev.get('error')}")
    if not sent:
        log.warning("minimax returned no text")
    yield _chunk(cid, cfg.model, finish="stop")
    yield "data: [DONE]\n\n"


VLM_MODEL = os.getenv("ARK_VLM_MODEL", "")       # 看图用的方舟模型；空 = 和聊天同一个（doubao-seed 系列本身多模态）


async def complete(provider: str, messages: list[dict[str, Any]], max_tokens: int = 2000,
                   images: list[bytes] | None = None, temperature: float = 0.3) -> str:
    """非流式补全（人设抽取 / 合并用）。images 附在最后一条 user 消息上（JPEG/PNG 字节）。"""
    cfg = LLMS[provider]
    imgs = [base64.b64encode(b).decode() for b in (images or [])]
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), proxy=cfg.proxy or None, trust_env=False) as c:
        if cfg.protocol == "openai":
            msgs = [dict(m) for m in messages]
            if imgs:
                last = msgs[-1]
                last["content"] = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in imgs] \
                    + [{"type": "text", "text": str(last.get("content") or "")}]
            body = {"model": VLM_MODEL if imgs and VLM_MODEL else cfg.model, "messages": msgs, "max_tokens": max_tokens,
                    "temperature": temperature, "thinking": {"type": "disabled"}}
            r = await c.post(f"{cfg.base_url.rstrip('/')}/chat/completions", json=body,
                             headers={"Authorization": f"Bearer {cfg.api_key}"})
            if r.status_code >= 400:
                raise RuntimeError(f"{cfg.label} {r.status_code}: {r.text[:300]}")
            return str(r.json()["choices"][0]["message"].get("content") or "")
        system = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
        turns: list[dict[str, Any]] = [{"role": m["role"], "content": str(m.get("content") or "")}
                                       for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
        if imgs and turns:
            turns[-1]["content"] = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
                                    for b in imgs] + [{"type": "text", "text": turns[-1]["content"]}]
        body = {"model": cfg.model, "max_tokens": max_tokens, "messages": turns, "temperature": temperature}
        if system:
            body["system"] = system
        r = await c.post(f"{cfg.base_url.rstrip('/')}/v1/messages", json=body,
                         headers={"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01"})
        if r.status_code >= 400:
            raise RuntimeError(f"{cfg.label} {r.status_code}: {r.text[:300]}")
        return "".join(p.get("text", "") for p in r.json().get("content", []) if p.get("type") == "text")


SAY_MARK = "​[SAY]"      # 后端 speak(mode=say)：OpenTalking 的 /speak 必走 LLM，用这个标记让路由原样回显，不调模型


async def echo(text: str) -> AsyncIterator[str]:
    """把文本当作模型输出原样流回（按句切块，好让 TTS 尽早开口）。"""
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？!?；;\n" and buf.strip():
            yield _chunk(cid, "say", buf)
            buf = ""
    if buf.strip():
        yield _chunk(cid, "say", buf)
    yield _chunk(cid, "say", finish="stop")
    yield "data: [DONE]\n\n"


# ---- 情绪标签：LLM 每句开头 [温柔] 之类（personas.RULES），这里从流里剥掉（字幕/历史/回调都看不到），
#      并记下「去标签后的文本 → 情绪」，TTS 收到句子时按文本匹配拿到情绪换成合成风格指令（tts.emotion_for）----
_TAG_RE = re.compile(r"\[(平静|温柔|开心|笑|心疼|叹气|惊讶|着急)\]")
# 模型偶尔自创标签（实测出过 [轻声]，被念出来、上了字幕）：短的中文方括号一律不放出去，
# 意思接近的归到已有情绪，其余直接丢掉
_TAG_ANY = re.compile(r"\[([一-鿿]{1,6})\]")
_TAG_ALIAS = {"轻声": "温柔", "小声": "温柔", "慈祥": "温柔", "关切": "温柔", "亲切": "温柔", "欣慰": "温柔",
              "高兴": "开心", "欢喜": "开心", "激动": "开心", "大笑": "笑", "笑着": "笑", "偷笑": "笑",
              "难过": "心疼", "伤心": "心疼", "哽咽": "心疼", "担心": "着急", "焦急": "着急", "吃惊": "惊讶",
              "感叹": "叹气", "无奈": "叹气"}
EMOTION_HINTS: deque = deque(maxlen=200)      # (time, 情绪, 文本累积)，30 秒内有效


def _norm(s: str) -> str:
    return re.sub(r"[\s，。！？!?,.、；;：:“”\"'…～~（）()\[\]]+", "", s)


def emotion_for(text: str) -> str:
    """TTS 侧：这句话（OpenTalking 切句后的文本）属于哪个情绪段。
    找不到但最近 30 秒内有带标签的回复 → 按"平静"处理（保证同一轮每句走同一条合成路径，音色一致）；完全没有标签 → 空。"""
    t = _norm(text)
    if not t:
        return ""
    now = time.time()
    recent = False
    for ts, emo, seg in reversed(EMOTION_HINTS):
        if now - ts > 30:
            break
        recent = True
        s = _norm(seg)
        if t in s or (len(t) >= 6 and s.startswith(t[:6])) or (len(s) >= 6 and t.startswith(s[:6])):
            return emo
    if recent:
        log.info("emotion lookup miss, default 平静: %s", text[:30])
        return "平静"
    return ""


class _TagFilter:
    """跨 delta 的标签剥离：'[' 到 ']' 之间先攒着，是合法标签就记情绪，不是就原样放出。"""

    def __init__(self) -> None:
        self.pending = ""
        self.cur: list | None = None       # 当前情绪段 [ts, emo, text]
        self.tags = 0

    def feed(self, s: str) -> str:
        out = ""
        s = self.pending + s
        self.pending = ""
        while s:
            i = s.find("[")
            if i < 0:
                out += s
                break
            out += s[:i]
            j = s.find("]", i)
            if j < 0:
                if len(s) - i > 6:           # 不是标签（标签最长 4 字），放出去
                    out += s[i]
                    s = s[i + 1:]
                    continue
                self.pending = s[i:]
                break
            m = _TAG_RE.fullmatch(s[i:j + 1])
            other = None if m else _TAG_ANY.fullmatch(s[i:j + 1])
            emo = m.group(1) if m else (_TAG_ALIAS.get(other.group(1)) if other else None)
            if emo:
                self.cur = [time.time(), emo, ""]
                EMOTION_HINTS.append(self.cur)
                self.tags += 1
            elif other:
                log.info("drop unknown tag %s", other.group(0))     # 自创标签：不念、不上字幕
            else:
                out += s[i:j + 1]
            s = s[j + 1:]
        if out and self.cur is not None:
            self.cur[2] += out
        return out

    def flush(self) -> str:
        s, self.pending = self.pending, ""
        if s and self.cur is not None:
            self.cur[2] += s
        return s


def _rewrite(piece: str, f: _TagFilter) -> str:
    """改写一条 SSE（只动 delta.content）。"""
    if not piece.startswith("data:"):
        return piece
    raw = piece[5:].strip()
    if raw == "[DONE]":
        tail = f.flush()
        log.info("llm reply done: %d emotion tag(s)%s", f.tags, "" if f.tags else " (persona rules not applied or model ignored them)")
        return (_chunk("tag", "", tail) if tail else "") + piece
    try:
        d = json.loads(raw)
        delta = d["choices"][0].get("delta") or {}
    except (ValueError, KeyError, IndexError, TypeError):
        return piece
    if not delta.get("content"):
        return piece
    delta["content"] = f.feed(delta["content"])
    if not delta["content"] and not d["choices"][0].get("finish_reason"):
        return ""                          # 整段都是标签：不发空块
    return f"data: {json.dumps(d, ensure_ascii=False)}\n\n"


async def stream(provider: str, messages: list[dict[str, Any]], max_tokens: int = 600) -> AsyncIterator[str]:
    """按 provider 流式返回 OpenAI SSE 文本；出错时说一句兜底话，避免数字人卡住不出声。"""
    cfg = LLMS[provider]
    fn = _ark if cfg.protocol == "openai" else _minimax
    t0, first = time.perf_counter(), True
    tf = _TagFilter()
    try:
        async for piece in fn(cfg, messages, max_tokens):
            if first:
                log.info("llm %s first chunk %.0f ms", provider, (time.perf_counter() - t0) * 1000)
                first = False
            piece = _rewrite(piece, tf)
            if piece:
                yield piece
    except Exception as e:  # noqa: BLE001
        log.exception("llm %s failed", provider)
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        yield _chunk(cid, cfg.model, f"抱歉，{cfg.label}暂时没有响应，请稍后再试。")
        yield _chunk(cid, cfg.model, finish="stop")
        yield "data: [DONE]\n\n"
        log.error("llm error detail: %s", str(e)[:300])
