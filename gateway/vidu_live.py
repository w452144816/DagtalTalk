"""Vidu S2 实时数字人（Real-time Edition）：另一种通话方式。

形象（照片）、人设、音色用我们这边的；语音识别、对话模型、合成、渲染和 RTC 都由 Vidu 提供。
- POST /api/vidu/live：建会话（Vidu key 只在服务端）。返回 live_id 和阿里云 RTC 入会凭证。
- WS   /api/vidu/ws/{live_id}：代理 Vidu 控制通道（浏览器的 WebSocket 不能带 Authorization 头）。
  代理负责 conn_init（视频模式下 Vidu 渲染端没连回来时会回 NOT_READY，自动重试），之后双向转发；
  页面只发 {"type": 7}（打断）/ {"type": 99, "text": "..."}（文字）/ {"type": 5}（挂断）。浏览器断开时自动挂断，避免空跑计费。
- 媒体（麦克风、数字人音视频）走阿里云 RTC，页面 /vidu 用 AliRTC Web SDK 入会。
计费：1.5 点/秒（从双方就绪开始算）；idle_timeout_seconds 兜底，没人说话 5 分钟自动断。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

import httpx
import websockets
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import personas

log = logging.getLogger("vidu_live")
router = APIRouter()
HOST = os.getenv("VIDU_HOST", "api.vidu.cn")
KEY_FILE = Path("/w/state/vidu.key")
CACHE = Path("/w/state/vidu_cache.json")          # {"avatars": {我们的形象id: vidu 形象资产id}, "voices": {我们的音色: vidu 克隆音色名}}
VOICE_DIR = Path("/w/data/vidu_voices")             # 克隆用的样音：<音色id>.wav + <音色id>.txt（样音文本）
AVATARS_DIR = Path(os.getenv("OPENTALKING_AVATARS_DIR", "/w/avatars"))
# Vidu 自己的对话模型直接出声音：不能要求它写 [情绪] 标签（会被念出来），只保留口语化、简短的要求
RULES = ("你正在和对方视频通话，你说的每个字都会被直接念出来。用口语化的中文简短回答，每次一到三句话，像真人聊天；"
         "不要用表情符号、括号里的动作描写、方括号标签或列表。"
         "你只用说话交流：不要表演动作，不要拿出、展示或变出任何东西（眼镜、书、杯子、手机等都不要）；"
         "对方让你做动作、拿东西、戴东西、换衣服时，不要照做，回答里也不要提到那些东西的名字（比如不要说眼镜、书、杯子），"
         "画面会跟着你说的话变，所以只要温和地把话题引回聊天就好。始终保持自然的姿势和平稳的神情。")
# 待机小动作：老人慢、幅度小（12 秒一次，做完歇 5 秒）
ELDER_IDLE = {"source": "custom", "motions": [
    {"name": "nod", "content": "缓慢地轻轻点一下头", "weight": 2},
    {"name": "blink", "content": "慢慢眨眼，温和地看着镜头", "weight": 3},
    {"name": "smile", "content": "嘴角带着慈祥的微笑，轻轻呼吸", "weight": 2}],
    "scheduling": {"motion_interval": 12, "rest_after_motion": 5}}
# 其他形象的待机小动作也只用细微的（平台默认动作库里有拿道具的大动作，实测会凭空变出眼镜、书）
GENTLE_IDLE = {"source": "custom", "recommend": "random", "motions": [
    {"name": "blink", "content": "自然地眨眨眼，温和地看着镜头", "weight": 3},
    {"name": "nod", "content": "轻轻点一下头", "weight": 1},
    {"name": "breathe", "content": "轻微地呼吸，表情放松", "weight": 2}],
    "scheduling": {"motion_interval": 15, "rest_after_motion": 5}}
ELDER_IDLE["recommend"] = "random"
# 实测 vidu-s2 会照着对方的话直接演（让她"戴眼镜、拿本书"，嘴上说没有，画面里却戴上眼镜、拿出书），action.enabled=false 也挡不住；
# 背景/服装已经在通话前换进照片里，不需要 S2 的中途换图 → 默认用 s1（文档写明"高稳定性"）
RT_MODEL = os.getenv("RT_MODEL", "vidu-s1")
PRESET_DIR = Path("/w/data/rt_presets")              # 预设背景/服装（scripts/rt_presets.py 生成）
ACTIVE: dict[str, str] = {}                          # 用户编号 → 进行中的会话（同一个人同时只留一个，免得多个会话一起计费）
KILL: dict[str, asyncio.Event] = {}                  # 会话 → 挂断信号（代理收到就发 hangup）
COMPOSED = Path("/w/data/rt_composed")              # 换好背景/服装的照片（通话前生成，通话时就用这张，过程中不再变）
_lock = asyncio.Lock()


def _key() -> str:
    k = os.getenv("VIDU_API_KEY", "").strip() or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else "")
    if not k:
        raise HTTPException(503, "实时通话服务没有配置密钥")
    return k


def _hdr() -> dict:
    return {"Authorization": f"Token {_key()}", "Content-Type": "application/json"}


def _load() -> dict:
    try:
        return json.loads(CACHE.read_text("utf-8"))
    except (OSError, ValueError):
        return {"avatars": {}, "voices": {}}


def _save(d: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(CACHE)


async def _api(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
    r = await c.request(method, f"https://{HOST}{path}", headers=_hdr(), **kw)
    if r.status_code >= 400:
        log.warning("vidu %s %s -> %s %s", method, path, r.status_code, r.text[:300])
        raise HTTPException(502, f"实时通话服务出错（{r.status_code}）：{_err_msg(r.text)}")
    return r.json()


def _err_msg(text: str) -> str:
    try:
        return str(json.loads(text).get("message") or text)[:160]
    except ValueError:
        return text[:160]


def _data_uri(p: Path) -> str:
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".wav": "audio/wav", ".mp3": "audio/mpeg"}.get(p.suffix.lower(), "application/octet-stream")
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


def _avatar_image(aid: str) -> Path:
    if aid.startswith("composed:"):
        f = COMPOSED / f"{re.sub(r'[^0-9a-f]', '', aid.split(':', 1)[1])}.jpg"
        if f.is_file():
            return f
        raise HTTPException(404, "换装照片不存在，请重新选择")
    d = AVATARS_DIR / aid
    for name in ("reference.png", "source/source.png", "preview.png"):
        if (d / name).is_file():
            return d / name
    raise HTTPException(404, f"形象 {aid} 没有可用的照片")


def _avatar_name(aid: str) -> str:
    try:
        return str(json.loads((AVATARS_DIR / aid / "manifest.json").read_text("utf-8")).get("name") or "")
    except (OSError, ValueError):
        return ""


def _persona_of(name: str) -> tuple[str, str]:
    """形象名 → (人设文本, 我们的音色 id)。按 AVATAR_PERSONA 的名字前缀匹配（如「奶奶·李秀兰（情绪）」）。"""
    pid = next((p for k, p in personas.AVATAR_PERSONA.items() if name.startswith(k)), None)
    for i, _n, _d, style, voice in personas.PERSONAS:
        if i == pid:
            return f"{style.strip()}\n{RULES}", voice
    return "", ""


async def _vidu_avatar(c: httpx.AsyncClient, aid: str) -> dict:
    """我们的形象 → Vidu 形象资产（上传一次缓存起来，建会话快很多；资产 90 天后过期，过期就重传）。"""
    cache = _load()
    vid = cache["avatars"].get(aid)
    if vid:
        a = {"status": "failed"}                      # 查不到（过期被删）就当失败，重新上传
        with contextlib.suppress(HTTPException):
            a = await _api(c, "GET", f"/live/v1/avatars/{vid}")
            if str(a.get("status")) in ("success", "11"):
                return {"id": vid}
        if str(a.get("status")) not in ("failed", "12"):       # 还在生成：接着等它，别重复上传
            return await _wait_asset(c, aid, vid, a)
    a = await _api(c, "POST", "/live/v1/avatars", json={"image_uri": _data_uri(_avatar_image(aid)), "name": aid[:60]})
    vid = str(a.get("id") or "")
    cache = _load()
    cache["avatars"][aid] = vid                       # 先记下（生成中也记，下次接着等，不重复上传）
    _save(cache)
    return await _wait_asset(c, aid, vid, a)


async def _wait_asset(c: httpx.AsyncClient, aid: str, vid: str, a: dict) -> dict:
    for _ in range(90):                               # 等资产描述生成（实测 30 秒以上）
        st = str(a.get("status"))
        if st in ("success", "11"):
            log.info("vidu avatar asset %s -> %s", aid, vid)
            return {"id": vid}
        if st in ("failed", "12"):
            cache = _load()
            cache["avatars"].pop(aid, None)
            _save(cache)
            log.warning("vidu avatar asset for %s failed, sending image inline", aid)
            return {"image_uri": _data_uri(_avatar_image(aid))}
        await asyncio.sleep(1)
        a = await _api(c, "GET", f"/live/v1/avatars/{vid}")
    log.warning("vidu avatar asset %s still %s after 90s", vid, a.get("status"))
    return {"id": vid}


async def _vidu_voice(c: httpx.AsyncClient, voice: str) -> str:
    """我们的音色（如 vox_elder_f）→ Vidu 克隆音色名（克隆一次缓存）。没有样音或克隆失败就用 Vidu 默认音色。"""
    if not voice:
        return ""
    cache = _load()
    if voice in cache["voices"]:
        return cache["voices"][voice]
    wav = VOICE_DIR / f"{voice}.wav"
    if not wav.is_file():
        return ""
    name = ("t_" + re.sub(r"[^A-Za-z0-9_]", "_", voice))[:16]
    body = {"audio_url": _data_uri(wav), "voice": name, "language": "zh", "provider": "qwen_omni"}
    txt = VOICE_DIR / f"{voice}.txt"
    if txt.is_file():
        body["text"] = txt.read_text("utf-8").strip()[:4000]
    try:
        r = await _api(c, "POST", "/live/v1/voices/clone", json=body)
        name = str(r.get("voice") or name)
    except HTTPException as e:
        if "exist" not in str(e.detail).lower() and "duplicate" not in str(e.detail).lower():
            log.warning("vidu voice clone %s failed: %s", voice, e.detail)
            return ""
    cache = _load()
    cache["voices"][voice] = name
    _save(cache)
    log.info("vidu voice %s -> %s", voice, name)
    return name


BUILTIN = [  # (voice, 名字, 说明)
    ("Tina", "甜甜", "奶茶少女，甜美温暖"), ("Serena", "苏瑶", "温柔的年轻女性"), ("Maia", "四月", "知性又温柔"),
    ("Liora Mira", "清欢", "有生活温度的女声"), ("Mia", "舒然", "慢生活气质的女声"), ("Katerina", "卡捷琳娜", "成熟御姐"),
    ("Sunnybobi", "知芝", "随和的邻家女孩"), ("Momo", "茉兔", "搞笑逗趣的女生"), ("Cindy", "林欣宜", "软软的台湾腔女生"),
    ("Qiao", "小乔妹", "台湾腔甜妹"), ("Angel", "安琪", "带点台湾腔的女声"), ("Mione", "敏儿", "女声"),
    ("Jennifer", "詹妮弗", "女声"), ("Ethan", "晨煦", "带一点北方口音的标准普通话男声"), ("Theo Calm", "予安", "沉静治愈的男声"),
    ("Harvey", "厚", "低沉、有岁月感的男声"), ("Evan", "江晨", "男大学生"), ("Raymond", "林川野", "声音清亮的宅男"),
    ("Li Cassian", "李公公", "话少、会察言观色的男声"), ("Joyner", "阿逗", "搞笑接地气"), ("Wil", "伟伦", "深圳长大、带港味的青年"),
    ("Ryan", "甜茶", "有节奏感的男声"), ("Aiden", "艾登", "男声"),
    ("Sunny", "晴儿", "四川话女声"), ("Eric", "程川", "四川话男声"), ("Dylan", "晓东", "北京话男声"),
    ("Peter", "李彼得", "天津话男声"), ("Marcus", "秦川", "陕西话，话少憨厚的大叔"), ("Li", "老李", "南京话，爱唠叨的老大爷"),
    ("Joseph Chen", "阿樸伯", "闽南话老伯"), ("Kiki", "阿清", "粤语，甜甜的香港女生"), ("Rocky", "阿强", "粤语，风趣幽默的男声"),
    ("Sohee", "素熙", "温柔开朗的韩国女生"), ("Ono Anna", "小野杏", "调皮的青梅竹马"), ("Lenn", "莱恩", "理性里带点叛逆"),
    ("Sonrisa", "索尼莎", "热情的拉丁大姐姐"), ("Bodega", "博德加", "温暖的西班牙大叔"), ("Emilien", "埃米尔安", "浪漫的法国大哥哥"),
]
VOICES_JSON = VOICE_DIR / "voices.json"          # 音色库（id/name/group/desc），从合成服务的 presets.json 拷来
PROFILE_PROMPT = """看这张照片里的人，给"实时视频通话数字人"配人设和音色。
从下面的音色库里选一个最贴合这个人（性别、年龄、气质）的音色 id：
{voices}

