"""网关进程内的 SenseVoice 识别。

OpenTalking 自带的 speak_audio_stream 也能识别，但它和 WebRTC 编码、口型渲染挤在同一个进程里，
3 秒的话要 1~2 秒才出字；单独进程里 GPU 推理只要 ~70ms，所以识别挪到网关做，出字后直接调 /speak。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

import numpy as np

log = logging.getLogger("asr")
MODEL_DIR = os.getenv("OPENTALKING_STT_SENSEVOICE_MODEL_DIR", "/w/models/local-audio/iic__SenseVoiceSmall")
# 双卡时网关的识别和 OpenTalking（渲染卡）分开放
DEVICE = os.getenv("GATEWAY_ASR_DEVICE") or os.getenv("OPENTALKING_STT_SENSEVOICE_DEVICE", "cuda:0")
_TAG = re.compile(r"<\|[^|]*\|>")
_model = None
_lock = threading.Lock()


def _load():
    global _model
    with _lock:
        if _model is None:
            from funasr import AutoModel
            t = time.perf_counter()
            _model = AutoModel(model=MODEL_DIR, device=DEVICE, disable_update=True, disable_pbar=True, log_level="ERROR")
            _model.generate(input=np.zeros(8000, dtype=np.float32), language="auto", use_itn=True)   # 预热，首推 ~600ms
            log.info("sensevoice loaded on %s in %.1fs", DEVICE, time.perf_counter() - t)
    return _model


def preload() -> None:
    threading.Thread(target=_load, daemon=True).start()


def transcribe(pcm16: bytes) -> tuple[str, float]:
    """16k 单声道 s16le → (文本, 耗时 ms)。"""
    audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    if audio.size < 1600:        # 不到 0.1 秒
        return "", 0.0
    model = _load()
    t = time.perf_counter()
    with _lock:
        res = model.generate(input=audio, language="auto", use_itn=True)
    text = _TAG.sub("", (res[0].get("text") if res else "") or "").strip()
    if not any(ch.isalnum() for ch in text):     # 只有标点（咳嗽、噪声常识别成 "."）不算一句话
        text = ""
    return text,(time.perf_counter() - t) * 1000
