"""方言实测：VoxCPM2 官方列的 9 种方言，每种出两段样音。

1. design：文字描述直接生成方言音色（可做成方言预设）
2. clone：克隆音色 + 方言指令（可控克隆），验证「复刻音色 + 方言发音」能否同时成立
文本用该方言的说法写，帮模型认出是哪种方言。在 talk-tts 容器里跑：
python dialect_samples.py --clone clone_xxx [--out /w/cache/dialect]
"""
from __future__ import annotations

import argparse
import time
import wave
from pathlib import Path

import httpx

URL = "http://127.0.0.1:8302"
SR = 16000
# (文件名, 方言, 用于 design 的音色描述, 方言文本)
DIALECTS = [
    ("sichuan", "四川话", "五十岁的四川阿姨，说话亲切爽快", "哎呀，你今天吃饭没得？莫得事就早点回来嘛，屋头给你炖了汤的。"),
    ("cantonese", "粤语", "五十岁的广东阿姨，声音温柔", "你今日食咗饭未呀？得闲就早啲返嚟，屋企煲咗汤俾你饮。"),
    ("wu", "上海话", "六十岁的上海阿婆，说话慢条斯理", "侬今朝饭吃过伐？呒没事体就早点转来，屋里向帮侬炖了汤。"),
    ("dongbei", "东北话", "五十岁的东北大叔，嗓门敞亮", "哎呀妈呀，你今儿吃饭没？没啥事儿早点回来，家里给你炖了汤，老香了。"),
    ("henan", "河南话", "五十岁的河南大叔，说话实在", "你今儿个吃饭没？没啥事儿早点回来，家里给你熬嘞汤，中不中？"),
    ("shaanxi", "陕西话", "五十岁的陕西大叔，说话直爽", "你今儿个咥饭了没？没啥事早些回来，屋里给你熬的汤，嘹扎咧。"),
    ("shandong", "山东话", "五十岁的山东大娘，说话热情", "你今天吃饭了吗？没事儿早点家来，家里给你炖的汤，可好喝了。"),
    ("tianjin", "天津话", "六十岁的天津大爷，说话幽默", "介是嘛呀，你今儿吃饭了吗？没事儿早点回来，家里给你炖嘞汤。"),
    ("minnan", "闽南话", "五十岁的闽南阿嬷，声音温和", "你今仔日食饱未？无代志就较早转来，厝里有煮汤予你啉。"),
]


def save(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR), w.writeframes(pcm)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clone", required=True, help="用来测「克隆 + 方言」的克隆音色 ID")
    ap.add_argument("--out", default="/w/cache/dialect")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=180, trust_env=False) as c:
        for key, name, desc, text in DIALECTS:
            for kind, body in [("design", {"voice": f"design:{name}，{desc}"}),
                               ("clone", {"voice": a.clone, "style": f"用{name}说"})]:
                t = time.time()
                r = c.post(f"{URL}/v1/audio/speech", json={"input": text, **body})
                r.raise_for_status()
                save(out / f"{key}_{kind}.wav", r.content)
                print(f"{name} {kind}: {len(r.content) / 2 / SR:.1f}s audio in {time.time() - t:.1f}s", flush=True)


if __name__ == "__main__":
    main()
