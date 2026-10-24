"""构建镜像时给 OpenTalking 打的小补丁（精确字符串替换，对不上就让构建失败），共 3 处，见下文与文件内注释。

动态形象的底板视频会动，原版有两处会"跳帧"：
1. 说话时 QuickTalk 按 正放→倒放 往返取底板帧，待机时却按 0..N 循环播原视频，两边各自计数，
   开口 / 说完的瞬间画面位置对不上。→ 待机改为和说话共用 session_state.frame_index 取同一套底板帧；
   正在准备回复（_speaking 但还没出画面，LLM+TTS+首块渲染约 1.3~2s）时待机**继续循环**（用私有计数，不动 frame_index），
   渲染起点取"待机位置 + 预测提前量"，开口前等待机走到起点再切（补丁 1b）；之前是停在当前帧，用户看到底板定格 2s 像卡死。
2. 往返播放时手势会"倒带"。→ OPENTALKING_QUICKTALK_TEMPLATE_LOOP=wrap 时改为一直正向循环
   （底板首尾已由网关交叉淡化成无缝循环，见 gateway/animate.py）。
python opentalking_patch.py /app/opentalking
"""
import sys
from pathlib import Path

root = Path(sys.argv[1])


def patch(rel: str, old: str, new: str, count: int = 1) -> None:
    p = root / rel
    s = p.read_text(encoding="utf-8")
    if s.count(old) != count:
        raise SystemExit(f"patch target: expected {count} occurrence(s) in {rel}, got {s.count(old)}: {old[:60]!r}")
    p.write_text(s.replace(old, new), encoding="utf-8")
    print("patched", rel)


RT = "opentalking/models/quicktalk/runtime.py"
patch(RT, """            cycle = state.frame_index // n
            offset = state.frame_index % n
            idx = offset if cycle % 2 == 0 else n - 1 - offset
            state.frame_index += 1""", """            idx = _template_next(state, n)""")
patch(RT, """            cycle = self.frame_index // n
            offset = self.frame_index % n
            idx = offset if cycle % 2 == 0 else n - 1 - offset
            self.frame_index += 1""", """            idx = _template_loop_index(self.frame_index, n)
            self.frame_index += 1""")
p = root / RT
p.write_text(p.read_text(encoding="utf-8") + '''

def _template_loop_index(frame_index: int, n: int, rng=None) -> int:
    """[talk patch] 底板取帧：wrap = 正向循环（底板需首尾无缝），默认 = 正放/倒放往返。
    rng=(a, b)：情绪底板只在 [a, b) 这一段里正向循环（每段首尾都是原照片姿势，段内循环无缝）。"""
    import os
    if rng:
        a, b = rng
        b = min(b, n)
        if 0 <= a < b:
            return a + frame_index % (b - a)
    if os.getenv("OPENTALKING_QUICKTALK_TEMPLATE_LOOP", "").strip().lower() == "wrap":
        return frame_index % n
    cycle, offset = divmod(frame_index, n)
    return offset if cycle % 2 == 0 else n - 1 - offset


def _template_next(state, n: int) -> int:
    """[talk patch 6] 取下一张底板帧并前进，处理情绪底板的几种跳转（都是在同一帧/最像的帧之间接，看不出）：
    - talk_pending=(m, 目标区间, 区间内位置, 放完去哪, 保持)：再放 m 帧后跳进目标情绪段（过渡表选的最像的帧）；
    - talk_hold=(峰值位置, 保持片段区间)：情绪要继续（talk_sustain）时，演到峰值帧就接进保持片段循环
      （保持片段首尾都是这一帧）；不再继续时，保持片段放完回到原段峰值帧之后，放回落部分；
    - 情绪段演完（回到照片姿势）：有排队的情绪就直接接，否则回待机区间（talk_after）。"""
    import logging
    lg = logging.getLogger(__name__)
    p = getattr(state, "talk_pending", None)
    if p is not None:
        m, rng, pos = p[:3]
        if m <= 0:
            state.talk_range, state.frame_index, state.talk_pending = rng, pos, None
            state.talk_after = p[3] if len(p) > 3 else None
            state.talk_hold = p[4] if len(p) > 4 else None
            state.talk_return = None
        else:
            state.talk_pending = (m - 1,) + tuple(p[1:])
    rng, after = getattr(state, "talk_range", None), getattr(state, "talk_after", None)
    h, ret = getattr(state, "talk_hold", None), getattr(state, "talk_return", None)
    sustain = getattr(state, "talk_sustain", False)
    if ret is not None:                                   # 正在保持片段里
        if state.frame_index >= rng[1] - rng[0]:
            if sustain:
                state.frame_index = 1                     # 首帧和尾帧是同一帧，跳过重复
            else:
                lg.info("[talk] hold %s done -> back to %s@%d", rng, ret[0], ret[1])
                state.talk_range, state.frame_index = ret
                state.talk_return = None
    elif h and sustain and after is not None and rng and state.frame_index == h[0] + 1:
        lg.info("[talk] enter hold %s at peak %d", h[1], rng[0] + h[0])
        state.talk_return = (rng, h[0] + 1)
        state.talk_range, state.frame_index = tuple(h[1]), 1
    rng = getattr(state, "talk_range", None)
    if (after is not None and rng and getattr(state, "talk_return", None) is None
            and state.frame_index >= rng[1] - rng[0]):
        # 情绪段演完一遍（回到照片姿势）：有排队的情绪直接接，否则回待机区间开头
        q = getattr(state, "talk_queued", None)
        pool = getattr(state, "talk_pool", None) or []
        if q is None and pool and (getattr(state, "talk_speaking", False) or (sustain and tuple(rng) in pool)):
            # [talk patch 11] 还在说话：不管刚演完的是讲话段还是强情绪段（已回落到照片姿势），都接讲话段，不回几乎不动的待机
            import random as _rnd
            rest = getattr(state, "talk_rest", None) or []
            cand = rest if (tuple(rng) in pool and rest) else pool      # [talk patch 17] 讲话段之后接一段低动作段
            nxt = _rnd.choice([r for r in cand if r != tuple(rng)] or cand)   # [talk patch 11] 还在说：接另一段讲话段（首尾都是照片姿势）
            lg.info("[talk] talk segment %s done -> next talk %s", rng, nxt)
            state.talk_range, state.frame_index, state.talk_hold = nxt, 0, None
        elif q is not None:
            lg.info("[talk] segment %s done -> queued %s", rng, q[0])
            state.talk_range, state.frame_index, state.talk_after, state.talk_queued = q[0], 0, q[1], None
            state.talk_hold = q[2] if len(q) > 2 else None
            state.talk_sustain = True
        else:
            lg.info("[talk] segment %s done -> %s", rng, after)
            state.talk_range, state.frame_index, state.talk_after, state.talk_hold = after, 0, None, None
    idx = _template_loop_index(state.frame_index, n, getattr(state, "talk_range", None))
    state.frame_index += 1
    state.talk_last_idx = idx
    hist = getattr(state, "talk_idx_hist", None)
    if hist is None:
        import collections
        hist = state.talk_idx_hist = collections.deque(maxlen=256)
    hist.append(idx)
    return idx
''', encoding="utf-8")

SR = "opentalking/pipeline/speak/synthesis_runner.py"
patch(SR, """        if self._quicktalk_idle_video is not None:
            idle_frame = self._quicktalk_idle_video.next_frame()""", """        if self.model_type == "quicktalk":
            idle_frame = self._quicktalk_shared_idle_frame()
        if idle_frame is None and self._quicktalk_idle_video is not None:
            idle_frame = self._quicktalk_idle_video.next_frame()""")
