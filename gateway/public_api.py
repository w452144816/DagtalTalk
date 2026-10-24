"""对外接口（只在 control 角色挂载），接口设计见 docs/AI服务多机方案.md 第七节：

- /v1/*  给业务后端：`Authorization: Bearer <AI_SERVICE_KEY>`。会话、形象、音色、任务、容量；事件回调到 callback_url
- /rt/*  给前端：凭建会话时发的 ticket（`X-Session-Ticket` 头或 `?ticket=`），只能操作绑定的那个会话。
         ticket = `sid.exp.hmac`，无状态，备控制面也能校验

会话真源在节点（node_sessions.py：用量、轮次、超时都在那边算），这里的 SESS 只是从心跳重建的缓存；
节点上报的事件（/internal/events）在这里转成对后端的签名回调。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any, Awaitable, Callable

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response

import animate as animator
import cluster
import ot_pool
import tts
from config import AVATAR_MODEL, SYSTEM_PROMPT
from llm_router import SAY_MARK

log = logging.getLogger("public_api")
v1 = APIRouter(prefix="/v1")
rt = APIRouter(prefix="/rt")

SERVICE_KEY = os.getenv("AI_SERVICE_KEY", "")
TICKET_SECRET = (os.getenv("TICKET_SECRET") or cluster.NODE_TOKEN or "dev").encode()
CALLBACK_SECRET = (os.getenv("CALLBACK_SECRET") or SERVICE_KEY).encode()
RT_BASE = f"{cluster.SELF_URL}/rt"       # 前端连发 ticket 的这台控制面（主备各自的地址；有 VIP 时就是 VIP）
TICKET_TTL = int(os.getenv("TICKET_TTL_S", "7200"))
DEFAULT_IDLE_S, DEFAULT_MAX_S = 300, 3600

# app.py 注入：建会话（选实例 + 绑定）、HTTP/WS 反代
create_session: Callable[[bytes, dict[str, str]], Awaitable[httpx.Response]]
proxy_http: Callable[..., Awaitable[Response]]
proxy_ws: Callable[..., Awaitable[None]]

SESS: dict[str, dict[str, Any]] = {}          # session_id → 节点报上来的元数据（缓存）
_IDEM: dict[str, str] = {}                     # Idempotency-Key → session_id
_SEEN: dict[str, float] = {}                   # 已处理的 event_id（节点重发去重）


# ---------- 鉴权 ----------

def _err(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status)


def require_key(request: Request) -> None:
    if not SERVICE_KEY:
        raise HTTPException(503, "AI_SERVICE_KEY 未配置")
    auth = request.headers.get("authorization", "")
    if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), SERVICE_KEY)):
        raise HTTPException(401, "bad service key")


def make_ticket(sid: str, ttl: int = TICKET_TTL) -> tuple[str, int]:
    exp = int(time.time()) + ttl
    sig = hmac.new(TICKET_SECRET, f"{sid}.{exp}".encode(), hashlib.sha256).digest()[:18]
    return f"{sid}.{exp}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}", exp


def verify_ticket(sid: str, ticket: str | None) -> bool:
    try:
        tsid, exp, sig = (ticket or "").rsplit(".", 2)
        if tsid != sid or int(exp) < time.time():
            return False
        want = hmac.new(TICKET_SECRET, f"{sid}.{exp}".encode(), hashlib.sha256).digest()[:18]
        return hmac.compare_digest(base64.urlsafe_b64encode(want).decode().rstrip("="), sig)
    except ValueError:
        return False


def require_ticket(sid: str, request: Request) -> None:
    t = request.headers.get("x-session-ticket") or request.query_params.get("ticket")
    if not verify_ticket(sid, t):
        raise HTTPException(401, "bad or expired ticket")


# ---------- 回调 / 节点事件 ----------

async def _callback(url: str, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    sig = hmac.new(CALLBACK_SECRET, body, hashlib.sha256).hexdigest()
    for delay in (0, 1, 5, 30, 120):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as c:
                r = await c.post(url, content=body, headers={"content-type": "application/json", "X-Signature": sig})
            if r.status_code < 500:
                return
        except httpx.HTTPError as e:
            log.warning("callback %s to %s failed: %s", payload.get("event"), url, e)
    log.error("callback %s to %s gave up", payload.get("event"), url)


def emit(ctx: dict, event: str, data: dict) -> None:
    if url := ctx.get("callback_url"):
        cluster.spawn(_callback(url, {"event": event, "event_id": uuid.uuid4().hex, "ts": time.time(),
                                      "session_id": ctx.get("session_id"), "biz_ref": ctx.get("biz_ref"), "data": data}))


def on_event(body: dict) -> None:
    """节点报来的会话事件：去重 → 更新缓存 → 回调后端。"""
    eid = str(body.get("event_id") or "")
    now = time.time()
    if eid in _SEEN:
        return
    _SEEN[eid] = now
    if len(_SEEN) > 5000:
        for k in [k for k, t in _SEEN.items() if now - t > 600]:
            _SEEN.pop(k, None)
    sid, ev = str(body.get("session_id")), str(body.get("event"))
    data = body.get("data") or {}
    if ev == "session.ended":
        SESS.pop(sid, None)
        ot_pool.unbind(sid)
        u = data.get("usage") or {}
        cluster.note("session", f"会话 {sid} 结束：{data.get('reason')}，{u.get('duration_s')}s / 说话 {u.get('talk_s')}s / {u.get('turns')} 轮",
                     sid=sid)
    elif ev == "session.started" and sid in SESS:
        SESS[sid]["state"] = "live"
    elif ev == "conversation.turn":
        cluster.note("turn", f"{sid}：{(data.get('user_text') or '…')[:30]} → {(data.get('bot_text') or '')[:40]}", sid=sid)
    if url := body.get("callback_url"):
        payload = {k: body.get(k) for k in ("event", "event_id", "ts", "session_id", "biz_ref", "data")}
        cluster.spawn(_callback(url, payload))


def adopt(node: cluster.Node, metas: list[dict]) -> None:
    """心跳里的会话元数据 → 刷新缓存（控制面重启 / 主备切换后也能从这里重建）。"""
    for m in metas:
        m["node_id"] = node.id
        SESS[m["id"]] = m


# ---------- 会话 ----------

def _prompt(body: dict) -> str:
    """system_prompt 的来源：persona.persona_id（结构化人设编译结果）> persona.system_prompt（手写）> 默认；再叠方言和历史。"""
    ps = body.get("persona") or {}
    base = ps.get("system_prompt")
    if ps.get("persona_id"):
        import persona_builder
        _, v = persona_builder.compiled_for(ps["persona_id"], ps.get("version"))
        base = v["compiled"]["system_prompt"]
        if ps.get("extra_prompt"):
            base += "\n" + str(ps["extra_prompt"]).strip()
    p = (base or SYSTEM_PROMPT).strip()
    if d := body.get("dialect"):
        hint = "用粤语口语书写（如「咗、嘅、唔、啲」）" if d == "粤语" else f"多用{d}特有的词汇和语气词"
        p += f"\n请用{d}和对方聊天：{hint}，但要让人听得懂，不要标注拼音或解释。"
    if hist := body.get("history"):
        lines = [f"{'用户' if m.get('role') == 'user' else '你'}：{str(m.get('content', ''))[:200]}" for m in hist[-10:]]
        p += "\n\n这是刚才的对话，请接着聊：\n" + "\n".join(lines)
    return p


def _voice(m: dict) -> str:
    v = m.get("voice") or tts.default_voice()
    return f"{v}@{m['dialect']}" if m.get("dialect") and tts.is_local(v) else v


def _public(m: dict) -> dict:
    keys = ("state", "speaking", "avatar_id", "biz_ref", "created_at", "started_at", "ended_reason", "usage", "node_id")
    return {"session_id": m["id"], "voice_id": m.get("voice"), "dialect": m.get("dialect")} | {k: m.get(k) for k in keys}


def _created(m: dict, ticket: str, exp: int) -> dict:
    return {"session_id": m["id"], "ticket": ticket, "ticket_expires_at": exp, "rt_base_url": RT_BASE,
            "expires_at": int(m["created_at"] + m["max_s"]), "node": m.get("node_id")}


def _get(sid: str) -> dict:
    s = SESS.get(sid)
    if not s:
        raise HTTPException(404, "session not found")
    return s


async def _node(sid: str, method: str, path: str = "", **kw) -> httpx.Response:
    """会话所在节点的 /internal/sessions/{sid}…（节点侧的会话真源）。"""
    inst = ot_pool.instance_of(sid)
    return await inst.node.client.request(method, f"/internal/sessions/{sid}{path}", **kw)


async def _ot(sid: str, method: str, path: str, **kw) -> httpx.Response:
    inst = ot_pool.instance_of(sid)
    return await inst.node.client.request(method, inst.path(f"sessions/{sid}/{path}".rstrip("/")), **kw)


@v1.post("/sessions", status_code=201)
async def v1_create_session(request: Request, body: dict):
    require_key(request)
    if (idem := request.headers.get("idempotency-key")) and (sid := _IDEM.get(idem)) and sid in SESS:
        ticket, exp = make_ticket(sid)
        return JSONResponse(_created(SESS[sid], ticket, exp), status_code=200)
    aid = str(body.get("avatar_id") or "")
    if not aid:
        return _err(400, "bad_request", "avatar_id 必填")
    llm = body.get("llm") or {}
    if llm.get("mode") == "external":
        return _err(400, "unsupported", "llm.mode=external 暂未实现，先用 builtin 或 none")
    ps = body.get("persona") or {}
    persona_id = ps.get("persona_id")
    if persona_id:                       # 结构化人设：没指定音色就用它推荐的；知识走 external 的人设必须配 external LLM
        import persona_builder
        try:
            pobj, pver = persona_builder.compiled_for(persona_id, ps.get("version"))
        except HTTPException as e:
            return _err(e.status_code, "bad_persona", str(e.detail))
        if pobj["status"] != "approved" and not ps.get("allow_draft"):
            return _err(400, "persona_not_approved", "人设还没审核通过（试聊用 /v1/personas/{id}/chat，或传 allow_draft:true）")
        if pver["compiled"]["knowledge_mode"] == "external" and llm.get("mode") != "external":
            return _err(400, "needs_external_llm", "这个人设知识量大，要配 llm.mode=external 由后端做检索")
        if not body.get("voice_id"):
            body["voice_id"] = (pver["data"].get("recommend") or {}).get("voice_id")
    meta = {"avatar_id": aid, "voice": body.get("voice_id"), "dialect": body.get("dialect") or "", "persona_id": persona_id,
            "biz_ref": body.get("biz_ref"), "callback_url": body.get("callback_url"),
            "idle_s": int(body.get("idle_timeout_s") or DEFAULT_IDLE_S), "max_s": int(body.get("max_duration_s") or DEFAULT_MAX_S)}
    ot_body = {"avatar_id": aid, "model": AVATAR_MODEL, "tts_provider": "openai_compatible", "tts_voice": _voice(meta),
               "stt_provider": "sensevoice", "llm_system_prompt": _prompt(body),
               "agent_enabled": False, "memory_enabled": False, "knowledge_enabled": False}
    headers = {"content-type": "application/json",
               "x-session-meta": base64.b64encode(json.dumps(meta, ensure_ascii=False).encode()).decode()}
    try:
        r = await create_session(json.dumps(ot_body).encode(), headers)
    except ot_pool.Full:
        return _err(503, "capacity_full", "当前通话人数已满", retry_after=30)
    except ot_pool.NoNode:
        return _err(503, "no_node", "没有在线的渲染节点", retry_after=30)
    except httpx.HTTPError as e:
        return _err(502, "upstream", f"渲染节点不可用：{e}")
    if r.status_code >= 400:
        return _err(502, "upstream", f"建会话失败：{r.text[:200]}")
    sid = r.json()["session_id"]
    inst = ot_pool.instance_of(sid)
    m = meta | {"id": sid, "state": "created", "speaking": False, "created_at": time.time(), "node_id": inst.node.id,
                "usage": {"duration_s": 0, "talk_s": 0, "turns": 0}}
    SESS[sid] = m
    if idem:
        _IDEM[idem] = sid
    ticket, exp = make_ticket(sid)
    log.info("v1 session %s avatar=%s node=%s biz=%s", sid, aid, inst.key, meta["biz_ref"])
    cluster.note("session", f"/v1 建会话 {sid}（{aid}，biz={meta['biz_ref']}）→ {inst.key}", sid=sid)
    return _created(m, ticket, exp)


@v1.get("/sessions/{sid}")
async def v1_get_session(sid: str, request: Request) -> dict:
    require_key(request)
    _get(sid)
    with contextlib.suppress(httpx.HTTPError):          # 节点上是实时的，缓存最多慢一个心跳
        r = await _node(sid, "GET", timeout=5)
        if r.status_code == 200:
            SESS[sid] = r.json() | {"node_id": SESS[sid].get("node_id")}
        elif r.status_code == 404:
            SESS.pop(sid, None)
            raise HTTPException(404, "session not found")
    return _public(_get(sid))


@v1.patch("/sessions/{sid}")
async def v1_patch_session(sid: str, request: Request, body: dict) -> dict:
    require_key(request)
    s = _get(sid)
    if body.get("system_prompt"):
        return _err(501, "unsupported", "通话中换人设 OpenTalking 不支持，请重建会话")
    fields = {k: v for k, v in (("voice", body.get("voice_id")), ("dialect", body.get("dialect"))) if v is not None}
    if fields:
        r = await _node(sid, "PATCH", json=fields, timeout=5)
        if r.status_code == 200:
            SESS[sid] = r.json() | {"node_id": s.get("node_id")}
    return _public(_get(sid))


async def _speak(sid: str, text: str, mode: str) -> httpx.Response:
    s = _get(sid)
    if mode == "say":
        text = SAY_MARK + text
        await _node(sid, "PATCH", json={}, timeout=5)                   # 只刷活动时间
    else:
        await _node(sid, "PATCH", json={"pending_user": text}, timeout=5)
    return await _ot(sid, "POST", "speak", json={"text": text, "voice": _voice(s), "tts_provider": "openai_compatible"})


@v1.post("/sessions/{sid}/speak")
async def v1_speak(sid: str, request: Request, body: dict):
    require_key(request)
    text = str(body.get("text") or "").strip()
    if not text:
        return _err(400, "bad_request", "text 必填")
    r = await _speak(sid, text, str(body.get("mode") or "chat"))
    if r.status_code >= 400:
        return _err(502, "upstream", r.text[:200])
    return {"ok": True}


@v1.post("/sessions/{sid}/interrupt")
async def v1_interrupt(sid: str, request: Request) -> dict:
    require_key(request)
    _get(sid)
    await _ot(sid, "POST", "interrupt")
    return {"ok": True}


@v1.post("/sessions/{sid}/ticket")
async def v1_ticket(sid: str, request: Request) -> dict:
    require_key(request)
    _get(sid)
    ticket, exp = make_ticket(sid)
    return {"ticket": ticket, "ticket_expires_at": exp, "rt_base_url": RT_BASE}


@v1.delete("/sessions/{sid}")
async def v1_delete_session(sid: str, request: Request) -> dict:
    require_key(request)
    s = _get(sid)
    usage = s.get("usage")
    try:
        r = await _node(sid, "POST", "/end", json={"reason": "hangup"}, timeout=15)
        if r.status_code == 200:
            usage = r.json().get("usage")
    except httpx.HTTPError as e:
        log.warning("end %s on node failed: %s", sid, e)
    SESS.pop(sid, None)
    ot_pool.unbind(sid)
    return {"ok": True, "usage": usage}


# ---------- 容量 / 健康 ----------

@v1.get("/health")
async def v1_health(request: Request) -> dict:
    require_key(request)
    return {"status": "ok", "role": "primary" if cluster.IS_PRIMARY else "standby", "active": cluster.active,
            "controls": cluster.CONTROL_URLS, "nodes": await ot_pool.health(), "sessions": len(SESS)}


@v1.get("/capacity")
async def v1_capacity(request: Request) -> dict:
    require_key(request)
    insts = cluster.instances()
    online = sum(ot_pool.load(i) for i in insts)
    cap = sum(i.cap for i in insts)
    return {"online": online, "max_online": cap, "available": max(cap - online, 0),
            "talking": sum(1 for s in SESS.values() if s.get("speaking")), "instances": len(insts)}


# ---------- 形象 ----------

@v1.get("/avatars")
async def v1_avatars(request: Request, type: str = "") -> list[dict]:
    require_key(request)
    try:
        inst = ot_pool.first()
    except ot_pool.NoNode:
        raise HTTPException(503, "没有在线的渲染节点") from None
    items = (await inst.node.client.get(inst.path("avatars"))).json()
    out = [{"id": a["id"], "name": a.get("name"), "builtin": not a.get("is_custom"), "width": a.get("width"),
            "height": a.get("height"), "preview_url": f"/v1/avatars/{a['id']}/preview"} for a in items]
    if type == "builtin":
        out = [a for a in out if a["builtin"]]
    elif type == "custom":
        out = [a for a in out if not a["builtin"]]
    return out


@v1.post("/avatars", status_code=202)
async def v1_create_avatar(request: Request, name: str = Form(...), image: UploadFile = File(...),
                           animate: bool = Form(True), callback_url: str = Form(""), biz_ref: str = Form(""),
                           clips: int = Form(animator.DEFAULT_CLIPS)) -> dict:
    require_key(request)
    raw = await image.read()
    if not raw or len(raw) > 15 * 1024 * 1024:
        raise HTTPException(400, "照片为空或超过 15MB")
    jid = animator.new_job(name.strip()[:32] or "形象", raw, animate, biz_ref=biz_ref, callback_url=callback_url, clips=clips)
    return {"job_id": jid, "avatar_id": None}


def job_done(job: dict) -> None:
    """animate.py 任务结束时回调后端（控制面重启续跑的任务也走这里）。"""
    ctx = {"callback_url": job.get("callback_url"), "biz_ref": job.get("biz_ref")}
    cluster.note("job", f"建形象 {job.get('name')}：{'完成 ' + str(job.get('avatar_id')) if job['state'] == 'done' else '失败 ' + str(job.get('error'))[:60]}")
    if job["state"] == "done":
        emit(ctx, "avatar.ready", {"avatar_id": job["avatar_id"], "animated": job["animated"], "job_id": job["id"]})
    else:
        emit(ctx, "avatar.failed", {"job_id": job["id"], "error": job.get("error")})


@v1.get("/jobs/{jid}")
async def v1_job(jid: str, request: Request) -> dict:
    require_key(request)
    import persona_builder
    if pj := persona_builder.JOBS.get(jid):
        return {"job_id": jid, "kind": pj["kind"], "persona_id": pj["persona_id"], "material_id": pj.get("material_id"),
                "status": {"running": "running", "done": "done", "error": "failed"}[pj["state"]], "step": pj["step"],
                "result": pj.get("result"), "error": pj.get("error"), "elapsed_s": round(time.time() - pj["t0"])}
    job = animator.JOBS.get(jid)
    if not job:
        raise HTTPException(404, "job not found")
    st = {"running": "running", "done": "done", "error": "failed"}[job["state"]]
    return {"job_id": jid, "status": st, "step": job["step"], "result": {"avatar_id": job["avatar_id"], "animated": job["animated"]},
            "error": job.get("error"), "elapsed_s": round(time.time() - job["t0"])}


@v1.get("/avatars/{aid}")
async def v1_avatar(aid: str, request: Request) -> dict:
    require_key(request)
    for a in await v1_avatars(request):
        if a["id"] == aid:
            return a | {"status": "ready", "nodes": [n.id for n in cluster.alive_nodes() if aid in n.avatars]}
    raise HTTPException(404, "avatar not found")


@v1.get("/avatars/{aid}/preview")
async def v1_avatar_preview(aid: str, request: Request):
    require_key(request)
    inst = ot_pool.first()
    return await proxy_http(inst.node.client, inst.path(f"avatars/{aid}/preview"), request, b"", {})


@v1.delete("/avatars/{aid}")
async def v1_delete_avatar(aid: str, request: Request) -> dict:
    require_key(request)
    ok = 0
    for n in cluster.alive_nodes():
        if n.instances:
            with contextlib.suppress(httpx.HTTPError):
                r = await n.client.delete(n.instances[0].path(f"avatars/{aid}"))
                ok += r.status_code < 400
                n.avatars.discard(aid)
    return {"ok": ok > 0, "nodes": ok}


# ---------- 音色 ----------

@v1.get("/voices/presets")
async def v1_voice_presets(request: Request) -> dict:
    require_key(request)
    return {"groups": tts.voice_groups(), "default_voice": tts.default_voice(),
            "dialects": tts.DIALECTS if tts.local_ready() else [], "clone_ready": tts.clone_ready()}


@v1.post("/voices/clone", status_code=201)
async def v1_voice_clone(request: Request, name: str = Form(...), audio: UploadFile = File(...),
                         biz_ref: str = Form("")) -> dict:
    require_key(request)
    if not tts.clone_ready():
        raise HTTPException(503, "克隆音色服务不可用")
    raw = await audio.read()
    if not raw or len(raw) > 20 * 1024 * 1024:
        raise HTTPException(400, "录音为空或超过 20MB")
    import app as _app     # 控制面没有识别模型，转写交给节点
    try:
        v = await tts.clone(name.strip()[:16] or "声音", raw, _app._transcribe_remote)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    cluster.spawn(_app._warm_voice(v["id"]))
    return {"voice_id": v["id"], "name": v["name"], "ref_text": v["ref_text"], "seconds": v["seconds"], "status": "ready",
            "biz_ref": biz_ref}


@v1.get("/voices/{vid}")
async def v1_voice(vid: str, request: Request) -> dict:
    require_key(request)
    base, _ = tts.split_voice(vid)
    for v in tts.cloned():
        if v["id"] == base:
            return {"voice_id": v["id"], "name": v["name"], "kind": "clone", "ref_text": v["ref_text"]}
    for v in tts.local_voices():
        if v["id"] == base:
            return {"voice_id": v["id"], "name": v["name"], "kind": "preset", "group": v.get("group"), "dialect": v.get("dialect")}
    for g in tts.voice_groups():
        for v in g["voices"]:
            if v["id"] == base:
                return {"voice_id": v["id"], "name": v["label"], "kind": "preset", "group": g["label"]}
    raise HTTPException(404, "voice not found")


@v1.get("/voices/{vid}/preview")
async def v1_voice_preview(vid: str, request: Request, dialect: str = "") -> FileResponse:
    require_key(request)
    voice = f"{vid}@{dialect}" if dialect and tts.is_local(vid) else vid
    return FileResponse(await tts.preview_mp3(voice), media_type="audio/mpeg")


@v1.delete("/voices/{vid}")
async def v1_voice_delete(vid: str, request: Request) -> dict:
    require_key(request)
    await tts.delete_clone(vid)
    cluster.spawn(cluster.fanout(f"/internal/voices/{vid}", "DELETE"))
    return {"ok": True}


# ---------- /rt：前端实时接口 ----------

@rt.get("/sessions/{sid}/ice-config")
async def rt_ice(sid: str, request: Request):
    require_ticket(sid, request)
    inst = ot_pool.instance_of(sid)
    return await proxy_http(inst.node.client, inst.path("sessions/webrtc/ice-config"), request, b"", {})


@rt.post("/sessions/{sid}/webrtc/offer")
async def rt_offer(sid: str, request: Request):
    require_ticket(sid, request)
    inst = ot_pool.instance_of(sid)
    return await proxy_http(inst.node.client, inst.path(f"sessions/{sid}/webrtc/offer"), request, await request.body(),
                            {"content-type": "application/json"})


@rt.post("/sessions/{sid}/start")
async def rt_start(sid: str, request: Request):
    require_ticket(sid, request)
    inst = ot_pool.instance_of(sid)
    return await proxy_http(inst.node.client, inst.path(f"sessions/{sid}/start"), request, b"", {})


@rt.get("/sessions/{sid}/events")
async def rt_events(sid: str, request: Request):
    require_ticket(sid, request)
    inst = ot_pool.instance_of(sid)
    return await proxy_http(inst.node.client, inst.path(f"sessions/{sid}/events"), request, b"", {})


@rt.post("/sessions/{sid}/interrupt")
async def rt_interrupt(sid: str, request: Request) -> dict:
    require_ticket(sid, request)
    await _ot(sid, "POST", "interrupt")
    return {"ok": True}


@rt.post("/sessions/{sid}/text")
async def rt_text(sid: str, request: Request, body: dict):
    require_ticket(sid, request)
    text = str(body.get("text") or "").strip()
    if not text:
        return _err(400, "bad_request", "text 必填")
    r = await _speak(sid, text, "chat")
    return {"ok": r.status_code < 400}


@rt.websocket("/sessions/{sid}/asr")
async def rt_asr(ws: WebSocket, sid: str) -> None:
    if not verify_ticket(sid, ws.query_params.get("ticket")) or sid not in SESS:
        await ws.close(code=1008)
        return
    node = ot_pool.instance_of(sid).node
    await proxy_ws(ws, node.ws_url(f"/internal/asr/{sid}"), cluster.HDR)
