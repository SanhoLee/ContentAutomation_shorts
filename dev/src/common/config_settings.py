"""/set-style runtime config settings for slack_bot.py.

The bot exposes per-job runtime overrides (models, research toggles,
TTS/caption/frame knobs). This module holds the table and its validation
logic, kept separate from the bot so the settings surface can be tested and
changed without touching transport code.
"""

from __future__ import annotations

import os

from script_runtime import speech_pace_profile

MODEL_ALIASES = {
    # Haiku 계열 (경량/저비용)
    "haiku": "claude-haiku-4-5-20251001",
    "haiku4.5": "claude-haiku-4-5-20251001",
    "haiku-4-5": "claude-haiku-4-5-20251001",

    # Sonnet 계열 (기본 스크립트 작업용)
    "sonnet": "claude-sonnet-4-6",
    "sonnet4.6": "claude-sonnet-4-6",
    "sonnet-4-6": "claude-sonnet-4-6",
    "sonnet5": "claude-sonnet-5",
    "sonnet-5": "claude-sonnet-5",
    "sonnet4.5": "claude-sonnet-4-5-20250929",
    "sonnet-4-5": "claude-sonnet-4-5-20250929",

    # Opus 계열 (상위 모델, 필요 시 최고품질 실험용)
    "opus": "claude-opus-4-8",
    "opus4.8": "claude-opus-4-8",
    "opus-4-8": "claude-opus-4-8",
    "opus4.7": "claude-opus-4-7",
    "opus-4-7": "claude-opus-4-7",
    "opus4.6": "claude-opus-4-6",
    "opus-4-6": "claude-opus-4-6",
    "opus4.5": "claude-opus-4-5-20251101",
    "opus-4-5": "claude-opus-4-5-20251101",

    # Fable 계열 (최신 최고성능 라인업 실험용)
    "fable": "claude-fable-5",
    "fable5": "claude-fable-5",
    "fable-5": "claude-fable-5",
}


def resolve_model_alias(value):
    """alias면 정식 모델 ID로 치환하고, alias가 아니면 입력값을 그대로 모델 ID로 사용한다."""
    raw = str(value).strip()
    return MODEL_ALIASES.get(raw.lower(), raw)


def env_value(key, default="-"):
    value = os.environ.get(key)
    return default if value in (None, "") else value


def positive_int(value, name):
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{name}은 양의 정수로 입력하세요: {value}")
    return text


def signed_int(value, name):
    text = str(value).strip()
    if text.startswith("-"):
        digits = text[1:]
    else:
        digits = text
    if not digits.isdigit():
        raise ValueError(f"{name}은 정수로 입력하세요: {value}")
    return text


def safe_caption_style(value):
    value = str(value).strip()
    if not value or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in value):
        raise ValueError(f"style은 영문/숫자/_/- 만 입력하세요: {value}")
    return value


def safe_choice(value, name, choices):
    value = str(value).strip()
    if value not in choices:
        raise ValueError(f"{name}은 {', '.join(choices)} 중 하나여야 합니다: {value}")
    return value


def positive_number(value, name):
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"{name}은 양수로 입력하세요: {value}")
    if number <= 0:
        raise ValueError(f"{name}은 양수로 입력하세요: {value}")
    return text


CONFIG_CATEGORIES = (
    ("models", "AI 모델", "스크립트·조사·전략·검색 모델"),
    ("research", "조사 / 검색", "웹 검색과 사례 조사"),
    ("channel", "채널 성과", "YouTube 실데이터 동기화와 판단 강도"),
    ("audio", "음성 / 영상 길이", "TTS 목소리·속도·목표 길이"),
    ("caption", "자막", "글자·여백·스타일·위치"),
    ("frame", "프레임 / B-roll", "화면 프레임·프리셋·채널명"),
    ("system", "시스템 (읽기 전용)", "실행 환경과 API 제한값"),
)

MODEL_CHOICES = (
    ("Haiku 4.5", "claude-haiku-4-5-20251001"),
    ("Sonnet 4.6", "claude-sonnet-4-6"),
    ("Sonnet 4.5", "claude-sonnet-4-5-20250929"),
    ("Opus 4.8", "claude-opus-4-8"),
)

CONFIG_INPUT_ALIASES = {
    "caption_style": "style",
    "frame_mode": "frame",
    "broll_fit_mode": "broll_fit",
    "speech_pace": "pace",
    "target_duration_sec": "duration",
    "web_research": "web",
    "case_research": "case",
    "feedback_strictness": "feedback_policy",
    "youtube_feedback_strictness": "feedback_policy",
    "youtube_feedback_auto_sync": "feedback_sync",
}

