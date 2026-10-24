"""SoulX-FlashHead 推理服务：实现 OpenTalking flashhead 客户端用的 WebSocket 协议（改自上游 V100 配方里的样例）。
协议：文本帧 session.create（带 image_b64）/ session.close；二进制帧 "AUDI"+PCM s16le 16k 进，"VIDX"+N+(len+JPEG)*N 出。
环境变量：FLASHHEAD_SRC（模型代码目录）、FLASHHEAD_MODEL_DIR、FLASHHEAD_WAV2VEC_DIR、FLASHHEAD_MODEL_TYPE（lite/pro）、
FLASHHEAD_PORT（8766）、FLASHHEAD_DTYPE（bf16，4090 有 bf16；V100 才要 fp16）、FLASHHEAD_COMPILE（0：关 torch.compile，音频长度变化会触发重编译报错）
"""
import asyncio
import base64
import json
import os
import struct
import sys
import tempfile
import time

import cv2
import numpy as np
import torch
import websockets

SRC = os.environ.get("FLASHHEAD_SRC", "/app/SoulX-FlashHead")
sys.path.insert(0, SRC)
os.chdir(SRC)

MODEL_DIR = os.environ.get("FLASHHEAD_MODEL_DIR", "/w/models/SoulX-FlashHead-1_3B")
WAV2VEC_DIR = os.environ.get("FLASHHEAD_WAV2VEC_DIR", "/w/models/wav2vec2-base-960h")
MODEL_TYPE = os.environ.get("FLASHHEAD_MODEL_TYPE", "lite")
PORT = int(os.environ.get("FLASHHEAD_PORT", "8766"))
MAGIC_AUDIO, MAGIC_VIDEO = b"AUDI", b"VIDX"
FRAMES = 33                 # 每块 33 帧 @25fps ≈ 1.32s，对应 21120 个 16k 样本
CHUNK_SAMPLES = 21120

if os.environ.get("FLASHHEAD_COMPILE", "0") != "1":
    import flash_head.src.pipeline.flash_head_pipeline as _fp
    _fp.COMPILE_MODEL = False
    _fp.COMPILE_VAE = False
from flash_head.inference import get_audio_embedding, get_base_data, get_pipeline, run_pipeline  # noqa: E402

pipeline = None
lock = asyncio.Lock()       # 单卡一次只跑一路


def load_pipeline() -> None:
    global pipeline
    t0 = time.time()
    pipeline = get_pipeline(world_size=1, ckpt_dir=MODEL_DIR, model_type=MODEL_TYPE, wav2vec_dir=WAV2VEC_DIR)
    print(f"[flashhead] loaded {MODEL_TYPE} in {time.time() - t0:.0f}s, vram {torch.cuda.memory_allocated() / 1024 ** 3:.1f}G", flush=True)


def pcm_to_float(b: bytes) -> np.ndarray:
    a = np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768.0
    if len(a) < CHUNK_SAMPLES:
        a = np.pad(a, (0, CHUNK_SAMPLES - len(a)))
    return a[:CHUNK_SAMPLES]


def frames_to_msg(arr: np.ndarray) -> bytes:
    parts = []
    for i in range(arr.shape[0]):
        _, buf = cv2.imencode(".jpg", cv2.cvtColor(arr[i], cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])
        parts.append(struct.pack("<I", len(buf)) + buf.tobytes())
    return MAGIC_VIDEO + struct.pack("<I", arr.shape[0]) + b"".join(parts)


async def handle(ws):
    sid = f"{int(time.time() * 1000) % 100000000}"
    ref_path = None
    print(f"[{sid}] connected", flush=True)
    try:
        async for msg in ws:
            if isinstance(msg, str):
                data = json.loads(msg)
                if data.get("type") == "session.create":
                    img = base64.b64decode(data["inputs"]["image_b64"])
                    fd, ref_path = tempfile.mkstemp(suffix=".png")
                    with os.fdopen(fd, "wb") as f:
                        f.write(img)
                    async with lock:
                        t0 = time.time()
                        await asyncio.to_thread(get_base_data, pipeline, ref_path, 42, True)
                    await ws.send(json.dumps({"type": "session.created", "session_id": sid,
                                              "audio": {"sample_rate": 16000, "chunk_samples": CHUNK_SAMPLES},
                                              "video": {"fps": 25, "width": 512, "height": 512, "frame_count": FRAMES}}))
                    print(f"[{sid}] session created, base data {time.time() - t0:.1f}s", flush=True)
                elif data.get("type") == "session.close":
                    await ws.send(json.dumps({"type": "session.closed"}))
                    break
            elif isinstance(msg, bytes) and msg[:4] == MAGIC_AUDIO:
                audio = pcm_to_float(msg[4:])
                async with lock:
                    t0 = time.time()
                    frames = await asyncio.to_thread(lambda: run_pipeline(pipeline, get_audio_embedding(pipeline, audio)))
                if frames is None:
                    continue
                arr = frames.cpu().numpy() if hasattr(frames, "cpu") else np.asarray(frames)
                if arr.ndim == 4 and arr.shape[-1] != 3:
                    arr = arr.transpose(0, 2, 3, 1)
                if arr.dtype != np.uint8:
                    arr = (np.clip(arr, 0, 1) * 255).astype(np.uint8) if arr.max() <= 1.0 else arr.astype(np.uint8)
                await ws.send(frames_to_msg(arr))
                print(f"[{sid}] {arr.shape[0]} frames in {time.time() - t0:.2f}s", flush=True)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if ref_path and os.path.exists(ref_path):
            os.unlink(ref_path)
        print(f"[{sid}] disconnected", flush=True)


async def main() -> None:
    load_pipeline()
    print(f"[flashhead] ws://0.0.0.0:{PORT}/v1/avatar/realtime", flush=True)
    async with websockets.serve(handle, "0.0.0.0", PORT, max_size=50 * 1024 * 1024):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
