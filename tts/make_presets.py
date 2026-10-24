"""生成本地预设音色：用 VoxCPM2 的 voice design（文字描述）各合成一段样音，存下来当固定参考。

在 talk-tts 容器里跑：python make_presets.py [--only id1,id2]
随机性较大，不满意的单独重跑（--only），满意的样音不会被覆盖。
"""
from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

import httpx

URL = "http://127.0.0.1:8302"
OUT = Path("/w/tts/presets")
SR = 16000
REF = "嗯，你好呀，很高兴认识你。最近过得怎么样？有什么开心的事，或者烦心的事，都可以跟我说说，我们慢慢聊。"
KID = "嘿嘿，你好呀！我今天在幼儿园画了一只大老虎，老师还夸我画得好呢，你想不想看看呀？"
# 老人样音：续写式克隆会把样音的语速语气整个继承下来，所以样音本身要有起伏（惊喜、心疼、笑、叮嘱各一句），不能平铺直叙
ELDER = "哎哟，是你呀！可算回来了。瘦了这么多，是不是又没好好吃饭？嘿嘿，没事没事，回来就好。天凉了，记得多穿一件，别跟奶奶客气。"

# (ID, 名称, 分组, 音色描述, 样音文本)
PRESETS = [
    ("vox_girl_kid", "萌萌女童", "儿童", "七八岁的小女孩，声音稚嫩清脆，活泼可爱", KID),
    ("vox_boy_kid", "淘气男童", "儿童", "七八岁的小男孩，声音稚嫩，调皮机灵", KID),
    ("vox_teen_girl", "清脆少女", "少年", "十六岁的少女，声音清脆明亮，元气满满", REF),
    ("vox_teen_boy", "邻家少年", "少年", "十六岁的少年，声音清澈干净，略带青涩", REF),
    ("vox_f_gentle", "温柔知性", "青年女", "二十多岁的年轻女性，声音温柔知性，语速舒缓，让人安心", REF),
    ("vox_f_sweet", "甜美女声", "青年女", "二十岁出头的女生，声音甜美软糯，带点撒娇", REF),
    ("vox_f_yujie", "成熟御姐", "青年女", "三十岁左右的成熟女性，声音低沉有磁性，自信干练", REF),
    ("vox_m_gentle", "温润青年", "青年男", "二十多岁的年轻男性，声音温润清朗，说话温和有礼", REF),
    ("vox_m_sunny", "阳光学长", "青年男", "二十出头的男大学生，声音阳光开朗，充满活力", REF),
    ("vox_m_radio", "深夜电台", "青年男", "三十岁的男主播，声音低沉醇厚，慵懒温柔，像深夜电台", REF),
    ("vox_f_anchor", "新闻女声", "中年", "四十岁的女新闻主播，标准普通话，字正腔圆，端庄大方", REF),
    ("vox_m_mature", "沉稳大叔", "中年", "四十多岁的中年男性，声音浑厚沉稳，可靠有分量", REF),
    ("vox_f_aunt", "热心大婶", "中年", "五十岁的热心阿姨，嗓门亮，说话热情爽快", REF),
    ("vox_grandma", "慈祥奶奶", "老年", "七十岁的老奶奶，声音苍老慈祥，语速慢，很温暖", REF),
    ("vox_grandpa", "幽默大爷", "老年", "七十岁的北京大爷，声音沙哑有点京腔，说话幽默风趣", REF),
    # 逝去的亲人人设用：要像真人老人，不夸张
    ("vox_elder_m", "慈祥爷爷", "老年", "七十多岁的老爷爷，普通话带一点山东口音，声音苍老温厚、有些沙哑，语速偏慢，语气有起伏，见到孙辈时惊喜、心疼、会呵呵笑、会叹气，像真人聊天，不夸张", ELDER),
    ("vox_elder_f", "温柔奶奶", "老年", "七十多岁的老奶奶，普通话带一点四川口音，声音苍老柔和，语速偏慢，语气有起伏，见到孙辈时惊喜、心疼、会轻轻笑、会叹气，像真人聊天，不夸张", ELDER),
]