patch(SR, """    async def _idle_tick(self) -> None:""", '''    def _quicktalk_shared_idle_frame(self) -> np.ndarray | None:
        """[talk patch] 待机与说话共用同一个底板位置，开口 / 说完时画面连续。"""
        st = getattr(self.flashtalk, "avatar_state", None)
        ss = getattr(st, "session_state", None)
        ctxs = getattr(getattr(st, "worker", None), "restore_contexts", None)
        if ss is None or not ctxs:
            return None
        from opentalking.models.quicktalk.runtime import _template_loop_index, _template_next
        segs = self._talk_emotion_segments()
        if segs and getattr(ss, "talk_range", None) is None:
            ss.talk_range = tuple(segs["idle"])          # [补丁 6] 情绪底板：待机只在平静/温柔段里循环
            ss.frame_index = 0
        if segs and "listen" in (segs.get("segments") or {}):
            self._talk_listen_tick(ss)                   # [补丁 8] 用户说话时切"倾听"段
        rng = getattr(ss, "talk_range", None)
        wait_pos = getattr(self, "_talk_idle_wait_pos", None)
        if self._speaking and wait_pos is not None:
            # [补丁 1b] 回复准备中：待机按私有计数继续循环（顺序帧，仍可发预编码），不碰渲染线程用的 frame_index
            _af = getattr(ss, "talk_after", None)
            if (rng and _af is not None and getattr(ss, "talk_return", None) is None
                    and wait_pos >= rng[1] - rng[0]):
                # [talk patch 15] 情绪/讲话段已经演完：接着放它之后的待机段。之前在段内绕回开头重放（大动作又来一遍），
                # 渲染那边却从下一段的照片姿势开口，画面从大动作一下跳回原位（用户看到的"直接恢复到原始位置"）
                idx = _template_loop_index(wait_pos - (rng[1] - rng[0]), len(ctxs), tuple(_af))
            else:
                idx = _template_loop_index(wait_pos, len(ctxs), rng)
            self._talk_idle_wait_pos = wait_pos + 1
            self._shared_idle_ref = (ctxs, idx)
            return ctxs[idx].frame
        if not self._speaking and wait_pos is not None:
            if not getattr(self, "_talk_media_started", False):
                # 上一次说话结束：没出过画面（被打断/出错）就把 frame_index 拉回待机位置，避免前跳
                ss.talk_pending = None
                ss.frame_index = wait_pos
                _snap = getattr(self, "_talk_snap", None)       # [talk patch 16]
                if _snap is not None:
                    ss.talk_range, ss.talk_after, ss.talk_hold, ss.talk_return = _snap
                ss.talk_fresh = False
            # [补丁 6] 说完停在情绪段时不切：这段自己演完（表情自然回落到照片姿势）再接回待机（_template_next）；
            #          排队还没演的情绪丢掉（话已经说完了）；不再保持，放完回落部分
            ss.talk_queued = None
            ss.talk_sustain = False
            ss.talk_speaking = False                          # [补丁 11] 说完了：段演完回待机，不再接讲话段
            segs_ = self._talk_emotion_segments() or {}
            if "listen" in (segs_.get("segments") or {}) and getattr(ss, "talk_range", None) == tuple(segs_["segments"]["listen"]):
                ss.talk_after = tuple(segs_["idle"])          # [补丁 8] 说话前还在倾听段：演完回待机
            elif getattr(ss, "talk_range", None) in (getattr(ss, "talk_pool", None) or []):
                # [补丁 11] 说完时停在讲话段：讲话段是"在说话"的头动，嘴也可能在动，不演完，按过渡表尽快回待机
                self._talk_plan_switch(ss, "idle", getattr(ss, "talk_last_idx", 0))
            self._talk_idle_wait_pos = None
            self._talk_start_mouth_fade(ctxs)
        idx = _template_next(ss, len(ctxs))
        self._shared_idle_ref = (ctxs, idx)   # 第几张底板帧：视频轨据此发预编码数据（补丁 4）
        frame = ctxs[idx].frame
        mf = getattr(self, "_talk_mouth_fade", None)
        if mf is not None:
            frame = self._talk_apply_mouth_fade(ctxs, idx, frame, mf)
        return frame

    def _talk_smooth_mouth(self, pcm_chunk, frames) -> None:
        """[talk patch 7] 口型模型在声音一停（句尾、停顿）时一帧就把嘴合上，看着像跳。
        每帧算"生成的嘴 - 它用的底板帧"的差异：声音响的帧原样输出（不影响口型同步）；
        安静的帧让这份差异向本帧的目标慢慢靠（指数平滑，约 4~5 帧），嘴是慢慢合上的。"""
        st = getattr(self.flashtalk, "avatar_state", None)
        ctxs = getattr(getattr(st, "worker", None), "restore_contexts", None)
        if not frames or not ctxs:
            return
        pcm = np.asarray(pcm_chunk, dtype=np.float32).reshape(-1)
        n = len(frames)
        step = max(1, len(pcm) // n)
        # [talk patch 13] 只在口型模型贴图的区域（restore context 的 roi）里算：之前整帧 600x900 做差、取绝对值，
        # 每帧几十毫秒，全跑在发音视频的那个线程上，排队 1.12 秒的数据要 1.3~2 秒，声音一下有一下没（用户实测）
        prev = getattr(self, "_talk_mouth_prev", None)          # (roi, 残差)
        prev_bi = getattr(self, "_talk_mouth_prev_bi", None)
        g = (getattr(self, "_talk_segs", None) or {}).get("grain")
        for k, f in enumerate(frames):
            bi = getattr(f, "talk_base_idx", None)
            d = getattr(f, "data", None)
            if bi is None or d is None or not (0 <= bi < len(ctxs)) or ctxs[bi].frame.shape != d.shape:
                prev = prev_bi = None
                continue
            x0, y0, x1, y1 = ctxs[bi].roi
            base = ctxs[bi].frame[y0:y1, x0:x1].astype(np.int16)
            res = d[y0:y1, x0:x1].astype(np.int16) - base
            # [talk patch 12] 混合按固定像素位置做：底板头在动时上一帧的嘴会留下影子 → 只在嘴部底板几乎没动时才混合
            still = False
            if prev is not None and prev[0] == (x0, y0, x1, y1) and prev_bi is not None and 0 <= prev_bi < len(ctxs):
                _pb = ctxs[prev_bi].frame[y0:y1, x0:x1].astype(np.int16)
                still = float(np.abs(base - _pb).mean()) < _TALK_STILL_DIFF
            prev_bi = bi
            seg = pcm[k * step:(k + 1) * step]
            loud = seg.size > 0 and float(np.sqrt(np.mean(seg * seg))) > _TALK_QUIET_RMS
            out = None
            if not loud and still:
                res = (prev[1] * 0.65 + res * 0.35).astype(np.int16)
                out = d.copy()
                out[y0:y1, x0:x1] = np.clip(base + res, 0, 255).astype(np.uint8)
            prev = ((x0, y0, x1, y1), res)
            if g:
                if out is None:
                    out = d.copy()
                out[y0:y1, x0:x1] = self._talk_mouth_finish(out[y0:y1, x0:x1], res, g)
            if out is not None:
                f.data = out
        self._talk_mouth_prev = prev
        self._talk_mouth_prev_bi = prev_bi

    def _talk_mouth_finish(self, img, res, grain):
        """[talk patch 9] 口型模型生成的嘴部区域比真实皮肤光滑、边缘发虚：在生成区域里锐化，并补上和底板一样的颗粒
        （底板建形象时烧了颗粒，生成区域被替换后没有颗粒，看着像磨皮）。区域边缘羽化，不出框。"""
        import cv2
        if not getattr(type(self), "_talk_cv_threads", False):
            cv2.setNumThreads(2)                          # [talk patch 13] OpenCV 默认开满核的线程池，小区域反而空转抢 CPU
            type(self)._talk_cv_threads = True
        m = np.abs(res).max(axis=-1) > 6
        if not m.any():
            return img
        ys, xs = np.nonzero(m)
        pad = 12
        y0, y1 = max(0, ys.min() - pad), min(img.shape[0], ys.max() + 1 + pad)
        x0, x1 = max(0, xs.min() - pad), min(img.shape[1], xs.max() + 1 + pad)
        roi = img[y0:y1, x0:x1].astype(np.float32)
        mk = cv2.GaussianBlur(m[y0:y1, x0:x1].astype(np.float32), (0, 0), 6)
        mk = np.clip(mk * 1.5, 0, 1)[..., None]
        blur = cv2.GaussianBlur(roi, (0, 0), 1.6)
        sharp = roi * (1 + _TALK_SHARPEN) - blur * _TALK_SHARPEN
        rng = getattr(self, "_talk_rng", None)
        if rng is None:
            rng = self._talk_rng = np.random.default_rng()
        noise = rng.normal(0.0, grain * _TALK_GRAIN_SCALE, roi.shape[:2])[..., None]
        out = img.copy()
        out[y0:y1, x0:x1] = np.clip(roi * (1 - mk) + (sharp + noise) * mk, 0, 255).astype(np.uint8)
        return out

    def _talk_start_mouth_fade(self, ctxs) -> None:
        """[talk patch 7] 说完：最后一帧口型画面 = 底板帧 + 嘴部差异（生成的嘴）。把这份差异留下，
        叠到接下来的待机底板帧上，0.4 秒内渐隐到 0——嘴从"生成的嘴"平滑过渡回原视频的嘴，底板照常往前走。"""
        self._talk_mouth_fade = None
        out, oi = getattr(self, "_talk_last_out", None), getattr(self, "_talk_last_out_idx", None)
        if out is None or oi is None or not getattr(self, "_talk_media_started", False) or not (0 <= oi < len(ctxs)):
            return
        base = ctxs[oi].frame
        if out.shape != base.shape:
            return
        res = out.astype(np.int16) - base.astype(np.int16)
        res[np.abs(res).max(axis=-1) < 6] = 0            # 只留贴图区域的差异（压缩噪声置 0）
        ys, xs = np.nonzero(np.abs(res).max(axis=-1))
        if len(ys) == 0:
            return
        box = (ys.min(), ys.max() + 1, xs.min(), xs.max() + 1)
        y0, y1, x0, x1 = box
        self._talk_mouth_fade = {"res": res[y0:y1, x0:x1], "box": box, "k": 0, "n": _TALK_MOUTH_FADE,
                                 "prev": oi, "ref": base[y0:y1, x0:x1].astype(np.int16)}
        log.info("[talk] mouth fade start: base %d, area %dx%d", oi, x1 - x0, y1 - y0)

    def _talk_apply_mouth_fade(self, ctxs, idx, frame, mf):
        """[talk patch 7] 待机帧叠加渐隐中的嘴部残差。底板不连续（情绪段跳转）就放弃；头动得多就加快结束，免得嘴"飘"。"""
        y0, y1, x0, x1 = mf["box"]
        if idx != mf["prev"] + 1 or self._speaking:
            self._talk_mouth_fade = None
            return frame
        mf["prev"] = idx
        cur = frame[y0:y1, x0:x1].astype(np.int16)
        res = mf["res"]
        if float(np.abs(cur - mf["ref"]).mean()) > 3:
            # [talk patch 12] 底板头动了：估计嘴部区域相对渐隐开始时的平移，残差跟着挪，嘴不"飘"、不留影子；
            # 估不准（相关性低）或挪得太多就 2 帧内收完
            import cv2
            g0 = cv2.cvtColor(mf["ref"].astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
            g1 = cv2.cvtColor(cur.astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
            (dx, dy), resp = cv2.phaseCorrelate(g0, g1)
            if resp < 0.2 or abs(dx) > 12 or abs(dy) > 12:
                if mf["n"] - mf["k"] > 2:
                    mf["n"] = mf["k"] + 2
            else:
                mt = np.float32([[1, 0, dx], [0, 1, dy]])
                res = cv2.warpAffine(res.astype(np.float32), mt, (res.shape[1], res.shape[0]),
                                     flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        mf["k"] += 1
        t = min(1.0, mf["k"] / mf["n"])
        w = 1.0 - (3 * t * t - 2 * t ** 3)                # 缓动：开头和结尾都慢
        out = frame.copy()
        out[y0:y1, x0:x1] = np.clip(cur + res * w, 0, 255).astype(np.uint8)
        self._shared_idle_ref = None                      # 混合帧不能用预编码
        if mf["k"] >= mf["n"]:
            self._talk_mouth_fade = None
            log.info("[talk] mouth fade done")
        return out

    def _talk_listen_tick(self, ss) -> None:
        """[talk patch 8] 用户在说话（网关 /internal/conv_state）且数字人没在说话：按过渡表从待机切进"倾听"段，
        在倾听段里循环（看着你、轻轻点头）；用户说完就把这一段演完（回到照片姿势）再回待机。
        正在演情绪段时不打断（演完回待机后下一帧再切）。"""
        if getattr(self, "_talk_conv_task", None) is None:
            import asyncio as _aio
            self._talk_conv_task = _aio.get_event_loop().create_task(self._talk_conv_poll())
        segs = self._talk_segs
        lr = tuple(segs["segments"]["listen"])
        idle = tuple(segs["idle"])
        listening = getattr(self, "_talk_listening", False)
        cur = getattr(ss, "talk_range", None)
        if listening and not self._speaking:
            if cur == idle and getattr(ss, "talk_pending", None) is None and getattr(ss, "talk_after", None) is None:
                self._talk_plan_switch(ss, "listen", getattr(ss, "talk_last_idx", 0))
                p = getattr(ss, "talk_pending", None)
                if p is not None:
                    ss.talk_pending = (p[0], p[1], p[2], None, None)   # 倾听时一直循环，不自动回待机
            elif cur == lr:
                ss.talk_after = None
        elif cur == lr and getattr(ss, "talk_after", None) is None:
            ss.talk_after = idle                                    # 不再倾听：演完这一段回待机

    async def _talk_conv_poll(self) -> None:
        """[talk patch 8] 每 0.2 秒问网关：用户是不是正在说话。"""
        import asyncio as _aio
        import os as _os
        import httpx as _httpx
        port = _os.environ.get("GATEWAY_LOCAL_PORT", "8301")
        async with _httpx.AsyncClient(timeout=0.5, trust_env=False) as c:
            while not self._closed:
                try:
                    r = await c.get(f"http://127.0.0.1:{port}/internal/conv_state", params={"sid": self.session_id})
                    v = bool(r.json().get("listening"))
                    if v != getattr(self, "_talk_listening", False):
                        log.info("[talk] user %s", "speaking -> listen" if v else "stopped")
                    self._talk_listening = v
                except Exception:
                    pass
                await _aio.sleep(0.2)

    def _talk_emotion_segments(self):
        """[talk patch 6] 形象目录里的 emotion_segments.json（情绪底板分段表），没有就 None。"""
        if hasattr(self, "_talk_segs"):
            return self._talk_segs
        import json as _json
        import os as _os
        p = _os.path.join(_os.environ.get("OPENTALKING_AVATARS_DIR", "/w/avatars"), str(self.avatar_id), "emotion_segments.json")
        try:
            self._talk_segs = _json.load(open(p, encoding="utf-8"))
            log.info("[talk] emotion segments for %s: %s", self.avatar_id, self._talk_segs.get("segments"))
        except Exception:
            self._talk_segs = None
        return self._talk_segs

    def _talk_plan_switch(self, ss, target: str, last_idx: int) -> None:
        """[talk patch 6] 从刚取的帧 last_idx 切到 target 段（或 "idle"）：查过渡表得"再放 m 帧、跳到第 j 帧"，
        记到 ss.talk_pending 由 _template_next 执行。没有表（旧分段表）就放到本段结尾再从目标段起点接（首尾都是照片姿势）。"""
        segs = self._talk_emotion_segments() or {}
        rng = tuple(segs["idle"]) if target == "idle" else tuple((segs.get("segments") or {}).get(target) or ())
        if not rng or rng == getattr(ss, "talk_range", None):
            ss.talk_pending = None
            return
        after = tuple(segs["idle"]) if target != "idle" and segs.get("idle") else None
        _h = (segs.get("hold") or {}).get(target)          # {"peak": 段内峰值帧, "range": 保持片段区间}
        hold = (int(_h["peak"]), tuple(_h["range"])) if isinstance(_h, dict) else None
        weak = target in ("calm", "gentle")
        cur = getattr(ss, "talk_range", None)
        if getattr(ss, "talk_return", None) is not None:
            cur = ss.talk_return[0]                        # 正在保持片段里：当前情绪是它所属的那个段
        # [talk patch 11] 讲话段：头和眉眼在"说话"（点头、扬眉、歪头），嘴唇闭合由口型模型换嘴。平静/温柔的句子用它，
        # 不再停在几乎不动的待机段；连着说就一段接一段（_template_next 里按 talk_pool 换），说完演完回待机
        pool = [(k, tuple(v)) for k, v in (segs.get("segments") or {}).items() if k.startswith("talk")]
        ss.talk_pool = [r for _, r in pool] or None
        # [talk patch 17] 老人说话不会一直点头：讲话段和低动作段（温柔/平静变体，本来就在待机区间里）交替
        ss.talk_rest = [tuple(v) for k, v in (segs.get("segments") or {}).items() if k in ("gentle", "calm2", "gentle2")] or None
        in_talk = cur in ((ss.talk_pool or []) + (ss.talk_rest or []))
        # [talk patch 16] 新一轮的第一句：上一轮没演完的情绪段/保持循环不再"保持"或"排队"，按这一轮的情绪重新切
        fresh = bool(getattr(ss, "talk_fresh", False)) and target != "idle"
        if fresh:
            ss.talk_fresh = False
            if rng == cur:
                ss.talk_sustain = True                     # 恰好还是同一个情绪：接着保持
                log.info("[talk] new turn keeps %s", cur)
                return
        if weak and target != "idle" and pool and not in_talk and (getattr(ss, "talk_after", None) is None or fresh):
            import random as _rnd
            target, rng = _rnd.choice(pool)
            weak, hold = False, None
            log.info("[talk] speaking (weak emotion) -> talk segment %s", target)
        if fresh and getattr(ss, "talk_return", None) is not None and not weak:
            # [talk patch 16] 还在上一轮的保持循环里（停在情绪峰值，不是照片姿势）：过渡表的兜底"放到段尾再接"在保持片段里不成立，
            # 会从峰值表情直接跳到新段开头。→ 保持循环收尾、原情绪段把回落演完（回到照片姿势）后再接这一轮的段
            ss.talk_queued = (rng, after, hold)
            ss.talk_sustain = False
            log.info("[talk] new turn: finish hold of %s, then %s", cur, target)
            return
        if target != "idle" and getattr(ss, "talk_after", None) is not None and in_talk and not weak and rng != cur:
            pass                                           # 讲话段里来了强情绪：不排队，下面按过渡表在最像的帧切过去
        elif target != "idle" and getattr(ss, "talk_after", None) is not None and not fresh:
            if rng == cur or weak:
                # 同一个情绪、或是温柔/平静这种弱情绪：保持当前情绪（在保持循环里继续），不回到平静
                ss.talk_sustain = True
                log.info("[talk] sustain %s (next sentence: %s)", cur, target)
                return
            # 不同的强情绪：当前情绪不再保持，放完回落部分（回到照片姿势）后直接接排队的
            ss.talk_queued = (rng, after, hold)
            ss.talk_sustain = False
            log.info("[talk] queue %s after current segment %s", target, cur)
            return
        if weak and target != "idle":
            return                                   # 待机本来就是平静/温柔，不切
        tr = (segs.get("trans") or {}).get(target)
        if tr and 0 <= last_idx < len(tr):
            m, j = tr[last_idx]
        else:
            cur = getattr(ss, "talk_range", None) or rng
            m, j = max(0, cur[1] - 1 - last_idx), rng[0]
        ss.talk_pending = (int(m), rng, int(j) - rng[0], after, hold)
        ss.talk_sustain = target != "idle"           # [talk patch 16] 回待机不保持（否则讲话段放完会接着换下一段讲话段）
        log.info("[talk] plan switch -> %s from %d: wait %d then frame %d", target, last_idx, m, j)

    async def _talk_emotion_seg(self, text: str):
        """[talk patch 6] 这句话的情绪（问网关，LLM 标签在网关里剥掉记下的）→ 情绪底板里的段名；没有返回 None。"""
        segs = self._talk_emotion_segments()
        if not segs or not text:
            return None
        import os as _os
        import httpx as _httpx
        try:
            port = _os.environ.get("GATEWAY_LOCAL_PORT", "8301")
            async with _httpx.AsyncClient(timeout=0.5, trust_env=False) as c:
                emo = (await c.get(f"http://127.0.0.1:{port}/internal/emotion", params={"text": text})).json().get("emotion", "")
        except Exception:
            emo = ""
        name = (segs.get("emotions") or {}).get(emo) or ""
        if not name and any(k.startswith("talk") for k in (segs.get("segments") or {})):
            name = "calm"                                  # [talk patch 11] 没标情绪也算平静：要进讲话段，不能停在待机
        log.info("[talk] emotion %r -> segment %s: %s", emo, name or "-", text[:20])
        return name if name in (segs.get("segments") or {}) else None

    async def _talk_idle_catchup(self) -> None:
        """[talk patch 1b] 开口前：等待机走到渲染起点（预测提前量偏大时多等几帧，偏小时开口画面倒退几帧），
        并按实际等待帧数更新下次的提前量（EMA + 2 帧余量）。"""
        target = getattr(self, "_talk_speak_start_idx", None)
        if target is None or getattr(self, "_talk_media_started", False):
            return
        import asyncio as _aio
        import time as _t
        # 待机 tick 没在跑（没有 WebRTC 对端，如 smoke / 预热会话）时计数不会动：0.2s 没前进就不等了
        pos = last = self._talk_idle_wait_pos or 0
        natural = pos - self._talk_gen_start_pos   # 首块就绪时待机实际走了几帧（不含下面的等待）：用它预测下次提前量
        deadline = _t.monotonic() + 1.0
        stall = _t.monotonic() + 0.2
        while pos < target and not self._interrupt.is_set() and _t.monotonic() < min(deadline, stall):
            await _aio.sleep(0.005)
            pos = self._talk_idle_wait_pos or 0
            if pos != last:
                last, stall = pos, _t.monotonic() + 0.2
        lead = self._talk_lead_frames
        self._talk_media_started = True
        if 0 < natural <= 2 * lead + 15:   # 待机真在跑才更新提前量；进程内所有会话共用（机器相关，不是会话相关）
            # 异常慢的一次（如 worker 冷启动，首块 10 秒、natural=256）不学：之前一次就把提前量顶到 60，
            # 之后每句开口都要等满 1 秒还追不上，画面往前跳 20 多帧（用户看到"卡"）
            type(self)._talk_lead_shared = max(5, min(60, round(0.7 * lead + 0.3 * (natural + 2))))
        log.info("[talk] idle catchup: session=%s lead=%d natural=%d waited=%d overshoot=%d next_lead=%d",
                 self.session_id, lead, natural, max(0, pos - natural - self._talk_gen_start_pos), max(0, pos - target),
                 getattr(type(self), "_talk_lead_shared", lead))

    async def _idle_tick(self) -> None:''')

