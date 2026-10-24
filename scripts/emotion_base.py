"""情绪底板：一张照片按几种情绪各生成一段待机视频（MiniMax H3，首尾帧都是照片），拼成一条底板建形象，
并在形象目录写 emotion_segments.json（各段帧区间 + 情绪标签→段），OpenTalking 补丁 6 据此按情绪切段。
在控制面容器里跑：python /tmp/emotion_base.py --image /w/data/grandma.jpg --name "奶奶·李秀兰（情绪）"
已生成的段会缓存在 /w/data/emo/<名字>/，重跑只补缺的段（--regen calm,sad 强制重做某几段）。
"""
import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app/gateway")

TAIL = "最后回到和开头完全一样的姿势。全程嘴唇闭合、不说话，头部动作幅度小而柔和，主要靠眉眼和脸部表情。背景和光线保持不变，镜头不移动、不推拉。"
HEAD = "固定机位，正面中景。"
# 顺序就是底板里的段顺序：前两段是待机（平静 + 温柔），其余说话时按情绪切
CLIPS = [
    ("calm", HEAD + "人物安静地看着镜头，神情平和，缓慢眨眼两三次，胸口随呼吸轻轻起伏。" + TAIL),
    ("gentle", HEAD + "人物温柔慈爱地看着镜头，眼神柔和，嘴角带着浅浅的微笑，轻轻点一下头，慢慢眨眼。" + TAIL),
    # 待机变体：待机是最常看到的画面，多两段不同的小动作，循环从 13 秒变 26 秒，不容易看出重复
    ("calm2", HEAD + "人物安静地坐着看向镜头，轻轻吸一口气，眼神往旁边看一下又温和地看回来，慢慢眨眼。" + TAIL),
    ("gentle2", HEAD + "人物慈祥地看着镜头，微微笑着，头轻轻歪一下又回正，像在等孩子说话，慢慢眨眼。" + TAIL),
    ("happy", HEAD + "人物很开心，眼睛笑得弯起来，眉毛舒展，脸颊上扬，带着笑意微微点头。" + TAIL),
    ("laugh", HEAD + "人物被逗乐了，眯着眼开怀地笑，笑得肩膀轻轻抖动，头微微后仰再回正，嘴巴闭着笑。" + TAIL),
    ("surprised", HEAD + "人物有点惊讶，眉毛扬起，眼睛睁大，头微微往后一收，随后神情放松恢复慈祥。" + TAIL),
    # 心疼：第一版"眼神低垂"导致整个头低下去、脸上还在笑（用户说做得差）→ 正对镜头、头不动、笑容收起、眉头皱起
    ("sad", HEAD + "人物始终正对镜头、头几乎不动。脸上的笑容慢慢收起，嘴唇轻轻抿着闭合，眉头皱起、眉心向上，"
            "眼神心疼地看着镜头，眼眶微微泛红，像在心疼眼前的孩子，轻轻缓慢地摇一下头，之后表情慢慢放松。" + TAIL),
    # 倾听：用户说话时循环（网关 ASR 一收到用户开口就切进来），看着镜头、时不时轻轻点头
    ("listen", HEAD + "人物专注地看着镜头，认真听对面的孩子说话，眼神温和关切，时不时轻轻点头表示在听，"
               "偶尔微微侧一下头，慢慢眨眼。" + TAIL),
]
# 讲话段（补丁 11）：平静/温柔的句子说话时用。头和眉眼跟着"说话"动，嘴唇闭合（嘴由口型模型生成；说完段还没演完时也不会"无声说话"）
TALK_TAIL = ("最后回到和开头完全一样的姿势。全程嘴唇闭合、不出声。头部随说话的节奏偶尔缓慢地点一下头、轻微转动或歪头，"
             "动作缓慢柔和、幅度小，像七十多岁的老人，不要频繁点头；眉毛和眼神表情生动。背景和光线保持不变，镜头不移动、不推拉。")
