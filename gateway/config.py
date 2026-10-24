"""网关配置：全部来自环境变量（容器里由 compose 的 env_file 注入）。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProviderConfig:
    key: str
    label: str
    protocol: str            # openai | anthropic
    api_key: str
    base_url: str
    model: str
    proxy: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.api_key)


PROXY = os.getenv("LLM_PROXY", "").strip()

LLMS: dict[str, ProviderConfig] = {
    "ark": ProviderConfig("ark", "豆包", "openai", os.getenv("ARK_API_KEY", ""),
                          os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/plan/v3"),
                          os.getenv("ARK_MODEL", "doubao-seed-2-0-mini-260215"), PROXY),   # 首字 ~0.4s，pro 要 1~1.4s
    "minimax": ProviderConfig("minimax", "MiniMax", "anthropic", os.getenv("MINIMAX_API_KEY", ""),
                              os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/anthropic"),
                              os.getenv("MINIMAX_MODEL", "MiniMax-M3"), PROXY),
}

OT_URL = os.getenv("OT_URL", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_PROVIDER = os.getenv("LLM_DEFAULT", "ark")
# 用照片建形象时复制哪个内置形象的配置（只借它的 manifest 骨架，画面全换成上传的照片）
BASE_AVATAR = os.getenv("BASE_AVATAR", "office-woman")
AVATAR_MODEL = os.getenv("AVATAR_MODEL", "quicktalk")
# 页面上可切换的渲染模型（评估用）：quicktalk=只换嘴；fasterliveportrait=整脸音频驱动（要 OmniRT）；flashhead=整头生成（要 FlashHead 服务）
RENDER_MODELS = [m.strip() for m in os.getenv("RENDER_MODELS", AVATAR_MODEL).split(",") if m.strip()]
# 照片 → 待机动作视频（眨眼/点头/手势），用 MiniMax 的 key
VIDEO_MODEL = os.getenv("MINIMAX_VIDEO_MODEL", "MiniMax-H3-Max")
STATE_FILE =Path(os.getenv("STATE_FILE", "/w/state/gateway.json"))
WEB_DIR = Path(__file__).resolve().parent / "web"

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "你是一个正在和用户视频通话的数字人。用口语化的中文简短回答，每次一到三句话，第一句话要短并用句号断句，"
    "像真人聊天一样自然，不要列清单，不要说自己是AI模型。",
)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    tmp.replace(STATE_FILE)


def current_provider() -> str:
    p = load_state().get("provider") or DEFAULT_PROVIDER
    return p if p in LLMS else DEFAULT_PROVIDER
