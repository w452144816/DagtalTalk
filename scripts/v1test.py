"""对外接口冒烟（后端视角 /v1 + 前端视角 /rt），在 talk-control 容器里跑：

docker cp scripts/v1test.py talk-control:/tmp/ && docker exec -e AI_SERVICE_KEY=... talk-control python /tmp/v1test.py [--base https://127.0.0.1:8443] [--avatar anchor]

步骤：capacity → 建会话（拿 ticket）→ /rt start + events(SSE) → /v1 speak(say) → speak(chat) → /rt text →
收 speech.ended → GET 会话 → 用 ticket 调 /rt interrupt → DELETE 拿用量。WebRTC 不在这里验证。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

import httpx


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:8443")
    ap.add_argument("--avatar", default="anchor")
    ap.add_argument("--voice", default="")
    a = ap.parse_args()
    key = os.environ["AI_SERVICE_KEY"]
    H = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(base_url=a.base, verify=False, timeout=120) as c:
        print("capacity", (await c.get("/v1/capacity", headers=H)).json())
        r = await c.post("/v1/sessions", headers=H | {"Idempotency-Key": "t1"}, json={
            "avatar_id": a.avatar, "voice_id": a.voice or None, "persona": {"system_prompt": "你是一个客服数字人，回答简短。"},
            "history": [{"role": "user", "content": "我叫小王"}, {"role": "assistant", "content": "你好小王。"}],
            "idle_timeout_s": 120, "biz_ref": "order_1"})
        print("create", r.status_code, r.text[:200])
        r.raise_for_status()
        d = r.json()
        sid, ticket = d["session_id"], d["ticket"]
        T = {"X-Session-Ticket": ticket}
        print("bad ticket ->", (await c.post(f"/rt/sessions/{sid}/start", headers={"X-Session-Ticket": "x.1.y"})).status_code)
        print("start", (await c.post(f"/rt/sessions/{sid}/start", headers=T)).status_code)
        print("ice", (await c.get(f"/rt/sessions/{sid}/ice-config", headers=T)).status_code)
        ended = asyncio.Event()
        texts: list[str] = []

        async def events() -> None:
            async with c.stream("GET", f"/rt/sessions/{sid}/events", headers=T, timeout=None) as s:
                ev = ""
                async for line in s.aiter_lines():
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:") and ev in ("speech.started", "speech.ended"):
                        data = json.loads(line[5:] or "{}")
                        print(f"  +{time.time() - t0:5.1f}s {ev} {str(data.get('text', ''))[:50]}")
                        if ev == "speech.ended":
                            texts.append(str(data.get("text", "")))
                            if len(texts) >= 3:
                                ended.set()
                                return
        t0 = time.time()
        task = asyncio.create_task(events())
        await asyncio.sleep(1)
        print("say", (await c.post(f"/v1/sessions/{sid}/speak", headers=H, json={"text": "您好，欢迎光临。请问有什么可以帮您？", "mode": "say"})).status_code)
        await asyncio.sleep(6)
        print("chat", (await c.post(f"/v1/sessions/{sid}/speak", headers=H, json={"text": "我叫什么名字？一句话回答"})).status_code)
        await asyncio.sleep(6)
        print("rt text", (await c.post(f"/rt/sessions/{sid}/text", headers=T, json={"text": "再见"})).status_code)
        try:
            await asyncio.wait_for(ended.wait(), 60)
        except asyncio.TimeoutError:
            print("  !! timeout waiting speech.ended")
        task.cancel()
        print("idem ->", (await c.post("/v1/sessions", headers=H | {"Idempotency-Key": "t1"}, json={"avatar_id": a.avatar})).status_code)
        print("get", (await c.get(f"/v1/sessions/{sid}", headers=H)).json())
        print("interrupt", (await c.post(f"/rt/sessions/{sid}/interrupt", headers=T)).status_code)
        print("delete", (await c.delete(f"/v1/sessions/{sid}", headers=H)).json())
        print("get after delete ->", (await c.get(f"/v1/sessions/{sid}", headers=H)).status_code)
        print("avatars", [x["id"] for x in (await c.get("/v1/avatars?type=builtin", headers=H)).json()][:5])
        print("presets groups", [g["label"] for g in (await c.get("/v1/voices/presets", headers=H)).json()["groups"]])


asyncio.run(main())