# 1b. 配合上面：进入说话态时初始化待机私有计数；首块渲染前把 frame_index 设为"待机位置 + 提前量"；每个开口点前先对齐
patch(SR, """            self._interrupt.clear()
            self._speaking = True
""", """            self._interrupt.clear()
            self._speaking = True
            _ss = getattr(getattr(self.flashtalk, "avatar_state", None), "session_state", None)   # [talk patch 1b]
            self._talk_idle_wait_pos = getattr(_ss, "frame_index", None) if self.model_type == "quicktalk" else None
            self._talk_lead_set = False
            self._talk_speak_start_idx = None
            self._talk_media_started = False
            self._talk_lead_frames = getattr(type(self), "_talk_lead_shared", 20)   # 首块渲染+预缓冲的典型帧数，按实测 EMA（进程内共用）
            self._talk_mouth_fade = None   # [talk patch 7] 又开口了：上一句的嘴部渐隐不再叠
            self._talk_mouth_prev = None
            self._talk_seg_text = None     # [talk patch 16] 上一轮最后一句和这一轮第一句相同（如都是"好的。"）时也要查情绪
            self._talk_snap = None
            if _ss is not None:            # [talk patch 16] 开口前被打断时整套恢复段状态（不只 frame_index）；新一轮第一次切换不沿用上一轮的保持/排队
                self._talk_snap = tuple(getattr(_ss, k, None) for k in ("talk_range", "talk_after", "talk_hold", "talk_return"))
                _ss.talk_fresh = True
""", count=2)
patch(SR, """        async with self._generate_lock:
            if source == "idle" and (self._speaking or self._closed):
                return []
            try:
                frames = await self.flashtalk.generate(pcm_chunk)""", """        async with self._generate_lock:
            if source == "idle" and (self._speaking or self._closed):
                return []
            _ss = getattr(getattr(self.flashtalk, "avatar_state", None), "session_state", None)
            _text = getattr(self, "_talk_cur_text", "") if source != "idle" else ""
            _seg = None
            if self._speaking and _ss is not None and source != "idle":
                _ss.talk_speaking = True                               # [talk patch 11] 正在说话（段演完接讲话段）
            if self._speaking and _ss is not None and _text and _text != getattr(self, "_talk_seg_text", None):
                self._talk_seg_text = _text
                _seg = await self._talk_emotion_seg(_text)            # [talk patch 6] 情绪底板
            if (self._speaking and not self._speech_media_active and source != "idle"
                    and getattr(self, "_talk_idle_wait_pos", None) is not None and not getattr(self, "_talk_lead_set", True)):
                if _ss is not None:                                    # [talk patch 1b]
                    self._talk_gen_start_pos = self._talk_idle_wait_pos
                    _ss.frame_index = self._talk_idle_wait_pos + self._talk_lead_frames
                    self._talk_speak_start_idx = _ss.frame_index
                    _ss.talk_pending = None
                    _r, _af = getattr(_ss, "talk_range", None), getattr(_ss, "talk_after", None)
                    if (_r and _af is not None and getattr(_ss, "talk_return", None) is None
                            and _ss.frame_index >= _r[1] - _r[0]):
                        # [talk patch 15] 开口位置已经过了当前段的结尾：和待机画面一样，算到它之后的待机段里
                        _ss.frame_index -= _r[1] - _r[0]
                        _ss.talk_range, _ss.talk_after, _ss.talk_hold = tuple(_af), None, None
                    if _seg:
                        # 渲染从待机位置接着往下；按过渡表在最像的帧处转进这句情绪的段
                        from opentalking.models.quicktalk.runtime import _template_loop_index
                        _n = len(getattr(getattr(self.flashtalk.avatar_state, "worker", None), "restore_contexts", None) or [1])
                        _last = _template_loop_index(max(_ss.frame_index - 1, 0), _n, getattr(_ss, "talk_range", None))
                        self._talk_plan_switch(_ss, _seg, _last)
                self._talk_lead_set = True
            elif self._speaking and _ss is not None and _seg:
                # 说话中换了情绪：从刚渲染到的帧按过渡表接过去
                self._talk_plan_switch(_ss, _seg, getattr(_ss, "talk_last_idx", 0))
            try:
                frames = await self.flashtalk.generate(pcm_chunk)
                _h = getattr(_ss, "talk_idx_hist", None) if _ss is not None else None   # [talk patch 7]
                if _h is not None and frames and len(_h) >= len(frames):
                    for _f, _i in zip(frames, list(_h)[-len(frames):]):
                        try:
                            _f.talk_base_idx = _i
                        except Exception:
                            break""")
