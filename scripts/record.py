"""录一段真实通话画面（aiortc 收 WebRTC 音视频 → mp4），用来对比不同渲染模型的效果。
在控制面容器里跑：python record.py --avatar <形象> --model quicktalk|flashhead|fasterliveportrait --text "..." --out /w/cache/rec.mp4
会话走网关 /ot（和页面一样经控制面分流），系统提示词可用 --persona 取 personas.py 里的人设。
"""
import argparse
import asyncio
import fractions
import sys
import time

import av
import httpx

sys.path.insert(0, "/app/gateway")


async def main() -> None:
    from aiortc import RTCPeerConnection, RTCSessionDescription

    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:8443/ot")
    ap.add_argument("--avatar", required=True)
    ap.add_argument("--model", default="quicktalk")
    ap.add_argument("--voice", default="vox_elder_f")
    ap.add_argument("--persona", default="late_grandma")
    ap.add_argument("--text", action="append", required=True, help="可多次：依次说多轮")
    ap.add_argument("--idle", type=float, default=3.0, help="开头先录几秒待机")
    ap.add_argument("--out", required=True)
    ap.add_argument("--interrupt", type=float, default=0, help="每轮开口后这么多秒打断（模拟用户插话），再进下一轮")
    ap.add_argument("--listen", type=float, default=0, help="每轮说话前先模拟用户说话这么多秒（走网关语音通道但不发 end，不触发回答）")
    a = ap.parse_args()

    prompt = None
    if a.persona:
        import personas
        style = next((p[3] for p in personas.PERSONAS if p[0] == a.persona), None)
        prompt = personas.compose(style) if style else None

    video, audio = [], []
    async with httpx.AsyncClient(base_url=a.base, timeout=180, verify=False) as c:
        await c.post(f"/avatars/{a.avatar}/prewarm", json={"model": a.model})
        r = await c.post("/sessions", json={"avatar_id": a.avatar, "model": a.model, "tts_provider": "openai_compatible",
                                            "tts_voice": a.voice, "stt_provider": "sensevoice", "agent_enabled": False,
                                            "memory_enabled": False, "knowledge_enabled": False,
                                            **({"llm_system_prompt": prompt} if prompt else {})})
        r.raise_for_status()
        sid = r.json()["session_id"]
        pc = RTCPeerConnection()
        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")
        t0 = [None]

        async def drain(track):
            try:
                while True:
                    f = await track.recv()
                    now = time.time()
                    if t0[0] is None:
                        t0[0] = now
                    (video if track.kind == "video" else audio).append((now - t0[0], f))
            except Exception:
                pass

        pc.on("track", lambda t: asyncio.ensure_future(drain(t)))
        await pc.setLocalDescription(await pc.createOffer())
        ans = (await c.post(f"/sessions/{sid}/webrtc/offer", json={"sdp": pc.localDescription.sdp, "type": "offer"})).json()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))
        await c.post(f"/sessions/{sid}/start")
        await asyncio.sleep(a.idle)
        for text in a.text:
            if a.listen > 0:                          # 模拟用户开口：建 ASR 连接、送静音，再断开（不发 end）
                import ssl
                import websockets
                ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
                host = a.base.split("://", 1)[1].split("/", 1)[0]
                async with websockets.connect(f"wss://{host}/api/asr/{sid}", ssl=ctx) as w:
                    await w.send('{"type":"meta"}')
                    t1 = time.time()
                    while time.time() - t1 < a.listen:
                        await w.send(bytes(3200))
                        await asyncio.sleep(0.1)
                await asyncio.sleep(0.8)              # 说完到开口的间隔（像浏览器 VAD 静音判定）
            t_speak = time.time()
            await c.post(f"/sessions/{sid}/speak", json={"text": text, "voice": a.voice, "tts_provider": "openai_compatible"})
            if a.interrupt > 0:                       # 模拟用户插话：开口后 N 秒打断，接着下一轮
                await asyncio.sleep(a.interrupt)
                await c.post(f"/sessions/{sid}/interrupt")
                await asyncio.sleep(0.6)
                continue
            # 等这一轮说完：SSE 上 speech.ended，最多 60 秒
            try:
                async with c.stream("GET", f"/sessions/{sid}/events", timeout=70) as s:
                    ev = ""
                    async for line in s.aiter_lines():
                        if line.startswith("event:"):
                            ev = line[6:].strip()
                        elif line.startswith("data:") and ev == "speech.ended":
                            break
                        if time.time() - t_speak > 60:
                            break
            except Exception:
                pass
            await asyncio.sleep(2.0)
        await pc.close()
        await c.delete(f"/sessions/{sid}")

    if not video:
        raise SystemExit("没收到视频帧")
    sizes, last = [], None                      # 分辨率变化（WebRTC 自适应 / 预编码与实时编码切换）
    for ts, f in video:
        if (f.width, f.height) != last:
            last = (f.width, f.height)
            sizes.append(f"{ts:.2f}s {f.width}x{f.height}")
    print("sizes:", ", ".join(sizes[:20]))
    w, h = video[0][1].width, video[0][1].height
    out = av.open(a.out, "w")
    vs = out.add_stream("libx264", rate=25)
    vs.width, vs.height, vs.pix_fmt = w - w % 2, h - h % 2, "yuv420p"
    vs.options = {"crf": "20", "preset": "fast"}
    ast = out.add_stream("aac", rate=48000)
    ast.layout = "mono"
    last = -1
    for ts, f in video:
        nf = f.reformat(width=vs.width, height=vs.height, format="yuv420p")
        pts = max(last + 1, round(ts * 25))      # 帧时间戳必须严格递增（同一时刻收到两帧会重号）
        last = pts
        nf = av.VideoFrame.from_ndarray(nf.to_ndarray(), format="yuv420p")
        nf.pts = pts
        nf.time_base = fractions.Fraction(1, 25)
        for p in vs.encode(nf):
            out.mux(p)
    for p in vs.encode():
        out.mux(p)
    rs = av.AudioResampler(format="fltp", layout="mono", rate=48000)
    n = 0
    import numpy as np
    for ts, f in audio:
        f.pts = None
        # 待机时服务端不发音频：按到达时间补静音，否则整段声音会提前（之前的录屏音画差了一个待机时长，不能用来看同步）
        gap = round(ts * 48000) - n
        while gap > 2400:
            k = min(gap, 4800)
            sf = av.AudioFrame.from_ndarray(np.zeros((1, k), np.float32), format="fltp", layout="mono")
            sf.sample_rate, sf.pts, sf.time_base = 48000, n, fractions.Fraction(1, 48000)
            n += k
            gap -= k
            for p in ast.encode(sf):
                out.mux(p)
        for rf in rs.resample(f):
            rf = av.AudioFrame.from_ndarray(rf.to_ndarray(), format="fltp", layout="mono")
            rf.sample_rate = 48000
            rf.pts = n
            rf.time_base = fractions.Fraction(1, 48000)
            n += rf.samples
            for p in ast.encode(rf):
                out.mux(p)
    for p in ast.encode():
        out.mux(p)
    out.close()
    print(f"saved {a.out}: {len(video)} frames {w}x{h}, {video[-1][0]:.1f}s, audio {n / 48000:.1f}s")
    # 跳变检测：相邻帧缩略图平均差，报告最大的几处（和中位数比）
    import numpy as np
    small = [f.reformat(width=48, height=72, format="gray").to_ndarray().astype(np.float32) for _, f in video]   # 统一尺寸（WebRTC 会自适应改分辨率）
    diffs = np.array([np.abs(small[i + 1] - small[i]).mean() for i in range(len(small) - 1)])
    med = float(np.median(diffs)) or 1e-6
    top = np.argsort(diffs)[-5:][::-1]
    print("adjacent diff median %.2f p99 %.2f; top jumps: %s" % (med, np.percentile(diffs, 99),
          ", ".join(f"{video[i + 1][0]:.1f}s={diffs[i]:.2f}({diffs[i] / med:.0f}x)" for i in top)))
    # 嘴部区域（竖版半身像：嘴大约在高 49%~58%、宽 38%~60%）
    mo = [f.reformat(width=120, height=180, format="gray").to_ndarray()[88:104, 46:72].astype(np.float32) for _, f in video]
    md = np.array([np.abs(mo[i + 1] - mo[i]).mean() for i in range(len(mo) - 1)])
    mt = np.argsort(md)[-5:][::-1]
    print("mouth diff median %.2f p95 %.2f; top: %s" % (np.median(md), np.percentile(md, 95),
          ", ".join(f"{video[i + 1][0]:.2f}s={md[i]:.1f}" for i in mt)))


if __name__ == "__main__":
    asyncio.run(main())
