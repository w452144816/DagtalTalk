"""全身实时通话：打断测试（直连实时数字人接口，开录像）。

每个组合建一个会话：开场白 → 让它讲长故事 → 说到一半发 audio_interrupted（type 7）→ 再问一句。
结束后用 rt_mouth_motion.py 分析录像里第二句回复嘴有没有动。
在控制面容器里跑：
  python /tmp/rt_bargein_test.py --avatar-asset <形象资产 id>           # 资产 id 见 /w/state/vidu_cache.json
  python /tmp/rt_bargein_test.py --avatar-asset <id> --combos s1:server,s2:semantic
录像：GET https://<控制面>/api/rt/lives/<live_id>/recording（会话结束约 2 分钟后可下载）。
"""
import argparse
import asyncio
import json
import time
import uuid

import httpx
import websockets

HOST = "api.vidu.cn"


async def one(key: str, asset: str, voice: str, model: str, vad: dict) -> dict:
    hdr = {"Authorization": "Token " + key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        r = await c.post(f"https://{HOST}/live/s_avatar/realtime", headers=hdr, json={
            "model": model, "call_mode": "video", "enable_recording": True, "idle_timeout_seconds": 120,
            "action": {"enabled": False}, "audio": {"enable_transcription": True}, "vad": vad,
            "avatar": {"id": asset, "persona": "你是一位慈祥、爱讲故事的老人。", "voice": voice}})
        r.raise_for_status()
        lid = r.json()["live"]["id"]
    cid = "b-" + uuid.uuid4().hex[:8]
    seq = [0]
    events = []

    def frame(t: int, payload: dict) -> str:
        seq[0] += 1
        return json.dumps({"type": t, "live_id": lid, "conn_id": cid, "seq_id": seq[0], "payload": payload})

    plan = [(10, 99, "给我讲一个很长很长的故事，讲你年轻时候的事"), (16, 7, None), (17, 99, "今天天气怎么样？你那边冷不冷？")]
    async with websockets.connect(f"wss://{HOST}/live/ws/live/connect?live_id={lid}&conn_id={cid}",
                                  additional_headers={"Authorization": "Token " + key}) as ws:
        await ws.send(frame(1, {"conn_init": {"version": 1}}))
        t0, step = time.time(), 0
        while time.time() - t0 < 45:
            if step < len(plan) and time.time() - t0 >= plan[step][0]:
                _, t, txt = plan[step]
                payload = {"text_msg": {"msg_id": str(step), "content": txt, "timestamp": int(time.time() * 1000)}} if t == 99 else {}
                await ws.send(frame(t, payload))
                events.append((round(time.time() - t0, 1), "SEND", t, txt))
                step += 1
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), 0.5))
            except asyncio.TimeoutError:
                continue
            if m.get("type") == 2 and not m["payload"]["conn_init_ack"].get("success"):
                await asyncio.sleep(1.5)
                await ws.send(frame(1, {"conn_init": {"version": 1}}))
            if m.get("type") in (9, 10):
                events.append((round(time.time() - t0, 1), m["type"], (m.get("payload") or {}).get("text_msg", {}).get("content", "")[:30]))
        await ws.send(frame(5, {"hangup": {"hangup_reason": "user_end"}}))
    return {"model": model, "vad": vad["type"], "live": lid, "events": events}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--avatar-asset", required=True)
    ap.add_argument("--voice", default="")
    ap.add_argument("--key-file", default="/w/state/vidu.key")
    ap.add_argument("--combos", default="s1:server,s2:server,s1:semantic,s2:semantic")
    a = ap.parse_args()
    key = open(a.key_file).read().strip()
    jobs = []
    for combo in a.combos.split(","):
        m, v = combo.split(":")
        vad = {"type": "server", "silence_duration_ms": 500} if v == "server" else {"type": "semantic"}
        jobs.append(one(key, a.avatar_asset, a.voice, f"vidu-{m}", vad))
    for r in await asyncio.gather(*jobs):
        print(json.dumps(r, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