SYSTEM_CONFIG_FIELDS = (
    ("ENV_NAME", "-"),
    ("CLAUDE_MODEL", "claude-sonnet-4-6"),
    ("MAX_TOKENS", "2600"),
    ("CLAUDE_HTTP_RETRIES", "1"),
    ("PUBMED_RETMAX", "3"),
    ("PUBMED_ABSTRACT_CHAR_LIMIT", "7000"),
    ("LOG_LEVEL", "-"),
)


def _env_bool(value):
    return str(value).strip().lower() not in ("off", "0", "false", "no")


def build_config_settings():
    """Build the bot's CONFIG_SETTINGS table.

    A handful of settings take their default from a SLACK_DEFAULT_* env var,
    resolved here so the table has exactly one definition.
    """
    default_caption_font_size = os.environ.get("SLACK_DEFAULT_CAPTION_FONT_SIZE", "62")
    default_caption_margin_v = os.environ.get("SLACK_DEFAULT_CAPTION_MARGIN_V", "60")
    default_caption_margin_h = os.environ.get("SLACK_DEFAULT_CAPTION_MARGIN_H", "10")
    default_caption_style = os.environ.get("SLACK_DEFAULT_CAPTION_STYLE", os.environ.get("CAPTION_STYLE", "default"))
    default_web_research = _env_bool(os.environ.get("SLACK_DEFAULT_WEB_RESEARCH", "true"))

    settings = {}
    settings.update({
        "script_model": {"category": "models", "label": "스크립트 모델", "job_key": "claude_script_model", "env": "CLAUDE_SCRIPT_MODEL", "default": lambda job: env_value("CLAUDE_MODEL", "claude-sonnet-4-6"), "kind": "model", "choices": MODEL_CHOICES},
        "research_model": {"category": "models", "label": "조사 모델", "job_key": "claude_research_model", "env": "CLAUDE_RESEARCH_MODEL", "default": lambda job: effective_setting_value(job, "script_model", settings)[0], "kind": "model", "choices": MODEL_CHOICES},
        "strategy_model": {"category": "models", "label": "전략 모델", "job_key": "claude_strategy_model", "env": "CLAUDE_STRATEGY_MODEL", "default": "claude-haiku-4-5-20251001", "kind": "model", "choices": MODEL_CHOICES},
        "query_model": {"category": "models", "label": "검색어 모델", "job_key": "claude_query_model", "env": "CLAUDE_QUERY_MODEL", "default": lambda job: effective_setting_value(job, "strategy_model", settings)[0], "kind": "model", "choices": MODEL_CHOICES},
        "web": {"category": "research", "label": "웹 검색", "job_key": "web_research", "env": "ENABLE_WEB_RESEARCH", "default": default_web_research, "kind": "bool", "choices": (("켜기", True), ("끄기", False))},
        "case": {"category": "research", "label": "사례 조사", "job_key": "case_research", "env": "ENABLE_CASE_RESEARCH", "default": True, "kind": "bool", "choices": (("켜기", True), ("끄기", False))},
        "feedback_policy": {"category": "channel", "label": "판단 강도", "job_key": "youtube_feedback_strictness", "env": "YOUTUBE_FEEDBACK_STRICTNESS", "default": "balanced", "kind": "choice", "choices": (("느슨함", "loose"), ("중간", "balanced"), ("엄격함", "strict"))},
        "feedback_sync": {"category": "channel", "label": "생성 전 동기화", "job_key": "youtube_feedback_auto_sync", "env": "YOUTUBE_FEEDBACK_AUTO_SYNC", "default": True, "kind": "bool", "choices": (("켜기", True), ("끄기", False))},
        "voice": {"category": "audio", "label": "TTS 목소리", "job_key": "tts_voice", "env": "TTS_VOICE", "default": "M2", "kind": "voice", "choices": tuple((voice, voice) for voice in ("F1", "F2", "M1", "M2"))},
        "pace": {"category": "audio", "label": "말하기 속도", "job_key": "speech_pace", "env": "SPEECH_PACE", "default": "legacy", "kind": "pace", "choices": (("느리게", "slow"), ("보통", "normal"), ("빠르게", "fast"), ("매우 빠르게", "very_fast"))},
        "duration": {"category": "audio", "label": "목표 길이(초)", "job_key": "target_duration_sec", "env": "TARGET_DURATION_SEC", "default": "60", "kind": "positive_int"},
        "font_size": {"category": "caption", "label": "글자 크기", "job_key": "caption_font_size", "env": "CAPTION_FONT_SIZE", "default": default_caption_font_size, "kind": "positive_int"},
        "margin_v": {"category": "caption", "label": "세로 여백", "job_key": "caption_margin_v", "env": "CAPTION_MARGIN_V", "default": default_caption_margin_v, "kind": "positive_int"},
        "margin_h": {"category": "caption", "label": "가로 여백", "job_key": "caption_margin_h", "env": "CAPTION_MARGIN_H", "default": default_caption_margin_h, "kind": "positive_int"},
        "style": {"category": "caption", "label": "자막 스타일", "job_key": "caption_style", "env": "CAPTION_STYLE", "default": default_caption_style, "kind": "style", "choices": tuple((style, style) for style in ("default", "center-outline", "center-yellow", "center-white"))},
        "offset_x": {"category": "caption", "label": "가로 위치 보정", "job_key": "caption_offset_x", "env": "CAPTION_OFFSET_X", "default": "0", "kind": "signed_int"},
        "offset_y": {"category": "caption", "label": "세로 위치 보정", "job_key": "caption_offset_y", "env": "CAPTION_OFFSET_Y", "default": "0", "kind": "signed_int"},
        "frame": {"category": "frame", "label": "프레임 모드", "job_key": "frame_mode", "env": "FRAME_MODE", "default": "full", "kind": "choice", "choices": (("전체 화면", "full"), ("상하 프레임", "framed"))},
        "broll_fit": {"category": "frame", "label": "B-roll 맞춤", "job_key": "broll_fit_mode", "env": "BROLL_FIT_MODE", "default": "cover", "kind": "choice", "choices": (("채우기", "cover"), ("원본 유지", "contain"), ("블러 여백", "blur-contain"))},
        "top_preset": {"category": "frame", "label": "상단 프리셋", "job_key": "frame_top_preset", "env": "FRAME_TOP_PRESET", "default": "default", "kind": "style", "choices": (("default", "default"), ("brain50", "brain50"))},
        "bottom_preset": {"category": "frame", "label": "하단 프리셋", "job_key": "frame_bottom_preset", "env": "FRAME_BOTTOM_PRESET", "default": "default", "kind": "style", "choices": (("default", "default"), ("minimal", "minimal"))},
        "top_pct": {"category": "frame", "label": "상단 높이(%)", "job_key": "frame_top_pct", "env": "FRAME_TOP_PCT", "default": "preset", "kind": "positive_number"},
        "bottom_pct": {"category": "frame", "label": "하단 높이(%)", "job_key": "frame_bottom_pct", "env": "FRAME_BOTTOM_PCT", "default": "preset", "kind": "positive_number"},
        "channel": {"category": "frame", "label": "하단 채널명", "job_key": "frame_bottom_channel_name", "env": "FRAME_BOTTOM_CHANNEL_NAME", "default": "브레인피프티", "kind": "text"},
        "header": {"category": "frame", "label": "상단 제목", "job_key": "frame_header_text", "env": "FRAME_HEADER_TEXT", "default": "자동 생성", "kind": "text"},
    })
    return settings


