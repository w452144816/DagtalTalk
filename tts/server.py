"""本地语音合成服务：VoxCPM2 + nano-vllm-voxcpm（连续批处理，多路并发），独立容器跑在 GPU0。

给网关一个 OpenAI 兼容的流式接口，网关再转给 OpenTalking：
  POST /v1/audio/speech {"input", "voice", "response_format": "pcm"} → 16k s16le 流
音色 = 一段参考音频 + 它的文字（VoxCPM2 的"终极克隆"续写方式，音色最稳）：
  - 预设音色：用 voice design（文字描述）生成一段样音，固定下来当参考 → presets.json
  - 克隆音色：网关登记在 state/voices.json 的录音（clone_xxx），启动时和新增时注册
参考音频只在注册时编码一次（add_prompt），之后每句话只带 prompt_id。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import soxr
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

log = logging.getLogger("tts")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

MODEL = os.getenv("VOX_MODEL", "/w/models/VoxCPM2")
STATE = Path(os.getenv("VOX_STATE", "/w/state"))          # 和 talk 共用：voices.json、voices/*.wav
PRESETS = Path(os.getenv("VOX_PRESETS", "/w/tts/presets"))  # presets.json + 样音 wav
OUT_SR = 16000
CFG = float(os.getenv("VOX_CFG", "2.0"))
MAX_GEN = int(os.getenv("VOX_MAX_GEN", "2000"))      # 单句生成步数上限；加上参考音频不能超过 max_model_len

pool = None
model_sr = 48000
prompts: dict[str, str] = {}          # 音色 ID → prompt_id（续写式克隆，音色最稳）
refs: dict[str, bytes] = {}           # 音色 ID → 参考音频 latents（可控克隆：参考音色 + 风格/方言指令）
meta: dict[str, dict] = {}            # 音色 ID → {name, group, ...}
plat: dict[str, tuple[bytes, str]] = {}   # 音色 ID → (续写提示 latents, 样音文本)：带指令的续写式克隆要自己拼提示文本（指令放最前）
stats = {"active": 0, "peak": 0, "done": 0}


async def _register(vid: str, wav: Path, text: str, info: dict) -> None:
    raw = wav.read_bytes()
    pid = await pool.add_prompt(raw, "wav", text)
    refs[vid] = await pool.encode_latents(raw, "wav")
    old = prompts.get(vid)
    prompts[vid], meta[vid] = pid, info
    plat[vid] = (pool._prompt_pool[pid]["latents"], text)
    if old:
        await pool.remove_prompt(old)


async def load_voices() -> None:
    try:
        presets = json.loads((PRESETS / "presets.json").read_text(encoding="utf-8"))
    except OSError:
        presets = []
    for p in presets:
        info = {"name": p["name"], "group": p.get("group", "本地预设")}
        if p.get("dialect"):         # 方言预设：不另带方言时，默认按这个方言说
            info["dialect"] = p["dialect"]
        await _register(p["id"], PRESETS / f"{p['id']}.wav", p["ref_text"], info)
    try:
        clones = json.loads((STATE / "voices.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        clones = []
    for v in clones:
        wav = STATE / "voices" / f"{v['id']}.wav"
        if wav.exists():
            await _register(v["id"], wav, v["ref_text"], {"name": v["name"], "group": "我的克隆音色"})
    log.info("voices registered: %d presets, %d clones", len(presets), len(clones))


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool, model_sr
    from nanovllm_voxcpm import VoxCPM
    t = time.time()
    pool = VoxCPM.from_pretrained(
        model=MODEL,
        max_num_batched_tokens=int(os.getenv("VOX_MAX_BATCHED_TOKENS", "8192")),
        max_num_seqs=int(os.getenv("VOX_MAX_SEQS", "32")),
        max_model_len=int(os.getenv("VOX_MAX_MODEL_LEN", "4096")),   # 参考音频 + 一句话远到不了；小显存卡可调低
        gpu_memory_utilization=float(os.getenv("VOX_GPU_MEM", "0.5")),
        enforce_eager=os.getenv("VOX_EAGER", "0") == "1",   # 1：不捕获 CUDA graph（省 2~3G 显存，慢一些）
        # 容器内卡号，多张卡各起一份模型，按负载分请求（显存小的卡单卡并发上不去，就多给几张）
        devices=[int(x) for x in os.getenv("VOX_DEVICES", "0").split(",")],
    )
    await pool.wait_for_ready()
    model_sr = int((await pool.get_model_info())["sample_rate"])
    await load_voices()
    log.info("voxcpm ready in %.1fs, sr=%d", time.time() - t, model_sr)
    async for _ in pool.generate(target_text="你好。", cfg_value=CFG):   # 预热
        pass
    yield
    await pool.stop()


app = FastAPI(lifespan=lifespan)


STYLE_MODE = os.environ.get("VOX_STYLE_MODE", "ultimate")   # ultimate：续写提示 + 参考 latents + 指令（用户试听定的：参考模式前后句音色漂得太厉害）；ref：只给参考 latents


class Speech(BaseModel):
    input: str
    voice: str = ""
    model: str = ""
    response_format: str = "pcm"
    style: str = ""       # 风格/方言指令，如「四川话，语气亲切」：克隆音色改走「参考音频 + 指令」的可控克隆


def _s16(y: np.ndarray) -> bytes:
    return (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes()


@app.post("/v1/audio/speech")
async def speech(req: Speech):
    text = req.input.strip()
    if not text:
        raise HTTPException(400, "empty input")
    kw: dict = {"cfg_value": CFG, "max_generate_length": MAX_GEN}
    style = req.style.strip()
    if not style and meta.get(req.voice, {}).get("dialect"):
        style = f"用{meta[req.voice]['dialect']}说"
    if style and req.voice in refs:                # 可控克隆：指令写在括号里
        kw["ref_audio_latents"] = refs[req.voice]
        if STYLE_MODE == "ultimate" and req.voice in plat:
            # 续写式 + 参考 latents + 指令：指令必须放在整段文本（样音文本 + 正文）的最前面；
            # 放在正文前会接在样音文本后头，模型会把指令念出来（用户实测）。所以不用 prompt_id，自己给 prompt_latents/prompt_text
            lat, ref_text = plat[req.voice]
            kw["prompt_latents"] = lat
            kw["prompt_text"] = f"({style}){ref_text}"
        else:
            text = f"({style}){text}"
    elif req.voice in prompts:
        kw["prompt_id"] = prompts[req.voice]
    elif req.voice.startswith("design:"):          # 临时：直接用文字描述的音色（每句音色会略有漂移）
        text = f"({req.voice[7:]}{'，' + style if style else ''}){text}"
    elif req.voice:
        raise HTTPException(404, f"unknown voice {req.voice}")

    async def gen():
        rs = soxr.ResampleStream(model_sr, OUT_SR, 1, dtype="float32")
        t0, first = time.time(), None
        n = 0
        stats["active"] += 1
        stats["peak"] = max(stats["peak"], stats["active"])
        try:
            async for chunk in pool.generate(target_text=text, **kw):
                y = rs.resample_chunk(np.asarray(chunk, dtype=np.float32).reshape(-1))
                if first is None:
                    first = time.time() - t0
                n += len(y)
                if len(y):
                    yield _s16(y)
            yield _s16(rs.resample_chunk(np.zeros(0, dtype=np.float32), last=True))
        finally:
            stats["active"] -= 1
            stats["done"] += 1
            wall = time.time() - t0
            dur = n / OUT_SR
            log.info("speech voice=%s chars=%d first=%.0fms audio=%.2fs wall=%.2fs rtf=%.2f active=%d",
                     req.voice, len(text), (first or 0) * 1000, dur, wall, (wall - (first or 0)) / dur if dur else 0,
                     stats["active"])

    return StreamingResponse(gen(), media_type="audio/pcm")


class Register(BaseModel):
    id: str
    name: str
    ref_text: str
    wav: str              # 容器内路径
    group: str = "我的克隆音色"
    dialect: str = ""     # 方言预设的默认方言


@app.post("/voices/register")
async def register(r: Register):
    info = {"name": r.name, "group": r.group}
    if r.dialect:
        info["dialect"] = r.dialect
    await _register(r.id, Path(r.wav), r.ref_text, info)
    return {"ok": True, "id": r.id}


@app.delete("/voices/{vid}")
async def unregister(vid: str):
    pid = prompts.pop(vid, None)
    meta.pop(vid, None)
    refs.pop(vid, None)
    if pid:
        await pool.remove_prompt(pid)
    return {"ok": True}


@app.get("/voices")
async def voices():
    return [{"id": k, **v} for k, v in meta.items()]


@app.get("/health")
async def health():
    return {"ok": pool is not None, "voices": len(prompts), **stats}
