"""长期记忆：按"用户 × 形象"记住聊过的事，下次聊天接得上。

- 用户是谁：页面给每个浏览器生成一个固定编号（localStorage），建会话时把
  `​[MEM <用户编号> <形象 id>]` 标记追加在系统提示词末尾；OpenTalking 每轮调 LLM 时会原样带回来。
- 用：网关 /v1/chat/completions（节点）看到标记就去掉它，把记忆拼进系统提示词（"你记得的关于对方的事"）。
- 记：这一轮回答说完后，后台让 LLM 从"已有记忆 + 这轮对话"里提炼、合并成新的记忆列表（最多 MAX 条）。
- 存：控制面 /w/state/memory/<key>.json（两台节点共用一份；节点经 /internal/memory 读写，按主、备顺序）。
  备控制面接管期间读不到主机上的记忆（磁盘不共享），主机回来即恢复。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path

import cluster
import llm_router

log = logging.getLogger("memory")
DIR = Path("/w/state/memory")
MAX = 40
MARK_RE = re.compile(r"​\[MEM ([\w-]{6,64}) ([^\]\n]{1,160})\]")
_locks: dict[str, asyncio.Lock] = {}


def key(uid: str, char: str) -> str:
    """文件名：用户编号 + 形象 id 的摘要（形象 id 含中文，避免文件名问题）。"""
    safe = re.sub(r"[^\w-]", "", uid)[:64]
    return safe + "__" + hashlib.sha1(char.encode()).hexdigest()[:16]


def split_marker(system: str) -> tuple[str, str, str] | None:
    """从系统提示词里取出记忆标记：返回 (去掉标记的提示词, 用户编号, 形象 id)；没有标记返回 None。"""
    m = MARK_RE.search(system or "")
    if not m:
        return None
    return (system[:m.start()] + system[m.end():]).rstrip(), m.group(1), m.group(2)


# ---------- 存储：控制面本地文件；节点经控制面读写 ----------

def load_local(k: str) -> dict:
    p = DIR / f"{k}.json"
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {"facts": [], "last_seen": 0, "turns": 0}


def save_local(k: str, data: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / f"{k}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(DIR / f"{k}.json")


def delete_local(k: str) -> None:
    (DIR / f"{k}.json").unlink(missing_ok=True)


async def load(k: str) -> dict:
    if cluster.ROLE == "control" or not cluster.CONTROL_URLS:
        return load_local(k)
    for url in cluster.CONTROL_URLS:
        try:
            r = await cluster.ctl_client(url).get("/internal/memory", params={"key": k}, timeout=1.0)
            if r.status_code < 400:
                return r.json()
        except Exception:  # noqa: BLE001
            continue
    log.warning("memory load %s: no control plane reachable", k)
    return {"facts": [], "last_seen": 0, "turns": 0}


async def save(k: str, data: dict) -> None:
    if cluster.ROLE == "control" or not cluster.CONTROL_URLS:
        save_local(k, data)
        return
    for url in cluster.CONTROL_URLS:
        try:
            r = await cluster.ctl_client(url).put("/internal/memory", params={"key": k}, json=data, timeout=5)
            if r.status_code < 400:
                return
        except Exception:  # noqa: BLE001
            continue
    log.warning("memory save %s: no control plane reachable", k)


# ---------- 用：拼进系统提示词 ----------

def _ago(ts: float) -> str:
    if not ts:
        return ""
    d = (time.time() - ts) / 86400
    if d < 0.02:
        return ""                                    # 刚聊过（同一次通话里）：不提
    if d < 1:
        return "你们今天早些时候刚聊过。"
    if d < 2:
        return "你们昨天聊过。"
    if d < 30:
        return f"你们上次聊天是{int(d)}天前。"
    return "你们有一个多月没聊了。"


def prompt(mem: dict) -> str:
    facts = [f for f in mem.get("facts") or [] if f]
    ago = _ago(mem.get("last_seen") or 0)
    if not facts and not ago:
        return ""
    lines = ["【你记得的关于对方的事（以前聊天时知道的）。自然地用上，像亲人一样记得，不要一条条念出来，"
             "也不要说“我记得你说过”这种生硬的话；对方没提到的不要硬扯】"]
    if ago:
        lines.append(ago)
    lines += [f"- {f}" for f in facts[-MAX:]]
    return "\n".join(lines)


# ---------- 记：每轮回答后提炼合并 ----------

EXTRACT = """你在帮一个陪伴型数字人整理“关于对方的长期记忆”。
已有记忆（JSON 数组）：
{facts}

刚才这一轮对话：
对方：{user}
数字人：{bot}

请输出更新后的完整记忆列表（JSON 字符串数组），规则：
1. 只记以后聊天用得上的、关于对方的事实：名字/称呼、年龄、工作学习、住在哪、家里人、身体状况、最近发生的事（带上大概时间，如“10 月 23 日说最近加班很累”）、喜好、愿望打算；以及数字人答应过对方的事。
2. 和已有记忆重复的合并，过时的更新，不要丢掉仍然有效的旧记忆。
3. 每条不超过 30 个字，用第三人称“对方……”，最多 {max} 条，重要的在前。
4. 这一轮没有新信息就原样输出已有记忆。
只输出 JSON 数组，不要别的文字。"""


async def remember(k: str, user_text: str, bot_text: str, provider: str) -> None:
    if not user_text.strip():
        return
    lock = _locks.setdefault(k, asyncio.Lock())
    async with lock:
        mem = await load(k)
        facts = list(mem.get("facts") or [])
        today = time.strftime("%m 月 %d 日")
        msg = EXTRACT.format(facts=json.dumps(facts, ensure_ascii=False), user=user_text.strip()[:600],
                             bot=bot_text.strip()[:600], max=MAX).replace("10 月 23 日", today)
        try:
            out = await llm_router.complete(provider, [{"role": "user", "content": msg}], max_tokens=3000, temperature=0.2)
            m = re.search(r"\[.*\]", out, re.S)
            if not m:
                log.warning("memory extract: no JSON array in reply (%d chars), keep old", len(out or ""))
            raw = json.loads(m.group(0)) if m else facts
            new = []
            for x in raw:                                 # 模型偶尔给对象 {"content": ...}：只要文字
                if isinstance(x, dict):
                    x = x.get("content") or x.get("text") or x.get("fact") or "；".join(str(v) for v in x.values())
                x = str(x).strip()
                if x and x not in new:
                    new.append(x)
            new = new[:MAX]
            if not new and facts:                         # 模型偶尔回空数组：不当真，保留旧记忆
                log.warning("memory extract returned empty list, keep %d old facts", len(facts))
                new = facts
        except Exception as e:  # noqa: BLE001
            log.warning("memory extract failed (%s), keep old", e)
            new = facts
        mem.update(facts=new, last_seen=time.time(), turns=int(mem.get("turns") or 0) + 1)
        await save(k, mem)
        if new != facts:
            log.info("memory %s: %d -> %d facts", k, len(facts), len(new))
