"""MiniMax H3 本地图生视频（首帧 = 尾帧 = 照片，做数字人可循环的待机底板），走 ComfyUI API。

在 talk-h3 容器里跑（ComfyUI 已在 127.0.0.1:8188）：
python gen.py --image /w/input/face.png [--steps 4] [--seconds 6] [--width 480 --height 832] [--prompt ...]
提交前先用 /object_info 核对每个节点的输入名，没写的必填参数用节点默认值补上。
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
import uuid
from pathlib import Path

import requests

URL = "http://127.0.0.1:8188"
IDLE_PROMPT = (
    "Fixed camera, frontal medium shot, realistic live-action footage. The person in <Picture 1> looks at the camera "
    "and listens calmly: blinks slowly two or three times, gives a slight nod, turns the head a little left and right, "
    "breathes naturally, hands move subtly. The mouth stays closed and relaxed the whole time, no talking, no smiling "
    "widely. Background, clothing and lighting stay exactly as in the photo. The clip starts and ends in the same pose "
    "as the photo so it loops seamlessly. Quiet room tone only."
)


def frames(seconds: float) -> int:
    n = max(5, round(seconds * 24))
    while n % 17 != 5:      # 模型的帧数网格：17k + 5
        n += 1
    return n


def graph(img: str, a) -> dict:
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": a.unet, "weight_dtype": "default"}},
        "lora": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["unet", 0], "lora_name": a.lora, "strength_model": 1.0}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": a.clip, "type": "minimax", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": a.vae}},
        "avae": {"class_type": "VAELoader", "inputs": {"vae_name": a.audio_vae}},
        "img": {"class_type": "LoadImage", "inputs": {"image": img}},
        "cond": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["clip", 0], "vae": ["vae", 0], "prompt": a.prompt, "width": a.width, "height": a.height,
            "length": frames(a.seconds), "first_frame": ["img", 0], "last_frame": ["img", 0]}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": a.seed}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "sigmas": {"class_type": "BasicScheduler", "inputs": {"model": ["lora", 0], "scheduler": "simple", "steps": a.steps, "denoise": 1.0}},
        "guider": {"class_type": "BasicGuider", "inputs": {"model": ["lora", 0], "conditioning": ["cond", 0]}},
        "run": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0], "sigmas": ["sigmas", 0],
            "latent_image": ["cond", 1]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["run", 0], "vae": ["vae", 0]}},
        "adec": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["run", 0], "vae": ["avae", 0]}},
        "video": {"class_type": "CreateVideo", "inputs": {"images": ["dec", 0], "audio": ["adec", 0], "fps": 24.0}},
        # format / codec 是动态选项：子选项用点号拼名字（format.codec）
        "save": {"class_type": "SaveVideo", "inputs": {"video": ["video", 0], "filename_prefix": f"h3/{a.tag}",
                                                       "format": "auto", "format.codec": "auto"}},
    }


def fill_and_check(g: dict) -> dict:
    """按 /object_info 校验输入名，并给没写的必填项补默认值；有对不上的直接报出来。"""
    info = requests.get(f"{URL}/object_info", timeout=30).json()
    for nid, n in g.items():
        spec = info.get(n["class_type"])
        if spec is None:
            raise SystemExit(f"节点不存在：{n['class_type']}")
        req, opt = spec["input"].get("required", {}), spec["input"].get("optional", {})
        for k in n["inputs"]:
            if "." in k and k.split(".")[0] in req:      # 动态选项的子参数
                continue
            if k not in req and k not in opt:
                raise SystemExit(f"{nid}/{n['class_type']} 没有输入 {k}；可用：{list(req) + list(opt)}")
        for k, v in req.items():
            if k in n["inputs"]:
                continue
            cfg = v[1] if len(v) > 1 and isinstance(v[1], dict) else {}
            if "default" in cfg:
                n["inputs"][k] = cfg["default"]
            elif isinstance(v[0], list) and v[0]:
                n["inputs"][k] = v[0][0]
            else:
                raise SystemExit(f"{nid}/{n['class_type']} 缺必填输入 {k}（{v[0]}）")
    return g


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--prompt", default=IDLE_PROMPT)
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--seconds", type=float, default=6)
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--height", type=int, default=832)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tag", default="idle")
    ap.add_argument("--unet", default="minimax_h3_fl2va_pruned_int8_convrot.safetensors")
    ap.add_argument("--lora", default="minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors")
    ap.add_argument("--clip", default="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")
    ap.add_argument("--vae", default="minimax_h3_video_vae_int8_convrot.safetensors")
    ap.add_argument("--audio-vae", default="minimax_h3_audio_vae_fp32.safetensors")
    a = ap.parse_args()

    with open(a.image, "rb") as f:
        up = requests.post(f"{URL}/upload/image", files={"image": (Path(a.image).name, f)}, data={"overwrite": "true"}).json()
    g = fill_and_check(graph(up["name"], a))
    cid = uuid.uuid4().hex
    t0 = time.time()
    r = requests.post(f"{URL}/prompt", json={"prompt": g, "client_id": cid}).json()
    if "prompt_id" not in r:
        raise SystemExit(json.dumps(r, ensure_ascii=False)[:2000])
    pid = r["prompt_id"]
    print(f"submitted {pid}: {a.width}x{a.height} {frames(a.seconds)} frames, {a.steps} steps", flush=True)
    while True:
        h = requests.get(f"{URL}/history/{pid}", timeout=30).json().get(pid)
        if h and h.get("status", {}).get("completed") is not None:
            break
        time.sleep(3)
    st = h["status"]
    print(f"status={st.get('status_str')} wall={time.time() - t0:.1f}s")
    if st.get("status_str") != "success":
        for m in st.get("messages", []):
            if m[0] == "execution_error":
                print(json.dumps(m[1], ensure_ascii=False)[:3000])
        raise SystemExit(1)
    for out in h["outputs"].values():
        for key in ("images", "videos", "gifs"):
            for v in out.get(key, []):
                print("output:", v.get("subfolder"), v.get("filename"))


if __name__ == "__main__":
    main()
