"""节点侧的会话真源（/v1 建的会话）：元数据落盘、跟本机 OpenTalking 的 SSE 算用量、超时自动结束、事件上报控制面。

控制面建会话时把元数据（biz_ref、callback_url、音色、超时……）交给节点（建会话请求的 X-Session-Meta 头），
之后会话的一切状态都在这里：说话时长、对话轮次、最近活动时间。心跳把这些带给控制面当缓存；
控制面重启/主备切换后从心跳重建，不丢。事件（session.started / conversation.turn / session.ended）
发给控制面 /internal/events 转回调后端，发不出去就落在 /w/state/events_spool/ 里重试。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from pathlib import Path

import httpx

import cluster
from llm_router import SAY_MARK

log = logging.getLogger("node_sessions")
DIR = Path("/w/state/sessions")
SPOOL = Path("/w/state/events_spool")
META: dict[str, dict] = {}
_TAILS: dict[str, asyncio.Task] = {}
PUBLIC = ("id", "avatar_id", "voice", "dialect", "biz_ref", "callback_url", "persona_id", "idle_s", "max_s", "state", "speaking",
          "created_at", "started_at", "last_activity", "talk_s", "turns", "ended_reason", "idx")


def _save(m: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / f"{m['id']}.tmp"
    tmp.write_text(json.dumps(m, ensure_ascii=False), "utf-8")
    tmp.replace(DIR / f"{m['id']}.json")


def public(m: dict) -> dict:
    return {k: m.get(k) for k in PUBLIC} | {"usage": usage(m)}


def usage(m: dict) -> dict:
    end = m.get("ended_at") or time.time()
    return {"duration_s": round(end - m["created_at"], 1), "talk_s": round(m.get("talk_s", 0.0), 1), "turns": m.get("turns", 0)}


def register(sid: str, idx: int, meta: dict) -> None:
    """控制面建会话时交来的元数据 → 开始跟踪。"""
    now = time.time()
    m = {"id": sid, "idx": idx, "state": "created", "speaking": False, "created_at": now, "last_activity": now,
         "talk_s": 0.0, "turns": 0, "pending_user": None, "speech_t0": None, "started_at": None, "ended_reason": None}
    m.update({k: meta.get(k) for k in ("avatar_id", "voice", "dialect", "biz_ref", "callback_url", "persona_id") if k in meta})
    m["idle_s"] = int(meta.get("idle_s") or 300)
    m["max_s"] = int(meta.get("max_s") or 3600)
    META[sid] = m
    _save(m)
    _TAILS[sid] = asyncio.create_task(_tail(sid))
    log.info("session %s tracked (biz=%s idx=%d)", sid, m.get("biz_ref"), idx)


def touch(sid: str, **fields) -> dict | None:
    """控制面改音色/方言、记下用户刚说的话（speak chat / 打字）；识别结果也从这里进。"""
    m = META.get(sid)
    if not m:
        return None
    m.update({k: v for k, v in fields.items() if k in ("voice", "dialect", "pending_user")})
    m["last_activity"] = time.time()
    return m


def emit(m: dict, event: str, data: dict) -> None:
    body = {"event": event, "event_id": uuid.uuid4().hex, "ts": time.time(), "session_id": m["id"],
            "biz_ref": m.get("biz_ref"), "callback_url": m.get("callback_url"), "node": cluster.NODE_ID, "data": data}
    cluster.spawn(_send(body))


async def _send(body: dict) -> None:
    if await cluster.post_event(body):
        return
    SPOOL.mkdir(parents=True, exist_ok=True)       # 控制面都不在：落盘，flush_spool 定时重发
    (SPOOL / f"{int(body['ts'] * 1000)}-{body['event_id']}.json").write_text(json.dumps(body, ensure_ascii=False), "utf-8")
    log.warning("event %s spooled", body["event"])


async def flush_spool() -> None:
    while True:
        await asyncio.sleep(10)
        files = sorted(SPOOL.glob("*.json")) if SPOOL.is_dir() else []
        if not files:
            continue
        for f in files[:50]:
            try:
                body = json.loads(f.read_text("utf-8"))
            except (OSError, ValueError):
                f.unlink(missing_ok=True)
                continue
            if not await cluster.post_event(body):
                break
            f.unlink(missing_ok=True)


async def end(sid: str, reason: str) -> dict | None:
    m = META.pop(sid, None)
    if not m:
        return None
    if t := _TAILS.pop(sid, None):
        t.cancel()
    m.update(state="ended", ended_at=time.time(), ended_reason=reason, speaking=False)
    with contextlib.suppress(httpx.HTTPError):
        await cluster.local_clients[m["idx"]].delete(f"/sessions/{sid}", timeout=10)
    cluster.local_sessions.pop(sid, None)
    (DIR / f"{sid}.json").unlink(missing_ok=True)
    emit(m, "session.ended", {"reason": reason, "usage": usage(m)})
    log.info("session %s ended: %s %s", sid, reason, usage(m))
    return m


async def _tail(sid: str) -> None:
    m = META[sid]
    client = cluster.local_clients[m["idx"]]
    try:
        while sid in META:
            try:
                async with client.stream("GET", f"/sessions/{sid}/events", timeout=None) as r:
                    if r.status_code == 404:
                        await end(sid, "error")
                        return
                    ev = ""
                    async for line in r.aiter_lines():
                        if line.startswith("event:"):
                            ev = line[6:].strip()
                            continue
                        if not line.startswith("data:") or ev == "ping":
                            continue
                        d = json.loads(line[5:] or "{}")
                        now = time.time()
                        if ev == "speech.started":
                            m.update(speaking=True, speech_t0=now, last_activity=now)
                            if m["state"] != "live":
                                m.update(state="live", started_at=now)
                                emit(m, "session.started", {})
                        elif ev == "speech.ended":
                            if m.get("speech_t0"):
                                m["talk_s"] += now - m["speech_t0"]
                            m.update(speaking=False, speech_t0=None, last_activity=now, turns=m["turns"] + 1)
                            bot = str(d.get("text") or "")
                            if bot.startswith(SAY_MARK):
                                bot = bot[len(SAY_MARK):]
                            emit(m, "conversation.turn", {"user_text": m.get("pending_user"), "bot_text": bot, "ts": now})
                            m["pending_user"] = None
                            _save(m)
                        elif ev == "session.state_changed" and d.get("state") == "error":
                            await end(sid, "error")
                            return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("tail %s: %s", sid, e)
            if sid in META:
                await asyncio.sleep(2)
    except asyncio.CancelledError:
        return


async def sweeper() -> None:
    while True:
        await asyncio.sleep(10)
        now = time.time()
        for m in list(META.values()):
            if now - m["created_at"] > m["max_s"]:
                await end(m["id"], "max_duration")
            elif now - m["last_activity"] > m["idle_s"] and not m["speaking"]:
                await end(m["id"], "idle_timeout")


def startup() -> None:
    """容器重启：OpenTalking 的会话全没了，磁盘上还记着的会话一律报结束（原因 node_restart）。"""
    if DIR.is_dir():
        for f in DIR.glob("*.json"):
            try:
                m = json.loads(f.read_text("utf-8"))
                m.update(state="ended", ended_at=time.time(), ended_reason="node_restart")
                emit(m, "session.ended", {"reason": "node_restart", "usage": usage(m)})
            except (OSError, ValueError):
                pass
            f.unlink(missing_ok=True)
    cluster.spawn(sweeper())
    cluster.spawn(flush_spool())