# 方言预设：(ID, 名称, 分组, 音色描述, 方言样音文本, 方言)。样音用 2026-10-17 试听确认过的 dialect_samples 设计样音
# （拷成 vox_d_<key>.wav 就不再重新生成）；合成时服务端自动带上「用某某话说」
DIALECT_PRESETS = [
    ("vox_d_sichuan", "四川阿姨", "方言", "五十岁的四川阿姨，说话亲切爽快",
     "哎呀，你今天吃饭没得？莫得事就早点回来嘛，屋头给你炖了汤的。", "四川话"),
    ("vox_d_cantonese", "粤语阿姨", "方言", "五十岁的广东阿姨，声音温柔",
     "你今日食咗饭未呀？得闲就早啲返嚟，屋企煲咗汤俾你饮。", "粤语"),
    ("vox_d_wu", "上海阿婆", "方言", "六十岁的上海阿婆，说话慢条斯理",
     "侬今朝饭吃过伐？呒没事体就早点转来，屋里向帮侬炖了汤。", "上海话"),
    ("vox_d_dongbei", "东北大叔", "方言", "五十岁的东北大叔，嗓门敞亮",
     "哎呀妈呀，你今儿吃饭没？没啥事儿早点回来，家里给你炖了汤，老香了。", "东北话"),
    ("vox_d_henan", "河南大叔", "方言", "五十岁的河南大叔，说话实在",
     "你今儿个吃饭没？没啥事儿早点回来，家里给你熬嘞汤，中不中？", "河南话"),
    ("vox_d_shaanxi", "陕西大叔", "方言", "五十岁的陕西大叔，说话直爽",
     "你今儿个咥饭了没？没啥事早些回来，屋里给你熬的汤，嘹扎咧。", "陕西话"),
    ("vox_d_shandong", "山东大娘", "方言", "五十岁的山东大娘，说话热情",
     "你今天吃饭了吗？没事儿早点家来，家里给你炖的汤，可好喝了。", "山东话"),
    ("vox_d_tianjin", "天津大爷", "方言", "六十岁的天津大爷，说话幽默",
     "介是嘛呀，你今儿吃饭了吗？没事儿早点回来，家里给你炖嘞汤。", "天津话"),
    ("vox_d_minnan", "闽南阿嬷", "方言", "五十岁的闽南阿嬷，声音温和",
     "你今仔日食饱未？无代志就较早转来，厝里有煮汤予你啉。", "闽南话"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    a = ap.parse_args()
    only = set(a.only.split(",")) if a.only else None
    OUT.mkdir(parents=True, exist_ok=True)
    pj = OUT / "presets.json"
    old = {p["id"]: p for p in json.loads(pj.read_text(encoding="utf-8"))} if pj.exists() else {}
    items = []
    defs = [(*p, "") for p in PRESETS] + DIALECT_PRESETS
    with httpx.Client(timeout=120, trust_env=False) as c:
        for vid, name, group, desc, text, dialect in defs:
            wav = OUT / f"{vid}.wav"
            item = {"id": vid, "name": name, "group": group, "desc": desc, "ref_text": text}
            if dialect:
                item["dialect"] = dialect
            if only is not None and vid not in only:
                if vid in old:
                    items.append(old[vid])
                continue
            if only is None and wav.exists():          # 已有样音（含手动放进来的）：不重生成
                items.append(old.get(vid, item))
            else:
                design = f"design:{dialect}，{desc}" if dialect else f"design:{desc}"
                r = c.post(f"{URL}/v1/audio/speech", json={"input": text, "voice": design})
                r.raise_for_status()
                with wave.open(str(wav), "wb") as w:
                    w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR), w.writeframes(r.content)
                print(f"{vid} {name}: {len(r.content) / 2 / SR:.1f}s")
                items.append(item)
            c.post(f"{URL}/voices/register", json={"id": vid, "name": name, "ref_text": text, "wav": str(wav),
                                                   "group": group, "dialect": dialect}).raise_for_status()
    order = [d[0] for d in defs]
    items.sort(key=lambda p: order.index(p["id"]))
    pj.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