只输出 JSON，不要别的文字：
{{"gender": "男/女", "age": 估计年龄数字, "look": "一句话描述外貌和气质（20 字内）",
  "name": "给这个角色起一个符合年龄、性别的中文称呼，如 王奶奶、李叔叔、小雨",
  "persona": "用第二人称写的人设，以"你是"开头，150 字以内：身份、年龄、性格、说话习惯，和照片的人相符，温暖自然",
  "voice": "音色库里的 id"}}"""


def _voices() -> list[dict]:
    try:
        return [v for v in json.loads(VOICES_JSON.read_text("utf-8")) if (VOICE_DIR / f"{v['id']}.wav").is_file()]
    except (OSError, ValueError):
        return []


def _voice_name(vid: str) -> str:
    return next((v["name"] for v in _voices() if v["id"] == vid), vid)


def _fallback_voice(gender: str, age: int) -> str:
    f = "女" in gender
    if age >= 60:
        return "vox_elder_f" if f else "vox_elder_m"
    if age >= 40:
        return "vox_f_aunt" if f else "vox_m_mature"
    if age >= 18:
        return "vox_f_gentle" if f else "vox_m_gentle"
    if age >= 12:
        return "vox_teen_girl" if f else "vox_teen_boy"
    return "vox_girl_kid" if f else "vox_boy_kid"


async def _auto_profile(img: bytes) -> dict:
    """看照片推断人设和音色（方舟多模态）；结果按图片内容缓存，同一张照片不重复调用。"""
    import llm_router
    from config import current_provider
    h = hashlib.sha1(img).hexdigest()[:20]
    cache = _load()
    if h in cache.setdefault("profiles", {}):
        return cache["profiles"][h]
    voices = _voices()
    lst = "\n".join(f"- {v['id']}：{v['name']}（{v.get('group', '')}）{v.get('desc', '')}" for v in voices
                     if v.get("group") != "方言")
    try:
        out = await llm_router.complete(current_provider(), [{"role": "user", "content": PROFILE_PROMPT.format(voices=lst)}],
                                        images=[img], max_tokens=800, temperature=0.3)
        d = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
    except Exception as e:  # noqa: BLE001
        log.warning("auto profile failed: %s", e)
        d = {}
    try:
        age = int(float(d.get("age") or 40))
    except (TypeError, ValueError):
        age = 40
    voice = str(d.get("voice") or "")
    if voice not in {v["id"] for v in voices}:
        voice = _fallback_voice(str(d.get("gender") or ""), age)
    persona = str(d.get("persona") or "").strip() or "你是一个亲切自然的人，正在和对方视频聊天。"
    prof = {"name": str(d.get("name") or ""), "look": str(d.get("look") or ""), "gender": str(d.get("gender") or ""),
            "age": age, "persona": persona, "voice": voice}
    cache = _load()
    cache.setdefault("profiles", {})[h] = prof
    _save(cache)
    log.info("auto profile %s: %s %s岁 voice=%s", h, prof["gender"], age, voice)
    return prof


def _image_bytes(aid: str, image: str) -> bytes:
    if image.startswith("data:image/"):
        return base64.b64decode(image.split(",", 1)[1])
    return _avatar_image(aid).read_bytes()


@router.get("/api/rt/presets")
async def presets() -> dict:
    try:
        return json.loads((PRESET_DIR / "presets.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {"bg": [], "outfit": []}


@router.get("/api/rt/presets/{kind}/{pid}.jpg")
async def preset_image(kind: str, pid: str) -> FileResponse:
    f = PRESET_DIR / re.sub(r"[^a-z]", "", kind) / f"{re.sub(r'[^A-Za-z0-9_-]', '', pid)}.jpg"
    if not f.is_file():
        raise HTTPException(404, "no such preset")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


KEEP = ("保持图1人物的脸、五官、皱纹、发型、头饰、肤色、年龄和神态完全不变，就是同一个人。"
        "构图和图1一致：胸口以上的近景，人物居中，正面看着镜头，人物在画面里的大小和图1相同。"
        "画面里不要出现书、眼镜、杯子、手机等多余物品，不要文字。真实照片质感，光线自然统一。")


def _preset_file(kind: str, pid: str) -> Path | None:
    f = PRESET_DIR / kind / f"{re.sub(r'[^A-Za-z0-9_-]', '', pid)}.jpg"
    return f if pid and f.is_file() else None


async def _compose(c: httpx.AsyncClient, person: bytes, bg: str, outfit: str) -> bytes:
    """通话前把照片换好背景/服装（参考生图，实测脸的相似度 0.86~0.94）。"""
    mime = "image/png" if person[:4] == b"\x89PNG" else "image/webp" if person[8:12] == b"WEBP" else "image/jpeg"
    imgs = [f"data:{mime};base64," + base64.b64encode(person).decode()]
    parts = ["图1中的人物"]
    fo, fb = _preset_file("outfit", outfit), _preset_file("bg", bg)
    if fo:
        imgs.append(_data_uri(fo))
        parts.append(f"穿上图{len(imgs)}的这件衣服（自然地穿在身上，衣服上不要吊牌和标签）")
    else:
        parts.append("衣服保持不变")
    if fb:
        imgs.append(_data_uri(fb))
        parts.append(f"背景换成图{len(imgs)}的场景（背景稍微虚化）")
    else:
        parts.append("背景保持不变")
    prompt = "，".join(parts) + "。" + KEEP
    j = await _api(c, "POST", "/ent/v2/reference2image", json={"model": "viduq2", "images": imgs, "prompt": prompt,
                                                                "aspect_ratio": "3:4"})
    tid = str(j.get("task_id") or "")
    for _ in range(90):
        await asyncio.sleep(3)
        q = await _api(c, "GET", f"/ent/v2/tasks/{tid}/creations")
        if q.get("state") == "success":
            return (await c.get(q["creations"][0]["url"], timeout=120)).content
        if q.get("state") == "failed":
            raise HTTPException(502, f"换装失败（{q.get('err_code') or '生成失败'}）")
    raise HTTPException(504, "换装超时")


@router.post("/api/rt/compose")
async def compose(request: Request) -> dict:
    """选了背景/服装 → 换好的照片（按 原照片+背景+服装 缓存；again=true 重新生成）。返回 key，通话时传 compose=key。"""
    body = await request.json()
    aid, image = str(body.get("avatar_id") or ""), str(body.get("image") or "")
    bg, outfit = str(body.get("bg") or ""), str(body.get("outfit") or "")
    if not (_preset_file("bg", bg) or _preset_file("outfit", outfit)):
        raise HTTPException(400, "没选背景或服装")
    person = _image_bytes(aid, image)
    key = hashlib.sha1(person + f"|{bg}|{outfit}".encode()).hexdigest()[:20]
    f = COMPOSED / f"{key}.jpg"
    if body.get("again") or not f.is_file():
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), trust_env=False) as c:
            out = await _compose(c, person, bg, outfit)
        COMPOSED.mkdir(parents=True, exist_ok=True)
        f.write_bytes(out)
        log.info("composed %s (avatar=%s bg=%s outfit=%s)", key, aid or "photo", bg or "-", outfit or "-")

        async def warm() -> None:                     # 顺手把这张图传成形象资产，开始通话时不用再等
            with contextlib.suppress(Exception):
                async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), trust_env=False) as c2:
                    async with _lock:
                        await _vidu_avatar(c2, f"composed:{key}")
        asyncio.get_running_loop().create_task(warm())
    return {"key": key, "url": f"/api/rt/composed/{key}.jpg"}


@router.get("/api/rt/composed/{key}.jpg")
async def composed_image(key: str) -> FileResponse:
    f = COMPOSED / f"{re.sub(r'[^0-9a-f]', '', key)}.jpg"
    if not f.is_file():
        raise HTTPException(404, "no such image")
    return FileResponse(f, media_type="image/jpeg")


@router.get("/api/rt/voices")
async def voices() -> list[dict]:
    """音色列表：our（我们的音色，首次用自动克隆过去）/ builtin（平台内置，值带 v: 前缀）/ mine（自己克隆的，值带 c: 前缀）。"""
    out = [{"id": x["id"], "name": f"{x['name']}（{x.get('group', '')}）", "group": "our"} for x in _voices() if x.get("group") != "方言"]
    out += [{"id": f"v:{v}", "name": f"{n}·{d}", "group": "builtin"} for v, n, d in BUILTIN]
    names = _load().get("custom_names", {})
    with contextlib.suppress(HTTPException, httpx.HTTPError):
        async with httpx.AsyncClient(timeout=15, trust_env=False) as c:
            got = await _api(c, "GET", "/live/v1/voices")
        for v in got.get("voices") or []:
            vid = str(v.get("voice") or "")
            if vid and not vid.startswith("t_"):          # t_ 开头的是我们自动克隆的音色库音色，不重复列
                out.append({"id": f"c:{vid}", "name": names.get(vid, vid), "group": "mine"})
    return out


@router.post("/api/rt/voices/clone")
async def clone_voice(audio: UploadFile = File(...), name: str = Form(""), text: str = Form("")) -> dict:
    """录一段 10~20 秒的声音（或上传音频）→ 克隆成自己的音色。浏览器录的 webm/mp4 先转成 16bit wav。"""
    raw = await audio.read()
    if len(raw) < 2000:
        raise HTTPException(400, "录音太短")
    if len(raw) > 30_000_000:
        raise HTTPException(400, "文件太大")
    p = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-i", "pipe:0", "-t", "30", "-ac", "1", "-ar", "24000",
                                             "-sample_fmt", "s16", "-f", "wav", "pipe:1",
                                             stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    wav, err = await p.communicate(raw)
    if p.returncode != 0 or len(wav) < 48000:      # 不到约 1 秒
        raise HTTPException(400, f"音频读不出来：{err.decode(errors='replace')[:120]}")
    vid = "u" + uuid.uuid4().hex[:12]
    body = {"audio_url": "data:audio/wav;base64," + base64.b64encode(wav).decode(), "voice": vid, "language": "zh",
            "provider": "qwen_omni"}
    if text.strip():
        body["text"] = text.strip()[:4000]
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), trust_env=False) as c:
        r = await _api(c, "POST", "/live/v1/voices/clone", json=body)
    vid = str(r.get("voice") or vid)
    cache = _load()
    cache.setdefault("custom_names", {})[vid] = (name.strip() or "我的声音")[:30]
    _save(cache)
    log.info("cloned custom voice %s (%s)", vid, name)
    return {"id": f"c:{vid}", "name": cache["custom_names"][vid]}


@router.delete("/api/rt/voices/{vid}")
async def delete_voice(vid: str) -> dict:
    vid = re.sub(r"^c:", "", vid)
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", vid) or vid.startswith("t_"):
        raise HTTPException(400, "只能删除自己克隆的音色")
    async with httpx.AsyncClient(timeout=20, trust_env=False) as c:
        await _api(c, "DELETE", f"/live/v1/voices/{vid}")
    cache = _load()
    cache.get("custom_names", {}).pop(vid, None)
    _save(cache)
    return {"ok": True}


@router.post("/api/rt/profile")
async def profile(request: Request) -> dict:
    """选了形象 / 上传了照片 → 人设和音色。形象本来就有固定人设（如奶奶·李秀兰）就用它，否则看照片自动配。"""
    body = await request.json()
    aid, image = str(body.get("avatar_id") or ""), str(body.get("image") or "")
    if aid:
        style, voice = _persona_of(_avatar_name(aid))
        if style:
            return {"persona": style.replace(f"\n{RULES}", "").strip(), "voice": voice, "voice_name": _voice_name(voice),
                    "source": "preset", "name": _avatar_name(aid)}
    if not aid and not image.startswith("data:image/"):
        raise HTTPException(400, "需要 avatar_id 或 image")
    p = await _auto_profile(_image_bytes(aid, image))
    return {**p, "voice_name": _voice_name(p["voice"]), "source": "auto"}


@router.post("/api/rt/live")
async def create_live(request: Request) -> dict:
    """建一次 Vidu S2 实时通话。body: {avatar_id?, image?(data URI，新上传的照片), persona?, voice?, uid?}"""
    body = await request.json()
    aid, image = str(body.get("avatar_id") or ""), str(body.get("image") or "")
    uid = re.sub(r"[^\w-]", "", str(body.get("uid") or ""))[:64] or "anon"
    if not aid and not image.startswith("data:image/"):
        raise HTTPException(400, "需要 avatar_id 或 image")
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), trust_env=False) as c:
        persona, our_voice = _persona_of(_avatar_name(aid)) if aid else ("", "")
        if not persona:                                 # 没有固定人设的形象 / 新照片：看照片自动配人设和音色
            p = await _auto_profile(_image_bytes(aid, image))
            persona, our_voice = f"{p['persona']}\n{RULES}", p["voice"]
        direct = ""
        sel = str(body.get("voice") or "")
        if sel in {v["id"] for v in _voices()}:
            our_voice = sel                             # 页面上手动换了我们的音色
        elif re.fullmatch(r"[vc]:[A-Za-z0-9_ ]{1,32}", sel):
            direct = sel[2:]                            # 平台内置 / 自己克隆的音色：直接用
        if str(body.get("persona") or "").strip():
            persona = f"{str(body['persona']).strip()}\n{RULES}"
        ck = re.sub(r"[^0-9a-f]", "", str(body.get("compose") or ""))
        async with _lock:                              # 资产上传/克隆缓存不并发写
            if ck and (COMPOSED / f"{ck}.jpg").is_file():
                av = await _vidu_avatar(c, f"composed:{ck}")
            else:
                av = await _vidu_avatar(c, aid) if aid else {"image_uri": image}
            voice = direct or await _vidu_voice(c, our_voice)
        req = {
            "model": str(body.get("model") or RT_MODEL), "call_mode": "video",
            "avatar": {**av, "persona": persona[:50000], "voice": voice,
                       "greeting_instruction": "用一句话亲切自然地打个招呼"},
            "audio": {"enable_transcription": True},     # WS 回字幕：type 9 用户说的、type 10 数字人说的
            "vad": {"type": "server", "silence_duration_ms": 500},
            "llm": {"max_tokens": 200, "temperature": 0.7},
            "idle_timeout_seconds": 300,
            "enable_recording": bool(body.get("record")),   # 内部测试用：结束后可下载整场录像检查画面
            "action": {"enabled": False},               # 不按对方的话匹配动作（避免"拿本书"就凭空变出书）
            "idle_motion": GENTLE_IDLE,
            "memory_retrieval": {"enabled": True, "provider": "vidu", "user_id": uid,
                                 "agent_id": hashlib.sha1((aid or "photo").encode()).hexdigest()[:32],
                                 "enable_write": True},
        }
        if our_voice in ("vox_elder_f", "vox_elder_m", "vox_grandma", "vox_grandpa"):
            req["idle_motion"] = ELDER_IDLE
        t0 = time.time()
        for i in range(30):
            try:
                d = await _api(c, "POST", "/live/s_avatar/realtime", json=req)
                break
            except HTTPException as e:                 # 形象资产还在生成：等一会再建（实测首次上传要 30 秒以上）
                if "still processing" not in str(e.detail) or i == 29:
                    raise
                await asyncio.sleep(3)
    live, rtc = d.get("live") or {}, d.get("rtc") or {}
    lid = str(live.get("id") or "")
    old = ACTIVE.get(uid)
    if old and old != lid and old in KILL:            # 这个人还有一个开着的会话（连点、刷新页面）：挂掉
        KILL[old].set()
        log.info("live %s replaces %s for uid %s", lid, old, uid)
    if lid:
        ACTIVE[uid] = lid
        KILL[lid] = asyncio.Event()
    log.info("vidu live %s created in %.1fs (avatar=%s voice=%s uid=%s)", live.get("id"), time.time() - t0, aid or "photo",
             voice or "-", uid)
    return {"live_id": str(live.get("id") or ""), "max_seconds": live.get("live_duration"),
            "rtc": {k: rtc.get(k) for k in ("app_id", "channel_id", "user_id", "token", "token_expire_at")}}


@router.get("/api/rt/lives/{live_id}/recording")
async def live_recording(live_id: str) -> dict:
    async with httpx.AsyncClient(timeout=20, trust_env=False) as c:
        return await _api(c, "GET", f"/live/v1/lives/{re.sub(r'[^0-9]', '', live_id)}/recording")


@router.get("/api/rt/lives/{live_id}")
async def live_status(live_id: str) -> dict:
    async with httpx.AsyncClient(timeout=20, trust_env=False) as c:
        d = await _api(c, "GET", f"/live/v1/lives/{re.sub(r'[^0-9]', '', live_id)}")
    lv = d.get("live") or {}
    return {k: lv.get(k) for k in ("id", "status", "billed_seconds", "credits_cost", "start_time", "end_time")}


@router.websocket("/api/rt/ws/{live_id}")
async def ws_proxy(ws: WebSocket, live_id: str) -> None:
    await ws.accept()
    live_id = re.sub(r"[^0-9]", "", live_id)
    cid = "talk-" + uuid.uuid4().hex[:10]
    seq = 0

    def frame(t: int, payload: dict) -> str:
        nonlocal seq
        seq += 1
        return json.dumps({"type": t, "live_id": live_id, "conn_id": cid, "seq_id": seq, "payload": payload},
                          ensure_ascii=False)

    try:
        up = await websockets.connect(f"wss://{HOST}/live/ws/live/connect?live_id={live_id}&conn_id={cid}",
                                      additional_headers={"Authorization": f"Token {_key()}"}, max_size=16 << 20)
    except Exception as e:  # noqa: BLE001
        log.warning("vidu ws connect %s failed: %s", live_id, e)
        await ws.send_text(json.dumps({"type": "error", "msg": "连不上实时通话服务的控制通道"}, ensure_ascii=False))
        await ws.close()
        return
    hung = False

    async def downlink() -> None:
        # 视频模式：Vidu 渲染端连回来之前 conn_init 会回 NOT_READY，隔 1.5 秒重发，最多约 60 秒
        nonlocal hung
        tries = 0
        async for m in up:
            await ws.send_text(m if isinstance(m, str) else m.decode("utf-8", "replace"))
            with contextlib.suppress(Exception):
                d = json.loads(m)
                if d.get("type") == 2:
                    ack = (d.get("payload") or {}).get("conn_init_ack") or {}
                    if not ack.get("success") and tries < 40:
                        tries += 1
                        await asyncio.sleep(1.5)
                        await up.send(frame(1, {"conn_init": {"version": 1}}))

                elif d.get("type") == 6:
                    hung = True

    async def uplink() -> None:
        nonlocal hung
        while True:
            msg = json.loads(await ws.receive_text())
            t = int(msg.get("type") or 0)
            if t == 99 and False:                       # 文字输入会直接驱动画面动作（实测"拿杯子"就真拿出杯子），页面不再提供，这里也不转发
                text = str(msg.get("text") or "").strip()[:2000]
                if text:
                    await up.send(frame(99, {"text_msg": {"msg_id": uuid.uuid4().hex[:12], "content": text,
                                                          "timestamp": int(time.time() * 1000)}}))
            elif t == 7:
                await up.send(frame(7, {}))

            elif t == 5:
                hung = True
                await up.send(frame(5, {"hangup": {"hangup_reason": "user_end"}}))
                return

    await up.send(frame(1, {"conn_init": {"version": 1}}))
    kill = KILL.setdefault(live_id, asyncio.Event())

    async def killed() -> None:                       # 同一个人开了新会话：这个挂掉
        await kill.wait()
        with contextlib.suppress(Exception):
            await ws.send_text(json.dumps({"type": 6, "payload": {"hangup": {"hangup_reason": "replaced"}}}))

    tasks = [asyncio.create_task(downlink()), asyncio.create_task(uplink()), asyncio.create_task(killed())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        if not hung:                                   # 页面关了/断网：主动挂断，别空跑计费
            with contextlib.suppress(Exception):
                await up.send(frame(5, {"hangup": {"hangup_reason": "user_end"}}))
                log.info("vidu live %s hung up (client gone)", live_id)
        KILL.pop(live_id, None)
        for u, l in list(ACTIVE.items()):
            if l == live_id:
                ACTIVE.pop(u, None)
        with contextlib.suppress(Exception):
            await up.close()
        with contextlib.suppress(Exception):
            await ws.close()