patch(SR, """                        # Opener chunk: start pacing immediately.
                        _start_pacing()""", """                        # Opener chunk: start pacing immediately.
                        await self._talk_idle_catchup()   # [talk patch 1b]
                        _start_pacing()""")
patch(SR, """                    if generated <= n_opener_chunks:
                        _start_pacing()""", """                    if generated <= n_opener_chunks:
                        await self._talk_idle_catchup()   # [talk patch 1b]
                        _start_pacing()""")
patch(SR, """                    _start_pacing()
                    for pc, bf, st in pending:""", """                    await self._talk_idle_catchup()   # [talk patch 1b]
                    _start_pacing()
                    for pc, bf, st in pending:""", count=2)
patch(SR, """                    _start_pacing()
                    for pc, buffered_frames, st in pending:""", """                    await self._talk_idle_catchup()   # [talk patch 1b]
                    _start_pacing()
                    for pc, buffered_frames, st in pending:""", count=2)

# 3. 口型时准时不准：音频 RTP 时间戳只按"已发样本数"走，待机不发音频时它停住，视频却一直在走，
#    两路时间戳和真实时间的对应关系越差越远。浏览器（Chrome）算出的音画差值动辄几十上百秒，
#    超过 10 秒就放弃同步，音视频各播各的，音频缓冲一被网络抖动拉长，嘴就比声音快。
#    → 视频每帧时间戳取真实发送时刻；音频不能跳时间戳（Opus 前的重采样要求 pts 连续，跳了整条音频发送会停），
#      改为没语音时按真实时间补静音、一直发。两路时间戳都跟真实时间走，浏览器就能按真实时间对齐。
RTC = "opentalking/providers/rtc/aiortc/adapter.py"
patch(RTC, """@dataclass
class _SharedWallClock:""", """_RTP_EPOCH = time.monotonic()   # [talk patch] 音视频 RTP 时间戳共用的时钟原点


@dataclass
class _SharedWallClock:""")
patch(RTC, """        vf = VideoFrame.from_ndarray(item.data, format="bgr24")
        vf.pts = self._next_pts_ms
        vf.time_base = fractions.Fraction(1, 1000)
        if self._debug_frames:""", """        vf = VideoFrame.from_ndarray(item.data, format="bgr24")
        # [talk patch] 时间戳 = 真实发送时刻（保证递增），不再按帧间隔累加
        self._next_pts_ms = max(self._next_pts_ms, int((time.monotonic() - _RTP_EPOCH) * 1000))
        vf.pts = self._next_pts_ms
        vf.time_base = fractions.Fraction(1, 1000)
        if self._debug_frames:""")
