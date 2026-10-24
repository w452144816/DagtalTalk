"""情绪底板的过渡表（视频纹理 / 动作图）：底板里所有帧两两比相似度，给每一帧算出
"再往下放 m 帧（≤MAX_WAIT），然后跳到目标段第 j 帧"代价最小的走法，写进 emotion_segments.json 的 trans 字段。
OpenTalking 补丁 6 切情绪段、说完回待机时按表在最像的两帧之间硬接，不跳、不用淡化。
用法（控制面容器里）：python emotion_transitions.py /w/avatars/<形象 id>
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

W, H = 96, 144           # 读帧的缩略图尺寸（竖版）；比较只用脸的上 2/3（见 face_region）
ENTRY = 0.4              # 进情绪段只落在该段前 40%（情绪刚起来），进去后能完整演完"起来→回落"
MAX_WAIT = 50            # 最多再往下放 2 秒找过渡点（实测 1 秒内一半找不到够像的帧，2 秒内中位数和相邻帧差一样小）
LAMBDA = 0.02            # 每多等一帧的代价（像素差量纲；相邻帧差中位约 0.2，等 50 帧折合 1.0）


def load_frames(video: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"scale={W}:{H},format=gray",
                          "-f", "rawvideo", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.float32)


def face_region(frames: np.ndarray) -> tuple[slice, slice]:
    """脸在哪：底板里动得最多的区域（表情和头动）就是脸；只取眉眼一带（嘴会被口型模型替换，不比）。"""
    std = frames.std(0)
    mask = std > np.percentile(std, 85)
    ys, xs = np.where(mask)
    y0, y1 = np.percentile(ys, [3, 97]).astype(int)
    x0, x1 = np.percentile(xs, [3, 97]).astype(int)
    y1 = y0 + int((y1 - y0) * 0.45)          # 动得最多的区域从头巾/额头一直到领口，取上 45% ≈ 额头到鼻子
    return slice(int(y0), int(max(y1, y0 + 4))), slice(int(x0), int(max(x1, x0 + 4)))


def main(avatar_dir: str) -> None:
    d = Path(avatar_dir)
    segp = d / "emotion_segments.json"
    seg = json.loads(segp.read_text("utf-8"))
    frames = load_frames(d / "source" / "source_video.mp4")
    n = len(frames)
    if n != seg["frames"]:
        raise SystemExit(f"帧数不一致：视频 {n}，分段表 {seg['frames']}")
    ry, rx = face_region(frames)
    print("face region (rows, cols):", (ry.start, ry.stop), (rx.start, rx.stop), "of", frames.shape[1:])
    flat = frames[:, ry, rx].reshape(n, -1)
    whole = frames[:, ::2, ::2].reshape(n, -1)            # 整帧（头的位置、身体）也要像：只看脸会接受"脸像但头挪了位置"的跳
    # 990×990 平均绝对差：分块算，省内存
    D = np.empty((n, n), np.float32)
    DW = np.empty((n, n), np.float32)
    for i in range(0, n, 64):
        D[i:i + 64] = np.abs(flat[i:i + 64, None, :] - flat[None, :, :]).mean(-1)
        DW[i:i + 64] = np.abs(whole[i:i + 64, None, :] - whole[None, :, :]).mean(-1)

    segs = {k: tuple(v) for k, v in seg["segments"].items()}
    owner = np.empty(n, int)                     # 每帧属于哪一段（段起点）
    seg_of = {}
    for name, (a, b) in segs.items():
        owner[a:b] = a
        seg_of.update({i: (a, b) for i in range(a, b)})

    def nxt(p: int, k: int) -> int:             # 不切换时，p 之后第 k 帧（段内循环）
        a, b = seg_of[p]
        return a + (p - a + k) % (b - a)

    # 门槛按待机段（平静/温柔）的相邻帧差算：过渡表只用于"从待机进第一个情绪"，笑段的大幅抖动不能把门槛撑大
    ia, ib = seg["idle"]
    adj = np.array([D[i, nxt(i, 1)] for i in range(ia, ib)])
    adjw = np.array([DW[i, nxt(i, 1)] for i in range(ia, ib)])
    # 两个门槛都要过：脸部差不超过相邻帧差的 p90×1.2，整帧差不超过 p95×1.2（底板里平常一帧到下一帧的变化量）
    thresh = float(np.percentile(adj, 90) * 1.2)
    thresh_w = float(np.percentile(adjw, 95) * 1.2)
    ok = (DW <= thresh_w)
    targets = {k: v for k, v in segs.items() if not k.endswith("_hold")}   # 保持片段只从峰值帧接进去，不当过渡目标
    targets["idle"] = tuple(seg["idle"])
    trans, stats = {}, {}
    for name, (ta, tb) in targets.items():
        rows, waits, bad = [], [], 0
        if name != "idle":
            tb = ta + max(1, int((tb - ta) * ENTRY))        # 只在情绪刚起来的部分落点
        for p in range(n):
            best = None
            for m in range(MAX_WAIT + 1):
                q = nxt(p, m + 1)                # 不切换的话第 m+1 帧本来要放的帧
                row = np.where(ok[q, ta:tb], D[q, ta:tb], np.inf)
                j = ta + int(np.argmin(row))
                if not np.isfinite(row[j - ta]):
                    continue
                cost = D[q, j] + LAMBDA * m
                if best is None or cost < best[0]:
                    best = (cost, m, j, D[q, j])
            if best is None:
                best = (np.inf, 0, ta, np.inf)
            _, m, j, dist = best
            if dist > thresh:                    # 怎么接都不够像：放到本段结尾，再从目标段起点接（首尾都是照片姿势）
                a, b = seg_of[p]
                m, j, bad = b - 1 - p, ta, bad + 1
            rows.append([int(m), int(j)])
            waits.append(m)
        trans[name] = rows
        stats[name] = {"avg_wait": round(float(np.mean(waits)), 1), "max_wait": int(np.max(waits)), "fallback": bad}
    seg["trans"] = trans
    seg["trans_meta"] = {"max_wait": MAX_WAIT, "lambda": LAMBDA, "thresh": round(thresh, 2), "entry": ENTRY,
                         "face": [ry.start, ry.stop, rx.start, rx.stop], "thresh_whole": round(thresh_w, 2),
                         "adj_p50": round(float(np.median(adj)), 2), "stats": stats}
    segp.write_text(json.dumps(seg, ensure_ascii=False), "utf-8")
    print(json.dumps(seg["trans_meta"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
