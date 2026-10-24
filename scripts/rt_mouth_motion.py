"""录像里每秒的嘴部动作量（人脸 106 点定位嘴部，相邻帧差）：说话时约 10~16，不说话约 5~8。
用来查"打断后下一句嘴不动"这类问题，也可以算整段录像里人脸和原照片的相似度。
在渲染节点容器里跑（有 insightface 和 buffalo_l 模型）：
  python /tmp/rt_mouth_motion.py a.mp4 b.mp4
  python /tmp/rt_mouth_motion.py --ref /w/avatars/<形象>/reference.png a.mp4     # 再算每 4 秒的脸部相似度
"""
import argparse

import cv2
import numpy as np
from insightface.app import FaceAnalysis

MODELS = "/w/models/quicktalk/checkpoints/auxiliary"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--ref", default="", help="原照片：给了就再算脸部相似度")
    a = ap.parse_args()
    lm_app = FaceAnalysis(name="buffalo_l", root=MODELS, allowed_modules=["detection", "landmark_2d_106"],
                          providers=["CPUExecutionProvider"])
    lm_app.prepare(ctx_id=-1, det_size=(320, 320))
    rec_app, ref = None, None
    if a.ref:
        rec_app = FaceAnalysis(name="buffalo_l", root=MODELS, providers=["CPUExecutionProvider"])
        rec_app.prepare(ctx_id=-1, det_size=(640, 640))
        ref = rec_app.get(cv2.imread(a.ref))[0].normed_embedding
    for p in a.videos:
        c = cv2.VideoCapture(p)
        fps = c.get(cv2.CAP_PROP_FPS) or 25
        i, per, prev, sims = 0, {}, None, []
        while True:
            ok, x = c.read()
            if not ok:
                break
            if i % 2 == 0:
                f = lm_app.get(x)
                if f:
                    lm = f[0].landmark_2d_106[52:72]
                    x0, y0 = lm.min(0).astype(int) - 6
                    x1, y1 = lm.max(0).astype(int) + 6
                    m = cv2.cvtColor(cv2.resize(x[max(y0, 0):y1, max(x0, 0):x1], (60, 30)), cv2.COLOR_BGR2GRAY).astype(float)
                    if prev is not None:
                        per.setdefault(int(i / fps), []).append(np.abs(m - prev).mean())
                    prev = m
            if rec_app is not None and i % int(fps * 4) == 0:
                f = rec_app.get(x)
                if f:
                    sims.append(round(float(np.dot(ref, f[0].normed_embedding)), 2))
            i += 1
        series = [round(float(np.mean(per[k])), 1) if k in per else 0 for k in range(max(per) + 1)] if per else []
        print(p, "mouth motion per second:", series)
        if sims:
            print(p, "identity per 4s:", sims)


if __name__ == "__main__":
    main()
