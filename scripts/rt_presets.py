"""全身实时通话的预设背景和服装图（主页上选，开播前一次性套上）。用 MiniMax 生图，只需要跑一次。
在控制面容器里跑：python /tmp/rt_presets.py  → /w/data/rt_presets/{bg,outfit}/<id>.jpg + presets.json
已有的图跳过；--regen id1,id2 重画某几张。
"""
import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, "/app/gateway")
from config import LLMS  # noqa: E402

OUT = Path("/w/data/rt_presets")
NO_PEOPLE = "画面里没有任何人，没有文字。真实摄影，柔和自然光，背景干净，构图平稳，适合作为视频通话的背景。"
FLAT = "白色纯背景上的服装平铺商品图，只有这一件衣服，没有人、没有模特、没有衣架、没有文字，正面平铺，完整可见，真实质感。"
PRESETS = {
    "bg": [
        ("home", "温馨客厅", "温馨的中式家庭客厅，木质家具、布艺沙发、暖色落地灯，傍晚。" + NO_PEOPLE),
        ("yard", "农家小院", "北方农家小院，红砖墙、木门、几盆花草和一棵枣树，午后阳光。" + NO_PEOPLE),
        ("park", "公园湖边", "城市公园湖边，柳树和长椅，秋天，阳光柔和。" + NO_PEOPLE),
        ("balcony", "阳光阳台", "洒满阳光的家庭阳台，绿植、藤椅、白色窗纱。" + NO_PEOPLE),
        ("kitchen", "家里厨房", "干净整洁的老式家庭厨房，灶台、碗柜、窗边挂着蒜辫，暖光。" + NO_PEOPLE),
        ("study", "安静书房", "安静的家庭书房，木书架、书桌、窗外绿树，柔光。" + NO_PEOPLE),
    ],
    "outfit": [
        ("cardigan", "米色开衫", "米色粗针织开衫毛衣，木纽扣。" + FLAT),
        ("tang", "中式棉袄", "深红色中式盘扣棉袄，传统立领。" + FLAT),
        ("shirt", "格子衬衫", "浅蓝色格子长袖衬衫。" + FLAT),
        ("vest", "毛线背心", "灰色手织毛线背心，V 领。" + FLAT),
        ("coat", "呢子大衣", "藏青色呢子大衣，翻领，双排扣。" + FLAT),
        ("floral", "碎花上衣", "淡紫色碎花棉布长袖上衣，圆领。" + FLAT),
    ],
}


async def gen(c: httpx.AsyncClient, prompt: str, ratio: str) -> bytes:
    key = LLMS["minimax"].api_key
    r = await c.post("https://api.minimaxi.com/v1/image_generation", headers={"Authorization": f"Bearer {key}"},
                     json={"model": "image-01", "prompt": prompt, "n": 1, "aspect_ratio": ratio,
                           "response_format": "base64", "prompt_optimizer": False})
    r.raise_for_status()
    j = r.json()
    if (j.get("base_resp") or {}).get("status_code"):
        raise RuntimeError(j["base_resp"])
    return base64.b64decode(j["data"]["image_base64"][0])


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regen", default="")
    a = ap.parse_args()
    regen = set(filter(None, a.regen.split(",")))
    meta = {}
    async with httpx.AsyncClient(timeout=180, trust_env=False) as c:
        for kind, items in PRESETS.items():
            (OUT / kind).mkdir(parents=True, exist_ok=True)
            meta[kind] = []
            jobs = [(i, n, p) for i, n, p in items if i in regen or not (OUT / kind / f"{i}.jpg").exists()]
            res = await asyncio.gather(*[gen(c, p, "3:4" if kind == "bg" else "1:1") for _, _, p in jobs],
                                       return_exceptions=True)
            for (i, _n, _p), r in zip(jobs, res):
                if isinstance(r, Exception):
                    print("fail", kind, i, r, flush=True)
                else:
                    (OUT / kind / f"{i}.jpg").write_bytes(r)
                    print("got", kind, i, len(r), flush=True)
            meta[kind] = [{"id": i, "name": n} for i, n, _ in items if (OUT / kind / f"{i}.jpg").exists()]
    (OUT / "presets.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