patch(RTC, """    async def _fill_buffer(self) -> None:
        while self._buffer.size < self._frame_samples and not self._eof:
            samples = await self._queue.get()
            if samples is None:
                self._eof = True
                break""", """    async def _fill_buffer(self) -> None:
        # [talk patch] 没有语音时按真实时间补静音：音频时间戳不能跳（Opus 编码前的重采样要求 pts 连续），
        # 只能一直发，让它和视频一样跟着真实时间走
        while self._buffer.size < self._frame_samples and not self._eof:
            if self._wall_t0 is None:
                self._wall_t0, self._wall_pts0 = time.monotonic(), self._next_pts
            due = self._wall_t0 + (self._next_pts - self._wall_pts0) / self.sample_rate
            try:   # 先直接取（wait_for 超时为 0 时即使队列有数据也会超时）
                samples = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                samples = ...
            try:
                if samples is ...:
                    samples = await asyncio.wait_for(self._queue.get(), max(0.001, due - time.monotonic()))
            except asyncio.TimeoutError:
                pad = self._frame_samples - self._buffer.size
                self._buffer = np.concatenate((self._buffer, np.zeros(pad, dtype=np.int16)))
                break
            if samples is None:
                self._eof = True
                break""")
patch(RTC, """        pts = self._next_pts
        if self._start_time is None:
            shared_start = self._shared_clock.start_time if self._shared_clock is not None else None
            if shared_start is None:
                shared_start = time.monotonic()
                if self._shared_clock is not None:
                    self._shared_clock.start_time = shared_start
            self._start_time = shared_start
            self._clock_start_pts = pts
            self._seen_audio = True

        assert self._start_time is not None
        target = self._start_time + ((pts - self._clock_start_pts) / self.sample_rate)
        now = time.monotonic()
        if now < target:
            await asyncio.sleep(target - now)
""", """        pts = self._next_pts
        # [talk patch] 按真实时间连续发送（含静音），不再随开口重置节拍；视频开口时按自己的时间线起播，
        # 两路首个语音样本 / 首帧都在入队后立即发出，起点一致
        if self._wall_t0 is None:
            self._wall_t0, self._wall_pts0 = time.monotonic(), pts
        target = self._wall_t0 + ((pts - self._wall_pts0) / self.sample_rate)
        now = time.monotonic()
        if now < target:
            await asyncio.sleep(target - now)
        elif now - target > 0.2:     # 事件循环卡顿后不要狂发追赶，重新锚定（pts 仍连续）
            self._wall_t0, self._wall_pts0 = now, pts
""")
patch(RTC, """        self._eof = False
        self._shared_clock = shared_clock

    async def put_pcm""", """        self._eof = False
        self._shared_clock = shared_clock
        self._wall_t0: float | None = None   # [talk patch] 连续发送的时间锚点
        self._wall_pts0 = 0

    async def put_pcm""")
patch(RTC, """        self._prev_source_ts_ms = frame_ts_ms
        self._next_pts_ms += max(1, delta_ms)
        return vf""", """        self._prev_source_ts_ms = frame_ts_ms
        self._next_pts_ms += 1   # [talk patch] 下一帧取 max(这里, 发送时刻)：按帧间隔累加会在待机掉帧时跑到真实时间前面
        return vf""")

# 4. 待机画面预编码：每个在线会话待机时都在实时编码同一段底板循环（实测待机 CPU 72% 花在编码上，
#    每路约 0.7 核，还拖慢同进程的口型渲染：5 路待机 + 3 路说话，渲染从 0.65s 涨到 0.92s/块、18 次超实时）。
#    → 每个形象的底板循环在后台线程里只编一次 VP8（每 25 帧一个关键帧），进程内所有会话共用；
#      待机按顺序取帧时视频轨直接发预编码数据（aiortc 对 Packet 走 encoder.pack，图片编号照常递增），
#      从实时切到预编码只在关键帧处切；切回实时（开口）时让实时编码器先出关键帧。
#      只在协商到 VP8 时启用；OPENTALKING_IDLE_PREENCODE=0 关闭。
patch(SR, """        frame = VideoFrameData(
            data=idle_frame,
            width=idle_frame.shape[1],
            height=idle_frame.shape[0],
            timestamp_ms=0.0,
        )
        await self._video_put_safe(frame)
        if program:""", """        frame = VideoFrameData(
            data=idle_frame,
            width=idle_frame.shape[1],
            height=idle_frame.shape[0],
            timestamp_ms=0.0,
        )
        ref = getattr(self, "_shared_idle_ref", None)   # [talk patch] 按顺序的底板帧 → 视频轨可发预编码
        if ref is not None and ref[0][ref[1]].frame is idle_frame:
            frame.talk_idle = ref
        await self._video_put_safe(frame)
        if program:""")
