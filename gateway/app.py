"""实时数字人对话网关。同一份代码按 GATEWAY_ROLE 跑两种角色（见 cluster.py）：

control（对外 https 8443，容器 talk-control，不用 GPU）
- /                     手机优先的单页（上传照片 → 视频通话）
- /api/*                页面用的小接口：引擎切换、音色、形象任务、识别 WS（转到会话所在节点）
- /ot/*                 反代 OpenTalking（REST / SSE / WebSocket）：按会话路由到节点网关的 /internal/ot/{idx}/*
- /internal/nodes/*     节点心跳、drain；/internal/voices/*.wav 给节点拉克隆音色的参考音频

node（内网 http 8301，容器 talk-node，和本机 OpenTalking 实例同容器）
- /internal/ot/{idx}/*  反代本机第 idx 个 OpenTalking 实例（带 X-Node-Token）
- /internal/asr/{sid}   语音识别 WS（识别模型在节点，随机器数扩）
- /internal/avatars/*   形象目录打包 / 导入（节点之间互拉）
- /v1/audio/speech、/v1/chat/completions  给本机 OpenTalking 回调用的 TTS / LLM 路由（仅本机可调）
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

import animate as animator
import asr
import clone_tts
import cluster
import llm_router
import memory
import node_sessions
import ot_pool
import personas
import tts
from config import (AVATAR_MODEL, BASE_AVATAR, LLMS, RENDER_MODELS, SYSTEM_PROMPT, WEB_DIR,
                    current_provider, load_state, save_state)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("gateway")
logging.getLogger("httpx").setLevel(logging.WARNING)     # 心跳、存活探测每几秒一条，INFO 太吵
ROLE = cluster.ROLE
IS_CONTROL, IS_NODE = ROLE == "control", ROLE == "node"


async def _apply_heartbeat(resp: dict) -> None:
    """节点：处理控制面的心跳应答——引擎选择、缺的形象去拉、缺的克隆音色去注册。"""
    cluster.remote_provider = resp.get("provider") or ""
    for a in resp.get("fetch_avatars") or []:
        if a["id"] not in _importing:
            _importing.add(a["id"])
            cluster.spawn(_import_avatar(a["id"], a["from"]))
    if tts.LOCAL_TTS_URL and (want := resp.get("voices")):
        have = {v["id"] for v in tts.local_voices()}
        for v in want:
            if v["id"] not in have and v["id"] not in _importing:
                _importing.add(v["id"])
                cluster.spawn(_import_voice(v))


_importing: set[str] = set()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    if IS_NODE:
        asr.preload()
        if clone_tts.available() and not tts.LOCAL_TTS_URL:   # 没有本地合成服务时克隆退回进程内 Qwen3-TTS
            ref = next((tts._ref_of(v["id"]) for v in tts.cloned()), None)
            clone_tts.preload(*(ref or (None, None)))
        cluster.extra_heartbeat = lambda: {
            "sessions_meta": [node_sessions.public(m) for m in node_sessions.META.values()],
            "tts": {"urls": tts.LOCAL_TTS_URLS, "max": tts.LOCAL_TTS_MAX, "inflight": dict(tts._inflight),
                    "down": [u for u, t in tts._down_until.items() if t > time.time()]}}
        node_sessions.startup()
        cluster.spawn(cluster.heartbeat_loop(_apply_heartbeat))
        for i in range(len(cluster.LOCAL_URLS)):
            cluster.spawn(cluster.warmup(i))
    if IS_CONTROL:
        animator.resume()
        persona_builder.startup()
        if not cluster.IS_PRIMARY:
            cluster.spawn(cluster.watch_primary())
    log.info("gateway role=%s node=%s primary=%s", ROLE, cluster.NODE_ID, cluster.IS_PRIMARY)
    yield


_PREWARM_LOCKS: dict = {}
_MEM_TASKS: set = set()      # 后台记忆提炼任务（留引用）   # 渲染实例 idx -> 预热锁

app = FastAPI(title=f"talk-{ROLE}", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def _standby_gate(request: Request, call_next):
    """备控制面没接管时只收心跳、答健康检查，其余一律 503 并告诉对方主机在哪（避免双活）。"""
    if IS_CONTROL and not cluster.active:
        p = request.url.path
        if not (p == "/health" or p.startswith("/internal/nodes/") or p.startswith("/static/")):
            return JSONResponse({"error": {"code": "standby", "message": "备控制面未接管", "primary": cluster.CONTROL_URL}},
                                status_code=503, headers={"Retry-After": "5", "X-Primary": cluster.CONTROL_URL})
    return await call_next(request)
if IS_CONTROL:
    import public_api
    public_api.create_session = lambda body, headers: ot_create_session(body, headers)
    public_api.proxy_http = lambda client, path, request, body, headers: _proxy_http(client, path, request, body, headers)
    public_api.proxy_ws = lambda ws, url, headers=None: _proxy_ws(ws, url, headers)
    import persona_builder
    app.include_router(public_api.v1)
    app.include_router(public_api.rt)
    app.include_router(persona_builder.router)
    import vidu_live                      # 全身实时通话（第三方实时数字人，见 vidu_live.py；页面 /rt）
    app.include_router(vidu_live.router)
    animator.on_done = public_api.job_done
HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade",
       "accept-encoding", "te", "trailer", "proxy-authorization", "proxy-authenticate"}


def _need(role: str) -> None:
    if ROLE != role:
        raise HTTPException(404, f"not a {role}")


@app.get("/")
async def index() -> Response:
    # 静态资源带版本号（文件修改时间）：手机浏览器会按启发式缓存旧的 css/js，部署后不刷新
    html = (WEB_DIR / "index.html").read_text("utf-8")
    for name in ("style.css", "app.js"):
        v = int((WEB_DIR / name).stat().st_mtime)
        html = html.replace(f"/static/{name}\"", f"/static/{name}?v={v}\"")
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health() -> dict:
    if IS_NODE:
        async def one(c: httpx.AsyncClient) -> bool:
            try:
                return (await c.get("/health", timeout=3)).status_code == 200
            except httpx.HTTPError:
                return False
        up = list(await asyncio.gather(*[one(c) for c in cluster.local_clients]))
        return {"ok": True, "role": ROLE, "node": cluster.NODE_ID, "instances": up,
                "sessions": len(cluster.local_sessions), "controls": cluster.CONTROL_URLS}
    nodes = await ot_pool.health()
    return {"ok": True, "role": ROLE, "primary": cluster.IS_PRIMARY, "active": cluster.active, "self": cluster.SELF_URL,
            "controls": cluster.CONTROL_URLS, "opentalking": any(n["alive"] for n in nodes.values()), "nodes": nodes,
            "provider": current_provider()}


# ---------- 页面接口（control） ----------

@app.get("/api/config")
async def get_config() -> dict:
    return {
        "provider": current_provider(),
        "providers": [{"key": k, "label": c.label, "model": c.model, "ready": c.ready} for k, c in LLMS.items()],
        "voice_groups": tts.voice_groups(),
        "default_voice": tts.default_voice(),
        "clone_ready": tts.clone_ready(),
        "dialects": tts.DIALECTS if tts.local_ready() else [],
        "base_avatar": BASE_AVATAR,
        "avatar_model": AVATAR_MODEL,
        "render_models": RENDER_MODELS,     # 多于一个时页面出现"渲染模型"下拉
        "system_prompt": SYSTEM_PROMPT,
        "personas": [p | {"voice": tts.map_voice(p["voice"])} for p in personas.as_list()],
        "default_persona": personas.DEFAULT,
        "avatar_personas": personas.AVATAR_PERSONA,   # 形象 id/名字前缀 → 人设 id，选形象时自动带人设
        "animate_ready": animator.video_ready(),
    }


@app.get("/api/personas")
async def api_personas() -> list[dict]:
    """演示页的「结构化人设」下拉：已 build 过的人设 + 编译好的提示词 + 推荐音色（草稿也列出来方便测）。"""
    _need("control")
    out = []
    for p in persona_builder.list_all():
        try:
            _, v = persona_builder.compiled_for(p["persona_id"])
        except HTTPException:
            continue
        out.append({"id": p["persona_id"], "name": p["name"], "status": p["status"], "version": v["version"],
                    "prompt": v["compiled"]["system_prompt"], "knowledge_mode": v["compiled"]["knowledge_mode"],
                    "voice": tts.map_voice((v["data"].get("recommend") or {}).get("voice_id") or ""),
                    "opening": (v["data"].get("recommend") or {}).get("opening") or ""})
    return out


@app.post("/api/personas")
async def api_persona_create(request: Request) -> dict:
    """演示页「用素材创建人设」：name + 若干文件（按 MIME 分 doc/image/audio/video）+ 网址 → 一键流水线，返回任务号轮询。"""
    _need("control")
    form = await request.form()
    name = str(form.get("name") or "").strip()[:40]
    if not name:
        raise HTTPException(400, "先给人设起个名字")
    hint = str(form.get("hint") or "")
    files: list[tuple[str, str, bytes]] = []
    total = 0
    for f in form.getlist("files"):
        if not hasattr(f, "read"):
            continue
        raw = await f.read()
        if not raw:
            continue
        total += len(raw)
        if total > 300 * 1024 * 1024:
            raise HTTPException(400, "素材总大小超过 300MB")
        ct = (f.content_type or "").lower()
        ext = Path(f.filename or "").suffix.lower()
        kind = ("image" if ct.startswith("image/") else "audio" if ct.startswith("audio/") else "video" if ct.startswith("video/")
                else "image" if ext in (".jpg", ".jpeg", ".png", ".webp") else "audio" if ext in (".mp3", ".wav", ".m4a", ".aac", ".ogg")
                else "video" if ext in (".mp4", ".mov", ".mkv", ".webm") else "doc")
        files.append((kind, f.filename or kind, raw))
    urls = [u.strip() for u in str(form.get("urls") or "").splitlines() if u.strip().startswith(("http://", "https://"))]
    seed = {}
    if intro := str(form.get("intro") or "").strip():
        seed = {"identity": {"intro": intro[:300]}}
    if not files and not urls and not seed:
        raise HTTPException(400, "至少传一份素材，或写一句介绍")
    pl = persona_builder.start_pipeline(name, hint, files, urls, seed)
    return {"job": pl["id"], "persona_id": pl["persona_id"]}


@app.get("/api/personas/jobs/{jid}")
async def api_persona_job(jid: str) -> dict:
    _need("control")
    pl = persona_builder.PIPELINES.get(jid)
    if not pl:
        raise HTTPException(404, "任务不存在")
    return pl | {"elapsed": round(time.time() - pl["t0"])}


@app.post("/api/config")
async def set_config(body: dict) -> dict:
    p = body.get("provider")
    if p not in LLMS:
        raise HTTPException(400, f"未知引擎 {p}")
    if not LLMS[p].ready:
        raise HTTPException(400, f"{LLMS[p].label} 未配置 API Key")
    state = load_state()
    state["provider"] = p
    save_state(state)
    log.info("llm provider -> %s", p)
    return {"provider": p}


@app.post("/api/avatars")
async def create_avatar(name: str = Form(...), image: UploadFile = File(...), animate: bool = Form(True),
                        clips: int = Form(animator.DEFAULT_CLIPS)) -> dict:
    """照片建形象（后台任务）：animate 时先生成 clips 段眨眼/手势待机视频拼成底板再建，GET /api/jobs/{id} 查进度。"""
    _need("control")
    raw = await image.read()
    if not raw or len(raw) > 15 * 1024 * 1024:
        raise HTTPException(400, "照片为空或超过 15MB")
    return {"job": animator.new_job(name.strip()[:32] or "我的形象", raw, animate, clips=clips)}


@app.get("/api/jobs/{jid}")
async def job_status(jid: str) -> dict:
    job = animator.JOBS.get(jid)
    if not job:
        raise HTTPException(404, "任务不存在")
    return {k: v for k, v in job.items() if k != "t0"} | {"elapsed": round(time.time() - job["t0"])}


@app.get("/api/voices/preview")
async def voice_preview(voice: str) -> FileResponse:
    try:
        return FileResponse(await tts.preview_mp3(voice), media_type="audio/mpeg")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"试听失败：{e}") from e


@app.post("/api/voices/clone")
async def voice_clone(name: str = Form(...), audio: UploadFile = File(...)) -> dict:
    _need("control")
    if not tts.clone_ready():
        raise HTTPException(400, "克隆音色模型没装好")
    raw = await audio.read()
    if not raw or len(raw) > 20 * 1024 * 1024:
        raise HTTPException(400, "录音为空或超过 20MB")
    try:
        v = await tts.clone(name.strip()[:16] or "我的声音", raw, _transcribe_remote)
        cluster.spawn(_warm_voice(v["id"]))
        return v
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        log.exception("clone failed")
        raise HTTPException(502, str(e)) from e


def _transcribe_remote(pcm16: bytes) -> tuple[str, float]:
    """控制面没有识别模型：录音转写交给任意一个在线节点。"""
    node = ot_pool.first().node
    r = httpx.post(f"{node.url}/internal/transcribe", content=pcm16, headers=cluster.HDR, timeout=60, trust_env=False)
    r.raise_for_status()
    d = r.json()
    return d["text"], d["ms"]


async def _warm_voice(vid: str) -> None:
    """新克隆的音色先合成一句：首次要加载参考音频 + 捕获 CUDA graph，免得通话第一句卡住。"""
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(timeout=180, trust_env=False) as c:
            await c.post(tts.TTS_URL, json={"input": "你好。", "voice": vid, "response_format": "pcm"})


@app.delete("/api/voices/{vid}")
async def voice_delete(vid: str) -> dict:
    _need("control")
    await tts.delete_clone(vid)
    cluster.spawn(cluster.fanout(f"/internal/voices/{vid}", "DELETE"))
    return {"ok": True}


# ---------- 节点内部：给本机 OpenTalking 回调的 TTS / LLM ----------

@app.post("/v1/audio/speech")
async def audio_speech(request: Request):
    """OpenTalking 的 openai_compatible TTS 调这里（仅本机），回 16k s16le PCM 流。"""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "local only")
    body = await request.json()
    text, voice = str(body.get("input") or ""), str(body.get("voice") or "")
    log.info("tts %s %r", voice, text[:40])
    return StreamingResponse(tts.speech_pcm(text, voice, str(body.get("model") or "")), media_type="audio/pcm")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "local only")
    body = await request.json()
    messages = body.get("messages") or []
    last = str((messages[-1] if messages else {}).get("content") or "")
    if last.startswith(llm_router.SAY_MARK):     # 后端 speak(mode=say)：原样念，不走模型
        return StreamingResponse(llm_router.echo(last[len(llm_router.SAY_MARK):]), media_type="text/event-stream")
    provider = cluster.remote_provider if cluster.remote_provider in LLMS else current_provider()
    # 用户插话打断时，被打断的那句回答不进历史：模型看到两条连着的用户消息，会先回上一句（实测问"年轻时候的事"，
    # 她先接着聊上一句"升职加薪"）。→ 中间补一句"被打断了"，让模型接最新这句
    fixed = []
    for m in messages:
        if fixed and m.get("role") == "user" and fixed[-1].get("role") == "user":
            fixed.append({"role": "assistant", "content": "（刚才的话还没说完，就被对方打断了）"})
        fixed.append(m)
    messages = fixed
    system = next((str(m.get("content") or "") for m in messages if m.get("role") == "system"), "")
    log.info("chat via %s, %d messages, system=%r, last=%r", provider, len(messages), system[:24],
             str((messages[-1] if messages else {}).get("content", ""))[:60])
    mk = memory.split_marker(system)
    if mk is None:
        return StreamingResponse(llm_router.stream(provider, messages, int(body.get("max_tokens") or 600)),
                                 media_type="text/event-stream")
    # 长期记忆：去掉标记，把记得的事拼进系统提示词；回答流完后后台提炼这一轮
    clean, uid, char = mk
    k = memory.key(uid, char)
    mem = await memory.load(k)
    extra = memory.prompt(mem)
    messages = [dict(m) for m in messages]
    for m in messages:
        if m.get("role") == "system":
            m["content"] = clean + ("\n\n" + extra if extra else "")
            break
    user_text = str((messages[-1] if messages else {}).get("content") or "")

    async def gen():
        reply = []
        try:
            async for piece in llm_router.stream(provider, messages, int(body.get("max_tokens") or 600)):
                for line in piece.splitlines():
                    if line.startswith("data:") and "[DONE]" not in line:
                        with contextlib.suppress(Exception):
                            reply.append((json.loads(line[5:])["choices"][0].get("delta") or {}).get("content") or "")
                yield piece
        finally:
            # 被打断（流没正常结束）也要记这一轮；任务留引用，免得被回收
            t = asyncio.get_running_loop().create_task(memory.remember(k, user_text, "".join(reply), provider))
            _MEM_TASKS.add(t)
            t.add_done_callback(_MEM_TASKS.discard)
    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------- 通用反代 ----------

async def _proxy_http(client: httpx.AsyncClient, path: str, request: Request, body: bytes,
                      headers: dict[str, str]) -> Response:
    for attempt in range(2):
        req = client.build_request(request.method, path, params=request.query_params, headers=headers, content=body)
        try:
            resp = await client.send(req, stream=True)
            break
        except (httpx.RemoteProtocolError, httpx.ConnectError) as e:
            # 连接在发出前/没收到任何响应就断了：请求没到上游，重发一次是安全的
            log.warning("proxy %s %s failed (%s: %s)%s", request.method, path, type(e).__name__, e,
                        ", retrying" if attempt == 0 else "")
            if attempt:
                return JSONResponse({"detail": f"上游不可用：{e}"}, status_code=502)
        except httpx.HTTPError as e:
            log.warning("proxy %s %s failed (%s: %s)", request.method, path, type(e).__name__, e)
            return JSONResponse({"detail": f"上游不可用：{e}"}, status_code=502)
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP | {"content-encoding"}}
    if "text/event-stream" in resp.headers.get("content-type", ""):
        out_headers["X-Accel-Buffering"] = "no"
        out_headers["Cache-Control"] = "no-cache"
    return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=out_headers,
                             background=BackgroundTask(resp.aclose))


async def _ws_connect(url: str, headers: dict[str, str]):
    try:
        return await websockets.connect(url, max_size=None, open_timeout=10, additional_headers=headers)
    except TypeError:   # websockets < 14
        return await websockets.connect(url, max_size=None, open_timeout=10, extra_headers=headers)


async def _proxy_ws(ws: WebSocket, url: str, headers: dict[str, str] | None = None) -> None:
    await ws.accept()
    try:
        up = await _ws_connect(url, headers or {})
        async with up:
            async def down_to_up() -> None:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        await up.close()
                        return
                    if msg.get("bytes") is not None:
                        await up.send(msg["bytes"])
                    elif msg.get("text") is not None:
                        await up.send(msg["text"])

            async def up_to_down() -> None:
                async for m in up:
                    if isinstance(m, bytes):
                        await ws.send_bytes(m)
                    else:
                        await ws.send_text(m)

            tasks = [asyncio.create_task(down_to_up()), asyncio.create_task(up_to_down())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                with contextlib.suppress(Exception, WebSocketDisconnect):
                    t.result()
    except Exception as e:  # noqa: BLE001
        log.warning("ws proxy %s failed: %s", url, e)
    with contextlib.suppress(Exception):
        await ws.close()


def _fwd_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in HOP | {"x-node-token"}}


# ---------- control：/ot/* 按会话路由到节点 ----------

async def ot_create_session(body: bytes, headers: dict[str, str]) -> httpx.Response:
    """选实例 → 占位 → 到节点建会话 → 绑定 session_id。抛 Full / NoNode / httpx.HTTPError。"""
    _b = json.loads(body or b"{}")
    inst, token = ot_pool.pick(str(_b.get("avatar_id", "")), str(_b.get("model", "")))
    try:
        try:
            r = await inst.node.client.post(inst.path("sessions"), content=body, headers=headers)
        except (httpx.RemoteProtocolError, httpx.ConnectError) as e:   # 连接没送达，重发一次
            log.warning("create session on %s failed (%s), retrying", inst.key, type(e).__name__)
            r = await inst.node.client.post(inst.path("sessions"), content=body, headers=headers)
    finally:
        ot_pool.unbind(token)
    if r.status_code < 400 and r.json().get("session_id"):
        ot_pool.bind(r.json()["session_id"], inst, token)
        cluster.note("session", f"建会话 {r.json()['session_id']} → {inst.key}", sid=r.json()["session_id"])
    return r


@app.get("/rt")
async def rt_page() -> Response:
    _need("control")
    html = (WEB_DIR / "rt.html").read_text("utf-8")
    for name in ("style.css", "rt.js"):
        v = int((WEB_DIR / name).stat().st_mtime)
        html = html.replace(f"/static/{name}\"", f"/static/{name}?v={v}\"")
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/dashboard")
async def dashboard_page() -> FileResponse:
    _need("control")
    return FileResponse(WEB_DIR / "dashboard.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/dashboard")
async def dashboard_data() -> dict:
    """看板数据：控制面状态、节点/实例/GPU、会话、合成池、任务、最近事件。每 2 秒拉一次。"""
    _need("control")
    now = time.time()
    nodes = []
    for n in cluster.NODES.values():
        nodes.append({"id": n.id, "url": n.url, "alive": n.alive, "draining": n.draining, "age": round(now - n.last_seen, 1),
                      "up_since": n.first_seen, "avatars": len(n.avatars), "gpus": n.gpus, "tts": n.tts,
                      "instances": [{"idx": i.idx, "cap": i.cap, "ready": i.ready, "online": ot_pool.load(i),
                                     "sessions": sorted(i.sessions), "warm": sorted(i.warm)} for i in n.instances]})
    insts = cluster.instances()
    sessions = sorted(public_api.SESS.values(), key=lambda s: s.get("created_at") or 0, reverse=True)
    demo = [{"id": sid, "node": inst.key} for sid, inst in ot_pool._owner.items()
            if not sid.startswith("pending:") and sid not in public_api.SESS]
    return {
        "now": now,
        "control": {"self": cluster.SELF_URL, "primary": cluster.IS_PRIMARY, "active": cluster.active,
                    "controls": cluster.CONTROL_URLS, "provider": current_provider(), "node_id": cluster.NODE_ID},
        "capacity": {"online": sum(ot_pool.load(i) for i in insts), "max_online": sum(i.cap for i in insts),
                     "instances": len(insts), "talking": sum(1 for s in sessions if s.get("speaking"))},
        "nodes": nodes,
        "sessions": [{k: s.get(k) for k in ("id", "state", "speaking", "avatar_id", "voice", "dialect", "biz_ref", "node_id",
                                            "created_at", "started_at", "usage")} for s in sessions],
        "demo_sessions": demo,
        "jobs": [{k: v for k, v in j.items() if k not in ("callback_url",)} for j in
                 sorted(animator.JOBS.values(), key=lambda j: j["t0"], reverse=True)[:20]],
        "personas": persona_builder.list_all(),
        "persona_jobs": sorted(persona_builder.JOBS.values(), key=lambda j: j["t0"], reverse=True)[:20],
        "events": list(cluster.RECENT)[:100],
    }


@app.websocket("/ot/{path:path}")
async def ws_proxy(ws: WebSocket, path: str) -> None:
    try:
        inst = ot_pool.index_for(path)
    except ot_pool.NoNode:
        await ws.close(code=1013)
        return
    await _proxy_ws(ws, inst.node.ws_url(inst.path(path)), cluster.HDR)


@app.api_route("/ot/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def http_proxy(path: str, request: Request):
    _need("control")
    headers = _fwd_headers(request)
    body = await request.body()
    path = path.strip("/")
    try:
        if request.method == "POST" and re.fullmatch(r"avatars/[^/]+/prewarm", path):   # 每个渲染实例都预热
            r = await ot_pool.prewarm(path.split("/")[1], json.loads(body or b"{}"))
            return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
        if request.method == "POST" and path == "sessions":       # 建会话：挑最空闲的渲染实例
            try:
                r = await ot_create_session(body, headers)
            except ot_pool.Full:      # 满员时让新用户等，不把已在通话的人拖卡
                return JSONResponse({"detail": "当前通话人数已满，请稍等一会儿再试"}, status_code=503)
            except httpx.HTTPError as e:
                return JSONResponse({"detail": f"渲染节点不可用：{e}"}, status_code=502)
            return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
        if request.method == "POST" and path == "avatars/custom":   # 建形象：建在一个节点上，再同步给其他节点
            inst = ot_pool.first()
            r = await inst.node.client.post(inst.path(path), content=body, headers=headers, timeout=600)
            if r.status_code < 400 and (aid := r.json().get("id")):
                inst.node.avatars.add(aid)
                await cluster.sync_avatar(aid, inst.node)
            return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
        if request.method == "DELETE" and re.fullmatch(r"avatars/[^/]+", path):   # 删形象：每个节点都删
            res = None
            for n in cluster.alive_nodes():
                if n.instances:
                    with contextlib.suppress(httpx.HTTPError):
                        rr = await n.client.delete(n.instances[0].path(path), headers=headers)
                        n.avatars.discard(path.split("/")[1])
                        res = res or rr
            if res is None:
                raise ot_pool.NoNode
            return Response(res.content, status_code=res.status_code, media_type=res.headers.get("content-type"))
        inst = ot_pool.index_for(path)
    except ot_pool.NoNode:
        return JSONResponse({"detail": "没有在线的渲染节点"}, status_code=503)
    if request.method == "DELETE" and re.fullmatch(r"sessions/sess_[\w-]+", path):
        ot_pool.unbind(path.split("/")[1])
    return await _proxy_http(inst.node.client, inst.path(path), request, body, headers)


@app.websocket("/api/asr/{sid}")
async def asr_ws_control(ws: WebSocket, sid: str) -> None:
    """页面的语音输入：转给会话所在节点识别（识别模型在节点上）。"""
    try:
        node = ot_pool.instance_of(sid).node
    except ot_pool.NoNode:
        await ws.close(code=1013)
        return
    await _proxy_ws(ws, node.ws_url(f"/internal/asr/{sid}"), cluster.HDR)


@app.get("/internal/memory")
async def internal_memory_get(key: str, request: Request) -> dict:
    _need("control")
    cluster.check_token(request)
    return memory.load_local(re.sub(r"[^\w-]", "", key))


@app.put("/internal/memory")
async def internal_memory_put(key: str, request: Request) -> dict:
    _need("control")
    cluster.check_token(request)
    memory.save_local(re.sub(r"[^\w-]", "", key), await request.json())
    return {"ok": True}


@app.get("/api/memory")
async def api_memory_get(uid: str, avatar: str) -> dict:
    """页面：看看她记得什么（设置里"记忆"）。"""
    _need("control")
    m = memory.load_local(memory.key(uid, avatar))
    return {"facts": m.get("facts") or [], "last_seen": m.get("last_seen") or 0, "turns": m.get("turns") or 0}


@app.delete("/api/memory")
async def api_memory_delete(uid: str, avatar: str) -> dict:
    _need("control")
    memory.delete_local(memory.key(uid, avatar))
    return {"ok": True}


@app.post("/internal/events")
async def node_events(request: Request, body: dict) -> dict:
    """节点上报的会话事件（session.started / conversation.turn / session.ended）→ 更新缓存、回调后端。"""
    _need("control")
    cluster.check_token(request)
    public_api.on_event(body)
    return {"ok": True}


@app.post("/internal/nodes/heartbeat")
async def node_heartbeat(request: Request) -> dict:
    _need("control")
    cluster.check_token(request)
    body = await request.json()
    resp = cluster.on_heartbeat(body, tts.cloned(), current_provider())
    node = cluster.NODES[str(body["node_id"])]
    ot_pool.adopt(node)
    public_api.adopt(node, body.get("sessions_meta") or [])
    resp["active"] = cluster.active          # 备机没接管时节点不按它的应答行事
    return resp


@app.post("/internal/nodes/{nid}/drain")
async def node_drain(nid: str, request: Request, body: dict) -> dict:
    """发版前：不再给这个节点分新会话，等老会话自己结束。"""
    _need("control")
    cluster.check_token(request)
    n = cluster.NODES.get(nid)
    if not n:
        raise HTTPException(404, "unknown node")
    n.draining = bool(body.get("draining", True))
    return {"node": nid, "draining": n.draining}


@app.get("/internal/voices/{vid}.wav")
async def voice_wav(vid: str, request: Request) -> FileResponse:
    _need("control")
    cluster.check_token(request)
    safe = re.sub(r"[^\w-]", "", vid)
    p = tts.VOICES_DIR / f"{safe}.wav"
    if not p.is_file():
        raise HTTPException(404, "no such voice")
    return FileResponse(p, media_type="audio/wav")


# ---------- node：本机 OpenTalking 实例、识别、素材 ----------

def _local(idx: int) -> httpx.AsyncClient:
    if idx < 0 or idx >= len(cluster.local_clients):
        raise HTTPException(404, "no such instance")
    return cluster.local_clients[idx]


@app.websocket("/internal/ot/{idx}/{path:path}")
async def node_ws_proxy(ws: WebSocket, idx: int, path: str) -> None:
    if not IS_NODE or not cluster.check_token_ws(ws.headers):
        await ws.close(code=1008)
        return
    await _proxy_ws(ws, cluster.LOCAL_URLS[idx].replace("http", "ws", 1) + "/" + path)


@app.get("/internal/emotion")
async def internal_emotion(text: str, request: Request) -> dict:
    """OpenTalking 渲染进程问：这句话（字幕文本）LLM 标的是什么情绪（情绪底板按它选段）。只给本机进程用。"""
    if (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "local only")
    return {"emotion": llm_router.emotion_for(text)}


@app.api_route("/internal/ot/{idx}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def node_http_proxy(idx: int, path: str, request: Request):
    _need("node")
    cluster.check_token(request)
    client = _local(idx)
    headers = _fwd_headers(request)
    body = await request.body()
    path = path.strip("/")
    if request.method == "POST" and path == "sessions":
        meta_b64 = request.headers.get("x-session-meta")      # /v1 建的会话：元数据交给节点保管
        r = await client.post("/sessions", content=body, headers=headers)
        if r.status_code < 400 and (sid := r.json().get("session_id")):
            cluster.local_sessions[sid] = idx
            cluster.local_warm[idx].add(str(json.loads(body or b"{}").get("avatar_id", "")))
            if meta_b64:
                node_sessions.register(sid, idx, json.loads(base64.b64decode(meta_b64)))
        return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
    if request.method == "POST" and (m := re.fullmatch(r"avatars/([^/]+)/prewarm", path)):
        # 同一渲染实例的预热排队做：QuickTalk 只缓存 1 个形象，页面打开、点形象、开通话各发一次预热，
        # 并发时两个形象互相挤掉，会话可能拿到刚被关掉的 worker（实测首块渲染 10 秒）
        async with _PREWARM_LOCKS.setdefault(idx, asyncio.Lock()):
            r = await client.post("/" + path, content=body, headers=headers, timeout=600)
        if r.status_code < 400:
            cluster.local_warm[idx].add(m.group(1))
        return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
    if request.method == "DELETE" and (m := re.fullmatch(r"sessions/(sess_[\w-]+)", path)):
        cluster.local_sessions.pop(m.group(1), None)
        if m.group(1) in node_sessions.META:        # 演示页直接删的 /v1 会话也要报结束
            await node_sessions.end(m.group(1), "hangup")
            return JSONResponse({"ok": True})
    return await _proxy_http(client, "/" + path, request, body, headers)


@app.patch("/internal/sessions/{sid}")
async def node_session_patch(sid: str, request: Request, body: dict) -> dict:
    """控制面改会话：音色 / 方言 / 记下用户刚说的话（speak chat、打字）。"""
    _need("node")
    cluster.check_token(request)
    m = node_sessions.touch(sid, **body)
    if not m:
        raise HTTPException(404, "session not tracked")
    return node_sessions.public(m)


@app.get("/internal/sessions/{sid}")
async def node_session_get(sid: str, request: Request) -> dict:
    _need("node")
    cluster.check_token(request)
    m = node_sessions.META.get(sid)
    if not m:
        raise HTTPException(404, "session not tracked")
    return node_sessions.public(m)


@app.post("/internal/sessions/{sid}/end")
async def node_session_end(sid: str, request: Request, body: dict) -> dict:
    _need("node")
    cluster.check_token(request)
    m = await node_sessions.end(sid, str(body.get("reason") or "hangup"))
    if not m:
        raise HTTPException(404, "session not tracked")
    return node_sessions.public(m)


@app.post("/internal/transcribe")
async def node_transcribe(request: Request) -> dict:
    _need("node")
    cluster.check_token(request)
    text, ms = await asyncio.to_thread(asr.transcribe, await request.body())
    return {"text": text, "ms": ms}


@app.websocket("/internal/asr/{sid}")
async def asr_ws(ws: WebSocket, sid: str) -> None:
    """一段话：先发 {"type":"meta","voice":...}，再发 16k s16le PCM 二进制，最后 {"type":"end"}。
    识别完回 {"text"}，有字就交给本机 OpenTalking /speak（走 LLM → TTS → 口型）。"""
    if not IS_NODE or not cluster.check_token_ws(ws.headers):
        await ws.close(code=1008)
        return
    await ws.accept()
    meta: dict = {}
    buf = bytearray()
    CONV_STATE[sid] = time.time()          # 用户开口了（浏览器 VAD 检测到说话才建这条连接）：数字人切"倾听"
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                buf += msg["bytes"]
                continue
            body = json.loads(msg.get("text") or "{}")
            if body.get("type") == "meta":
                meta = body
            elif body.get("type") == "end":
                break
        CONV_STATE.pop(sid, None)
        text, ms = await asyncio.to_thread(asr.transcribe, bytes(buf))
        log.info("asr %.1fs audio -> %.0f ms %r", len(buf) / 32000, ms, text)
        if not text:
            await ws.send_json({"error": "未能识别有效语音，请重试。"})
            return
        await ws.send_json({"text": text})
        node_sessions.touch(sid, pending_user=text)
        client = cluster.local_clients[cluster.local_sessions.get(sid, 0)]
        r = await client.post(f"/sessions/{sid}/speak", json={
            "text": text, "voice": meta.get("voice") or None, "tts_provider": "openai_compatible"})
        if r.status_code >= 400:
            await ws.send_json({"error": f"发送失败：{r.text[:200]}"})
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa: BLE001
        log.exception("asr ws failed")
        with contextlib.suppress(Exception):
            await ws.send_json({"error": f"识别出错：{e}"})
    finally:
        CONV_STATE.pop(sid, None)
        with contextlib.suppress(Exception):
            await ws.close()


CONV_STATE: dict[str, float] = {}       # 会话 → 用户开始说话的时刻（说完/断开即删）


@app.get("/internal/conv_state")
async def internal_conv_state(sid: str, request: Request) -> dict:
    """OpenTalking 渲染进程问：这个会话的用户是不是正在说话（情绪底板切"倾听"段用）。只给本机进程用。"""
    if (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "local only")
    t = CONV_STATE.get(sid)
    if t is not None and time.time() - t > 60:      # 兜底：连接异常没清掉
        CONV_STATE.pop(sid, None)
        t = None
    return {"listening": t is not None}


_AID = re.compile(r"^[\w.\-一-鿿]+$")


@app.get("/internal/avatars/{aid}/archive")
async def avatar_archive(aid: str, request: Request):
    """把本机的一个形象目录打成 tar.gz 流（给别的节点拉）。"""
    _need("node")
    cluster.check_token(request)
    if not _AID.match(aid) or not Path(cluster.AVATARS_DIR, aid, "manifest.json").is_file():
        raise HTTPException(404, "no such avatar")
    p = await asyncio.create_subprocess_exec("tar", "-C", cluster.AVATARS_DIR, "-czf", "-", aid,
                                             stdout=asyncio.subprocess.PIPE)

    async def gen():
        try:
            while chunk := await p.stdout.read(1 << 16):
                yield chunk
        finally:
            if p.returncode is None:
                p.kill()
            await p.wait()
    return StreamingResponse(gen(), media_type="application/gzip")


async def _import_avatar(aid: str, src: str) -> None:
    """从别的节点拉一个形象目录到本机（先解到临时目录，整体换进去）。"""
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory(dir=cluster.AVATARS_DIR, prefix=".import-") as tmp:
            tgz = Path(tmp, "a.tgz")
            async with cluster.download(src) as r:
                with tgz.open("wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
            p = await asyncio.create_subprocess_exec("tar", "-C", tmp, "-xzf", str(tgz))
            if await p.wait() or not Path(tmp, aid, "manifest.json").is_file():
                raise RuntimeError("bad archive")
            dst = Path(cluster.AVATARS_DIR, aid)
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(Path(tmp, aid)), str(dst))
        log.info("avatar %s imported from %s in %.1fs", aid, src, time.time() - t0)
    except Exception as e:  # noqa: BLE001
        log.warning("import avatar %s from %s failed: %s", aid, src, e)
    finally:
        _importing.discard(aid)


@app.post("/internal/avatars/{aid}/import")
async def avatar_import(aid: str, request: Request, body: dict) -> dict:
    _need("node")
    cluster.check_token(request)
    if not _AID.match(aid):
        raise HTTPException(400, "bad id")
    _importing.add(aid)
    await _import_avatar(aid, str(body["from"]))
    if not Path(cluster.AVATARS_DIR, aid, "manifest.json").is_file():
        raise HTTPException(502, "import failed")
    return {"ok": True}


async def _import_voice(v: dict) -> None:
    """控制面有、本机合成服务没有的克隆音色：拉参考 wav 并注册。"""
    vid = v["id"]
    try:
        wav = tts.VOICES_DIR / f"{vid}.wav"
        if not wav.is_file():
            tts.VOICES_DIR.mkdir(parents=True, exist_ok=True)
            tmp = wav.with_suffix(".part")
            async with cluster.download(v["from"]) as r:
                with tmp.open("wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
            tmp.replace(wav)
        async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
            r = await c.post(f"{tts.LOCAL_TTS_URL}/voices/register",
                             json={"id": vid, "name": v["name"], "ref_text": v["ref_text"], "wav": str(wav)})
            r.raise_for_status()
        if not any(x["id"] == vid for x in tts.cloned()):     # 记进本机登记表：talk-tts 重启时按它重新加载
            tts._save_cloned([{"id": vid, "name": v["name"], "ref_text": v["ref_text"], "created": int(time.time())}]
                             + tts.cloned())
        tts.invalidate_local()
        log.info("voice %s registered from control", vid)
    except Exception as e:  # noqa: BLE001
        log.warning("import voice %s failed: %s", vid, e)
    finally:
        _importing.discard(vid)


@app.delete("/internal/voices/{vid}")
async def node_voice_delete(vid: str, request: Request) -> dict:
    _need("node")
    cluster.check_token(request)
    await tts.delete_clone(vid)
    return {"ok": True}
