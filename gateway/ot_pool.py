"""控制面的会话调度：把会话分到某个节点的某个 OpenTalking 实例（每张渲染卡一个）。

- 建会话：候选 = 所有在线、非 draining 节点的实例；按 (在线数/cap, 未预热该形象, 节点 id, 实例下标) 选最空的，
  先占位再真正去建（同时来的多个建会话请求不会都挤到同一个实例）；全满抛 Full
- 之后这个会话的所有请求（REST / SSE / WebSocket / 识别后的 /speak）都发到同一个实例
- 形象预热：发给所有实例（各节点本地都有素材，见 cluster.py 的对账）
- 其余请求（形象列表、建形象、ice-config……）发给第一个在线实例
会话归属只记在控制面内存里，节点心跳里带着真实在线会话，掉线节点的会话由 drop_dead_sessions 清掉。
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid

import httpx

import cluster
from config import AVATAR_MODEL
from cluster import Instance

log = logging.getLogger("ot_pool")
_owner: dict[str, Instance] = {}        # session_id（或占位 token）→ 实例
_SID = re.compile(r"^/?sessions/(sess_[\w-]+)")


class Full(Exception):
    """所有渲染实例都满了。"""


class NoNode(Exception):
    """没有在线的渲染节点。"""


def first() -> Instance:
    insts = cluster.instances(include_draining=True)
    if not insts:
        raise NoNode
    return insts[0]


def index_for(path: str) -> Instance:
    m = _SID.match(path)
    if m and (inst := _owner.get(m.group(1))):
        return inst
    return first()


def instance_of(sid: str) -> Instance:
    return _owner.get(sid) or first()


def bind(sid: str, inst: Instance, token: str) -> None:
    """建会话成功：占位换成真正的 session_id。"""
    _owner.pop(token, None)
    _owner[sid] = inst
    inst.sessions.add(sid)


def unbind(sid: str) -> None:
    inst = _owner.pop(sid, None)
    if inst:
        inst.sessions.discard(sid)


def adopt(node: cluster.Node) -> None:
    """心跳里节点报的在线会话，路由表里没有的补上（控制面重启 / 主备切换后重建路由）。"""
    for inst in node.instances:
        for sid in inst.sessions:
            if sid not in _owner:
                _owner[sid] = inst


def load(inst: Instance) -> int:
    """在线数：节点心跳报的 + 本地刚建还没出现在心跳里的 + 占位。"""
    return len(inst.sessions | {s for s, i in _owner.items() if i is inst})


def pick(avatar_id: str, model: str = "") -> tuple[Instance, str]:
    """选实例并立刻占位；返回 (实例, 占位 token)。model 非默认时只在报了支持它的节点里选。"""
    cluster.drop_dead_sessions(unbind)
    insts = cluster.instances()
    if model and model != AVATAR_MODEL:
        insts = [i for i in insts if model in i.node.models]
    if not insts:
        raise NoNode
    token = f"pending:{uuid.uuid4().hex}"
    loads = {i.key: load(i) for i in insts}
    ok = [i for i in insts if not i.cap or loads[i.key] < i.cap]
    if not ok:
        log.warning("all instances full: %s", {k: f"{v}/{cluster.find_instance(k).cap}" for k, v in loads.items()})
        raise Full
    inst = min(ok, key=lambda i: (loads[i.key] / (i.cap or 1e9) if i.cap else loads[i.key],
                                  avatar_id not in i.warm, i.node.id, i.idx))
    _owner[token] = inst          # 计算和占位之间没有 await，不会被别的请求插队
    inst.warm.add(avatar_id)
    log.info("session -> %s load=%s", inst.key, loads)
    return inst, token


async def prewarm(avatar_id: str, body: dict) -> httpx.Response:
    """返回第一个实例的结果；其余实例后台预热。"""
    insts = cluster.instances(include_draining=True)
    if not insts:
        raise NoNode

    async def one(inst: Instance) -> httpx.Response | None:
        try:
            r = await inst.node.client.post(inst.path(f"avatars/{avatar_id}/prewarm"), json=body)
            if r.status_code < 400:
                inst.warm.add(avatar_id)
            return r
        except httpx.HTTPError as e:
            log.warning("prewarm %s on %s failed: %s", avatar_id, inst.key, e)
            return None

    r = await one(insts[0])
    for inst in insts[1:]:
        cluster.spawn(one(inst))
    if r is None:
        return httpx.Response(502, json={"detail": "prewarm failed"})
    return r


async def health() -> dict:
    nodes = {}
    for n in cluster.NODES.values():
        nodes[n.id] = {"alive": n.alive, "url": n.url, "draining": n.draining,
                       "instances": [{"idx": i.idx, "cap": i.cap, "ready": i.ready, "online": load(i), "warm": len(i.warm)}
                                     for i in n.instances]}
    return nodes