patch(RTC, """_RTP_EPOCH = time.monotonic()   # [talk patch] 音视频 RTP 时间戳共用的时钟原点
""", '''_RTP_EPOCH = time.monotonic()   # [talk patch] 音视频 RTP 时间戳共用的时钟原点

# [talk patch] 待机画面预编码（补丁 4）
import collections as _talk_collections
import threading as _talk_threading
_PREENC_ON = os.environ.get("OPENTALKING_IDLE_PREENCODE", "1") != "0"
_PREENC_BITRATE = int(os.environ.get("OPENTALKING_IDLE_PREENCODE_BITRATE", "1200000"))
_PREENC_GOP = 25
_PREENC: "_talk_collections.OrderedDict[int, _PreEncodedIdle]" = _talk_collections.OrderedDict()
_PREENC_LOCK = _talk_threading.Lock()


class _PreEncodedIdle:
    """一个形象的底板循环编好的 VP8 帧（不含 RTP 封装）；后台线程编码，编好前 data 为 None。"""

    def __init__(self, ctxs) -> None:
        import weakref
        self.ctxs = ctxs
        self.ref0 = weakref.ref(ctxs[0])        # [talk patch 16] 认形象用弱引用；编完就放掉 ctxs，不攥着整套帧数据
        self.n = len(ctxs)
        self.data: list[bytes] | None = None
        self.key: list[bool] | None = None
        _talk_threading.Thread(target=self._build_release, daemon=True, name="talk-preenc").start()

    def _build_release(self) -> None:
        try:
            self._build()
        finally:
            self.ctxs = None

    def _build(self) -> None:
        import logging
        import av
        lg = logging.getLogger("talk.preenc")
        t0 = time.monotonic()
        try:
            h, w = self.ctxs[0].frame.shape[:2]
            codec = av.CodecContext.create("libvpx", "w")
            codec.width, codec.height, codec.pix_fmt = w, h, "yuv420p"
            codec.bit_rate = _PREENC_BITRATE
            codec.time_base = fractions.Fraction(1, 25)
            codec.gop_size = _PREENC_GOP
            br = str(_PREENC_BITRATE)
            # 离线编，可以比实时编码（cpu-used -6）用更慢更清楚的档位；lag 0 保证一帧进一帧出
            codec.options = {"deadline": "good", "cpu-used": "2", "lag-in-frames": "0", "bufsize": br,
                             "minrate": br, "maxrate": br, "static-thresh": "1", "partitions": "0"}
            data, key = [], []
            for i, c in enumerate(self.ctxs):
                vf = VideoFrame.from_ndarray(c.frame, format="bgr24").reformat(format="yuv420p")
                vf.pts = i
                if i % _PREENC_GOP == 0:
                    vf.pict_type = av.video.frame.PictureType.I
                pkts = codec.encode(vf)
                if len(pkts) != 1:
                    raise RuntimeError(f"frame {i}: encoder returned {len(pkts)} packets")
                data.append(bytes(pkts[0]))
                key.append(bool(pkts[0].is_keyframe))
            if not key[0]:
                raise RuntimeError("first frame is not a keyframe")
            self.data, self.key = data, key
            lg.info("idle preencoded: %d frames %dx%d, %d keyframes, %.1f KB/frame, %.1fs",
                    self.n, w, h, sum(key), sum(map(len, data)) / self.n / 1024, time.monotonic() - t0)
        except Exception:
            lg.exception("idle preencode failed; idle frames stay live-encoded")


def _preenc_for(ctxs) -> "_PreEncodedIdle | None":
    if not _PREENC_ON:
        return None
    with _PREENC_LOCK:
        p = _PREENC.get(id(ctxs))
        if p is None or p.ref0() is not ctxs[0]:
            p = _PreEncodedIdle(ctxs)
            _PREENC[id(ctxs)] = p
            while len(_PREENC) > 8:            # 最多留 8 个形象（和形象缓存数量相当）
                _PREENC.popitem(last=False)
        else:
            _PREENC.move_to_end(id(ctxs))
        return p


def _talk_idle_packet(track, item):
    """能发预编码就返回 av.Packet；否则 None（走实时编码）。从预编码切回实时时让编码器出关键帧。"""
    import av
    pkt = None
    ref = getattr(item, "talk_idle", None)
    sender = getattr(track, "_talk_sender", None)
    enc = getattr(sender, "_RTCRtpSender__encoder", None) if sender is not None else None
    if ref is not None and enc is not None and type(enc).__name__ == "Vp8Encoder":
        ctxs, idx = ref
        pre = _preenc_for(ctxs)
        if pre is not None and pre.data is not None and idx < pre.n:
            # 接着上一帧往下发，或者正好落在关键帧上（解码器可以从这里开始）
            if getattr(track, "_talk_pre_next", None) == idx or pre.key[idx]:
                track._talk_pre_next = (idx + 1) % pre.n
                pkt = av.Packet(pre.data[idx])
                pkt.pts = track._next_pts_ms
                pkt.time_base = fractions.Fraction(1, 1000)
    if pkt is None and getattr(track, "_talk_pre_next", None) is not None:
        track._talk_pre_next = None        # 切回实时编码：解码器参考的是预编码帧，必须先来关键帧
        if sender is not None:
            sender._RTCRtpSender__force_keyframe = True
    return pkt
''')
# 4b. 待机时的静音音频也不再逐帧编码：每路待机每秒 50 帧静音都在重采样 + Opus 编码，
#     视频预编码后它是待机剩下的大头（约 40%），满载时和渲染抢主线程，4 路说话 + 4 路待机渲染仍从 0.71s 涨到 1.05s/块。
#     → 连续 10 帧（200ms）静音后，Opus 编码器直接返回一个预先编好的静音包，时间戳按 960 样本/帧接着往下走；
#       静音帧仍喂给重采样器（换新重采样器实测时间戳会倒退 6 万多样本），只省 Opus 编码那 2/3。
patch(RTC, """
def _talk_idle_packet(track, item):""", '''
def _talk_patch_opus() -> None:
    from aiortc.codecs.opus import OpusEncoder
    orig = OpusEncoder.encode
    silence: list[bytes] = []

    def silence_packet() -> bytes | None:
        if not silence:
            enc = OpusEncoder()
            zero = np.zeros(320, dtype=np.int16).tobytes()
            last = None
            for i in range(25):
                f = AudioFrame(format="s16", layout="mono", samples=320)
                f.planes[0].update(zero)
                f.sample_rate, f.pts, f.time_base = 16000, i * 320, fractions.Fraction(1, 16000)
                payloads, _ = orig(enc, f)
                last = payloads[-1] if payloads else last
            if last:
                silence.append(last)
        return silence[0] if silence else None

    def encode(self, frame, force_keyframe=False):
        # RTP 时间戳自己数：每帧按样本数往前走。跳过编码后 Opus 编码器自己给的时间戳会倒退 / 跳跃（实测），不能用
        cur = getattr(self, "_talk_ts", 0)
        self._talk_ts = cur + frame.samples * 48000 // frame.sample_rate
        mute = not np.frombuffer(bytes(frame.planes[0]), dtype=np.int16).any()
        self._talk_mute = getattr(self, "_talk_mute", 0) + 1 if mute else 0
        if self._talk_mute > 10:
            data = silence_packet()
            if data:
                for _ in self.resampler.resample(frame):   # 重采样器的时间线要连续，照样喂；只省 Opus 编码
                    pass
                return [data], cur
        payloads, _ = orig(self, frame, force_keyframe)
        return (payloads, cur) if payloads else ([], None)

    OpusEncoder.encode = encode


if _PREENC_ON:
    _talk_patch_opus()


def _talk_idle_packet(track, item):''')
patch(RTC, """        vf = VideoFrame.from_ndarray(item.data, format="bgr24")
        # [talk patch] 时间戳 = 真实发送时刻（保证递增），不再按帧间隔累加
        self._next_pts_ms = max(self._next_pts_ms, int((time.monotonic() - _RTP_EPOCH) * 1000))
        vf.pts = self._next_pts_ms""", """        # [talk patch] 时间戳 = 真实发送时刻（保证递增），不再按帧间隔累加
        self._next_pts_ms = max(self._next_pts_ms, int((time.monotonic() - _RTP_EPOCH) * 1000))
        pkt = _talk_idle_packet(self, item)   # [talk patch] 待机帧直接发预编码数据，省掉转换和编码
        if pkt is not None:
            self._prev_source_ts_ms = frame_ts_ms
            self._next_pts_ms += 1
            return pkt
        vf = VideoFrame.from_ndarray(item.data, format="bgr24")
        vf.pts = self._next_pts_ms""")
