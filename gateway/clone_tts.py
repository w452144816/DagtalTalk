"""本地克隆音色：Qwen3-TTS 0.6B Base + faster-qwen3-tts（CUDA graph 加速，3090 首包 ~0.18s，约 2.7 倍实时）。

MiniMax 的 key 是 token 套餐，不支持 voice_clone，所以克隆走本地。
模型只在给 OpenTalking 合成语音的那个网关进程（127.0.0.1:8301，GATEWAY_TTS=1）里加载；
对外进程（8443）的试听也是转调 8301，GPU 上只放一份。
官方 qwen-tts 实现在 3090 上比实时还慢（3 秒语音要 4 秒），所以用 faster-qwen3-tts 0.3.x
（0.4 起要 transformers 5，和锁定的 torch 2.5.1 冲突）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import AsyncIterator

import numpy as np

log = logging.getLogger("clone_tts")
MODEL_DIR = os.getenv("CLONE_TTS_MODEL_DIR", "/w/models/local-audio/Qwen__Qwen3-TTS-12Hz-0.6B-Base")
DEVICE = os.getenv("CLONE_TTS_DEVICE", "cuda:0")
CHUNK_STEPS = 4          # 每块 4 步 ≈ 0.33 秒语音
_model = None
_load_lock = threading.Lock()
_gen_lock = threading.Lock()   # CUDA graph 不能并发


def available() -> bool:
    return Path(MODEL_DIR, "model.safetensors").exists()


def _load():
    global _model
    with _load_lock:
        if _model is None:
            import torch
            from faster_qwen3_tts import FasterQwen3TTS
            t = time.perf_counter()
            m = FasterQwen3TTS.from_pretrained(MODEL_DIR, device=DEVICE, dtype=torch.bfloat16)
            _model = m
            log.info("qwen3-tts loaded in %.1fs", time.perf_counter() - t)
    return _model


def preload(ref_wav: str | None = None, ref_text: str | None = None) -> None:
    """加载并跑一句，把 CUDA graph 捕获掉（首次 ~4s），后台线程里做。"""
    def run() -> None:
        try:
            m = _load()
            if ref_wav and ref_text:
                with _gen_lock:
                    for _ in m.generate_voice_clone_streaming(text="你好。", language="Chinese", ref_audio=ref_wav,
                                                              ref_text=ref_text, chunk_size=CHUNK_STEPS):
                        pass
                log.info("qwen3-tts warmed up")
        except Exception:  # noqa: BLE001
            log.exception("qwen3-tts preload failed")
    threading.Thread(target=run, daemon=True).start()


async def stream_pcm16k(text: str, ref_wav: str, ref_text: str) -> AsyncIterator[bytes]:
    """边生成边吐 16k s16le。客户端断开（被打断）时停止生成。"""
    import soxr
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    def work() -> None:
        try:
            m = _load()
            rs = None
            with _gen_lock:
                for chunk, sr, _ in m.generate_voice_clone_streaming(text=text, language="Chinese", ref_audio=ref_wav,
                                                                     ref_text=ref_text, chunk_size=CHUNK_STEPS):
                    if stop.is_set():
                        break
                    rs = rs or soxr.ResampleStream(sr, 16000, 1, dtype="float32")
                    y = rs.resample_chunk(np.asarray(chunk, dtype=np.float32).reshape(-1))
                    loop.call_soon_threadsafe(q.put_nowait, (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
                if rs is not None and not stop.is_set():
                    y = rs.resample_chunk(np.zeros(0, dtype=np.float32), last=True)
                    loop.call_soon_threadsafe(q.put_nowait, (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
        except Exception as e:  # noqa: BLE001
            loop.call_soon_threadsafe(q.put_nowait, e)
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    threading.Thread(target=work, daemon=True).start()
    try:
        while (item := await q.get()) is not None:
            if isinstance(item, Exception):
                raise item
            if item:
                yield item
    finally:
        stop.set()
