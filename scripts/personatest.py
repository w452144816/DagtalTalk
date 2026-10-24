"""人设构建冒烟（后端视角），在 talk-control 容器里跑：

docker cp scripts/personatest.py talk-control:/tmp/ && docker exec -e AI_SERVICE_KEY=... talk-control python /tmp/personatest.py

建人设 → 传一份文本话术 + 一张图（形象参考图）+ 一段音频（hello.mp3）→ 等抽取 → build → 看结构化结果 →
文本试聊 → approve → 用 persona_id 建会话 speak 一句 → 删除。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

import httpx

DOC = """
【公司介绍】星河教育成立于 2015 年，专注中小学数学思维培训，全国 32 个城市有校区，累计学员 20 万人。
【讲师】王老师，北师大数学系硕士，12 年教龄，主讲三到六年级奥数，学生亲切地叫他"王叔"。
口头禅："咱们先别急着算，先看题目在问什么。"说话爱举生活里的例子，语速慢，常用"对吧""你看"。
【课程】
- 思维启蒙班（三年级）：每周一次，90 分钟，一期 16 次课，2980 元。
- 奥数提高班（四五年级）：每周一次，120 分钟，一期 16 次课，3980 元，含 4 次线上答疑。
- 小升初冲刺班（六年级）：寒暑假集训 10 天，5800 元。
【常见问题】
问：可以试听吗？答：可以，每个班型提供一次免费试听，预约后 24 小时内老师回电。
问：中途退费怎么算？答：开课 3 次以内全额退，之后按剩余课次退。
问：线上还是线下？答：以线下为主，提高班有线上答疑。
【禁忌】不评价其他机构，不承诺提分幅度，不透露学员个人信息。价格以校区最新公示为准。
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:8443")
    ap.add_argument("--image", default="/w/avatars/custom-Nero_AI_Avatar_G-20261016-010844-956/reference.png")
    ap.add_argument("--audio", default="/w/cache/hello.mp3")
    ap.add_argument("--avatar", default="anchor")
    a = ap.parse_args()
    H = {"Authorization": f"Bearer {os.environ['AI_SERVICE_KEY']}"}
    async with httpx.AsyncClient(base_url=a.base, verify=False, timeout=300) as c:
        r = await c.post("/v1/personas", headers=H, json={"name": "王老师-奥数", "biz_ref": "cust_001",
                                                           "seed": {"identity": {"name": "王老师", "role": "奥数讲师"}}})
        print("create", r.status_code, r.json().get("persona_id"))
        pid = r.json()["persona_id"]
        jobs = []
        r = await c.post(f"/v1/personas/{pid}/materials", headers=H, data={"kind": "doc", "hint": "公司和课程介绍、话术"},
                         files={"file": ("intro.txt", DOC.encode(), "text/plain")})
        print("doc", r.status_code, r.text[:120]); jobs.append(r.json()["job_id"])
        if os.path.exists(a.image):
            r = await c.post(f"/v1/personas/{pid}/materials", headers=H, data={"kind": "image", "hint": "讲师形象照"},
                             files={"file": ("teacher.png", open(a.image, "rb").read(), "image/png")})
            print("image", r.status_code, r.text[:120]); jobs.append(r.json()["job_id"])
        if os.path.exists(a.audio):
            r = await c.post(f"/v1/personas/{pid}/materials", headers=H, data={"kind": "audio", "hint": "讲师说话片段"},
                             files={"file": ("clip.mp3", open(a.audio, "rb").read(), "audio/mpeg")})
            print("audio", r.status_code, r.text[:120]); jobs.append(r.json()["job_id"])
        t0 = time.time()
        for jid in jobs:
            while True:
                j = (await c.get(f"/v1/jobs/{jid}", headers=H)).json()
                if j["status"] != "running":
                    print(f"  job {jid} {j['status']} +{time.time() - t0:.0f}s {j.get('result') or j.get('error')}")
                    break
                await asyncio.sleep(3)
        mats = (await c.get(f"/v1/personas/{pid}/materials", headers=H)).json()
        for m in mats:
            print("  material", m["kind"], m["status"], (m.get("summary") or m.get("error") or "")[:80])
        r = await c.post(f"/v1/personas/{pid}/build", headers=H)
        print("build", r.status_code, r.text[:100])
        jid = r.json()["job_id"]
        while (j := (await c.get(f"/v1/jobs/{jid}", headers=H)).json())["status"] == "running":
            await asyncio.sleep(3)
        print("  build", j["status"], f"+{time.time() - t0:.0f}s", j.get("error") or "")
        p = (await c.get(f"/v1/personas/{pid}", headers=H)).json()
        d = p.get("data") or {}
        print("identity", d.get("identity"))
        print("style", json.dumps(d.get("style"), ensure_ascii=False)[:300])
        print("knowledge facts", len((d.get("knowledge") or {}).get("facts") or []), "faq", len((d.get("knowledge") or {}).get("faq") or []))
        print("boundaries", d.get("boundaries"))
        print("recommend", d.get("recommend"))
        print("compiled", {k: v for k, v in (p.get("compiled") or {}).items() if k != "system_prompt"})
        print("--- system_prompt ---\n" + (p.get("compiled") or {}).get("system_prompt", "")[:1200] + "\n---")
        async with c.stream("POST", f"/v1/personas/{pid}/chat", headers=H,
                            json={"messages": [{"role": "user", "content": "提高班多少钱？能试听吗？"}]}) as s:
            out = ""
            async for line in s.aiter_lines():
                if line.startswith("data:") and "[DONE]" not in line:
                    try:
                        out += (json.loads(line[5:])["choices"][0]["delta"].get("content") or "")
                    except Exception:  # noqa: BLE001
                        pass
            print("chat ->", out)
        print("approve", (await c.post(f"/v1/personas/{pid}/approve", headers=H)).json().get("status"))
        r = await c.post("/v1/sessions", headers=H, json={"avatar_id": a.avatar, "persona": {"persona_id": pid}, "biz_ref": "cust_001"})
        print("session with persona", r.status_code, r.text[:160])
        if r.status_code == 201:
            sid = r.json()["session_id"]
            print("  speak", (await c.post(f"/v1/sessions/{sid}/speak", headers=H, json={"text": "你好，你是谁？"})).status_code)
            await asyncio.sleep(8)
            print("  session", (await c.get(f"/v1/sessions/{sid}", headers=H)).json().get("usage"))
            print("  delete", (await c.delete(f"/v1/sessions/{sid}", headers=H)).status_code)
        print("export keys", list((await c.get(f"/v1/personas/{pid}/export", headers=H)).json().keys()))
        print("list", [(x["name"], x["status"], x["version"]) for x in (await c.get("/v1/personas", headers=H)).json()])


asyncio.run(main())