patch(RTC, """        self.pc.addTrack(self.video)
        self.pc.addTrack(self.audio)""", """        self.video._talk_sender = self.pc.addTrack(self.video)   # [talk patch] 预编码切回实时要强制关键帧
        self.pc.addTrack(self.audio)""")

# 5. FlashHead 客户端 init_session 不认运行器统一传的 wav2lip_postprocess_mode 等关键字（TypeError，会话 503）→ 多余关键字吞掉
patch("opentalking/providers/synthesis/flashhead/ws_client.py", """        prompt: str = "A person is talking. Only the foreground characters are moving, the background remains static.",
        seed: int = 9999,
    ) -> dict[str, Any]:""", """        prompt: str = "A person is talking. Only the foreground characters are moving, the background remains static.",
        seed: int = 9999,
        **_ignored: Any,
    ) -> dict[str, Any]:""")


# 6. 情绪底板（2026-10-22，先在奶奶形象上试）：一个形象的底板 = 平静/温柔/开心/笑/心疼/惊讶 等几段（每段首尾都是原照片姿势）拼成一条，
#    形象目录 emotion_segments.json 记各段帧区间；待机在平静+温柔段循环，说话时按这句的情绪标签（网关 /internal/emotion）切段，
#    切换、说完回待机都按过渡表（scripts/emotion_transitions.py）在最像的两帧处硬接。没有分段表的形象行为不变。
patch(SR, """                    pcm_chunk, sub_tag = item
""", """                    pcm_chunk, sub_tag = item
                    if sub_tag:
                        self._talk_cur_text = sub_tag   # [talk patch 6] 这块音频属于哪句话（情绪底板按句选段）
""", count=2)
patch(SR, """        # Cache last frame for idle loop
        if frames:
            self._last_frame = frames[-1].data""", """        # Cache last frame for idle loop
        if frames:
            self._last_frame = frames[-1].data
            self._talk_last_out = frames[-1].data   # [talk patch 6]
            self._talk_last_out_idx = getattr(getattr(getattr(self.flashtalk, "avatar_state", None), "session_state", None),
                                              "talk_last_idx", None)   # [talk patch 7] 这一帧用的底板帧号""")

# 7. 说完时嘴部从"生成的嘴"平滑过渡回底板原视频的嘴（见 _talk_start_mouth_fade）；句尾/停顿时嘴慢慢合上（_talk_smooth_mouth）
patch(SR, """        arr = np.asarray(pcm_chunk, dtype=np.int16)
        total_samples = len(arr)""", """        self._talk_smooth_mouth(pcm_chunk, frames)   # [talk patch 7]
        arr = np.asarray(pcm_chunk, dtype=np.int16)
        total_samples = len(arr)""")
_p7 = root / SR
_p7.write_text(_p7.read_text(encoding="utf-8") + "\n\n_TALK_STILL_DIFF = 2.5   # [talk patch 12] mouth-area base diff below this counts as still\n_TALK_MOUTH_FADE = 10   # [talk patch 7] 说完后嘴部渐隐帧数（0.4 秒）\n_TALK_QUIET_RMS = 500   # [talk patch 7] 低于这个音量（16bit RMS）算安静帧\n_TALK_SHARPEN = 0.8   # [talk patch 9] 嘴部锐化强度\n_TALK_GRAIN_SCALE = 0.35   # [talk patch 9] 嘴部补颗粒的标准差 = 底板 noise 强度 × 这个系数（底板颗粒经缩放、编码后会变弱）\n", encoding="utf-8")

# 10. 说话时嘴太"忙"：口型模型逐帧按语音特征把每个音节都张合到位，弱读音节也张满，看着像机器。
#     在语音特征（HuBERT）上收敛：向"静音特征"按系数拉回（幅度变小，嘴形仍是模型生成的，不会像像素混合那样出重影），
#     再沿时间做轻度平滑（协同发音，快速开合被抹掉）。参数可在 /w/cache/talk_mouth.json 热调：{"gain":0.8,"smooth":0.5}
patch(RT, """        repst = self.v2.extract_representations_pcm(pcm, sample_rate)""",
      """        repst = self.v2.extract_representations_pcm(pcm, sample_rate)
        repst = _talk_damp_reps(self, repst)   # [talk patch 10]""")
p = root / RT
p.write_text(p.read_text(encoding="utf-8") + '''

_TALK_MOUTH_CFG = {"t": None, "gain": 0.8, "smooth": 0.5}


def _talk_mouth_cfg():
    """[talk patch 10] 读 /w/cache/talk_mouth.json（按修改时间缓存），没有就用默认值。"""
    import json as _json
    import os as _os
    path = _os.environ.get("TALK_MOUTH_CFG", "/w/cache/talk_mouth.json")
    try:
        t = _os.path.getmtime(path)
    except OSError:
        return _TALK_MOUTH_CFG
    if t != _TALK_MOUTH_CFG["t"]:
        try:
            d = _json.loads(open(path, encoding="utf-8").read())
            _TALK_MOUTH_CFG.update(gain=float(d.get("gain", 0.8)), smooth=float(d.get("smooth", 0.5)), t=t)
        except Exception:  # noqa: BLE001
            _TALK_MOUTH_CFG["t"] = t
    return _TALK_MOUTH_CFG


def _talk_damp_reps(worker, repst):
    """[talk patch 10] repst: (1, C, T) HuBERT 特征（50 帧/秒）。向静音特征收敛 + 时间平滑。"""
    cfg = _talk_mouth_cfg()
    gain, sm = cfg["gain"], cfg["smooth"]
    if gain >= 0.999 and sm <= 0.001:
        return repst
    sil = getattr(worker, "_talk_sil", None)
    if sil is None:
        z = worker.v2.extract_representations_pcm(np.zeros(16000, dtype=np.int16), 16000)
        sil = worker._talk_sil = z.mean(axis=2, keepdims=True).astype(repst.dtype)
    out = sil + (repst - sil) * gain
    if sm > 0.001 and out.shape[2] >= 3:
        pad = np.concatenate([out[:, :, :1], out, out[:, :, -1:]], axis=2)
        out = pad[:, :, 1:-1] * (1 - sm) + (pad[:, :, :-2] + pad[:, :, 2:]) * (sm / 2)
    return out.astype(repst.dtype)
''', encoding="utf-8")

# 14. 一张卡多客户多形象（每个客户一个自己的亲人形象）：原来每个形象的每一帧都在显卡上存 3 份 3 通道的
#     区域图/遮罩（奶奶 2640 帧占 5~6G 显存），一张卡只放得下两三个形象，worker 缓存也只留 1 个，
#     换形象就互相挤。→ 遮罩只留 1 通道、放内存，区域图渲染时从整帧现切，每帧渲染时才搬上显卡（28 帧一块，几毫秒）；
#     整帧、模型输入（转 fp16）、人脸裁图写到磁盘做内存映射，不在通话的形象不占内存，在用的由系统页缓存兜着。
patch(RT, """        for frame, (face, coords, affine) in zip(self.frames, self.face_det_results):
            contexts.append(
                self._build_fast_restore_context(
                    frame=frame,
                    face=face,
                    coords=coords,
                    affine=affine,
                )
            )""", """        for frame, (face, coords, affine) in zip(self.frames, self.face_det_results):
            contexts.append(_talk_compact_ctx(   # [talk patch 14] 建一帧就把显存里的东西挪走，加载时显存也不冲高
                self._build_fast_restore_context(
                    frame=frame,
                    face=face,
                    coords=coords,
                    affine=affine,
                )
            ))""")
patch(RT, """        self.restore_contexts = self._build_fast_restore_contexts()
        self.frame_index = 0""", """        self.restore_contexts = self._build_fast_restore_contexts()
        _talk_mmap_worker(self)   # [talk patch 14]
        self.frame_index = 0""")
