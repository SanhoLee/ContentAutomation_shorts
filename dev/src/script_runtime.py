import os
from dataclasses import dataclass

FALSE_VALUES = {"0", "false", "off", "no", "n"}


def env_bool(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() not in FALSE_VALUES


def env_csv(name, default=()):
    value = os.environ.get(name)
    if value in (None, ""):
        return tuple(default)
    return tuple(part.strip() for part in value.split(",") if part.strip())


def env_int(name, default):
    return int(os.environ.get(name, str(default)))


def env_float(name, default):
    return float(os.environ.get(name, str(default)))


def script_length_targets(target_duration_sec, atempo, chars_per_sec):
    total = int(target_duration_sec * atempo * chars_per_sec)
    prompt_target = int(total * 1.15)
    min_scenes = max(8, prompt_target // 28)
    return total, prompt_target, min_scenes


@dataclass(frozen=True)
class ScriptRuntimeSettings:
    work_dir: str
    atempo: float
    target_duration_sec: int
    chars_per_sec: float
    trend_candidate_count: int
    request_timeout: int
    claude_timeout: int
    claude_http_retries: int
    pubmed_query_timeout: int
    pubmed_retmax: int
    pubmed_abstract_char_limit: int
    claude_model: str
    claude_script_model: str
    claude_query_model: str
    claude_strategy_model: str
    claude_strategy_fallback_models: tuple[str, ...]
    max_tokens: int
    enable_web_research: bool
    web_research_timeout: int
    strategy_path: str
    insights_path: str
    total_chars: int
    prompt_target_chars: int
    min_scenes_estimate: int


def load_runtime_settings():
    work_dir = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
    data_dir = os.path.normpath(os.path.join(work_dir, ".."))
    atempo = env_float("ATEMPO", "1.0")
    target_duration_sec = env_int("TARGET_DURATION_SEC", 60)
    chars_per_sec = env_float("CHARS_PER_SEC", "4.66")
    total_chars, prompt_target_chars, min_scenes_estimate = script_length_targets(
        target_duration_sec,
        atempo,
        chars_per_sec,
    )
    claude_model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    return ScriptRuntimeSettings(
        work_dir=work_dir,
        atempo=atempo,
        target_duration_sec=target_duration_sec,
        chars_per_sec=chars_per_sec,
        trend_candidate_count=env_int("TREND_CANDIDATE_COUNT", 5),
        request_timeout=env_int("REQUEST_TIMEOUT", 20),
        claude_timeout=env_int("CLAUDE_TIMEOUT", 180),
        claude_http_retries=env_int("CLAUDE_HTTP_RETRIES", os.environ.get("CLAUDE_RETRIES", "2")),
        pubmed_query_timeout=env_int("PUBMED_QUERY_TIMEOUT", 60),
        pubmed_retmax=env_int("PUBMED_RETMAX", 3),
        pubmed_abstract_char_limit=env_int("PUBMED_ABSTRACT_CHAR_LIMIT", 7000),
        claude_model=claude_model,
        claude_script_model=os.environ.get("CLAUDE_SCRIPT_MODEL", claude_model),
        claude_query_model=os.environ.get("CLAUDE_QUERY_MODEL", claude_model),
        claude_strategy_model=os.environ.get("CLAUDE_STRATEGY_MODEL", "claude-3-5-haiku-latest"),
        claude_strategy_fallback_models=env_csv("CLAUDE_STRATEGY_FALLBACK_MODELS", ("claude-3-5-haiku-20241022",)),
        max_tokens=env_int("MAX_TOKENS", 2600),
        enable_web_research=env_bool("ENABLE_WEB_RESEARCH", True),
        web_research_timeout=env_int("WEB_RESEARCH_TIMEOUT", 120),
        strategy_path=os.environ.get("STRATEGY_PATH", os.path.join(work_dir, "strategy.json")),
        insights_path=os.environ.get("FEEDBACK_INSIGHTS", os.path.join(data_dir, "feedback_insights.json")),
        total_chars=total_chars,
        prompt_target_chars=prompt_target_chars,
        min_scenes_estimate=min_scenes_estimate,
    )