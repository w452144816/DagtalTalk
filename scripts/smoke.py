"""端到端冒烟：在服务器上跑（容器内或宿主机均可），不依赖浏览器。

python smoke.py [--base https://127.0.0.1:8443] [--photo face.jpg [--animate]] [--wav hello.wav] [--provider ark|minimax]

步骤：/health → /api/config 切引擎 → (可选)上传照片建形象 → 建会话 → start →
speak 一句文本（走 LLM）→ (可选)把 wav 以 16k PCM 推到网关 /api/asr（网关识别后转 /speak）→
从 SSE 收 speech.started / subtitle.chunk / speech.ended，打印耗时 → 删会话。
WebRTC 不在这里验证（需要浏览器）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import subprocess
import time

import httpx
import websockets


def pcm16k(path: str) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                          check=True, capture_output=True).stdout


async def watch(c: httpx.AsyncClient, sid: str, until: int, t0: float, timeout: float = 120) -> None:
    ended = 0
    async with c.stream("GET", f"/ot/sessions/{sid}/events", timeout=None) as r:
        ev = ""
        deadline = time.time() + timeout
        async for line in r.aiter_lines():
            if time.time() > deadline:
                print("  !! timeout waiting events")
                return
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:") and ev != "ping":
                data = json.loads(line[5:] or "{}")
                print(f"  +{time.time() - t0:5.1f}s {ev} {str(data.get('text', data.get('state', '')))[:60]}")
                if ev == "speech.ended":
                    ended += 1
                    if ended >= until:
                        return


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:8443")
    ap.add_argument("--photo")
    ap.add_argument("--wav")
    ap.add_argument("--provider", default="ark")
    ap.add_argument("--avatar")
    ap.add_argument("--voice", default="Chinese (Mandarin)_Gentle_Youth")
    ap.add_argument("--animate", action="store_true", help="--photo 走网关 /api/avatars 生成动态形象")
    a = ap.parse_args()
    async with httpx.AsyncClient(base_url=a.base, verify=False, timeout=300) as c:
        print("health", (await c.get("/health")).json())
        print("provider", (await c.post("/api/config", json={"provider": a.provider})).json())
        cfg = (await c.get("/api/config")).json()
        avatar = a.avatar
        if a.photo and a.animate:
            t = time.time()
            with open(a.photo, "rb") as f:
                r = await c.post("/api/avatars", data={"name": "smoke", "animate": "true"},
                                 files={"image": ("p.jpg", f, "image/jpeg")})
            r.raise_for_status()
            job = r.json()["job"]
            while (j := (await c.get(f"/api/jobs/{job}")).json())["state"] == "running":
                await asyncio.sleep(2)
            print(f"animated avatar job {j} in {time.time() - t:.1f}s")
            if j["state"] != "done":
                return
            avatar = j["avatar_id"]
        elif a.photo:
            t = time.time()
            with open(a.photo, "rb") as f:
                r = await c.post("/ot/avatars/custom", data={"base_avatar_id": cfg["base_avatar"], "name": "smoke",
                                                             "model": cfg["avatar_model"]},
                                 files={"image": ("p.jpg", f, "image/jpeg")})
            r.raise_for_status()
            avatar = r.json()["id"]
            print(f"avatar created {avatar} in {time.time() - t:.1f}s")
        if not avatar:
            avatar = next(x["id"] for x in (await c.get("/ot/avatars")).json() if x["model_type"] == cfg["avatar_model"])
        t = time.time()
        r = await c.post(f"/ot/avatars/{avatar}/prewarm", json={"model": cfg["avatar_model"]})
        print("prewarm", r.status_code, r.json().get("runtime_status"), f"{time.time() - t:.1f}s")
        t0 = time.time()
        r = await c.post("/ot/sessions", json={"avatar_id": avatar, "model": cfg["avatar_model"], "tts_provider": "openai_compatible",
                                               "tts_voice": a.voice, "stt_provider": "sensevoice",
                                               "agent_enabled": False, "memory_enabled": False,
                                               "knowledge_enabled": False})
        r.raise_for_status()
        sid = r.json()["session_id"]
        print("session", sid)
        print("start", (await c.post(f"/ot/sessions/{sid}/start")).json(), f"{time.time() - t0:.1f}s")
        try:
            t0 = time.time()
            w = asyncio.create_task(watch(c, sid, 1, t0))
            await asyncio.sleep(0.5)
            print("speak", (await c.post(f"/ot/sessions/{sid}/speak", json={"text": "你好，用一句话介绍一下你自己", "voice": a.voice, "tts_provider": "openai_compatible"})).status_code)
            await w
            if a.wav:
                pcm = pcm16k(a.wav)
                t0 = time.time()
                w = asyncio.create_task(watch(c, sid, 1, t0))
                ctx = ssl.create_default_context()
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
                url = a.base.replace("http", "ws", 1) + f"/api/asr/{sid}"
                async with websockets.connect(url, ssl=ctx if url.startswith("wss") else None) as ws:
                    await ws.send(json.dumps({"type": "meta", "voice": a.voice, "tts_provider": "openai_compatible",
                                              "tts_model": "", "stt_provider": "sensevoice"}))
                    for i in range(0, len(pcm), 640):
                        await ws.send(pcm[i:i + 640])
                    await ws.send(json.dumps({"type": "end"}))
                    print(f"  stt -> {await ws.recv()}  ({time.time() - t0:.1f}s)")
                await w
        finally:
            await c.delete(f"/ot/sessions/{sid}")
            if a.photo and avatar:
                await c.delete(f"/ot/avatars/{avatar}")


if __name__ == "__main__":
    asyncio.run(main())
