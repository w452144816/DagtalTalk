"""多机：控制面（talk-control）和渲染节点（talk-node）之间的注册、心跳、素材对账。

角色由 GATEWAY_ROLE 决定（见 entrypoint.sh）：
- control：对外 8443。维护节点注册表（NODES），所有会话/形象请求按注册表转发到节点网关的 /internal/ot/{idx}/…
- node：内网 8301。管本机的 OpenTalking 实例（LOCAL_URLS），每 5 秒向控制面心跳：实例、在线会话、已预热形象、本机形象清单；
  心跳的应答里带回 LLM 引擎选择和「你缺的形象/音色去哪拉」，节点后台补齐（素材每台本地全量，不用共享盘）
状态原则：会话真源在节点（OpenTalking 进程里），控制面只是从心跳重建的缓存。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field

import httpx
from fastapi import HTTPException, Request

log = logging.getLogger("cluster")
ROLE = os.getenv("GATEWAY_ROLE", "control")          # control | node
NODE_TOKEN = os.getenv("NODE_TOKEN", "")
# 控制面地址列表，第一个是主、后面是备（没有 VIP 时的应用层主备：节点同时向所有控制面心跳，
# 备机探测到主机不可达才受理请求；后端配同样的地址列表自己切）。有 VIP 时填一个 VIP 即可
CONTROL_URLS = [u.strip().rstrip("/") for u in os.getenv("CONTROL_URLS", os.getenv("CONTROL_URL", "")).split(",") if u.strip()]
CONTROL_URL = CONTROL_URLS[0] if CONTROL_URLS else ""
GATEWAY_PORT = os.getenv("GATEWAY_PORT", "8443")
SELF_URL = f"https://{os.getenv('PUBLIC_HOST', '127.0.0.1')}:{GATEWAY_PORT}"     # 控制面自己对外的地址
IS_PRIMARY = not CONTROL_URLS or SELF_URL == CONTROL_URLS[0]
active = IS_PRIMARY                                   # 备控制面：主机挂了才变 True（watch_primary）
TAKEOVER_S = 10.0
NODE_ID = os.getenv("NODE_ID") or socket.gethostname()
# 本节点能跑的渲染模型（逗号分隔）：quicktalk 本地就有；flashhead / fasterliveportrait 要本机有对应推理服务
NODE_MODELS = [m.strip() for m in os.getenv("NODE_MODELS", os.getenv("AVATAR_MODEL", "quicktalk")).split(",") if m.strip()]
NODE_URL = os.getenv("NODE_URL", "").rstrip("/")     # 控制面访问本节点网关的地址，如 http://<GPU2_LAN_IP>:8301
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "")
HEARTBEAT_S = 5.0
DEAD_S = 15.0
HDR = {"X-Node-Token": NODE_TOKEN}
AVATARS_DIR = os.getenv("OPENTALKING_AVATARS_DIR", "/w/avatars")


def check_token(request: Request) -> None:
    if not NODE_TOKEN or request.headers.get("x-node-token") != NODE_TOKEN:
        raise HTTPException(403, "bad node token")


def check_token_ws(headers) -> bool:
    return bool(NODE_TOKEN) and headers.get("x-node-token") == NODE_TOKEN


# ---------- 控制面：节点注册表 ----------

@dataclass(eq=False)
class Instance:
    node: "Node"
    idx: int
    cap: int = 0
    ready: bool = True                                # 节点上线后这个实例的 QuickTalk worker 热过了吗（没热前不分会话）
    sessions: set[str] = field(default_factory=set)   # 节点心跳报上来的在线会话
    warm: set[str] = field(default_factory=set)       # 已预热的形象

    @property
    def key(self) -> str:
        return f"{self.node.id}/{self.idx}"

    def path(self, p: str) -> str:
        return f"/internal/ot/{self.idx}/{p.lstrip('/')}"


@dataclass(eq=False)
class Node:
    id: str
    url: str
    client: httpx.AsyncClient
    instances: list[Instance] = field(default_factory=list)
    avatars: set[str] = field(default_factory=set)
    public_host: str = ""
    last_seen: float = 0.0
    draining: bool = False
    gpus: list[dict] = field(default_factory=list)   # 节点心跳带的 nvidia-smi 快照
    models: set[str] = field(default_factory=set)    # 节点能跑的渲染模型（心跳带的 NODE_MODELS；空 = 只有默认模型）
    tts: dict = field(default_factory=dict)          # 节点合成池状态（inflight / down）
    first_seen: float = 0.0

    @property
    def alive(self) -> bool:
        return time.time() - self.last_seen < DEAD_S

    def ws_url(self, path: str) -> str:
        return self.url.replace("http", "ws", 1) + "/" + path.lstrip("/")


NODES: dict[str, Node] = {}
RECENT: deque = deque(maxlen=300)                   # 看板用的最近事件（建会话、结束、节点上下线、接管……）


def note(kind: str, text: str, **extra) -> None:
    RECENT.appendleft({"ts": time.time(), "kind": kind, "text": text, **extra})


def _client(url: str) -> httpx.AsyncClient:
    # 空闲连接 2 秒就丢：uvicorn keep-alive 5 秒，复用快到期的连接会撞上服务端关连接（实测偶发 502）
    return httpx.AsyncClient(base_url=url, headers=HDR, timeout=httpx.Timeout(300.0, connect=5.0), trust_env=False,
                             limits=httpx.Limits(keepalive_expiry=2.0))


def alive_nodes() -> list[Node]:
    return [n for n in NODES.values() if n.alive]


def instances(include_draining: bool = False) -> list[Instance]:
    return [i for n in alive_nodes() if include_draining or not n.draining for i in n.instances
            if include_draining or i.ready]


def find_instance(key: str) -> Instance | None:
    nid, _, idx = key.rpartition("/")
    n = NODES.get(nid)
    if n and idx.isdigit() and int(idx) < len(n.instances):
        return n.instances[int(idx)]
    return None


def on_heartbeat(body: dict, voices: list[dict], provider: str) -> dict:
    """节点心跳：更新注册表；应答里告诉节点缺哪些形象/音色、当前 LLM 引擎。"""
    nid, url = str(body["node_id"]), str(body["url"]).rstrip("/")
    n = NODES.get(nid)
    if n is None or n.url != url:
        if n is not None:
            asyncio.get_running_loop().create_task(n.client.aclose())
        n = NODES[nid] = Node(nid, url, _client(url), first_seen=time.time())
        log.info("node %s registered at %s", nid, url)
        note("node", f"节点 {nid} 注册（{url}）")
    elif not n.alive:
        log.info("node %s back online", nid)
        note("node", f"节点 {nid} 重新上线")
    reported = body.get("instances") or []
    while len(n.instances) < len(reported):
        n.instances.append(Instance(n, len(n.instances)))
    del n.instances[len(reported):]
    for inst, r in zip(n.instances, reported):
        inst.cap = int(r.get("cap") or 0)
        inst.ready = bool(r.get("ready", True))
        inst.sessions = set(r.get("sessions") or [])
        inst.warm = set(r.get("warm") or [])
    n.avatars = set(body.get("avatars") or [])
    n.public_host = str(body.get("public_host") or "")
    n.gpus = list(body.get("gpus") or [])
    n.models = set(body.get("models") or [])
    n.tts = dict(body.get("tts") or {})
    n.last_seen = time.time()
    # 对账：别的节点有、这个节点没有的形象 → 让它去那个节点拉
    have_elsewhere: dict[str, Node] = {}
    for other in alive_nodes():
        if other is not n:
            for a in other.avatars:
                have_elsewhere.setdefault(a, other)
    fetch = [{"id": a, "from": f"{src.url}/internal/avatars/{a}/archive"}
             for a, src in have_elsewhere.items() if a not in n.avatars]
    return {"provider": provider, "fetch_avatars": fetch[:5],
            "voices": [{"id": v["id"], "name": v["name"], "ref_text": v["ref_text"],
                        "from": f"{SELF_URL}/internal/voices/{v['id']}.wav"} for v in voices]}


def drop_dead_sessions(unbind) -> None:
    """节点掉线：它上面的会话从路由表里清掉（会话本身随节点一起没了，前端会重连）。"""
    for n in NODES.values():
        if not n.alive:
            dropped = 0
            for inst in n.instances:
                for sid in list(inst.sessions):
                    unbind(sid)
                    dropped += 1
                inst.sessions.clear()
            if dropped:
                note("node", f"节点 {n.id} 掉线，清掉 {dropped} 个会话")


def gpu_stats() -> list[dict]:
    """节点：nvidia-smi 快照（容器里能看到分给它的卡）。"""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 5:
            gpus.append({"index": int(p[0]), "name": p[1].replace("NVIDIA ", "").replace("GeForce ", ""),
                         "util": int(p[2] or 0), "mem_used": int(p[3] or 0), "mem_total": int(p[4] or 0)})
    return gpus


async def fanout(path: str, method: str = "POST", json: dict | None = None, nodes: list[Node] | None = None,
                 timeout: float = 120) -> dict[str, int]:
    """同一个请求发给每个在线节点（节点网关自己的 /internal/* 接口），返回 {node_id: status}。"""
    async def one(n: Node) -> tuple[str, int]:
        try:
            r = await n.client.request(method, path, json=json, timeout=timeout)
            return n.id, r.status_code
        except httpx.HTTPError as e:
            log.warning("fanout %s %s to %s failed: %s", method, path, n.id, e)
            return n.id, 0
    return dict(await asyncio.gather(*[one(n) for n in (nodes if nodes is not None else alive_nodes())]))


async def sync_avatar(avatar_id: str, source: Node) -> None:
    """新形象建在 source 节点上，让其他节点立刻去拉（不等心跳对账）。"""
    others = [n for n in alive_nodes() if n is not source]
    if not others:
        return
    src = f"{source.url}/internal/avatars/{avatar_id}/archive"
    res = await fanout(f"/internal/avatars/{avatar_id}/import", json={"from": src}, nodes=others, timeout=300)
    log.info("avatar %s synced from %s -> %s", avatar_id, source.id, res)
    for n in others:
        if res.get(n.id) == 200:
            n.avatars.add(avatar_id)


# ---------- 节点侧：本机实例、心跳 ----------

LOCAL_URLS = [u.strip().rstrip("/") for u in os.getenv("OT_URLS", os.getenv("OT_URL", "http://127.0.0.1:8000")).split(",")
              if u.strip()]
LOCAL_CAP = int(os.getenv("OT_MAX_SESSIONS_PER_INSTANCE", "0"))
local_clients = [httpx.AsyncClient(base_url=u, timeout=httpx.Timeout(300.0, connect=5.0), trust_env=False,
                                   limits=httpx.Limits(keepalive_expiry=2.0)) for u in LOCAL_URLS]
local_sessions: dict[str, int] = {}                   # 本机 session_id → 实例下标
local_warm: list[set[str]] = [set() for _ in LOCAL_URLS]
local_ready: list[bool] = [False for _ in LOCAL_URLS]  # warmup() 跑过一次会话后置 True
remote_provider: str = ""                             # 控制面选的 LLM 引擎（心跳应答带回）
extra_heartbeat = lambda: {}                          # noqa: E731  节点侧挂钩：心跳里再带点什么（会话元数据，见 node_sessions）
_BG: set[asyncio.Task] = set()


def local_avatars() -> list[str]:
    try:
        return sorted(d for d in os.listdir(AVATARS_DIR) if os.path.isfile(os.path.join(AVATARS_DIR, d, "manifest.json")))
    except OSError:
        return []


async def prune_local() -> None:
    async def alive(sid: str, idx: int) -> None:
        try:
            r = await local_clients[idx].get(f"/sessions/{sid}", timeout=3)
            if r.status_code == 404:
                local_sessions.pop(sid, None)
        except httpx.HTTPError:
            pass
    await asyncio.gather(*[alive(s, i) for s, i in list(local_sessions.items())])


_ctl_clients: dict[str, httpx.AsyncClient] = {}


def ctl_client(url: str) -> httpx.AsyncClient:
    if url not in _ctl_clients:
        _ctl_clients[url] = httpx.AsyncClient(base_url=url, headers=HDR, timeout=10, verify=False, trust_env=False)
    return _ctl_clients[url]


async def heartbeat_loop(apply) -> None:
    """节点：每 5 秒向所有控制面报到（主备都收，备机接管时注册表已经是热的）；
    `apply(resp)` 只处理当前受理那一台的应答（拉缺的素材、更新引擎）。"""
    if not CONTROL_URLS:
        log.warning("CONTROL_URLS not set, node runs standalone")
        return
    tick = 0
    while True:
        try:
            if tick % 3 == 0:
                await prune_local()
            body = {"node_id": NODE_ID, "url": NODE_URL, "public_host": PUBLIC_HOST, "avatars": local_avatars(),
                    "models": NODE_MODELS,
                    "instances": [{"idx": i, "cap": LOCAL_CAP, "ready": local_ready[i],
                                   "sessions": [s for s, j in local_sessions.items() if j == i],
                                   "warm": sorted(local_warm[i])} for i in range(len(LOCAL_URLS))]}
            body.update(extra_heartbeat())
            body["gpus"] = await asyncio.to_thread(gpu_stats)

            async def one(url: str) -> dict | None:
                try:
                    r = await ctl_client(url).post("/internal/nodes/heartbeat", json=body)
                    if r.status_code == 200:
                        return r.json()
                    log.warning("heartbeat to %s rejected: %s %s", url, r.status_code, r.text[:120])
                except httpx.HTTPError as e:
                    if tick % 12 == 0:
                        log.warning("heartbeat to %s failed: %s", url, type(e).__name__)
                return None
            results = await asyncio.gather(*[one(u) for u in CONTROL_URLS])
            for resp in results:              # 第一个在线且正在受理的控制面说了算
                if resp and resp.get("active", True):
                    await apply(resp)
                    break
        except Exception as e:  # noqa: BLE001
            log.warning("heartbeat loop: %s", e)
        tick += 1
        await asyncio.sleep(HEARTBEAT_S)


async def warmup(idx: int, avatar_id: str = "") -> None:
    """节点上线：这个实例先空跑一次会话，把 QuickTalk worker（HuBERT 等模型）加载起来再接流量。
    不热的话第一路会话的 webrtc/offer 要等 runner 就绪，TITAN V 上超过 OpenTalking 的 60 秒就 500。"""
    c = local_clients[idx]
    t0 = time.time()
    try:
        while time.time() - t0 < 600:                    # 等 OpenTalking 起来
            try:
                if (await c.get("/health", timeout=3)).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(3)
        aid = avatar_id or next((a for a in local_avatars() if a == "anchor"), None) or (local_avatars() or [""])[0]
        if not aid:
            return
        r = await c.post("/sessions", json={"avatar_id": aid, "model": "quicktalk", "tts_provider": "openai_compatible",
                                            "stt_provider": "sensevoice", "agent_enabled": False,
                                            "memory_enabled": False, "knowledge_enabled": False}, timeout=60)
        if r.status_code >= 400:
            log.warning("warmup %d: create session failed %s", idx, r.text[:120])
            return
        sid = r.json()["session_id"]
        for _ in range(5):                               # /start 会等 runner 就绪（worker 建完）再返回，最多 120 秒
            try:
                rs = await c.post(f"/sessions/{sid}/start", timeout=200)
            except httpx.HTTPError:
                break
            if rs.status_code == 409:                    # runner 还没注册上，稍等再试
                await asyncio.sleep(3)
                continue
            break
        with contextlib.suppress(httpx.HTTPError):
            await c.delete(f"/sessions/{sid}", timeout=10)
        local_warm[idx].add(aid)
        log.info("instance %d warmed with %s in %.0fs", idx, aid, time.time() - t0)
    except Exception as e:  # noqa: BLE001
        log.warning("warmup %d failed: %s", idx, e)
    finally:
        local_ready[idx] = True                          # 热不热得起来都放行，别让一张卡永远闲着


async def post_event(body: dict) -> bool:
    """节点 → 控制面：会话事件。按主、备顺序投递，谁在受理谁收（备机不受理时回 409）。"""
    for url in CONTROL_URLS:
        try:
            r = await ctl_client(url).post("/internal/events", json=body, timeout=5)
            if r.status_code < 400:
                return True
        except httpx.HTTPError:
            continue
    return False


async def watch_primary() -> None:
    """备控制面：主机连续 TAKEOVER_S 不可达 → 开始受理；主机回来 → 让回去（主机优先，避免双活）。"""
    global active
    down_since: float | None = None
    async with httpx.AsyncClient(timeout=3, verify=False, trust_env=False) as c:
        while True:
            try:
                r = await c.get(f"{CONTROL_URLS[0]}/health")
                ok = r.status_code == 200 and r.json().get("active", True)
            except Exception:  # noqa: BLE001
                ok = False
            now = time.time()
            if ok:
                if active:
                    log.warning("primary %s is back, standby goes passive", CONTROL_URLS[0])
                    note("control", f"主控制面 {CONTROL_URLS[0]} 恢复，本机退回备机")
                active, down_since = False, None
            else:
                down_since = down_since or now
                if not active and now - down_since >= TAKEOVER_S:
                    log.warning("primary %s down for %.0fs, standby takes over", CONTROL_URLS[0], now - down_since)
                    note("control", f"主控制面 {CONTROL_URLS[0]} 不可达 {now - down_since:.0f} 秒，本机接管")
                    active = True
            await asyncio.sleep(2)


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _BG.add(t)
    t.add_done_callback(_BG.discard)


@contextlib.asynccontextmanager
async def download(url: str):
    async with httpx.AsyncClient(headers=HDR, timeout=httpx.Timeout(600, connect=10), verify=False, trust_env=False) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            yield r
