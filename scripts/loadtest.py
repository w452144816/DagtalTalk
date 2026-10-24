"""多路并发压测：每路一个独立进程，用 aiortc 当 WebRTC 客户端真实收流，同时让所有会话说话。

python loadtest.py -n 3 [--avatar ID[,ID2...]] [--voice V] [--rounds 3] [--ot http://127.0.0.1:8000[,http://127.0.0.1:8001]]

每路指标：speak → speech.started（开口延迟）、说话期间收到的视频帧率、最大帧间隔（卡顿）。
多个 --ot 时按轮询把会话分到不同 OpenTalking 实例（双卡各一个实例）。
渲染耗时（每 1.12s 音频要渲染多久）从 OpenTalking 日志里另看。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import queue
import threading
import multiprocessing as mp
import statistics
import time

import httpx

PROMPTS = ["请用三句话介绍一下你自己，每句话长一点。", "讲一个关于小猫的简短故事，三四句话。",
           "说说周末适合做的三件事，每件说一句。", "用几句话聊聊你喜欢的季节和原因。"]


async def client(idx: int, ot: str, avatar: str, voice: str, rounds: int, barrier, out) -> None:
    from aiortc import RTCPeerConnection, RTCSessionDescription

    res = {"idx": idx, "ot": ot, "rounds": []}
    frames: list[float] = []
    frozen: list[tuple[float, bool]] = []   # (到达时刻, 和上一帧内容是否相同)：底板定格检测（说完→开口期间重复帧）
    sig = [None]
    audio_n = [0]
    loud: list[float] = []      # 有声音的音频帧（20ms）到达时刻：用来找说话中途的断音
    async with httpx.AsyncClient(base_url=ot, timeout=120, verify=False) as c:   # 可直连实例，也可走网关 https://…:8443/ot
        r = await c.post("/sessions", json={"avatar_id": avatar, "model": "quicktalk", "tts_provider": "openai_compatible",
                                            "tts_voice": voice, "stt_provider": "sensevoice", "agent_enabled": False,
                                            "memory_enabled": False, "knowledge_enabled": False})
        r.raise_for_status()
        sid = r.json()["session_id"]
        res["sid"] = sid
        pc = RTCPeerConnection()
        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")

        async def drain(track):
            try:
                while True:
                    f = await track.recv()
                    if track.kind == "video":
                        now = time.time()
                        frames.append(now)
                        try:   # 底板定格检测：相邻帧 1/8 抽样后最大像素差 < 12 视为同一帧（嘴动/眨眼都远大于此）
                            g = f.to_ndarray(format="rgb24")[::8, ::8].astype("float32").mean(axis=2)
                            same = sig[0] is not None and float(abs(g - sig[0]).max()) < 12
                            sig[0] = g
                            frozen.append((now, same))
                        except Exception as e:  # noqa: BLE001
                            if sig[0] is None:
                                print("frame decode failed:", repr(e), flush=True)
                    else:
                        audio_n[0] += 1
                        a = f.to_ndarray()
                        if a.size and float(abs(a.astype("float32")).mean()) > 300:   # s16，静音补帧≈0
                            loud.append(time.time())
            except Exception:
                pass

        pc.on("track", lambda t: asyncio.ensure_future(drain(t)))
        await pc.setLocalDescription(await pc.createOffer())
        ans = (await c.post(f"/sessions/{sid}/webrtc/offer",
                            json={"sdp": pc.localDescription.sdp, "type": "offer"})).json()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))
        t = time.time()
        await c.post(f"/sessions/{sid}/start")
        res["start_s"] = round(time.time() - t, 2)

        events: asyncio.Queue = asyncio.Queue()

        async def sse():
            async with c.stream("GET", f"/sessions/{sid}/events", timeout=None) as s:
                ev = ""
                async for line in s.aiter_lines():
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:") and ev in ("speech.started", "speech.ended"):
                        await events.put((ev, time.time()))

        sse_task = asyncio.create_task(sse())
        await asyncio.sleep(3)  # 待机画面先跑起来
        hold = float(os.getenv("LOADTEST_HOLD", "0"))
        if rounds == 0 and hold:        # 只在线不说话：看待机会话自己的帧率，以及对其他说话会话的影响
            t0 = time.time()
            await asyncio.sleep(hold)
            win = [f for f in frames if f >= t0]
            gaps = [b - a for a, b in zip(win, win[1:])]
            res["idle"] = {"fps": round(len(win) / hold, 1), "max_gap_ms": round(max(gaps) * 1000) if gaps else None,
                           "gaps_over_200ms": sum(g > 0.2 for g in gaps)}
        try:
            for k in range(rounds):
                try:    # 所有会话同时开口；有客户端卡住时不要把其他人一起拖死
                    await asyncio.get_running_loop().run_in_executor(None, barrier.wait, 240)
                except threading.BrokenBarrierError:
                    res["rounds"].append({"error": "barrier"})
                    break
                while not events.empty():
                    events.get_nowait()
                t0 = time.time()
                await c.post(f"/sessions/{sid}/speak", json={"text": PROMPTS[(idx + k) % len(PROMPTS)],
                                                            "voice": voice, "tts_provider": "openai_compatible"})
                started = ended = None
                try:
                    while ended is None:
                        ev, ts = await asyncio.wait_for(events.get(), 90)
                        if ev == "speech.started" and started is None:
                            started = ts
                        elif ev == "speech.ended" and started is not None:
                            ended = ts
                except asyncio.TimeoutError:
                    res["rounds"].append({"error": "timeout"})
                    continue
                win = [f for f in frames if started <= f <= ended]
                gaps = [b - a for a, b in zip(win, win[1:])]
                await asyncio.sleep(1.5)        # speech.ended 比声音播完早，多收一会儿
                dur = ended - started
                snd = [x for x in loud if started <= x <= ended + 1.5]
                sil = [b - a for a, b in zip(snd, snd[1:])]
                fz = [(t, same) for t, same in frozen if t0 <= t <= ended]   # 从发 speak 起最长连续重复帧
                best = cur = 0.0; run0 = None
                for t, same in fz:
                    if same:
                        run0 = t if run0 is None else run0
                        cur = t - run0
                    else:
                        best = max(best, cur); cur = 0.0; run0 = None
                best = max(best, cur)
                res["rounds"].append({"open_s": round(started - t0, 2), "speech_s": round(dur, 1),
                                      "fps": round(len(win) / dur, 1) if dur > 0 else 0,
                                      "max_gap_ms": round(max(gaps) * 1000) if gaps else None,
                                      "gaps_over_200ms": sum(g > 0.2 for g in gaps),
                                      "max_frozen_ms": round(best * 1000),
                                      "first_sound_s": round(snd[0] - t0, 2) if snd else None,
                                      "max_silence_ms": round(max(sil) * 1000) if sil else None,
                                      "silences_over_600ms": sum(s > 0.6 for s in sil)})
        finally:
            sse_task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pc.close(), 10)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(c.delete(f"/sessions/{sid}"), 10)
    res["audio_frames"] = audio_n[0]
    out.put(res)


def run(*a) -> None:
    try:
        asyncio.run(client(*a))
    except Exception as e:  # noqa: BLE001
        a[-1].put({"idx": a[0], "error": repr(e)})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1)
    ap.add_argument("--ot", default="http://127.0.0.1:8000")
    ap.add_argument("--avatar", required=True, help="逗号分隔则轮流分配")
    ap.add_argument("--voice", default="Chinese (Mandarin)_Gentle_Youth")
    ap.add_argument("--rounds", type=int, default=3)
    a = ap.parse_args()
    ots, avatars = a.ot.split(","), a.avatar.split(",")
    for ot in ots:  # 预热各实例上的形象
        for av in set(avatars):
            httpx.post(f"{ot}/avatars/{av}/prewarm", json={"model": "quicktalk"}, timeout=300, verify=False)
    barrier, out = mp.Barrier(a.n), mp.Queue()
    ps = [mp.Process(target=run, args=(i, ots[i % len(ots)], avatars[i % len(avatars)], a.voice, a.rounds, barrier, out))
          for i in range(a.n)]
    for p in ps:
        p.start()
    results = []
    deadline = time.time() + 120 * a.rounds + float(os.getenv("LOADTEST_HOLD", "0")) + 300
    for _ in ps:
        try:
            results.append(out.get(timeout=max(1, deadline - time.time())))
        except queue.Empty:
            break
    for p in ps:
        p.join(5)
        if p.is_alive():
            p.kill()
    done = {r["idx"] for r in results}
    results += [{"idx": i, "error": "hung"} for i in range(a.n) if i not in done]
    results.sort(key=lambda r: r["idx"])
    print(json.dumps(results, ensure_ascii=False))
    idle = [r["idle"] for r in results if "idle" in r]
    if idle:
        print(f"IDLE n={len(idle)}/{a.n} fps_min={min(x['fps'] for x in idle)} "
              f"fps_med={statistics.median(x['fps'] for x in idle)} "
              f"max_gap={max(x['max_gap_ms'] or 0 for x in idle)}ms stalls>200ms={sum(x['gaps_over_200ms'] for x in idle)}")
    rs = [x for r in results for x in r.get("rounds", []) if "open_s" in x]
    if rs:
        print(f"SUMMARY n={a.n} ok_rounds={len(rs)}/{a.n * a.rounds} "
              f"open_med={statistics.median(x['open_s'] for x in rs):.2f}s open_max={max(x['open_s'] for x in rs):.2f}s "
              f"fps_min={min(x['fps'] for x in rs)} fps_med={statistics.median(x['fps'] for x in rs)} "
              f"max_gap={max(x['max_gap_ms'] or 0 for x in rs)}ms stalls>200ms={sum(x['gaps_over_200ms'] for x in rs)} "
              f"max_frozen={max(x.get('max_frozen_ms') or 0 for x in rs)}ms frozen_med={statistics.median(x.get('max_frozen_ms') or 0 for x in rs):.0f}ms")
        fs = sorted(x["first_sound_s"] for x in rs if x.get("first_sound_s"))
        if fs:
            print(f"AUDIO first_sound_med={statistics.median(fs):.2f}s first_sound_max={fs[-1]:.2f}s "
                  f"max_silence={max(x['max_silence_ms'] or 0 for x in rs)}ms "
                  f"silences>600ms={sum(x['silences_over_600ms'] for x in rs)} no_sound={sum(not x.get('first_sound_s') for x in rs)}")


if __name__ == "__main__":
    main()
