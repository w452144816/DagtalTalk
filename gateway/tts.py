"""语音合成：给 OpenTalking 一个 OpenAI 兼容的 /v1/audio/speech（response_format=pcm，流式 16k s16le）。

- 音色 ID 是 edge 的 ShortName（zh-CN-xxxNeural）→ edge-tts（免费，首包 ~2s）
- 配了 LOCAL_TTS_URL（本地合成服务 tts/server.py，VoxCPM2，多路并发）：clone_ / vox_ 开头 → 本地
  （vox_ 是用文字描述生成的预设音色，clone_ 是录音克隆）；没配时 clone_ → 网关进程内的 Qwen3-TTS（clone_tts.py，单路串行）
- 其余 → MiniMax T2A 系统音色（首包 ~0.15s）
- 出错时退回 edge 默认音色，不让对话哑掉
- 克隆：录音/上传 → 截前 20 秒存 24k wav，SenseVoice 识别出参考文本，记在 state/voices.json
  （MiniMax 这个 key 是 token 套餐，voice_clone 接口报不支持，所以克隆走本地）
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import AsyncIterator

import httpx

import clone_tts
import llm_router
import personas
from config import LLMS, STATE_FILE

log = logging.getLogger("tts")
MM = "https://api.minimaxi.com/v1"
SAMPLE_RATE = 16000
TTS_MODEL = "speech-2.8-turbo"
EDGE_FALLBACK = "zh-CN-XiaoxiaoNeural"
VOICES_FILE = STATE_FILE.parent / "voices.json"
VOICES_DIR = STATE_FILE.parent / "voices"
TTS_URL = "http://127.0.0.1:8301/v1/audio/speech"   # 合成都在 8301 进程（克隆模型只加载一份）
PREVIEW_DIR = Path("/w/cache/voice_preview")
PREVIEW_TEXT = "你好呀，很高兴认识你，今天想聊点什么？"

# (分组, [(音色 ID, 名称)])，MiniMax 系统音色按年龄段挑的普通话音色
VOICE_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("儿童", [("lovely_girl", "萌萌女童"), ("cute_boy", "可爱男童"), ("clever_boy", "聪明男童")]),
    ("少年", [("Chinese (Mandarin)_Crisp_Girl", "清脆少女"), ("Chinese (Mandarin)_Warm_Girl", "温暖少女"),
              ("Chinese (Mandarin)_Straightforward_Boy", "率真弟弟"),
              ("Chinese (Mandarin)_Pure-hearted_Boy", "清澈邻家弟弟")]),
    ("青年女", [("Chinese (Mandarin)_Warm_Bestie", "温暖闺蜜"), ("Chinese (Mandarin)_Sweet_Lady", "甜美女声"),
               ("Chinese (Mandarin)_Gentle_Senior", "温柔学姐"), ("female-yujie", "御姐")]),
    ("青年男", [("Chinese (Mandarin)_Gentle_Youth", "温润青年"), ("Chinese (Mandarin)_Sincere_Adult", "真诚青年"),
               ("male-qn-jingying", "精英青年"), ("Chinese (Mandarin)_Radio_Host", "电台男主播"),
               ("male-qn-badao", "霸道青年"), ("Chinese (Mandarin)_Stubborn_Friend", "嘴硬竹马")]),
    ("中年", [("Chinese (Mandarin)_Wise_Women", "阅历姐姐"), ("Chinese (Mandarin)_News_Anchor", "新闻女声"),
              ("Chinese (Mandarin)_Kind-hearted_Antie", "热心大婶"),
              ("Chinese (Mandarin)_Reliable_Executive", "沉稳高管"), ("Chinese (Mandarin)_Male_Announcer", "播报男声")]),
    ("老年", [("Chinese (Mandarin)_Kind-hearted_Elder", "花甲奶奶"), ("Chinese (Mandarin)_Humorous_Elder", "搞笑大爷")]),
]
EDGE_GROUP = ("免费备用（edge，较慢）", [("zh-CN-XiaoxiaoNeural", "晓晓（女）"), ("zh-CN-YunxiNeural", "云希（男）"),
                                     ("zh-CN-YunxiaNeural", "云夏（男童）"), ("zh-CN-YunjianNeural", "云健（男）")])
DEFAULT_VOICE = "Chinese (Mandarin)_Warm_Bestie"

# 合成池：LOCAL_TTS_URLS 逗号分隔，第一个是本机（优先），其余是别的机器的 talk-tts（本机满了 / 挂了才用）。
# LOCAL_TTS_URL（单个）仍兼容，等于只有本机。每台 talk-tts 的并发上限 LOCAL_TTS_MAX（205 的 3090 配 32 路，206 的 TITAN V 4 路）
LOCAL_TTS_URLS = [u.strip().rstrip("/") for u in os.getenv("LOCAL_TTS_URLS", os.getenv("LOCAL_TTS_URL", "")).split(",") if u.strip()]
LOCAL_TTS_URL = LOCAL_TTS_URLS[0] if LOCAL_TTS_URLS else ""
LOCAL_TTS_MAX = int(os.getenv("LOCAL_TTS_MAX", "0"))          # 0 = 不限（永远用本机）
_inflight: dict[str, int] = {u: 0 for u in LOCAL_TTS_URLS}
_down_until: dict[str, float] = {}


def pick_tts() -> str:
    """选合成服务：本机没满就本机；满了选别的机器里最闲的；都挂了还是回本机（让它报错走兜底）。"""
    now = time.time()
    ok = [u for u in LOCAL_TTS_URLS if _down_until.get(u, 0) <= now]
    if not ok:
        return LOCAL_TTS_URL
    local = LOCAL_TTS_URLS[0]
    if local in ok and (not LOCAL_TTS_MAX or _inflight[local] < LOCAL_TTS_MAX):
        return local
    return min(ok, key=lambda u: (_inflight[u], u != local))
LOCAL_DEFAULT = "vox_f_gentle"
# 人设推荐的 MiniMax 音色 → 本地预设里最接近的
LOCAL_MAP = {
    "Chinese (Mandarin)_Warm_Bestie": "vox_f_gentle", "female-yujie": "vox_f_yujie",
    "Chinese (Mandarin)_Crisp_Girl": "vox_teen_girl", "Chinese (Mandarin)_Stubborn_Friend": "vox_m_sunny",
    "male-qn-badao": "vox_m_mature", "Chinese (Mandarin)_Sincere_Adult": "vox_m_sunny",
    "Chinese (Mandarin)_Kind-hearted_Elder": "vox_grandma", "Chinese (Mandarin)_Humorous_Elder": "vox_grandpa",
    "lovely_girl": "vox_girl_kid", "Chinese (Mandarin)_Reliable_Executive": "vox_m_mature",
    "Chinese (Mandarin)_Wise_Women": "vox_f_gentle", "Chinese (Mandarin)_News_Anchor": "vox_f_anchor",
    "Chinese (Mandarin)_Radio_Host": "vox_m_radio", "male-qn-jingying": "vox_m_gentle",
    "Chinese (Mandarin)_Gentle_Youth": "vox_m_gentle",
}
_local_cache: tuple[float, list[dict]] = (0.0, [])
# 本地合成（VoxCPM2）支持的方言。音色 ID 带后缀 `@方言`（如 clone_xxx@四川话）→ 本地服务按「参考音色 + 用某某话说」合成
DIALECTS = ["粤语", "四川话", "上海话", "闽南话", "东北话", "天津话", "河南话", "陕西话", "山东话"]


def split_voice(voice: str) -> tuple[str, str]:
    """`clone_xxx@四川话` → (`clone_xxx`, `四川话`)；没有后缀 → 方言为空。"""
    base, _, dialect = voice.partition("@")
    return base, dialect if dialect in DIALECTS else ""


def _key() -> str:
    return LLMS["minimax"].api_key


def ready() -> bool:
    return bool(_key())


def local_voices() -> list[dict]:
    """本地合成服务里注册的音色（预设 + 克隆），缓存 30 秒；服务不在时返回空。"""
    global _local_cache
    if not LOCAL_TTS_URL:
        return []
    if time.time() - _local_cache[0] < 30 and _local_cache[1]:
        return _local_cache[1]
    try:
        items = httpx.get(f"{LOCAL_TTS_URL}/voices", timeout=2, trust_env=False).json()
    except (httpx.HTTPError, ValueError):
        items = []
    _local_cache = (time.time(), items)
    return items


def invalidate_local() -> None:
    global _local_cache
    _local_cache = (0.0, [])


def local_ready() -> bool:
    return bool(local_voices())


def clone_ready() -> bool:
    return local_ready() or clone_tts.available()


def default_voice() -> str:
    if local_ready():
        return LOCAL_DEFAULT
    return DEFAULT_VOICE if ready() else EDGE_FALLBACK


def map_voice(v: str) -> str:
    """人设推荐音色：用本地合成时换成本地预设。"""
    return LOCAL_MAP.get(v, v) if local_ready() else v


def is_local(voice: str) -> bool:
    return bool(LOCAL_TTS_URL) and split_voice(voice)[0].startswith(("clone_", "vox_"))


def is_edge(voice: str) -> bool:
    return bool(re.fullmatch(r"[a-z]{2}-[A-Z]{2}(-[a-z]+)?-\w+Neural", voice))


# ---------- 克隆音色登记 ----------

def cloned() -> list[dict]:
    try:
        return json.loads(VOICES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_cloned(items: list[dict]) -> None:
    VOICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = VOICES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(VOICES_FILE)


def voice_groups() -> list[dict]:
    groups = []
    local = local_voices()
    if local:
        mine = [(v["id"], v["name"]) for v in local if v["id"].startswith("clone_")]
        if mine:
            groups.append(("我的克隆音色", mine))
        labels: list[str] = []
        for v in local:
            if v["id"].startswith("vox_") and v["group"] not in labels:
                labels.append(v["group"])
        groups += [(f"本地·{g}", [(v["id"], v["name"]) for v in local
                                 if v["id"].startswith("vox_") and v["group"] == g]) for g in labels]
    else:
        mine = [(v["id"], v["name"]) for v in cloned()] if clone_tts.available() else []
        if mine:
            groups.append(("我的克隆音色", mine))
    if ready():
        groups += [(f"MiniMax·{g}" if local else g, vs) for g, vs in VOICE_GROUPS]
    groups.append(EDGE_GROUP)
    dialect = {v["id"]: v["dialect"] for v in local if v.get("dialect")}   # 方言预设自带的方言，页面选中时联动
    return [{"label": g, "voices": [{"id": i, "label": n, **({"dialect": dialect[i]} if i in dialect else {})}
                                    for i, n in vs]} for g, vs in groups]


async def _run(cmd: list[str], data: bytes) -> bytes:
    p = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate(data)
    if p.returncode:
        raise RuntimeError(err.decode(errors="ignore")[-300:])
    return out


def _ref_of(vid: str) -> tuple[str, str] | None:
    for v in cloned():
        if v["id"] == vid:
            return str(VOICES_DIR / f"{vid}.wav"), v["ref_text"]
    return None


async def clone(name: str, audio: bytes, transcribe) -> dict:
    """录音（任意格式）→ 前 20 秒 24k 单声道 wav + SenseVoice 识别的参考文本。"""
    global _local_cache
    # 去掉开头静音、截 20 秒；参考音频太长反而拖慢每次合成
    flt = "silenceremove=start_periods=1:start_threshold=-45dB,atrim=0:20"
    pcm16 = await _run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-af", flt, "-ac", "1", "-ar", "16000",
                        "-f", "s16le", "pipe:1"], audio)
    dur = len(pcm16) / 32000
    if dur < 5:
        raise ValueError(f"有效录音只有 {dur:.0f} 秒，至少要 5 秒（建议 10 秒以上）")
    text, _ = await asyncio.to_thread(transcribe, pcm16)
    if len(text) < 4:
        raise ValueError("没听清录音里说了什么，换个安静的环境再录一次")
    vid = f"clone_{int(time.time() * 1000):x}"
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    await _run(["ffmpeg", "-v", "error", "-y", "-i", "pipe:0", "-af", flt, "-ac", "1", "-ar", "24000",
                str(VOICES_DIR / f"{vid}.wav")], audio)
    item = {"id": vid, "name": name, "ref_text": text, "seconds": round(dur, 1), "created": int(time.time())}
    _save_cloned([item] + cloned())
    if LOCAL_TTS_URL:   # 本地合成服务挂载了同一个 state 目录，路径一致
        async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
            r = await c.post(f"{LOCAL_TTS_URL}/voices/register", json={
                "id": vid, "name": name, "ref_text": text, "wav": str(VOICES_DIR / f"{vid}.wav")})
            r.raise_for_status()
        _local_cache = (0.0, [])
    log.info("cloned voice %s (%s) from %.1fs: %r", vid, name, dur, text)
    return item


async def delete_clone(vid: str) -> None:
    global _local_cache
    _save_cloned([v for v in cloned() if v["id"] != vid])
    if LOCAL_TTS_URL:
        with contextlib.suppress(httpx.HTTPError):
            async with httpx.AsyncClient(timeout=10, trust_env=False) as c:
                await c.delete(f"{LOCAL_TTS_URL}/voices/{vid}")
        _local_cache = (0.0, [])
    (VOICES_DIR / f"{vid}.wav").unlink(missing_ok=True)
    for p in PREVIEW_DIR.glob(f"{vid}.*"):
        p.unlink(missing_ok=True)


# ---------- 合成 ----------

async def _minimax_pcm(text: str, voice: str, model: str) -> AsyncIterator[bytes]:
    body = {"model": model, "text": text, "stream": True,
            "voice_setting": {"voice_id": voice, "speed": 1.0, "vol": 1.0, "pitch": 0},
            "audio_setting": {"sample_rate": SAMPLE_RATE, "format": "pcm", "channel": 1}}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), trust_env=False) as c:
        async with c.stream("POST", f"{MM}/t2a_v2", headers={"Authorization": f"Bearer {_key()}"}, json=body) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"MiniMax TTS {r.status_code}: {(await r.aread())[:200]!r}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    if line.startswith("{"):    # 非流式错误体
                        br = json.loads(line).get("base_resp") or {}
                        raise RuntimeError(f"MiniMax TTS: {br.get('status_msg')}")
                    continue
                d = json.loads(line[5:])
                br = d.get("base_resp") or {}
                if br.get("status_code"):
                    raise RuntimeError(f"MiniMax TTS: {br.get('status_msg')}")
                data = d.get("data") or {}
                if data.get("status") == 1 and data.get("audio"):   # status=2 那条是整段汇总，跳过
                    yield bytes.fromhex(data["audio"])


# 老人音色合成后再保音高放慢（ffmpeg atempo，WSOLA）：七十多岁老人自然语速约 3~3.5 字/秒，
# 合成出来 4.1~4.4 字/秒，指令压不下去 → 0.85 倍落到 3.5 左右。流式：边收边变速，首包只多几十毫秒
ELDER_TEMPO = float(os.getenv("ELDER_TEMPO", "0.9"))   # 实测 0.85 → 3.2 字/秒偏慢，0.9 约 3.4


async def _tempo(src: AsyncIterator[bytes], factor: float) -> AsyncIterator[bytes]:
    p = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                                             "-i", "pipe:0", "-af", f"atempo={factor}", "-f", "s16le",
                                             "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1",
                                             stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)

    async def feed() -> None:
        try:
            async for b in src:
                p.stdin.write(b)
                await p.stdin.drain()
        finally:
            p.stdin.close()

    task = asyncio.create_task(feed())
    try:
        odd = b""
        while chunk := await p.stdout.read(3200):
            chunk, odd = odd + chunk, b""
            if len(chunk) % 2:
                chunk, odd = chunk[:-1], chunk[-1:]
            if chunk:
                yield chunk
        await task                                   # 合成出错要抛出来（走 edge 兜底）
    finally:
        task.cancel()
        if p.returncode is None:
            p.kill()


async def _edge_pcm(text: str, voice: str) -> AsyncIterator[bytes]:
    import edge_tts
    p = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-f", "mp3", "-i", "pipe:0", "-f", "s16le",
                                             "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1",
                                             stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)

    async def feed() -> None:
        try:
            async for ch in edge_tts.Communicate(text, voice).stream():
                if ch["type"] == "audio":
                    p.stdin.write(ch["data"])
                    await p.stdin.drain()
        finally:
            p.stdin.close()

    task = asyncio.create_task(feed())
    try:
        while chunk := await p.stdout.read(6400):
            yield chunk
        await task
    finally:
        task.cancel()
        if p.returncode is None:
            p.kill()


async def _local_pcm(text: str, voice: str, emo_text: str = "") -> AsyncIterator[bytes]:
    base, dialect = split_voice(voice)
    body = {"input": text, "voice": base, "response_format": "pcm"}
    style = [f"用{dialect}说"] if dialect else []
    emo = llm_router.emotion_for(emo_text or text)   # LLM 给这句标的情绪（见 llm_router._TagFilter），按原文匹配
    if emo and personas.EMOTIONS.get(emo):
        style.append(personas.EMOTIONS[emo])
    if base.startswith("vox_elder") and emo not in ("开心", "笑", "惊讶", "着急"):
        style.append("像七十多岁的老人说话，语速很慢，字字清楚")   # 指令只能慢一点（实测 4.4→4.1 字/秒），真正放慢靠下面的 atempo
    if style:
        body["style"] = "，".join(style)
    if emo:
        log.info("tts emotion [%s] -> %s: %s", emo, body.get("style", "-"), text[:30])
    tried: set[str] = set()
    while True:
        url = pick_tts()
        if url in tried:                     # 能选的都试过了
            raise RuntimeError("no tts backend reachable")
        tried.add(url)
        if url != LOCAL_TTS_URL:
            log.info("tts -> %s (local inflight %d)", url, _inflight.get(LOCAL_TTS_URL, 0))
        _inflight[url] = _inflight.get(url, 0) + 1
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=3), trust_env=False) as c:
                try:
                    cm = c.stream("POST", f"{url}/v1/audio/speech", json=body)
                    r = await cm.__aenter__()
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    _down_until[url] = time.time() + 15          # 连不上：15 秒内不再选它，换下一个
                    log.warning("tts %s unreachable, trying another", url)
                    continue
                try:
                    if r.status_code >= 400:
                        raise RuntimeError(f"local tts {r.status_code}: {(await r.aread())[:200]!r}")
                    odd = b""
                    async for b in r.aiter_bytes():
                        b, odd = odd + b, b""
                        if len(b) % 2:          # s16le 按样本对齐再往下传
                            b, odd = b[:-1], b[-1:]
                        if b:
                            yield b
                    return
                finally:
                    await cm.__aexit__(None, None, None)
        finally:
            _inflight[url] -= 1


# LLM 偶尔夹英文（"APP""OK""WiFi"），中文音色念起来很怪：常见缩写换成中文念法；其余 2~5 个大写字母的缩写拆开按字母念；
# 普通英文单词（首字母大写或小写）保留，模型本身会读英文。字幕用的是原文，这里只改送合成的文本
_EN_MAP = {
    "APP": "应用", "App": "应用", "app": "应用", "OK": "好", "ok": "好", "Ok": "好", "AI": "人工智能", "WiFi": "无线网", "WIFI": "无线网",
    "wifi": "无线网", "GPS": "导航", "PPT": "幻灯片", "CEO": "总裁", "VIP": "贵宾", "iPhone": "苹果手机", "iPad": "平板",
    "KTV": "卡拉OK厅", "ATM": "取款机", "DIY": "自己动手做", "NBA": "美职篮", "USB": "优盘接口", "PDF": "文档", "ID": "账号",
    "QQ": "扣扣", "VR": "虚拟现实", "AR": "增强现实", "HR": "人事", "B站": "哔哩哔哩", "TV": "电视", "SPA": "水疗",
}
_EN_RE = re.compile(r"[A-Za-z][A-Za-z&']*")


_BRACKET_TAG = re.compile(r"\[[一-鿿]{1,6}\]|【[一-鿿]{1,6}】")


def normalize_for_tts(text: str) -> str:
    text = _BRACKET_TAG.sub("", text)              # 兜底：漏网的 [轻声] 这类标签不念出来
    def rep(m: re.Match) -> str:
        w = m.group(0)
        if w in _EN_MAP:
            return _EN_MAP[w]
        if w.isupper() and 2 <= len(w) <= 5:      # 没见过的缩写：拆成字母念，避免被当成一个怪词
            return " ".join(w)
        return w
    out = _EN_RE.sub(rep, text)
    if out != text:
        log.info("tts normalize: %r -> %r", text[:40], out[:40])
    return out


async def speech_pcm(text: str, voice: str, model: str = "") -> AsyncIterator[bytes]:
    voice = voice or default_voice()
    emo_text = text                        # 情绪按原文匹配（llm_router 记的是原文）
    text = normalize_for_tts(text)
    if is_local(voice):
        sent = False
        try:
            src = _local_pcm(text, voice, emo_text)
            if split_voice(voice)[0].startswith("vox_elder") and ELDER_TEMPO < 0.999:
                src = _tempo(src, ELDER_TEMPO)
            async for b in src:
                sent = True
                yield b
        except Exception as e:  # noqa: BLE001
            log.warning("local tts failed (%s), fallback edge: %s", voice, e)
            if not sent:
                async for b in _edge_pcm(text, EDGE_FALLBACK):
                    yield b
        return
    if voice.startswith("clone_"):
        ref = _ref_of(voice)
        sent = False
        try:
            if ref is None:
                raise RuntimeError("克隆音色不存在")
            async for b in clone_tts.stream_pcm16k(text, *ref):
                sent = True
                yield b
        except Exception as e:  # noqa: BLE001
            log.warning("clone tts failed (%s), fallback edge: %s", voice, e)
            if not sent:
                async for b in _edge_pcm(text, EDGE_FALLBACK):
                    yield b
        return
    if is_edge(voice) or not ready():
        async for b in _edge_pcm(text, voice if is_edge(voice) else EDGE_FALLBACK):
            yield b
        return
    sent = False
    try:
        async for b in _minimax_pcm(text, voice, model or TTS_MODEL):
            sent = True
            yield b
    except Exception as e:  # noqa: BLE001
        log.warning("minimax tts failed (%s), fallback edge: %s", voice, e)
        if not sent:
            async for b in _edge_pcm(text, EDGE_FALLBACK):
                yield b


async def preview_mp3(voice: str) -> Path:
    """试听：合成一句固定的话，缓存成 mp3。"""
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out = PREVIEW_DIR / (re.sub(r"[^\w.-]", "_", voice) + ".mp3")
    if not out.exists():
        async with httpx.AsyncClient(timeout=120, trust_env=False) as c:
            r = await c.post(TTS_URL, json={"input": PREVIEW_TEXT, "voice": voice, "response_format": "pcm"})
            r.raise_for_status()
            pcm = r.content
        out.write_bytes(await _run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                                    "-i", "pipe:0", "-b:a", "64k", "-f", "mp3", "pipe:1"], pcm))
    return out