patch(RT, """        pasted_face = context.hard_mask_roi_3d * inv_face
        output = context.frame.copy() if base_frame is None else base_frame.copy()
        base_roi_t = context.frame_roi_t
        if base_frame is not None:
            base_roi_t = (
                torch.from_numpy(base_frame[y0:y1, x0:x1])
                .to(device=context.soft_mask_roi_3d.device, dtype=context.soft_mask_roi_3d.dtype)
                .permute(2, 0, 1)
                .contiguous()
            )
        soft_mask_roi_3d = context.soft_mask_roi_3d""", """        _dev, _dt = inv_face.device, inv_face.dtype   # [talk patch 14] 遮罩/区域图可能在内存里：用到时才搬上显卡
        pasted_face = context.hard_mask_roi_3d.to(device=_dev, dtype=_dt, non_blocking=True) * inv_face
        output = np.array(context.frame) if base_frame is None else base_frame.copy()
        base_roi_t = context.frame_roi_t
        if base_roi_t is None and base_frame is None:
            base_roi_t = torch.from_numpy(np.ascontiguousarray(context.frame[y0:y1, x0:x1])).to(
                device=_dev, dtype=_dt).permute(2, 0, 1).contiguous()
        if base_frame is not None:
            base_roi_t = (
                torch.from_numpy(np.ascontiguousarray(base_frame[y0:y1, x0:x1]))
                .to(device=_dev, dtype=_dt)
                .permute(2, 0, 1)
                .contiguous()
            )
        soft_mask_roi_3d = context.soft_mask_roi_3d.to(device=_dev, dtype=_dt, non_blocking=True)""")
patch(RT, """                g, hn_out, cn_out = self.v2.run_model(
                    rep_input.astype(np.float32),
                    context.face_input,""", """                g, hn_out, cn_out = self.v2.run_model(
                    rep_input.astype(np.float32),
                    np.asarray(context.face_input, dtype=np.float32),   # [talk patch 14] 存的是 fp16 内存映射""")
p = root / RT
p.write_text(p.read_text(encoding="utf-8") + '''

def _talk_compact_ctx(ctx):
    """[talk patch 14] 一帧的上下文：遮罩只留 1 通道并挪到内存，区域图不存（渲染时从整帧切）。"""
    import dataclasses
    return dataclasses.replace(
        ctx,
        frame_roi_t=None,
        hard_mask_roi_3d=ctx.hard_mask_roi_3d[:1].detach().to("cpu"),
        soft_mask_roi_3d=ctx.soft_mask_roi_3d[:1].detach().to("cpu"),
    )


def _talk_mmap_worker(worker) -> None:
    """[talk patch 14] 整帧、模型输入（fp16）、人脸裁图写进 /w/cache/qtmm/<key>/ 做只读内存映射，替换掉内存里的数组。
    同一个形象再加载时直接复用文件。写失败就保持原样（只是多占内存）。"""
    import dataclasses
    import hashlib
    import logging
    import os
    lg = logging.getLogger("talk.qtmm")
    ctxs = worker.restore_contexts
    try:
        n = len(ctxs)
        fshape = tuple(ctxs[0].frame.shape)
        ishape = tuple(np.asarray(ctxs[0].face_input).shape)
        faces = [r[0] for r in worker.face_det_results]
        cshape = tuple(np.asarray(faces[0]).shape)
        same_face = all(tuple(np.asarray(f).shape) == cshape for f in faces)
        tv = str(worker.template_video)
        key = hashlib.sha1(f"{tv}|{os.path.getmtime(tv)}|{n}|{fshape}|{ishape}|{cshape}".encode()).hexdigest()[:20]
        d = Path(os.environ.get("TALK_QT_MMAP_DIR", "/w/cache/qtmm")) / key
        ok = d / "ok"
        if not ok.exists():
            # [talk patch 16] 3 个渲染进程会同时预热同一个形象：各写各的临时目录，写完原子改名；别人先建好就用别人的
            import shutil
            final = d
            d = final.with_name(f"{key}.tmp{os.getpid()}")
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
            fr = np.memmap(d / "frames.u8", np.uint8, "w+", shape=(n,) + fshape)
            fi = np.memmap(d / "face_input.f16", np.float16, "w+", shape=(n,) + ishape)
            for i, c in enumerate(ctxs):
                fr[i] = c.frame
                fi[i] = np.asarray(c.face_input, dtype=np.float16)
            fr.flush()
            fi.flush()
            del fr, fi
            if same_face:
                fc = np.memmap(d / "faces.u8", np.uint8, "w+", shape=(n,) + cshape)
                for i, f in enumerate(faces):
                    fc[i] = f
                fc.flush()
                del fc
            (d / "meta.txt").write_text(tv)
            (d / "ok").write_text("1")
            try:
                os.rename(d, final)
            except OSError:
                shutil.rmtree(d, ignore_errors=True)     # 别的进程先建好了
            d = final
            # 同一个模板视频的旧目录（形象重建前的）删掉；正在用它的进程映射不受影响（Linux 删文件不影响已打开的）
            for old in d.parent.iterdir():
                try:
                    if old != d and old.is_dir() and not old.name.count(".tmp") and (old / "meta.txt").read_text() == tv:
                        shutil.rmtree(old, ignore_errors=True)
                        lg.info("removed stale mmap dir %s", old)
                except OSError:
                    pass
        fr = np.memmap(d / "frames.u8", np.uint8, "r", shape=(n,) + fshape)
        fi = np.memmap(d / "face_input.f16", np.float16, "r", shape=(n,) + ishape)
        fc = np.memmap(d / "faces.u8", np.uint8, "r", shape=(n,) + cshape) if same_face and (d / "faces.u8").exists() else None
        worker.restore_contexts = [
            dataclasses.replace(c, frame=fr[i], face_input=fi[i], face=(fc[i] if fc is not None else c.face))
            for i, c in enumerate(ctxs)]
        worker.frames = [fr[i] for i in range(n)]
        if fc is not None:
            worker.face_det_results = [(fc[i],) + tuple(r[1:]) for i, r in enumerate(worker.face_det_results)]
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        lg.info("quicktalk contexts memory-mapped: %d frames %s -> %s", n, fshape, d)
    except Exception:  # noqa: BLE001
        lg.exception("memory-map contexts failed; keeping in-memory arrays")
''', encoding="utf-8")

# 14b. 待机预编码存盘：同一个形象（底板帧内容相同）只编一次，之后读文件（原来每次 worker 重建都要 1~1.5 分钟满 CPU）
patch(RTC, """        t0 = time.monotonic()
        try:
            h, w = self.ctxs[0].frame.shape[:2]""", """        t0 = time.monotonic()
        cache = _talk_preenc_cache_path(self.ctxs)
        if cache is not None and cache.exists():
            try:
                import pickle
                self.data, self.key = pickle.loads(cache.read_bytes())
                lg.info("idle preencode loaded from %s (%d frames)", cache, self.n)
                return
            except Exception:  # noqa: BLE001
                lg.warning("idle preencode cache unreadable, rebuilding: %s", cache)
        try:
            h, w = self.ctxs[0].frame.shape[:2]""")
patch(RTC, """            self.data, self.key = data, key
            lg.info("idle preencoded""", """            self.data, self.key = data, key
            if cache is not None:
                try:
                    import pickle
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cache.with_suffix(".tmp")
                    tmp.write_bytes(pickle.dumps((data, key)))
                    tmp.replace(cache)
                except Exception:  # noqa: BLE001
                    lg.warning("idle preencode cache write failed: %s", cache)
            lg.info("idle preencoded""")
patch(RTC, """def _preenc_for(ctxs) -> "_PreEncodedIdle | None":""", '''def _talk_preenc_cache_path(ctxs):
    """[talk patch 14b] 预编码缓存文件：按帧数、尺寸、码率和几张帧内容的摘要定位（形象内容变了自然换文件）。"""
    try:
        import hashlib
        from pathlib import Path as _P
        n = len(ctxs)
        h = hashlib.sha1(f"{n}|{ctxs[0].frame.shape}|{_PREENC_BITRATE}|{_PREENC_GOP}".encode())
        for i in (0, n // 3, n // 2, (2 * n) // 3, n - 1):
            h.update(np.ascontiguousarray(ctxs[i].frame[::7, ::7]).tobytes())
        return _P(os.environ.get("TALK_PREENC_DIR", "/w/cache/preenc")) / (h.hexdigest()[:24] + ".pkl")
    except Exception:  # noqa: BLE001
        return None


def _preenc_for(ctxs) -> "_PreEncodedIdle | None":''')
