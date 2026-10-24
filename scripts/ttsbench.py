"""本地合成服务并发压测：N 路同时流式合成，按"边收边播"算会不会断音。

python ttsbench.py -n 8 --voice clone_xxx [--url http://127.0.0.1:8302] [--rounds 2] [--save out.wav]
每路指标：首包、整句 RTF、断音（某块到达时，前面已收到的音频早就播完了 → 播放器得等）。
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time
import wave

import httpx

TEXTS = ["哎，你这么一说我就明白了，其实这事儿没那么复杂，咱们一步一步来就好。",
         "今天天气挺不错的，要不要出去走走？顺便聊聊最近发生的那些有意思的事情。",
         "我觉得你说得很有道理，不过也可以换个角度想想，说不定会有新的发现呢。",
         "好呀，那我先给你讲个小故事吧，从前有一只小猫，它特别喜欢在窗台上晒太阳。"]
SR = 16000


async def one(c: httpx.AsyncClient, url: str, voice: str, text: str, save: str | None) -> dict:
    t0 = time.time()
    got = 0
    first = None
    stall = 0.0
    buf = bytearray()
    async with c.stream("POST", url + "/v1/audio/speech", json={"input": text, "voice": voice, "response_format": "pcm"}) as r:
        r.raise_for_status()
        async for b in r.aiter_bytes():
            now = time.time()
            if first is None:
                first = now
            else:   # 播放从 first 开始；这块到的时候，之前收到的 got 字节应该已经播到哪了
                stall = max(stall, (now - first) - got / 2 / SR)
            got += len(b)
            if save:
                buf += b
    dur = got / 2 / SR
    if save:
        with wave.open(save, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR), w.writeframes(bytes(buf))
    return {"first_ms": (first - t0) * 1000, "audio_s": dur, "wall_s": time.time() - t0,
            "rtf": (time.time() - first) / dur if dur else 0, "stall_ms": max(stall, 0) * 1000}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1)
    ap.add_argument("--url", default="http://127.0.0.1:8302")
    ap.add_argument("--voice", default="")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--save")
    a = ap.parse_args()
    res = []
    async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
        for k in range(a.rounds):
            res += await asyncio.gather(*[one(c, a.url, a.voice, TEXTS[(i + k) % len(TEXTS)],
                                              a.save if (i == 0 and k == 0) else None) for i in range(a.n)])
    f = sorted(x["first_ms"] for x in res)
    print(f"n={a.n} reqs={len(res)} first_p50={statistics.median(f):.0f}ms first_max={f[-1]:.0f}ms "
          f"rtf_avg={statistics.mean(x['rtf'] for x in res):.2f} rtf_max={max(x['rtf'] for x in res):.2f} "
          f"stall_max={max(x['stall_ms'] for x in res):.0f}ms stalled(>100ms)={sum(x['stall_ms'] > 100 for x in res)} "
          f"audio_avg={statistics.mean(x['audio_s'] for x in res):.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