CLIPS += [
    ("talk_a", HEAD + "人物像在跟对面的孩子聊家常：看着镜头，说到要紧处轻轻点两下头，眉毛随着说话自然地抬起又放下，"
               "表情亲切生动，慢慢眨眼。" + TALK_TAIL),
    ("talk_b", HEAD + "人物像在耐心地叮嘱孩子：头微微向前探一下，轻轻歪头，眉毛微微上扬，点头强调，"
               "然后温和地看着镜头。" + TALK_TAIL),
    # 第一版嘴张开露了牙（说完没声音时像在默念）→ 强调嘴唇抿着
    ("talk_c", HEAD + "人物像在回想以前的事：眼神先往旁边看一下像在回想，再看回镜头，带着笑意轻轻点头，眉毛轻轻上扬。"
               "嘴唇始终轻轻抿着闭合，不张嘴、不露牙齿。" + TALK_TAIL),
]
IDLE = ["calm", "gentle", "calm2", "gentle2"]   # 待机区间 = 这几段连续拼在最前面
# 保持片段：情绪要持续（连着几句都是心疼）时循环，首尾帧都是该情绪段的峰值帧，不会中途回到照片的笑脸
HOLD_TAIL = ("最后回到和开头完全一样的姿势和表情。全程嘴唇闭合、不说话，头几乎不动。背景和光线保持不变，镜头不移动、不推拉。")
HOLDS = {
    "happy": HEAD + "人物保持开心的表情，眼睛笑得弯弯的，只有缓慢眨眼和轻微呼吸，表情始终不变。" + HOLD_TAIL,
    "laugh": HEAD + "人物保持开怀的笑容，眯着眼，肩膀随笑意轻轻抖动，表情始终不变。" + HOLD_TAIL,
    "surprised": HEAD + "人物保持惊讶的表情，眉毛扬起、眼睛睁大，只有缓慢眨眼和轻微呼吸，表情始终不变。" + HOLD_TAIL,
    "sad": HEAD + "人物保持心疼难过的表情，眉头皱着、眉心向上，眼神心疼地看着镜头，只有缓慢眨眼和轻微呼吸，表情始终不变。" + HOLD_TAIL,
}
EMOTIONS = {"平静": "calm", "温柔": "gentle", "开心": "happy", "笑": "laugh", "惊讶": "surprised",
            "心疼": "sad", "叹气": "sad", "着急": "sad"}


XF = 8   # 保持片段首尾各 8 帧和峰值帧渐变


def _read_rgb(p: Path, w: int, h: int, fps: bool = True):
    vf = (f"fps=25," if fps else "") + f"scale={w}:{h},format=rgb24"
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-vf", vf, "-f", "rawvideo", "-"],
                         check=True, capture_output=True).stdout
    import numpy as np
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)


def _read_yuv(p: Path, w: int, h: int, fps: bool = True):
    """读成 yuv420p 原始平面（N, h*w*3/2），不经过 RGB：和其他段的编码同一套色彩约定。"""
    import numpy as np
    vf = (f"fps=25," if fps else "") + f"scale={w}:{h},format=yuv420p"
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-vf", vf, "-f", "rawvideo", "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h * w * 3 // 2)


def blend_hold(src: Path, main_norm: Path, peak: int, w: int, h: int, out: Path) -> None:
    """保持片段：模型重画的画面比原片段略暗、纹理也有细微差别，直接接会"闪"一下。
    → 整段按峰值帧做逐平面调色（Y/U/V 各自均值/方差对齐），首尾各 XF 帧和峰值帧渐变，首帧和尾帧就是峰值帧本身，
    这样从峰值帧进、在保持片段里循环、回到峰值帧之后，三个接口都是同一帧。
    全程在 YUV 里做：之前转 RGB 调色再编回 YUV，色彩范围/矩阵对不上，整段偏亮偏粉（用户看到"笑着说话最后变粉"）。"""
    import numpy as np
    pk = _read_yuv(main_norm, w, h, fps=False)[peak].astype(np.float32)
    fr = _read_yuv(src, w, h).astype(np.float32)
    ny = w * h
    nq = ny // 4
    planes = [(0, ny), (ny, ny + nq), (ny + nq, ny + 2 * nq)]
    for a0, a1 in planes:
        x = fr[:, a0:a1]
        mu, sd = x.mean(), x.std() + 1e-6
        fr[:, a0:a1] = (x - mu) / sd * pk[a0:a1].std() + pk[a0:a1].mean()
    n = len(fr)
    for k in range(XF):
        a = k / XF                                   # 0 = 峰值帧，越往里越接近保持片段本身
        fr[k] = pk * (1 - a) + fr[k] * a
        fr[n - 1 - k] = pk * (1 - a) + fr[n - 1 - k] * a
    data = np.clip(fr, 0, 255).astype(np.uint8).tobytes()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}", "-r", "25",
                    "-i", "-", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "16", "-preset", "fast", str(out)],
                   input=data, check=True)


