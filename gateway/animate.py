"""照片 → 待机动作视频 → OpenTalking 形象。

QuickTalk 只改嘴，其余画面原样取自形象的底板视频；照片建的底板是静帧，所以只有嘴动。
这里先用 MiniMax 图生视频（首帧 = 尾帧 = 照片，便于无缝循环）生成一段眨眼 / 点头 / 手势的待机视频，
再用视频建形象。视频生成失败时退回用照片建静态形象。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx

import cluster
import ot_pool
from config import AVATAR_MODEL, BASE_AVATAR, LLMS, VIDEO_MODEL

log = logging.getLogger("animate")
MM_V2 = "https://api.minimaxi.com/v2"
# 底板分几段生成（每段 6 秒、首尾帧都是照片，所以段与段天然能接上），每段动作不同，拼起来循环才不显得重复。
# 一张照片 N 段 = N 次视频额度。照片里没有手的，手势那段模型会自动忽略手。
_IDLE_TAIL = ("最后回到和开头完全一样的姿势。全程嘴唇闭合、不说话，动作幅度小而柔和。"
              "背景和光线保持不变，镜头不移动、不推拉。")
IDLE_PROMPTS = [
    "固定机位，正面中景。人物看着镜头自然地待机倾听：缓慢眨眼两三次，轻微点头、头部小幅度左右转动，"
    "面带浅浅的微笑，双手做自然放松的小幅度手势后放回原位，" + _IDLE_TAIL,
    "固定机位，正面中景。人物安静地看着镜头：缓慢眨眼，胸口随呼吸轻轻起伏，眼神先看向旁边一下再温和地看回镜头，"
    "嘴角微微上扬像是听到了什么，身体几乎不动，" + _IDLE_TAIL,
    "固定机位，正面中景。人物像在专注听人说话：轻轻点头两下表示认同，头微微偏一下，眨眼，"
    "若画面里有手就抬手做一个很轻的示意手势再放回原位，" + _IDLE_TAIL,
]
IDLE_PROMPT = IDLE_PROMPTS[0]
DEFAULT_CLIPS = 3
MAX_CLIPS = 5
JOBS: dict[str, dict] = {}
JOBS_DIR = Path("/w/state/jobs")       # 任务落盘（参数 + 原图）：控制面重启后续跑，不让用户白传照片
_TASKS: set[asyncio.Task] = set()
on_done = None                          # 任务结束钩子（public_api.job_done：回调后端 avatar.ready / failed）


def video_ready() -> bool:
    return LLMS["minimax"].ready


def _save(job: dict) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = JOBS_DIR / f"{job['id']}.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False), "utf-8")
    tmp.replace(JOBS_DIR / f"{job['id']}.json")


def new_job(name: str, image: bytes, animate: bool, biz_ref: str = "", callback_url: str = "",
            clips: int = DEFAULT_CLIPS) -> str:
    jid = uuid.uuid4().hex[:12]
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    (JOBS_DIR / f"{jid}.img").write_bytes(image)
    JOBS[jid] = {"id": jid, "name": name, "animate": bool(animate), "clips": max(1, min(MAX_CLIPS, int(clips or 1))),
                 "biz_ref": biz_ref, "callback_url": callback_url, "state": "running", "step": "排队中", "avatar_id": None, "animated": False, "error": None, "t0": time.time()}
    _save(JOBS[jid])
    _start(jid, image)
    for k in [k for k, j in JOBS.items() if time.time() - j["t0"] > 3600 and j["state"] != "running"]:
        JOBS.pop(k, None)
        (JOBS_DIR / f"{k}.json").unlink(missing_ok=True)
    return jid


def _start(jid: str, image: bytes) -> None:
    job = JOBS[jid]
    task = asyncio.create_task(_run(jid, job["name"], image, job["animate"] and video_ready(), int(job.get("clips") or 1)))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def resume() -> None:
    """控制面启动：磁盘上没跑完的任务接着跑，跑完的留着给 GET /jobs 查。"""
    if not JOBS_DIR.is_dir():
        return
    for f in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        JOBS[job["id"]] = job
        img = JOBS_DIR / f"{job['id']}.img"
        if job["state"] == "running":
            if img.is_file():
                log.info("resuming avatar job %s (%s)", job["id"], job["name"])
                job["step"] = "重启后续跑"
                _start(job["id"], img.read_bytes())
            else:
                job.update(state="error", error="控制面重启，原图丢失")
                _save(job)


async def _run(jid: str, name: str, image: bytes, animate: bool, clips: int = 1) -> None:
    job = JOBS[jid]
    video = None
    try:
        try:
            inst = ot_pool.first()           # 建在一个渲染节点上，建好后同步给其他节点
        except ot_pool.NoNode:
            raise RuntimeError("没有在线的渲染节点") from None
        if job.get("avatar_id"):          # 重启前已经建好形象：只补同步和预热，别再建一个
            aid = job["avatar_id"]
            inst.node.avatars.add(aid)
            await cluster.sync_avatar(aid, inst.node)
            r = await ot_pool.prewarm(aid, {"model": AVATAR_MODEL})
            job.update(state="done", step="完成")
            return
        if animate:
            job["step"] = f"正在生成动作视频（{clips} 段，约 {20 + 10 * clips} 秒）"
            # 各段并发生成；部分失败就用成功的段，全失败退回静态照片
            results = await asyncio.gather(*[_gen_video(image, IDLE_PROMPTS[i % len(IDLE_PROMPTS)]) for i in range(clips)],
                                           return_exceptions=True)
            ok = [v for v in results if isinstance(v, (bytes, bytearray))]
            errs = [e for e in results if not isinstance(e, (bytes, bytearray))]
            if errs:
                log.warning("idle video: %d/%d clips failed: %s", len(errs), clips, errs[0])
            if ok:
                try:
                    job["step"] = "正在拼接底板"
                    video = await asyncio.to_thread(lambda: seamless_loop(concat_clips(ok)))
                    if errs:
                        job["warn"] = f"动作视频 {len(errs)}/{clips} 段生成失败，用剩下 {len(ok)} 段拼的：{str(errs[0])[:80]}"
                except Exception as e:  # noqa: BLE001
                    log.exception("concat idle clips failed")
                    errs.append(e)
                    video = None
            if video is None:
                job["warn"] = f"动作视频生成失败，已改用静态照片：{str(errs[0])[:80] if errs else ''}"
        job["step"] = "正在创建形象"
        data = {"base_avatar_id": BASE_AVATAR, "name": name, "model": AVATAR_MODEL}
        files = {"video": ("idle.mp4", video, "video/mp4")} if video else {"image": ("photo.jpg", image, "image/jpeg")}
        r = await inst.node.client.post(inst.path("avatars/custom"), data=data, files=files, timeout=600)
        if r.status_code >= 400:
            raise RuntimeError(f"建形象失败：{r.text[:200]}")
        aid = r.json()["id"]
        inst.node.avatars.add(aid)
        job.update(avatar_id=aid, animated=bool(video), step="正在同步到各节点")
        _save(job)
        await cluster.sync_avatar(aid, inst.node)
        job.update(step="正在准备形象（约 15 秒）")
        r = await ot_pool.prewarm(aid, {"model": AVATAR_MODEL})   # 每个渲染实例都预热
        if r.status_code >= 400:
            log.warning("prewarm %s failed: %s", aid, r.text[:200])
        job.update(state="done", step="完成")
        log.info("avatar %s ready (animated=%s) in %.0fs", aid, bool(video), time.time() - job["t0"])
    except Exception as e:  # noqa: BLE001
        log.exception("avatar job failed")
        job.update(state="error", error=str(e)[:300])
    finally:
        _save(job)
        (JOBS_DIR / f"{jid}.img").unlink(missing_ok=True)
        if on_done:
            try:
                on_done(job)
            except Exception:  # noqa: BLE001
                log.exception("job on_done failed")


def _seam_ratio(path: Path) -> float:
    """首尾帧差 / 相邻帧差的 90 分位：≤1 说明从最后一帧跳回第一帧和正常播放一样平滑。"""
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(str(path))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(cv2.resize(f, (192, 256)).astype(np.float32))
    cap.release()
    if len(frames) < 10:
        return 99.0
    adj = [np.abs(frames[i + 1] - frames[i]).mean() for i in range(len(frames) - 1)]
    return float(np.abs(frames[0] - frames[-1]).mean() / max(np.percentile(adj, 90), 1e-3))


def seamless_loop(video: bytes, fade: float = 1.0) -> bytes:
    """做成可无缝循环的 25fps 底板（QuickTalk 底板帧率）。
    生成时首尾帧都指定为照片，模型通常能回到起始姿势，首尾本就接得上，直接用（淡化反而会让手出重影）；
    没回到位时首尾交叉淡化：去掉开头 fade 秒，结尾 fade 秒渐变到被去掉的开头，成片尾帧 ≈ 首帧。"""
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "in.mp4", Path(d) / "loop.mp4"
        src.write_bytes(video)
        ratio = _seam_ratio(src)
        if ratio <= 1.0:
            graph = "[0:v]fps=25,format=yuv420p"
        else:
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                        "-of", "csv=p=0", str(src)],
                                       check=True, capture_output=True, text=True).stdout.strip())
            fade = min(fade, dur / 4)
            graph = (f"[0:v]split[a][b];[a]trim=start={fade},setpts=PTS-STARTPTS[main];"
                     f"[b]trim=end={fade},setpts=PTS-STARTPTS[head];"
                     f"[main][head]xfade=transition=fade:duration={fade}:offset={dur - 2 * fade:.3f},"
                     f"fps=25,format=yuv420p")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex", graph, "-an",
                        "-c:v", "libx264", "-crf", "16", "-preset", "fast", str(out)], check=True)
        log.info("idle loop: seam ratio %.2f -> %s (after %.2f)", ratio,
                 "as-is" if ratio <= 1.0 else "crossfade", _seam_ratio(out))
        return out.read_bytes()


def _dur(path: Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                                check=True, capture_output=True, text=True).stdout.strip())


def concat_clips(videos: list[bytes], fade: float = 0.4) -> bytes:
    """把同一张照片生成的几段待机视频首尾相接（每段首尾都是照片姿势，接缝只做很短的交叉淡化），
    统一 25fps；输出再交给 seamless_loop 处理整体的首尾循环。一段就原样返回。"""
    if len(videos) == 1:
        return videos[0]
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for i, v in enumerate(videos):
            p = Path(d) / f"c{i}.mp4"
            p.write_bytes(v)
            paths.append(p)
        durs = [_dur(p) for p in paths]
        fade = min(fade, min(durs) / 4)
        n = len(paths)
        # 每段先统一帧率/像素格式，再 xfade 链式拼接；offset 是前面已拼成片的时长减去淡化
        g = "".join(f"[{i}:v]fps=25,format=yuv420p,setpts=PTS-STARTPTS[v{i}];" for i in range(n))
        prev, acc = "v0", durs[0]
        for i in range(1, n):
            out = f"x{i}" if i < n - 1 else "out"
            g += f"[{prev}][v{i}]xfade=transition=fade:duration={fade:.3f}:offset={acc - fade:.3f}[{out}];"
            acc += durs[i] - fade
            prev = out
        g = g.rstrip(";")
        out = Path(d) / "cat.mp4"
        args = ["ffmpeg", "-v", "error", "-y"]
        for p in paths:
            args += ["-i", str(p)]
        args += ["-filter_complex", g, "-map", "[out]", "-an", "-c:v", "libx264", "-crf", "16", "-preset", "fast", str(out)]
        subprocess.run(args, check=True)
        log.info("idle clips: %d segments %s -> %.1fs", n, [round(x, 1) for x in durs], _dur(out))
        return out.read_bytes()


async def _gen_video(image: bytes, prompt: str = IDLE_PROMPT) -> bytes:
    key = LLMS["minimax"].api_key
    uri = "data:image/jpeg;base64," + base64.b64encode(image).decode()
    body = {"model": VIDEO_MODEL, "resolution": "768P", "duration": 6, "ratio": "adaptive", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": uri}, "role": "first_frame"},
        {"type": "image_url", "image_url": {"url": uri}, "role": "last_frame"},
    ]}
    h = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), trust_env=False) as c:
        r = await c.post(f"{MM_V2}/video_generation", headers=h, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"MiniMax {r.status_code}: {r.text[:150]}")
        tid = r.json().get("task_id")
        if not tid:
            raise RuntimeError(f"没拿到任务号：{r.text[:150]}")
        deadline = time.time() + 300
        while time.time() < deadline:
            await asyncio.sleep(3)
            t = (await c.get(f"{MM_V2}/query/video_generation/{tid}", headers=h)).json().get("task") or {}
            if t.get("status") == "succeeded":
                url = (t.get("content") or {}).get("url")
                return (await c.get(url, timeout=300)).content
            if t.get("status") == "failed":
                err = t.get("error")
                raise RuntimeError((err.get("message") if isinstance(err, dict) else str(err)) or "生成失败")
        raise RuntimeError("生成超时")