def effective_setting_value(job, setting_id, settings_table):
    setting = settings_table[setting_id]
    job_key = setting["job_key"]
    if job_key in job:
        return job[job_key], "작업 override"
    env_name = setting.get("env")
    env_value_raw = os.environ.get(env_name) if env_name else None
    if env_value_raw not in (None, ""):
        value = _env_bool(env_value_raw) if setting["kind"] == "bool" else env_value_raw
        return value, "환경 설정"
    default = setting.get("default", "-")
    if callable(default):
        default = default(job)
    return default, "기본값"


def display_setting_value(value):
    if isinstance(value, bool):
        return "켜짐" if value else "꺼짐"
    return str(value) if value not in (None, "") else "(비어 있음)"


def validate_setting_value(setting_id, value, settings_table):
    setting = settings_table[setting_id]
    kind = setting["kind"]
    if kind == "positive_int":
        return positive_int(value, setting_id)
    if kind == "signed_int":
        return signed_int(value, setting_id)
    if kind == "positive_number":
        return positive_number(value, setting_id)
    if kind == "pace":
        return speech_pace_profile(value)[0]
    if kind == "model":
        return resolve_model_alias(value)
    if kind == "style":
        return safe_caption_style(value)
    if kind == "voice":
        text = str(value).strip().upper()
        allowed = tuple(choice_value for _, choice_value in setting.get("choices", ()))
        if text not in allowed:
            raise ValueError(f"voice은 {', '.join(allowed)} 중 하나여야 합니다: {value}")
        return text
    if kind == "bool":
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered not in ("on", "off", "true", "false", "1", "0", "yes", "no"):
            raise ValueError(f"{setting_id}은 on 또는 off로 입력하세요: {value}")
        return lowered in ("on", "true", "1", "yes")
    if kind == "choice":
        allowed = tuple(choice_value for _, choice_value in setting.get("choices", ()))
        return safe_choice(value, setting_id, allowed)
    text = str(value).strip()
    if not text:
        raise ValueError(f"{setting_id}은 빈 값으로 설정할 수 없습니다.")
    return text


def set_config_value(job, setting_id, value, settings_table):
    setting = settings_table[setting_id]
    validated = validate_setting_value(setting_id, value, settings_table)
    job[setting["job_key"]] = validated
    return validated