# 老人的动作要慢：讲话段、情绪段、保持片段建底板时补帧慢放（待机/倾听本来就慢，不动）。
# 实测讲话段 6.6 秒点头 4~7 次、最快每秒移动半个脸宽，像年轻人；0.7 倍后速度降 30%
SLOW_SEGS = {"talk_a", "talk_b", "talk_c", "happy", "laugh", "surprised", "sad"}


def slow_clip(src: Path, out: Path, factor: float, w: int, h: int) -> None:
    """运动补偿补帧慢放（ffmpeg minterpolate）：动作轨迹不变，变慢、变长；首尾帧不变（仍是照片姿势/峰值帧）。"""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf",
                    f"fps=25,scale={w}:{h},setpts=PTS/{factor},"
                    "minterpolate=fps=25:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,format=yuv420p",
                    "-an", "-c:v", "libx264", "-crf", "16", "-preset", "fast", str(out)], check=True)


def frames_of(p: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                          "stream=nb_read_frames", "-of", "csv=p=0", str(p)], check=True, capture_output=True, text=True)
    return int(out.stdout.strip())


async def main() -> None:
    import animate
    import cluster
    import ot_pool
    from config import AVATAR_MODEL, BASE_AVATAR

    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--regen", default="")
    ap.add_argument("--variants", type=int, default=0, help="只给 --regen 的段各生成 N 个候选（<段>.v1.mp4…），不建形象")
    ap.add_argument("--pick", default="", help="选定候选：如 sad=2，把 sad.v2.mp4 设为正式段再建形象")
    ap.add_argument("--slow", type=float, default=1.0,
                    help="讲话段/情绪段/保持片段慢放倍数（如 0.7 = 放慢到 0.7 倍速），待机/倾听段不动")
    ap.add_argument("--grain", type=int, default=0,
                    help="底板烧进胶片颗粒强度（ffmpeg noise，0=不加；建议 7）。说话时嘴部生成区域由补丁 9 补同样的颗粒+锐化")
    a = ap.parse_args()
    image = Path(a.image).read_bytes()
    cache = Path("/w/data/emo") / a.name
    cache.mkdir(parents=True, exist_ok=True)
    regen = set(filter(None, a.regen.split(",")))
    GEN = animate._gen_video
    prompts = dict(CLIPS)
    if a.variants:
        jobs = [(n, k) for n in regen for k in range(1, a.variants + 1)]
        res = await asyncio.gather(*[GEN(image, prompts[n]) for n, _ in jobs], return_exceptions=True)
        for (n, k), r in zip(jobs, res):
            if isinstance(r, Exception):
                print(f"{n}.v{k} failed: {r}", flush=True)
            else:
                (cache / f"{n}.v{k}.mp4").write_bytes(r)
                print(f"got {n}.v{k}", flush=True)
        return
    for kv in filter(None, a.pick.split(",")):
        n, k = kv.split("=")
        (cache / f"{n}.mp4").write_bytes((cache / f"{n}.v{k}.mp4").read_bytes())
        print(f"picked {n}.v{k}", flush=True)

    todo = [(n, p) for n, p in CLIPS if n in regen or not (cache / f"{n}.mp4").exists()]
    print("generate:", [n for n, _ in todo], flush=True)
    res = await asyncio.gather(*[GEN(image, p) for _, p in todo], return_exceptions=True)
    for (n, _), r in zip(todo, res):
        if isinstance(r, Exception):
            raise SystemExit(f"{n} failed: {r}")
        (cache / f"{n}.mp4").write_bytes(r)
        print("got", n, len(r), flush=True)

    # 统一 25fps、尺寸按第一段，硬拼接（每段首尾都是照片姿势，段与段直接接上）
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        norm, ranges, start = [], {}, 0
        first = cache / f"{CLIPS[0][0]}.mp4"
        wh = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                             "-of", "csv=p=0", str(first)], check=True, capture_output=True, text=True).stdout.strip()
        w, h = (int(x) for x in wh.split(","))
        slow_jobs, seg_files = [], []
        for n, _ in CLIPS:
            o = d / f"{n}.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(cache / f"{n}.mp4"), "-vf",
                            f"fps=25,scale={w}:{h},format=yuv420p", "-an", "-c:v", "libx264", "-crf", "16",
                            "-preset", "fast", str(o)], check=True)
            if a.slow < 0.999 and n in SLOW_SEGS:
                so = d / f"{n}.slow.mp4"
                slow_jobs.append((o, so))
                o = so
            seg_files.append((n, o))
        if slow_jobs:                                   # 补帧很吃 CPU：4 段并行
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(4) as ex:
                list(ex.map(lambda j: slow_clip(j[0], j[1], a.slow, w, h), slow_jobs))
            print(f"slowed {len(slow_jobs)} segments to {a.slow}x", flush=True)
        for n, o in seg_files:
            k = frames_of(o)
            ranges[n] = [start, start + k]
            start += k
            norm.append(o)
        # 保持片段：在每个强情绪段里找峰值帧（离照片姿势最远的一帧，限定段的 20%~60%），以它为首尾帧生成
        import numpy as np
        holds = {}
        for name in HOLDS:
            o = d / f"{name}.mp4"
            raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(o), "-vf", "scale=48:72,format=gray", "-f", "rawvideo", "-"],
                                 check=True, capture_output=True).stdout
            fr = np.frombuffer(raw, np.uint8).reshape(-1, 72, 48).astype(np.float32)
            L = len(fr)
            lo, hi = int(L * 0.2), int(L * 0.6)
            peak = lo + int(np.argmax([np.abs(fr[i] - fr[0]).mean() for i in range(lo, hi)]))
            meta = cache / f"{name}_hold.json"
            if not (cache / f"{name}_hold.mp4").exists() or json.loads(meta.read_text() if meta.exists() else "{}").get("peak") != peak:
                img = d / f"{name}_peak.jpg"
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(o), "-vf", f"select=eq(n\\,{peak})", "-frames:v", "1",
                                "-q:v", "2", str(img)], check=True)
                holds[name] = (peak, img.read_bytes())
            else:
                holds[name] = (peak, None)
        gen = [(nm, pk, im) for nm, (pk, im) in holds.items() if im is not None]
        print("hold clips to generate:", [(nm, pk) for nm, pk, _ in gen], flush=True)
        res = await asyncio.gather(*[GEN(im, HOLDS[nm]) for nm, _, im in gen], return_exceptions=True)
        for (nm, pk, _), r in zip(gen, res):
            if isinstance(r, Exception):
                raise SystemExit(f"hold {nm} failed: {r}")
            (cache / f"{nm}_hold.mp4").write_bytes(r)
            (cache / f"{nm}_hold.json").write_text(json.dumps({"peak": pk}))
        hold_meta = {}
        for name, (peak, _) in holds.items():
            o = d / f"{name}_hold.mp4"
            src, main = cache / f"{name}_hold.mp4", d / f"{name}.mp4"
            if a.slow < 0.999 and name in SLOW_SEGS:
                # 峰值帧在原速片段上找的（缓存的保持片段才不用重新生成），换算到慢放后的帧号；保持片段本身也慢放
                main = d / f"{name}.slow.mp4"
                peak = min(int(round(peak / a.slow)), frames_of(main) - 1)
                ss = d / f"{name}_hold.slow.mp4"
                slow_clip(src, ss, a.slow, w, h)
                src = ss
            blend_hold(src, main, peak, w, h, o)
            k = frames_of(o)
            ranges[f"{name}_hold"] = [start, start + k]
            hold_meta[name] = {"peak": peak, "range": [start, start + k]}
            start += k
            norm.append(o)
        lst = d / "list.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in norm))
        out = d / "base.mp4"
        if a.grain > 0:     # 颗粒随帧变化（allf=t），和真实摄像头噪点一样
            # 颗粒很占码率：节点上传上限 200MB，从高画质往下试，直到放得下
            for crf in (16, 19, 22, 25, 28):
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                                "-vf", f"noise=alls={a.grain}:allf=t", "-c:v", "libx264", "-crf", str(crf),
                                "-preset", "medium", "-pix_fmt", "yuv420p", str(out)], check=True)
                mb = out.stat().st_size / 1e6
                print(f"grain base crf={crf}: {mb:.0f}MB", flush=True)
                if mb < 190:
                    break
        else:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)],
                           check=True)
        total = frames_of(out)
        print("base:", total, "frames", ranges, flush=True)
        video = out.read_bytes()

    idle = [ranges[IDLE[0]][0], ranges[IDLE[-1]][1]]
    seg = {"segments": ranges, "idle": idle, "emotions": EMOTIONS, "frames": total, "hold": hold_meta}
    if a.grain > 0:
        seg["grain"] = a.grain            # 补丁 9 看到这个字段才给嘴部补颗粒+锐化（只对烧了颗粒的形象生效）

    await cluster_ready()
    import os as _os
    host = _os.environ.get("PUBLIC_HOST", "")
    insts = cluster.instances(include_draining=True)
    # 建在控制面所在机器的节点上（分段表要写进它的形象目录），再同步给其他节点
    inst = next((i for i in insts if host and (host in i.node.url or i.node.id == _os.environ.get("NODE_ID"))), None) or ot_pool.first()
    r = await inst.node.client.post(inst.path("avatars/custom"),
                                    data={"base_avatar_id": BASE_AVATAR, "name": a.name, "model": AVATAR_MODEL},
                                    files={"video": ("idle.mp4", video, "video/mp4")}, timeout=900)
    r.raise_for_status()
    aid = r.json()["id"]
    # 分段表写进形象目录（控制面和本机节点共用 /w/avatars），再同步到其他节点
    for _ in range(60):                    # 节点写目录可能稍晚于接口返回
        if (Path("/w/avatars") / aid).is_dir():
            break
        await asyncio.sleep(1)
    (Path("/w/avatars") / aid / "emotion_segments.json").write_text(json.dumps(seg, ensure_ascii=False, indent=1), "utf-8")
    # 过渡表（情绪段之间在最像的两帧处切换）：和本脚本放在同一目录
    sys.path.insert(0, str(Path(__file__).parent))
    import emotion_transitions
    emotion_transitions.main(str(Path("/w/avatars") / aid))
    inst.node.avatars.add(aid)
    await cluster.sync_avatar(aid, inst.node)
    r = await ot_pool.prewarm(aid, {"model": AVATAR_MODEL})
    print("avatar", aid, "prewarm", r.status_code, flush=True)
    print(json.dumps({"avatar_id": aid, **seg}, ensure_ascii=False))


async def cluster_ready() -> None:
    """独立进程里没有心跳：直接问控制面拿节点表，填进 cluster。"""
    import cluster
    import httpx
    async with httpx.AsyncClient(verify=False, timeout=10) as c:
        d = (await c.get("https://127.0.0.1:8443/health")).json()
    for nid, n in d["nodes"].items():
        if not n.get("alive"):
            continue
        cluster.on_heartbeat({"node_id": nid, "url": n["url"], "avatars": [],
                              "instances": [{"idx": i["idx"], "cap": i.get("cap", 0), "ready": i.get("ready", True)}
                                            for i in n["instances"]]}, [], "")


if __name__ == "__main__":
    asyncio.run(main())
